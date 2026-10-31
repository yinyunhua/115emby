"""配置：环境变量集中读取。

**节流参数与 115-album 同名同义**，避免两套服务对同一个 115 账号
各自打流量时给出矛盾预算：

    API_MIN_INTERVAL      两次上游请求最小间隔（秒），默认 1.0
    API_CONCURRENCY       同时在上游的请求数，默认 2
    PREFETCH_REST_EVERY   每完成多少条休息一次，默认 200
    PREFETCH_REST_SECONDS 每次休息秒数，默认 5

缩略图相关：:

    THUMB_DB             加密缩略图库路径，默认 ./data/thumbs.db
    THUMB_SIZE           长边像素，默认 384
    THUMB_QUALITY        JPEG 质量，默认 78
    THUMB_MAX_BYTES      单张上游缩略图体积上限，默认 300 KB（超过直接跳过）
    THUMB_LRU_MB         进程内解密缓存上限，默认 32 MB

夜间预热（定时窗口 + 断点续传）：:

    THUMB_PREWARM_ENABLED     1/0，默认 0（默认只懒抓）
    THUMB_PREWARM_WINDOW      形如 02:00-06:00；跨零点写 23:00-05:00；相同=全天
    THUMB_PREWARM_MAX_PER_NIGHT  每夜最多抓多少张，0=按窗口时长自动推算
    THUMB_PREWARM_RETRY_HOURS    失败的记录多久后允许重试，默认 6
    THUMB_PREWARM_MAX_ATTEMPTS   最多重试次数，默认 3
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()

def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default

def _float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default

def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")

@dataclass(slots=True)
class Settings:
    # ---- 节流（与 115-album 同名）----
    api_min_interval: float = 1.0
    api_concurrency: int = 2
    rest_every: int = 200
    rest_seconds: float = 5.0

    # ---- 缩略图存储 ----
    db_path: Path = BASE_DIR / "data" / "thumbs.db"
    size: int = 384
    quality: int = 78
    max_bytes: int = 300 * 1024
    lru_mb: int = 32
    # 同时解码/缩放的重活上限。**这是 990MB 小机的关键阀门**：
    # 一张 12MP JPEG 解码峰值 ≈ +120MB，并发 2 就可能顶到 240MB
    encode_concurrency: int = 1
    placeholder_repeat: int = 5
    # 占位图/无图/超限时回退取**原图**，本地缩（115 的缩略图接口兜不住的情况）
    origin_max_bytes: int = 30 * 1024 * 1024
    gif_skip: bool = True          # GIF 不进缩略图队列（用户要求）

    # ---- 夜间预热 ----
    prewarm_enabled: bool = False
    window: str = "02:00-06:00"
    max_per_night: int = 0
    retry_hours: float = 6.0
    max_attempts: int = 3
    tick_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_min_interval=_float("API_MIN_INTERVAL", 1.0),
            api_concurrency=_int("API_CONCURRENCY", 2),
            rest_every=_int("PREFETCH_REST_EVERY", 200),
            rest_seconds=_float("PREFETCH_REST_SECONDS", 5.0),
            db_path=Path(_env("THUMB_DB", str(BASE_DIR / "data" / "thumbs.db"))),
            size=_int("THUMB_SIZE", 384),
            quality=_int("THUMB_QUALITY", 78),
            max_bytes=_int("THUMB_MAX_BYTES", 300 * 1024),
            lru_mb=_int("THUMB_LRU_MB", 32),
            encode_concurrency=max(1, _int("THUMB_ENCODE_CONCURRENCY", 1)),
            placeholder_repeat=_int("THUMB_PLACEHOLDER_REPEAT", 5),
            origin_max_bytes=_int("THUMB_ORIGIN_MAX_BYTES", 30 * 1024 * 1024),
            gif_skip=_bool("THUMB_GIF_SKIP", True),
            prewarm_enabled=_bool("THUMB_PREWARM_ENABLED", False),
            window=_env("THUMB_PREWARM_WINDOW", "02:00-06:00"),
            max_per_night=_int("THUMB_PREWARM_MAX_PER_NIGHT", 0),
            retry_hours=_float("THUMB_PREWARM_RETRY_HOURS", 6.0),
            max_attempts=_int("THUMB_PREWARM_MAX_ATTEMPTS", 3),
            tick_seconds=_int("THUMB_PREWARM_TICK", 30),
        )

    # ------------------------------------------------------------------ 派生
    def nightly_budget(self, window_minutes: int) -> int:
        """按节流间隔推算一夜（一个窗口）能抓多少张。"""
        if self.max_per_night > 0:
            return self.max_per_night
        if self.api_min_interval <= 0:
            return max(1, window_minutes * 60 * max(1, self.api_concurrency))
        return max(1, int(window_minutes * 60 / self.api_min_interval))
