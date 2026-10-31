"""定向补扫：把指定目录重新列一遍，缺的条目补回库（用于撤销误删）。

用法：python tools/rescan_dirs.py /tmp/rescan_dirs.json
只补不删；黑名单里的条目依然会被丢掉（这是 upsert_items 的行为）。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")
from app import index_crypto as ic                      # noqa: E402
from app import source115                                # noqa: E402
from app.db import DB, KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER   # noqa: E402
from app.scanner import kind_of                          # noqa: E402
from app.thumb_store import ThumbStore                   # noqa: E402
from pathlib import Path

KINDS = (KIND_IMAGE, KIND_VIDEO, KIND_AUDIO, KIND_OTHER)


async def main():
    dirs = json.load(open(sys.argv[1], encoding="utf-8"))
    db = DB(Path(os.getenv("EMBY_DB") or "/app/data/emby.db"))
    ts = None
    try:
        ts = ThumbStore(Path(os.getenv("THUMB_DB") or "/app/data/thumbs.db"))
    except Exception as exc:                              # noqa: BLE001
        print("缩略图队列跳过:", exc)

    total_new = 0
    for cid in dirs:
        try:
            listing = await source115.list_dir(cid)
        except Exception as exc:                          # noqa: BLE001
            print("列目录失败", cid, exc)
            continue
        files = listing.get("files") or []
        before = {r["id"] for r in db.list_children(cid, limit=100000)}
        rows = []
        for f in files:
            fid = str(f.get("id") or "")
            name = f.get("name", "")
            if not fid:
                continue
            ext = (f.get("ext") or (name.rsplit(".", 1)[-1] if "." in name else "")).lower()
            fields = ic.seal_fields(name, "", name)
            thumb_url = str(f.get("thumb") or "")
            rows.append({
                "id": fid, "parent_id": cid, "kind": kind_of(ext, KINDS),
                "ext": ext, "size": int(f.get("size") or 0), "mtime": int(f.get("mtime") or 0),
                "name_enc": fields["name_enc"], "name_idx": fields["name_idx"],
                "name_bucket": fields["name_bucket"], "path_enc": fields["path_enc"],
                "thumb_enc": ic.seal_link(thumb_url) if thumb_url else "",
            })
        new = [r for r in rows if r["id"] not in before]
        if rows:
            db.upsert_items(rows)
            if ts is not None and new:
                try:
                    ts.enqueue([(r["id"], "", 0) for r in new])
                except Exception:                          # noqa: BLE001
                    pass
        total_new += len(new)
        print("目录 %s：115 侧 %d 个文件，库里原有 %d，新补 %d" % (cid, len(rows), len(before), len(new)))
        for r in new:
            nm = ic.safe_unseal(r["name_enc"])
            print("      + %-6s %8.2fMB  %s" % (r["ext"], r["size"] / 1048576, nm[:56]))
    print("合计补回:", total_new)


asyncio.run(main())
