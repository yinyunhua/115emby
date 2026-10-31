"""后台接口：一键「快速读取新内容」（只扫变化过的目录分支）。

* ``POST /admin/scan/quick``  启动（同一时间只允许一个；全量扫描在跑时拒绝）
* ``GET  /admin/scan/quick``  查进度/结果

后台按钮点一下就走这里，前端轮询显示：请求数 / 已查目录 / 跳过子树 / 新增项。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from .admin_auth import make_guard
from . import quick_scan

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"
ROOTS_FILE = Path(os.getenv("ROOTS_FILE") or "/app/data/scan_roots.json")
HOME_CID = os.getenv("HOME_CID") or ""

STATE = {
    "running": False, "root": "", "roots": [], "requests": 0, "dirs": 0,
    "skipped": 0, "folders": 0, "items": 0, "new_items": 0, "queued_thumbs": 0,
    "started": 0, "finished": 0, "error": "", "result": None, "stop": False,
}

# ★ req-scanreport：这些字段每根目录都要累加（多根时不能只看最后一根）
_AGG_KEYS = ("requests", "dirs", "skipped", "folders", "items", "new_items",
             "queued_thumbs")


def _saved_roots() -> list[str]:
    try:
        data = json.loads(ROOTS_FILE.read_text(encoding="utf-8"))
        roots = [str(r.get("cid")) for r in data if r.get("cid")]
        if roots:
            return roots
    except Exception:
        pass
    return [HOME_CID] if HOME_CID else []


def _full_scan_busy() -> str:
    """全量扫描是否在跑（跑着就别再快速扫，避免两路请求叠加）。"""
    try:
        con = sqlite3.connect(EMBY, timeout=10)
        row = con.execute(
            "SELECT id, status FROM scan_jobs WHERE status IN ('pending','running') "
            "ORDER BY created DESC LIMIT 1").fetchone()
        con.close()
        if row:
            return str(row[0])
    except Exception:
        pass
    return ""


async def _worker(roots: list[str]) -> None:
    STATE.update({"running": True, "error": "", "requests": 0, "dirs": 0, "skipped": 0,
                  "folders": 0, "items": 0, "new_items": 0, "result": None,
                  "started": int(time.time()), "finished": 0, "stop": False})
    agg = {k: 0 for k in _AGG_KEYS}

    def progress(p: dict) -> None:
        """单根：直接用回调里的值；多根：已完成部分 + 当前根的进度。"""
        for k in _AGG_KEYS:
            agg[k] = (p.get(k, 0) or 0) if len(roots) == 1 \
                else (STATE.get("_done_" + k, 0) + (p.get(k, 0) or 0))
        STATE.update(dict(agg))

    for _k in _AGG_KEYS:
        STATE["_done_" + _k] = 0
    try:
        for root in roots:
            STATE["root"] = root
            res = await quick_scan.run_quick(root, db_path=EMBY, max_requests=200000,
                                             on_progress=progress,
                                             should_stop=lambda: bool(STATE.get("stop")))
            for _k in _AGG_KEYS:                     # ★ req-scanreport：全部累加
                STATE["_done_" + _k] = STATE.get("_done_" + _k, 0) + int(res.get(_k, 0) or 0)
            STATE["result"] = res
        STATE.update({_k: STATE["_done_" + _k] for _k in _AGG_KEYS})
    except Exception as exc:                    # noqa: BLE001
        STATE["error"] = "%s: %s" % (type(exc).__name__, exc)
        try:
            import traceback
            traceback.print_exc()
        except Exception:
            pass
    finally:
        STATE["running"] = False
        STATE["finished"] = int(time.time())


def install(app) -> None:
    guard = getattr(app.state, "admin_guard", None) or make_guard()

    @app.post("/admin/scan/quick", dependencies=[Depends(guard)])
    async def start_quick(body: dict = Body(default_factory=dict)):
        if STATE["running"]:
            raise HTTPException(409, "快速读取已在运行，请稍候")
        busy = _full_scan_busy()
        if busy and not bool((body or {}).get("force")):
            raise HTTPException(409, "全量扫描 %s 正在运行：先暂停/取消，或再点一次强制" % busy)
        roots = _saved_roots()
        if not roots:
            raise HTTPException(400, "没有配置根目录（先在扫描页选一个）")
        STATE["roots"] = roots
        asyncio.create_task(_worker(roots))
        return JSONResponse({"ok": True, "roots": roots, "started": int(time.time())})

    @app.post("/admin/scan/quick/stop", dependencies=[Depends(guard)])
    async def stop_quick():
        STATE["stop"] = True
        return JSONResponse({"ok": True, "stopping": STATE["running"]})

    @app.get("/admin/scan/quick", dependencies=[Depends(guard)])
    async def quick_status():
        out = {k: v for k, v in STATE.items() if not k.startswith("_")}
        return JSONResponse(out)
