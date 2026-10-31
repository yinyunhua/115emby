"""一次性清理：把库里"非图片/视频"的条目和黑名单小视频删掉（连带缩略图）。

**只删本地索引，不碰 115 上的文件。**
在容器里跑：
    docker cp tools/purge_blocked.py 115emby-api:/tmp/ && docker exec 115emby-api python /tmp/purge_blocked.py
加 --dry 只看不删。
"""
import os
import sqlite3
import sys

sys.path.insert(0, "/app")
from app import hide_rules                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"
EMBY = os.path.join(DATA, "emby.db")
THUMB = os.path.join(DATA, "thumbs.db")
DRY = "--dry" in sys.argv


def main():
    e = sqlite3.connect(EMBY, timeout=30)
    e.row_factory = sqlite3.Row
    print("规则:", hide_rules.describe())

    # ① 非图片/视频
    other = e.execute("SELECT id, kind, ext, size, name_enc FROM items "
                      "WHERE kind NOT IN ('image','video')").fetchall()
    # ② 黑名单小视频（名字要解密后判断）
    vids = e.execute("SELECT id, kind, ext, size, name_enc FROM items "
                     "WHERE kind IN ('image','video') AND COALESCE(size,0) < ?",
                     (hide_rules.MAX_VIDEO_BYTES,)).fetchall()
    hits = []
    for r in vids:
        nm = ic.safe_unseal(r["name_enc"] or "")
        if hide_rules.is_blocked(nm, r["kind"], r["size"] or 0):
            hits.append((r["id"], nm, r["size"] or 0, hide_rules.matched(nm)))

    print("非图片/视频条目:", len(other), "条",
          ("（示例: %s）" % ", ".join(sorted({(x['kind'] or '?') + '/' + (x['ext'] or '') for x in other})[:8]))
          if other else "")
    print("命中黑名单的条目:", len(hits), "条")
    for i, nm, sz, kw in hits[:10]:
        print("   [%s] %.1fMB  %s" % (kw, sz / 1048576, nm[:60]))
    if len(hits) > 10:
        print("   ...（共 %d 条）" % len(hits))

    dead = [r["id"] for r in other] + [h[0] for h in hits]
    if DRY:
        print("--dry：不删除。将删除", len(dead), "条")
        return
    if not dead:
        print("没有要删的")
        return

    CH = 400
    with e:
        for i in range(0, len(dead), CH):
            chunk = dead[i:i + CH]
            q = ",".join("?" * len(chunk))
            e.execute("DELETE FROM items WHERE id IN (%s)" % q, chunk)
    print("已从 items 删除", len(dead), "条")

    # 缩略图跟着删（释放空间）
    try:
        t = sqlite3.connect(THUMB, timeout=30)
        n1 = n2 = 0
        with t:
            for i in range(0, len(dead), CH):
                chunk = dead[i:i + CH]
                q = ",".join("?" * len(chunk))
                n1 += t.execute("DELETE FROM thumbs WHERE pid IN (%s)" % q, chunk).rowcount
                n2 += t.execute("DELETE FROM thumb_src WHERE pid IN (%s)" % q, chunk).rowcount
        t.commit()
        print("缩略图删除:", n1, "张；队列记录删除:", n2, "条")
        t.close()
    except Exception as exc:                                # noqa: BLE001
        print("缩略图清理跳过：", exc)
    left = e.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print("items 剩余:", left)
    e.close()


main()
