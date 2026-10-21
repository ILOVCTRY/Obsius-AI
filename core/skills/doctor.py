"""pack doctor：能力包/场景轨静态体检（设置页健康度与 scripts/pack_doctor.py 的共同后端）。

只读 packs，零 fastapi 依赖。三级结论：
- error   绑定断裂：角色引用不存在的技能、task_type 未注册、frontmatter name 与
           目录名不一致；专家池同型断裂（expert-skill-missing /
           expert-track-unknown / expert-generalist-missing）；场景档组队
           引用断裂或轨外（profile-expert-missing）；阶段剧本解析失败/
           剧本任务类型未注册/剧本角色不在池/next 指向不存在的阶段
           （phase-bad-yaml / phase-tasktype-unregistered /
           phase-expert-missing / phase-next-missing）；
           （expert-tasktype-unregistered 已随专家 task_types 字段退役移除，
           2026-10-21——该字段零运行时消费，再校验等于给死字段做体检。）
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
from core.skills.taxonomy import (
    GENERIC_TASK_TYPE, TRACKS, kb_root, list_directions, load_task_types,
    platform_rules_dir, rating_dir, redline_path, rule_templates_dir,
)

_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


def _track_dirs(root: Path) -> list[Path]:
    """场景轨目录（packs/kb/<track>/，2026-10-13 轨并入 kb 根）。"""
    base = kb_root(root)
    return [base / t for t in TRACKS if (base / t).is_dir()]

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
                    packs_root: Path, cap: str | None = None,
                    ) -> list[tuple[str, str, str]]:
    """返回 [(code, 模块, 说明)]：kb_open 指向未导入快照 / 引用模块文件不存在。
    cap=技能所属方向（快照目录名跨方向碰撞时先按引用方自身 kb 解析）。"""
    text = skill_path.read_text(encoding="utf-8")
    out: list[tuple[str, str, str]] = []
    for module in refs.extract_modules(text, snaps):
        verdict = refs.classify_module(module, snaps, packs_root, prefer_cap=cap)
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

    # task_type 值域并集（路由 task_type 加分的对侧防线：拼错=加分静默失效）
    registered_task_types: set[str] = set()
    for tdir in _track_dirs(root):
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
        for code, module, msg in _broken_kb_refs(sk.path, snaps, root):
            rep.issues.append(Issue("warning", code, _rel(sk.path, root), msg))

    # ---- 场景轨：task_type 注册、redlines（轨并入 packs/kb/）----
    for tdir in _track_dirs(root):
            track = tdir.name
            valid_types = load_task_types(root, track)
            if not (tdir / "task_types.yaml").is_file():
                rep.issues.append(Issue(
                    "warning", "missing-task-types", f"kb/{track}/task_types.yaml",
                    f"轨 {track} 无 task_types.yaml（只有内置 {GENERIC_TASK_TYPE} 合法）"))
            if not redline_path(root, track).is_file():
                rep.issues.append(Issue(
                    "warning", "missing-redlines", f"kb/安全红线/轨红线/{track}.md",
                    f"轨 {track} 缺 安全红线/轨红线/{track}.md"))
            # 平台规则/评级与价值口径/模板为渗透独有（全局单份），只体检一次
            if track == "pentest":
                if rating_dir(root).is_dir():
                    owners_dir = platform_rules_dir(root)
                    for rf in sorted(rating_dir(root).glob("*.md")):
                        if not (owners_dir / rf.name).is_file():
                            rep.issues.append(Issue(
                                "warning", "rating-without-owner", _rel(rf, root),
                                f"评级规则 评级与价值口径/{rf.stem} 无同名 平台规则/ 配套"))
                tpl_dir = rule_templates_dir(root)
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
                                f"规则模板 {tf.stem} frontmatter 缺 trigger 或与文件名不一致"))
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
        valid_tracks = {t for t in TRACKS if (kb_root(root) / t).is_dir()}
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
            # 专家 task_types 体检已移除（2026-10-21 字段退役）：任务认领机制
            # 2026-10-06 退役后该字段零运行时消费，再校验等于给死字段做体检。
        if GENERALIST not in expert_ids:
            rep.issues.append(Issue(
                "error", "expert-generalist-missing", "experts/_generalist.yaml",
                "专家池缺 _generalist 兜底专家（load_expert 回退依赖，必须存在）"))
        # ---- 场景档（expert-pool M4a）：组队引用可达且可服务本轨、board_view 值域 ----
        for prof in sorted(kb_root(root).glob("*/profiles/*.yaml")):
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

    # ---- kb 知识方向（能力包已退役：方向即 packs/kb/<方向>/ 目录本身）----
    kb_root_dir = kb_root(root)

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
        kb_doms = set(list_directions(root))
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
    # 2026-10-14 树状化重构：refs/ 已拆入各类目，上游原件现居
    # 「赛事专题/题目知识」（豁免项由 refs 段改为赛事专题段）；
    # 同属上游原料集合的「载荷与字典/工具手册」（CTF常用脚本及工具__*、工具使用__*
    # 扁平命名收编）一并豁免。
    if kb_root_dir.is_dir():
        for md in sorted(kb_root_dir.rglob("*.md")):
            if ".history" in md.parts or "kb-trash" in md.parts \
                    or "refs" in md.parts or "赛事专题" in md.parts \
                    or "载荷与字典" in md.parts or "工具手册" in md.parts:
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

    # ---- kb 长手册分段索引体检（progressive-entry，2026-10-21）----
    # 背景：`kb_open` 返回**全文**，不支持锚点定位。1000+ 行手册一开就吃掉
    # 整个上下文（图片隐写 1159 行 ≈ 40KB）。所以「渐进式入口」必须靠
    # `<同名>.idx.md` 分段索引 + skill 表里的 §/行号 指引。
    # 这里只查「长手册有没有配索引」，不查索引内容是否过期（行号漂移需人工）。
    _LONG_MANUAL_LINES = 400
    if kb_root_dir.is_dir():
        _missing_idx: list[tuple[int, str]] = []
        for md in sorted(kb_root_dir.rglob("*.md")):
            if ".history" in md.parts or "kb-trash" in md.parts \
                    or "赛事专题" in md.parts or "载荷与字典" in md.parts \
                    or "工具手册" in md.parts or md.name.endswith(".idx.md"):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            nlines = text.count("\n") + 1
            if nlines < _LONG_MANUAL_LINES:
                continue
            if md.with_name(md.stem + ".idx.md").is_file():
                continue
            _missing_idx.append((nlines, _rel(md, root)))
        # 只报最长的 5 篇 + 一条汇总：79 篇逐条列会淹没其它 info
        # （实测全库 79 篇，逐条刷屏 79 行，人不会读）
        if _missing_idx:
            _missing_idx.sort(reverse=True)
            for nlines, rel in _missing_idx[:5]:
                rep.issues.append(Issue(
                    "info", "kb-long-manual-no-index", rel,
                    f"长手册 {nlines} 行未配分段索引（同名 .idx.md）——"
                    "kb_open 返回全文会挤占上下文；建议按 H2 节列出行范围，"
                    "供 Agent 用 offset/limit 只读目标节"))
            if len(_missing_idx) > 5:
                rep.issues.append(Issue(
                    "info", "kb-long-manual-no-index-more", "",
                    f"另有 {len(_missing_idx) - 5} 篇长手册（≥{_LONG_MANUAL_LINES} 行）"
                    "同样未配分段索引；完整清单见 `scripts/pack_doctor.py --list-long`"))

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
        # 体检是全 packs 视角：枚举 kb 全部方向目录
        all_caps = list_directions(root)
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
