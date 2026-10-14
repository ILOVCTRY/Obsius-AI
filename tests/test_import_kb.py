"""scripts/import_kb.py 幂等与产物契约测试（用最小假源，不依赖 Knowledge/）。

2026-10-14 树状化重构后落点口径：
- ctf-skills 快照原件 → packs/kb/<方向>/赛事专题/题目知识/ctf-<x>/，根 LICENSE
  上收 packs/kb/licenses/；
- src-strike 按新树类目重映射落 packs/kb/web/（打法→实战打法/、资料→
  载荷与字典//行业视野//PoC与模板/、散篇→实践笔记/、手册→漏洞分类/<类目>/手册.md）；
- owners 完整版覆盖 packs/kb/安全红线/平台规则/；
- kb_sources.json 为已废止的旧布局遗产，脚本不再生成。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.import_kb import main as import_kb  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    knowledge = tmp_path / "Knowledge"
    ctf = knowledge / "ctf-skills-main" / "ctf-skills-main"
    # 两个 ctf 技能（一个 web 一个 misc）+ LICENSE
    (ctf / "ctf-web" / "sqli").mkdir(parents=True)
    (ctf / "ctf-web" / "sqli" / "README.md").write_text("# sqli", encoding="utf-8")
    (ctf / "ctf-web" / "scripts").mkdir()
    (ctf / "ctf-web" / "scripts" / "x.py").write_text("print(1)", encoding="utf-8")
    (ctf / "ctf-misc").mkdir(parents=True)
    (ctf / "ctf-misc" / "SKILL.md").write_text("---\nname: ctf-misc\n---\n", encoding="utf-8")
    (ctf / "LICENSE").write_text("MIT License", encoding="utf-8")
    # src-strike：新树类目重映射落点（实战打法/行业视野/PoC与模板/载荷与字典/实践笔记/）
    # + 应被排除的杂物
    strike = knowledge / "src-strike"
    skill = strike / "skills" / "src-strike"
    (skill / "知识库").mkdir(parents=True)
    (skill / "知识库" / "idor-test.md").write_text("# idor", encoding="utf-8")
    (skill / "SKILL.md").write_text("# strike", encoding="utf-8")
    refs = skill / "references"
    (refs / "h1-reports").mkdir(parents=True)
    (refs / "h1-reports" / "r1.md").write_text("# h1", encoding="utf-8")
    (refs / "playbooks" / "sqli-test.md").mkdir(parents=True)
    (refs / "playbooks" / "sqli-test.md" / "a.md").write_text("# pb", encoding="utf-8")
    (refs / "compliance.md").write_text("# 合规", encoding="utf-8")
    (skill / "随手笔记.md").write_text("# 散篇", encoding="utf-8")
    (strike / "rules").mkdir(parents=True)
    (strike / "rules" / "edusrc-rules.md").write_text("完整版平台规则 v2", encoding="utf-8")
    (strike / "rules" / "other-method.md").write_text("方法论留快照", encoding="utf-8")
    (skill / "AGENTS.md").write_text("外部 agent 指令，必须排除", encoding="utf-8")
    (skill / "mcp-servers").mkdir()
    (skill / "mcp-servers" / "big.exe").write_text("x", encoding="utf-8")

    packs = tmp_path / "packs"
    owners = packs / "kb" / "安全红线" / "平台规则"
    owners.mkdir(parents=True)
    (owners / "edusrc.md").write_text("旧摘编版 v1", encoding="utf-8")
    return knowledge, packs


def test_import_twice_is_idempotent(env):
    knowledge, packs = env
    s1 = import_kb(knowledge, packs)
    # ctf 快照原件落 赛事专题/题目知识/，LICENSE 上收 kb/licenses/
    assert (packs / "kb" / "web" / "赛事专题" / "题目知识" / "ctf-web"
            / "sqli" / "README.md").is_file()
    assert (packs / "kb" / "licenses" / "LICENSE").read_text(
        encoding="utf-8") == "MIT License"
    assert (packs / "kb" / "misc" / "赛事专题" / "题目知识" / "ctf-misc"
            / "SKILL.md").is_file()
    # src-strike 类目重映射：打法→实战打法/、资料→行业视野/、散篇→实践笔记/；
    # 排除项不进快照
    # K5 测试包分类：知识库/idor-test.md → 漏洞分类/idor/手册.md（含分面 frontmatter）
    idor = packs / "kb" / "web" / "漏洞分类" / "idor" / "手册.md"
    assert idor.is_file()
    assert idor.read_text(encoding="utf-8").startswith(
        "---\nphase: webapp\nvuln_class: [idor, authz]\n---")
    assert (packs / "kb" / "web" / "实战打法" / "SKILL.md").is_file()
    assert (packs / "kb" / "web" / "实战打法" / "rules" / "other-method.md").is_file()
    assert (packs / "kb" / "web" / "行业视野" / "playbooks" / "sqli-test.md"
            / "a.md").is_file()
    assert (packs / "kb" / "web" / "赛事专题" / "h1-reports" / "r1.md").is_file()
    assert (packs / "kb" / "web" / "行业视野" / "compliance.md").is_file()
    assert (packs / "kb" / "web" / "实践笔记" / "随手笔记.md").is_file()
    assert not (packs / "kb" / "web" / "实战打法" / "AGENTS.md").exists()
    assert not (packs / "kb" / "web" / "实践笔记" / "AGENTS.md").exists()
    assert not (packs / "kb" / "web" / "实践笔记" / "mcp-servers").exists()
    # owners 完整版覆盖 + 旧版备份（现居 kb/安全红线/平台规则/）
    owner = packs / "kb" / "安全红线" / "平台规则" / "edusrc.md"
    assert owner.read_text(encoding="utf-8") == "完整版平台规则 v2"
    assert list((owner.parent / ".history").glob("edusrc.md.*.bak"))
    assert s1["owners"] == ["edusrc.md"]

    # 第二次跑：快照全跳过；owners 与源一致不再备份/覆盖
    backups_before = len(list((owner.parent / ".history").glob("*.bak")))
    s2 = import_kb(knowledge, packs)
    assert len(s2["copied"]) == 0
    assert s2["owners"] == []
    assert len(list((owner.parent / ".history").glob("*.bak"))) == backups_before


def test_force_rebuild_preserves_local(env):
    """C3 新语义：--force 重建快照，但本地新增经验 md 与 .history 版本必须保全；
    上游同路径文件仍由上游内容胜出（本地副本不盖回）。"""
    knowledge, packs = env
    stats0 = import_kb(knowledge, packs)
    snap = packs / "kb" / "web" / "赛事专题" / "题目知识" / "ctf-web"
    local_md = snap / "LOCAL.md"
    local_nested = snap / "notes" / "本地经验.md"
    local_nested.parent.mkdir(parents=True)
    local_md.write_text("本地经验", encoding="utf-8")
    local_nested.write_text("# 嵌套新增", encoding="utf-8")
    hist = snap / ".history" / "kb-backups" / "20260101T000000Z__sqli__README.md.bak"
    hist.parent.mkdir(parents=True)
    hist.write_text("旧版本", encoding="utf-8")
    # 上游同路径文件本地被改过：重建后以上游为准
    upstream_readme = knowledge / "ctf-skills-main" / "ctf-skills-main" / "ctf-web" / "sqli" / "README.md"
    upstream_readme.write_text("# sqli v2", encoding="utf-8")

    import_kb(knowledge, packs)  # 非 force：跳过，全部保留
    assert local_md.is_file()
    stats = import_kb(knowledge, packs, force=True)  # force 重建
    assert local_md.read_text(encoding="utf-8") == "本地经验"
    assert local_nested.read_text(encoding="utf-8") == "# 嵌套新增"
    assert hist.is_file() and hist.read_text(encoding="utf-8") == "旧版本"
    assert (snap / "sqli/README.md").read_text(encoding="utf-8") == "# sqli v2"
    assert any("ctf-web" in p for p, _n in stats.get("preserved", []))
    assert stats0 is not stats  # 两次调用独立 stats


def test_missing_source_is_tolerated(tmp_path):
    stats = import_kb(tmp_path / "nope", tmp_path / "packs",
                      external_fallback=False)
    assert stats["copied"] == []
    assert not (tmp_path / "packs").exists()
