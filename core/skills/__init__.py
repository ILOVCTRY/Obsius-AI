"""core.skills —— Skill 体系（DESIGN.md §4、§4.5）。

对外入口：
    from core.skills import SkillRegistry, SkillRouter, build_rules_preamble
"""

from core.skills.registry import SkillMeta, SkillRegistry
from core.skills.router import RoutedSkill, SkillRouter
from core.skills.rules import (
    KbSource,
    build_rules_preamble,
    load_kb_sources,
    owner_rules,
    pack_rules,
    rating_rules,
    resolve_rule_profiles,
    role_rules,
)
from core.skills.taxonomy import (
    GENERIC_TASK_TYPE,
    LEGACY_DOMAIN_MAP,
    list_packs,
    load_task_types,
    project_binding,
)

__all__ = [
    "SkillMeta",
    "SkillRegistry",
    "RoutedSkill",
    "SkillRouter",
    "pack_rules",
    "owner_rules",
    "rating_rules",
    "resolve_rule_profiles",
    "role_rules",
    "load_kb_sources",
    "KbSource",
    "build_rules_preamble",
    "project_binding",
    "list_packs",
    "load_task_types",
    "LEGACY_DOMAIN_MAP",
    "GENERIC_TASK_TYPE",
]
