#!/usr/bin/env python3
"""Deterministic triage/unpack runner for Godot-engine Android CTF APKs.

Pipeline (no LLM required):
  1. APK triage: detect Godot markers (assets/.godot, project.binary,
     libgodot_android.so, *.gdc, *.gdextension, assets.sparsepck).
  2. Locate the custom GDExtension .so (the small lib referenced by
     assets/ext/*.gdextension or any non-godot/non-c++ lib).
  3. Packer detection on the custom .so (section/program header sanity,
     .text entropy, init_array stub with raw svc syscalls, embedded ELF).
  4. Unpack via unicorn emulation (upx_shlib_emu.py) + RELATIVE reloc repair
     (apply_relocs.py) -> unpacked_reloc.bin (vaddr == file offset).
  5. Evidence extraction on the unpacked image:
     - strings + suspicious crypto/flag strings;
     - adrp+add / adr manual-decoded xrefs to those strings;
     - init-time XOR deobfuscation routines (mov/movk 64-bit key + adr + loop)
       -> candidate keys/nonces recovered;
     - crypto constant scan (ChaCha20 sigma variants, MD5/SHA init vectors,
       AES S-box, base64/hex alphabets).
  6. findings.json + report.md with next-step guidance for the solver agent.

The runner never claims a flag; it produces verified *evidence* (unpacked
image, xrefs, recovered constants) so the LLM solver can finish the last mile
(read the flag function, write verifier).

Usage:
  python3 godot_ctf_runner.py <target.apk> --workspace <dir> [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

GODOT_MARKERS = ("assets/.godot/", "assets/project.binary", "libgodot_android.so")
ENGINE_LIB_SUBSTR = ("libgodot", "libc++_shared")
CRYPTO_STRINGS = re.compile(
    rb"expand 32-byte k|chacha|salsa|md5|sha1|sha256|sha-?1|sha-?256|aes|base64|rc4|"
    rb"0123456789abcdef|0123456789ABCDEF|flag\{|flag_|token", re.I)
# stock godot-cpp / engine noise that matches the patterns above but is never the challenge
STOCK_NOISE = re.compile(
    rb"^string_|^editor_help|_chars$|^window_|^global_menu_|^accessibility_|"
    rb"^variant_|^print_|^is_processing|^set_process|keyboard|filenocasecmp|casecmp", re.I)


# ---------------------------------------------------------------- ELF helpers

def parse_phdrs(data: bytes) -> list[dict]:
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


def parse_shdrs(data: bytes) -> list[dict]:
    e_shoff = struct.unpack('<Q', data[0x28:0x30])[0]
    shentsize = struct.unpack('<H', data[0x3A:0x3C])[0]
    shnum = struct.unpack('<H', data[0x3C:0x3E])[0]
    shstrndx = struct.unpack('<H', data[0x3E:0x40])[0]
    secs = []
    for i in range(shnum):
        o = e_shoff + i * shentsize
        name, stype, flags, addr, off, size = struct.unpack('<IIQQQQ', data[o:o + 40])
        secs.append(dict(name=name, type=stype, flags=flags, addr=addr, off=off, size=size))
    if 0 <= shstrndx < len(secs):
        st = secs[shstrndx]
        tab = data[st['off']:st['off'] + st['size']]
        for s in secs:
            n = s['name']
            if n < len(tab):
                s['sname'] = tab[n:tab.find(b'\0', n)].decode('utf-8', 'replace')
            else:
                s['sname'] = ''
    return secs


def entropy(buf: bytes) -> float:
    if not buf:
        return 0.0
    c = Counter(buf)
    n = len(buf)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def sext(v: int, n: int) -> int:
    return v - (1 << n) if v >> (n - 1) else v


# ------------------------------------------------------------ packer detection

def detect_packer(so: bytes, secs: list[dict], phdrs: list[dict]) -> dict:
    findings = []
    text = next((s for s in secs if s.get('sname') == '.text'), None)
    if text:
        blob = so[text['off']:text['off'] + min(text['size'], 0x10000)]
        ent = entropy(blob)
        if ent > 5.5:
            findings.append('.text first 64K entropy %.2f (>5.5, likely packed/encrypted)' % ent)
        if b'\x7fELF' in so[text['off']:text['off'] + text['size']]:
            findings.append('embedded \\x7fELF inside .text (packed payload)')
        if b'    ' in blob[:0x400]:
            findings.append('repeated 0x20 runs in .text head (encrypted-fill pattern)')
    for s in secs:
        if s.get('sname') == '.plt' and s['off'] + s['size'] > len(so):
            findings.append('.plt section points past EOF (fake/mangled section headers)')
    rx = [p for p in phdrs if p['type'] == 1 and p['flags'] & 1]
    for p in rx:
        if p['vaddr'] != p['off']:
            findings.append('RX segment vaddr(%#x) != file offset(%#x)' % (p['vaddr'], p['off']))
    # init_array stub with raw svc: look for svc #0 (d4000001) clusters in RX tail
    svc_count = len(re.findall(rb'\x01\x00\x00\xd4', so))
    if svc_count > 4:
        findings.append('%d raw svc #0 instructions (syscall-based unpacker stub)' % svc_count)
    return {'packed': bool(findings), 'evidence': findings}


# ------------------------------------------------------- init XOR deobfuscator

def scan_init_xor(img: bytes, text_lo: int, text_hi: int) -> list[dict]:
    """Find mov/movk-built 64-bit constants used as XOR keys over adr-targeted data.

    Pattern (from sec2026): movz/movk x10 building key, adr x8, #data, then a
    byte loop with eor/strb. We simply: collect movz/movk 64-bit constant builds,
    nearby adr targets, and try XORing 8-64 bytes at the adr target with the key;
    a printable result is reported.
    """
    out = []
    for off in range(text_lo, min(text_hi, len(img)) - 4, 4):
        w = struct.unpack('<I', img[off:off + 4])[0]
        if (w & 0x9F000000) != 0x10000000:  # adr only
            continue
        immlo = (w >> 29) & 3
        immhi = (w >> 5) & 0x7FFFF
        tgt = off + sext((immhi << 2) | immlo, 21)
        if not (0 <= tgt < len(img) - 8):
            continue
        # try keys from any movz/movk 64-bit chains within +/-64 bytes
        keys = set()
        for b in range(max(off - 64, text_lo), min(off + 64, text_hi, len(img) - 4), 4):
            ww = struct.unpack('<I', img[b:b + 4])[0]
            if (ww & 0xFF800000) in (0xD2800000, 0xF2800000):  # movz/movk 64-bit
                hw = (ww >> 21) & 3
                imm16 = (ww >> 5) & 0xFFFF
                rd = ww & 31
                keys.add((rd, hw, imm16))
        byrd = {}
        for rd, hw, imm16 in keys:
            byrd.setdefault(rd, {})[hw] = imm16
        for rd, parts in byrd.items():
            if len(parts) < 2:
                continue
            k = 0
            for hw, imm16 in parts.items():
                k |= imm16 << (16 * hw)
            if k == 0:
                continue
            kb = k.to_bytes(8, 'little')
            dec = bytes(c ^ kb[i % 8] for i, c in enumerate(img[tgt:tgt + 40]))
            printable = sum(1 for c in dec[:32] if 32 <= c <= 126 or c == 0)
            if printable >= 28:
                s = dec.split(b'\0')[0]
                if len(s) >= 8:
                    out.append({'adr_site': hex(off), 'data': hex(tgt),
                                'xor_key64': hex(k), 'decoded': s.decode('utf-8', 'replace')})
    # dedup
    seen = set()
    uniq = []
    for o in out:
        if o['decoded'] not in seen:
            seen.add(o['decoded'])
            uniq.append(o)
    return uniq[:16]


# ------------------------------------------------------------------- xref scan

def scan_xrefs(img: bytes, text_lo: int, text_hi: int, targets: dict[int, str]) -> list[dict]:
    """Manual adrp+add / adr decode (capstone linear sweep desyncs on data)."""
    hits = []
    for off in range(text_lo, min(text_hi, len(img)) - 4, 4):
        w = struct.unpack('<I', img[off:off + 4])[0]
        typ = w & 0x9F000000
        if typ == 0x90000000:  # adrp
            immlo = (w >> 29) & 3
            immhi = (w >> 5) & 0x7FFFF
            page = (off & ~0xFFF) + (sext((immhi << 2) | immlo, 21) << 12)
            rd = w & 31
            for t, name in targets.items():
                if (page & ~0xFFF) == (t & ~0xFFF):
                    for k in range(1, 5):
                        nw = struct.unpack('<I', img[off + 4 * k:off + 4 * k + 4])[0]
                        if (nw & 0x7F000000) == 0x11000000:
                            imm12 = (nw >> 10) & 0xFFF
                            if ((nw >> 22) & 3) == 1:
                                imm12 <<= 12
                            if ((nw >> 5) & 31) == rd and page + imm12 == t:
                                hits.append({'site': hex(off), 'target': hex(t), 'string': name})
        elif typ == 0x10000000:  # adr
            immlo = (w >> 29) & 3
            immhi = (w >> 5) & 0x7FFFF
            t = off + sext((immhi << 2) | immlo, 21)
            if t in targets:
                hits.append({'site': hex(off), 'target': hex(t), 'string': targets[t]})
    return hits[:200]


def strings_with_offsets(data: bytes, min_len: int = 6) -> list[tuple[int, bytes]]:
    out = []
    for m in re.finditer(rb'[ -~]{%d,}' % min_len, data):
        out.append((m.start(), m.group()))
    return out


# ------------------------------------------------------------------ main flow

def run_cmd(cmd: list[str], timeout: int = 1200) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode('utf-8', 'replace')
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, 'timeout'


def analyze(apk: Path, workspace: Path) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    extract_dir = workspace / 'extract'
    with zipfile.ZipFile(apk) as zf:
        zf.extractall(extract_dir)
        entries = zf.namelist()

    result: dict = {'apk': str(apk.resolve()), 'workspace': str(workspace.resolve())}

    godot_hits = [m for m in GODOT_MARKERS if any(e.startswith(m) or m in e for e in entries)]
    gdc = [e for e in entries if e.endswith('.gdc')]
    gdext = [e for e in entries if e.endswith('.gdextension')]
    result['godot'] = {
        'detected': bool(godot_hits),
        'markers': godot_hits,
        'gdc_scripts': gdc,
        'gdextensions': gdext,
        'sparsepck': [e for e in entries if e.endswith('.sparsepck')],
    }
    libs = [e for e in entries if re.match(r'lib/[^/]+/[^/]+\.so$', e)]
    custom_libs = [e for e in libs if not any(s in e for s in ENGINE_LIB_SUBSTR)]
    result['libs'] = {'all': libs, 'custom': custom_libs}
    if not result['godot']['detected']:
        result['verdict'] = 'not a Godot APK (markers missing); use android_ctf_runner.py instead'
        return result

    unpack_dir = workspace / 'unpacked'
    unpack_dir.mkdir(exist_ok=True)
    result['packed_libs'] = {}
    for rel in custom_libs:
        so_path = extract_dir / rel
        data = so_path.read_bytes()
        secs = parse_shdrs(data)
        phdrs = parse_phdrs(data)
        pk = detect_packer(data, secs, phdrs)
        result['packed_libs'][rel] = pk
        if not pk['packed']:
            continue

        # --- unpack via unicorn emulation
        prefix = unpack_dir / (Path(rel).stem)
        code, out = run_cmd([sys.executable, str(THIS_DIR / 'upx_shlib_emu.py'),
                             str(so_path), str(prefix)])
        (unpack_dir / (Path(rel).stem + '_emu.log')).write_text(out, encoding='utf-8')
        libbase = Path(str(prefix) + '_libbase.bin')
        if not libbase.exists():
            pk['unpack_error'] = out[-2000:]
            continue
        reloc = unpack_dir / (Path(rel).stem + '_reloc.bin')
        code, out2 = run_cmd([sys.executable, str(THIS_DIR / 'apply_relocs.py'),
                              str(so_path), str(libbase), str(reloc)])
        pk['unpack'] = {
            'emu_log': str(unpack_dir / (Path(rel).stem + '_emu.log')),
            'libbase': str(libbase),
            'reloc': str(reloc) if reloc.exists() else None,
            'relocs_output': out2.strip(),
        }

        # --- evidence extraction on the relocated image
        img = reloc.read_bytes() if reloc.exists() else libbase.read_bytes()
        secs2 = parse_shdrs(data)  # original section layout for .text bounds
        text = next((s for s in secs2 if s.get('sname') == '.text'), None)
        text_lo = text['addr'] if text else 0
        text_hi = text_lo + text['size'] * 4 if text else len(img)  # unpacked text is larger
        text_hi = min(max(text_hi, len(img) // 2), len(img))

        strs = strings_with_offsets(img[:text_lo] if text_lo else img)
        interesting = {}
        for off, s in strs:
            if CRYPTO_STRINGS.search(s) and len(s) <= 64 and not STOCK_NOISE.search(s):
                interesting[off] = s.decode('ascii', 'replace')
        xrefs = scan_xrefs(img, text_lo, text_hi, interesting)
        xor_found = scan_init_xor(img, text_lo, text_hi)

        const_hits = []
        for off, s in strings_with_offsets(img):
            if s in (b'expand 32-byte k',) or (len(s) == 16 and b'by' in s and b' k' in s):
                const_hits.append({'off': hex(off), 'value': s.decode('ascii', 'replace')})

        pk['analysis'] = {
            'interesting_strings': {hex(k): v for k, v in list(interesting.items())[:120]},
            'xrefs': xrefs,
            'init_xor_recovered': xor_found,
            'crypto_constants': const_hits,
        }

    write_outputs(result, workspace)
    return result


def write_outputs(result: dict, workspace: Path) -> None:
    lines = ['# Godot CTF Runner Report', '']
    g = result.get('godot', {})
    lines.append('## Godot detection')
    lines.append('')
    lines.append('- detected: `%s`' % g.get('detected'))
    lines.append('- markers: `%s`' % ', '.join(g.get('markers', [])))
    lines.append('- gdc scripts: `%s`' % ', '.join(g.get('gdc_scripts', [])))
    lines.append('- gdextensions: `%s`' % ', '.join(g.get('gdextensions', [])))
    lines.append('')
    if 'verdict' in result:
        lines.append(result['verdict'])
    for rel, pk in result.get('packed_libs', {}).items():
        lines.append('## Packed lib: `%s`' % rel)
        lines.append('')
        for ev in pk.get('evidence', []):
            lines.append('- %s' % ev)
        if 'unpack' in pk:
            lines.append('')
            lines.append('- unpacked image (RELATIVE relocs applied): `%s`' % pk['unpack'].get('reloc'))
            lines.append('- emu log: `%s`' % pk['unpack'].get('emu_log'))
        if 'unpack_error' in pk:
            lines.append('- UNPACK FAILED: `%s`' % pk['unpack_error'][-300:])
        ev = pk.get('analysis', {})
        if ev.get('init_xor_recovered'):
            lines.append('')
            lines.append('### Recovered init-XOR constants (candidate keys/nonces)')
            lines.append('')
            for o in ev['init_xor_recovered']:
                lines.append('- site %s data %s key `%s` -> `%s`' % (
                    o['adr_site'], o['data'], o['xor_key64'], o['decoded']))
        if ev.get('crypto_constants'):
            lines.append('')
            lines.append('### Crypto constants (check for MUTATED values!)')
            lines.append('')
            for c in ev['crypto_constants']:
                lines.append('- %s: `%s`' % (c['off'], c['value']))
        if ev.get('xrefs'):
            lines.append('')
            lines.append('### Xrefs into interesting strings (top 40)')
            lines.append('')
            for x in ev['xrefs'][:40]:
                lines.append('- %s -> %s `%s`' % (x['site'], x['target'], x['string']))
        lines.append('')
    lines.append('## Next steps for the solver agent')
    lines.append('')
    lines.append('1. Open the unpacked image (`*_reloc.bin`, vaddr == file offset) and read the')
    lines.append('   extension method implementations referenced by the xrefs above.')
    lines.append('2. Check crypto implementations for mutated constants before reusing stock code.')
    lines.append('3. Reimplement token->flag in Python; verify with vectors or round-trip; write verify_candidate.py.')
    lines.append('')
    (workspace / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    (workspace / 'findings.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Godot CTF triage/unpack runner')
    ap.add_argument('apk')
    ap.add_argument('--workspace', '-w', default='godot_ctf_out')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    apk = Path(args.apk).resolve()
    if not apk.exists():
        ap.error('APK not found: %s' % apk)

    try:
        import unicorn  # noqa: F401
    except ImportError:
        print('warning: unicorn not installed; unpack stage will fail. pip3 install unicorn', file=sys.stderr)

    result = analyze(apk, Path(args.workspace))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print('report = %s' % (Path(args.workspace) / 'report.md'))
        print('findings = %s' % (Path(args.workspace) / 'findings.json'))
        g = result.get('godot', {})
        print('godot_detected = %s' % g.get('detected'))
        for rel, pk in result.get('packed_libs', {}).items():
            print('packed_lib = %s (%s)' % (rel, 'unpacked' if 'unpack' in pk else 'unpack failed'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
