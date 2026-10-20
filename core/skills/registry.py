"""Skill 注册表与解析（cc 风格技能目录）。

Skill = 自包含目录（SKILL.md + 可选 references/scripts/examples/assets）。
旧版只含路由正文的技能仍可被扫描，便于渐进迁移；新技能不再依赖
packs/kb 才能工作。
SKILL.md frontmatter 解析用极简 YAML（名字/关键词/特征平铺结构够用；
引入 PyYAML 后可换）。

扫描单根（2026-10-13 能力包退役，技能扁平化、无包归属）：
  packs/kb/skills/<skill>/SKILL.md
kb/ 快照区下其它目录（方向域/轨/安全红线）里的 SKILL.md 不扫描——它们是资料不是路由技能。

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
mode: self-contained                       # self-contained（默认）| legacy
---
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from core.skills.taxonomy import skills_root


@dataclass
class SkillMeta:
    name: str
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
    mode: str = "self-contained"

    @property
    def root(self) -> Path:
        """技能目录根；附属资源只能从这里解析。"""
        return self.path.parent

    @property
    def resources(self) -> list[Path]:
        """cc 约定资源清单，跳过隐藏目录和未知文件。"""
        allowed = {"references", "scripts", "examples", "assets"}
        out: list[Path] = []
        for dirname in allowed:
            base = self.root / dirname
            if not base.is_dir():
                continue
            root = self.root.resolve()
            out.extend(
                p for p in base.rglob("*")
                if p.is_file()
                and not p.is_symlink()
                and root in p.resolve().parents
                and not any(part.startswith(".")
                            for part in p.relative_to(self.root).parts)
            )
        return sorted(out, key=lambda p: p.relative_to(self.root).as_posix())

    @property
    def is_self_contained(self) -> bool:
        return self.mode not in {"legacy", "thin-route"}

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
        """扫描 packs/kb/skills 下的 SKILL.md（支持分组子目录）。返回技能数。

        **2026-10-19 支持分组**：技能目录可按阶段/方向分到子目录
        （``skills/02-方向深挖/Web注入手法/SKILL.md``），故用 ``rglob`` 而非
        ``glob("*/SKILL.md")``。排除规则：技能目录内部的 ``references/`` 等
        资源区里的 SKILL.md 属于资料不是技能，必须跳过——否则一个技能里放了
        参考用的 SKILL.md 就会被误注册成独立技能。

        同名技能以后加载者覆盖；kb 方向域/轨/安全红线区不在扫描范围。"""
        self._skills.clear()
        root = skills_root(self.packs_root)
        if not root.is_dir():
            return 0
        # 资源区名（与 skill_open 的白名单一致）：这些目录下的 SKILL.md 是资料
        _RESOURCE_DIRS = {"references", "scripts", "examples", "assets"}
        for skill_md in sorted(root.rglob("SKILL.md")):
            if ".history" in skill_md.parts:
                continue
            if _RESOURCE_DIRS & set(skill_md.relative_to(root).parts[:-1]):
                continue
            raw = skill_md.read_text(encoding="utf-8")
            meta = parse_frontmatter(raw)
            name = meta.get("name", skill_md.parent.name)
            self._skills[name] = SkillMeta(
                name=name,
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
                mode=(meta.get("mode") or meta.get("format") or
                      ("legacy" if ("kb_open" in raw or "知识库" in raw)
                       else "self-contained")).strip().lower(),
            )
        return len(self._skills)

    def get(self, name: str) -> SkillMeta | None:
        return self._skills.get(name)

    def all(self) -> list[SkillMeta]:
        return list(self._skills.values())
