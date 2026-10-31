"""重试补回剩下的条目（只 upsert，不删）。

改进点：
  * direct_link 失败重试 3 次（容器里 appapi.115.com 偶发 DNS 失败）；
  * 搜索用多种关键词变体：前 12 字 / 前 22 字 / 去扩展名整名；
  * 命中判定放宽到"包含"。
"""
import asyncio
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, "/app")
import httpx                                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402
from app import source115                               # noqa: E402
from app.db import DB, KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER   # noqa: E402
from app.scanner import kind_of                          # noqa: E402
from pathlib import Path                                 # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"


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


async def link_name(client, fid):
    for attempt in range(3):
        try:
            url, _e, _u = await source115.direct_link({"id": fid}, client=client)
            import urllib.parse
            seg = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
            if seg:
                return seg
        except Exception:                                 # noqa: BLE001
            await asyncio.sleep(1.5)
    return ""


async def main():
    ids = json.load(open("/tmp/adwords_ids.json", encoding="utf-8"))
    d = sqlite3.connect(Path(DATA) / "emby.db")
    have_ids = {r[0] for r in d.execute("SELECT id FROM items")}
    names = {}
    for (i, enc) in d.execute("SELECT id, name_enc FROM items"):
        names[i] = norm(ic.safe_unseal(enc or "") or "")
    d.close()
    missing = [i for i in ids if i not in have_ids]
    print("仍缺 id:", len(missing))

    db = DB(Path(DATA) / "emby.db")
    kinds = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER)
    dirs = set()
    got = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                 follow_redirects=True, verify=False) as client:
        for k, fid in enumerate(missing, 1):
            nm = await link_name(client, fid)
            if not nm:
                continue
            got += 1
            base = re.sub(r"\.[a-z0-9]{1,5}$", "", nm, flags=re.I)
            base = re.sub(r"^www\.98t\.la@\s*", "", base, flags=re.I)
            for keyw in (base[:22], base[:12], base[-14:]):
                keyw = keyw.strip()
                if len(keyw) < 4:
                    continue
                hits = await search(client, keyw)
                tgt = norm(nm)
                hit = next((h for h in hits if norm(h["name"]) == tgt and h["cid"]), None) or \
                      next((h for h in hits if tgt[:14] in norm(h["name"]) and h["cid"]), None)
                if hit:
                    dirs.add(hit["cid"])
                    break
            if k % 10 == 0:
                print("  进度 %d/%d：解析出名字 %d，定位到目录 %d" % (k, len(missing), got, len(dirs)))
    print("解析出名字:", got, "/", len(missing), "| 新增定位目录:", len(dirs))
    added = 0
    for cid in sorted(dirs):
        try:
            listing = await source115.list_dir(cid)
        except Exception as exc:                          # noqa: BLE001
            print("   列目录失败", cid, str(exc)[:60])
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

    d = sqlite3.connect(Path(DATA) / "emby.db")
    after = {r[0] for r in d.execute("SELECT id FROM items")}
    print("\n本次补 %d 条；仍缺 id: %d（原缺 %d）"
          % (added, len([i for i in missing if i not in after]), len(missing)))
    print("items 总数:", d.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    d.close()


asyncio.run(main())
