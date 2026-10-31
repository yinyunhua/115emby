"""补回我自加规则误删的内容条目（**只 upsert，不删**）。

补回对象 = ⑦其他/无规则命中（去掉未成年人那条）+ ④推广语里被误伤的内容图。
做法：从备份库读名字 → 115 搜索定位父目录 → list_dir + upsert_items。
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
MINOR = ("10yo", "15yo", "幼", "未成年")
# 明确补回的名字（上次回复里列给你的"该还给你的"）
WANT_EXACT = [
    "[bt-btt.com]mt.mp4", "[bt-btt.com]xym.mp4", "[bt-btt.com]fync_0414.mov.mp4",
    "[bt-btt.com]yt.mp4", "[bt-btt.com]sys.mp4",
    "2048.cc-5_6253258051223031936.mov.jpg",
    "2048.cc-微博热传胡连馨男朋友裸聊视频的瓜.jpg",
    "趁着宿舍没人直接在男生宿舍做爱，还有洗澡裸体自拍以及与男友裸聊视频，看来平时经常自拍，流出的这些应该也是冰山一角，有这样的大学生",
    "gc2048.com-20230122_09573063.png.jpg",
    "gc2048.com-104626nu3eq8b00ungab0y.jpg.thumb.jpg",
    "gc2048.com-104634mllvvhzvhwny1h8z.jpg.thumb.jpg",
    "gc2048.com-104636c3lassxdenynylb2.jpg.thumb.jpg",
    "sis001影视联盟.gif",
]


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
    cur = sqlite3.connect(Path(DATA) / "emby.db")
    have_ids = {r[0] for r in cur.execute("SELECT id FROM items")}
    have_names = {norm(ic.safe_unseal(r[0] or "") or "") for r in cur.execute("SELECT name_enc FROM items")}
    cur.close()
    bak = sqlite3.connect(BAK)
    bak.row_factory = sqlite3.Row

    targets = []
    for r in bak.execute("SELECT id, kind, ext, size, name_enc FROM items"):
        if r["id"] in have_ids:
            continue
        nm = ic.safe_unseal(r["name_enc"] or "") or ""
        if any(k in nm.lower() for k in MINOR):
            continue
        if norm(nm) in {norm(x) for x in WANT_EXACT}:
            targets.append({"id": r["id"], "name": nm, "kind": r["kind"], "size": r["size"] or 0})
    # 顺便把 ④ 里"含 98T 命名"的那 1 条也带上（若为图/视频）
    for r in bak.execute("SELECT id, kind, ext, size, name_enc FROM items"):
        if r["id"] in have_ids or r["kind"] not in ("image", "video"):
            continue
        nm = ic.safe_unseal(r["name_enc"] or "") or ""
        low = nm.lower()
        if not re.match(r"^\s*(?:www\.)?98t\.(?:la|com)@?\s*", nm, re.I):
            continue
        if hide_rules.is_blocked_by_rules(nm, r["kind"], r["size"] or 0) and \
           ("98t" in low and (hide_rules._ad_re().search(low) or hide_rules.split_domain_evasion(nm, r["size"] or 0))):
            targets.append({"id": r["id"], "name": nm, "kind": r["kind"], "size": r["size"] or 0})
    seen, tl = set(), []
    for t in targets:
        if t["id"] not in seen:
            seen.add(t["id"]); tl.append(t)
    print("待补回:", len(tl), "条")
    for t in sorted(tl, key=lambda x: -x["size"]):
        print("   %9.3fMB  %s" % (t["size"] / 1048576, t["name"][:62]))

    db = DB(Path(DATA) / "emby.db")
    kinds = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER)
    dirs, unresolved = set(), []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                 follow_redirects=True, verify=False) as client:
        for t in tl:
            nm = t["name"]
            base = re.sub(r"\.[a-z0-9]{1,5}$", "", nm, flags=re.I)
            base = re.sub(r"^www\.98t\.la@\s*", "", base, flags=re.I)
            hit = None
            for kw in (base[:24], base[:14], base[-16:]):
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
    print("\n定位到目录:", len(dirs), "| 未定位:", len(unresolved))
    for nm in unresolved:
        print("   未定位:", nm[:58])
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
    after_ids = {r[0] for r in cur.execute("SELECT id FROM items")}
    after_names = {norm(ic.safe_unseal(r[0] or "") or "") for r in cur.execute("SELECT name_enc FROM items")}
    back = [t for t in tl if t["id"] in after_ids or norm(t["name"]) in after_names]
    print("\n目标 %d 条 → 已回库 %d 条；本次新增行 %d" % (len(tl), len(back), added))
    for t in tl:
        if t not in back:
            print("   仍缺: %9.3fMB  %s" % (t["size"] / 1048576, t["name"][:58]))
    print("items 总数:", cur.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    print("非图非视频:", cur.execute("SELECT COUNT(*) FROM items WHERE kind NOT IN ('image','video')").fetchone()[0])
    cur.close()


asyncio.run(main())
