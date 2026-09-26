"""判据模板与判据解析（C2 作战模式自动派生，DESIGN.md §6.9）。

判据解析四层优先级（自动派生开启时永远有判据，不存在空转真空）：
1. 项目阶段目标 goal（meta.phase_goal，人类确认口径——goal 统一后的第一源）；
2. 项目手写 mission 判据（旧「作战计划」存量兼容，写入口已退役）；
3. 项目所选判据模板（用户模板）；
4. mode 内置默认模板（渗透/红队各一套，永远兜底）。

用户自定义模板存全局 `config/judgment_templates.json`（{name: criteria}），
与 providers.json 同款 cwd 相对路径惯例；全部函数可注入路径供测试。
"""

import json
from pathlib import Path

TEMPLATES_PATH = Path("config") / "judgment_templates.json"

BUILTIN_TEMPLATES: dict[str, str] = {
    "渗透默认": (
        "□ 已登记资产全部覆盖（uncovered=0，含 visited/scanning 状态推进）\n"
        "□ 每个 web 资产完成指纹与目录枚举\n"
        "□ 发现均 verified 或标注死路（false-positive 附原因）\n"
        "□ 输出结构化风险摘要与下一步建议"
    ),
    "红队默认": (
        "□ 拿到至少 1 台机器权限（verified 发现）\n"
        "□ 获取严重信息泄露（high+ verified 发现）\n"
        "□ 横向移动路径记录（findings/资产标注）\n"
        "□ ROE 范围内资产穷尽"
    ),
}


def templates_path(config_dir: str | Path = "config") -> Path:
    return Path(config_dir) / "judgment_templates.json"


def load_user_templates(config_dir: str | Path = "config") -> dict[str, str]:
    """读用户自定义模板；文件缺失/损坏返回 {}。"""
    path = templates_path(config_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_user_templates(config_dir: str | Path, templates: dict[str, str]) -> None:
    """整表写入用户模板（{name: criteria}，全部非空字符串）。"""
    path = templates_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {str(k).strip(): str(v) for k, v in (templates or {}).items()
             if str(k).strip() and str(v).strip()}
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_criteria(config: dict, config_dir: str | Path = "config",
                     track: str | None = None,
                     goal: dict | None = None) -> dict:
    """解析项目生效判据。返回 {source: goal|mission|template|builtin, criteria, name?}。
    优先级：阶段目标 goal（criteria 非空才算源）> 项目手写 mission（存量兼容）
    > 所选用户模板 > 轨内置默认模板。
    goal 传 meta.phase_goal（{criteria: [...]}，数组组装成多行文本）。
    track 给定时按轨选内置默认（redteam→红队默认，其余→渗透默认）；
    未给 track 时回退读 config.mode（旧调用兼容，mode 已退役仅兜底）。"""
    cfg = config or {}
    gl = goal or {}
    lines = [str(c).strip() for c in (gl.get("criteria") or []) if str(c).strip()]
    if lines:
        return {"source": "goal", "criteria": "\n".join(lines)}
    mission = cfg.get("mission") or {}
    if str(mission.get("criteria") or "").strip():
        return {"source": "mission", "criteria": str(mission["criteria"])}
    chosen = str(cfg.get("criteria_template") or "").strip()
    if chosen:
        user = load_user_templates(config_dir)
        if chosen in user and user[chosen].strip():
            return {"source": "template", "criteria": user[chosen], "name": chosen}
        if chosen in BUILTIN_TEMPLATES:  # 内置模板名被显式选定时精确命中（跨轨也尊重）
            return {"source": "builtin", "criteria": BUILTIN_TEMPLATES[chosen], "name": chosen}
    if track is not None:
        builtin_name = "红队默认" if track == "redteam" else "渗透默认"
    else:
        builtin_name = "红队默认" if cfg.get("mode") == "redteam" else "渗透默认"
    return {"source": "builtin",
            "criteria": BUILTIN_TEMPLATES[builtin_name],
            "name": builtin_name}
