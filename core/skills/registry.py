"""Skill 注册表与解析（DESIGN.md §4、§4.5）。

Skill = 目录约定（SKILL.md frontmatter + 可选 helpers/examples）。
SKILL.md frontmatter 解析用极简 YAML（名字/关键词/特征平铺结构够用；
引入 PyYAML 后可换）。

扫描两个根（正交分类学）：
  packs/capabilities/<cap>/skills/<skill>/SKILL.md     kind=capability
  packs/tracks/<track>/skills/<skill>/SKILL.md         kind=track
kb/ 快照区下的 SKILL.md（如 ctf-skills 原件）不扫描——它们是资料不是路由技能。

frontmatter 约定：
---
name: sqli-test
description: SQL 注入测试方法论
keywords: 注入, sql, sqli, 搜索, 筛选
features: has_search, has_filter          # Web 目标特征（对应目标特征对照表）
file_features: ELF, NX, Canary            # 逆向文件特征（路由 ×3 加权）
platforms: linux, windows                 # 分类标签（§4.5.3，路由 ×3 加权）
formats: elf, pe
vuln_classes: stack, heap
task_types: exploit                        # 可认领的任务类型（角色过滤联动）
required_tools: sqlmap
enabled: true                              # false = 不参与路由（设置页可启停）
---
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.skills.taxonomy import CAPABILITIES_DIR, TRACKS_DIR


@dataclass
class SkillMeta:
    name: str
    pack: str                        # 来源包 slug（capability 或 track 名）
    kind: str                        # capability | track
    path: Path                       # SKILL.md 路径
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    file_features: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    vuln_classes: list[str] = field(default_factory=list)
    task_types: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def labels(self) -> list[str]:
        """全部分类标签（platforms/formats/vuln_classes）。"""
        return self.platforms + self.formats + self.vuln_classes

    def body(self) -> str:
        """frontmatter 之后的正文（注入 Agent 上下文的部分）。"""
        text = self.path.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                return parts[2].lstrip("\n")
        return text


def parse_frontmatter(text: str) -> dict[str, str]:
    """极简 frontmatter 解析：平铺 key: value + 块标量 |。嵌套结构不支持。"""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    meta: dict[str, str] = {}
    current_key: str | None = None
    block: list[str] = []
    for line in parts[1].splitlines():
        if re.match(r"^\s+#", line) or not line.strip():
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            if current_key and block:
                meta[current_key] = "\n".join(block).strip()
                block = []
            key, value = m.group(1), m.group(2)
            if value.strip() in {"|", "|-", ">"}:
                current_key, block = key, []
            else:
                meta[key] = value.strip().strip('"').strip("'")
                current_key = key if value.strip() == "" else None
        elif current_key is not None and line.startswith((" ", "\t")):
            block.append(line.strip())
    if current_key and block:
        meta[current_key] = "\n".join(block).strip()
    return meta


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in re.split(r"[,，]", value) if v.strip()]


def _is_enabled(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().lower() not in {"false", "no", "0", "off"}


class SkillRegistry:
    def __init__(self, packs_root: str | Path):
        self.packs_root = Path(packs_root)
        self._skills: dict[str, SkillMeta] = {}

    def load(self) -> int:
        """扫描 capabilities/*/skills 与 tracks/*/skills。返回技能数。

        同名技能以后加载者覆盖（track 在 capability 之后，轨级同名技能优先）；
        kb/ 区不在扫描范围（快照资料不进路由表）。"""
        self._skills.clear()
        root = self.packs_root
        if not root.is_dir():
            return 0
        patterns = [
            (CAPABILITIES_DIR, "capability"),
            (TRACKS_DIR, "track"),
        ]
        for group, kind in patterns:
            for skill_md in sorted(root.glob(f"{group}/*/skills/*/SKILL.md")):
                meta = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
                name = meta.get("name", skill_md.parent.name)
                rel = skill_md.relative_to(root).parts
                self._skills[name] = SkillMeta(
                    name=name,
                    pack=rel[1],
                    kind=kind,
                    path=skill_md,
                    description=meta.get("description", ""),
                    keywords=_split(meta.get("keywords")),
                    features=_split(meta.get("features")),
                    file_features=_split(meta.get("file_features")),
                    platforms=_split(meta.get("platforms")),
                    formats=_split(meta.get("formats")),
                    vuln_classes=_split(meta.get("vuln_classes")),
                    task_types=_split(meta.get("task_types")),
                    required_tools=_split(meta.get("required_tools")),
                    enabled=_is_enabled(meta.get("enabled")),
                )
        return len(self._skills)

    def get(self, name: str) -> SkillMeta | None:
        return self._skills.get(name)

    def all(self) -> list[SkillMeta]:
        return list(self._skills.values())
