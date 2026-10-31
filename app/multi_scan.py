"""多根目录串行扫描 + 层级进度 + **根目录选择服务端持久化**（跨设备同步）。"""
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

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"
THUMB = os.getenv("THUMB_DB") or "/app/data/thumbs.db"
ROOTS_FILE = Path(os.getenv("ROOTS_FILE") or "/app/data/scan_roots.json")


def _depth(v, default: int = 0) -> int:
    """扫描深度：<=0 表示不限（一直扫到叶子）。"""
    try:
        d = int(v)
    except (TypeError, ValueError):
        d = default
    return d if d > 0 else 0

STATE_FILE = Path(os.getenv("SCAN_STATE_FILE") or "/app/data/scan_state.json")



def _q(sql, args=()):
    con = sqlite3.connect(EMBY, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _load_roots() -> list:
    try:
        if ROOTS_FILE.exists():
            data = json.loads(ROOTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [{"cid": str(r.get("cid")), "name": str(r.get("name") or "")}
                        for r in data if (r or {}).get("cid")]
    except Exception:
        pass
    return []


def _save_roots(roots: list) -> None:
    try:
        ROOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ROOTS_FILE.write_text(json.dumps(roots, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(ROOTS_FILE, 0o600)
    except Exception:
        pass


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "queue": state.get("queue") or [],
            "current": state.get("current"),
            "paused": bool(state.get("paused")),
            "saved_at": int(time.time()),
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        try:
            os.chmod(STATE_FILE, 0o600)
        except OSError:
            pass
    except Exception as exc:                      # noqa: BLE001
        print("scan_state 写入失败：", type(exc).__name__, exc, flush=True)


def install(app) -> None:
    scanner = getattr(app.state, "scanner", None)
    if scanner is None or not (os.getenv("ADMIN_TOKEN") or "").strip():
        return
    guard = make_guard()

    saved = _load_state()
    state = {"queue": list(saved.get("queue") or []), "current": None, "worker": None,
             "paused": bool(saved.get("paused")), "stop": False, "log": [],
             "roots": _load_roots()}

    # 上次被打断的任务：把它的根目录放回队首，继续扫
    prev = saved.get("current") or {}
    if prev.get("cid"):
        state["queue"] = [q for q in state["queue"] if q.get("cid") != prev["cid"]]
        state["queue"].insert(0, {"cid": prev["cid"], "name": prev.get("name", ""),
                                  "depth": _depth(prev.get("depth")),
                                  "kinds": prev.get("kinds") or ["image", "video"]})
        state["log"].append("重启后继续上次中断的根目录：%s" % (prev.get("name") or prev.get("cid")))

    async def _worker():
        try:
            while state["queue"]:
                if state["stop"]:
                    state["queue"].clear()
                    state["log"].append("已取消")
                    break
                while state["paused"]:
                    await asyncio.sleep(2)
                    if state["stop"]:
                        break
                if state["stop"]:
                    state["queue"].clear()
                    break
                item = state["queue"].pop(0)
                try:
                    job_id = scanner.start(item["cid"], depth=item["depth"],
                                           root_name=item.get("name", ""),
                                           kinds=item.get("kinds") or [])
                    state["current"] = {"cid": item["cid"], "name": item.get("name", ""),
                                        "job_id": job_id,
                                        "depth": _depth(item.get("depth")),
                                        "kinds": item.get("kinds") or []}
                    _save_state(state)
                    await scanner.run(job_id)
                except asyncio.CancelledError:
                    # 容器停止/重启：保留 current，下次启动据此续跑
                    _save_state(state)
                    raise
                except Exception as exc:
                    state["log"].append("根目录 %s 失败：%s" % (item.get("cid"), exc))
                    state["current"] = None
                    _save_state(state)
                else:
                    state["current"] = None
                    _save_state(state)
            state["log"].append("全部根目录处理完毕")
        finally:
            state["worker"] = None
            state["stop"] = False

    def _ensure():
        w = state.get("worker")
        if w is None or w.done():
            state["stop"] = False
            state["worker"] = asyncio.create_task(_worker())

    # ---------------- 根目录选择（服务端保存，跨设备） ----------------
    @app.get("/admin/scan/roots", dependencies=[Depends(guard)])
    async def get_roots():
        state["roots"] = _load_roots()
        return JSONResponse({"roots": state["roots"]})

    @app.post("/admin/scan/roots", dependencies=[Depends(guard)])
    async def set_roots(body: dict = Body(...)):
        roots = []
        for r in (body.get("roots") or []):
            cid = str((r or {}).get("cid") or "").strip()
            if not cid:
                continue
            roots.append({"cid": cid, "name": str(r.get("name") or "")})
        state["roots"] = roots
        _save_roots(roots)
        return JSONResponse({"ok": True, "saved": len(roots)})

    @app.get("/admin/scan/multi", dependencies=[Depends(guard)])
    async def get_multi():
        cur = state.get("current")
        job = None
        if cur:
            try:
                row = scanner.db.job(cur["job_id"])
                job = dict(row) if row else None
            except Exception:
                job = None
        return JSONResponse({"queue": state["queue"], "current": cur, "job": job,
                             "paused": state["paused"],
                             "running": bool(state.get("worker") and not state["worker"].done()),
                             "roots": state["roots"],
                             "log": state["log"][-6:]})

    @app.post("/admin/scan/multi", dependencies=[Depends(guard)])
    async def post_multi(body: dict = Body(...)):
        """排队扫描。

        ★ req-rootguard：``save_roots=False`` 时**不覆盖** ``scan_roots.json``。
        历史 bug：补扫单个目录时也调这个接口，把「扫描根目录」覆盖成了那一个目录，
        而**账户管理的可选目录正是从 scan_roots.json 读的** → 账户管理的目录选择
        变成了「补扫」那一个目录。单目录补扫请用 ``/admin/scan/one``。
        """
        roots = body.get("roots") or []
        depth = _depth(body.get("depth"))
        kinds = body.get("kinds") or []
        save_roots = body.get("save_roots", True)     # ★ req-rootguard
        added = 0
        saved = []
        for r in roots:
            cid = str((r or {}).get("cid") or "").strip()
            if not cid:
                continue
            saved.append({"cid": cid, "name": str(r.get("name") or "")})
            if any(q["cid"] == cid for q in state["queue"]):
                continue
            state["queue"].append({"cid": cid, "name": str(r.get("name") or ""),
                                   "depth": depth, "kinds": kinds})
            added += 1
        if saved and save_roots:                      # ★ req-rootguard：只有明确要保存时才写
            state["roots"] = saved
            _save_roots(saved)
        if not added and not state.get("current"):
            raise HTTPException(400, "没有可扫描的根目录")
        _save_state(state)
        _ensure()
        return JSONResponse({"ok": True, "queued": len(state["queue"]), "depth": depth})

    @app.post("/admin/scan/one", dependencies=[Depends(guard)])
    async def post_scan_one(body: dict = Body(...)):
        """★ req-rootguard：**单目录补扫**，绝不碰 ``scan_roots.json``。

        用途：某个目录被漏扫/内容为空时，只补这一个（depth=0 到叶子）。
        ⛔ 不要用 ``/admin/scan/multi`` 来补扫 —— 那会把扫描根目录覆盖成这一个目录，
          连带把「账户管理」的可选目录也变成它。
        """
        cid = str((body or {}).get("cid") or "").strip()
        if not cid:
            raise HTTPException(400, "缺少 cid")
        depth = _depth((body or {}).get("depth", 0))
        kinds = (body or {}).get("kinds") or ["image", "video"]
        name = str((body or {}).get("name") or cid)
        if any(q["cid"] == cid for q in state["queue"]):
            return JSONResponse({"ok": True, "queued": 0, "note": "已在队列里"})
        state["queue"].append({"cid": cid, "name": name, "depth": depth, "kinds": kinds})
        _save_state(state)
        _ensure()
        log.info("单目录补扫：%s（%s），depth=%s", cid, name, depth)
        return JSONResponse({"ok": True, "queued": len(state["queue"]), "depth": depth})

    @app.post("/admin/scan/multi/{action}", dependencies=[Depends(guard)])
    async def ctl(action: str):
        if action == "pause":
            state["paused"] = True
        elif action == "resume":
            state["paused"] = False
        elif action == "cancel":
            state["stop"] = True
            state["queue"].clear()
            cur = state.get("current")
            if cur:
                try:
                    scanner.cancel(cur["job_id"])
                except Exception:
                    pass
        else:
            raise HTTPException(400, "未知动作")
        _save_state(state)
        return JSONResponse({"ok": True, "paused": state["paused"]})

    @app.get("/admin/scan/progress", dependencies=[Depends(guard)])
    async def progress():
        cur = state.get("current") or {}
        job_id = cur.get("job_id")
        scanned = _q("SELECT depth, COUNT(*) AS n FROM folders GROUP BY depth ORDER BY depth")
        pending = []
        if job_id:
            pending = _q("SELECT depth, COUNT(*) AS n FROM scan_queue "
                         "WHERE job_id=? AND state='pending' GROUP BY depth ORDER BY depth",
                         (job_id,))
        # ★ req-numfix2：与媒体概览同一口径（排除已隐藏的 46 项），
        #   否则「扫描管理 媒体文件总数」比「概览 库内媒体总数」多出 hidden 的条目。
        #   实测代价 45ms → 101ms（14.7 万行），可接受。
        by_kind = _q("SELECT kind, COUNT(*) AS n FROM items "
                     "WHERE COALESCE(hidden,0)=0 AND kind IN ('image','video') "
                     "GROUP BY kind")
        thumbs = {}
        try:
            t = sqlite3.connect(THUMB, timeout=10)
            thumbs = {"done": t.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0],
                      "pending": t.execute("SELECT COUNT(*) FROM thumb_src "
                                           "WHERE state='pending'").fetchone()[0]}
            t.close()
        except Exception:
            pass
        failed = []
        try:
            failed = _q("SELECT dir_id, attempts, err, epoch FROM scan_failed "
                        "ORDER BY epoch DESC LIMIT 20")
            failed_total = _q("SELECT COUNT(*) AS n FROM scan_failed")
        except Exception:
            failed_total = [{"n": 0}]
        return JSONResponse({"job_id": job_id, "root": cur.get("name", ""),
                             "scanned_dirs_by_depth": scanned,
                             "pending_dirs_by_depth": pending,
                             "by_kind": by_kind,
                             "files_total": sum(x["n"] for x in by_kind),
                             "thumbs": thumbs,
                             "failed_dirs": failed,
                             "failed_dirs_total": (failed_total[0]["n"] if failed_total else 0)})

    @app.on_event("startup")
    async def _resume_on_boot():
        # 事件循环此时已就绪；把上次没跑完的队列接着跑
        if state["queue"]:
            _ensure()
            state["log"].append("启动后继续：%d 个根目录" % len(state["queue"]))

    app.state.multi_scan = state
