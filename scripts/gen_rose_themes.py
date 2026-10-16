# -*- coding: utf-8 -*-
"""生成 rose-dark / rose-light 主题块，插入 webui/src/index.css。

派生规则（对齐 warm-dark 的派生方式）：
- rose-dark  <- cyber-dark：蓝中性 hue(245/250/220/235/230/210/205) 与 brand(190/195) -> 350；
  info 240->265；success/finding(145/155/165)->170（薄荷）；danger/warning/sev 保持语义色；
  viz 的 hex 值按 warm-dark 同款 oklch 等价物色相平移（hue 350）。
- rose-light <- light：brand(190/195)->355 并加深；蓝中性（含小数 hue）->350；
  info 240->265；success 族 ->170；viz-chain 紫 ->345。
最后按语义覆盖若干主 token。只插块，不改动现有内容。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "webui" / "src" / "index.css"

DARK_HUE = {190: "350", 195: "350", 250: "350", 245: "350", 220: "350",
            235: "350", 230: "350", 210: "350", 205: "350",
            240: "265", 145: "170", 155: "170", 165: "170"}
LIGHT_HUE = {190: "355", 195: "355", 250: "350", 245: "350", 220: "350",
             235: "350", 230: "350", 210: "350", 205: "350",
             240: "265", 145: "170", 155: "170", 165: "170",
             301.686: "345", 211.53: "350", 202.145: "350", 238.025: "350",
             252.31: "350", 259.263: "350", 256.816: "350", 256.806: "350",
             258.357: "350", 256.798: "350", 254.723: "350", 256.331: "350",
             259.668: "350", 258.74: "350", 247.992: "350", 250.922: "350"}

# cyber-dark viz hex -> warm-dark 同款 oklch 等价物（色相改 350）
DARK_HEX = {
    "#bc8cff": "oklch(0.75 0.17 350)",
    "#30363d": "oklch(0.33 0.015 350)",
    "#101722": "oklch(0.203 0.008 350)",
    "#9fe6c8": "oklch(0.85 0.11 170)",
    "#161b22": "oklch(0.22 0.016 350)",
    "#1c2128": "oklch(0.246 0.015 350)",
    "#0d1117": "oklch(0.176 0.014 350)",
    "#21262d": "oklch(0.267 0.015 350)",
    "#484f58": "oklch(0.425 0.017 350)",
    "#6e7681": "oklch(0.563 0.02 350)",
    "#1f6feb": "oklch(var(--brand-lch))",
    "#0f1c30": "oklch(0.226 0.008 350)",
    "#22d3ee": "oklch(0.72 0.16 350)",
    "#39c5cf": "oklch(0.755 0.116 350)",
    "#f0883e": "oklch(0.727 0.153 52.833)",
    "#d7e1e8": "oklch(0.904 0.014 350)",
    "#c9d1d9": "oklch(0.857 0.014 350)",
    "#8b949e": "oklch(0.662 0.018 350)",
}

# 语义覆盖（色相映射之后整体替换）
DARK_OVERRIDE = {
    "--brand-lch": "0.72 0.16 350",
    "--brand-soft-lch": "0.63 0.15 350",
    "--brand-dim-lch": "0.55 0.13 350",
    "--danger-lch": "0.63 0.19 25",
    "--warning-lch": "0.8 0.14 75",
    "--success-lch": "0.72 0.14 170",
    "--info-lch": "0.75 0.09 265",
    "--surface-0-lch": "0.11 0.014 350",
    "--surface-1-lch": "0.14 0.015 350",
    "--surface-2-lch": "0.195 0.017 350",
    "--surface-3-lch": "0.245 0.02 350",
    "--surface-raised-lch": "0.2 0.016 350",
    "--surface-border-lch": "0.3 0.02 350",
    "--text-lch": "0.94 0.012 350",
    "--text-muted-lch": "0.64 0.018 350",
    "--viz-sev-info": "oklch(0.58 0.02 350)",
    "--viz-node-deadend": "oklch(0.58 0.02 350)",
    "--viz-sev-info-bg": "oklch(0.58 0.02 350 / 0.13)",
    "--viz-chain": "oklch(0.75 0.17 350)",
    "--viz-chain-active": "oklch(0.72 0.16 350)",
    "--viz-chain-glow": "oklch(0.13 0.12 350 / 0.25)",
    "--viz-code": "oklch(0.85 0.11 170)",
    "--viz-accent-cyan": "oklch(0.755 0.116 350)",
    "--viz-target": "oklch(var(--brand-lch))",
    "--primary-foreground": "oklch(0.17 0.03 350)",
    "--accent": "oklch(0.32 0.04 350)",
}
LIGHT_OVERRIDE = {
    "--brand-lch": "0.56 0.19 355",
    "--brand-soft-lch": "0.62 0.15 355",
    "--brand-dim-lch": "0.7 0.11 355",
    "--danger-lch": "0.52 0.19 25",
    "--warning-lch": "0.55 0.14 75",
    "--success-lch": "0.5 0.13 170",
    "--info-lch": "0.5 0.11 265",
    "--surface-0-lch": "0.965 0.008 350",
    "--surface-1-lch": "0.975 0.006 350",
    "--surface-2-lch": "1 0 350",
    "--surface-3-lch": "0.945 0.008 350",
    "--surface-raised-lch": "1 0 350",
    "--surface-border-lch": "0.9 0.016 350",
    "--text-lch": "0.28 0.03 350",
    "--text-muted-lch": "0.5 0.03 350",
    "--viz-chain": "oklch(0.56 0.17 345)",
    "--accent": "oklch(0.93 0.025 355)",
}


def extract(name: str) -> str:
    m = re.search(r'\[data-theme="%s"\] \{\n(.*?)\n\}\n' % name, CSS.read_text(encoding="utf-8"), re.S)
    if not m:
        sys.exit(f"block {name} not found")
    return m.group(1)


def remap_hue(body: str, table: dict) -> str:
    def ok(m):
        l, c, h = m.group(1), m.group(2), m.group(3)
        key = float(h)
        if key in table:
            h = table[key]
        elif key in LIGHT_HUE and 200 <= key <= 260:   # light 的小数蓝中性
            h = "350"
        return f"oklch({l} {c} {h}"
    return re.sub(r'oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)', ok, body)


def apply(body: str, hexmap: dict, override: dict) -> str:
    for old, new in hexmap.items():
        body = body.replace(old, new)
    out = []
    for line in body.split("\n"):
        m = re.match(r'  (--[\w-]+): (.+);$', line)
        if m and m.group(1) in override:
            line = f"  {m.group(1)}: {override[m.group(1)]};"
        out.append(line)
    return "\n".join(out)


dark = apply(remap_hue(extract("cyber-dark"), DARK_HUE), DARK_HEX, DARK_OVERRIDE)
light = apply(remap_hue(extract("light"), LIGHT_HUE), {}, LIGHT_OVERRIDE)

new_block = (
    "\n"
    "/* ---------- 粉色主题（2026-10-15）：绯樱 rose-dark / 蔷薇 rose-light。\n"
    "   由 cyber-dark / light 派生（色相映射），语义 token 口径与既有主题一致。 ---------- */\n"
    '[data-theme="rose-dark"] {\n'
    '  color-scheme: dark;\n'
    f"{dark}\n"
    "}\n\n"
    '[data-theme="rose-light"] {\n'
    '  color-scheme: light;\n'
    f"{light}\n"
    "}\n"
)

css = CSS.read_text(encoding="utf-8")
anchor = '[data-theme="warm-dark"] {'
idx = css.index(anchor)
end = css.index("\n}\n", idx) + len("\n}\n")
assert 'data-theme="rose-dark"' not in css, "already inserted"
CSS.write_text(css[:end] + new_block + css[end:], encoding="utf-8")

# 自检：数一下两个新块的 token 数
m = re.search(r'\[data-theme="rose-dark"\] \{\n(.*?)\n\}\n', CSS.read_text(encoding="utf-8"), re.S)
n_dark = len(re.findall(r'  --[\w-]+:', m.group(1)))
m = re.search(r'\[data-theme="rose-light"\] \{\n(.*?)\n\}\n', CSS.read_text(encoding="utf-8"), re.S)
n_light = len(re.findall(r'  --[\w-]+:', m.group(1)))
print(f"inserted. rose-dark tokens={n_dark} rose-light tokens={n_light}")
