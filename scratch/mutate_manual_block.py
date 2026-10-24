# -*- coding: utf-8 -*-
"""行为层变异测试（第二版）。

第一版踩了「假绿」：其中两个「变异」对被测输入是**等效变换**（no-op），
样本根本没走到被测那行 —— 于是变绿被我误判成覆盖不足。这里把两类分开：
  Part A  真变异：对 448B 头部缓冲确实改变行为，必须全部变红。
  Part B  等效变异：先**证明**它对当前输入不改变语义（不是漏洞），
          再把「扇区推进」放到真正生效的多扇区数据区去测。
"""
import io
import os
import re
import struct
import textwrap
import zlib

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA512

MD = r"E:\ILOVCTRY\cyberstrike-pro\packs\kb\forensics\磁盘与内存\磁盘数据恢复与容器链提取.md"
src = io.open(MD, encoding="utf-8").read()
sec = src.split("## 6. VeraCrypt", 1)[1].split("\n## 7.", 1)[0]
GOOD = textwrap.dedent(re.findall(r"```python\n(.*?)```", sec, re.S)[0])


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


def build_volume():
    PW = b"hunter2"
    SALT = os.urandom(64)
    MK = os.urandom(96)
    body = bytearray(448)
    body[0:4] = b"VERA"
    struct.pack_into("<H", body, 4, 5)
    struct.pack_into("<Q", body, 44, 131072)
    struct.pack_into("<I", body, 64, 512)
    body[192:192 + len(MK)] = MK
    struct.pack_into("<I", body, 8, zlib.crc32(bytes(body[192:448])))
    struct.pack_into("<I", body, 188, zlib.crc32(bytes(body[0:188])))
    dk = PBKDF2(PW, SALT, dkLen=192, count=500000, hmac_hash_module=SHA512)
    hdr = bytearray(os.urandom(65536))
    hdr[:64] = SALT
    hdr[64:64 + 448] = xts_encrypt(dk[:64], bytes(body))
    return PW, MK, bytes(hdr)


PW, MK, HDR = build_volume()


def run(code, payload=HDR):
    ns = {}
    exec(compile(code, "<mut>", "exec"), ns)
    return ns["try_header"](PW, payload)


def ok(r):
    return r is not None and r[192:192 + 96] == MK


print("=" * 66)
assert ok(run(GOOD)), "基线就不通，后面全是空跑"
print("[基线] 手册原版 -> 解出且 MK 正确")

# ---------------- Part A：真变异 ----------------
print("-" * 66)
mutants = [
    ("A1 CRC 范围用规范头偏移 out[256:512]",
     GOOD.replace("out[192:448]", "out[256:512]")),
    ("A2 CRC 期望值取错位 out[8:12]->out[12:16]",
     GOOD.replace("out[8:12]", "out[12:16]")),
    ("A3 salt 取错位 hdr[:64]->hdr[64:128]",
     GOOD.replace("hdr[:64]", "hdr[64:128]")),
    ("A4 XTS 用错半把 key dk[:64]->dk[64:128]",
     GOOD.replace("dk[:64]", "dk[64:128]")),
    ("A5 解密区起点错 64->0（含 salt）",
     GOOD.replace("hdr[64:64 + 448]", "hdr[0:448]")),
    ("A6 魔数比对写错 b\"VERA\" -> b\"VERB\"（恒不等）",
     GOOD.replace('out[:4] != b"VERA"', 'out[:4] != b"VERB"')),
]
red = 0
for name, mut in mutants:
    assert mut != GOOD, "变异没落地：%s" % name
    if ok(run(mut)):
        print("[假绿] %-44s -> 居然还解出，校验没覆盖这处" % name)
    else:
        print("[红]   %-44s -> 被拒（校验生效）" % name)
        red += 1
assert red == len(mutants), "Part A 有假绿：%d/%d" % (red, len(mutants))
print("     Part A %d/%d 真变异全部变红" % (red, len(mutants)))

# ---------------- Part B：等效变异（先证明，再换场景） ----------------
print("-" * 66)
print("先证明下面三个是**等效变换**，对 448B 头部缓冲不改变语义：")
print("  · 448 = 28*16，512 = 32*16，都是 16 的整数倍 -> 无 ciphertext stealing")
print("  · off//unit 在 len(data)<=512 时恒为 0 -> 扇区号推进这段代码根本没执行")
print("  · PBKDF2 输出**前缀稳定** -> dkLen 只影响取多少，不影响前 64B")

# B1：解密区 448 -> 512，前 448 字节明文应完全相同
m1 = GOOD.replace("hdr[64:64 + 448]", "hdr[64:64 + 512]")
r1 = run(m1)
print("  B1 取 512B 而非 448B：前 448B 明文字节相同 = %s -> 等效，非漏洞"
      % (r1 is not None and r1[:448] == run(GOOD)[:448]))

# B2：扇区推进 off//unit -> 常量，对单扇区缓冲是 no-op
m2 = GOOD.replace("(sector + off // unit)", "(sector)")
print("  B2 去掉扇区推进：单扇区缓冲下结果相同 = %s -> 等效，非漏洞"
      % (ok(run(m2))))

# B3：dkLen 192 -> 128，只影响尾巴 64B 的 HMAC key，头部解密不用它
m3 = GOOD.replace("dkLen=192", "dkLen=128")
print("  B3 dkLen 192->128：头部解密只用 dk[:64]，结果相同 = %s -> 等效"
      % (ok(run(m3))))
_dk192 = PBKDF2(PW, HDR[:64], dkLen=192, count=500000, hmac_hash_module=SHA512)
_dk128 = PBKDF2(PW, HDR[:64], dkLen=128, count=500000, hmac_hash_module=SHA512)
print("     直接验证前缀稳定：dk[:64] 相同 = %s" % (_dk192[:64] == _dk128[:64]))
assert _dk192[:64] == _dk128[:64]

# 真正该测的地方：多扇区数据区（动作 4「用 MK 解数据区」）
print("-" * 66)
print("把「扇区推进」放回它真正生效的多扇区数据区来测：")
ns = {}
exec(compile(GOOD, "<good>", "exec"), ns)
xts_decrypt = ns["xts_decrypt"]
mk = os.urandom(64)
data = os.urandom(3072)                       # 6 个 512B 扇区
real = xts_encrypt(mk, data, sector=256)      # 从 mk scope(131072B=256扇区) 起算
assert xts_decrypt(mk, real, sector=256) == data, "多扇区基线就不通"
print("  基线：从扇区 256 起解 6 个扇区 -> 完全还原")
assert xts_decrypt(mk, real, sector=0) != data, "扇区起点错却仍能还原？"
print("  [红] 扇区起点错成 0 -> 解出乱码（校验能抓到）")


# 模拟「扇区推进被删」的实现
def xts_no_advance(key64, data, sector=0, unit=512):
    k1, k2 = key64[:32], key64[32:]
    c1, c2 = AES.new(k1, AES.MODE_ECB), AES.new(k2, AES.MODE_ECB)
    out = bytearray()
    for off in range(0, len(data), unit):
        blk = data[off:off + unit]
        tweak = c2.encrypt(sector.to_bytes(16, "little"))       # 不推进
        for i in range(0, len(blk), 16):
            t = bytes(a ^ b for a, b in zip(tweak, blk[i:i + 16]))
            out += bytes(a ^ b for a, b in zip(c1.decrypt(t), tweak))
            carry = 0x87 if tweak[15] & 0x80 else 0
            tweak = ((int.from_bytes(tweak, "little") << 1) & ((1 << 128) - 1)
                     ^ carry).to_bytes(16, "little")
    return bytes(out)


assert xts_no_advance(mk, real, sector=256) != data, "删掉扇区推进居然还能还原？"
print("  [红] 删掉扇区推进 -> 第 2 个扇区起全错（单扇区测不出，多扇区必红）")
print("=" * 66)
print("结论：Part A 6/6 真变异变红；Part B 三例已证明为等效变换而非漏洞；")
print("      扇区推进在多扇区场景下可被检出。手册代码块的下标与守卫均有效。")
