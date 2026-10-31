"""快速增量扫描（可被后台接口调用）：只往下钻「目录时间变了」的分支。

设计要点
--------
* 115 的目录 entry 带 `t/te/tu/tp/to`，取 `max(...)` 作为「目录内容时间」`mt`；
* 逐层下钻：子目录 `mt` 与库内一致 且已 `scan_done` → **整棵子树跳过**（不再发请求）；
* 每访问一个目录，顺手 upsert 它下面的文件（新增文件就此入库）；
* 全新库/首次运行没有 `folder_mtime` → 等价于一次全量（会写种子），之后才快。

对外接口
--------
``async def run_quick(root, *, db_path, max_requests, on_progress, dry_run) -> dict``
返回 ``{requests, dirs, skipped, folders, items, new_items, elapsed, stopped}``。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time

from .db import KIND_IMAGE, KIND_VIDEO
from . import hide_rules
from . import index_crypto as ic
from . import source115
from .thumb_store import ThumbStore

DEFAULT_DB = os.getenv("EMBY_DB") or "/app/data/emby.db"

VIDEO = {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts", "m2ts", "rmvb", "rm",
         "3gp", "3g2", "mpg", "mpeg", "mts", "vob", "asf", "divx", "f4v", "m2v", "mxf",
         "ogv", "wtv", "tp", "trp", "mod", "tod"}
IMAGE = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif", "avif", "tiff", "svg"}
AUDIO = {"mp3", "flac", "aac", "m4a", "wav", "ogg", "opus", "wma"}


def kind_of(ext):
    e = (ext or "").lower().lstrip(".")
    if e in VIDEO:
        return "video"
    if e in IMAGE:
        return "image"
    if e in AUDIO:
        return "audio"
    return "other"


def mt_of(entry):
    """目录内容变化信号（优先用 source115 归一化后的 mtime）。"""
    v = entry.get("mtime")
    try:
        if v:
            return int(v)
    except (TypeError, ValueError):
        pass
    times = entry.get("times") or {}
    if times:
        try:
            return max(int(x) for x in times.values())
        except (TypeError, ValueError):
            pass
    vals = []
    for k in ("t", "te", "tu", "tp", "to"):
        x = entry.get(k)
        try:
            if x not in (None, ""):
                vals.append(int(float(x)))
        except (TypeError, ValueError):
            pass
    return max(vals) if vals else 0


def _cols(con, table):
    return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}


def _upsert(con, table, row, key="id"):
    have = _cols(con, table)
    use = {k: v for k, v in row.items() if k in have}
    if key not in use:
        return
    names = list(use.keys())
    q = ("INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s"
         % (table, ",".join(names), ",".join("?" * len(names)), key,
            ",".join("%s=excluded.%s" % (n, n) for n in names if n != key)))
    con.execute(q, [use[n] for n in names])


async def run_quick(root: str, *, db_path: str = "", max_requests: int = 50000,
                    on_progress=None, dry_run: bool = False, sleep_between: float = 0.35,
                    should_stop=None) -> dict:
    db = db_path or DEFAULT_DB
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row

    have = _cols(con, "folders")
    if "folder_mtime" not in have:
        con.execute("ALTER TABLE folders ADD COLUMN folder_mtime INTEGER DEFAULT 0")
        con.commit()

    has_done = bool(con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scan_done'").fetchone())
    done_dirs = {r[0] for r in con.execute("SELECT dir_id FROM scan_done")} if has_done else set()
    known = {r["id"]: int(r["folder_mtime"] or 0)
             for r in con.execute("SELECT id, folder_mtime FROM folders")}
    known_depth = {r["id"]: int(r["depth"] or 0)
                   for r in con.execute("SELECT id, depth FROM folders")}
    items_before = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    state = {"requests": 0, "dirs": 0, "skipped": 0, "folders": 0, "items": 0, "stopped": ""}
    # ★ req-scanqueue：本次新入库的媒体 id，结束时统一登记到缩略图队列。
    # 不登记的话这些条目在 thumb_src 里根本没有行，
    # 后台「媒体概览 · 等待生成」就会一直是 0（GROUP BY 数不到不存在的行）。
    new_media_ids = []
    t0 = time.time()
    queue = [(str(root), 0)]
    seen = set()

    def tick():
        if on_progress:
            try:
                on_progress(dict(state, elapsed=round(time.time() - t0, 1),
                                 new_items=max(0, con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                                               - items_before)))
            except Exception:
                pass

    while queue:
        if should_stop and should_stop():
            state["stopped"] = "stopped_by_user"
            break
        if state["requests"] >= max_requests:
            state["stopped"] = "max_requests"
            break
        cid, rel = queue.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        try:
            listing = await source115.list_dir(cid)
            state["requests"] += 1
        except Exception:
            continue
        state["dirs"] += 1
        now = int(time.time())
        depth_now = rel

        for f in (listing.get("folders") or []):
            fid = str(f.get("id") or "")
            if not fid:
                continue
            mt = mt_of(f)
            sealed = ic.seal_fields(f.get("name", ""))
            # 注意：depth 必须是**绝对层级**（相对扫描根写会把深层目录改浅，
            # 2026-10-20 就因此把 346 个第 2 层目录写成第 1 层）。
            abs_depth = known_depth.get(cid, 0) + 1
            known_depth[fid] = abs_depth
            if not dry_run:
                _upsert(con, "folders", {
                    "id": fid, "parent_id": cid, "depth": abs_depth,
                    "name_enc": sealed["name_enc"], "name_idx": sealed["name_idx"],
                    "name_bucket": sealed["name_bucket"], "folder_mtime": mt,
                    "scanned_at": now, "updated": now,
                })
                state["folders"] += 1
            if fid in known and known[fid] == mt and mt > 0 and fid in done_dirs:
                state["skipped"] += 1
                continue
            queue.append((fid, depth_now + 1))

        for f in (listing.get("files") or []):
            fid = str(f.get("id") or "")
            if not fid:
                continue
            name = f.get("name", "")
            ext = (f.get("ext") or (name.rsplit(".", 1)[-1] if "." in name else "")).lower()
            # ★ 这个函数是**自己写库**的，绕过了 db.upsert_items 的过滤，
            #   所以这里必须同样丢掉：非图片/视频、以及黑名单里的广告。
            _k = kind_of(ext)
            if _k not in (KIND_IMAGE, KIND_VIDEO):
                continue
            if hide_rules.is_blocked(name, _k, int(f.get("size") or 0)):
                continue
            sealed = ic.seal_fields(name, "", name)
            thumb = str(f.get("thumb") or "")
            if not dry_run:
                _upsert(con, "items", {
                    "id": fid, "parent_id": cid, "kind": _k, "ext": ext,
                    "size": int(f.get("size") or 0), "mtime": int(f.get("mtime") or 0),
                    "name_enc": sealed["name_enc"], "name_idx": sealed["name_idx"],
                    "name_bucket": sealed["name_bucket"], "path_enc": sealed["path_enc"],
                    "thumb_enc": ic.seal_link(thumb) if thumb else "",
                    "updated": now,
                })
                state["items"] += 1
                new_media_ids.append(fid)          # ★ req-scanqueue

        if not dry_run:
            if has_done:
                _upsert(con, "scan_done", {"dir_id": cid, "epoch": now}, key="dir_id")
            con.commit()
        tick()
        if sleep_between:
            await asyncio.sleep(sleep_between)

    if not state["stopped"]:
        state["stopped"] = "done"
    items_after = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    con.close()
    # ★ req-scanqueue：把本次新入库的媒体登记成 pending，
    # 供夜间窗口 / 看图懒抓消费。失败不影响扫描结果（缩略图是次要路径）。
    queued = 0
    if new_media_ids and not dry_run:
        try:
            store = ThumbStore(os.getenv("THUMB_DB") or "/app/data/thumbs.db")
            # ★ req-enqueuefilter：入队前就排除注定生成不了的（GIF / 小图 / 非媒体），
            # 否则「待生成」会虚高（实测 2335 里 2016 个是 GIF）。
            queued = int(store.enqueue_eligible(
                [(pid, "", 0) for pid in new_media_ids], emby_db=db) or 0)
        except Exception as exc:                    # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("入队缩略图失败：%s", exc)
    tick_result = dict(state)
    tick_result.update({"new_items": max(0, items_after - items_before),
                        "elapsed": round(time.time() - t0, 1),
                        "items_total": items_after,
                        "queued_thumbs": queued})
    return tick_result
