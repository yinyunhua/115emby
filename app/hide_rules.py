"""入库黑名单：某些"文件名 + 体积"组合直接不入库、也不显示。

规则（用户 2026-10-22 指定）：
* 只针对**视频**；
* 体积 **小于 20MB**；
* 文件名（不区分大小写）包含下列任一关键词：
  ``N号房的精彩`` / ``美女荷官`` / ``激情隨時看``（含简体"激情随时看"）/ ``美女直播``。

为什么用"入库时就丢"而不是"查询时过滤"
--------------------------------------
名字在库里是密文（只有盲索引 ``name_idx``），SQL 没法做子串匹配；
而这些条目对用户毫无价值（小体积引流视频），所以直接在**写库前**丢掉，
再把历史数据清一遍（见 ``tools/purge_blocked.py``），列表/随机发现就自然干净了。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

KEYWORDS = tuple(k.strip().lower() for k in (
    os.getenv("HIDE_KEYWORDS")
    or ("N号房的精彩,N房间的精彩,美女荷官,激情隨時看,激情随时看,美女直播,"
        "台灣UU線上美女,台湾uu线上美女,台灣UU線上")
).split(",") if k.strip())

# 命中这些关键词时，"图片 + 视频"都拦（原来是只拦视频；实测还有一批
# 激情隨時看.gif 和 台灣UU線上美女 这类引流图，一并清掉更干净）
# 明确的"广告推广语"（配合 20MB 体积门槛使用；不含"激情/赌场"这类容易误伤片名的词）
# ★ 2026-10-22：这条"我自己加的推广语规则"**默认关闭**（用户反馈被多屏蔽/多删了）。
#   关闭后只保留：用户三份清单的黑名单(block_names.json) + 用户指定的关键字(KEYWORDS)。
#   想重新启用：设环境变量 HIDE_AD_WORDS=词1,词2 后重启容器。
AD_WORDS = tuple(k.strip().lower() for k in (
    os.getenv("HIDE_AD_WORDS") or "__disabled__"
).split(",") if k.strip())
# ★ 教训：这里**不能放短词**（qr / sis / 裸聊 / 一对一 这种），
#   它们是子串匹配，会把 DVAJ-033.FHD.wmv、twitter-video-…、…大饱眼福！.mp4 这类正常片误删。

_AD_RE = None


def _ad_re():
    global _AD_RE
    if _AD_RE is None:
        import re as _re
        _AD_RE = _re.compile("|".join(_re.escape(w) for w in AD_WORDS), _re.I)
    return _AD_RE


KINDS = tuple(k.strip() for k in (
    os.getenv("HIDE_KINDS") or "image,video").split(",") if k.strip())

# 只有小于这个体积才丢（用户明确说 20MB 以内）
MAX_VIDEO_BYTES = int(os.getenv("HIDE_VIDEO_MAX_BYTES") or (20 * 1024 * 1024))


# ---------------------------------------------------------------- 精确黑名单
# 由清库脚本写入 data/block_names.json：{"pairs":[[名字,体积],...],"names":[...]}
# 用途：清掉的广告在**重新扫描时不会再被写回**（关键字规则抓不到的、人工确认过的那些）。
_BLOCK = {"at": -1.0, "pairs": set(), "names": set()}


def block_path() -> Path:
    return Path(os.getenv("DATA_DIR") or "/app/data") / "block_names.json"


def _load_block() -> None:
    p = block_path()
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _BLOCK["at"]:
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        _BLOCK["pairs"] = {(str(n).lower(), int(s)) for n, s in (data.get("pairs") or [])}
        _BLOCK["names"] = {str(n).lower() for n in (data.get("names") or [])}
        _BLOCK["at"] = m
    except Exception:                                      # noqa: BLE001
        pass


def add_block_pairs(pairs, names=()) -> int:
    """把 (名字, 体积) / 名字 追加进黑名单文件，返回写入后的总数。"""
    p = block_path()
    data = {}
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:                                      # noqa: BLE001
        data = {}
    cur = {(str(n), int(s)) for n, s in (data.get("pairs") or [])}
    cur |= {(str(n), int(s)) for n, s in pairs}
    nm = {str(x) for x in (data.get("names") or [])} | {str(x) for x in names}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pairs": sorted(cur), "names": sorted(nm)},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    _BLOCK["at"] = -1.0                                    # 立刻失效，下次读盘
    _load_block()
    return len(cur) + len(nm)


# 纯域名文件名（去掉空格/符号后整个名字就是一个网址）—— 实测广告常用"插空格"绕过关键词：
#   x u u 9 2 .c o m.mp4 / x u u 6 2 . c o m.mp4 / 同城少妇…auu35.com.gif（后者靠关键词已能命中）
# 注意：**不能去掉点**，否则 "x u u 9 2 .c o m" 压成 "xuu92com" 就认不出域名了；
# 这里只清空白和常见分隔符，"x u u 9 2 . c o m" → "xuu92.com"
_SQUASH = re.compile(r"[\s\-_—·、,，。:：;；!！\?？\+&'\"“”‘’\(\)\[\]【】<>《》]+")
# TLD 必须"像真的"：2 位国家码，或下面这份常见 gTLD 名单。
# ★ 教训：一开始写成 [a-z]{2,6} 无限制，结果 [BT-btt.com]XW.mp4 去括号后变成 "bt-btt.comxw"、
#   IMG_2580 .PNG.jpg 变成 "img_2580.png"，都被当成域名了 —— 误删了 7 个大文件。
_TLD = ("com|net|org|info|biz|name|pro|mobi|asia|xyz|top|vip|club|online|site|shop|store|app|"
        "icu|fun|live|one|link|buzz|wang|art|click|download|stream|space|world|life|day|today|"
        "media|agency|company|tech|cc|tv|me|io|co|gg|to|ws|pw|se|su|ru|cn|jp|kr|tw|hk|uk|us|"
        "de|fr|nl|ca|au|in|id|th|vn|ph|my|sg|mo|la|ly|gd|vc|tk|ml|ga|cf|bet|win|lol|porn|sex|"
        "adult|casino|onion|fan|xyz")
_DOMAIN_ONLY = re.compile(r"^(?:www)?(?:[a-z0-9][a-z0-9\-]{0,30}\.)+(?:%s)$" % _TLD)
_SPACED_NAME = re.compile(r"\S\s+\S\s+\S")            # 字间插空格（规避写法）


DOMAIN_RULE = (os.getenv("HIDE_DOMAIN_RULE") or "0").strip() not in ("", "0", "false", "no")


def split_domain_evasion(name: str, size: int = 0) -> bool:
    """名字整个就是个域名（含"字间插空格"的规避写法），判为广告。

    体积门槛：域名式命名还把整个名字当文件名的，正常内容里极少见；
    但为避免误伤"大体积内容片"（如 [BT-btt.com]XW.mp4），只在
    ① 名字是插空格写法，或 ② 体积 < 20MB 时才判广告。
    """
    if not DOMAIN_RULE:
        return False                                   # 默认关闭（用户反馈多删）
    raw = name or ""
    spaced = bool(_SPACED_NAME.search(raw))
    s = raw.lower().strip()
    s = re.sub(r"\.[a-z0-9]{1,5}$", "", s)
    s = _SQUASH.sub("", s)
    if not s or not s.isascii():
        return False                                       # 含中文/非 ASCII 的交给关键词规则
    if not _DOMAIN_ONLY.match(s):
        return False
    return spaced or int(size or 0) < MAX_VIDEO_BYTES


def is_blocked_by_rules(name: str, kind: str = "", size: int = 0) -> bool:
    """只看"算法规则"（关键词/推广词/纯域名/体积），**不读黑名单**。

    用途：判断某条记录是否该进黑名单时用这个 —— 用 is_blocked 会因为
    "黑名单自己命中自己"而永远返回 True（踩过这个坑）。
    """
    if split_domain_evasion(name, size):
        return True
    if (kind or "") not in KINDS:
        return False
    try:
        if int(size or 0) >= MAX_VIDEO_BYTES:
            return False
    except Exception:                                      # noqa: BLE001
        return False
    n = (name or "").lower()
    if not n:
        return False
    if any(k in n for k in KEYWORDS):
        return True
    if not AD_WORDS or AD_WORDS == ("__disabled__",):
        return False                                       # 推广语规则已关闭
    return bool(_ad_re().search(n))


def is_blocked(name: str, kind: str = "", size: int = 0) -> bool:
    """要不要把这条媒体项丢掉（黑名单 + 算法规则）。"""
    _load_block()
    n0 = (name or "").lower()
    if n0 and ((n0, int(size or 0)) in _BLOCK["pairs"] or n0 in _BLOCK["names"]):
        return True                                        # 人工确认过的广告
    if (kind or "") not in KINDS:
        return False
    return is_blocked_by_rules(name, kind, size)


def matched(name: str) -> str:
    """返回命中的关键词（没命中返回空串），用于日志/统计。"""
    n = (name or "").lower()
    for k in KEYWORDS:
        if k and k in n:
            return k
    return ""


def describe() -> str:
    return "小于 %.0fMB 且文件名含 %s 的 %s" % (
        MAX_VIDEO_BYTES / 1048576, " / ".join(KEYWORDS), "/".join(KINDS))
