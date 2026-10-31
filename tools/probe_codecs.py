"""给库里还没探测过编码的视频批量打标（vcodec / width / height / duration）。

为什么需要
----------
115 里的 mp4 不都是 H.264（实测抽样 24 个里有 HEVC 和 MPEG-4 Part 2），这些在
Chrome 里表现为"元数据有、videoWidth=0×0"，得靠编码预判才能在列表页就标出来。
按需探测只在打开播放页时跑一次；这个脚本是用来把存量库慢慢补全的。

用法（在服务器上，容器里也能跑）：

    python3 tools/probe_codecs.py --limit 300 --interval 1.2
    python3 tools/probe_codecs.py --limit 0            # 0 = 不限量，一直跑到做完
    python3 tools/probe_codecs.py --only-no-thumb      # 只补没有缩略图的（更省）

特性
----
* 单并发 + 间隔节流（和抓缩略图共用同一路 115 额度，别开并发）；
* 探测结果落库，中断可续（下次自动跳过已探测的）；
* 只读必要字节（ffprobe 的 -probesize 限流）；
* 全部通过 CDN_UA 签发/取流 —— 115Browser 这类 UA 会被 CDN 403。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import source115                                  # noqa: E402
from app.codecs import classify                            # noqa: E402
from app.db import DB                                      # noqa: E402
from app.video_thumb import VideoThumbOptions, probe_media  # noqa: E402

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"


async def run(limit: int, interval: float, dry: bool) -> None:
    db = DB(EMBY)
    opts = VideoThumbOptions.from_env()
    done = ok = bad = fail = 0
    t0 = time.time()
    while True:
        rows = db.items_missing_meta(limit=min(50, limit - done) if limit else 50)
        if not rows:
            break
        for r in rows:
            if limit and done >= limit:
                break
            done += 1
            item = {"id": r["id"]}
            try:
                url, _exp, ua = await source115.direct_link(item, ua=source115.CDN_UA)
            except Exception as exc:                        # noqa: BLE001
                fail += 1
                print("  取链失败 %s：%s" % (r["id"][:26], str(exc)[:80]), flush=True)
                await asyncio.sleep(interval)
                continue
            meta = await probe_media(url, opts, headers={"User-Agent": ua})
            if not meta.get("vcodec"):
                fail += 1
                print("  探测失败 %s（ext=%s）" % (r["id"][:26], r["ext"]), flush=True)
                await asyncio.sleep(interval)
                continue
            info = classify(meta["vcodec"])
            tag = "OK " if info["playable"] else "!! "
            if info["playable"]:
                ok += 1
            else:
                bad += 1
            print("  %s%s  %s  %sx%s  %s" % (tag, meta["vcodec"], r["id"][:26],
                                            meta.get("width"), meta.get("height"),
                                            info["label"] or ""), flush=True)
            if not dry:
                db.set_media_meta(r["id"], meta)
            await asyncio.sleep(interval)
        if limit and done >= limit:
            break
    dt = time.time() - t0
    print("\n完成：处理 %d 条，可直接播 %d，需外部播放器 %d，失败 %d，用时 %.1fs"
          % (done, ok, bad, fail, dt))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="处理条数，0=不限")
    ap.add_argument("--interval", type=float, default=1.2, help="每条之间的间隔秒")
    ap.add_argument("--dry", action="store_true", help="只探测不写库")
    a = ap.parse_args()
    if not source115.ready():
        print("没有 115 凭据（COOKIE_115 / ACCESS_TOKEN_115），先配置再跑", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(run(a.limit, a.interval, a.dry))


if __name__ == "__main__":
    main()
