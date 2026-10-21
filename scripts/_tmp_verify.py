# -*- coding: utf-8 -*-
"""临时：核对指定 WP 的独有词是否已进主库。用完即删。"""
import re
import sys
from pathlib import Path

KB = Path('packs/kb')
DOMS = ['web', 'binary', 'crypto', 'misc', 'forensics', 'cloud', 'pentest']


def main_text():
    buf = []
    for dom in DOMS:
        d = KB / dom
        if not d.exists():
            continue
        for p in d.rglob('*.md'):
            if '赛事专题' in p.parts:
                continue
            buf.append(p.read_text(encoding='utf-8', errors='ignore'))
    return '\n'.join(buf).lower()


def toks(s):
    out = set()
    for m in re.finditer(r'`([^`\n]{3,50})`', s):
        t = m.group(1).strip()
        if re.fullmatch(r'[\w\-./:]{3,50}', t):
            out.add(t)
    return out


def main(pattern):
    main = main_text()
    d = KB / 'misc' / '赛事专题' / 'Writeup合集'
    for p in sorted(d.glob('*.md')):
        if pattern not in p.name:
            continue
        s = p.read_text(encoding='utf-8', errors='ignore')
        ts = toks(s)
        miss = sorted(t for t in ts if t.lower() not in main)
        print(f'== {p.name}')
        print(f'   tokens {len(ts)} / missing {len(miss)}')
        print('   miss: ' + ', '.join(miss[:20]))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '')
