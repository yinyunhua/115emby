"""缩略图取图 v4：按需换签名 + 拦截占位图 + **取 480px 原图**（原来只取 100px）

发现
----
115 的缩略图 URL 带尺寸后缀（``..._100?t=...``），我们扫描时拿到的都是 ``_100``，
取回来只有 100px 宽，再编码到 384px 就是放大糊图。
实测把后缀换成 ``_480`` 可用（``_250`` / ``_1000`` 会 404），体积约 31KB。

逻辑
----
1. 取图前把尺寸后缀改写成 ``THUMB_UPSTREAM_SIZE``（默认 480，与相册一致）；
2. 检查 ``t=`` 签名，超过 ``THUMB_URL_FRESH_S``（默认 600 秒）就重列父目录换新签名；
3. 取回 < 2KB 判为「图片已过期」占位图：不入库，换新签名再试一次；
4. 图片走独立油门（``THUMB_IMG_MIN_INTERVAL`` / ``THUMB_IMG_CONCURRENCY``），不占 API 预算。
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
import urllib.parse

from . import index_crypto as ic
from . import source115

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"
SIZE_RE = re.compile(r"_\d+(?=\?|$)")

PLACEHOLDER_BYTES = 2048


class Gate:
    def __init__(self, interval: float, concurrency: int) -> None:
        self.interval = max(0.0, float(interval))
        self.sem = asyncio.Semaphore(max(1, int(concurrency)))
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self.calls = 0

    async def __aenter__(self):
        await self.sem.acquire()
        if self.interval > 0:
            async with self._lock:
                now = time.monotonic()
                slot = max(now, self._next_at)
                self._next_at = slot + self.interval
            delay = slot - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        self.calls += 1
        return self

    async def __aexit__(self, *exc):
        self.sem.release()
        return False


def _sign_ts(url: str) -> int:
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return int((q.get("t") or [0])[0])
    except Exception:
        return 0


def _bigger(url: str, size: int) -> str:
    if not url or size <= 0:
        return url
    return SIZE_RE.sub("_%d" % size, url, count=1)


def _looks_placeholder(data) -> bool:
    """判断拿到的字节是不是 115 的「图片已过期」占位图。

    ★ req-thumbpx：原来**只看体积 < 2048B**，而「图片已过期」那张实测是
    **6402 字节**（384x384 灰底 JPEG）——远超阈值，于是被当成真图入库，
    界面就显示一张"图片已过期"。换签名逻辑救不了它（签名是新鲜的）。

    判据（**精确匹配，不做泛化**）——依据实测：

    | 项 | 值 |
    |---|---|
    | 尺寸 | **384 x 384**（正方形；真图多是 288x384 / 384x216） |
    | 均值 RGB | **241, 241, 241**（浅灰底） |
    | 标准差 | **15.9** |

    ⛔ 曾用泛化阈值 `mean>200 and sd<25`，**误杀了真图**（espuri.c 浅色渐变
    logo，sd=24.5）。所以这里必须**同时卡尺寸 + 均值 + 标准差**，三者都要命中。

    ⛔ 纯黑(sd=0)/纯暗红(sd=6.8) 那类坏图**不在本判据范围内**（用户明确要求
    只判定这一种"图片已过期"），避免误伤。
    """
    if not data or len(data) < PLACEHOLDER_BYTES:
        return True
    try:
        import io as _io
        from PIL import Image, ImageStat
        im = Image.open(_io.BytesIO(data)).convert("RGB")
        if im.size != (384, 384):          # 只认正方形 384x384
            return False
        st = ImageStat.Stat(im)
        mean = [float(x) for x in st.mean]
        sd = sum(st.stddev) / 3.0
        # 均值三个通道都贴近 241（容差 ±8），标准差贴近 15.9（区间 12~21）
        near_grey = all(abs(m - 241.0) <= 8.0 for m in mean)
        if near_grey and 12.0 <= sd <= 21.0:
            return True
        return False
    except Exception:                      # noqa: BLE001
        return True                        # 解不开的按占位图处理

def _update_thumb_enc(pairs: dict) -> int:
    if not pairs:
        return 0
    con = sqlite3.connect(EMBY, timeout=30)
    try:
        n = 0
        for pid, sealed in pairs.items():
            n += con.execute("UPDATE items SET thumb_enc=? WHERE id=?", (sealed, pid)).rowcount
        con.commit()
        return n
    finally:
        con.close()


def install(app) -> None:
    db = getattr(app.state, "db", None)
    fetcher = getattr(app.state, "fetcher", None)
    if db is None or fetcher is None:
        return

    interval = float(os.getenv("THUMB_IMG_MIN_INTERVAL") or 0.6)
    conc = max(1, int(os.getenv("THUMB_IMG_CONCURRENCY") or 2))
    fresh_s = float(os.getenv("THUMB_URL_FRESH_S") or 600)
    cooldown = float(os.getenv("THUMB_RELIST_COOLDOWN_S") or 600)
    upstream_size = int(os.getenv("THUMB_UPSTREAM_SIZE") or 480)

    gate = Gate(interval, conc)
    relist_at: dict[str, float] = {}
    stats = {"relisted": 0, "placeholder": 0, "fresh_hits": 0, "patched": 0, "upscaled": 0}

    async def _relist(parent: str) -> dict:
        now = time.monotonic()
        if now - relist_at.get(parent, 0.0) < cooldown:
            return {}
        relist_at[parent] = now
        listing = await source115.list_dir(parent)
        fresh = {str(f.get("id")): str(f.get("thumb") or "")
                 for f in (listing.get("files") or []) if f.get("id") and f.get("thumb")}
        if fresh:
            sealed = {pid: ic.seal_link(u) for pid, u in fresh.items()}
            stats["patched"] += await asyncio.to_thread(_update_thumb_enc, sealed)
        stats["relisted"] += 1
        return fresh

    async def _fetch(url: str):
        import httpx

        async with gate:
            headers = {"User-Agent": source115.UA, "Referer": "https://115.com/"}
            try:
                async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as c:
                    r = await c.get(url, headers=headers)
            except Exception:
                return None
        return r.content if r.status_code == 200 else None

    async def fetch_bytes(item_photo):
        pid = str(getattr(item_photo, "id", "") or "")
        url = str(getattr(item_photo, "thumb", "") or "")
        row = db.get_item(pid)
        if not url and row is not None:
            try:
                url = ic.safe_unseal_link(row["thumb_enc"] or "")
            except Exception:
                url = ""
        if not url:
            return None
        parent = str(row["parent_id"] or "") if row is not None else ""

        # 1) 签名过旧 → 换新
        ts = _sign_ts(url)
        if parent and ts and (time.time() - ts) > fresh_s:
            try:
                fresh = await _relist(parent)
            except Exception:
                fresh = {}
            if fresh.get(pid):
                url = fresh[pid]
                stats["fresh_hits"] += 1

        # 2) 尺寸后缀放大到 480（原图太小，编码到 384 会糊）
        big = _bigger(url, upstream_size)
        data = await _fetch(big)
        if data:
            stats["upscaled"] += 1
        else:
            data = await _fetch(url)          # 放大版拿不到就退回原尺寸

        if data and not _looks_placeholder(data):
            return data

        # 3) 占位图：换新签名再试一次
        stats["placeholder"] += 1
        if parent:
            try:
                fresh = await _relist(parent)
            except Exception:
                fresh = {}
            new_url = fresh.get(pid, "")
            if new_url:
                data = await _fetch(_bigger(new_url, upstream_size))
                if data and not _looks_placeholder(data):
                    return data
        return None

    fetcher.bind_fetch(fetch_bytes)

    try:
        st = getattr(fetcher, "settings", None)
        if st is not None:
            st.api_min_interval = interval
            st.api_concurrency = conc
        thr = getattr(fetcher, "_throttle", None)
        if thr is not None:
            thr.min_interval = interval
            thr.concurrency = conc
            thr._sem = asyncio.Semaphore(conc)
    except Exception:
        pass

    app.state.thumb_gate = gate
    app.state.thumb_stats = stats
