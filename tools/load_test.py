"""内存压测：本地/服务器都能跑，给出"每张缩略图吃多少内存"的实测数。

用途
----
部署前先在这台机器上跑一遍，得到真实峰值；服务器上再用同一份脚本卡阈值。
它只做**纯本机**的事（合成图片 → 编码 → 加密 → 入库），不联网、不碰 115。

跑法::

    python tools/load_test.py                    # 默认 50 张、并发 1
    python tools/load_test.py -n 200 -c 2        # 压到并发 2
    python tools/load_test.py -n 200 -c 2 --limit-mb 220   # 超阈值即失败退出

输出
----
* 每张耗时（ms）、峰值 Python 堆（tracemalloc）
* 进程 RSS 峰值（Linux 读 /proc/self/status 的 VmHWM；Windows 用 ctypes 取 PeakWorkingSetSize）
* 库体积与每张加密后字节数
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("THUMB_ENC_KEY", "00" * 32)


# --------------------------------------------------------------------- RSS
def peak_rss_mb() -> float | None:
    """进程峰值 RSS（MB）。Linux 读 VmHWM，Windows 读 PeakWorkingSetSize。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return None
            return pmc.PeakWorkingSetSize / 1048576.0
        except Exception:  # noqa: BLE001
            return None
    return None


# --------------------------------------------------------------------- 造图
def make_jpeg(w: int, h: int, seed: int, quality: int = 88) -> bytes:
    from PIL import Image, ImageFilter

    rnd = random.Random(seed)
    base = Image.new("RGB", (w, h), (rnd.randrange(60, 200), 90, 160))
    noise = Image.effect_noise((w, h), 26).convert("L")
    base = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.12)
    base = base.filter(ImageFilter.GaussianBlur(0.5))
    buf = io.BytesIO()
    base.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class Photo:
    __slots__ = ("id", "name", "thumb", "size", "ctime", "ext", "type", "is_video")

    def __init__(self, pid: str) -> None:
        self.id, self.name, self.thumb = pid, pid + ".jpg", "u"
        self.size, self.ctime, self.ext, self.type, self.is_video = 5 << 20, 0, "jpg", 5, False


# --------------------------------------------------------------------- 主流程
async def run(n: int, conc: int, size: int, src_w: int, src_h: int, trace: bool = True) -> dict:
    import tracemalloc

    from app.settings import Settings
    from app.thumb_fetch import ThumbFetcher
    from app.thumb_store import ThumbStore

    tmp = ROOT / "data" / f"_loadtest_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    store = ThumbStore(tmp / "thumbs.db")
    st = Settings(api_min_interval=0.0, api_concurrency=conc, rest_every=0,
                  db_path=tmp / "thumbs.db", size=size, quality=78, lru_mb=16)
    fetcher = ThumbFetcher(store, st, fetch_bytes=None)

    payloads = [make_jpeg(src_w, src_h, seed=i) for i in range(min(8, n))]
    sem = asyncio.Semaphore(conc)
    durations: list[float] = []
    if trace:
        tracemalloc.start()

    async def one(i: int) -> None:
        async with sem:
            p = Photo(f"load:{i:05d}")
            t0 = time.perf_counter()
            await fetcher.ensure(p, allow_fetch=False)          # 先占位（必然 MISS）
            enc = fetcher.encode(payloads[i % len(payloads)])    # 真实解码+缩放
            if enc:
                import hashlib
                jpeg, w, h = enc
                etag = hashlib.sha256(jpeg).hexdigest()[:32]
                await asyncio.to_thread(store.put, p.id, jpeg, etag=etag, w=w, h=h)
            durations.append((time.perf_counter() - t0) * 1000)

    await asyncio.gather(*(one(i) for i in range(n)))
    if trace:
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    else:
        peak = 0

    stats = store.stats()
    avg_bytes = 0.0
    try:
        import sqlite3
        con = sqlite3.connect(str(tmp / "thumbs.db"))
        row = con.execute("SELECT COUNT(*), COALESCE(SUM(pbytes),0) FROM thumbs").fetchone()
        con.close()
        avg_bytes = (row[1] / row[0]) if row and row[0] else 0.0
    except Exception:  # noqa: BLE001
        pass
    for f in tmp.glob("*"):
        try:
            f.unlink()
        except Exception:  # noqa: BLE001
            pass
    try:
        tmp.rmdir()
    except Exception:  # noqa: BLE001
        pass

    durations.sort()
    return {
        "n": n, "concurrency": conc,
        "avg_ms": round(sum(durations) / len(durations), 1),
        "p95_ms": round(durations[int(len(durations) * 0.95) - 1], 1),
        "max_ms": round(durations[-1], 1),
        "py_peak_mb": round(peak / 1048576, 1),
        "rss_peak_mb": peak_rss_mb(),
        "thumb_bytes_avg": round(avg_bytes, 0),
        "source": f"{src_w}x{src_h}",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="缩略图内存/耗时压测")
    ap.add_argument("-n", "--count", type=int, default=50)
    ap.add_argument("-c", "--concurrency", type=int, default=1)
    ap.add_argument("--size", type=int, default=384, help="缩略图长边")
    ap.add_argument("--src-w", type=int, default=4000, help="源图宽（模拟手机照）")
    ap.add_argument("--src-h", type=int, default=3000)
    ap.add_argument("--no-trace", action="store_true", help="关掉 tracemalloc（它的自身开销会虚高 RSS）")
    ap.add_argument("--limit-mb", type=float, default=0,
                    help="RSS 峰值超过该值即以退出码 1 失败（服务器测试用）")
    args = ap.parse_args(argv)

    print(f"压测：{args.count} 张 / 并发 {args.concurrency} / 源图 {args.src_w}x{args.src_h} / 输出 {args.size}px")
    out = asyncio.run(run(args.count, args.concurrency, args.size, args.src_w, args.src_h,
                          trace=not args.no_trace))
    for k, v in out.items():
        print(f"  {k:16} {v}")

    rss = out.get("rss_peak_mb")
    if args.limit_mb and rss and rss > args.limit_mb:
        print(f"\n✗ RSS 峰值 {rss} MB 超过阈值 {args.limit_mb} MB")
        return 1
    if rss:
        print(f"\n✓ RSS 峰值 {rss} MB"
              + (f"（阈值 {args.limit_mb} MB）" if args.limit_mb else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
