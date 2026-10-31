"""视频缩略图：ffmpeg 直接读 URL 抓一帧（与 115-album 的 `_extract_frame_from_url` 同源）。

为什么是"让 ffmpeg 自己读 URL"，而不是"读一段字节喂管道"
--------------------------------------------------------
这是 115-album 里已经验证过的结论，直接沿用：

* MP4/MOV 的索引 ``moov atom`` 常常在文件**尾部**，管道（pipe）不可 seek，
  ffmpeg 就永远解不开；而给它 URL 时，它自己会发 HTTP Range 去跳读 moov，
  **无论 moov 在前在后都能抓到帧**；
* ffmpeg 只读它需要的部分（头 + moov + 目标关键帧），不会把整段视频拉下来；
* ``-ss`` 放在 ``-i`` 之前 = 输入侧快速定位，比逐帧解码到目标时间快几个数量级。

⚠️ 由此要修正一个说法：``VIDEO_THUMB_BYTES`` **不是流量硬上限**。
对 moov 在尾部的文件，总流量 ≈ 头部探测 + moov（通常几百 KB ~ 几 MB），
仍然远小于整片，但别把它理解成"绝对不超过 3 MB"。

格式兼容矩阵（ffmpeg gyan full build 实测/文档能力）
---------------------------------------------------
| 容器 | 抓帧 | 说明 |
|---|---|---|
| mp4 / mov / m4v | ✅ | 同一 demuxer；moov 在尾也靠 Range 跳读。**iPhone 的 MOV 多为此类** |
| mkv / webm | ✅ | Matroska 无 moov 概念，读头部 + 定位关键帧即可 |
| avi / wmv / flv | ✅ | 索引可在尾部，ffmpeg 会 seek |
| ts / m2ts / mpg | ⚠️ | 无全局索引，``-ss`` 可能需要向前扫描 -> 读得比别的格式多，默认低优先 |
| rmvb / rm | ⚠️ | RealMedia 老容器，需 header/index；gyan build 带 rv30/40 解码，多数可解 |
| m3u8 (HLS) | ❌ | 会按需拉多个分片，流量不可控 -> 默认直接跳过 |

大文件（几十 GB）说明
---------------------
抓帧成本**与文件大小基本无关**，只与 ``moov`` 位置和关键帧距离有关：
一个 40 GB 的 mp4 抓一帧通常仍是几 MB 级。真正的风险是
**CDN 单请求慢 -> 超时**，以及 **4K HEVC 解码吃 CPU**，所以：
``-threads`` 限制并发解码、超时给足、失败做退避（见 thumb_fetch 的 failed 重试）。

环境变量（与 115-album 同名者保持一致）
--------------------------------------
VIDEO_THUMB_ENABLED=1            总开关
VIDEO_THUMB_BYTES=3145728        探测读取量（不是硬流量上限）
VIDEO_THUMB_SEEK=1.0             取第几秒（避开片头黑屏）
VIDEO_THUMB_TIMEOUT=45           单次抓帧超时
VIDEO_THUMB_THREADS=2            交给 ffmpeg 的解码线程数（2C 机器别给大）
VIDEO_THUMB_SIZE=384             输出长边（与缩略图库一致）
VIDEO_THUMB_SKIP_EXTS=m3u8       永不尝试的扩展名（逗号分隔）
VIDEO_THUMB_LOWPRIORITY_EXTS=ts,m2ts,mpg,rm,rmvb
VIDEO_THUMB_ALLOW_LOWPRIORITY=0  1 才尝试低优先格式
VIDEO_THUMB_HEADERS=             额外请求头，形如 "Cookie: xxx;|X-Foo: bar"
FFMPEG_BIN=ffmpeg
UA=Mozilla/5.0 115Browser/27.0.5.7
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field

log = logging.getLogger("album.videothumb")

_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_EXTRA_SEP = "|"          # VIDEO_THUMB_HEADERS 里用 | 分隔多个头
_ffmpeg_missing_logged = False


def _csv(name: str, default: str) -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return {x.strip().lower().lstrip(".") for x in raw.split(",") if x.strip()}


@dataclass(slots=True)
class VideoThumbOptions:
    enabled: bool = True
    max_bytes: int = 3 * 1024 * 1024
    seek: float = 1.0
    timeout: int = 45
    threads: int = 2
    size: int = 384
    ffmpeg: str = "ffmpeg"
    ua: str = _DEFAULT_UA
    referer: str = "https://115.com/"
    extra_headers: dict[str, str] = field(default_factory=dict)
    skip_exts: set[str] = field(default_factory=lambda: {"m3u8"})
    lowpriority_exts: set[str] = field(
        default_factory=lambda: {"ts", "m2ts", "mpg", "rm", "rmvb"}
    )
    allow_lowpriority: bool = False

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_env(cls) -> "VideoThumbOptions":
        def _i(name: str, d: int) -> int:
            try:
                return int(os.getenv(name) or d)
            except ValueError:
                return d

        def _f(name: str, d: float) -> float:
            try:
                return float(os.getenv(name) or d)
            except ValueError:
                return d

        extra: dict[str, str] = {}
        raw_headers = (os.getenv("VIDEO_THUMB_HEADERS") or "").strip()
        if raw_headers:
            for part in raw_headers.split(_EXTRA_SEP):
                if ":" in part:
                    k, _, v = part.partition(":")
                    extra[k.strip()] = v.strip()

        return cls(
            enabled=(os.getenv("VIDEO_THUMB_ENABLED", "1") or "1").lower() in ("1", "true", "yes", "on"),
            max_bytes=_i("VIDEO_THUMB_BYTES", 3 * 1024 * 1024),
            seek=_f("VIDEO_THUMB_SEEK", 1.0),
            timeout=_i("VIDEO_THUMB_TIMEOUT", 45),
            threads=max(1, _i("VIDEO_THUMB_THREADS", 2)),
            size=_i("VIDEO_THUMB_SIZE", 384),
            ffmpeg=os.getenv("FFMPEG_BIN") or "ffmpeg",
            # 取流 UA 只认 CDN_UA：容器 env 里的 UA 是给 115 接口用的 115Browser，
            # 拿去 CDN 会被 403（见 source115.CDN_UA 注释）。
            ua=(os.getenv("CDN_UA") or _DEFAULT_UA),
            referer=os.getenv("VIDEO_REFERER") or "https://115.com/",
            extra_headers=extra,
            skip_exts=_csv("VIDEO_THUMB_SKIP_EXTS", "m3u8"),
            lowpriority_exts=_csv("VIDEO_THUMB_LOWPRIORITY_EXTS", "ts,m2ts,mpg,rm,rmvb"),
            allow_lowpriority=(os.getenv("VIDEO_THUMB_ALLOW_LOWPRIORITY", "0") or "0").lower()
            in ("1", "true", "yes", "on"),
        )

    # ------------------------------------------------------------------ 策略
    def should_attempt(self, ext: str) -> tuple[bool, str]:
        """按扩展名决定要不要抓帧，返回 (是否尝试, 原因)。"""
        if not self.enabled:
            return False, "VIDEO_THUMB_ENABLED=0"
        e = (ext or "").lower().lstrip(".")
        if e in self.skip_exts:
            return False, f"{e} 在 skip 列表（多为分片/无索引，流量不可控）"
        if e in self.lowpriority_exts and not self.allow_lowpriority:
            return False, f"{e} 属低优先（无全局索引，可能多读），需开 VIDEO_THUMB_ALLOW_LOWPRIORITY"
        return True, "ok"


def find_ffprobe(binary: str = "ffprobe") -> str | None:
    """找 ffprobe（编码探测用）。规则与 find_ffmpeg 一致。"""
    if not binary:
        return None
    if os.path.isfile(binary):
        return binary
    found = shutil.which(binary)
    if found:
        return found
    for cand in ("/usr/bin/ffprobe", "/usr/local/bin/ffprobe",
                 r"C:\ffmpeg\bin\ffprobe.exe", "/opt/homebrew/bin/ffprobe"):
        if os.path.isfile(cand):
            return cand
    return None


async def probe_media(
    url: str,
    opts: "VideoThumbOptions | None" = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict:
    """用 ffprobe 读编码 / 分辨率 / 时长，供"这条浏览器能不能直接播"预判。

    只读必要字节（``-probesize`` 限流），任何失败都返回 ``{}``，不抛异常。
    返回形如 ``{"vcodec": "hevc", "vtag": "hev1", "width": 2042, "height": 3630,
    "acodec": "aac", "duration": 14.33}``。
    """
    if not url:
        return {}
    opts = opts or VideoThumbOptions.from_env()
    fp = find_ffprobe(os.getenv("FFPROBE_BIN") or "ffprobe")
    if not fp:
        log.info("未找到 ffprobe，跳过编码探测")
        return {}

    ua = opts.ua
    for k, v in (headers or {}).items():
        if k.lower() == "user-agent":
            ua = v
    cmd = [
        # 注意：ffprobe 不认 -nostdin（那是 ffmpeg 的选项），带上它会让后面的
        # -user_agent 被当成它的参数，报 "Failed to set value '-user_agent'"。
        fp, "-hide_banner", "-loglevel", "error",
        "-user_agent", ua,
        "-headers", _headers_block(opts, headers),
        "-probesize", str(max(32 * 1024, opts.max_bytes)),
        "-analyzeduration", str(max(1, opts.max_bytes // 1024) * 1000),
        "-show_entries",
        "stream=codec_type,codec_name,codec_tag_string,profile,width,height"
        ":format=duration",
        "-of", "json",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("ffprobe 启动失败：%s", exc)
        return {}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=max(10, opts.timeout))
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        log.info("ffprobe 探测超时（%ss）", opts.timeout)
        return {}

    if proc.returncode != 0 or not out:
        detail = (err or b"")[:160].decode("utf-8", "replace").strip()
        log.info("ffprobe 未读出信息 rc=%s：%s", proc.returncode, detail)
        return {}
    try:
        js = json.loads(out.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}

    meta: dict = {}
    for s in js.get("streams", []) or []:
        t = s.get("codec_type")
        if t == "video" and not meta.get("vcodec"):
            meta["vcodec"] = (s.get("codec_name") or "").lower()
            meta["vtag"] = (s.get("codec_tag_string") or "").lower()
            meta["vprofile"] = s.get("profile") or ""
            meta["width"] = int(s.get("width") or 0)
            meta["height"] = int(s.get("height") or 0)
        elif t == "audio" and not meta.get("acodec"):
            meta["acodec"] = (s.get("codec_name") or "").lower()
    try:
        meta["duration"] = float((js.get("format") or {}).get("duration") or 0)
    except Exception:  # noqa: BLE001
        meta["duration"] = 0.0
    return meta


def find_ffmpeg(binary: str = "ffmpeg") -> str | None:
    if not binary:
        return None
    if os.path.isfile(binary):
        return binary
    found = shutil.which(binary)
    if found:
        return found
    # 只有"默认名"才去猜常见安装路径；指定了别的名字却找不到 -> 就是没有
    if os.path.basename(binary).lower() not in ("ffmpeg", "ffmpeg.exe"):
        return None
    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _headers_block(opts: VideoThumbOptions, headers: dict[str, str] | None) -> str:
    """把 UA/Referer/额外头拼成一个 ``-headers`` 参数（115-album 的做法）。"""
    merged: dict[str, str] = {"Referer": opts.referer}
    merged.update(opts.extra_headers)
    for k, v in (headers or {}).items():
        if k.lower() == "user-agent":
            continue
        merged[k] = v
    return "".join(f"{k}: {v}\r\n" for k, v in merged.items())


def build_cmd(
    ffmpeg: str,
    url: str,
    opts: VideoThumbOptions,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """构造 ffmpeg 命令（与 115-album 对齐，并加上线程/流控）。

    * ``-probesize`` / ``-analyzeduration``：限制探测读取量（不是硬流量上限）
    * ``-rw_timeout``：HTTP 读写超时（微秒）
    * ``-threads``：解码线程数，2C 机器别放任它开满
    * ``-ss`` 在 ``-i`` 前：输入侧定位，快且省流量
    * ``-vf scale=<size>:-2``：长边缩到库尺寸，高按比例且保持偶数（mjpeg 要求）
    * ``-q:v 4``：与 115-album 一致的 mjpeg 质量
    """
    probe_us = max(1, opts.max_bytes // 1024) * 1000
    ua = opts.ua
    for k, v in (headers or {}).items():
        if k.lower() == "user-agent":
            ua = v
    cmd = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error",
        "-nostdin",
        "-rw_timeout", str(max(5, opts.timeout) * 1_000_000),
        "-probesize", str(opts.max_bytes),
        "-analyzeduration", str(probe_us),
        "-threads", str(max(1, opts.threads)),
        "-user_agent", ua,
        "-headers", _headers_block(opts, headers),
        "-ss", f"{max(0.0, opts.seek):.2f}",
        "-i", url,
        "-frames:v", "1",
        "-vf", f"scale={max(16, opts.size)}:-2",
        "-f", "image2", "-vcodec", "mjpeg", "-q:v", "4",
        "pipe:1",
    ]
    return cmd


async def grab_frame(
    url: str,
    opts: VideoThumbOptions | None = None,
    *,
    headers: dict[str, str] | None = None,
    ext: str = "",
) -> bytes | None:
    """抓一帧；任何失败都返回 None（调用方负责标记/退避），不抛异常。

    ``ext`` 用于格式策略判断（m3u8/ts 等）。传空表示不检查。
    """
    global _ffmpeg_missing_logged

    opts = opts or VideoThumbOptions.from_env()
    if not url:
        return None
    if ext:
        ok, why = opts.should_attempt(ext)
        if not ok:
            log.info("跳过视频抓帧（%s）：%s", ext, why)
            return None
    elif not opts.enabled:
        return None

    ffmpeg = find_ffmpeg(opts.ffmpeg)
    if not ffmpeg:
        if not _ffmpeg_missing_logged:
            log.warning("未找到 ffmpeg（FFMPEG_BIN=%s），视频缩略图将全部跳过", opts.ffmpeg)
            _ffmpeg_missing_logged = True
        return None

    cmd = build_cmd(ffmpeg, url, opts, headers)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("ffmpeg 启动失败：%s", exc)
        return None

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=max(5, opts.timeout))
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        log.info("ffmpeg 抓帧超时（%ss）", opts.timeout)
        return None

    if proc.returncode != 0 or not out:
        detail = (err or b"")[:200].decode("utf-8", "replace").strip()
        # 常见：moov atom not found / Invalid data / 超时 —— 都别把命令行打进日志
        log.info("ffmpeg 未解出帧 rc=%s：%s", proc.returncode, detail)
        return None
    if out[:3] != b"\xff\xd8\xff":
        log.info("ffmpeg 输出不是 JPEG（%d 字节）", len(out))
        return None
    return out
