"""115emby 自己的数据模型（媒体库，不是相册）。

与"相册"的根本区别
------------------
| 相册（115-album） | 115emby（媒体库） |
|---|---|
| 主角是"照片"，围绕时间轴/EXIF/GPS/地图 | 主角是"文件夹 + 媒体项"，围绕层级浏览与播放 |
| `photos` 表带 taken/camera/lens/lat/lon/orientation | **没有**这些字段；只关心能不能播、播到哪 |
| 缩略图是为了"看墙" | 缩略图是为了"认出这是哪一集" |
| 播放是顺带的 | **播放是核心**：进度、续播、字幕、音轨 |

所以本模块**不依赖、不读取** 115-album 的 `index.db`；它完全是新库。

表
--
``folders``   目录树（按需展开，`parent_id` 指向父目录；`-1` 为根）
``items``     媒体项（视频/图片/音频；只存"能不能播、多大、多长"）
``links``     直链缓存（按 item 一行；加密存储；带过期时间与 UA）
``progress``  播放进度（按 item 一行：位置、时长、看完时间）
``kv``        杂项（游标、开关、统计）

设计约束
--------
* **名字/路径加密**（与 115-album 的 `secure_index` 同一套派生密钥，见 `app/index_crypto.py`）；
* **直链也加密**（URL 里带 UID/签名）；
* 不含任何 EXIF / GPS / 拍摄时间字段 —— 需要"按时间排"时用**文件 mtime**，那是网盘本来就有的；
* `kind`：``video`` / ``image`` / ``audio`` / ``other``，用来决定前端走播放器还是看图。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable
from . import hide_rules
from . import hidden_store
from . import index_crypto as ic

log = logging.getLogger("album.db")

# 小于这个字节数的图片不参与显示与搜索（二维码/占位小图；用户要求 30KB）
MIN_IMAGE_BYTES = int(os.getenv("MIN_IMAGE_BYTES") or 30000)


def visible_cond(alias: str = "", include_hidden: bool = False) -> str:
    """返回一段可直接拼进 WHERE 的条件（以 AND 开头）。

    规则：
    * ``kind=image`` 且 ``size < MIN_IMAGE_BYTES`` 不可见（二维码、1px 占位图之类）；
    * ``hidden=1`` 不可见（已排除：损坏文件/假视频/拿不到缩略图，见 ``hidden_store``）。

    所有"列给用户看 / 计数 / 搜索"的查询都要带上它，否则数量与界面会对不上。
    ``include_hidden=True`` 供"显示已排除"开关使用（只放行 hidden，小图规则照旧）。
    """
    a = (alias + ".") if alias else ""
    sql = (" AND NOT (%skind=? AND COALESCE(%ssize,0) < %d)"
           % (a, a, MIN_IMAGE_BYTES))
    if not include_hidden:
        sql += " AND COALESCE(%shidden,0)=0" % a
    return sql


KIND_VIDEO = "video"
KIND_IMAGE = "image"
KIND_AUDIO = "audio"
KIND_OTHER = "other"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id          TEXT PRIMARY KEY,          -- 115 的 cid（或 AList 路径哈希）
    parent_id   TEXT NOT NULL DEFAULT '-1',
    name_enc    TEXT DEFAULT '',           -- 目录名（密文）
    name_idx    TEXT DEFAULT '',           -- 盲索引（精确）
    name_bucket TEXT DEFAULT '',           -- 前缀桶
    name_rank   INTEGER,                   -- 同层按名字排序的秩
    depth       INTEGER DEFAULT 0,
    child_count INTEGER DEFAULT 0,
    scanned_at  INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    created     INTEGER DEFAULT 0,
    folder_mtime INTEGER DEFAULT 0           -- ★ 目录修改时间（增量扫描用；原先只在运行时 ALTER 补，
                                             --   全新部署访问 folder-previews/tree 会直接 500）
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id, name_rank);
CREATE INDEX IF NOT EXISTS idx_folders_idx ON folders(name_idx);

CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,          -- 115 的 file_id:pickcode
    parent_id   TEXT NOT NULL DEFAULT '-1',-- 所在目录
    name_enc    TEXT DEFAULT '',
    name_idx    TEXT DEFAULT '',
    name_bucket TEXT DEFAULT '',
    name_rank   INTEGER,
    path_enc    TEXT DEFAULT '',           -- 完整路径（密文，便于"最近添加"记忆）
    kind        TEXT DEFAULT 'other',
    ext         TEXT DEFAULT '',
    size        INTEGER DEFAULT 0,
    mtime       INTEGER DEFAULT 0,         -- 网盘修改时间（唯一的"时间"来源）
    duration    REAL DEFAULT 0,            -- 视频/音频时长（秒），抓帧时顺手取
    width       INTEGER DEFAULT 0,
    height      INTEGER DEFAULT 0,
    vcodec      TEXT DEFAULT '',           -- 决定"能不能直接播"
    acodec      TEXT DEFAULT '',
    has_thumb   INTEGER DEFAULT 0,
    thumb_enc   TEXT DEFAULT '',   -- 上游缩略图地址（密文，会过期）
    updated     INTEGER DEFAULT 0,
    created     INTEGER DEFAULT 0,
    hidden      INTEGER DEFAULT 0          -- ★ 1=已排除（损坏/无法生成缩略图），界面一律不显示
);
CREATE INDEX IF NOT EXISTS idx_items_parent ON items(parent_id, name_rank);
CREATE INDEX IF NOT EXISTS idx_items_kind_mtime ON items(kind, mtime DESC);
-- ★ mtime 索引：/api/library 默认按 mtime 排序。没有它时
-- SQLite 对 kind IN (...) + COALESCE(mtime,0) 会退化成"全表扫 + 临时 B 树排序"
-- （实测 120ms）；有它并显式 INDEXED BY 后是 0.25ms（取满 60 条即停）。
CREATE INDEX IF NOT EXISTS idx_items_mtime ON items(mtime DESC, id);
CREATE INDEX IF NOT EXISTS idx_items_idx ON items(name_idx);
CREATE INDEX IF NOT EXISTS idx_items_bucket ON items(name_bucket);

-- 直链缓存：按 item 一行，密文存储（URL 含 UID/签名）
CREATE TABLE IF NOT EXISTS links (
    item_id   TEXT PRIMARY KEY,
    url_enc   TEXT DEFAULT '',
    expires   INTEGER DEFAULT 0,
    ua        TEXT DEFAULT '',
    updated   INTEGER DEFAULT 0
);

-- 播放进度：这是 115emby 的核心表之一
CREATE TABLE IF NOT EXISTS progress (
    item_id     TEXT PRIMARY KEY,
    position    REAL DEFAULT 0,            -- 秒
    duration    REAL DEFAULT 0,
    finished    INTEGER DEFAULT 0,
    plays       INTEGER DEFAULT 0,
    last_play   INTEGER DEFAULT 0
);

-- 扫描任务：后台跑、可暂停/恢复/取消，进程重启后靠 scan_queue 续跑
CREATE TABLE IF NOT EXISTS scan_jobs (
    id            TEXT PRIMARY KEY,
    root_id       TEXT DEFAULT '-1',        -- 从哪个目录开始（115 的 cid；-1=根）
    root_name_enc TEXT DEFAULT '',
    depth         INTEGER DEFAULT 3,
    kinds         TEXT DEFAULT '',          -- 逗号分隔；空=全部
    status        TEXT DEFAULT 'pending',   -- pending/running/paused/done/canceled/error
    dirs_done     INTEGER DEFAULT 0,
    items_found   INTEGER DEFAULT 0,
    folders_found INTEGER DEFAULT 0,
    last_error    TEXT DEFAULT '',
    created       INTEGER DEFAULT 0,
    updated       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_queue (
    job_id  TEXT NOT NULL,
    dir_id  TEXT NOT NULL,
    depth   INTEGER DEFAULT 0,
    state   TEXT DEFAULT 'pending',         -- pending/done
    PRIMARY KEY (job_id, dir_id)
);
CREATE INDEX IF NOT EXISTS idx_scan_queue_state ON scan_queue(job_id, state);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- req-fav：我的收藏（一条目一行；added 用于"最近收藏在前"）
CREATE TABLE IF NOT EXISTS fav (
    item_id  TEXT PRIMARY KEY,
    kind     TEXT DEFAULT '',
    added    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fav_added ON fav(added DESC);
"""


def _now() -> int:
    return int(time.time())


class DB:
    """115emby 的元数据库。所有写操作串行；每次操作即用即关（Windows 下不会残留句柄）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init()

    # ------------------------------------------------------------------ 连接
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        # ★ 不在每个连接上设 journal_mode：它是**写操作**（要拿写锁），
        #   扫描期每目录多次建连 → database is locked 的潜在来源。
        #   库级 WAL 在 init 时设一次即可；这里只留连接级 PRAGMA。
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # 老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段
    _EXTRA_COLUMNS = {
        # ★ 新增列必须**同时**写进 _SCHEMA 与这里：只靠运行时 ALTER 会让"全新部署"炸
        "items": [("thumb_enc", "TEXT DEFAULT ''"),
                  ("hidden", "INTEGER DEFAULT 0")],
        "folders": [],
        "links": [],
        "progress": [],
        "scan_jobs": [],
        "scan_queue": [],
    }

    def _migrate(self, conn) -> None:
        for table, cols in self._EXTRA_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue
            for name, decl in cols:
                if name not in have:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    except sqlite3.OperationalError:
                        pass

    def init(self) -> None:
        with self._lock, self._db() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    # ------------------------------------------------------------------ 写入
    def upsert_folder(self, cid: str, parent_id: str, name_enc: str, name_idx: str,
                      name_bucket: str, *, depth: int = 0, child_count: int = 0) -> None:
        now = _now()
        with self._lock, self._db() as conn:
            conn.execute(
                """INSERT INTO folders (id, parent_id, name_enc, name_idx, name_bucket,
                                        depth, child_count, scanned_at, updated, created)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     parent_id=excluded.parent_id, name_enc=excluded.name_enc,
                     name_idx=excluded.name_idx, name_bucket=excluded.name_bucket,
                     depth=excluded.depth, child_count=excluded.child_count,
                     scanned_at=excluded.scanned_at, updated=excluded.updated""",
                (cid, parent_id, name_enc, name_idx, name_bucket, depth, child_count,
                 now, now, now),
            )

    def upsert_items(self, rows: Iterable[dict]) -> int:
        """批量写入媒体项（调用方负责先把名字加密好）。"""
        payload = []
        now = _now()
        for r in rows:
            # 只收图片和视频；再按黑名单丢掉"引流小视频"（见 hide_rules.py）
            _kind = r.get("kind", KIND_OTHER)
            if _kind not in (KIND_IMAGE, KIND_VIDEO):
                continue
            _name = ""
            try:
                _name = ic.safe_unseal(r.get("name_enc") or "")
            except Exception:                              # noqa: BLE001
                _name = ""
            if hide_rules.is_blocked(_name, _kind, int(r.get("size") or 0)):
                continue
            # ★ 已排除条目（损坏/拿不到缩略图）：重扫时按名字+体积重新标 hidden，
            #   这样即使条目被清库重建，也不会又冒出来（见 hidden_store.py）
            _hid = 1 if hidden_store.is_hidden(r.get("id", ""), _name,
                                               int(r.get("size") or 0)) else 0
            payload.append((
                r["id"], r.get("parent_id", "-1"), r.get("name_enc", ""),
                r.get("name_idx", ""), r.get("name_bucket", ""), r.get("path_enc", ""),
                r.get("kind", KIND_OTHER), r.get("ext", ""), int(r.get("size") or 0),
                int(r.get("mtime") or 0), float(r.get("duration") or 0),
                int(r.get("width") or 0), int(r.get("height") or 0),
                r.get("vcodec", ""), r.get("acodec", ""), r.get("thumb_enc", ""),
                _hid, now, now,
            ))
        if not payload:
            return 0
        with self._lock, self._db() as conn:
            conn.executemany(
                """INSERT INTO items (id, parent_id, name_enc, name_idx, name_bucket, path_enc,
                                      kind, ext, size, mtime, duration, width, height,
                                      vcodec, acodec, thumb_enc, hidden, updated, created)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     parent_id=excluded.parent_id, name_enc=excluded.name_enc,
                     name_idx=excluded.name_idx, name_bucket=excluded.name_bucket,
                     path_enc=excluded.path_enc, kind=excluded.kind, ext=excluded.ext,
                     size=excluded.size, mtime=excluded.mtime, duration=excluded.duration,
                     width=excluded.width, height=excluded.height,
                     vcodec=excluded.vcodec, acodec=excluded.acodec,
                     thumb_enc=CASE WHEN excluded.thumb_enc<>'' THEN excluded.thumb_enc
                                    ELSE items.thumb_enc END,
                     -- ★ 一旦排除就保持排除（MAX 让已有 1 不会被重扫清成 0）
                     hidden=MAX(COALESCE(items.hidden,0), COALESCE(excluded.hidden,0)),
                     updated=excluded.updated""",
                payload,
            )
        return len(payload)

    def count_children(self, parent_id: str, *, kind: str | None = None,
                       exclude_gif: bool = False, show_hidden: bool = False) -> int:
        """某目录下的条目数（走 idx_items_parent，很快）。用于"本目录图片 120/484 项"这种显示。"""
        # 这个前台只展示图片和视频：音频/压缩包/文档之类一律不列（用户要求）
        sql = ("SELECT COUNT(*) c FROM items INDEXED BY idx_items_parent "
               "WHERE parent_id=? AND kind IN (?,?)")
        args: list[Any] = [parent_id, KIND_IMAGE, KIND_VIDEO]
        sql += visible_cond(include_hidden=show_hidden)
        args.append(KIND_IMAGE)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if exclude_gif:
            sql += " AND lower(COALESCE(ext,'')) <> 'gif'"
        with self._lock, self._db() as conn:
            row = conn.execute(sql, args).fetchone()
        return int((row["c"] if row else 0) or 0)

    def set_media_meta(self, item_id: str, meta: dict) -> None:
        """把 ffprobe 探到的编码/分辨率/时长补写进 items（不清空已有值）。"""
        if not item_id or not meta:
            return
        sets, args = [], []
        if meta.get("vcodec"):
            sets.append("vcodec=?")
            args.append(str(meta["vcodec"]))
        if meta.get("acodec"):
            sets.append("acodec=?")
            args.append(str(meta["acodec"]))
        if int(meta.get("width") or 0):
            sets.append("width=?")
            args.append(int(meta["width"]))
        if int(meta.get("height") or 0):
            sets.append("height=?")
            args.append(int(meta["height"]))
        if float(meta.get("duration") or 0):
            sets.append("duration=?")
            args.append(float(meta["duration"]))
        if not sets:
            return
        sets.append("updated=?")
        args.append(_now())
        args.append(item_id)
        with self._lock, self._db() as conn:
            conn.execute("UPDATE items SET %s WHERE id=?" % ", ".join(sets), args)

    def items_missing_meta(self, limit: int = 200) -> list:
        """还没探测过编码的视频（用于夜间批量补标）。"""
        with self._lock, self._db() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT id, ext, size FROM items WHERE kind=? "
                "AND COALESCE(vcodec,'')='' ORDER BY COALESCE(mtime,0) DESC LIMIT ?",
                (KIND_VIDEO, int(limit)))]

    def set_link(self, item_id: str, url_enc: str, expires: int, ua: str = "") -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                """INSERT INTO links (item_id, url_enc, expires, ua, updated) VALUES (?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET url_enc=excluded.url_enc,
                     expires=excluded.expires, ua=excluded.ua, updated=excluded.updated""",
                (item_id, url_enc, int(expires), ua, _now()),
            )

    def get_link(self, item_id: str) -> sqlite3.Row | None:
        with self._lock, self._db() as conn:
            return conn.execute("SELECT * FROM links WHERE item_id=?", (item_id,)).fetchone()

    # ------------------------------------------------------------ req-fav 我的收藏
    def set_fav(self, item_id: str, on: bool, kind: str = "") -> bool:
        """加/取消收藏，返回改动后的状态。"""
        with self._lock, self._db() as conn:
            if on:
                conn.execute(
                    """INSERT INTO fav (item_id, kind, added) VALUES (?,?,?)
                       ON CONFLICT(item_id) DO NOTHING""",
                    (item_id, kind or "", _now()),
                )
            else:
                conn.execute("DELETE FROM fav WHERE item_id=?", (item_id,))
        return bool(on)

    def is_fav(self, item_id: str) -> bool:
        with self._lock, self._db() as conn:
            return conn.execute("SELECT 1 FROM fav WHERE item_id=?",
                                (item_id,)).fetchone() is not None

    def fav_count(self) -> int:
        with self._lock, self._db() as conn:
            return int(conn.execute("SELECT COUNT(*) c FROM fav").fetchone()["c"] or 0)

    def fav_ids(self) -> set:
        """只要 id 集合（列表标星用）。"""
        with self._lock, self._db() as conn:
            return {r["item_id"] for r in conn.execute("SELECT item_id FROM fav")}

    def save_progress(self, item_id: str, position: float, duration: float) -> None:
        finished = 1 if duration and position >= duration * 0.95 else 0
        with self._lock, self._db() as conn:
            conn.execute(
                """INSERT INTO progress (item_id, position, duration, finished, plays, last_play)
                   VALUES (?,?,?,?,1,?)
                   ON CONFLICT(item_id) DO UPDATE SET
                     position=excluded.position, duration=excluded.duration,
                     finished=excluded.finished, plays=progress.plays+1,
                     last_play=excluded.last_play""",
                (item_id, float(position), float(duration), finished, _now()),
            )

    # ------------------------------------------------------------------ 扫描任务
    def create_job(self, job_id: str, root_id: str, root_name_enc: str, depth: int,
                   kinds: str = "") -> None:
        now = _now()
        with self._lock, self._db() as conn:
            conn.execute(
                """INSERT INTO scan_jobs (id, root_id, root_name_enc, depth, kinds, status,
                                          created, updated) VALUES (?,?,?,?,?, 'pending', ?,?)""",
                (job_id, root_id, root_name_enc, int(depth), kinds, now, now))
            conn.execute(
                "INSERT OR IGNORE INTO scan_queue (job_id, dir_id, depth, state)"
                " VALUES (?,?,0,'pending')", (job_id, root_id))

    def job(self, job_id: str) -> sqlite3.Row | None:
        with self._lock, self._db() as conn:
            return conn.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()

    def jobs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock, self._db() as conn:
            return list(conn.execute(
                "SELECT * FROM scan_jobs ORDER BY created DESC LIMIT ?", (int(limit),)))

    def active_job(self) -> sqlite3.Row | None:
        with self._lock, self._db() as conn:
            return conn.execute(
                "SELECT * FROM scan_jobs WHERE status IN ('pending','running','paused')"
                " ORDER BY created DESC LIMIT 1").fetchone()

    def reset_running_jobs(self) -> int:
        """进程重启后，之前"running"的任务其实已经没了 —— 一律置为 paused，可续跑。"""
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE scan_jobs SET status='paused', last_error='进程重启，已暂停（可续跑）',"
                " updated=? WHERE status='running'", (int(time.time()),))
            return cur.rowcount

    def set_job_status(self, job_id: str, status: str, error: str = "") -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE scan_jobs SET status=?, last_error=?, updated=? WHERE id=?",
                (status, (error or "")[:300], _now(), job_id))

    def bump_job(self, job_id: str, *, dirs_done: int = 0, items: int = 0,
                 folders: int = 0) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                """UPDATE scan_jobs SET dirs_done=dirs_done+?, items_found=items_found+?,
                       folders_found=folders_found+?, updated=? WHERE id=?""",
                (dirs_done, items, folders, _now(), job_id))

    def queue_next(self, job_id: str) -> sqlite3.Row | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM scan_queue WHERE job_id=? AND state='pending'"
                " ORDER BY depth ASC, rowid ASC LIMIT 1", (job_id,)).fetchone()
        return row

    def queue_mark_done(self, job_id: str, dir_id: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE scan_queue SET state='done' WHERE job_id=? AND dir_id=?",
                (job_id, dir_id))

    def queue_push(self, job_id: str, dirs: Iterable[tuple[str, int]]) -> int:
        rows = [(job_id, d, int(depth)) for d, depth in dirs]
        if not rows:
            return 0
        with self._lock, self._db() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO scan_queue (job_id, dir_id, depth, state)"
                " VALUES (?,?,?,'pending')", rows)
            return conn.total_changes - before

    def queue_size(self, job_id: str) -> dict:
        with self._lock, self._db() as conn:
            row = conn.execute(
                """SELECT SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
                          SUM(CASE WHEN state='done'    THEN 1 ELSE 0 END) AS done
                   FROM scan_queue WHERE job_id=?""", (job_id,)).fetchone()
        return {"pending": int(row["pending"] or 0), "done": int(row["done"] or 0)}

    # ------------------------------------------------------------------ 读取
    def list_children(self, parent_id: str, *, kind: str | None = None,
                      limit: int = 200, offset: int = 0, order: str = "name",
                      exclude_gif: bool = False, show_hidden: bool = False) -> list[sqlite3.Row]:
        # INDEXED BY：kind 等值条件会让 SQLite 误选 idx_items_kind_mtime 全库扫
        # （同一坑在 /api/discover 里实测 2.4s），这里固定走 parent 索引。
        sql = ("SELECT * FROM items INDEXED BY idx_items_parent "
               "WHERE parent_id=? AND kind IN (?,?)")
        args: list[Any] = [parent_id, KIND_IMAGE, KIND_VIDEO]
        sql += visible_cond(include_hidden=show_hidden)
        args.append(KIND_IMAGE)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if exclude_gif:
            sql += " AND lower(COALESCE(ext,'')) <> 'gif'"
        ordering = {"name": "COALESCE(name_rank, 1<<30), id",
                    "mtime": "COALESCE(mtime,0) DESC, id",
                    "size": "COALESCE(size,0) DESC, id"}
        sql += " ORDER BY " + ordering.get((order or "name").lower(), ordering["name"]) + " LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        with self._lock, self._db() as conn:
            return list(conn.execute(sql, args))

    def list_folders(self, parent_id: str) -> list[sqlite3.Row]:
        with self._lock, self._db() as conn:
            return list(conn.execute(
                "SELECT * FROM folders WHERE parent_id=? ORDER BY COALESCE(name_rank, 1<<30)",
                (parent_id,)))

    def get_item(self, item_id: str) -> sqlite3.Row | None:
        with self._lock, self._db() as conn:
            return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def get_parent_of(self, item_id: str) -> str:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT parent_id FROM items WHERE id=?", (item_id,)).fetchone()
        return (row["parent_id"] if row else "-1") or "-1"

    def get_progress(self, item_id: str) -> sqlite3.Row | None:
        with self._lock, self._db() as conn:
            return conn.execute("SELECT * FROM progress WHERE item_id=?", (item_id,)).fetchone()

    def folder(self, folder_id: str) -> sqlite3.Row | None:
        with self._lock, self._db() as conn:
            return conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()

    def ancestors(self, folder_id: str, limit: int = 20) -> list[dict]:
        """从根到当前目录的链（用于面包屑）。中途断掉就返回已解出的部分。"""
        from . import index_crypto as _ic

        chain: list[dict] = []
        cur = self.folder(folder_id) if folder_id not in ("-1", "", None) else None
        hops = 0
        while cur is not None and hops < limit:
            chain.append({"id": cur["id"], "name": _ic.safe_unseal(cur["name_enc"] or "")})
            parent = cur["parent_id"] or "-1"
            cur = self.folder(parent) if parent not in ("-1", "", None) else None
            hops += 1
        chain.reverse()
        return chain

    def continue_watching(self, limit: int = 20) -> list[sqlite3.Row]:
        """Emby 式"继续观看"：有进度、没看完、按最近播放排序。"""
        with self._lock, self._db() as conn:
            return list(conn.execute(
                """SELECT i.*, p.position, p.duration AS p_duration, p.last_play
                   FROM progress p JOIN items i ON i.id = p.item_id
                   WHERE p.finished=0 AND p.position > 30
                   ORDER BY p.last_play DESC LIMIT ?""", (int(limit),)))

    def stats(self) -> dict:
        # ★ 缓存 20 秒：这个统计要在 items(14 万行) 上做 4 次 COUNT，
        # 而 /api/health 会被管理台轮询 —— 不缓存会把单 worker 占满（实测首屏 18.6 秒 → 3.8 秒）。
        import time as _t
        _c = getattr(self, "_stats_cache", None)
        if _c and _t.time() - _c[0] < 20:
            return dict(_c[1])
        with self._lock, self._db() as conn:
            row = conn.execute(
                """SELECT
                     (SELECT COUNT(*) FROM items)                AS items,
                     (SELECT COUNT(*) FROM items WHERE kind='video') AS videos,
                     (SELECT COUNT(*) FROM items WHERE kind='image') AS images,
                     (SELECT COALESCE(SUM(size),0) FROM items
                       WHERE kind IN ('image','video'))           AS media_bytes,
                     (SELECT COUNT(*) FROM folders)              AS folders,
                     (SELECT COUNT(*) FROM links)                AS links,
                     (SELECT COUNT(*) FROM progress)             AS progress_rows"""
            ).fetchone()
        out = dict(row)
        out["db_mb"] = round(self.path.stat().st_size / 1048576, 2) if self.path.exists() else 0.0
        self._stats_cache = (_t.time(), dict(out))
        return out
