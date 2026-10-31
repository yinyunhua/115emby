"""前台新界面（/browse2）需要的聚合接口。

只做「读 + 聚合」，不改数据结构、不碰扫描与加密写入：
* ``/api/library``          全部图片 / 全部视频 / 最近添加（分页）
* ``/api/discover``         分层随机：随机挑 N 个含媒体的目录，每目录取 M 项
* ``/api/folder-previews``  批量取目录的预览图 id（前端自己拼 2×2，服务器不出图字节）
* ``/api/tree``             目录树（含媒体数与子目录数），供左栏使用

约定：前台一律**不显示** ``kind='other'``（文档/压缩包），这里所有查询都只取 image/video。
"""
import logging
import os
import random
import sqlite3
import threading
import time as _time

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import index_crypto as ic
from .codecs import classify as _classify_codec
from .db import KIND_IMAGE, MIN_IMAGE_BYTES, visible_cond
from . import hide_rules
from . import hidden_store

log = logging.getLogger("album.frontend")

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"
# 轻量 TTL 缓存：库内容只在扫描时变，短缓存对"秒开"意义很大
_TTL: dict = {}
def _ttl_get(key, ttl):
    hit = _TTL.get(key)
    if hit and _time.time() - hit[0] < ttl:
        return hit[1]
    return None
def _ttl_set(key, val):
    if len(_TTL) > 64:
        _TTL.clear()
    _TTL[key] = (_time.time(), val)
MEDIA = ("image", "video")


def _q(sql, args=()):
    con = sqlite3.connect(EMBY, timeout=20)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _exec(sql, args=()):
    """写库（frontend_api 不持有 DB 对象，这里自己开连接提交）。"""
    con = sqlite3.connect(EMBY, timeout=20)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def _fav_clear_cache():
    """收藏数一变，把 fav_total 缓存全部打掉。"""
    for k in [k for k in list(_TTL) if isinstance(k, tuple) and k and k[0] == "fav_total"]:
        _TTL.pop(k, None)


# ===== req-dircover：目录封面 =====
# 媒体条件抽成常量，避免三处各写一份（改一处漏两处是历史踩过的坑）
_PV_MEDIA = ("kind IN ('image','video') AND lower(COALESCE(ext,'')) <> 'gif' "
             "AND COALESCE(hidden,0)=0 AND NOT (kind='image' AND COALESCE(size,0) < ?)")
_PV_NEED = 4        # 每个目录最多 4 张封面（前端拼 2×2）
_PV_LVL = 4         # 最多向下探 4 层
_PV_PER_LVL = 8     # 每层每个目录最多展开 8 个子目录
_PV_BUDGET = 24     # 单个目录最多访问 24 个子目录（防大库拖慢）
_PV_MS = 500        # 整次请求为"借封面"的总时间预算
_PV_MIN_MS = 8      # 单个目录至少分到的时间片（避免前几个贵目录把后面的饿死）
#   实测：逐子目录索引查询 ~6.4ms/个（要下探 2 层的）/ 3.6ms/个（1 层就够）；
#   前端一次会请求当前目录的**全部**子目录（后端上限 60），60 个约 380ms。
#   ⛔ 不要改成 "parent_id IN (...) + ROW_NUMBER" 的批量写法：实测 80ms/个，
#     因为 IN 列表 + 窗口函数用不上 idx_items_parent，比逐条索引查询慢十几倍。


def _borrow_descendants(cid, got, deadline):
    """目录自身媒体不足时，**逐层向下级目录借封面**（层浅优先）。

    旧实现只下探 1 层（向最多 4 个子目录各借 1 张），于是"只装子目录的容器目录"
    常常没有封面。实测 1,023 个这类目录里，1 层只覆盖 763 个，再深 2~4 层还能多覆盖 92 个。

    ⛔ 必须有预算上限与时间熔断：一次请求可能带 60 个目录，无上限会把库扫穿。
    """
    budget = _PV_BUDGET
    seen = {cid}
    frontier = [cid]
    for _lvl in range(_PV_LVL):
        if len(got) >= _PV_NEED or budget <= 0 or not frontier:
            break
        if _time.time() > deadline:
            break
        nxt = []
        for pid in frontier[:_PV_PER_LVL]:
            if len(got) >= _PV_NEED or budget <= 0 or _time.time() > deadline:
                break
            subs = _q("SELECT id FROM folders WHERE parent_id=? "
                      "ORDER BY COALESCE(folder_mtime,0) DESC, id LIMIT ?",
                      (pid, _PV_PER_LVL))
            for sb in subs:
                if len(got) >= _PV_NEED or budget <= 0 or _time.time() > deadline:
                    break
                sid = sb["id"]
                if sid in seen:
                    continue
                seen.add(sid)
                budget -= 1
                one = _q("SELECT id FROM items WHERE parent_id=? AND " + _PV_MEDIA +
                         " ORDER BY COALESCE(name_rank, 1<<30) LIMIT 1", (sid, MIN_IMAGE_BYTES))
                if one and one[0]["id"] not in got:
                    got.append(one[0]["id"])
                nxt.append(sid)      # 无论有没有借到都继续下探（预算与时间双限额兜底）
        frontier = nxt
    return got


def _item(row, folder_name=""):
    from .codecs import classify as _classify
    kind = row["kind"]
    d = {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "kind": kind,
        "ext": (row["ext"] or "").lower(),
        "size": row["size"] or 0,
        "mtime": row["mtime"] or 0,
        "duration": row["duration"] or 0,
        "name": ic.safe_unseal(row["name_enc"] or ""),
        "folder": folder_name,
        "hidden": int((row["hidden"] if "hidden" in row.keys() else 0) or 0),
    }
    if kind == "video":
        vcodec = (row["vcodec"] if "vcodec" in row.keys() else "") or ""
        info = _classify(vcodec)
        d["vcodec"] = vcodec
        d["width"] = int((row["width"] if "width" in row.keys() else 0) or 0)
        d["height"] = int((row["height"] if "height" in row.keys() else 0) or 0)
        d["playable_codec"] = info["playable"]     # True/False/None(未知，没探测过)
        d["codec_label"] = info["label"]
        d["codec_note"] = info["note"]
    return d


def _item_probe(row) -> dict:
    """只取过滤需要的三个字段（避免为过滤跑一遍完整的 _item）。"""
    try:
        return {"name": ic.safe_unseal(row["name_enc"] or ""), "kind": row["kind"],
                "size": row["size"] or 0,
                "hidden": int((row["hidden"] if "hidden" in row.keys() else 0) or 0)}
    except Exception:                                      # noqa: BLE001
        return {}


def _keep(it: dict) -> bool:
    """列表/随机发现的兜底过滤：已排除条目、黑名单小视频、非图非视频一律不显示。"""
    if not it:
        return False
    if it.get("kind") not in ("image", "video"):
        return False
    if int(it.get("hidden") or 0):
        return False                       # ★ 已排除（损坏/无缩略图，见 hidden_store）
    return not hide_rules.is_blocked(it.get("name") or "", it.get("kind") or "",
                                     int(it.get("size") or 0))


def _folder_names(cids):
    cids = [c for c in {c for c in cids if c}]
    if not cids:
        return {}
    marks = ",".join("?" * len(cids))
    rows = _q("SELECT id, name_enc FROM folders WHERE id IN (%s)" % marks, cids)
    return {r["id"]: ic.safe_unseal(r["name_enc"] or "") for r in rows}


def _who(request) -> str:
    """当前会话账户名（空 = 未登录，调用方按"看不到任何东西"处理）。"""
    try:
        from .site_auth import session_user
        return session_user(request) or ""
    except Exception:                                       # noqa: BLE001
        return ""


def _dir_filter(user: str, column: str = "parent_id") -> tuple[str, tuple]:
    """★ req-accounts：目录授权过滤片段。管理员 → 空；普通账户 → 递归 CTE。

    见 ``accounts.sql_filter`` 的说明：**没有授权时必须 AND 1=0**，绝不能放行。
    """
    from . import accounts as _acc
    return _acc.sql_filter(user, column)


def install(app) -> None:
    @app.get("/api/library")
    async def library(request: Request, kind: str = Query("all"), offset: int = Query(0, ge=0),
                      limit: int = Query(60, ge=1, le=200), order: str = Query("mtime"),
                      show_hidden: int = Query(0)):
        kinds = MEDIA if kind in ("", "all") else (kind,)
        marks = ",".join("?" * len(kinds))
        # GIF 全局过滤（用户要求：界面一律不显示动图）
        where = (("kind IN (%s) AND lower(COALESCE(ext,'')) <> 'gif'" % marks)
                 + visible_cond(include_hidden=bool(show_hidden)))
        # ★ req-accounts：按授权目录过滤（管理员为空片段）
        _u = _who(request)
        _df, _dp = _dir_filter(_u, "parent_id")
        where = where + _df
        # ★ COUNT(*) 在 14 万行上要 113ms（占接口近一半），而库只在扫描时变 → 缓存 30 秒
        # ⛔ 缓存键**必须带账户名**，否则 A 的计数会被 B 复用（越权泄露）
        total = _ttl_get(("lib_total", kind, _u), 30)
        if total is None:
            total = _q("SELECT COUNT(*) c FROM items WHERE " + where,
                       list(kinds) + [KIND_IMAGE] + list(_dp))[0]["c"]
            _ttl_set(("lib_total", kind, _u), total)
        # ★ 默认（mtime）排序必须 INDEXED BY idx_items_mtime：
        # 不加时 SQLite 对 kind IN ('image','video') + COALESCE(mtime,0) 会"全表扫 + 临时 B 树"，
        # 实测 120ms；显式走 mtime 索引后 0.25ms（索引按 mtime 有序扫，取满 limit 即停）。
        # 全库 mtime 无 NULL，且 ORDER BY mtime DESC 的 NULL 也在末尾，与原 COALESCE 写法结果逐条一致。
        if (order or "mtime").lower() in ("", "mtime"):
            rows = _q("SELECT * FROM items INDEXED BY idx_items_mtime WHERE " + where +
                      " ORDER BY mtime DESC, id LIMIT ? OFFSET ?",
                      list(kinds) + [KIND_IMAGE] + list(_dp) + [int(limit), int(offset)])
        else:
            ob = {"name": "COALESCE(name_rank, 1<<30), id",
                  "size": "COALESCE(size,0) DESC, id"}.get(order, "mtime DESC, id")
            rows = _q("SELECT * FROM items WHERE " + where +
                      " ORDER BY " + ob + " LIMIT ? OFFSET ?",
                      list(kinds) + [KIND_IMAGE] + list(_dp) + [int(limit), int(offset)])
        rows = [r for r in rows if _keep(_item_probe(r))]
        names = _folder_names([r["parent_id"] for r in rows])
        return JSONResponse({
            "kind": kind, "total": total, "offset": offset, "limit": limit,
            "items": [_item(r, names.get(r["parent_id"], "")) for r in rows],
        })

    # ------------------------------------------------------ req-fav 我的收藏
    @app.get("/api/fav")
    async def fav_list(request: Request, kind: str = Query("all"), offset: int = Query(0, ge=0),
                       limit: int = Query(60, ge=1, le=200),
                       order: str = Query("added")):
        """我的收藏列表。**带 folder / parent_id**，卡片因此能显示「跳到所在目录」。"""
        kinds = MEDIA if kind in ("", "all") else (kind,)
        marks = ",".join("?" * len(kinds))
        where = ("i.kind IN (%s) AND lower(COALESCE(i.ext,'')) <> 'gif'" % marks
                 ) + visible_cond(alias="i", include_hidden=False)
        # ★ req-accounts：收藏也按授权目录过滤（否则收藏夹会泄露未授权目录的条目）
        _u = _who(request)
        _df, _dp = _dir_filter(_u, "i.parent_id")
        where = where + _df
        base = "FROM fav f JOIN items i ON i.id = f.item_id WHERE " + where
        total = _ttl_get(("fav_total", kind, _u), 5)
        if total is None:
            total = _q("SELECT COUNT(*) c " + base,
                       list(kinds) + [KIND_IMAGE] + list(_dp))[0]["c"]
            _ttl_set(("fav_total", kind, _u), total)
        ob = {"mtime": "i.mtime DESC, i.id",
              "name": "COALESCE(i.name_rank, 1<<30), i.id",
              "size": "COALESCE(i.size,0) DESC, i.id"}.get(
                  (order or "added").lower(), "f.added DESC, i.id")
        rows = _q("SELECT i.* " + base + " ORDER BY " + ob + " LIMIT ? OFFSET ?",
                  list(kinds) + [KIND_IMAGE] + list(_dp) + [int(limit), int(offset)])
        rows = [r for r in rows if _keep(_item_probe(r))]
        names = _folder_names([r["parent_id"] for r in rows])
        items = [_item(r, names.get(r["parent_id"], "")) for r in rows]
        for it in items:
            it["fav"] = 1
        return JSONResponse({"kind": kind, "total": total, "offset": offset,
                             "limit": limit, "items": items})

    @app.post("/api/fav")
    async def fav_set(request: Request, payload: dict):
        """{id, on} → 收藏/取消；不给 on 就按"取反"处理。"""
        item_id = str(payload.get("id") or "").strip()
        if not item_id:
            raise HTTPException(400, "缺少 id")
        # ★ req-accounts：只能收藏自己**有权看到**的条目
        from . import accounts as _acc
        _u = _who(payload.get("_request") if isinstance(payload, dict) else None) if False else ""
        row = _q("SELECT id, kind, parent_id FROM items WHERE id=?", (item_id,))
        if not row:
            raise HTTPException(404, "没有这个媒体项")
        _u = _who(request)
        if not _acc.can_see_item(_u, row[0]["parent_id"]):
            raise HTTPException(404, "没有这个媒体项")   # 不透露存在性
        kind = row[0]["kind"] or ""
        have = bool(_q("SELECT 1 x FROM fav WHERE item_id=?", (item_id,)))
        want = bool(payload["on"]) if isinstance(payload.get("on"), bool) else (not have)
        if want and not have:
            _exec("INSERT INTO fav (item_id, kind, added) VALUES (?,?,?) "
                  "ON CONFLICT(item_id) DO NOTHING", (item_id, kind, int(_time.time())))
        elif not want and have:
            _exec("DELETE FROM fav WHERE item_id=?", (item_id,))
        _fav_clear_cache()
        total = _q("SELECT COUNT(*) c FROM fav")[0]["c"]
        return JSONResponse({"id": item_id, "fav": bool(want), "total": int(total)})

    @app.get("/api/discover")
    # ★ req-per1：dirs 40→80、per 3→1 —— 每张图来自**不同目录**，不再出现
    #   "同一目录 3 张连排"。实测 80×1 出 80 条 / 80 个不同目录，耗时 0.074s。
    async def discover(request: Request, dirs: int = Query(80, ge=1, le=80),
                       per: int = Query(1, ge=1, le=8),
                       seed: int = Query(0, ge=0),
                       kind: str = Query("all"),
                       show_hidden: int = Query(0)):
        """随机发现。给了 seed 就**可复现**（同一 seed 结果完全一致），
        这样从播放页返回时不会重抽一批。

        kind: all | image | video —— 在随机发现内部就地过滤类型
        （之前前端点「图片」会整库切到“全部图片”，随机性丢失）。

        ★ req-per1：`per` 默认 1 —— 每个目录只贡献 1 条，**每条来自不同目录**。
        配合 `dirs=80`，一次展示 80 条 / 80 个目录。
        `per=1` 不损害公平性：目录内每条被抽中的概率仍是 `1/n`（环形窗口保证均匀）。"""
        rnd = random.Random(seed) if seed else random
        # 用户要求：GIF 不进主页随机流（只在目录里可见）
        if kind == "image":
            _cond = ("kind = 'image' AND lower(COALESCE(ext,'')) <> 'gif'"
                     " AND COALESCE(size,0) >= %d" % MIN_IMAGE_BYTES)
        elif kind == "video":
            _cond = "kind = 'video' AND lower(COALESCE(ext,'')) <> 'gif'"
        else:
            _cond = ("kind IN ('image','video') AND lower(COALESCE(ext,'')) <> 'gif'"
                     " AND NOT (kind='image' AND COALESCE(size,0) < %d)" % MIN_IMAGE_BYTES)
        # ★ 候选池是"确定性"的（只跟 kind 过滤条件有关，库变了才变），全库 GROUP BY 要 194ms
        # → 按 kind 缓存 60 秒，主页首屏不再为它等 0.2 秒。
        _hid_all = "" if show_hidden else " AND COALESCE(hidden,0)=0"
        # ★ req-accounts：候选池也必须按授权目录过滤，否则随机发现会抽到未授权目录
        _u = _who(request)
        _df, _dp = _dir_filter(_u, "parent_id")
        # ★ req-discwin：顺带算出每个目录的**符合条件总数 n**。
        #   GROUP BY 本来就要扫，加 COUNT(*) 不额外花钱，但让我们知道窗口该怎么取。
        #   缓存键加 "2"：旧格式没有 n，换键避免吃到旧缓存。
        pool = _ttl_get(("disc_pool2", kind, int(show_hidden or 0), _u), 60)
        if pool is None:
            pool = _q("SELECT parent_id, COUNT(*) AS n FROM items WHERE " + _cond + _hid_all + _df +
                      " GROUP BY parent_id ORDER BY parent_id", tuple(_dp))  # 确定性候选池
            if pool:
                _ttl_set(("disc_pool2", kind, int(show_hidden or 0), _u), pool)
        if not pool:
            return JSONResponse({"dirs": 0, "per": per, "total_in_dirs": 0,
                                 "items": [], "kind": kind, "seed": int(seed)})
        picked = rnd.sample(pool, min(int(dirs), len(pool)))
        names = _folder_names([p["parent_id"] for p in picked])
        items = []
        # 每个目录只取这几列、只取 60 行来抽样（以前是 SELECT * + LIMIT 400）。
        # 40 个目录 ×400 行 = 1.6 万行，实测 kind=image 时整个接口要 2.3 秒；
        # 收窄列 + 限 60 行后回到 0.2 秒级。
        _cols = ("id,parent_id,kind,ext,size,mtime,duration,vcodec,width,height,"
                 "name_enc,hidden")
        # ★ req-discwin：每个目录参与随机的**窗口大小**
        #   原来写死 60 且只从 id 最小的 60 个里抽 → 大目录 97% 的内容永远抽不到
        #   （实测全库 37.5% 的媒体永远进不了随机流）。现在改成环形窗口，见下。
        _win = max(int(per), int(os.getenv("DISCOVER_WIN") or 200))
        # ⛔ _hid_cond 必须在**循环外**定义：下面的 _sql 模板引用了它。
        #    第一版把它留在循环内 → UnboundLocalError（500）。
        _hid_cond = "" if show_hidden else " AND COALESCE(hidden,0)=0"
        _sql = ("SELECT " + _cols + " FROM items INDEXED BY idx_items_parent "
                "WHERE parent_id=? AND " + _cond + _hid_cond +
                " ORDER BY id LIMIT ? OFFSET ?")
        for p in picked:
            cid = p["parent_id"]
            # ★ 必须 INDEXED BY idx_items_parent：
            # kind='image' 这种**等值**条件下，SQLite 会挑 idx_items_kind_mtime(kind,mtime)
            # 然后扫全库 14.9 万行再按 parent_id 过滤 —— 40 个目录实测 2.4 秒。
            # 强制走 (parent_id, name_rank) 索引后同样 40 个目录只要 6 毫秒。
            _n = int(p.get("n") or 0)                  # 该目录符合条件的总数（来自候选池）
            if _n <= _win:
                rows = _q(_sql, (cid, _win, 0))        # 目录不大 → 全部参与随机
            else:
                # ★ 环形窗口：把按 id 排序的列表当作环，起点 start 由 seed 决定。
                #   对任意一条 i，"包含 i 的起点"恰好有 _win 个（共 _n 个起点）
                #   → **每条被抽中的概率完全相等**，且同 seed 结果完全可复现。
                start = rnd.randrange(0, _n)
                rows = _q(_sql, (cid, _win, start))
                _got = len(rows)
                if _got < _win:                        # 窗口越过末尾 → 从开头补齐
                    rows = rows + _q(_sql, (cid, _win - _got, 0))
            if len(rows) > int(per):
                rows = rnd.sample(rows, int(per))
            for r in rows:
                _d = _item(r, names.get(cid, ""))
                if _keep(_d):
                    items.append(_d)
        rnd.shuffle(items)
        return JSONResponse({"dirs": len(picked), "per": per,
                             "total_in_dirs": len(items), "items": items,
                             "kind": kind, "seed": int(seed)})

    @app.get("/api/folder-previews")
    async def folder_previews(request: Request, cids: str = Query("")):
        """每个目录最多返回 4 个媒体 id（前端拼 2×2 封面，服务器不出图字节）。"""
        out = {}
        # ★ req-accounts：未授权目录的封面一律不返回（连"有没有"都不透露）
        from . import accounts as _acc
        _u = _who(request)
        _t0 = _time.time()
        _cids = [c for c in (cids or "").split(",") if c][:60]
        for _i, cid in enumerate(_cids):
            # ★ req-dircover：按目录公平分时 —— 剩余预算 / 剩余目录数（下限 _PV_MIN_MS）。
            #   用"全局熔断"会让前几个昂贵目录吃光预算、后面的目录全都没封面。
            _left = _PV_MS / 1000.0 - (_time.time() - _t0)
            _share = max(_PV_MIN_MS / 1000.0, _left / max(1, len(_cids) - _i))
            _deadline = _time.time() + _share
            if not _acc.can_see_folder(_u, cid):
                continue
            rows = _q("SELECT id FROM items WHERE parent_id=? AND " + _PV_MEDIA +
                      " ORDER BY COALESCE(name_rank, 1<<30) LIMIT ?",
                      (cid, MIN_IMAGE_BYTES, _PV_NEED))
            if len(rows) < _PV_NEED:
                # ★ req-dircover：容器目录自身媒体不足 → 逐层向下级目录借封面
                rows = [{"id": i} for i in
                        _borrow_descendants(cid, [r["id"] for r in rows], _deadline)]
            if rows:
                out[cid] = [r["id"] for r in rows]
        return JSONResponse({"previews": out})

    _TREE_CACHE: dict = {}

    @app.get("/api/tree")
    async def tree(request: Request, depth: int = Query(2, ge=1, le=3), root: str = Query("")):
        # 目录树是几条 GROUP BY 全表统计，实测 0.15~1.7s；客户端进页面只取一次，
        # 所以缓存 30 秒（前端 sessionStorage 还有一层，双保险）。
        import time as _tt
        # ★ req-accounts：树缓存键必须带账户名，否则 A 的树会被 B 复用（越权泄露）
        _u = _who(request)
        _ck = (int(depth), root or "", _u)
        _hit = _TREE_CACHE.get(_ck)
        if _hit and _tt.time() - _hit[0] < 30:
            return JSONResponse(_hit[1])
        media = {r["parent_id"]: r["c"] for r in
                 _q("SELECT parent_id, COUNT(*) c FROM items "
                    "WHERE kind IN ('image','video') "
                    "AND NOT (kind='image' AND COALESCE(size,0) < %d) "
                    "GROUP BY parent_id" % MIN_IMAGE_BYTES)}
        subs = {r["parent_id"]: r["c"] for r in
                _q("SELECT parent_id, COUNT(*) c FROM folders GROUP BY parent_id")}
        from . import accounts as _acc
        _allowed = _acc.allowed_folder_ids(_u)          # None = 管理员
        if not root:
            # 普通账户：树的起点是**它被授权的目录**，不是全库根
            if _allowed is not None:
                _ar = _acc.authorized_roots(_u)
                if not _ar:
                    return JSONResponse({"root": "", "depth": depth, "nodes": []})
                root = _ar[0]
            else:
                rows = _q("SELECT id FROM folders WHERE parent_id NOT IN (SELECT id FROM folders) LIMIT 1")
                root = rows[0]["id"] if rows else ""
        if _allowed is not None and root not in _allowed:
            # 指定的 root 不在授权范围内 → 返回空树（不泄露是否存在）
            return JSONResponse({"root": "", "depth": depth, "nodes": []})

        def walk(cid, level):
            rows = _q("SELECT id, name_enc, folder_mtime FROM folders WHERE parent_id=? "
                      "ORDER BY COALESCE(folder_mtime,0) DESC, id LIMIT 400", (cid,))
            if _allowed is not None:
                rows = [r for r in rows if str(r["id"]) in _allowed]   # ★ 只保留授权子树
            out = []
            for r in rows:
                node = {"id": r["id"], "name": ic.safe_unseal(r["name_enc"] or ""),
                        "media": media.get(r["id"], 0), "folders": subs.get(r["id"], 0)}
                if level < depth and node["folders"]:
                    node["children"] = walk(r["id"], level + 1)
                out.append(node)
            return out

        _out = {"root": root, "depth": depth, "nodes": walk(root, 1)}
        _TREE_CACHE[_ck] = (_tt.time(), _out)
        return JSONResponse(_out)

    # ---------------------------------------------------------------- 搜索
    # 名字在库里是密文（只有盲索引 name_idx / 前缀桶 name_bucket），没法直接用 LIKE 搜，
    # 所以这里在**内存里**建一份解密后的名字索引（绝不落盘，DB 里仍然扫不到明文）。
    # 实测：全库 ~15 万项解密约 1.2s —— 每次查询都解一遍不可接受，故做缓存。
    #
    # 内存很关键（这台机器 990MB / 容器 448MB，抓帧每路还要 ~100MB），所以索引刻意做瘦：
    #   * 不存 ext（从名字现推）、不存两份名字（只存原始名，匹配时现转小写）
    #   * parent_id / vcodec 用 sys.intern 归一（parent 只有 6 千个不同值）
    #   * 元组固定 8 项，别塞不必要的字段
    _IDX = {"at": 0.0, "n": -1, "items": [], "folders": {}, "partial": False}
    _IDX_LOCK = threading.Lock()
    IDX_TTL = int(os.getenv("SEARCH_INDEX_TTL") or 600)
    IDX_MAX = int(os.getenv("SEARCH_INDEX_MAX") or 300000)

    def _build_index():
        import gc as _gc
        import sys as _sys
        import time as _t
        # 用"参与索引的行数"做失效判断：GIF 被排除是规则不是截断，
        # 不能拿全表行数比，否则 partial 会永远是 true。
        cnt = _q("SELECT COUNT(*) c FROM items "
                 "WHERE lower(COALESCE(ext,'')) <> 'gif' "
                 "AND COALESCE(hidden,0)=0 "
                 "AND NOT (kind='image' AND COALESCE(size,0) < %d)" % MIN_IMAGE_BYTES)[0]["c"]
        if (_IDX["items"] and _t.time() - _IDX["at"] < IDX_TTL and _IDX["n"] == cnt):
            return _IDX
        with _IDX_LOCK:
            # ★ 双重检查：并发搜索时如果别的线程刚建好，直接复用，
            # 避免 N 个请求同时解密 14 万行重建索引（实测最坏单次 8.7 秒）。
            if (_IDX["items"] and _t.time() - _IDX["at"] < IDX_TTL and _IDX["n"] == cnt):
                return _IDX
            return _build_index_locked(cnt)

    def _build_index_locked(cnt):
        import gc as _gc
        import sys as _sys
        import time as _t
        # 注意：这里**不能**用 _q() —— 它会把 14 万行物化成 dict 列表（峰值多占 ~90MB）。
        # 直接拿游标逐行消费，行对象用完即弃。
        con = sqlite3.connect(EMBY, timeout=30)
        items = []
        intern = _sys.intern
        try:
            cur = con.execute(
                "SELECT id,parent_id,kind,size,mtime,duration,vcodec,name_enc FROM items "
                "WHERE lower(COALESCE(ext,'')) <> 'gif' "
                "AND COALESCE(hidden,0)=0 "
                "AND NOT (kind='image' AND COALESCE(size,0) < %d) "
                "ORDER BY COALESCE(mtime,0) DESC LIMIT ?" % MIN_IMAGE_BYTES, (int(IDX_MAX),))
            for r in cur:
                _nm = ic.safe_unseal(r[7] or "")
                if hide_rules.is_blocked(_nm, r[2] or "", int(r[3] or 0)):
                    continue                                   # 黑名单小视频不进索引
                items.append((_nm, r[0], intern(r[2] or ""),
                              int(r[3] or 0), int(r[4] or 0), int(r[5] or 0) or None,
                              intern(r[6] or ""), intern(r[1] or "")))
        finally:
            con.close()
        frows = _q("SELECT id,name_enc FROM folders")
        folders = {}
        for r in frows:
            folders[r["id"]] = ic.safe_unseal(r["name_enc"] or "")
        del frows
        _IDX.update({"at": _t.time(), "n": cnt, "items": items, "folders": folders,
                     "partial": cnt > len(items)})
        _gc.collect()
        log.info("搜索索引已建：items=%d folders=%d partial=%s", len(items), len(folders),
                 _IDX["partial"])
        return _IDX

    @app.get("/api/search")
    async def search(request: Request, q: str = Query(""), kind: str = Query("all"),
                     limit: int = Query(120, ge=1, le=300)):
        import time as _t
        t0 = _t.time()
        needle = (q or "").strip().lower()
        if not needle:
            return JSONResponse({"q": q, "items": [], "folders": [], "total_items": 0,
                                 "total_folders": 0, "ms": 0})
        # ★ 建索引要 1~1.2 秒：放到线程里跑，避免把事件循环（所有其它请求）一起卡住
        import asyncio as _a
        idx = await _a.to_thread(_build_index)
        want = None if kind in ("", "all") else kind

        # ★ req-accounts：索引是**全库**的（不能按用户重建，那样太贵），
        #   所以在**结果上**过滤 —— 只保留授权目录内的文件夹与条目。
        from . import accounts as _acc
        _u = _who(request)
        _allowed = _acc.allowed_folder_ids(_u)          # None = 管理员
        hit_f = [(cid, nm) for cid, nm in idx["folders"].items()
                 if needle in nm.lower()
                 and (_allowed is None or cid in _allowed)]
        hit_f.sort(key=lambda x: x[1])
        folders = [{"id": cid, "name": nm} for cid, nm in hit_f[:60]]

        items, total = [], 0
        lim = int(limit)
        for nm, iid, k, size, mtime, dur, vcodec, pid in idx["items"]:
            # 名字只存了一份（保留大小写），匹配时现转小写，省一半名字内存
            if needle not in nm.lower():
                continue
            if want and k != want:
                continue
            if k == "image" and size < MIN_IMAGE_BYTES:
                continue
            if _allowed is not None and pid not in _allowed:
                continue                                 # ★ 未授权目录的条目一律不返回
            total += 1
            if len(items) < lim:
                info = _classify_codec(vcodec) if k == "video" else None
                dot = nm.rfind(".")
                d = {"id": iid, "parent_id": pid, "kind": k,
                     "ext": (nm[dot + 1:].lower() if dot > 0 else ""), "size": size,
                     "mtime": mtime, "duration": dur, "name": nm,
                     "folder": idx["folders"].get(pid, "")}
                if info:
                    d["vcodec"] = vcodec
                    d["playable_codec"] = info["playable"]
                    d["codec_label"] = info["label"]
                    d["codec_note"] = info["note"]
                items.append(d)
        return JSONResponse({"q": q, "kind": kind, "items": items, "folders": folders,
                             "total_items": total, "total_folders": len(hit_f),
                             "partial": idx["partial"], "truncated": total > len(items),
                             "ms": int((_t.time() - t0) * 1000)})
