#!/usr/bin/env python3
"""Generic unicorn-based unpacker for UPX-style packed aarch64 Android shared libraries.

Handles the "shlib fold" scheme seen in Godot CTF extensions:
  init_array stub -> NRV2B stage1 -> memfd/ftruncate/mmap(MAP_SHARED) ->
  NRV2B/LZMA decompress whole original image -> munmap -> mmap(MAP_PRIVATE) -> jump.

Key emulation notes:
  - auxv is faked with AT_PAGESZ (type 6) = 0x1000.
  - Writes through MAP_SHARED memfd mappings are synced back to the emulated
    file on munmap; without this the MAP_PRIVATE remap loses the unpacked data.
  - Execution stops when the PC first enters a large (>64 KiB) memfd-backed
    region (the unpacked image), then memory is dumped.

Usage:
  python3 upx_shlib_emu.py <packed.so> <out_prefix> [--entry 0xADDR] [--dump-size 0xN]

Outputs:
  <out_prefix>_libbase.bin   memory image of the library base region
  <out_prefix>_memfd.bin     the big memfd mapping (unpacked image tail), if any
  <out_prefix>.log           syscall/decompress log
Requires: unicorn (pip install unicorn), capstone not required.
"""
import argparse
import struct
import sys

from unicorn import UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN, UC_PROT_ALL, Uc, UcError
from unicorn.arm64_const import (
    UC_ARM64_REG_PC,
    UC_ARM64_REG_SP,
    UC_ARM64_REG_X0,
    UC_ARM64_REG_X1,
    UC_ARM64_REG_X2,
    UC_ARM64_REG_X3,
    UC_ARM64_REG_X4,
    UC_ARM64_REG_X5,
    UC_ARM64_REG_X8,
)
from unicorn import UC_HOOK_CODE, UC_HOOK_INTR

LIB_BASE = 0x10000000
STACK_BASE = 0x20000000
STACK_SIZE = 0x200000
ALLOC_TOP = 0x40000000


def parse_elf(data):
    assert data[:4] == b'\x7fELF' and data[4] == 2, 'need ELF64'
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


def vaddr_to_off(phdrs, vaddr):
    for p in phdrs:
        if p['type'] == 1 and p['vaddr'] <= vaddr < p['vaddr'] + p['filesz']:
            return p['off'] + (vaddr - p['vaddr'])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('so')
    ap.add_argument('out_prefix')
    ap.add_argument('--entry', default=None, help='override init entry vaddr (hex)')
    ap.add_argument('--dump-size', default='0x100000')
    ap.add_argument('--max-insns', type=int, default=80_000_000)
    args = ap.parse_args()

    data = open(args.so, 'rb').read()
    phdrs = parse_elf(data)
    max_vaddr = max(p['vaddr'] + p['memsz'] for p in phdrs if p['type'] == 1)
    lib_size = (max_vaddr + 0xFFFFF) & ~0xFFFFF

    mu = Uc(UC_ARCH_ARM64, UC_MODE_LITTLE_ENDIAN)
    mu.mem_map(LIB_BASE, lib_size, UC_PROT_ALL)
    for p in phdrs:
        if p['type'] == 1 and p['filesz']:
            mu.mem_write(LIB_BASE + p['vaddr'], data[p['off']:p['off'] + p['filesz']])
    mu.mem_map(STACK_BASE, STACK_SIZE, UC_PROT_ALL)
    mu.reg_write(UC_ARM64_REG_SP, STACK_BASE + STACK_SIZE - 0x1000)
    for r in (UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2):
        mu.reg_write(r, 0)

    # find DT_INIT / DT_INIT_ARRAY via PT_DYNAMIC
    entry = int(args.entry, 0) if args.entry else None
    if entry is None:
        for p in phdrs:
            if p['type'] != 2:
                continue
            dyn = data[p['off']:p['off'] + p['filesz']]
            d = {}
            for i in range(0, len(dyn) - 15, 16):
                tag, val = struct.unpack('<qQ', dyn[i:i + 16])
                if tag == 0:
                    break
                d[tag] = val
            if 0x19 in d:  # DT_INIT_ARRAY
                off = vaddr_to_off(phdrs, d[0x19])
                entry = struct.unpack('<Q', data[off:off + 8])[0]
                break
            if 0x0C in d:  # DT_INIT
                entry = d[0x0C]
                break
    if entry is None:
        sys.exit('could not locate init entry; pass --entry')

    alloc_ptr = [ALLOC_TOP]
    memfd = {'data': bytearray(), 'pos': 0}
    memfd_maps = []  # (addr, len, file_off, shared)
    log = open(args.out_prefix + '.log', 'w')

    auxv = struct.pack('<QQ', 6, 0x1000) + struct.pack('<QQ', 0, 0)
    auxv += b'\0' * (0x400 - len(auxv))

    def read_str(addr):
        s = b''
        while True:
            c = mu.mem_read(addr, 1)
            if c == b'\0':
                break
            s += c
            addr += 1
        return s.decode('utf-8', 'replace')

    def map_at(addr, length):
        length = (length + 0xFFF) & ~0xFFF
        addr &= ~0xFFF
        try:
            mu.mem_map(addr, length, UC_PROT_ALL)
        except UcError:
            mu.mem_unmap(addr, length)
            mu.mem_map(addr, length, UC_PROT_ALL)
        return addr

    def hook_intr(uc, intno, user):
        nr = uc.reg_read(UC_ARM64_REG_X8)
        x0 = uc.reg_read(UC_ARM64_REG_X0)
        x1 = uc.reg_read(UC_ARM64_REG_X1)
        x2 = uc.reg_read(UC_ARM64_REG_X2)
        x4 = uc.reg_read(UC_ARM64_REG_X4)
        x5 = uc.reg_read(UC_ARM64_REG_X5)
        ret = 0
        if nr == 56:    # openat
            log.write('openat(%s)\n' % read_str(x1))
            ret = 100
        elif nr == 63:  # read
            if x0 == 100:
                n = min(x2, len(auxv))
                uc.mem_write(x1, auxv[:n])
                ret = n
        elif nr == 57:  # close
            pass
        elif nr == 64:  # write
            if x0 == 200:
                pos = memfd['pos']
                chunk = bytes(uc.mem_read(x1, x2))
                if len(memfd['data']) < pos + x2:
                    memfd['data'] += b'\0' * (pos + x2 - len(memfd['data']))
                memfd['data'][pos:pos + x2] = chunk
                log.write('write memfd %d bytes at pos %#x\n' % (x2, pos))
                for (maddr, mln, moff, _shared) in memfd_maps:
                    if moff <= pos < moff + mln:
                        try:
                            uc.mem_write(maddr + (pos - moff), chunk)
                        except UcError:
                            pass
                memfd['pos'] = pos + x2
            ret = x2
        elif nr == 46:  # ftruncate
            if x0 == 200:
                if len(memfd['data']) < x1:
                    memfd['data'] += b'\0' * (x1 - len(memfd['data']))
                else:
                    memfd['data'] = memfd['data'][:x1]
                log.write('ftruncate(memfd, %#x)\n' % x1)
        elif nr == 279:  # memfd_create
            ret = 200
        elif nr == 222:  # mmap
            flags = uc.reg_read(UC_ARM64_REG_X3)
            if flags & 0x10 and x0:
                addr = map_at(x0, x1)
            else:
                addr = map_at(alloc_ptr[0], x1)
                alloc_ptr[0] += ((x1 + 0xFFF) & ~0xFFF) + 0x1000
            if x4 == 200:
                chunk = bytes(memfd['data'][x5:x5 + x1])
                if chunk:
                    uc.mem_write(addr, chunk)
                memfd_maps.append((addr, x1, x5, bool(flags & 1)))
                log.write('mmap MEMFD addr=%#x len=%#x flags=%#x off=%#x\n' % (addr, x1, flags, x5))
            ret = addr
        elif nr == 215:  # munmap
            for (maddr, mln, moff, shared) in list(memfd_maps):
                if maddr == x0 and shared:
                    try:
                        content = bytes(uc.mem_read(maddr, (mln + 0xFFF) & ~0xFFF))
                        need = moff + len(content)
                        if len(memfd['data']) < need:
                            memfd['data'] += b'\0' * (need - len(memfd['data']))
                        memfd['data'][moff:moff + len(content)] = content
                        log.write('munmap: synced %#x bytes back to memfd\n' % len(content))
                    except UcError as e:
                        log.write('munmap sync fail: %s\n' % e)
            ret = 0
        elif nr in (226, 227):  # mprotect / msync
            ret = 0
        elif nr == 62:  # lseek
            if x0 == 200 and x2 == 0:
                memfd['pos'] = x1
            ret = memfd['pos'] if x0 == 200 else 0
        else:
            log.write('UNKNOWN syscall %d args=(%#x,%#x,%#x,%#x)\n' % (nr, x0, x1, x2, x4))
        uc.reg_write(UC_ARM64_REG_X0, ret & 0xFFFFFFFFFFFFFFFF)

    mu.hook_add(UC_HOOK_INTR, hook_intr)

    done = [False]

    def hook_code(uc, address, size, user):
        for (addr, ln, off, _s) in memfd_maps:
            if ln > 0x10000 and addr <= address < addr + ln:
                log.write('REACHED UNPACKED CODE at %#x (img+%#x)\n' % (address, address - addr))
                done[0] = True
                uc.emu_stop()

    mu.hook_add(UC_HOOK_CODE, hook_code)

    try:
        mu.emu_start(LIB_BASE + entry, LIB_BASE + lib_size, count=args.max_insns)
    except UcError as e:
        log.write('EMU ERROR %s at pc=%#x\n' % (e, mu.reg_read(UC_ARM64_REG_PC)))
    log.close()

    dump_size = min(int(args.dump_size, 0), lib_size)
    img = bytes(mu.mem_read(LIB_BASE, dump_size))
    open(args.out_prefix + '_libbase.bin', 'wb').write(img)
    print('wrote %s_libbase.bin (%d bytes), reached_unpacked=%s' % (args.out_prefix, len(img), done[0]))
    for (addr, ln, off, _s) in memfd_maps:
        if ln > 0x10000:
            img2 = bytes(mu.mem_read(addr, (ln + 0xFFF) & ~0xFFF))[:ln]
            open(args.out_prefix + '_memfd.bin', 'wb').write(img2)
            print('wrote %s_memfd.bin (%d bytes from %#x)' % (args.out_prefix, len(img2), addr))
            break
    print('log: %s.log' % args.out_prefix)


if __name__ == '__main__':
    main()
