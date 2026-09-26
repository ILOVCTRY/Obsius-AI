#!/usr/bin/env python3
"""Apply R_AARCH64_RELATIVE relocations from a packed ELF onto an unpacked memory image.

The packed file's .rela.dyn addends already reference original vaddrs, so with a
base-0 memory image this is just: image[r_offset] = r_addend (8 bytes, LE).
Also copies PT_LOAD RW segments from the packed file into the image
(packed vaddrs for RW are preserved by this packer family).

Usage:
  python3 apply_relocs.py <packed.so> <image_in.bin> <image_out.bin>
"""
import struct
import sys

R_AARCH64_RELATIVE = 0x403


def parse_phdrs(data):
    e_phoff = struct.unpack('<Q', data[0x20:0x28])[0]
    phentsize = struct.unpack('<H', data[0x36:0x38])[0]
    phnum = struct.unpack('<H', data[0x38:0x3A])[0]
    phdrs = []
    for i in range(phnum):
        o = e_phoff + i * phentsize
        p_type, p_flags = struct.unpack('<II', data[o:o + 8])
        p_off, p_vaddr, _, p_filesz, p_memsz, _ = struct.unpack('<QQQQQQ', data[o + 8:o + 56])
        phdrs.append(dict(type=p_type, flags=p_flags, off=p_off,
                          vaddr=p_vaddr, filesz=p_filesz, memsz=p_memsz))
    return phdrs


def main():
    packed = open(sys.argv[1], 'rb').read()
    img = bytearray(open(sys.argv[2], 'rb').read())
    phdrs = parse_phdrs(packed)

    # copy non-exec LOAD segments (RW data) into the image
    for p in phdrs:
        if p['type'] == 1 and p['flags'] & 2 and p['filesz']:  # PF_W
            end = p['vaddr'] + p['filesz']
            if end > len(img):
                img += b'\0' * (end - len(img))
            img[p['vaddr']:end] = packed[p['off']:p['off'] + p['filesz']]

    # locate .rela.dyn through PT_DYNAMIC
    n = 0
    for p in phdrs:
        if p['type'] != 2:
            continue
        dyn = packed[p['off']:p['off'] + p['filesz']]
        d = {}
        for i in range(0, len(dyn) - 15, 16):
            tag, val = struct.unpack('<qQ', dyn[i:i + 16])
            if tag == 0:
                break
            d[tag] = val
        rela, relasz, relaent = d.get(0x7), d.get(0x8), d.get(0x9, 24)
        if not rela or not relasz:
            continue
        # rela vaddr -> file offset via RX load (vaddr==off for the first LOAD)
        off = None
        for q in phdrs:
            if q['type'] == 1 and q['vaddr'] <= rela < q['vaddr'] + q['filesz']:
                off = q['off'] + (rela - q['vaddr'])
                break
        if off is None:
            continue
        for i in range(relasz // relaent):
            r_off, r_info, r_addend = struct.unpack('<QQq', packed[off + i * relaent: off + i * relaent + 24])
            if (r_info & 0xFFFFFFFF) == R_AARCH64_RELATIVE and r_off + 8 <= len(img):
                struct.pack_into('<q', img, r_off, r_addend)
                n += 1

    open(sys.argv[3], 'wb').write(bytes(img))
    print('applied %d RELATIVE relocs -> %s' % (n, sys.argv[3]))


if __name__ == '__main__':
    main()
