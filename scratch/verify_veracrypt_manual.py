# -*- coding: utf-8 -*-
"""用手册里即将发布的 try_header 去解一个按权威规范合成的 VeraCrypt 头。
目的：证明手册代码块里的**下标**是对的（此前版本用 plain[256:512] 切 448B 缓冲，
静默截成 192B → 永远失败，这正是「手册主动引导模型撞墙」）。
"""
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA512
import os, struct, zlib

COUNT = 500000


# ---------- 手册版 XTS（与 cryptography 的 modes.XTS 已逐字节对齐过） ----------
def xts_decrypt(key64, data, sector=0, unit=512):
    k1, k2 = key64[:32], key64[32:]
    c1, c2 = AES.new(k1, AES.MODE_ECB), AES.new(k2, AES.MODE_ECB)
    out = bytearray()
    for off in range(0, len(data), unit):
        blk = data[off:off + unit]
        tweak = c2.encrypt((sector + off // unit).to_bytes(16, "little"))
        for i in range(0, len(blk), 16):
            t = bytes(a ^ b for a, b in zip(tweak, blk[i:i + 16]))
            out += bytes(a ^ b for a, b in zip(c1.decrypt(t), tweak))
            carry = 0x87 if tweak[15] & 0x80 else 0
            tweak = ((int.from_bytes(tweak, "little") << 1) & ((1 << 128) - 1)
                     ^ carry).to_bytes(16, "little")
    return bytes(out)


def xts_encrypt(key64, data, sector=0, unit=512):
    k1, k2 = key64[:32], key64[32:]
    c1, c2 = AES.new(k1, AES.MODE_ECB), AES.new(k2, AES.MODE_ECB)
    out = bytearray()
    for off in range(0, len(data), unit):
        blk = data[off:off + unit]
        tweak = c2.encrypt((sector + off // unit).to_bytes(16, "little"))
        for i in range(0, len(blk), 16):
            t = bytes(a ^ b for a, b in zip(tweak, blk[i:i + 16]))
            out += bytes(a ^ b for a, b in zip(c1.encrypt(t), tweak))
            carry = 0x87 if tweak[15] & 0x80 else 0
            tweak = ((int.from_bytes(tweak, "little") << 1) & ((1 << 128) - 1)
                     ^ carry).to_bytes(16, "little")
    return bytes(out)


# ---------- 待测：手册里的 try_header ----------
def try_header(pw: bytes, hdr: bytes, count=COUNT):
    """hdr = 该位置起的 65536 字节；返回 None 表示口令/参数不对。
    注意：缓冲下标 = 头偏移 - 64（缓冲从头的第 64 字节开始）。"""
    salt, enc = hdr[:64], hdr[64:64 + 448]
    dk = PBKDF2(pw, salt, dkLen=192, count=count, hmac_hash_module=SHA512)
    out = xts_decrypt(dk[:64], enc, sector=0)
    if out[:4] != b"VERA":
        return None
    if zlib.crc32(out[192:448]) != struct.unpack("<I", out[8:12])[0]:
        return None
    return out


# ---------- 合成一个符合规范的卷头 ----------
PW = b"correct-horse"
SALT = os.urandom(64)
K1, K2, KMK = os.urandom(32), os.urandom(32), os.urandom(32)
MK = K1 + K2 + KMK          # 96B = primary32 + secondary32 + hmac32

body = bytearray(448)
body[0:4] = b"VERA"
struct.pack_into("<H", body, 4, 5)          # header 68  format version = 5
struct.pack_into("<H", body, 6, 0x010b)     # header 70  min program version
struct.pack_into("<Q", body, 28, 0)         # header 92  hidden volume size
struct.pack_into("<Q", body, 36, 4 << 20)   # header 100 volume size
struct.pack_into("<Q", body, 44, 131072)    # header 108 mk scope offset
struct.pack_into("<Q", body, 52, 4 << 20)   # header 116 encrypted area size
struct.pack_into("<I", body, 60, 1)         # header 124 flags
struct.pack_into("<I", body, 64, 512)       # header 128 sector size
body[192:192 + len(MK)] = MK                # header 256  master keys
# **顺序敏感**：key 区 CRC 写进 buf[8]，而 buf[8] 又落在头 CRC 的覆盖区 buf[0:188] 内。
# 所以必须先落 key 区 CRC，再算头 CRC；反过来则头 CRC 恒错（这是真实实现里最易踩的一步）。
# CRC of key area: covers header 256..511 == buffer[192:448]
struct.pack_into("<I", body, 8, zlib.crc32(bytes(body[192:448])))
# CRC of header fields: covers header 64..251  == buffer[0:188]
struct.pack_into("<I", body, 188, zlib.crc32(bytes(body[0:188])))

dk = PBKDF2(PW, SALT, dkLen=192, count=COUNT, hmac_hash_module=SHA512)
hdr = bytearray(os.urandom(65536))
hdr[:64] = SALT
hdr[64:64 + 448] = xts_encrypt(dk[:64], bytes(body))
hdr = bytes(hdr)

print("=" * 62)
plain = try_header(PW, hdr)
if plain is None:
    print("[FAIL] 正确口令也被拒 —— 手册代码块的 CRC 范围/下标有误")
    raise SystemExit(1)
print("[OK] 正确口令 -> 解出")
print("  format ver : %d        (头偏移 68  = buf[4])" % struct.unpack("<H", plain[4:6])[0])
print("  sector size: %d        (头偏移 128 = buf[64])" % struct.unpack("<I", plain[64:68])[0])
print("  mk scope   : %d    (头偏移 108 = buf[44])" % struct.unpack("<Q", plain[44:52])[0])
print("  MK 还原     : %s      (头偏移 256 = buf[192])" % (plain[192:192 + len(MK)] == MK))
print("  header CRC : %s" % (zlib.crc32(plain[0:188]) == struct.unpack("<I", plain[188:192])[0]))
if plain[192:192 + len(MK)] != MK:
    raise SystemExit("[FAIL] MK 不一致")

print("-" * 62)
if try_header(b"wrong-password", hdr) is None:
    print("[OK] 错误口令 -> 正确拒绝")
else:
    print("[FAIL] 错误口令被接受")
    raise SystemExit(1)

print("-" * 62)
# 反例：证明「旧写法」为什么必然失败（缓冲只有 448B，[256:512] 静默截成 192B）
enc = hdr[64:64 + 448]
old_plain = xts_decrypt(dk[:64], enc, sector=0)
print("[证据] 448B 缓冲上 plain[256:512] 实际长度 = %d 字节（应为 256）"
      % len(old_plain[256:512]))
print("[证据] 用旧下标求 CRC32 = 0x%08x，期望 = 0x%08x → 恒不相等"
      % (zlib.crc32(old_plain[256:512]) & 0xffffffff,
         struct.unpack("<I", old_plain[8:12])[0]))
print("=" * 62)
