"""收尾：删掉同族漏网的 3 条 + 把 59 个广告名改成"按名拦截（不限体积）"。"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app")
from app import hide_rules                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"
e = sqlite3.connect(os.path.join(DATA, "emby.db"), timeout=30)
e.row_factory = sqlite3.Row
t = sqlite3.connect(os.path.join(DATA, "thumbs.db"), timeout=30)

# 1) 名字在广告名单里、体积不同（漏网）+ 纯域名插空格
names = {l.strip() for l in open("/tmp/ad_names.txt", encoding="utf-8") if l.strip()}
dead, pairs, nm_extra = [], [], []
for r in e.execute("SELECT id, kind, ext, size, name_enc FROM items").fetchall():
    nm = ic.safe_unseal(r["name_enc"] or "") or ""
    if nm in names or hide_rules.split_domain_evasion(nm):
        dead.append(r["id"])
        pairs.append((nm, int(r["size"] or 0)))
        if nm in names:
            nm_extra.append(nm)
        print("  删:", r["ext"], "%.3fMB" % ((r["size"] or 0) / 1048576), nm[:50])
print("本轮新增删除:", len(dead), "条")

# 2) 名字级拦截：清单里的所有广告名（不限体积）
n = hide_rules.add_block_pairs(pairs, names=sorted(names))
print("黑名单已更新，共", n, "条规则")
bl = json.load(open(hide_rules.block_path(), encoding="utf-8"))
print("  pairs=%d names=%d" % (len(bl["pairs"]), len(bl["names"])))

CH = 300
with e:
    for i in range(0, len(dead), CH):
        ch = dead[i:i + CH]
        q = ",".join("?" * len(ch))
        e.execute("DELETE FROM items WHERE id IN (%s)" % q, ch)
with t:
    for i in range(0, len(dead), CH):
        ch = dead[i:i + CH]
        q = ",".join("?" * len(ch))
        t.execute("DELETE FROM thumbs WHERE pid IN (%s)" % q, ch)
        t.execute("DELETE FROM thumb_src WHERE pid IN (%s)" % q, ch)
t.commit()
print("items 剩余:", e.execute("SELECT COUNT(*) FROM items").fetchone()[0])
print("thumbs 剩余:", t.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0])
e.close()
t.close()
