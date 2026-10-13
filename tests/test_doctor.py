"""pack doctor 测试：tmp packs 造结构，精确断言 error/warning/info 分级。"""

from core.skills.doctor import diagnose

_SKILL = """---
name: {name}
description: {desc}
---
# {name}
{body}
"""


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_packs(root) -> None:
    # 技能扁平（kb/skills）：齐全技能 + 一个 name 不一致（error）
    _write(root / "kb/skills/good-skill/SKILL.md",
           _SKILL.format(name="good-skill", desc="好技能", body="正文"))
    _write(root / "kb/skills/wrong-dir/SKILL.md",
           _SKILL.format(name="actually-other-name", desc="名实不符", body="正文"))
    _write(root / "kb/skills/broken-refs/SKILL.md",
           _SKILL.format(name="broken-refs", desc="引用失效", body=(
               "见 `kb_open(module=\"ctf-web/missing.md\")`。")))
    _write(root / "kb/skills/disabled-skill/SKILL.md",
           "---\nname: disabled-skill\ndescription: 停用\nenabled: false\n---\n# x\n")
    # 引用未知快照 → kb-snapshot-unknown（首段非域目录且不在快照表）
    _write(root / "kb/skills/crypto-broken/SKILL.md",
           _SKILL.format(name="crypto-broken", desc="未知快照引用",
                         body="另引 `kb_open(module=\"nope-snap/x.md\")`。"))
    # kb 域 web（快照 ctf-web）
    _write(root / "kb/web/ctf-web/auth-jwt.md", "# jwt\n")

    # ctf 轨：专家引用悬空技能/禁用技能/未注册 task_type
    _write(root / "kb/ctf/track.yaml", "name: ctf\n")
    _write(root / "kb/安全红线/轨红线/ctf.md", "# ctf 红线\n")
    _write(root / "kb/ctf/task_types.yaml", "generic: passive\nsolve: passive\n")
    _write(root / "experts/_generalist.yaml", 'name: _generalist\nskills: null\n')
    _write(root / "experts/recon.yaml",
           'name: recon\n'
           'skills: [good-skill, ghost-skill, disabled-skill]\n'
           'task_types: [solve, bogus-type]\n'
           'tracks: [ctf]\n')
    # 回收站有一项（info）
    _write(root / "experts/.history/trash/old-expert.20260101T000000Z.bak",
           "name: old-expert\n")

    # research/redteam 空轨：缺 task_types.yaml 与 redlines（warning），不应炸
    _write(root / "kb/research/track.yaml", "name: research\n")
    _write(root / "kb/redteam/track.yaml", "name: redteam\n")


def test_doctor_levels_and_codes(tmp_path):
    root = tmp_path / "packs"
    _build_packs(root)
    rep = diagnose(root)
    issues = rep.issues
    by_code: dict[str, list] = {}
    for i in issues:
        by_code.setdefault(i.code, []).append(i)

    errors = {i.code for i in issues if i.level == "error"}
    warnings = {i.code for i in issues if i.level == "warning"}
    infos = {i.code for i in issues if i.level == "info"}

    # error：绑定断裂（技能名实不符 + 专家引用悬空技能/未注册 task_type）
    assert "skill-name-mismatch" in errors
    assert "expert-skill-missing" in errors
    assert "expert-tasktype-unregistered" in errors
    assert any("ghost-skill" in i.message for i in by_code["expert-skill-missing"])
    assert any("bogus-type" in i.message for i in by_code["expert-tasktype-unregistered"])

    # warning：禁用引用 / 缺红线 / kb 引用失效（含未知快照）/ 空轨缺注册表
    assert "expert-skill-disabled" in warnings
    assert "missing-redlines" in warnings
    assert "kb-module-broken" in warnings
    assert "kb-snapshot-unknown" in warnings
    assert "missing-task-types" in warnings
    assert any("ctf-web/missing.md" in i.message for i in by_code["kb-module-broken"])
    assert any("nope-snap/x.md" in i.message for i in by_code["kb-snapshot-unknown"])
    # research 与 redteam 各缺一条红线（ctf 有）
    redline_targets = {i.target for i in by_code["missing-redlines"]}
    assert "kb/安全红线/轨红线/research.md" in redline_targets
    assert "kb/安全红线/轨红线/redteam.md" in redline_targets
    assert "kb/安全红线/轨红线/ctf.md" not in redline_targets

    # info：孤儿技能（被引用的 good-skill 不报；禁用技能不报）+ 回收站
    orphan = {i.message for i in by_code.get("orphan-skill", [])}
    assert all("good-skill" not in m for m in orphan)
    assert all("disabled-skill" not in m for m in orphan)
    assert "trash-present" in infos

    # counts 与序列化
    counts = rep.counts
    assert counts["error"] >= 3 and counts["warning"] >= 6 and counts["info"] >= 2
    payload = rep.to_dict()
    assert set(payload["counts"]) == {"error", "warning", "info"}
    assert payload["issues"] == sorted(
        payload["issues"],
        key=lambda x: ({"error": 0, "warning": 1, "info": 2}[x["level"]],
                       x["code"], x["target"]))


def test_doctor_clean_pack_has_only_soft_notes(tmp_path):
    """健康结构：零 error；kb 引用全部有效；引用得到的技能不报孤儿。"""
    root = tmp_path / "packs"
    _write(root / "kb/skills/s1/SKILL.md",
           '---\nname: s1\ndescription: d\n---\n# s1\n`kb_open(module="ctf-web/auth-jwt.md")`\n')
    _write(root / "kb/web/ctf-web/auth-jwt.md", "# jwt\n")
    _write(root / "kb/ctf/track.yaml", "name: ctf\n")
    _write(root / "kb/安全红线/轨红线/ctf.md", "# r\n")
    _write(root / "kb/ctf/task_types.yaml", "generic: passive\nsolve: passive\n")
    _write(root / "experts/_generalist.yaml", 'name: 通用\nskills: null\n')
    _write(root / "experts/recon.yaml",
           "name: recon\nskills: [s1]\ntask_types: [solve]\ntracks: [ctf]\n")
    rep = diagnose(root)
    assert rep.counts["error"] == 0
    codes = {i.code for i in rep.issues}
    assert not {"kb-module-broken", "kb-snapshot-unknown", "orphan-skill"} & codes


def test_doctor_expert_display_name_decoupled_from_stem(tmp_path):
    """yaml name 行=中文显示名（可 ≠ 文件 stem）：不报 mismatch；缺 name 行才 warning。"""
    root = tmp_path / "packs"
    _write(root / "kb/ctf/track.yaml", "name: ctf\n")
    _write(root / "kb/安全红线/轨红线/ctf.md", "# r\n")
    _write(root / "kb/ctf/task_types.yaml", "generic: passive\n")
    _write(root / "experts/_generalist.yaml", 'name: 通用\nskills: null\n')
    _write(root / "experts/noname.yaml", "skills: null\n")  # 缺 name 行
    rep = diagnose(root)
    missing = [i for i in rep.issues if i.code == "expert-name-missing"]
    assert len(missing) == 1 and "noname" in missing[0].target


def test_doctor_rating_without_owner(tmp_path):
    """F11：评级与价值口径/<tag>.md 存在但 平台规则/<tag>.md 缺失 → warning；同存 → 不告警。"""
    root = tmp_path / "packs"
    _write(root / "kb/pentest/track.yaml", "name: pentest\n")
    _write(root / "kb/安全红线/评级与价值口径/osrc.md", "# osrc 评级与价值口径\n")
    rep = diagnose(root)
    hits = [i for i in rep.issues if i.code == "rating-without-owner"]
    assert len(hits) == 1 and hits[0].level == "warning"
    assert "osrc" in hits[0].message and hits[0].target.endswith("评级与价值口径/osrc.md")
    # 补齐同名 平台规则 → 告警消失
    _write(root / "kb/安全红线/平台规则/osrc.md", "# osrc 授权与红线\n")
    rep2 = diagnose(root)
    assert not [i for i in rep2.issues if i.code == "rating-without-owner"]


def test_doctor_missing_root(tmp_path):
    rep = diagnose(tmp_path / "nope")
    assert rep.counts["error"] == 1
    assert rep.issues[0].code == "packs-root-missing"


# ---------- M2 同义词表体检（retrieval-upgrade，2026-09-23） ----------

def test_doctor_synonyms_missing_and_dead_group(tmp_path):
    """词表缺失=info 提示；解析不出组=warning；死组（全成员 kb 零命中）=warning；
    空转成员=info。"""
    import pytest as _pytest
    from core.skills.doctor import diagnose
    root = tmp_path / "packs"
    root.mkdir()
    rep = diagnose(root)
    codes = {(i.code, i.target) for i in rep.issues}
    assert ("synonyms-missing", "kb/synonyms.yaml") in codes
    # 词表存在但解析不出组 → warning
    _write(root / "kb" / "synonyms.yaml", "groups: []\n")
    rep = diagnose(root)
    assert any(i.code == "synonyms-empty" for i in rep.issues)
    # 好组 + 死组并存（好组成员全部能命中 kb 文档，否则也会被报空转）
    _write(root / "kb" / "web" / "poc" / "越权检测.md", "# 越权检测方法\n正文")
    _write(root / "kb" / "synonyms.yaml", (
        "groups:\n"
        "  - id: good\n"
        "    terms: [越权, 越权检测]\n"
        "  - id: dead\n"
        "    terms: [量子纠缠, 玄学漏洞]\n"))
    rep = diagnose(root)
    msgs = {i.code: i.message for i in rep.issues}
    assert not any(i.code == "synonyms-missing" for i in rep.issues)
    assert "synonyms-dead-group" in msgs
    assert "dead" in msgs["synonyms-dead-group"]
    assert not any("good" == i.target.split("#")[-1] for i in rep.issues
                   if i.code.startswith("synonyms"))
    # 空转成员：好组混入一个 kb 零命中词
    _write(root / "kb" / "synonyms.yaml", (
        "groups:\n"
        "  - id: good\n"
        "    terms: [越权, 玄学漏洞]\n"))
    rep = diagnose(root)
    idle = [i for i in rep.issues if i.code == "synonyms-idle-term"]
    assert len(idle) == 1 and "玄学漏洞" in idle[0].message
