"""验证手册里那段代码：自造一个 VeraCrypt 结构卷，能否正确解出。"""
import struct, zlib, os
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA512


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


def xts_decrypt(key64, data, sector=0, unit=512):
    """手册里的实现（一字不改）。"""
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


PASSWORD = b"hunter2"
COUNT = 50000          # 测试用小迭代
salt = os.urandom(64)

# ---- 造明文 header 前 512 字节 ----
plain = bytearray(512)
plain[0:4] = b"VERA"
struct.pack_into("<H", plain, 4, 5)              # 实际偏移 68-64=4
struct.pack_into("<H", plain, 6, 0x010b)
plain[8:12] = b"\x00" * 4                        # 待填 CRC @72
struct.pack_into("<Q", plain, 36, 0)             # hidden size @100
struct.pack_into("<Q", plain, 44, 4 << 20)       # volume size @108
struct.pack_into("<Q", plain, 52, 131072)        # mk scope offset @116
struct.pack_into("<Q", plain, 60, 4 << 20)       # encrypted area size @124
struct.pack_into("<I", plain, 64, 512)           # sector size @128
struct.pack_into("<I", plain, 188, 0)            # crc of header fields @252
mk = os.urandom(64)
plain[192:256] = mk                              # master keys @256

# CRC-32 of header fields: 覆盖明文 [64:252] → 相对 plain[0:188]
crc_hdr = zlib.crc32(bytes(plain[0:188]))
struct.pack_into("<I", plain, 188, crc_hdr)
# CRC-32 of key area: 覆盖明文 [256:512] → 相对 plain[192:448]
crc_key = zlib.crc32(bytes(plain[192:448]))
struct.pack_into("<I", plain, 8, crc_key)

dk = PBKDF2(PASSWORD, salt, dkLen=192, count=COUNT, hmac_hash_module=SHA512)
enc = xts_encrypt(dk[:64], bytes(plain))
hdr = salt + enc + b"\x00" * (65536 - 512)

# ---- 用手册那段 try_header 解 ----
def try_header(pw, hdr, count=COUNT):
    salt_, enc_ = hdr[:64], hdr[64:64 + 448]
    dk_ = PBKDF2(pw, salt_, dkLen=192, count=count, hmac_hash_module=SHA512)
    plain_ = xts_decrypt(dk_[:64], enc_, sector=0)
    if plain_[:4] != b"VERA":
        return None
    if zlib.crc32(plain_[256:512]) != struct.unpack("<I", plain_[8:12])[0]:
        return None
    return plain_

got = try_header(PASSWORD, hdr)
print("正确口令 ->", "解出" if got else "失败")
if got:
    print("  format version:", struct.unpack_from("<H", got, 68)[0])
    print("  sector size   :", struct.unpack_from("<I", got, 128)[0])
    print("  mk scope off  :", struct.unpack_from("<Q", got, 108)[0])
    print("  MK 还原正确   :", got[256:320] == mk)

print("错误口令 ->", "误判通过(错!)" if try_header(b"wrong", hdr) else "正确拒绝")
