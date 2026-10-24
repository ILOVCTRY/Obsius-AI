# -*- coding: utf-8 -*-
"""交叉校验手册版手写 XTS 与 cryptography 的 modes.XTS 是否逐字节一致，
并验证「用 MK 解数据区」时 tweak 扇区号的两种起点（卷起点 vs mk scope 起点）。
"""
import os
from Crypto.Cipher import AES
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


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


def ref_xts_decrypt(key64, data, sector=0):
    """cryptography 的 modes.XTS 把**整个入参当作一个 data unit**，
    不会按 512B 自动推进扇区号。所以多扇区必须自己切分逐扇区调用。"""
    d = Cipher(algorithms.AES(key64), modes.XTS(sector.to_bytes(16, "little"))).decryptor()
    return d.update(data) + d.finalize()


def ref_multi_sector(key64, data, first_sector=0, unit=512):
    out = bytearray()
    for i in range(0, len(data), unit):
        out += ref_xts_decrypt(key64, data[i:i + unit], sector=first_sector + i // unit)
    return bytes(out)


key = os.urandom(64)
data = os.urandom(1536)          # 3 个 512B 扇区
print("=" * 62)
for sec in (0, 1, 7, 256, 131072 // 512):
    mine = xts_decrypt(key, data, sector=sec)
    ref = ref_multi_sector(key, data, first_sector=sec)
    print("sector=%-7d 手写 == cryptography(逐扇区) : %s" % (sec, mine == ref))
    assert mine == ref, "XTS 实现不一致"

print("-" * 62)
# 反例：把整段直接喂给 modes.XTS（不分扇区）会得到不同结果
whole = ref_xts_decrypt(key, data, sector=0)
print("[坑] 整段喂 modes.XTS != 逐扇区推进 : %s  （必须自己切 512B）"
      % (whole != xts_decrypt(key, data, sector=0)))

print("-" * 62)
# 单扇区（448B 头区）也过一遍
part = data[:448]
print("448B 非整扇区段一致 :",
      xts_decrypt(key, part, sector=0) == ref_xts_decrypt(key, part, sector=0))
print("=" * 62)
print("[OK] 手册版 XTS 与 cryptography 参考实现逐字节一致 —— 可放心发布")
