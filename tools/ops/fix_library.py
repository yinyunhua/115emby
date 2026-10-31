"""清掉相册导入的残留 + 给「我的资源」补上根目录行。

跑法（容器内）：
    python3 /app/tools/fix_library.py --root-cid ROOT_CID_PLACEHOLDER --root-name 我的资源
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, "/app")

from app import index_crypto as ic     # noqa: E402

EMBY = os.getenv("EMBY_DB", "/app/data/emby.db")
THUMB = os.getenv("THUMB_DB", "/app/data/thumbs.db")
OLD_ROOT = "3126530746357906982"        # 之前从相册索引导入的那棵树的 cid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root-cid", default="ROOT_CID_PLACEHOLDER")
    ap.add_argument("--root-name", default="我的资源")
    ap.add_argument("--drop-old", default=OLD_ROOT)
    args = ap.parse_args()

    e = sqlite3.connect(EMBY, timeout=30)
    t = sqlite3.connect(THUMB, timeout=30)

    before_items = e.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    before_folders = e.execute("SELECT COUNT(*) FROM folders").fetchone()[0]

    # 1) 找出老根目录子树里的所有 pid（含孙目录）
    ids = [r[0] for r in e.execute(
        "SELECT id FROM items WHERE parent_id=?", (args.drop_old,))]
    sub = [r[0] for r in e.execute(
        "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT f.id FROM folders f JOIN d ON f.parent_id=d.id) "
        "SELECT id FROM d", (args.drop_old,))]
    if sub:
        q = ",".join("?" * len(sub))
        ids += [r[0] for r in e.execute(
            "SELECT id FROM items WHERE parent_id IN (%s)" % q, sub)]
    ids = sorted(set(ids))

    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        e.execute("DELETE FROM items WHERE id IN (%s)" % q, chunk)
        e.execute("DELETE FROM links WHERE item_id IN (%s)" % q, chunk)
        t.execute("DELETE FROM thumb_src WHERE pid IN (%s)" % q, chunk)
        t.execute("DELETE FROM thumbs WHERE pid IN (%s)" % q, chunk)

    if sub:
        q = ",".join("?" * len(sub))
        e.execute("DELETE FROM folders WHERE id IN (%s)" % q, sub)

    e.commit()
    t.commit()

    # 2) 补根目录行（扫描器不写根，导致主页只看得到别的树）
    child_folders = e.execute("SELECT COUNT(*) FROM folders WHERE parent_id=?",
                              (args.root_cid,)).fetchone()[0]
    child_items = e.execute("SELECT COUNT(*) FROM items WHERE parent_id=?",
                            (args.root_cid,)).fetchone()[0]
    e.execute(
        "INSERT INTO folders (id,parent_id,name_enc,name_idx,name_bucket,depth,child_count,"
        "scanned_at,updated,created) VALUES (?,?,?,?,?,?,?,strftime('%s','now'),"
        "strftime('%s','now'),strftime('%s','now')) "
        "ON CONFLICT(id) DO UPDATE SET name_enc=excluded.name_enc, name_idx=excluded.name_idx, "
        "name_bucket=excluded.name_bucket, child_count=excluded.child_count",
        (args.root_cid, "-1", ic.seal(args.root_name), ic.blind(args.root_name),
         ic.bucket(args.root_name), 0, child_folders + child_items))
    e.commit()

    after_items = e.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    after_folders = e.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    pending = t.execute("SELECT COUNT(*) FROM thumb_src WHERE state='pending'").fetchone()[0]

    # 3) 列一下主页会看到什么
    print("删除条目 %d（相册导入残留）" % (before_items - after_items))
    print("items: %d -> %d | folders: %d -> %d" % (before_items, after_items,
                                                   before_folders, after_folders))
    print("缩略图队列 pending: %d" % pending)
    print("根目录 %s (%s)：子文件夹 %d / 直接条目 %d" % (
        args.root_cid, args.root_name, child_folders, child_items))
    print("--- 主页将显示 ---")
    for r in e.execute("SELECT id, child_count FROM folders WHERE parent_id=? ORDER BY id LIMIT 12",
                       (args.root_cid,)):
        try:
            name = ic.safe_unseal(e.execute("SELECT name_enc FROM folders WHERE id=?", (r[0],)).fetchone()[0])
        except Exception:
            name = "?"
        print("   [目录] %s  (cid %s, %s 项)" % (name, r[0], r[1]))
    for r in e.execute("SELECT id FROM items WHERE parent_id=? LIMIT 3", (args.root_cid,)):
        print("   [文件] %s" % r[0])
    e.close(); t.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
