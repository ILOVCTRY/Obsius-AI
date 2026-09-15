"""Skill 路由器（DESIGN.md §4 + §4.5 + §6.6）。

组合关系：角色先窄化（白名单），路由器在窄域内按 特征/标签 + 关键词 评分。
评分：目标特征/文件特征/分类标签命中 ×3（对照表是主判据）> 关键词命中 ×2 > 描述 ×1。
候选集 = 项目启用能力包技能 ∪ 轨技能（packs 集合），默认排除 enabled:false。
"""

from dataclasses import dataclass, field

from core.skills.registry import SkillMeta, SkillRegistry


@dataclass
class RoutedSkill:
    skill: SkillMeta
    score: float
    matched: list[str] = field(default_factory=list)
    # 分类贡献：{类别: {"weight": 权重, "hits": [命中词]}}（C5 可观测）
    contrib: dict = field(default_factory=dict)

    @property
    def breakdown(self) -> list[dict]:
        """评分明细（route-preview/前端试算器展示为什么选中它）：
        features/file_features/labels +3、keywords +2、description +1。"""
        order = ("features", "file_features", "labels", "keywords", "description")
        labels_zh = {"features": "目标特征", "file_features": "文件特征",
                     "labels": "分类标签", "keywords": "关键词",
                     "description": "描述"}
        out = []
        for key in order:
            item = self.contrib.get(key)
            if item and item.get("hits"):
                out.append({"category": key, "label": labels_zh[key],
                            "weight": item["weight"], "hits": item["hits"],
                            "score": item["weight"] * len(item["hits"])})
        return out


class SkillRouter:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def route(
        self,
        query: str = "",
        features: list[str] | None = None,
        file_features: list[str] | None = None,
        labels: list[str] | None = None,
        role_skills: list[str] | None = None,
        packs: list[str] | set[str] | None = None,
        top_k: int = 3,
        include_disabled: bool = False,
    ) -> list[RoutedSkill]:
        """query=用户输入/目标描述。

        features      进站识别的 Web 目标特征（has_upload/returns_401…）。
        file_features 文件特征（ELF/NX/Canary/PE…），与 sk.file_features 匹配。
        labels        分类标签（platforms/formats/vuln_classes，§4.5.3）。
        role_skills   非空时只在白名单内路由（§6.6 软边界；白名单外走越界审批）。
        packs         允许的来源包（项目 caps ∪ track）；None=不限（设置页试算用）。
        """
        features = features or []
        file_features = file_features or []
        labels = labels or []
        pack_set = set(packs) if packs is not None else None
        q = query.lower()
        scored: list[RoutedSkill] = []
        for sk in self.registry.all():
            if not include_disabled and not sk.enabled:
                continue
            if pack_set is not None and sk.pack not in pack_set:
                continue
            if role_skills is not None and sk.name not in role_skills:
                continue
            score, matched = 0.0, []
            contrib = {k: {"weight": w, "hits": []} for k, w in (
                ("features", 3), ("file_features", 3), ("labels", 3),
                ("keywords", 2), ("description", 1))}
            for f in features:
                if f in sk.features:
                    score += 3.0
                    matched.append(f)
                    contrib["features"]["hits"].append(f)
            for ff in file_features:
                if ff in sk.file_features:
                    score += 3.0
                    matched.append(f"file:{ff}")
                    contrib["file_features"]["hits"].append(ff)
            for lb in labels:
                if lb in sk.labels:
                    score += 3.0
                    matched.append(f"label:{lb}")
                    contrib["labels"]["hits"].append(lb)
            for kw in sk.keywords:
                if kw.lower() in q:
                    score += 2.0
                    matched.append(kw)
                    contrib["keywords"]["hits"].append(kw)
            for word in sk.description.lower().split():
                if len(word) >= 4 and word in q:
                    score += 1.0
                    matched.append(word)
                    contrib["description"]["hits"].append(word)
                    break  # 描述命中最多计一次
            if score > 0:
                scored.append(RoutedSkill(sk, score, matched, contrib))
        scored.sort(key=lambda r: (-r.score, r.skill.name))
        return scored[:top_k]
