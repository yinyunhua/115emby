"""把"被过宽规则误删"的条目找回来。

思路：上一轮删的时候把 (名字,体积) 全写进了 block_names.json，
所以名字还在那儿 —— 用**收紧后的规则**重判，
凡是"名字不在人工清单里、且新规则不再判广告"的，就是误删：
  ① 从黑名单里撤掉；
  ② 用 115 搜索定位它所在目录（搜索文件名的一段特征串）；
  ③ 定向补扫那个目录，条目就回来了。
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
VIDEO_EXT = ("mp4", "avi", "mov", "mkv", "wmv", "ts", "rmvb", "flv", "m4v", "mpg", "mpeg", "rm", "vob", "3gp")


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
            # ★ 115 搜索返回的是 {"count":N,"data":[...]}：data **是数组**，
            #   之前按 data["list"] 取，永远取不到 → 全部"搜索未命中"。
            data = js.get("data") if isinstance(js, dict) else js
            if isinstance(data, dict):
                items = data.get("list") or data.get("data") or []
            elif isinstance(data, list):
                items = data
            else:
                items = []
            if items:
                return [{"name": it.get("n") or it.get("name") or "",
                         "cid": str(it.get("cid") or it.get("parent_id") or ""),
                         "fid": str(it.get("fid") or it.get("id") or "")} for it in items]
        except Exception:                                 # noqa: BLE001
            continue
    return []


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


async def main():
    bp = Path(DATA) / "block_names.json"
    bl = json.load(open(bp, encoding="utf-8"))
    keep_names = {norm(x) for x in (bl.get("names") or [])}      # 人工清单，永远保留
    pairs = bl.get("pairs") or []
    fps, keep_pairs = [], []
    for nm, sz in pairs:
        kind = "video" if nm.lower().rsplit(".", 1)[-1] in VIDEO_EXT else "image"
        if norm(nm) in keep_names or hide_rules.is_blocked_by_rules(nm, kind, int(sz)):
            keep_pairs.append([nm, sz])
        else:
            fps.append((nm, int(sz), kind))
    print("黑名单 %d 条 → 保留 %d 条，撤回（误删）%d 条" % (len(pairs), len(keep_pairs), len(fps)))
    for nm, sz, k in sorted(fps, key=lambda x: -x[1])[:20]:
        print("   撤回: %-8s %9.3fMB  %s" % (k, sz / 1048576, nm[:50]))
    json.dump({"pairs": keep_pairs, "names": sorted(keep_names)}, open(bp, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("黑名单已更新")
    if not fps:
        return

    print("\n== 用 115 搜索定位误删项的目录 ==")
    db = DB(Path(DATA) / "emby.db")
    kinds = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER)
    todo_dirs, found = set(), 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0),
                                 follow_redirects=True) as client:
        for nm, sz, k in fps:
            base = nm.rsplit(".", 1)[0]
            key = re.sub(r"^www\.98t\.la@\s*", "", base, flags=re.I)[:14].strip() or base[:14]
            hits = await search(client, key)
            tgt = norm(nm)
            hit = next((h for h in hits if norm(h["name"]) == tgt and h["cid"]), None)
            if not hit:
                hit = next((h for h in hits if norm(h["name"]).startswith(tgt[:18]) and h["cid"]), None)
            if hit:
                found += 1
                todo_dirs.add(hit["cid"])
                print("   %-40s -> cid %s" % (nm[:40], hit["cid"]))
            else:
                print("   %-40s -> 搜索未命中（关键词 %r，返回 %d 条）" % (nm[:40], key, len(hits)))

    print("\n== 补扫 %d 个目录 ==" % len(todo_dirs))
    added = 0
    for cid in sorted(todo_dirs):
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
        print("   %s：%d 文件，补 %d" % (cid, len(rows), len(new)))
    print("\n搜索命中 %d/%d，共补回 %d 条" % (found, len(fps), added))
    d = sqlite3.connect(Path(DATA) / "emby.db")
    print("items 总数:", d.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    d.close()


asyncio.run(main())
