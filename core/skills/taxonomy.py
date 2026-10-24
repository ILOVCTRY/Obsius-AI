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

# 轨道 → kb 方向检索权重（**Agent 侧检索排序**专用，2026-10-23 Nan）。
#
# 为什么需要它：知识库对模型「默认全部打开」——可见范围不再按项目绑定裁剪，
# 相关性改由**轨加权**承担。渗透/红队轨把 web 域顶上去，逆向轨把二进制域顶上
# 去；CTF 轨**刻意不配**（= 全等权）——比赛题目跨域混杂，任何方向都可能是
# 那道题，提前压权重只会让冷门方向的正确手册搜不出来。
#
# 边界：只作用于 Agent 侧检索（kb_search 工具合并排序、顾问 kb 召回 hints）。
# 知识库工作台的正文搜索框与 route.json 确定性导航**不加权**（人工浏览/关键词
# 精确命中，加权只会让人看不出为什么排序是这样）。
# 未列出的轨/方向恒 1.0；改权重先改本表（护栏见 tests/test_kb_track_weight.py）。
TRACK_KB_DIRECTION_WEIGHTS: dict[str, dict[str, float]] = {
    "pentest": {"web": 2.0},
    "redteam": {"web": 2.0},
    "research": {"binary": 2.0},
}


def kb_direction_weight(track: str | None, direction: str | None) -> float:
    """(场景轨, kb 方向) → 检索权重；未配置的轨/方向恒 1.0（CTF 轨全等权）。"""
    if not track or not direction:
        return 1.0
    return TRACK_KB_DIRECTION_WEIGHTS.get(str(track).strip(), {}).get(
        str(direction).strip(), 1.0)

# 技能展示分组（专家池勾选清单按类别展示；**纯展示口径**，不参与加载/路由/校验）。
# 技能目录是扁平的（kb/skills/<name>），没有方向归属，故按命名约定归类；
# 新增技能若不套前缀会落到「其他」，不影响功能，只是显示位置靠后。
SKILL_GROUP_ORDER = ("分诊", "Web 攻击", "二进制与逆向", "密码学", "取证与隐写",
                     "杂项手法", "红队后渗透", "云与容器", "侦察", "专项逆向",
                     "蓝图与建模", "其他")
# 2026-10-19 技能名中文化：旧 ASCII 前缀规则（web-/binary-/rt-…）全部失效，
# 改按**中文名关键词**归类。同时保留旧 ASCII 前缀规则——存量项目/旧快照里
# 仍可能有 ASCII 技能名，两套并存才不会让老数据掉进「其他」。
#
# 顺序敏感（首个命中即返回）：
# - 后渗透排在最前，否则「Web后渗透」会被 Web 前缀吃掉（它属后渗透方向，
#   故显式列名——前缀匹配无法表达「中间含关键词」）；
# - 专项逆向必须排在二进制与逆向之前，否则「安卓逆向手法」被更宽的关键词吃掉；
# - 密码学/杂项必须显式成组，不能靠兜底（历史上 `endswith("手法")` 把它们全归了二进制）。
_SKILL_GROUP_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("rt-", "后渗透", "web后渗透", "内网", "横向", "权限提升"), "红队后渗透"),
    (("web-", "web", "外网打点"), "Web 攻击"),
    (("取证", "隐写", "forensics"), "取证与隐写"),
    (("密码学", "crypto", "crypt"), "密码学"),
    (("杂项", "misc"), "杂项手法"),
    (("edr", "安卓", "net逆向", "逆向重建"), "专项逆向"),
    (("binary-", "二进制", "pwn", "-rev"), "二进制与逆向"),
    (("cloud-", "云"), "云与容器"),
    (("recon-", "侦察", "资产"), "侦察"),
    (("blueprint-",), "蓝图与建模"),
)


def skill_group(name: str) -> str:
    """技能所属展示分组（按命名约定推断，未知回落「其他」）。

    ``*-triage`` / ``*分诊`` 一律归「分诊」——分诊是入口技能，跨域（web/binary/
    crypto/forensics/misc）共用一套判类流程，按域拆开反而看不出它是同一件事。

    规则里的键支持三种形态：普通子串前缀（``web-``/``云``）、``-xxx`` 后缀
    （``-rev`` 命中 ``android-rev``/``dotnet-rev``）、以及 ``xxx*`` 通配前缀。
    """
    n = (name or "").strip().lower()
    if n in ("triage", "总入口分诊") or n.endswith("-triage") or n.endswith("分诊"):
        return "分诊"
    for prefixes, group in _SKILL_GROUP_RULES:
        for p in prefixes:
            # ``-rev`` 这类以连字符开头的键是**后缀**语义：startswith 永远匹配不上，
            # 会让 android-rev / dotnet-rev 全落进「其他」（2026-10-20 修复）。
            if p.startswith("-"):
                if n.endswith(p):
                    return group
            elif n.startswith(p):
                return group
    return "其他"

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


def find_skill_dir(packs_root: str | Path, name: str) -> Path | None:
    """按技能名解析**已存在**的技能目录，找不到返回 None。

    2026-10-19 技能按阶段分组（``skills/01-入口分诊/<name>/``）后目录不再是
    扁平的 ``skills/<name>``，但 ``skill_dir()`` 一直按扁平拼接，导致
    ``/api/skills/catalog`` 估算 token 时读不到 SKILL.md 而 500（技能库整页
    空白、计数为 0）。

    同名技能可能出现在多个分组下（registry 的 ``load()`` 以后加载者覆盖），
    故取**最后一个**匹配，与注册表最终生效的那条保持一致。
    """
    root = skills_root(packs_root)
    if not root.is_dir():
        return None
    found: Path | None = None
    for md in sorted(root.rglob("SKILL.md")):
        if ".history" in md.parts:
            continue
        # 技能目录内部的 references/ 等资源区里的 SKILL.md 是资料不是技能
        if {"references", "scripts", "examples", "assets"} & set(
                md.relative_to(root).parts[:-1]):
            continue
        if md.parent.name == name:
            found = md.parent
    return found


def skill_dir(packs_root: str | Path, name: str) -> Path:
    """技能目录：已存在则按实际位置解析（含分组子目录），否则回落扁平落点。

    回落值只用于**新建**技能（``skills/<name>`` 是合法的注册位置，registry
    用 rglob 扫描）。读取/删除路径一律走已存在分支，故不会把分组技能误判成
    不存在。
    """
    found = find_skill_dir(packs_root, name)
    return found if found is not None else skills_root(packs_root) / name


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
