#!/usr/bin/env python3
"""NRV2B decompressor matching the UPX arm64 stub semantics (bit-exact).

Usage as library:  from nrv2b import nrv2b_decompress
    out, consumed = nrv2b_decompress(src_bytes)

The algorithm mirrors the UPX aarch64 decompress stub:
  - 32-bit bit buffer `bb` initialized to 0x80000000; when a shift empties it,
    a new LE u32 is loaded and shifted in with the marker carry.
  - literal: bit==1 -> copy one byte from src.
  - match:   getlen() gamma-ish length; offset high bits from length-3 plus one
             raw byte, stored negated (~); end marker when ~offset == 0.
"""
import struct


def nrv2b_decompress(src):
    spos = 0
    dst = bytearray()
    bb = 0x80000000

    def getbit():
        nonlocal spos, bb
        t = bb * 2
        carry = t >> 32
        bb = t & 0xFFFFFFFF
        if bb == 0:
            bb = struct.unpack('<I', src[spos:spos + 4])[0]
            spos += 4
            t = bb * 2 + carry
            carry = t >> 32
            bb = t & 0xFFFFFFFF
        return carry

    def getlen():
        # w1 = 1; repeat: w1 = w1*2 + bit; until the next bit is 1
        w1 = 1
        while True:
            w1 = (w1 * 2 + getbit()) & 0xFFFFFFFF
            if getbit():
                break
        return w1

    m_off = 0xFFFFFFFF  # w5, unsigned32, initial -1
    while True:
        if getbit():
            dst.append(src[spos])
            spos += 1
            continue
        acc = getlen()
        hi = (acc - 3) & 0xFFFFFFFF
        if acc >= 3:
            b = src[spos]
            spos += 1
            raw = ((hi << 8) | b) & 0xFFFFFFFF
            m_off = (~raw) & 0xFFFFFFFF
            if m_off == 0:
                break  # end of stream
        w1 = getbit() & 1
        w1 = ((w1 << 1) | getbit()) & 0xFFFFFFFF
        if w1 == 0:
            w1 = (getlen() + 2) & 0xFFFFFFFF
        if m_off < (0x100000000 - 0xD00):
            w1 += 1
        off = m_off - 0x100000000  # signed (negative window offset)
        while True:
            dst.append(dst[len(dst) + off])
            w1 -= 1
            if w1 < 0:
                break
    return bytes(dst), spos


if __name__ == '__main__':
    import sys
    inp = open(sys.argv[1], 'rb').read()
    skip = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0
    out, used = nrv2b_decompress(inp[skip:])
    open(sys.argv[3], 'wb').write(out)
    print('decompressed %d bytes, consumed %#x of input' % (len(out), used))
