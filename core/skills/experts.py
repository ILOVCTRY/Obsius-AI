"""专家池加载（expert-pool 方案 §4.1-§4.2，DESIGN.md §6.6 专家池层）。

packs/experts/<id>.yaml 与角色 yaml 同约定：平铺 key: value、内联列表 [a, b]、
null = 不过滤（不支持嵌套结构，刻意保持扁平）。在角色字段之外新增三个语义段：

- tracks: [a, b]                  可服务轨域；缺省/null = 全轨
- protected: true                 受保护不可删（仅 _generalist 置位）
- variant_<track>_<field>: …      轨变体字段级覆写——load_expert 按当前轨应用
                                  前缀键覆写，产出与角色同形状 dict，下游零改动

M2 起（项目绑定）本模块是运行时唯一角色源：构造链 load_expert、发布链
expert_exists / allowed_roles、能力面推导 caps_effective（§4.4/§4.6）。
tracks/*/roles/ 已退役删除（git 历史可查）。
"""

from pathlib import Path

from core.skills.registry import SkillRegistry
from core.skills.roles import _parse_inline_value

GENERALIST = "_generalist"
_VARIANT_PREFIX = "variant_"


def _parse_expert(path: Path) -> dict:
    """专家 yaml 极简解析（与 core.skills.roles 同约定）。"""
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = _parse_inline_value(value)
    return out


def _apply_variant(expert: dict, track: str) -> dict:
    """应用轨变体覆写（§4.2）：variant_<track>_<field> -> <field>。

    只应用当前轨前缀键；其它轨的变体键一律剥除不透传。
    轨名不含下划线（ctf/pentest/redteam/research），前缀含尾下划线故无歧义。
    """
    prefix = f"{_VARIANT_PREFIX}{track}_"
    out = {k: v for k, v in expert.items() if not k.startswith(_VARIANT_PREFIX)}
    for key, value in expert.items():
        if key.startswith(prefix):
            out[key[len(prefix):]] = value
    return out


def load_expert(packs_root: str | Path, name: str, track: str) -> dict:
    """加载专家定义并应用当前轨变体（§4.2）。

    专家不存在时回退 _generalist（沿用 roles 回退语义；连兜底都没有则抛错）。
    """
    base = Path(packs_root) / "experts"
    path = base / f"{name}.yaml"
    if not path.is_file():
        path = base / f"{GENERALIST}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"专家与兜底专家均不存在: experts/{name}")
    return _apply_variant(_parse_expert(path), track)


def expert_exists(packs_root: str | Path, name: str, track: str) -> bool:
    """专家是否在池内真实存在且可服务该轨（§4.6 发布链路校验用，M2 接线）。

    不用 load_expert 判存在——它对缺失专家静默回退 _generalist，会放行拼错
    的专家名；track ∉ tracks 也不算存在（跨轨引用应拒）。
    """
    path = Path(packs_root) / "experts" / f"{name}.yaml"
    if not path.is_file():
        return False
    tracks = _parse_expert(path).get("tracks")
    return tracks is None or track in tracks


def list_experts(packs_root: str | Path, track: str | None = None) -> list[str]:
    """池内专家 id 清单（experts/*.yaml 的 stem，含 _generalist）。

    track 给定时只返回可服务该轨的专家（组队 UI 按轨过滤的数据源）。
    """
    base = Path(packs_root) / "experts"
    if not base.is_dir():
        return []
    out: list[str] = []
    for p in sorted(base.glob("*.yaml")):
        if track is not None:
            tracks = _parse_expert(p).get("tracks")
            if tracks is not None and track not in tracks:
                continue
        out.append(p.stem)
    return out


def expert_skills(packs_root: str | Path, track: str,
                  experts: list[str] | None) -> list[str] | None:
    """绑定专家的技能白名单并集（§4.4 专家面，经当前轨变体）。

    返回 None = 「不限定」，两情形：绑定清单为空（未绑定，存量语义）或任一
    绑定专家 skills=null（全量，如 _generalist）。caps_effective 会先判空绑定
    再调本函数，故它手里的 None 必是全量分支。
    绑定专家 yaml 被删时跳过该专家（宁严勿松：不静默回退 _generalist 扩权，
    doctor expert-* 体检兜底提示）。
    """
    bound = [e for e in (experts or []) if str(e).strip()]
    if not bound:
        return None
    base = Path(packs_root) / "experts"
    out: set[str] = set()
    for name in bound:
        path = base / f"{name}.yaml"
        if not path.is_file():
            continue
        skills = _apply_variant(_parse_expert(path), track).get("skills")
        if skills is None:
            return None
        out.update(skills)
    return sorted(out)


def caps_effective(packs_root: str | Path, track: str,
                   experts: list[str] | None,
                   fallback: list[str] | None = None) -> list[str]:
    """项目知识可见范围推导（§4.4 caps_effective，M2 构造链/meta 视图共用）。

    - 有绑定专家：专家面 = 各绑定专家 skills 并集（含轨变体）∪ 轨技能，取其中
      capability 类技能的所属包集合（轨技能 kind=track 不引包，恒在 {track} 内）；
      任一专家 skills=null = 全量 → 全部能力包目录（all_capability_packs）。
    - 无绑定专家（存量项目零翻译）：fallback（meta.capabilities）直通。
    找不到的技能名跳过（doctor expert-skill-missing 兜底）。
    """
    bound = [e for e in (experts or []) if str(e).strip()]
    if not bound:
        return list(fallback or [])
    names = expert_skills(packs_root, track, bound)
    if names is None:  # 绑定含全量专家（skills=null）
        return all_capability_packs(packs_root)
    if not names:
        return []
    reg = SkillRegistry(packs_root)
    reg.load()
    caps = {sk.pack for n in names if (sk := reg.get(n)) and sk.kind == "capability"}
    return sorted(caps)


def all_capability_packs(packs_root: str | Path) -> list[str]:
    """全部能力包目录名（_generalist 全量可见分支用）。"""
    caps_dir = Path(packs_root) / "capabilities"
    if not caps_dir.is_dir():
        return []
    return sorted(p.name for p in caps_dir.iterdir() if p.is_dir())


def allowed_roles(packs_root: str | Path, track: str,
                  experts: list[str] | None) -> list[str]:
    """发布链路 allowed_roles（§4.6）：绑定专家清单优先；
    未绑定（存量项目）= 按轨过滤的池内专家——与退役前 list_roles 行为等价。"""
    bound = [e for e in (experts or []) if str(e).strip()]
    return bound or list_experts(packs_root, track)
