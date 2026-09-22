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
    rating_rules,
    resolve_rule_profiles,
    role_rules,
)
from core.skills.taxonomy import LEGACY_DOMAIN_MAP, load_task_types, project_binding


@pytest.fixture()
def packs(tmp_path):
    """最小正交布局：web 能力包（2 技能+红线+kb_sources）× pentest 轨（红线+owner+角色）。"""
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
    make_skill("tracks", "pentest", "triage",
               "name: triage\ndescription: 分诊入口\nkeywords: 分诊, triage\n")

    cap_rules = root / "capabilities" / "web" / "rules"
    cap_rules.mkdir(parents=True)
    (cap_rules / "redlines.md").write_text("Web 能力红线", encoding="utf-8")

    track_rules = root / "tracks" / "pentest" / "rules"
    track_rules.mkdir(parents=True)
    (track_rules / "redlines.md").write_text("禁止登出用户会话\n禁止真资损", encoding="utf-8")
    owners = track_rules / "owners"
    owners.mkdir()
    (owners / "edusrc.md").write_text("教育资产：不取他人 PII", encoding="utf-8")
    role_rules_dir = track_rules / "role-rules"
    role_rules_dir.mkdir()
    (role_rules_dir / "recon.md").write_text("侦察角色：只被动收集", encoding="utf-8")

    roles = root / "tracks" / "pentest" / "roles"
    roles.mkdir(parents=True)
    (roles / "_generalist.yaml").write_text("name: _generalist\npersona: 兜底\n", encoding="utf-8")
    (roles / "recon.yaml").write_text(
        "name: recon\npersona: 侦察\nskills: [idor-test]\ntask_types: [recon]\n",
        encoding="utf-8")
    (track_rules.parent / "task_types.yaml").write_text(
        "recon: passive\nexploit: low\n", encoding="utf-8")

    # kb 全局单根（M0）：packs/kb/<域>/<快照>/…，一域一根（recursive 恒 True）；
    # 未建 kb 目录的能力域（binary）= 合成源为空
    kb = root / "kb" / "web"
    (kb / "snap-a" / "sub").mkdir(parents=True)
    (kb / "snap-a" / "top.md").write_text("a", encoding="utf-8")
    (kb / "snap-a" / "sub" / "deep.md").write_text("a-deep", encoding="utf-8")
    (kb / "snap-b").mkdir(parents=True)
    (kb / "snap-b" / "only.md").write_text("b", encoding="utf-8")
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
    assert triage.kind == "track" and triage.pack == "pentest"
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
    hits = router.route(query="pwn 溢出", packs={"web", "pentest"})
    assert all(h.skill.name != "binary-pwn" for h in hits)
    # 轨技能在候选集内
    hits = router.route(query="分诊 triage", packs={"web", "pentest"})
    assert hits and hits[0].skill.name == "triage"
    # enabled:false 默认排除，include_disabled 可见
    assert not router.route(query="disabled", packs={"binary"})
    hits = router.route(query="disabled", packs={"binary"}, include_disabled=True)
    assert hits and hits[0].skill.name == "disabled-skill"


def test_router_feature_beats_keyword(packs):
    reg = SkillRegistry(packs)
    reg.load()
    router = SkillRouter(reg)
    pack_set = {"web", "pentest"}
    # 特征命中（×3）压过仅关键词命中（×2）
    hits = router.route(query="这里有个上传功能", features=["has_upload"], packs=pack_set)
    assert hits[0].skill.name == "file-upload-test"
    assert "has_upload" in hits[0].matched
    # 纯关键词
    hits = router.route(query="测一下越权 换id 看看", packs=pack_set)
    assert hits[0].skill.name == "idor-test"


def test_router_role_narrowing(packs):
    """角色白名单=偏好排序（v0.65 降级）：白名单内排前 +breakdown「角色偏好」；
    白名单外仍可正常命中（能力提升而非限制）。"""
    reg = SkillRegistry(packs)
    reg.load()
    router = SkillRouter(reg)
    hits = router.route(query="上传功能测试", role_skills=["file-upload-test"],
                        packs={"web", "pentest"})
    assert hits and hits[0].skill.name == "file-upload-test"
    assert any(c["category"] == "role" for c in hits[0].breakdown)
    # 白名单外技能不再被硬裁剪：idor-test 白名单下 file-upload-test 仍可命中
    hits2 = router.route(query="上传功能测试", role_skills=["idor-test"],
                         packs={"web", "pentest"})
    assert any(h.skill.name == "file-upload-test" for h in hits2)
    assert all(h.skill.name != "file-upload-test"
               or not any(c["category"] == "role" for c in h.breakdown)
               for h in hits2)


def test_rules_chain_owner_and_role_rules(packs):
    # 能力包红线 ∪ 轨红线
    rules = dict((n, t) for n, t in pack_rules(packs, ["web"], "pentest"))
    assert "cap:web/redlines" in rules and rules["cap:web/redlines"] == "Web 能力红线"
    assert rules["track:pentest/redlines"] == "禁止登出用户会话\n禁止真资损"
    # owner 叠加走场景轨
    assert owner_rules(packs, "pentest", ["edusrc"])[0][0] == "owner:edusrc"
    assert owner_rules(packs, "pentest", ["不存在的厂商"]) == []
    # 角色专属红线
    assert role_rules(packs, "pentest", "recon") == (
        "role:recon", "侦察角色：只被动收集")
    assert role_rules(packs, "pentest", "nobody") is None
    preamble = build_rules_preamble(
        packs, track="pentest", capabilities=["web"],
        owner_tags=["edusrc"], role="recon")
    assert "Web 能力红线" in preamble
    assert "禁止真资损" in preamble
    assert "不取他人 PII" in preamble and "owner 叠加" in preamble
    assert "只被动收集" in preamble and "角色专属红线" in preamble


def test_rating_rules_and_rule_profiles(packs):
    """F11 评级与价值口径：rating 注入四态 + 段序 + 硬指令 + 不存在 tag 静默剔除。"""
    tr = packs / "tracks" / "pentest" / "rules"
    (tr / "rating").mkdir()
    (tr / "rating" / "edusrc.md").write_text("教育判级：高危=交互实证后系统权限", encoding="utf-8")
    (tr / "rating" / "edu-rating.md").write_text("教育评级：任意文件覆盖写=中危核", encoding="utf-8")
    assert rating_rules(packs, "pentest", ["edusrc"])[0][0] == "rating:edusrc"

    # 缺省 profiles：owners 自动全注入；rating 自动 = owner 命中 ∩ rating 文件存在
    eo, er = resolve_rule_profiles(packs, "pentest", ["edusrc"], None)
    assert eo == ["edusrc"] and er == ["edusrc"]
    pre = build_rules_preamble(packs, track="pentest", owner_tags=["edusrc"])
    assert "rule:rating:edusrc" in pre
    assert pre.count("评级硬指令") == 1  # 硬指令恰一次
    assert "rule:rating:edu-rating" not in pre  # 未自动命中的 tag 不注入

    # 显式 rating 全集：可提前挂未自动命中的 tag
    eo, er = resolve_rule_profiles(packs, "pentest", ["edusrc"], {"rating": ["edu-rating"]})
    assert er == ["edu-rating"]
    pre = build_rules_preamble(packs, track="pentest", owner_tags=["edusrc"],
                               rule_profiles={"rating": ["edu-rating"]})
    assert "rule:rating:edu-rating" in pre

    # rating=[] 关闭；owners 不受影响
    pre = build_rules_preamble(packs, track="pentest", owner_tags=["edusrc"],
                               rule_profiles={"rating": []})
    assert "rule:rating:" not in pre and "rule:owner:edusrc" in pre

    # owners 清单裁剪：自动命中 ∩ 空清单 = 全不注入（rating 自动跟随为空）
    eo, er = resolve_rule_profiles(packs, "pentest", ["edusrc"], {"owners": []})
    assert eo == [] and er == []
    pre = build_rules_preamble(packs, track="pentest", owner_tags=["edusrc"],
                               rule_profiles={"owners": []})
    assert "rule:owner:edusrc" not in pre and "rule:rating:" not in pre

    # 不存在的 tag 静默剔除
    eo, er = resolve_rule_profiles(packs, "pentest", ["edusrc", "不存在"], {"rating": ["不存在"]})
    assert eo == ["edusrc"] and er == []

    # 段序：owner 段 < rating 段 < role 段
    pre = build_rules_preamble(packs, track="pentest", capabilities=["web"],
                               owner_tags=["edusrc"], role="recon")
    assert -1 < pre.find("rule:owner:edusrc") \
        < pre.find("rule:rating:edusrc") < pre.find("rule:role:recon")
    assert "评级硬指令" in pre


def test_kb_sources_synthetic(packs):
    """M0 合成源：packs/kb/<域> 一域一根（id=<域>-kb、recursive 恒 True）；
    未建 kb 目录的能力域（含占位包）→ 空清单，不再读 kb_sources.json。"""
    sources = load_kb_sources(packs, ["web", "binary"])
    assert [(s.id, s.recursive) for s in sources] == [("web-kb", True)]
    assert isinstance(sources[0], KbSource)
    assert sources[0].root == (packs / "kb" / "web").resolve()
    # 目录不存在的域被过滤（合成不造目录）
    assert load_kb_sources(packs, ["binary"]) == []
    assert load_kb_sources(packs, []) == []


def test_task_types_registry(packs):
    table = load_task_types(packs, "pentest")
    assert table["generic"] == "passive"  # 内置
    assert table["recon"] == "passive" and table["exploit"] == "low"
    # 缺注册表的轨：只有 generic（不炸开窗）
    assert load_task_types(packs, "ctf") == {"generic": "passive"}


def test_legacy_domain_mapping():
    assert project_binding({"domain": "pentest"}) == ("pentest", ["web"])
    assert project_binding({"domain": "ctf"}) == ("ctf", ["binary"])
    # 新字段优先
    assert project_binding({"track": "ctf", "capabilities": ["crypto"],
                            "domain": "pentest"}) == ("ctf", ["crypto"])
    # 旧 reverse 项目自动归位 research+[binary]（不改 project.json）
    assert project_binding({"domain": "reverse"}) == ("research", ["binary"])
    assert LEGACY_DOMAIN_MAP["reverse"] == ("research", ["binary"])
    # 未知旧 domain：轨名沿用、包为空
    assert project_binding({"domain": "redteam"}) == ("redteam", [])


def test_legacy_track_mapping_assessment_to_pentest():
    """R1：旧 track 值 assessment 读兼容映射为 pentest（盘上 project.json 不改）。"""
    from core.skills.taxonomy import LEGACY_TRACK_MAP
    assert LEGACY_TRACK_MAP == {"assessment": "pentest"}
    assert project_binding({"track": "assessment", "capabilities": ["web"]}) == \
        ("pentest", ["web"])
    # 无 capabilities 的旧行同样映射（caps 空）
    assert project_binding({"track": "assessment"}) == ("pentest", [])
    # 新轨值原样返回
    assert project_binding({"track": "redteam", "capabilities": ["web"]}) == \
        ("redteam", ["web"])
    assert LEGACY_DOMAIN_MAP["pentest"] == ("pentest", ["web"])


# ---------- 真实仓库 packs 回归（护栏：实包技能区分度 + 角色 yaml） ----------


def test_real_pentest_pack_skill_distinction():
    """capabilities/web 实包：recon 路由到 recon-asset-enum，打点角色到 web-strike-entry。"""
    reg = SkillRegistry("packs")
    reg.load()
    assert {"recon-asset-enum", "web-strike-entry"} <= {s.name for s in reg.all()}
    by_name = {s.name: s for s in reg.all()}
    assert by_name["recon-asset-enum"].pack == "web"
    router = SkillRouter(reg)
    query = "对目标做信息收集 资产枚举 指纹识别"
    hits_recon = router.route(query=query, role_skills=["recon-asset-enum"],
                              packs={"web", "pentest"})
    assert hits_recon and hits_recon[0].skill.name == "recon-asset-enum"
    hits_strike = router.route(query=query, role_skills=["web-strike-entry"],
                               packs={"web", "pentest"})
    # v0.65 白名单降偏好：recon-asset-enum 仍可命中（白名单外可达），
    # 但 recon 角色下 recon-asset-enum 拿「角色偏好」加分排第一
    assert any(h.skill.name == "recon-asset-enum" for h in hits_strike)
    hits_recon_role = router.route(query=query, role_skills=["recon-asset-enum"],
                                   packs={"web", "pentest"})
    assert hits_recon_role[0].skill.name == "recon-asset-enum"
    # 偏好加分的可观测效果：recon 角色下 recon-asset-enum 分差显著拉开
    # （recon：13 = 8 文本分 + 5 偏好；strike 视角下无偏好仅 8）


def test_real_k1_thin_entry_skills():
    """K1 细化补缺护栏（2026-09-20）：薄入口技能注册，注入/杂项可路由；
    每个新技能带特征→模块对照表（正文含 kb_open 指引）。
    2026-09-21：pentest 轨 intranet-recon/lateral-move/privesc-win-lin 退场
    （红队向，owners 规则禁内网渗透/提权；redteam 轨 rt-* 镜像为唯一条目）。"""
    reg = SkillRegistry("packs")
    reg.load()
    names = {s.name for s in reg.all()}
    assert {"web-authn-session", "web-injection", "web-api-attack",
            "web-client-side", "web-post-exp", "misc-triage",
            "rt-intranet-recon", "rt-lateral-move", "rt-privesc"} <= names
    assert not {"intranet-recon", "lateral-move", "privesc-win-lin"} & names
    # rt-* 独立条目且归属正确（pentest 侧已退场）
    by_name = {s.name: s for s in reg.all()}
    assert by_name["rt-lateral-move"].pack == "redteam"
    router = SkillRouter(reg)
    # 注入任务命中 web-injection（与 web-strike-entry 同分并列即可）
    hits = router.route(query="测试 sql 注入点 盲注", packs={"web", "pentest"},
                        top_k=3)
    assert any(h.skill.name == "web-injection" for h in hits)
    # 横向任务 + 红队角色白名单 → rt-lateral-move 排第一（task_type+偏好）
    hits = router.route(query="内网横向 移动到域控", packs={"web", "redteam"},
                        role_skills=["web-strike-entry", "rt-lateral-move"],
                        task_type="lateral-movement", top_k=3)
    assert hits[0].skill.name == "rt-lateral-move"
    # 杂项 pyjail 题命中 misc-triage
    hits = router.route(query="pyjail 沙箱逃逸 过滤 eval",
                        packs={"misc", "ctf"}, top_k=3)
    assert hits and hits[0].skill.name == "misc-triage"
    # 对照表覆盖度抽检：每个新技能正文都有 kb_open 对照表
    for n in ("web-injection", "web-post-exp", "misc-triage"):
        body = by_name[n].path.read_text(encoding="utf-8")
        assert "kb_open" in body and "|---" in body


def test_real_pentest_experts_yaml():
    """pentest 轨专家护栏（expert-pool M2；极简 YAML 无 schema，解析错了这里报警）。"""
    from core.skills.experts import load_expert

    recon = load_expert("packs", "recon", "pentest")
    assert recon["skills"] == ["recon-asset-enum"]  # recon 已与打点技能分离
    assert recon["task_types"] == ["recon", "asset-enum"]
    assert recon["default_noise"] == "passive"
    assert recon.get("description")  # v0.2：职责描述必填护栏
    for role_name, expected_types, expected_skills in [
        ("external-entry", ["exploit", "recon"], ["web-strike-entry"]),
        # J 组（2026-09-20 开源对标扩充）：OSINT 情报 + 报告工程师
        ("osint", ["recon", "asset-enum"], ["recon-asset-enum"]),
        ("report-writer", ["report"], None),  # skills: null 全可见
    ]:
        r = load_expert("packs", role_name, "pentest")
        assert r["task_types"] == expected_types, role_name
        assert r["skills"] == expected_skills, role_name  # K1 绑定细粒度技能
    tt = load_task_types("packs", "pentest")
    assert tt["report"] == "passive"  # J 组新类型
    # 2026-09-21 红队向退场：内网/提权类型不再注册（内容归 redteam 轨）
    assert not {"privesc", "lateral-movement", "credential-access"} & set(tt)
    g = load_expert("packs", "_generalist", "pentest")
    assert g["skills"] is None and g["task_types"] is None  # 兜底专家不过滤


def test_real_ctf_experts_yaml():
    """ctf 轨专家 + 注册表可读（原 ctf/recon 已换 id 为 triage，§4.3 映射）。"""
    from core.skills.experts import load_expert

    assert load_task_types("packs", "ctf")["solve"] == "passive"
    g = load_expert("packs", "_generalist", "ctf")
    assert g.get("default_noise") == "passive"  # variant_ctf_default_noise 覆写
    for name in ("triage", "reverse"):
        load_expert("packs", name, "ctf")  # 存在且可解析
    # J 组（2026-09-20 开源对标扩充）：四个分类解题手（对标 CAI/EnIGMA 按类分工）
    solvers = {
        "web-solver": (["solve", "verify"], ["triage", "web-strike-entry", "web-injection", "web-authn-session"]),
        "pwn-solver": (["solve", "reverse", "verify"], ["file-triage", "binary-rev", "binary-pwn"]),
        "crypto-solver": (["solve", "verify"], ["crypto-triage", "file-triage"]),
        "forensics-solver": (["triage", "solve", "verify"],
                             ["forensics-triage", "misc-triage", "file-triage"]),
    }
    for name, (types, skills) in solvers.items():
        r = load_expert("packs", name, "ctf")
        assert r["task_types"] == types, name
        assert r["skills"] == skills, name
        assert r["default_noise"] == "passive", name
        assert r.get("persona"), name


def test_real_redteam_experts_j_batch():
    """redteam 镜像专家（osint/report-writer）+ doctor 对 tracks/ 与 experts/ 零 error。"""
    from core.skills.doctor import diagnose
    from core.skills.experts import load_expert

    assert load_task_types("packs", "redteam")["report"] == "passive"
    osint = load_expert("packs", "osint", "redteam")
    assert osint["task_types"] == ["recon", "asset-enum"]
    assert osint["skills"] == ["recon-asset-enum"]
    assert osint["default_noise"] == "passive"
    rw = load_expert("packs", "report-writer", "redteam")
    assert rw["task_types"] == ["report"] and rw["skills"] is None
    # 新角色全落位后 doctor 零 error（skills/task_types 引用不悬空）
    rep = diagnose(Path("packs"))
    bad = [i for i in rep.issues if i.level == "error"
           and ((i.target or "").replace("\\", "/").startswith("tracks/")
                or (i.target or "").replace("\\", "/").startswith("experts/"))]
    assert not bad, [(i.level, i.code, i.target) for i in bad]


def test_real_research_track_landed():
    """research 轨已落地（2026-09-14 P1）：全 passive 注册表 + reverse-analyst + doctor 零告警。"""
    from core.skills.doctor import diagnose
    from core.skills.experts import load_expert

    table = load_task_types("packs", "research")
    assert table == {"generic": "passive", "triage": "passive", "reverse": "passive",
                     "analyze": "passive", "verify": "passive",
                     "blueprint": "passive", "reconstruct": "passive"}  # R4 新增两类型
    analyst = load_expert("packs", "reverse-analyst", "research")
    assert analyst["skills"] == ["file-triage", "binary-rev", "android-rev"]  # android-kb-sourcing M1 挂载
    assert analyst["task_types"] == ["triage", "reverse", "analyze", "verify"]
    assert analyst["default_noise"] == "passive"
    assert analyst.get("persona")  # 数据纪律 persona 必填护栏
    auditor = load_expert("packs", "code-auditor", "research")  # J 组：代码审计员
    assert auditor["task_types"] == ["analyze", "verify"]
    assert auditor["skills"] == ["file-triage", "binary-rev"]
    g = load_expert("packs", "_generalist", "research")
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
    # web 关键词补强后，短 query 不再 0 命中；K1 细粒度技能（web-injection）与
    # 入口技能同分时，注入专精技能排前（同分按注册序/模块序稳定）
    hits = router.route(query="SQL 注入", packs={"web", "pentest"})
    assert hits and {h.skill.name for h in hits[:1]} & {"web-strike-entry",
                                                        "web-injection"}

    # 专家白名单引用真实存在的技能（不悬空；expert-pool M2）
    from core.skills.experts import load_expert
    names = set(by_name)
    for track, role in [("ctf", "triage"), ("ctf", "reverse")]:
        for s in (load_expert("packs", role, track).get("skills") or []):
            assert s in names, f"{track}/{role} 悬空引用 {s}"


# ---------- 路由增强（2026-09-18）：task_type 加分 / kb 索引 / doctor 值域 ----------

def test_router_task_type_bonus(packs):
    """task_type 命中 +10：弱文本匹配翻盘强关键词；不传 task_type 回落纯文本。"""
    reg = SkillRegistry(packs)
    reg.load()
    router = SkillRouter(reg)
    # 无 task_type：query 只命中 triage 的关键词「分诊」→ triage 胜
    hits = router.route(query="分诊", packs={"web", "pentest"}, top_k=3)
    assert hits[0].skill.name == "triage"
    # 传 task_type=exploit：file-upload-test/idor-test 各 +10 翻盘（文本分定先后）
    hits = router.route(query="分诊", packs={"web", "pentest"}, top_k=3,
                        task_type="exploit")
    assert hits[0].skill.name in {"file-upload-test", "idor-test"}
    top = hits[0]
    assert any(b["category"] == "task_type" and b["label"] == "任务类型"
               for b in top.breakdown)
    assert top.breakdown[0]["category"] == "task_type"  # 高权重排最前
    # task_type 不在技能 frontmatter → 不加分，仍纯文本
    hits = router.route(query="分诊", packs={"web", "pentest"}, top_k=3,
                        task_type="recon")
    assert hits[0].skill.name == "triage"


def test_doctor_skill_task_types_warning(packs):
    """技能 frontmatter task_types 越出所有轨注册表 → warning（拼错=加分静默失效）。"""
    from core.skills.doctor import diagnose
    bad = packs / "capabilities" / "web" / "skills" / "bad-tt"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\nname: bad-tt\ndescription: 坏类型技能\ntask_types: nosuchtype\n---\nx",
        encoding="utf-8")
    rep = diagnose(packs)
    hits = [i for i in rep.issues if i.code == "skill-tasktype-unregistered"]
    assert len(hits) == 1 and "nosuchtype" in hits[0].message
    assert hits[0].level == "warning"


# ---------- kbindex：中文标题索引与 2-gram 匹配 ----------

def _kb_fixture(tmp_path):
    """web 域 kb 源（M0 全局树 packs/kb/web/）：中文 H1 / frontmatter title /
    纯英文文件名 / .history 排除项。module 全局形态 web/poc/<文件>。"""
    root = tmp_path / "packs"
    kb = root / "kb" / "web"
    (kb / "poc").mkdir(parents=True)
    (kb / "poc" / "越权检测.md").write_text(
        "# 越权检测方法\n正文内容不应出现在提示行", encoding="utf-8")
    (kb / "poc" / "tduck.md").write_text(
        "---\ntitle: 问卷系统越权合集\n---\n正文", encoding="utf-8")
    (kb / "poc" / "buffer-overflow.md").write_text("Buffer Overflow notes",
                                                   encoding="utf-8")
    hist = kb / ".history"
    hist.mkdir()
    (hist / "越权检测.md").write_text("旧版本", encoding="utf-8")
    return root


def test_kbindex_build_and_match(tmp_path):
    from core.skills.kbindex import build_kb_index, match_kb_index
    root = _kb_fixture(tmp_path)
    index = build_kb_index(root, ["web"])
    assert len(index) == 3  # .history 排除
    by_module = {e.module: e for e in index}
    # module 全局形态 <域>/<快照>/<路径>（M0）；title 优先级：frontmatter title > H1 > stem
    assert by_module["web/poc/tduck.md"].title == "问卷系统越权合集"
    assert by_module["web/poc/越权检测.md"].title == "越权检测方法"
    assert by_module["web/poc/buffer-overflow.md"].title == "buffer-overflow"
    # 中文 2-gram 命中中文标题
    hits = match_kb_index(index, "检测 问卷系统越权")
    assert hits[0].module in {"web/poc/越权检测.md", "web/poc/tduck.md"}
    assert len(hits) <= 4
    # 英文整词命中英文文件名
    hits = match_kb_index(index, "buffer overflow 分析")
    assert hits[0].module == "web/poc/buffer-overflow.md"
    # 停用 2-gram（「任务」「测试」等泛词）不产生噪声命中
    assert match_kb_index(index, "这个任务需要测试") == []


def test_kbindex_mtime_rebuild(tmp_path):
    """kb 文件新增/修改后 mtime 快检触发重建（编辑当任务即生效）。"""
    from core.skills.kbindex import kb_module_hints
    root = _kb_fixture(tmp_path)
    assert kb_module_hints(root, ["web"], "越权")  # 首建缓存
    new = root / "kb" / "web" / "poc" / "新专题.md"
    new.write_text("# SSRF 检测手册\n内容", encoding="utf-8")
    hits = kb_module_hints(root, ["web"], "SSRF 检测")
    assert any(e.module == "web/poc/新专题.md" for e, _sec in hits)


def test_kbindex_sections_and_summary(tmp_path):
    """K3：H2 段落标题进索引并可定位命中段落；摘要=首个正文行截断。"""
    from core.skills.kbindex import build_kb_index, kb_module_hints
    root = _kb_fixture(tmp_path)
    kb = root / "kb" / "web" / "poc"
    (kb / "jwt-handbook.md").write_text(
        "---\ntitle: JWT 攻击手册\n---\n"
        "# JWT 攻击手册\n本篇覆盖 JWT 常见伪造路径。\n"
        "## none 算法\n把 alg 换成 none 绕过签名校验。\n"
        "## 弱密钥爆破\nhashcat 对 HS256 做字典爆破。\n"
        "## 密钥混淆\nRS256 转 HS256 的公钥滥用。\n",
        encoding="utf-8")
    index = build_kb_index(root, ["web"])
    e = next(x for x in index if x.module == "web/poc/jwt-handbook.md")
    assert e.sections == ("none 算法", "弱密钥爆破", "密钥混淆")
    assert e.summary.startswith("本篇覆盖 JWT 常见伪造路径")
    # H2 标题进 tokens：objective 提到「爆破」也能命中本篇，且定位到段落
    pairs = kb_module_hints(root, ["web"], "JWT 密钥爆破")
    assert any(x.module == e.module and sec == "弱密钥爆破" for x, sec in pairs)
    # 段落词命中但不带篇名时，仍给段落定位
    pairs2 = kb_module_hints(root, ["web"], "hashcat 爆破")
    assert any(sec == "弱密钥爆破" for _e, sec in pairs2)


def test_kbindex_cache_isolated_per_root(tmp_path):
    """不同 packs_root / capabilities 缓存互不串（键含两者）。"""
    from core.skills.kbindex import kb_module_hints
    root = _kb_fixture(tmp_path)
    assert kb_module_hints(root, ["web"], "越权")
    assert kb_module_hints(root, ["binary"], "越权") == []
    assert kb_module_hints(root, ["web"], "越权")


# ---------- kb 任务导航路由（kb/route.json，2026-09-19） ----------

def test_kb_route_hints(tmp_path):
    """route.json 静态路由（M0：落 packs/kb/<域>/，值=全局模块前缀带域打头）：
    键（|多选一）子串命中 task_type/query → 前缀组；缺文件/坏 JSON 静默降级。"""
    from core.skills.kbindex import kb_route_hints, load_kb_routes
    root = tmp_path / "packs"
    kbdir = root / "kb" / "web"
    kbdir.mkdir(parents=True)
    (kbdir / "route.json").write_text(json.dumps({
        "sql注入|sqli": ["web/ctf-web/sql-injection.md"],
        "越权|未授权|auth": ["web/ctf-web/auth-and-access.md", "web/ctf-web/auth-jwt.md"],
    }, ensure_ascii=False), encoding="utf-8")

    routes = load_kb_routes(root, ["web"])
    assert routes["越权|未授权|auth"] == ["web/ctf-web/auth-and-access.md",
                                          "web/ctf-web/auth-jwt.md"]
    # task_type 直配 + query 中文命中
    hits = kb_route_hints(root, ["web"], "auth_bypass", "检测后台越权")
    keys = [k for k, _ in hits]
    assert "越权|未授权|auth" in keys
    # cap 限组数
    hits = kb_route_hints(root, ["web"], None, "sql注入 auth 越权 未授权 sqli", cap=1)
    assert len(hits) == 1

    # 缺 route.json 的域 → 空（降级走 2-gram hints）
    assert kb_route_hints(root, ["binary"], None, "sql注入") == []
    # 坏 JSON 不抛
    (kbdir / "route.json").write_text("{broken", encoding="utf-8")
    assert kb_route_hints(root, ["web"], None, "sql注入") == []


# ---------- routeindex（v0.65 测试点路由索引；M0 全局单表） ----------

_INDEX = """\
# 注释行应被忽略
entries:
  - point: 文件上传测试
    match: [文件上传, upload]   # 行内注释
    kb: web/poc/越权检测.md
    tags: [file-upload-test]
  - point: 全员可见条目
    kb: web/poc/tduck.md
  - point: 他域条目
    kb: crypto/refs/rsa.md
"""


def test_routeindex_parse_trim_render(tmp_path):
    from core.skills.routeindex import load_route_index, parse_entries, render_route_index
    (tmp_path / "packs" / "kb").mkdir(parents=True)
    (tmp_path / "packs" / "kb" / "route_index.yaml").write_text(_INDEX,
                                                                encoding="utf-8")
    es = parse_entries(_INDEX)
    assert [(e.point, e.kb, e.tags) for e in es] == [
        ("文件上传测试", "web/poc/越权检测.md", ["file-upload-test"]),
        ("全员可见条目", "web/poc/tduck.md", []),
        ("他域条目", "crypto/refs/rsa.md", [])]
    assert es[0].match == ["文件上传", "upload"]
    # M0 域过滤：load_route_index 只回 kb 首段==cap 的条目
    assert [e.point for e in load_route_index(tmp_path / "packs", "web")] == \
        ["文件上传测试", "全员可见条目"]
    assert [e.point for e in load_route_index(tmp_path / "packs", "crypto")] == \
        ["他域条目"]
    # 裁剪：tags 交集
    r_strike = render_route_index(tmp_path / "packs", ["web"], ["file-upload-test"])
    assert "文件上传测试" in r_strike and "全员可见条目" in r_strike
    r_other = render_route_index(tmp_path / "packs", ["web"], ["idor-test"])
    assert "文件上传测试" not in r_other and "全员可见条目" in r_other
    r_none = render_route_index(tmp_path / "packs", ["web"], None)
    assert r_none.count("- ") == 2
    # 无全局索引文件 = 空串
    assert render_route_index(tmp_path / "packs2", ["web"], None) == ""


def test_routeindex_bad_yaml_returns_empty(tmp_path):
    from core.skills.routeindex import load_route_index, render_route_index
    kb = tmp_path / "packs" / "kb"
    kb.mkdir(parents=True)
    (kb / "route_index.yaml").write_text(
        "entries:\n  - point: 缺kb\n", encoding="utf-8")  # 缺必填字段
    assert load_route_index(tmp_path / "packs", "web") == []
    # 非法字段
    (kb / "route_index.yaml").write_text(
        "entries:\n  - point: x\n    kb: web/a.md\n    typo: 1\n", encoding="utf-8")
    assert load_route_index(tmp_path / "packs", "web") == []
    assert render_route_index(tmp_path / "packs", ["web"], None) == ""


def test_routeindex_mtime_rebuild(tmp_path):
    from core.skills.routeindex import load_route_index
    kb = tmp_path / "packs" / "kb"
    kb.mkdir(parents=True)
    f = kb / "route_index.yaml"
    f.write_text(_INDEX, encoding="utf-8")
    assert len(load_route_index(tmp_path / "packs", "web")) == 2
    f.write_text(_INDEX.replace("全员可见条目", "改名条目"), encoding="utf-8")
    es = load_route_index(tmp_path / "packs", "web")
    assert es[1].point == "改名条目"


def test_routeindex_read_kb_module(tmp_path):
    from core.skills.routeindex import read_kb_module
    root = _kb_fixture(tmp_path)
    body = read_kb_module(root, ["web"], "web/poc/越权检测.md", max_chars=10)
    assert body.startswith("# 越权检测方法")
    assert read_kb_module(root, ["web"], "web/poc/不存在.md") == ""


def test_doctor_route_index_checks(tmp_path):
    from core.skills.doctor import diagnose
    root = _kb_fixture(tmp_path)
    # 全局索引（M0 单表）：失效 kb 路径 + 不存在技能标签；条目 kb 带域前缀
    (root / "kb" / "route_index.yaml").write_text(
        "entries:\n"
        "  - point: 失效路径\n    kb: web/poc/没有.md\n"
        "  - point: 坏标签\n    kb: web/poc/tduck.md\n    tags: [ghost-skill]\n",
        encoding="utf-8")
    rep = diagnose(root)
    codes = {(i.code, i.message) for i in rep.issues}
    assert not any(c == "route-index-bad-yaml" for c, _ in codes)
    assert any(c == "route-index-kb-missing" and "失效路径" in m for c, m in codes)
    assert any(c == "route-index-skill-missing" and "ghost-skill" in m
               for c, m in codes)
    # 解析失败
    (root / "kb" / "route_index.yaml").write_text("entries:\n  - point: 缺kb\n",
                                                  encoding="utf-8")
    codes = {i.code for i in diagnose(root).issues}
    assert "route-index-bad-yaml" in codes


def test_proposals_index_edit_flow(tmp_path):
    """index 提案（M0 全局单表）：edit 走通（备份+落盘+域归并——content 只含本域
    条目，apply 与他域条目合并写回）；create 已退役被拒；条目须带本域域前缀。"""
    from core.skills import proposals
    root = _kb_fixture(tmp_path)
    (root / "capabilities" / "web").mkdir(parents=True)  # 提案归属校验要求能力包目录
    index = root / "kb" / "route_index.yaml"
    index.write_text("entries:\n"
                     "  - point: 原条目\n    kb: web/poc/tduck.md\n"
                     "  - point: 他域条目\n    kb: crypto/refs/rsa.md\n",
                     encoding="utf-8")
    new_content = ("entries:\n"
                   "  - point: 原条目\n    kb: web/poc/tduck.md\n"
                   "  - point: 新增条目\n    kb: web/poc/越权检测.md\n"
                   "    tags: [file-upload-test]\n")
    p = proposals.create_proposal(root, {
        "target": {"kind": "index", "cap": "web"}, "mode": "edit",
        "content": new_content, "summary": "索引加一条",
        "reason": "任务证据"}, origin="agent")
    assert p["status"] == "pending"
    detail = proposals.proposal_detail(root, p["id"])
    assert "新增条目" in detail["live"]["diff"]
    assert "他域条目" not in detail["live"]["diff"]  # 审批视野=本域过滤视图
    out = proposals.apply_proposal(root, p["id"], "human")
    assert out["result"]["backup"]
    text = index.read_text(encoding="utf-8")
    assert "新增条目" in text
    # 域归并：他域条目不被提案 content 覆盖丢失
    assert "他域条目" in text and "crypto/refs/rsa.md" in text
    # create 已退役（全局单表恒存在，只走 edit 增补）
    with pytest.raises(proposals.ProposalError):
        proposals.create_proposal(root, {
            "target": {"kind": "index", "cap": "web"}, "mode": "create",
            "content": new_content, "summary": "s", "reason": "r"}, origin="agent")
    # 条目 kb 缺本域域前缀被拒（防跨域越写）
    with pytest.raises(proposals.ProposalError):
        proposals.create_proposal(root, {
            "target": {"kind": "index", "cap": "web"}, "mode": "edit",
            "content": "entries:\n  - point: 越界\n    kb: poc/tduck.md\n",
            "summary": "s", "reason": "r"}, origin="agent")
    # 内容解析失败被拒
    with pytest.raises(proposals.ProposalError):
        proposals.create_proposal(root, {
            "target": {"kind": "index", "cap": "web"}, "mode": "edit",
            "content": "entries:\n  - point: 缺kb\n", "summary": "s",
            "reason": "r"}, origin="agent")
    # 能力包不存在被拒
    with pytest.raises(proposals.ProposalError):
        proposals.create_proposal(root, {
            "target": {"kind": "index", "cap": "nope"}, "mode": "edit",
            "content": new_content, "summary": "s", "reason": "r"}, origin="agent")


def test_proposals_skill_suggest_flow(tmp_path):
    """K4 技能拆分建议提案：mode=suggest 走通（批准后落技能目录 拆分建议.md，
    SKILL.md 本体不动）；mode 越权仍拒。"""
    from core.skills import proposals
    root = _kb_fixture(tmp_path)
    skill = root / "capabilities" / "web" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo 技能\n---\n正文", encoding="utf-8")
    before = (skill / "SKILL.md").read_text(encoding="utf-8")
    suggest_doc = "# 拆分建议\ndemo 建议拆成 demo-a 与 demo-b，理由……"
    p = proposals.create_proposal(root, {
        "target": {"kind": "skill", "skill_kind": "capability",
                   "owner": "web", "name": "demo"},
        "mode": "suggest", "content": suggest_doc,
        "summary": "demo 技能建议拆分", "reason": "r"}, origin="agent")
    assert p["status"] == "pending"
    out = proposals.apply_proposal(root, p["id"], "human")
    assert out["result"]["backup"] is None
    sug = skill / "拆分建议.md"
    assert sug.is_file() and "拆成 demo-a" in sug.read_text(encoding="utf-8")
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == before  # 本体不动
    # 二次 suggest 覆盖时自动备份
    p2 = proposals.create_proposal(root, {
        "target": {"kind": "skill", "skill_kind": "capability",
                   "owner": "web", "name": "demo"},
        "mode": "suggest", "content": "# 拆分建议 v2",
        "summary": "s", "reason": "r"}, origin="agent")
    out2 = proposals.apply_proposal(root, p2["id"], "human")
    assert out2["result"]["backup"]
    # 新建技能仍不允许
    with pytest.raises(proposals.ProposalError):
        proposals.create_proposal(root, {
            "target": {"kind": "skill", "skill_kind": "capability",
                       "owner": "web", "name": "ghost"},
            "mode": "create", "content": "---\nname: ghost\n---\nx",
            "summary": "s", "reason": "r"}, origin="agent")
