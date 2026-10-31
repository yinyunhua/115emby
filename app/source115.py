"""115 目录列表适配器（给"可视化选目录"用）。

只做一件事：**给一个 cid，返回它的子目录和文件**，顺带带回面包屑（115 自己会给 ``path``）。

不碰的东西
----------
* 不抓缩略图、不取直链、不下载任何文件字节 —— 纯目录列表；
* 每次调用都应在调用方（Scanner / Server）的 ``Throttle`` 之后执行，
  因为同一 115 账号可能还有别的客户端在用。

凭据
----
沿用 115-album 的两个环境变量：``COOKIE_115``（网页 cookie）或
``ACCESS_TOKEN_115``（开放平台 Bearer）。两者都没有时 ``ready()`` 返回 False，
``/api/dirs`` 会退化成"只显示已扫描入库的目录"。

响应形状（解析成统一的内部结构）::

    {"folders": [{"id": "<cid>", "name": "电影"}],
     "files":   [{"id": "<fid>:<pickcode>", "name": "a.mkv", "size": 123,
                  "mtime": 1700000000, "ext": "mkv"}],
     "path":    [{"cid": "0", "name": "根目录"}, {"cid": "123", "name": "电影"}]}
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("album.source115")

WEBAPI_BASES = [
    os.getenv("API_BASE_115", "").strip() or "https://webapi.115.com",
    "https://web.api.115.com",
    "https://proapi.115.com",
]
UA = os.getenv("UA") or "Mozilla/5.0 115Browser/27.0.5.7"
# CDN 取流专用 UA。115 的 CDN 现在会直接把 "115Browser/..." 这类客户端 UA 挡掉
# （2026-10-21 实测：签发与取流都用 115Browser 一律 403，普通浏览器 UA / VLC / 空 UA 全 206；
#  且该 UA 签发的直链对任何取流 UA 都是 403）。所以服务端下载/抓帧必须用浏览器 UA，
# 114 的 webapi/proapi 接口 UA 不受影响，继续用 UA。
CDN_UA = (os.getenv("CDN_UA") or
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
PAGE_LIMIT = int(os.getenv("SCAN_PAGE_LIMIT") or 1000)
MAX_PAGES = int(os.getenv("SCAN_MAX_PAGES") or 20)


def cookie() -> str:
    return (os.getenv("COOKIE_115") or "").strip()


def access_token() -> str:
    return (os.getenv("ACCESS_TOKEN_115") or "").strip()


def ready() -> bool:
    return bool(cookie() or access_token())


def _headers() -> dict[str, str]:
    h = {"User-Agent": UA, "Referer": "https://115.com/", "Accept": "application/json"}
    if cookie():
        h["Cookie"] = cookie()
    if access_token():
        h["Authorization"] = f"Bearer {access_token()}"
    return h


def _to_int(value, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value or "").strip()
        if not text:
            return default
        return int(float(text)) if "." in text else int(text)
    except (TypeError, ValueError):
        return default


def _to_epoch(value) -> int:
    """115 的 `t` 有时是 epoch，有时是 '2026-05-25 16:11' 这种字符串 —— 两种都吃。"""
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)) or str(value).isdigit():
        return _to_int(value)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            import datetime as _dt

            return int(_dt.datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return 0


def _is_dir(entry: dict) -> bool:
    """115 的目录项 ``fid`` 为 0，``cid`` 是它自己的 id。"""
    fid = str(entry.get("fid", entry.get("file_id", "0")) or "0")
    return fid in ("0", "")


def _ext_of(entry: dict) -> str:
    ico = str(entry.get("ico") or "").strip().lower()
    if ico and ico not in ("folder", "dir"):
        return ico.lstrip(".")
    name = str(entry.get("n") or entry.get("name") or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def uid() -> str:
    """从 Cookie 里取 UID（115 直链的 payload 需要它）。"""
    for part in cookie().split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip().upper() == "UID":
            return v.strip()
    return ""


def pickcode_of(item: dict) -> str:
    """item 的 id 形如 ``<fid>:<pickcode>``；也兼容单独的 pickcode 字段。"""
    pc = str(item.get("pickcode") or item.get("pc") or "").strip()
    if pc:
        return pc
    raw = str(item.get("id") or "")
    if ":" in raw:
        return raw.split(":", 1)[1].strip()
    return ""


def _expires_from_url(url: str) -> int:
    """115 CDN 直链自带 ``t=<epoch>`` 过期参数，直接读它。"""
    try:
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(url).query)
        for key in ("t", "expires", "e"):
            if key in q and str(q[key][0]).isdigit():
                return int(q[key][0])
    except Exception:  # noqa: BLE001
        pass
    return 0


def _find_url(obj: Any, depth: int = 0) -> str:
    """从解密后的响应里挖出真正的下载地址（结构随版本变，做宽松查找）。"""
    if depth > 6:
        return ""
    if isinstance(obj, str):
        return obj if obj.startswith("http") else ""
    if isinstance(obj, dict):
        for key in ("url", "download_url", "file_url"):
            v = obj.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            got = _find_url(v, depth + 1)
            if got:
                return got
    if isinstance(obj, list):
        for v in obj:
            got = _find_url(v, depth + 1)
            if got:
                return got
    return ""


async def direct_link(item: dict, *, ua: str = "", client: Any = None) -> tuple[str, int, str]:
    """取 115 直链。返回 (url, expires_epoch, signing_ua)。

    * payload = ``{"pickcode": ...}`` 用一次性会话密钥加密后放进表单 ``data``；
    * 响应里的 ``data`` 也是密文，用同一会话密钥解；
    * **直链绑定签发时的 UA** —— 签发用哪个 UA，取流就必须用哪个；
    * 顺序回退 proapi → appapi → webapi（AList 以 proapi 为主）。
    """
    import json
    import time

    import httpx

    from . import v115_crypto as crypto

    pc = pickcode_of(item)
    if not pc:
        raise RuntimeError("缺少 pickcode，无法取直链")
    signing_ua = (ua or UA).strip() or UA
    if not ready():
        raise RuntimeError("没有 115 凭据")

    # 实测（2026-10）：proapi 可用（会回 302，需跟随跳转）；appapi DNS 已不可解析；
    # webapi 的 /app/chrome/downurl 返回 404 错误页 —— 所以按这个顺序回退。
    bases = [b for b in (
        os.getenv("PRO_APP_BASE_115", "").strip() or "https://proapi.115.com",
        "https://webapi.115.com",
        os.getenv("APP_BASE_115", "").strip() or "https://appapi.115.com",
    ) if b]

    own = client is None
    # 必须跟随跳转：proapi 会先回 302 再到真正的接口
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0),
                                         follow_redirects=True)
    last_err = ""
    try:
        for base in bases:
            headers0 = dict(_headers())
            headers0["User-Agent"] = signing_ua        # 直链绑定这个 UA

            # ---- 途径①：网页版下载接口（2026-10-22 新增）
            # 实测当天：appapi.115.com 已从 DNS 撤掉（Name or service not known）、
            # proapi 的 /app/chrome/downurl 返回 405、webapi 的同路径返回 404，
            # 于是老路子全废、/api/image 一律 502（前端表现就是图片变问号）。
            # 只有这个 GET 还正常：直接返回 file_url。
            try:
                r1 = await client.get(f"{base}/files/download",
                                      params={"pickcode": pc}, headers=headers0)
                if r1.status_code == 200:
                    try:
                        j1 = r1.json()
                    except Exception:  # noqa: BLE001
                        j1 = {}
                    if isinstance(j1, dict):
                        u1 = str(j1.get("file_url") or j1.get("url") or "")
                        if u1:
                            e1 = _expires_from_url(u1) or (int(time.time()) + 1800)
                            return u1, e1, signing_ua
                        if j1.get("state") is False:
                            last_err = (f"{base}/files/download: "
                                        f"{j1.get('msg') or j1.get('errno') or 'state=false'}")
                        else:
                            last_err = f"{base}/files/download: 响应里没有 file_url"
                    else:
                        last_err = f"{base}/files/download: 响应非对象"
                else:
                    last_err = f"{base}/files/download: HTTP {r1.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_err = f"{base}/files/download: {exc}"

            # ---- 途径②：老的加密 POST（保留兜底）
            session_key = crypto.generate_key()
            form = {"data": crypto.encode(json.dumps({"pickcode": pc}).encode("utf-8"),
                                          session_key)}
            headers = dict(_headers())
            headers["User-Agent"] = signing_ua        # 直链绑定这个 UA
            try:
                resp = await client.post(f"{base}/app/chrome/downurl",
                                         params={"t": str(int(time.time()))},
                                         data=form, headers=headers)
            except Exception as exc:  # noqa: BLE001
                last_err = f"{base}: {exc}"
                continue

            if resp.status_code != 200:
                last_err = f"{base}: HTTP {resp.status_code}"
                continue
            text = resp.text or ""
            if "tengine" in text.lower():             # WAF 页面
                last_err = f"{base}: 命中 WAF"
                continue
            try:
                js = resp.json()
            except Exception:  # noqa: BLE001
                last_err = f"{base}: 响应非 JSON"
                continue
            if isinstance(js, dict) and js.get("state") is False:
                last_err = f"{base}: {js.get('error') or js.get('message') or js.get('errno')}"
                continue

            url = ""
            payload = js.get("data") if isinstance(js, dict) else None
            if isinstance(payload, str) and payload:
                try:
                    plain = crypto.decode(payload, session_key)
                    url = _find_url(json.loads(plain.decode("utf-8", "replace")))
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{base}: 解密失败 {exc}"
            elif isinstance(payload, (dict, list)):
                url = _find_url(payload)              # 少数场景直接给明文
            else:
                url = _find_url(js)

            if url:
                exp = _expires_from_url(url)
                if exp <= 0:
                    exp = int(time.time()) + 1800
                return url, exp, signing_ua
            last_err = f"{base}: 响应里没有 URL"
    finally:
        if own:
            await client.aclose()

    raise RuntimeError(f"取直链失败：{last_err}")


async def list_dir(cid: str = "0", *, client: Any = None) -> dict:
    """列一个目录（分页拉全；受 SCAN_MAX_PAGES 保护，避免超大目录失控）。"""
    import httpx

    cid = str(cid or "0")
    if cid == "-1":                     # 内部用 -1 表示根，115 用 0
        cid = "0"

    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0),
                                         follow_redirects=True)
    folders: list[dict] = []
    files: list[dict] = []
    crumbs: list[dict] = []
    offset = 0
    pages = 0
    last_err = ""

    try:
        while pages < MAX_PAGES:
            params = {"cid": cid, "offset": offset, "limit": PAGE_LIMIT,
                      "show_dir": 1, "aid": 1, "o": "user_ptime", "asc": 0,
                      "natsort": 1, "format": "json"}
            js: dict | None = None
            for base in WEBAPI_BASES:
                try:
                    resp = await client.get(f"{base}/files", params=params, headers=_headers())
                    if resp.status_code != 200:
                        last_err = f"HTTP {resp.status_code} @ {base}"
                        continue
                    data = resp.json()
                    if not isinstance(data, dict):
                        last_err = f"非 JSON @ {base}"
                        continue
                    if "state" in data and not data.get("state"):
                        last_err = f"{base}: {data.get('error') or data.get('message') or '拒绝'}"
                        continue
                    js = data
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{base}: {exc}"
                    continue
            if js is None:
                raise RuntimeError(f"列目录失败 cid={cid}：{last_err}")

            entries = js.get("data") or []
            if not isinstance(entries, list):      # req-scanfix：极少数情况 data 是 dict
                entries = []
            if not crumbs and js.get("path"):
                crumbs = [{"cid": str(p.get("cid") or "0"), "name": p.get("name") or ""}
                          for p in js["path"]]
            for e in entries:
                # ★ req-scanfix：115 的 data 数组里**可能混进 null 元素**，
                #   不判空就是 "AttributeError: 'NoneType' object has no attribute 'get'"，
                #   那次扫描因此在 6093 个目录处整个崩掉，剩下目录永久没扫。
                if not isinstance(e, dict):
                    continue
                name = str(e.get("n") or e.get("name") or "")
                if not name:
                    continue
                if _is_dir(e):
                    # 目录项也带时间：t/te=创建, tu=修改, tp/to=排序/最近操作
                    # 快速增量扫描靠它判断"这棵子树有没有变过"
                    _times = {}
                    for _k in ("t", "te", "tu", "tp", "to"):
                        _v = e.get(_k)
                        try:
                            if _v not in (None, ""):
                                _times[_k] = int(float(_v))
                        except (TypeError, ValueError):
                            pass
                    folders.append({"id": str(e.get("cid") or e.get("cid_id") or ""),
                                    "name": name,
                                    "mtime": max(_times.values()) if _times else 0,
                                    "times": _times})
                else:
                    fid = str(e.get("fid") or e.get("file_id") or "")
                    pc = str(e.get("pc") or e.get("pick_code") or "")
                    files.append({
                        "id": f"{fid}:{pc}" if pc else fid,
                        "pickcode": pc,
                        "name": name,
                        "size": _to_int(e.get("s") or e.get("size") or 0),
                        "mtime": _to_epoch(e.get("t") or e.get("te")
                                           or e.get("utime") or e.get("user_ptime")),
                        "ext": _ext_of(e),
                        # 115 列表项自带缩略图地址：优先 u，其次 thumb/pic
                        "thumb": str(e.get("u") or e.get("thumb") or e.get("pic") or ""),
                    })
            pages += 1
            total = int(js.get("count") or 0)
            offset += len(entries)
            if not entries or offset >= total:
                break
    finally:
        if own:
            await client.aclose()

    return {"folders": folders, "files": files, "path": crumbs,
            "truncated": pages >= MAX_PAGES}
