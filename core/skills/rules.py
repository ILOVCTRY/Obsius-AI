"""规则链与知识库源（DESIGN.md §4、§4.5、§6.6）。

优先级链：rules > SKILL.md > kb 知识库 > references。

2026-10-13 能力包退役后的规则布局（全部收在 `packs/kb/安全红线/`）：
- 轨红线：`安全红线/轨红线/<track>.md`（全量注入）。
- 平台规则：`安全红线/平台规则/<tag>.md`（owner 叠加，**渗透独有**）。
- 评级与价值口径：`安全红线/评级与价值口径/<tag>.md`（F11 判级依据，**渗透独有**）。
- 四段一体模板：`安全红线/templates/<tag>.md`。
- 角色专属红线 role-rules 已退役（原为空）。
- load_kb_sources(directions)：合成启用方向的 kb 源（`packs/kb/<方向>/`，kb_open 消费）。
"""

from dataclasses import dataclass
from pathlib import Path
import re

from core.skills.taxonomy import (
    platform_rules_dir, rating_dir, redline_path, rule_templates_dir,
)

# noise_caps 值域（与 tasks.noise_budget 同枚举）；语义=噪声上限档，叠加取最严
NOISE_LEVELS = ("passive", "low", "medium", "high")

# 平台规则 / 评级与价值口径仅对渗透轨生效
_PLATFORM_TRACK = "pentest"


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
    """返回 [(规则名, 正文)]：轨红线（`安全红线/轨红线/<track>.md`）。

    capabilities 入参保留兼容（能力包红线已退役，不再有包级红线）。"""
    out: list[tuple[str, str]] = []
    if track:
        text = _read_text_safe(redline_path(packs_root, track))
        if text is not None:
            out.append((f"track:{track}/redlines", text))
    return out


def owner_rules(packs_root: str | Path, track: str, owner_tags: list[str]) -> list[tuple[str, str]]:
    """平台规则（owner 叠加，渗透独有）：`安全红线/平台规则/<tag>.md`。"""
    if track != _PLATFORM_TRACK:
        return []
    base = platform_rules_dir(packs_root)
    out: list[tuple[str, str]] = []
    for tag in owner_tags:
        f = base / f"{tag}.md"
        if f.is_file():
            text = _read_text_safe(f)
            if text is not None:
                out.append((f"owner:{tag}", text))
    return out


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


def load_rule_templates(packs_root: str | Path, track: str | None = None) -> list[dict]:
    """规则模板库：`安全红线/templates/*.md` → [{name, meta, body}]（名称序）。

    坏 frontmatter 字段静默降级为空 meta（validate_rule_meta 由 doctor 显式调用
    出体检项，读取侧宁容错不阻断注入）。"""
    base = rule_templates_dir(packs_root)
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
    """评级与价值口径（F11，渗透独有）：`安全红线/评级与价值口径/<tag>.md`。"""
    if track != _PLATFORM_TRACK:
        return []
    base = rating_dir(packs_root)
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
    (生效 owners, 生效 ratings)。平台规则/评级与价值口径**渗透独有**，
    非渗透轨恒返回空。

    **勾哪个生效哪个**：owners/rating 都只认显式清单，未配（键缺失/None）= 不注入。
    只做文件存在性过滤，不存在的 tag 静默剔除；非法形态已在 projects 层归一化 422。
    `owner_tags` 保留入参以兼容既有调用签名（显式化后不再参与解析）。
    """
    if track != _PLATFORM_TRACK:
        return [], []
    profiles = rule_profiles or {}
    owner_base = platform_rules_dir(packs_root)
    eff_owners = [t for t in (profiles.get("owners") or [])
                  if (owner_base / f"{t}.md").is_file()]
    rating_base = rating_dir(packs_root)
    eff_ratings = [t for t in (profiles.get("rating") or [])
                   if (rating_base / f"{t}.md").is_file()]
    return eff_owners, eff_ratings


def rating_profile_active(packs_root: str | Path, track: str | None,
                          rule_profiles: dict | None) -> bool:
    """本项目是否**激活了「评级与价值口径（rating）」**——勾选且对应文件存在。

    与 `build_rules_preamble` 注入同源（都走 `resolve_rule_profiles`）：只有真正
    会被注入判级硬指令的项目才返回 True。用于「勾选才强制 rating_basis」类硬闸
    （2026-10-16）：未勾选的项目**不叠加 rating**，自然也不该被要求填依据。

    读取容错——任何异常按未激活返回 False（宁松勿误拒：硬闸误伤比漏放更糟）。"""
    try:
        _, eff_ratings = resolve_rule_profiles(
            packs_root, track or "", [], rule_profiles)
        return bool(eff_ratings)
    except Exception:  # noqa: BLE001 —— 判定失败按未激活
        return False


# ---------- 判级依据引用校验（rating-basis-gate v2，2026-10-17）----------

_RATING_REF_RE = re.compile(r"rating\s*[:：]\s*([A-Za-z0-9_.\-]+)", re.I)
# basis 里点名的「*rating*.md」文件名（旧副本 / 报告格式里的判级表都在这一类）
_RATING_DOC_RE = re.compile(r"([A-Za-z0-9_.\-]*rating[A-Za-z0-9_.\-]*)\.md", re.I)


def effective_rating_tags(packs_root: str | Path, track: str | None,
                          rule_profiles: dict | None) -> list[str]:
    """本项目**生效**的评级口径 tag（勾选且文件存在）；读取异常 → []。"""
    try:
        _, eff = resolve_rule_profiles(packs_root, track or "", [], rule_profiles)
        return list(eff)
    except Exception:  # noqa: BLE001 —— 判定失败按未激活
        return []


def validate_rating_basis(packs_root: str | Path, track: str | None,
                          rule_profiles: dict | None, basis: str | None
                          ) -> tuple[bool, str]:
    """`rating_basis` 是否**真的引用了本项目生效口径** → (通过, 打回理由)。

    与「非空」硬闸互补：非空只挡漏填，挡不住乱填。2026-10-17 实测有子专家写
    `评级与价值口径/edus-rating-rules.md`（该文件不存在，且引用的是已废止旧副本）
    照样落库；同时旧副本判级表把 low 判成了 high。故补这一层引用校验。

    判定（宁松勿误拒，任何读取异常按放行）：
    1. 未激活 rating → 放行（本闸只服务勾了口径的项目）；
    2. basis 含 `rating:<tag>` 且 tag 生效 → 通过；
    3. 未写前缀但正文出现生效 tag 名 → 通过（模型常写「edu-rating 低危#1 …」）；
    4. 前两条都不中、却点名了某个 `*rating*.md` 文件名 → 打回（典型：旧副本名）；
    5. 完全没提口径 → 打回，附正确写法。
    """
    text = str(basis or "").strip()
    tags = effective_rating_tags(packs_root, track, rule_profiles)
    if not tags or not text:
        return True, ""
    for tag in _RATING_REF_RE.findall(text):
        if tag.lower() in {t.lower() for t in tags}:
            return True, ""
    # tag 名必须独立成词：`edu-rating-rules` 里的 `edu-rating` 不算引用正本
    for tag in tags:
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(tag)}(?![A-Za-z0-9_-])", text, re.I):
            return True, ""
    docs = [m.lower() for m in _RATING_DOC_RE.findall(text)]
    hint = ("请写成「rating:%s <档位>#<条号> <一句话依据>」（生效 tag: %s）；"
            "判级条款只认 system 提示里注入的那份，勿引用旧副本或报告格式里的判级表。"
            % (tags[0], " / ".join(tags)))
    if docs:
        return False, (f"rating_basis 引用了非生效口径文件（{', '.join(d + '.md' for d in docs)}）"
                       f"——{hint}")
    if "redline" in text.lower() or "红线" in text:
        return False, f"rating_basis 引的是红线/证据纪律，不是判级口径——{hint}"
    return False, f"rating_basis 未引用本项目生效的评级口径——{hint}"


def load_kb_sources(packs_root: str | Path, directions: list[str] | None) -> list[KbSource]:
    """合成各启用方向的 kb 源（`packs/kb/<方向>/`）。

    每方向一个源 root=packs/kb/<方向>，域内多快照靠 module 路径前缀消歧；
    module 全局形态 `<方向>/<快照>/<包内路径>`，防穿越在 kb_open/resolve_kb 侧。
    """
    out: list[KbSource] = []
    for direction in directions or []:
        root = Path(packs_root) / "kb" / direction
        if root.is_dir():
            out.append(KbSource(id=f"{direction}-kb", root=root.resolve(), recursive=True))
    return out


def build_rules_preamble(packs_root: str | Path, track: str | None = None,
                         capabilities: list[str] | None = None,
                         owner_tags: list[str] | None = None,
                         role: str | None = None,
                         rule_profiles: dict | None = None) -> str:
    """系统提示规则前言：轨红线 + 平台规则（渗透）+ 评级口径（渗透）。每次会话构建，优先级最高。"""
    parts: list[str] = ["# 场景规则与红线（永久强制，优先级最高）"]
    for name, text in pack_rules(packs_root, capabilities, track):
        parts.append(f"## rule:{name}\n{text}")
    eff_owners, eff_ratings = resolve_rule_profiles(
        packs_root, track or "", owner_tags or [], rule_profiles)
    for name, text in owner_rules(packs_root, track or "", eff_owners):
        parts.append(f"## rule:{name}（平台规则叠加，压过通用默认）\n{text}")
    if eff_ratings:
        parts.append(
            "> **评级硬指令（F11）**：发布 bb_add_finding 时 severity 必须依据以下评级口径判级，"
            "rating_basis 字段填「规则名+条款+一句话依据」"
            "（如 `rating:edu-rating 高危#2 任意文件覆盖写`）；无对应条款的口径外判级视为违规。"
            "**定级以实证危害为准——证据不足以支撑高档的，降到证据能支撑的档，禁止就高凑档**"
            "（finding-severity-calibration 2026-10-09；服务端会对漏洞登记做定级校准复验，"
            "虚高自动降一档）。")
    for name, text in rating_rules(packs_root, track or "", eff_ratings):
        parts.append(f"## rule:{name}（评级与价值口径 · 判级依据）\n{text}")
    return "\n\n".join(parts)


def _read_md_dir(d: Path, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):  # 非递归：子目录不注入
        text = _read_text_safe(f)
        if text is not None:
            out.append((f"{prefix}{f.stem}", text))
    return out
