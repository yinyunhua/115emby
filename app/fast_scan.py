"""目录扫描提速 v4：令牌桶 + 顺序对齐预读 + **深度感知**（不预读深度上限外的目录）。

环境变量：
    FAST_SCAN_INTERVAL=0.5      # 平均请求间隔
    FAST_SCAN_CONCURRENCY=3     # 同时在飞
    FAST_SCAN_LOOKAHEAD=12      # 每层预读几个
"""
from __future__ import annotations

import asyncio
import os
import time

from . import index_crypto as ic
from . import source115


class RateGate:
    """令牌桶：平均速率 = 1/interval，同时允许 concurrency 并发。"""

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


def install(app) -> None:
    scanner = getattr(app.state, "scanner", None)
    db = getattr(app.state, "db", None)
    if scanner is None or db is None:
        return

    interval = float(os.getenv("FAST_SCAN_INTERVAL") or 0.5)
    conc = max(1, int(os.getenv("FAST_SCAN_CONCURRENCY") or 3))
    lookahead = max(0, int(os.getenv("FAST_SCAN_LOOKAHEAD") or 12))

    gate = RateGate(interval, conc)
    cache: dict[str, dict] = {}
    inflight: dict[str, asyncio.Task] = {}
    stats = {"hits": 0, "fetched": 0, "prefetched": 0, "errors": 0, "skipped_prefetch": 0}

    def _depth_limit() -> int:
        try:
            job = db.active_job()
            d = int(job["depth"]) if job else 0
            return d if d > 0 else 10 ** 6        # 0 = 不限（到叶子）
        except Exception:
            return 99

    def _depth_of(cid: str) -> int:
        try:
            row = db.folder(cid)
            return int(row["depth"]) if row else 0
        except Exception:
            return 0

    async def _fetch(cid: str) -> dict:
        async with gate:
            return await source115.list_dir(cid)

    def _order(listing: dict) -> list:
        folders = listing.get("folders") or []
        try:
            folders = sorted(folders, key=lambda f: ic.normalize(f.get("name", "")))
        except Exception:
            pass
        return [str(f.get("id")) for f in folders if f.get("id")]

    def _schedule(listing: dict, parent_cid: str) -> None:
        if lookahead <= 0:
            return
        limit = _depth_limit()
        depth = _depth_of(parent_cid)
        if depth + 1 > limit:                    # 已在深度上限：预读纯属浪费
            stats["skipped_prefetch"] += 1
            return
        started = 0
        for cid in _order(listing):
            if started >= lookahead:
                break
            if cid in cache or cid in inflight:
                continue

            async def run(_cid=cid):
                try:
                    cache[_cid] = await _fetch(_cid)
                    stats["prefetched"] += 1
                except Exception:
                    stats["errors"] += 1
                finally:
                    inflight.pop(_cid, None)

            inflight[cid] = asyncio.create_task(run())
            started += 1

    failed = set()

    def _record_failure(cid: str, err: str) -> None:
        try:
            import sqlite3
            con = sqlite3.connect(os.getenv("EMBY_DB") or "/app/data/emby.db", timeout=20)
            con.execute("""CREATE TABLE IF NOT EXISTS scan_failed (
                dir_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0,
                err TEXT DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1)""")
            con.execute("""INSERT INTO scan_failed (dir_id, epoch, err, attempts) VALUES (?,?,?,1)
                ON CONFLICT(dir_id) DO UPDATE SET epoch=excluded.epoch, err=excluded.err,
                attempts=scan_failed.attempts+1""", (str(cid), int(time.time()), str(err)[:300]))
            con.commit(); con.close()
        except Exception:
            pass

    async def _call_list(cid: str) -> dict:
        cid = str(cid or "0")
        try:
            return await _call_list_inner(cid)
        except Exception as exc:                      # 单个目录失败不要终止整个任务
            failed.add(cid)
            stats["errors"] += 1
            _record_failure(cid, exc)
            print("列目录失败，跳过该目录 %s：%s" % (cid, exc), flush=True)
            return {"folders": [], "files": [], "path": [], "_failed": True}

    async def _call_list_inner(cid: str) -> dict:
        hit = cache.pop(cid, None)
        if hit is not None:
            stats["hits"] += 1
            _schedule(hit, cid)
            return hit
        task = inflight.pop(cid, None)
        if task is not None:
            try:
                listing = await task
                stats["hits"] += 1
                _schedule(listing, cid)
                return listing
            except Exception:
                pass
        listing = await _fetch(cid)
        stats["fetched"] += 1
        _schedule(listing, cid)
        return listing

    scanner._call_list = _call_list              # type: ignore[assignment]

    try:
        st = getattr(scanner, "settings", None)
        if st is not None:
            st.api_min_interval = interval
            st.api_concurrency = conc
        thr = getattr(scanner, "_throttle", None)
        if thr is not None:
            thr.min_interval = interval
    except Exception:
        pass

    app.state.fast_scan = {"gate": gate, "stats": stats, "failed": failed, "interval": interval,
                           "concurrency": conc, "lookahead": lookahead}
