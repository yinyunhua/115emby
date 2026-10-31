"""115emby 服务骨架：文件夹浏览 + 看图 + 播放（另类 Emby，不刮削、不转码）。

它和"相册"以及"标准 Emby"的区别
--------------------------------
* **数据来自文件夹**，不做时间轴/EXIF/GPS/地图（那是相册的事）；
* **不刮削**（不查 TMDB/TVDB）、**不转码**（原画 302 直连 115 CDN）、**不做剧集/季集元数据**；
* 只做三件事，但每件都做扎实：
  1. **看图的浏览体验**：同目录内上一张/下一张、原图直开、缩略图墙；
  2. **放视频**：直链 302 + 断链自愈 + **续播进度**（"继续观看"）；
  3. **能认出是什么**：缩略图（图片走 115 缩略图、视频走 ffmpeg 抓帧），全部加密落盘。

接口
----
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/browse?parent=<id>` | 同层：子文件夹 + 媒体项（名字已解密，未鉴权前不下发直链） |
| GET | `/api/item/{id}` | 单项信息（含同目录 prev/next，供看图翻页/连播） |
| GET | `/api/thumb/{id}` | 缩略图（加密库出图，ETag/304） |
| GET | `/api/image/{id}` | 原图 → 302 到 115 CDN |
| GET | `/api/play/{id}` | 视频 → 302 到 115 CDN（带 `ua` 供客户端透传） |
| POST | `/api/progress` | 上报播放进度 `{id, position, duration}` |
| GET | `/api/continue` | 继续观看列表 |
| GET | `/api/health` | 自检：库统计 + 加密算法 + 队列 |

设计要点
--------
* 名字在库里就是密文（`db.py` 只有 `name_enc`，**没有明文列**），所以这里读出即解密；
* 直链在 `links` 表里也是密文，`/api/play` 返回前才解密，并带 `expires`；
* 注入式依赖，方便离线测试：`resolve_link` / `fetch_bytes` / `video_fetch` 都是回调。
"""
# 注意：本模块**不能**用 from __future__ import annotations ——
# FastAPI 在闭包内解析 request: Request 时拿不到局部名字，会把它误判成查询参数（422）。
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import index_crypto as ic
from . import source115
from .codecs import classify as _classify_codec
from .db import DB, KIND_IMAGE, KIND_VIDEO
from .thumb_fetch import ThumbFetcher
from .thumb_store import ThumbStore

log = logging.getLogger("album.server")

ResolveLink = Callable[[dict], Awaitable[tuple[str, int, str]]]   # item -> (url, expires, ua)


def _who(request) -> str:
    """当前会话账户名（空 = 未登录）。"""
    try:
        from .site_auth import session_user
        return session_user(request) or ""
    except Exception:                                       # noqa: BLE001
        return ""


def _deny():
    """未授权一律 404 —— **不透露资源是否存在**（403 等于承认"有这个东西"）。"""
    from fastapi import HTTPException
    raise HTTPException(404, "没有这个资源")


def _acc_can(user: str, row) -> bool:
    """row（items 行）是否对 user 可见。管理员恒 True。"""
    try:
        from . import accounts as _acc
        return _acc.can_see_item(user, (row["parent_id"] if row is not None else "") or "")
    except Exception as _e:                                 # noqa: BLE001
        import logging
        logging.getLogger("album.server").warning(
            "可见性判定失败（按不可见处理）：%s: %s", type(_e).__name__, _e)
        return False                                        # fail-closed


def _need_folder(request, folder_id: str) -> None:
    from . import accounts as _acc
    if not _acc.can_see_folder(_who(request), folder_id):
        _deny()


def _item_to_photo(item: dict) -> Any:
    """把 items 行包装成缩略图抓取器认得的"photo"对象（鸭子类型）。"""
    return type("ItemPhoto", (), {
        "id": item["id"],
        "name": item.get("name", ""),
        "thumb": item.get("thumb", ""),          # 上游缩略图 URL（可能为空）
        "size": int(item.get("size") or 0),
        "ctime": int(item.get("mtime") or 0),
        "ext": item.get("ext", ""),
        "type": 2 if item.get("kind") == KIND_VIDEO else 5,
        "is_video": item.get("kind") == KIND_VIDEO,
    })()


def create_app(
    db: DB,
    store: ThumbStore,
    fetcher: ThumbFetcher,
    *,
    resolve_link: ResolveLink | None = None,
    scanner=None,
    list_dirs=None,
    title: str = "115emby",
):
    """构造 FastAPI 应用。``resolve_link`` 为空时 `/api/play` 返回 501（便于分阶段接入）。"""
    from fastapi import Body, FastAPI, HTTPException, Query, Request
    from fastapi.responses import JSONResponse, RedirectResponse, Response

    app = FastAPI(title=title, docs_url="/api/docs")

    # ------------------------------------------------------------------ 工具
    def _row_to_dict(row) -> dict:
        d = dict(row)
        d["name"] = ic.safe_unseal(d.get("name_enc") or "")
        d["path"] = ic.safe_unseal(d.get("path_enc") or "")
        for k in ("name_enc", "name_idx", "name_bucket", "path_enc"):
            d.pop(k, None)
        return d

    def _get_item(item_id: str) -> dict:
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "没有这个媒体项")
        return _row_to_dict(item)

    async def _link_for(item_row: dict, client_ua: str = "") -> tuple[str, int, str]:
        """取直链。

        **115 的直链和签发时的 UA 严格绑定**（实测：换 Chrome/Safari/VLC/空 UA 一律 403）。
        所以这里必须用"将来真正发请求的那个 UA"去签发 —— 浏览器播放时就是浏览器的 UA。
        缓存也按 UA 校验：UA 变了就重签，避免拿到"别人 UA"的死链。
        """
        import time

        want_ua = (client_ua or "").strip()
        cached = db.get_link(item_row["id"])
        if cached and int(cached["expires"] or 0) > time.time() + 60:
            same_ua = (cached["ua"] or "") == want_ua or not want_ua
            if same_ua:
                url = ic.safe_unseal_link(cached["url_enc"])
                if url:
                    return url, int(cached["expires"]), cached["ua"] or ""
        if resolve_link is None:
            raise HTTPException(501, "尚未接入直链解析（resolve_link 未注入）")
        try:
            url, expires, ua = await resolve_link(_row_to_dict(item_row), ua=want_ua)
        except HTTPException:
            raise
        except Exception as exc:                       # noqa: BLE001
            log.warning("取直链失败 %s：%s", item_row["id"], exc)
            raise HTTPException(502, f"取直链失败：{exc}") from exc
        if not url:
            raise HTTPException(502, "上游没有返回直链")
        db.set_link(item_row["id"], ic.seal_link(url), expires, ua)
        return url, expires, ua

    # 缩略图并发闸门：图片缩略图最多同时取 4 张（env THUMB_CONCURRENCY）。
    # 2 vCPU 的小机器上，放手让 100 张一起抢会把 API 一起拖死。
    _thumb_sem: dict = {}
    _THUMB_CONC = max(1, int(os.getenv("THUMB_CONCURRENCY") or 4))

    def _thumb_gate():
        import asyncio as _a
        sem = _thumb_sem.get("s")
        if sem is None:
            sem = _thumb_sem["s"] = _a.Semaphore(_THUMB_CONC)
        return sem

    def _codec_fields(vcodec: str) -> dict:
        """把编码翻成前端直接能用的字段：能不能直接播 + 一句话原因。"""
        info = _classify_codec(vcodec)
        return {"playable_codec": info["playable"], "codec_label": info["label"],
                "codec_note": info["note"]}

    # 后台抓帧：同一个条目只留一个任务；同时在跑的**最多 2 个**（默认，env 可调）。
    # 这台机器只有 2 vCPU，而且抓帧期间 SQLite/JSON 这些还要跑；之前放开到 40 个排队，
    # 结果列表页一打开就把机器占满，连 /api/discover 都从 0.2s 变成 4s。
    # 名额满了就直接不排队：前端 5/10/18/30/45 秒退避重试，容量空出来时自然轮到它。
    _bg_thumbs: dict = {}
    _BG_MAX = max(1, int(os.getenv("THUMB_BG_MAX") or 2))

    def _kick_video_thumb(item_id: str, photo) -> None:
        import asyncio as _a

        t = _bg_thumbs.get(item_id)
        if t is not None and not t.done():
            return
        if sum(1 for x in _bg_thumbs.values() if not x.done()) >= _BG_MAX:
            return

        async def _run():
            try:
                await fetcher.ensure(photo, allow_fetch=True)
            except Exception as exc:                       # noqa: BLE001
                log.info("后台抓帧失败 %s：%s", item_id, exc)
            finally:
                _bg_thumbs.pop(item_id, None)

        _bg_thumbs[item_id] = _a.create_task(_run())

    def _client_ua(request, override: str = "") -> str:
        """优先用显式覆盖，其次是调用方自己的 UA（播放器/浏览器）。"""
        return (override or request.headers.get("user-agent") or "").strip()

    # ------------------------------------------------------------------ 页面
    static_dir = Path(__file__).resolve().parent / "static"

    # ---------------------------------------------------------------- gzip
    # 之前**完全没有压缩**：browse2.html 66KB、player.html 73KB 原样发给手机，
    # 而这两个页面是 no-cache 的，每次从播放页返回都要重来一遍 —— 手机端"返回慢"
    # 很大一块就在这。这里只压文本类（HTML/JSON/JS/CSS），图片本来就不用压。
    _GZIP_MIN = 700
    # ★ 压缩结果缓存：页面是带 ETag 的静态内容，16 并发打开时原来要压 16 次 70KB
    #（0.6 核上合计 ~3 秒），现在同一份只压一次。只缓存带 ETag 的响应。
    _GZIP_CACHE: dict = {}
    _GZIP_TYPES = ("text/", "application/json", "application/javascript",
                   "application/xml", "image/svg+xml")

    @app.middleware("http")
    async def _gzip_mw(request: Request, call_next):
        import gzip as _gz
        resp = await call_next(request)
        try:
            enc = (request.headers.get("accept-encoding") or "").lower()
            ctype = (resp.headers.get("content-type") or "").lower()
            if ("gzip" not in enc or resp.status_code != 200
                    or "content-encoding" in resp.headers
                    or not any(ctype.startswith(t) for t in _GZIP_TYPES)):
                return resp
            body = b"".join([chunk async for chunk in resp.body_iterator])
            if len(body) < _GZIP_MIN:
                return Response(body, status_code=200, headers=dict(resp.headers),
                                media_type=resp.media_type)
            _ck = ((request.url.path or "") + "?" + (request.url.query or ""),
                   resp.headers.get("etag") or "")
            _cached = _GZIP_CACHE.get(_ck) if _ck[1] else None
            if _cached is not None:
                packed = _cached
            else:
                packed = _gz.compress(body, 5)
                if _ck[1] and len(_GZIP_CACHE) < 64:
                    _GZIP_CACHE[_ck] = packed
            headers = dict(resp.headers)
            headers["content-encoding"] = "gzip"
            headers["content-length"] = str(len(packed))
            headers["vary"] = "Accept-Encoding"
            return Response(packed, status_code=200, headers=headers,
                            media_type=resp.media_type)
        except Exception:                                   # noqa: BLE001
            return resp

    def _page(name: str, request: Request | None = None):
        """HTML 页面：**允许缓存 + ETag**。

        以前是 no-store —— 手机上每次从播放页返回都要重下 66KB 的 browse2.html
        （而且没开 gzip），这是"返回慢"里最实的一块。现在：
        `no-cache`（可缓存但每次校验）+ 强 ETag，没改过就回 304（几乎不带字节）。
        """
        import hashlib as _hash
        from fastapi.responses import HTMLResponse, Response as _Resp

        path = static_dir / name
        if not path.exists():                              # pragma: no cover
            raise HTTPException(500, f"{name} 缺失")
        raw = path.read_bytes()
        etag = '"' + _hash.md5(raw).hexdigest() + '"'
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        if request is not None and request.headers.get("if-none-match") == etag:
            return _Resp(status_code=304, headers=headers)
        return HTMLResponse(raw.decode("utf-8"), headers=headers)

    @app.get("/", include_in_schema=False)
    @app.get("/browse", include_in_schema=False)
    async def browse_page(request: Request):
        """新版前台（目录树 + 随机发现 + 2×2 文件夹封面）。"""
        """首页网格：文件夹优先 + 缩略图（2 列）。"""
        return _page("browse2.html", request)

    @app.get("/play/{item_id}", include_in_schema=False)
    @app.get("/view/{item_id}", include_in_schema=False)
    async def player_page(item_id: str, request: Request):
        """播放/查看页（浏览器原生格式直放；其它交给本地播放器）。"""
        return _page("player.html", request)

    @app.get("/browse2", include_in_schema=False)
    async def browse2_page(request: Request):
        """新版前台（目录树 + 随机发现 + 2×2 文件夹封面）。"""
        return _page("browse2.html", request)

    @app.get("/view", include_in_schema=False)
    async def view_page(item_id: str = Query("", alias="id"),
                        parent: str = Query("-1")):
        """深链：跳到浏览页并直接打开灯箱（不再单独一套页面）。"""
        from urllib.parse import quote

        if not item_id:
            return RedirectResponse("/browse?parent=" + quote(parent), status_code=302)
        return RedirectResponse("/browse?parent=" + quote(parent) + "&open=" + quote(item_id),
                                status_code=302)

    @app.get("/picker", include_in_schema=False)
    async def picker_page(request: Request):
        """可视化选目录（扫库用）。"""
        return _page("picker.html", request)

    @app.get("/api/folder/{folder_id}")
    async def folder_info(folder_id: str, request: Request):
        # ★ req-accounts：目录本身也要校验（否则能探测出未授权目录的名字/层级）
        if folder_id not in ("-1", ""):
            _need_folder(request, folder_id)
        row = db.folder(folder_id) if folder_id not in ("-1", "") else None
        return JSONResponse({
            "id": folder_id,
            "name": ic.safe_unseal(row["name_enc"] or "") if row else "",
            "parent_id": (row["parent_id"] if row else "-1") or "-1",
            "ancestors": db.ancestors(folder_id),
        })

    # ------------------------------------------------------------------ 浏览
    @app.get("/api/browse")
    async def browse(request: Request, parent: str = Query("-1"), kind: str | None = None,
                     limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                     order: str = Query("mtime"), show_hidden: int = Query(0)):
        # ★ req-accounts：浏览的 parent 必须在授权集合内（未授权 → 404）
        if parent not in ("-1", ""):
            _need_folder(request, parent)
        # kind 可能是空/all（前端"全部"）——只有 image/video 才是真过滤条件，
        # 否则会被当成字面量写进 SQL（kind='all' 永远查不到东西）
        k = kind if kind in (KIND_IMAGE, KIND_VIDEO) else None
        folders = [_row_to_dict(f) for f in db.list_folders(parent)]
        # 文件夹也按时间排（folder_mtime = 115 目录项 t/te/tu/tp/to 的最大值）；
        # 没写过的回退到 updated；?order=name 可切回名字序
        if (order or "mtime").lower() != "name":
            folders.sort(
                key=lambda x: (int(x.get("folder_mtime") or x.get("updated") or 0),
                               str(x.get("id") or "")),
                reverse=True,
            )
        items = [_row_to_dict(r) for r in db.list_children(parent, kind=k,
                                                          limit=limit, offset=offset,
                                                          order=order, exclude_gif=True,
                                                          show_hidden=bool(show_hidden))]
        for it in items:
            it["playable"] = it["kind"] == KIND_VIDEO
            it["viewable"] = it["kind"] == KIND_IMAGE
        return JSONResponse({"parent": parent, "folders": folders, "items": items,
                             "total_items": db.count_children(parent, kind=k, exclude_gif=True,
                                                             show_hidden=bool(show_hidden)),
                             "counts": {"folders": len(folders), "items": len(items)}})

    @app.get("/api/probe/{item_id}")
    async def probe_item(item_id: str, request: Request, force: bool = Query(False)):
        """按需探测编码（ffprobe 直读 CDN，只读必要字节），结果落库后长期复用。

        界面用它给"这条浏览器到底能不能直接播"一个确定答案；探测失败返回
        ``probed:false``，前端退回按扩展名 + 运行时 videoWidth 判断。
        """
        row = db.get_item(item_id)
        if not row:
            raise HTTPException(404, "没有这个媒体项")
        if not _acc_can(_who(request), row):
            raise HTTPException(404, "没有这个媒体项")
        d = _row_to_dict(row)
        if d.get("kind") != KIND_VIDEO:
            return JSONResponse({"probed": False, "reason": "not_video"})
        cached = {"vcodec": d.get("vcodec") or "", "width": int(d.get("width") or 0),
                  "height": int(d.get("height") or 0), "acodec": d.get("acodec") or ""}
        if cached["vcodec"] and cached["width"] and not force:
            return JSONResponse(dict(cached, probed=True, cached=True,
                                     **_codec_fields(cached["vcodec"])))
        from .video_thumb import probe_media, VideoThumbOptions

        if force:
            try:
                db.set_media_meta(item_id, {"vcodec": "", "acodec": "", "width": 0,
                                            "height": 0, "duration": 0})
            except Exception:
                pass
        try:
            url, _exp, ua = await _link_for(row, source115.CDN_UA)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"probed": False, "reason": "link_failed: %s" % exc})
        if not url:
            return JSONResponse({"probed": False, "reason": "no_link"})
        meta = await probe_media(url, VideoThumbOptions.from_env(),
                                 headers={"User-Agent": ua})
        if not meta.get("vcodec"):
            return JSONResponse({"probed": False, "reason": "ffprobe_failed"})
        db.set_media_meta(item_id, meta)
        out = {"probed": True, "cached": False, "vcodec": meta.get("vcodec", ""),
               "acodec": meta.get("acodec", ""), "width": int(meta.get("width") or 0),
               "height": int(meta.get("height") or 0),
               "duration": float(meta.get("duration") or 0)}
        out.update(_codec_fields(out["vcodec"]))
        return JSONResponse(out)

    @app.get("/api/item/{item_id}")
    async def item_detail(item_id: str, request: Request, kind: str = Query("same")):
        item = _get_item(item_id)
        if not _acc_can(_who(request), item):
            raise HTTPException(404, "没有这个媒体项")
        if (item.get("kind") or "") == KIND_VIDEO:
            item.update(_codec_fields(item.get("vcodec") or ""))
        item["fav"] = db.is_fav(item_id)        # req-fav：播放页据此画星
        # ★ 原先硬编码 limit=500：同层超过 500 项时 idx=-1，prev/next 双双变 None，
        #   播放页"上一个/下一个"会静默失效。改为给足上限（极端目录兜底 20000）。
        _sib_limit = 20000
        siblings = [_row_to_dict(r) for r in
                    db.list_children(db.get_parent_of(item_id), limit=_sib_limit)]
        # 上一个/下一个只在**同类型**里切换（图片只切图片、视频只切视频）
        want = item.get("kind") if kind in ("", "same") else kind
        if want in ("image", "video"):
            siblings = [s for s in siblings if s.get("kind") == want]
        idx = next((i for i, s in enumerate(siblings) if s["id"] == item_id), -1)
        item["prev_id"] = siblings[idx - 1]["id"] if idx > 0 else None
        item["next_id"] = siblings[idx + 1]["id"] if 0 <= idx < len(siblings) - 1 else None
        prog = db.get_progress(item_id)
        item["progress"] = dict(prog) if prog else None
        # 信息面板的"所在目录"：从根目录走到该文件所在目录的链（不含文件名）。
        # 注意 items.path_enc 里存的是**文件名**（不是目录），所以必须走 folders 表。
        try:
            item["dir_chain"] = db.ancestors(item.get("parent_id") or "-1")
        except Exception:                                  # noqa: BLE001
            item["dir_chain"] = []
        return JSONResponse(item)

    # ------------------------------------------------------------------ 缩略图
    @app.api_route("/api/thumb/{item_id}", methods=["GET", "HEAD"])
    async def thumb(item_id: str, request: Request):
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "没有这个媒体项")
        if not _acc_can(_who(request), item):
            raise HTTPException(404, "没有这个媒体项")
        photo = _item_to_photo(_row_to_dict(item))
        _kind = ""
        try:
            _kind = (item["kind"] or "") if "kind" in item.keys() else ""
        except Exception:  # noqa: BLE001
            _kind = ""

        # ★ 缩略图并发闸门
        # 列表页一屏就有 100+ 张缩略图要取，未抓过的要走 115 上游/ffmpeg/PIL。
        # 不设闸门时这些请求会把整个应用压住 —— 实测列表页打开后 /api/discover
        # 从 0.2s 变成 10s+（页面一直"载入中…"），用户体感就是"进/退都要卡几秒"。
        # 所以：先查缓存（命中直接给，不进闸门），未命中才排队，最多 THUMB_CONCURRENCY 个。
        probe = await fetcher.ensure(photo, allow_fetch=False)
        if not probe.ok:
            if _kind == KIND_VIDEO:
                # 视频抓帧要 8~12 秒，绝不阻塞请求：丢后台 + 404 PENDING，前端退避重试
                _kick_video_thumb(item_id, photo)
                return Response(status_code=404, headers={
                    "X-Thumb": "PENDING",
                    "Cache-Control": "no-store",
                    "Retry-After": "8",
                })
            async with _thumb_gate():
                result = await fetcher.ensure(photo, allow_fetch=True)
        else:
            result = probe

        if not result.ok:
            label = (result.label or "").upper()
            if label == "PENDING":
                # 视频正在后台抓帧：用 404 让 <img> 触发 onerror，前端自动退避重试
                return Response(status_code=404, headers={
                    "X-Thumb": "PENDING",
                    "Cache-Control": "no-store",
                    "Retry-After": "3",
                })
            # 终态（skipped / failed）：给中性图标，不写字，避免“无缩略图”这种观感
            glyph = "&#9654;" if _kind == KIND_VIDEO else "&#128444;"
            placeholder = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="384" height="384">'
                '<rect width="100%" height="100%" fill="#171b23"/>'
                '<text x="50%" y="52%" fill="#4a5568" font-family="sans-serif" '
                'font-size="120" text-anchor="middle">' + glyph + '</text></svg>'
            ).encode("utf-8")
            return Response(placeholder, media_type="image/svg+xml",
                            headers={"Cache-Control": "no-store",
                                     "X-Thumb": label or "MISS"})
        etag = f'"{result.etag}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(result.data, media_type="image/jpeg", headers={
            "ETag": etag,
            # 缩略图内容不会变（按条目 id 生成一次），长缓存 + 不要 must-revalidate：
            # 带 must-revalidate 时 iOS 每次返回列表都要逐张回源校验（10+ 次往返），
            # 这是手机端"图片要等一会儿才出现"的主因。immutable 让它直接用本地副本。
            "Cache-Control": "private, max-age=604800, immutable",
            "X-Thumb": result.label,
        })

    # ------------------------------------------------------------------ 看图 / 播放
    @app.get("/api/image/{item_id}")
    async def image(item_id: str, request: Request, ua: str = Query(""),
                    proxy: int = Query(0)):
        """图片：**默认 302 到 115 直链**（和相册一致，服务器不出图片字节）。

        浏览器里用 ``<img src>`` 加载即可显示——115 CDN 虽然带
        ``Content-Disposition: attachment``，但媒体元素不受它影响；
        只有"在地址栏直接打开"才会变成下载。
        ``?proxy=1`` 才由服务器代理（极少数需要内联导航的场景）。
        """
        row = db.get_item(item_id)
        if not row:
            raise HTTPException(404, "没有这个媒体项")
        if not _acc_can(_who(request), row):
            raise HTTPException(404, "没有这个媒体项")
        want_ua = _client_ua(request, ua)
        url, expires, signed_ua = await _link_for(row, want_ua)

        if not proxy:
            return RedirectResponse(url, status_code=302,
                                    headers={"X-Link-Expires": str(expires), "Cache-Control": "no-store"})

        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as hc:
                r = await hc.get(url, headers={"User-Agent": signed_ua or want_ua,
                                               "Referer": "https://115.com/"})
            if r.status_code != 200 or not r.content or len(r.content) > 40 * 1024 * 1024:
                return RedirectResponse(url, status_code=302,
                                        headers={"X-Link-Expires": str(expires), "Cache-Control": "no-store"})
            ctype = r.headers.get("content-type") or "image/jpeg"
            if not ctype.startswith("image/"):
                ctype = "image/jpeg"
            return Response(r.content, media_type=ctype, headers={
                "Content-Disposition": "inline",
                "Cache-Control": "private, max-age=3600",
                "X-Link-Expires": str(expires), "Cache-Control": "no-store",
            })
        except Exception as exc:                          # noqa: BLE001
            log.warning("代理取图失败 %s：%s，退回 302", item_id, exc)
            return RedirectResponse(url, status_code=302,
                                    headers={"X-Link-Expires": str(expires), "Cache-Control": "no-store"})

    @app.get("/api/link/{item_id}")
    async def link(item_id: str, request: Request, ua: str = Query("")):
        """返回 115 直链（JSON，不跳转）—— 供前台"复制直链/打开直链"。

        与相册一致：图片和视频都给直链；直链绑定签发时的 UA，所以这里
        用**调用方自己的 UA** 去签（浏览器拿到后用自己的 UA 打开才有效）。
        """
        row = db.get_item(item_id)
        if not row:
            raise HTTPException(404, "没有这个媒体项")
        if not _acc_can(_who(request), row):
            raise HTTPException(404, "没有这个媒体项")
        want_ua = _client_ua(request, ua)
        url, expires, signed = await _link_for(row, want_ua)
        return JSONResponse({
            "id": item_id,
            "url": url,
            "expires": expires,
            "ua": signed or want_ua,
            "kind": row["kind"],
            "name": ic.safe_unseal(row["name_enc"] or ""),
            "size": row["size"] or 0,
        })

    @app.get("/api/play/{item_id}")
    async def play(item_id: str, request: Request, ua: str = Query("")):
        row = db.get_item(item_id)
        if not row:
            raise HTTPException(404, "没有这个媒体项")
        if not _acc_can(_who(request), row):
            raise HTTPException(404, "没有这个媒体项")
        if not row:
            raise HTTPException(404, "没有这个媒体项")
        if row["kind"] != KIND_VIDEO:
            raise HTTPException(400, "这一项不是视频")
        url, expires, ua_signed = await _link_for(row, _client_ua(request, ua))
        return RedirectResponse(url, status_code=302, headers={
            "X-Link-Expires": str(expires), "Cache-Control": "no-store",
            # 客户端必须用这个 UA 去 CDN 取流（浏览器/播放器默认就是自己的 UA）
            "X-Upstream-UA": ua_signed or _client_ua(request, ua),
            "Cache-Control": "no-store",
        })

    # ------------------------------------------------------------------ 进度
    @app.post("/api/progress")
    async def progress(body: dict = Body(...)):
        item_id = str((body or {}).get("id") or "")
        if not item_id or not db.get_item(item_id):
            raise HTTPException(404, "没有这个媒体项")
        db.save_progress(item_id, float(body.get("position") or 0),
                         float(body.get("duration") or 0))
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ 自检
    @app.get("/api/dirs")
    async def dirs(request: Request, parent: str = Query("-1")):
        """可选目录：给了 list_dirs 就实时问上游，否则回退到已入库的目录树。

        ★ req-accounts：**普通账户一律走本地库并限定授权子树**。
        原因有两条：① 实时问上游会暴露管理员整个 115 盘的目录结构；
        ② 上游列举不受我们的授权集合约束，无法保证"不显示未授权目录"。
        """
        from . import accounts as _acc
        _u = _who(request)
        _allowed = _acc.allowed_folder_ids(_u)
        if _allowed is not None:
            if not _acc.can_see_folder(_u, parent):
                raise HTTPException(404, "没有这个目录")
            rows = db.list_folders(parent)
            return JSONResponse({"parent": parent, "source": "local", "folders": [
                {"id": r["id"], "name": ic.safe_unseal(r["name_enc"] or "")}
                for r in rows if str(r["id"]) in _allowed]})
        if list_dirs is not None:
            raw = await list_dirs(parent)
            # 兼容两种返回：{"folders":[...], "path":[...]} 或直接 [ ... ]
            children = (raw.get("folders") or []) if isinstance(raw, dict) else list(raw or [])
            crumbs = (raw.get("path") or []) if isinstance(raw, dict) else []
            out = {
                "parent": parent, "source": "upstream",
                "folders": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in children],
                "crumbs": [{"id": str(c.get("cid") or c.get("id") or "0"),
                            "name": c.get("name", "")} for c in crumbs],
            }
            if isinstance(raw, dict) and raw.get("truncated"):
                out["truncated"] = True
            return JSONResponse(out)
        rows = db.list_folders(parent)
        return JSONResponse({"parent": parent, "source": "local", "folders": [
            {"id": r["id"], "name": ic.safe_unseal(r["name_enc"] or "")} for r in rows]})

    @app.post("/api/scan")
    async def scan_start(body: dict = Body(...)):
        if scanner is None:
            raise HTTPException(501, "扫描器未注入（scanner=None）")
        root_id = str((body or {}).get("root_id") or "-1")
        raw_depth = (body or {}).get("depth")
        try:
            depth = int(raw_depth) if raw_depth is not None else 0
        except (TypeError, ValueError):
            depth = 0
        if depth < 0:
            depth = 0            # <=0 一律表示「不限深度，扫到叶子」
        kinds = (body or {}).get("kinds") or []
        running = db.active_job()
        if running and running["status"] in ("pending", "running"):
            raise HTTPException(409, f"已有任务在跑：{running['id']}（先暂停或取消）")
        job_id = scanner.start(root_id, depth=depth, root_name=str((body or {}).get("root_name") or ""),
                               kinds=kinds)
        scanner.run_in_background(job_id)
        return JSONResponse({"job_id": job_id, "status": "started",
                             "root_id": root_id, "depth": depth})

    @app.get("/api/scan")
    async def scan_list(limit: int = Query(10, ge=1, le=50)):
        jobs = []
        for j in db.jobs(limit):
            d = dict(j)
            d["root_name"] = ic.safe_unseal(d.pop("root_name_enc", "") or "")
            d.update(db.queue_size(d["id"]))
            jobs.append(d)
        active = db.active_job()
        return JSONResponse({"jobs": jobs,
                             "active": dict(active)["id"] if active else None})

    @app.get("/api/scan/{job_id}")
    async def scan_status(job_id: str):
        if scanner is None:
            raise HTTPException(501, "扫描器未注入")
        st = scanner.status(job_id)
        if not st:
            raise HTTPException(404, "没有这个任务")
        return JSONResponse(st)

    @app.post("/api/scan/{job_id}/{action}")
    async def scan_control(job_id: str, action: str):
        if scanner is None:
            raise HTTPException(501, "扫描器未注入")
        if not db.job(job_id):
            raise HTTPException(404, "没有这个任务")
        if action == "pause":
            scanner.pause(job_id)
        elif action == "resume":
            scanner.resume(job_id)
        elif action == "cancel":
            scanner.cancel(job_id)
        else:
            raise HTTPException(400, f"未知动作 {action}")
        return JSONResponse(scanner.status(job_id))

    @app.get("/api/health")
    async def health():
        st = fetcher.stats()
        return JSONResponse({
            "ok": True,
            "kind": "115emby（文件夹驱动 · 看图 + 播放，不刮削不转码）",
            "library": db.stats(),
            "thumbs": {k: st.get(k) for k in ("thumbs", "disk_mb", "queue", "enc", "key_fp")},
            "has_resolver": resolve_link is not None,
            "has_scanner": scanner is not None,
            "scan": dict(db.active_job())["id"] if db.active_job() else None,
        })

    return app
