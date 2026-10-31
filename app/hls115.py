"""115 官方 HLS 代理：把 mkv/avi/rmvb/flv/ts 这类"浏览器解不了"的文件，
用 115 自己的转码流（115vod.com）在浏览器里播放。

原理（抓包实测 2026-10-21）
--------------------------
115 网页播放器的链路是：

    GET https://115vod.com/webapi/files/video?pickcode=<pc>&share_id=0&local=1
    GET https://115vod.com/site/api/video/m3u8/<pc>.m3u8      ← HLS 主列表
    → https://cpats01.115.com/<hash>/<TS>/<sha1>_1920.m3u8?u=..&se=u,ua&s=..
    → https://cpats01.115.com/<hash>/<TS>/<sha1>_1920-00001.ts?...   （分片）

关键约束：**签名绑 UA**（`se=u,ua`）。用 115Browser UA 取是 200，换 Chrome/Safari 立即
`403 invalid signature`。而分片响应头是 `Access-Control-Allow-Origin: https://115.com`，
浏览器跨域也读不到。

因此这里做**同源代理**：服务端统一用 115Browser UA 去取列表与分片，浏览器只访问我们的
`/api/hls/...`。转码由 115 完成，本机 CPU ≈ 0，只消耗出口流量。

路由
----
* ``GET /api/hls/{item_id}/master.m3u8``        主列表（自动改写变体地址）
* ``GET /api/hls/{item_id}/v.m3u8?u=<b64url>``  变体列表（改写分片地址）
* ``GET /api/hls/{item_id}/s?u=<b64url>``       分片（透传 Range，206）
"""
from __future__ import annotations

import base64
import re
import os
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

UA115 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 115Browser/35.10.0.2")
VOD = "https://115vod.com"
ALLOW_HOST_SUFFIX = (".115.com", ".115cdn.net")
CACHE_TTL = 300


def _b64(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _unb64(tok: str) -> str:
    pad = "=" * (-len(tok) % 4)
    try:
        return base64.urlsafe_b64decode(tok + pad).decode()
    except Exception:
        raise HTTPException(400, "bad token")


def _host_ok(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h.endswith(s) for s in ALLOW_HOST_SUFFIX)


# ★ 硬约束：本机带宽仅 2Mbps，且还跑着 3 个吃网络的服务。
# 因此**禁止**用服务器中转视频字节（HLS 分片）。只允许「列表直连」模式：
# 服务端仅把 1~2 KB 的播放列表签给浏览器，分片由浏览器直连 115 CDN。
PROXY_DISABLED = (os.getenv("HLS_PROXY_ENABLED", "0").strip() in ("0", "", "false", "False"))


def install(app) -> None:
    import httpx

    cookie = (os.getenv("COOKIE_115") or "").strip()
    cache: dict[str, tuple] = {}

    def client() -> "httpx.AsyncClient":
        return httpx.AsyncClient(
            timeout=httpx.Timeout(40.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": UA115, "Referer": "https://115vod.com/",
                     **({"Cookie": cookie} if cookie else {})},
        )

    async def _get_text(url: str) -> str:
        async with client() as c:
            r = await c.get(url)
        if r.status_code != 200:
            raise HTTPException(502, "upstream %s" % r.status_code)
        return r.text

    def _pickcode(item_id: str) -> str:
        return item_id.split(":")[-1].strip()

    async def _get_text_ua(url: str, ua: str) -> str:
        """用指定 UA 取文本（直连模式：按浏览器自己的 UA 让 115 签发）。"""
        async with client() as c:
            r = await c.get(url, headers={"User-Agent": ua})
        if r.status_code != 200:
            raise HTTPException(502, "upstream %s" % r.status_code)
        return r.text

    @app.get("/api/hls/{item_id}/master.m3u8")
    async def master(item_id: str, request: Request, direct: int = Query(0)):
        pc = _pickcode(item_id)
        if not pc:
            raise HTTPException(400, "no pickcode")
        ua = (request.headers.get("user-agent") or UA115).strip() or UA115

        # ---- 直连模式：用浏览器自己的 UA 签发，返回原始（绝对 CDN）地址 ----
        if direct:
            key = "d:" + pc + ":" + ua
            hit = cache.get(key)
            if hit and time.time() < hit[0]:
                body = hit[1]
            else:
                body = await _get_text_ua("%s/site/api/video/m3u8/%s.m3u8" % (VOD, pc), ua)
                cache[key] = (time.time() + CACHE_TTL, body)
            return Response(body, media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store",
                                     "X-Hls-Mode": "direct"})

        # ---- 代理模式（已禁用，仅留作本地调试）----
        if PROXY_DISABLED:
            raise HTTPException(403, "请使用 ?direct=1 或 /lowest（禁止服务器中转）")
        key = "m:" + pc
        hit = cache.get(key)
        if hit and time.time() < hit[0]:
            body = hit[1]
        else:
            body = await _get_text("%s/site/api/video/m3u8/%s.m3u8" % (VOD, pc))
            cache[key] = (time.time() + CACHE_TTL, body)

        out, base = [], "https://cpats01.115.com"
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("#EXT-X-STREAM-INF"):
                out.append(line)
                continue
            if s and not s.startswith("#"):
                absu = s if s.startswith("http") else base + ("" if s.startswith("/") else "/") + s
                out.append("/api/hls/%s/v.m3u8?u=%s" % (item_id, _b64(absu)))
            else:
                out.append(line)
        return Response("\n".join(out) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/hls/{item_id}/v.m3u8")
    async def variant(item_id: str, u: str = Query(...)):
        if PROXY_DISABLED:
            raise HTTPException(403, "服务器代理已禁用（带宽 2M，仅允许浏览器直连 115）")
        absu = _unb64(u)
        if not _host_ok(absu):
            raise HTTPException(403, "host not allowed")
        key = "v:" + absu
        hit = cache.get(key)
        if hit and time.time() < hit[0]:
            body = hit[1]
        else:
            body = await _get_text(absu)
            cache[key] = (time.time() + CACHE_TTL, body)

        base = "https://" + (urlparse(absu).hostname or "cpats01.115.com")
        # 分片相对路径的锚点：同级目录
        anchor = absu.rsplit("/", 1)[0]
        out = []
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                if s.startswith("http"):
                    seg = s
                elif s.startswith("/"):
                    seg = base + s
                else:
                    seg = anchor + "/" + s
                out.append("/api/hls/%s/s?u=%s" % (item_id, _b64(seg)))
            else:
                out.append(line)
        return Response("\n".join(out) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/hls/{item_id}/s")
    async def segment(item_id: str, request: Request, u: str = Query(...)):
        if PROXY_DISABLED:
            raise HTTPException(403, "服务器代理已禁用（带宽 2M，仅允许浏览器直连 115）")
        absu = _unb64(u)
        if not _host_ok(absu):
            raise HTTPException(403, "host not allowed")
        rng = request.headers.get("range")
        headers = {"User-Agent": UA115, "Referer": "https://115vod.com/"}
        if rng:
            headers["Range"] = rng

        c = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True)
        req = c.build_request("GET", absu, headers=headers)
        up = await c.send(req, stream=True)
        if up.status_code >= 400:
            await up.aclose()
            await c.aclose()
            raise HTTPException(up.status_code, "upstream segment error")

        async def gen():
            try:
                async for chunk in up.aiter_bytes(65536):
                    yield chunk
            finally:
                await up.aclose()
                await c.aclose()

        hdrs = {"Cache-Control": "public, max-age=60"}
        for k in ("content-type", "content-length", "content-range", "accept-ranges"):
            v = up.headers.get(k)
            if v:
                hdrs[k] = v
        return StreamingResponse(gen(), status_code=up.status_code, headers=hdrs,
                                 media_type=up.headers.get("content-type", "video/mp2t"))

    def _parse_levels(raw: str) -> list:
        """解析主列表里的所有档位，按码率从高到低排序。

        返回 ``[{"i":0,"bw":15000000,"res":"3840x2160","name":"原画","line":"#EXT-X-STREAM-INF:...","url":"https://..."}]``
        """
        lines = raw.splitlines()
        out = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            if ln.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", ln)
                bw = int(m.group(1)) if m else 0
                r = re.search(r"RESOLUTION=([0-9x]+)", ln)
                nm = re.search(r'NAME="([^"]*)"', ln)
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
                    j += 1
                url = lines[j].strip() if j < len(lines) else ""
                if url:
                    out.append({"bw": bw, "res": r.group(1) if r else "",
                                "name": nm.group(1) if nm else "", "line": ln, "url": url})
                i = j
            i += 1
        out.sort(key=lambda x: -x["bw"])          # 高码率优先，i=0 就是"最好的"
        for n, x in enumerate(out):
            x["i"] = n
        return out

    async def _levels(item_id: str, ua: str) -> list:
        pc = _pickcode(item_id)
        raw = await _get_text_ua("%s/site/api/video/m3u8/%s.m3u8" % (VOD, pc), ua)
        return _parse_levels(raw)

    @app.get("/api/hls/{item_id}/levels")
    async def levels(item_id: str, request: Request):
        """可选画质档位（给前端的"画质"菜单用）。"""
        ua = (request.headers.get("user-agent") or UA115).strip() or UA115
        try:
            lv = await _levels(item_id, ua)
        except HTTPException as exc:
            return JSONResponse({"ok": False, "reason": str(exc.detail)[:80], "levels": []})
        except Exception as exc:                      # noqa: BLE001
            return JSONResponse({"ok": False, "reason": str(exc)[:80], "levels": []})
        return JSONResponse({"ok": True, "levels": [
            {"i": x["i"], "bw": x["bw"], "res": x["res"], "name": x["name"],
             "mbps": round(x["bw"] / 1e6, 1)} for x in lv]})

    @app.get("/api/hls/{item_id}/at")
    async def at_level(item_id: str, request: Request, i: int = Query(0)):
        """只给第 i 档（0=最高画质）。默认第 0 档 —— 用户明确说过不用为流量挑低档。"""
        ua = (request.headers.get("user-agent") or UA115).strip() or UA115
        key = "a:%s:%s:%d" % (_pickcode(item_id), ua, i)
        hit = cache.get(key)
        if hit and time.time() < hit[0]:
            body = hit[1]
        else:
            lv = await _levels(item_id, ua)
            if not lv:
                raise HTTPException(502, "上游没有可用档位")
            pick = lv[max(0, min(len(lv) - 1, int(i)))]
            body = "#EXTM3U\n%s\n%s\n" % (pick["line"], pick["url"])
            cache[key] = (time.time() + CACHE_TTL, body)
        return Response(body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store", "X-Hls-Mode": "direct-level"})

    @app.get("/api/hls/{item_id}/lowest")
    async def lowest(item_id: str, request: Request):
        """直连模式 + 只留最低码率档（兼容旧链接；新前端默认走 /at?i=）。"""
        pc = _pickcode(item_id)
        if not pc:
            raise HTTPException(400, "no pickcode")
        ua = (request.headers.get("user-agent") or UA115).strip() or UA115
        key = "l:" + pc + ":" + ua
        hit = cache.get(key)
        if hit and time.time() < hit[0]:
            body = hit[1]
        else:
            raw = await _get_text_ua("%s/site/api/video/m3u8/%s.m3u8" % (VOD, pc), ua)
            lv = _parse_levels(raw)
            body = ("#EXTM3U\n%s\n%s\n" % (lv[-1]["line"], lv[-1]["url"])) if lv else raw
            cache[key] = (time.time() + CACHE_TTL, body)
        return Response(body, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store", "X-Hls-Mode": "direct-lowest"})

    @app.get("/api/hls/{item_id}/info")
    async def info(item_id: str):
        """给前端判断是否可走 115 HLS（顺带返回分辨率/时长）。"""
        pc = _pickcode(item_id)
        try:
            async with client() as c:
                r = await c.get("%s/webapi/files/video" % VOD,
                                params={"pickcode": pc, "share_id": 0, "local": 1})
            js = r.json() if r.status_code == 200 else {}
        except Exception as exc:                      # noqa: BLE001
            return JSONResponse({"ok": False, "reason": str(exc)[:80]})
        return JSONResponse({
            "ok": bool(js.get("state")),
            "width": int(js.get("width") or 0) if str(js.get("width") or "").isdigit() else js.get("width"),
            "height": int(js.get("height") or 0) if str(js.get("height") or "").isdigit() else js.get("height"),
            "duration": js.get("play_long"),
            "definitions": list((js.get("definition_list") or {}).values()),
            "hls": "/api/hls/%s/master.m3u8" % item_id,
        })

    app.state.hls115 = {"cache": cache}
