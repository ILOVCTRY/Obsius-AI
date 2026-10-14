#!/usr/bin/env python3
"""
preliminary.apk (2026 腾讯游戏安全初赛 / Truck Town) token -> flag 算法
正向: token(8位hex字符串) -> flag 后缀(16位大写hex)
逆向: flag 后缀 -> token

流程(均已在还原的 libsec2026.so 原始镜像中逐字节核对):
  1. GDScript 侧 xor_enc: r[i]=a[i]^a[i+1] (i=0..6), r[7]=a[7]^r[0]
  2. Native GameExtension.Process(): ChaCha20 变体加密, 取 keystream 前 8 字节异或
     - key   = "Th1s ls n0t a rea1 key!!@sec2026"  (.data 0xED5D2, 运行时 XOR 0x97A36EF7A74F5E4E 解密)
     - nonce = "012345678901"                      (.data 0xED5F3, 运行时解密: enc[i]^kb[i%8]^i)
     - counter = 0
     - 魔改点: state[0..3] 常量不是 "expand 32-byte k", 而是 "fxpaod 31-byse k" (0x19ED8)
  3. %02X 大写 hex
flag 完整形态: flag{sec2026_PART1_<suffix>}   (PART0 为固定示例 flag{sec2026_PART0_example})
"""
import struct

KEY = b"Th1s ls n0t a rea1 key!!@sec2026"
NONCE = b"012345678901"
CONST = b"fxpaod 31-byse k"   # 魔改的 ChaCha20 常量

def chacha20_block(key, nonce, counter=0, const=CONST):
    st = list(struct.unpack('<4I', const) + struct.unpack('<8I', key) + (counter,) + struct.unpack('<3I', nonce))
    w = st[:]
    def qr(a, b, c, d):
        w[a] = (w[a]+w[b]) & 0xffffffff; w[d] ^= w[a]; w[d] = ((w[d]<<16)|(w[d]>>16)) & 0xffffffff
        w[c] = (w[c]+w[d]) & 0xffffffff; w[b] ^= w[c]; w[b] = ((w[b]<<12)|(w[b]>>20)) & 0xffffffff
        w[a] = (w[a]+w[b]) & 0xffffffff; w[d] ^= w[a]; w[d] = ((w[d]<<8)|(w[d]>>24)) & 0xffffffff
        w[c] = (w[c]+w[d]) & 0xffffffff; w[b] ^= w[c]; w[b] = ((w[b]<<7)|(w[b]>>25)) & 0xffffffff
    for _ in range(10):
        qr(0,4,8,12); qr(1,5,9,13); qr(2,6,10,14); qr(3,7,11,15)
        qr(0,5,10,15); qr(1,6,11,12); qr(2,7,8,13); qr(3,4,9,14)
    return struct.pack('<16I', *[(x+y) & 0xffffffff for x, y in zip(w, st)])

KEYSTREAM = chacha20_block(KEY, NONCE, 0)

def xor_enc(token: str) -> bytes:
    a = token.encode()[:8]
    assert len(a) == 8
    r = bytearray(8)
    for i in range(7):
        r[i] = a[i] ^ a[i+1]
    r[7] = a[7] ^ r[0]
    return bytes(r)

def xor_dec(r: bytes) -> bytes:
    r = bytearray(r)
    r[7] ^= r[0]                 # 必须先还原尾字节
    for i in range(6, -1, -1):
        r[i] ^= r[i+1]
    return bytes(r)

def token_to_flag(token: str) -> str:
    """token(8字符) -> 完整 flag"""
    ct = bytes(a ^ b for a, b in zip(xor_enc(token), KEYSTREAM))
    return "flag{sec2026_PART1_%s}" % ''.join('%02X' % b for b in ct)

def flag_to_token(flag: str) -> str:
    """完整 flag(或16位hex后缀) -> token"""
    s = flag
    if s.startswith("flag{"):
        s = s[5:-1]
    if "PART1_" in s:
        s = s.split("PART1_", 1)[1]
    ct = bytes.fromhex(s)
    return xor_dec(bytes(a ^ b for a, b in zip(ct, KEYSTREAM))).decode()

if __name__ == '__main__':
    vecs = {'a1b2c3d4': '2A4C031823617318', 'cf14eaad': '7F4856187736261D', 'deadbeef': '7B1B564F7436201B'}
    for tok, exp in vecs.items():
        got = token_to_flag(tok)
        assert got == "flag{sec2026_PART1_%s}" % exp, (tok, got)
        back = flag_to_token(got)
        assert back == tok, (got, back)
        print('%s -> %s -> %s  OK' % (tok, got, back))
    print('all vectors pass (forward + reverse)')
