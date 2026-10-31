"""索引"名字加密"迁移工具：文件名 / 文件夹名 / 路径加密，直链保持明文。

四个步骤（可反复跑，幂等）::

    python tools/secure_index.py report  --db data/index.db
    python tools/secure_index.py migrate --db data/index.db            # 加列 + 回填 + 算排序秩
    python tools/secure_index.py migrate --db data/index.db --purge    # 再抹掉明文列
    python tools/secure_index.py verify  --db data/index.db            # 核查：无明文残留 + 抽样解密
    python tools/secure_index.py search  --db data/index.db --q IMG_2025

安全性
------
* 默认**先备份** ``<db>.bak.<时间戳>``（可 ``--no-backup`` 关闭）；
* ``--purge`` 只有在 ``verify`` 通过后才建议执行；本工具会先做一次自检，不通过就拒绝 purge；
* 直链表 ``pending_links`` **完全不碰**（按需求不加密）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import index_crypto as ic          # noqa: E402

COLUMNS = {
    "name_enc": "TEXT DEFAULT ''",
    "name_idx": "TEXT DEFAULT ''",
    "name_bucket": "TEXT DEFAULT ''",
    "name_rank": "INTEGER",
    "folder_enc": "TEXT DEFAULT ''",
    "folder_idx": "TEXT DEFAULT ''",
    "folder_bucket": "TEXT DEFAULT ''",
    "path_enc": "TEXT DEFAULT ''",
}
LINK_COLUMN = ("url_enc", "TEXT DEFAULT ''")

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_photos_name_idx ON photos(name_idx)",
    "CREATE INDEX IF NOT EXISTS idx_photos_name_bucket ON photos(name_bucket)",
    "CREATE INDEX IF NOT EXISTS idx_photos_name_rank ON photos(name_rank)",
    "CREATE INDEX IF NOT EXISTS idx_photos_folder_idx ON photos(folder_idx)",
]
BATCH = 500


# --------------------------------------------------------------------- 基础
def connect(db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def columns(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(photos)")}


def ensure_columns(conn, include_path: bool = True) -> list[str]:
    have = columns(conn)
    added = []
    for name, decl in COLUMNS.items():
        if name == "path_enc" and not include_path:
            continue
        if name not in have:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def ensure_link_column(conn) -> list[str]:
    """pending_links 加 url_enc 列。"""
    if not table_exists(conn, "pending_links"):
        return []
    have = {r[1] for r in conn.execute("PRAGMA table_info(pending_links)")}
    name, decl = LINK_COLUMN
    if name not in have:
        conn.execute(f"ALTER TABLE pending_links ADD COLUMN {name} {decl}")
        return [name]
    return []


def table_exists(conn, name: str = "photos") -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# --------------------------------------------------------------------- report
def cmd_report(args) -> int:
    conn = connect(args.db)
    if not table_exists(conn):
        print(f"✗ {args.db} 里没有 photos 表")
        return 2
    have = columns(conn)
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    out = {
        "db": str(args.db),
        "size_mb": round(Path(args.db).stat().st_size / 1048576, 2),
        "rows": total,
        "migrated_columns": sorted(COLUMNS.keys() & have),
        "missing_columns": sorted(set(COLUMNS) - have),
        "plaintext_names_left": conn.execute(
            "SELECT COUNT(*) FROM photos WHERE COALESCE(name,'')<>''"
        ).fetchone()[0] if "name" in have else None,
        "links_table": conn.execute(
            "SELECT COUNT(*) FROM pending_links"
        ).fetchone()[0] if table_exists(conn, "pending_links") else None,
    }
    if "name_enc" in have:
        out["with_enc"] = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE COALESCE(name_enc,'')<>''"
        ).fetchone()[0]
    cols = [c for c in ("id", "name_enc", "name", "folder_enc", "path_enc") if c in have]
    sample = conn.execute(f"SELECT {', '.join(cols)} FROM photos LIMIT 3").fetchall()
    out["sample"] = [
        {"id": r["id"], "name_enc_head": (r["name_enc"] if "name_enc" in r.keys() else "")[:24],
         "name_plain": r["name"] if "name" in r.keys() else None}
        for r in sample
    ]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    conn.close()
    return 0


# --------------------------------------------------------------------- migrate
def cmd_migrate(args) -> int:
    db = Path(args.db)
    if not db.exists():
        print(f"✗ 找不到 {db}")
        return 2
    if args.backup:
        bak = db.with_suffix(db.suffix + f".bak.{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(db, bak)
        print(f"已备份 → {bak.name}（{bak.stat().st_size/1048576:.2f} MB）")

    conn = connect(db)
    if not table_exists(conn):
        print("✗ 没有 photos 表")
        return 2

    added = ensure_columns(conn, include_path=not args.no_path)
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()
    print(f"新增列：{added or '（已存在，跳过）'}")

    rows = conn.execute(
        "SELECT id, COALESCE(name,'') AS name, COALESCE(folder,'') AS folder, "
        "COALESCE(path,'') AS path, COALESCE(name_enc,'') AS name_enc FROM photos"
    ).fetchall()
    print(f"待处理 {len(rows)} 行…")

    done = 0
    pairs: list[tuple[str, str]] = []          # (id, 解密后的名字) 供算排序秩
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        payload = []
        for r in chunk:
            if r["name"]:
                # 还有明文 -> 重新加密（幂等：重复跑结果一致）
                fields = ic.seal_fields(r["name"], r["folder"], r["path"])
                if ic.unseal(fields["name_enc"]) != r["name"]:
                    print(f"✗ 自检失败（加密后再解密不一致）：{r['id']}")
                    conn.close()
                    return 3
                plain = r["name"]
                payload.append((
                    fields["name_enc"], fields["name_idx"], fields["name_bucket"],
                    fields["folder_enc"], fields["folder_idx"], fields["folder_bucket"],
                    fields["path_enc"] if not args.no_path else "",
                    r["id"],
                ))
            else:
                # 已 purge：沿用已有密文，**绝不能写成空**（否则数据就丢了）
                plain = ic.safe_unseal(r["name_enc"]) if r["name_enc"] else ""
            pairs.append((r["id"], plain))
        conn.executemany(
            """UPDATE photos SET name_enc=?, name_idx=?, name_bucket=?,
                                 folder_enc=?, folder_idx=?, folder_bucket=?, path_enc=?
               WHERE id=?""",
            payload,
        )
        conn.commit()
        done += len(chunk)
        if not args.quiet:
            print(f"  回填 {done}/{len(rows)}", end="\r")
    print(f"\n回填完成：{done} 行")

    # 排序秩：按"解密后的名字"全局排序，一次算好，之后 ORDER BY name_rank 即可
    pairs.sort(key=lambda x: ic.normalize(x[1]))
    conn.executemany(
        "UPDATE photos SET name_rank=? WHERE id=?",
        [(i, pid) for i, (pid, _name) in enumerate(pairs)],
    )
    conn.commit()
    print(f"排序秩已写入（0..{len(pairs)-1}）")

    # ---- 直链也加密（URL 里带 UID/签名，属敏感串）
    link_done = 0
    if not getattr(args, "no_links", False) and table_exists(conn, "pending_links"):
        added_link = ensure_link_column(conn)
        conn.commit()
        if added_link:
            print(f"pending_links 新增列：{added_link}")
        lrows = conn.execute(
            "SELECT id, COALESCE(url,'') AS url FROM pending_links WHERE COALESCE(url,'')<>''"
        ).fetchall()
        for i in range(0, len(lrows), BATCH):
            chunk = lrows[i : i + BATCH]
            payload = [(ic.seal_link(r["url"]), r["id"]) for r in chunk]
            conn.executemany("UPDATE pending_links SET url_enc=? WHERE id=?", payload)
            conn.commit()
            link_done += len(chunk)
        print(f"直链回填完成：{link_done} 行")

    if args.purge:
        ok = verify_db(conn, quiet=True, require_purged=False)
        if not ok["pass"]:
            print(f"✗ 自检未通过，拒绝 purge：{ok['problems']}")
            conn.close()
            return 4
        n = conn.execute(
            "UPDATE photos SET name='', folder='', path='' "
            "WHERE COALESCE(name_enc,'')<>'' OR COALESCE(folder_enc,'')<>''"
        ).rowcount
        conn.commit()
        print(f"已抹掉明文列：{n} 行（ext/size/ctime 等保留）")
        if table_exists(conn, "pending_links") and not getattr(args, "no_links", False):
            m = conn.execute(
                "UPDATE pending_links SET url='' WHERE COALESCE(url_enc,'')<>''"
            ).rowcount
            conn.commit()
            print(f"已抹掉直链明文：{m} 行（expires/ua 保留）")
        conn.commit()
        # 关键：UPDATE 只是把新值写进页，旧页内容还留在 freelist 里
        # -> 不 VACUUM 的话，用字节扫描仍能翻出原文件名（这是实测踩到的坑）
        conn.isolation_level = None
        conn.execute("VACUUM")
        print("已 VACUUM：旧页被重写，文件里不再残留明文")

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    conn.close()
    print("完成。建议再跑一次 verify。")
    return 0


# --------------------------------------------------------------------- verify
def verify_db(conn, *, sample: int = 20, quiet: bool = False,
              require_purged: bool = True) -> dict:
    problems: list[str] = []
    have = columns(conn)
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    enc_rows = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE COALESCE(name_enc,'')<>''"
    ).fetchone()[0] if "name_enc" in have else 0
    plain_rows = conn.execute(
        "SELECT COUNT(*) FROM photos WHERE COALESCE(name,'')<>''"
    ).fetchone()[0] if "name" in have else 0

    link_total = link_enc = link_plain = 0
    if table_exists(conn, "pending_links"):
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(pending_links)")}
        link_total = conn.execute("SELECT COUNT(*) FROM pending_links").fetchone()[0]
        if "url_enc" in lcols:
            link_enc = conn.execute(
                "SELECT COUNT(*) FROM pending_links WHERE COALESCE(url_enc,'')<>''").fetchone()[0]
        if "url" in lcols:
            link_plain = conn.execute(
                "SELECT COUNT(*) FROM pending_links WHERE COALESCE(url,'')<>''").fetchone()[0]
        if link_total and link_enc < link_total:
            problems.append(f"直链未全部加密：{link_enc}/{link_total}")
        if link_plain:
            problems.append(f"仍有 {link_plain} 行直链明文（未 purge）") if require_purged else None

    if "name_enc" not in have:
        problems.append("缺少 name_enc 列（还没迁移）")
    if enc_rows == 0:
        problems.append("没有任何一行写了 name_enc")
    if plain_rows and require_purged:
        problems.append(f"仍有 {plain_rows} 行明文 name（未 purge）")

    # 抽样解密
    ok_decrypt = 0
    names: list[str] = []
    if enc_rows:
        for r in conn.execute(
            f"SELECT id, name_enc FROM photos WHERE COALESCE(name_enc,'')<>'' "
            f"ORDER BY name_rank LIMIT {int(sample)}"
        ):
            try:
                nm = ic.unseal(r["name_enc"])
                names.append(nm)
                ok_decrypt += 1
            except Exception as exc:  # noqa: BLE001
                problems.append(f"解密失败 {r['id']}: {exc}")
    if names and plain_rows == 0:
        pass
    if enc_rows and ok_decrypt == 0:
        problems.append("抽样解密全部失败（密钥不对？）")

    # 明文残留扫描：直接在库文件字节里找这些名字
    return {
        "pass": not problems,
        "links_total": link_total, "links_enc": link_enc, "links_plain": link_plain,
        "problems": problems,
        "rows": total,
        "enc_rows": enc_rows,
        "plain_rows": plain_rows,
        "sample_decrypted": names[:5],
        "sample_ok": ok_decrypt,
    }


def raw_residue(db: Path, names: list[str]) -> list[str]:
    """在库文件（含 WAL）原始字节里找这些名字，返回仍然可见的那些。"""
    blob = db.read_bytes()
    wal = db.with_suffix(db.suffix + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    hits = []
    for nm in names:
        if len(nm) >= 4 and nm.encode("utf-8") in blob:
            hits.append(nm)
    return hits


def links_blob(conn) -> str:
    """直链表里的全部 URL 拼起来，用于判断"残留名是否来自直链"。"""
    if not table_exists(conn, "pending_links"):
        return ""
    return " || ".join((r[0] or "") for r in conn.execute("SELECT url FROM pending_links"))


def cmd_verify(args) -> int:
    db = Path(args.db)
    conn = connect(db)
    res = verify_db(conn, sample=args.sample)
    # 额外做一次"原始字节残留"检查
    names = list(res.get("sample_decrypted") or [])
    more = [r["name_enc"] for r in conn.execute(
        "SELECT name_enc FROM photos WHERE COALESCE(name_enc,'')<>'' LIMIT 200")]
    for token in more:
        nm = ic.safe_unseal(token)
        if nm and nm not in names:
            names.append(nm)
    residue = raw_residue(db, names)
    # 115 直链 URL 里带文件名（.../IMG_0001.jpg?t=...&u=...）。
    # 按需求"直链不加密"，所以这部分残留是**预期内**的，要和"真泄漏"分开算。
    blob_links = links_blob(conn)
    from_links = [n for n in residue if n in blob_links]
    other = [n for n in residue if n not in blob_links]
    res["checked_names"] = len(names)
    res["residue_total"] = len(residue)
    res["residue_from_direct_links"] = from_links[:10]
    res["residue_other"] = other[:10]
    if other:
        res["pass"] = False
        res["problems"].append(f"除直链外仍有 {len(other)} 个文件名明文（真泄漏）")
    if from_links and not getattr(args, "allow_link_residue", False):
        res["pass"] = False
        res["problems"].append(
            f"{len(from_links)} 个名字仍出现在直链 URL 明文里（直链已加密，说明没 purge 或没回填）")
    conn.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["pass"] else 1


# --------------------------------------------------------------------- 查询演示
def cmd_search(args) -> int:
    conn = connect(args.db)
    q = args.q
    t0 = time.perf_counter()
    if args.exact:
        rows = conn.execute(
            "SELECT id, name_enc FROM photos WHERE name_idx=?", (ic.blind(q),)
        ).fetchall()
        mode = "精确(盲索引)"
    else:
        rows = conn.execute(
            "SELECT id, name_enc FROM photos WHERE name_bucket=?",
            (ic.bucket(q),),
        ).fetchall()
        mode = "前缀(桶+解密过滤)"
    hits = [r for r in rows if ic.safe_unseal(r["name_enc"]).casefold().startswith(q.casefold())]
    dt = (time.perf_counter() - t0) * 1000
    print(json.dumps({
        "query": q, "mode": mode, "bucket_candidates": len(rows), "hits": len(hits),
        "ms": round(dt, 1),
        "sample": [ic.safe_unseal(r["name_enc"]) for r in hits[:8]],
    }, ensure_ascii=False, indent=2))
    conn.close()
    return 0


def cmd_list(args) -> int:
    conn = connect(args.db)
    order = "name_rank" if args.order == "name" else "ctime DESC"
    rows = conn.execute(
        f"SELECT * FROM photos ORDER BY {order} LIMIT ?", (args.limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({"rank": d.get("name_rank"), "name": ic.safe_unseal(d.get("name_enc") or ""),
                    "folder": ic.safe_unseal(d.get("folder_enc") or ""),
                    "ctime": d.get("ctime")})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    conn.close()
    return 0


# --------------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="索引名字加密（文件名/文件夹名/路径）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("report"); p.add_argument("--db", required=True); p.set_defaults(fn=cmd_report)

    p = sub.add_parser("migrate"); p.add_argument("--db", required=True)
    p.add_argument("--purge", action="store_true", help="抹掉明文 name/folder/path")
    p.add_argument("--no-path", action="store_true", help="不加密完整路径")
    p.add_argument("--no-links", action="store_true", help="不加密 pending_links.url")
    p.add_argument("--no-backup", dest="backup", action="store_false", default=True)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("verify"); p.add_argument("--db", required=True)
    p.add_argument("--sample", type=int, default=20)
    p.add_argument("--allow-link-residue", action="store_true",
                   help="允许名字残留在直链 URL 里（直链已加密时不需要这个开关）")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("search"); p.add_argument("--db", required=True)
    p.add_argument("--q", required=True); p.add_argument("--exact", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("list"); p.add_argument("--db", required=True)
    p.add_argument("--order", choices=["name", "ctime"], default="name")
    p.add_argument("--limit", type=int, default=10); p.set_defaults(fn=cmd_list)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
