"""115 目录扫描器：后台跑、可选目录、限深度、可暂停/恢复/取消、断点续传。

为什么是"任务 + 队列"而不是一个 while 循环
------------------------------------------
扫描几万个目录要跑很久，中途会：被叫停、进程重启、上游限流。所以：

* 任务信息落 ``scan_jobs``（从哪个 cid 开始、深度、状态、计数）；
* 待扫目录落 ``scan_queue``（一行一个 cid，``state`` 标 pending/done）；
* 进程重启后 ``resume()`` 直接从队列里接着扫 —— 这就是断点续传；
* 暂停/取消只是改任务状态，队列不动，随时能续。

节流
----
每一次上游 ``list_dir`` 都穿过 :class:`app.throttle.Throttle`
（``API_MIN_INTERVAL`` / ``API_CONCURRENCY`` / ``PREFETCH_REST_*``），
和缩略图抓取共用同一套预算 —— 同一个 115 账号只有一个出口。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Iterable, Optional

from . import index_crypto as ic
from .db import DB, KIND_AUDIO, KIND_IMAGE, KIND_OTHER, KIND_VIDEO
from .settings import Settings
from .throttle import Throttle

log = logging.getLogger("album.scanner")

# list_dir(cid) -> {"folders":[{"id","name"}], "files":[{"id","name","size","mtime","ext"}]}
ListDir = Callable[[str], Awaitable[dict]]

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_CANCELED = "canceled"
STATUS_ERROR = "error"

# depth<=0 表示「不限深度，一直扫到叶子」
MAX_FAIL_STREAK = 5      # req-scanfix：连续这么多个目录列取失败才停任务

UNLIMITED_DEPTH = 10 ** 6


def kind_of(ext: str, kinds: Iterable[str] | None = None) -> str:
    e = (ext or "").lower().lstrip(".")
    from .video_thumb import VideoThumbOptions  # noqa: F401  (保持依赖方向清晰)

    video = {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts", "m2ts", "rmvb", "rm"}
    image = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif", "avif", "tiff", "svg"}
    audio = {"mp3", "flac", "aac", "m4a", "wav", "ogg", "opus", "wma"}
    k = KIND_VIDEO if e in video else KIND_IMAGE if e in image else KIND_AUDIO if e in audio else KIND_OTHER
    if kinds and k not in kinds:
        return KIND_OTHER          # 被过滤掉的类型统一标 other，前端不展示
    return k


class Scanner:
    def __init__(self, db: DB, list_dir: ListDir, settings: Optional[Settings] = None,
                 thumb_store: Any = None) -> None:
        """``thumb_store`` 给了就"只入队"，扫描过程**不会**发任何缩略图请求。"""
        self.db = db
        self.list_dir = list_dir
        self.thumb_store = thumb_store
        self.settings = settings or Settings.from_env()
        self._throttle = Throttle(self.settings.api_min_interval, self.settings.api_concurrency)
        self._tasks: dict[str, asyncio.Task] = {}
        self._rest_counter = 0

    # ------------------------------------------------------------------ 任务
    def start(self, root_id: str = "-1", *, depth: int = 3, root_name: str = "",
              kinds: Iterable[str] | None = None) -> str:
        """创建一个扫描任务（不启动）。返回 job_id。"""
        job_id = uuid.uuid4().hex[:12]
        self.db.create_job(job_id, root_id, ic.seal(root_name) if root_name else "",
                           max(0, int(depth)),
                           ",".join(sorted(kinds)) if kinds else "")
        log.info("扫描任务已创建 %s：root=%s depth=%s", job_id, root_id or "-1",
                 "不限(到叶子)" if int(depth) <= 0 else depth)
        return job_id

    def run_in_background(self, job_id: str) -> asyncio.Task:
        """后台跑（不阻塞调用方）；重复调用返回同一个 task。"""
        task = self._tasks.get(job_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self.run(job_id))
        self._tasks[job_id] = task
        return task

    def pause(self, job_id: str) -> None:
        self.db.set_job_status(job_id, STATUS_PAUSED)

    def cancel(self, job_id: str) -> None:
        self.db.set_job_status(job_id, STATUS_CANCELED)

    def resume(self, job_id: str) -> asyncio.Task:
        """从队列继续（断点续传）。"""
        self.db.set_job_status(job_id, STATUS_PENDING)
        return self.run_in_background(job_id)

    def status(self, job_id: str) -> dict:
        job = self.db.job(job_id)
        if not job:
            return {}
        d = dict(job)
        d["root_name"] = ic.safe_unseal(d.get("root_name_enc") or "")
        d.pop("root_name_enc", None)
        d.update(self.db.queue_size(job_id))
        d["kinds"] = [k for k in (d.get("kinds") or "").split(",") if k]
        return d

    # ------------------------------------------------------------------ 主循环
    async def run(self, job_id: str, *, max_dirs: int | None = None) -> dict:
        job = self.db.job(job_id)
        if not job:
            raise ValueError(f"没有这个任务：{job_id}")
        if job["status"] == STATUS_CANCELED:
            # 已被取消：不要把它改回 running 再扫一遍
            log.info("任务 %s 已取消，不启动", job_id)
            return self.status(job_id)
        self.db.set_job_status(job_id, STATUS_RUNNING)
        try:      # 老库补列（幂等）
            with self.db._lock, self.db._db() as _c:        # noqa: SLF001
                _have = {r[1] for r in _c.execute("PRAGMA table_info(folders)")}
                if "folder_mtime" not in _have:
                    _c.execute("ALTER TABLE folders ADD COLUMN folder_mtime INTEGER DEFAULT 0")
        except Exception:
            pass
        fail_streak = 0        # ★ req-scanfix：必须在函数内初始化（模块级 += 会 UnboundLocalError）
        depth_limit = int(job["depth"]) or UNLIMITED_DEPTH   # 0 = 不限
        # 若把一个子目录当根来扫，必须沿用它在树里的真实深度，
        # 否则子目录会被重新编号成 depth=1（曾把 V/图 从 9 写成 1）
        base_depth = 0
        try:
            import sqlite3 as _sq
            _con = _sq.connect(str(getattr(self.db, "path", "/app/data/emby.db")))
            _row = _con.execute("SELECT depth FROM folders WHERE id=?",
                                (job["root_id"],)).fetchone()   # 注意：root_id 还没赋值
            _con.close()
            if _row and _row[0]:
                base_depth = int(_row[0])
        except Exception:
            base_depth = 0
        kinds = {k for k in (job["kinds"] or "").split(",") if k}
        root_id = job["root_id"]
        processed = 0

        try:
            while True:
                state = self.db.job(job_id)
                if state is None or state["status"] in (STATUS_PAUSED, STATUS_CANCELED):
                    log.info("任务 %s 停在状态 %s（可续跑）", job_id,
                             state["status"] if state else "gone")
                    return self.status(job_id)
                row = self.db.queue_next(job_id)
                if row is None:
                    self.db.set_job_status(job_id, STATUS_DONE)
                    log.info("任务 %s 完成：目录 %d、媒体 %d", job_id,
                             self.db.job(job_id)["dirs_done"],
                             self.db.job(job_id)["items_found"])
                    return self.status(job_id)

                cid, _rel_depth = row["dir_id"], int(row["depth"])
                depth = _rel_depth if _rel_depth else base_depth   # 根沿用真实深度
                try:
                    listing = await self._call_list(cid)
                except Exception as exc:  # noqa: BLE001
                    # ★ req-scanfix：**单个目录失败不该终止整个任务**。
                    #   原来这里是 `return`，一次上游异常就让任务结束、队列里剩下的目录
                    #   永久不扫（实测因此漏掉 1 个目录，用户点进去是空的）。
                    #   现在：记 scan_failed（带重试计数）、跳过这个目录、继续下一个。
                    #   连续失败过多才真的停（多半是 Cookie 失效 / 被风控）。
                    self._mark_dir_failed(cid, str(exc)[:200])
                    fail_streak += 1
                    log.warning("列目录失败 %s（连续 %d 次）：%s", cid, fail_streak, exc)
                    if fail_streak >= MAX_FAIL_STREAK:
                        self.db.set_job_status(
                            job_id, STATUS_ERROR,
                            f"连续 {fail_streak} 个目录列取失败，疑似 Cookie 失效或被风控：{exc}")
                        log.error("连续 %d 次列目录失败，停止任务", fail_streak)
                        return self.status(job_id)
                    self.db.queue_mark_done(job_id, cid)   # 标记为已处理，别卡在队列里
                    continue

                fail_streak = 0                       # req-scanfix：成功就清零
                folders = listing.get("folders") or []
                files = listing.get("files") or []

                # ---- 子目录：加密名字 + 同层排序秩 + 入队（不超过深度限制）
                folders_sorted = sorted(folders, key=lambda f: ic.normalize(f.get("name", "")))
                child_dirs: list[tuple[str, int]] = []
                for rank, f in enumerate(folders_sorted):
                    fid = str(f.get("id") or "")
                    if not fid:
                        continue
                    fields = ic.seal_fields(f.get("name", ""))
                    self.db.upsert_folder(
                        fid, cid, fields["name_enc"], fields["name_idx"],
                        fields["name_bucket"], depth=depth + 1, child_count=0)
                    # 同层排序秩直接写进 folders.name_rank
                    self._set_folder_rank(fid, rank)
                    # 顺手记录目录内容时间（快速增量靠它判断子树是否变化）
                    self._set_folder_mtime(fid, int(f.get("mtime") or 0))
                    if depth + 1 <= depth_limit:
                        child_dirs.append((fid, depth + 1))

                # ---- 文件：加密名字 + kind + 尺寸/时间
                rows = []
                files_sorted = sorted(files, key=lambda x: ic.normalize(x.get("name", "")))
                for rank, f in enumerate(files_sorted):
                    fid = str(f.get("id") or "")
                    name = f.get("name", "")
                    if not fid:
                        continue
                    ext = (f.get("ext") or (name.rsplit(".", 1)[-1] if "." in name else "")).lower()
                    fields = ic.seal_fields(name, "", name)
                    thumb_url = str(f.get("thumb") or "")
                    rows.append({
                        "id": fid, "parent_id": cid, "kind": kind_of(ext, kinds),
                        "ext": ext, "size": int(f.get("size") or 0),
                        "mtime": int(f.get("mtime") or 0),
                        "name_enc": fields["name_enc"], "name_idx": fields["name_idx"],
                        "name_bucket": fields["name_bucket"], "path_enc": fields["path_enc"],
                        "thumb_enc": ic.seal_link(thumb_url) if thumb_url else "",
                    })
                if rows:
                    self.db.upsert_items(rows)
                    self._set_items_rank([r["id"] for r in rows])
                    # 只登记到缩略图队列（pending），真正的抓取交给
                    # "看图时懒抓" 或 "夜间时间窗" —— 扫描阶段零额外上游请求
                    if self.thumb_store is not None:
                        try:
                            self.thumb_store.enqueue([(r["id"], "", 0) for r in rows])
                        except Exception as exc:  # noqa: BLE001
                            log.warning("缩略图入队失败（不影响扫描）：%s", exc)

                self.db.queue_mark_done(job_id, cid)
                self.db.queue_push(job_id, child_dirs)
                self.db.bump_job(job_id, dirs_done=1, items=len(rows), folders=len(folders_sorted))
                processed += 1
                log.info("扫描 %s：目录 %s 完成（%d 子目录 / %d 文件）", job_id, cid,
                         len(folders_sorted), len(rows))

                if max_dirs is not None and processed >= max_dirs:
                    log.info("到达 max_dirs=%d，主动停下（可续跑）", max_dirs)
                    return self.status(job_id)
        except asyncio.CancelledError:
            self.db.set_job_status(job_id, STATUS_PAUSED, "cancelled")
            raise
        except Exception as exc:                       # noqa: BLE001
            # 任何意外都要落到 error，绝不能让任务永远停在 running
            log.exception("扫描任务 %s 异常终止", job_id)
            self.db.set_job_status(job_id, STATUS_ERROR, f"{type(exc).__name__}: {exc}")
            return self.status(job_id)

    # ------------------------------------------------------------------ 内部
    def _mark_dir_failed(self, cid: str, err: str) -> None:
        """记一次目录列取失败（req-scanfix）。

        与 ``fast_scan.py`` 写同一张 ``scan_failed`` 表，语义一致：
        ``attempts`` 累加，``epoch`` 更新为最近一次失败时间。
        后台「扫描管理」的失败明细就是读这张表。
        """
        import time as _t
        try:
            with self.db._lock, self.db._db() as conn:      # noqa: SLF001
                conn.execute("""CREATE TABLE IF NOT EXISTS scan_failed (
                    dir_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL,
                    err TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1)""")
                conn.execute("""INSERT INTO scan_failed (dir_id, epoch, err, attempts)
                    VALUES (?,?,?,1) ON CONFLICT(dir_id) DO UPDATE SET
                    epoch=excluded.epoch, err=excluded.err,
                    attempts=scan_failed.attempts+1""",
                             (str(cid), int(_t.time()), str(err)[:300]))
        except Exception:                                   # noqa: BLE001
            pass

    def _set_folder_mtime(self, fid: str, mt: int) -> None:
        """记录目录的"内容时间"（t/te/tu/tp/to 的最大值），供快速增量判断是否变化。"""
        if not mt:
            return
        try:
            with self.db._lock, self.db._db() as conn:      # noqa: SLF001
                conn.execute("UPDATE folders SET folder_mtime=? WHERE id=?", (int(mt), fid))
        except Exception:
            pass

    def _set_folder_rank(self, fid: str, rank: int) -> None:
        with self.db._lock, self.db._db() as conn:      # noqa: SLF001 (同包内部使用)
            conn.execute("UPDATE folders SET name_rank=? WHERE id=?", (rank, fid))

    def _set_items_rank(self, ids: list[str]) -> None:
        with self.db._lock, self.db._db() as conn:      # noqa: SLF001
            conn.executemany("UPDATE items SET name_rank=? WHERE id=?",
                             [(i, pid) for i, pid in enumerate(ids)])

    async def _call_list(self, cid: str) -> dict:
        """唯一的出口：并发闸门 + 最小间隔 + 节奏休息。"""
        await self._throttle.acquire()
        try:
            self._throttle.note_call()
            return await self.list_dir(cid)
        finally:
            self._throttle.release()
            self._rest_counter += 1
            if (self.settings.rest_every > 0
                    and self._rest_counter % self.settings.rest_every == 0):
                self._throttle.note_rest()
                await asyncio.sleep(max(0.0, self.settings.rest_seconds))
