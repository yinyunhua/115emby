"""缩略图夜间预热 CLI：时间窗定时启停 + 断点续传。

两种模式
--------
**demo（默认，不碰 115）**：用合成图片压测，验证节流/窗口/续传逻辑::

    python tools/thumb_prewarm.py seed --count 200 --with-video
    python tools/thumb_prewarm.py now --limit 50
    python tools/thumb_prewarm.py window --window 02:00-06:00
    python tools/thumb_prewarm.py daemon

**host（接真实 115 数据）**：把 ``app/`` 里 5 个模块复制进 115-album 的 ``app/``，
然后在本项目目录执行::

    python tools/thumb_prewarm.py --host ../115相册/115-album daemon

此模式下本进程只 import **115-album 的 app 包**（同名包只加载一个，避免冲突）。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _use_package(path: Path) -> None:
    """把指定目录放到 sys.path 最前（决定 import 到哪个 app 包）。"""
    p = str(Path(path).resolve())
    sys.path = [p] + [x for x in sys.path if Path(x or ".").resolve() != Path(p)]


_use_package(ROOT)          # demo 默认用本项目


# --------------------------------------------------------------------- demo 源
def _demo_jpeg(pid: str, w: int = 1600, h: int = 1200) -> bytes:
    from PIL import Image, ImageFilter

    seed = abs(hash(pid)) % 65536
    base = Image.new("RGB", (w, h))
    px = base.load()
    for y in range(0, h, 16):
        for x in range(0, w, 16):
            c = ((seed + x * 7) % 200 + 40, (seed // 3 + y * 5) % 180 + 50,
                 (seed // 7 + (x + y) * 3) % 160 + 60)
            for dy in range(16):
                for dx in range(16):
                    if x + dx < w and y + dy < h:
                        px[x + dx, y + dy] = c
    noise = Image.effect_noise((w, h), 26).convert("L")
    base = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.12)
    base = base.filter(ImageFilter.GaussianBlur(0.5))
    buf = io.BytesIO()
    base.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


class DemoPhoto:
    __slots__ = ("id", "name", "thumb", "size", "ctime", "ext", "type", "is_video")

    def __init__(self, pid: str, video: bool = False) -> None:
        self.id = pid
        self.name = pid + (".mp4" if video else ".jpg")
        self.thumb = "" if video else f"https://demo.invalid/{pid}"
        self.size = 3 * 1024 * 1024
        self.ctime = 0
        self.ext = "mp4" if video else "jpg"
        self.type = 2 if video else 5
        self.is_video = video


def _demo_hooks(latency: float):
    async def get_photo(pid: str):
        if pid.startswith("vid:"):
            p = DemoPhoto(pid, video=True)
            return p
        return DemoPhoto(pid)

    async def fetch_bytes(photo):
        await asyncio.sleep(latency)
        return _demo_jpeg(photo.id)

    return get_photo, fetch_bytes


# --------------------------------------------------------------------- host 源
def _host_hooks():
    """从 115-album 的 app 包里取需要的三件套（模块需先复制过去）。"""
    import sqlite3

    from app import config, thumbs                     # 115-album
    from app.sources import get_source

    source = get_source()
    db = Path(config.INDEX_DB)

    async def get_photo(pid: str):
        from app import index_store
        getter = getattr(index_store, "get", None)
        row = getter(pid) if callable(getter) else None
        if row is None and db.exists():
            conn = sqlite3.connect(str(db), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                r = conn.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
                row = dict(r) if r else None
            finally:
                conn.close()
        if not row:
            return None
        name = str(row.get("name") or "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return type("P", (), {
            "id": str(row.get("id") or pid), "name": name,
            "thumb": str(row.get("thumb") or ""), "size": int(row.get("size") or 0),
            "ctime": int(row.get("ctime") or 0), "ext": ext,
            "type": int(row.get("type") or 0),
            "is_video": ext in {"mp4", "mkv", "avi", "mov", "rmvb", "webm", "flv", "ts", "m2ts"},
        })()

    async def fetch_bytes(photo):
        return await thumbs.fetch_upstream_thumb(source, photo)

    return get_photo, fetch_bytes


# --------------------------------------------------------------------- 装配
def build(args):
    from app.settings import Settings
    from app.thumb_store import ThumbStore
    from app.thumb_fetch import ThumbFetcher

    settings = Settings.from_env()
    if args.db:
        settings.db_path = Path(args.db)
    if args.window:
        settings.window = args.window
    if args.interval is not None:
        settings.api_min_interval = args.interval
    if args.concurrency is not None:
        settings.api_concurrency = args.concurrency

    store = ThumbStore(settings.db_path)
    get_photo, fetch_bytes = _demo_hooks(args.latency) if args.demo else _host_hooks()
    fetcher = ThumbFetcher(store, settings, fetch_bytes=fetch_bytes)
    return store, fetcher, get_photo


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def run(args) -> int:
    store, fetcher, get_photo = build(args)
    win = fetcher.settings.window
    from app.window import Window

    if args.cmd == "status":
        _print(fetcher.stats())
        return 0

    if args.cmd == "seed":
        if not args.demo:
            print("seed 仅 demo 模式可用", file=sys.stderr)
            return 2
        items = [(f"img:{i:06d}", f"https://demo.invalid/{i}", 0) for i in range(args.count)]
        if args.with_video:
            items.append(("vid:000001", "", 0))
        print(f"入队 {store.enqueue(items)} 条 → {store.count_state()}")
        return 0

    if args.cmd == "now":
        _print(await fetcher.run_window(get_photo, respect_window=False, max_items=args.limit or 100))
        _print(fetcher.stats())
        return 0

    if args.cmd == "window":
        _print(await fetcher.run_window(
            get_photo, respect_window=True,
            max_items=None if args.until_empty else (args.limit or None),
        ))
        return 0

    if args.cmd == "daemon":
        w = Window.parse(win)
        print(f"守护启动：窗口 {w.describe()}｜节流 {fetcher.settings.api_min_interval}s / "
              f"{fetcher.settings.api_concurrency} 并发｜每夜上限 "
              f"{fetcher.settings.nightly_budget(w.duration_minutes)} 张｜Ctrl-C 停止（自动断点）")
        try:
            await fetcher.run_daemon(get_photo, stop=asyncio.Event())
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("已停止，进度已保存在 thumbs.db")
        return 0

    if args.cmd in ("pause", "resume"):
        store.kv_set("prewarm_paused", "1" if args.cmd == "pause" else "0")
        print("已暂停" if args.cmd == "pause" else "已恢复")
        return 0

    if args.cmd == "reset":
        print(f"重置 {store.reset_state(tuple(args.state.split(',')))} 条 → pending")
        return 0

    print(f"未知命令 {args.cmd}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="缩略图夜间预热（时间窗 + 断点续传）")
    ap.add_argument("cmd", choices=["status", "seed", "now", "window", "daemon", "pause", "resume", "reset"])
    ap.add_argument("--db")
    ap.add_argument("--window", help="02:00-06:00 / 23:00-05:00 / 相同=全天")
    ap.add_argument("--interval", type=float, help="覆盖 API_MIN_INTERVAL")
    ap.add_argument("--concurrency", type=int, help="覆盖 API_CONCURRENCY")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--until-empty", action="store_true")
    ap.add_argument("--state", default="failed")
    ap.add_argument("--demo", action="store_true", default=None,
                    help="合成图片模式（给了 --host 则自动关闭）")
    ap.add_argument("--latency", type=float, default=0.05)
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--with-video", action="store_true")
    ap.add_argument("--host", help="115-album 目录，接真实数据")
    args = ap.parse_args(argv)

    # 没有 --host 就是 demo 模式（合成图片，不碰 115）；给了 --host 就一定走真实数据源。
    args.demo = args.host is None
    if args.host:
        host = Path(args.host).resolve()
        if not (host / "app" / "__init__.py").is_file():
            print(f"--host 目录里没有 app 包：{host}", file=sys.stderr)
            return 2
        _use_package(host)          # 决定 import 到 115-album 的 app 包
        os.environ.setdefault("ALBUM_HOST_DIR", str(host))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
