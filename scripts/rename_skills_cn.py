#!/usr/bin/env python
"""技能中文化迁移：目录名 + frontmatter name 同步改中文，并统一命名体系。

为什么必须同时改两处：``core/skills/doctor.py:127`` 有 ``skill-name-mismatch``
**error** 检查（frontmatter name 必须等于目录名）。只改目录不改 name 会让
doctor 体检报错。

命名体系（后缀限定四类，反映「方向-用途」）：
- 入口：判断往哪走，不深挖
- 手法：一类攻击/分析的具体路线
- 审计：白盒/源码视角
- 后渗透：落点之后的动作

用法：``python scripts/rename_skills_cn.py --dry-run`` 先看，再 ``--apply``。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "packs" / "kb" / "skills"

# 旧名 → (新名, 分组目录)。分组目录为空表示留在 skills 根下。
# 分组：01-入口分诊 / 02-方向深挖 / 03-后渗透 / 04-专项
MAPPING: dict[str, tuple[str, str]] = {
    # ---- 01 入口分诊 ----
    "triage":            ("总入口分诊", "01-入口分诊"),
    "file-triage":       ("附件分诊",   "01-入口分诊"),
    # ---- 02 方向深挖 ----
    "web-strike-entry":  ("外网打点入口",   "02-方向深挖"),
    "web-injection":     ("Web注入手法",    "02-方向深挖"),
    "web-authn-session": ("Web认证会话手法", "02-方向深挖"),
    "web-client-side":   ("Web客户端手法",  "02-方向深挖"),
    "web-api-attack":    ("Web接口攻击手法", "02-方向深挖"),
    "web-source-audit":  ("Web源码审计",    "02-方向深挖"),
    "binary-pwn":        ("二进制利用手法", "02-方向深挖"),
    "binary-rev":        ("二进制逆向手法", "02-方向深挖"),
    "binary-diff":       ("二进制版本比对", "02-方向深挖"),
    "crypto-triage":     ("密码学手法",     "02-方向深挖"),
    "cloud-entry":       ("云安全入口",     "02-方向深挖"),
    "recon-asset-enum":  ("资产侦察手法",   "02-方向深挖"),
    # 已手工改过的两个：源目录已是中文名，这里用「已迁移」形态登记，
    # 让脚本第二次运行时能把它们也归入分组目录。
    "取证与隐写": ("取证与隐写", "02-方向深挖"),
    "杂项手法":   ("杂项手法",   "02-方向深挖"),
    # ---- 04 专项（按平台/技术细分，非阶段） ----
    "android-rev":       ("安卓逆向手法",   "04-专项"),
    "dotnet-rev":        ("NET逆向手法",    "04-专项"),
    "edr-hook-rev":      ("EDR对抗手法",    "04-专项"),
    "blueprint-rebuild": ("逆向重建手法",   "04-专项"),
    # ---- 03 后渗透 ----
    "web-post-exp":      ("Web后渗透",      "03-后渗透"),
    "rt-intranet-recon": ("内网侦察",       "03-后渗透"),
    "rt-lateral-move":   ("横向移动",       "03-后渗透"),
    "rt-privesc":        ("权限提升",       "03-后渗透"),
}


def _rewrite_name(skill_md: Path, new_name: str) -> str | None:
    """把 frontmatter 的 name: 改成新名，返回旧名（无变化返回 None）。"""
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    head = m.group(1)
    nm = re.search(r"^name:\s*(.+)$", head, re.M)
    if not nm:
        return None
    old = nm.group(1).strip().strip("'\"")
    if old == new_name:
        return None
    head2 = re.sub(r"^name:\s*.+$", f"name: {new_name}", head, count=1, flags=re.M)
    skill_md.write_text(f"---\n{head2}\n---\n" + text[m.end():], encoding="utf-8")
    return old


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    args = ap.parse_args()

    moved = 0
    skipped = []
    for old, (new, group) in MAPPING.items():
        src = SKILLS / old
        dst = (SKILLS / group / new) if group else (SKILLS / new)
        # 幂等：目标已存在且源不存在 → 已迁移过
        if dst.exists() and not src.exists():
            skipped.append(f"{old} → {new}（已迁移）")
            continue
        if not src.exists():
            skipped.append(f"{old}（源不存在）")
            continue
        md = src / "SKILL.md"
        if not md.is_file():
            skipped.append(f"{old}（无 SKILL.md）")
            continue
        print(f"{'APPLY' if args.apply else 'DRY  '} {old:22} → {group + '/' if group else ''}{new}")
        if not args.apply:
            moved += 1
            continue
        if group:
            (SKILLS / group).mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        old_name = _rewrite_name(dst / "SKILL.md", new)
        moved += 1
        if old_name and old_name != new:
            print(f"       frontmatter name: {old_name} → {new}")

    print(f"\n{'迁移' if args.apply else '将迁移'} {moved} 个技能")
    if skipped:
        print("跳过：")
        for s in skipped:
            print("  -", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
