"""115emby 服务入口（uvicorn）。

    uvicorn app.main:app --host 127.0.0.1 --port 8000

它把四件事拼起来：

* ``DB``            —— 媒体库（folders / items / links / progress）
* ``ThumbStore``    —— 加密缩略图库
* ``ThumbFetcher``  —— 懒抓 + 夜间窗口（图片走 115 缩略图，视频走 ffmpeg 抓帧）
* ``Scanner``       —— 后台目录扫描（可视化选目录 → 起步扫描）

环境变量沿用 115-album 那一套（``COOKIE_115`` / ``ACCESS_TOKEN_115`` /
``API_MIN_INTERVAL`` …），另外新增：``EMBY_DB`` / ``THUMB_DB`` / ``DATA_DIR``。
"""
from __future__ import annotations

import logging
import os
from fastapi import Request  # noqa: E402  (供管理守卫使用)
from pathlib import Path

from . import index_crypto as ic
from . import source115
from .db import DB
from .scanner import Scanner
from .server import create_app
from .settings import Settings
from .thumb_fetch import ThumbFetcher
from .thumb_store import ThumbStore
from .video_thumb import VideoThumbOptions

logging.basicConfig(
    level=os.getenv("LOG_LEVEL") or "INFO",
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("album.main")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def build_app():
    settings = Settings.from_env()
    db = DB(Path(os.getenv("EMBY_DB") or (DATA_DIR / "emby.db")))
    store = ThumbStore(Path(os.getenv("THUMB_DB") or (DATA_DIR / "thumbs.db")))
    fetcher = ThumbFetcher(store, settings, video_opts=VideoThumbOptions.from_env())

    async def fetch_bytes(item_photo):
        """图片缩略图：先解出库里的上游地址（密文），直接去 115 取小图。"""
        url = str(getattr(item_photo, "thumb", "") or "")
        if not url:
            row = db.get_item(str(getattr(item_photo, "id", "")))
            if row is not None:
                try:
                    url = ic.safe_unseal_link(row["thumb_enc"] or "")
                except Exception:            # noqa: BLE001
                    url = ""
        if not url:
            return None
        # 115 的缩略图 URL 结尾带尺寸后缀，实测可用档位：_100/_200/_480/_800
        # 列表接口默认给 _100（只有 100px，网格里很糊），这里提到 _480（约 46KB）
        want = int(os.getenv("THUMB_UPSTREAM_SIZE") or 480)
        import re as _re
        url = _re.sub(r"_(?:100|200|480|800)(?=\?|$)", "_%d" % want, url)
        import httpx

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": source115.UA,
                                          "Referer": "https://115.com/"})
            if r.status_code == 200 and (r.content or b""):
                return r.content
            # 该尺寸不存在时回退到原后缀
            if r.status_code == 404:
                back = _re.sub(r"_%d(?=\?|$)" % want, "_100", url)
                r2 = await c.get(back, headers={"User-Agent": source115.UA,
                                                "Referer": "https://115.com/"})
                return r2.content if r2.status_code == 200 else None
            return None

    async def video_fetch(item_photo):
        """视频封面：拿直链后交给 ffmpeg 抓一帧（只读头部若干 MB）。"""
        from .video_thumb import grab_frame

        row = db.get_item(str(getattr(item_photo, "id", "")))
        if not row:
            return None
        # 服务端抓帧必须用 CDN_UA 签发并取流：115Browser 这类客户端 UA 会被 CDN 403
        url, _exp, ua = await resolve_link(dict(row), ua=source115.CDN_UA)
        if not url:
            return None
        return await grab_frame(url, VideoThumbOptions.from_env(),
                                headers={"User-Agent": ua}, ext=row["ext"] or "")

    fetcher.bind_fetch(fetch_bytes)
    async def fetch_original(item_photo):
        """占位图/无图/超限时的兜底：取**原图**字节，交给 encode() 本地缩成缩略图。

        115 的缩略图接口对部分文件只会返回固定的占位图 PNG，但原图一直是可取的。
        注意：直链绑定签发 UA，这里用普通 UA（`Mozilla/5.0`）签发并立即取，避免 403。
        """
        pid = str(getattr(item_photo, "id", ""))
        row = db.get_item(pid)
        if not row:
            return None
        ext = (row["ext"] or "").lower()
        if ext == "gif" and getattr(settings, "gif_skip", True):
            return None
        size = int(row["size"] or 0)
        if size and size > settings.origin_max_bytes:
            return None
        try:
            url, _exp, ua = await resolve_link(dict(row), ua="Mozilla/5.0")
        except Exception:              # noqa: BLE001
            return None
        if not url:
            return None
        import httpx

        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": ua or "Mozilla/5.0"})
            if r.status_code != 200:
                return None
            body = r.content or b""
            if len(body) > settings.origin_max_bytes:
                return None
            return body or None
        except Exception:              # noqa: BLE001
            return None

    fetcher.bind_origin(fetch_original)
    fetcher.bind_video(video_fetch)

    async def resolve_link(item: dict, ua: str = ""):
        """取直链。``ua`` 是**将来真正去 CDN 取流的那个 UA**（浏览器/播放器自己的），
        115 的直链与签发 UA 严格绑定，所以必须透传，不能用固定的服务端 UA。
        """
        getter = getattr(source115, "direct_link", None)
        if getter is None:
            return "", 0, ua or source115.UA
        return await getter(item, ua=ua)

    scanner = Scanner(db, source115.list_dir, settings, thumb_store=store)

    app = create_app(
        db, store, fetcher,
        resolve_link=resolve_link if hasattr(source115, "direct_link") else None,
        scanner=scanner,
        list_dirs=source115.list_dir,
        title="115emby",
    )

    @app.on_event("startup")
    async def _startup() -> None:
        n = db.reset_running_jobs()
        if n:
            log.warning("清理了 %d 个上次残留的 running 任务（已置 paused，可续跑）", n)
        log.info("115emby 启动：数据目录 %s", DATA_DIR)
        if source115.ready():
            log.info("115 凭据可用（%s）",
                     "cookie" if source115.cookie() else "access_token")
        else:
            log.warning("没有 115 凭据：目录选择器只能看到已扫描入库的目录")

    # ------------------------------------------------------------ 管理台（可选）
    # 设了 ADMIN_TOKEN 才启用：/thumb/{pid} 出图 + /admin/thumb/* 队列与预热控制 + /admin 控制台页
    app.state.db, app.state.store, app.state.fetcher = db, store, fetcher
    app.state.scanner = scanner
    _admin_token = (os.getenv("ADMIN_TOKEN") or "").strip()
    if _admin_token:
        from fastapi import Header, HTTPException, Request
        from .thumbs_router import build_router

        async def _photo_for_router(pid: str):
            row = db.get_item(pid)
            if row is None:
                return None
            d = dict(row)
            is_video = (d.get("kind") or "") == "video"
            return type("P", (), {
                "id": pid,
                "name": ic.safe_unseal(d.get("name_enc") or ""),
                "thumb": "",
                "size": int(d.get("size") or 0),
                "ctime": int(d.get("mtime") or 0),
                "ext": d.get("ext") or "",
                "type": 2 if is_video else 5,
                "is_video": is_video,
            })()

        def _admin_guard(request: Request,
                         x_admin_token: str = Header(default="")) -> None:
            """接受两种凭据：X-Admin-Token 头，或 /login 写入的 HttpOnly Cookie。"""
            import hashlib as _hl
            import hmac as _hm
            _expect = _hm.new(_admin_token.encode("utf-8"), b"emby-admin-session",
                              _hl.sha256).hexdigest()
            if (x_admin_token or "").strip() == _admin_token:
                return
            if request.cookies.get("emby_admin") == _expect:
                return
            # 站点登录（/login 的新口令体系）也算已授权：不然登录完进后台还要再输一次
            try:
                # ★ req-accounts：必须判**管理员**，不能只判"已登录" ——
                #   否则普通访问账户能直接读后台的缩略图统计（已实测泄露）。
                from .site_auth import is_admin as _is_admin
                if _is_admin(request):
                    return
            except Exception:                        # noqa: BLE001
                pass
            raise HTTPException(401, "后台未登录")

        app.include_router(build_router(store, _photo_for_router, fetcher, guard=_admin_guard))

        @app.get("/admin", include_in_schema=False)
        async def _admin_console():
            from fastapi.responses import HTMLResponse
            _p = Path(__file__).resolve().parent / "static" / "admin.html"
            return HTMLResponse(_p.read_text(encoding="utf-8"))

        log.info("管理台已启用：/admin（口令来自 ADMIN_TOKEN）")

    return app


app = build_app()
