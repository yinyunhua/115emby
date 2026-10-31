"""用 115 搜索接口定位被误删文件所在的目录，再定向补扫。

115 webapi: GET /files/search?search_value=xxx&cid=0&aid=1&limit=...&format=json
返回里 cid 即父目录 id（不同版本字段名可能不同，这里做兼容）。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")
import httpx                                              # noqa: E402
from app import source115                                 # noqa: E402

KEYS = ("BT-btt", "IMG_2580")


async def search(client, kw):
    out = []
    for base in source115.WEBAPI_BASES:
        try:
            r = await client.get(base + "/files/search",
                                 params={"search_value": kw, "cid": "0", "aid": "1",
                                         "offset": 0, "limit": 50, "format": "json",
                                         "show_dir": 1},
                                 headers=source115._headers())
            if r.status_code != 200:
                continue
            js = r.json()
        except Exception as exc:                          # noqa: BLE001
            print("  搜索异常", base, exc)
            continue
        data = js.get("data") if isinstance(js, dict) else None
        if isinstance(data, dict):
            items = data.get("list") or []
        elif isinstance(data, list):
            items = data
        else:
            items = js.get("list") if isinstance(js, dict) else []
        for it in items or []:
            out.append({"name": it.get("n") or it.get("name") or "",
                        "cid": str(it.get("cid") or it.get("parent_id") or ""),
                        "fid": str(it.get("fid") or it.get("file_id") or it.get("id") or ""),
                        "size": int(str(it.get("s") or it.get("size") or 0).replace(",", "") or 0),
                        "pc": it.get("pc") or it.get("pick_code") or ""})
        if out:
            break
    return out


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0),
                                 follow_redirects=True) as client:
        dirs = set()
        for kw in KEYS:
            hits = await search(client, kw)
            print("搜索 %-10s 命中 %d 条" % (kw, len(hits)))
            for h in hits[:10]:
                print("     cid=%-22s %8.2fMB  %s" % (h["cid"], (h["size"] or 0) / 1048576,
                                                      h["name"][:50]))
                if h["cid"] and h["cid"] not in ("0", ""):
                    dirs.add(h["cid"])
        print("涉及目录:", len(dirs))
        json.dump(sorted(dirs), open("/tmp/fp_dirs.json", "w"))
        for d in sorted(dirs):
            print("   ", d)


asyncio.run(main())
