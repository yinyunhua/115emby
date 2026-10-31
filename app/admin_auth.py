"""后台登录：口令校验 + HttpOnly Cookie 会话（登录后无需任何口令输入框）。

* ``GET  /login``          登录页（静态 login.html）
* ``POST /admin/login``    JSON ``{"password": "..."}`` → 成功写 HttpOnly Cookie
* ``GET  /admin/logout``   清除 Cookie
* ``make_guard()``         供其它模块复用：接受 **Cookie** 或 **X-Admin-Token 头**

Cookie 值 = HMAC-SHA256(ADMIN_TOKEN, "emby-admin-session")，不含明文口令。
不依赖 python-multipart（表单走 JSON）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from fastapi import Body, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "emby_admin"
STATIC = Path(__file__).resolve().parent / "static"


def _token() -> str:
    return (os.getenv("ADMIN_TOKEN") or "").strip()


def session_value() -> str:
    tok = _token()
    if not tok:
        return ""
    return hmac.new(tok.encode("utf-8"), b"emby-admin-session", hashlib.sha256).hexdigest()


def make_guard():
    def guard(request: Request, x_admin_token: str = Header(default="")) -> None:
        tok = _token()
        if not tok:
            raise HTTPException(status_code=401, detail="admin disabled")
        if (x_admin_token or "").strip() == tok:
            return
        if request.cookies.get(COOKIE) == session_value():
            return
        # 站点登录（新的 /login 口令体系）也算 —— 但**只有管理员**才算。
        # ★ req-accounts：原来只判 check_session，于是普通访问账户登录后
        #   **可以直接进后台**（能改设置、能看全部目录）。这里必须收紧到 is_admin。
        try:
            from .site_auth import is_admin as _is_admin
            if _is_admin(request):
                return
        except Exception as _e:                            # noqa: BLE001
            import logging
            logging.getLogger("album.adminauth").warning(
                "站点管理员判定失败：%s: %s", type(_e).__name__, _e)
        raise HTTPException(status_code=401, detail="admin login required")

    return guard


def install(app) -> None:
    tok = _token()
    if not tok:
        return

    @app.get("/login", include_in_schema=False)
    async def login_page():
        p = STATIC / "login.html"
        if not p.exists():
            return HTMLResponse("<h1>login.html missing</h1>", status_code=500)
        return HTMLResponse(p.read_text(encoding="utf-8"))

    @app.post("/admin/login")
    async def do_login(body: dict = Body(...)):
        pw = str((body or {}).get("password") or "").strip()
        if not hmac.compare_digest(pw, tok):
            raise HTTPException(status_code=401, detail="口令不正确")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, session_value(), max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax", secure=True)
        return resp

    @app.get("/admin/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    app.state.admin_guard = make_guard()
