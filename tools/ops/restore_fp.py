"""核对那 7 个被误删的文件是否已回到库里；缺的用 115 精确搜索定位并补扫。"""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app")
import httpx                                              # noqa: E402
from app import source115                                 # noqa: E402
from app import index_crypto as ic                        # noqa: E402
from app.db import DB, KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER   # noqa: E402
from app.scanner import kind_of                           # noqa: E402
from pathlib import Path                                  # noqa: E402

WANT = ["[bt-btt.com]xw.mp4", "[bt-btt.com]mt.mp4", "[bt-btt.com]sd.mp4",
        "[bt-btt.com]ss.mp4", "[bt-btt.com]sm.mp4", "img_2580 .png.jpg"]
DBP = "/app/data/emby.db"


def present():
    d = sqlite3.connect(DBP)
    have = {}
    for (enc,) in d.execute("SELECT name_enc FROM items"):
        nm = ic.safe_unseal(enc or "") or ""
        low = nm.lower()
        for w in WANT:
            if low == w or (low.startswith("[bt-btt.com]") and low == w):
                have.setdefault(w, []).append(nm)
    d.close()
    return have


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
            items = (js.get("data") or {}).get("list") if isinstance(js.get("data"), dict) else js.get("list")
            if items:
                return [(it.get("n") or it.get("name") or "", str(it.get("cid") or "")) for it in items]
        except Exception:                                 # noqa: BLE001
            continue
    return []


async def main():
    have = present()
    print("== 当前库里已存在 ==")
    for w in WANT:
        print("  %-22s %s" % (w, "有 %d 份" % len(have[w]) if w in have else "缺失"))
    missing = [w for w in WANT if w not in have]
    if not missing:
        print("7 个全部已恢复")
        return
    print("\n== 缺 %d 个，用 115 搜索定位所在目录 ==" % len(missing))
    dirs = set()
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0),
                                 follow_redirects=True) as client:
        for w in missing:
            hits = await search(client, w)
            print("  搜索 %-22s 命中 %d" % (w, len(hits)))
            for nm, cid in hits[:6]:
                if cid:
                    dirs.add(cid)
                    print("      cid=%s  %s" % (cid, nm[:46]))
    json.dump(sorted(dirs), open("/tmp/fp_dirs3.json", "w"))
    # 补扫
    db = DB(Path(DBP))
    kinds = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER)
    added = 0
    for cid in sorted(dirs):
        try:
            listing = await source115.list_dir(cid)
        except Exception as exc:                          # noqa: BLE001
            print("  列目录失败", cid, exc)
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
        print("  目录 %s：%d 文件，补 %d" % (cid, len(rows), len(new)))
    print("合计补回:", added)
    print("\n== 复查 ==")
    have2 = present()
    for w in WANT:
        print("  %-22s %s" % (w, "有 %d 份" % len(have2[w]) if w in have2 else "仍缺失"))


asyncio.run(main())
