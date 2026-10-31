"""补回那 25 条"被过宽规则误删的正常内容"（**只 upsert，绝不删除**）。

做法：名字从备份库 emby.db.bak.thfix-081955 读取（含解密），
然后用 115 搜索按名字定位父目录，再 list_dir + upsert。
自动跳过：现在已不在缺列表里的、判为广告的、以及文件名指向未成年人的条目。
"""
import asyncio
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, "/app")
import httpx                                              # noqa: E402
from app import hide_rules                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402
from app import source115                               # noqa: E402
from app.db import DB, KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER   # noqa: E402
from app.scanner import kind_of                          # noqa: E402
from pathlib import Path                                 # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"
BAK = os.path.join(DATA, "emby.db.bak.thfix-081955")
SKIP_WORDS = ("10yo", "15yo", "幼", "未成年")


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


async def search(client, kw):
    for base in source115.WEBAPI_BASES:
        try:
            r = await client.get(base + "/files/search",
                                 params={"search_value": kw, "cid": "0", "aid": "1",
                                         "offset": 0, "limit": 50, "format": "json"},
                                 headers=source115._headers())
            if r.status_code != 200:
                continue
            js = r.json()
            data = js.get("data") if isinstance(js, dict) else js
            items = (data.get("list") if isinstance(data, dict) else data) or []
            if items:
                return [{"name": it.get("n") or it.get("name") or "",
                         "cid": str(it.get("cid") or it.get("parent_id") or "")} for it in items]
        except Exception:                                 # noqa: BLE001
            continue
    return []


async def main():
    ids = set(json.load(open("/tmp/adwords_ids.json", encoding="utf-8")))
    cur = sqlite3.connect(Path(DATA) / "emby.db")
    have = {r[0] for r in cur.execute("SELECT id FROM items")}
    cur.close()
    missing = list(ids - have)

    bak = sqlite3.connect(BAK)
    bak.row_factory = sqlite3.Row
    targets = []
    for i in range(0, len(missing), 500):
        ch = missing[i:i + 500]
        q = ",".join("?" * len(ch))
        for r in bak.execute("SELECT id, kind, ext, size, name_enc FROM items WHERE id IN (%s)" % q, ch):
            nm = ic.safe_unseal(r["name_enc"] or "") or ""
            low = nm.lower()
            if any(k in low for k in SKIP_WORDS):
                print("   跳过（内容性质）:", nm[:50])
                continue
            if hide_rules.is_blocked_by_rules(nm, r["kind"], r["size"] or 0):
                continue                                  # 判为广告的不补
            targets.append({"id": r["id"], "name": nm, "kind": r["kind"], "size": r["size"] or 0})
    print("待补回:", len(targets), "条")

    db = DB(Path(DATA) / "emby.db")
    kinds = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER)
    dirs, unresolved = set(), []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                 follow_redirects=True, verify=False) as client:
        for t in targets:
            nm = t["name"]
            base = re.sub(r"\.[a-z0-9]{1,5}$", "", nm, flags=re.I)
            base = re.sub(r"^www\.98t\.la@\s*", "", base, flags=re.I)
            kws = [base[:24], base[:14], base[-16:]]
            hit = None
            for kw in kws:
                kw = kw.strip()
                if len(kw) < 4:
                    continue
                hits = await search(client, kw)
                hit = next((h for h in hits if norm(h["name"]) == norm(nm) and h["cid"]), None) or \
                      next((h for h in hits if norm(nm)[:14] in norm(h["name"]) and h["cid"]), None)
                if hit:
                    break
            if hit:
                dirs.add(hit["cid"])
            else:
                unresolved.append(nm)

    print("定位到目录:", len(dirs), "| 未定位:", len(unresolved))
    for nm in unresolved:
        print("   未定位:", nm[:56])
    added = 0
    for cid in sorted(dirs):
        try:
            listing = await source115.list_dir(cid)
        except Exception as exc:                          # noqa: BLE001
            print("   列目录失败", cid, str(exc)[:50])
            continue
        rows = []
        for f in listing.get("files") or []:
            fid, name = str(f.get("id") or ""), f.get("name", "")
            if not fid:
                continue
            ext = (f.get("ext") or (name.rsplit(".", 1)[-1] if "." in name else "")).lower()
            fields = ic.seal_fields(name, "", name)
            tu = str(f.get("thumb") or "")
            rows.append({"id": fid, "parent_id": cid, "kind": kind_of(ext, kinds), "ext": ext,
                         "size": int(f.get("size") or 0), "mtime": int(f.get("mtime") or 0),
                         "name_enc": fields["name_enc"], "name_idx": fields["name_idx"],
                         "name_bucket": fields["name_bucket"], "path_enc": fields["path_enc"],
                         "thumb_enc": ic.seal_link(tu) if tu else ""})
        before = {r["id"] for r in db.list_children(cid, limit=100000)}
        new = [r for r in rows if r["id"] not in before]
        if rows:
            db.upsert_items(rows)
        added += len(new)
        print("   目录 %s：%d 文件，补 %d" % (cid, len(rows), len(new)))

    cur = sqlite3.connect(Path(DATA) / "emby.db")
    after = {r[0] for r in cur.execute("SELECT id FROM items")}
    back = [t for t in targets if t["id"] in after]
    print("\n目标 %d 条 → 已回到库里 %d 条；本次新增行 %d"
          % (len(targets), len(back), added))
    for t in targets:
        if t["id"] not in after:
            print("   仍缺: %8.3fMB  %s" % (t["size"] / 1048576, t["name"][:56]))
    print("items 总数:", cur.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    print("非图非视频:", cur.execute("SELECT COUNT(*) FROM items WHERE kind NOT IN ('image','video')").fetchone()[0])
    cur.close()


asyncio.run(main())
