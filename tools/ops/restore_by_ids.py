"""按 id 把误删的条目补回来（**只 upsert，绝不删除**）。

步骤：
  ① 对每个 id（``fid:pickcode``）取 115 直链，从 URL 里取出原始文件名；
  ② 用文件名特征串调 115 搜索，拿到它所在的目录 cid；
  ③ 定向补扫这些目录（list_dir → upsert_items）。
另外支持直接给一批"文件名词"（用于那些已经知道名字的，如 [BT-btt.com]XW.mp4）。
"""
import asyncio
import json
import os
import re
import sqlite3
import sys
import urllib.parse

sys.path.insert(0, "/app")
import httpx                                              # noqa: E402
from app import hide_rules                              # noqa: E402
from app import index_crypto as ic                      # noqa: E402
from app import source115                               # noqa: E402
from app.db import DB, KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER   # noqa: E402
from app.scanner import kind_of                          # noqa: E402
from pathlib import Path                                 # noqa: E402

DATA = os.getenv("DATA_DIR") or "/app/data"
IDS_FILE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/adwords_ids.json"
EXTRA_NAMES = ["[bt-btt.com]xw.mp4", "[bt-btt.com]mt.mp4", "[bt-btt.com]sd.mp4",
               "[bt-btt.com]ss.mp4", "[bt-btt.com]sm.mp4"]


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
            if isinstance(data, dict):
                items = data.get("list") or data.get("data") or []
            elif isinstance(data, list):
                items = data
            else:
                items = []
            if items:
                return [{"name": it.get("n") or it.get("name") or "",
                         "cid": str(it.get("cid") or it.get("parent_id") or "")} for it in items]
        except Exception:                                 # noqa: BLE001
            continue
    return []


def name_from_url(url):
    try:
        p = urllib.parse.urlparse(url).path
        seg = urllib.parse.unquote(p.rsplit("/", 1)[-1])
        return seg
    except Exception:                                     # noqa: BLE001
        return ""


async def main():
    ids = json.load(open(IDS_FILE, encoding="utf-8"))
    print("待恢复 id:", len(ids), "| 额外按名字恢复:", len(EXTRA_NAMES))
    db = DB(Path(DATA) / "emby.db")
    d = sqlite3.connect(Path(DATA) / "emby.db")
    have_ids = {r[0] for r in d.execute("SELECT id FROM items")}
    have_names = set()
    for (enc,) in d.execute("SELECT name_enc FROM items"):
        have_names.add(norm(ic.safe_unseal(enc or "") or ""))
    d.close()

    todo = [(i, None) for i in ids if i not in have_ids]
    todo += [(None, n) for n in EXTRA_NAMES if norm(n) not in have_names]
    print("确认缺失:", len(todo), "条（其余已在库）")
    if not todo:
        return

    kinds = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER)
    dirs, resolved = set(), 0
    names_of = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                 follow_redirects=True, verify=False) as client:
        # ① id → 文件名
        for i, nm in todo:
            if nm:
                names_of[i or nm] = nm
                continue
            try:
                url, _exp, _ua = await source115.direct_link({"id": i}, client=client)
                fn = name_from_url(url) or ""
            except Exception as exc:                      # noqa: BLE001
                print("   取直链失败", i[:22], str(exc)[:60])
                fn = ""
            names_of[i] = fn
            if fn:
                resolved += 1
        print("① 解析出文件名:", resolved, "/", len([1 for i, n in todo if not n]))

        # ② 名字 → 目录
        miss = []
        for key, nm in names_of.items():
            if not nm:
                miss.append(key)
                continue
            base = re.sub(r"\.[a-z0-9]{1,5}$", "", nm, flags=re.I)
            keyw = re.sub(r"^www\.98t\.la@\s*", "", base, flags=re.I)[:16].strip() or base[:16]
            hits = await search(client, keyw)
            tgt = norm(nm)
            hit = next((h for h in hits if norm(h["name"]) == tgt and h["cid"]), None) or \
                  next((h for h in hits if norm(h["name"]).startswith(tgt[:16]) and h["cid"]), None)
            if hit:
                dirs.add(hit["cid"])
            else:
                miss.append(key)
        print("② 定位到目录:", len(dirs), "个；未定位:", len(miss))

    added = 0
    for cid in sorted(dirs):
        try:
            listing = await source115.list_dir(cid)
        except Exception as exc:                          # noqa: BLE001
            print("   列目录失败", cid, exc)
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
        print("   目录 %s：%d 个文件，补 %d" % (cid, len(rows), len(new)))

    d = sqlite3.connect(Path(DATA) / "emby.db")
    after_ids = {r[0] for r in d.execute("SELECT id FROM items")}
    after_names = {norm(ic.safe_unseal(r[0] or "") or "")
                   for r in d.execute("SELECT name_enc FROM items")}
    back = sum(1 for i, n in todo if (i and i in after_ids) or (n and norm(n) in after_names))
    print("\n本次补回 %d 条；待恢复 %d 条中已有 %d 条回到库里" % (added, len(todo), back))
    print("items 总数:", d.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    d.close()


asyncio.run(main())
