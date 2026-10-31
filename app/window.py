"""夜间预热时间窗：``02:00-06:00`` 解析、跨零点、边界计算。

约定
----
* ``HH:MM-HH:MM``，左闭右开 ``[start, end)``；
* ``end < start`` 视为**跨零点**（如 ``23:00-05:00`` 表示 23:00 到次日 05:00）；
* ``start == end`` 视为**全天**（1440 分钟），方便"临时全天跑"；
* 解析失败一律退化成"全天"，并通过 ``parse().ok=False`` 让调用方记日志 ——
  宁可多抓一点，也不要因为拼错时间导致夜里一直不跑。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DAY_MINUTES = 24 * 60


def _parse_hhmm(text: str) -> int | None:
    text = (text or "").strip()
    if not text or ":" not in text:
        return None
    hh, _, mm = text.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 24 and 0 <= m < 60):
        return None
    if h == 24 and m != 0:
        return None
    return h * 60 + m


@dataclass(slots=True)
class Window:
    start_min: int = 2 * 60
    end_min: int = 6 * 60
    text: str = "02:00-06:00"
    ok: bool = True

    # ------------------------------------------------------------------ 解析
    @classmethod
    def parse(cls, text: str) -> "Window":
        raw = (text or "").strip()
        left, sep, right = raw.partition("-")
        a, b = _parse_hhmm(left), _parse_hhmm(right) if sep else None
        if a is None or b is None:
            return cls(0, DAY_MINUTES, f"(无效:{raw or '空'})全天", ok=False)
        if a == b:
            return cls(0, DAY_MINUTES, raw, ok=True)
        return cls(a, b, raw, ok=True)

    # ------------------------------------------------------------------ 属性
    @property
    def wraps(self) -> bool:
        return self.end_min < self.start_min

    @property
    def duration_minutes(self) -> int:
        if self.start_min == 0 and self.end_min == DAY_MINUTES:
            return DAY_MINUTES
        if self.wraps:
            return DAY_MINUTES - self.start_min + self.end_min
        return max(0, self.end_min - self.start_min)

    # ------------------------------------------------------------------ 判断
    @staticmethod
    def _minute_of(dt: datetime) -> int:
        return dt.hour * 60 + dt.minute

    def contains(self, dt: datetime) -> bool:
        cur = self._minute_of(dt)
        if self.start_min == 0 and self.end_min == DAY_MINUTES:
            return True
        if self.wraps:
            return cur >= self.start_min or cur < self.end_min
        return self.start_min <= cur < self.end_min

    # ------------------------------------------------------------------ 边界
    def next_open(self, dt: datetime) -> datetime:
        """最近的（含当前时刻）窗口开始时间。"""
        if self.contains(dt):
            return dt
        today_start = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            minutes=self.start_min
        )
        return today_start if today_start > dt else today_start + timedelta(days=1)

    def next_close(self, dt: datetime) -> datetime | None:
        """当前窗口的结束时间；不在窗口内返回 None。"""
        if not self.contains(dt):
            return None
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        close_today = midnight + timedelta(minutes=self.end_min)
        if self.start_min == 0 and self.end_min == DAY_MINUTES:
            return midnight + timedelta(days=1)
        if self.wraps and self._minute_of(dt) >= self.start_min:
            return close_today + timedelta(days=1)          # 跨零点：明早收工
        return close_today

    def night_key(self, dt: datetime) -> str:
        """把"这一夜"归属到开始那天（跨零点时 01:00 归属前一天）。"""
        if self.wraps and self._minute_of(dt) < self.end_min:
            return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")

    def describe(self) -> str:
        state = "跨零点" if self.wraps else ("全天" if self.duration_minutes >= DAY_MINUTES else "当日")
        return f"{self.text}（{self.duration_minutes} 分钟/{state}）"
