# -*- coding: utf-8 -*-
"""临时：WP 分档（缺口词计数）。用完即删。"""
import re
import sys
from pathlib import Path

KB = Path('packs/kb')
DOMS = ['web', 'binary', 'crypto', 'misc', 'forensics', 'cloud', 'pentest']


def load_main():
    buf = []
    for dom in DOMS:
        d = KB / dom
        if not d.exists():
            continue
        for p in d.rglob('*.md'):
            if '赛事专题' in p.parts:
                continue
            buf.append(p.read_text(encoding='utf-8', errors='ignore'))
    return '\n'.join(buf)


def load_judge():
    buf = []
    for p in KB.rglob('*.md'):
        if '判据库' in p.parts:
            buf.append(p.read_text(encoding='utf-8', errors='ignore'))
    return '\n'.join(buf)


def tokens(s):
    out = set()
    for m in re.finditer(r'`([^`\n]{2,60})`', s):
        t = m.group(1).strip()
        if re.fullmatch(r'[\w\-./:]{2,60}', t):
            out.add(t)
    return out


def main(domain):
    main_txt = (load_main() + load_judge()).lower()
    d = KB / domain / '赛事专题' / 'Writeup合集'
    rows = []
    for p in sorted(d.glob('*.md')):
        s = p.read_text(encoding='utf-8', errors='ignore')
        toks = tokens(s)
        miss = [t for t in toks if t.lower() not in main_txt]
        rows.append((len(miss), len(toks), p.name, miss))
    rows.sort()
    for n, tot, name, miss in rows:
        print(f'[缺{n:3d}/{tot:3d}] {name}')
        if n:
            print('        ' + ', '.join(miss[:14]))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'misc')
