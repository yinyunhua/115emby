"""缩略图抓取器：懒抓（C 方案）+ 夜间时间窗预热 + 断点续传。

三条入口
--------
1. ``ensure(photo)``：**懒抓**。用户第一次看到某张图时抓一次，之后永久命中本地。
2. ``prewarm_batch(get_photo, limit)``：抓一小批（供定时器/CLI 调用）。
3. ``run_daemon(get_photo, stop)``：**夜间时间窗**守护循环，到点自动开始、到点自动收工。

风控预算
--------
* 每一次上游请求都必须穿过 :class:`app.throttle.Throttle`
  （``API_MIN_INTERVAL=1.0`` / ``API_CONCURRENCY=2``，与 115-album 同名同义）；
* 每 ``PREFETCH_REST_EVERY=200`` 次调用整体休息 ``PREFETCH_REST_SECONDS=5`` 秒；
* 每夜容量 ``THUMB_PREWARM_MAX_PER_NIGHT``，0 时按"窗口分钟数 ÷ 间隔"自动推算；
* 视频且上游无封面（``photo.thumb`` 为空）时**不发请求**直接跳过 —— 115 不给视频封面，
  补一次 downurl 也是空的（115-album 里 2231 个视频就是 2231 次无谓请求）。

断点续传
--------
* 队列状态在 ``thumb_src``（pending/done/skipped/failed），进程重启不丢；
* 每抓完一张写一次 ``kv['prewarm_cursor'] = pid``，并且 ``updated`` 时间戳升序取批，
  所以重启后自然从"最久未动"的记录继续；
* 每夜进度记在 ``kv['prewarm_night'] / kv['prewarm_night_done']``，过零点自动归零重算。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import sqlite3
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

from . import secretbox
from .settings import Settings
from .video_thumb import VideoThumbOptions
from .thumb_store import (
    STATE_DONE,
    STATE_FAILED,
    STATE_SKIPPED,
    STATE_EXCLUDED,
    ThumbStore,
)
from .throttle import Throttle
from .window import Window

log = logging.getLogger("album.thumbfetch")

# 允许被浏览器直接显示的容器；用于快速判定"这不是图"
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",                       # JPEG
    b"\x89PNG\r\n\x1a\n",                  # PNG
    b"GIF87a",
    b"GIF89a",
)
# 115 对「无法生成缩略图」的文件统一返回这张 3122 字节 PNG（实测 sha1 固定）
_PLACEHOLDER_SHA1 = '11077b9e372187e268ac4a9e954cd7b71eeb35d2'

# 视频扩展名（用于兜底判断）：调用方若传了 is_video=False 的兜底对象，
# 只要扩展名在这个集合里就仍按视频走抓帧，绝不放它进"上游占位图"的图片分支。
_VIDEO_EXTS = {
    "mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "asf", "flv", "rm", "rmvb",
    "ts", "m2ts", "mts", "mpg", "mpeg", "vob", "3gp", "3g2", "ogv", "divx", "f4v",
}

_PLACEHOLDER_MAX_BYTES = 8 * 1024          # 小图才登记"重复指纹"


@dataclass(slots=True)
class FetchResult:
    data: bytes | None = None
    etag: str = ""
    label: str = "MISS"      # HIT / FETCHED / SKIPPED / FAILED / MISS

    @property
    def ok(self) -> bool:
        return self.data is not None


def _pid(photo: Any) -> str:
    return str(getattr(photo, "id", "") or "")


def _looks_image(raw: bytes) -> bool:
    if not raw:
        return False
    if raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    return raw.startswith(b"BM")           # BMP，极少数老图


class ThumbFetcher:
    def __init__(
        self,
        store: ThumbStore,
        settings: Optional[Settings] = None,
        *,
        fetch_bytes: Callable[[Any], Awaitable[Optional[bytes]]] | None = None,
        video_fetch: Callable[[Any], Awaitable[Optional[bytes]]] | None = None,
        video_opts: "VideoThumbOptions | None" = None,
        now: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or Settings.from_env()
        self._fetch_bytes = fetch_bytes
        self._video_fetch = video_fetch
        self._origin_fetch = None
        self.video_opts = video_opts or VideoThumbOptions.from_env()
        self._now = now or time.time
        self._clock = clock or datetime.now
        self._throttle = Throttle(self.settings.api_min_interval, self.settings.api_concurrency)
        # 解码/缩放也要限并发：它比上游请求更吃内存（Pillow 解码 12MP ≈ +120MB）
        self._encode_sem = asyncio.Semaphore(max(1, self.settings.encode_concurrency))
        self.peak_encode = 0
        self._encode_now = 0
        self._rest_counter = 0
        self._lru: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
        self._lru_bytes = 0
        # ★ 占位图判定计数：原先按"内容 sha1"全局累计且无上限 → 同一张**正常**小图
        #   出现第 6 次后，所有内容相同的条目都被判占位图（永久 skipped）。
        #   改为按 (pid, sha1) 记录、有界（超出丢最旧）。
        self._placeholder_seen: "OrderedDict[tuple[str, str], int]" = OrderedDict()
        self._placeholder_seen_max = 4000
        self.counters: Counter[str] = Counter()

    # ------------------------------------------------------------------ 工具
    def bind_fetch(self, fetch_bytes: Callable[[Any], Awaitable[Optional[bytes]]]) -> None:
        """注入"如何取一张上游缩略图"（由 upstream_adapter 提供）。"""
        self._fetch_bytes = fetch_bytes

    def bind_origin(self, origin_fetch: Callable[[Any], Awaitable[Optional[bytes]]]) -> None:
        """注入"取原图"（占位图/无图/超限时的兜底：下载原图，本地缩成缩略图）。"""
        self._origin_fetch = origin_fetch

    def bind_video(self, video_fetch: Callable[[Any], Awaitable[Optional[bytes]]]) -> None:
        """注入"如何给视频抓一帧"（upstream_adapter.make_video_fetch，内部走 ffmpeg）。"""
        self._video_fetch = video_fetch

    def _lru_get(self, pid: str) -> tuple[bytes, str] | None:
        hit = self._lru.get(pid)
        if hit is not None:
            self._lru.move_to_end(pid)
        return hit

    def _lru_put(self, pid: str, data: bytes, etag: str) -> None:
        cap = max(0, self.settings.lru_mb) * 1024 * 1024
        if cap <= 0:
            return
        old = self._lru.pop(pid, None)
        if old:
            self._lru_bytes -= len(old[0])
        self._lru[pid] = (data, etag)
        self._lru_bytes += len(data)
        while self._lru_bytes > cap and self._lru:
            _, (evicted, _e) = self._lru.popitem(last=False)
            self._lru_bytes -= len(evicted)

    async def _call_upstream(self, coro_factory: Callable[[], Awaitable[Optional[bytes]]]) -> Optional[bytes]:
        """所有上游请求的唯一出口：并发闸门 + 最小间隔 + 节奏休息。"""
        await self._throttle.acquire()
        try:
            self._throttle.note_call()
            return await coro_factory()
        finally:
            self._throttle.release()
            self._rest_counter += 1
            if self.settings.rest_every > 0 and self._rest_counter % self.settings.rest_every == 0:
                self._throttle.note_rest()
                log.info(
                    "已调用 %d 次，按 PREFETCH_REST_EVERY 休息 %.1fs",
                    self._rest_counter, self.settings.rest_seconds,
                )
                await asyncio.sleep(max(0.0, self.settings.rest_seconds))

    # ------------------------------------------------------------------ 编码
    def encode(self, raw: bytes) -> tuple[bytes, int, int] | None:
        """把上游图重编码为 ``THUMB_SIZE`` 长边的渐进式 JPEG。"""
        try:
            from PIL import Image, ImageOps
        except Exception:  # noqa: BLE001
            log.warning("未安装 Pillow，退化为原样存储")
            return raw, 0, 0
        try:
            # 30MB 原图可能对应上亿像素：先设上限防止"解压炸弹"，再用 draft 让 JPEG 按比例解码
            try:
                Image.MAX_IMAGE_PIXELS = 60_000_000
            except Exception:
                pass
            with Image.open(io.BytesIO(raw)) as im:
                try:
                    im.draft("RGB", (self.settings.size * 4, self.settings.size * 4))
                except Exception:
                    pass
                if im.size[0] * im.size[1] > 60_000_000:
                    log.info("原图过大（%dx%d），跳过解码", im.size[0], im.size[1])
                    return None
                im = ImageOps.exif_transpose(im)
                if im.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    im = im.convert("RGBA")
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                im.thumbnail((self.settings.size, self.settings.size), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(
                    buf, format="JPEG",
                    quality=self.settings.quality, optimize=True, progressive=True,
                )
                return buf.getvalue(), im.width, im.height
        except Exception as exc:  # noqa: BLE001
            log.info("缩略图重编码失败：%s", exc)
            return None

    def _is_placeholder(self, pid: str, raw: bytes) -> bool:
        """识别 115 的"图片已过期"占位图：靠"同一份小内容重复出现"判定。

        115 正常缩略图中位数只有 3998 字节，与占位图 3122 字节非常接近，
        **不能按体积筛**；它真正稳定的特征是内容完全相同。
        """
        if len(raw) > _PLACEHOLDER_MAX_BYTES:
            return False
        h = hashlib.sha1(raw).hexdigest()
        if h == _PLACEHOLDER_SHA1:      # 115 固定的"无法生成缩略图"占位图
            return True
        key = (pid, h)
        n = self._placeholder_seen.get(key, 0)
        if n >= self.settings.placeholder_repeat:
            return True
        self._placeholder_seen[key] = n + 1
        self._placeholder_seen.move_to_end(key)
        while len(self._placeholder_seen) > self._placeholder_seen_max:
            self._placeholder_seen.popitem(last=False)
        return n + 1 > self.settings.placeholder_repeat

    # ------------------------------------------------------------------ 懒抓
    async def _settle_done(self, pid: str) -> None:
        """★ req-lazydone：命中缓存时把队列状态收敛成 done。

        为什么要做：``ensure()`` 的两处 HIT 分支直接 return、不写状态，
        而 ``ensure()`` 是懒抓（``/api/thumb``）与预热共用的入口 ——
        用户浏览时图已在库（HIT），state 却还停在 pending，
        于是「待生成」会虚高，而且 next_batch 会反复取走这些行白跑。

        只在确实不是 done 时才写，避免每次命中都打一次库。
        """
        try:
            st = await asyncio.to_thread(self.store.state_of, pid)
            if st and st != STATE_DONE:
                await asyncio.to_thread(self.store.mark, pid, STATE_DONE)
        except Exception:                              # noqa: BLE001
            pass

    async def ensure(self, photo: Any, *, allow_fetch: bool = True,
                     _direct: bool = False) -> FetchResult:
        pid = _pid(photo)
        if not pid:
            return FetchResult(None, "", "SKIPPED")

        hit = self._lru_get(pid)
        if hit:
            self.counters["HIT"] += 1
            await self._settle_done(pid)               # ★ req-lazydone
            return FetchResult(hit[0], hit[1], "HIT")

        try:
            row = await asyncio.to_thread(self.store.get, pid)
        except secretbox.DecryptError as exc:
            # 换过密钥 / 密文损坏：删掉这条重抓，绝不让它变成 500
            log.warning("缩略图解密失败，已删除待重抓：%s（%s）", pid, exc)
            await asyncio.to_thread(self.store.delete, pid)
            row = None
        if row:
            data, etag = row
            self._lru_put(pid, data, etag)
            self.counters["HIT"] += 1
            await self._settle_done(pid)               # ★ req-lazydone
            return FetchResult(data, etag, "HIT")

        if not allow_fetch or self._fetch_bytes is None:
            self.counters["MISS"] += 1
            return FetchResult(None, "", "MISS")

        # 视频：**一律自己抓帧**。115 给视频的"上游封面"绝大多数是占位图，
        # 走图片路径只会拿到占位图然后被判 skipped（实测 367 个视频因此从没抓过帧）。
        #
        # ★ 再兜一层：判断"是不是视频"时**同时看扩展名**。
        # 原因是有的调用方会传一个只有 id 的兜底对象（is_video 默认 False），
        # 视频一旦被当成图片，就会走到下面"上游占位图"的分支 —— 而 skipped 是**终态**，
        # 于是这些视频永远拿不到缩略图（历史遗留 171 条这么来的）。
        # 用扩展名兜底后，无论调用方传什么对象，视频都只会走抓帧路径。
        ext_l0 = str(getattr(photo, "ext", "") or "").lower().lstrip(".")
        is_video = bool(getattr(photo, "is_video", False)) or ext_l0 in _VIDEO_EXTS
        has_cover = bool(str(getattr(photo, "thumb", "") or "").strip())
        if is_video:
            if self._video_fetch is None:
                await asyncio.to_thread(
                    self.store.mark, pid, STATE_EXCLUDED, "视频无上游封面且未绑定抓帧器"
                )
                self.counters["SKIPPED"] += 1
                return FetchResult(None, "", "SKIPPED")
            ext = str(getattr(photo, "ext", "") or "")
            allowed, why = self.video_opts.should_attempt(ext)
            if not allowed:
                await asyncio.to_thread(self.store.mark, pid, STATE_EXCLUDED, f"抓帧跳过：{why}")
                self.counters["SKIPPED"] += 1
                return FetchResult(None, "", "SKIPPED")
            # 和图片一样：请求直接等到 ffmpeg 抓完，返回真图（大文件约 10–20 秒）。
            # 抓帧本身有串行闸门（video_gate，1 并发 + 1 秒间隔），不会并发吃内存。
            label_kind = "VIDEO"
        else:
            label_kind = "PHOTO"

        try:
            if label_kind == "VIDEO":
                raw = await self._call_upstream(lambda: self._video_fetch(photo))  # type: ignore[misc]
            else:
                raw = await self._call_upstream(lambda: self._fetch_bytes(photo))  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            log.info("抓取失败 %s：%s", pid, exc)
            await asyncio.to_thread(self.store.mark, pid, STATE_FAILED, str(exc), bump_attempt=True)
            self.counters["FAILED"] += 1
            return FetchResult(None, "", "FAILED")

        # ---- 原图兜底：占位图 / 空 / 超限 / 非图片 → 取原图，本地缩 ----
        ext_l = str(getattr(photo, "ext", "") or "").lower()
        from_origin = False        # raw 是否来自"原图兜底"（原图本来就大，不能吃缩略图的体积上限）
        bad = (not raw) or (len(raw) > self.settings.max_bytes) or (not _looks_image(raw))
        if not bad:
            try:
                bad = self._is_placeholder(pid, raw)
            except Exception:
                bad = False
        if bad and label_kind != "VIDEO" and self._origin_fetch is not None:
            if ext_l == "gif" and getattr(self.settings, "gif_skip", True):
                await asyncio.to_thread(self.store.mark, pid, STATE_EXCLUDED, "GIF 已排除")
                self.counters["SKIPPED"] += 1
                return FetchResult(None, "", "SKIPPED")
            log.info("缩略图不可用（%s），改取原图：%s", "占位图/无图/超限", pid)
            try:
                raw2 = await self._call_upstream(lambda: self._origin_fetch(photo))  # type: ignore[misc]
            except Exception as exc:      # noqa: BLE001
                log.info("取原图失败 %s：%s", pid, exc)
                raw2 = None
            if raw2 and len(raw2) <= self.settings.origin_max_bytes and _looks_image(raw2):
                raw = raw2
                from_origin = True
                log.info("原图已取回 %s（%d 字节），本地缩略", pid, len(raw2))
            else:
                log.info("原图也不可用 %s（%s 字节）", pid, len(raw2) if raw2 else 0)

        if not raw:
            if label_kind == "VIDEO":
                # moov 在尾部 / 超时 / 编解码不支持 —— 记为 failed，走退避与次数上限，
                # 不像 skipped 那样永不重试（大文件偶发慢，重试常常就好了）。
                await asyncio.to_thread(
                    self.store.mark, pid, STATE_FAILED,
                    "抓帧失败（moov 位置/超时/格式）", bump_attempt=True,
                )
                self.counters["FAILED"] += 1
                return FetchResult(None, "", "FAILED")
            await asyncio.to_thread(self.store.mark, pid, STATE_FAILED, "上游无缩略图", bump_attempt=True)
            self.counters["SKIPPED"] += 1
            return FetchResult(None, "", "SKIPPED")
        # 体积上限：上游缩略图按 max_bytes（默认 300KB，超了直接放弃）；
        # 但"原图兜底"取回来的原图**本来就该是 MB 级**，用 300KB 卡它会白白丢掉
        # （实测 gc2048.com-DSC04170.JPG 9.01MB 就因此被判"体积超限"跳过）。
        # 原图只按 origin_max_bytes 判，交给 encode()（draft 缩比解码 + 6000 万像素上限）去缩。
        # 先判"是不是图片"：损坏文件（如 115 上的全零数据）体积再大也不该报"体积超限"，
        # 否则后台显示的原因会误导（实测 gc2048.com-DSC04170.JPG 9.4MB 全零被误报为体积超限）。
        if not _looks_image(raw):
            await asyncio.to_thread(self.store.mark, pid, STATE_FAILED, "非图片内容（文件可能损坏）", bump_attempt=True)
            self.counters["SKIPPED"] += 1
            return FetchResult(None, "", "SKIPPED")
        _limit = self.settings.origin_max_bytes if from_origin else self.settings.max_bytes
        if len(raw) > _limit:
            await asyncio.to_thread(
                self.store.mark, pid, STATE_FAILED, f"体积超限 {len(raw)}B", bump_attempt=True
            )
            self.counters["SKIPPED"] += 1
            return FetchResult(None, "", "SKIPPED")
        # ★ 占位图识别只对"图片路径"有效。
        # 抓帧回来的帧永远不会是 115 的占位图，但**同一系列视频的首帧常常完全相同**
        # （实测 30-5 (66)~(71).mp4 首帧都是同一张 737 字节的图），
        # 于是"同内容重复 ≥5 次即判占位图"的启发式会把它们全部误判成
        # skipped「上游占位图」—— 而 skipped 是终态，这些视频就永远拿不到缩略图。
        # 所以视频一律跳过这个检查。
        try:
            _is_ph = label_kind != "VIDEO" and self._is_placeholder(pid, raw)
        except Exception as exc:                       # noqa: BLE001
            # ★ 占位图判定是"锦上添花"，绝不能因为它抛异常让整个请求挂死
            #   （曾因 _is_placeholder 里漏传 pid 触发 NameError → 缩略图请求悬挂 15s+、
            #     信号量不释放，表现为"随机发现页要等十几秒"）
            log.warning("占位图判定异常（已忽略）%s：%s", pid, exc)
            _is_ph = False
        if _is_ph:
            await asyncio.to_thread(self.store.mark, pid, STATE_FAILED, "上游占位图", bump_attempt=True)
            self.counters["SKIPPED"] += 1
            return FetchResult(None, "", "SKIPPED")

        async with self._encode_sem:
            self._encode_now += 1
            self.peak_encode = max(self.peak_encode, self._encode_now)
            try:
                encoded = await asyncio.to_thread(self.encode, raw)
            finally:
                self._encode_now -= 1
        if encoded is None:
            await asyncio.to_thread(self.store.mark, pid, STATE_FAILED, "重编码失败", bump_attempt=True)
            self.counters["SKIPPED"] += 1
            return FetchResult(None, "", "SKIPPED")

        jpeg, w, h = encoded
        etag = hashlib.sha256(jpeg).hexdigest()[:32]
        await asyncio.to_thread(
            self.store.put, pid, jpeg, etag=etag, w=w, h=h, encaps=secretbox.active_alg()
        )
        await asyncio.to_thread(self.store.mark, pid, STATE_DONE)
        self._lru_put(pid, jpeg, etag)
        self.counters["FETCHED"] += 1
        if label_kind == "VIDEO":
            self.counters["VIDEO"] += 1
        return FetchResult(jpeg, etag, "FETCHED")

    # ------------------------------------------------------------------ 队列
    def enqueue(self, photos: Iterable[Any]) -> int:
        """把待抓项塞进队列（建库时顺手调用；URL 会过期，所以要趁热入队）。"""
        items = []
        for p in photos:
            pid = _pid(p)
            if not pid:
                continue
            items.append((pid, str(getattr(p, "thumb", "") or ""), 0))
        return self.store.enqueue(items)

    async def prewarm_batch(
        self,
        get_photo: Callable[[str], Awaitable[Any]],
        limit: int,
    ) -> dict[str, int]:
        """抓一小批：pending 优先，其次到点重试的 failed。"""
        rows = await asyncio.to_thread(
            self.store.next_batch,
            limit,
            retry_backoff_s=int(self.settings.retry_hours * 3600),
            max_attempts=self.settings.max_attempts,
        )
        done = {"total": len(rows), "FETCHED": 0, "SKIPPED": 0, "FAILED": 0, "HIT": 0}
        for row in rows:
            pid = row["pid"]
            photo = await get_photo(pid)
            if photo is None:
                await asyncio.to_thread(self.store.mark, pid, STATE_EXCLUDED, "上游已无此文件")
                done["SKIPPED"] += 1
                continue
            if row["url"] and not getattr(photo, "thumb", ""):
                try:
                    photo.thumb = row["url"]
                except Exception:  # noqa: BLE001  (slots 对象)
                    pass
            result = await self.ensure(photo, allow_fetch=True)
            done[result.label] = done.get(result.label, 0) + 1
            # ★ req-thumbstate：命中缓存（HIT）也必须把队列状态收敛成 done。
            #   ensure() 只有"真正抓取成功"那一处会 mark(DONE)，缓存命中直接 return
            #   不写状态 —— 于是"图已在库、状态却停在 pending"的行
            #   （典型来源：tools/thumb_prewarm.py reset 把 failed 重置成 pending）
            #   会被 next_batch 按 updated ASC **每轮反复取走**、每轮命中缓存、永不写 done
            #   → 永久卡在"等待生成"，还白计夜间预算。
            #   这里按结果收敛：抓到图或本来就有图 → done。
            if result.ok:
                await asyncio.to_thread(self.store.mark, pid, STATE_DONE)
            await asyncio.to_thread(self.store.kv_set, "prewarm_cursor", pid)
        return done

    # ------------------------------------------------------------------ 夜间窗口
    async def run_window(
        self,
        get_photo: Callable[[str], Awaitable[Any]],
        *,
        stop: asyncio.Event | None = None,
        window: Window | None = None,
        respect_window: bool = True,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """在一个时间窗内跑预热；窗口结束/暂停/收到 stop 就停（可续跑）。

        ``respect_window=False`` 表示"现在就跑，不看时间"（CLI ``--mode now``）。
        """
        win = window or Window.parse(self.settings.window)
        stop = stop or asyncio.Event()
        if not win.ok:
            log.warning("时间窗配置无效，按全天处理：%s", win.text)

        # max_items 给了就是"本次调用上限"（CLI --limit / 后台按钮），
        # 不读也不写夜间累计，方便人工手动补几批而不影响夜间预算。
        per_call = max_items is not None
        budget = max_items if per_call else self.settings.nightly_budget(win.duration_minutes)
        night = win.night_key(self._clock())
        used = 0
        if not per_call:
            stored_night = self.store.kv_get("prewarm_night", "")
            used = int(self.store.kv_get("prewarm_night_done", "0") or 0)
            if stored_night != night:
                used = 0
                await asyncio.to_thread(self.store.kv_set, "prewarm_night", night)
                await asyncio.to_thread(self.store.kv_set, "prewarm_night_done", "0")

        summary = {"night": night, "budget": budget, "used_before": used, "fetched": 0,
                   "skipped": 0, "failed": 0, "hit": 0, "stopped": "", "ticks": 0}

        chunk = max(1, min(self.settings.rest_every or 20, 50))
        while not stop.is_set():
            if respect_window:
                now = self._clock()
                if not win.contains(now):
                    summary["stopped"] = "window-closed"
                    break
            if self.store.kv_get("prewarm_paused", "0") == "1":
                summary["stopped"] = "paused"
                break
            if used >= budget:
                summary["stopped"] = "budget"
                break

            n = min(chunk, budget - used)
            stats = await self.prewarm_batch(get_photo, n)
            summary["ticks"] += 1
            summary["fetched"] += stats.get("FETCHED", 0)
            summary["skipped"] += stats.get("SKIPPED", 0)
            summary["failed"] += stats.get("FAILED", 0)
            summary["hit"] += stats.get("HIT", 0)
            used += stats.get("total", 0)
            if not per_call:
                await asyncio.to_thread(self.store.kv_set, "prewarm_night_done", str(used))

            if stats.get("total", 0) == 0:
                summary["stopped"] = "queue-empty"
                break
            if stop.is_set():
                summary["stopped"] = "stop"
                break
        if not summary["stopped"]:
            summary["stopped"] = "stop"
        log.info(
            "预热窗口结束：%s｜夜=%s｜本次 %d 张（已用 %d/%d）｜失败 %d",
            summary["stopped"], night, summary["fetched"], used, budget, summary["failed"],
        )
        return summary

    async def run_daemon(
        self,
        get_photo: Callable[[str], Awaitable[Any]],
        *,
        stop: asyncio.Event | None = None,
    ) -> None:
        """守护循环：窗口内跑 ``run_window``，窗口外睡到下次开门。"""
        stop = stop or asyncio.Event()
        win = Window.parse(self.settings.window)
        log.info("预热守护启动：时间窗 %s，节流 %.2fs/%d 并发，每夜上限 %d",
                 win.describe(), self.settings.api_min_interval,
                 self.settings.api_concurrency, self.settings.nightly_budget(win.duration_minutes))
        while not stop.is_set():
            now = self._clock()
            if win.contains(now) and self.store.kv_get("prewarm_paused", "0") != "1":
                await self.run_window(get_photo, stop=stop, window=win)
                if stop.is_set():
                    break
                # 窗口已结束/暂停/队列空 → 统一睡到下一个开门时刻
                await asyncio.sleep(max(5, self.settings.tick_seconds))
                continue
            nxt = win.next_open(now)
            wait = max(self.settings.tick_seconds, (nxt - now).total_seconds())
            log.info("不在时间窗内（现在 %s），下次开门 %s", now.strftime("%H:%M"), nxt.strftime("%m-%d %H:%M"))
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(wait, 900))
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ 状态
    def _count_never_queued(self) -> int:
        """★ req-scanqueue：items 里是媒体、但 thumb_src 里完全没有行的条目数。

        这些才是真正"等待生成"的积压：它们从未被 enqueue，
        所以 count_state() 的 GROUP BY state 里看不到它们。

        ⛔ **req-numfix2：绝对不要在 Python 里把两边全表拉出来做 set 差集**
        （14 万 + 14 万行 → 内存与 CPU 双爆，实测把 2G 内存的服务器拖垮）。
        正确做法：ATTACH 之后一条 ``NOT EXISTS``，让 SQLite 走 thumb_src.pid 主键。
        实测 **429 ms**（14.7 万 items / 14.5 万 thumb_src），可接受。

        ⛔ 原实现还错在 ``self._db()`` —— ``ThumbFetcher`` 没有这个方法
        （只有 ``ThumbStore`` 有），抛 AttributeError 被外层 except 吞掉 → 恒为 0，
        于是"等待生成"永远少 2385 项，两页数字对不上。

        缓存 **120 秒**：后台每 4 秒轮询一次，20 秒缓存等于每分钟跑 3 次全表比对。
        """
        import time as _t
        _c = getattr(self, "_nq_cache", None)
        if _c and _t.time() - _c[0] < 120:
            return int(_c[1])
        emby = os.getenv("EMBY_DB") or "/app/data/emby.db"
        thumb = os.getenv("THUMB_DB") or "/app/data/thumbs.db"
        with self.store._lock:
            con = sqlite3.connect(emby, timeout=30)
            try:
                con.execute("ATTACH DATABASE ? AS th", ("file:%s?mode=ro" % thumb,))
                # ★ req-neverq：必须套用与 enqueue_eligible **完全一致**的过滤，
                #   否则 GIF / <30KB 小图（永远抓不到）会被算成"从未入队"，
                #   把「等待生成」撑成"永远抓不到"的数（实测 2,385 条全是 GIF/小图）。
                gif_skip = bool(getattr(self.settings, "gif_skip", True))
                n = int(con.execute(
                    "SELECT COUNT(*) FROM items i "
                    "WHERE COALESCE(i.hidden,0)=0 AND i.kind IN ('image','video') "
                    "AND NOT (i.kind='image' AND COALESCE(i.size,0)>0 "
                    "         AND COALESCE(i.size,0) < 30000) "
                    "AND NOT (? AND lower(COALESCE(i.ext,''))='gif') "
                    "AND NOT EXISTS (SELECT 1 FROM th.thumb_src s WHERE s.pid = i.id)",
                    (1 if gif_skip else 0,)
                ).fetchone()[0])
            finally:
                con.close()
        self._nq_cache = (_t.time(), n)
        return n

    def _count_items_media(self) -> int:
        """★ req-numalign：items 里媒体（图片+视频）的总数，用于与队列口径核对。
        缓存 20 秒。"""
        import time as _t
        _c = getattr(self, "_im_cache", None)
        if _c and _t.time() - _c[0] < 20:
            return int(_c[1])
        emby = os.getenv("EMBY_DB") or "/app/data/emby.db"
        with self.store._lock:
            con = sqlite3.connect(emby, timeout=30)
            try:
                n = int(con.execute(
                    "SELECT COUNT(*) FROM items WHERE COALESCE(hidden,0)=0 "
                    "AND kind IN ('image','video')").fetchone()[0])
            finally:
                con.close()
        self._im_cache = (_t.time(), n)
        return n

    def stats(self) -> dict[str, Any]:
        out = self.store.stats()
        _q = out.get("queue") or {}
        _ex = int(out.get("queue_excluded") or 0)
        out["queue_bad_real"] = max(0, int(_q.get("failed") or 0) + int(_q.get("skipped") or 0) - _ex)
        # ★ req-scanqueue：真实待生成数。
        # _q["pending"] 只统计 thumb_src 里已有记录的行；从未入队的媒体
        # 在表里根本没有行，GROUP BY 数不到 → 概览会一直显示 0。
        # 这里跨库比对 items 与 thumb_src，把"从没进过队列"的也算进来。
        try:
            out["queue_never_queued"] = self._count_never_queued()
            out["queue_pending_real"] = int(_q.get("pending") or 0) + int(out["queue_never_queued"])
        except Exception as _e:
            # ⛔ 附五十二 铁律：**不要静默给 0**。给 0 会让字段"看起来正常"，
            # 掩盖掉真实错误（历史上 self._lock / self._db 两次都是这样被藏住的）。
            log.warning("never_queued 计算失败：%s: %s", type(_e).__name__, _e)
            out["queue_never_queued"] = 0
            out["queue_never_queued_err"] = "%s: %s" % (type(_e).__name__, str(_e)[:120])
            out["queue_pending_real"] = int(_q.get("pending") or 0)
        # ★ req-numalign：让"总数对得上"这件事可核对。
        # 扫描管理显示的是 items 媒体总数；概览显示的是 done/pending/excluded。
        # 三者关系：done + pending + count_excluded == items_media（含 hidden 的差异见 queue_total）
        try:
            # 注意：_q 来自 store.stats() 的缓存副本，这里必须重新取一次 count_state，
            # 否则缓存里没有 done/excluded 这些键时会算出 0（踩过）。
            _qs = self.store.count_state()
            out["queue_total"] = (int(_qs.get("done") or 0) + int(_qs.get("pending") or 0)
                                  + int(_qs.get("excluded") or 0) + int(_qs.get("skipped") or 0))
            out["items_media"] = self._count_items_media()
        except Exception as _e:
            out["queue_total"] = 0
            out["items_media"] = 0
            out["queue_total_err"] = str(_e)[:120]
        out.update(
            {
                "throttle": self._throttle.stat(),
                "lru_mb": round(self._lru_bytes / 1048576, 2),
                "encode_concurrency": self.settings.encode_concurrency,
                "peak_encode": self.peak_encode,
                "lru_items": len(self._lru),
                "counters": dict(self.counters),
                "window": Window.parse(self.settings.window).describe(),
                "nightly_budget": self.settings.nightly_budget(
                    Window.parse(self.settings.window).duration_minutes
                ),
                "size_px": self.settings.size,
                "quality": self.settings.quality,
                "video": {
                    "enabled": self.video_opts.enabled,
                    "bound": self._video_fetch is not None,
                    "seek_s": self.video_opts.seek,
                    "size_px": self.video_opts.size,
                    "probe_bytes": self.video_opts.max_bytes,
                    "threads": self.video_opts.threads,
                    "skip_exts": sorted(self.video_opts.skip_exts),
                    "lowpriority_exts": sorted(self.video_opts.lowpriority_exts),
                    "allow_lowpriority": self.video_opts.allow_lowpriority,
                },
                "enc": "AES-256-GCM" if secretbox.active_alg() == 1 else "HMAC-CTR(回退)",
                "key_fp": secretbox.key_fingerprint(),
            }
        )
        return out
