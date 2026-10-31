"""普通访问账户的权限闸门：**默认拒绝**。

规则
----
会话里的账户是**管理员** → 全部放行（后台、设置、全部目录）。

会话是**普通账户** → 只有下面这些「明确开放」的路径可用，其余 ``/api/*`` 一律 403：

    GET  /api/site/state          身份（登录页要用）
    GET  /api/library            已按授权目录过滤
    GET  /api/discover           同上
    GET  /api/search             同上
    GET  /api/tree               只返回授权子树
    GET  /api/folder-previews    同上
    GET  /api/browse             校验 parent 在授权集合内
    GET  /api/folder/{id}        同上
    GET  /api/dirs               只列授权范围内的子目录
    GET  /api/item/{id}          校验 parent_id
    GET  /api/thumb/{id}         同上
    GET  /api/image/{id}         同上
    GET  /api/link/{id}          同上
    GET  /api/play/{id}          同上
    GET  /api/probe/{id}         同上
    GET/POST /api/fav            收藏（只返回自己可见的）
    GET  /api/handoff/{id}       外部播放器交接
    POST /api/progress           播放进度
    GET  /api/health             健康检查（不含任何数据）

**一律禁止**（普通账户）：``/admin/*`` 全部、``/api/scan``、``/api/scan/*``、
``/api/root``、``/api/site/password``、``/api/hls/*``（转码吃资源）、``/api/docs``。

⛔ 这是**白名单**：将来新增接口默认就是"普通账户不能用"，
不会因为忘了加判断而泄露数据。
"""
from __future__ import annotations

import logging
from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger("album.perm")

# 普通账户**允许**的方法+路径前缀（精确到前缀，`/api/item/xxx` 也匹配）
ALLOW = (
    ("GET", "/api/site/state"),
    ("GET", "/api/library"),
    ("GET", "/api/discover"),
    ("GET", "/api/search"),
    ("GET", "/api/tree"),
    ("GET", "/api/folder-previews"),
    ("GET", "/api/browse"),
    ("GET", "/api/folder/"),
    ("GET", "/api/dirs"),
    ("GET", "/api/item/"),
    ("GET", "/api/thumb/"),
    ("HEAD", "/api/thumb/"),
    ("GET", "/api/image/"),
    ("GET", "/api/link/"),
    ("GET", "/api/play/"),
    ("GET", "/api/probe/"),
    ("GET", "/api/fav"),
    ("POST", "/api/fav"),
    ("GET", "/api/handoff/"),
    ("POST", "/api/progress"),
    ("GET", "/api/health"),
)

# 普通账户**永远禁止**（即便以后不小心加进 ALLOW，这里也兜一层）
DENY_PREFIX = ("/admin", "/api/scan", "/api/root", "/api/site/password",
               "/api/hls", "/api/docs")


def _allowed(method: str, path: str) -> bool:
    for bad in DENY_PREFIX:
        if path == bad or path.startswith(bad + "/") or path.startswith(bad):
            return False
    for m, pre in ALLOW:
        if m == method.upper() and (path == pre or path.startswith(pre)):
            return True
    return False


def install(app) -> None:
    @app.middleware("http")
    async def _perm_guard(request: Request, call_next):
        path = request.url.path
        # 只管 /api/*；页面本身由 site_auth 管，后台由 admin_auth 管
        if not path.startswith("/api/"):
            return await call_next(request)
        try:
            from .site_auth import session_user, is_admin, PUBLIC_PREFIX
        except Exception as _e:                            # noqa: BLE001
            log.warning("权限闸门：无法导入 site_auth：%s: %s", type(_e).__name__, _e)
            return JSONResponse({"detail": "权限校验不可用"}, status_code=503)
        # ★ 公开路径（/api/login、/api/site/state 等）必须直接放行 ——
        #   否则登录接口自己会被本闸门拦成 401，**谁都登不进来**（已踩）。
        if path.startswith(PUBLIC_PREFIX):
            return await call_next(request)
        # 本机 /api/health 必须放行：容器 healthcheck 用它，
        # 且该接口只返回状态，不含任何用户数据（site_auth 里也已有同样的本机白名单）。
        if path == "/api/health":
            try:
                from .site_auth import _is_local
                if _is_local(request):
                    return await call_next(request)
            except Exception:                             # noqa: BLE001
                pass
        u = session_user(request)
        if not u:
            # 未登录交给 site_auth 的 401（它先装，通常到不了这里）
            return JSONResponse({"detail": "未登录", "login": "/login"},
                                status_code=401, headers={"Cache-Control": "no-store"})
        if is_admin(request):
            return await call_next(request)
        if _allowed(request.method, path):
            return await call_next(request)
        log.info("普通账户 %s 被拒绝访问 %s %s", u, request.method, path)
        return JSONResponse({"detail": "该账户无权访问此功能", "user": u},
                            status_code=403, headers={"Cache-Control": "no-store"})
