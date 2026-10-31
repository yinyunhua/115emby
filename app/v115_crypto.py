"""115 的 RSA + XOR 请求/响应加解密协议。

来源：移植自本项目 115-album 的 ``app/v115_crypto.py``（那份又是从 AList 的 115driver 移植、
并与独立实现 p115cipher 做过逐字节互证）。原样保留，未改算法。

移植自 AList 所用的 115driver：``pkg/crypto/m115``（Go）。
已用独立的 Python 实现 ``p115cipher`` 做逐字节互证。

关于本协议的三个反直觉之处（都踩过坑，务必注意）：

1. 115 在**客户端两个方向都用公钥 e**：请求用 e 加密（服务端持私钥 d 解密），
   响应用 d 处理（客户端用 e 解开）。因此 ``encode`` 之后自己 ``decode``
   永远得不到原文（e^2 != 单位元），做"往返自测"是没有意义的。

2. 客户端用 ``os.urandom`` 生成 16 字节会话密钥，随请求明文一起前置发送；
   服务端在响应里回显该密钥，客户端据此派生解码所需的 XOR key。

3. ``encode`` 与 ``decode`` 的混淆步骤**并不互逆**：
   ``encode`` 用固定常量 XOR key，``decode`` 用"回显 key 派生的 12 字节 key"。
   所以服务端响应的构造方式与请求不同，不能想当然套用同一套变换。

函数划分：
    ``encode(payload, key)``  客户端侧：明文 -> 待发送密文（base64）
    ``decode(encoded, key)``  客户端侧：服务端响应 -> 明文
内部的 ``_rsa_encrypt`` / ``_rsa_decrypt`` 仅分别为两方向服务，不要交叉使用。
"""
from __future__ import annotations
import base64, os

_XOR_KEY_SEED = bytes([
    0xf0,0xe5,0x69,0xae,0xbf,0xdc,0xbf,0x8a,0x1a,0x45,0xe8,0xbe,0x7d,0xa6,0x73,0xb8,
    0xde,0x8f,0xe7,0xc4,0x45,0xda,0x86,0xc4,0x9b,0x64,0x8b,0x14,0x6a,0xb4,0xf1,0xaa,
    0x38,0x01,0x35,0x9e,0x26,0x69,0x2c,0x86,0x00,0x6b,0x4f,0xa5,0x36,0x34,0x62,0xa6,
    0x2a,0x96,0x68,0x18,0xf2,0x4a,0xfd,0xbd,0x6b,0x97,0x8f,0x4d,0x8f,0x89,0x13,0xb7,
    0x6c,0x8e,0x93,0xed,0x0e,0x0d,0x48,0x3e,0xd7,0x2f,0x88,0xd8,0xfe,0xfe,0x7e,0x86,
    0x50,0x95,0x4f,0xd1,0xeb,0x83,0x26,0x34,0xdb,0x66,0x7b,0x9c,0x7e,0x9d,0x7a,0x81,
    0x32,0xea,0xb6,0x33,0xde,0x3a,0xa9,0x59,0x34,0x66,0x3b,0xaa,0xba,0x81,0x60,0x48,
    0xb9,0xd5,0x81,0x9c,0xf8,0x6c,0x84,0x77,0xff,0x54,0x78,0x26,0x5f,0xbe,0xe8,0x1e,
    0x36,0x9f,0x34,0x80,0x5c,0x45,0x2c,0x9b,0x76,0xd5,0x1b,0x8f,0xcc,0xc3,0xb8,0xf5,
])
_XOR_CLIENT_KEY = bytes([0x78,0x06,0xad,0x4c,0x33,0x86,0x5d,0x18,0x4c,0x01,0x3f,0x46])
_RSA_N = int(
    "8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b86021"
    "36477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad633"
    "8c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f1"
    "0a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683", 16)
_RSA_E = 0x10001
_KEY_LEN = 128

def generate_key() -> bytes:
    return os.urandom(16)

def derive_key(seed: bytes, size: int) -> bytes:
    key = bytearray(size)
    for i in range(size):
        key[i] = (seed[i] + _XOR_KEY_SEED[size * i]) & 0xff
        key[i] ^= _XOR_KEY_SEED[size * (size - i - 1)]
    return bytes(key)

def xor_transform(data: bytearray, key: bytes) -> None:
    dlen, klen = len(data), len(key)
    if not klen:
        return
    mod = dlen % 4
    for i in range(mod):
        data[i] ^= key[i % klen]
    for i in range(mod, dlen):
        data[i] ^= key[(i - mod) % klen]

def reverse_bytes(data: bytearray) -> None:
    data.reverse()

def rsa_encrypt_block(msg: int) -> bytes:
    return pow(msg, _RSA_E, _RSA_N).to_bytes(_KEY_LEN, "big")

def rsa_decrypt_block(block: bytes) -> bytes:
    # 115 把 (n,e) 当"私钥"用：解密也是 pow(m, e, n)
    ret = pow(int.from_bytes(block, "big"), _RSA_E, _RSA_N).to_bytes(_KEY_LEN, "big")
    for i, b in enumerate(ret):
        if b == 0 and i != 0:
            return ret[i + 1:]
    return b""

def _rsa_encrypt(data: bytes, deterministic_pad: bool = False) -> bytes:
    out = bytearray()
    for off in range(0, len(data), _KEY_LEN - 11):
        chunk = data[off:off + _KEY_LEN - 11]
        pad_size = _KEY_LEN - len(chunk) - 3
        if deterministic_pad:
            pad = bytes([0x02]) * pad_size           # 与 p115cipher 对齐
        else:
            pad = bytes(b % 0xff + 0x01 for b in os.urandom(pad_size))
        buf = bytearray(_KEY_LEN)
        buf[0], buf[1] = 0, 2
        buf[2:2 + pad_size] = pad
        buf[pad_size + 2] = 0
        buf[pad_size + 3:] = chunk
        out += rsa_encrypt_block(int.from_bytes(bytes(buf), "big"))
    return bytes(out)

def _rsa_decrypt(data: bytes) -> bytes:
    out = bytearray()
    for off in range(0, len(data), _KEY_LEN):
        out += rsa_decrypt_block(data[off:off + _KEY_LEN])
    return bytes(out)

def encode(payload: bytes, key: bytes, deterministic_pad: bool = False) -> str:
    buf = bytearray(len(payload) + 16)
    buf[:16] = key
    buf[16:] = payload
    body = buf[16:]
    xor_transform(body, derive_key(key, 4))
    reverse_bytes(body)
    xor_transform(body, _XOR_CLIENT_KEY)
    buf[16:] = body
    return base64.b64encode(_rsa_encrypt(bytes(buf), deterministic_pad)).decode()

def decode(encoded: str, key: bytes) -> bytes:
    data = _rsa_decrypt(base64.b64decode(encoded))
    out = bytearray(data[16:])
    xor_transform(out, derive_key(data[:16], 12))
    reverse_bytes(out)
    xor_transform(out, derive_key(key, 4))
    return bytes(out)
