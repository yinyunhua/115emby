"""低优先格式（rm/rmvb/ts/m2ts/mpg）抓帧优化：显式 demuxer，跳过探测。

问题
----
rm/rmvb 这类老容器没有全局索引（moov/index 结构），ffmpeg 只能顺序探测；
默认 `-ss 1` + 3MB probesize 在 1GB 的 rmvb 上会一直读，实测 150 秒超时。

做法
----
按扩展名**显式指定 demuxer**（`-f rm` / `-f mpegts` / `-f mpeg`），
ffmpeg 就不用猜格式；这类文件一律从 0 秒开始、探测预算压到 512KB，
确认流之后再抓一帧。正常 mp4/mkv/avi 仍走原来的通用路径。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time

from . import source115
from . import v115_crypto as crypto
from .video_thumb import VideoThumbOptions

_logged_missing = False

# 扩展名 → (demuxer, 是否低优先)
DEMUX = {
    "rm": "rm", "rmvb": "rm",
    "ts": "mpegts", "m2ts": "mpegts",
    "mpg": "mpeg", "mpeg": "mpeg",
}
LOWPRI = {"rm", "rmvb", "ts", "m2ts", "mpg", "mpeg"}


class _Gate:
    def __init__(self, interval: float, concurrency: int = 1) -> None:
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


# 并发数：缩略图是同步抓帧，1 太慢（整屏视频要 30+ 秒），2 明显更快且内存安全
# （实测 1 并发峰值 154MB / 上限 448MB）
_gate = _Gate(float(os.getenv("VIDEO_API_MIN_INTERVAL") or 1.0),
              max(1, int(os.getenv("VIDEO_CONCURRENCY") or 2)))


def _bases():
    return [b for b in (
        os.getenv("PRO_APP_BASE_115", "").strip() or "https://proapi.115.com",
        os.getenv("API_BASE_115", "").strip() or "https://webapi.115.com",
        os.getenv("APP_BASE_115", "").strip() or "https://appapi.115.com",
    ) if b]


async def frame_source(item: dict) -> tuple[str, dict]:
    import httpx

    pc = source115.pickcode_of(item)
    if not pc or not source115.ready():
        return "", {}
    # 必须用浏览器 UA：115 CDN 会 403 掉 115Browser 这类客户端 UA，且直链与签发 UA 绑定，
    # 所以 downurl 的 UA 与后面取流的 UA 必须是同一个（见 source115.CDN_UA 注释）。
    ua = source115.CDN_UA
    base_headers = dict(source115._headers())
    base_headers["User-Agent"] = ua

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0),
                                 follow_redirects=True) as client:
        for base in _bases():
            key = crypto.generate_key()
            form = {"data": crypto.encode(json.dumps({"pickcode": pc}).encode("utf-8"), key)}
            try:
                async with _gate:
                    resp = await client.post(base + "/app/chrome/downurl",
                                             params={"t": str(int(time.time()))},
                                             data=form, headers=base_headers)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                js = resp.json()
            except Exception:
                continue
            if isinstance(js, dict) and js.get("state") is False:
                continue

            url = ""
            payload = js.get("data") if isinstance(js, dict) else None
            if isinstance(payload, str) and payload:
                try:
                    plain = crypto.decode(payload, key)
                    url = source115._find_url(json.loads(plain.decode("utf-8", "replace")))
                except Exception:
                    url = ""
            elif isinstance(payload, (dict, list)):
                url = source115._find_url(payload)
            if not url:
                continue

            dl_cookie = "; ".join("%s=%s" % (c.name, c.value) for c in resp.cookies.jar)
            headers = {"User-Agent": ua, "Referer": "https://115.com/", "Accept": "*/*"}
            parts = [p for p in (source115.cookie(), dl_cookie) if p]
            if parts:
                headers["Cookie"] = "; ".join(parts)
            return url, headers
    return "", {}


def _lean_cmd(ff: str, url: str, headers: dict, opts, ext: str = "") -> list:
    hdrs = "\r\n".join("%s: %s" % (k, v) for k, v in headers.items()) + "\r\n"
    ext = (ext or "").lower()
    demux = DEMUX.get(ext)
    probe = int(getattr(opts, "max_bytes", 3145728) or 3145728)
    seek = float(getattr(opts, "seek", 1.0) or 0.0)
    if demux:                       # 老容器：从 0 开始，探测预算压低
        probe = min(probe, 512 * 1024)
        seek = 0.0
    # 实测（30GB mp4）：探测预算从 3MB 降到 512KB、加 fastseek 略快，
    # 而 `-skip_frame nokey` 会让一批老 mp4/avi 直接报错
    # （rc=234 Codec AVOption skip_frame is not a decoding option / rc=183），故移除。
    if not demux:
        probe = min(probe, 512 * 1024)
    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rw_timeout", str(max(5, int(getattr(opts, "timeout", 60) or 60)) * 1_000_000),
        "-probesize", str(probe),
        "-analyzeduration", str(max(1, probe // 1024) * 1000),
        "-threads", "1", "-filter_threads", "1",
        "-user_agent", headers.get("User-Agent", ""),
        "-headers", hdrs,
    ]
    if demux:
        cmd += ["-f", demux]
    else:
        cmd += ["-fflags", "+fastseek", "-noaccurate_seek"]
    cmd += ["-ss", "%.2f" % max(0.0, seek), "-i", url]
    cmd += [
        "-an", "-sn", "-dn",
        "-frames:v", "1",
        # 修：老 avi/rmvb 常见「非全范围 YUV」，mjpeg 编码器会直接拒绝
        #     （rc=234: Non full-range YUV is non-standard / ff_frame_thread_encoder_init failed）
        "-strict", "-1",
        "-pix_fmt", "yuvj420p",
        "-vf", "scale=%d:-2" % max(16, int(getattr(opts, "size", 384) or 384)),
        "-f", "image2", "-vcodec", "mjpeg", "-q:v", "4",
        "pipe:1",
    ]
    return cmd


def _run(cmd: list, timeout: float):
    global _logged_missing
    if not shutil.which(cmd[0]):
        if not _logged_missing:
            print("未找到 ffmpeg：%s" % cmd[0])
            _logged_missing = True
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("ffmpeg 超时（%.0fs）" % timeout)
        return None
    except OSError as exc:
        print("ffmpeg 启动失败：%s" % exc)
        return None
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")[:200].replace("\n", " ")
        print("ffmpeg 未解出帧 rc=%s：%s" % (p.returncode, err))
        return None
    return p.stdout or None


def _retry_cmd(cmd: list, ss: str = "", extra: list | None = None) -> list:
    """在基础命令上派生一条重试命令：改 -ss、加额外参数（放在 -i 之前）。

    返回 None 表示这条命令没有 -i/-ss，不该乱改。
    """
    try:
        i = cmd.index("-i")
    except ValueError:
        return None
    out = list(cmd)
    if extra:
        out[i:i] = list(extra)
        i += len(extra)
    if ss:
        try:
            j = out.index("-ss")
            out[j + 1] = ss
        except ValueError:
            out[i:i] = ["-ss", ss]
    return out


def install(app) -> None:
    db = getattr(app.state, "db", None)
    fetcher = getattr(app.state, "fetcher", None)
    if db is None or fetcher is None:
        return

    async def video_fetch(item_photo):
        pid = str(getattr(item_photo, "id", "") or "")
        row = db.get_item(pid)
        if not row:
            return None
        opts = VideoThumbOptions.from_env()
        if not opts.enabled:
            return None
        ext = (row["ext"] or "").lower()
        if ext:
            ok, why = opts.should_attempt(ext)
            if not ok:
                print("跳过视频抓帧（%s）：%s" % (ext, why))
                return None
        url, headers = await frame_source(dict(row))
        if not url:
            return None
        cmd = _lean_cmd(opts.ffmpeg or "ffmpeg", url, headers, opts, ext)
        budget = float(getattr(opts, "timeout", 60) or 60) + 15
        data = await asyncio.to_thread(_run, cmd, budget)
        if data:
            return data

        # ---- 回退 1：从 0 秒 + **放大探测预算** ----
        # 实测（2026-10-22）：一批 mp4 用默认的「-ss 1 + 512KB 探测」会报
        # `rc=234 Output file does not contain any stream`，
        # 换成 `-ss 0 -probesize 5M -analyzeduration 20M` 全部正常出图（10~15KB，0.5~0.7s）。
        # 这类文件多是 moov 在尾部 / 元数据在后面的老 mp4。
        ff = opts.ffmpeg or "ffmpeg"
        big_probe = ["-probesize", "5242880", "-analyzeduration", "20000000"]
        cmd2 = _retry_cmd(cmd, ss="0.00", extra=big_probe)
        if cmd2:
            data = await asyncio.to_thread(_run, cmd2, budget)
            if data:
                return data

        if ext in LOWPRI:
            return None                      # 老容器重试意义不大，交给重试策略

        # ---- 回退 2：再试试"忽略 editlist"（有些封装的时间轴不标准） ----
        cmd3 = _retry_cmd(cmd, ss="0.00", extra=big_probe + ["-ignore_editlist", "1"])
        if cmd3:
            data = await asyncio.to_thread(_run, cmd3, budget)
            if data:
                return data

        # ---- 回退 3：强制指定解复用器（探测把容器认错时救一把） ----
        demux = DEMUX.get(ext) or ("mpegts" if ext in ("ts", "m2ts") else None)
        if demux:
            cmd4 = _retry_cmd(cmd, ss="0.00", extra=["-f", demux] + big_probe)
            if cmd4:
                return await asyncio.to_thread(_run, cmd4, budget)
        return None

    fetcher.bind_video(video_fetch)
    app.state.video_gate = _gate
