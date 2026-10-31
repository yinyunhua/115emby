"""解析 2 个"从未入过库"的目录究竟是什么（只读列目录）。"""
import asyncio
import sys

sys.path.insert(0, "/app")
from app import source115                               # noqa: E402

DIRS = ["644168263198216032", "2077506546161352758"]


async def main():
    for cid in DIRS:
        try:
            js = await source115.list_dir(cid)
        except Exception as exc:                        # noqa: BLE001
            print(cid, "列目录失败:", exc)
            continue
        crumbs = js.get("crumbs") or []
        path = " / ".join([c.get("name", "?") for c in crumbs]) or "(无面包屑)"
        files = js.get("files") or []
        folders = js.get("folders") or []
        print("目录 %s" % cid)
        print("   路径: %s" % path[:120])
        print("   子目录 %d 个，文件 %d 个" % (len(folders), len(files)))
        for f in files[:5]:
            print("      %-6s %8.2fMB  %s" % (f.get("ext"), (f.get("size") or 0) / 1048576,
                                              (f.get("name") or "")[:48]))


asyncio.run(main())
