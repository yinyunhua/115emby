"""按"重复标题"分析全库（只读）：找出被批量投放的同一标题。

归一化标题（用来判断"是不是同一个标题"）：
  去掉 98T 水印前缀 / 名字里的网址 / 扩展名 / 复制序号 (1)(2)、-1、_2、[3] / 多余空白与符号，
  再转小写。这样 "N房间精彩直播 xxx-uuv82.com (2).mp4" 和 "...uuv82.com.mp4" 会归成同一组。

输出：分组档位统计 + Top 组（按条数 / 按体积）+ CSV（组>=3 的条目）。
"""
import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/app")
from app import index_crypto as ic                      # noqa: E402

DB = os.path.join(os.getenv("DATA_DIR") or "/app/data", "emby.db")
WM = re.compile(r"^\s*(?:www\.)?98t\.(?:la|com)@?\s*", re.I)
URL = re.compile(r"(?:[a-z0-9][a-z0-9\-]{1,30}\.)+(?:com|net|la|xyz|cc|top|vip|club|tv|me|info|"
                 r"site|online|shop|app|cn|org|biz|pro|icu|fun|live|one|link|buzz|wang)\b", re.I)
COPY = re.compile(r"\s*(?:[\(\[]\s*\d{1,3}\s*[\)\]]|[-_—]\s*\d{1,3})\s*$")
PUNCT = re.compile(r"[\s\.\-_—·、,，。:：;；!！\?？\+&'\"“”‘’\(\)\[\]【】<>《》]+")


def norm(name: str) -> str:
    s = (name or "").lower()
    s = WM.sub("", s)
    s = URL.sub(" ", s)
    s = re.sub(r"\.[a-z0-9]{1,5}$", "", s)          # 去扩展名
    for _ in range(2):                               # 复制序号可能有两层
        s = COPY.sub("", s)
    s = PUNCT.sub("", s)
    return s.strip()


def main():
    d = sqlite3.connect(DB, timeout=30)
    d.row_factory = sqlite3.Row
    rows = d.execute("SELECT id, parent_id, kind, ext, size, name_enc FROM items").fetchall()
    total_n = len(rows)
    total_b = 0
    groups = defaultdict(list)
    for r in rows:
        nm = ic.safe_unseal(r["name_enc"] or "") or ""
        size = int(r["size"] or 0)
        total_b += size
        groups[norm(nm)].append({"id": r["id"], "parent": r["parent_id"], "kind": r["kind"] or "",
                                 "ext": (r["ext"] or "").lower(), "size": size, "name": nm})
    tiers = [(2, 2), (3, 3), (5, 5), (10, 10), (20, 20), (50, 50)]
    stat = {}
    for _, lo in tiers:
        gs = [g for g in groups.values() if len(g) >= lo]
        n = sum(len(g) for g in gs)
        b = sum(x["size"] for g in gs for x in g)
        stat["≥%d 份" % lo] = {"标题组数": len(gs), "条目数": n,
                               "体积GB": round(b / 1073741824, 2),
                               "占全库条数%": round(n / total_n * 100, 1)}
    top_n = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:25]
    top_b = sorted(groups.items(), key=lambda kv: -sum(x["size"] for x in kv[1]))[:15]

    def brief(g):
        items = g
        dirs = len({x["parent"] for x in items})
        kinds = Counter(x["kind"] for x in items)
        return {"标题": items[0]["name"][:52], "份数": len(items), "不同目录": dirs,
                "类型": dict(kinds), "总体积MB": round(sum(x["size"] for x in items) / 1048576, 1)}

    out = {
        "全库": {"条数": total_n, "总体积GB": round(total_b / 1073741824, 1),
                 "不同标题数": len(groups)},
        "按重复份数分档": stat,
        "Top25_份数最多": [brief(g) for _, g in top_n],
        "Top15_占体积最大": [brief(g) for _, g in top_b],
        "同一标题跨目录最多的组": [brief(g) for _, g in
                                   sorted(groups.items(), key=lambda kv: -len({x['parent'] for x in kv[1]}))[:10]],
    }
    with open("/tmp/dup.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["group_size", "dup_dirs", "id", "kind", "ext", "size_mb", "name"])
        for g in groups.values():
            if len(g) < 3:
                continue
            dirs = len({x["parent"] for x in g})
            for x in g:
                w.writerow([len(g), dirs, x["id"], x["kind"], x["ext"],
                            round(x["size"] / 1048576, 3), x["name"]])
    open("/tmp/dup_report.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({"全库": out["全库"], "按重复份数分档": out["按重复份数分档"]},
                     ensure_ascii=False, indent=1))


main()
