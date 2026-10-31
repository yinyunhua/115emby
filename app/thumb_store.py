"""缩略图持久层（C 方案存储）：加密 BLOB + 抓取状态 + 断点续传游标。

为什么单独一个库文件
--------------------
* 主索引 ``data/index.db`` 是"目录列表"的产物，天天写；
  缩略图库 ``data/thumbs.db`` 是"逐张抓取"的产物，写得更碎。
  分开之后：备份、VACUUM、换密钥、单独丢弃重抓，都互不影响。
* 将来若把主库换成 SQLCipher，这个库可以独立处理（同样的信封加密已经做了）。

表
--
``thumbs``      pid -> 加密后的缩略图字节（+ etag/宽高/命中次数）
``thumb_src``   pid -> 抓取队列状态（pending/done/skipped/failed）+ 重试信息
``kv``          断点续传游标、夜间预热开关、统计快照

状态机
------
``pending`` → ``done``    抓到并入库（终态）
``pending`` → ``skipped`` 不该抓（上游没有封面 / 体积超限 / 非图片 / 占位图）（终态，可人工重置）
``pending`` → ``failed``  网络或解析失败；超过 ``max_attempts`` 后不再自动重试
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("album.thumbstore")

STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_SKIPPED = "skipped"
# ★ 已排除（确认"无法正常显示"后转入；界面按"已自动排除"统计）
STATE_EXCLUDED = "excluded"
STATE_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbs (
    pid      TEXT PRIMARY KEY,
    etag     TEXT NOT NULL,
    w        INTEGER DEFAULT 0,
    h        INTEGER DEFAULT 0,
    nbytes   INTEGER DEFAULT 0,     -- 加密后体积（占磁盘口径）
    pbytes   INTEGER DEFAULT 0,     -- 明文字节数（排查用）
    encaps   INTEGER DEFAULT 0,     -- 算法编号
    blob     BLOB NOT NULL,         -- 信封加密后的缩略图
    created  INTEGER DEFAULT 0,
    hits     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_thumbs_etag ON thumbs(etag);
-- ★ 覆盖索引：thumbs 表把加密 blob 内联存着（现在 850MB+），
-- 统计接口的 COUNT(*)/SUM(nbytes)/SUM(pbytes)/SUM(hits) 走全表扫描时要
-- **把 850MB blob 全读一遍**（实测 8.265 秒），直接拖垮 /api/health 与
-- 容器 healthcheck（timeout 8s → 判 unhealthy）。有了这个索引只读索引页，
-- 同样的统计降到毫秒级。
CREATE INDEX IF NOT EXISTS idx_thumbs_stat ON thumbs(nbytes, pbytes, hits);

CREATE TABLE IF NOT EXISTS thumb_src (
    pid        TEXT PRIMARY KEY,
    url        TEXT DEFAULT '',
    url_exp    INTEGER DEFAULT 0,
    state      TEXT NOT NULL DEFAULT 'pending',
    attempts   INTEGER DEFAULT 0,
    last_err   TEXT DEFAULT '',
    updated    INTEGER DEFAULT 0,
    created    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_thumb_src_state ON thumb_src(state, updated);
CREATE INDEX IF NOT EXISTS idx_thumb_src_updated ON thumb_src(updated);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> int:
    return int(time.time())


class ThumbStore:
    """加密缩略图库。所有写操作串行（SQLite 单写者）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init()

    # ------------------------------------------------------------- 连接/建表
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def _db(self):
        """打开→事务→**关闭**。Windows 下不显式 close 会残留句柄（删库/换库会失败）。"""
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def init(self) -> None:
        with self._lock, self._db() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------- 缩略图读写
    def put(self, pid: str, plaintext: bytes, *, etag: str, w: int = 0, h: int = 0,
            encaps: int = 0) -> int:
        """写入/覆盖一张缩略图，返回加密后字节数。"""
        from . import secretbox

        blob = secretbox.encrypt(plaintext)
        now = _now()
        with self._lock, self._db() as conn:
            conn.execute(
                """INSERT INTO thumbs (pid, etag, w, h, nbytes, pbytes, encaps, blob, created, hits)
                   VALUES (?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(pid) DO UPDATE SET
                     etag=excluded.etag, w=excluded.w, h=excluded.h,
                     nbytes=excluded.nbytes, pbytes=excluded.pbytes,
                     encaps=excluded.encaps, blob=excluded.blob, created=excluded.created""",
                (pid, etag, int(w), int(h), len(blob), len(plaintext), int(encaps), blob, now),
            )
        return len(blob)

    def get(self, pid: str) -> tuple[bytes, str] | None:
        """读一张缩略图（解密），返回 (明文字节, etag)；不存在返回 None。"""
        from . import secretbox

        with self._lock, self._db() as conn:
            row = conn.execute("SELECT blob, etag FROM thumbs WHERE pid=?", (pid,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE thumbs SET hits=hits+1 WHERE pid=?", (pid,))
        return secretbox.decrypt(row["blob"]), row["etag"]

    def get_meta(self, pid: str) -> dict | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT pid, etag, w, h, nbytes, pbytes, encaps, created, hits"
                " FROM thumbs WHERE pid=?", (pid,)
            ).fetchone()
        return dict(row) if row else None

    def has(self, pid: str) -> bool:
        with self._lock, self._db() as conn:
            return conn.execute("SELECT 1 FROM thumbs WHERE pid=?", (pid,)).fetchone() is not None

    def delete(self, pid: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute("DELETE FROM thumbs WHERE pid=?", (pid,))

    def purge_all(self) -> int:
        """丢弃全部缩略图（保留队列状态），返回删除条数。"""
        with self._lock, self._db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM thumbs").fetchone()["c"]
            conn.execute("DELETE FROM thumbs")
        return int(n)

    # ------------------------------------------------------------- 抓取队列
    def enqueue(self, items: Iterable[tuple[str, str, int]]) -> int:
        """把 (pid, thumb_url, url_exp) 放进队列；已存在的（任何状态）不覆盖。"""
        rows = [(pid, url or "", int(exp or 0), STATE_PENDING, 0, "", _now(), _now())
                for pid, url, exp in items]
        if not rows:
            return 0
        with self._lock, self._db() as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT INTO thumb_src (pid, url, url_exp, state, attempts, last_err, updated, created)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(pid) DO NOTHING""",
                rows,
            )
            return conn.total_changes - before

    def enqueue_eligible(self, items: Iterable[tuple[str, str, int]],
                         *, emby_db: str = "", gif_skip: bool = True,
                         min_bytes: int = 30000) -> int:
        """★ req-enqueuefilter：只把**有可能生成缩略图**的条目放进队列。

        与 ``thumb_fetch`` 处理时的跳过规则保持一致，避免把注定 skipped 的条目
        也算进「待生成」：
          · ``gif_skip`` 为真时排除 GIF（GIF 不进缩略图队列是既有需求）
          · 图片小于 ``min_bytes`` 排除（太小无意义，处理时也会被跳过）
          · 非 image/video 排除

        参数 ``items`` 是 ``(pid, url, exp)``；要过滤就必须知道条目的
        kind/ext/size，因此从 ``emby_db`` 里读（读不到时退回"全部入队"）。
        """
        items = list(items)
        if not items:
            return 0
        info: dict[str, tuple[str, str, int]] = {}
        db = emby_db or os.getenv("EMBY_DB") or "/app/data/emby.db"
        try:
            con = sqlite3.connect(db, timeout=30)
            try:
                con.row_factory = sqlite3.Row
                pids = [it[0] for it in items]
                for i in range(0, len(pids), 500):
                    chunk = pids[i:i + 500]
                    q = ",".join("?" for _ in chunk)
                    for r in con.execute(
                        "SELECT id, kind, ext, size FROM items WHERE id IN (%s)" % q, chunk
                    ):
                        info[r["id"]] = (str(r["kind"] or ""),
                                         str(r["ext"] or "").lower(),
                                         int(r["size"] or 0))
            finally:
                con.close()
        except Exception:                              # noqa: BLE001
            return self.enqueue(items)                 # 读不到就不过滤，保持旧行为
        keep = []
        drop: list[tuple[str, str]] = []
        for pid, url, exp in items:
            meta = info.get(pid)
            if meta is None:
                continue                               # items 里没有 → 不该入队
            kind, ext, size = meta
            # ★ req-neverq：被排除的**要建行记成 excluded**，不能静默跳过 ——
            #   不建行会让它们混进「从未入队」，把「等待生成」撑成"永远抓不到"的数。
            if kind not in ("image", "video"):
                drop.append((pid, "按设计排除：非图片/视频"))
                continue
            if ext == "gif" and gif_skip:
                drop.append((pid, "按设计排除：GIF"))
                continue
            if kind == "image" and size and size < min_bytes:
                drop.append((pid, "按设计排除：图片<%dKB" % (min_bytes // 1000)))
                continue
            keep.append((pid, url, exp))
        if drop:
            self.mark_excluded_many(drop)
        return self.enqueue(keep)

    def mark_excluded_many(self, pairs: Iterable[tuple[str, str]]) -> int:
        """把"按设计排除"的条目**批量记成 excluded**（pid, 原因）。

        ★ req-neverq：``enqueue_eligible`` 原来对 GIF / 小图直接 ``continue``，
        **不建行** → 这些条目在 ``items`` 里存在、``thumb_src`` 里没有，
        于是 ``_count_never_queued`` 把它们当成"从未入队"，
        让「等待生成」显示一堆**永远抓不到**的条目（实测 2,385 条全是 GIF/小图）。

        ⛔ 用 ``INSERT OR IGNORE``：**绝不覆盖**已有的 done/pending/failed 行。
        """
        rows = [(pid, "", 0, STATE_EXCLUDED, 0, err, _now(), _now())
                for pid, err in pairs if pid]
        if not rows:
            return 0
        with self._lock, self._db() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO thumb_src
                   (pid, url, url_exp, state, attempts, last_err, updated, created)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return len(rows)

    def reset_state(self, states: Sequence[str] = (STATE_FAILED, STATE_SKIPPED)) -> int:
        """把指定状态重新置为 pending（人工重试用）。"""
        with self._lock, self._db() as conn:
            q = ",".join("?" for _ in states)
            cur = conn.execute(
                f"UPDATE thumb_src SET state=?, attempts=0, last_err='', updated=?"
                f" WHERE state IN ({q})",
                (STATE_PENDING, _now(), *states),
            )
            return cur.rowcount

    def next_batch(self, limit: int, *, retry_backoff_s: int = 6 * 3600,
                   max_attempts: int = 3) -> list[sqlite3.Row]:
        """取下一批待抓：pending 优先，再取"该重试"的 failed。

        用 ``updated`` 做软游标，天然支持断点续传（进程重启后从最久未动的记录继续）。
        """
        now = _now()
        with self._lock, self._db() as conn:
            rows = conn.execute(
                """SELECT pid, url, url_exp, attempts FROM thumb_src
                   WHERE state='pending'
                   ORDER BY updated ASC, pid ASC
                   LIMIT ?""",
                (int(limit),),
            ).fetchall()
            if len(rows) >= limit:
                return rows
            more = conn.execute(
                """SELECT pid, url, url_exp, attempts FROM thumb_src
                   WHERE state='failed' AND attempts < ? AND updated <= ?
                   ORDER BY updated ASC, pid ASC
                   LIMIT ?""",
                (int(max_attempts), now - int(retry_backoff_s), int(limit) - len(rows)),
            ).fetchall()
        return list(rows) + list(more)

    def has_work(self, *, retry_backoff_s: int = 6 * 3600,
                 max_attempts: int = 3) -> bool:
        """★ req-prewarmidle：**只判断**有没有待抓的（给守护循环用）。

        队列抓空后没必要每分钟起一次 run_window —— 实测每晚 ~480 次空转，
        每次都要写一次 kv + 打一行日志。这里用一条 LIMIT 1 的索引查询代替。
        判定口径必须与 ``next_batch`` 一致，否则会出现"说有活却取不到"。
        """
        now = _now()
        with self._lock, self._db() as conn:
            if conn.execute(
                    "SELECT 1 FROM thumb_src WHERE state='pending' LIMIT 1").fetchone():
                return True
            return conn.execute(
                """SELECT 1 FROM thumb_src
                   WHERE state='failed' AND attempts < ? AND updated <= ?
                   LIMIT 1""",
                (int(max_attempts), now - int(retry_backoff_s))).fetchone() is not None

    def state_of(self, pid: str) -> str:
        """★ req-lazydone：取某个 pid 当前的队列状态（没有记录返回空串）。

        懒抓命中缓存时要用它判断"是否还停在 pending"，只在该写的时候写。
        """
        with self._lock, self._db() as conn:
            r = conn.execute("SELECT state FROM thumb_src WHERE pid=?", (pid,)).fetchone()
        return str(r["state"]) if r else ""

    def mark(self, pid: str, state: str, err: str = "", *, bump_attempt: bool = False) -> None:
        """记录抓取状态；行不存在时**顺手建行**（懒抓的条目不在队列里也要能记账）。"""
        now = _now()
        with self._lock, self._db() as conn:
            conn.execute(
                """INSERT INTO thumb_src (pid, url, url_exp, state, attempts, last_err, updated, created)
                   VALUES (?, '', 0, ?, ?, ?, ?, ?)
                   ON CONFLICT(pid) DO UPDATE SET
                     state=excluded.state,
                     last_err=excluded.last_err,
                     updated=excluded.updated,
                     attempts=thumb_src.attempts + ?""",
                (pid, state, 1 if bump_attempt else 0, (err or "")[:300], now, now,
                 1 if bump_attempt else 0),
            )

    # ------------------------------------------------------------- kv / 统计
    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def count_excluded(self) -> int:
        """skipped/failed 里属于"界面不显示"的条目数（<30KB 图片 / GIF / 非图非视频）。

        thumbs.db 和 emby.db 同在 data/ 下，用 ATTACH 关联一次算清。

        ★ 缓存 30 秒：这个 ATTACH + JOIN 要扫 thumb_src(14 万行)，
        而 /api/health 与管理台的"队列状态"都会调它 —— 不缓存会把应用拖慢
        （实测管理台一开，切页会长时间停在"读取中"）。
        """
        import time as _t
        _cache = getattr(self, "_excl_cache", None)
        if _cache and _t.time() - _cache[0] < 30:
            return int(_cache[1])
        try:
            emby = self.path.parent / "emby.db"
            if not emby.exists():
                return 0
            with self._db() as conn:
                conn.execute("ATTACH DATABASE ? AS edb", (str(emby),))
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM thumb_src t "
                        "LEFT JOIN edb.items i ON i.id = t.pid "
                        "WHERE t.state IN ('skipped','failed') AND ("
                        "  i.id IS NULL"
                        # 小于 30KB 的一律算"排除"：图片是二维码/占位图，
                        # 视频是空壳（size=0 的 mov/mp4 根本抓不到帧，也没法播）
                        "  OR COALESCE(i.size,0) < 30000"
                        "  OR lower(COALESCE(i.ext,''))='gif'"
                        "  OR i.kind NOT IN ('image','video'))"
                        # ★ req-numfix2：已隐藏的条目界面不显示，不该计入「已自动排除」，
                        #   否则 已存+待生成+已排除 会比「库内媒体总数」多出这些行。
                        "  AND COALESCE(i.hidden,0)=0"
                    ).fetchone()
                    n = int((row["n"] if row else 0) or 0)
                    # ★ 已转入 excluded 状态的一并计入"已自动排除"
                    # ★ req-numfix2：原来是不看 items 的裸 COUNT(*)，
                    #   把 hidden 条目的行也算进来了（实测 37 个）。
                    # ★ req-thumbclean：必须 **LEFT JOIN**。
                    #   有些 excluded 行在 items 表里已经不存在（被清出/人工排除），
                    #   内 JOIN 会把它们直接丢掉（实测少 37 条）。
                    #   口径 = 「items 里没有该行」或「该行未隐藏」都算排除。
                    row2 = conn.execute(
                        "SELECT COUNT(*) AS n FROM thumb_src t "
                        "LEFT JOIN edb.items i ON i.id = t.pid "
                        "WHERE t.state='excluded' "
                        "AND (i.id IS NULL OR COALESCE(i.hidden,0)=0)"
                    ).fetchone()
                    n += int((row2["n"] if row2 else 0) or 0)
                    self._excl_cache = (_t.time(), n)
                    return n
                finally:
                    try:
                        conn.execute("DETACH DATABASE edb")
                    except Exception:                      # noqa: BLE001
                        pass
        except Exception as _e:                            # noqa: BLE001
            # ⛔ 附五十二 铁律：不要静默给 0（会把真错误藏住）
            import logging
            logging.getLogger("album.thumbstore").warning(
                "count_excluded 失败：%s: %s", type(_e).__name__, _e)
            return 0

    def count_state(self) -> dict[str, int]:
        # 缓存 20 秒（GROUP BY 扫 thumb_src 14 万行）
        import time as _t
        _cache = getattr(self, "_state_cache", None)
        if _cache and _t.time() - _cache[0] < 20:
            return dict(_cache[1])
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS c FROM thumb_src GROUP BY state"
            ).fetchall()
        out = {r["state"]: int(r["c"]) for r in rows}
        self._state_cache = (_t.time(), dict(out))
        return out

    def stats(self) -> dict[str, Any]:
        # 缓存 20 秒（COUNT/SUM 扫 thumbs 表）
        import time as _t
        _cache = getattr(self, "_stats_cache", None)
        if _cache and _t.time() - _cache[0] < 20:
            return dict(_cache[1])
        with self._lock, self._db() as conn:
            t = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(nbytes),0) AS nb,"
                " COALESCE(SUM(pbytes),0) AS pb, COALESCE(SUM(hits),0) AS hits FROM thumbs"
            ).fetchone()
        out: dict[str, Any] = {
            "thumbs": int(t["n"]),
            "disk_mb": round((t["nb"] or 0) / 1048576, 2),
            "plain_mb": round((t["pb"] or 0) / 1048576, 2),
            "hits": int(t["hits"] or 0),
            "queue": self.count_state(),
            # 失败/跳过里有多少是"界面根本不会显示"的（<30KB 小图、GIF、非媒体）——
            # 这些是规则排除，不该混进"失败"里吓人。
            "queue_excluded": self.count_excluded(),
            "cursor": self.kv_get("prewarm_cursor", ""),
            "paused": self.kv_get("prewarm_paused", "0") == "1",
            "db_mb": round(self.path.stat().st_size / 1048576, 2) if self.path.exists() else 0.0,
        }
        self._stats_cache = (_t.time(), dict(out))
        return out
