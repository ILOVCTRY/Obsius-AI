"""pack doctor：能力包/场景轨静态体检（设置页健康度与 scripts/pack_doctor.py 的共同后端）。

只读 packs，零 fastapi 依赖。三级结论：
- error   绑定断裂：角色引用不存在的技能、task_type 未注册、frontmatter name 与
           目录名不一致；专家池同型断裂（expert-skill-missing /
           expert-tasktype-unregistered / expert-track-unknown /
           expert-generalist-missing）；场景档组队引用断裂或轨外
           （profile-expert-missing）；阶段剧本解析失败/剧本任务类型未注册/
           剧本角色不在池/next 指向不存在的阶段（phase-bad-yaml /
           phase-tasktype-unregistered / phase-expert-missing / phase-next-missing）；
- warning 软问题：角色引用已禁用技能、缺 redlines / task_types.yaml、技能正文
           引用的 kb 模块失效、全局 route_index.yaml 解析失败或条目 kb 域前缀/
           模块路径失效/标签技能不存在；专家池软问题（expert-skill-disabled /
           expert-skill-ambiguous / expert-name-missing / role-rules-orphan）；
           场景档 board_view 值域外（profile-board-view-unknown）；阶段剧本
           软问题（phase-field-missing / phase-gate-unknown-key /
           phase-gatetype-unregistered）；kb 长手册缺段落结构
           （kb-structure-thin，experience-sedimentation M3 丁）；
- info    编辑提示：孤儿技能（无角色/专家显式引用）、能力包无 kb 域目录（占位包属
           预期）、.history/trash 有待清理项。
"""

from dataclasses import dataclass, field
from pathlib import Path

from core.phases import GATE_KEYS, parse_phase_file, phase_spec
from core.skills import refs
from core.skills.experts import GENERALIST, expert_exists
from core.skills.profiles import BOARD_VIEWS
from core.skills.roles import _parse_inline_value
from core.skills.registry import SkillRegistry, parse_frontmatter
from core.skills.rules import parse_rule_doc
from core.skills.taxonomy import GENERIC_TASK_TYPE, load_task_types

_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}

# 丁 kb 结构体检（experience-sedimentation M3）：长手册单段长文检测阈值
# （>800 字符且标题行 <2 才报；沉淀口径段要求在 agent 新建侧，见 proposals）
_KB_STRUCTURE_MIN_CHARS = 800


@dataclass
class Issue:
    level: str          # error | warning | info
    code: str           # 机器可读码（前端可据此跳转/过滤）
    target: str         # packs 相对定位（posix）
    message: str        # 中文说明


@dataclass
class DoctorReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {lvl: sum(1 for i in self.issues if i.level == lvl)
                for lvl in ("error", "warning", "info")}

    def to_dict(self) -> dict:
        return {"issues": [i.__dict__ for i in sorted(
                    self.issues, key=lambda x: (_LEVEL_ORDER[x.level], x.code, x.target))],
                "counts": self.counts}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# kb 引用形态（kb_open / 引号完整路径）与快照索引已统一到 core.skills.refs（C3）。
# doctor 只保留"判定失效并报 issue"这一层。

def _broken_kb_refs(skill_path: Path, snaps: dict[str, str],
                    caps_root: Path, cap: str | None = None,
                    ) -> list[tuple[str, str, str]]:
    """返回 [(code, 模块, 说明)]：kb_open 指向未导入快照 / 引用模块文件不存在。
    cap=技能所属能力包（快照目录名跨包碰撞时先按引用方自身 kb 解析）。"""
    text = skill_path.read_text(encoding="utf-8")
    out: list[tuple[str, str, str]] = []
    for module in refs.extract_modules(text, snaps):
        verdict = refs.classify_module(module, snaps, caps_root, prefer_cap=cap)
        if verdict:
            out.append((verdict[0], module, verdict[1]))
    return out


def diagnose(packs_root: str | Path,
             tools_root: str | Path | None = None) -> DoctorReport:
    """对 packs 目录做全量体检。只读，不产生任何副作用。

    tools_root 非空时追加工具链注册表体检（toolchain-registry M1：
    tool-missing info / tool-registry-invalid error）。"""
    root = Path(packs_root)
    rep = DoctorReport()
    if not root.is_dir():
        rep.issues.append(Issue("error", "packs-root-missing", "",
                                f"packs 目录不存在: {root}"))
        return rep

    reg = SkillRegistry(root)
    reg.load()
    skills = reg.all()
    snaps = refs.snapshot_index(root)
    caps_root = root / "capabilities"

    # task_type 值域并集（路由 task_type 加分的对侧防线：拼错=加分静默失效）
    registered_task_types: set[str] = set()
    tracks_base_all = root / "tracks"
    if tracks_base_all.is_dir():
        for tdir in sorted(p for p in tracks_base_all.iterdir() if p.is_dir()):
            registered_task_types |= set(load_task_types(root, tdir.name))

    # ---- 技能自身：name 一致性、kb 引用失效、孤儿统计 ----
    referenced: set[str] = set()
    for sk in skills:
        meta = parse_frontmatter(sk.path.read_text(encoding="utf-8"))
        if "name" in meta and meta["name"] != sk.path.parent.name:
            rep.issues.append(Issue(
                "error", "skill-name-mismatch", _rel(sk.path, root),
                f"frontmatter name={meta['name']!r} 与目录名 {sk.path.parent.name!r} 不一致"))
        for tt in sk.task_types or []:
            if tt not in registered_task_types:
                rep.issues.append(Issue(
                    "warning", "skill-tasktype-unregistered", _rel(sk.path, root),
                    f"技能 frontmatter task_types 含未注册类型: {tt}"
                    "（路由 task_type 加分不会命中，检查拼写或各轨 task_types.yaml）"))
        for code, module, msg in _broken_kb_refs(sk.path, snaps, caps_root,
                                                 cap=sk.pack):
            rep.issues.append(Issue("warning", code, _rel(sk.path, root), msg))

    # ---- 场景轨：角色绑定、task_type 注册、redlines、trash ----
    tracks_base = root / "tracks"
    if tracks_base.is_dir():
        for tdir in sorted(p for p in tracks_base.iterdir() if p.is_dir()):
            track = tdir.name
            valid_types = load_task_types(root, track)
            if not (tdir / "task_types.yaml").is_file():
                rep.issues.append(Issue(
                    "warning", "missing-task-types", f"tracks/{track}/task_types.yaml",
                    f"轨 {track} 无 task_types.yaml（只有内置 {GENERIC_TASK_TYPE} 合法）"))
            if not (tdir / "rules" / "redlines.md").is_file():
                rep.issues.append(Issue(
                    "warning", "missing-redlines", f"tracks/{track}/rules/redlines.md",
                    f"轨 {track} 缺 rules/redlines.md"))
            # F11：评级 tag 无授权边界配套（迁移后 edu-rating 等纯评级 tag 常驻此
            # warning 属预期——语义即「该评级口径无 owners 授权红线配套」）
            rating_dir = tdir / "rules" / "rating"
            if rating_dir.is_dir():
                owners_dir = tdir / "rules" / "owners"
                for rf in sorted(rating_dir.glob("*.md")):
                    if not (owners_dir / rf.name).is_file():
                        rep.issues.append(Issue(
                            "warning", "rating-without-owner", _rel(rf, root),
                            f"评级规则 rating/{rf.stem} 无同名 owners/ 授权边界配套"))
            # 四段一体模板（rules-four-section M1）：schema fail-fast + trigger 缺失提示
            tpl_dir = tdir / "rules" / "templates"
            if tpl_dir.is_dir():
                from core.skills.rules import validate_rule_meta as _vr_meta
                for tf in sorted(tpl_dir.glob("*.md")):
                    meta, _ = parse_rule_doc(tf)
                    try:
                        _vr_meta(tf.stem, meta)
                    except ValueError as e:
                        rep.issues.append(Issue(
                            "error", "rule-template-invalid", _rel(tf, root), str(e)))
                        continue
                    if str(meta.get("trigger") or "").strip() != tf.stem:
                        rep.issues.append(Issue(
                            "warning", "rule-template-no-trigger", _rel(tf, root),
                            f"规则模板 {tf.stem} frontmatter 缺 trigger 或与文件名不一致"
                            "（M2 选择器按 trigger 命中，缺了等于死件）"))
            roles_dir = tdir / "roles"
            if roles_dir.is_dir():
                for rp in sorted(roles_dir.glob("*.yaml")):
                    role = _parse_role(rp)
                    if not role.get("name"):
                        # name 行=中文显示名（可 ≠ 文件 stem，stem 才是角色 id），缺失才提示
                        rep.issues.append(Issue(
                            "warning", "role-name-missing", _rel(rp, root),
                            f"角色 {rp.stem} yaml 缺 name 行（显示名缺失，界面回退文件名）"))
                    for sk_name in role.get("skills") or []:
                        referenced.add(sk_name)
                        sk = reg.get(sk_name)
                        if sk is None:
                            rep.issues.append(Issue(
                                "error", "role-skill-missing", _rel(rp, root),
                                f"角色 {rp.stem} 引用了不存在的技能: {sk_name}"))
                        elif not sk.enabled:
                            rep.issues.append(Issue(
                                "warning", "role-skill-disabled", _rel(rp, root),
                                f"角色 {rp.stem} 引用了已禁用技能: {sk_name}（开窗时被静默过滤）"))
                    for tt in role.get("task_types") or []:
                        if tt not in valid_types:
                            rep.issues.append(Issue(
                                "error", "role-tasktype-unregistered", _rel(rp, root),
                                f"角色 {rp.stem} 的 task_type 未在轨注册表: {tt}"))
            # ---- 阶段剧本（pentest-phased-workflow M1）：解析/引用完整性 ----
            # 剧本任务条目缺 task_type/objective 由 phase_spec 整体拒收 → 落
            # phase-bad-yaml（不单列 task-missing-field 码，加载侧同口径跳过）
            phases_dir = tdir / "phases"
            if phases_dir.is_dir():
                phase_ids = {p.stem for p in phases_dir.glob("*.yaml")}
                for pp in sorted(phases_dir.glob("*.yaml")):
                    try:
                        spec = phase_spec(pp.stem, parse_phase_file(pp))
                    except ValueError as e:
                        rep.issues.append(Issue(
                            "error", "phase-bad-yaml", _rel(pp, root),
                            f"阶段剧本解析失败（加载侧跳过，该轨阶段机制缺此环节）: {e}"))
                        continue
                    for key in ("name", "goal"):
                        if not spec.get(key):
                            rep.issues.append(Issue(
                                "warning", "phase-field-missing", _rel(pp, root),
                                f"阶段 {pp.stem} 缺 {key}"
                                + ("（展示名回退阶段 id）" if key == "name" else "（系统提示无阶段目标）")))
                    for gk in spec.get("gate") or {}:
                        if gk not in GATE_KEYS:
                            rep.issues.append(Issue(
                                "warning", "phase-gate-unknown-key", _rel(pp, root),
                                f"阶段 {pp.stem} 的 gate 含未知指标: {gk}（合法: {sorted(GATE_KEYS)}，判定忽略）"))
                    for tt in spec.get("gate_types") or []:
                        if tt not in valid_types:
                            rep.issues.append(Issue(
                                "warning", "phase-gatetype-unregistered", _rel(pp, root),
                                f"阶段 {pp.stem} 的 gate_types 未在轨注册表: {tt}（门永不可达）"))
                    for t in spec.get("tasks") or []:
                        if t["task_type"] not in valid_types:
                            rep.issues.append(Issue(
                                "error", "phase-tasktype-unregistered", _rel(pp, root),
                                f"阶段 {pp.stem} 剧本任务的 task_type 未在轨注册表: {t['task_type']}（首发被拒收）"))
                        if t["role"] and not expert_exists(root, t["role"], track):
                            rep.issues.append(Issue(
                                "error", "phase-expert-missing", _rel(pp, root),
                                f"阶段 {pp.stem} 剧本任务角色 {t['role']} 不在专家池或不服务本轨"))
                    for nid in spec.get("next") or []:
                        if nid not in phase_ids:
                            rep.issues.append(Issue(
                                "error", "phase-next-missing", _rel(pp, root),
                                f"阶段 {pp.stem} 的 next 指向不存在的阶段: {nid}"
                                "（流转 API 422 / 自动过门无目标）"))

    # ---- 专家池（expert-pool M1）：skills 归属/跨包唯一、task_type 并集注册、tracks 合法性 ----
    # experts/ 目录缺省时整段跳过（专家池为可选层，M2 才接运行时）
    experts_base = root / "experts"
    if experts_base.is_dir():
        valid_tracks = ({p.name for p in tracks_base_all.iterdir() if p.is_dir()}
                        if tracks_base_all.is_dir() else set())
        # 跨包同名扫描必须绕开注册表去重（同名轨覆盖包后 all() 只剩一条）——
        # 直接扫 capabilities/tracks 双根取 frontmatter name 归包
        skill_packs: dict[str, set[str]] = {}
        for group in ("capabilities", "tracks"):
            for smd in root.glob(f"{group}/*/skills/*/SKILL.md"):
                nm = parse_frontmatter(smd.read_text(encoding="utf-8")).get("name") \
                    or smd.parent.name
                skill_packs.setdefault(nm, set()).add(smd.relative_to(root).parts[1])
        expert_ids: set[str] = set()
        expert_tracks: dict[str, list | None] = {}
        for ep in sorted(experts_base.glob("*.yaml")):
            expert_ids.add(ep.stem)
            expert = _parse_role(ep)
            expert_tracks[ep.stem] = expert.get("tracks")
            if not expert.get("name"):
                rep.issues.append(Issue(
                    "warning", "expert-name-missing", _rel(ep, root),
                    f"专家 {ep.stem} yaml 缺 name 行（显示名缺失，界面回退文件名）"))
            # variant_<track>_* 的 track 合法性（轨名不含下划线，取前缀首段）
            variant_tracks = {k[len("variant_"):].split("_", 1)[0]
                              for k in expert if k.startswith("variant_")}
            for tr in sorted({*(expert.get("tracks") or []), *variant_tracks}):
                if tr not in valid_tracks:
                    rep.issues.append(Issue(
                        "error", "expert-track-unknown", _rel(ep, root),
                        f"专家 {ep.stem} 引用未知轨域: {tr}"
                        "（tracks 字段或 variant_<track>_* 前缀）"))
            for sk_name in expert.get("skills") or []:
                referenced.add(sk_name)  # 专家引用也算可达（M2 后专家是唯一引用源）
                sk = reg.get(sk_name)
                if sk is None:
                    rep.issues.append(Issue(
                        "error", "expert-skill-missing", _rel(ep, root),
                        f"专家 {ep.stem} 引用了不存在的技能: {sk_name}"))
                elif not sk.enabled:
                    rep.issues.append(Issue(
                        "warning", "expert-skill-disabled", _rel(ep, root),
                        f"专家 {ep.stem} 引用了已禁用技能: {sk_name}"))
                packs_of = skill_packs.get(sk_name, set())
                if len(packs_of) > 1:
                    rep.issues.append(Issue(
                        "warning", "expert-skill-ambiguous", _rel(ep, root),
                        f"专家 {ep.stem} 引用的技能 {sk_name} 存在于多个能力包: "
                        f"{sorted(packs_of)}（归属不唯一，能力面推导将重复计入）"))
            for tt in expert.get("task_types") or []:
                if tt not in registered_task_types:
                    rep.issues.append(Issue(
                        "error", "expert-tasktype-unregistered", _rel(ep, root),
                        f"专家 {ep.stem} 的 task_type 未在任何轨注册表: {tt}"
                        "（专家跨轨服务，按各轨注册表并集体检）"))
        if GENERALIST not in expert_ids:
            rep.issues.append(Issue(
                "error", "expert-generalist-missing", "experts/_generalist.yaml",
                "专家池缺 _generalist 兜底专家（load_expert 回退依赖，必须存在）"))
        # role-rules 悬空（M2 roles 退役前按双源核对：文件既无同名专家也无同名角色
        # 时永远不会注入任何会话）
        role_ids: set[str] = set()
        for tdir in tracks_base.iterdir() if tracks_base.is_dir() else []:
            rd = tdir / "roles"
            if rd.is_dir():
                role_ids |= {p.stem for p in rd.glob("*.yaml")}
        for rr in sorted(tracks_base.glob("*/rules/role-rules/*.md")) if tracks_base.is_dir() else []:
            if rr.stem not in expert_ids and rr.stem not in role_ids:
                rep.issues.append(Issue(
                    "warning", "role-rules-orphan", _rel(rr, root),
                    f"role-rules/{rr.stem} 无同名专家或角色（该规则不会注入任何会话）"))
        # ---- 场景档（expert-pool M4a）：组队引用可达且可服务本轨、board_view 值域 ----
        for prof in sorted(tracks_base.glob("*/profiles/*.yaml")) if tracks_base.is_dir() else []:
            track_name = prof.relative_to(root).parts[1]
            p = _parse_role(prof)
            for ename in p.get("experts") or []:
                if ename not in expert_ids:
                    rep.issues.append(Issue(
                        "error", "profile-expert-missing", _rel(prof, root),
                        f"场景档 {prof.stem} 引用了不存在的专家: {ename}"))
                elif (ets := expert_tracks.get(ename)) is not None and track_name not in ets:
                    rep.issues.append(Issue(
                        "error", "profile-expert-missing", _rel(prof, root),
                        f"场景档 {prof.stem} 引用的专家 {ename} 不服务 {track_name} 轨"
                        f"（tracks: {ets}）"))
            if (bv := p.get("board_view")) and bv not in BOARD_VIEWS:
                rep.issues.append(Issue(
                    "warning", "profile-board-view-unknown", _rel(prof, root),
                    f"场景档 {prof.stem} 的 board_view 不在值域: {bv}（合法: {BOARD_VIEWS}）"))

    # ---- 能力包：redlines、kb 域目录 ----
    kb_root_dir = root / "kb"
    if caps_root.is_dir():
        for cdir in sorted(p for p in caps_root.iterdir() if p.is_dir()):
            cap = cdir.name
            if not (cdir / "rules" / "redlines.md").is_file():
                rep.issues.append(Issue(
                    "warning", "missing-redlines", f"capabilities/{cap}/rules/redlines.md",
                    f"能力包 {cap} 缺 rules/redlines.md"))
            # M0：kb 全局单根，五域知识域目录在 packs/kb/<cap>（占位包无 kb 属预期，info）
            if not (kb_root_dir / cap).is_dir():
                rep.issues.append(Issue(
                    "info", "kb-domain-missing", f"kb/{cap}",
                    f"能力包 {cap} 无 packs/kb/{cap}/ 知识域目录（占位包属预期）"))

    # ---- 全局 route_index.yaml 体检（M0 单表：解析失败 / kb 路径失效 / 标签技能不存在）----
    index_file = kb_root_dir / "route_index.yaml"
    if index_file.is_file():
        from core.skills import routeindex, writing as _writing
        try:
            entries = routeindex.parse_entries(
                index_file.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            rep.issues.append(Issue(
                "warning", "route-index-bad-yaml", _rel(index_file, root),
                f"route_index.yaml 解析失败（索引按空处理，不影响任务）: {e}"))
            entries = []
        kb_doms = ({p.name for p in kb_root_dir.iterdir() if p.is_dir()}
                   if kb_root_dir.is_dir() else set())
        skill_names = {s.name for s in skills}
        for e in entries:
            dom = e.kb.split("/", 1)[0]
            if dom not in kb_doms:
                rep.issues.append(Issue(
                    "warning", "route-index-kb-missing", _rel(index_file, root),
                    f"索引条目「{e.point}」的 kb 域前缀无效: {e.kb}"))
            else:
                try:
                    kb_ok = _writing.resolve_kb(root, dom, e.kb).path.is_file()
                except _writing.KbError:
                    kb_ok = False
                if not kb_ok:
                    rep.issues.append(Issue(
                        "warning", "route-index-kb-missing", _rel(index_file, root),
                        f"索引条目「{e.point}」的 kb 模块不存在: {e.kb}"))
            for tag in e.tags:
                if tag not in skill_names:
                    rep.issues.append(Issue(
                        "warning", "route-index-skill-missing",
                        _rel(index_file, root),
                        f"索引条目「{e.point}」的 tags 引用不存在技能: {tag}"
                        "（裁剪按空交集处理，该条目仅全放行角色可见）"))

    # ---- kb 手册结构体检（experience-sedimentation M3 丁；nuclei 质检门借鉴）----
    # 回扫口径（2026-09-22 实施校准，现网数据定标）：>800 字符且标题行（#/##/###…）
    # 少于 2 行=单段长文才报——H1 分节（payloader playbook 导入风格）/H2/H3 任一
    # 分节形态都算有结构；refs/ 子树豁免（上游快照原文不动是既有约定）；短条目/
    # 存根不检。沉淀口径段（已验证路径/坑/…）要求只约束 agent 新建侧
    # （proposals._validate_agent_kb），不强制存量手册改标题。
    if kb_root_dir.is_dir():
        for md in sorted(kb_root_dir.rglob("*.md")):
            if ".history" in md.parts or "kb-trash" in md.parts or "refs" in md.parts:
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if len(text) <= _KB_STRUCTURE_MIN_CHARS:
                continue
            if sum(1 for ln in text.splitlines() if ln.startswith("#")) >= 2:
                continue
            rep.issues.append(Issue(
                "warning", "kb-structure-thin", _rel(md, root),
                "长手册为单段长文（无任何标题分节）——建议按「适用条件/步骤/坑」"
                "加标题结构，便于路由命中与段落级引用（K3 hints 定位）"))

    # ---- 同义词表体检（M2 retrieval-upgrade，2026-09-23；反向校验防死组）----
    # 每个成员词对 kb 索引跑一次匹配：零命中的成员=空转词（扩展了也命不中任何
    # 文档），整组全空转=死组（词表条目纯占位）。词表缺失只提示不报错（增强层）。
    from core.skills.kbindex import (DEFAULT_HINT_CAP, build_kb_index,
                                     hit_synonym_groups, load_synonyms,
                                     match_kb_index, _query_segments)
    groups = load_synonyms(root)
    if not groups:
        if (root / "kb" / "synonyms.yaml").exists():
            rep.issues.append(Issue(
                "warning", "synonyms-empty", "kb/synonyms.yaml",
                "同义词表存在但解析不出任何组（groups 列表为空或格式不符）"))
        else:
            rep.issues.append(Issue(
                "info", "synonyms-missing", "kb/synonyms.yaml",
                "领域同义词表未配置（retrieval-upgrade M2 查询扩展层退化）——"
                "跨语言盲区（越权↔IDOR 类）靠它补，建议参照文档建组"))
    else:
        kb_root_dir2 = root / "kb"
        # capabilities=None 不展开任何源——体检是全 packs 视角，枚举 kb/ 全部
        # 域目录（licenses 是许可文档非能力域，排除）
        all_caps = ([d.name for d in kb_root_dir2.iterdir()
                     if d.is_dir() and d.name != "licenses"]
                    if kb_root_dir2.is_dir() else [])
        kb_index = build_kb_index(root, all_caps)
        for gid, terms in groups:
            dead: list[str] = []
            for term in terms:
                segs = _query_segments(term)
                if not segs:
                    dead.append(term)   # 切不出有效段（纯停用泛词）同样空转
                    continue
                if not match_kb_index(kb_index, term, cap=DEFAULT_HINT_CAP):
                    dead.append(term)
            if len(dead) == len(terms):
                rep.issues.append(Issue(
                    "warning", "synonyms-dead-group", f"kb/synonyms.yaml#{gid}",
                    f"同义词组「{gid}」全部成员对当前 kb 零命中（死组）——"
                    f"成员：{'、'.join(dead)[:100]}；确认 kb 域覆盖或删组"))
            elif dead:
                rep.issues.append(Issue(
                    "info", "synonyms-idle-term", f"kb/synonyms.yaml#{gid}",
                    f"组「{gid}」空转成员（kb 零命中，扩展无效）：{'、'.join(dead)}；"
                    "确认拼写/语料覆盖，或移出组防噪声"))

    # ---- 近重复技能（info；Jaccard ≥0.6 且共有词 ≥2，提示合并/区分边界）----
    enabled_skills = [s for s in skills if s.enabled]
    signatures = {
        s.name: {t.lower() for t in (s.keywords + s.features + s.file_features
                                     + s.labels)}
        for s in enabled_skills
    }
    seen_pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(enabled_skills):
        sa = signatures[a.name]
        for b in enabled_skills[i + 1:]:
            sb = signatures[b.name]
            if not sa or not sb:
                continue
            shared = sa & sb
            if len(shared) < 2:
                continue
            jaccard = len(shared) / len(sa | sb)
            if jaccard >= 0.6:
                pair = tuple(sorted((a.name, b.name)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                rep.issues.append(Issue(
                    "info", "skill-near-duplicate", _rel(a.path, root),
                    f"技能 {a.name} 与 {b.name} 特征高度重叠"
                    f"（Jaccard={jaccard:.2f}，共有 {len(shared)} 项："
                    f"{'、'.join(sorted(shared))[:80]}），考虑合并或在 description 区分边界"))

    # ---- 孤儿技能（info；仅编辑提示：没有角色显式挂它）----
    for sk in skills:
        if sk.enabled and sk.name not in referenced:
            rep.issues.append(Issue(
                "info", "orphan-skill", _rel(sk.path, root),
                f"技能 {sk.name} 未被任何角色显式引用（skills: null 的全放行角色仍可用它）"))

    # ---- 回收站（info）----
    for hist in root.glob("**/.history"):
        trash = hist / "trash"
        if trash.is_dir():
            items = [p for p in trash.iterdir() if not p.name.startswith(".")]
            if items:
                rep.issues.append(Issue(
                    "info", "trash-present", _rel(trash, root),
                    f"回收站有 {len(items)} 项可清理或恢复: "
                    + "、".join(sorted(p.name for p in items)[:5])
                    + ("…" if len(items) > 5 else "")))

    # ---- 工具链注册表（toolchain-registry M1，2026-09-23；tools_root=None 跳过）----
    # 缺失条目 info 级（按能力降级哲学，工具缺失不是错误；guide 文案随条目给出）；
    # registry 结构坏 = error（入库声明出错是 bug，fail-fast 显性化）。
    if tools_root is not None:
        try:
            from core.toolchain import load_tool_overrides, probe_tools as _registry_probe
            for row in _registry_probe(tools_root, overrides=load_tool_overrides()):
                if row["status"] == "ready":
                    continue
                rep.issues.append(Issue(
                    "info", "tool-missing", f"tools/registry.json#{row['name']}",
                    f"工具 {row['name']}（{row.get('kind')}）未检出——{row.get('detail') or '无指引'}"))
        except ValueError as e:
            rep.issues.append(Issue(
                "error", "tool-registry-invalid", "tools/registry.json", str(e)))
        except Exception as e:  # noqa: BLE001 —— 体检不阻断主流程
            rep.issues.append(Issue(
                "warning", "tool-registry-error", "tools/registry.json",
                f"registry 探测异常: {e}"))

    return rep


def _parse_role(path: Path) -> dict:
    """角色 yaml 极简解析（与 core.skills.roles 同约定）。"""
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = _parse_inline_value(value)
    return out
