"""scripts/import_kb.py 幂等与产物契约测试（用最小假源，不依赖 Knowledge/）。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.import_kb import main as import_kb  # noqa: E402
from core.skills.rules import load_kb_sources  # noqa: E402


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
    # src-strike：拍平内容 + rules + 应被排除的杂物
    strike = knowledge / "src-strike"
    skill = strike / "skills" / "src-strike"
    (skill / "知识库").mkdir(parents=True)
    (skill / "知识库" / "idor-test.md").write_text("# idor", encoding="utf-8")
    (skill / "SKILL.md").write_text("# strike", encoding="utf-8")
    (strike / "rules").mkdir(parents=True)
    (strike / "rules" / "edusrc-rules.md").write_text("完整版平台规则 v2", encoding="utf-8")
    (strike / "rules" / "other-method.md").write_text("方法论留快照", encoding="utf-8")
    (strike / "AGENTS.md").write_text("外部 agent 指令，必须排除", encoding="utf-8")
    (strike / "mcp-servers").mkdir()
    (strike / "mcp-servers" / "big.exe").write_text("x", encoding="utf-8")

    packs = tmp_path / "packs"
    owners = packs / "tracks" / "assessment" / "rules" / "owners"
    owners.mkdir(parents=True)
    (owners / "edusrc.md").write_text("旧摘编版 v1", encoding="utf-8")
    return knowledge, packs


def test_import_twice_is_idempotent(env):
    knowledge, packs = env
    s1 = import_kb(knowledge, packs)
    assert (packs / "capabilities/web/kb/ctf-web/sqli/README.md").is_file()
    assert (packs / "capabilities/web/kb/CTF-SKILLS-LICENSE").read_text(
        encoding="utf-8") == "MIT License"
    assert (packs / "capabilities/misc/kb/ctf-misc/SKILL.md").is_file()
    # src-strike 拍平 + rules 随快照；排除项不进快照
    assert (packs / "capabilities/web/kb/src-strike/知识库/idor-test.md").is_file()
    assert (packs / "capabilities/web/kb/src-strike/rules/other-method.md").is_file()
    assert not (packs / "capabilities/web/kb/src-strike/AGENTS.md").exists()
    assert not (packs / "capabilities/web/kb/src-strike/mcp-servers").exists()
    # owners 完整版覆盖 + 旧版备份
    owner = packs / "tracks/assessment/rules/owners/edusrc.md"
    assert owner.read_text(encoding="utf-8") == "完整版平台规则 v2"
    assert list((owner.parent / ".history").glob("edusrc.md.*.bak"))
    # kb_sources.json 契约可被 core 解析
    sources = load_kb_sources(packs, ["web", "misc"])
    assert {s.id for s in sources} == {"web-kb", "misc-kb"}
    assert all(s.root.is_dir() for s in sources)
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
    snap = packs / "capabilities/web/kb/ctf-web"
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
