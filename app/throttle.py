"""上游请求节流 —— 语义与 ``115-album/app/throttle.py`` 完全一致。

* **全局最小间隔**：任意两次上游调用之间至少间隔 ``API_MIN_INTERVAL`` 秒；
* **并发闸门**：同时在飞的请求不超过 ``API_CONCURRENCY``；
* **节奏休息**：每 ``PREFETCH_REST_EVERY`` 次调用后整体停 ``PREFETCH_REST_SECONDS`` 秒。

三者都绕不过（顺序调用也受间隔约束），这是 115 风控预算的唯一入口。
"""
from __future__ import annotations

import asyncio
import time


class Throttle:
    def __init__(self, min_interval: float = 0.0, concurrency: int = 2) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.concurrency = max(1, int(concurrency))
        self._sem = asyncio.Semaphore(self.concurrency)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.calls = 0
        self.total_wait = 0.0
        self.rests = 0

    @property
    def enabled(self) -> bool:
        return self.min_interval > 0

    async def acquire(self) -> None:
        await self._sem.acquire()
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                self.total_wait += wait
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass

    def note_call(self) -> None:
        self.calls += 1

    def note_rest(self) -> None:
        self.rests += 1

    def stat(self) -> dict:
        return {
            "min_interval_s": self.min_interval,
            "concurrency": self.concurrency,
            "calls": self.calls,
            "waited_s": round(self.total_wait, 1),
            "rests": self.rests,
        }
