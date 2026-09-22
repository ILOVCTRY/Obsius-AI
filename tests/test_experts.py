"""专家池测试（expert-pool M1）：加载/轨变体覆写/回退/expert_exists/list 过滤 + doctor 体检码。"""

from pathlib import Path

import pytest

from core.skills.doctor import diagnose
from core.skills.experts import (
    GENERALIST,
    _apply_variant,
    all_capability_packs,
    allowed_roles,
    caps_effective,
    expert_exists,
    expert_skills,
    list_experts,
    load_expert,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_packs(root: Path) -> Path:
    """最小专家池 + 双轨 + web 包（供 doctor 体检与加载面测试共用）。"""
    _write(root / "experts/recon.yaml", (
        "# 侦察专家\n"
        "name: 侦察\n"
        'description: "pentest 侦察"\n'
        'persona: "pentest 侦察 persona"\n'
        "skills: [recon-asset-enum]\n"
        "task_types: [recon, asset-enum]\n"
        "default_noise: passive\n"
        "tracks: [pentest, redteam]\n"
        'variant_redteam_description: "redteam 侦察"\n'
        'variant_redteam_persona: "redteam 侦察 persona"\n'
    ))
    _write(root / "experts/_generalist.yaml", (
        "name: 通用\n"
        "skills: null\n"
        "task_types: null\n"
        "default_noise: medium\n"
        "max_steps: 200\n"
        "protected: true\n"
        "variant_ctf_default_noise: passive\n"
    ))
    _write(root / "experts/ctf-only.yaml", (
        "name: 仅 CTF\n"
        "skills: null\n"
        "tracks: [ctf]\n"
    ))

    _write(root / "tracks/ctf/track.yaml", "name: ctf\n")
    _write(root / "tracks/ctf/task_types.yaml", "generic: passive\nrecon: passive\n")
    _write(root / "tracks/ctf/rules/redlines.md", "# ctf 红线\n")
    _write(root / "tracks/ctf/roles/_generalist.yaml", "name: 通用\nskills: null\n")
    _write(root / "tracks/pentest/track.yaml", "name: pentest\n")
    _write(root / "tracks/pentest/task_types.yaml",
           "generic: passive\nrecon: passive\nasset-enum: passive\n")
    _write(root / "tracks/pentest/rules/redlines.md", "# pentest 红线\n")
    _write(root / "tracks/pentest/roles/_generalist.yaml", "name: 通用\nskills: null\n")
    _write(root / "tracks/redteam/track.yaml", "name: redteam\n")
    _write(root / "tracks/redteam/task_types.yaml",
           "generic: passive\nrecon: passive\nasset-enum: passive\n")
    _write(root / "tracks/redteam/rules/redlines.md", "# redteam 红线\n")
    _write(root / "tracks/redteam/roles/_generalist.yaml", "name: 通用\nskills: null\n")

    _write(root / "capabilities/web/pack.yaml", "kind: capability\nname: web\n")
    _write(root / "capabilities/web/rules/redlines.md", "# web 红线\n")
    _write(root / "capabilities/web/skills/recon-asset-enum/SKILL.md",
           "---\nname: recon-asset-enum\ndescription: 侦察\n---\n# x\n")
    return root


# ---- load_expert：轨变体覆写（§4.2）----

def test_load_expert_applies_track_variant(tmp_path):
    root = _build_packs(tmp_path / "packs")
    base = load_expert(root, "recon", "pentest")
    assert base["description"] == "pentest 侦察"
    assert base["persona"] == "pentest 侦察 persona"
    assert base["skills"] == ["recon-asset-enum"]
    assert base["tracks"] == ["pentest", "redteam"]  # 池字段原样保留
    assert not [k for k in base if k.startswith("variant_")]  # 变体键不透传

    rt = load_expert(root, "recon", "redteam")
    assert rt["description"] == "redteam 侦察"      # 覆写生效
    assert rt["persona"] == "redteam 侦察 persona"
    assert rt["skills"] == ["recon-asset-enum"]      # 未覆写字段不动
    assert not [k for k in rt if k.startswith("variant_")]


def test_apply_variant_strips_other_track_keys():
    e = {"a": 1, "variant_redteam_b": 2, "variant_ctf_c": 3}
    assert _apply_variant(e, "redteam") == {"a": 1, "b": 2}
    assert _apply_variant(e, "ctf") == {"a": 1, "c": 3}
    assert _apply_variant(e, "pentest") == {"a": 1}


# ---- load_expert：_generalist 回退 + 变体 ----

def test_load_expert_falls_back_to_generalist_with_variant(tmp_path):
    root = _build_packs(tmp_path / "packs")
    ctf = load_expert(root, "ghost-expert", "ctf")
    assert ctf["default_noise"] == "passive"   # ctf 变体覆写
    assert ctf["max_steps"] == 200             # 基线字段保留
    pentest = load_expert(root, "ghost-expert", "pentest")
    assert pentest["default_noise"] == "medium"  # 无变体=基线


def test_load_expert_raises_without_generalist(tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        load_expert(root, "anything", "ctf")


# ---- expert_exists（§4.6：池内存在 且 track ∈ tracks）----

def test_expert_exists(tmp_path):
    root = _build_packs(tmp_path / "packs")
    assert expert_exists(root, "recon", "pentest")
    assert expert_exists(root, "recon", "redteam")
    assert not expert_exists(root, "recon", "ctf")      # 轨域外
    assert not expert_exists(root, "ghost-expert", "ctf")  # 池内不存在
    assert expert_exists(root, GENERALIST, "research")  # 缺 tracks=全轨
    assert expert_exists(root, "ctf-only", "ctf")
    assert not expert_exists(root, "ctf-only", "pentest")
    assert not expert_exists(tmp_path / "nope", "recon", "ctf")  # 池目录缺失


# ---- list_experts：轨过滤 ----

def test_list_experts_track_filter(tmp_path):
    root = _build_packs(tmp_path / "packs")
    assert list_experts(root) == ["_generalist", "ctf-only", "recon"]
    assert list_experts(root, "ctf") == ["_generalist", "ctf-only"]
    assert list_experts(root, "pentest") == ["_generalist", "recon"]
    assert list_experts(root, "redteam") == ["_generalist", "recon"]
    assert list_experts(tmp_path / "nope") == []


# ---- doctor 体检码 ----

def _codes(rep):
    return {i.code for i in rep.issues}


def test_doctor_healthy_expert_pool_no_issues(tmp_path):
    root = _build_packs(tmp_path / "packs")
    rep = diagnose(root)
    codes = _codes(rep)
    assert not {c for c in codes if c.startswith("expert-") or c == "role-rules-orphan"}
    # 专家引用的技能不报孤儿（专家引用也算可达）
    assert not any("recon-asset-enum" in i.message
                   for i in rep.issues if i.code == "orphan-skill")


def test_doctor_expert_broken_bindings(tmp_path):
    root = _build_packs(tmp_path / "packs")
    _write(root / "capabilities/web/skills/off/SKILL.md",
           "---\nname: off\ndescription: 停用\nenabled: false\n---\n# x\n")
    _write(root / "experts/broken.yaml", (
        "skills: [ghost-skill, off]\n"      # 不存在 + 已禁用
        "task_types: [bogus-type]\n"        # 未注册
        "tracks: [nosuch]\n"                # 未知轨
        "variant_nosuch_x: 1\n"             # 变体轨未知
    ))
    rep = diagnose(root)
    issues = rep.issues
    errors = {i.code for i in issues if i.level == "error"}
    warnings = {i.code for i in issues if i.level == "warning"}
    assert {"expert-skill-missing", "expert-tasktype-unregistered",
            "expert-track-unknown"} <= errors
    assert {"expert-skill-disabled", "expert-name-missing"} <= warnings
    broken = [i for i in issues if "experts/broken.yaml" in i.target]
    assert any("ghost-skill" in i.message for i in broken)
    assert any("bogus-type" in i.message for i in broken)
    # tracks 与 variant_ 前缀指向同一未知轨 → 去重后只报一条
    assert sum(1 for i in broken if i.code == "expert-track-unknown") == 1
    assert any("nosuch" in i.message for i in broken if i.code == "expert-track-unknown")


def test_doctor_expert_skill_ambiguous_across_packs(tmp_path):
    root = _build_packs(tmp_path / "packs")
    _write(root / "capabilities/crypto/pack.yaml", "kind: capability\nname: crypto\n")
    _write(root / "capabilities/crypto/rules/redlines.md", "# r\n")
    _write(root / "capabilities/crypto/skills/recon-asset-enum/SKILL.md",
           "---\nname: recon-asset-enum\ndescription: 重名\n---\n# x\n")
    rep = diagnose(root)
    hits = [i for i in rep.issues if i.code == "expert-skill-ambiguous"]
    assert len(hits) == 1 and hits[0].level == "warning"
    assert "recon-asset-enum" in hits[0].message and "crypto" in hits[0].message


def test_doctor_expert_generalist_missing(tmp_path):
    root = _build_packs(tmp_path / "packs")
    (root / "experts/_generalist.yaml").unlink()
    rep = diagnose(root)
    hits = [i for i in rep.issues if i.code == "expert-generalist-missing"]
    assert len(hits) == 1 and hits[0].level == "error"


def test_doctor_role_rules_orphan(tmp_path):
    root = _build_packs(tmp_path / "packs")
    _write(root / "tracks/ctf/rules/role-rules/recon.md", "# 绑专家 recon，不告警\n")
    _write(root / "tracks/ctf/rules/role-rules/_generalist.md", "# 绑角色，不告警\n")
    _write(root / "tracks/ctf/rules/role-rules/ghost-role.md", "# 悬空\n")
    rep = diagnose(root)
    hits = [i for i in rep.issues if i.code == "role-rules-orphan"]
    assert len(hits) == 1 and hits[0].level == "warning"
    assert "ghost-role" in hits[0].message


def test_doctor_skips_expert_checks_without_pool(tmp_path):
    """experts/ 目录缺省 → 专家体检整段静默（现有包结构不受影响）。"""
    root = tmp_path / "packs"
    _write(root / "capabilities/web/pack.yaml", "kind: capability\nname: web\n")
    _write(root / "capabilities/web/rules/redlines.md", "# r\n")
    rep = diagnose(root)
    assert not {c for c in _codes(rep)
                if c.startswith("expert-") or c == "role-rules-orphan"}


# ---- M2 推导面（§4.4 专家面 / §4.6 allowed_roles，2026-09-21）----

def test_expert_skills_union_null_and_missing(tmp_path):
    root = _build_packs(tmp_path / "packs")
    # 并集去重排序
    assert expert_skills(root, "pentest", ["recon", "recon"]) == ["recon-asset-enum"]
    # 任一全量专家（skills=null，如 _generalist）→ None = 不限定
    assert expert_skills(root, "pentest", ["recon", "_generalist"]) is None
    # 空绑定 → None（caps_effective 先判空绑定走 fallback，故其手中 None 必是全量分支）
    assert expert_skills(root, "pentest", []) is None
    assert expert_skills(root, "pentest", None) is None
    # 绑定专家 yaml 被删：宁严勿松——跳过不静默回退，全缺 → 空白名单
    assert expert_skills(root, "pentest", ["ghost-expert"]) == []


def test_caps_effective_fallback_full_and_union(tmp_path):
    root = _build_packs(tmp_path / "packs")
    _write(root / "capabilities/crypto/pack.yaml", "kind: capability\nname: crypto\n")
    _write(root / "capabilities/crypto/rules/redlines.md", "# r\n")
    _write(root / "capabilities/crypto/skills/crypto-scan/SKILL.md",
           "---\nname: crypto-scan\ndescription: c\n---\n# x\n")
    _write(root / "experts/crypto-auditor.yaml",
           "name: 密码审计\nskills: [crypto-scan]\ntracks: [pentest]\n")
    # 存量直通：无绑定 → fallback（meta.capabilities）原样，零翻译
    assert caps_effective(root, "pentest", None, fallback=["web"]) == ["web"]
    assert caps_effective(root, "pentest", [], fallback=None) == []
    # 全量专家 → capabilities/ 目录全集
    assert caps_effective(root, "pentest", ["_generalist"]) == ["crypto", "web"]
    # 绑定并集：专家技能（capability 类）所属包推导
    assert caps_effective(root, "pentest", ["recon", "crypto-auditor"]) == ["crypto", "web"]
    assert caps_effective(root, "pentest", ["crypto-auditor"]) == ["crypto"]
    # 全部绑定专家缺文件 → 空面（绝不静默回退 fallback 扩权，doctor 兜底体检）
    assert caps_effective(root, "pentest", ["ghost"], fallback=["web"]) == []


def test_all_capability_packs_lists_capability_dirs(tmp_path):
    root = _build_packs(tmp_path / "packs")
    assert all_capability_packs(root) == ["web"]


def test_allowed_roles_bound_list_wins_over_pool(tmp_path):
    root = _build_packs(tmp_path / "packs")
    assert allowed_roles(root, "pentest", ["recon"]) == ["recon"]
    assert allowed_roles(root, "pentest", ["recon", "  "]) == ["recon"]  # 空白项剔除
    # 未绑定 = 按轨过滤全池（与退役前 list_roles 行为等价）
    assert allowed_roles(root, "pentest", []) == ["_generalist", "recon"]
    assert allowed_roles(root, "ctf", None) == ["_generalist", "ctf-only"]


# ---- M4a 场景档（§4.8/D6）：加载面 + doctor 体检码（2026-09-21）----

def test_load_track_profiles_and_single(tmp_path):
    from core.skills.profiles import load_profile, load_track_profiles, profile_snapshot
    root = _build_packs(tmp_path / "packs")
    _write(root / "tracks/ctf/profiles/full-squad.yaml", (
        "name: 全栈解题队\n"
        "experts: [recon, ctf-only]\n"
        "board_view: findings\n"
        "artifacts: [writeup]\n"
    ))
    _write(root / "tracks/pentest/profiles/recon-first.yaml", "name: 侦察先行\n")
    profs = load_track_profiles(root, "ctf")
    assert [p["id"] for p in profs] == ["full-squad"]
    assert profs[0]["experts"] == ["recon", "ctf-only"] and profs[0]["board_view"] == "findings"
    assert load_track_profiles(root, "redteam") == []  # 无 profiles 目录 = 空
    p = load_profile(root, "ctf", "full-squad")
    assert p["id"] == "full-squad" and p["name"] == "全栈解题队"
    assert load_profile(root, "ctf", "nope") is None
    snap = profile_snapshot(p)
    assert snap == {"id": "full-squad", "name": "全栈解题队", "playbook": None,
                    "artifacts": ["writeup"], "knowledge": []}


def test_doctor_profile_checks(tmp_path):
    """场景档体检：组队引用不存在/轨外 → error；board_view 值域外 → warning。"""
    root = _build_packs(tmp_path / "packs")
    _write(root / "tracks/ctf/profiles/good.yaml",
           "name: 好\nexperts: [ctf-only]\nboard_view: assets\n")
    _write(root / "tracks/ctf/profiles/bad-ref.yaml",
           "name: 坏引用\nexperts: [recon]\n")  # recon tracks=[pentest,redteam] 不服务 ctf
    _write(root / "tracks/ctf/profiles/bad-view.yaml",
           "name: 坏视图\nboard_view: kanban\n")
    rep = diagnose(root)
    assert "profile-expert-missing" in {i.code for i in rep.issues}
    assert "profile-board-view-unknown" in {i.code for i in rep.issues}
    assert not any("good.yaml" in i.target for i in rep.issues)
