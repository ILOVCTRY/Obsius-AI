"""pack doctor：能力包/场景轨静态体检（设置页健康度与 scripts/pack_doctor.py 的共同后端）。

只读 packs，零 fastapi 依赖。三级结论：
- error   绑定断裂：角色引用不存在的技能、task_type 未注册、frontmatter name 与
           目录名不一致、kb_sources 声明的 root 不存在；
- warning 软问题：角色引用已禁用技能、缺 redlines / task_types.yaml、kb_sources
           坏 JSON、技能正文引用的 kb 快照模块失效；
- info    编辑提示：孤儿技能（无角色显式引用）、.history/trash 有待清理项。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.skills import refs
from core.skills.roles import _parse_inline_value
from core.skills.registry import SkillRegistry, parse_frontmatter
from core.skills.taxonomy import GENERIC_TASK_TYPE, load_task_types

_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


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
                    caps_root: Path) -> list[tuple[str, str, str]]:
    """返回 [(code, 模块, 说明)]：kb_open 指向未导入快照 / 引用模块文件不存在。"""
    text = skill_path.read_text(encoding="utf-8")
    out: list[tuple[str, str, str]] = []
    for module in refs.extract_modules(text, snaps):
        verdict = refs.classify_module(module, snaps, caps_root)
        if verdict:
            out.append((verdict[0], module, verdict[1]))
    return out


def diagnose(packs_root: str | Path) -> DoctorReport:
    """对 packs 目录做全量体检。只读，不产生任何副作用。"""
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

    # ---- 技能自身：name 一致性、kb 引用失效、孤儿统计 ----
    referenced: set[str] = set()
    for sk in skills:
        meta = parse_frontmatter(sk.path.read_text(encoding="utf-8"))
        if "name" in meta and meta["name"] != sk.path.parent.name:
            rep.issues.append(Issue(
                "error", "skill-name-mismatch", _rel(sk.path, root),
                f"frontmatter name={meta['name']!r} 与目录名 {sk.path.parent.name!r} 不一致"))
        for code, module, msg in _broken_kb_refs(sk.path, snaps, caps_root):
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
            roles_dir = tdir / "roles"
            if roles_dir.is_dir():
                for rp in sorted(roles_dir.glob("*.yaml")):
                    role = _parse_role(rp)
                    if role.get("name") and role["name"] != rp.stem:
                        rep.issues.append(Issue(
                            "warning", "role-name-mismatch", _rel(rp, root),
                            f"yaml name={role['name']!r} 与文件名 {rp.stem!r} 不一致"))
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

    # ---- 能力包：redlines、kb_sources root ----
    if caps_root.is_dir():
        for cdir in sorted(p for p in caps_root.iterdir() if p.is_dir()):
            cap = cdir.name
            if not (cdir / "rules" / "redlines.md").is_file():
                rep.issues.append(Issue(
                    "warning", "missing-redlines", f"capabilities/{cap}/rules/redlines.md",
                    f"能力包 {cap} 缺 rules/redlines.md"))
            src_file = cdir / "kb_sources.json"
            if src_file.is_file():
                try:
                    data = json.loads(src_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    rep.issues.append(Issue(
                        "warning", "kb-sources-bad-json", _rel(src_file, root),
                        f"kb_sources.json 解析失败: {e}"))
                    data = None
                if isinstance(data, dict):
                    for src in data.get("sources", []):
                        kb_root = cdir / str(src.get("root", ""))
                        if not kb_root.is_dir():
                            rep.issues.append(Issue(
                                "error", "kb-source-root-missing", _rel(src_file, root),
                                f"知识源 {src.get('id')!r} 的 root 不存在: "
                                f"{src.get('root')!r}（应在 capabilities/{cap}/ 下）"))

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
