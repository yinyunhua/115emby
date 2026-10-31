"""已排除条目：确认"无法正常显示"的条目（损坏文件 / 假视频 / 拿不到缩略图）。

与 ``hide_rules`` 的分工
------------------------
* ``hide_rules``   管**广告/引流**：按关键词与体积**规则**判断，入库时就丢。
* ``hidden_store`` 管**文件本身不行**：人工/程序确认后进清单，界面一律不显示。

两处作用
--------
1. ``items.hidden=1`` → 所有列表/计数/搜索的 SQL 通过 ``db.visible_cond()`` 过滤掉；
2. ``data/hidden_items.json`` → **重新扫描时仍然标 hidden**（条目被清库重建也不丢），
   同时记录 ``reason`` 供恢复工具查看。

为什么不用 ``hide_rules`` 的 block_names.json
--------------------------------------------
那份是"广告黑名单"，语义不同（用户可能要分别审阅）；而且这里的判定依据是
"缩略图抓取结果"，与名字无关，混在一起以后没法分辨。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_STATE = {"at": -1.0, "ids": set(), "pairs": set(), "items": []}


def path() -> Path:
    return Path(os.getenv("DATA_DIR") or "/app/data") / "hidden_items.json"


def _load() -> None:
    p = path()
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _STATE["at"]:
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("items") or []
        _STATE["items"] = rows
        _STATE["ids"] = {str(r.get("id")) for r in rows if r.get("id")}
        _STATE["pairs"] = {(str(r.get("name", "")).lower(), int(r.get("size") or 0))
                           for r in rows if r.get("name")}
        _STATE["at"] = m
    except Exception:                                      # noqa: BLE001
        pass


def reload() -> int:
    _STATE["at"] = -1.0
    _load()
    return len(_STATE["ids"])


def is_hidden(pid: str = "", name: str = "", size: int = 0) -> bool:
    """这条是否已被排除（按 id 优先，其次按 (名字, 体积)）。"""
    _load()
    if pid and str(pid) in _STATE["ids"]:
        return True
    if name:
        return (str(name).lower(), int(size or 0)) in _STATE["pairs"]
    return False


def all_items() -> list[dict]:
    _load()
    return list(_STATE["items"])


def count() -> int:
    _load()
    return len(_STATE["items"])


def _write(rows: list[dict]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)
    reload()


def add(entries) -> int:
    """追加排除项。entries: 可迭代的 (id, name, size, reason)。已存在的按 id 去重。"""
    _load()
    cur = {str(r.get("id")): dict(r) for r in _STATE["items"] if r.get("id")}
    now = int(time.time())
    added = 0
    for pid, name, size, reason in entries:
        pid = str(pid or "")
        if not pid:
            continue
        if pid in cur:
            continue
        cur[pid] = {"id": pid, "name": name or "", "size": int(size or 0),
                    "reason": reason or "", "at": now}
        added += 1
    _write(list(cur.values()))
    return added


def remove(keys) -> int:
    """恢复：keys 可以是 id，也可以是名字片段（不区分大小写）。"""
    _load()
    keys_l = [str(k).strip().lower() for k in keys if str(k).strip()]
    keep, dropped = [], 0
    for r in _STATE["items"]:
        rid = str(r.get("id") or "").lower()
        rnm = str(r.get("name") or "").lower()
        if any(k == rid or (len(k) >= 3 and k in rnm) for k in keys_l):
            dropped += 1
            continue
        keep.append(r)
    if dropped:
        _write(keep)
    return dropped
