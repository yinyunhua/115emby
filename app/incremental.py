"""增量扫描 v2：只跳过「真的扫完了」的目录。

为什么不能用 folders.scanned_at
------------------------------
`upsert_folder()` 是"父目录列出子目录"时调用的，它顺手把 ``scanned_at`` 写成 now。
于是目录刚入队、还没扫，就已经被打上"刚扫过"的时间戳 —— 增量跳过会把从未扫过的
目录（含里面所有文件）永久跳过。实测库里 3736 个目录，3680 个"看起来 1 小时内扫过"，
其中大量仍在 pending。

正确做法
--------
自己在 ``scan_done(dir_id, epoch)`` 表里记录"真正扫完的时刻"：
* 包住 ``db.queue_mark_done()`` —— 扫描器每处理完一个目录就调它，**在文件入库之后**；
* 增量过滤时只认这张表：``epoch >= now - TTL`` 才跳过；
* 表不存在时自动建，空表 = 第一次全量跑一遍，之后才真正省请求。

环境变量：
    SCAN_INCREMENTAL=1
    SCAN_SKIP_WITHIN_S=3600
"""
from __future__ import annotations

import os
import sqlite3
import time

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_done (
    dir_id  TEXT PRIMARY KEY,
    epoch   INTEGER NOT NULL DEFAULT 0,
    files   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scan_done_epoch ON scan_done(epoch);
"""


def _enabled() -> bool:
    return (os.getenv("SCAN_INCREMENTAL") or "1").strip() != "0"


def _skip_within() -> int:
    try:
        return max(0, int(float(os.getenv("SCAN_SKIP_WITHIN_S") or 3600)))
    except ValueError:
        return 3600


def _conn():
    con = sqlite3.connect(EMBY, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _ensure_schema() -> None:
    con = _conn()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def _recent(cutoff: int) -> set:
    con = _conn()
    try:
        return {r[0] for r in con.execute(
            "SELECT dir_id FROM scan_done WHERE epoch >= ?", (cutoff,))}
    finally:
        con.close()


def _mark(dir_id: str, nfiles: int = 0) -> None:
    con = _conn()
    try:
        con.execute(
            "INSERT INTO scan_done (dir_id, epoch, files) VALUES (?,?,?) "
            "ON CONFLICT(dir_id) DO UPDATE SET epoch=excluded.epoch, files=excluded.files",
            (str(dir_id), int(time.time()), int(nfiles)))
        con.commit()
    finally:
        con.close()


def install(app) -> None:
    db = getattr(app.state, "db", None)
    if db is None or getattr(db, "_incremental_v2", False):
        return
    _ensure_schema()

    orig_push = db.queue_push
    orig_done = db.queue_mark_done

    def queue_mark_done(job_id, dir_id):
        rc = orig_done(job_id, dir_id)
        fs = getattr(app.state, "fast_scan", None) or {}
        if str(dir_id) in (fs.get("failed") or set()):
            try:
                fs["failed"].discard(str(dir_id))
            except Exception:
                pass
            return rc                     # 失败目录不记为已扫，下次还会重试
        try:
            try:
                n = len(db.list_children(str(dir_id)))
            except Exception:
                n = 0
            _mark(dir_id, n)
        except Exception:
            pass
        return rc

    def queue_push(job_id, dirs):
        dirs = list(dirs or [])
        if not dirs or not _enabled():
            return orig_push(job_id, dirs)
        ttl = _skip_within()
        if ttl <= 0:
            return orig_push(job_id, dirs)
        try:
            recent = _recent(int(time.time()) - ttl)
        except Exception:
            recent = set()
        keep, skipped = [], 0
        for item in dirs:
            try:
                cid = str(item[0])
            except Exception:
                keep.append(item)
                continue
            if cid in recent:
                skipped += 1
                continue
            keep.append(item)
        if skipped:
            try:
                st = app.state.incremental_stats
                st["skipped"] += skipped
                st["last_skip_at"] = int(time.time())
            except Exception:
                pass
        return orig_push(job_id, keep)

    db.queue_push = queue_push                 # type: ignore[assignment]
    db.queue_mark_done = queue_mark_done       # type: ignore[assignment]
    db._incremental_v2 = True
    app.state.incremental_stats = {"skipped": 0, "last_skip_at": 0}
