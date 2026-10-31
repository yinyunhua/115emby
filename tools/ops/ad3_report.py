"""广告判定（只读，第三版）：**同一份文件被批量复制投放**。

分组键 = (文件名小写, 体积)。真广告是同一份文件复制到几百个目录，所以名字与体积完全一致；
而 `1.mp4` 这种"各目录自己的编号"在不同目录里是**不同文件、体积不同**，自然不会被归到一组。

入选条件：
  ① 同组 >= 3 份，且出现在 >= 3 个不同目录；
  ② 名字像推广：含网址（98T 水印不算）/ 含推广词 / 或"很小的图片或GIF + 铺得极广(>=10 目录)"。
另外单独列出"字节级重复但名字中性"的组，供人工判断（不算广告）。
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
WM = re.compile(r"98t\.(?:la|com)", re.I)
URL = re.compile(r"(?:[a-z0-9][a-z0-9\-]{1,30}\.)+(?:com|net|la|xyz|cc|top|vip|club|tv|me|info|"
                 r"site|online|shop|app|cn|org|biz|pro|icu|fun|live|one|link|buzz|wang)\b", re.I)
PROMO = ["扫码", "掃碼", "二维码", "二維碼", "qr", "获取地址", "獲取地址", "交友", "同城",
         "约炮", "約炮", "约爱", "美女", "学生妹", "學生妹", "少妇", "少婦", "点击进入",
         "點擊進入", "看美女", "找美女", "惊喜", "驚喜", "加微信", "微信号", "加群", "进群",
         "直播", "荷官", "激情", "裸聊", "博彩", "彩票", "棋牌", "赌场", "賭場", "澳门",
         "澳門", "威尼斯", "开户", "開戶", "返水", "代理", "客服", "官网", "官網", "下载",
         "下載", "地址", "发布页", "發布頁", "永久", "防失联", "防失聯", "福利", "狼友",
         "社区", "社區", "2048", "一对一", "一對一", "试看", "試看", "大饱眼福", "表演",
         "注册", "註冊", "免费", "免費", "提款", "彩金", "真人", "荷倌"]
PROMO_RE = re.compile("|".join(re.escape(w) for w in PROMO), re.I)


def main():
    d = sqlite3.connect(DB, timeout=30)
    d.row_factory = sqlite3.Row
    rows = d.execute("SELECT id, parent_id, kind, ext, size, name_enc FROM items").fetchall()
    total_n, total_b = len(rows), 0
    groups = defaultdict(list)
    for r in rows:
        nm = ic.safe_unseal(r["name_enc"] or "") or ""
        size = int(r["size"] or 0)
        total_b += size
        groups[(nm.lower(), size)].append(
            {"id": r["id"], "parent": r["parent_id"], "kind": r["kind"] or "",
             "ext": (r["ext"] or "").lower(), "size": size, "name": nm})

    ads, neutral = {}, {}
    for key, g in groups.items():
        dirs = len({x["parent"] for x in g})
        if len(g) < 3 or dirs < 3:
            continue
        nm = g[0]["name"]
        low = nm.lower()
        has_url = bool(URL.search(low)) and not bool(WM.search(low))
        has_promo = bool(PROMO_RE.search(low))
        tiny_img = (all(x["kind"] == "image" for x in g) and g[0]["size"] < 1048576
                    and g[0]["ext"] in ("png", "jpg", "jpeg", "gif", "webp"))
        mass_tiny = tiny_img and dirs >= 10
        if has_url or has_promo or mass_tiny:
            ads[key] = (g, {"网址": has_url, "推广词": has_promo, "小图遍布": mass_tiny})
        else:
            neutral[key] = g

    items = [x for g, _ in ads.values() for x in g]
    cats = Counter()
    for g, f in ads.values():
        for k, v in f.items():
            if v:
                cats[k] += len(g)

    def brief(pair):
        g, f = pair
        return {"标题": g[0]["name"][:60], "份数": len(g), "目录数": len({x["parent"] for x in g}),
                "单份MB": round(g[0]["size"] / 1048576, 3),
                "总MB": round(sum(x["size"] for x in g) / 1048576, 2),
                "类型": dict(Counter(x["kind"] for x in g)),
                "特征": [k for k, v in f.items() if v]}

    out = {
        "全库": {"条数": total_n, "总体积GB": round(total_b / 1073741824, 1)},
        "广告": {"条目数": len(items), "组数": len(ads),
                 "总体积GB": round(sum(x["size"] for x in items) / 1073741824, 3),
                 "占全库条数%": round(len(items) / total_n * 100, 2),
                 "按特征(条目数)": dict(cats),
                 "按类型": dict(Counter(x["kind"] for x in items)),
                 "按扩展名": dict(Counter(x["ext"] for x in items).most_common()),
                 "体积分布": dict(Counter(
                     "<100KB" if x["size"] < 102400 else "<1MB" if x["size"] < 1048576
                     else "<10MB" if x["size"] < 10 * 1048576 else ">=10MB" for x in items))},
        "字节级重复但名字中性(待人工判断)": {
            "组数": len(neutral), "条目数": sum(len(g) for g in neutral.values()),
            "体积GB": round(sum(x["size"] for g in neutral.values() for x in g) / 1073741824, 2),
            "Top10": [{"标题": g[0]["name"][:50], "份数": len(g),
                       "目录数": len({x["parent"] for x in g}),
                       "单份MB": round(g[0]["size"] / 1048576, 2)}
                      for _, g in sorted(neutral.items(), key=lambda kv: -len(kv[1]))[:10]]},
        "广告组_Top35": [brief(x) for x in sorted(ads.values(), key=lambda kv: -len(kv[0]))[:35]],
    }
    with open("/tmp/ad3.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["copies", "dirs", "kind", "ext", "size_mb", "id", "name"])
        for g, _ in sorted(ads.values(), key=lambda kv: -len(kv[0])):
            dirs = len({x["parent"] for x in g})
            for x in g:
                w.writerow([len(g), dirs, x["kind"], x["ext"], round(x["size"] / 1048576, 3),
                            x["id"], x["name"]])
    open("/tmp/ad3_report.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out["广告"], ensure_ascii=False, indent=1))
    print("中性重复组:", out["字节级重复但名字中性(待人工判断)"]["组数"], "组")
    print("CSV 行数:", len(items) + 1)


main()
