"""kb 引用的 wikilink 口径（refs.normalize_module / extract_modules / find_module_refs）。

背景（2026-10-14）：`packs/kb` 已注册为 Obsidian vault，用户把 `newLinkFormat`
改成了绝对路径 + wikilink 模式，本仓 md 之间的引用今后会以
`[[web/漏洞分类/sqli/手册|手册]]` / `[[web/…/手册]]` 形态落盘。读取层
（kb_open / doctor / 改名联动）必须认得这种形态，否则新写的链接对 Agent 是死链。

决策：**只认全路径 wikilink**。kb 里有 52 个 `手册.md`、30 个 `README.md`，
裸名 wikilink 天然歧义，一律不解析（也不报错）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.skills import refs


# ---------- normalize_module：一种进、一种出 ----------

@pytest.mark.parametrize("raw,expect", [
    # 反引号/单引号/双引号包裹都剥
    ("`web/漏洞分类/sqli/手册.md`", "web/漏洞分类/sqli/手册.md"),
    ('"web/漏洞分类/sqli/手册.md"', "web/漏洞分类/sqli/手册.md"),
    ("'web/实战打法/sqli.md'", "web/实战打法/sqli.md"),
    # 前后空白与多余左斜杠
    ("  web/实战打法/sqli.md  ", "web/实战打法/sqli.md"),
    ("/web/实战打法/sqli.md", "web/实战打法/sqli.md"),
    # wikilink：别名/锚点只是展示层，目标按 md 补全
    ("[[web/漏洞分类/sqli/手册|手册]]", "web/漏洞分类/sqli/手册.md"),
    ("[[web/漏洞分类/sqli/手册]]", "web/漏洞分类/sqli/手册.md"),
    ("[[web/漏洞分类/sqli/手册#提权]]", "web/漏洞分类/sqli/手册.md"),
    ("[[web/实战打法/api-rest/00-index]]", "web/实战打法/api-rest/00-index.md"),
    # 已有扩展名不重复补
    ("[[web/实战打法/api-rest/00-index.md]]", "web/实战打法/api-rest/00-index.md"),
    ("[[binary/漏洞利用/scripts/x.py|x.py]]", "binary/漏洞利用/scripts/x.py"),
    # 裸名 wikilink：不补成"kb 路径语义"，只是加个 .md（是否可用由 resolve 判）
    ("[[手册]]", "手册.md"),
    # 非 wikilink 形态不补扩展名——不污染「模块不存在」错误面
    ("web/实战打法/sqli", "web/实战打法/sqli"),
    ("", ""),
])
def test_normalize_module(raw: str, expect: str):
    assert refs.normalize_module(raw) == expect


# ---------- 夹具：一套最小 kb ----------

TARGET = "web/漏洞分类/sqli/手册.md"
OTHER = "web/漏洞分类/xxe/手册.md"


@pytest.fixture()
def packs(tmp_path: Path) -> Path:
    kb = tmp_path / "packs" / "kb"
    (kb / "web" / "漏洞分类" / "sqli").mkdir(parents=True)
    (kb / "web" / "漏洞分类" / "sqli" / "手册.md").write_text(
        "# sqli 手册\n内容\n", encoding="utf-8")
    (kb / "web" / "漏洞分类" / "xxe").mkdir(parents=True)
    (kb / "web" / "漏洞分类" / "xxe" / "手册.md").write_text(
        "# xxe 手册\n内容\n", encoding="utf-8")
    return tmp_path / "packs"


# ---------- extract_modules：三形态合一 ----------

def test_extract_modules_three_forms():
    """kb_open / 反引号引号 / wikilink 三种形态并入同一份模块清单。"""
    text = (
        '显式：kb_open(module="web/实战打法/sqli.md")\n'
        f"反引号：`{OTHER}`\n"
        f"wikilink：[[web/漏洞分类/sqli/手册|手册]]\n"
        "省扩展名：[[binary/漏洞利用/advanced]]\n"
    )
    mods = refs.extract_modules(text, {"web": "web", "binary": "binary"})
    assert mods == ["web/实战打法/sqli.md", OTHER, TARGET,
                    "binary/漏洞利用/advanced.md"]


def test_extract_modules_dedup():
    text = (f"`{TARGET}` 又见 [[web/漏洞分类/sqli/手册|手册]] 再认 "
            f"kb_open(module=\"{TARGET}\")")
    assert refs.extract_modules(text, {"web": "web"}) == [TARGET]


# ---------- classify_module：wikilink 目标可判活，裸名忽略 ----------

def test_classify_module_wikilink_and_bare(packs: Path):
    snaps = refs.snapshot_index(packs)
    assert refs.classify_module("[[web/漏洞分类/sqli/手册|手册]]",
                                snaps, packs) is None
    assert refs.classify_module("[[web/漏洞分类/sqli/手册]]",
                                snaps, packs) is None
    # 裸名 wikilink（歧义源）→ 不判活也不报错
    assert refs.classify_module("[[手册]]", snaps, packs) is None
    # 真断链照旧报
    code, _msg = refs.classify_module("web/漏洞分类/nope/手册.md", snaps, packs)
    assert code == "kb-module-broken"


# ---------- _splice_wikilinks：改名联动的 wikilink 分支 ----------

def test_splice_wikilinks_keeps_alias():
    text = (f"A：`{TARGET}`\n"
            f"B：[[web/漏洞分类/sqli/手册|手册]]\n"
            f"C：[[web/漏洞分类/sqli/手册]]\n"
            f"D：[[web/漏洞分类/xxe/手册|XXE]]\n")
    out, n = refs._splice_wikilinks(text, TARGET,
                                    "web/漏洞分类/sqli/手册二.md")
    assert n == 2
    assert "[[web/漏洞分类/sqli/手册二.md|手册]]" in out
    assert "[[web/漏洞分类/sqli/手册二.md]]" in out
    # 别的模块的链接一个都不能动
    assert "[[web/漏洞分类/xxe/手册|XXE]]" in out
    # 别名的自然语言部分也没被误伤
    assert "B：[[web/漏洞分类/sqli/手册二.md|手册]]" in out


# ---------- find_module_refs：wikilink 命中 + 无误报 ----------

def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_find_module_refs_wikilink_hit_and_no_false_positive(packs: Path):
    kb = packs / "kb"
    _write(kb / "web" / "实战打法" / "linked.md",
           "# 引用\n详见 [[web/漏洞分类/sqli/手册|sqli 手册]]。\n")
    _write(kb / "web" / "实战打法" / "shortform.md",
           "# 只放别的 wikilink\n只有 [[web/漏洞分类/xxe/手册|XXE]] 这个。\n")
    hits = {h.file: h for h in
            refs.find_module_refs(packs, TARGET)}
    # wikilink 形态能命中，且行号/形态登记正确
    assert "kb/web/实战打法/linked.md" in hits
    assert hits["kb/web/实战打法/linked.md"].forms == ["wikilink"]
    assert hits["kb/web/实战打法/linked.md"].line == 2
    # 正文含别的 wikilink 但没引用目标模块 → 不得误报
    assert "kb/web/实战打法/shortform.md" not in hits


# ---------- kb_open：Agent 侧直接用 wikilink ----------

@pytest.fixture()
def dispatcher(packs: Path):
    from types import SimpleNamespace
    from core.agent.tools import ToolDispatcher
    stub = SimpleNamespace(
        packs_root=packs, capabilities=["web"],
        project_id="p", session_id="s", author="a", _step=0,
        bb=SimpleNamespace(append_event=lambda *a, **k: None))
    return ToolDispatcher, stub


def test_kb_open_accepts_wikilink(dispatcher):
    TD, stub = dispatcher
    for ref in ("[[web/漏洞分类/sqli/手册|手册]]",
                "[[web/漏洞分类/sqli/手册]]",
                f"`{TARGET}`"):
        out = TD._tool_kb_open(stub, ref)
        assert out.startswith("知识库模块（域 web）:"), ref
        assert out.splitlines()[0].endswith("手册.md"), ref


def test_kb_open_reports_missing_not_hallucinate(dispatcher):
    TD, stub = dispatcher
    out = TD._tool_kb_open(stub, "[[web/漏洞分类/nope/手册|手册]]")
    assert out.startswith("[防幻觉] 模块不存在: web/漏洞分类/nope/手册.md")


def test_kb_open_rejects_traversal(dispatcher):
    TD, stub = dispatcher
    out = TD._tool_kb_open(stub, "[[../secret/手册|手册]]")
    assert out.startswith("[拒绝]")
