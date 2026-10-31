"""后台「账户管理」接口：普通访问账户的增删改 + 授权目录选择器。

* ``GET    /admin/accounts``              列出账户（不含哈希）
* ``POST   /admin/accounts``              新增 / 修改（``{user, pw?, dirs?, note?, enabled?}``）
* ``DELETE /admin/accounts/{user}``       删除
* ``GET    /admin/accounts/roots``        管理员根目录（可选目录的**唯一来源**）
* ``GET    /admin/accounts/dirs?parent=`` 逐级列子目录（**只列根目录子树内的**）

⛔ 目录选择器只走**本地库**（``folders`` 表），不实时问上游 ——
实时问上游会绕过"只允许根目录之下"的约束。
⛔ 所有校验失败都返回**明确错误**，不静默丢弃（管理员要看得见没生效）。
"""
from __future__ import annotations

import logging

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import accounts as acc
from . import index_crypto as ic

log = logging.getLogger("album.adminaccounts")


def install(app) -> None:
    guard = getattr(app.state, "admin_guard", None)
    if guard is None:
        return
    from fastapi import Depends
    deps = [Depends(guard)]

    def _db():
        return getattr(app.state, "db", None)

    @app.get("/admin/accounts", dependencies=deps, name="accounts-list")
    async def accounts_list() -> JSONResponse:
        """★ req-accdirs：每个账户的 ``dirs`` 除了 id，再给一份 ``dir_names``。

        后台账户列表要在右侧显示"可访问目录"的**名字**（原来只有"授权目录 N 个"，
        管理员根本看不出到底授权了哪几个目录）。
        """
        accounts = acc.list_accounts()
        db = _db()
        names: dict[str, str] = {}
        if db is not None:
            # ⛔ folders 表**没有 name 列**，只有加密的 name_enc —— 必须 safe_unseal 解密。
            #   第一版写成 row["name"] 取不到 → 回退成 id，界面显示成一串数字。
            from . import index_crypto as _ic
            for a in accounts:
                for cid in (a.get("dirs") or []):
                    if cid in names:
                        continue
                    try:
                        row = db.folder(str(cid))
                        nm = _ic.safe_unseal((row["name_enc"] if row else "") or "")
                        names[cid] = nm or str(cid)
                    except Exception:                       # noqa: BLE001
                        names[cid] = str(cid)
        for a in accounts:
            a["dir_names"] = [names.get(c, c) for c in (a.get("dirs") or [])]
        return JSONResponse({
            "admin": acc.admin_user(),
            "accounts": accounts,
            "roots": acc.roots(),
        })

    @app.get("/admin/accounts/roots", dependencies=deps, name="accounts-roots")
    async def accounts_roots() -> JSONResponse:
        """可选授权目录的**唯一来源**：管理员在「扫描目录」里添加的根目录。"""
        return JSONResponse({"roots": acc.roots()})

    @app.get("/admin/accounts/dirs", dependencies=deps, name="accounts-dirs")
    async def accounts_dirs(parent: str = Query("")) -> JSONResponse:
        """逐级列子目录。``parent`` 为空 → 返回所有根目录。

        ⛔ 只在**根目录子树内**列举；用本地库，不实时问上游。
        """
        db = _db()
        if db is None:
            raise HTTPException(503, "数据库未就绪")
        roots = acc.roots()
        if not parent:
            return JSONResponse({"parent": "", "folders": [
                {"id": r["cid"], "name": r["name"], "root": True} for r in roots]})
        # 校验 parent 在某个根目录的子树内
        root_ids = [r["cid"] for r in roots]
        if not root_ids:
            raise HTTPException(400, "还没有添加任何根目录")
        if parent not in acc._closure(root_ids):
            raise HTTPException(400, "该目录不在已添加的根目录下")
        rows = db.list_folders(parent)
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "name": ic.safe_unseal(r["name_enc"] or ""),
                "root": False,
            })
        return JSONResponse({"parent": parent, "folders": out})

    @app.get("/admin/accounts/path", dependencies=deps, name="accounts-path")
    async def accounts_path(id: str = Query("")) -> JSONResponse:
        """返回从**根目录**到 ``id`` 的祖先链（含自身）。

        用途：账户已授权的目录可能在很深的层级，前端需要逐级展开才能让管理员
        看到"到底选了哪几个"。链上每一级都校验在根目录子树内。
        """
        db = _db()
        if db is None:
            raise HTTPException(503, "数据库未就绪")
        target = (id or "").strip()
        if not target:
            raise HTTPException(400, "缺少 id")
        root_ids = [r["cid"] for r in acc.roots()]
        if not root_ids:
            raise HTTPException(400, "还没有添加任何根目录")
        allowed = acc._closure(root_ids)
        if target not in allowed:
            raise HTTPException(400, "该目录不在已添加的根目录下")
        chain: list[str] = []
        cur = target
        # 用 folders 表往上走（最多 64 层，防环）
        for _ in range(64):
            chain.append(cur)
            row = db.folder(cur)
            if not row:
                break
            pid = str(row["parent_id"] or "-1")
            if pid in ("-1", "") or pid == cur:
                break
            if pid not in allowed:
                break
            cur = pid
        chain.reverse()                      # 根 → 目标
        return JSONResponse({"id": target, "path": chain})

    @app.post("/admin/accounts", dependencies=deps, name="accounts-save")
    async def accounts_save(payload: dict = Body(...)) -> JSONResponse:
        user = str((payload or {}).get("user") or "").strip()
        pw = str((payload or {}).get("pw") or "")
        note = (payload or {}).get("note")
        enabled = (payload or {}).get("enabled")
        dirs = (payload or {}).get("dirs") if "dirs" in (payload or {}) else None
        if not user:
            raise HTTPException(400, "缺少账户名")
        try:
            if acc.get_account(user):
                out = acc.update_account(user, pw=pw, dirs=dirs,
                                         note=(None if note is None else str(note)),
                                         enabled=(None if enabled is None else bool(enabled)))
            else:
                if len(pw.strip()) < 4:
                    raise ValueError("口令至少 4 位")
                out = acc.add_account(user, pw, dirs or [], str(note or ""))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        acc.invalidate(user)
        log.info("账户已保存：%s（授权目录 %d 个）", user, len(out.get("dirs") or []))
        return JSONResponse({"ok": True, "account": out})

    @app.delete("/admin/accounts/{user}", dependencies=deps, name="accounts-delete")
    async def accounts_delete(user: str) -> JSONResponse:
        try:
            ok = acc.delete_account(user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not ok:
            raise HTTPException(404, "账户不存在")
        acc.invalidate(user)
        log.info("账户已删除：%s", user)
        return JSONResponse({"ok": True, "user": user})
