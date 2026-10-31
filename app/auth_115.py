"""115 第三方登录：开放平台 PKCE 设备码（主力）+ 网页扫码（无门槛兜底）。

为什么要两条路
--------------
**A. 开放平台 PKCE 设备码（推荐）**
    流程与 fnOS 的 `cloud_storage_dav` 一致（见你的 `REPORT-115读取链路.md` §2.2）::

        POST https://passportapi.115.com/open/authDeviceCode   client_id + code_challenge(S256) + scope
          -> { uid, time, sign, qrcode }
        轮询 https://qrcodeapi.115.com/get/status/             uid + time + sign
          -> 0 待扫码 / 1 已扫码待确认 / 2 已确认 / -1 过期 / -2 取消
        POST https://passportapi.115.com/open/deviceCodeToToken  uid + code_verifier(+ client_id)
          -> { access_token, refresh_token, expires_in }

    优点：不碰网页 Cookie（不会被网页端踢下线），token 48 小时 + refresh 续期。
    **前提：client_id 必须是你在 115 开放平台注册的应用**。公开客户端用 PKCE 不需要
    client_secret，所以不存在"密钥硬编码进客户端"的问题；但不能复用别人的 client_id。

**B. 网页扫码（零门槛）**
    不需要注册应用，扫 115 网页版二维码直接拿 Cookie（`UID/CID/SEID/KID`）。
    115-album 的 ``app/v115_qr.py`` 已经实现了完整长轮询，这里只做适配说明。

安全约定
--------
* token **不明文落库**：写入前用 :mod:`app.secretbox` 加密，再 base64 存进 ``kv``，
  所以 `thumbs.db` 被整盘拷走也拿不到可用 token；
* refresh 失败要**退避**（115 侧对重复刷新会去重甚至熔断，fnOS 日志里
  ``refresh token failed recently`` / ``try to disable storageId`` 就是这个）；
* 任何情况下都不把 token 回传给浏览器（只服务端用）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import secretbox

log = logging.getLogger("album.auth115")

KV_KEY = "115_oauth"
DEFAULT_SCOPE = "files.read photo.read video.read user.read"


class OAuthError(RuntimeError):
    """登录/刷新失败（区分"用户没扫码"与"服务端拒绝"）。"""


# --------------------------------------------------------------------- 端点
def _bases() -> dict[str, str]:
    return {
        "passport": os.getenv("PASSPORT_BASE_115", "https://passportapi.115.com").rstrip("/"),
        "qrcode": os.getenv("QRCODE_BASE_115", "https://qrcodeapi.115.com").rstrip("/"),
    }


def client_id() -> str:
    return (os.getenv("APP_ID_115") or "").strip()


def scope() -> str:
    return (os.getenv("OAUTH_SCOPE_115") or DEFAULT_SCOPE).strip()


# --------------------------------------------------------------------- PKCE
def new_verifier(nbytes: int = 48) -> str:
    """code_verifier：43–128 字符的 URL-safe 随机串。"""
    return secrets.token_urlsafe(nbytes)[:96]


def challenge_of(verifier: str) -> str:
    """code_challenge = BASE64URL(SHA256(verifier))，去掉 padding（S256）。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# --------------------------------------------------------------------- 数据结构
STATUS_WAITING = 0
STATUS_SCANNED = 1
STATUS_ALLOWED = 2
STATUS_EXPIRED = -1
STATUS_CANCELED = -2

STATUS_TEXT = {
    STATUS_WAITING: "等待扫码",
    STATUS_SCANNED: "已扫码，请在手机上确认",
    STATUS_ALLOWED: "已确认，正在换取 token",
    STATUS_EXPIRED: "二维码已过期",
    STATUS_CANCELED: "已在手机上取消",
}


@dataclass(slots=True)
class DeviceCode:
    uid: str
    qrcode: str                      # 二维码内容（交给前端渲染成图）
    sign: str = ""
    issued_at: int = 0
    verifier: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def challenge(self) -> str:
        return challenge_of(self.verifier)


@dataclass(slots=True)
class Token:
    access_token: str
    refresh_token: str = ""
    expires_in: int = 0
    obtained_at: int = 0
    uid: str = ""

    @property
    def expires_at(self) -> int:
        return int(self.obtained_at + max(0, self.expires_in))

    def fresh(self, slack: int = 600) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - slack


# --------------------------------------------------------------------- 加密保管
class TokenVault:
    """token 的加密保管处（复用 thumbs.db 的 kv 表）。"""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._cached: Token | None = None

    def save(self, token: Token) -> None:
        blob = secretbox.encrypt(json.dumps({
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_in": token.expires_in,
            "obtained_at": token.obtained_at or int(time.time()),
            "uid": token.uid,
        }, ensure_ascii=False).encode("utf-8"))
        self.store.kv_set(KV_KEY, base64.b64encode(blob).decode("ascii"))
        self._cached = token

    def load(self) -> Token | None:
        if self._cached is not None:
            return self._cached
        raw = self.store.kv_get(KV_KEY, "")
        if not raw:
            return None
        try:
            blob = base64.b64decode(raw)
            data = json.loads(secretbox.decrypt(blob).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("token 解密失败（换过密钥？）：%s", exc)
            return None
        self._cached = Token(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_in=int(data.get("expires_in") or 0),
            obtained_at=int(data.get("obtained_at") or 0),
            uid=data.get("uid", ""),
        )
        return self._cached

    def clear(self) -> None:
        self.store.kv_set(KV_KEY, "")
        self._cached = None


# --------------------------------------------------------------------- HTTP 客户端
def _default_client():  # pragma: no cover - 仅生产用
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True)


async def _post(client, url: str, data: dict) -> dict:
    resp = await client.post(url, data=data)
    return _unwrap(resp)


async def _get(client, url: str, params: dict) -> dict:
    resp = await client.get(url, params=params)
    return _unwrap(resp)


def _unwrap(resp) -> dict:
    if resp.status_code != 200:
        raise OAuthError(f"HTTP {resp.status_code}")
    try:
        js = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"响应不是 JSON：{exc}") from exc
    if isinstance(js, dict) and "state" in js and not js.get("state"):
        raise OAuthError(f"上游拒绝：{js.get('error') or js.get('message') or js}")
    return js


# --------------------------------------------------------------------- 流程
async def request_device_code(
    cid: str | None = None,
    *,
    client=None,
    verifier: str | None = None,
) -> DeviceCode:
    """领设备码。``verifier`` 只在测试时注入。"""
    cid = (cid or client_id()).strip()
    if not cid:
        raise OAuthError("缺少 APP_ID_115（115 开放平台应用的 client_id）")
    verifier = verifier or new_verifier()
    own = client is None
    client = client or _default_client()
    try:
        js = await _post(client, f"{_bases()['passport']}/open/authDeviceCode", {
            "client_id": cid,
            "code_challenge": challenge_of(verifier),
            "code_challenge_method": "S256",
            "scope": scope(),
        })
    finally:
        if own:
            await client.aclose()

    data = js.get("data") if isinstance(js.get("data"), dict) else js
    uid = str(data.get("uid") or "")
    if not uid:
        raise OAuthError(f"未拿到 uid：{js}")
    return DeviceCode(
        uid=uid,
        qrcode=str(data.get("qrcode") or ""),
        sign=str(data.get("sign") or ""),
        issued_at=int(data.get("time") or time.time()),
        verifier=verifier,
        raw=data,
    )


async def poll_status(code: DeviceCode, *, client=None) -> int:
    own = client is None
    client = client or _default_client()
    try:
        js = await _get(client, f"{_bases()['qrcode']}/get/status/", {
            "uid": code.uid, "time": code.issued_at, "sign": code.sign, "_": int(time.time()),
        })
    finally:
        if own:
            await client.aclose()
    data = js.get("data") if isinstance(js.get("data"), dict) else js
    try:
        return int(data.get("status", STATUS_WAITING))
    except (TypeError, ValueError):
        return STATUS_WAITING


async def exchange_token(code: DeviceCode, cid: str | None = None, *, client=None) -> Token:
    """手机确认后，用 uid + code_verifier 换 token。"""
    cid = (cid or client_id()).strip()
    own = client is None
    client = client or _default_client()
    try:
        js = await _post(client, f"{_bases()['passport']}/open/deviceCodeToToken", {
            "client_id": cid,
            "uid": code.uid,
            "code_verifier": code.verifier,
        })
    finally:
        if own:
            await client.aclose()
    return _token_from(js, uid=code.uid)


def _token_from(js: dict, *, uid: str = "") -> Token:
    data = js.get("data") if isinstance(js.get("data"), dict) else js
    access = str(data.get("access_token") or "")
    if not access:
        raise OAuthError(f"未拿到 access_token：{js}")
    return Token(
        access_token=access,
        refresh_token=str(data.get("refresh_token") or ""),
        expires_in=int(data.get("expires_in") or 0) or 48 * 3600,
        obtained_at=int(time.time()),
        uid=uid or str(data.get("uid") or ""),
    )


async def refresh(token: Token, cid: str | None = None, *, client=None) -> Token:
    """refresh_token 续期；失败抛 OAuthError（调用方需退避，别连刷）。"""
    cid = (cid or client_id()).strip()
    if not token.refresh_token:
        raise OAuthError("没有 refresh_token")
    own = client is None
    client = client or _default_client()
    try:
        js = await _post(client, f"{_bases()['passport']}/open/refreshToken", {
            "client_id": cid, "refresh_token": token.refresh_token,
        })
    finally:
        if own:
            await client.aclose()
    fresh = _token_from(js, uid=token.uid)
    if not fresh.refresh_token:
        fresh.refresh_token = token.refresh_token
    return fresh


class LoginSession:
    """一次完整登录：start → wait → finish，并负责把 token 加密入库。"""

    def __init__(self, vault: TokenVault, *, cid: str | None = None, client=None) -> None:
        self.vault = vault
        self.cid = (cid or client_id()).strip()
        self.client = client
        self.code: DeviceCode | None = None

    async def start(self) -> DeviceCode:
        self.code = await request_device_code(self.cid, client=self.client)
        return self.code

    async def wait(self, *, timeout: float = 300.0, interval: float = 2.0) -> int:
        """长轮询到"已确认/过期/取消"。返回最终状态码。"""
        if self.code is None:
            raise OAuthError("还没 start()")
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            st = await poll_status(self.code, client=self.client)
            if st in (STATUS_ALLOWED, STATUS_EXPIRED, STATUS_CANCELED):
                return st
            await asyncio.sleep(max(0.5, interval))
        return STATUS_EXPIRED

    async def finish(self) -> Token:
        if self.code is None:
            raise OAuthError("还没 start()")
        token = await exchange_token(self.code, self.cid, client=self.client)
        self.vault.save(token)
        log.info("115 授权成功（uid=%s，有效期 %ss）", token.uid[:8], token.expires_in)
        return token


async def ensure_token(vault: TokenVault, *, cid: str | None = None, client=None) -> Token:
    """取一个可用的 access_token：库里有效就直接用，快过期就 refresh。"""
    token = vault.load()
    if token is None:
        raise OAuthError("尚未登录（先跑扫码/设备码流程）")
    if token.fresh():
        return token
    refreshed = await refresh(token, cid, client=client)
    vault.save(refreshed)
    return refreshed
