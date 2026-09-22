"""项目自主级别 / 会话上限 / 用量预算（批 2，DESIGN §6.8）。

唯一配置形态存于 project.config["autonomy"]（project.json 与黑板 projects 行双写）：
    {level, paused, sessions_cap, max_chain_ticks, token_budget, task_budget}

纪律：
- 任何档位都不放松安全层（net:real/越界/L3 审批永远强制，见 §7）；
- 闸门每次实时重读黑板 projects 行（降级/暂停/改预算即时生效），不缓存；
- sessions_cap 对人手开窗与编排开窗同效（资源硬上限，超=409/拒绝）；
- 预算硬闸只拦**自主动作**（orch 的 publish_task/spawn_session），人手动作仅警告；
- 80% 软警告 budget.soft_warning 持久去重（orchestrator_state.budget_warned），
  预算调大使用量回落到 80% 以下后自动复位，可再次报警。
"""

from typing import Any, Callable

from core.llm.provider import Usage

LEVELS = ("L0", "L1", "L2")

# 建项默认档（malware 轨落地前只给 L0；research 默认全 passive）
# R1（2026-09-17）：assessment 分解为 pentest/redteam——pentest 继承 L1、redteam=L0（宁严勿松）
TRACK_DEFAULT_LEVEL: dict[str, str] = {
    "ctf": "L0",
    "pentest": "L1",
    "redteam": "L0",
    "assessment": "L1",  # 旧 track 值（LEGACY_TRACK_MAP→pentest），兜底保留
    "research": "L1",
    "malware": "L0",
}

_DEFAULTS: dict[str, Any] = {
    "paused": False,
    "sessions_cap": 4,
    "max_chain_ticks": 3,
    "token_budget": None,   # None = 不限；否则正整数（token 总数）
    "task_budget": None,    # None = 不限；否则正整数（编排自主发布任务数）
    "max_concurrent_tasks": 3,  # v0.71 任务即窗口：自动挡同时执行任务数上限
}
CAP_MIN, CAP_MAX = 1, 20
SOFT_WARN_RATIO = 0.8


def default_level(track: str | None) -> str:
    return TRACK_DEFAULT_LEVEL.get(track or "", "L0")


def _pos_int_or_none(v: Any, field: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{field} 必须是正整数或 null（不限）")
    return v


def _bounded_int(v: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
        raise ValueError(f"{field} 必须是 {lo}..{hi} 的整数")
    return v


def normalize_autonomy(raw: dict | None, *, track: str | None = "ctf") -> dict:
    """校验并补齐 autonomy 配置；非法值抛 ValueError（API 层转 422）。

    旧项目无 autonomy 段时按场景轨给默认档（读时也走这里，所以旧库无感升级）。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("autonomy 必须是对象")
    level = raw.get("level", default_level(track))
    # C2 mission 自动派生开关（§6.9）：bool，缺省 False（显式开启才自动派生）
    auto_derive = raw.get("auto_derive", False)
    if not isinstance(auto_derive, bool):
        raise ValueError("auto_derive 须为布尔值")
    # v0.71 任务即窗口：自动挡同时执行任务数上限（独立于 sessions_cap——
    # 待命窗不耗 LLM，窗数资源上限与执行并发是两个旋钮）
    max_concurrent_tasks = _bounded_int(
        raw.get("max_concurrent_tasks", _DEFAULTS["max_concurrent_tasks"]),
        "max_concurrent_tasks", 1, CAP_MAX)
    if level not in LEVELS:
        raise ValueError(f"非法自主级别: {level}（合法: {LEVELS}）")
    paused = raw.get("paused", _DEFAULTS["paused"])
    if not isinstance(paused, bool):
        raise ValueError("paused 必须是布尔值")
    return {
        "level": level,
        "paused": paused,
        "sessions_cap": _bounded_int(
            raw.get("sessions_cap", _DEFAULTS["sessions_cap"]),
            "sessions_cap", CAP_MIN, CAP_MAX),
        "max_chain_ticks": _bounded_int(
            raw.get("max_chain_ticks", _DEFAULTS["max_chain_ticks"]),
            "max_chain_ticks", 1, CAP_MAX),
        "token_budget": _pos_int_or_none(raw.get("token_budget"), "token_budget"),
        "task_budget": _pos_int_or_none(raw.get("task_budget"), "task_budget"),
        "auto_derive": auto_derive,  # C2 mission 自动派生开关（§6.9）
        "max_concurrent_tasks": max_concurrent_tasks,  # v0.71 并发执行上限（缺省 3）
    }


def autonomy_of(config: dict | None, *, track: str | None = "ctf") -> dict:
    """从项目 config 读 autonomy 并归一化（无段/旧库 → 按轨默认档）。"""
    return normalize_autonomy((config or {}).get("autonomy"), track=track)


def normalize_rule_profiles(raw: Any) -> dict:
    """F11 rule_profiles 归一化：合法形态 {"owners": "*" | [tag…], "rating": [tag…]}。

    None / {} → {}（调用方剥键恢复缺省态）；owners 仅 "*" 或字符串列表
    （"all" 等其他字符串非法）；rating 仅字符串列表（空列表=关闭，合法）；
    tag strip 非空、去重保序；未知键剥除；非法抛 ValueError（API 层转 422）。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("rule_profiles 必须是对象")

    def _tags(v: list) -> list[str]:
        out: list[str] = []
        for x in v:
            t = x.strip()
            if t and t not in out:
                out.append(t)
        return out

    out: dict = {}
    if "owners" in raw:
        v = raw["owners"]
        if v == "*":
            out["owners"] = "*"
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            out["owners"] = _tags(v)
        else:
            raise ValueError('rule_profiles.owners 仅支持 "*" 或字符串列表')
    if "rating" in raw:
        v = raw["rating"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError("rule_profiles.rating 必须是字符串列表")
        out["rating"] = _tags(v)
    return out


# ---------- 用量记账 ----------

def total_tokens(row: dict) -> int:
    return (int(row.get("tokens_in", 0)) + int(row.get("tokens_out", 0))
            + int(row.get("tokens_cache_read", 0))
            + int(row.get("tokens_cache_creation", 0)))


def count_active_sessions(bb, project_id: str) -> int:
    """计数口径 = 黑板 sessions 行非 closed（计划批 2 明文；不关内存态的事）。"""
    return bb.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE project_id=? AND status!='closed'",
        (project_id,)).fetchone()["n"]


def record_llm_usage(
    bb, project_id: str, usage: Usage, *,
    source: str, session_id: str | None, model: str,
) -> dict | None:
    """一次 chat 后记账：原子累加 orchestrator_state + llm.usage 事件；
    跨 80% 阈值首发 budget.soft_warning（持久去重/回落复位）。全 0 用量跳过。

    配置在事务内随预算一起重读，保证警告与阈值一致。"""
    ti = int(getattr(usage, "input_tokens", 0) or 0)
    to = int(getattr(usage, "output_tokens", 0) or 0)
    cr = int(getattr(usage, "cache_read_tokens", 0) or 0)
    cc = int(getattr(usage, "cache_creation_tokens", 0) or 0)
    if not any((ti, to, cr, cc)):
        return None
    proj = bb.get_project(project_id)
    auto = autonomy_of(proj["config"], track=proj.get("track"))
    st = bb.usage_add_llm(project_id, ti=ti, to=to, cache_read=cr,
                          cache_creation=cc, token_budget=auto["token_budget"])
    used = total_tokens(st)
    bb.append_event(
        project_id, "llm.usage",
        {"source": source, "model": model or "",
         "input_tokens": ti, "output_tokens": to,
         "cache_read_tokens": cr, "cache_creation_tokens": cc,
         "total_tokens": used},
        session_id=session_id,
        # 对话化编排器（M1）：orchestrator-chat 与 tick 预算同池，作者同挂 orchestrator
        author=session_id or ("orchestrator" if source in ("orchestrator",
                                                           "orchestrator-chat") else "system"))
    if st.get("budget_warn") and auto["token_budget"]:
        bb.append_event(
            project_id, "budget.soft_warning",
            {"scope": "tokens", "used": used, "budget": auto["token_budget"],
             "pct": round(used / auto["token_budget"], 4),
             "note": "Token 用量已达预算 80%；预算硬闸只拦编排自主动作，人手动作仅警告。"},
            author="system")
    return st


def hard_block_reason(bb, project_id: str, action: str) -> str | None:
    """自主动作硬闸（orch publish_task/spawn_session 前调用；返回 None=放行）。

    每次重读配置；cap 对开窗动作生效，task_budget 只对发任务生效，
    token_budget 对两类自主动作都生效。"""
    proj = bb.get_project(project_id)
    auto = autonomy_of(proj["config"], track=proj.get("track"))
    if action == "spawn_session" and count_active_sessions(bb, project_id) >= auto["sessions_cap"]:
        return (f"活跃会话已达项目上限 sessions_cap={auto['sessions_cap']}"
                "（计数=非 closed 会话；关窗后可再开）")
    st = bb.usage_state_get(project_id)
    tb = auto["token_budget"]
    if tb and total_tokens(st) >= tb:
        return f"Token 预算已用尽（{total_tokens(st)}/{tb}），自主动作被硬闸拦截"
    if action == "publish_task":
        tkb = auto["task_budget"]
        if tkb and int(st.get("tasks_published", 0)) >= tkb:
            return f"自主任务预算已用尽（{st['tasks_published']}/{tkb}）"
    return None


def human_warning(bb, project_id: str) -> str | None:
    """人手动作（手动开窗等）的预算软警告文案：超 100% 才提示，不拦截。"""
    proj = bb.get_project(project_id)
    auto = autonomy_of(proj["config"], track=proj.get("track"))
    st = bb.usage_state_get(project_id)
    tb = auto["token_budget"]
    if tb and total_tokens(st) >= tb:
        return f"Token 预算已用尽（{total_tokens(st)}/{tb}）：人手动作放行，编排自主动作将被拦截"
    return None


ROE_KEYS = ("targets", "window", "exclusions", "approver")


def normalize_track_semantics(config: dict, *, track: str) -> dict:
    """轨级行为语义归一化（R2：mode 退役→轨级，§6.9 2026-09-17）。

    - config.mode 键退役：由调用方剥离（盘上旧值忽略）；
    - mission {text, criteria} 为两轨通用配置；
    - redteam 轨：redteam_roe 四要素**不再强制**——缺省=按 pentest 上限兜底
      （usage.roe_complete=False 提示补全，§6.9 ROE 语义）；传入即归一化，
      只保留非空键，四要素全非空才算完整（roe_complete）；
    - 非 redteam 轨：忽略 redteam_roe（行为本就是 pentest 上限）。"""
    out: dict = {}
    mission = config.get("mission")
    if isinstance(mission, dict) and (mission.get("text") or mission.get("criteria")):
        out["mission"] = {"text": str(mission.get("text") or ""),
                          "criteria": str(mission.get("criteria") or "")}
    if track == "redteam":
        roe = config.get("redteam_roe")
        if isinstance(roe, dict):
            out["redteam_roe"] = {k: str(roe.get(k) or "").strip() for k in ROE_KEYS
                                  if str(roe.get(k) or "").strip()}
    return out


def roe_complete(roe: dict | None) -> bool:
    """redteam ROE 四要素是否齐全（不齐全=行为按 pentest 上限兜底）。"""
    return bool(roe) and all(str(roe.get(k) or "").strip() for k in ROE_KEYS)


def usage_view(bb, project_id: str) -> dict:
    """GET 项目用量视图：autonomy 全字段 + 实时计数/百分比。"""
    proj = bb.get_project(project_id)
    track = proj.get("track") or "ctf"
    auto = autonomy_of(proj["config"], track=track)
    st = bb.usage_state_get(project_id)
    used = total_tokens(st)
    tb, tkb = auto["token_budget"], auto["task_budget"]
    published = int(st.get("tasks_published", 0))
    sem = normalize_track_semantics(proj["config"] or {}, track=track)
    roe = sem.get("redteam_roe")
    return {
        **auto,
        "auto_derive": bool(auto.get("auto_derive")),
        "criteria_template": str((proj["config"] or {}).get("criteria_template") or ""),
        "mission": sem.get("mission"),
        "redteam_roe": roe,
        "roe_complete": roe_complete(roe) if track == "redteam" else None,
        "active_sessions": count_active_sessions(bb, project_id),
        "llm_calls": int(st.get("llm_calls", 0)),
        "tokens": {"used": used, "budget": tb,
                   "pct": round(used / tb, 4) if tb else None},
        "tasks": {"published": published, "budget": tkb,
                  "pct": round(published / tkb, 4) if tkb else None},
        # mission 自动派生上次判定（2026-09-18）：last_result=published:n / empty / error:…
        "derive": {"last_at": st.get("last_derive_at") or "",
                   "last_result": st.get("last_derive_result") or ""},
    }


# Orchestrator 注入的两个回调类型（本模块定义避免循环 import）
Gate = Callable[[str], str | None]
"""gate(action) -> 拒绝原因串或 None；action='spawn_session'/'publish_task'。"""

OnTaskPublished = Callable[[], None]
"""orch 自主发布任务成功后的计数回调（task_budget 口径只计自主发布）。"""
