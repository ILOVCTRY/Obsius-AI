#!/usr/bin/env python
"""手册.md 可读化改名（一次性迁移，2026-10-20）。

54 个 `漏洞分类/<类目>/手册.md` 全部同名，人类在编辑器/文件树里分不清，
Obsidian 里裸名 wikilink 也天然歧义（refs.py 因此明确拒解析裸名）。

改法：用 frontmatter 的 `title` 做文件名，剥掉「测试手册」「手册」后缀，
得到 `越权漏洞（IDOR权限绕过）.md` 这类自解释名字。无 title 时退回目录名。

只改 `漏洞分类/<类目>/手册.md` 这一形态；`信息收集/methodology/手册.md`
等其它位置的同名文件不在本次范围（用户明确说的是漏洞分类手册）。

用法：
    python scripts/rename_handbooks.py --dry-run
    python scripts/rename_handbooks.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VULN_ROOT = ROOT / "packs" / "kb" / "web" / "漏洞分类"

# 文件名里不能出现的字符（Windows 最严：\ : * ? " < > |）
# 斜杠单独处理成连接符，不直接删（否则 401/403 会粘成 401403）
_BAD_CHARS = re.compile(r'[\\:*?"<>|\r\n\t]')
# 「手册」「测试手册」是泛称，无论出现在结尾还是括号前都剥掉
_TAIL = re.compile(r"(测试手册|手册)")
_WS = re.compile(r"[ \u3000]{2,}")


def _read_title(p: Path) -> str:
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")


def _safe_name(raw: str, fallback: str) -> str:
    """title → 文件名主干。

    规则：
    - 斜杠 `/`（「或」语义）换成 `-`，保留分隔信息；`401/403` → `401-403`
    - 剥掉「手册」「测试手册」这类零信息量泛称
    - 剥完若只剩目录名（如 SSRF 测试手册 → SSRF），补「测试」区分同目录文件
    """
    s = (raw or "").strip()
    # HTTP/2、HTTP/3 是版本号不是「或」，先护住
    s = re.sub(r"HTTP\s*/\s*(\d)", r"HTTP\g<1>", s)
    s = s.replace("/", "-").replace("\\", "-")
    s = _BAD_CHARS.sub("", s)
    s = _TAIL.sub("", s)
    # 清理剥词后留下的空括号、连续空格、悬挂连接符
    s = re.sub(r"[（(]\s*[）)]", "", s)
    s = _WS.sub(" ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = s.strip(" -—·")
    if not s:
        s = _BAD_CHARS.sub("", fallback)
    # 与目录同名时补「测试」，否则文件名相对目录名零增量
    if s.replace(" ", "") == fallback.replace(" ", ""):
        s = f"{s}测试"
    return s or "手册"

SCAN_DIRS = ("packs", "core", "tests")
SCAN_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".ts", ".tsx"}
SKIP_PARTS = {".history", "node_modules", "__pycache__", "dist", "scripts",
              ".tmp_pytest"}


def _plan() -> list[tuple[Path, Path]]:
    out: list[tuple[Path, Path]] = []
    for src in sorted(VULN_ROOT.glob("*/手册.md")):
        if ".history" in src.parts:
            continue
        folder = src.parent.name
        title = _read_title(src)
        stem = _safe_name(title, folder)
        dst = src.with_name(stem + ".md")
        if dst == src:
            continue
        out.append((src, dst))
    return out


def _iter_targets() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
                continue
            if SKIP_PARTS & set(p.parts):
                continue
            out.append(p)
    return out


def _rewrite_refs(pairs: list[tuple[Path, Path]]) -> list[tuple[Path, int]]:
    """把 `漏洞分类/<类目>/手册[.md]` 全量替换成新文件名。

    长路径优先，避免 `A/手册.md` 被 `B/A/手册.md` 的前缀吃掉。
    """
    hits: list[tuple[Path, int]] = []
    rules: list[tuple[re.Pattern, str]] = []
    for src, dst in pairs:
        folder = src.parent.name
        new_stem = dst.stem
        # 带扩展名：`漏洞分类/<folder>/手册.md`
        rules.append((
            re.compile(rf"(漏洞分类[/\\]{re.escape(folder)}[/\\])手册\.md"),
            rf"\g<1>{new_stem}.md",
        ))
        # 不带扩展名（wikilink 省略 .md）：`漏洞分类/<folder>/手册` 后接 | ] # 或行尾
        rules.append((
            re.compile(
                rf"(漏洞分类[/\\]{re.escape(folder)}[/\\])手册(?=[\]|#\s）)，。]|$)"),
            rf"\g<1>{new_stem}",
        ))
    for p in _iter_targets():
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = text
        for pat, repl in rules:
            new = pat.sub(repl, new)
        if new != text:
            n = sum(len(pat.findall(text)) for pat, _ in rules)
            hits.append((p, n))
            p.write_text(new, encoding="utf-8")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    pairs = _plan()
    print(f"待改名: {len(pairs)} 个手册.md")
    print()
    for src, dst in pairs:
        print(f"  {src.parent.name:16s} 手册.md  ->  {dst.name}")

    if args.dry_run:
        print("\n[dry-run] 未做任何修改。")
        return 0

    hits = _rewrite_refs(pairs)
    print(f"\n[1/2] 已改写 {len(hits)} 个引用文件")
    for p, n in sorted(hits, key=lambda t: -t[1])[:10]:
        print(f"  {n:4d}  {p.relative_to(ROOT)}")

    done = 0
    for src, dst in pairs:
        if dst.exists():
            print(f"  !! 目标已存在，跳过: {dst.name}")
            continue
        src.rename(dst)
        done += 1
    print(f"[2/2] 已改名 {done} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
