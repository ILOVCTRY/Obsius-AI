"""专家池加载（expert-pool 方案 §4.1-§4.2，DESIGN.md §6.6 专家池层）。

packs/experts/<id>.yaml 与角色 yaml 同约定：平铺 key: value、内联列表 [a, b]、
null = 不过滤（不支持嵌套结构，刻意保持扁平）。在角色字段之外新增三个语义段：

- tracks: [a, b]                  可服务轨域；**空/缺省 = 不启用**（2026-10-25 改）
- protected: true                 受保护不可删（仅 _generalist 置位）
- variant_<track>_<field>: …      轨变体字段级覆写——load_expert 按当前轨应用
                                  前缀键覆写，产出与角色同形状 dict，下游零改动

**tracks 语义 2026-10-25 反转（用户要求）**：此前「不选轨 = 全轨通用」，结果是
任何漏填 tracks 的新专家静默出现在所有轨的池里；现在「不选 = 不启用」——不勾
任何轨的专家不进任何轨的专家池、`expert_exists` 对任何轨都为 False（设置页左栏
标「未启用」）。因此**全域兜底专家必须显式列全四轨**（`_generalist` / `worker`
已补）；这也让「忘了勾轨」从静默放行变成显式失效，doctor 的
`expert-track-empty` warning 会把它顶到体检报告里。

M2 起（项目绑定）本模块是运行时唯一角色源：构造链 load_expert、发布链
expert_exists / allowed_roles、kb 方向面推导 directions_effective（§4.4/§4.6）。
2026-10-13 能力包退役后：专家 yaml 的 `skills` 白名单为**硬选**（会话可见/可路由
技能 = 并集），新增 `knowledge` 方向白名单（项目 kb 可见方向 = 并集）。
tracks/*/roles/ 已退役删除（git 历史可查）。
"""

from pathlib import Path

from core.skills.roles import _parse_inline_value
from core.skills.taxonomy import list_directions

GENERALIST = "_generalist"
_VARIANT_PREFIX = "variant_"

# 轨默认 kb 方向（2026-09-30）：research 轨 = binary（前端 research→rev-generic
# 逆向工作台口径）。项目无专家绑定时 directions_effective 按轨默认补全。
# pentest/ctf 无默认（存量直通语义不变）。
TRACK_DEFAULT_CAPS: dict[str, list[str]] = {"research": ["binary"]}


def serves_track(tracks: list[str] | None, track: str) -> bool:
    """专家是否服务该轨（tracks 的唯一判据，2026-10-25 收紧）。

    **空/缺省 = 不启用**（原口径是「缺省 = 全轨」）。三处调用点（`expert_exists`
    / `list_experts` / `doctor`）必须同口径，否则会出现「池里看得见但派不出去」
    或反过来的撕裂。
    """
    return bool(tracks) and track in tracks



def _parse_expert(path: Path) -> dict:
    """专家 yaml 极简解析（与 core.skills.roles 同约定）。

    额外支持 ``|``/``|-`` 块标量，用于保存多行 Markdown persona；块内容按
    YAML 的两空格缩进还原，避免把 ``persona: |-`` 当作字面量。
    """
    out: dict = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in {"|", "|-", "|+"}:
            block: list[str] = []
            i += 1
            while i < len(lines):
                child = lines[i]
                # 下一个顶层 key 开始，块标量结束；空行属于块内容。
                if child and not child.startswith((" ", "\t")):
                    break
                if child.startswith("  "):
                    block.append(child[2:])
                elif child.startswith("\t"):
                    block.append(child[1:])
                else:
                    block.append("")
                i += 1
            text = "\n".join(block)
            if value == "|":
                text += "\n"
            out[key] = text
            continue
        out[key] = _parse_inline_value(value)
        i += 1
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
    2026-10-25：tracks 缺省不再等于全轨（见 `serves_track`）——不勾轨的专家
    对任何轨都不存在。
    """
    path = Path(packs_root) / "experts" / f"{name}.yaml"
    if not path.is_file():
        return False
    return serves_track(_parse_expert(path).get("tracks"), track)


def list_experts(packs_root: str | Path, track: str | None = None) -> list[str]:
    """池内专家 id 清单（experts/*.yaml 的 stem，含 _generalist）。

    track 给定时只返回可服务该轨的专家（组队 UI 按轨过滤的数据源）。
    2026-10-25：缺省 tracks 的专家**不再**出现在任何轨的清单里（原口径是
    缺省=全轨，会让漏填 tracks 的专家静默进入所有轨的池）。
    """
    base = Path(packs_root) / "experts"
    if not base.is_dir():
        return []
    out: list[str] = []
    for p in sorted(base.glob("*.yaml")):
        if track is not None and not serves_track(
                _parse_expert(p).get("tracks"), track):
            continue
        out.append(p.stem)
    return out


def expert_skills(packs_root: str | Path, track: str,
                  experts: list[str] | None) -> list[str] | None:
    """绑定专家的技能白名单并集（§4.4 专家面，经当前轨变体）。

    **硬选口径（2026-10-13）**：会话可见/可路由技能 = 本并集；返回 None = 「不限定」
    （绑定清单为空，或任一绑定专家 skills=null 如 _generalist）。明确的 skills=[] = 不加载任何技能。
    绑定专家 yaml 被删时跳过该专家（宁严勿松：不静默回退 _generalist 扩权，doctor expert-* 体检兜底）。
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
        expert = _apply_variant(_parse_expert(path), track)
        if "skills" not in expert or expert.get("skills") is None:
            return None  # 缺省/null = 不限定（全量技能）
        skills = expert.get("skills")
        if not skills:
            continue  # 明确 [] = 不加载；与其他专家取并集
        out.update(skills)
    return sorted(out)


def expert_directions(packs_root: str | Path, track: str,
                      experts: list[str] | None) -> list[str] | None:
    """绑定专家的 kb 方向白名单并集（`knowledge` 字段，经当前轨变体）。

    返回 None = 「不限定」（绑定清单为空，或任一绑定专家 knowledge=null → 全部方向）。
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
        dirs = _apply_variant(_parse_expert(path), track).get("knowledge")
        if not dirs:  # null 或空列表 = 不限定（全部方向）
            return None
        out.update(dirs)
    return sorted(out)


def directions_effective(packs_root: str | Path, track: str,
                         experts: list[str] | None,
                         fallback: list[str] | None = None) -> list[str]:
    """项目 kb 可见方向推导（原 caps_effective；2026-10-13 能力包退役后按专家 knowledge）。

    - 有绑定专家：专家面 = knowledge 并集（含轨变体）；任一专家 knowledge=null = 全量
      → 全部 kb 方向目录（list_directions）。
    - 无绑定专家（存量项目零翻译）：fallback 直通；fallback 为空时按 TRACK_DEFAULT_CAPS
      轨默认补全（research→binary）。
    """
    bound = [e for e in (experts or []) if str(e).strip()]
    if not bound:
        return list(fallback or TRACK_DEFAULT_CAPS.get(track, []))
    dirs = expert_directions(packs_root, track, bound)
    if dirs is None:  # 绑定含全量专家（knowledge=null）
        return list_directions(packs_root)
    return dirs


def allowed_roles(packs_root: str | Path, track: str,
                  experts: list[str] | None) -> list[str]:
    """发布链路 allowed_roles（§4.6）：绑定专家清单优先；
    未绑定（存量项目）= 按轨过滤的池内专家——与退役前 list_roles 行为等价。"""
    bound = [e for e in (experts or []) if str(e).strip()]
    return bound or list_experts(packs_root, track)
