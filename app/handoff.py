"""免密直通：给外部播放器用的 token 链接（服务端只做 302，不传媒体字节）。

为什么需要
----------
播放器（VLC / mpv / nPlayer / Infuse / MX）没法带网关 Basic 认证，而且 115 直链
**绑定签发时的 UA**。所以给出 ``/p/<token>``：

1. 播放器请求 ``/p/<token>``，**用他自己的 UA**；
2. 我们先校验 token（HMAC + 过期），再用**这个 UA** 去 115 签发直链；
3. 回 302，播放器用同一 UA 跟随 → 206。

这样任何播放器、任何格式都能播，服务器只回几百字节。

curl 用法
---------
    curl "https://host/p/<token>"          → 302 → 115 CDN
    curl "https://host/pl/<token>.m3u"     → 播放列表（内含 /p/<token>）
"""
import base64
import hashlib
import hmac
import os
import re
import sqlite3
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import index_crypto as ic
from . import source115

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"
SECRET = (os.getenv("HANDOFF_SECRET") or os.getenv("ADMIN_TOKEN") or "115emby-dev").encode()
TTL = int(os.getenv("HANDOFF_TTL") or 6 * 3600)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(item_id: str, ttl: int = TTL) -> str:
    payload = ("%s|%d" % (item_id, int(time.time()) + int(ttl))).encode()
    sig = hmac.new(SECRET, payload, hashlib.sha256).digest()[:16]
    return _b64(payload) + "." + _b64(sig)


def verify_token(token: str) -> str:
    try:
        p, s = token.split(".", 1)
        payload, sig = _unb64(p), _unb64(s)
    except Exception:
        return ""
    want = hmac.new(SECRET, payload, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(sig, want):
        return ""
    try:
        item_id, exp = payload.decode().rsplit("|", 1)
        if int(exp) < time.time():
            return ""
    except Exception:
        return ""
    return item_id


def _base(request) -> str:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host")
            or "127.0.0.1:17867")
    proto = request.headers.get("x-forwarded-proto") or "https"
    return proto + "://" + host


# 115 CDN 会直接 403 的客户端 UA（2026-10-21 实测：这类 UA 无论用哪个 UA 签发都取不到流，
# 因为 CDN 端就把它拒了；浏览器 UA / VLC / 空 UA 全是 206）。可用环境变量覆盖。
BLOCKED_UA_RE = re.compile(os.getenv("PASS_BLOCKED_UA_RE") or r"115browser", re.I)


def _blocked_ua(ua: str) -> bool:
    try:
        return bool(BLOCKED_UA_RE.search(ua or ""))
    except Exception:  # noqa: BLE001
        return False


def _blocked_html(base: str, token: str, item: dict, ua: str) -> str:
    """给"客户端 UA 被 CDN 拉黑"的情况一个能看懂、能自救的页面（不代理任何字节）。"""
    import html as _html

    name = _html.escape(item.get("name") or item.get("id") or "")
    ua_e = _html.escape(ua[:120])
    purl = "%s/p/%s" % (base, token)
    m3u = "%s/pl/%s.m3u" % (base, token)
    return ("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>换个浏览器打开 · 115emby</title>
<style>
 body{margin:0;background:#0b0d12;color:#e9edf5;font:15px/1.7 -apple-system,BlinkMacSystemFont,
   "Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;padding:28px 18px}
 .w{max-width:620px;margin:0 auto}
 h1{font-size:18px;margin:0 0 6px}
 p{color:#aab4c4;margin:10px 0}
 .n{color:#e9edf5;font-weight:600;word-break:break-all}
 code{display:block;word-break:break-all;background:#151922;border:1px solid #232936;
   border-radius:9px;padding:10px 12px;font:12.5px/1.6 ui-monospace,Consolas,monospace;color:#b9c2d6}
 a.btn{display:inline-block;margin:14px 10px 0 0;background:#4c8dff;color:#fff;text-decoration:none;
   border-radius:9px;padding:10px 16px;font-size:14px}
 a.gh{background:#1b2130;color:#cfd7e6}
 .tip{font-size:12.5px;color:#7d8798;margin-top:18px}
</style></head><body><div class="w">
<h1>这个浏览器取不到视频流</h1>
<p class="n">%s</p>
<p>你的客户端 UA 里带了 <b>115Browser</b>，115 的 CDN 会直接拒绝这类 UA（返回 403，
和文件本身无关：同一个文件用 Safari / Chrome / VLC 打开都是正常的 206）。</p>
<p>它不是"链接过期"，也不是编码问题，所以重试多少次都一样。三条出路，任选一条：</p>
<p><b>① 换系统浏览器打开</b>（Safari / Chrome / Edge / 夸克 / Via 都行）—— 直接复制本页地址即可。</p>
<p><b>② 丢给本地播放器</b>（VLC / nPlayer / Infuse / MX Player）—— 播放器用它自己的 UA 取流，不受影响：</p>
<code>%s</code>
<p><b>③ 下载播放列表</b>，双击即用默认播放器打开：</p>
<a class="btn" href="%s">下载 .m3u</a>
<a class="btn gh" href="%s">重新尝试</a>
<p class="tip">当前 UA：%s<br>服务端只做 302 跳转，不转发任何视频字节。</p>
</div></body></html>""" % (name, purl, m3u, purl, ua_e))


def _item(item_id: str) -> dict:
    con = sqlite3.connect(EMBY, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT id, name_enc, ext, size, kind FROM items WHERE id=?",
                          (item_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return {}
    return {
        "id": row["id"],
        "pickcode": source115.pickcode_of({"id": row["id"]}),
        "ext": (row["ext"] or "").lower(),
        "size": row["size"] or 0,
        "kind": row["kind"] or "",
        "name": ic.safe_unseal(row["name_enc"] or ""),
    }


def install(app) -> None:
    @app.get("/p/{token}", include_in_schema=False)
    async def pass_through(token: str, request: Request):
        item_id = verify_token(token)
        if not item_id:
            raise HTTPException(403, "链接无效或已过期")
        item = _item(item_id)
        if not item:
            raise HTTPException(404, "条目不存在")
        ua = request.headers.get("user-agent") or "Mozilla/5.0"
        if _blocked_ua(ua):
            # 这类 UA 被 115 CDN 拉黑：无论用哪个 UA 签发，带着它的请求都会被 403。
            # 唯一靠谱的处理是把原因和替代入口讲清楚（服务器不代理视频字节）。
            return HTMLResponse(_blocked_html(_base(request), token, item, ua),
                                status_code=200)
        url, expires, _signed = await source115.direct_link(item, ua=ua)

        return RedirectResponse(url, status_code=302, headers={
            "X-Link-Expires": str(expires),
            "Cache-Control": "no-store",
        })

    @app.get("/pl/{token}.m3u", include_in_schema=False)
    async def playlist(token: str, request: Request):
        item_id = verify_token(token)
        if not item_id:
            raise HTTPException(403, "链接无效或已过期")
        item = _item(item_id)
        base = _base(request)
        name = (item.get("name") or item_id).replace("\n", " ")
        body = "#EXTM3U\n#EXTINF:-1,%s\n%s/p/%s\n" % (name, base, token)
        return Response(body.encode("utf-8"),
                        media_type="audio/x-mpegurl; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s.m3u"' % token[:12],
                                 "Cache-Control": "no-store"})

    @app.get("/api/handoff/{item_id}")
    async def handoff(item_id: str, request: Request, ttl: int = 0):
        item = _item(item_id)
        if not item:
            raise HTTPException(404, "条目不存在")
        # ★ req-accounts：**签发时是唯一的把关点**。
        #   /p/<token> 与 /pl/<token> 在 site_auth 免登录白名单里（外部播放器没法带 Cookie），
        #   所以 token 一旦发出去就等同于永久通行证 —— 必须在这里按授权目录严格校验。
        try:
            from .site_auth import session_user
            from . import accounts as _acc
            if not _acc.can_see_item(session_user(request) or "", item.get("parent_id") or ""):
                raise HTTPException(404, "条目不存在")     # 不透露存在性
        except HTTPException:
            raise
        except Exception as _e:                            # noqa: BLE001
            import logging
            logging.getLogger("album.handoff").warning(
                "可见性判定失败（按不可见处理）：%s: %s", type(_e).__name__, _e)
            raise HTTPException(404, "条目不存在") from _e
        token = make_token(item_id, ttl or TTL)
        base = _base(request)
        return JSONResponse({
            "id": item_id,
            "name": item["name"],
            "kind": item["kind"],
            "ext": item["ext"],
            "token": token,
            "pass_url": "%s/p/%s" % (base, token),
            "m3u_url": "%s/pl/%s.m3u" % (base, token),
            "expires_in": ttl or TTL,
        })
