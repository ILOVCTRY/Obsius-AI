"""skill ↔ kb 路径护栏（skill-kb-path-guard，2026-10-14）。

背景：`packs/kb/skills/<skill>/references/` 曾是 kb 树的**字节级副本**
（实测 127 个文件与 kb 正本 sha256 相同，且 xxe/jndi/el-injection 已出现内容漂移）。
而 `doctor._broken_kb_refs` 只认「以快照名/域名打头」的引号路径，SKILL.md 表里的
`references/...` 与裸文件名**完全逃逸校验**——副本失效没有任何护栏。

本文件把三件事钉成测试：
1. SKILL.md 内所有路径引用**必须可解析**（不得断链）。
2. SKILL.md 引用一律 kb module 口径（`MIGRATED=True` 后强制）。
3. skill `references/` 不得再出现与 kb 正本字节相同的副本（防副本回潮）。

抽取口径须与 `scripts/migrate_skill_kb_paths.py` 的 `extract_refs` 保持一致：
反引号与双引号两种包裹都算（`kb_open(module="…")` 是双引号），扩展名覆盖
md/py/yaml/yml/json/txt；纯正文提及（`robots.txt`、扩展名清单）按噪声过滤掉。
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

import pytest

PACKS = Path(__file__).resolve().parents[1] / "packs"
KB = PACKS / "kb"
SKILLS = KB / "skills"

# 迁移完成（2026-10-14）：严格口径与防副本回潮的断言随即生效。
MIGRATED = True

_EXT = r"(?:md|py|yaml|yml|json|txt)"
_INDEX_EXT = {".md", ".py", ".yaml", ".yml", ".json", ".txt"}
# 反引号 `` `a/b.md` `` 或双引号 `"a/b.md"`；以及 wikilink 形态 [[a/b/c|别名]]
_REF_RE = re.compile(r"`([^`\n]+?\.%s)`|\"([^\"\n]+?\.%s)\"" % (_EXT, _EXT))
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def _refs_of(skill_md: Path) -> set[str]:
    text = skill_md.read_text(encoding="utf-8")
    out: list[str] = []
    for m in _REF_RE.finditer(text):
        ref = m.group(1) or m.group(2)
        if "://" in ref or " " in ref or ref.startswith("mcp__"):
            continue
        out.append(ref)
    for m in _WIKILINK_RE.finditer(text):
        raw = m.group(1).strip()
        out.append(raw if raw.endswith(".md") else raw + ".md")
    # 噪声过滤：扩展名清单（`.py/.sage/.txt`）与正文里的文件名提及（robots.txt）
    return {
        r for r in out
        if not r.startswith(".")
        and ("/" in r or Path(r).name in _kb_basenames())
    }


def _skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS.glob("*/SKILL.md"))


@lru_cache(maxsize=1)
def _kb_basenames() -> dict[str, list[str]]:
    """kb 树（排除 skills/.history）的文件名 → [module 路径…]，供裸名 legacy 解析。"""
    idx: dict[str, list[str]] = {}
    for f in KB.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _INDEX_EXT:
            continue
        parts = f.relative_to(KB).parts
        if parts[0] == "skills" or ".history" in parts:
            continue
        idx.setdefault(f.name, []).append("/".join(parts))
    return idx


def _resolve(skill_md: Path, ref: str) -> str:
    """返回 'kb' | 'skill' | 'references' | 'bare' | 'dead'。

    'bare' = 裸文件名/相对名，kb 树里存在同名文件但路径不是本 ref 本身——
    上游 skill 的「弹药列」写法（如 `sqli.md`、`14-ssti.md`），迁移时须补全为
    kb module 口径。
    """
    ref = ref.lstrip("/")
    if (KB / ref).is_file():
        return "kb"
    if (skill_md.parent / ref).is_file():
        return "skill"
    if (skill_md.parent / "references" / ref).is_file():
        return "references"
    if Path(ref).name in _kb_basenames():
        return "bare"
    return "dead"


def test_skill_refs_resolve():
    """SKILL.md 内每个路径引用都必须能解析（三口径任一），不得断链。"""
    dead: list[str] = []
    for sk in _skill_dirs():
        for ref in sorted(_refs_of(sk)):
            if _resolve(sk, ref) == "dead":
                dead.append(f"{sk.parent.name}: `{ref}`")
    assert not dead, "SKILL.md 存在无法解析的路径引用：\n  " + "\n  ".join(dead)


@pytest.mark.skipif(not MIGRATED, reason="skill references 迁移未完成")
def test_skill_refs_are_kb_modules():
    """迁移后：SKILL.md 引用一律 kb module 口径（不得再指向 skill 本地副本）。"""
    bad: list[str] = []
    for sk in _skill_dirs():
        for ref in sorted(_refs_of(sk)):
            if _resolve(sk, ref) != "kb":
                bad.append(f"{sk.parent.name}: `{ref}`")
    assert not bad, "以下引用未走 kb module 口径：\n  " + "\n  ".join(bad)


@pytest.mark.skipif(not MIGRATED, reason="skill references 迁移未完成")
def test_no_duplicate_skill_copies():
    """迁移后：skill references/ 不得存在与 kb 正本字节相同的副本（防漂移回潮）。"""
    kb_hashes: set[str] = set()
    for f in KB.rglob("*"):
        if not f.is_file():
            continue
        parts = f.relative_to(KB).parts
        if parts[0] == "skills" or ".history" in parts:
            continue
        kb_hashes.add(hashlib.sha256(f.read_bytes()).hexdigest())

    dupes: list[str] = []
    for sk in _skill_dirs():
        refs = sk.parent / "references"
        if not refs.is_dir():
            continue
        for f in refs.rglob("*"):
            if f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() in kb_hashes:
                dupes.append(f"{sk.parent.name}/{f.relative_to(sk.parent).as_posix()}")
    assert not dupes, "skill references 仍与 kb 正本字节相同（应删副本走引用）：\n  " + "\n  ".join(dupes)
