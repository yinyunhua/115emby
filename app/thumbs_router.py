"""缩略图队列控制台（FastAPI 路由）。

★ req-delthumb：本模块原来还提供 ``/thumb/{pid}`` 与 ``/thumb/{pid}/meta``
（上游 115-album 自带），与 ``server.py`` 的 ``/api/thumb/{item_id}`` **功能重复**。
运维记录 §28（2026-10-20）就发现并列进了清理清单，但一直没执行，导致：

* 两条路径的缓存头不一致（``/api/thumb`` 硬编码 7 天 ``immutable``，
  ``/thumb`` 读 ``THUMB_BROWSER_CACHE`` 默认 1 天 ``must-revalidate``），
  改那个配置**以为能控制缩略图缓存、其实完全无效**；
* 前端只用 ``/api/thumb/``，``/thumb/*`` 无人引用（只有 ``tools/fetch_pending.py``
  在用，已一并切到 ``/api/thumb/``）。

**现已删除这两条死路径**，本模块只保留后台统计接口：

* ``GET /admin/thumb/stats``        运行信息 + 预热状态
* ``GET /admin/thumb/queue-detail`` 失败/跳过原因归类与明细

⛔ 出图统一走 ``server.py`` 的 ``/api/thumb/{item_id}``（那里有并发闸门、
占位 SVG、``X-Thumb`` 标记、以及普通账户的可见性校验）。
"""

import logging
from typing import Any, Awaitable, Callable, Optional

from . import index_crypto as ic
from .settings import Settings
from .thumb_fetch import ThumbFetcher
from .thumb_store import ThumbStore

log = logging.getLogger("album.thumbs_api")

# ★ req-delthumb：PLACEHOLDER_SVG 已随 /thumb/{pid} 一并删除
#   （出图走 server.py 的 /api/thumb，占位 SVG 在那里）


def build_router(
    store: ThumbStore,
    get_photo: Callable[[str], Awaitable[Any]],
    fetcher: Optional[ThumbFetcher] = None,
    *,
    settings: Optional[Settings] = None,
    guard: Callable[..., Any] | None = None,
):
    """构造 APIRouter。

    ``guard`` 是 FastAPI 依赖（如后台口令校验），给了就挂到管理接口上。
    """
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    st = settings or (fetcher.settings if fetcher else Settings.from_env())
    fetcher = fetcher or ThumbFetcher(store, st)
    deps = [Depends(guard)] if guard else []
    router = APIRouter(tags=["thumbs"])

    # ★ req-delthumb：/thumb/{pid} 与 /thumb/{pid}/meta 已删除
    #   （与 /api/thumb/{item_id} 功能重复，缓存头还不一致）

    if guard:
        @router.get("/admin/thumb/stats", dependencies=deps, name="thumb-stats")
        async def thumb_stats(request: Request) -> JSONResponse:
            """运行信息 + 预热状态。

            ★ req-thumbnow：管理台要能显示「定时窗口是否在跑 / 立即生成是否在跑」，
            否则点了"立即生成"看不出有没有在动。状态由 prewarm_daemon 放在
            ``app.state.prewarm_holder``（没有该守护时全部按关闭显示）。
            """
            import os

            out = fetcher.stats()
            holder = getattr(request.app.state, "prewarm_holder", None) or {}
            task = holder.get("task")
            man = holder.get("manual") or {}
            out["prewarm"] = {
                "enabled": (os.getenv("THUMB_PREWARM_ENABLED") or "0").strip() == "1",
                "window": (os.getenv("THUMB_PREWARM_WINDOW") or "").strip(),
                "inside": bool(holder.get("inside")),
                "running": bool(task is not None and not task.done()),
                "idle_rounds": int(holder.get("idle") or 0),
            }
            out["prewarm_now"] = {
                "running": bool(man.get("running")),
                "limit": int(man.get("limit") or 0),
                "started": int(man.get("started") or 0),
                "finished": int(man.get("finished") or 0),
                "fetched": int(man.get("fetched") or 0),
                "failed": int(man.get("failed") or 0),
                "hit": int(man.get("hit") or 0),
                "stopped": man.get("stopped") or "",
                "error": man.get("error") or "",
            }
            return JSONResponse(out)

        @router.get("/admin/thumb/queue-detail", dependencies=deps, name="thumb-queue-detail")
        async def thumb_queue_detail(limit: int = 30) -> JSONResponse:
            """失败/跳过的**原因归类 + 明细**（只读）。

            管理台原来只有一个"失败或跳过 238"的合计，把两种完全不同的情况混在一起：
            * ``failed``  真失败（ffmpeg 抓不到帧 / 上游报错）
            * ``skipped`` 按设计跳过（上游只给占位图、体积超限、规则排除）
            这里把它们分开，并给出每种原因各多少条、以及具体是哪些文件。
            """
            import os
            import sqlite3

            limit = max(1, min(int(limit or 30), 200))
            thumb_db = getattr(store, "path", None)
            emby_db = os.getenv("EMBY_DB") or "/app/data/emby.db"
            out: dict[str, Any] = {"failed": {"total": 0, "reasons": [], "items": []},
                                   "skipped": {"total": 0, "reasons": [], "items": []},
                                   "excluded": 0}
            if not thumb_db:
                return JSONResponse(out)
            try:
                con = sqlite3.connect(str(thumb_db), timeout=15)
                con.row_factory = sqlite3.Row
                try:
                    con.execute("ATTACH DATABASE ? AS e", (str(emby_db),))
                    joined = True
                except Exception:                          # noqa: BLE001
                    joined = False

                for state, key in (("failed", "failed"), ("skipped", "skipped")):
                    row = con.execute("SELECT COUNT(*) c FROM thumb_src WHERE state=?",
                                      (state,)).fetchone()
                    out[key]["total"] = int(row["c"] if row else 0)
                    for r in con.execute(
                            "SELECT COALESCE(last_err,'') e, COUNT(*) c FROM thumb_src "
                            "WHERE state=? GROUP BY e ORDER BY c DESC", (state,)):
                        out[key]["reasons"].append({"reason": r["e"] or "(无说明)",
                                                    "n": int(r["c"])})
                    sql = ("SELECT s.pid, COALESCE(s.last_err,'') err, s.attempts, "
                           "i.kind, i.ext, i.size, i.name_enc FROM thumb_src s "
                           + ("LEFT JOIN e.items i ON i.id = s.pid " if joined else "")
                           + "WHERE s.state=? ORDER BY s.updated DESC LIMIT ?")
                    for r in con.execute(sql, (state, limit)):
                        d = dict(r)
                        out[key]["items"].append({
                            "id": d.get("pid", ""),
                            "name": ic.safe_unseal(d.get("name_enc") or "") or "(条目已不在库)",
                            "kind": d.get("kind") or "", "ext": d.get("ext") or "",
                            "size": int(d.get("size") or 0), "err": d.get("err") or "",
                            "attempts": int(d.get("attempts") or 0),
                        })
                if joined:
                    out["excluded"] = int(con.execute(
                        "SELECT COUNT(*) c FROM thumb_src s LEFT JOIN e.items i ON i.id = s.pid "
                        "WHERE s.state IN ('failed','skipped') AND (i.id IS NULL "
                        "OR COALESCE(i.size,0) < 30000 OR lower(COALESCE(i.ext,''))='gif' "
                        "OR COALESCE(i.kind,'') NOT IN ('image','video'))").fetchone()["c"])
                con.close()
            except Exception as exc:                       # noqa: BLE001
                out["error"] = str(exc)[:200]
            return JSONResponse(out)

    else:
        @router.get("/admin/thumb/stats", name="thumb-stats-open")
        async def thumb_stats_open() -> JSONResponse:      # pragma: no cover
            return JSONResponse(fetcher.stats())

    return router


# ★ req-delthumb：_maybe_photo 只被已删除的 /thumb/{pid} 使用，一并删除