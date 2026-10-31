"""修复「上游占位图」的 21 条：重列父目录拿新签名 thumb URL，再把队列重置为 pending。

思路
----
115 的缩略图 URL（``https://thumb.115.com/thumb/...``）带签名时效，过期后 CDN 会返回
"图片已过期"占位图。``downurl`` 响应里没有缩略图字段，所以只能**重新列一次父目录**
（``/files?cid=...``），从文件列表里取新的 ``thumb`` 字段写回 ``items.thumb_enc``。

顺带清理：队列里 pid 已不在 ``items`` 的孤儿记录（早期相册树残留）。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time

sys.path.insert(0, "/app")

from app import index_crypto as ic        # noqa: E402
from app import source115                 # noqa: E402

EMBY = os.getenv("EMBY_DB", "/app/data/emby.db")
THUMB = os.getenv("THUMB_DB", "/app/data/thumbs.db")
INTERVAL = float(os.getenv("REFRESH_INTERVAL") or 1.0)


class Gate:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last = 0.0

    async def __aenter__(self):
        if self.interval > 0:
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        return False


async def main() -> int:
    e = sqlite3.connect(EMBY, timeout=30)
    e.row_factory = sqlite3.Row
    t = sqlite3.connect(THUMB, timeout=30)

    # 1) 清理孤儿队列行
    orphans = [r[0] for r in t.execute("SELECT pid FROM thumb_src")
               if e.execute("SELECT 1 FROM items WHERE id=?", (r[0],)).fetchone() is None]
    for pid in orphans:
        t.execute("DELETE FROM thumb_src WHERE pid=?", (pid,))
        t.execute("DELETE FROM thumbs WHERE pid=?", (pid,))
    t.commit()
    print("清理孤儿队列记录: %d" % len(orphans))

    # 2) 找出占位图的 pid
    rows = list(t.execute("SELECT pid FROM thumb_src WHERE state='skipped' AND last_err='上游占位图'"))
    pids = [r[0] for r in rows]
    print("待修复占位图: %d" % len(pids))
    if not pids:
        e.close(); t.close()
        return 0

    parents = {}
    for pid in pids:
        r = e.execute("SELECT parent_id FROM items WHERE id=?", (pid,)).fetchone()
        if r:
            parents.setdefault(r[0], []).append(pid)
    print("涉及父目录: %d 个" % len(parents))

    gate = Gate(INTERVAL)
    updated, missing, no_thumb = 0, 0, 0
    for cid, group in parents.items():
        try:
            async with gate:
                listing = await source115.list_dir(cid)
        except Exception as exc:
            print("  列目录失败 %s：%s" % (cid, exc))
            continue
        fresh = {str(f.get("id")): str(f.get("thumb") or "")
                 for f in (listing.get("files") or [])}
        for pid in group:
            url = fresh.get(pid, "")
            if not url:
                if pid in fresh:
                    no_thumb += 1
                else:
                    missing += 1
                continue
            e.execute("UPDATE items SET thumb_enc=? WHERE id=?", (ic.seal_link(url), pid))
            t.execute("UPDATE thumb_src SET state='pending', attempts=0, last_err='' WHERE pid=?", (pid,))
            updated += 1
        e.commit(); t.commit()
        print("  %s：本组 %d 条，取到新地址 %d" % (cid, len(group), updated))

    print("结果：更新 %d / 上游无缩略图 %d / 列表里找不到 %d" % (updated, no_thumb, missing))
    e.close(); t.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
