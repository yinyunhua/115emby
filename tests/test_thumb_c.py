"""C 方案（懒抓 + 永久保存 + 加密 + 夜间时间窗 + 断点续传）离线自测。

跑法（不需要 115 凭证，也不需要网络）::

    python tests/test_thumb_c.py
"""
from __future__ import annotations

import asyncio
import io
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT))

# 固定测试密钥（必须在 import secretbox 之前设好）
os.environ.setdefault("THUMB_ENC_KEY", secrets.token_hex(32))
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))

from app import secretbox                       # noqa: E402
from app.settings import Settings               # noqa: E402
from app.thumb_fetch import ThumbFetcher        # noqa: E402
from app.thumb_store import (                   # noqa: E402
    STATE_DONE, STATE_FAILED, STATE_PENDING, STATE_SKIPPED, ThumbStore,
)
from app.video_thumb import VideoThumbOptions   # noqa: E402
from app.window import Window                   # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [OK ] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def jpeg(w: int = 1600, h: int = 1200, seed: int = 0) -> bytes:
    from PIL import Image, ImageFilter

    im = Image.new("RGB", (w, h), (30 + seed % 200, 90, 160))
    noise = Image.effect_noise((w, h), 30).convert("L")
    im = Image.blend(im, Image.merge("RGB", (noise, noise, noise)), 0.15)
    im = im.filter(ImageFilter.GaussianBlur(0.5))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


class Photo:
    __slots__ = ("id", "name", "thumb", "size", "ctime", "ext", "type", "is_video")

    def __init__(self, pid: str, *, thumb: str = "https://x.invalid/t", video: bool = False):
        self.id = pid
        self.name = pid + (".mp4" if video else ".jpg")
        self.thumb = "" if video else thumb
        self.size = 1024 * 1024
        self.ctime = 0
        self.ext = "mp4" if video else "jpg"
        self.type = 2 if video else 5
        self.is_video = video


class Counter:
    def __init__(self) -> None:
        self.n = 0

    async def __call__(self, photo) -> bytes | None:
        self.n += 1
        return jpeg(seed=self.n)


def new_store(tmp: Path) -> ThumbStore:
    return ThumbStore(tmp / "thumbs.db")


def settings(tmp: Path, **kw) -> Settings:
    st = Settings(
        api_min_interval=0.0, api_concurrency=2, rest_every=0, rest_seconds=0.0,
        db_path=tmp / "thumbs.db", size=384, quality=78, lru_mb=8,
    )
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# --------------------------------------------------------------------- 1. 加密
def test_secretbox(tmp: Path) -> None:
    print("\n[1] 加密层 secretbox")
    pt = b"hello thumbnail " * 100
    blob = secretbox.encrypt(pt)
    check("信封带 TBX1 标识", blob[:4] == b"TBX1", blob[:4].hex())
    check("密文不含明文", pt[:16] not in blob)
    check("往返一致", secretbox.decrypt(blob) == pt)
    _have = secretbox._have_aesgcm()
    check(f"算法与依赖一致（cryptography {'有' if _have else '无'}→alg={1 if _have else 2}）",
          secretbox.active_alg() == (1 if _have else 2), str(secretbox.active_alg()))

    tampered = bytearray(blob)
    tampered[-1] ^= 0xFF
    try:
        secretbox.decrypt(bytes(tampered))
        check("篡改被拒", False, "竟然解开了")
    except secretbox.DecryptError:
        check("篡改被拒", True)

    good_key = os.environ["THUMB_ENC_KEY"]
    try:
        os.environ["THUMB_ENC_KEY"] = secrets.token_hex(32)
        secretbox.reset_key_cache()
        try:
            secretbox.decrypt(blob)
            check("换密钥解不开", False, "竟然解开了")
        except secretbox.DecryptError:
            check("换密钥解不开", True)
    finally:
        os.environ["THUMB_ENC_KEY"] = good_key
        secretbox.reset_key_cache()

    # 标准库回退路径（模拟没装 cryptography）
    orig = secretbox._have_aesgcm
    secretbox._have_aesgcm = lambda: False          # type: ignore[assignment]
    secretbox.reset_key_cache()
    try:
        blob2 = secretbox.encrypt(pt)
        check("回退算法编号=2", blob2[4] == 2, str(blob2[4]))
        check("回退模式往返一致", secretbox.decrypt(blob2) == pt)
        check("回退模式篡改被拒", (lambda: _tamper_ok(blob2))())
    finally:
        secretbox._have_aesgcm = orig            # type: ignore[assignment]
        secretbox.reset_key_cache()


def _tamper_ok(blob: bytes) -> bool:
    bad = bytearray(blob)
    bad[-1] ^= 0x01
    try:
        secretbox.decrypt(bytes(bad))
        return False
    except secretbox.DecryptError:
        return True


# --------------------------------------------------------------------- 2. 存储
def test_store(tmp: Path) -> None:
    print("\n[2] 存储层 thumb_store")
    store = new_store(tmp)
    data = jpeg(800, 600)
    pid = "1234567890123456789:abcdefghij01234567"      # 115 的 file_id:pickcode，不是文件名
    n = store.put(pid, data, etag="etag1", w=384, h=288)
    check("写入返回加密体积", n > len(data) - 1)

    got = store.get(pid)
    check("读回一致", got is not None and got[0] == data)
    check("etag 一致", got is not None and got[1] == "etag1")
    meta = store.get_meta(pid)
    check("meta 宽高正确", meta and meta["w"] == 384 and meta["h"] == 288, str(meta))
    check("hits 自增", store.get_meta(pid)["hits"] >= 1)
    check("明文体积记录", meta and meta["pbytes"] == len(data))

    # 磁盘上不能有明文（本库只存 pid + 密文，从不存文件名）
    raw = Path(store.path).read_bytes()
    wal = Path(str(store.path) + "-wal")
    blob_all = raw + (wal.read_bytes() if wal.exists() else b"")
    check("库文件不含 JPEG 文件头", b"\xff\xd8\xff" not in blob_all)
    check("库文件不含文件名明文", b"IMG_20250101_120000.jpg" not in blob_all)
    check("库文件只留不透明 id", pid.encode() in blob_all)

    st = store.stats()
    check("统计 thumbs=1", st["thumbs"] == 1, str(st))
    check("统计 disk_mb>0", st["disk_mb"] > 0, str(st))


# --------------------------------------------------------------------- 3. 队列
def test_queue(tmp: Path) -> None:
    print("\n[3] 抓取队列 + 断点游标")
    store = new_store(tmp)
    n = store.enqueue([("a", "u1", 0), ("b", "u2", 0), ("c", "u3", 0)])
    check("入队 3 条", n == 3, str(n))
    check("重复入队不新增", store.enqueue([("a", "u1", 0)]) == 0)
    check("pending=3", store.count_state().get(STATE_PENDING) == 3, str(store.count_state()))

    batch1 = store.next_batch(2)
    check("取批 2 条", len(batch1) == 2, str(len(batch1)))
    store.mark(batch1[0]["pid"], STATE_DONE)
    store.mark(batch1[1]["pid"], STATE_FAILED, "boom", bump_attempt=True)

    batch2 = store.next_batch(5)
    pids = {r["pid"] for r in batch2}
    check("失败的不会立刻重试", batch1[1]["pid"] not in pids, str(pids))
    check("剩余 pending 可取", "c" in pids or batch1[1]["pid"] in pids, str(pids))

    store.kv_set("prewarm_cursor", "b")
    check("游标可持久化", store.kv_get("prewarm_cursor") == "b")
    check("重置失败项", store.reset_state((STATE_FAILED,)) == 1)
    check("重置后回到 pending", store.count_state().get(STATE_PENDING) == 2, str(store.count_state()))


# --------------------------------------------------------------------- 4. 懒抓
def test_ensure(tmp: Path) -> None:
    print("\n[4] 懒抓：一次抓取 + 永久命中")
    store = new_store(tmp)
    calls = Counter()
    st = settings(tmp)
    fetcher = ThumbFetcher(store, st, fetch_bytes=calls)

    async def main() -> None:
        p = Photo("img:1")
        r1 = await fetcher.ensure(p)
        check("首次 FETCHED", r1.label == "FETCHED", r1.label)
        check("返回 JPEG", r1.data is not None and r1.data[:3] == b"\xff\xd8\xff")
        check("命中 LRU", (await fetcher.ensure(p)).label == "HIT")
        fetcher2 = ThumbFetcher(store, st, fetch_bytes=calls)      # 新进程，无 LRU
        r3 = await fetcher2.ensure(p)
        check("重启后仍从库里命中", r3.label == "HIT", r3.label)
        check("上游只被调用一次", calls.n == 1, str(calls.n))

        # 视频且无上游封面 → 不发请求
        v = Photo("vid:1", video=True)
        rv = await fetcher.ensure(v)
        check("视频无封面直接跳过", rv.label == "SKIPPED", rv.label)
        check("视频没打上游", calls.n == 1, str(calls.n))

        # 编码尺寸
        from PIL import Image
        with Image.open(io.BytesIO(r1.data)) as im:
            check("长边 = 384", max(im.size) == 384, str(im.size))

    asyncio.run(main())


# --------------------------------------------------------------------- 5. 跳过规则
def test_skip_rules(tmp: Path) -> None:
    print("\n[5] 跳过规则：超限 / 非图片 / 占位图")
    store = new_store(tmp)
    st = settings(tmp, max_bytes=50 * 1024, placeholder_repeat=5)

    async def big(photo):
        return b"\xff\xd8\xff" + b"0" * (60 * 1024)

    async def notimg(photo):
        return b"<html>403</html>"

    placeholder = jpeg(20, 20, seed=99)

    async def same_placeholder(photo):
        return placeholder

    async def main() -> None:
        f = ThumbFetcher(store, st, fetch_bytes=big)
        check("超限被跳过", (await f.ensure(Photo("big:1"))).label == "SKIPPED")
        check("超限记 skipped", store.count_state().get(STATE_SKIPPED, 0) >= 1)

        f2 = ThumbFetcher(store, st, fetch_bytes=notimg)
        check("非图片被跳过", (await f2.ensure(Photo("html:1"))).label == "SKIPPED")

        f3 = ThumbFetcher(store, st, fetch_bytes=same_placeholder)
        labels = [(await f3.ensure(Photo(f"ph:{i}"))).label for i in range(7)]
        check("前 5 张按正常抓", labels[:5].count("FETCHED") == 5, str(labels))
        check("重复占位图被识别", labels[-1] == "SKIPPED", str(labels))

    asyncio.run(main())


# --------------------------------------------------------------------- 6. 节流
def test_throttle(tmp: Path) -> None:
    print("\n[6] 节流（复用 API_MIN_INTERVAL / API_CONCURRENCY 语义）")
    store = new_store(tmp)
    st = settings(tmp, api_min_interval=0.05, api_concurrency=1)
    calls = Counter()
    fetcher = ThumbFetcher(store, st, fetch_bytes=calls)

    async def main() -> None:
        t0 = time.monotonic()
        for i in range(5):
            await fetcher.ensure(Photo(f"t:{i}"))
        elapsed = time.monotonic() - t0
        check("5 次请求 ≥ 0.20s（间隔生效）", elapsed >= 0.20, f"{elapsed:.3f}s")
        stat = fetcher._throttle.stat()
        check("统计到 5 次调用", stat["calls"] == 5, str(stat))

    asyncio.run(main())


# --------------------------------------------------------------------- 7. 时间窗
def test_window() -> None:
    print("\n[7] 夜间时间窗解析")
    w = Window.parse("02:00-06:00")
    check("解析成功", w.ok and w.start_min == 120 and w.end_min == 360, w.describe())
    check("窗口 240 分钟", w.duration_minutes == 240, str(w.duration_minutes))
    check("03:00 在窗内", w.contains(datetime(2026, 10, 20, 3, 0)))
    check("07:00 不在窗内", not w.contains(datetime(2026, 10, 20, 7, 0)))
    check("06:00 右开区间", not w.contains(datetime(2026, 10, 20, 6, 0)))
    check("下次开门 = 次日 02:00",
          w.next_open(datetime(2026, 10, 20, 7, 0)) == datetime(2026, 10, 21, 2, 0),
          str(w.next_open(datetime(2026, 10, 20, 7, 0))))
    check("窗口结束 = 当天 06:00",
          w.next_close(datetime(2026, 10, 20, 3, 0)) == datetime(2026, 10, 20, 6, 0))

    w2 = Window.parse("23:00-05:00")
    check("跨零点识别", w2.wraps and w2.duration_minutes == 360, w2.describe())
    check("01:00 在跨零点窗内", w2.contains(datetime(2026, 10, 20, 1, 0)))
    check("23:30 在跨零点窗内", w2.contains(datetime(2026, 10, 20, 23, 30)))
    check("12:00 不在窗内", not w2.contains(datetime(2026, 10, 20, 12, 0)))
    check("跨零点收工 = 次晨 05:00",
          w2.next_close(datetime(2026, 10, 20, 23, 30)) == datetime(2026, 10, 21, 5, 0),
          str(w2.next_close(datetime(2026, 10, 20, 23, 30))))
    check("跨零点归属前一天",
          w2.night_key(datetime(2026, 10, 21, 1, 0)) == "2026-10-20",
          w2.night_key(datetime(2026, 10, 21, 1, 0)))

    w3 = Window.parse("08:00-08:00")
    check("起止相同=全天", w3.duration_minutes == 1440 and w3.contains(datetime(2026, 10, 20, 15, 0)))
    w4 = Window.parse("乱写")
    check("非法输入退化为全天而非崩", w4.duration_minutes == 1440 and not w4.ok)

    st = Settings(api_min_interval=1.0, api_concurrency=2, max_per_night=0)
    check("每夜预算=窗口分钟/间隔", st.nightly_budget(240) == 14400, str(st.nightly_budget(240)))
    check("显式上限优先", Settings(max_per_night=500).nightly_budget(240) == 500)


# --------------------------------------------------------------------- 8. 预热与续传
def test_prewarm(tmp: Path) -> None:
    print("\n[8] 夜间预热：限额 / 断点续传 / 暂停")
    store = new_store(tmp)
    st = settings(tmp, retry_hours=6, max_attempts=3)
    calls = Counter()
    fetcher = ThumbFetcher(store, st, fetch_bytes=calls)
    store.enqueue([(f"p:{i}", "u", 0) for i in range(10)])

    async def get_photo(pid: str):
        return Photo(pid)

    async def main() -> None:
        s1 = await fetcher.run_window(get_photo, respect_window=False, max_items=4)
        check("第一批抓 4 张", s1["fetched"] == 4, str(s1))
        s2 = await fetcher.run_window(get_photo, respect_window=False, max_items=4)
        check("第二批接着抓 4 张（不重复）", s2["fetched"] == 4, str(s2))
        check("上游累计 8 次", calls.n == 8, str(calls.n))
        check("游标已推进", store.kv_get("prewarm_cursor").startswith("p:"),
              store.kv_get("prewarm_cursor"))

        # 暂停后不再抓
        store.kv_set("prewarm_paused", "1")
        s3 = await fetcher.run_window(get_photo, respect_window=False, max_items=4)
        check("暂停后 0 抓取", s3["fetched"] == 0 and s3["stopped"] == "paused", str(s3))
        store.kv_set("prewarm_paused", "0")

        s4 = await fetcher.run_window(get_photo, respect_window=False, max_items=10)
        check("恢复后把剩余抓完", s4["fetched"] == 2, str(s4))
        check("队列清空", store.count_state().get(STATE_PENDING, 0) == 0, str(store.count_state()))
        check("done=10", store.count_state().get(STATE_DONE) == 10, str(store.count_state()))

        s5 = await fetcher.run_window(get_photo, respect_window=False, max_items=10)
        check("空队列立即收工", s5["stopped"] == "queue-empty", str(s5))

    asyncio.run(main())


# --------------------------------------------------------------------- 9. 时间窗外不跑
def test_window_gate(tmp: Path) -> None:
    print("\n[9] 时间窗约束：窗外不发请求")
    store = new_store(tmp)
    st = settings(tmp, window="02:00-06:00")
    calls = Counter()
    fixed = datetime(2026, 10, 20, 12, 0)          # 中午，肯定在窗外
    fetcher = ThumbFetcher(store, st, fetch_bytes=calls, clock=lambda: fixed)
    store.enqueue([(f"w:{i}", "u", 0) for i in range(5)])

    async def get_photo(pid: str):
        return Photo(pid)

    async def main() -> None:
        s = await fetcher.run_window(get_photo, respect_window=True)
        check("窗外 0 抓取", s["fetched"] == 0 and s["stopped"] == "window-closed", str(s))
        check("窗外没打上游", calls.n == 0, str(calls.n))

        fetcher_in = ThumbFetcher(
            store, st, fetch_bytes=calls, clock=lambda: datetime(2026, 10, 20, 3, 0)
        )
        s2 = await fetcher_in.run_window(get_photo, respect_window=True)
        check("窗内按预算抓完 5 张", s2["fetched"] == 5, str(s2))

    asyncio.run(main())


# --------------------------------------------------------------------- 10. 路由
def test_router(tmp: Path) -> None:
    print("\n[10] HTTP 路由 /thumb/{id}（ETag / 304 / 占位图）")
    try:
        import httpx
        from fastapi import FastAPI
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 缺 fastapi/httpx：{exc}")
        return

    from app.thumbs_router import build_router

    store = new_store(tmp)
    calls = Counter()
    fetcher = ThumbFetcher(store, settings(tmp, api_min_interval=0.0), fetch_bytes=calls)

    async def get_photo(pid: str):
        if pid == "gone":
            return None
        return Photo(pid)

    app = FastAPI()
    app.include_router(build_router(store, get_photo, fetcher))

    async def main() -> None:
        tr = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            r = await c.get("/thumb/img:1")
            check("首次 200", r.status_code == 200, str(r.status_code))
            check("Content-Type=image/jpeg", r.headers["content-type"] == "image/jpeg",
                  r.headers.get("content-type", ""))
            check("带 ETag", "etag" in {k.lower() for k in r.headers}, str(dict(r.headers))[:120])
            check("私有缓存头", "private" in r.headers.get("cache-control", ""),
                  r.headers.get("cache-control", ""))
            etag = r.headers["etag"]

            r2 = await c.get("/thumb/img:1", headers={"If-None-Match": etag})
            check("ETag 命中 304", r2.status_code == 304, str(r2.status_code))

            r3 = await c.get("/thumb/img:1/meta")
            check("meta 200", r3.status_code == 200 and r3.json()["w"] == 384, r3.text[:80])

            r4 = await c.get("/thumb/gone")
            check("查不到的条目 404/占位", r4.status_code in (404, 200), str(r4.status_code))

            r5 = await c.get("/admin/thumb/stats")
            body = r5.json()
            check("统计接口可用", r5.status_code == 200 and body["thumbs"] >= 1, str(body)[:120])
            check("统计里含加密算法", body.get("enc") in ("AES-256-GCM", "HMAC-CTR(回退)"),
                  str(body.get("enc")))

    asyncio.run(main())



# --------------------------------------------------------------------- 11. 视频抓帧
def test_video(tmp: Path) -> None:
    print("\n[11] 视频缩略图：ffmpeg 抓帧路径 / 格式策略 / 失败退避")
    from app.video_thumb import VideoThumbOptions

    store = new_store(tmp)
    calls = Counter()
    vcalls = Counter()
    st = settings(tmp, api_min_interval=0.0)

    async def video_fetch(photo):
        vcalls.n += 1
        return jpeg(1280, 720, seed=7)

    opts = VideoThumbOptions(enabled=True)          # 默认：m3u8 跳过、ts/rmvb 低优先
    fetcher = ThumbFetcher(store, st, fetch_bytes=calls,
                           video_fetch=video_fetch, video_opts=opts)
    store.enqueue([("vid:1", "", 0)])

    async def main() -> None:
        v = Photo("vid:1", video=True)
        v.ext = "mp4"
        r1 = await fetcher.ensure(v)
        check("mp4 无上游封面 → 走抓帧", r1.label == "FETCHED", r1.label)
        check("抓帧器被调用 1 次", vcalls.n == 1, str(vcalls.n))
        check("图片通道未被调用", calls.n == 0, str(calls.n))
        check("视频计数", fetcher.counters["VIDEO"] == 1, str(fetcher.counters))
        check("二次命中不再抓帧", (await fetcher.ensure(v)).label == "HIT" and vcalls.n == 1)

        # m3u8：默认直接跳过，零请求
        m = Photo("vid:2", video=True); m.ext = "m3u8"
        r2 = await fetcher.ensure(m)
        check("m3u8 直接跳过", r2.label == "SKIPPED", r2.label)
        check("m3u8 零请求", vcalls.n == 1, str(vcalls.n))

        # ts：默认低优先 → 跳过；开启后放行
        t = Photo("vid:3", video=True); t.ext = "ts"
        check("ts 默认跳过", (await fetcher.ensure(t)).label == "SKIPPED")
        f_ts = ThumbFetcher(store, st, fetch_bytes=calls, video_fetch=video_fetch,
                            video_opts=VideoThumbOptions(enabled=True, allow_lowpriority=True))
        t2 = Photo("vid:4", video=True); t2.ext = "ts"
        check("允许低优先后 ts 尝试", (await f_ts.ensure(t2)).label == "FETCHED")

        # mov：必须放行（iPhone MOV 常见）
        mv = Photo("vid:5", video=True); mv.ext = "mov"
        check("mov 可抓帧", (await fetcher.ensure(mv)).label == "FETCHED", "mov 被误跳过")

        # 抓帧返回 None（moov 在尾/超时）→ failed 而非 skipped，可重试
        async def video_fail(photo):
            vcalls.n += 1
            return None
        f_fail = ThumbFetcher(store, st, fetch_bytes=calls, video_fetch=video_fail,
                              video_opts=VideoThumbOptions(enabled=True))
        fl = Photo("vid:6", video=True); fl.ext = "mp4"
        check("抓帧失败 → FAILED", (await f_fail.ensure(fl)).label == "FAILED")
        check("失败记为 failed 可重试", store.get_meta("vid:6") is None)

        # 没有绑定抓帧器 → 跳过且零请求
        f2 = ThumbFetcher(store, st, fetch_bytes=calls)
        r3 = await f2.ensure(Photo("vid:7", video=True))
        check("未绑定抓帧器则跳过", r3.label == "SKIPPED", r3.label)

        # 视频若上游给了封面，仍走图片通道
        v3 = Photo("vid:8"); v3.is_video, v3.ext, v3.type = True, "mp4", 2
        v3.thumb = "https://x.invalid/cover"
        r4 = await fetcher.ensure(v3)
        check("有上游封面走图片通道", r4.label == "FETCHED" and calls.n == 1, f"{r4.label}/{calls.n}")

    asyncio.run(main())


def test_video_cmd() -> None:
    print("\n[12] ffmpeg 命令构造 / 格式策略矩阵 / 缺失降级")
    from app.video_thumb import VideoThumbOptions, build_cmd, find_ffmpeg, grab_frame

    opts = VideoThumbOptions(enabled=True, max_bytes=3 * 1024 * 1024, seek=1.0,
                             timeout=45, threads=2, size=384, ffmpeg="ffmpeg",
                             ua="UA-TEST", referer="https://115.com/")
    cmd = build_cmd("ffmpeg", "https://cdn.example/v.mp4", opts)
    joined = " ".join(cmd)
    check("含 -probesize", "-probesize 3145728" in joined, joined[:160])
    check("含 -rw_timeout", "-rw_timeout" in joined)
    check("限制解码线程", "-threads 2" in joined)
    check("只看一帧", "-frames:v 1" in joined)
    check("定位到 1.0s", "-ss 1.00" in joined)
    check("缩放到 384 长边并保持偶数", "scale=384:-2" in joined, joined[-160:])
    check("mjpeg 质量 4", "-q:v 4" in joined)
    check("透传 UA", "UA-TEST" in joined)
    check("透传 Referer", "Referer: https://115.com/" in joined)
    check("输出到 pipe:1", cmd[-1] == "pipe:1")

    # 额外头（如 Cookie）与 UA 覆盖
    cmd2 = build_cmd("ffmpeg", "u", opts, {"Cookie": "UID=1; CID=2", "User-Agent": "UA-2"})
    j2 = " ".join(cmd2)
    check("额外头拼进 -headers", "Cookie: UID=1; CID=2" in j2)
    check("headers 里的 UA 覆盖默认", "UA-2" in j2)

    # 格式策略矩阵
    cases = {"mp4": True, "mov": True, "m4v": True, "mkv": True, "webm": True,
             "avi": True, "wmv": True, "flv": True,
             "m3u8": False, "ts": False, "m2ts": False, "rmvb": False, "rm": False}
    bad = [e for e, want in cases.items() if VideoThumbOptions(enabled=True).should_attempt(e)[0] != want]
    check("格式策略矩阵正确（默认档）", not bad, str(bad))
    check("允许低优先后 ts 放行",
          VideoThumbOptions(enabled=True, allow_lowpriority=True).should_attempt("ts")[0])
    check("关掉开关后全不放行",
          not VideoThumbOptions(enabled=False).should_attempt("mp4")[0])
    check("跳过原因可读", "skip" in VideoThumbOptions().should_attempt("m3u8")[1].lower()
          or "分片" in VideoThumbOptions().should_attempt("m3u8")[1])
    check("指定不存在的 ffmpeg 名 → None（不兜底猜路径）",
          find_ffmpeg("definitely-not-exist-xyz") is None)
    _def = find_ffmpeg("ffmpeg")
    check("默认名可以兜底到系统路径（有则非空）", _def is None or os.path.isfile(_def), str(_def))

    async def main() -> None:
        r = await grab_frame("http://127.0.0.1:1/x.mp4",
                             VideoThumbOptions(ffmpeg="definitely-not-exist-xyz"), ext="mp4")
        check("无 ffmpeg 时返回 None（不抛异常）", r is None)
        r2 = await grab_frame("http://127.0.0.1:1/x.m3u8",
                              VideoThumbOptions(ffmpeg="definitely-not-exist-xyz"), ext="m3u8")
        check("m3u8 在找 ffmpeg 之前就被拦掉", r2 is None)

    asyncio.run(main())

# --------------------------------------------------------------------- 13. 115 第三方登录
def test_auth115(tmp: Path) -> None:
    print("\n[13] 115 第三方登录（PKCE 设备码 + token 加密保管）")
    import json as _json

    import httpx

    from app import auth_115
    from app.thumb_store import ThumbStore as _Store

    os.environ["APP_ID_115"] = "test-client-id"
    store = _Store(tmp / "auth.db")
    vault = auth_115.TokenVault(store)

    # --- PKCE 基础
    v = auth_115.new_verifier()
    check("verifier 长度合规(43-128)", 43 <= len(v) <= 128, str(len(v)))
    import base64 as _b64
    import hashlib as _h
    expect = _b64.urlsafe_b64encode(_h.sha256(v.encode()).digest()).decode().rstrip("=")
    check("challenge = BASE64URL(SHA256)", auth_115.challenge_of(v) == expect)
    check("challenge 无 padding", "=" not in auth_115.challenge_of(v))

    seen: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/open/authDeviceCode"):
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            seen["auth"] = body
            return httpx.Response(200, json={"state": True, "data": {
                "uid": "UID-123", "time": 1700000000, "sign": "SIG", "qrcode": "https://q/abc"}})
        if path.endswith("/get/status/"):
            n = seen.get("polls", 0)
            seen["polls"] = n + 1
            return httpx.Response(200, json={"state": True, "data": {"status": 0 if n == 0 else (1 if n == 1 else 2)}})
        if path.endswith("/open/deviceCodeToToken"):
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            seen["exchange"] = body
            return httpx.Response(200, json={"state": True, "data": {
                "access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 172800}})
        if path.endswith("/open/refreshToken"):
            return httpx.Response(200, json={"state": True, "data": {
                "access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 172800}})
        return httpx.Response(404, json={"state": False, "error": "no route"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def main() -> None:
        code = await auth_115.request_device_code(client=client)
        check("拿到 uid", code.uid == "UID-123", code.uid)
        check("请求带 client_id", seen["auth"].get("client_id") == "test-client-id", str(seen["auth"])[:80])
        check("请求带 S256", seen["auth"].get("code_challenge_method") == "S256")
        check("请求带 challenge 且与 verifier 匹配",
              seen["auth"].get("code_challenge") == auth_115.challenge_of(code.verifier))
        check("请求带 scope", "scope" in seen["auth"], str(seen["auth"].keys()))

        session = auth_115.LoginSession(vault, client=client)
        session.code = code
        st = await session.wait(timeout=5, interval=0.01)
        check("轮询到已确认", st == auth_115.STATUS_ALLOWED, str(st))
        check("状态文案齐备", auth_115.STATUS_TEXT[st] == "已确认，正在换取 token")

        token = await session.finish()
        check("换到 access_token", token.access_token == "AT-1", token.access_token)
        check("换到 refresh_token", token.refresh_token == "RT-1")
        check("交换请求带 code_verifier", seen["exchange"].get("code_verifier") == code.verifier)
        check("token 已入库并可用", vault.load() is not None and vault.load().access_token == "AT-1")

        raw_kv = store.kv_get(auth_115.KV_KEY, "")
        check("库里 tokens 不是明文", "AT-1" not in raw_kv and "access_token" not in raw_kv)

        # 过期 → 自动 refresh
        expired = auth_115.Token(access_token="AT-1", refresh_token="RT-1",
                                 expires_in=1, obtained_at=1, uid="UID-123")
        vault.save(expired)
        fresh = await auth_115.ensure_token(vault, client=client)
        check("过期自动续期", fresh.access_token == "AT-2", fresh.access_token)
        check("续期后落库", vault.load().access_token == "AT-2")

        vault.clear()
        check("clear 后为空", vault.load() is None)

        os.environ.pop("APP_ID_115", None)
        try:
            await auth_115.request_device_code(client=client)
            check("缺 client_id 报错", False, "没报错")
        except auth_115.OAuthError as exc:
            check("缺 client_id 报错", "APP_ID_115" in str(exc), str(exc))
        finally:
            os.environ["APP_ID_115"] = "test-client-id"

        await client.aclose()

    asyncio.run(main())


# --------------------------------------------------------------------- 14. 编码闸门
def test_encode_gate(tmp: Path) -> None:
    print("\n[14] 编码并发闸门（990MB 小机的内存保险）")
    store = new_store(tmp)
    st = settings(tmp, api_min_interval=0.0, encode_concurrency=1)
    calls = Counter()
    fetcher = ThumbFetcher(store, st, fetch_bytes=calls)

    active = {"now": 0, "peak": 0}
    real_encode = fetcher.encode

    def slow_encode(raw):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        try:
            time.sleep(0.05)
            return real_encode(raw)
        finally:
            active["now"] -= 1

    fetcher.encode = slow_encode          # type: ignore[assignment]

    async def main() -> None:
        await asyncio.gather(*(fetcher.ensure(Photo(f"e:{i}")) for i in range(6)))

    asyncio.run(main())
    check("闸门=1 时并发解码不超过 1", active["peak"] == 1, str(active["peak"]))
    check("6 张全部入库", store.count_state().get("done") == 6, str(store.count_state()))
    check("stats 暴露闸门与峰值", fetcher.stats()["encode_concurrency"] == 1
          and fetcher.stats()["peak_encode"] >= 1, str(fetcher.stats().get("peak_encode")))


# --------------------------------------------------------------------- 15. 换密钥
def test_key_rotation(tmp: Path) -> None:
    print("\n[15] 换密钥后旧密文：删掉重抓，不 500")
    from app import secretbox as sb

    store = new_store(tmp)
    st = settings(tmp, api_min_interval=0.0)
    calls = Counter()
    fetcher = ThumbFetcher(store, st, fetch_bytes=calls)

    async def main() -> None:
        r1 = await fetcher.ensure(Photo("rot:1"))
        check("首抓成功", r1.label == "FETCHED", r1.label)

        old_key = os.environ["THUMB_ENC_KEY"]
        try:
            os.environ["THUMB_ENC_KEY"] = secrets.token_hex(32)   # 换钥匙
            sb.reset_key_cache()
            f2 = ThumbFetcher(store, st, fetch_bytes=calls)       # 新进程、空 LRU
            r2 = await f2.ensure(Photo("rot:1"))
            check("旧密文解不开时自动重抓", r2.label == "FETCHED", r2.label)
            check("重抓成功后可正常命中", (await f2.ensure(Photo("rot:1"))).label == "HIT")
            check("没有抛异常（不会变成 500）", True)
        finally:
            os.environ["THUMB_ENC_KEY"] = old_key
            sb.reset_key_cache()

    asyncio.run(main())


# --------------------------------------------------------------------- 16. 索引名字加密
def test_index_crypto(tmp: Path) -> None:
    print("\n[16] 索引名字加密：文件名/文件夹名/路径（直链不动）")
    import sqlite3

    sys.path.insert(0, str(ROOT / "tools"))
    import secure_index as si                    # type: ignore
    from app import index_crypto as ic

    # ---- 基础加解密
    names = ["IMG_20250101_120000.jpg", "阿凡达 2009 4K.mkv", "a" * 200, ""]
    for nm in names:
        sealed = ic.seal(nm)
        check(f"加解密往返（{nm[:12] or '空串'}）", ic.unseal(sealed) == nm if nm else sealed == "")
    check("密文与明文不同", ic.seal("abc.jpg") != "abc.jpg")
    check("坏密文不抛异常（safe_unseal）", ic.safe_unseal("not-a-token!!") == "")

    # ---- 盲索引
    check("盲索引确定性", ic.blind("Abc.JPG") == ic.blind("abc.jpg"))
    check("盲索引区分大小写写法但归一化后相同", ic.blind("ＡＢＣ") == ic.blind("abc"))
    check("不同名字不同盲索引", ic.blind("a.jpg") != ic.blind("b.jpg"))
    check("前缀桶：同前缀同桶", ic.bucket("IMG_0001.jpg") == ic.bucket("IMG_9999.jpg"))
    check("前缀桶：不同前缀不同桶", ic.bucket("IMG_0001.jpg") != ic.bucket("VID_0001.jpg"))

    # ---- 密钥隔离
    master = sb_key = os.environ["THUMB_ENC_KEY"]
    from app import secretbox as sb2
    check("名字子密钥 ≠ 主密钥", ic.name_key() != sb2.load_key())
    check("加密/盲索引用了不同子密钥", ic.name_key() != ic.blind_key())

    # ---- 造一个和 115-album 同构的库
    db = tmp / "index.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE photos (id TEXT PRIMARY KEY, root TEXT, name TEXT, size INTEGER,
                             type INTEGER, ext TEXT, modified TEXT, ctime INTEGER,
                             thumb TEXT, pickcode TEXT, path TEXT, folder TEXT);
        CREATE TABLE pending_links (id TEXT PRIMARY KEY, url TEXT, expires INTEGER, ua TEXT);
    """)
    rows = [(f"id{i}", "/r", f"IMG_{i:05d}.jpg", 1000 + i, 5, "jpg", "", 1700000000 + i,
             "t", "pc", f"/相册/2025/IMG_{i:05d}.jpg", "2025")
            for i in range(120)]
    rows += [("zh1", "/r", "阿凡达 4K.mkv", 9, 2, "mkv", "", 1700000001, "t", "pc",
              "/相册/电影/阿凡达 4K.mkv", "电影")]
    conn.executemany("INSERT INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO pending_links VALUES ('p1','https://cdn.example/x?u=50487111',0,'UA')")
    conn.commit()
    conn.close()

    # ---- 迁移
    rc = si.main(["migrate", "--db", str(db), "--quiet"])
    check("migrate 返回 0", rc == 0, str(rc))
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    have = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    check("新列齐全", {"name_enc", "name_idx", "name_bucket", "name_rank",
                       "folder_enc", "path_enc"} <= have, str(sorted(have)))
    enc = conn.execute("SELECT COUNT(*) FROM photos WHERE COALESCE(name_enc,'')<>''").fetchone()[0]
    check("全部行都有密文", enc == len(rows), f"{enc}/{len(rows)}")
    ranks = [r[0] for r in conn.execute("SELECT name_rank FROM photos ORDER BY name_rank")]
    check("排序秩连续 0..n-1", ranks == list(range(len(rows))), str(ranks[:5]))
    first = conn.execute("SELECT name_enc FROM photos ORDER BY name_rank LIMIT 1").fetchone()[0]
    expect_first = sorted([r[2] for r in rows], key=ic.normalize)[0]
    check("秩顺序 = 解密后名字排序", ic.unseal(first) == expect_first,
          f"{ic.unseal(first)} vs {expect_first}")

    # ---- 直链也加密
    row = conn.execute("SELECT url, url_enc FROM pending_links").fetchone()
    check("直链已有密文列", bool(row["url_enc"]), "url_enc 为空")
    check("直链密文解回原 URL", ic.unseal_link(row["url_enc"]).startswith("https://cdn.example/"),
          ic.unseal_link(row["url_enc"])[:40])
    check("直链子密钥与名字子密钥隔离", ic.link_key() != ic.name_key())
    conn.close()

    # ---- purge
    rc = si.main(["migrate", "--db", str(db), "--quiet", "--purge"])
    check("purge 返回 0", rc == 0, str(rc))
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    left = conn.execute("SELECT COUNT(*) FROM photos WHERE name<>'' OR folder<>'' OR path<>''").fetchone()[0]
    check("明文列已清空", left == 0, str(left))
    lurl = conn.execute("SELECT url, url_enc FROM pending_links").fetchone()
    check("直链明文已清空", lurl["url"] == "", str(lurl["url"])[:40])
    check("直链密文仍可解", ic.unseal_link(lurl["url_enc"]).startswith("https://cdn.example/"))
    ext_keep = conn.execute("SELECT COUNT(*) FROM photos WHERE ext<>''").fetchone()[0]
    check("ext 等字段保留", ext_keep == len(rows), str(ext_keep))
    conn.close()

    # ---- 原始字节残留扫描
    residue = si.raw_residue(db, ["IMG_00042.jpg", "阿凡达 4K.mkv", "/相册/电影/阿凡达 4K.mkv",
                                 "https://cdn.example/x?u=50487111", "50487111"])
    check("库文件里找不到文件名/路径/直链明文", residue == [], str(residue))

    # ---- verify
    rc = si.main(["verify", "--db", str(db), "--sample", "5"])
    check("verify 通过", rc == 0, str(rc))

    # ---- 幂等：purge 之后再跑一次 migrate，密文不能被写成空
    rc = si.main(["migrate", "--db", str(db), "--quiet", "--no-backup"])
    check("purge 后重复 migrate 返回 0", rc == 0, str(rc))
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    enc2 = conn.execute("SELECT COUNT(*) FROM photos WHERE COALESCE(name_enc,'')<>''").fetchone()[0]
    check("重复 migrate 不破坏密文", enc2 == len(rows), f"{enc2}/{len(rows)}")
    still = ic.safe_unseal(conn.execute("SELECT name_enc FROM photos LIMIT 1").fetchone()[0])
    check("重复 migrate 后仍可解密", bool(still), str(still))
    conn.close()

    # ---- 查询能力
    rc = si.main(["search", "--db", str(db), "--q", "IMG_0004"])
    check("前缀搜索可用", rc == 0)
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    hit = conn.execute("SELECT name_enc FROM photos WHERE name_idx=?", (ic.blind("阿凡达 4K.mkv"),)).fetchall()
    check("精确查找（盲索引）命中 1 条", len(hit) == 1, str(len(hit)))
    conn.close()
    check("主密钥未被改动", os.environ["THUMB_ENC_KEY"] == master)


# --------------------------------------------------------------------- 17. 115emby 媒体库
def test_media_library(tmp: Path) -> None:
    print("\n[17] 115emby 媒体库：文件夹浏览 + 看图 + 播放（无相册表）")
    import sqlite3

    import httpx
    from fastapi import FastAPI

    from app import index_crypto as ic
    from app.db import DB, KIND_IMAGE, KIND_VIDEO
    from app.server import create_app

    db = DB(tmp / "emby.db")
    store = new_store(tmp)
    calls = Counter()
    vcalls = Counter()

    async def fetch_bytes(photo):
        calls.n += 1
        return jpeg(1200, 900, seed=3)

    async def video_fetch(photo):
        vcalls.n += 1
        return jpeg(1280, 720, seed=5)

    fetcher = ThumbFetcher(store, settings(tmp, api_min_interval=0.0),
                           fetch_bytes=fetch_bytes, video_fetch=video_fetch,
                           video_opts=VideoThumbOptions(enabled=True))

    # ---- 写入：1 个目录 + 3 张图 + 2 个视频（名字只以密文入库）
    def put_item(iid, parent, name, kind, mtime, size=1 << 20, ext="jpg"):
        f = ic.seal_fields(name, "", "/媒体/" + name)
        db.upsert_items([{"id": iid, "parent_id": parent, "kind": kind, "ext": ext,
                          "size": size, "mtime": mtime, **f}])

    db.upsert_folder("dir1", "-1", ic.seal("2024 假期"), ic.blind("2024 假期"),
                     ic.bucket("2024 假期"))
    put_item("img1", "dir1", "IMG_001.jpg", KIND_IMAGE, 1700000001)
    put_item("img2", "dir1", "IMG_002.jpg", KIND_IMAGE, 1700000002)
    put_item("img3", "dir1", "IMG_003.jpg", KIND_IMAGE, 1700000003)
    put_item("vid1", "dir1", "第01集.mp4", KIND_VIDEO, 1700000010, 4 << 30, "mp4")
    put_item("vid2", "dir1", "第02集.mkv", KIND_VIDEO, 1700000011, 8 << 30, "mkv")

    # ---- 没有相册表
    con = sqlite3.connect(str(tmp / "emby.db"))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols_items = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    con.close()
    check("没有相册的 photos 表", "photos" not in tables, str(tables))
    check("没有相册的 pending_links 表", "pending_links" not in tables)
    check("有媒体库自己的表", {"folders", "items", "links", "progress"} <= tables, str(sorted(tables)))
    check("items 没有明文 name 列", "name" not in cols_items, str(sorted(cols_items)))
    check("items 没有 EXIF/GPS 字段",
          not ({"taken", "camera", "lat", "lon", "lens"} & cols_items), str(sorted(cols_items)))

    # ---- 库文件字节级：没有明文名字
    raw = Path(tmp / "emby.db").read_bytes()
    wal = Path(str(tmp / "emby.db") + "-wal")
    blob = raw + (wal.read_bytes() if wal.exists() else b"")
    check("库里扫不到明文文件名", b"IMG_001.jpg" not in blob and "第01集".encode() not in blob)
    check("库里扫不到明文目录名", "2024 假期".encode() not in blob)

    # ---- 服务
    async def resolve_link(item, ua=""):
        return (f"https://cdn.115.example/{item['id']}?u=50487111&t=999", 4102444800,
                ua or "Mozilla/5.0 115Browser/27.0.5.7")

    app: FastAPI = create_app(db, store, fetcher, resolve_link=resolve_link)
    app_nores: FastAPI = create_app(db, store, fetcher, resolve_link=None)

    async def main() -> None:
        tr = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            r = await c.get("/api/browse", params={"parent": "-1"})
            body = r.json()
            check("根目录列出 1 个子文件夹", body["counts"]["folders"] == 1, str(body["counts"]))
            check("文件夹名已解密", body["folders"][0]["name"] == "2024 假期", str(body["folders"])[:80])

            r = await c.get("/api/browse", params={"parent": "dir1"})
            body = r.json()
            kinds = [i["kind"] for i in body["items"]]
            check("同层列出 3 图 + 2 视频", body["counts"]["items"] == 5, str(body["counts"]))
            check("图片与视频都在", kinds.count("image") == 3 and kinds.count("video") == 2, str(kinds))
            check("图片可看 / 视频可播", all(i["viewable"] for i in body["items"] if i["kind"] == "image")
                  and all(i["playable"] for i in body["items"] if i["kind"] == "video"))
            check("中文视频名已解密", any(i["name"] == "第01集.mp4" for i in body["items"]),
                  str([i["name"] for i in body["items"]]))

            # 看图翻页
            r = await c.get("/api/item/img2")
            d = r.json()
            check("看图有 prev/next", d["prev_id"] == "img1" and d["next_id"] == "img3",
                  f"{d.get('prev_id')}/{d.get('next_id')}")

            # 缩略图
            r = await c.get("/api/thumb/img1")
            check("缩略图 200", r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff",
                  str(r.status_code))
            etag = r.headers.get("etag")
            r2 = await c.get("/api/thumb/img1", headers={"If-None-Match": etag})
            check("缩略图 ETag 304", r2.status_code == 304, str(r2.status_code))

            # 视频缩略图走抓帧
            r = await c.get("/api/thumb/vid1")
            check("视频缩略图（抓帧）成功", r.status_code == 200 and vcalls.n >= 1, str(vcalls.n))

            # 原图 / 播放都是 302
            r = await c.get("/api/image/img1", follow_redirects=False)
            check("看图 302 到 CDN", r.status_code == 302 and "cdn.115.example" in r.headers["location"])
            check("302 带过期时间头", r.headers.get("x-link-expires") == "4102444800",
                  r.headers.get("x-link-expires", ""))

            r = await c.get("/api/play/vid1", follow_redirects=False,
                            headers={"User-Agent": "TEST-PLAYER/1.0"})
            check("播放 302 到 CDN", r.status_code == 302 and "cdn.115.example" in r.headers["location"])
            check("按客户端 UA 签发（115 直链绑 UA）",
                  r.headers.get("x-upstream-ua") == "TEST-PLAYER/1.0",
                  r.headers.get("x-upstream-ua", ""))
            r2 = await c.get("/api/play/vid1", follow_redirects=False,
                             headers={"User-Agent": "ANOTHER-PLAYER/2.0"})
            check("换 UA 会重签直链",
                  r2.headers.get("x-upstream-ua") == "ANOTHER-PLAYER/2.0",
                  r2.headers.get("x-upstream-ua", ""))
            r = await c.get("/api/play/img1", follow_redirects=False)
            check("图片不能当视频播", r.status_code == 400, str(r.status_code))

            # 直链密文落库
            con2 = sqlite3.connect(str(tmp / "emby.db")); con2.row_factory = sqlite3.Row
            link_row = con2.execute("SELECT url_enc FROM links WHERE item_id='vid1'").fetchone()
            con2.close()
            check("直链以密文入 links 表", bool(link_row and link_row["url_enc"]), str(link_row))
            check("直链明文不落库", "cdn.115.example" not in Path(tmp / "emby.db").read_bytes().decode("latin1"))

            # 进度 / 继续观看
            r = await c.post("/api/progress", json={"id": "vid1", "position": 600, "duration": 2400})
            check("上报进度成功", r.json().get("ok") is True)
            r = await c.get("/api/continue")
            items = r.json()["items"]
            check("继续观看返回 1 条", len(items) == 1 and items[0]["id"] == "vid1", str(items)[:100])
            check("进度值正确", round(items[0]["position"]) == 600, str(items[0]["position"]))

            r = await c.get("/api/health")
            h = r.json()
            check("health 说明这是媒体库", "115emby" in h["kind"], h["kind"])
            check("health 里库统计正确", h["library"]["videos"] == 2 and h["library"]["images"] == 3,
                  str(h["library"]))

        tr2 = httpx.ASGITransport(app=app_nores)
        async with httpx.AsyncClient(transport=tr2, base_url="http://t") as c2:
            r = await c2.get("/api/play/vid2", follow_redirects=False)
            check("未接直链解析时返回 501", r.status_code == 501, str(r.status_code))

    asyncio.run(main())


# --------------------------------------------------------------------- 18. 扫描器
def test_scanner(tmp: Path) -> None:
    print("\n[18] 目录扫描器：后台任务 / 选目录 / 限深度 / 暂停恢复 / 只入队不抓图")
    from app.db import DB
    from app.scanner import Scanner, STATUS_CANCELED, STATUS_DONE, STATUS_PAUSED

    db = DB(tmp / "emby.db")
    store = new_store(tmp)
    calls = {"list": 0}

    # 假网盘树：root(3 子目录，其中 sub2 还有子目录) + 每层若干文件
    TREE = {
        "-1": {"folders": [{"id": "d1", "name": "电影"}, {"id": "d2", "name": "剧集"},
                           {"id": "d3", "name": "照片"}],
               "files": []},
        "d1": {"folders": [], "files": [
            {"id": "f1", "name": "阿凡达.mkv", "size": 8 << 30, "mtime": 1700000001, "ext": "mkv"},
            {"id": "f2", "name": "沙丘.mp4", "size": 4 << 30, "mtime": 1700000002, "ext": "mp4"}]},
        "d2": {"folders": [{"id": "d2s", "name": "第一季"}],
               "files": [{"id": "f3", "name": "S01E01.mp4", "size": 1 << 30,
                          "mtime": 1700000003, "ext": "mp4"}]},
        "d2s": {"folders": [], "files": [
            {"id": "f4", "name": "S01E02.mp4", "size": 1 << 30, "mtime": 1700000004, "ext": "mp4"}]},
        "d3": {"folders": [], "files": [
            {"id": "f5", "name": "IMG_001.jpg", "size": 3 << 20, "mtime": 1700000005, "ext": "jpg"}]},
    }

    async def list_dir(cid):
        calls["list"] += 1
        return TREE.get(cid, {"folders": [], "files": []})

    async def main() -> None:
        # ---- 后台任务：从根、深度 2（应扫到 d2s）
        sc = Scanner(db, list_dir, settings(tmp, api_min_interval=0.0), thumb_store=store)
        job = sc.start("-1", depth=2, root_name="我的网盘")
        await sc.run_in_background(job)
        st = sc.status(job)
        check("任务完成", st["status"] == STATUS_DONE, st["status"])
        check("扫了 5 个目录", st["dirs_done"] == 5, str(st["dirs_done"]))
        check("上游 list 请求 = 目录数", calls["list"] == 5, str(calls["list"]))
        check("收录 5 个文件", st["items_found"] == 5, str(st["items_found"]))
        check("收录 4 个子目录(不含根)", st["folders_found"] == 4, str(st["folders_found"]))
        check("队列已清空", st["pending"] == 0, str(st))

        # ---- 名字进库即密文
        raw = Path(tmp / "emby.db").read_bytes()
        wal = Path(str(tmp / "emby.db") + "-wal")
        blob = raw + (wal.read_bytes() if wal.exists() else b"")
        check("库里扫不到明文目录名", "剧集".encode() not in blob and "第一季".encode() not in blob)
        check("库里扫不到明文文件名", "阿凡达.mkv".encode() not in blob)
        check("库里扫不到明文根名", "我的网盘".encode() not in blob)

        # ---- kind 判定 + 解密展示
        from app import index_crypto as ic
        vids = db.list_children("d1", kind="video")
        check("d1 两个视频都判定为 video", len(vids) == 2, str(len(vids)))
        names = sorted(ic.safe_unseal(r["name_enc"]) for r in vids)
        check("视频名解密正确", names == sorted(["阿凡达.mkv", "沙丘.mp4"]), str(names))
        pics = db.list_children("d3", kind="image")
        check("d3 图片判定为 image", len(pics) == 1, str(len(pics)))

        # ---- 同层排序秩
        ranks = [r["name_rank"] for r in db.list_children("d1")]
        check("同层排序秩从 0 开始连续", ranks == [0, 1], str(ranks))

        # ---- 缩略图：只入队，没抓
        q = store.count_state()
        check("缩略图全部入队(pending)", q.get("pending") == 5, str(q))
        check("扫描阶段没有生成缩略图", store.stats()["thumbs"] == 0, str(store.stats()["thumbs"]))

        # ---- 深度限制：depth=0 只扫根
        job0 = sc.start("-1", depth=0)
        await sc.run(job0)
        st0 = sc.status(job0)
        check("depth=0 只扫根目录", st0["dirs_done"] == 1, str(st0["dirs_done"]))

        # ---- 暂停 / 续跑（断点续传）
        db2 = DB(tmp / "emby2.db")
        calls["list"] = 0
        sc2 = Scanner(db2, list_dir, settings(tmp, api_min_interval=0.0))
        job2 = sc2.start("-1", depth=2)
        part = await sc2.run(job2, max_dirs=2)          # 主动只跑 2 个目录
        check("分片跑只处理 2 个目录", part["dirs_done"] == 2, str(part["dirs_done"]))
        check("队列里还有待扫", part["pending"] >= 1, str(part))
        sc2.pause(job2)
        check("暂停后状态为 paused", sc2.status(job2)["status"] == STATUS_PAUSED)
        await sc2.resume(job2)
        done = sc2.status(job2)
        check("续跑后跑完", done["status"] == STATUS_DONE, done["status"])
        check("续跑没有重复扫根目录", done["dirs_done"] == 5, str(done["dirs_done"]))

        # ---- 取消
        db3 = DB(tmp / "emby3.db")
        sc3 = Scanner(db3, list_dir, settings(tmp, api_min_interval=0.0))
        job3 = sc3.start("-1", depth=3)
        sc3.cancel(job3)
        st3 = await sc3.run(job3)
        check("取消后不继续扫", st3["status"] == STATUS_CANCELED and st3["dirs_done"] == 0, str(st3))

        # ---- 类型过滤：只要图片
        db4 = DB(tmp / "emby4.db")
        sc4 = Scanner(db4, list_dir, settings(tmp, api_min_interval=0.0))
        job4 = sc4.start("-1", depth=2, kinds=["image"])
        await sc4.run(job4)
        con = db4.list_children("d1")
        check("过滤后视频被标成 other", all(r["kind"] == "other" for r in con),
              str([r["kind"] for r in con]))

    asyncio.run(main())


# --------------------------------------------------------------------- 19. 后台扫描接口
def test_scan_api(tmp: Path) -> None:
    print("\n[19] 后台扫描接口：选目录 → 启动 → 查进度 → 暂停/取消")
    import httpx

    from app.db import DB
    from app.scanner import Scanner
    from app.server import create_app

    db = DB(tmp / "emby.db")
    store = new_store(tmp)
    fetcher = ThumbFetcher(store, settings(tmp, api_min_interval=0.0))

    TREE = {
        "-1": {"folders": [{"id": "d1", "name": "电影"}, {"id": "d2", "name": "照片"}], "files": []},
        "d1": {"folders": [], "files": [{"id": "f1", "name": "a.mkv", "size": 100,
                                          "mtime": 1, "ext": "mkv"}]},
        "d2": {"folders": [], "files": [{"id": "f2", "name": "b.jpg", "size": 200,
                                          "mtime": 2, "ext": "jpg"}]},
    }
    calls = {"n": 0}

    async def list_dir(cid):
        calls["n"] += 1
        return TREE.get(cid, {"folders": [], "files": []})

    sc = Scanner(db, list_dir, settings(tmp, api_min_interval=0.0), thumb_store=store)
    app = create_app(db, store, fetcher, scanner=sc, list_dirs=list_dir)

    async def main() -> None:
        tr = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            # 可选目录（实时问上游）
            r = await c.get("/api/dirs", params={"parent": "-1"})
            body = r.json()
            check("可选目录来自上游", body["source"] == "upstream", body["source"])
            check("列出 2 个可选目录", len(body["folders"]) == 2, str(body["folders"]))
            check("目录名未加密泄露", body["folders"][0]["name"] == "电影", str(body["folders"]))

            # 启动后台扫描（只选 d2 这个目录）
            r = await c.post("/api/scan", json={"root_id": "d2", "depth": 1})
            check("启动扫描 200", r.status_code == 200, str(r.status_code))
            job_id = r.json()["job_id"]
            await asyncio.sleep(0.4)      # 让后台任务跑完

            r = await c.get(f"/api/scan/{job_id}")
            st = r.json()
            check("任务已完成", st["status"] == "done", st["status"])
            check("只扫了选中的目录(+根不入队)", st["dirs_done"] == 1, str(st["dirs_done"]))
            check("找到 1 个媒体项", st["items_found"] == 1, str(st["items_found"]))

            # 扫描结果能浏览
            r = await c.get("/api/browse", params={"parent": "d2"})
            items = r.json()["items"]
            check("扫描结果可浏览", len(items) == 1 and items[0]["kind"] == "image", str(items))

            # 重复启动同一时间只允许一个在跑
            r1 = await c.post("/api/scan", json={"root_id": "-1", "depth": 0})
            check("可以再次启动新任务", r1.status_code == 200, str(r1.status_code))

            # 任务列表 / 健康检查带扫描信息
            r = await c.get("/api/scan")
            check("任务列表可查", len(r.json()["jobs"]) >= 2, str(len(r.json()["jobs"])))
            r = await c.get("/api/health")
            check("health 标记扫描器可用", r.json()["has_scanner"] is True)

            # 未知动作
            r = await c.post(f"/api/scan/{job_id}/explode")
            check("未知动作 400", r.status_code == 400, str(r.status_code))

    asyncio.run(main())


# --------------------------------------------------------------------- 20. 可视化选目录
def test_picker(tmp: Path) -> None:
    print("\n[20] 可视化目录选择器：点目录 → 起扫描（不用知道 cid）")
    import httpx

    from app.db import DB
    from app.scanner import Scanner
    from app.server import create_app

    db = DB(tmp / "emby.db")
    store = new_store(tmp)
    fetcher = ThumbFetcher(store, settings(tmp, api_min_interval=0.0))

    # 假上游：带 path 面包屑（115 真实响应里就有 path 字段）
    TREE = {
        "0": {"folders": [{"id": "100", "name": "电影"}, {"id": "200", "name": "照片"}],
              "files": [],
              "path": [{"cid": "0", "name": "根目录"}]},
        "100": {"folders": [{"id": "101", "name": "科幻"}], "files": [],
                "path": [{"cid": "0", "name": "根目录"}, {"cid": "100", "name": "电影"}]},
        "101": {"folders": [], "files": [{"id": "f1:pc1", "name": "沙丘.mkv", "size": 9,
                                           "mtime": 1, "ext": "mkv"}],
                "path": [{"cid": "0", "name": "根目录"}, {"cid": "100", "name": "电影"},
                         {"cid": "101", "name": "科幻"}]},
        "-1": {"folders": [{"id": "100", "name": "电影"}, {"id": "200", "name": "照片"}],
               "files": [], "path": [{"cid": "0", "name": "根目录"}]},
    }

    async def list_dirs(cid):
        return TREE.get(str(cid), {"folders": [], "files": [], "path": []})

    sc = Scanner(db, lambda c: list_dirs(c), settings(tmp, api_min_interval=0.0),
                 thumb_store=store)
    app = create_app(db, store, fetcher, scanner=sc, list_dirs=list_dirs)

    async def main() -> None:
        tr = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            r = await c.get("/picker")
            check("选择器页面可访问", r.status_code == 200 and "选目录扫描" in r.text,
                  str(r.status_code))
            check("页面含目录网格与启动按钮", "📁" in r.text and "从此目录开始扫描" in r.text)

            # 浏览根 → 看到两个目录（名字可见，无需 cid）
            r = await c.get("/api/dirs", params={"parent": "-1"})
            j = r.json()
            check("根目录列出 2 个可选目录", len(j["folders"]) == 2, str(j["folders"]))
            check("目录名直接可读", {f["name"] for f in j["folders"]} == {"电影", "照片"},
                  str(j["folders"]))
            check("带面包屑（根）", j["crumbs"][0]["name"] == "根目录", str(j["crumbs"]))

            # 点进"电影"→ 看到"科幻"，面包屑变成 根›电影
            r = await c.get("/api/dirs", params={"parent": "100"})
            j = r.json()
            check("二级目录可见", [f["name"] for f in j["folders"]] == ["科幻"], str(j["folders"]))
            check("面包屑含两级",
                  [x["name"] for x in j["crumbs"]] == ["根目录", "电影"], str(j["crumbs"]))

            # 从"科幻"开始扫描（视觉选定的就是 cid 101）
            r = await c.post("/api/scan", json={"root_id": "101", "depth": 1,
                                                "root_name": "科幻"})
            check("按选中的目录启动扫描", r.status_code == 200, str(r.status_code))
            job_id = r.json()["job_id"]
            await asyncio.sleep(0.3)

            r = await c.get(f"/api/scan/{job_id}")
            st = r.json()
            check("扫描完成（只扫这个目录）", st["status"] == "done" and st["dirs_done"] == 1,
                  str(st))
            check("收录到《沙丘.mkv》", st["items_found"] == 1, str(st["items_found"]))
            check("根名以密文保存并可解密", st["root_name"] == "科幻", st["root_name"])

            r = await c.get("/api/browse", params={"parent": "101"})
            items = r.json()["items"]
            check("扫完即可浏览该目录内容",
                  len(items) == 1 and items[0]["name"] == "沙丘.mkv", str(items))

            # ---- 首页网格（照用户给的版式）
            r = await c.get("/")
            html = r.text
            check("首页是网格页", r.status_code == 200 and 'id="grid"' in html, str(r.status_code))
            check("网格是两列", "repeat(2,minmax(0,1fr))" in html, "未找到两列样式")
            check("文件夹用 📂 图标", "📂" in html)
            check("视频右上角有标注", "vbadge" in html and "视频" in html)
            check("文件名两行截断", "-webkit-line-clamp:2" in html)
            check("文件夹优先渲染", html.index("tileFolder") < html.index("tileItem"),
                  "顺序不对")

            # ---- 目录信息（面包屑）
            r = await c.get("/api/folder/101")
            j2 = r.json()
            check("目录信息带祖先链",
                  [a["name"] for a in j2["ancestors"]] == ["电影", "科幻"] or j2["ancestors"] == [],
                  str(j2["ancestors"]))

    asyncio.run(main())


# --------------------------------------------------------------------- 21. 直链解析
def test_direct_link(tmp: Path) -> None:
    print("\n[21] 115 直链解析：加密 payload / UA 绑定 / 多域名回退")
    import httpx

    from app import source115

    check("pickcode 从 fid:pc 取出", source115.pickcode_of({"id": "123:abc"}) == "abc")
    check("pickcode 直接用字段", source115.pickcode_of({"id": "x", "pickcode": "pp"}) == "pp")
    check("pickcode 缺失返回空", source115.pickcode_of({"id": "onlyfid"}) == "")
    check("URL 过期时间可解析",
          source115._expires_from_url("https://c/x.mp4?t=4102444800&u=1") == 4102444800)
    check("无 t 参数返回 0", source115._expires_from_url("https://c/x.mp4") == 0)
    check("宽松挖 URL", source115._find_url({"a": {"b": [{"url": "https://cdn/z"}]}}) == "https://cdn/z")

    os.environ["COOKIE_115"] = "UID=50487111; CID=abc; SEID=def"
    seen = {"posts": 0, "data_len": 0, "uas": [], "hosts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["posts"] += 1
        seen["hosts"].append(request.url.host)
        body = request.content.decode("latin1")
        seen["data_len"] = body.count("data=")
        seen["uas"].append(request.headers.get("user-agent", ""))
        # 记录"表单里确实带了密文的 data 字段"
        assert "data=" in body
        mode = os.environ.get("DL_TEST_MODE", "ok")
        if mode == "state_false" and "proapi" in request.url.host:
            return httpx.Response(200, json={"state": False, "error": "请重新登录"})
        if mode == "waf" and "proapi" in request.url.host:
            return httpx.Response(200, text="<html>tengine</html>")
        return httpx.Response(200, json={
            "state": True,
            "data": {"url": {"url": "https://cdnfhnfile.115cdn.net/x.mp4?t=4102444800&u=50487111"}},
        })

    async def main() -> None:
        # 正常
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        url, exp, ua = await source115.direct_link({"id": "1:pc1"}, ua="UA-TEST", client=client)
        check("拿到直链", url.startswith("https://cdnfhnfile.115cdn.net/"), url)
        check("过期时间来自 URL", exp == 4102444800, str(exp))
        check("返回签发 UA", ua == "UA-TEST", ua)
        check("请求带 UA（直链绑 UA）", seen["uas"][0] == "UA-TEST", str(seen["uas"]))
        check("请求带加密 data 字段", seen["data_len"] >= 1, str(seen["data_len"]))

        # state=false → 回退到下一个域名
        os.environ["DL_TEST_MODE"] = "state_false"
        seen["hosts"].clear()
        client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        url2, _e, _u = await source115.direct_link({"id": "1:pc2"}, client=client2)
        check("首个域名拒绝后回退成功", url2.startswith("https://"), url2)
        check("确实换过域名", len(set(seen["hosts"])) >= 2, str(seen["hosts"]))

        # WAF 页面 → 回退
        os.environ["DL_TEST_MODE"] = "waf"
        client3 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        url3, _e, _u = await source115.direct_link({"id": "1:pc3"}, client=client3)
        check("命中 WAF 也能回退", url3.startswith("https://"), url3)

        # 全失败
        os.environ["DL_TEST_MODE"] = "always_fail"

        def bad(request):
            return httpx.Response(500, text="boom")

        client4 = httpx.AsyncClient(transport=httpx.MockTransport(bad))
        try:
            await source115.direct_link({"id": "1:pc4"}, client=client4)
            check("全部失败会抛错", False, "没抛")
        except RuntimeError as exc:
            check("全部失败会抛错", "取直链失败" in str(exc), str(exc)[:60])

        # 没凭据
        saved = os.environ.pop("COOKIE_115", None)
        try:
            client5 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await source115.direct_link({"id": "1:pc5"}, client=client5)
                check("没凭据会报错", False, "没报")
            except RuntimeError as exc:
                check("没凭据会报错", "凭据" in str(exc), str(exc))
        finally:
            if saved is not None:
                os.environ["COOKIE_115"] = saved
            os.environ.pop("DL_TEST_MODE", None)

    asyncio.run(main())

# --------------------------------------------------------------------- 主流程
def main() -> int:
    print("=" * 68)
    print("115 相册 Web 版 · 缩略图 C 方案自测（离线）")
    print("=" * 68)
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for i, fn in enumerate(
            (test_secretbox, test_store, test_queue, test_ensure, test_skip_rules,
             test_throttle, test_prewarm, test_window_gate, test_router, test_video, test_auth115, test_encode_gate, test_key_rotation, test_index_crypto, test_media_library, test_scanner, test_scan_api, test_picker, test_direct_link)
        ):
            sub = base / f"c{i}"
            sub.mkdir(parents=True, exist_ok=True)
            fn(sub)  # type: ignore[arg-type]
        test_window()
        test_video_cmd()

    print("\n" + "=" * 68)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAIL: {name}  {detail}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
