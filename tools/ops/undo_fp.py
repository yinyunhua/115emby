"""撤销误伤：把 [BT-btt.com]* / IMG_2580 .PNG.jpg 从黑名单移除，并重新扫描它们所在的目录。

这些是被我那条过宽的"域名规则"误删的内容片（0.4~1.2GB），必须恢复。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app")
from app import hide_rules                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"
BAD_PREFIX = ("[bt-btt.com]",)
BAD_EXACT = ("img_2580 .png.jpg",)


def is_fp(name: str) -> bool:
    n = (name or "").lower()
    return n.startswith(BAD_PREFIX) or n in BAD_EXACT


def main():
    p = hide_rules.block_path()
    bl = json.load(open(p, encoding="utf-8"))
    pairs = [x for x in bl.get("pairs", []) if not is_fp(x[0])]
    names = [x for x in bl.get("names", []) if not is_fp(x)]
    removed = (len(bl.get("pairs", [])) - len(pairs)) + (len(bl.get("names", [])) - len(names))
    json.dump({"pairs": pairs, "names": names}, open(p, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("黑名单撤销 %d 条 → 现在 pairs=%d names=%d" % (removed, len(pairs), len(names)))

    # 找它们所在的目录：库里同名前缀的兄弟文件还在，用兄弟的 parent_id 定位
    d = sqlite3.connect(os.path.join(DATA, "emby.db"), timeout=30)
    d.row_factory = sqlite3.Row
    dirs = set()
    for r in d.execute("SELECT parent_id, name_enc FROM items"):
        nm = ic.safe_unseal(r["name_enc"] or "") or ""
        if nm.lower().startswith("[bt-btt.com]") or "img_2580" in nm.lower():
            dirs.add(r["parent_id"])
    print("需要重新扫描的目录:", len(dirs))
    for x in sorted(dirs):
        print("   ", x)
    open("/tmp/rescan_dirs.json", "w").write(json.dumps(sorted(dirs)))
    d.close()


main()
