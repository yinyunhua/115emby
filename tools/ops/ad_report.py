"""全库广告判定（只读）。分三档，避免把"成人内容描述"误判成广告。

判定思路
--------
这个库本身就是成人内容，所以**不能**拿"学生妹/模特/口爆"这类词当广告特征。
真正的广告特征是"**推广**"：
* 平台引流：直播 / 荷官 / 激情 / 约炮 / 加微信 / 加群 / 电报 / 同城约 / 上门服务…
* 博彩类：博彩 / 彩票 / 棋牌 / 赌场 / 澳门 / 威尼斯 / 开户 / 返水 / 代理…
* 网址：名字里带域名（98T 是整库抓取水印，单独统计，不算广告）
* 体积异常小：<1MB（基本是引流片头）/<5MB（典型广告片）
* 二维码/占位小图：<30KB；GIF 动图
* 批量投放：同一个 名字+体积 出现在 >=3 个目录
"""
import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/app")
from app import index_crypto as ic                       # noqa: E402

DB = os.path.join(os.getenv("DATA_DIR") or "/app/data", "emby.db")

WM = re.compile(r"98t\.la|98t\.com|98tla", re.I)
DOMAIN = re.compile(r"(?:[a-z0-9][a-z0-9\-]{1,30}\.)+(?:com|net|la|xyz|cc|top|vip|club|tv|me|"
                    r"info|site|online|shop|app|cn|org|biz|pro|icu|fun|live|one|link|buzz|wang)\b", re.I)

# 平台引流 / 博彩 / 联系类（真正"推广"的词）
PROMO = ["直播", "裸聊", "荷官", "激情", "約炮", "约炮", "约爱", "約愛", "同城", "上门服务",
         "上门", "会所", "會所", "楼凤", "樓鳳", "外围", "外圍", "兼职", "兼職", "包养", "包養",
         "加微信", "微信号", "加qq", "加群", "进群", "進群", "电报", "telegram", "t.me",
         "博彩", "彩票", "棋牌", "赌场", "賭場", "澳门", "澳門", "威尼斯", "开户", "開戶",
         "返水", "代理", "客服", "官网", "官網", "下载app", "下载安装", "扫码", "掃碼",
         "二维码", "二維碼", "永久域名", "最新地址", "新地址", "发布页", "發布頁", "防失联",
         "防失聯", "免费看", "免費看", "在线看", "在線看", "看片神器", "福利群", "狼友",
         "夜夜", "日日", "爽片", "资源站", "资源群"]
PROMO_RE = re.compile("|".join(re.escape(w) for w in PROMO), re.I)

TINY1 = 1 * 1024 * 1024
TINY5 = 5 * 1024 * 1024
TINY20 = 20 * 1024 * 1024
SMALL_IMG = 30 * 1024


def main():
    d = sqlite3.connect(DB, timeout=30)
    d.row_factory = sqlite3.Row
    rows = d.execute("SELECT id, parent_id, kind, ext, size, name_enc FROM items").fetchall()
    total_n = len(rows)
    total_b = 0
    items = []
    name_dirs = defaultdict(set)
    for r in rows:
        nm = ic.safe_unseal(r["name_enc"] or "") or ""
        size = int(r["size"] or 0)
        total_b += size
        items.append({"id": r["id"], "parent": r["parent_id"], "kind": r["kind"] or "",
                      "ext": (r["ext"] or "").lower(), "size": size, "name": nm})
        name_dirs[(nm.lower(), size)].add(r["parent_id"])
    for it in items:
        low = it["name"].lower()
        it["dup"] = len(name_dirs[(low, it["size"])])
        it["wm"] = bool(WM.search(low))
        it["doms"] = [m.group(0) for m in DOMAIN.finditer(low) if not WM.search(m.group(0))]
        it["promo"] = PROMO_RE.search(low)
        # 广告分档
        rules = []
        if it["promo"] and (it["doms"] or it["size"] < TINY20 or it["dup"] >= 3):
            rules.append("A_平台引流")
        if it["doms"] and it["size"] < TINY20:
            rules.append("A_网址引流")
        if it["kind"] == "video" and it["size"] < TINY1:
            rules.append("B_视频<1MB")
        if it["kind"] == "video" and TINY1 <= it["size"] < TINY5:
            rules.append("B_视频1-5MB")
        if it["kind"] == "image" and it["size"] < SMALL_IMG:
            rules.append("B_小图<30KB")
        if it["ext"] == "gif":
            rules.append("B_GIF")
        if it["dup"] >= 3:
            rules.append("C_跨目录重复>=3")
        if it["doms"]:
            rules.append("D_名字带网址")
        if it["wm"]:
            rules.append("D_98T水印")
        it["rules"] = rules
        it["ad"] = any(x.startswith(("A_", "B_")) for x in rules)

    cats = defaultdict(lambda: {"n": 0, "b": 0, "ex": []})
    for it in items:
        for c in it["rules"]:
            e = cats[c]
            e["n"] += 1
            e["b"] += it["size"]
            if len(e["ex"]) < 4:
                e["ex"].append([it["name"][:46], round(it["size"] / 1048576, 2), it["dup"]])
    ads = [x for x in items if x["ad"]]
    out = {
        "全库": {"条数": total_n, "总体积GB": round(total_b / 1073741824, 1)},
        "分类": {k: {"条数": v["n"], "体积GB": round(v["b"] / 1073741824, 3), "示例": v["ex"]}
                 for k, v in sorted(cats.items(), key=lambda x: -x[1]["n"])},
        "广告合计(去重)": {"条数": len(ads),
                           "体积GB": round(sum(x["size"] for x in ads) / 1073741824, 2),
                           "占全库条数比": round(len(ads) / total_n * 100, 1),
                           "占全库体积比": round(sum(x["size"] for x in ads) / max(1, total_b) * 100, 2)},
        "广告_类型": dict(Counter(x["kind"] for x in ads)),
        "广告_体积分布": dict(Counter(
            "<1MB" if x["size"] < TINY1 else "<5MB" if x["size"] < TINY5
            else "<20MB" if x["size"] < TINY20 else "<100MB" if x["size"] < 100 * 1048576
            else ">=100MB" for x in ads)),
        "网址排行(非98T)": Counter(x for it in items for x in it["doms"]).most_common(15),
        "广告名_出现最多": [(n[:50], c) for n, c in Counter(x["name"].lower() for x in ads).most_common(12)],
        "广告_体积最大": [[x["name"][:50], round(x["size"] / 1073741824, 2)] for x in
                          sorted(ads, key=lambda y: -y["size"])[:10]],
    }
    with open("/tmp/ads.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "ext", "size_mb", "dup_dirs", "rules", "name"])
        for x in sorted(ads, key=lambda y: -y["size"]):
            w.writerow([x["id"], x["kind"], x["ext"], round(x["size"] / 1048576, 3),
                        x["dup"], "|".join(x["rules"]), x["name"]])
    open("/tmp/ads_report.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("\nCSV: /tmp/ads.csv 行数 %d" % len(ads))


main()
