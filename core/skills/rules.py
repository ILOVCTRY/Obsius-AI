"""规则链与知识库源（DESIGN.md §4、§4.5、§6.6）。

优先级链：rules > SKILL.md > kb 知识库 > references。

- pack_rules(capabilities, track)：能力包红线 ∪ 轨红线，构建系统提示时全量注入。
- owner_rules(track, tags)：按资产 owner 叠加的更严规则（EDUSRC/OSRC/YSRC 模式）。
- rating_rules(track, tags)：评级与价值口径（rating/<tag>.md，F11 判级依据注入）。
- resolve_rule_profiles(...)：rule_profiles 显式解析 → (生效 owners, 生效 ratings)。
- role_rules(track, role)：角色专属红线（role-rules/<role>.md，仅绑定角色注入）。
- parse_rule_doc / validate_rule_meta / load_rule_templates：四段一体模板
  （rules-four-section M1：frontmatter 结构化字段 + 正文；M2 接注入与实例层）。
- load_kb_sources(capabilities)：合成启用能力域的 kb 源（packs/kb/<域>/，kb_open 消费）。

rules/*.md glob 不递归：owners/、role-rules/、rating/ 子目录天然不进全量注入。
"""

from dataclasses import dataclass
from pathlib import Path

from core.skills.taxonomy import capability_dir, track_dir

# noise_caps 值域（与 tasks.noise_budget 同枚举）；语义=噪声上限档，叠加取最严
NOISE_LEVELS = ("passive", "low", "medium", "high")


@dataclass
class KbSource:
    id: str
    root: Path            # 已解析为绝对路径
    recursive: bool = True


def _read_text_safe(path: Path) -> str | None:
    """规则文件读取容错（2026-10-03）：glob 命中后文件被删/被占用（Windows
    AV/索引锁）时返回 None 跳过该文件，绝不把 OSError 抛给系统提示构建——
    规则链是增强注入，缺一条不应中断整轮对话。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def pack_rules(packs_root: str | Path, capabilities: list[str] | None = None,
               track: str | None = None) -> list[tuple[str, str]]:
    """返回 [(规则名, 正文)]：启用能力包 rules/*.md ∪ 场景轨 rules/*.md。"""
    out: list[tuple[str, str]] = []
    for cap in capabilities or []:
        out.extend(_read_md_dir(capability_dir(packs_root, cap) / "rules",
                                prefix=f"cap:{cap}/"))
    if track:
        out.extend(_read_md_dir(track_dir(packs_root, track) / "rules",
                                prefix=f"track:{track}/"))
    return out


def owner_rules(packs_root: str | Path, track: str, owner_tags: list[str]) -> list[tuple[str, str]]:
    """按 owner 标签叠加规则：tracks/<track>/rules/owners/<tag>.md。"""
    base = track_dir(packs_root, track) / "rules" / "owners"
    out: list[tuple[str, str]] = []
    for tag in owner_tags:
        f = base / f"{tag}.md"
        if f.is_file():
            text = _read_text_safe(f)
            if text is not None:
                out.append((f"owner:{tag}", text))
    return out


def role_rules(packs_root: str | Path, track: str, role: str) -> tuple[str, str] | None:
    """角色专属红线：tracks/<track>/rules/role-rules/<role>.md。不存在返回 None。"""
    f = track_dir(packs_root, track) / "rules" / "role-rules" / f"{role}.md"
    if f.is_file():
        text = _read_text_safe(f)
        if text is not None:
            return f"role:{role}", text
    return None


# ---------- 四段一体模板（rules-four-section M1，2026-09-23）----------

_VALID_META_KEYS = {"trigger", "scope", "forbidden", "noise_caps", "uncollectable", "rating_ref"}


def parse_rule_doc(path: str | Path) -> tuple[dict, str]:
    """规则/模板 md → (frontmatter meta, 正文)。frontmatter 缺失/坏 yaml → ({}, 原文)。

    宁容错：散文层永远可注入，结构化 meta 是增量红利不是前置条件。
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, ""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text  # 只有开栏没有闭栏 → 当无 frontmatter
    try:
        import yaml
        meta = yaml.safe_load(parts[1])
    except Exception:  # noqa: BLE001 —— 坏 yaml 容错为无 meta
        return {}, text
    return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n")


def validate_rule_meta(name: str, meta: dict) -> None:
    """frontmatter schema 校验（fail-fast，入库声明出错不静默吞——对齐 registry 惯例）。

    合法键：trigger / scope / forbidden / noise_caps / uncollectable / rating_ref。
    scope={in:[str], out:[str]}；forbidden/uncollectable=[str]；
    noise_caps={task_type: 档}，档 ∈ NOISE_LEVELS；rating_ref=str；trigger=str。
    """
    def _str_list(key: str, v) -> None:
        if not (isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)):
            raise ValueError(f"规则模板 {name}.{key} 须为非空字符串数组")

    unknown = set(meta) - _VALID_META_KEYS
    if unknown:
        raise ValueError(f"规则模板 {name} 含未知字段: {sorted(unknown)}")
    trig = meta.get("trigger")
    if trig is not None and not (isinstance(trig, str) and trig.strip()):
        raise ValueError(f"规则模板 {name}.trigger 须为非空字符串")
    scope = meta.get("scope")
    if scope is not None:
        if not isinstance(scope, dict):
            raise ValueError(f"规则模板 {name}.scope 须为对象")
        for k in scope:
            if k not in ("in", "out"):
                raise ValueError(f"规则模板 {name}.scope 含未知键 {k!r}（仅 in/out）")
            _str_list(f"scope.{k}", scope[k])
    for key in ("forbidden", "uncollectable"):
        if meta.get(key) is not None:
            _str_list(key, meta[key])
    caps = meta.get("noise_caps")
    if caps is not None:
        if not isinstance(caps, dict):
            raise ValueError(f"规则模板 {name}.noise_caps 须为对象")
        for k, v in caps.items():
            if v not in NOISE_LEVELS:
                raise ValueError(
                    f"规则模板 {name}.noise_caps[{k}] 须为 {'/'.join(NOISE_LEVELS)} 之一")
    ref = meta.get("rating_ref")
    if ref is not None and not (isinstance(ref, str) and ref.strip()):
        raise ValueError(f"规则模板 {name}.rating_ref 须为非空字符串")


def load_rule_templates(packs_root: str | Path, track: str) -> list[dict]:
    """轨模板库：tracks/<track>/rules/templates/*.md → [{name, meta, body}]（名称序）。

    坏 frontmatter 字段静默降级为空 meta（validate_rule_meta 由 doctor 显式调用
    出体检项，读取侧宁容错不阻断注入）。
    """
    base = track_dir(packs_root, track) / "rules" / "templates"
    out: list[dict] = []
    if not base.is_dir():
        return out
    for f in sorted(base.glob("*.md")):
        meta, body = parse_rule_doc(f)
        try:
            validate_rule_meta(f.stem, meta)
        except ValueError:
            meta = {}
        out.append({"name": f.stem, "meta": meta, "body": body})
    return out


def rating_rules(packs_root: str | Path, track: str, tags: list[str]) -> list[tuple[str, str]]:
    """评级与价值口径（F11）：tracks/<track>/rules/rating/<tag>.md，判级依据注入。"""
    base = track_dir(packs_root, track) / "rules" / "rating"
    out: list[tuple[str, str]] = []
    for tag in tags:
        f = base / f"{tag}.md"
        if f.is_file():
            text = _read_text_safe(f)
            if text is not None:
                out.append((f"rating:{tag}", text))
    return out


def resolve_rule_profiles(
    packs_root: str | Path, track: str, owner_tags: list[str],
    rule_profiles: dict | None,
) -> tuple[list[str], list[str]]:
    """rule_profiles 显式解析（F11；2026-10-01 去三态改纯显式）→
    (生效 owners, 生效 ratings)。

    **勾哪个生效哪个**：owners/rating 都只认显式清单，未配（键缺失/None）= 不注入。
    不再有「缺省=自动全注入 owner_tags」与「rating 自动跟随 owner 命中」——项目
    未配置过就不叠加任何 owner/rating 规则（能力包红线/轨红线/角色红线不经此处，
    仍恒注入）。

    - owners：键存在 = 显式清单 ∩ 文件存在；键缺失/None = 空。
    - rating：键存在 = 显式清单 ∩ 文件存在；键缺失/None = 空。
    只做文件存在性过滤，不存在的 tag 静默剔除；非法形态已在 projects 层归一化 422。
    `owner_tags` 保留入参以兼容既有调用签名（显式化后不再参与解析）。
    """
    profiles = rule_profiles or {}
    owner_base = track_dir(packs_root, track) / "rules" / "owners"
    eff_owners = [t for t in (profiles.get("owners") or [])
                  if (owner_base / f"{t}.md").is_file()]
    rating_base = track_dir(packs_root, track) / "rules" / "rating"
    eff_ratings = [t for t in (profiles.get("rating") or [])
                   if (rating_base / f"{t}.md").is_file()]
    return eff_owners, eff_ratings


def load_kb_sources(packs_root: str | Path, capabilities: list[str] | None) -> list[KbSource]:
    """合成各启用能力域的 kb 源（expert-pool M0：kb 全局单根 packs/kb/<域>/）。

    旧版读各包 kb_sources.json（树重组后该文件退役）；现按启用域直接合成，
    每域一个源 root=packs/kb/<域>，域内多快照靠 module 路径前缀消歧；
    module 全局形态 `<域>/<快照>/<包内路径>`，防穿越在 kb_open/resolve_kb 侧。
    """
    out: list[KbSource] = []
    for cap in capabilities or []:
        root = Path(packs_root) / "kb" / cap
        if root.is_dir():
            out.append(KbSource(id=f"{cap}-kb", root=root.resolve(), recursive=True))
    return out


def build_rules_preamble(packs_root: str | Path, track: str | None = None,
                         capabilities: list[str] | None = None,
                         owner_tags: list[str] | None = None,
                         role: str | None = None,
                         rule_profiles: dict | None = None) -> str:
    """系统提示规则前言：永久红线 + owner 叠加 + 评级口径 + 角色红线。每次会话构建，优先级最高。"""
    parts: list[str] = ["# 场景规则与红线（永久强制，优先级最高）"]
    for name, text in pack_rules(packs_root, capabilities, track):
        parts.append(f"## rule:{name}\n{text}")
    eff_owners, eff_ratings = resolve_rule_profiles(
        packs_root, track or "", owner_tags or [], rule_profiles)
    for name, text in owner_rules(packs_root, track or "", eff_owners):
        parts.append(f"## rule:{name}（owner 叠加，压过通用默认）\n{text}")
    if eff_ratings:
        parts.append(
            "> **评级硬指令（F11）**：发布 bb_add_finding 时 severity 必须依据以下评级口径判级，"
            "rating_basis 字段填「规则名+条款+一句话依据」"
            "（如 `rating:edu-rating 高危#2 任意文件覆盖写`）；无对应条款的口径外判级视为违规。")
    for name, text in rating_rules(packs_root, track or "", eff_ratings):
        parts.append(f"## rule:{name}（评级与价值口径 · 判级依据）\n{text}")
    if role:
        rr = role_rules(packs_root, track or "", role)
        if rr is not None:
            name, text = rr
            parts.append(f"## rule:{name}（角色专属红线）\n{text}")
    return "\n\n".join(parts)


def _read_md_dir(d: Path, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):  # 非递归：owners/role-rules 不注入
        text = _read_text_safe(f)
        if text is not None:
            out.append((f"{prefix}{f.stem}", text))
    return out
