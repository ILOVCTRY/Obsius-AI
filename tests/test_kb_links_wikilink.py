"""kb 内链 wikilink 化（scripts/kb_links_to_wikilinks.py）的契约与存量护栏。

背景（2026-10-14）：`packs/kb` 是 Obsidian vault，`newLinkFormat=absolute` +
wikilink 模式。存量 kb 文档用的是 md 相对链接（`x.md` / `../a/b.md`），已经一次性
转成全路径 wikilink（`[[web/漏洞分类/sqli/手册|手册]]`）。本测试钉住两件事：

1. 转换器行为：只转能解析的、不碰代码围栏/行内代码/图片/外链、锚点与别名保留、
   旧布局技能链接确定性回填、幂等；
2. 存量护栏：kb 文档里不得再残留**可解析**的 md 相对链接（断了的不算——那是
   需人工的历史坏账，另出报告）；kb 文档里的 wikilink 必须全路径
  （裸名 `[[手册]]` 在 kb 里有 52 个同名，天然歧义，读取层不认）。

注意：`*/题目知识/*` 是 ctf-skills 上游快照原件，与上游仓库保持字节一致，
不在转换范围，也不在本护栏范围内。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.kb_links_to_wikilinks import (  # noqa: E402
    _MD_LINK, _PATHISH, _SKIP_SEGMENTS, convert_text, iter_prose)

REPO = Path(__file__).resolve().parents[1]
KB = REPO / "packs" / "kb"
SKILLS = KB / "skills"

WIKI = re.compile(r"\[\[([^\]|#\n]+?)(?:\|[^\]]*)?\]\]")


def _new(stats=None):
    return stats if stats is not None else {"seen": 0, "converted": 0, "broken": 0}


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    kb = tmp_path / "packs" / "kb"
    (kb / "web" / "实战打法").mkdir(parents=True)
    (kb / "web" / "实战打法" / "SSTI.md").write_text("# SSTI\n", encoding="utf-8")
    (kb / "web" / "实战打法" / "SQL.md").write_text("# SQL\n", encoding="utf-8")
    (kb / "web" / "漏洞分类" / "sqli").mkdir(parents=True)
    (kb / "web" / "漏洞分类" / "sqli" / "手册.md").write_text(
        "# sqli 手册\n\n## 提权\n", encoding="utf-8")
    (kb / "web" / "漏洞分类" / "xxe").mkdir(parents=True)
    (kb / "web" / "漏洞分类" / "xxe" / "手册.md").write_text("# xxe\n", encoding="utf-8")
    (kb / "skills" / "web-strike-entry").mkdir(parents=True)
    (kb / "skills" / "web-strike-entry" / "SKILL.md").write_text(
        "---\nname: web-strike-entry\n---\n# w\n", encoding="utf-8")
    return kb


# ---------- 转换器行为 ----------

def _convert(kb: Path, rel: str, body: str):
    src = kb / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    stats = _new()
    return convert_text(src.read_text(encoding="utf-8"), src, stats, kb)[0]


def test_same_dir_and_parent_links(vault: Path):
    out = _convert(vault, "web/实战打法/JWT.md",
                   "- 关联: [SSTI.md](SSTI.md) — JWT 的 SSTI | [SQL.md](./SQL.md)\n")
    assert out == ("- 关联: [[web/实战打法/SSTI.md|SSTI.md]] — JWT 的 SSTI | "
                   "[[web/实战打法/SQL.md|SQL.md]]\n")


def test_label_with_inline_code_converts(vault: Path):
    """标签本身带行内代码（kb 后渗透手册的导航行）也必须转——不能因遮代码段而漏。"""
    out = _convert(vault, "web/实战打法/JWT.md",
                   "下一篇:[`SQL.md`](SQL.md) 完。\n")
    assert "下一篇:[[web/实战打法/SQL.md|SQL.md]] 完。" in out
    assert "`" not in out


def test_alias_and_anchor_preserved(vault: Path):
    out = _convert(vault, "web/漏洞分类/sqli/手册.md",
                   "见 [xxe 手册](../xxe/手册.md#外部实体)。\n"
                   "回显 [手册](手册.md#提权) 里的清单。\n")
    assert "[[web/漏洞分类/xxe/手册.md#外部实体|xxe 手册]]" in out
    assert "[[web/漏洞分类/sqli/手册.md#提权|手册]]" in out


def test_code_fence_and_inline_code_untouched(vault: Path):
    body = (
        "围栏内：\n"
        "```lua\n"
        "local s = data.utf8 -- [fake](link.md)\n"
        "```\n"
        "围栏外：[真链接](SSTI.md)\n"
        "行内：`[不是链接](not-a-link.md)` 结束\n"
    )
    out = _convert(vault, "web/实战打法/JWT.md", body)
    assert "[fake](link.md)" in out          # 围栏内原样
    assert "[不是链接](not-a-link.md)" in out  # 行内代码原样
    assert "[[web/实战打法/SSTI.md|真链接]]" in out


def test_images_and_external_untouched(vault: Path):
    out = _convert(vault, "web/实战打法/JWT.md",
                   "![图](../xxe/手册.md) 见图。\n"
                   "外链 [官网](https://example.com/a.md) 与 [站内](#sec)。\n")
    assert "![图](../xxe/手册.md)" in out
    assert "[官网](https://example.com/a.md)" in out
    assert "[站内](#sec)" in out


def test_legacy_skill_link_repaired(vault: Path):
    """旧 `../../capabilities/<域>/skills/<名>/SKILL.md` 布局 → vault 内 skills/。"""
    out = _convert(vault, "web/实战打法/JWT.md",
                   "转 [web-strike-entry](../../capabilities/web/skills/"
                   "web-strike-entry/SKILL.md)。\n")
    assert "[[skills/web-strike-entry/SKILL.md|web-strike-entry]]" in out


def test_broken_link_left_alone(vault: Path):
    out = _convert(vault, "web/实战打法/JWT.md",
                   "坏链 [不存在](nope.md) 与旧目录 [x](../WP汇总/x.md)。\n")
    assert "[不存在](nope.md)" in out
    assert "[x](../WP汇总/x.md)" in out


def test_idempotent(vault: Path):
    src = vault / "web" / "实战打法" / "JWT.md"
    body = "- a: [SSTI.md](SSTI.md)\n- b: [手册](../漏洞分类/sqli/手册.md)\n"
    first = _convert(vault, "web/实战打法/JWT.md", body)
    second = convert_text(first, src, _new(), vault)[0]
    assert first == second
    assert "](SSTI.md)" not in first


# ---------- 存量护栏（真仓库） ----------

def _doc_files():
    for p in sorted(KB.rglob("*.md")):
        rel = p.relative_to(KB).as_posix()
        if rel.startswith(".obsidian") or "/.history/" in rel:
            continue
        if any(s in _SKIP_SEGMENTS for s in rel.split("/")):
            continue
        yield p, rel


def test_no_resolvable_md_links_left_in_kb():
    """可解析的 md 链接应全部 wikilink 化；只剩本来就断的历史坏账。"""
    leftovers: list[str] = []
    for p, rel in _doc_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for line in iter_prose(text):
            for m in _MD_LINK.finditer(line):
                target = m.group(2).partition("#")[0]
                if not target or "://" in target:
                    continue
                if not _PATHISH.search(target) \
                        and not (p.parent / target).is_file():
                    continue
                if (p.parent / target).is_file():
                    leftovers.append(f"{rel}: {m.group(0)}")
    assert not leftovers, (
        f"仍有 {len(leftovers)} 条可解析的 md 链接未转 wikilink（前 10）：\n  "
        + "\n  ".join(leftovers[:10]))


def test_kb_wikilinks_are_full_path():
    """kb 文档里的 wikilink 必须全路径；裸名（`[[手册]]`）读取层不认。

    代码围栏/行内代码里的 `[[...]]`（Python 列表、Lua 调用、JSON 数组、正则字符
    类）是噪声不是链接——护栏与转换器同口径，都走 iter_prose + 行内代码剔除。
    另外只在"看起来真是个链接"时判违规：目标有 kb 里的同名文件（kb 有 52 个
    `手册.md`、30 个 `README.md`，裸名天然歧义）。"""
    basenames = {p.name for p in KB.rglob("*")
                 if p.is_file() and ".history" not in p.parts}
    in_code = re.compile(r"`[^`\n]*`")
    bad: list[str] = []
    for p, rel in _doc_files():
        for line in iter_prose(p.read_text(encoding="utf-8", errors="replace")):
            spans = [m.span() for m in in_code.finditer(line)]
            for m in WIKI.finditer(line):
                if any(a <= m.start() and m.end() <= b for a, b in spans):
                    continue
                target = m.group(1).strip()
                if "/" in target:
                    continue
                plausible = (target in basenames
                             or f"{target}.md" in basenames
                             or re.search(r"\.(?:md|py|txt|yaml|yml|json)$", target))
                if plausible:
                    bad.append(f"{rel}: {m.group(0)}")
    assert not bad, (
        f"仍有 {len(bad)} 条可疑裸名 wikilink（前 10）：\n  " + "\n  ".join(bad[:10]))


def test_skill_mds_have_no_md_links():
    """技能正文一律 kb module 口径（见 tests/test_skill_kb_paths.py 的严格断言）。"""
    bad: list[str] = []
    for p in sorted(SKILLS.glob("*/SKILL.md")):
        for m in _MD_LINK.finditer(p.read_text(encoding="utf-8")):
            bad.append(f"{p.parent.name}: {m.group(0)}")
    assert not bad, "技能正文仍含 md 链接：\n  " + "\n  ".join(bad)
