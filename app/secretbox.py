"""缩略图存储加密层（C 方案：懒抓 + 永久保存 + 静态加密）。

设计取向
--------
1. **只加密"存储"**，不碰传输：视频/原图仍然 302 直连 115 CDN，
   缩略图字节被写入本地库时加密，出图时在进程内解密。
2. **算法可降级**：装有 ``cryptography`` 时用 AES-256-GCM（硬件加速）；
   没有时用纯标准库的 ``HMAC-SHA256/CTR + HMAC 标签``（encrypt-then-MAC，
   同样是认证加密，只是慢一点）。两者信封格式一致，靠 ``alg`` 字节区分。
3. **密钥不落库**：优先读 ``THUMB_ENC_KEY``（32 字节 hex/base64）；
   其次是 ``THUMB_ENC_PASSPHRASE`` + 盐文件做 PBKDF2-HMAC-SHA256（60 万轮）；
   都没有才退化成本机随机 keyfile（仅开发/自测用，日志会告警）。

信封格式::

    b'TBX1' | alg(1) | nonce(12) | ciphertext(+tag)

* ``alg = 1``：AES-256-GCM，tag 16 字节追加在密文尾部
* ``alg = 2``：HMAC-CTR-256，tag 32 字节追加在密文尾部

环境变量
--------
THUMB_ENC_KEY           32 字节密钥（hex 64 字符 或 base64），优先级最高
THUMB_ENC_PASSPHRASE    口令；配合 THUMB_ENC_SALT_FILE 派生
THUMB_ENC_SALT_FILE     盐文件路径，默认 <data>/.thumbsalt（自动生成）
THUMB_ENC_KEYS_FILE     退化用的 keyfile 路径，默认 <data>/.thumbkey
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import struct
from pathlib import Path

log = logging.getLogger("album.secretbox")

MAGIC = b"TBX1"
ALG_AES_GCM = 1
ALG_HMAC_CTR = 2
NONCE_LEN = 12
PBKDF2_ROUNDS = 600_000

_key: bytes | None = None
_alg: int | None = None


# --------------------------------------------------------------------- 算法探测
def _have_aesgcm() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def active_alg() -> int:
    """当前生效的算法编号（1=AES-GCM，2=HMAC-CTR 回退）。"""
    global _alg
    if _alg is None:
        _alg = ALG_AES_GCM if _have_aesgcm() else ALG_HMAC_CTR
    return _alg


# --------------------------------------------------------------------- 密钥加载
def _default_data_dir() -> Path:
    return Path(os.getenv("DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))


def _parse_key_text(raw: str) -> bytes:
    raw = raw.strip()
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    try:
        key = base64.b64decode(raw, validate=True)
        if len(key) == 32:
            return key
    except Exception:  # noqa: BLE001
        pass
    key = hashlib.sha256(raw.encode("utf-8")).digest()      # 任意字符串兜底派生
    log.warning("THUMB_ENC_KEY 不是标准 32 字节 hex/base64，已用 sha256 派生（建议换成标准格式）")
    return key


def _read_or_create(path: Path, size: int) -> bytes:
    if path.is_file():
        return path.read_bytes()[:size]
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = secrets.token_bytes(size)
    path.write_bytes(blob)
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001   (Windows 上可能不支持)
        pass
    return blob


def load_key() -> bytes:
    """取 32 字节主密钥（进程内缓存）。"""
    global _key
    if _key is not None:
        return _key

    raw = (os.getenv("THUMB_ENC_KEY") or "").strip()
    if raw:
        _key = _parse_key_text(raw)
        return _key

    passphrase = (os.getenv("THUMB_ENC_PASSPHRASE") or "").strip()
    if passphrase:
        data_dir = _default_data_dir()
        salt_path = Path(os.getenv("THUMB_ENC_SALT_FILE") or (data_dir / ".thumbsalt"))
        salt = _read_or_create(salt_path, 32)
        _key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ROUNDS, 32)
        return _key

    # 退化路径：本机随机 keyfile。**密钥与密文同盘 = 挡不住 root**，
    # 只适合开发自测；生产请配 THUMB_ENC_PASSPHRASE 或 THUMB_ENC_KEY。
    keys_path = Path(os.getenv("THUMB_ENC_KEYS_FILE") or (_default_data_dir() / ".thumbkey"))
    log.warning(
        "未配置 THUMB_ENC_PASSPHRASE / THUMB_ENC_KEY，已使用本机 keyfile %s —— "
        "密钥与密文同盘，只能防「文件被单独拷走」，防不住整机入侵", keys_path,
    )
    _key = _read_or_create(keys_path, 32)
    return _key


def reset_key_cache() -> None:
    """测试用：清掉进程内缓存的密钥与算法选择。"""
    global _key, _alg
    _key = None
    _alg = None


def key_fingerprint() -> str:
    """密钥指纹（不泄露密钥本身），写日志便于确认"换没换密钥"。"""
    return hashlib.sha256(b"fp:" + load_key()).hexdigest()[:12]


# --------------------------------------------------------------------- 加密/解密
def _subkeys(key: bytes) -> tuple[bytes, bytes]:
    return (
        hmac.new(key, b"enc", hashlib.sha256).digest(),
        hmac.new(key, b"mac", hashlib.sha256).digest(),
    )


def _keystream(key_enc: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key_enc, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: bytes, key: bytes | None = None) -> bytes:
    """加密一段字节，返回自描述信封（可直接存 BLOB）。

    ``key`` 省略时用主密钥；传入派生密钥可让不同用途（缩略图 / 索引名字）彼此隔离。
    """
    key = key or load_key()
    nonce = secrets.token_bytes(NONCE_LEN)
    alg = active_alg()
    header = MAGIC + bytes([alg]) + nonce

    if alg == ALG_AES_GCM:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        body = AESGCM(key).encrypt(nonce, plaintext, header)      # 含 16 字节 tag
        return header + body

    key_enc, key_mac = _subkeys(key)
    stream = _keystream(key_enc, nonce, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, stream))
    tag = hmac.new(key_mac, header + ct, hashlib.sha256).digest()
    return header + ct + tag


class DecryptError(ValueError):
    """密文被篡改、密钥不对或信封损坏。"""


def decrypt(blob: bytes, key: bytes | None = None) -> bytes:
    if not blob or len(blob) < len(MAGIC) + 1 + NONCE_LEN:
        raise DecryptError("信封过短")
    if blob[:4] != MAGIC:
        raise DecryptError("信封标识不匹配（不是本模块写入的数据）")

    alg = blob[4]
    nonce = blob[5 : 5 + NONCE_LEN]
    body = blob[5 + NONCE_LEN :]
    header = blob[: 5 + NONCE_LEN]
    key = key or load_key()

    if alg == ALG_AES_GCM:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            return AESGCM(key).decrypt(nonce, body, header)
        except Exception as exc:  # noqa: BLE001
            raise DecryptError(f"AES-GCM 解密失败：{exc}") from exc

    if alg == ALG_HMAC_CTR:
        if len(body) < 32:
            raise DecryptError("密文过短")
        ct, tag = body[:-32], body[-32:]
        key_enc, key_mac = _subkeys(key)
        expect = hmac.new(key_mac, header + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expect):
            raise DecryptError("HMAC 校验失败（密钥不对或数据被篡改）")
        stream = _keystream(key_enc, nonce, len(ct))
        return bytes(a ^ b for a, b in zip(ct, stream))

    raise DecryptError(f"未知算法编号 {alg}")
