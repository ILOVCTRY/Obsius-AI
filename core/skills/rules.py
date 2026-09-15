"""规则链与知识库源（DESIGN.md §4、§4.5、§6.6）。

优先级链：rules > SKILL.md > kb 知识库 > references。

- pack_rules(capabilities, track)：能力包红线 ∪ 轨红线，构建系统提示时全量注入。
- owner_rules(track, tags)：按资产 owner 叠加的更严规则（EDUSRC/OSRC/YSRC 模式）。
- role_rules(track, role)：角色专属红线（role-rules/<role>.md，仅绑定角色注入）。
- load_kb_sources(capabilities)：各能力包 kb_sources.json 合并（kb_open 消费）。

rules/*.md glob 不递归：owners/、role-rules/ 子目录天然不进全量注入。
"""

import json
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


def load_kb_sources(packs_root: str | Path, capabilities: list[str] | None) -> list[KbSource]:
    """合并启用能力包的 kb_sources.json。

    格式（每能力包一个）：
      {"sources": [{"id": "ctf-web", "root": "kb/ctf-web", "recursive": true}]}
    root 为相对能力包目录的路径；服务端解析为绝对路径，kb_open 负责防穿越。
    """
    out: list[KbSource] = []
    for cap in capabilities or []:
        cap_dir = capability_dir(packs_root, cap)
        f = cap_dir / "kb_sources.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for s in data.get("sources", []):
            sid, root = s.get("id"), s.get("root")
            if not sid or not root:
                continue
            root_path = Path(root)
            if not root_path.is_absolute():
                root_path = cap_dir / root_path
            out.append(KbSource(id=str(sid), root=root_path.resolve(),
                                recursive=bool(s.get("recursive", True))))
    return out


def build_rules_preamble(packs_root: str | Path, track: str | None = None,
                         capabilities: list[str] | None = None,
                         owner_tags: list[str] | None = None,
                         role: str | None = None) -> str:
    """系统提示规则前言：永久红线 + owner 叠加 + 角色红线。每次会话构建，优先级最高。"""
    parts: list[str] = ["# 场景规则与红线（永久强制，优先级最高）"]
    for name, text in pack_rules(packs_root, capabilities, track):
        parts.append(f"## rule:{name}\n{text}")
    for name, text in owner_rules(packs_root, track or "", owner_tags or []):
        parts.append(f"## rule:{name}（owner 叠加，压过通用默认）\n{text}")
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
