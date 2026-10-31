"""普通访问账户：口令、授权目录、以及**严格**的可见性判定。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

ITER = 200_000
_CACHE_TTL = 60.0


# ----------------------------------------------------------------- 存储
def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR") or "/app/data")


def _store_path() -> Path:
    return Path(os.getenv("SITE_AUTH_FILE") or (_data_dir() / "site_auth.json"))


def _roots_path() -> Path:
    return Path(os.getenv("SCAN_ROOTS_FILE") or (_data_dir() / "scan_roots.json"))


def _emby_path() -> str:
    return os.getenv("EMBY_DB") or str(_data_dir() / "emby.db")


_load_cache: dict = {"mtime": None, "data": {}}


def _load() -> dict:
    p = _store_path()
    try:
        st = p.stat()
        if _load_cache["mtime"] == st.st_mtime_ns:
            return _load_cache["data"]
        data = json.loads(p.read_text(encoding="utf-8"))
        _load_cache["mtime"] = st.st_mtime_ns
        _load_cache["data"] = data
        return data
    except Exception:                                       # noqa: BLE001
        return _load_cache["data"] or {}


def _save(d: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:                                       # noqa: BLE001
        pass
    tmp.replace(p)
    _load_cache["mtime"] = None
    invalidate()


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, ITER).hex()


def admin_user() -> str:
    return str(_load().get("user") or "admin")


def roots() -> list[dict]:
    """管理员添加的根目录（``scan_roots.json``），是可选授权目录的**唯一来源**。"""
    try:
        raw = json.loads(_roots_path().read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return []
    if isinstance(raw, dict):
        raw = raw.get("roots") or []
    out = []
    for r in (raw or []):
        if not isinstance(r, dict):
            continue
        cid = str(r.get("cid") or r.get("id") or "").strip()
        if cid:
            out.append({"cid": cid, "name": str(r.get("name") or cid)})
    return out


# ----------------------------------------------------------------- 账户
def _public(a: dict) -> dict:
    """对外暴露的账户信息（不含任何哈希）。"""
    return {
        "user": str(a.get("u") or ""),
        "dirs": [str(x) for x in (a.get("dirs") or [])],
        "note": str(a.get("note") or ""),
        "enabled": bool(a.get("enabled", True)),
        "created": int(a.get("created") or 0),
        "changed_at": int(a.get("changed_at") or 0),
    }


def list_accounts() -> list[dict]:
    return [_public(a) for a in (_load().get("accounts") or []) if isinstance(a, dict)]


def get_account(user: str) -> Optional[dict]:
    user = (user or "").strip()
    for a in (_load().get("accounts") or []):
        if isinstance(a, dict) and str(a.get("u") or "") == user:
            return a
    return None


def verify_login(user: str, pw: str) -> Optional[dict]:
    """校验登录。

    ★ req-pwonly：**只凭口令登录**。不给账户名时：先试管理员，再**逐个**试所有启用的
    普通账户，谁的口令对上就是谁。

    为什么不做账户名输入框：用户明确要求"只做口令登录"，访客只需记自己的口令。
    代价是 PBKDF2 要试 N 次（20 万轮 × 账户数；10 个账户约 0.5s，可接受），
    且管理员优先 → 管理员口令总是最快命中。
    """
    user = (user or "").strip()
    pw = (pw or "").strip()
    d = _load()

    def _admin_ok() -> bool:
        if not pw or not d.get("hash"):
            return False
        try:
            salt = bytes.fromhex(str(d.get("salt") or ""))
        except Exception:                                   # noqa: BLE001
            return False
        return hmac.compare_digest(_hash(pw, salt), str(d.get("hash") or ""))

    def _acc_ok(ac: dict) -> bool:
        if not pw or not ac or ac.get("enabled") is False:
            return False
        try:
            salt = bytes.fromhex(str(ac.get("salt") or ""))
        except Exception:                                   # noqa: BLE001
            return False
        return hmac.compare_digest(_hash(pw, salt), str(ac.get("hash") or ""))

    # ---- 明确给了账户名：只校验那一个（保留给旧接口 / 工具用）----
    if user:
        if user == admin_user():
            return ({"u": admin_user(), "admin": True, "dirs": [], "enabled": True}
                    if _admin_ok() else None)
        ac = get_account(user)
        return dict(ac, admin=False) if _acc_ok(ac) else None

    # ---- 只给口令：管理员优先，再逐个普通账户 ----
    if _admin_ok():
        return {"u": admin_user(), "admin": True, "dirs": [], "enabled": True}
    for ac in (d.get("accounts") or []):
        if isinstance(ac, dict) and _acc_ok(ac):
            return dict(ac, admin=False)
    return None


def epoch_of(user: str) -> int:
    """会话代次：改口令/改授权/停用都 +1，让旧会话立刻失效。"""
    user = (user or "").strip()
    if not user or user == admin_user():
        return int(_load().get("epoch") or 0)
    a = get_account(user)
    return int(a.get("epoch") or 0) if a else -1


def session_secret() -> str:
    return str(_load().get("secret") or "")


def add_account(user: str, pw: str, dirs, note: str = "") -> dict:
    user = (user or "").strip()
    if not user:
        raise ValueError("账户名不能为空")
    if user == admin_user():
        raise ValueError("不能与管理员同名")
    if not (2 <= len(user) <= 32):
        raise ValueError("账户名长度需在 2~32 之间")
    if any(c in user for c in " \t/:\\\"'<>|"):
        raise ValueError("账户名不能含空格或 / : \\ \" ' < > |")
    if len((pw or "").strip()) < 4:
        raise ValueError("口令至少 4 位")
    if get_account(user):
        raise ValueError("账户已存在：%s" % user)
    clean = validate_dirs(dirs)
    salt = secrets.token_bytes(16)
    a = {
        "u": user,
        "salt": salt.hex(),
        "hash": _hash((pw or "").strip(), salt),
        "iter": ITER,
        "epoch": 1,
        "dirs": clean,
        "note": note or "",
        "enabled": True,
        "created": int(time.time()),
        "changed_at": int(time.time()),
    }
    d = _load()
    accs = d.get("accounts") or []
    accs.append(a)
    d["accounts"] = accs
    d.setdefault("secret", secrets.token_hex(32))
    _save(d)
    return _public(a)


def update_account(user: str, *, pw: str = "", dirs=None, note=None,
                   enabled=None) -> dict:
    user = (user or "").strip()
    d = _load()
    accs = d.get("accounts") or []
    for a in accs:
        if str(a.get("u") or "") != user:
            continue
        if pw:
            if len(pw.strip()) < 4:
                raise ValueError("口令至少 4 位")
            salt = secrets.token_bytes(16)
            a["salt"] = salt.hex()
            a["hash"] = _hash(pw.strip(), salt)
        if dirs is not None:
            a["dirs"] = validate_dirs(dirs)
        if note is not None:
            a["note"] = str(note)
        if enabled is not None:
            a["enabled"] = bool(enabled)
        a["epoch"] = int(a.get("epoch") or 0) + 1     # 任何改动都让旧会话失效
        a["changed_at"] = int(time.time())
        d["accounts"] = accs
        _save(d)
        return _public(a)
    raise ValueError("账户不存在：%s" % user)


def delete_account(user: str) -> bool:
    user = (user or "").strip()
    d = _load()
    accs = [a for a in (d.get("accounts") or []) if str(a.get("u") or "") != user]
    if len(accs) == len(d.get("accounts") or []):
        return False
    d["accounts"] = accs
    _save(d)
    return True


# ----------------------------------------------------------------- 授权目录校验
def _all_root_ids() -> list[str]:
    return [r["cid"] for r in roots()]


def validate_dirs(dirs) -> list[str]:
    """**严格**校验：每个目录都必须在管理员根目录的子树内，否则直接报错。

    ⛔ 不做"静默丢弃"——静默丢弃会让管理员以为选上了、实际没生效。
    """
    want = [str(x).strip() for x in (dirs or []) if str(x).strip()]
    if not want:
        return []
    root_ids = _all_root_ids()
    if not root_ids:
        raise ValueError("还没有添加任何根目录，请先到「扫描管理 → 扫描目录」添加")
    allowed = _closure(root_ids)
    bad = [x for x in want if x not in allowed]
    if bad:
        raise ValueError("这些目录不在已添加的根目录下，不能授权：%s"
                         % "、".join(bad[:5]))
    seen, out = set(), []
    for x in want:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ----------------------------------------------------------------- 可见集合
_lock = threading.Lock()
_cache: dict = {}          # user -> (ts, folders_count, frozenset)


def _folders_count() -> int:
    try:
        con = sqlite3.connect(_emby_path(), timeout=15)
        try:
            return int(con.execute("SELECT COUNT(*) FROM folders").fetchone()[0])
        finally:
            con.close()
    except Exception:                                       # noqa: BLE001
        return -1


def _closure(root_ids: list[str]) -> set:
    """目录子树闭包（含起点自身）。走 ``idx_folders_parent``，实测 6,564 目录 24ms。"""
    root_ids = [str(x) for x in (root_ids or []) if str(x).strip()]
    if not root_ids:
        return set()
    con = sqlite3.connect(_emby_path(), timeout=30)
    try:
        con.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in root_ids)
        sql = ("WITH RECURSIVE sub(id) AS ("
               "  SELECT id FROM folders WHERE id IN (%s) "
               "  UNION "
               "  SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id"
               ") SELECT id FROM sub" % marks)
        return {str(r["id"]) for r in con.execute(sql, tuple(root_ids))}
    finally:
        con.close()


def authorized_roots(user: str) -> list[str]:
    """该账户授权目录的**原始选择**（数量少，适合塞进 SQL 的递归 CTE）。"""
    a = get_account(user)
    return [str(x) for x in ((a or {}).get("dirs") or []) if str(x).strip()]


def allowed_folder_ids(user: str) -> Optional[set]:
    """该账户可见的目录 id 集合。

    返回 ``None`` **只代表管理员**（不受限）。普通账户一律返回集合，
    集合可能是空的（= 什么都看不到）——**空集合与 None 语义完全不同**，调用方必须区分。
    """
    user = (user or "").strip()
    if not user or user == admin_user():
        return None
    a = get_account(user)
    if not a or not bool(a.get("enabled", True)):
        return set()
    key = user
    cnt = _folders_count()
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] == cnt and (now - hit[0]) < _CACHE_TTL:
            return hit[2]
    ids = _closure([str(x) for x in (a.get("dirs") or []) if str(x).strip()])
    with _lock:
        _cache[key] = (now, cnt, frozenset(ids))
    return set(ids)


def invalidate(user: str = "") -> None:
    with _lock:
        if user:
            _cache.pop(user.strip(), None)
        else:
            _cache.clear()


def can_see_folder(user: str, folder_id: str) -> bool:
    ids = allowed_folder_ids(user)
    if ids is None:
        return True
    return str(folder_id or "") in ids


def can_see_item(user: str, item_parent_id: str) -> bool:
    return can_see_folder(user, item_parent_id)


def sql_filter(user: str, column: str = "parent_id") -> tuple[str, tuple]:
    """给 SQL 用的过滤片段。

    * 管理员 → ``("", ())``（不过滤）
    * 普通账户 → ``(" AND col IN (递归CTE)", (root_id, ...))`` ——
      **只传少数授权根 id**，让 SQLite 自己展开子树，避免把 6,564 个 id 塞进 IN。

    ⛔ 没有授权目录时必须生成 ``AND 1=0``（什么都看不到），
    **绝不能生成空串**（空串 = 不过滤 = 泄露全部）。
    """
    user = (user or "").strip()
    if not user or user == admin_user():
        return "", ()
    roots_ = authorized_roots(user)
    if not roots_:
        return " AND 1=0", ()
    sub = ("(WITH RECURSIVE sub(id) AS ("
           "  SELECT id FROM folders WHERE id IN (%s) "
           "  UNION SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id"
           ") SELECT id FROM sub)" % ",".join("?" for _ in roots_))
    return " AND %s IN %s" % (column, sub), tuple(roots_)


def sql_filter_folders(user: str, column: str = "id") -> tuple[str, tuple]:
    """给 ``folders`` 表自身用的过滤（列名默认 ``id``）。"""
    return sql_filter(user, column)
