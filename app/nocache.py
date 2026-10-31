"""HTML 页面缓存策略：**可缓存但每次都校验**（no-cache + ETag），改前端立即生效。

历史：以前这里是 ``no-store, no-cache, must-revalidate, max-age=0`` —— 页面一个字都不许留，
于是手机上每次从播放页返回都要把 66KB 的 browse2.html **重新下载一遍**（而且当时没开 gzip），
这是"返回慢"最实的一块。

现在：
* ``Cache-Control: no-cache`` —— 允许留副本，但必须先用 ETag 校验；
* 页面路由已经算了强 ETag 并处理 ``If-None-Match`` → 没改过就回 **304（0 字节）**；
* 我改前端 → ETag 变 → 手机下一次访问自动拿新版，不需要用户清缓存。
"""
from __future__ import annotations


def install(app) -> None:
    @app.middleware("http")
    async def _nocache_html(request, call_next):
        resp = await call_next(request)
        ct = (resp.headers.get("content-type") or "").lower()
        if ct.startswith("text/html"):
            # 页面自己设了 ETag（server._page）就尊重它，只补 Pragma/Expires
            resp.headers.setdefault("Cache-Control", "no-cache")
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp
