"""skills 层测试：正交分类学（能力包×场景轨）、注册表、路由评分、规则链、kb 多源。"""

import json
from pathlib import Path

import pytest

from core.skills import (
    KbSource,
    SkillRegistry,
    SkillRouter,
    build_rules_preamble,
    load_kb_sources,
    owner_rules,
    pack_rules,
    role_rules,
)
from core.skills.taxonomy import LEGACY_DOMAIN_MAP, load_task_types, project_binding


@pytest.fixture()
def packs(tmp_path):
    """最小正交布局：web 能力包（2 技能+红线+kb_sources）× assessment 轨（红线+owner+角色）。"""
    root = tmp_path / "packs"

    def make_skill(group, owner, name, frontmatter, body="步骤内容"):
        d = root / group / owner / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")

    make_skill("capabilities", "web", "file-upload-test", (
        "name: file-upload-test\n"
        "description: 文件上传漏洞测试方法论\n"
        "keywords: 上传, upload, 附件\n"
        "features: has_upload\n"
        "platforms: linux\n"
        "task_types: exploit\n"
    ))
    make_skill("capabilities", "web", "idor-test", (
        "name: idor-test\n"
        "description: 越权访问测试，读差分优先\n"
        "keywords: 越权, idor, 换id\n"
        "features: has_user_system, has_object_id\n"
        "task_types: exploit\n"
    ))
    # 二进制包：文件特征/格式标签 + 一个禁用技能
    make_skill("capabilities", "binary", "binary-pwn", (
        "name: binary-pwn\n"
        "description: 二进制漏洞利用入口\n"
        "keywords: pwn, 溢出\n"
        "file_features: NX, Canary\n"
        "formats: elf\n"
        "vuln_classes: stack\n"
    ))
    make_skill("capabilities", "binary", "disabled-skill", (
        "name: disabled-skill\n"
        "description: 停用技能\n"
        "keywords: disabled\n"
        "enabled: false\n"
    ))
    # 轨级技能
    make_skill("tracks", "assessment", "triage",
               "name: triage\ndescription: 分诊入口\nkeywords: 分诊, triage\n")

    cap_rules = root / "capabilities" / "web" / "rules"
    cap_rules.mkdir(parents=True)
    (cap_rules / "redlines.md").write_text("Web 能力红线", encoding="utf-8")

    track_rules = root / "tracks" / "assessment" / "rules"
    track_rules.mkdir(parents=True)
    (track_rules / "redlines.md").write_text("禁止登出用户会话\n禁止真资损", encoding="utf-8")
    owners = track_rules / "owners"
    owners.mkdir()
    (owners / "edusrc.md").write_text("教育资产：不取他人 PII", encoding="utf-8")
    role_rules_dir = track_rules / "role-rules"
    role_rules_dir.mkdir()
    (role_rules_dir / "recon.md").write_text("侦察角色：只被动收集", encoding="utf-8")

    roles = root / "tracks" / "assessment" / "roles"
    roles.mkdir(parents=True)
    (roles / "_generalist.yaml").write_text("name: _generalist\npersona: 兜底\n", encoding="utf-8")
    (roles / "recon.yaml").write_text(
        "name: recon\npersona: 侦察\nskills: [idor-test]\ntask_types: [recon]\n",
        encoding="utf-8")
    (track_rules.parent / "task_types.yaml").write_text(
        "recon: passive\nexploit: low\n", encoding="utf-8")

    # kb_sources：双源，一个非递归
    kb = root / "capabilities" / "web" / "kb"
    (kb / "src-a" / "sub").mkdir(parents=True)
    (kb / "src-a" / "top.md").write_text("a", encoding="utf-8")
    (kb / "src-a" / "sub" / "deep.md").write_text("a-deep", encoding="utf-8")
    (kb / "src-b").mkdir(parents=True)
    (kb / "src-b" / "only.md").write_text("b", encoding="utf-8")
    (root / "capabilities" / "web" / "kb_sources.json").write_text(json.dumps({
        "sources": [
            {"id": "src-a", "root": "kb/src-a", "recursive": True},
            {"id": "src-b", "root": "kb/src-b", "recursive": False},
        ]}, ensure_ascii=False), encoding="utf-8")
    return root


def test_registry_scan_and_parse(packs):
    reg = SkillRegistry(packs)
    assert reg.load() == 5
    sk = reg.get("file-upload-test")
    assert sk.kind == "capability" and sk.pack == "web"
    assert "上传" in sk.keywords and "has_upload" in sk.features
    assert sk.platforms == ["linux"]
    assert sk.task_types == ["exploit"]
    assert sk.enabled is True
    assert sk.body().startswith("步骤内容")  # frontmatter 不进正文
    triage = reg.get("triage")
    assert triage.kind == "track" and triage.pack == "assessment"
    assert reg.get("disabled-skill").enabled is False


def test_router_labels_and_pack_filter(packs):
    reg = SkillRegistry(packs)
    reg.load()
    router = SkillRouter(reg)
    # file_features ×3 命中
    hits = router.route(query="", file_features=["NX"], packs={"binary"})
    assert hits[0].skill.name == "binary-pwn"
    assert "file:NX" in hits[0].matched
    # 标签（formats/vuln_classes 合并 labels）×3
    hits = router.route(query="", labels=["elf"], packs={"binary"})
    assert hits[0].skill.name == "binary-pwn"
    assert "label:elf" in hits[0].matched
    # packs 候选集过滤：web 包内不可见 binary 技能
    hits = router.route(query="pwn 溢出", packs={"web", "assessment"})
    assert all(h.skill.name != "binary-pwn" for h in hits)
    # 轨技能在候选集内
    hits = router.route(query="分诊 triage", packs={"web", "assessment"})
    assert hits and hits[0].skill.name == "triage"
    # enabled:false 默认排除，include_disabled 可见
    assert not router.route(query="disabled", packs={"binary"})
    hits = router.route(query="disabled", packs={"binary"}, include_disabled=True)
    assert hits and hits[0].skill.name == "disabled-skill"


def test_router_feature_beats_keyword(packs):
    reg = SkillRegistry(packs)
    reg.load()
    router = SkillRouter(reg)
    pack_set = {"web", "assessment"}
    # 特征命中（×3）压过仅关键词命中（×2）
    hits = router.route(query="这里有个上传功能", features=["has_upload"], packs=pack_set)
    assert hits[0].skill.name == "file-upload-test"
    assert "has_upload" in hits[0].matched
    # 纯关键词
    hits = router.route(query="测一下越权 换id 看看", packs=pack_set)
    assert hits[0].skill.name == "idor-test"


def test_router_role_narrowing(packs):
    """角色白名单窄化（§6.6）：白名单外的 skill 不可路由。"""
    reg = SkillRegistry(packs)
    reg.load()
    router = SkillRouter(reg)
    hits = router.route(query="上传功能测试", role_skills=["idor-test"],
                        packs={"web", "assessment"})
    assert all(h.skill.name != "file-upload-test" for h in hits)
    assert len(hits) == 0  # idor 不匹配查询 → 空


def test_rules_chain_owner_and_role_rules(packs):
    # 能力包红线 ∪ 轨红线
    rules = dict((n, t) for n, t in pack_rules(packs, ["web"], "assessment"))
    assert "cap:web/redlines" in rules and rules["cap:web/redlines"] == "Web 能力红线"
    assert rules["track:assessment/redlines"] == "禁止登出用户会话\n禁止真资损"
    # owner 叠加走场景轨
    assert owner_rules(packs, "assessment", ["edusrc"])[0][0] == "owner:edusrc"
    assert owner_rules(packs, "assessment", ["不存在的厂商"]) == []
    # 角色专属红线
    assert role_rules(packs, "assessment", "recon") == (
        "role:recon", "侦察角色：只被动收集")
    assert role_rules(packs, "assessment", "nobody") is None
    preamble = build_rules_preamble(
        packs, track="assessment", capabilities=["web"],
        owner_tags=["edusrc"], role="recon")
    assert "Web 能力红线" in preamble
    assert "禁止真资损" in preamble
    assert "不取他人 PII" in preamble and "owner 叠加" in preamble
    assert "只被动收集" in preamble and "角色专属红线" in preamble


def test_kb_sources_multi(packs):
    sources = load_kb_sources(packs, ["web"])
    assert {s.id for s in sources} == {"src-a", "src-b"}
    by_id = {s.id: s for s in sources}
    assert isinstance(by_id["src-a"], KbSource)
    assert by_id["src-a"].root.is_absolute()
    assert by_id["src-a"].recursive is True
    assert by_id["src-b"].recursive is False
    # 未登记知识库源的能力包 → 空清单
    assert load_kb_sources(packs, ["binary"]) == []
    # 坏 JSON 不炸开窗
    (packs / "capabilities" / "binary" / "kb_sources.json").write_text("{坏", encoding="utf-8")
    assert load_kb_sources(packs, ["binary"]) == []


def test_task_types_registry(packs):
    table = load_task_types(packs, "assessment")
    assert table["generic"] == "passive"  # 内置
    assert table["recon"] == "passive" and table["exploit"] == "low"
    # 缺注册表的轨：只有 generic（不炸开窗）
    assert load_task_types(packs, "ctf") == {"generic": "passive"}


def test_legacy_domain_mapping():
    assert project_binding({"domain": "pentest"}) == ("assessment", ["web"])
    assert project_binding({"domain": "ctf"}) == ("ctf", ["binary"])
    # 新字段优先
    assert project_binding({"track": "ctf", "capabilities": ["crypto"],
                            "domain": "pentest"}) == ("ctf", ["crypto"])
    # 旧 reverse 项目自动归位 research+[binary]（不改 project.json）
    assert project_binding({"domain": "reverse"}) == ("research", ["binary"])
    assert LEGACY_DOMAIN_MAP["reverse"] == ("research", ["binary"])
    # 未知旧 domain：轨名沿用、包为空
    assert project_binding({"domain": "redteam"}) == ("redteam", [])
    assert LEGACY_DOMAIN_MAP["pentest"] == ("assessment", ["web"])


# ---------- 真实仓库 packs 回归（护栏：实包技能区分度 + 角色 yaml） ----------


def test_real_assessment_pack_skill_distinction():
    """capabilities/web 实包：recon 路由到 recon-asset-enum，打点角色到 web-strike-entry。"""
    reg = SkillRegistry("packs")
    reg.load()
    assert {"recon-asset-enum", "web-strike-entry"} <= {s.name for s in reg.all()}
    by_name = {s.name: s for s in reg.all()}
    assert by_name["recon-asset-enum"].pack == "web"
    router = SkillRouter(reg)
    query = "对目标做信息收集 资产枚举 指纹识别"
    hits_recon = router.route(query=query, role_skills=["recon-asset-enum"],
                              packs={"web", "assessment"})
    assert hits_recon and hits_recon[0].skill.name == "recon-asset-enum"
    hits_strike = router.route(query=query, role_skills=["web-strike-entry"],
                               packs={"web", "assessment"})
    assert all(h.skill.name != "recon-asset-enum" for h in hits_strike)  # 白名单外不可达


def test_real_assessment_roles_yaml():
    """assessment 轨 5 角色 yaml 护栏（极简 YAML 无 schema，解析错了这里报警）。"""
    from core.skills.roles import load_role

    recon = load_role("packs", "assessment", "recon")
    assert recon["skills"] == ["recon-asset-enum"]  # recon 已与打点技能分离
    assert recon["task_types"] == ["recon", "asset-enum"]
    assert recon["default_noise"] == "passive"
    assert recon.get("description")  # v0.2：职责描述必填护栏
    for role_name, expected_types in [
        ("external-entry", ["exploit", "recon"]),
        ("privesc", ["privesc"]),
        ("lateral", ["lateral-movement", "credential-access"]),
    ]:
        r = load_role("packs", "assessment", role_name)
        assert r["task_types"] == expected_types, role_name
        assert r["skills"] == ["web-strike-entry"], role_name
    g = load_role("packs", "assessment", "_generalist")
    assert g["skills"] is None and g["task_types"] is None  # 兜底角色不过滤


def test_real_ctf_roles_yaml():
    """ctf 轨 3 角色 + 注册表可读。"""
    from core.skills.roles import load_role

    assert load_task_types("packs", "ctf")["solve"] == "passive"
    g = load_role("packs", "ctf", "_generalist")
    assert g.get("default_noise") == "passive"
    for name in ("recon", "reverse"):
        load_role("packs", "ctf", name)  # 存在且可解析


def test_real_research_track_landed():
    """research 轨已落地（2026-09-14 P1）：全 passive 注册表 + reverse-analyst + doctor 零告警。"""
    from core.skills.doctor import diagnose
    from core.skills.roles import load_role

    table = load_task_types("packs", "research")
    assert table == {"generic": "passive", "triage": "passive", "reverse": "passive",
                     "analyze": "passive", "verify": "passive"}
    analyst = load_role("packs", "research", "reverse-analyst")
    assert analyst["skills"] == ["file-triage", "binary-rev"]
    assert analyst["task_types"] == ["triage", "reverse", "analyze", "verify"]
    assert analyst["default_noise"] == "passive"
    assert analyst.get("persona")  # 数据纪律 persona 必填护栏
    g = load_role("packs", "research", "_generalist")
    assert g["skills"] is None

    # 白名单技能真实存在（不悬空）
    reg = SkillRegistry("packs")
    reg.load()
    assert {"file-triage", "binary-rev"} <= {s.name for s in reg.all()}

    # 真实仓库 doctor：research 轨零 error/warning（redlines.md/task_types.yaml 是必要件）
    rep = diagnose(Path("packs"))
    bad = [i for i in rep.issues if i.level in ("error", "warning")
           and (i.target or "").replace("\\", "/").startswith("tracks/research")]
    assert not bad, [(i.level, i.code, i.target) for i in bad]


def test_real_thin_router_skills_stage2():
    """阶段 2：5 篇中文薄路由技能/分诊技能真实落包 + 路由可命中 + 白名单不悬空。"""
    reg = SkillRegistry("packs")
    reg.load()
    by_name = {s.name: s for s in reg.all()}
    # 新增 4 篇的归属
    assert by_name["binary-pwn"].kind == "capability" and by_name["binary-pwn"].pack == "binary"
    assert by_name["crypto-triage"].pack == "crypto"
    assert by_name["forensics-triage"].pack == "forensics"
    triage = by_name["triage"]
    assert triage.kind == "track" and triage.pack == "ctf"
    # kb/ 快照内的 SKILL.md（ctf-crypto 等）不得混进注册表
    assert all(not (s.pack == "crypto" and s.name == "ctf-crypto") for s in reg.all())

    router = SkillRouter(reg)
    # file_features ×3 稳命中 binary-pwn
    hits = router.route(query="", file_features=["NX", "Canary", "PIE"],
                        packs={"binary", "ctf"})
    assert hits[0].skill.name == "binary-pwn"
    # 纯中文题面：crypto / forensics 分诊
    assert router.route(query="RSA n e c 求解", packs={"crypto", "ctf"})[0].skill.name == "crypto-triage"
    assert router.route(query="pcap 流量分析", packs={"forensics", "ctf"})[0].skill.name == "forensics-triage"
    # 轨级分诊在全能力包候选集里对"开局分诊"题面可命中
    hits = router.route(query="拿到题包不知道从哪开始",
                        packs={"binary", "crypto", "forensics", "web", "misc", "ctf"})
    assert hits and hits[0].skill.name == "triage"
    # web 关键词补强后，短 query 不再 0 命中
    assert router.route(query="SQL 注入", packs={"web", "assessment"})[0].skill.name == "web-strike-entry"

    # 角色白名单引用真实存在的技能（不悬空）
    from core.skills.roles import load_role
    names = set(by_name)
    for track, role in [("ctf", "recon"), ("ctf", "reverse")]:
        for s in (load_role("packs", track, role).get("skills") or []):
            assert s in names, f"{track}/{role} 悬空引用 {s}"
