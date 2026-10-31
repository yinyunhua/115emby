"""与 115-album 对接的适配层（可选）。

新项目里不 import 旧项目，避免包名 ``app`` 冲突；这里只提供**契约**：
调用方把自己项目的三个函数喂进来即可。

    from app.upstream_adapter import PhotoLite, make_fetch_bytes, make_get_photo, enqueue_from_rows

    fetch_bytes = make_fetch_bytes(thumbs.fetch_upstream_thumb, source)   # 115-album 现成的
    get_photo   = make_get_photo(index_store)                             # 见下
    fetcher.bind_fetch(fetch_bytes)

``115-album`` 侧需要补一个按 id 取行的函数（5 行，加到 ``app/index_store.py``）::

    def get(pid: str):
        with _lock, _connect() as conn:
            row = conn.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

log = logging.getLogger("album.adapter")

VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "rmvb", "webm", "flv", "m3u8", "ts", "m2ts", "wmv"}


@dataclass
class PhotoLite:
    """缩略图抓取只需要这几个字段（故意不带 slots，便于回写 thumb）。"""

    id: str
    name: str = ""
    thumb: str = ""
    size: int = 0
    ctime: int = 0
    ext: str = ""
    type: int = 0

    @property
    def is_video(self) -> bool:
        if self.type == 2:                      # models.TYPE_VIDEO
            return True
        return (self.ext or "").lower().lstrip(".") in VIDEO_EXTS


def make_fetch_bytes(
    fetch_upstream_thumb: Callable[..., Awaitable[bytes | None]],
    source: Any,
) -> Callable[[Any], Awaitable[bytes | None]]:
    """把 115-album 的 ``thumbs.fetch_upstream_thumb`` 包成 fetcher 需要的签名。

    它内部已经做了：地址过期自动换新、占位图识别、视频无封面直接返回 None。
    """

    async def _fetch(photo: Any) -> bytes | None:
        return await fetch_upstream_thumb(source, photo)

    return _fetch


def make_get_photo(
    get_row: Callable[[str], dict | None],
    *,
    ext_of: Callable[[str], str] | None = None,
) -> Callable[[str], Awaitable[PhotoLite | None]]:
    """用"按 id 取行"的回调造 ``get_photo``；行不存在返回 None（会被标记 skipped）。"""

    async def _get(pid: str) -> PhotoLite | None:
        row = get_row(pid)
        if not row:
            return None
        name = str(row.get("name") or "")
        ext = (ext_of(name) if ext_of else (name.rsplit(".", 1)[-1] if "." in name else ""))
        return PhotoLite(
            id=str(row.get("id") or pid),
            name=name,
            thumb=str(row.get("thumb") or ""),
            size=int(row.get("size") or 0),
            ctime=int(row.get("ctime") or 0),
            ext=ext,
            type=int(row.get("type") or 0),
        )

    return _get


def enqueue_from_rows(fetcher: Any, rows: Iterable[dict], *, limit: int | None = None) -> int:
    """把索引行批量入队（建库时调用一次）。

    只入队"图片/视频且非目录"的行；``thumb`` 为空也入队 ——
    抓取时若确实没有上游封面会被标记 ``skipped``，不会反复重试。
    """
    items = []
    for i, row in enumerate(rows):
        if limit is not None and i >= limit:
            break
        pid = str(row.get("id") or "")
        if not pid:
            continue
        if int(row.get("type") or 0) == 1:      # TYPE_FOLDER
            continue
        items.append((pid, str(row.get("thumb") or ""), 0))
    return fetcher.store.enqueue(items)

def make_video_fetch(
    video_thumb: Callable[..., Awaitable[bytes | None]],
    source: Any,
) -> Callable[[Any], Awaitable[bytes | None]]:
    """把 115-album 的 ``thumbs.video_thumb(source, photo)`` 包成 fetcher 的签名。

    它内部：拿直链（带 UA）→ ffmpeg 只读头部若干 MB 抓一帧 → 返回 JPEG 字节。
    抓不到（moov 在尾部 / 超时 / 非视频）返回 None，调用方会标记 skipped。
    """

    async def _fetch(photo: Any) -> bytes | None:
        return await video_thumb(source, photo)

    return _fetch


def make_video_fetch_direct(
    options: Any = None,
    link_resolver: Callable[[Any], Awaitable[str]] | None = None,
) -> Callable[[Any], Awaitable[bytes | None]]:
    """不依赖 115-album 的直连版本：自己解析直链 + ffmpeg 抓帧。

    ``link_resolver(photo)`` 需返回带签名的 115 直链（可为空字符串）。
    """
    from .video_thumb import VideoThumbOptions, grab_frame

    opts = options or VideoThumbOptions.from_env()

    async def _fetch(photo: Any) -> bytes | None:
        if link_resolver is None:
            return None
        url = await link_resolver(photo)
        if not url:
            return None
        return await grab_frame(url, opts)

    return _fetch
