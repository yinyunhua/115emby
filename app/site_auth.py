"""站点登录：**取代 nginx 的 Basic 弹窗**，口令存在服务端、可在后台修改。

设计
----
* 口令：``PBKDF2-HMAC-SHA256``（20 万次迭代 + 随机盐）存在 ``data/site_auth.json``，
  **不存明文**；文件权限 600。
* 会话：HttpOnly + Secure + SameSite=Lax 的 ``emby_site`` Cookie，
  值 = ``base64(payload) + "." + HMAC(secret, payload)``，
  payload = ``{"u": 用户名, "exp": 过期时间, "ep": 会话代次}``。
  Cookie 里没有口令，改口令靠**代次 +1** 让所有旧会话立刻失效。
* 默认 30 天（勾了"记住我"），否则 12 小时。
* 登录失败限速：同 IP 连续 5 次错 → 锁 120 秒（内存计数，重启即清）。
* 免登录白名单：登录页与登录接口、``/p/*`` ``/pl/*``（外部播放器的 HMAC token 直通）、
  本机访问 ``/api/health``（容器 healthcheck 要用）。

接口
----
* ``GET  /login``                  登录页（static/login.html）
* ``POST /api/login``              ``{"password": "...", "remember": true}``
* ``GET  /api/logout``             清 Cookie 回登录页
* ``GET  /api/site/state``         当前登录状态（给前端显示用）
* ``POST /api/site/password``      ``{"new": "...", "old": "..."}`` 改口令（要已登录）

改口令也可以用命令行（忘记口令时的救急）：
``docker exec 115emby-api python -m app.site_auth setpw <新口令>``
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import logging

COOKIE = "emby_site"
log = logging.getLogger("album.site_auth")

ITER = 200_000
TTL_REMEMBER = 60 * 60 * 24 * 30      # 30 天
TTL_SHORT = 60 * 60 * 12              # 12 小时
MAX_FAILS = 5
LOCK_SECONDS = 120

STATIC = Path(__file__).resolve().parent / "static"
_FAILS: dict[str, list] = {}          # ip -> [失败次数, 解锁时间戳, 最后活动]
_FAILS_MAX = 5000                     # 上限（防止伪造 IP 撑爆内存）
_FAILS_TTL = 60 * 60 * 6              # 6 小时没活动就丢弃


def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR") or "/app/data")

def _store_path() -> Path:
    return Path(os.getenv("SITE_AUTH_FILE") or (_data_dir() / "site_auth.json"))

_LOAD_CACHE: dict = {"mtime": None, "data": {}}


def _load() -> dict:
    """读站点凭据文件。**每个请求**都会走这里（_site_guard 必经），
    所以按 mtime 做进程内缓存 —— 原先每次都 read_text+json.loads，
    同步 I/O 写在 async 中间件里会阻塞事件循环。"""
    p = _store_path()
    try:
        st = p.stat()
        if _LOAD_CACHE["mtime"] == st.st_mtime_ns:
            return _LOAD_CACHE["data"]
        data = json.loads(p.read_text(encoding="utf-8"))
        _LOAD_CACHE["mtime"] = st.st_mtime_ns
        _LOAD_CACHE["data"] = data
        return data
    except Exception:                                  # noqa: BLE001
        return _LOAD_CACHE["data"] or {}


def _save(d: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:                                  # noqa: BLE001
        pass
    tmp.replace(p)
    _LOAD_CACHE["mtime"] = None          # 让缓存失效


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, ITER).hex()

def has_password() -> bool:
    return bool(_load().get("hash"))

def set_password(pw: str, user: str = "") -> dict:
    """设置/修改口令。代次 +1 → 所有旧会话失效。"""
    pw = (pw or "").strip()
    if len(pw) < 4:
        raise ValueError("口令太短（至少 4 位）")
    d = _load()
    salt = secrets.token_bytes(16)
    d.update({
        "v": 1,
        "user": (user or d.get("user") or "admin"),
        "salt": salt.hex(),
        "hash": _hash(pw, salt),
        "iter": ITER,
        "secret": d.get("secret") or secrets.token_hex(32),
        "epoch": int(d.get("epoch") or 0) + 1,
        "changed_at": int(time.time()),
    })
    _save(d)
    return d

def verify(pw: str) -> bool:
    d = _load()
    if not d.get("hash"):
        return False
    try:
        salt = bytes.fromhex(d["salt"])
    except Exception:                                  # noqa: BLE001
        return False
    return hmac.compare_digest(_hash((pw or "").strip(), salt), d["hash"])

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def _b64d(tok: str) -> bytes:
    return base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))

def make_session(ttl: int, user: str = "") -> str:
    """★ req-accounts：会话里带**账户名**，普通账户与管理员用同一套 Cookie 机制，
    靠 payload 的 ``u`` 区分，靠 ``ep``（该账户的代次）让改口令/停用立即失效。"""
    from . import accounts as _acc
    d = _load()
    u = (user or "").strip() or _acc.admin_user()
    payload = json.dumps({"u": u, "exp": int(time.time()) + int(ttl),
                          "ep": _acc.epoch_of(u)},
                         separators=(",", ":")).encode()
    sig = hmac.new((d.get("secret") or "").encode(), payload, hashlib.sha256).digest()[:20]
    return _b64e(payload) + "." + _b64e(sig)

def session_user(request: Request) -> str:
    """返回会话里的账户名；无效会话返回 ``""``。

    ★ req-accounts：原来只校验**全局** epoch，现在按**该账户自己的** epoch 校验 ——
    管理员改口令不会踢掉普通账户，普通账户被改口令/停用只踢它自己。
    """
    tok = (request.cookies.get(COOKIE) or "").strip()
    if "." not in tok:
        return ""
    d = _load()
    if not d.get("secret"):
        return ""
    try:
        p64, s64 = tok.split(".", 1)
        payload, sig = _b64d(p64), _b64d(s64)
        want = hmac.new(d["secret"].encode(), payload, hashlib.sha256).digest()[:20]
        if not hmac.compare_digest(sig, want):
            return ""
        js = json.loads(payload)
        if int(js.get("exp") or 0) < time.time():
            return ""
        u = str(js.get("u") or "").strip()
        if not u:
            return ""
        from . import accounts as _acc
        if int(js.get("ep") or 0) != _acc.epoch_of(u):
            return ""                                     # 该账户改过口令/被停用 → 旧会话作废
        # 账户可能已被删除或停用
        if u != _acc.admin_user():
            a = _acc.get_account(u)
            if not a or not bool(a.get("enabled", True)):
                return ""
        return u
    except Exception:                                  # noqa: BLE001
        return ""

def check_session(request: Request) -> bool:
    return bool(session_user(request))

def is_admin(request: Request) -> bool:
    """当前会话是不是**管理员**。普通账户一律 False。"""
    u = session_user(request)
    if not u:
        return False
    from . import accounts as _acc
    return u == _acc.admin_user()

# ★ 只有确实在可信反向代理之后，才允许信 X-Forwarded-For / X-Real-IP。
#   实测本服务前面**没有**反向代理（ss 只有 docker-proxy 直连），而 XFF 完全由客户端可控
#   → 伪造 10.x 就能绕过登录（/api/health 曾可无凭据 200），也能轮换 IP 绕过登录限速。
_TRUST_PROXY = (os.getenv("TRUST_PROXY_HEADERS", "0").strip().lower()
                in ("1", "true", "yes", "on"))


def _client_ip(request: Request) -> str:
    if _TRUST_PROXY:
        for h in ("x-real-ip", "x-forwarded-for"):
            v = (request.headers.get(h) or "").strip()
            if v:
                return v.split(",")[0].strip()
    return (request.client.host if request.client else "") or "?"


def _is_local(request: Request) -> bool:
    ip = _client_ip(request)
    return ip in ("127.0.0.1", "::1", "localhost", "") or ip.startswith("172.1") or ip.startswith("10.")

def _locked(ip: str) -> int:
    rec = _FAILS.get(ip)
    if not rec:
        return 0
    if rec[1] > time.time():
        return int(rec[1] - time.time())
    return 0

def _prune_fails() -> None:
    """清掉过期/超量记录（每次写失败记录时顺手做；n 有上限，O(n) 可接受）。"""
    now = time.time()
    for k in [k for k, v in _FAILS.items()
              if len(v) > 2 and now - v[2] > _FAILS_TTL and v[1] < now]:
        _FAILS.pop(k, None)
    if len(_FAILS) > _FAILS_MAX:
        keep = _FAILS_MAX // 2
        for k, _ in sorted(_FAILS.items(),
                           key=lambda kv: kv[1][2] if len(kv[1]) > 2 else 0)[:len(_FAILS) - keep]:
            _FAILS.pop(k, None)


def _note_fail(ip: str) -> None:
    rec = _FAILS.setdefault(ip, [0, 0, time.time()])
    while len(rec) < 3:
        rec.append(time.time())
    rec[0] += 1
    rec[2] = time.time()
    if rec[0] >= MAX_FAILS:
        rec[1] = time.time() + LOCK_SECONDS
        rec[0] = 0
    _prune_fails()


def _clear_fail(ip: str) -> None:
    _FAILS.pop(ip, None)

# 免登录前缀（外部播放器的 /p /pl 靠 handoff 自己的 HMAC token 校验）
PUBLIC_PREFIX = ("/login", "/api/login", "/api/logout", "/p/", "/pl/",
                 "/favicon.ico", "/static/login",
                 # 登录页要用它判断"是否已登录 / 是否设过口令"，只返回这两个布尔值，不含任何秘密
                 "/api/site/state",
                 # ★ req-setup：首次部署设置管理口令（未设口令时登录页要用）
                 "/api/setup",
                 # req-extcheck：自检页用来判断扩展是否生效，不该先登录
                 "/static/_extcheck.html"
                 )

def install(app) -> None:
    @app.middleware("http")
    async def _site_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith(PUBLIC_PREFIX):
            return await call_next(request)
        if path == "/api/health" and _is_local(request):     # 容器 healthcheck
            return await call_next(request)
        if check_session(request):
            return await call_next(request)
        # /admin/xxx 是后台的 JSON 接口（/admin 本身是页面）→ 也回 401 而不是跳转，
        # 否则前端拿到一个 HTML 登录页会很难排查
        _json_api = (path.startswith("/api/") or path.startswith("/admin/")
                     or "application/json" in (request.headers.get("accept") or ""))
        if _json_api:
            return JSONResponse({"detail": "未登录", "login": "/login"}, status_code=401,
                                headers={"Cache-Control": "no-store"})
        nxt = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(nxt, safe=""), status_code=302,
                                headers={"Cache-Control": "no-store"})

    @app.get("/login", include_in_schema=False)
    async def login_page():
        p = STATIC / "login.html"
        if not p.exists():                                     # pragma: no cover
            return HTMLResponse("<h1>login.html 缺失</h1>", status_code=500)
        return HTMLResponse(p.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-cache"})

    @app.post("/api/login")
    async def do_login(request: Request, body: dict = Body(...)):
        ip = _client_ip(request)
        left = _locked(ip)
        if left:
            return JSONResponse({"ok": False, "detail": "尝试过于频繁，请 %d 秒后再试" % left},
                                status_code=429)
        pw = str((body or {}).get("password") or "")
        uname = str((body or {}).get("user") or "").strip()
        if not has_password():
            return JSONResponse({"ok": False, "detail": "服务端还没设置口令，请在服务器上执行 "
                                                        "python -m app.site_auth setpw <口令>"},
                                status_code=409)
        # ★ req-accounts：多账户登录。uname 为空或等于管理员名 → 管理员。
        from . import accounts as _acc
        who = _acc.verify_login(uname, pw)
        if not who:
            _note_fail(ip)
            n = _FAILS.get(ip, [0, 0])[0]
            tip = "口令不正确" if uname else "口令不正确"
            if n:
                tip += "（还可试 %d 次）" % max(0, MAX_FAILS - n)
            return JSONResponse({"ok": False, "detail": tip}, status_code=401)
        _clear_fail(ip)
        ttl = TTL_REMEMBER if (body or {}).get("remember", True) else TTL_SHORT
        resp = JSONResponse({"ok": True, "ttl": ttl, "user": who["u"],
                             "admin": bool(who.get("admin"))})
        resp.set_cookie(COOKIE, make_session(ttl, who["u"]), max_age=ttl, httponly=True,
                        samesite="lax", secure=True, path="/")
        return resp

    @app.get("/api/logout", include_in_schema=False)
    async def do_logout():
        resp = RedirectResponse("/login", status_code=303,
                                headers={"Cache-Control": "no-store"})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/setup")
    async def do_setup(request: Request, body: dict = Body(...)):
        """★ req-setup：首次部署时**在网页上**设置管理口令。

        以前未设口令时登录直接返回 409，要求在服务器上跑
        ``python -m app.site_auth setpw <口令>`` —— 对新手部署太不友好。

        安全约束：
        * 只在 ``has_password()`` 为假时可用（设过就永久关闭，返回 409）；
        * 若设了环境变量 ``SETUP_TOKEN``，请求必须带上 ``setup_token`` 且一致
          （公网部署建议设置，避免被人抢先设口令）；
        * 口令至少 8 位（比改口令的 4 位严，因为这是唯一一次无凭据入口）；
        * 成功后自动建管理员账户、签发会话，直接进站。
        """
        if has_password():
            return JSONResponse({"ok": False, "detail": "已经设置过口令，请直接登录"},
                                status_code=409)
        want = (os.getenv("SETUP_TOKEN") or "").strip()
        got = str((body or {}).get("setup_token") or "").strip()
        if want and not hmac.compare_digest(got, want):
            return JSONResponse({"ok": False, "detail": "部署口令（SETUP_TOKEN）不正确"},
                                status_code=403)
        pw = str((body or {}).get("password") or "")
        cf = str((body or {}).get("confirm") or "")
        if len(pw) < 8:
            return JSONResponse({"ok": False, "detail": "口令至少 8 位"}, status_code=400)
        if pw != cf:
            return JSONResponse({"ok": False, "detail": "两次输入的口令不一致"}, status_code=400)
        try:
            d = set_password(pw, user="admin")
        except ValueError as exc:
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
        # 管理员就是 site_auth.json 的**顶层**（accounts.py 里 admin 是隐式的），
        # 不需要也不应该再建一个普通账户。
        _who = d.get("user") or "admin"
        log.warning("首次部署：已通过网页设置管理口令（用户 %s）", _who)
        resp = JSONResponse({"ok": True, "user": _who, "admin": True, "ttl": TTL_REMEMBER})
        resp.set_cookie(COOKIE, make_session(TTL_REMEMBER, _who), max_age=TTL_REMEMBER,
                        httponly=True, samesite="lax", secure=True, path="/")
        return resp

    @app.get("/api/site/state")
    async def site_state(request: Request):
        d = _load()
        _u = session_user(request)
        return JSONResponse({"ok": True, "logged_in": bool(_u), "user": _u,
                             "admin": is_admin(request),
                             "has_password": has_password(),
                             # req-setup：告诉登录页"要不要部署口令"，不要泄露口令本身
                             "need_setup_token": bool((os.getenv("SETUP_TOKEN") or "").strip()),
                             "user": d.get("user") or "",
                             "changed_at": int(d.get("changed_at") or 0)})

    @app.post("/api/site/password")
    async def change_password(request: Request, body: dict = Body(...)):
        """改站点口令：必须已登录，且要么给对旧口令、要么带后台令牌。"""
        if not check_session(request):
            return JSONResponse({"ok": False, "detail": "请先登录"}, status_code=401)
        old = str((body or {}).get("old") or "")
        new = str((body or {}).get("new") or "")
        tok = (request.headers.get("x-admin-token") or "").strip()
        admin_tok = (os.getenv("ADMIN_TOKEN") or "").strip()
        ok_admin = bool(admin_tok) and hmac.compare_digest(tok, admin_tok)
        if not ok_admin and not verify(old):
            return JSONResponse({"ok": False, "detail": "当前口令不正确"}, status_code=401)
        try:
            set_password(new, user=_load().get("user") or "admin")
        except ValueError as exc:
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
        # 代次已 +1：旧 Cookie 全部失效，直接给当前浏览器发一张新的，免得自己被踢
        ttl = TTL_REMEMBER
        resp = JSONResponse({"ok": True, "note": "已改口令，其它设备需要重新登录"})
        resp.set_cookie(COOKIE, make_session(ttl), max_age=ttl, httponly=True,
                        samesite="lax", secure=True, path="/")
        return resp

    app.state.site_auth = {"check": check_session, "set_password": set_password,
                           "verify": verify, "cookie": COOKIE}

if __name__ == "__main__":                                  # pragma: no cover
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] in ("setpw", "set"):
        d = set_password(sys.argv[2], user=(sys.argv[3] if len(sys.argv) > 3 else "admin"))
        print("已设置站点口令（代次 %d），所有旧会话已失效" % d["epoch"])
    elif len(sys.argv) >= 3 and sys.argv[1] == "check":
        print("口令正确" if verify(sys.argv[2]) else "口令不正确")
    else:
        print(__doc__)
