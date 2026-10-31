"""索引里的"名字"加密：文件名 / 文件夹名 / 路径。

为什么不是 SQLCipher
--------------------
* SQLCipher 需要额外依赖（`pysqlcipher3` / `sqlcipher3` + libsqlcipher 或 manylinux wheel），
  而且**整库加密**会把直链也一起加密 —— 你的需求是"直链不用加密，名字加密就好"；
* 列级加密能做到同样的保护范围，**零新依赖**，且不动直链、不动数字字段（size/ctime/type），
  排序、分页、时间线全部照常；
* 代价只有一个：**名字不能再直接 `ORDER BY name` / `LIKE '前缀%'`**，用下面两个替代：
  - **盲索引** ``name_idx``（HMAC）→ 精确查找
  - **前缀桶** ``name_bucket``（前 3 个字符的 HMAC）→ 前缀搜索先按桶收敛，再解密过滤
  - **排序秩** ``name_rank``（迁移时一次性按解密后的名字排好序）→ `ORDER BY name_rank`

密钥派生
--------
不复用缩略图那把密钥，而是从主密钥派生出**用途隔离**的子密钥：

    name_key   = HMAC(master, b"enc|idx:name:v1")     # 加解密
    blind_key  = HMAC(master, b"idx|idx:name:v1")     # 盲索引/桶

所以：换掉缩略图密钥不会毁掉名字索引；反之亦然。

字段
----
| 列 | 内容 |
|---|---|
| `name_enc`     | 文件名密文（base64 信封） |
| `name_idx`     | 文件名盲索引（HMAC，精确匹配） |
| `name_bucket`  | 前 3 字符盲索引（前缀搜索收敛） |
| `name_rank`    | 按名字排序的整数秩（迁移时算好） |
| `folder_enc` / `folder_idx` / `folder_bucket` | 文件夹名同上 |
| `path_enc`     | 完整路径密文（路径里含各级文件夹名，默认一起加密） |

`pending_links.url`（直链）**不加密**，按你的要求保持明文。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import unicodedata

from . import secretbox

log = logging.getLogger("album.indexcrypto")

KEY_INFO = b"idx:name:v1"
LINK_INFO = b"idx:link:v1"
BUCKET_CHARS = 3

# 一次性派生并缓存
_cache: dict[str, bytes] = {}


def _derive(purpose: bytes) -> bytes:
    master = secretbox.load_key()
    return hmac.new(master, purpose + b"|" + KEY_INFO, hashlib.sha256).digest()


def name_key() -> bytes:
    if "enc" not in _cache:
        _cache["enc"] = _derive(b"enc")
    return _cache["enc"]


def link_key() -> bytes:
    """直链用的独立子密钥：换名字密钥不会毁掉直链缓存，反之亦然。"""
    if "link" not in _cache:
        _cache["link"] = hmac.new(secretbox.load_key(), b"enc|" + LINK_INFO,
                                  hashlib.sha256).digest()
    return _cache["link"]


def blind_key() -> bytes:
    if "idx" not in _cache:
        _cache["idx"] = _derive(b"idx")
    return _cache["idx"]


def reset_cache() -> None:
    """测试/换钥用。"""
    _cache.clear()


def normalize(text: str) -> str:
    """盲索引的归一化：NFKC + casefold + 折叠连续空白。"""
    t = unicodedata.normalize("NFKC", text or "").casefold().strip()
    return " ".join(t.split())


# --------------------------------------------------------------------- 加解密
def seal(text: str) -> str:
    """把名字加密成可直接落库的 base64 文本。空串原样返回（保持 NULL-ish 语义）。"""
    if not text:
        return ""
    return base64.b64encode(secretbox.encrypt(text.encode("utf-8"), key=name_key())).decode("ascii")


def unseal(token: str) -> str:
    """解密；失败抛 secretbox.DecryptError（调用方决定是重抓还是显示占位）。"""
    if not token:
        return ""
    return secretbox.decrypt(base64.b64decode(token), key=name_key()).decode("utf-8", "replace")


def safe_unseal(token: str) -> str:
    """解密失败不抛异常，返回空串（列表页不至于因为一条坏数据整页 500）。"""
    try:
        return unseal(token)
    except Exception as exc:  # noqa: BLE001
        log.warning("名字解密失败（换过密钥或数据损坏）：%s", exc)
        return ""


# --------------------------------------------------------------------- 盲索引
def blind(text: str) -> str:
    """精确匹配用：HMAC(归一化名字) 的 hex 前 32 位。"""
    n = normalize(text).encode("utf-8")
    return hmac.new(blind_key(), n, hashlib.sha256).hexdigest()[:32]


def bucket(text: str) -> str:
    """前缀搜索用：取归一化后的前 3 个字符做 HMAC，桶内再解密过滤。"""
    n = normalize(text)[:BUCKET_CHARS].encode("utf-8")
    return hmac.new(blind_key(), b"b:" + n, hashlib.sha256).hexdigest()[:24]


# --------------------------------------------------------------------- 便捷封装
# --------------------------------------------------------------------- 直链
def seal_link(url: str) -> str:
    """加密 115 直链（URL 里带 UID 与签名，属于敏感串）。空串原样返回。"""
    if not url:
        return ""
    return base64.b64encode(secretbox.encrypt(url.encode("utf-8"), key=link_key())).decode("ascii")


def unseal_link(token: str) -> str:
    if not token:
        return ""
    return secretbox.decrypt(base64.b64decode(token), key=link_key()).decode("utf-8", "replace")


def safe_unseal_link(token: str) -> str:
    try:
        return unseal_link(token)
    except Exception as exc:  # noqa: BLE001
        log.warning("直链解密失败（换过密钥或已过期清理）：%s", exc)
        return ""


def seal_fields(name: str = "", folder: str = "", path: str = "") -> dict[str, str]:
    return {
        "name_enc": seal(name),
        "name_idx": blind(name) if name else "",
        "name_bucket": bucket(name) if name else "",
        "folder_enc": seal(folder),
        "folder_idx": blind(folder) if folder else "",
        "folder_bucket": bucket(folder) if folder else "",
        "path_enc": seal(path),
    }


def open_row(row: dict) -> dict:
    """把一行（含 *_enc）还原成可读 dict；没有 *_enc 时回退原始明文列（兼容未迁移的库）。"""
    name = safe_unseal(row.get("name_enc") or "") or (row.get("name") or "")
    folder = safe_unseal(row.get("folder_enc") or "") or (row.get("folder") or "")
    path = safe_unseal(row.get("path_enc") or "") or (row.get("path") or "")
    return {"id": row.get("id"), "name": name, "folder": folder, "path": path,
            "size": row.get("size"), "ctime": row.get("ctime"), "type": row.get("type")}
