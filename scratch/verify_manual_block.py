# -*- coding: utf-8 -*-
"""把手册 §6 的 python 代码块**原文抽出来**执行，证明手册自身可跑。
（不是跑我另写的脚本 —— 那样会漏掉手册里的笔误。）
"""
import io
import re
import os
import struct
import zlib
import sys
import textwrap

MD = r"E:\ILOVCTRY\cyberstrike-pro\packs\kb\forensics\磁盘与内存\磁盘数据恢复与容器链提取.md"

src = io.open(MD, encoding="utf-8").read()
# 取 §6 里的 python 代码块
sec = src.split("## 6. VeraCrypt", 1)[1].split("\n## 7.", 1)[0]
blocks = re.findall(r"```python\n(.*?)```", sec, re.S)
assert len(blocks) >= 1, "没抽到 python 代码块"
code = blocks[0]
# 代码块嵌在有序列表项里，行首有 3 空格缩进；复制出来必须先 dedent，否则 IndentationError
code = textwrap.dedent(code)
print("=" * 62)
print("抽出手册代码块 %d 行" % len(code.splitlines()))
assert "crc32(out[192:448])" in code, "手册代码块里没有修正后的 CRC 下标"
assert "plain[256:512]" not in code, "手册里还残留旧的错误下标"

ns = {}
exec(compile(code, "<manual>", "exec"), ns)
xts_decrypt, try_header = ns["xts_decrypt"], ns["try_header"]

# 用同样的代码块里的 xts 做加密（ECB 反向）来造一个卷头
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


PW = b"hunter2"
SALT = os.urandom(64)
MK = os.urandom(96)
body = bytearray(448)
body[0:4] = b"VERA"
struct.pack_into("<H", body, 4, 5)
struct.pack_into("<Q", body, 44, 131072)   # mk scope
struct.pack_into("<I", body, 64, 512)      # sector size
body[192:192 + len(MK)] = MK
struct.pack_into("<I", body, 8, zlib.crc32(bytes(body[192:448])))   # key 区 CRC 先写
struct.pack_into("<I", body, 188, zlib.crc32(bytes(body[0:188])))   # header CRC 后写

dk = PBKDF2(PW, SALT, dkLen=192, count=500000, hmac_hash_module=SHA512)
hdr = bytearray(os.urandom(65536))
hdr[:64] = SALT
hdr[64:64 + 448] = xts_encrypt(dk[:64], bytes(body))
hdr = bytes(hdr)

print("-" * 62)
plain = try_header(PW, hdr)
if plain is None:
    print("[FAIL] 手册代码块：正确口令被拒")
    sys.exit(1)
print("[OK] 手册代码块解出正确口令")
print("     format version =", struct.unpack("<H", plain[4:6])[0], "(期望 5)")
print("     sector size    =", struct.unpack("<I", plain[64:68])[0], "(期望 512)")
print("     mk scope       =", struct.unpack("<Q", plain[44:52])[0], "(期望 131072)")
print("     MK 还原        =", plain[192:192 + len(MK)] == MK)
assert plain[192:192 + len(MK)] == MK
assert try_header(b"wrong", hdr) is None, "[FAIL] 错误口令未被拒"
print("[OK] 手册代码块拒掉错误口令")
print("=" * 62)
print("手册 §6 代码块原文可执行且结论正确")
