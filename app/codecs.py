"""编码能力表：把 ffprobe 的 ``codec_name`` 翻成「浏览器能不能直接播 + 为什么」。

背景（2026-10-21 实测）：115 里的 MP4 并不都是 H.264。随机抽 24 个 mp4：
h264/avc1 22 个、mpeg4/mp4v（MPEG-4 Part 2）1 个、hevc/hvc1 1 个。
后两类在 Chrome 里表现为「元数据拿到了（readyState=1、duration 有值）但
videoWidth=0×0」——不是网络问题，也不是 Content-Type/Content-Disposition 的问题
（成功取流本来就带 ``application/octet-stream`` + ``attachment``，浏览器照播）。

这里只做纯函数映射，不碰网络，方便单测。
"""
from __future__ import annotations

# 浏览器 <video> 能直接解的（Chrome 与 Safari 的共同子集）
WEB_OK = {
    "h264", "avc1", "avc3",
    "vp8", "vp9", "vp09",
    "av1", "av01",
    "theora",
}

# 已知解不了 / 只有部分平台能解：codec_name -> (显示名, 说明)
UNSUPPORTED = {
    "hevc": ("HEVC / H.265", "Chrome、Android 解不了；iPhone / iPad / Mac 的 Safari 可以直接播"),
    "h265": ("HEVC / H.265", "Chrome、Android 解不了；iPhone / iPad / Mac 的 Safari 可以直接播"),
    "mpeg4": ("MPEG-4 Part 2", "浏览器都不支持（那不是 H.264），只能交给本地播放器"),
    "msmpeg4v3": ("MS MPEG-4 v3", "浏览器都不支持，只能交给本地播放器"),
    "msmpeg4v2": ("MS MPEG-4 v2", "浏览器都不支持，只能交给本地播放器"),
    "wmv1": ("WMV7", "浏览器都不支持，只能交给本地播放器"),
    "wmv2": ("WMV8", "浏览器都不支持，只能交给本地播放器"),
    "wmv3": ("WMV9 / VC-1", "浏览器都不支持，只能交给本地播放器"),
    "vc1": ("VC-1", "浏览器都不支持，只能交给本地播放器"),
    "rv30": ("RealVideo 8", "浏览器都不支持，只能交给本地播放器"),
    "rv40": ("RealVideo 9/10", "浏览器都不支持，只能交给本地播放器"),
    "flv1": ("Sorenson H.263", "浏览器都不支持，只能交给本地播放器"),
    "mpeg1video": ("MPEG-1", "浏览器都不支持，只能交给本地播放器"),
    "mpeg2video": ("MPEG-2", "浏览器都不支持，只能交给本地播放器"),
}


def classify(vcodec: str) -> dict:
    """返回 ``{"playable": bool|None, "label": str, "note": str}``。

    ``playable`` 为 ``None`` 表示"还不知道"（没探测过编码，或编码名不认识），
    此时界面应该退回按扩展名判断，不要乱下结论。
    """
    v = (vcodec or "").strip().lower()
    if not v:
        return {"playable": None, "label": "", "note": ""}
    if v in WEB_OK:
        return {"playable": True, "label": "", "note": ""}
    if v in UNSUPPORTED:
        label, note = UNSUPPORTED[v]
        return {"playable": False, "label": label, "note": note}
    # 没见过的编码：不冒充"能播"，也不吓唬人
    return {"playable": None, "label": v.upper(), "note": ""}
