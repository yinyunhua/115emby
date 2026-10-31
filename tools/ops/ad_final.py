"""广告判定最终版：重复投放 + 推广特征 + **字间插空格伪装**。

产出两份 CSV：
  /tmp/ad_final.csv       明确广告（推广词/网址/小图遍布）
  /tmp/ad_suspect.csv     疑似：字节级重复 + 名字中性，但去掉空格后能拼出网址/推广词（规避过滤的写法）
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
         "注册", "註冊", "免费", "免費", "提款", "彩金", "真人", "现场", "現場", "情報",
         "最新", "找回家", "不迷路", "关注", "關注", "程序", "app", "安卓", "苹果", "蘋果"]
PROMO_RE = re.compile("|".join(re.escape(w) for w in PROMO), re.I)
SPACED = re.compile(r"^(?:\S\s){4,}\S")            # 每个字符之间都有空格（>=5 个字符）


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

    ads, suspect = {}, {}
    for g in groups.values():
        dirs = len({x["parent"] for x in g})
        if len(g) < 3 or dirs < 3:
            continue
        nm = g[0]["name"]
        low = nm.lower()
        squash = re.sub(r"\s+", "", low)               # 去掉所有空格再判一次
        has_url = bool(URL.search(low)) and not bool(WM.search(low))
        has_promo = bool(PROMO_RE.search(low))
        tiny_img = (all(x["kind"] == "image" for x in g) and g[0]["size"] < 1048576
                    and g[0]["ext"] in ("png", "jpg", "jpeg", "gif", "webp"))
        mass_tiny = tiny_img and dirs >= 10
        if has_url or has_promo or mass_tiny:
            ads[(nm.lower(), g[0]["size"])] = g
        elif SPACED.match(nm) or URL.search(squash) or PROMO_RE.search(squash):
            suspect[(nm.lower(), g[0]["size"])] = g       # 插空格伪装

    items = [x for g in ads.values() for x in g]
    sus = [x for g in suspect.values() for x in g]

    def top(gs, n=12):
        out = []
        for g in sorted(gs.values(), key=lambda y: -len(y))[:n]:
            out.append({"标题": g[0]["name"][:52], "份数": len(g),
                        "目录数": len({x["parent"] for x in g}),
                        "单份MB": round(g[0]["size"] / 1048576, 3),
                        "总MB": round(sum(x["size"] for x in g) / 1048576, 1)})
        return out

    out = {
        "全库": {"条数": total_n, "总体积GB": round(total_b / 1073741824, 1)},
        "广告": {"条目数": len(items), "组数": len(ads),
                 "体积GB": round(sum(x["size"] for x in items) / 1073741824, 3),
                 "占条数%": round(len(items) / total_n * 100, 2),
                 "类型": dict(Counter(x["kind"] for x in items)),
                 "扩展名": dict(Counter(x["ext"] for x in items).most_common()),
                 "体积分布": dict(Counter(
                     "<100KB" if x["size"] < 102400 else "<1MB" if x["size"] < 1048576
                     else "<10MB" if x["size"] < 10 * 1048576 else ">=10MB" for x in items)),
                 "Top12": top(ads)},
        "疑似_插空格伪装": {"条目数": len(sus), "组数": len(suspect),
                            "体积GB": round(sum(x["size"] for x in sus) / 1073741824, 3),
                            "Top12": top(suspect)},
    }
    out["合计"] = {"条目数": len(items) + len(sus),
                   "体积GB": round((sum(x["size"] for x in items) + sum(x["size"] for x in sus)) / 1073741824, 3),
                   "占条数%": round((len(items) + len(sus)) / total_n * 100, 2)}
    for fn, gs in (("/tmp/ad_final.csv", ads), ("/tmp/ad_suspect.csv", suspect)):
        with open(fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["copies", "dirs", "kind", "ext", "size_mb", "id", "name"])
            for g in sorted(gs.values(), key=lambda y: -len(y)):
                dirs = len({x["parent"] for x in g})
                for x in g:
                    w.writerow([len(g), dirs, x["kind"], x["ext"], round(x["size"] / 1048576, 3),
                                x["id"], x["name"]])
    open("/tmp/ad_final.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
