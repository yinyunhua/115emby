"""应用"推广词"规则：先列命中的条目，再删除并写入黑名单（含安全上限）。"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app")
from app import hide_rules                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"
LIMIT = 300                                    # 超过这个数只报告不删，避免规则过宽造成大面积误删
e = sqlite3.connect(os.path.join(DATA, "emby.db"), timeout=30)
e.row_factory = sqlite3.Row
t = sqlite3.connect(os.path.join(DATA, "thumbs.db"), timeout=30)

hits = []
for r in e.execute("SELECT id, kind, ext, size, name_enc FROM items"):
    nm = ic.safe_unseal(r["name_enc"] or "") or ""
    if hide_rules.is_blocked(nm, r["kind"], r["size"] or 0):
        hits.append((r["id"], nm, int(r["size"] or 0), r["kind"] or "", (r["ext"] or "").lower()))

print("命中规则:", len(hits), "条")
for _, nm, sz, k, x in sorted(hits, key=lambda z: -z[2])[:15]:
    print("   %-6s %9.3fMB  %s" % (k, sz / 1048576, nm[:56]))
if len(hits) > 15:
    print("   ...（共 %d 条）" % len(hits))
json.dump([h[0] for h in hits], open("/tmp/adwords_ids.json", "w"))

if not hits:
    print("没有要删的")
elif len(hits) > LIMIT:
    print("超过安全上限 %d，只报告不删 —— 需要人工确认规则是否过宽" % LIMIT)
else:
    n = hide_rules.add_block_pairs([(h[1], h[2]) for h in hits])
    print("黑名单已更新:", n, "条规则")
    ids = [h[0] for h in hits]
    CH = 300
    with e:
        for i in range(0, len(ids), CH):
            ch = ids[i:i + CH]
            q = ",".join("?" * len(ch))
            e.execute("DELETE FROM items WHERE id IN (%s)" % q, ch)
    with t:
        for i in range(0, len(ids), CH):
            ch = ids[i:i + CH]
            q = ",".join("?" * len(ch))
            t.execute("DELETE FROM thumbs WHERE pid IN (%s)" % q, ch)
            t.execute("DELETE FROM thumb_src WHERE pid IN (%s)" % q, ch)
    t.commit()
    print("已删除 items:", len(ids))
    print("items 剩余:", e.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    print("thumbs 剩余:", t.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0])
e.close()
t.close()
