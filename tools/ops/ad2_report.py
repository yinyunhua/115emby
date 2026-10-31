"""广告判定（只读）：**重复投放** 且 **标题本身是推广语/网址/二维码** 的条目。

关键区分（上一版吃过亏）：
* `1.mp4` 出现 13907 次、`2.mp4` 978 次 —— 那是每个目录各自的序号命名，同名不同内容，**不是广告**；
* `2048社区扫码获取地址.png` 390 次 / 385 个目录、`同城一YQ交友.gif` 383 次 —— 小图/GIF 铺满几百个目录，
  这才是**批量投放的广告**。

所以判定 = ① 归一化标题同组 >= 3 份、且跨 >= 3 个目录；**并且** ② 标题满足任一推广特征：
    含网址 / 含推广词 / 很小的图片或 GIF（<1MB）且名字里有"扫码 二维码 交友 同城 美女 学生妹"之类。
   序号类名字（纯数字、单字母+数字、www.98T.la@ (1) 这种）直接排除。
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
# 纯序号名：1 / 01 / v10 / p / (1) / 49 / 003 / nuclear? 不，只判"基本只有数字或单字母+数字"
SEQ = re.compile(r"^(?:[a-z]{0,3}\d{1,4}|\d{1,4}[a-z]{0,3})$", re.I)
PROMO = ["扫码", "掃碼", "二维码", "二維碼", "qr", "获取地址", "獲取地址", "交友", "同城",
         "约炮", "約炮", "约爱", "美女", "学生妹", "學生妹", "少妇", "少婦", "点击进入",
         "點擊進入", "看美女", "找美女", "惊喜", "驚喜", "加微信", "微信号", "加群",
         "进群", "直播", "荷官", "激情", "裸聊", "博彩", "彩票", "棋牌", "赌场", "賭場",
         "澳门", "澳門", "威尼斯", "开户", "返水", "代理", "客服", "官网", "官網", "下载",
         "下載", "地址", "发布页", "發布頁", "永久", "防失联", "防失聯", "福利", "狼友",
         "社区", "社區", "2048", "2048社区"]
PROMO_RE = re.compile("|".join(re.escape(w) for w in PROMO), re.I)


def norm(name: str) -> str:
    s = (name or "").lower()
    s = WM.sub("", s)
    s = URL.sub(" ", s)
    s = re.sub(r"\.[a-z0-9]{1,5}$", "", s)
    for _ in range(2):
        s = COPY.sub("", s)
    return PUNCT.sub("", s).strip()


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
        groups[norm(nm) or ("__id__" + r["id"])].append(
            {"id": r["id"], "parent": r["parent_id"], "kind": r["kind"] or "",
             "ext": (r["ext"] or "").lower(), "size": size, "name": nm})

    ad_groups, seq_groups = {}, {}
    for key, g in groups.items():
        if len(g) < 3:
            continue
        dirs = len({x["parent"] for x in g})
        if dirs < 3:
            continue
        has_url = any(URL.search(x["name"]) and not WM.search(x["name"]) for x in g)
        has_promo = any(PROMO_RE.search(x["name"]) for x in g)
        tiny = all(x["size"] < 1048576 for x in g) and any(
            x["ext"] in ("png", "jpg", "jpeg", "gif", "webp") for x in g)
        if not (has_url or has_promo or tiny):
            continue
        # 排除纯序号名（那种 1.mp4 / v10.mp4 是各目录自己的编号）
        if SEQ.match(key) and not has_promo and not has_url:
            seq_groups[key] = g
            continue
        ad_groups[key] = (g, {"网址": has_url, "推广词": has_promo, "小图": tiny})

    ads = [x for g, _ in ad_groups.values() for x in g]
    cats = Counter()
    for g, f in ad_groups.values():
        for k, v in f.items():
            if v:
                cats[k] += len(g)

    def brief(item):
        g, f = item
        return {"标题": g[0]["name"][:56], "份数": len(g),
                "目录数": len({x["parent"] for x in g}),
                "总MB": round(sum(x["size"] for x in g) / 1048576, 2),
                "类型": dict(Counter(x["kind"] for x in g)),
                "特征": [k for k, v in f.items() if v]}

    out = {
        "全库": {"条数": total_n, "总体积GB": round(total_b / 1073741824, 1)},
        "广告": {
            "条目数": len(ads),
            "标题组数": len(ad_groups),
            "总体积GB": round(sum(x["size"] for x in ads) / 1073741824, 3),
            "占条数%": round(len(ads) / total_n * 100, 2),
            "按特征(条目数)": dict(cats),
            "按类型": dict(Counter(x["kind"] for x in ads)),
            "按扩展名Top": Counter(x["ext"] for x in ads).most_common(8),
            "体积分布": dict(Counter(
                "<100KB" if x["size"] < 102400 else "<1MB" if x["size"] < 1048576
                else "<10MB" if x["size"] < 10 * 1048576 else ">=10MB" for x in ads)),
        },
        "被排除的序号类同名(不是广告)": {
            "组数": len(seq_groups),
            "条目数": sum(len(g) for g in seq_groups.values()),
            "体积GB": round(sum(x["size"] for g in seq_groups.values() for x in g) / 1073741824, 1),
            "举例": [{"标题": g[0]["name"][:40], "份数": len(g),
                      "目录数": len({x["parent"] for x in g})}
                     for _, g in sorted(seq_groups.items(), key=lambda kv: -len(kv[1]))[:6]],
        },
        "广告组_Top30(按份数)": [brief(x) for x in
                                 sorted(ad_groups.values(), key=lambda kv: -len(kv[0]))[:30]],
    }
    with open("/tmp/ad2.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["title_group_size", "dirs", "kind", "ext", "size_mb", "id", "name"])
        for g, _ in ad_groups.values():
            dirs = len({x["parent"] for x in g})
            for x in g:
                w.writerow([len(g), dirs, x["kind"], x["ext"], round(x["size"] / 1048576, 3),
                            x["id"], x["name"]])
    open("/tmp/ad2_report.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))


main()
