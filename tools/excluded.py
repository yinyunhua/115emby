"""已排除条目管理（损坏/无法正常显示的条目）。

这些条目：
* 界面一律不显示（目录浏览 / 随机发现 / 全部图片 / 全部视频 / 最近添加 / 搜索 / 目录封面）；
* 重新扫描时不会再入库；
* 看板按"已自动排除"统计。

用法
----
  python tools/excluded.py list                      # 列出全部已排除条目
  python tools/excluded.py list -k 广告              # 按名字片段过滤
  python tools/excluded.py restore <id|名字片段>...   # 恢复（重新显示）
  python tools/excluded.py add <id> [<id>...]        # 手动排除
  python tools/excluded.py stats                     # 只打印条数

恢复后若还想让缩略图重新尝试生成：
  python tools/thumb_prewarm.py reset --state excluded
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import hidden_store                              # noqa: E402
from app import index_crypto as ic                        # noqa: E402

EMBY = os.getenv("EMBY_DB") or "/app/data/emby.db"


def _name(con, pid: str) -> str:
    try:
        row = con.execute("SELECT name_enc FROM items WHERE id=?", (pid,)).fetchone()
        return ic.safe_unseal(row[0] or "") if row else ""
    except Exception:                                     # noqa: BLE001
        return ""


def _set_hidden(pids, value: int) -> int:
    if not pids:
        return 0
    con = sqlite3.connect(EMBY, timeout=20)
    try:
        q = ",".join("?" for _ in pids)
        cur = con.execute("UPDATE items SET hidden=? WHERE id IN (%s)" % q,
                          (int(value), *pids))
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def cmd_list(args) -> int:
    rows = hidden_store.all_items()
    kw = (args.keyword or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in str(r.get("name", "")).lower()]
    con = sqlite3.connect(EMBY, timeout=20)
    try:
        print("已排除条目 %d 条（hidden_items.json）%s" % (
            len(rows), ("｜过滤：%s" % args.keyword) if kw else ""))
        print("%-46s %10s  %-22s %s" % ("id", "size", "reason", "name"))
        for r in sorted(rows, key=lambda x: str(x.get("name", ""))):
            nm = str(r.get("name") or "") or _name(con, str(r.get("id")))
            print("%-46s %10d  %-22s %s" % (
                str(r.get("id"))[:46], int(r.get("size") or 0),
                str(r.get("reason") or "")[:22], nm[:60]))
    finally:
        con.close()
    return 0


def cmd_stats(args) -> int:
    print(hidden_store.count())
    return 0


def _reset_thumb_state(pids) -> int:
    """把 thumb_src 里 state='excluded' 的条目打回 'failed'（attempts=0），
    这样夜间预热会**重新尝试**生成缩略图（恢复的意义就在这里）。"""
    if not pids:
        return 0
    db = os.getenv("THUMB_DB") or "/app/data/thumbs.db"
    try:
        con = sqlite3.connect(db, timeout=20)
    except sqlite3.Error:
        return 0
    try:
        q = ",".join("?" for _ in pids)
        cur = con.execute(
            "UPDATE thumb_src SET state='failed', attempts=0, last_err='人工恢复为可见', updated=? "
            "WHERE state='excluded' AND pid IN (%s)" % q,
            (int(_time.time()), *pids))
        con.commit()
        return cur.rowcount
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def cmd_restore(args) -> int:
    keys = list(args.keys or [])
    if not keys:
        print("要恢复哪些？给 id 或名字片段", file=sys.stderr)
        return 2
    rows = hidden_store.all_items()
    kl = [k.strip().lower() for k in keys]
    hit_ids = []
    for r in rows:
        rid = str(r.get("id") or "").lower()
        rnm = str(r.get("name") or "").lower()
        if any(k == rid or (len(k) >= 3 and k in rnm) for k in kl):
            hit_ids.append(str(r.get("id")))
    n = hidden_store.remove(keys)
    m = _set_hidden(hit_ids, 0)
    t = _reset_thumb_state(hit_ids)
    print("清单移除 %d 条；items.hidden 置 0 → %d 行；缩略图队列打回待重试 → %d 行" % (n, m, t))
    print("提示：看板的「已自动排除」有 30 秒缓存，稍等再看；夜里预热会重新尝试生成缩略图。")
    return 0


def _mark_thumb_excluded(pids) -> int:
    """把 thumb_src 里这些条目标成 state='excluded'（与 restore 对称）。"""
    if not pids:
        return 0
    db = os.getenv("THUMB_DB") or "/app/data/thumbs.db"
    try:
        con = sqlite3.connect(db, timeout=20)
    except sqlite3.Error:
        return 0
    try:
        q = ",".join("?" for _ in pids)
        cur = con.execute(
            "UPDATE thumb_src SET state='excluded', updated=? "
            "WHERE state <> 'excluded' AND pid IN (%s)" % q,
            (int(_time.time()), *pids))
        con.commit()
        return cur.rowcount
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def cmd_add(args) -> int:
    con = sqlite3.connect(EMBY, timeout=20)
    try:
        entries = []
        for pid in args.ids:
            row = con.execute("SELECT size FROM items WHERE id=?", (pid,)).fetchone()
            if not row:
                print("跳过（库里没有）：%s" % pid, file=sys.stderr)
                continue
            entries.append((pid, _name(con, pid), int(row[0] or 0), args.reason or "手动排除"))
    finally:
        con.close()
    n = hidden_store.add(entries)
    pids = [e[0] for e in entries]
    m = _set_hidden(pids, 1)
    t = _mark_thumb_excluded(pids)
    print("清单新增 %d 条；items.hidden 置 1 → %d 行；缩略图队列转 excluded → %d 行" % (n, m, t))
    print("提示：看板的「已自动排除」有 30 秒缓存，稍等再看。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="已排除条目管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("list"); p1.add_argument("-k", "--keyword", default="")
    p1.set_defaults(func=cmd_list)
    sub.add_parser("stats").set_defaults(func=cmd_stats)
    p2 = sub.add_parser("restore"); p2.add_argument("keys", nargs="+")
    p2.set_defaults(func=cmd_restore)
    p3 = sub.add_parser("add"); p3.add_argument("ids", nargs="+")
    p3.add_argument("--reason", default="")
    p3.set_defaults(func=cmd_add)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
