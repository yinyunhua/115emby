"""按清单清库（items + 缩略图 + 队列），并把"名字+体积"写进持久黑名单。

清单来源：/tmp/purge_ids.json（id 数组，来自 广告清单.csv + 广告清单_疑似.csv）。
只删本地索引，**不碰 115 上的文件**。加 --dry 只看不删。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app")
from app import hide_rules                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"
EMBY = os.path.join(DATA, "emby.db")
THUMB = os.path.join(DATA, "thumbs.db")
LIST = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "/tmp/purge_ids.json"
DRY = "--dry" in sys.argv
CH = 300


def main():
    ids = json.load(open(LIST, encoding="utf-8"))
    print("清单:", LIST, "->", len(ids), "条")
    e = sqlite3.connect(EMBY, timeout=30)
    e.row_factory = sqlite3.Row
    found, pairs, missing = [], [], 0
    for i in range(0, len(ids), CH):
        chunk = ids[i:i + CH]
        q = ",".join("?" * len(chunk))
        for r in e.execute("SELECT id, kind, ext, size, name_enc FROM items WHERE id IN (%s)" % q, chunk):
            nm = ic.safe_unseal(r["name_enc"] or "") or ""
            found.append((r["id"], nm, int(r["size"] or 0), r["kind"] or "", (r["ext"] or "").lower()))
            pairs.append((nm, int(r["size"] or 0)))
    missing = len(ids) - len(found)
    print("库里命中:", len(found), "条；清单里已不存在:", missing, "条")
    by_kind = {}
    by_ext = {}
    total_b = 0
    for _, _, sz, k, x in found:
        by_kind[k] = by_kind.get(k, 0) + 1
        by_ext[x] = by_ext.get(x, 0) + 1
        total_b += sz
    print("类型:", by_kind, "| 扩展名:", dict(sorted(by_ext.items(), key=lambda kv: -kv[1])))
    print("合计体积: %.2f GB" % (total_b / 1073741824))
    if DRY:
        print("--dry：不删除")
        return
    if not found:
        return

    n = hide_rules.add_block_pairs(pairs)
    print("黑名单已写入 %s（当前 %d 条）" % (hide_rules.block_path(), n))

    dead = [x[0] for x in found]
    with e:
        for i in range(0, len(dead), CH):
            chunk = dead[i:i + CH]
            q = ",".join("?" * len(chunk))
            e.execute("DELETE FROM items WHERE id IN (%s)" % q, chunk)
    print("items 已删除:", len(dead))
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
        t.close()
        print("缩略图删除:", n1, "| 队列记录删除:", n2)
    except Exception as exc:                                # noqa: BLE001
        print("缩略图清理跳过:", exc)
    print("items 剩余:", e.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    e.close()


main()
