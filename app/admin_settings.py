"""后台设置接口（Cookie/头部鉴权由 admin_auth 统一提供）。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from . import source115
from .admin_auth import make_guard

WRITABLE = (
    "COOKIE_115", "ACCESS_TOKEN_115", "APP_ID_115", "UA",
    "API_MIN_INTERVAL", "API_CONCURRENCY",
    "THUMB_PREWARM_MAX_PER_NIGHT", "VIDEO_THUMB_ENABLED",
    "THUMB_PREWARM_ENABLED", "THUMB_PREWARM_WINDOW",
    "VIDEO_THUMB_ALLOW_LOWPRIORITY",
    "HOME_CID", "HOME_NAME", "THUMB_UPSTREAM_SIZE",
    "THUMB_IMG_MIN_INTERVAL", "THUMB_IMG_CONCURRENCY",
    "FAST_SCAN_INTERVAL", "FAST_SCAN_CONCURRENCY", "FAST_SCAN_LOOKAHEAD",
    "SCAN_INCREMENTAL", "SCAN_SKIP_WITHIN_S",
)
MASKED = ("COOKIE_115", "ACCESS_TOKEN_115")
ENVFILE = Path(os.getenv("ENV_FILE") or "/app/.env")


def _persist(updates):
    lines = ENVFILE.read_text(encoding="utf-8").splitlines() if ENVFILE.exists() else []
    seen = set()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in ln:
            continue
        k = ln.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = "%s=%s" % (k, updates[k])
            seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append("%s=%s" % (k, v))
    ENVFILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENVFILE, 0o600)
    except OSError:
        pass


def _apply_runtime(app, updates):
    import asyncio

    targets = [getattr(app.state, "fetcher", None), getattr(app.state, "scanner", None)]
    try:
        if "API_MIN_INTERVAL" in updates:
            mi = float(updates["API_MIN_INTERVAL"] or 2.0)
            for obj in targets:
                if obj is None:
                    continue
                st = getattr(obj, "settings", None)
                if st is not None:
                    st.api_min_interval = mi
                for attr in ("_throttle", "throttle"):
                    thr = getattr(obj, attr, None)
                    if thr is not None and hasattr(thr, "min_interval"):
                        thr.min_interval = mi
        if "API_CONCURRENCY" in updates:
            cc = max(1, int(updates["API_CONCURRENCY"] or 1))
            for obj in targets:
                if obj is None:
                    continue
                st = getattr(obj, "settings", None)
                if st is not None:
                    st.api_concurrency = cc
        fs = getattr(app.state, "fast_scan", None)
        if fs:
            if "FAST_SCAN_INTERVAL" in updates:
                fs["gate"].interval = float(updates["FAST_SCAN_INTERVAL"] or 1.0)
                fs["interval"] = fs["gate"].interval
            if "FAST_SCAN_CONCURRENCY" in updates:
                cc = max(1, int(updates["FAST_SCAN_CONCURRENCY"] or 2))
                fs["gate"].sem = asyncio.Semaphore(cc)
                fs["concurrency"] = cc
            if "FAST_SCAN_LOOKAHEAD" in updates:
                fs["lookahead"] = max(0, int(updates["FAST_SCAN_LOOKAHEAD"] or 0))
        gate = getattr(app.state, "thumb_gate", None)
        if gate is not None:
            if "THUMB_IMG_MIN_INTERVAL" in updates:
                gate.interval = float(updates["THUMB_IMG_MIN_INTERVAL"] or 0.6)
            if "THUMB_IMG_CONCURRENCY" in updates:
                gate.sem = asyncio.Semaphore(max(1, int(updates["THUMB_IMG_CONCURRENCY"] or 2)))
        # ★ req-prewarmidle：预热相关设置一改就唤醒守护循环，避免"保存了要等几分钟才生效"
        if any(k.startswith("THUMB_PREWARM_") for k in updates):
            from .prewarm_daemon import poke
            poke(app)
    except Exception:
        pass


def install(app):
    guard = make_guard()

    @app.get("/api/root")
    async def home_root():
        return JSONResponse({
            "cid": (os.getenv("HOME_CID") or "").strip(),
            "name": (os.getenv("HOME_NAME") or "").strip(),
        })

    @app.get("/admin/settings", dependencies=[Depends(guard)])
    async def get_settings():
        out = {}
        for k in WRITABLE:
            v = (os.getenv(k) or "").strip()
            if k in MASKED and v:
                out[k] = {"set": True, "length": len(v), "preview": v[:16] + "..." + v[-6:]}
            else:
                out[k] = {"set": bool(v), "value": v}
        st = getattr(getattr(app.state, "fetcher", None), "settings", None)
        out["_ready"] = {"cookie": bool(source115.cookie()),
                         "access_token": bool(source115.access_token()),
                         "upstream": bool(source115.ready())}
        out["_runtime"] = {"api_min_interval": getattr(st, "api_min_interval", None),
                           "api_concurrency": getattr(st, "api_concurrency", None),
                           "window": getattr(st, "window", None),
                           "env_file": str(ENVFILE),
                           "home_cid": (os.getenv("HOME_CID") or "").strip()}
        return JSONResponse(out)

    @app.post("/admin/settings", dependencies=[Depends(guard)])
    async def set_settings(body: dict = Body(...)):
        updates = {k: str(v if v is not None else "").strip()
                   for k, v in (body or {}).items() if k in WRITABLE}
        if not updates:
            raise HTTPException(status_code=400, detail="no writable field")
        for k, v in updates.items():
            os.environ[k] = v
        _persist(updates)
        _apply_runtime(app, updates)
        return JSONResponse({"ok": True, "written": sorted(updates)})
