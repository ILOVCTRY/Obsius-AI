"""能力包 × 场景轨正交分类学（DESIGN.md §4.5）。

- 项目绑定：project.json 的 track（单选）+ capabilities（多选）；
  旧项目只有 domain 字段，经 LEGACY_DOMAIN_MAP 读兼容映射。
- 物理布局：packs/capabilities/<cap>/ 与 packs/tracks/<track>/。
- 任务类型注册表：tracks/<track>/task_types.yaml（极简行式 <type>: <noise>）。
"""

from pathlib import Path

CAPABILITIES_DIR = "capabilities"
TRACKS_DIR = "tracks"

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

    新字段优先；只有旧 domain 时走映射；track 值 "assessment" 经
    LEGACY_TRACK_MAP 映射为 pentest（盘上不改，读兼容）；未知 domain 轨名
    沿用、能力包为空（调用方负责提示/兜底）。"""
    track = meta.get("track")
    caps = meta.get("capabilities")
    if track and caps is not None:
        t = str(track)
        return LEGACY_TRACK_MAP.get(t, t), list(caps)
    if track:
        t = str(track)
        return LEGACY_TRACK_MAP.get(t, t), []
    domain = meta.get("domain")
    if domain in LEGACY_DOMAIN_MAP:
        t, cs = LEGACY_DOMAIN_MAP[domain]
        return t, list(cs)
    return LEGACY_TRACK_MAP.get(str(domain or "ctf"), str(domain or "ctf")), []


def capability_dir(packs_root: str | Path, cap: str) -> Path:
    return Path(packs_root) / CAPABILITIES_DIR / cap


def track_dir(packs_root: str | Path, track: str) -> Path:
    return Path(packs_root) / TRACKS_DIR / track


def list_packs(packs_root: str | Path, kind: str) -> list[dict]:
    """枚举能力包/场景轨元数据。kind: 'capability' | 'track'。

    读各包 <pack.yaml|track.yaml> 的 label/description（缺失回退目录名）。"""
    base = Path(packs_root) / (CAPABILITIES_DIR if kind == "capability" else TRACKS_DIR)
    out: list[dict] = []
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        meta_file = d / ("pack.yaml" if kind == "capability" else "track.yaml")
        label, description = d.name, ""
        if meta_file.is_file():
            for line in meta_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("label:"):
                    label = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"').strip("'")
        out.append({"name": d.name, "label": label, "description": description})
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
