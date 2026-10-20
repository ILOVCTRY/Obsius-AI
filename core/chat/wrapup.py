"""撞运行预算后的收尾阶段（settlement / wrap-up）。

对应上游 ``ARTEX-main/agent/wrapup.go`` 与 ``agent/worker.go:425``：

    撞步数上限（MaxTurns）或墙钟预算（MaxDuration）时，SDK 的 settlement
    阶段会注入这段提示，让 agent 先把已识别但未写回的内容落库、再输出一句
    总结，避免烂尾。

三条关键设计（照抄上游，缺一不可）：

1. **收尾提示词**：明确「不要再运行任何命令/探测」+「把已识别但还没写回的
   内容逐条写回」+「最后单独用一句话纯文本总结」。上游注释点明这句纯文本
   「会作为本次运行的结果展示」。
2. **收尾自身有独立轮数预算**（``wrapupTurnDefaults`` 各 agent 均 10 轮）：
   与主预算分离，保证收尾阶段有足够步数落库，不会被主预算的余量卡死。
3. **收尾阶段禁用探测类工具**（worker 传 ``DisabledTools=["Bash"]``）：只留
   落库类工具。上游原文——「禁用哪些工具、收尾自身给几轮预算属代码固定策略」，
   即这是**不可由用户配置改掉的 guardrail**：收尾只能捞回，不能借收尾继续打。

为什么必须有这一步：只把终态标成 exhausted 而不捞回，撞上限时「已识别但未
落库」的观察（资产、事实、漏洞）会随轮次结束一起蒸发——那正是探索里最贵的
部分（见过但没写下来的东西）。收尾阶段把这段损失降到最低。
"""
from __future__ import annotations

# 收尾阶段轮数预算（与主循环预算分离）。上游 wrapupTurnDefaults 各 agent 均 10。
WRAPUP_MAX_TURNS = 10

# 收尾阶段禁用工具的策略（**代码固定，不开放配置**）。
#
# 判据不是「按名字枚举工具」，而是「按**能力**归类」：探测类工具的共同点是
# 会对外部目标产生新动作（跑命令、发请求、点浏览器、反编译采样）。收尾阶段
# 只允许**写回已识别内容**的动作，所以按前缀/名称族整类剥离。
_WRAPUP_DENIED_TOOLS = frozenset({
    # 命令执行与文件探查
    "run_cmd", "read_file", "search_files", "unpack_attachment",
    # 浏览器族（browser_* 前缀统一处理，此处列出兜底）
    "browser_navigate", "browser_click", "browser_type", "browser_screenshot",
    "browser_content", "browser_back", "browser_replay",
    # 逆向采样族
    "decompile", "disasm", "list_symbols", "strings_search", "func_xrefs",
    # 知识检索（收尾不需要再查资料，避免用收尾时间读文档）
    "kb_search", "kb_open", "route_lookup", "skill_open",
    # 联网搜索
    "web_search",
    # CTF 候选字典生成（ctf-flag-channel）：收尾阶段不许再「展开候选表」——
    # 那是探索动作，不是写回。只产候选不执行，但仍会白烧收尾轮次。
    "ctf_ammo",
})

# 收尾阶段按前缀整类剥离的工具族（前缀 → 原因）。
_WRAPUP_DENIED_PREFIXES = (
    ("browser_", "浏览器探测"),
    ("mcp__", "外部 MCP 能力"),
)

# 收尾阶段**必须保留**的落库工具（即便命中上面的剥离规则也不许剥）。
# 显式白名单优于「默认放行」：收尾的价值全押在这几个动作上，任何一个被误剥
# 都会让收尾白跑，故写成正向清单。
_WRAPUP_KEEP_TOOLS = frozenset({
    "bb_add_asset", "bb_asset_status", "bb_add_finding", "bb_update_finding",
    "bb_add_artifact", "bb_upsert_func", "bb_delete_asset",
    "todo_write",
    # ARTEX 侧的写回通道（探索图 fact / 目标证明）
    "record_fact", "artex_goal", "artex_hint", "artex_status",
    "artex_worker_trace_list", "artex_worker_trace_get",
    "artex_worker_trace_search",
    # CTF flag 写回（ctf-flag-channel）：收尾阶段最该保的就是「把已解出的 flag
    # 落进台账」——它直接决定题目状态与任务终态。剥掉它等于让收尾白跑。
    "ctf_submit_flag",
    # CTF 整题放弃（ctf-abandon，2026-10-20）：收尾阶段**正是**该判「这题我打不
    # 完了」的时刻。此前这里没有放弃出口，模型只能去 bb_asset_status 标
    # tested_clean（CTF 轨不认这个状态，必撞门禁），再为凑 dead_end 查 event id
    # ——收尾 10 轮全烧在门禁往返上。
    "ctf_abandon",
    # CTF 主动收尾（ctf-conclude，2026-10-20）：收尾阶段同样是「答案已提交、
    # 该停止派工」最该被表达的时刻。剥掉它，主控在收尾时无法把
    # `meta.concluded` 落下去，题目会一直留在 partial 被反复派工。
    "ctf_conclude",
})

# ---------- 收尾提示词（按 agent 分） ----------

# worker：上游 settleWrapUpPrompt 逐字口径。
WRAPUP_PROMPT_WORKER = (
    "你即将因预算耗尽被终止。不要再运行任何命令/探测。请依次："
    "(1) 把你上面已识别但还没写回的内容逐条写回——新资产用 bb_add_asset、"
    "探索结论/事实用 record_fact（summary 一句话结论 + detail 细节 + evidence 一行"
    "证据 + confidence 标 observed/inferred；**一次探索汇成一条，别拆碎**，"
    "否定结论也要写）、确认漏洞用 bb_add_finding；"
    "(2) **最后单独用一句话纯文本**总结你做了什么、得到哪些关键结论"
    "（这句会作为本次运行的结果展示，务必输出）。"
)

# planner：上游 plannerWrapUpDefault 的语义——本轮结束 ≠ 任务终止，
# 系统之后还会唤醒，故不要求它收束整个规划。
WRAPUP_PROMPT_PLANNER = (
    "你本轮规划的步数即将用尽——注意只是【这一轮】结束，系统之后仍会随态势变化"
    "再次唤醒你继续规划，并非任务终止，你无需在此收束整个规划。"
    "请把本轮已经想清楚的结论落地、别让这一轮白跑，但也【不要为了收尾硬凑意图】"
    "（本轮 0 个意图仍是完全正常的结果）："
    "(1) 若已判断出【当前就该派发】的探索方向，用一次 artex_plan 批量提交；"
    "(2) 对已被某发现/事实证明达成的目标，用 artex_goal(action=\"prove\") 标记；"
    "(3) 若识别出需要分步的串行利用链，写进规划待办便于下次唤醒接着派。"
    "做完直接结束本轮，无需输出总结文本。"
)

# mainagent / 对话主控：面向人类的交互结束，只需一句话总结。
WRAPUP_PROMPT_MAINAGENT = (
    "你的步数即将用尽，本次交互就要结束。不要再发起新的探索/操作。"
    "请**单独用一句话纯文本**向用户总结当前进展、关键结论，以及建议的下一步。"
)

# 通用兜底（未命中的自定义 agent）。
WRAPUP_PROMPT_GENERIC = (
    "你即将因预算耗尽被终止。请先把已完成但未落库的结果写回，"
    "再**单独用一句话纯文本**总结你做了什么、得到哪些关键结论"
    "（这句会作为本次运行的结果展示）。"
)

# ---------- 任务级超时收尾词（artex-taskclock） ----------
#
# 上游 ``agent/wrapup.go`` 的 ``taskTimeoutWrapupDefaults``：与上面 per-run 的
# 四版是【两套】，语义常相反——
#
#   per-run：「你这一次 run 的预算用完了」（planner 版本还特意说「别停，接着
#   规划，系统还会唤醒你」）；
#   任务超时：「整个任务到点、即将结束」（planner 版本必须说「到点停止规划、
#   做最后判定，别再派意图」）。
#
# 用错一套的后果是反向的：任务到点时给 planner 说「别停继续规划」，它会派一批
# 永远不会被执行的新意图；per-run 耗尽时给 planner 说「别再派意图」，整个探索
# 就此停摆。故按**实际 reason** 现场挑，而不是按「是否夹逼」粗分。

WRAPUP_PROMPT_TASK_TIMEOUT_WORKER = (
    "**整个任务已到达超时上限，即将结束**（不是你这次 run 的预算，是整场探索"
    "到点了）。这是最后机会："
    "(1) 把你已识别但还没写回的内容【全部】落库——新资产用 bb_add_asset、"
    "探索结论/事实用 record_fact（含否定结论，confidence 如实标）、"
    "确认漏洞用 bb_add_finding；"
    "(2) 不要再启动任何新命令/探测；"
    "(3) **最后单独用一句话纯文本**总结你在本意图上的关键结论。"
)

WRAPUP_PROMPT_TASK_TIMEOUT_PLANNER = (
    "**整个任务已到达超时上限，即将结束**（不是本轮，是整个任务终止）。"
    "请基于当前【全部】事实与发现，做最后一次目标判定：对已被证据证明达成的"
    "目标用 artex_goal(action=\"prove\") 标记 met（别漏判）。"
    "**不要再生成任何新意图**（此时派意图也不会再被执行）。"
    "判定完即收束，无需输出总结文本。"
)


def wrapup_prompt(agent_key: str, *, is_orchestrator: bool = False) -> str:
    """按 agent 取收尾提示词（DB 覆盖暂不开放，用内置默认）。"""
    key = str(agent_key or "").strip()
    if key == "worker":
        return WRAPUP_PROMPT_WORKER
    if key == "planner":
        return WRAPUP_PROMPT_PLANNER
    if is_orchestrator:
        return WRAPUP_PROMPT_MAINAGENT
    return WRAPUP_PROMPT_GENERIC


def task_timeout_prompt(agent_key: str) -> str:
    """任务到点（而非本次 run 预算耗尽）时该用的收尾词。

    只给 worker / planner 配置——上游 ``taskTimeoutWrapupDefaults`` 同款：
    未配置的（mainagent / chat）返回空串，调用方应回退 per-run 词。
    """
    key = str(agent_key or "").strip()
    if key == "worker":
        return WRAPUP_PROMPT_TASK_TIMEOUT_WORKER
    if key == "planner":
        return WRAPUP_PROMPT_TASK_TIMEOUT_PLANNER
    return ""


# 收尾阶段的「实际 reason」——与上游 harness 的 TerminalReason 同名同义。
REASON_TIMEOUT = "timeout"        # 墙钟预算耗尽（含被任务 deadline 夹逼）
REASON_MAX_TURNS = "max_turns"    # 步数预算耗尽


def wrapup_prompt_for_reason(agent_key: str, reason: str, *,
                             clamped: bool = False,
                             is_orchestrator: bool = False) -> str:
    """按**实际**收尾 reason 挑收尾词（上游 ``PromptByReason`` 的等价物）。

    上游 ``wrapupSettlementForTask`` 的口径：

    * ``clamped=false`` → 任务还早，两种 reason 都用 per-run 词（退化成
      ``wrapupSettlement``）；
    * ``clamped=true`` + ``ReasonTimeout`` → 任务到点 → 任务超时词；
    * ``clamped=true`` + ``ReasonMaxTurns`` → 夹逼窗口内步数先耗尽、任务还剩
      时间 → 回退 per-run 词。

    未给任务超时词的 agent（mainagent / chat / 自定义）一律回退 per-run 词。
    """
    per_run = wrapup_prompt(agent_key, is_orchestrator=is_orchestrator)
    if not clamped or str(reason or "") != REASON_TIMEOUT:
        return per_run
    return task_timeout_prompt(agent_key) or per_run


def wrapup_max_turns(agent_key: str) -> int:
    """收尾阶段轮数预算（上游各 agent 均 10；此处统一，保持简单）。"""
    return WRAPUP_MAX_TURNS


def _tool_name(spec_or_name) -> str:
    if isinstance(spec_or_name, str):
        return spec_or_name.strip()
    if isinstance(spec_or_name, dict):
        return str(spec_or_name.get("name") or "").strip()
    return str(getattr(spec_or_name, "name", "") or "").strip()


def is_wrapup_allowed(tool_name: str) -> bool:
    """该工具在收尾阶段是否可用。

    白名单优先（落库工具恒放行）→ 显式禁用集 → 前缀族 → 其余放行。
    「其余放行」是有意的：ARTEX/自定义工具里可能有我们没枚举到的写回通道，
    误剥它们比误放一个探测工具更伤（收尾本来就只剩几轮）。
    """
    name = str(tool_name or "").strip()
    if not name:
        return False
    if name in _WRAPUP_KEEP_TOOLS:
        return True
    if name in _WRAPUP_DENIED_TOOLS:
        return False
    for prefix, _reason in _WRAPUP_DENIED_PREFIXES:
        if name.startswith(prefix):
            return False
    return True


def filter_wrapup_tools(tools: list[dict] | None) -> list[dict]:
    """把工具面裁成收尾可用的子集（保持原顺序）。"""
    if not tools:
        return []
    return [t for t in tools if is_wrapup_allowed(_tool_name(t))]


def filter_wrapup_whitelist(allowed: list[str] | None) -> list[str]:
    """把执行白名单裁成收尾可用的子集——与 ``filter_wrapup_tools`` 同口径。

    工具面（schema）与执行白名单必须**双边同步**：只裁 schema 而白名单仍放行，
    模型看不见但仍能调（它可能从历史里记得）；只裁白名单而 schema 还在，模型
    会一直尝试被拒的调用、白烧收尾轮次。
    """
    if not allowed:
        return []
    return [n for n in allowed if is_wrapup_allowed(n)]
