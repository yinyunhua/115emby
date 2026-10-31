"""命令行入口：快速增量扫描（逻辑在 app/quick_scan.py，后台按钮走同一份）。

    python tools/quick_scan.py <root_cid> [--max-requests N] [--dry-run] [--verbose]
"""
import argparse
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("/app", os.path.dirname(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.quick_scan import run_quick          # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--max-requests", type=int, default=200000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--db", default=os.getenv("EMBY_DB") or "/app/data/emby.db")
    a = ap.parse_args()

    def show(p):
        if a.verbose:
            sys.stdout.write("  请求 %d / 目录 %d / 跳过 %d / 新增 %d\r"
                             % (p["requests"], p["dirs"], p["skipped"], p["new_items"]))
            sys.stdout.flush()

    res = await run_quick(a.root, db_path=a.db, max_requests=a.max_requests,
                          on_progress=show, dry_run=a.dry_run)
    print("\n=== 快速增量结果 ===")
    print("列目录请求: %d 次（上限 %d）" % (res["requests"], a.max_requests))
    print("访问目录: %d | 跳过整棵未变化子树: %d 个" % (res["dirs"], res["skipped"]))
    print("写入: 文件夹 %d | 文件 %d | 新增项 %d" % (res["folders"], res["items"], res["new_items"]))
    print("耗时: %.1f 秒 | 状态: %s | 库内 items=%d"
          % (res["elapsed"], res["stopped"], res.get("items_total", 0)))


if __name__ == "__main__":
    asyncio.run(main())
