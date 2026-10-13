"""能力域 × 场景轨分类学（DESIGN.md §4.5）。

- 项目绑定：project.json 的 track（单选）；**能力包（多选）已退役（2026-10-13）**，
  kb 可见方向改由绑定专家的 `knowledge` 白名单决定（见 core/skills/experts.py）。
- 物理布局：`packs/kb/` 为唯一内容根——
    packs/kb/skills/<skill>/          技能（扁平、Claude 风格，无包归属）
    packs/kb/<track>/                 场景轨（track.yaml/task_types/phases/profiles/dimensions）
    packs/kb/<方向>/                  kb 知识域（web/binary/crypto/forensics/misc/cloud）
    packs/kb/安全红线/{轨红线,平台规则,评级与价值口径,templates}/
- 任务类型注册表：`packs/kb/<track>/task_types.yaml`（极简行式 <type>: <noise>）。
"""

from pathlib import Path

KB_DIR = "kb"
SKILLS_DIR = "skills"
REDLINE_DIR = "安全红线"
TRACK_REDLINES = "轨红线"
PLATFORM_RULES = "平台规则"
RATING_DIR = "评级与价值口径"
RULE_TEMPLATES = "templates"

# packs/kb/ 下的保留目录（既非 kb 方向也非场景轨）
_KB_RESERVED = {SKILLS_DIR, REDLINE_DIR, "licenses"}

# 场景轨（唯一绑定维度）
TRACKS = ("ctf", "pentest", "redteam", "research")

# kb 知识方向（原能力包名，降级为知识组织单位）
KB_DIRECTIONS = ("web", "binary", "crypto", "forensics", "misc", "cloud")

# 旧平铺领域包 → (track, capabilities) 读兼容映射（§4.5.5）
# R1（2026-09-17）：assessment 轨分解为 pentest/redteam，本表目标轨同步更新
LEGACY_DOMAIN_MAP: dict[str, tuple[str, list[str]]] = {
    "pentest": ("pentest", ["web"]),
    "ctf": ("ctf", ["binary"]),
    "reverse": ("research", ["binary"]),
}

# 旧 track 值读兼容映射（R1：assessment 键退役 → pentest，盘上 project.json 不改）
LEGACY_TRACK_MAP: dict[str, str] = {
    "assessment": "pentest",
}

# 内置兜底任务类型（任何轨都合法，默认 passive）
GENERIC_TASK_TYPE = "generic"


def project_binding(meta: dict) -> tuple[str, list[str]]:
    """从 project.json meta 解析 (track, capabilities)。

    能力包退役后第二元素恒为空列表（保留元组形状兼容既有调用方）；track 值
    "assessment" 经 LEGACY_TRACK_MAP 映射为 pentest（盘上不改，读兼容）。"""
    track = meta.get("track")
    if track:
        t = str(track)
        return LEGACY_TRACK_MAP.get(t, t), []
    domain = meta.get("domain")
    if domain in LEGACY_DOMAIN_MAP:
        t, _cs = LEGACY_DOMAIN_MAP[domain]
        return t, []
    return LEGACY_TRACK_MAP.get(str(domain or "ctf"), str(domain or "ctf")), []


def kb_root(packs_root: str | Path) -> Path:
    return Path(packs_root) / KB_DIR


def skills_root(packs_root: str | Path) -> Path:
    """技能根：packs/kb/skills/（每技能一个子目录）。"""
    return kb_root(packs_root) / SKILLS_DIR


def skill_dir(packs_root: str | Path, name: str) -> Path:
    return skills_root(packs_root) / name


def track_dir(packs_root: str | Path, track: str) -> Path:
    return kb_root(packs_root) / track


def redlines_dir(packs_root: str | Path) -> Path:
    return kb_root(packs_root) / REDLINE_DIR / TRACK_REDLINES


def redline_path(packs_root: str | Path, track: str) -> Path:
    """轨红线文件：packs/kb/安全红线/轨红线/<track>.md。"""
    return redlines_dir(packs_root) / f"{track}.md"


def platform_rules_dir(packs_root: str | Path) -> Path:
    """平台规则（owner 叠加规则，渗透独有）：packs/kb/安全红线/平台规则/。"""
    return kb_root(packs_root) / REDLINE_DIR / PLATFORM_RULES


def rating_dir(packs_root: str | Path) -> Path:
    """评级与价值口径（渗透独有）：packs/kb/安全红线/评级与价值口径/。"""
    return kb_root(packs_root) / REDLINE_DIR / RATING_DIR


def rule_templates_dir(packs_root: str | Path) -> Path:
    """四段一体规则模板：packs/kb/安全红线/templates/。"""
    return kb_root(packs_root) / REDLINE_DIR / RULE_TEMPLATES


def list_directions(packs_root: str | Path) -> list[str]:
    """枚举 packs/kb/ 下的 kb 知识方向目录（排除保留目录与场景轨）。"""
    base = kb_root(packs_root)
    out: list[str] = []
    if not base.is_dir():
        return out
    reserved = _KB_RESERVED | set(TRACKS)
    for d in sorted(base.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and d.name not in reserved:
            out.append(d.name)
    return out


def list_packs(packs_root: str | Path, kind: str) -> list[dict]:
    """枚举场景轨元数据（kind: 'track'）。读 packs/kb/<track>/track.yaml 的
    label/description（缺失回退目录名）。kind='capability' 已随能力包退役返回空。"""
    if kind != "track":
        return []
    base = kb_root(packs_root)
    out: list[dict] = []
    if not base.is_dir():
        return out
    for name in TRACKS:
        d = base / name
        if not d.is_dir():
            continue
        meta_file = d / "track.yaml"
        label, description = name, ""
        if meta_file.is_file():
            for line in meta_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("label:"):
                    label = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"').strip("'")
        out.append({"name": name, "label": label, "description": description})
    return out


def load_task_types(packs_root: str | Path, track: str) -> dict[str, str]:
    """轨任务类型注册表：{type: 默认噪声预算}。generic 始终内置。

    文件缺失时只返回 generic（不炸开窗；pack doctor 负责报缺注册表）。"""
    table: dict[str, str] = {GENERIC_TASK_TYPE: "passive"}
    f = track_dir(packs_root, track) / "task_types.yaml"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            t, _, noise = line.partition(":")
            t, noise = t.strip(), noise.strip()
            if t and noise:
                table[t] = noise
    return table
