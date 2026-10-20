#!/usr/bin/env python
"""技能名中文化后同步全部交叉引用（专家 yaml / 技能互引 / kb 文档 / 前端）。

背景：24 个技能改中文名后，旧 ASCII 名在 66 个文件里仍有残留引用。
``expert_skills()`` 按名字硬选，专家 yaml 里的旧名会导致技能匹配不上
（`skills: [web-strike-entry]` → 白名单命中不到 `外网打点入口`，专家变哑巴）。

替换策略：
- **长名优先**（`web-authn-session` 先于 `web-injection`），避免前缀截断；
- 词边界：ASCII 名前后不能紧邻 `[A-Za-z0-9_-]`，防止 `binary-rev` 命中
  `binary-rev-x` 这类不存在的名字；
- 跳过 `.history` 与 `packs/kb/skills/**` 之外无关目录。

用法：``python scripts/sync_skill_refs.py --dry-run`` / ``--apply``
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 旧 ASCII 名 → 新中文名（与 rename_skills_cn.py 的 MAPPING 保持一致）
RENAME = {
    "triage": "总入口分诊",
    "file-triage": "附件分诊",
    "web-strike-entry": "外网打点入口",
    "web-injection": "Web注入手法",
    "web-authn-session": "Web认证会话手法",
    "web-client-side": "Web客户端手法",
    "web-api-attack": "Web接口攻击手法",
    "web-source-audit": "Web源码审计",
    "binary-pwn": "二进制利用手法",
    "binary-rev": "二进制逆向手法",
    "binary-diff": "二进制版本比对",
    "crypto-triage": "密码学手法",
    "cloud-entry": "云安全入口",
    "recon-asset-enum": "资产侦察手法",
    "forensics-triage": "取证与隐写",
    "misc-triage": "杂项手法",
    "android-rev": "安卓逆向手法",
    "dotnet-rev": "NET逆向手法",
    "edr-hook-rev": "EDR对抗手法",
    "blueprint-rebuild": "逆向重建手法",
    "web-post-exp": "Web后渗透",
    "rt-intranet-recon": "内网侦察",
    "rt-lateral-move": "横向移动",
    "rt-privesc": "权限提升",
}

# 长名优先，防止 web-injection 抢先吃掉更长的名字
_ORDERED = sorted(RENAME.items(), key=lambda kv: -len(kv[0]))

# 不扫描：备份区、依赖、构建产物
_SKIP_PARTS = {".history", "node_modules", "dist", "__pycache__", ".git"}
# 目标后缀
_EXTS = {".py", ".yaml", ".yml", ".md", ".tsx", ".ts", ".json"}

# 这些词同时也是**业务标识符**（API 路径 / 变量名 / 任务类型），不能盲目替换。
# 实测：`/binaries/<sha>/triage` 是二进制分诊接口，`triageProgress` 是它的前端状态；
# `task_types: [triage]` 是任务类型枚举。全替换会把功能改坏。
# 处理方式：这些词只在**明确的技能上下文**里替换（见 _CONTEXT_PATTERNS）。
_AMBIGUOUS = {"triage"}

# 明确的技能引用上下文（只有这些形态才替换 triage）
_CONTEXT_PATTERNS = [
    # 专家 yaml 的 skills 列表：skills: [..., triage, ...] / - triage
    re.compile(r"(skills:\s*\[[^\]]*?)(?<![A-Za-z0-9_-])triage(?![A-Za-z0-9_-])"),
    re.compile(r"(^\s*-\s*)triage(\s*$)", re.M),
    # 技能互引：`skill_open(name="triage")` / 反引号包裹的 `triage`
    re.compile(r"(skill_open\(name=[\"'])triage([\"'])"),
    re.compile(r"(`)triage(`)"),
]


def _pattern(old: str) -> re.Pattern[str]:
    """词边界匹配：前后不能是 ASCII 标识符字符。"""
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(old) + r"(?![A-Za-z0-9_-])")


def _targets() -> list[Path]:
    out: list[Path] = []
    # 注意：不扫 scripts/ —— 本脚本与 rename_skills_cn.py 的 RENAME/MAPPING 表里
    # 必须以「旧 ASCII 名」为键，被替换就会自毁映射。
    for base in ("packs", "core", "webui/src"):
        d = ROOT / base
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if not p.is_file() or p.suffix not in _EXTS:
                continue
            if _SKIP_PARTS & set(p.parts):
                continue
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    total_files = 0
    total_hits = 0
    detail: list[tuple[Path, str, int]] = []
    for path in _targets():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        orig = text
        hits = 0
        for old, new in _ORDERED:
            if old in _AMBIGUOUS:
                # 只替换明确的技能引用形态，业务标识符（API 路径/变量/任务类型）不动
                n = 0
                for pat in _CONTEXT_PATTERNS:
                    def _sub(m: re.Match[str], _new: str = new) -> str:
                        return m.group(0).replace(old, _new, 1)
                    text, k = pat.subn(_sub, text)
                    n += k
                if n:
                    hits += n
                    detail.append((path.relative_to(ROOT), old, n))
                continue
            pat = _pattern(old)
            n = len(pat.findall(text))
            if n:
                text = pat.sub(new, text)
                hits += n
                detail.append((path.relative_to(ROOT), old, n))
        if text != orig:
            total_files += 1
            total_hits += hits
            if args.apply:
                path.write_text(text, encoding="utf-8")

    for rel, old, n in detail:
        print(f"  {str(rel):70} {old:22} x{n}")
    print(f"\n{'已更新' if args.apply else '将更新'} {total_files} 个文件，{total_hits} 处引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
