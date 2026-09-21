"""规则链与知识库源（DESIGN.md §4、§4.5、§6.6）。

优先级链：rules > SKILL.md > kb 知识库 > references。

- pack_rules(capabilities, track)：能力包红线 ∪ 轨红线，构建系统提示时全量注入。
- owner_rules(track, tags)：按资产 owner 叠加的更严规则（EDUSRC/OSRC/YSRC 模式）。
- rating_rules(track, tags)：评级与价值口径（rating/<tag>.md，F11 判级依据注入）。
- resolve_rule_profiles(...)：rule_profiles 三态解析 → (生效 owners, 生效 ratings)。
- role_rules(track, role)：角色专属红线（role-rules/<role>.md，仅绑定角色注入）。
- load_kb_sources(capabilities)：合成启用能力域的 kb 源（packs/kb/<域>/，kb_open 消费）。

rules/*.md glob 不递归：owners/、role-rules/、rating/ 子目录天然不进全量注入。
"""

from dataclasses import dataclass
from pathlib import Path

from core.skills.taxonomy import capability_dir, track_dir


@dataclass
class KbSource:
    id: str
    root: Path            # 已解析为绝对路径
    recursive: bool = True


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
            out.append((f"owner:{tag}", f.read_text(encoding="utf-8")))
    return out


def role_rules(packs_root: str | Path, track: str, role: str) -> tuple[str, str] | None:
    """角色专属红线：tracks/<track>/rules/role-rules/<role>.md。不存在返回 None。"""
    f = track_dir(packs_root, track) / "rules" / "role-rules" / f"{role}.md"
    if f.is_file():
        return f"role:{role}", f.read_text(encoding="utf-8")
    return None


def rating_rules(packs_root: str | Path, track: str, tags: list[str]) -> list[tuple[str, str]]:
    """评级与价值口径（F11）：tracks/<track>/rules/rating/<tag>.md，判级依据注入。"""
    base = track_dir(packs_root, track) / "rules" / "rating"
    out: list[tuple[str, str]] = []
    for tag in tags:
        f = base / f"{tag}.md"
        if f.is_file():
            out.append((f"rating:{tag}", f.read_text(encoding="utf-8")))
    return out


def resolve_rule_profiles(
    packs_root: str | Path, track: str, owner_tags: list[str],
    rule_profiles: dict | None,
) -> tuple[list[str], list[str]]:
    """rule_profiles 三态解析（F11）→ (生效 owners, 生效 ratings)。

    - owners：键缺失或 "*" = 自动命中全注入（现行为）；清单 = 自动命中 ∩ 清单（可裁剪）。
    - rating：键缺失 = 自动（= 自动命中的 owner tags ∩ rating/ 文件存在，向后兼容）；
      键存在（含空列表）= 显式全集（空=关闭）——可含未自动命中的 tag（提前挂标准）。
    只做文件存在性过滤，不存在的 tag 静默剔除；非法形态已在 projects 层归一化 422。
    """
    profiles = rule_profiles or {}
    auto = [t for t in (owner_tags or [])]
    owners_cfg = profiles.get("owners", "*")
    if owners_cfg == "*":
        eff_owners = [t for t in auto
                      if (track_dir(packs_root, track) / "rules" / "owners" / f"{t}.md").is_file()]
    else:
        eff_owners = [t for t in owners_cfg if t in auto
                      and (track_dir(packs_root, track) / "rules" / "owners" / f"{t}.md").is_file()]
    rating_base = track_dir(packs_root, track) / "rules" / "rating"
    if "rating" not in profiles:
        eff_ratings = [t for t in eff_owners if (rating_base / f"{t}.md").is_file()]
    else:
        eff_ratings = [t for t in profiles["rating"] if (rating_base / f"{t}.md").is_file()]
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
        out.append((f"{prefix}{f.stem}", f.read_text(encoding="utf-8")))
    return out
