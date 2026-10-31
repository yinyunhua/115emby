"""用**相册索引**灌 115emby 媒体库 —— 不再单独扫 115，跟相册永远同源。

相册索引（/opt/115-album/data/index.db）是扁平的：
``root`` 列 = 相册根 cid，``folder`` / ``path`` 为空。
所以这里建 1 个根文件夹 + N 个 items，名字与缩略图地址全部加密落库
（明文只在**相册那边**，115emby 这边库里没有明文）。

跑法（容器内）：

    python3 /app/tools/import_album_index.py \
        --index /proddata/index.db --db /app/data/emby.db [--prune]

* ``--prune``：删掉相册里已经不存在的条目（含其直链缓存）；
* 幂等：反复跑只会 upsert，不动 progress / links 里已有的播放记录。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from app import index_crypto as ic            # noqa: E402
from app.db import DB, KIND_IMAGE, KIND_VIDEO, KIND_OTHER   # noqa: E402

VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "rmvb", "webm", "flv", "ts", "m2ts", "wmv", "mpg"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif", "avif", "tif", "tiff"}


def kind_of(row: dict) -> str:
    ext = (row.get("ext") or "").lower()
    if ext in VIDEO_EXTS:
        return KIND_VIDEO
    if ext in IMAGE_EXTS:
        return KIND_IMAGE
    t = int(row.get("type") or 0)
    if t == 2:
        return KIND_VIDEO
    if t == 5:
        return KIND_IMAGE
    return KIND_OTHER


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="/proddata/index.db")
    ap.add_argument("--db", default="/app/data/emby.db")
    ap.add_argument("--root-name", default=os.getenv("ALBUM_TITLE") or "相册")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--batch", type=int, default=1000)
    args = ap.parse_args()

    if not Path(args.index).exists():
        print("!! 找不到相册索引：%s" % args.index)
        return 2

    con = sqlite3.connect(args.index, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, root, name, size, type, ext, ctime, thumb FROM photos").fetchall()
    con.close()
    if not rows:
        print("!! 相册索引是空的")
        return 3

    roots = {str(r["root"] or "-1") for r in rows}
    root_id = sorted(roots)[0] if len(roots) == 1 else "-1"

    db = DB(Path(args.db))
    db.init()
    t0 = time.time()

    name_enc, name_idx, name_bucket = ic.seal(args.root_name), ic.blind(args.root_name), ic.bucket(args.root_name)
    db.upsert_folder(root_id, "-1", name_enc, name_idx, name_bucket,
                     depth=0, child_count=len(rows))

    buf, done, n_img, n_vid, n_other, n_thumb = [], 0, 0, 0, 0, 0
    ids = set()
    for r in rows:
        d = dict(r)
        pid = str(d["id"])
        ids.add(pid)
        name = str(d.get("name") or pid)
        kind = kind_of(d)
        if kind == KIND_IMAGE:
            n_img += 1
        elif kind == KIND_VIDEO:
            n_vid += 1
        else:
            n_other += 1
        thumb = str(d.get("thumb") or "")
        if thumb:
            n_thumb += 1
        buf.append({
            "id": pid,
            "parent_id": root_id,
            "name_enc": ic.seal(name),
            "name_idx": ic.blind(name),
            "name_bucket": ic.bucket(name),
            "path_enc": "",
            "kind": kind,
            "ext": (d.get("ext") or "").lower(),
            "size": int(d.get("size") or 0),
            "mtime": int(d.get("ctime") or 0),
            "thumb_enc": ic.seal_link(thumb) if thumb else "",
        })
        if len(buf) >= args.batch:
            done += db.upsert_items(buf)
            buf = []
    if buf:
        done += db.upsert_items(buf)

    pruned = 0
    if args.prune:
        con2 = sqlite3.connect(args.db, timeout=30)
        old = {r[0] for r in con2.execute("SELECT id FROM items")}
        stale = old - ids
        for pid in stale:
            con2.execute("DELETE FROM items WHERE id=?", (pid,))
            con2.execute("DELETE FROM links WHERE item_id=?", (pid,))
        con2.commit()
        con2.close()
        pruned = len(stale)

    st = db.stats()
    print("导入完成：写入 %d 条，用时 %.1fs" % (done, time.time() - t0))
    print("  根文件夹 cid = %s（%s）" % (root_id, args.root_name))
    print("  图片 %d / 视频 %d / 其他 %d，带上游缩略图地址 %d" % (n_img, n_vid, n_other, n_thumb))
    print("  清理陈旧条目 %d" % pruned)
    print("  库现状：%s" % st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
