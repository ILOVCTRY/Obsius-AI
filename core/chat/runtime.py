"""智能体工作台对话运行时（K9，2026-09-29）：独立轻量 agentic loop。

与 AgentLoop（会话窗/任务队列体系）完全解耦：无会话行、无任务认领、无
计划闸——每线程一个 ChatTurn 跑一轮（LLM 循环到纯文本回复为止），消息
按 API 数组序落 chat_messages（core/chat/store.py）。

分工（蛙池式主控轻专家重）：
- 主控线程（agent_id=chat-orchestrator）：todo_write / call_expert /
  skill_open / kb_search / kb_open / bb_query + 受控资产维护工具 + mcp__*；
  不直接执行扫描、利用等实操。
- 子专家线程（agent_id=专家池 id）：AGENT_TOOLS 全量裁剪（去掉任务队列/
  计划/意图/收尾控制原语）+ mcp__*；call_expert 不可用（spawn 深度=1）。
- **激进主控（agent_id=aggressive-controller，2026-10-17）**的子专家线程
  再裁一层（probe_mode）：去掉 bb_query 与意图三件套、豁免意图先行闸与
  「发现必挂意图」、跳过维度收敛闸。快速 worker 只拿 task 干活，不继承主控
  上下文，也不注入额外的子代理提示词，详见 _PROBE_EXCLUDED_TOOLS。

call_expert：spawn 持久子线程（parent_thread_id 留档）→ 隔离上下文跑完
→ 摘要+线程引用回传主控（不审批，全程事件流可见）。

流式：llm 的 text/thinking 回调分别攒 delta → 节流落 chat.delta /
chat.thinking.delta 事件（前端按 thread_id 组装）；每步思考完成落
chat.thinking 终稿；工具调用落 chat.tool 事件；回复终稿落 chat.message 事件。
消息全文以 chat_messages 表为准，事件只承担实时可见性。
"""

from __future__ import annotations

import json
import hashlib
import inspect
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from core.agent.tool_registry import (_CONTROL_TOOLS, _PLAN_TOOLS,
                                      bb_query_schema_for)
from core.agent.tools import AGENT_TOOLS, ToolDispatcher
from core.blackboard.store import BlackboardClosedError
from core.blackboard.artex_intents import (claim_intent as claim_artex_intent,
                                            create_intent as create_artex_intent,
                                            transition_intent as transition_artex_intent)
from core.blackboard.artex_facts import (RECORD_FACT_TOOL_NAME,
                                          RECORD_FACT_TOOL_SPEC,
                                          dispatch_record_fact)
from core.blackboard.artex_traces import (get_worker_trace, list_worker_traces,
                                           search_all_worker_traces)
from core.chat import store as chat_store
from core.chat.artex_steering import SteeringContext
from core.chat.artex_steering import dispatch as artex_steering_dispatch
from core.chat.artex_steering import is_artex_tool
from core.chat.mcp_bridge import MCPBridge
from core.chat.wrapup import (REASON_MAX_TURNS, REASON_TIMEOUT,
                              filter_wrapup_tools, filter_wrapup_whitelist,
                              is_wrapup_allowed as wrapup_allowed,
                              wrapup_max_turns, wrapup_prompt_for_reason)
from core.chat.taskclock import clamp_max_duration
from core.coverage import situation_snapshot
from core.diagnostics.failog import record_mcp_failure, record_tool_failure
from core.diagnostics.system_prompt import save_system_prompt
from core.dimensions import (
    dimension_coverage,
    dimensions_for_type,
    load_track_dimensions,
)
from core.llm.provider import (ContextOverflowError, LLMError, TransientStreamError,
                               is_reasoning_echo)
from core.runtime.gateway import ExecutionGateway
from core.runtime.recovery import EnvironmentUnavailable
from core.skills.experts import load_expert
from core.skills.registry import SkillRegistry
from core.skills.rules import build_rules_preamble, rating_profile_active

log = logging.getLogger(__name__)

ORCHESTRATOR_ID = "chat-orchestrator"

# 激进主控（2026-10-17）：第二个「控制器类」智能体。与 chat-orchestrator
# 共用委派原语（call_expert / 并行派发 / 主控工具面），但调度策略相反：
# chat-orchestrator 是蛙式排队——一批 call_expert 并发跑、**等整批摘要
# 回来再推进**；激进主控是探针海——同一轮并发派出一堆单一快速探针，
# **第一个子代理返回即触发**（[命中]→立即派深耕专家深挖，[未命中]→放弃
# 该方向），剩余探针后台继续跑、完成时以「[后台探针结果]」消息补达
# （_parallel_expert_dispatch 的 first_return 模式，仅激进主控启用）。
# 两者都是用户在工作台 composer 直接选中的顶层智能体，都不是「可被委派
# 的专家」：_delegatable_experts 排除，call_expert 硬校验拒收，且 spawn
# 出来的子线程一律不可再委派（深度=1，expert_names=[] 兜底）。
AGGRESSIVE_CONTROLLER_ID = "aggressive-controller"
ARTEX_CONTROLLER_ID = "artex-controller"
CONTROLLER_IDS = frozenset({ORCHESTRATOR_ID, AGGRESSIVE_CONTROLLER_ID,
                            ARTEX_CONTROLLER_ID})

# 子专家不可用的控制原语（任务队列/计划闸/收尾/私信/HITL——
# 对话线程没有任务上下文与收件箱，这些工具既无语义也危险：publish/claim
# 会污染任务队列）
# 意图三件套（declare/close/reopen_intent）自 2026-10-01 起**开放给对话链**
# （见 intent-tools-chat）：对话主控/子专家同样要按「声明假设→执行→收尾」走，
# 否则对话产出的发现不落链路图（用户实测：任务跑完但在「黑板→发现→链路」看不到
# 任何意图——对话链整条链路从未声明过意图）。
# **propose_pack_edit 自 2026-10-14（kb-iteration）起开放给对话链**（主控 + 子专家）：
# 经验沉淀提案正是对话链的产物，子专家跑任务有一手证据。详见 _ORCH_BASE_TOOLS。
_EXPERT_EXCLUDED = {
    "task_plan", "task_step", "task_reconcile", "publish_task",
    "complete_task", "fail_task", "finish", "request_steps",
    "bb_notify", "request_authorization",
    # CTF 题目登记只归主控（challenge-asset v38）：一个主控线程只发一道题，
    # 子专家应沿用主控已声明的题目，不重复建节点。
    "bb_declare_challenge",
}

# 意图三件套（对话链也要，语义=规划产物与收尾纪律，不依赖任务队列）
_INTENT_TOOLS = ("declare_intent", "close_intent", "reopen_intent", "bb_delete_intent")

# 激进主控子代理（probe_mode）的工具面剔除集（2026-10-17，用户要求：
# 「不需要声明意图、不需要查询黑板」）。语义 = 单点快速子任务——为一次
# 几分钟的快检先建意图链路、再翻黑板盘点纯属浪费；两件事都不做，任务
# 描述自带目标与已知信息，直接开干、结论以 [命中]/[未命中] 回传。
# 剔除必须**工具面（_tool_specs）与执行层（_build_dispatcher allowed）
# 双边同步**：只砍 schema 不砍白名单 = 模型调了被「越界拒绝」打回（错误
# 文案还指向它看不见的工具）；只砍白名单不砍 schema = 模型看得见却调不动。
# 配套豁免：ToolDispatcher(intent_free=True) 跳过意图先行闸与「发现必挂
# 意图」；_dimension_gate_status 跳过（维度收敛以意图覆盖为口径，与探针
# 语义互斥）。chat-orchestrator 的子专家**不享受**此待遇（默认
# probe_mode=False，行为与改动前逐字一致）。
# 激进主控及其 worker 不开放技能、知识库迭代提案或意图控制面；但允许
# 直接查询知识库（kb_search/kb_open），用于查找打法与按需阅读正文。worker
# 仍额外继承 probe_mode 的黑板查询裁剪；工具 schema、执行白名单保持同口径。
_NO_SKILL_OR_INTENT_TOOLS = ("skill_open", "propose_pack_edit", *_INTENT_TOOLS)
# 激进主控允许直接查看项目文件、执行受网关策略约束的命令，并查询知识库；
# 这些是工具能力，不会开放专家池、技能、知识库迭代或意图控制面。
# CTF 解题的核心工具面：文件/命令、资产与证据、知识库、必要的浏览器与逆向。
# 高风险删除/合并和业务建模工具不随主控/worker 下发，减少模型工具选择成本。
# ctf_submit_flag（2026-10-19，ctf-flagjudge）：flag 判定+登记的唯一出口——
# 形态置信/变换回环/跨题防串三道机械判定，取代「模型自述拿到 flag」。
# ctf_ammo（2026-10-19，ctf-ammo）：弱口令候选字典生成——线索机械展开成候选表，
# 治「凭记忆打两个弱口令就放弃」。只产候选，不执行爆破。
_CTF_CORE_TOOLS = (
    "run_cmd", "read_file", "search_files", "unpack_attachment",
    "bb_add_asset", "bb_asset_status",
    "bb_add_finding", "bb_update_finding", "bb_add_artifact",
    "ctf_submit_flag", "ctf_ammo",
    "kb_search", "kb_open", "route_lookup",
    "browser_navigate", "browser_click", "browser_type",
    "browser_screenshot", "browser_content", "browser_back", "browser_replay",
    "decompile", "disasm", "list_symbols", "strings_search", "func_xrefs",
    "bb_upsert_func",
)
_AGGRESSIVE_CONTROLLER_TOOLS = ("run_cmd", "read_file", "search_files")
_PROBE_EXCLUDED_TOOLS = ("bb_query", *_NO_SKILL_OR_INTENT_TOOLS)

_ORCH_BASE_TOOLS = ("todo_write", "call_expert", "skill_open",
                    "kb_search", "kb_open", "bb_query",
                    "bb_delete_asset", "bb_delete_assets", "bb_merge_assets",
                    "bb_declare_challenge",
                    # 经验沉淀提案（kb-iteration，2026-10-14）：主控可提 kb/技能变更提案
                    # （只落 pending，人审）；每会话上限 3 条（ToolDispatcher 内）。
                    "propose_pack_edit",
                    *_INTENT_TOOLS)

# ARTEX Worker trace 工具 schema（artex-traces，2026-10-19）：让 Worker 能检索
# 本任务其他 work 的执行过程，复用他人观察、避免重复劳动。
_WORKER_TRACE_TOOLS = {
    "artex_worker_trace_list": {
        "name": "artex_worker_trace_list",
        "description": (
            "列出本任务里【已跑过】的 work（意图）索引：intent_id + 方向 + 状态 + 步数。"
            "你看不到探索图整体，用它来发现有哪些 work 值得翻看——"
            "再用 artex_worker_trace_get 看其步骤详情。"
            "只列已执行的（不含 pending），不含还没跑的 open。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "按方向关键字过滤（可选）"},
                "limit": {"type": "integer", "description": "返回上限，默认 50（可选）"},
            },
        },
    },
    "artex_worker_trace_get": {
        "name": "artex_worker_trace_get",
        "description": (
            "查看某条意图(work)的【执行过程】（区别于只看最终结论）。\n"
            "三种用法：\n"
            "① 只传 intent_id → 返回该 work 每一步的摘要流（summary≤100字，含 step_id）；\n"
            "② intent_id + q → 只返回命中关键字的步骤摘要；\n"
            "③ intent_id + step_ids → 返回这些步骤的完整内容(detail)；一次最多取 5 个。\n"
            "典型流程：先①/②定位可疑步骤的 step_id，再用③取其完整输出。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string", "description": "意图 id（= work 句柄）"},
                "q": {"type": "string", "description": "关键字：只返回摘要/完整输出命中它的步骤（可选；与 step_ids 互斥）"},
                "step_ids": {"type": "array", "items": {"type": "integer"}, "description": "要取完整内容的 step_id（来自①/②返回；一次最多取 5 个）"},
                "limit": {"type": "integer", "description": "摘要流/检索的返回上限（可选）"},
            },
            "required": ["intent_id"],
        },
    },
    "artex_worker_trace_search": {
        "name": "artex_worker_trace_search",
        "description": (
            "【通常不推荐使用，因为系统中已经给了大部分信息了】"
            "在【本任务其他 work 的执行过程】里按关键字(q)检索——"
            "用于找回某个 worker 见过、却没写进 fact 的东西（某路径/token/报错等）。"
            "已自动排除你自己这条意图的步骤（那些本就在你上下文里）。"
            "只返回命中步骤的摘要(summary≤100字)，每条带 intent_id；"
            "据此再用 artex_worker_trace_get(intent_id, step_ids=[...]) 取完整内容。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "关键字（在所有 work 步骤的摘要+完整输出里搜）"},
                "limit": {"type": "integer", "description": "返回上限，默认 100（可选）"},
            },
            "required": ["q"],
        },
    },
}

# 对话轮步数上限（2026-10-01 由 24/32 提升至 200：修「步数耗尽但全程只调工具
# → 无终稿 → 落『本轮未产出文本回复』」；配合 _loop 末步强制终稿兜底）
_ORCH_MAX_STEPS = 200
_EXPERT_MAX_STEPS = 200

# 激进主控的工作方式（注入 system，替代 chat-orchestrator 的蛙式排队文案）。
# 与 yaml persona 互补：persona 定角色性格，这段定**可执行的调度纪律**
# （含先返先触发与后台补达的运行时事实，改派发语义时必须同步）。
_AGGRESSIVE_CONTROLLER_STYLE = (
    "## 工作方式（激进主控 · 探针海 + 先返先触发）\n"
    "你是激进主控：用「多并发探针 + 先返先触发」快速锁定方向。你自己不实操"
    "——实操是子专家的事。\n"
    "1. **登记与盘点**：CTF 轨先 bb_declare_challenge 登记题目；附件/样本先 "
    "bb_add_asset 落资产并记 sha256；bb_query 看黑板已有资产与发现，避免重复"
    "劳动；需要方法细节时 kb_search/kb_open 查知识库。\n"
    "2. **探针化拆解**：把当前目标拆成一批**单一、快速、可独立完成**的探针"
    "——每个探针只查一个方向、几分钟内能跑完（例：lsb 隐写检查、音频隐写"
    "检查 spectrogram/steghide、strings/熵值/魔术字节、crypto 模式识别、"
    "压缩包嵌套与明文泄露……）。禁止把多个方向塞进一个探针，禁止让探针做"
    "深度利用。\n"
    "3. **方向分散后同轮并发派出**：先建立方向覆盖表，至少选择 3 个彼此不相似"
    "的测试面，再在**同一轮**用多个 call_expert 并发派出（并发执行，不是排队）。"
    "优先覆盖文件/样本结构与元数据、编码/密码与凭据、网络/Web/协议、逆向/二进制、"
    "取证/日志、隐写/媒体等不同类别。每个 task 必须自包含：目标文件/资产（附"
    "sha256）、只做什么、验收标准，并明确要求「结论以 [命中] 或 [未命中] 开头，"
    "后跟一句证据」。同一批 task 不得都集中在相同字符串、同一工具、同一文件特征"
    "或同一个猜测；两个 task 的目标、证据来源、主要工具和验收标准高度相似时，"
    "必须改派到尚未覆盖的方向。重大价值方向可将同一个 task 同时派给 2 或 3 个独立"
    "worker 做交叉验证，但不能因此减少其他不同方向的基础覆盖；至少保留一个持续"
    "追踪 worker，直到正式 flag、决定性结论或方向穷尽。用户未说明格式时，管理员密码、"
    "数据库密码、API key、凭据和普通字符串只能作为候选，不能自动停止。\n"
    "4. **先返先触发**：探针派出后**只要第一个返回就立刻行动，严禁干等整批**"
    "——[命中] 立即对该方向派发对应深耕专家（crypto-solver / pwn-solver / "
    "reverse / web-solver / forensics-solver…）深挖（任务同样自包含并带上"
    "命中证据）；[未命中] 放弃该方向，继续派发/等待下一批探针。时间是本角色"
    "的核心指标，蛙式排队等整批是违规操作。\n"
    "5. **后台补达**：未等到的探针会在后台继续跑，完成时以「[后台探针结果]」"
    "消息补达——收到后按第 4 条同一规则处置（命中就补派深挖）。\n"
    "6. **收敛与汇总**：方向锁定并拿到正式结论后收尾；todo_write 实时反映"
    "「探针→命中→深挖→结论」的状态迁移。用户未说明 flag 格式时，只有经验证的"
    "`flag{...}`、`xxxx{...}` 等明确题目格式才可作为正式 flag；管理员密码、数据库"
    "密码、API key、凭据、普通字符串或其他非该格式命中，只能标为候选 flag/高价值"
    "线索，不能自动停止，必须继续挖掘其他方向。**拿到疑似 flag 一律走 "
    "ctf_submit_flag 登记**（它做形态置信/变换回环/跨题防串三道机械判定，比自述"
    "可靠；候选线索加 candidate=true）。全部完成后向人类汇总署名输出"
    "（走了哪些方向、正式 flag、候选线索、各自证据和仍未确认的内容）。")

# ARTEX 主控的工作方式（artex-fusion，2026-10-19）。与 yaml persona 互补：
# persona 定角色性格，这段定**可执行的操舵纪律**——主控不再自己 call_expert
# 派活，而是驱动项目级 ArtexEngine：Planner 生成 Intent、Engine 调度、Worker
# 执行，主控只观察与调整（对应原生 ARTEX 的 MainAgent 定位）。
_ARTEX_CONTROLLER_STYLE = (
    "## ARTEX 工作方式（观察 → 规划 → 引擎调度 → 操舵 → 目标收敛）\n"
    "你是 ARTEX 主控。**你不自己执行，也不逐个派活**：Intent 由 Planner 生成，"
    "项目 Engine 从 Frontier 领取并并发派给 Worker 执行。你只做判断与调整。\n"
    "1. **盘点**：bb_query 看已登记资产与发现；artex_status 看当前任务的 runtime "
    "状态、**注册目标及达成情况**、Intent 计数、活跃 Worker 与探索图里的事实/发现。"
    "已有方向不要重复规划。\n"
    "2. **规划**：artex_plan(goal=\"…\", mode=\"new\") 建立任务树（Planner 会把目标"
    "拆成可核验的离散目标并登记）；对进行中的任务用 artex_plan(mode=\"extend\") "
    "让 Planner 依据新事实追加方向，或用 artex_plan(intents=[…]) 直接指定方向。"
    "Planner 是唯一 Intent 生成者——不要自己拼任务描述去 call_expert 绕过它。\n"
    "3. **观察**：Worker 完成后会把结果写成探索图 Fact、把关联发现写成 Finding，"
    "并把证据自动挂到相关目标上；每轮先用 artex_status 复核，再决定是继续追加"
    "方向还是收尾。artex_status 同时显示**资产覆盖度**（已测/范围、未覆盖数）——"
    "量化目标（覆盖度达 X%、测过 N 个资产）以此实测值为准。\n"
    "3.1 **翻事实库**：artex_status 只给最近几条标题，真正排方向前用 "
    "artex_fact(action=\"list\") 翻本任务事实（可按 intent_id / confidence 筛、"
    "分页），artex_fact(action=\"get\", id=…) 取某条完整详情。"
    "**否定结论也在事实库里**（端口关闭 / 参数不可注入 / 未发现入口）——"
    "派方向前先查一遍，避免重复劳动；confidence=inferred 的结论是推断，"
    "不代表该方向已穷尽，别据此永久放弃。\n"
    "4. **目标收敛（关键）**：任务是否完成由**目标达成**决定，不是「Intent 跑完」。"
    "artex_status 显示目标未达成却已无方向时，要么依证据用 "
    "artex_goal(action=\"prove\", goal_id=…, evidence_ids=[…]) 证明目标达成，"
    "要么用 artex_plan 追加能证明它的方向；确认全部目标已达成后用 "
    "artex_goal(action=\"goal_met\", reason=\"…\") 全局收官（reason 必须是真实依据）。"
    "**严禁**在目标未达成时宣布收官。\n"
    "4.1 **量化验收核对**：目标含可量化条件（覆盖度达 X% 等）时，证明/收官前"
    "【必须】核对 artex_status 的覆盖度实测值；未达标就【禁止】盖章，改派意图补测"
    "（用 artex_goal(action=\"anchor\") 看未覆盖清单，或 artex_plan 派补测方向）。"
    "覆盖度是**下限/验收项、不是探索目标本身**——发现高价值入口后优先往深打穿，"
    "而不是为拉平覆盖度逐个资产浅测。\n"
    "5. **操舵**：方向跑偏或需要补充线索时用 artex_hint——指定运行中的 intent_id "
    "会直接下发 steer，Worker 在下一步收到「[Worker 方向调整]」；只写 Hint 则留给"
    "后续规划作依据。需要暂停/取消/终止时用 artex_control。\n"
    "6. **收敛**：artex_status 显示 goal_reached 时收尾，汇总已测方向、资产范围、"
    "已验证发现与证据、未验证假设和仍需人工确认的动作；显示 needs_review 时"
    "必须先解释「哪条目标未达成、为什么」，再决定补方向还是交人工。\n"
    "7. **call_expert 例外**：只用于不值得建 Intent 的一次性查询/登记（例如查一条"
    "配置、确认一个资产归属）。测试与深挖方向一律走 artex_plan。\n"
    "8. **边界**：只在登记的授权范围内扩展子域、站点和端口，跨出范围立即暂停并请求"
    "确认；漏洞利用、爆破、上传、提权、横向移动、业务写入和 CTF 提交等高风险动作"
    "必须逐项审批，发现线索不等于获得执行授权。\n"
    "9. **证据**：命中结论必须有实际输出、请求响应、文件路径或复现步骤支撑；"
    "没有证据的内容只能标为假设——目标达成同样要有证据，模型自述不算数。")


class ChatStopped(Exception):
    """人类中止本轮的专用信号（2026-10-09）。

    此前中止靠 `LLMError("已中断")` 穿出，被 `_loop` 的 `except LLMError`
    当成可重试的流中断——只吐 thinking 时原样重发 ≤`_CHAT_RESEND_MAX` 次、
    有半截文本时续写 ≤`_CHAT_INTERRUPT_MAX` 次，耗尽才抛；抛到 `run()` 又被
    `_classify_error` 判成 `unknown` → 「执行异常」错误卡（用户实测截图）。
    中止是**人为意图**而非故障：用独立异常直达 `run()` 归位 idle，不进任何
    重试逻辑、不落错误卡。"""


# 流式节流（2026-10-04 顺滑化）：由 80 字符/1s 一路压到 6 字符/0.05s（约 20Hz）——
# 文本按大块跳变是「一顿顿的」的直接来源。**真正的瓶颈不只在节流**：WS 投递节奏
# （app.py `ws_events` 轮询间隔）与前端 flush 窗口都要同步压密，只降节流=白降。
# 写入开销：synchronous=NORMAL（2026-10-12 起，见 core/blackboard/store.py）下实测
# 0.13ms/次，20Hz ≈ 0.26% 核/项目（此前 FULL 为 2.7ms/次 ≈ 5%，多项目并发成 CPU 首耗）；
# 每条携带累计全文，故 run() 正常收尾后调 prune_chat_thread_deltas 清剪。
_DELTA_MIN_CHARS = 6
_DELTA_MIN_INTERVAL = 0.05

# 工具参数流截断的整轮重试上限（2026-10-01 事故修复）：工具参数 JSON 残缺/截断
# （LLMError.truncated）时，流不可重放 → 轮级重发 ≤2 次（与 agent 循环同口径）。
_CHAT_TRUNC_RETRIES = 2

# 纯文本终稿被输出上限截断时的自动续写（2026-10-01，修「话说一半就正常终止」）：
# 终稿 stop_reason=length/max_tokens → 把半截文本作为 assistant 前缀 + 追一条
# user「接着说完」继续循环，拼接成完整终稿。上限防死循环；超限接受现有文本并落
# chat.truncated 事件（前端可提示「输出可能不完整」）。
_CHAT_CONTINUE_MAX = 3
_TRUNC_STOP_REASONS = ("length", "max_tokens")

# 上游偶发返回空响应（无文本、无工具调用）时只在内存中续问，避免瞬时空流
# 直接结束会话；达到上限后走 _EMPTY_FINAL_TEXT 兜底（2026-10-16 起：有
# thinking 累积则降级为可见终稿，见 _loop 空响应分支）。
_EMPTY_RESPONSE_RETRIES = 3
_EMPTY_RESPONSE_NUDGE = (
    "上一条响应没有产生文本或工具调用。请继续处理当前请求，必须返回可见文本回复，"
    "或返回需要执行的工具调用；不要返回空响应。"
)
# 空响应「独立续问」升级（2026-10-11 事故修复）：把 nudge 只并进尾部 tool_result
# 时，重试上下文与上一次**几乎逐字相同**——上游对同一上下文确定性返回「只 reasoning、
# 无文本/无工具调用」的空响应时，3 次重试等同 1 次，用户只看到「不会自动重试」后
# 直接落「本轮未产出文本回复」。修正：重试时**新起一条独立 user 消息**打破上下文
# 相同性（Anthropic 角色严格交替，故先补一条 assistant 占位；见
# `_append_standalone_nudge`）。文本随重试次数递增变强，逼模型给可见产物。
_EMPTY_RESPONSE_ASSIST_PLACEHOLDER = "（无输出，继续）"
# 空响应重试前一级压缩的保留步数（2026-10-16）：比正常路径的 _KEEP_RECENT_STEPS
# （12）更激进——重试场景要尽量砍掉重吃的全量数据（如 416 条资产的分页枚举）。
_EMPTY_RETRY_KEEP_STEPS = 4
# 重试耗尽后的终稿兜底（2026-10-16 事故修复，两处兜底共用）：旧文案
# 「（本轮未产出文本回复，请重试或改述）」不说成因、不给出路。新文案点明
# 最常见成因（推理过长未及输出正文）并给出两个可立即执行的动作。
_EMPTY_FINAL_TEXT = (
    "（本轮模型未产出回复：可能推理过长、未及输出正文。"
    "可缩小任务范围重问，或直接说「先给结论」）"
)


def _empty_response_nudge(attempt: int) -> str:
    """第 ``attempt`` 次（从 1 起）空响应重试的独立续问文本，随次数升级加重。"""
    base = _EMPTY_RESPONSE_NUDGE
    if attempt <= 1:
        return base
    extra = ("你已经连续多次返回空响应（只有推理、没有面向人类的输出）。"
             "立即停止空转：要么直接给出面向人类的文本结论/摘要，"
             "要么立即发出下一步工具调用；不要再返回空内容。")
    return f"{base}\n\n{extra}"


def _append_standalone_nudge(messages: list[dict[str, Any]], nudge: str) -> None:
    """新起一条**独立 user 消息**承载 nudge（打破重试上下文相同性）。

    与 `_inject_nudge`（并入尾部 tool_result）互补：后者上下文几乎不变，对上游
    「确定性空响应」无效。Anthropic 原生协议要求 user/assistant 严格交替，故尾部
    已是 user 时先补一条 assistant 占位再追加 user；尾部是 assistant/空则直接追加。
    三种协议（anthropic / chat-completions / responses）都能安全接受该序列。
    """
    if not messages or messages[-1].get("role") == "user":
        messages.append({"role": "assistant",
                         "content": _EMPTY_RESPONSE_ASSIST_PLACEHOLDER})
    messages.append({"role": "user", "content": nudge})


_CONTINUE_NUDGE = ("（上一条回复因输出长度上限被截断）请**接着上一句继续写完**，"
                   "不要重复已写内容，也不要重新开头。")

# 推理回声守卫（2026-10-09 事故）：上游中转把模型的推理正文同时经内容通道与思考
# 通道下发，导致纯文本终稿其实是「整段推理」。解析层（core.llm.provider
# `collapse_reasoning_echo`）已把这类 content 清空——这里再兜一道（覆盖未经该
# 清空路径的 provider），命中则不当终稿收轮，推回让模型给结论；上限防死循环。
_REASONING_ECHO_NUDGE = (
    "（系统提示）你上一条回复只是在复述推理过程，并不是面向人类的答复。"
    "请直接给出结论、摘要或下一步工具调用；不要复述任何推理内容。")
_REASONING_ECHO_MAX = 3

# 推理膨胀闸（2026-10-09 事故）：单步思考超此长度视为「盘算失控」——模型陷在
# 「怎么翻页/怎么枚举」这类元问题上反复推演而迟迟不动作（实测单步 5.2 万字符、
# 单线程累计 output 24.6 万 token）。此时把「停止盘算、直接动作或给结论」并入
# 当轮提示；连续推回超上限只记账不再推（不改变收轮语义）。
_REASONING_BLOAT_CHARS = 20_000
_REASONING_NUDGE_MAX = 3
_REASONING_NUDGE = (
    "（系统提示）你已连续大量推演。停止继续盘算，直接用工具执行下一步，"
    "或给出面向人类的结论摘要；不要复述推理过程。")

# 流式 body 中断续跑（2026-10-08 事故修复）：已吐增量后连接中断（LLMError.partial
# ——「peer closed connection ... incomplete chunked read」这类）时，半截文本已
# 上屏、流不可重放。此前直接整轮失败（用户看到「网络中断」错误卡）；现把半截文本
# 作为 assistant 前缀落库 + 追一条续写指令继续循环拼接，上限 _CHAT_INTERRUPT_MAX
# 防死循环，超限才如实失败（交 run() 落错误卡）。
_CHAT_INTERRUPT_MAX = 10
_INTERRUPT_NUDGE = ("（上一条回复因网络中断被截断）请**接着上一句继续写完**，"
                    "不要重复已写内容，也不要重新开头。")

# 无文本增量的流中断「原样重发」（2026-10-09 事故修复）：模型只吐了 thinking
# （或工具参数）就断流时，`partial=True` 但文本累加为空——工作台续写需要半截
# 文本，此处无可续，此前直接抛错落「网络中断」卡，且一次都没重试（用户实测
# 症状）。修正：无内容提交 → 清 delta 后原样重发本 step（与 agent 循环 partial
# 整轮重发同口径）。重发要重做整轮 LLM（长思考可达数分钟），故与续写上限分开、
# 取小值——no 无限空转。
_CHAT_RESEND_MAX = 3

# 主控一批 tool_calls 中允许同时运行的子专家数。超过上限的调用会在
# executor 中排队，避免模型一次生成大量 call_expert 时无限创建线程。
# 2026-10-06 由 4 提到 8：主控常一次并发 5+ 个 call_expert，4 会让多出的排队
# 串行（实测「先跑 4 个再跑第 5 个」）；8 给常见批量留出余量，仍防无限建线程。
_MAX_PARALLEL_EXPERTS = 8

# 子专家维度收敛闸（agent-path-intent-loop M2，2026-10-09）：纯文本收尾前，
# spawn 委派的子专家若轨有维度清单，检查已锚定资产的适用面覆盖；未覆盖推回
# plan（declare_intent → execute → close_intent）而不是允许提前收尾。连续 nudge
# 上限防模型始终不给 plan 时空转整轮；超限接受现有文本并落 giveup 事件。
_DIMENSION_GATE_MAX = 5

# 子专家启动注入的资产台账条数上限（防 system 膨胀；超出截断并提示）。
_ASSET_LEDGER_CAP = 200

# 蓝图注入上限（R4 消费面接线 M1，2026-10-13）：主控/子专家 system 里摊蓝图
# （名/status/模块/锚点数），不进 spec/notes 全文——与 func_kb/findings 共享
# 物化预算，须紧凑；详情让 Agent 用 bb_query what=blueprint 自取。
_BLUEPRINT_CAP = 10            # 最多列几份蓝图
_BLUEPRINT_MODULE_CAP = 20     # 每份蓝图最多列几个模块

# ---------- 上下文治理参数（2026-09-30 压缩上下文方案） ----------
# 背景：单条工具结果可达数百万字符原样入历史 → 跨轮全量回放 → input 突破
# 网关硬上限 → HTTP 400（不可重试、无降级）→ 线程 status=error。治理分三层：
#   ①单条工具结果截断（入历史前，见 _loop 工具结果追加处）
#   ②发送前两级联动压缩（一级占位化旧 tool_result → 二级 LLM 摘要）
#   ③reactive 兜底（捕 ContextOverflowError 强制压缩重试一次）
# 参数集中在常量块，providers.json 可逐 provider 覆盖（见 ct-7）。
_MODEL_WINDOW_FALLBACK = 1_048_566   # 硬窗口兜底（providers.json 的 model_window 优先）
_CTX_SOFT_BUDGET = 512_000           # 有效软上限：支持 1M≠在 1M 最好，超此即压
_CTX_TRIGGER_RATIO = 0.9             # 触发阈值 = min(该比例×硬窗口, 软上限)
_MAX_TOOL_RESULT_CHARS = 120_000     # 单条工具结果入历史上限（约 40K token）
_KEEP_RECENT_STEPS = 12              # 逐字保留的最近步数（其外才允许压缩）
_TOOL_RESULT_EVENT_HEAD = 400        # 事件流 result_head 预览长度

# 先返先触发（激进主控）：被提前断候的探针，主线程先落**占位** tool 结果
# 保 tool_use/tool_result 配对合法（否则下一轮请求被网关 400 拒收），worker
# 完成后以**同 tool_use_id 续写**真实结果——_load_history 与前端
# buildWbItems 都按「同 id 后写优先、占位不覆盖真实」选取，故探针卡从
# 「[后台继续]」平滑更新为真实结果，跨轮回放也不丢信息。
_PROBE_PENDING = "[后台继续]"
_PROBE_OVERFLOW_HEAD = 1500          # overflow 注入上下文的单条截断

# 压缩占位/摘要标记（前端与测试可据此识别已压缩内容）
_CTX_PLACEHOLDER_TAG = "[上下文压缩] "
_CTX_SUMMARY_TAG = "[历史摘要] "
_CTX_SUMMARY_MAX_INPUT = 400_000     # 摘要器单次输入上限（超出取首尾、中段省略）

# 二级摘要（LLM）系统提示：逆向领域增强模板（目标/关键地址/结论/假设/产物/待办）
_CTX_SUMMARY_SYS = (
    "你是逆向与渗透分析的历史压缩器。把给定对话历史压缩成一段高保真摘要，"
    "供后续在更小上下文里继续分析。严格要求：\n"
    "1. 只保留事实与结论，不臆造；无法确定的信息显式标注「未确认」。\n"
    "2. 按以下结构输出（无内容的项写「无」）：\n"
    "   - 目标：二进制/服务名、路径、哈希（MD5/SHA1/SHA256）\n"
    "   - 关键函数/地址/偏移：函数名、地址、RVA/文件偏移、关键字符串\n"
    "   - 已确认结论：已验证的事实、漏洞/风险点\n"
    "   - 待验证假设：尚未证实的推测与线索\n"
    "   - 关键产物：文件路径、脚本、PoC、日志\n"
    "   - 待办：下一步应做的事项\n"
    "3. 保留具体数值（地址/偏移/哈希/长度），不要用「某个地址」含糊代替。\n"
    "4. 中文输出，尽量紧凑，不要复述寒暄与过程性文字。"
)


def _est_tokens(text: str) -> int:
    """粗估 token 数（中英混合 ~3 字符/token）。仅用于上下文构成分解的比例
    估算，不做精确计数——真值由 LLM usage 归一校准（见 _loop 的 breakdown）。"""
    return max(1, len(text) // 3) if text else 0


def _to_epoch(value: Any) -> float | None:
    """把「ISO8601 字符串 / epoch 秒（int/float/数字串）」统一成 epoch 秒。

    CTF 赛制截止时间在项目 config 里可能是 `"2026-10-20T12:00:00Z"` 也可能是
    `1761000000`（平台 API 常直接给时间戳）。解析失败返回 None——调用方据此
    退回阶梯推断，**绝不猜一个假时间**。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:  # 纯数字串
        num = float(text)
        return num if num > 0 else None
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:  # 无时区按本地时间（赛题页给的多为本地时间）
            return dt.timestamp()
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def _msg_text(m: dict[str, Any]) -> str:
    """提取消息可估文本：字符串直取；assistant blocks 取 text+tool_use 入参；
    tool_result blocks 取 content（2026-09-30 伪影修复：此前漏计工具结果，
    est_msgs 被严重低估 → 真值归一 scale 膨胀 → 缺口被成倍记进「工具定义」
    桶，面板把纯工具 JSON 放大成 271K 的假象）。"""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out: list[str] = []
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                rc = b.get("content")
                if isinstance(rc, str):
                    out.append(rc)
                elif isinstance(rc, list):
                    out.extend(str(x.get("text") or "") for x in rc
                               if isinstance(x, dict))
                continue
            out.append(str(b.get("text") or ""))
            if b.get("input"):
                out.append(json.dumps(b["input"], ensure_ascii=False))
        return "".join(out)
    return ""


def _recent_boundary(messages: list[dict[str, Any]],
                     keep: int = _KEEP_RECENT_STEPS) -> int:
    """返回「逐字保留区」的起始下标：从末尾往前数第 keep 条 assistant 消息。
    该下标之前（不含）为可压缩的旧区；找不到足够步数则返回 0（无可压缩）。"""
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            seen += 1
            if seen >= keep:
                return i
    return 0


def _mask_old_tool_results(messages: list[dict[str, Any]],
                           boundary: int) -> int:
    """一级压缩（零 LLM 成本）：把 boundary 之前的旧 tool_result 内容换成
    占位符（保留 tool_use_id 配对与 assistant 文本/调用记录），返回占位数。
    幂等：已占位的不再重压。参考 Claude context-editing 的 clear_tool_uses。"""
    masked = 0
    for m in messages[:boundary]:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            rc = b.get("content")
            if isinstance(rc, str) and not rc.startswith(_CTX_PLACEHOLDER_TAG):
                b["content"] = (f"{_CTX_PLACEHOLDER_TAG}工具结果原文 {len(rc)} "
                                f"字符已省略以控制上下文；如需可重新调用该工具）")
                masked += 1
    return masked


def _specs_by_names(names: set[str]) -> list[dict[str, Any]]:
    return [s for s in AGENT_TOOLS if s.get("name") in names]


# 轮次失败分类（2026-10-01 健壮性）：把异常映射为前端错误卡片可渲染的
# {category,title,message,hint}。分类为有限枚举，每类给「下一步怎么做」建议。
# 之前只亮一个「出错」徽标、错误原文不落库不展示，用户无从判断原因。
_ERROR_CATEGORIES: dict[str, tuple[str, str]] = {
    "network": (
        "网络中断",
        "与模型网关的连接中断、超时或被重置。系统已自动重试仍未成功——"
        "请稍后重发本条消息；若持续出现，检查网络/代理与网关（base_url）可用性。"),
    "rate_limit": (
        "网关限流",
        "模型网关返回限流（HTTP 429）。稍等片刻后重发即可；频繁触发请降低并发或换供应商。"),
    "upstream": (
        "上游服务不可用",
        "模型网关/中转站瞬时故障（HTTP 5xx，如「Upstream service temporarily "
        "unavailable」）。系统已自动重试若干次仍未成功——通常是上游抖动，"
        "稍后重发即可；若持续出现，请检查供应商状态或改用其它供应商。"),
    "auth": (
        "鉴权失败",
        "模型网关拒绝请求（HTTP 401/403）。请在「技能与设置 → 模型供应商」核对该"
        "供应商的 API Key 与 base_url 是否正确、是否过期。"),
    "quota": (
        "额度不足",
        "模型网关提示额度/余额不足。请充值，或改用其它可用供应商后重发。"),
    "context": (
        "上下文超限",
        "单轮上下文超出模型窗口，自动压缩后仍被网关拒收（400/413）。"
        "请新开线程，或精简历史消息后重试。"),
    "model_cooldown": (
        "模型不可用（冷却中）",
        "模型网关提示该模型在所有账号上均处于冷却期（HTTP 400 per-model "
        "cooldown）。系统已重试多次仍未恢复——请在「技能与设置 → 模型供应商」"
        "改用其它模型或供应商后重发（网关原文亦提示 try another model）。"),
    "gateway_block": (
        "请求被网关拦截",
        "模型网关的渠道校验风控拦截（HTTP 400，如 code 11128 / conversation "
        "id missing / channel verification）。与请求内容无关，通常是短时间"
        "内请求过多触发网关侧风控，冷却几分钟后自行恢复。系统已按 30s 间隔"
        "自动重试约 5 分钟；若仍被拦，请稍后重发，或在「技能与设置 → 模型"
        "供应商」临时切换其它供应商。"),
    "bad_request": (
        "请求被拒",
        "模型网关判定请求不合法（HTTP 400）。常见于历史消息结构问题"
        "（如工具调用配对缺失）。系统已尝试修复历史；若仍失败，请新开线程。"),
    "stream": (
        "网关流异常",
        "工具参数流被网关异常中断（SSE 尾部冗余/截断）。系统已自动整轮重试；"
        "若仍失败，请重发该指令或改述。"),
    "filesystem": (
        "文件访问失败",
        "读取本地文件/目录时失败（文件被移动或删除、被安全软件/索引服务瞬时占用）。"
        "系统已自动重试仍未成功——请确认相关文件仍在原位后重发；"
        "若文件已被删除，新开线程可绕开残留引用。"),
    "db": (
        "数据存储异常",
        "读写项目数据库失败（项目可能正在删除/重建，或进程刚重启）。"
        "请稍后重试；若持续出现，检查该项目是否已被删除。"),
    "environment": (
        "运行环境不可用",
        "命令执行所需的环境缺失或不可用（工具/依赖/运行时/Docker 镜像等），"
        "自动恢复未成功或未获准执行。请按下方提示补齐环境后重发；"
        "若提示需要审批，请先在审批卡片中放行安装/构建。"),
    "unknown": (
        "执行异常",
        "本轮执行发生未预期异常。技术细节见下方，可据此进一步排查或反馈。"),
}


def _is_truncated_final(resp) -> bool:
    """终稿是否被输出上限截断（2026-10-01）。

    主判据：``stop_reason`` 命中 ``length``/``max_tokens``。
    兜底：网关未报 stop_reason（空串）时，用 output_tokens 是否逼近本步
    输出上限来判（宁漏勿误——只在明确触顶时才认）。带 tool_calls 的响应不算
    终稿（走工具路径或工具参数重试），直接 False。
    """
    if getattr(resp, "tool_calls", None):
        return False
    sr = (getattr(resp, "stop_reason", "") or "").lower()
    if sr in _TRUNC_STOP_REASONS:
        return True
    # 兜底：stop_reason 缺失且无文本（空终稿）时无法判断，不误伤；仅当有文本、
    # 且调用方计入的 output_tokens 触顶才认——此处只做保守的显式判据。
    return False


def _inject_nudge(messages: list[dict[str, Any]], nudge: str) -> None:
    """把系统提示并入对话尾部（供下一步 LLM 看到），保证角色交替合法。

    优先并进尾部 user 消息里**首个 tool_result 的正文**——三种协议
    （anthropic / chat-completions / responses）都原样透传 tool_result 文本，
    且不会产生连续两条 user（Anthropic 要求角色严格交替，连续 user 会被 400
    拒收；chat-completions 的 `_chat_messages` 还会把混在 tool_result 里的 text
    块整块丢掉，故不能靠追加 text 块）。没有 tool_result 可挂时才并进尾部 user
    的正文，尾部不是 user 才新起一条。"""
    if not messages:
        messages.append({"role": "user", "content": nudge})
        return
    last = messages[-1]
    if last.get("role") != "user":
        messages.append({"role": "user", "content": nudge})
        return
    content = last.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                block["content"] = f"{nudge}\n\n{block.get('content', '')}"
                return
        content.append({"type": "text", "text": nudge})
        return
    last["content"] = f"{content}\n\n{nudge}" if content else nudge


def _classify_error(e: BaseException) -> dict[str, str]:
    """异常 → 结构化错误（category/title/message/hint）。message 为原始异常
    文案（技术细节），title/hint 为面向人类的分类与建议。"""
    msg = str(e)
    low = msg.lower()
    status = getattr(e, "status", 0) or 0
    # 环境类必须先判：EnvironmentUnavailable 的文案可能含 "temporarily
    # unavailable"，否则会被下面的 upstream 分支抢走，误导成「上游抖动」。
    if isinstance(e, EnvironmentUnavailable) or "环境错误" in msg \
            or "[环境恢复" in msg:
        cat = "environment"
    elif getattr(e, "truncated", False) or "工具参数流截断" in msg:
        cat = "stream"
    # 上游 5xx / 流内瞬时错误帧（TransientStreamError 是 ConnectionError 子类，
    # 必须排在 network 之前，否则会被 isinstance 抢走判成「网络中断」）。
    elif isinstance(e, TransientStreamError) or status in (500, 502, 503, 504) \
            or "upstream" in low or "temporarily unavailable" in low \
            or "service unavailable" in low or "bad gateway" in low \
            or "gateway timeout" in low or "overloaded" in low:
        cat = "upstream"
    elif isinstance(e, ContextOverflowError) or (
            status in (400, 413) and ("too long" in low or "input length" in low
                                      or "context length" in low
                                      or "maximum context" in low)):
        cat = "context"
    elif isinstance(e, (TimeoutError, ConnectionError)) \
            or "网络错误" in msg or "网络连接失败" in msg or "流式传输中断" in msg \
            or "connection reset" in low or "connection aborted" in low \
            or "remote host" in low or "incomplete chunked read" in low \
            or "peer closed connection" in low:
        cat = "network"
    elif status == 429 or "429" in msg or "rate limit" in low or "限流" in msg:
        cat = "rate_limit"
    elif status in (401, 403) or "401" in msg or "403" in msg \
            or "unauthorized" in low or "authentication" in low or "invalid api key" in low:
        cat = "auth"
    elif status == 402 or "余额" in msg or "insufficient" in low or "quota" in low:
        cat = "quota"
    # 模型冷却必须先于 bad_request 判定（2026-10-10）：网关把「该模型全账号冷却」
    # 塞进 HTTP 400，此前落 bad_request「历史消息结构问题」——方向完全跑偏，
    # 而文案里唯一有效的自救指令「try another model」被丢弃。
    elif "per-model cooldown" in low or "on every account" in low \
            or "try another model" in low or "model is unavailable" in low:
        cat = "model_cooldown"
    # 渠道校验风控拦截（2026-10-16）：私有中转网关（api.huanling.icu）在风控
    # 窗口内对所有请求回 400（code=11128 / conversation id missing /
    # channel verification）——与历史结构无关（新线程首条也照挂），冷却几分钟
    # 恢复。此前落 bad_request 的「历史消息结构问题」文案方向跑偏；传输层已按
    # 冷却口径自动重试（retry.is_channel_verification_error），此处只兜耗尽后的
    # 终态文案。
    elif "conversation id missing" in low or "channel verification" in low \
            or "11128" in low or "渠道校验" in msg or "请求被拦截" in msg \
            or "通路验证" in msg or "通路校驗" in msg or "官方客户端" in msg:
        cat = "gateway_block"
    elif status == 400 or "400" in msg or "invalidparameter" in low:
        cat = "bad_request"
    elif isinstance(e, sqlite3.Error) or isinstance(e, BlackboardClosedError):
        cat = "db"
    # 文件系统类放最后：TimeoutError/ConnectionError 是 OSError 子类，已在上面
    # 的 network 分支拦下，不会误落此处。
    elif isinstance(e, OSError):
        cat = "filesystem"
    else:
        cat = "unknown"
    title, hint = _ERROR_CATEGORIES[cat]
    return {"category": cat, "title": title, "message": msg[:600], "hint": hint}


def _retry_transient(fn, *, attempts: int = 2, delay: float = 0.25):
    """瞬时 IO/DB 错误重试（2026-10-03 健壮性）：Windows 安全软件/索引服务会
    短暂锁文件（FileNotFoundError/PermissionError），项目库偶发 locked——退避
    重试 1 次往往即过。**只重试 OSError/sqlite3.Error**；BlackboardClosedError
    等语义性错误（项目正在删除）立即上抛，不浪费重试。"""
    for i in range(attempts):
        try:
            return fn()
        except (OSError, sqlite3.Error) as e:
            if i >= attempts - 1:
                raise
            log.warning("瞬时 IO/DB 错误，%.2fs 后重试（%d/%d）：%s",
                        delay, i + 1, attempts - 1, e)
            time.sleep(delay)


def _sanitize_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """重建严格配对的 tool-use/tool-result 历史，兼容旧版本坏记录。

    网关要求每个 assistant tool-use id 唯一，且紧随一个同 id 的 tool result；
    旧工作台线程可能已经持久化重复/错配调用，这里只修复内存回放，不改原始审计。
    """
    cleaned: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            cleaned.append(m)
            continue
        blocks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for b in m["content"]:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                blocks.append(b)
                continue
            tid = str(b.get("id") or "").strip()
            name = str(b.get("name") or "").strip()
            if tid and name and tid not in seen:
                seen.add(tid)
                blocks.append(b)
        if blocks:
            cleaned.append({**m, "content": blocks})

    out: list[dict[str, Any]] = []
    i = 0
    while i < len(cleaned):
        m = cleaned[i]
        use_ids = [str(b.get("id")) for b in (m.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "tool_use"] \
            if m.get("role") == "assistant" else []
        if use_ids:
            out.append(m)
            candidates: list[dict[str, Any]] = []
            j = i + 1
            while j < len(cleaned):
                nxt = cleaned[j]
                if nxt.get("role") != "user" or not isinstance(nxt.get("content"), list):
                    break
                tool_blocks = [b for b in nxt["content"]
                               if isinstance(b, dict) and b.get("type") == "tool_result"]
                if not tool_blocks:
                    break
                candidates.extend(tool_blocks)
                j += 1
            by_id: dict[str, dict[str, Any]] = {}
            for b in candidates:
                tid = str(b.get("tool_use_id") or "").strip()
                if tid in use_ids and tid not in by_id:
                    by_id[tid] = b
            results = [by_id[tid] for tid in use_ids if tid in by_id]
            results.extend({"type": "tool_result", "tool_use_id": tid,
                            "content": "（该工具调用无结果记录：历史已自动修复）"}
                           for tid in use_ids if tid not in by_id)
            out.append({"role": "user", "content": results})
            i = j if j > i + 1 else i + 1
            continue
        if m.get("role") == "user" and isinstance(m.get("content"), list) \
                and any(isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in m["content"]):
            i += 1
            continue
        out.append(m)
        i += 1
    return out


def expert_tool_names(packs_root: str | Path, agent_id: str,
                      track: str) -> list[str] | None:
    """子专家工具白名单：专家 yaml tools 字段优先；缺省=全量裁剪。

    tools 缺键/null = 不限制（全量）；显式 ``[]`` = 一个业务工具都不启用，
    只保留收尾/计划原语（否则会被 ``or`` 兜底成全量，与勾选语义相反）。
    """
    expert = load_expert(packs_root, agent_id, track)
    tools = expert.get("tools")
    if isinstance(tools, list):
        if tools:
            return [t for t in tools if t not in _EXPERT_EXCLUDED]
        return sorted(_CONTROL_TOOLS | _PLAN_TOOLS)
    return None


class ChatTurn:
    """一轮对话的执行器（一个线程实例只跑一轮；消息历史自 store 装载）。"""

    # 类级默认值（2026-10-19）：`object.__new__(ChatTurn)` 的轻量构造（错误恢复
    # 回归测试、历史回放）不会走 __init__，此前 `_aborted`/`_system_prompt`/
    # `_dispatch_mcp` 直接读这些新属性会抛 AttributeError，把「工具失败」放大成
    # 整轮 unknown。正常构造一律在 __init__ 覆盖，语义不变。
    worker_control = None
    recovery = None
    is_orchestrator = False
    is_aggressive = False
    is_artex = False
    is_worker = False
    is_first_return_controller = False
    probe_mode = False
    intent_free = False
    web_search = False
    _worker_kill_reason = ""
    # 任务级 deadline 夹逼（artex-taskclock）：类级默认 0 = 不限，行为与引入前
    # 完全一致；`object.__new__` 轻量构造也不会 AttributeError。
    task_deadline_at = 0.0
    run_budget_s = 0.0
    budget_reason = ""
    _run_budget_clamped = False

    def __init__(self, *, bb, llm, project_id: str, thread_id: str,
                 packs_root: str | Path, track: str,
                 capabilities: list[str] | None,
                 mcp_bridge: MCPBridge | None = None,
                 expert_names: list[str] | None = None,
                 abort_event: threading.Event | None = None,
                 owner_tags: list[str] | None = None,
                 rule_profiles: dict[str, Any] | None = None,
                 artifacts_dir: str | Path | None = None,
                 browser_pool=None,
                 decompiler_factory=None,
                 containers=None,
                 assigned_asset_ids: list[str] | None = None,
                 probe_mode: bool = False,
                 artex_intent_id: str | None = None,
                 artex_task_id: str | None = None,
                 task_deadline_at: float = 0.0,
                 run_budget_s: float = 0.0,
                 worker_control: Callable[[], dict[str, Any]] | None = None,
                 artex_engine_provider: Callable[[], Any] | None = None,
                 artex_planner_llm=None,
                 recovery=None):
        self.bb = bb
        self.llm = llm
        self.project_id = project_id
        self.thread_id = thread_id
        self.packs_root = packs_root
        self.track = track
        self.capabilities = capabilities or []
        self.mcp_bridge = mcp_bridge
        self.expert_names = expert_names or []
        self.abort_event = abort_event or threading.Event()
        self.owner_tags = owner_tags or []
        self.rule_profiles = rule_profiles
        self.artifacts_dir = artifacts_dir
        self.browser_pool = browser_pool
        self.decompiler_factory = decompiler_factory
        self.containers = containers
        # 受控环境恢复器（项目级共享，None=不自动安装/构建）：装配到 dispatcher
        # 后 run_cmd 命中环境类失败才会走「恢复→重探→一次重试」。
        self.recovery = recovery
        self.assigned_asset_ids = [str(x).strip()
                                   for x in (assigned_asset_ids or [])
                                   if str(x).strip()]
        self.artex_intent_id = str(artex_intent_id or "").strip()
        self.artex_task_id = str(artex_task_id or "").strip()
        # 任务级 deadline 夹逼（artex-taskclock，对齐上游 agent/taskclock.go）：
        # Engine 把「任务根 Intent 起始 + task_deadline_s」推成的绝对 deadline 传
        # 进来，本 run 据此把自己的墙钟预算夹到任务剩余时间，并按**实际**收尾
        # reason 挑收尾词（per-run vs 任务超时，两套语义常相反）。
        # 缺省 0 = 不限 → 行为与引入前完全一致。
        try:
            self.task_deadline_at = max(0.0, float(task_deadline_at or 0.0))
        except (TypeError, ValueError):
            self.task_deadline_at = 0.0
        try:
            self.run_budget_s = max(0.0, float(run_budget_s or 0.0))
        except (TypeError, ValueError):
            self.run_budget_s = 0.0
        # budget_reason：本次收尾是「撞步数」还是「撞墙钟」（上游 TerminalReason
        # 同名同义）。空串 = 尚未收尾；进入收尾时由主循环写入。
        self.budget_reason = ""
        # _run_budget_clamped：任务 deadline 是否成了本次 run 的约束。它决定
        # 「超时收尾该说任务到点还是 run 预算耗尽」——两个正交信息，不能只看
        # eff 预算大小猜（见 core/chat/taskclock.clamp_max_duration 注释）。
        self._run_budget_clamped = False
        # ARTEX Worker 控制（2026-10-19）：Engine 注入的 poll 回调，返回
        # {"kill": 原因, "steer": [方向...]}。kill 走中止通道（流式期间也能
        # 生效），steer 在步边界作为 user 消息注入下一轮；回调是"取出并清空"
        # 语义，故本地缓存 steer 防高频轮询丢消息。
        self.worker_control = worker_control if callable(worker_control) else None
        self._worker_kill_reason = ""
        self._pending_steer: list[str] = []
        self._worker_poll_at = 0.0
        # ARTEX 主控操舵通道（artex-fusion，2026-10-19）：对话式主控不再自己
        # call_expert 派活，而是通过 artex_plan / artex_status / artex_hint /
        # artex_control 驱动项目级 ArtexEngine（Planner 生成 Intent、Engine
        # 调度、Worker 执行）。engine_provider 惰性取引擎——引擎可能在本轮
        # 对话期间才被创建；planner_llm 缺省回落到执行模型。
        self.artex_engine_provider = (artex_engine_provider
                                      if callable(artex_engine_provider) else None)
        self.artex_planner_llm = artex_planner_llm or llm
        # aggressive worker 只接收父控通过 task 明确交代的内容，不读取或注入
        # 主控的资产/维度上下文；该标记在构造子线程时由 probe_mode 传入。
        self._isolated_worker = bool(probe_mode and self.thread_id)
        # probe_mode（2026-10-17，激进主控子代理）：True=快速子任务线程——
        # 工具面剔除 bb_query + 意图三件套（_PROBE_EXCLUDED_TOOLS）、意图
        # 闸与「发现必挂意图」豁免（ToolDispatcher intent_free）、维度收敛闸
        # 跳过；只消费主控通过 user task 传入的任务，不注入主控提示。
        # 仅 _tool_call_expert 在 self.is_aggressive 时置 True（chat-orchestrator
        # 子专家不受影响）。
        self.probe_mode = bool(probe_mode)
        self.thread = chat_store.get_thread(bb, thread_id) or {}
        self.agent_id = str(self.thread.get("agent_id") or ORCHESTRATOR_ID)
        # 联网搜索开关（v43，web-search-toggle）：线程级。composer「联网」打开 =
        # 本线程主控 + 其 spawn 的子代理（call_expert / ARTEX Worker 建线程时
        # 从父线程继承该位）默认拿 web_search 工具；工具面（_tool_specs）与
        # 执行白名单（_build_dispatcher）双边同步。不进专家 yaml 勾选——这是
        # 会话级用户决定，不是专家能力声明。子代理不因开了联网就自动外发：
        # 模型不调工具即不触发；SearXNG/ddgs 双后端与降级在 core/websearch.py。
        self.web_search = bool(self.thread.get("web_search"))
        # worker.yaml 可声明 intent_free；该配置与激进主控派生的 probe_mode
        # 语义一致，允许独立 worker 不声明意图。
        try:
            worker_cfg = load_expert(self.packs_root, self.agent_id, self.track)
            self.intent_free = bool(worker_cfg.get("intent_free", False))
        except Exception:  # noqa: BLE001
            self.intent_free = False
        # 控制器类智能体（chat-orchestrator + 激进主控）：拿主控工具面、
        # 可委派专家池。两者只在调度策略上不同（见 _system_prompt 与
        # _parallel_expert_dispatch 的 first_return）。
        self.is_orchestrator = self.agent_id in CONTROLLER_IDS
        self.is_aggressive = self.agent_id == AGGRESSIVE_CONTROLLER_ID
        self.is_artex = self.agent_id == ARTEX_CONTROLLER_ID
        self.is_first_return_controller = self.is_aggressive or self.is_artex
        self.is_worker = self.agent_id == "worker"
        # 先返先触发的后台探针账本（仅激进主控写读）：
        # _probe_abandoned——已被提前断候的 tool_call id（worker 完成时不
        #   独占该 id，改推 overflow + 以同 id 续写真实 tool 行）；
        # _probe_overflow——已完成待注入的真实结果，主循环在每个步边界
        #   （下一次 LLM 请求前）_drain_probe_overflow 排干。
        self._probe_lock = threading.Lock()
        self._probe_abandoned: set[str] = set()
        self._probe_overflow: list[dict[str, Any]] = []
        # 预算耗尽标记（artex-exhausted）：本轮是否因**撞步数上限**而被动收尾。
        # 与「正常给出终稿」严格区分——上游 server/engine.go:1133-1146 的口径是
        # 「撞步数上限 ≠ 完成」：此时该方向只写回了部分事实，Planner 必须知道它
        # 「试过但没做完、需换角度或接着跑」，而不是当成已覆盖永久跳过。
        # 由 ``run()`` 在结束时读取，``make_worker`` 据此把 Intent 置 exhausted。
        self.budget_exhausted = False
        # 收尾阶段标志（artex-wrapup）：撞预算后进入 settlement 时为 True——
        # 工具面/白名单被裁成「只能写回」，_dispatch 另有一道硬闸。
        self._wrapup_active = False
        self._dispatcher = self._build_dispatcher()

    @property
    def _is_spawned_subagent(self) -> bool:
        """被 call_expert spawn 的子专家线程（有父线程 + 派发任务留档）。

        控制器类智能体若被当专家委派（expert_names=[] 本就堵死），这层是
        语义兜底：spawn 深度恒为 1，子线程一律不可再委派。"""
        return bool(self.thread.get("parent_thread_id")
                    and self.thread.get("spawned_task"))

    def _aborted(self) -> bool:
        if self.abort_event.is_set():
            return True
        if self.worker_control is None:
            return False
        self._poll_worker_control()
        return bool(self._worker_kill_reason)

    def _poll_worker_control(self, *, force: bool = False) -> None:
        """拉取 Engine 的 Worker 控制（节流 0.2s，流式回调高频调用）。

        poll 回调会清空 steer 队列，因此取到的方向提示一律进本地缓存，
        由步边界统一注入，避免被 `_aborted` 的高频轮询吞掉。
        """
        if self.worker_control is None:
            return
        now = time.monotonic()
        if not force and now - self._worker_poll_at < 0.2:
            return
        self._worker_poll_at = now
        try:
            control = self.worker_control() or {}
        except Exception:  # noqa: BLE001 —— 控制通道故障不阻断 Worker
            log.exception("ARTEX Worker 控制轮询失败 intent=%s",
                          self.artex_intent_id)
            return
        for text in control.get("steer") or []:
            text = str(text or "").strip()
            if text:
                self._pending_steer.append(text)
        kill = str(control.get("kill") or "").strip()
        if kill and not self._worker_kill_reason:
            self._worker_kill_reason = kill

    def _take_pending_steer(self) -> list[str]:
        pending = self._pending_steer
        self._pending_steer = []
        return pending

    # ---------- 装配 ----------

    def _open_intents_of_self(self) -> list[str]:
        """本线程（author=chat-<threadid>）的 open 意图 id 列表（收尾前拦截用）。"""
        author = f"chat-{self.thread_id[-12:]}"
        rows = self.bb.conn.execute(
            "SELECT id FROM intents WHERE project_id=? AND status='open'"
            " AND author=? ORDER BY created_at",
            (self.project_id, author)).fetchall()
        return [r["id"] for r in rows]

    def _mission_context(self) -> str:
        """子专家工作台上下文：分配资产台账 + 测试维度清单 + plan→execute→plan 纪律。

        注入 system（不污染消息历史），使子专家知道主控划定的测试范围、每个测试
        面的 dimension 取值与收尾纪律。**分配资产（assigned_asset_ids）非空时**
        只列这些资产并要求逐面覆盖；未分配（查询/登记类委派）则列出项目台账供上下
        文，不做收敛强制。读取失败降级为占位文本，不阻断。"""
        try:
            all_assets = self.bb.list_assets(self.project_id)
        except Exception:  # noqa: BLE001 —— 注入失败不阻断委派
            log.exception("chat 子专家资产台账读取失败 thread=%s", self.thread_id)
            all_assets = []
        by_id = {str(a.get("id")): a for a in all_assets}
        lines = ["## 工作台委派上下文"]
        if self.assigned_asset_ids:
            lines.append("### 本线程分配资产（只测这些，越界须先上报主控）")
            missing = [i for i in self.assigned_asset_ids if i not in by_id]
            for aid in self.assigned_asset_ids:
                a = by_id.get(aid)
                if a is None:
                    continue
                value = str(a.get("value") or "").strip().replace("\n", " ")[:200]
                lines.append(
                    f"- id={aid} type={a.get('type')} "
                    f"status={a.get('status')} value={value or '<空>'}")
            if missing:
                lines.append(f"- （以下分配 id 在黑板上不存在，须向主控确认：{missing}）")
            others = len(all_assets) - len(self.assigned_asset_ids)
            if others > 0:
                lines.append(
                    f"- 项目另有 {others} 条未分配资产：仅可用 bb_add_asset 上报，"
                    "不要直接测试——由主控另行派发。")
        else:
            lines.append("### 当前项目测试资产台账（供上下文，测试须先确认主控已分配）")
            shown = all_assets[:_ASSET_LEDGER_CAP]
            if not shown:
                lines.append("- （黑板当前无资产行；如需测试目标，先确认主控是否已落资产）")
            for a in shown:
                value = str(a.get("value") or "").strip().replace("\n", " ")[:200]
                lines.append(
                    f"- id={a.get('id')} type={a.get('type')} "
                    f"status={a.get('status')} value={value or '<空>'}")
            if len(all_assets) > _ASSET_LEDGER_CAP:
                lines.append(
                    f"- …（共 {len(all_assets)} 条，仅注入前 {_ASSET_LEDGER_CAP} 条）")
        try:
            dims = load_track_dimensions(self.packs_root, self.track)
        except Exception:  # noqa: BLE001 —— 维度清单失败视同无面可查
            log.exception("chat 子专家维度清单加载失败 thread=%s", self.thread_id)
            dims = []
        if self.probe_mode:
            lines.append("### 测试维度面清单（仅供参考；本线程不声明意图）")
        else:
            lines.append("### 测试维度面清单（declare_intent 的 dimension 取值）")
        if dims:
            for d in dims:
                intent = (d.get("intent") or "").replace("\n", " ").strip()[:200]
                hint = (d.get("evidence_hint") or "").replace("\n", " ").strip()[:200]
                lines.append(
                    f"- id={d['id']} name={d.get('name')} "
                    f"applies_to={','.join(d.get('applies_to') or [])}")
                if intent:
                    lines.append(f"  intent={intent}")
                if hint:
                    lines.append(f"  evidence_hint={hint}")
        else:
            lines.append("- （本轨无维度清单，不强制面收敛）")
        if self.probe_mode:
            lines.append(
                "### 快速子任务纪律\n"
                "只查任务描述中的一个方向，几分钟内跑完即收尾；不深挖、不盘点"
                "黑板、不声明意图、不多方向。结论摘要以 [命中]/[未命中] 开头，"
                "后跟一句证据（命中=该方向有值得主控跟进的信号）。")
        else:
            lines.append(
                "### plan→execute→plan 纪律\n"
                "先 plan：对目标资产用 declare_intent(target_asset_id=..., dimension=..., "
                "statement=...) 逐面声明测试假设；再 execute；每条意图用 close_intent "
                "收尾。收尾摘要前自检：已声明资产还有适用维度未覆盖时，先补 plan 再执行，"
                "不要提前给最终摘要。")
        return "\n".join(lines)

    def _workspace_files_context(self) -> str:
        """CTF 主控可见的项目文件清单，统一输出容器路径，避免重复猜路径。"""
        if not self.artifacts_dir:
            return ""
        root = Path(self.artifacts_dir).parent
        rows: list[str] = []
        for rel_root in ("samples", "artifacts/attachments", "artifacts"):
            base = root / rel_root
            if not base.is_dir():
                continue
            try:
                for p in sorted(base.rglob("*"), key=lambda x: str(x).lower()):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(root).as_posix()
                    if rel.startswith("artifacts/") and rel_root == "artifacts":
                        # attachments 已在前一轮列过，避免重复。
                        if rel.startswith("artifacts/attachments/"):
                            continue
                    try:
                        size = p.stat().st_size
                    except OSError:
                        size = 0
                    rows.append(f"- /workspace/{rel} ({size} bytes)")
                    if len(rows) >= 100:
                        break
            except OSError:
                continue
            if len(rows) >= 100:
                break
        if not rows:
            return "## CTF 项目文件清单\n- （samples/ 与 artifacts/ 下暂无文件）"
        return ("## CTF 项目文件清单（容器内路径，优先直接使用，不要重复 find）\n"
                + "\n".join(rows))

    def _challenge_context(self) -> str:
        """CTF 题目上下文（challenge-asset，2026-10-14）：把「本线程归属的题目」
        显式注入 system，并交代建题与归属纪律。非 CTF 轨返空串（不注入）。

        用户的实际用法是把「题目描述 + 附件」一起丢进输入框，所以**资产为空是
        常态**——这道题的资产节点通常还不存在。注入告诉 Agent：看到新题就调
        `bb_declare_challenge` 立起来；立起来之后本线程的 `declare_intent` /
        `bb_add_finding` 不必再传 `target_asset_id`（runtime 侧同样有兜底，
        双保险——Agent 忘了声明，第一次落发现时也会按线程标题补建）。
        """
        if self.track != "ctf":
            return ""
        cid, name, desc = "", "", ""
        try:
            cid = chat_store.resolve_challenge_asset_id(self.bb, self.thread_id) or ""
            if cid:
                a = self.bb.get_asset(cid)
                if a is not None:
                    name = str(a.get("value") or "").strip()
                    desc = str((a.get("meta") or {}).get("title") or "").strip()
        except Exception:  # noqa: BLE001 —— 注入失败降级为「尚未建题」文案
            log.exception("chat 题目上下文读取失败 thread=%s", self.thread_id)
            cid = ""
        lines = ["## CTF 题目上下文（一题一线程）"]
        if cid:
            lines.append(f"- 当前题目：id={cid} 名字={name or '<未命名>'}")
            if desc:
                lines.append(f"  描述：{desc}")
            lines.append(
                "- 后续本线程的 declare_intent / bb_add_finding **不必再填**"
                " target_asset_id，会自动归到这道题下；附件也无需单独登记。")
        else:
            lines.append(
                "- 本线程**尚未登记题目**。题目描述与附件都由用户在输入框给出，"
                "确认是一道新题时先调 bb_declare_challenge("
                "name=<简短题目名>, description=<一句话题目描述>,"
                " tags=[<题型>]) 把题立起来；同一道题重复调用会复用既有节点，"
                "不会建出重复行。")
            lines.append(
                "- 立题之后本线程的 declare_intent / bb_add_finding 不必再填"
                " target_asset_id，会自动归到这道题下。")
        # flag 判定纪律（ctf-flagjudge，2026-10-19）：把「拿到疑似 flag 怎么办」
        # 从纯自然语言约束升级成可执行工具调用——模型自己说「解出来了」不再算数。
        lines.append(
            "- **拿到疑似 flag 一律用 `ctf_submit_flag(flag=…, evidence=…)` 登记**，"
            "不要直接 bb_add_finding 报 flag。它会做三道机械判定："
            "① 形态置信（题面占位符 `flag{example}`/`flag{test}`、模板语法 "
            "`{% if %}`、诱饵前缀 `fake_flag{}` 会被拒）；② 变换回环（若 flag 是 "
            "base64/hex/url/rot13 解出来的，传 `source`=原始字节 + `transform`=变换名，"
            "它会验证「编码回去确实出现在 source 里」——瞎猜的过不了）；③ 跨题防串"
            "（内层与其它题目已登记 flag 高度相似 → 拒收，防多题并发串号）。"
            "判定通过后写入 **flag 台账**（题目状态的唯一真值），本题进度按 "
            "`meta.flag_count` 算——**拿满即解出，调度层不再为它派 Worker**。")
        lines.append(
            "- 回执里会给出 `本题进度 N/M`：M>N 说明是多 flag 题，**继续找剩下的**，"
            "别因为拿到一个 flag 就收工。")
        lines.append(
            "- 人工可能把某串标为**错误 flag**（回执会明确说「已被人工标记为错误」）："
            "**不要重复提交同一串**，回题目重新找线索。")
        lines.append(
            "- 管理员密码/API key/数据库口令这类**非 flag 格式**的命中，"
            "用 `ctf_submit_flag(…, candidate=true)` 登记为候选线索（intel/未验证），"
            "不会判为解出，仍需继续挖正式 flag。")
        pctx = self._pacing_context()
        if pctx:
            lines.append(pctx)
        lines.append(
            "- 弱口令/默认口令题不要只凭记忆试 `admin/admin`：把题面线索（产品名、"
            "年份、管理员名字）交给 `ctf_ammo(hint_text=…, obs_text=…)` 生成候选表"
            "（`pairs=true` 给 `user:pass`），写入 scratch 后再用 run_cmd 消费。"
            "它只产候选，不爆破、不发请求。")
        return "\n".join(lines)

    def _pacing_context(self) -> str:
        """CTF 时间预算（ctf-pacing，2026-10-19）：把「这场还能花多久」变成可算的数。

        **要解决的问题**：CTF 里所有时间判断都是相对的——同一个「这题再花 40 分钟
        就放弃」在 2 小时短赛是半场比赛、在 48 小时长赛是零头。硬编码阈值必然两头
        都不对。`core.ctf.pacing` 把阈值乘以「总时长 / 题量」的缩放系数解决这件事，
        本方法负责把结果**注入提示词**（否则内核再好也没人看）。

        配置来源 `config.ctf`（项目 config 浅合并，无需新增归一化）：
        ``{"deadline": "<ISO8601 或 epoch 秒>", "tasks": <题目总数>}``。
        都没配 → 用 `Pacing` 的阶梯推断（按本线程已跑时长向上猜赛制），仍给出
        收尾窗口提示——**不知道赛制不等于没有时间感**。

        非 CTF 轨返空串（不注入）；读取失败降级返回空串（绝不阻断对话）。
        """
        if self.track != "ctf":
            return ""
        try:
            from core.ctf import pacing as ctf_pacing
            proj = self.bb.get_project(self.project_id) or {}
            cfg = (proj.get("config") or {}).get("ctf") or {}
            n_tasks = int(cfg.get("tasks") or 0) or ctf_pacing.ANCHOR_NTASKS
            started = self._thread_started_at()
            deadline = cfg.get("deadline")
            if deadline:
                ts = _to_epoch(deadline)
                if ts:
                    p = ctf_pacing.Pacing.from_deadline(ts, n_tasks=n_tasks,
                                                        started_at=started)
                else:
                    p = ctf_pacing.Pacing(n_tasks=n_tasks, started_at=started)
            else:
                p = ctf_pacing.Pacing(n_tasks=n_tasks, started_at=started)
            desc = p.describe()
            cap = p.hard_total_cap_s() // 60
            line = (f"- 时间预算（ctf-pacing）：{desc}；"
                    f"单题硬上限按 {cap} 分钟算")
            if p.in_endgame():
                line += ("。**已进收尾窗口**——停止开新战线，只做「能交的题收尾」"
                         "与 writeup，别再启动新的探索方向。")
            return line
        except Exception:  # noqa: BLE001 —— 时间感注入失败绝不阻断对话
            log.exception("chat 时间预算读取失败 thread=%s", self.thread_id)
            return ""

    def _thread_started_at(self) -> float | None:
        """本线程创建时间（epoch 秒）；读不到返回 None（Pacing 视作刚开始）。"""
        try:
            row = self.bb.conn.execute(
                "SELECT created_at FROM chat_threads WHERE id=?",
                (self.thread_id,)).fetchone()
            if row is None or not row["created_at"]:
                return None
            return _to_epoch(row["created_at"])
        except Exception:  # noqa: BLE001
            return None

    def _situation_context(self) -> str:
        """主控态势注入（asset-tri-state D8，2026-10-09）：三态分桶让主控看得见
        「哪些没测、哪些测了没测尽、哪些测干净」，据此规划委派；否则主控只能靠
        bb_query 手动搜资产拼现状。

        与 `_mission_context`（子专家）对称：注入 system 不污染消息历史，**每轮
        重投影**（态势随黑板演进）。数据源=`core.coverage.situation_snapshot`
        （读时派生三态，纯函数）。读取失败降级占位，不阻断对话。"""
        try:
            assets = self.bb.list_assets(self.project_id)
            findings = self.bb.list_findings(self.project_id)
            snap = situation_snapshot(assets, findings)
        except Exception:  # noqa: BLE001 —— 注入失败不阻断委派
            log.exception("chat 主控态势读取失败 thread=%s", self.thread_id)
            return ("## 项目资产态势\n"
                    "- （态势读取失败，请用 bb_query 自查当前资产与发现）")
        counts = snap["counts"]
        untested, visited = snap["untested"], snap["visited"]
        lines = ["## 项目资产态势（三态，据此规划委派）"]
        if not (untested or visited or snap["clean_ids"]):
            lines.append("- （黑板当前无测试资产：先 bb_query 确认，必要时委派侦察登记）")
            return "\n".join(lines)

        def _row(it: dict) -> str:
            val = str(it.get("value") or "<空>").replace("\n", " ")[:60]
            mark = " ⚠有洞" if it.get("has_findings") else ""
            return f"  - id={it['id']} type={it.get('type')} value={val}{mark}"

        lines.append(f"- **未测试 open：{len(untested)} 条**（优先派发）")
        lines.extend(_row(it) for it in untested)
        if snap["untested_truncated"]:
            lines.append("  - …（仅列前 30 条，用 bb_query 看全量）")
        lines.append(f"- **已访问 visited：{len(visited)} 条**"
                     "（碰过、未测尽——续派按未覆盖测试面继续深挖，不得重复已覆盖面）")
        lines.extend(_row(it) for it in visited)
        if snap["visited_truncated"]:
            lines.append("  - …（仅列前 20 条，用 bb_query 看全量）")
        lines.append(f"- **已测试干净 tested_clean：{counts.get('tested_clean', 0)} 条**"
                     "（勿重复派发；新增子资产会自动使其掉出 clean，重新纳入）")
        lines.append(
            "纪律：优先委派**未测试**资产；**已访问**资产续派时按未覆盖的测试面深挖；"
            "**已测干净**资产不再派发。委派时用 call_expert(asset_ids=...) 显式划定"
            "该子专家的资产范围，避免多个子专家重复测试同一资产。")
        return "\n".join(lines)

    def _blueprint_context(self) -> str:
        """蓝图注入（R4 消费面接线 M1，2026-10-13）：把项目开发蓝图（模块级业务
        功能↔函数锚点）摊给主控与子专家——此前 AI 侧完全读不到 blueprints 表，
        模块深析/重建无从规划。**紧凑形态**：只给蓝图名/status/模块名/status/锚点
        数，**不进 spec/notes 全文**（与 func_kb/findings 共享物化预算；详情让 Agent
        用 `bb_query what=blueprint` 自取）。读取失败降级空串，不阻断对话。"""
        try:
            bps = self.bb.list_blueprints(self.project_id)
        except Exception:  # noqa: BLE001 —— 注入失败不阻断委派
            log.exception("chat 蓝图读取失败 thread=%s", self.thread_id)
            return ""
        if not bps:
            return ""
        lines = ["## 开发蓝图（R4：模块级业务功能↔函数锚点）"]
        for bp in bps[:_BLUEPRINT_CAP]:
            name = str(bp.get("name") or "")
            status = str(bp.get("status") or "draft")
            goal = str(bp.get("goal") or "").replace("\n", " ").strip()
            lines.append(f"- {name}（{status}）" + (f"｜目标 {goal[:40]}" if goal else ""))
            mods = [m for m in (bp.get("modules") or []) if isinstance(m, dict)]
            for m in mods[:_BLUEPRINT_MODULE_CAP]:
                anchors = len(m.get("func_addresses") or [])
                lines.append(f"  - {m.get('name')} [{m.get('status')}]"
                             + (f" 锚点 {anchors}" if anchors else ""))
            if len(mods) > _BLUEPRINT_MODULE_CAP:
                lines.append(f"  - …共 {len(mods)} 个模块")
        if len(bps) > _BLUEPRINT_CAP:
            lines.append(f"- …共 {len(bps)} 份蓝图")
        lines.append("纪律：模块深析/重建按模块锚点函数展开；接口 spec 与蓝图正文用 "
                     'bb_query(what="blueprint", blueprint_id="…") 自取。')
        return "\n".join(lines)

    def _dimension_gate_status(self) -> dict | None:
        """子专家维度收敛态；None=不启用（主控/非 spawn/无目标资产/无面可查）。

        收敛目标 = 主控**显式分配的资产**（assigned_asset_ids）∪ 本线程意图锚定
        的资产（intents.target_asset_id）。两者都空 → 不启用（查询/登记类委派不
        受强制）。分配了资产但无任何意图 → empty=True（没 plan），推回声明；否则
        逐资产按类型匹配适用维度做 dimension_coverage，ok=所有适用面都覆盖。

        **probe_mode（激进主控子代理）恒 None**：收敛口径是「意图覆盖」，
        而探针按用户要求不声明意图（无 declare_intent 工具）→ 不跳过会永远
        empty=True 推回，与探针语义直接互斥。"""
        if self.is_orchestrator:
            return None
        # ARTEX Worker（有 artex_intent_id）：覆盖纪律由任务树/Planner 承担，
        # 不再叠加「按资产全维度覆盖」闸——否则挂了父线程血缘后，一个只测
        # Web 面的 Intent 会被反复推回要求补测其余维度，与任务边界冲突。
        if self.artex_intent_id:
            return None
        if self.probe_mode or self.intent_free:
            return None
        if not self._is_spawned_subagent:
            return None
        try:
            dims = load_track_dimensions(self.packs_root, self.track)
        except Exception:  # noqa: BLE001 —— 读取失败视同无面可查，不误拦
            log.exception("chat 维度收敛读取失败 thread=%s", self.thread_id)
            dims = []
        if not dims:
            return None
        author = f"chat-{self.thread_id[-12:]}"
        try:
            rows = self.bb.conn.execute(
                "SELECT id, target_asset_id, dimension, status FROM intents"
                " WHERE project_id=? AND author=? AND target_asset_id IS NOT NULL"
                " ORDER BY created_at",
                (self.project_id, author)).fetchall()
        except Exception:  # noqa: BLE001 —— 查询失败不阻断对话
            log.exception("chat 维度意图查询失败 thread=%s", self.thread_id)
            return None
        intents = [dict(r) for r in rows]
        plain_ids = {str(r.get("target_asset_id") or "").strip() for r in intents}
        target_ids = sorted({i for i in self.assigned_asset_ids if i} | plain_ids)
        if not target_ids:
            return None
        covered: list[str] = []
        uncovered: list[str] = []
        for aid in target_ids:
            asset = self.bb.get_asset(aid)
            if asset is None:
                continue
            asset_intents = [r for r in intents
                             if str(r.get("target_asset_id") or "").strip() == aid]
            dims_for = dimensions_for_type(dims, str(asset.get("type") or ""))
            if not dims_for:
                continue
            cov = dimension_coverage(asset_intents, dims_for)
            covered.extend(cov["covered"])
            uncovered.extend(cov["uncovered"])
        return {"ok": not uncovered, "empty": not plain_ids,
                "covered": sorted(set(covered)),
                "uncovered": sorted(set(uncovered))}

    def _dimension_gate_nudge(self, status: dict) -> str:
        """维度收敛闸的推回文案：明确未覆盖维度，要求先声明意图再执行。"""
        if status.get("empty"):
            return ("你正准备收尾，但本线程还没有任何已锚定资产的测试意图。"
                    "请先 plan：用 declare_intent(target_asset_id=..., dimension=..., "
                    "statement=...) 逐面声明测试假设，再执行并用 close_intent 收尾；"
                    "不要提前给最终摘要。")
        names: dict[str, str] = {}
        try:
            names = {d["id"]: d.get("name") or d["id"]
                     for d in load_track_dimensions(self.packs_root, self.track)}
        except Exception:  # noqa: BLE001 —— 名称缺失用 id 兜底
            pass
        ids = status.get("uncovered") or []
        detail = "\n".join(f"- {i}（{names.get(i, i)}）" for i in ids)
        return ("你准备收尾，但以下测试面还没有覆盖（未覆盖 = 该面无 closed 意图，"
                "或还有 open 意图未收尾）：\n"
                + detail
                + "\n请先 plan：用 declare_intent(dimension=<面id>, target_asset_id="
                  "<资产id>, statement=...) 声明未覆盖面，执行后 close_intent 收尾；"
                  "全部适用面覆盖后再给最终摘要。")

    def _build_dispatcher(self) -> ToolDispatcher | None:
        """复用 ToolDispatcher 的黑板/技能/网关工具处理器（run_cmd/http/
        kb/skill/bb_* 全套安全机制免费拿）；allowed_tools 按线程角色收敛。"""
        try:
            if self.is_orchestrator:
                if self.is_aggressive:
                    base = (*_CTF_CORE_TOOLS, "bb_declare_challenge")
                elif self.is_artex:
                    base = tuple(_ORCH_BASE_TOOLS) + ("run_cmd", "read_file",
                                                       "search_files")
                else:
                    base = _ORCH_BASE_TOOLS
                allowed = [t for t in base if t != "todo_write"
                           and t != "call_expert"]
            else:
                names = expert_tool_names(self.packs_root, self.agent_id,
                                          self.track)
                if names is None:
                    # CTF worker 的默认工具面只保留解题所需能力；其他轨道继续
                    # 使用原有全量裁剪语义。
                    names = (list(_CTF_CORE_TOOLS)
                             if self.track == "ctf" and (self.is_worker or self.probe_mode)
                             else [s["name"] for s in AGENT_TOOLS
                                   if s["name"] not in _EXPERT_EXCLUDED])
                allowed = names
            allowed = [t for t in allowed
                       if t not in ("todo_write", "call_expert")]
            # 探针子任务（激进主控子代理，probe_mode）：执行层与 _tool_specs
            # 同口径剔除 bb_query 与意图三件套——双边同步防止「schema 有、
            # 执行拒」或「执行放行、schema 无」的撕裂。
            if self.probe_mode or self.is_aggressive or self.is_worker or self.intent_free:
                allowed = [t for t in allowed
                           if t not in _PROBE_EXCLUDED_TOOLS]
            # 知识库迭代开关（2026-10-15）：关 = Agent 不提任何提案 → 工具面剔除
            # propose_pack_edit（与 _maybe_kb_iteration/_system_prompt 同口径实时读）；
            # ToolDispatcher 执行层另有一道硬闸，双保险。
            if not self._kb_iteration_on():
                allowed = [t for t in allowed if t != "propose_pack_edit"]
            # 联网搜索开关（v43，web-search-toggle）：线程级，主控开 = 子代理
            # 继承（call_expert/ARTEX Worker 建线程时从父线程带位）。工具面与
            # 执行白名单双边同步（与 probe 剔除同纪律），专家 yaml 无需声明——
            # 这是会话级用户决定，不是专家能力。
            if self._web_search_on() and "web_search" not in allowed:
                allowed = [*allowed, "web_search"]
            # Worker 间过程级信息交换（v46）：ARTEX Worker 放行 trace 查询三件套。
            if self.artex_intent_id and not self.is_orchestrator:
                for tname in _WORKER_TRACE_TOOLS:
                    if tname not in allowed:
                        allowed = [*allowed, tname]
                # 事实写回（artex-record-fact）：与 schema 追加**双边同步**。
                if RECORD_FACT_TOOL_NAME not in allowed:
                    allowed = [*allowed, RECORD_FACT_TOOL_NAME]
            # 收尾阶段（artex-wrapup）：白名单裁成「只能写回」。与 _tool_specs
            # 的同类裁剪**双边同步**——只裁一边会撕裂（schema 有执行拒 / 执行放行
            # schema 无）。放在最后，确保它裁掉前面所有分支刚加进来的探测类工具。
            if getattr(self, "_wrapup_active", False):
                allowed = filter_wrapup_whitelist(allowed)
            if not allowed:
                return None
            expert = load_expert(self.packs_root, self.agent_id, self.track) \
                if not self.is_orchestrator else {}
        except Exception:  # noqa: BLE001 —— 白名单/专家 yaml 读取失败降级无工具面
            log.exception("chat 工具面白名单装配失败 thread=%s", self.thread_id)
            return None
        try:
            dispatcher = ToolDispatcher(
                self.bb, ExecutionGateway(bb=self.bb, containers=self.containers),
                project_id=self.project_id,
                session_id=f"chat-{self.thread_id[-12:]}",
                author=f"chat-{self.thread_id[-12:]}",
                packs_root=self.packs_root, track=self.track,
                capabilities=self.capabilities,
                allowed_tools=allowed,
                abort_event=self.abort_event,
                artifacts_dir=self.artifacts_dir,
                role_skills=([] if (self.is_aggressive or self.probe_mode or self.is_worker)
                             else expert.get("skills")),
                assigned_asset_ids=self.assigned_asset_ids,
                intent_free=(self.probe_mode or self.intent_free),
                environment_id=(f"ctf-{self.project_id}" if self.track == "ctf" else None),
                thread_id=self.thread_id,
                recovery=self.recovery)
        except Exception:  # noqa: BLE001 —— 装配失败降级为无黑板工具
            log.exception("chat ToolDispatcher 装配失败 thread=%s", self.thread_id)
            return None
        # 重装备构造后挂载（与任务链会话工厂同款）：browser 按轨注入共享池
        # （轨外 app.py 传 None）；decompiler 每线程一实例（工厂闭包携带
        # gateway/ida_mcp 上下文），失败降级 None（headless 缓存语义不破）
        dispatcher.browser = self.browser_pool
        if self.decompiler_factory is not None:
            try:
                dispatcher.decompiler = self.decompiler_factory(
                    session_id=f"chat-{self.thread_id[-12:]}",
                    author=f"chat-{self.thread_id[-12:]}")
            except Exception:  # noqa: BLE001
                log.exception("chat decompiler 装配失败 thread=%s", self.thread_id)
        # C6 漏洞核对 hook（2026-10-16，rating-basis-gate）：与任务链同构，把
        # vuln_gate 接进对话路径——**仅当项目勾选了「评级与价值口径（rating）」时**
        # 接线（gate 的规则串里要有判级条款才谈得上校准；未勾选的项目维持原行为，
        # 零新增 LLM 调用）。动机：此前对话子专家 category=vuln 登记完全绕过核对与
        # 定级校准（vuln_gate 只在 core/agent/loop.py 接线），叠加 rating_basis 软约束
        # → 上游异常期一次生成滑坡就静默漏依据。gate 内部失败一律降级跳过，不阻断。
        if rating_profile_active(self.packs_root, self.track, self.rule_profiles):
            try:
                from core.skills.judge import build_vuln_gate
                dispatcher.vuln_gate = build_vuln_gate(
                    self.llm, packs_root=self.packs_root, track=self.track,
                    capabilities=self.capabilities, owner_tags=self.owner_tags,
                    rule_profiles=self.rule_profiles)
            except Exception:  # noqa: BLE001 —— 接线失败降级为不核对
                log.exception("chat vuln_gate 接线失败 thread=%s", self.thread_id)
        return dispatcher

    def _tool_specs(self) -> list[dict[str, Any]]:
        if self.is_orchestrator:
            if self.is_aggressive:
                base = (*_CTF_CORE_TOOLS, "bb_declare_challenge")
            elif self.is_artex:
                base = tuple(_ORCH_BASE_TOOLS) + ("run_cmd", "read_file",
                                                   "search_files")
            else:
                base = _ORCH_BASE_TOOLS
            names = {t for t in base if t not in ("todo_write", "call_expert")}
            specs = _specs_by_names(names)
            specs = [*self._orch_tool_specs(), *specs]
        else:
            names = set(expert_tool_names(self.packs_root, self.agent_id,
                                          self.track)
                        or (_CTF_CORE_TOOLS
                            if self.track == "ctf" and (self.is_worker or self.probe_mode)
                            else [s["name"] for s in AGENT_TOOLS
                                  if s["name"] not in _EXPERT_EXCLUDED]))
            specs = _specs_by_names(names)
        # 探针子任务（激进主控子代理，probe_mode）：bb_query 与意图三件套
        # 不下发——「不查询黑板、不声明意图」在工具面上落实（执行层同步裁，
        # 见 _build_dispatcher）。放在 mcp/知识库开关过滤之前，与最终
        # bb_query_schema_for 替换互不干扰（bb_query 已不在表里）。
        if self.probe_mode or self.is_aggressive or self.is_worker or self.intent_free:
            specs = [s for s in specs
                     if s.get("name") not in _PROBE_EXCLUDED_TOOLS]
        if self.mcp_bridge is not None:
            mcp_specs = self.mcp_bridge.tool_specs()
            # 专家显式勾选 MCP server 时，仅暴露这些 server；缺省沿用全局已启用集合。
            if not self.is_orchestrator:
                try:
                    mcp_allow = load_expert(self.packs_root, self.agent_id, self.track).get("mcp_servers")
                    if isinstance(mcp_allow, list):
                        allowed_servers = {str(x).strip() for x in mcp_allow if str(x).strip()}

                        def _server_of(spec: dict[str, Any]) -> str:
                            parts = str(spec.get("name") or "").split("__", 2)
                            return parts[1] if len(parts) == 3 else ""

                        mcp_specs = [s for s in mcp_specs
                                     if _server_of(s) in allowed_servers]
                except Exception:  # noqa: BLE001
                    log.exception("chat MCP 专家白名单读取失败 thread=%s", self.thread_id)
            specs = [*specs, *mcp_specs]
        # 知识库迭代开关（2026-10-15）：关 = 不下发 propose_pack_edit 的 schema
        # （LLM 看不到工具就不会调；执行层硬闸 + dispatcher 白名单三重保险）。
        if not self._kb_iteration_on():
            specs = [t for t in specs if t.get("name") != "propose_pack_edit"]
        # 联网搜索开关（v43）：开 = 追加 web_search 的 schema（LLM 看得见才调得
        # 动；执行层白名单同步放行）。放在过滤链尾部、bb_query 轨裁剪之前，
        # 与既有开关互不干扰；已声明过（专家 yaml 列了）则不重复挂。
        if self._web_search_on() and not any(
                s.get("name") == "web_search" for s in specs):
            specs = [*specs, *_specs_by_names({"web_search"})]
        # Worker 间过程级信息交换（v46，artex-traces）：ARTEX Worker（有 intent_id）
        # 动态追加 trace 查询三件套，已声明过（专家 yaml 列了）则不重复挂。
        if self.artex_intent_id and not self.is_orchestrator:
            for tname, tspec in _WORKER_TRACE_TOOLS.items():
                if not any(s.get("name") == tname for s in specs):
                    specs = [*specs, tspec]
            # 事实写回（artex-record-fact，2026-10-19）：Worker 的唯一探索图写入口。
            # 与 trace 三件套同处动态追加——专家 yaml 不列它（它是 ARTEX 语义，
            # 不是专家能力），已声明过则不重复挂。
            if not any(s.get("name") == RECORD_FACT_TOOL_NAME for s in specs):
                specs = [*specs, RECORD_FACT_TOOL_SPEC]
        # bb_query 的 what 面按轨裁剪（track-query-split，2026-10-13）：
        # 渗透/逆向互用不到的面不下发——与 AgentSession 同口径（tool_registry 家族表）。
        specs = [bb_query_schema_for(self.track) if s.get("name") == "bb_query" else s
                 for s in specs]
        # 收尾阶段（artex-wrapup）：裁成「只能写回」的子集。与 _build_dispatcher
        # 的白名单裁剪**双边同步**。放最后，确保裁掉上面所有分支刚追加的工具。
        if getattr(self, "_wrapup_active", False):
            specs = filter_wrapup_tools(specs)
        return specs

    def _orch_tool_specs(self) -> list[dict[str, Any]]:
        """主控专属工具规格：todo_write + call_expert（专家清单动态生成）。

        ARTEX 主控额外挂 artex_* 操舵四件套（见 ``_artex_tool_specs``）：派发
        权归项目 Engine，主控只做观察与调整。
        """
        expert_rows = []
        for eid in self._delegatable_experts():
            if self.is_aggressive and eid == "worker":
                expert_rows.append("- worker：唯一隔离子代理；只执行 task 中交代的任务")
                continue
            try:
                e = load_expert(self.packs_root, eid, self.track)
            except FileNotFoundError:
                continue
            expert_rows.append(f"- {eid}：{e.get('name', eid)}——"
                               f"{e.get('description', '')}")
        specs = [
            {
                "name": "todo_write",
                "description": "维护当前任务的待办清单（工作记忆外化，人类实时可见）。"
                               "整体覆写：每次提交完整清单；状态 pending/in_progress/"
                               "completed。接到意图先拆待办，每完成一项立即更新。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "status": {"type": "string",
                                               "enum": list(chat_store.TODO_STATUSES)},
                                },
                                "required": ["title", "status"],
                            },
                        },
                    },
                    "required": ["items"],
                },
            },
            {
                "name": "call_expert",
                "description": "委派专家池专家执行一项任务：spawn 隔离的专家线程"
                               "（持久留档，人类可打开追问），专家跑完返回摘要。"
                               "相互独立的任务可在同一轮并发调用；有依赖的任务按顺序委派。"
                               "任务描述必须自包含（目标/范围/已知"
                               "信息/期望交付物）。你在委派前不直接实操——实操是"
                               "专家的事。（expert 只填下方专家 id，勿填技能名；"
                               "测试类任务用 asset_ids 把该专家负责的资产 id 明确划给它）"
                               + ("\n【先返先触发 · ARTEX 调度】多个 call_expert "
                                  "同轮并发派出后，第一个有效结果返回就立即推进对应方向，"
                                  "未等到的任务后台继续跑、完成后以「[后台并发结果]」消息补达。"
                                  "每个任务必须自包含、只覆盖一个测试方向并带真实证据；"
                                  "不同任务应覆盖不同资产或测试面，禁止重复派发。"
                                  "高风险动作必须逐项审批。"
                                  "\n注意：ARTEX 主控的**默认派发通道是 artex_plan**"
                                  "（Planner 生成 Intent → Engine 调度 Worker）。"
                                  "call_expert 只用于不值得建 Intent 的一次性查询/登记；"
                                  "不要用它替代任务规划，否则任务树与探索图会与执行脱节。"
                                  if self.is_artex else "")
                               + ("\n【先返先触发 · 激进主控】多个 call_expert "
                                  "同轮并发派出后，第一个探针返回你就继续推进"
                                  "（[命中]→立即派深耕专家深挖，[未命中]→放弃"
                                  "该方向），未等到的探针后台继续跑、完成后以"
                                  "「[后台探针结果]」消息补达。普通探针任务必须"
                                  "单一快速且覆盖不同测试面；同一批不得都集中在相同字符串、"
                                  "工具、文件特征或猜测，重复方向必须改派。重大价值方向允许将相同"
                                  "任务同时派给 2 或 3 个独立 worker 做交叉验证，但不能替代其他方向。"
                                  "至少保留一个持续追踪 worker，直到 flag、决定性结论或方向穷尽。"
                                  "快速探针结论以 [命中]/[未命中] 开头。"
                                  if self.is_aggressive else "")
                               + ("\n可用专家：\n" + "\n".join(expert_rows)
                                  if expert_rows else ""),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        # enum（2026-10-01）：模型曾把技能名 recon-asset-enum 当专家名
                        # 传进来（技能清单与专家名录同屏、语义相近）。值域与「可用专家」
                        # 文本块同源（_delegatable_experts），仅作软约束降错率；
                        # 硬校验仍在 _tool_call_expert 的成员判定。
                        "expert": {"type": "string",
                                   "enum": self._delegatable_experts(),
                                   "description": "专家 id（见可用专家清单，"
                                                  "不是技能名）"},
                        "task": {"type": "string",
                                 "description": "自包含的任务描述"},
                        "asset_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "分配给该子专家测试的资产 id 列表"
                                           "（严格单一派发：不同子专家分不同资产，"
                                           "避免重复测试。测试类任务必填；纯查询/登记"
                                           "类任务可省略）",
                        },
                    },
                    "required": ["expert", "task"],
                },
            },
        ]
        if self.is_artex:
            specs = [*specs, *self._artex_tool_specs()]
        return specs

    def _artex_tool_specs(self) -> list[dict[str, Any]]:
        """ARTEX 主控操舵四件套（artex-fusion，2026-10-19）。

        原生 ARTEX 里 MainAgent 只观察和操舵，Intent 由 Planner 生成、Engine
        调度、Worker 执行。这四个工具就是「观察 + 操舵」的全部手段——主控不再
        自己决定派给谁，避免与 Engine 各自维护一套并发/重试/生命周期。
        """
        return [
            {
                "name": "artex_plan",
                "description": "让 ARTEX Planner 生成/扩展 Intent（这是 ARTEX 主控的"
                               "**默认派发通道**）。mode=\"new\" 新建任务树；"
                               "mode=\"extend\" 依据探索图里的事实与发现向同一任务追加"
                               "新方向；mode=\"auto\" 有活跃任务就追加、没有就新建。"
                               "不传 intents 时由 Planner 决策方向；传 intents 则按给定"
                               "方向直接落库（跳过 LLM）。**强依赖的串行链（后一步要用"
                               "前一步的实际产出，如 注入点→凭据→横向→提权）要分步派**："
                               "用 todos 记下整条链，本轮只派第一步，下轮据实际产出再派"
                               "下一步——一次性并行下发会让下游空转或重复。Intent 落库后"
                               "Engine 会并发调度 Worker，主控随后用 artex_status 观察。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string",
                                 "description": "任务目标（mode=new 必填）"},
                        "mode": {"type": "string",
                                 "enum": ["auto", "new", "extend"],
                                 "description": "auto（默认）/ new / extend"},
                        "task_id": {"type": "string",
                                    "description": "追加到指定任务；留空按活跃任务推导"},
                        "intents": {
                            "type": "array",
                            "description": "显式方向（可选）：给了就不再问 Planner。"
                                           "强依赖的串行链（后一步要用前一步的实际产出）"
                                           "不要一次性并行下发——只派第一步，后一步用 "
                                           "depends_on 指向它；本数组内按输出顺序从 1 "
                                           "开始编号，如第 2 项 depends_on=[\"#1\"]。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "objective": {"type": "string"},
                                    "expert": {"type": "string"},
                                    "priority": {"type": "integer"},
                                    "asset_ids": {"type": "array",
                                                  "items": {"type": "string"}},
                                    "depends_on": {
                                        "type": "array", "items": {"type": "string"},
                                        "description": "前置 Intent：本批内序号"
                                                       "（\"#1\"）或既有 Intent id。"
                                                       "下游要等前置 completed 才被调度；"
                                                       "前置 failed/cancelled 时下游置 blocked。"},
                                },
                                "required": ["objective"],
                            },
                        },
                        "asset_ids": {"type": "array", "items": {"type": "string"},
                                      "description": "本批 Intent 的初始资产"},
                        "todos": {
                            "type": "array",
                            "description": "规划待办（可选，整表覆盖、跨唤醒保留）："
                                           "把强依赖串行链记成待办（每步一条），"
                                           "本轮只派第一步，下轮据实际产出派下一步。"
                                           "每轮传完整清单（不是增量）；传 [] 清空；"
                                           "不传则保持原清单。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string",
                                             "description": "步骤描述（与对应 Intent 对齐）"},
                                    "status": {"type": "string",
                                               "enum": ["pending", "in_progress",
                                                        "completed"]},
                                },
                                "required": ["text"],
                            },
                        },
                        "max_new": {"type": "integer",
                                    "description": "本次最多新增的 Intent 数（默认 3）"},
                    },
                    "required": [],
                },
            },
            {
                "name": "artex_status",
                "description": "观察 ARTEX 任务：runtime 状态（exploring / draining / "
                               "goal_reached / needs_review / cancelled）、各状态 Intent "
                               "计数、活跃 Worker 数、探索图里的 Fact / Finding / Hint。"
                               "每轮先看它再决定要不要 artex_plan 或 artex_control。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "留空取活跃任务"},
                        "detail": {"type": "string", "enum": ["summary", "full"],
                                   "description": "summary（默认）只列进行中 Intent；"
                                                  "full 列全量"},
                    },
                    "required": [],
                },
            },
            {
                "name": "artex_hint",
                "description": "给 ARTEX 操舵：把一条方向判断写成探索图 Hint。"
                               "指定 intent_id 且该 Worker 正在运行时会直接下发 steer"
                               "（Worker 在下一步收到「[Worker 方向调整]」），否则只落"
                               "Hint 供后续规划参考。用于纠正跑偏、补充线索、要求换方向。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string",
                                 "description": "具体的方向调整内容"},
                        "intent_id": {"type": "string",
                                      "description": "目标 Intent；运行中会同步 steer"},
                        "task_id": {"type": "string", "description": "留空取活跃任务"},
                        "steer": {"type": "boolean",
                                  "description": "false = 只写 Hint，不下发 steer"},
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "artex_control",
                "description": "ARTEX 调度控制。start 启动 Engine；drain 停止领取新 "
                               "Intent 等 Worker 收尾；stop 停掉 Engine；pause 暂停任务"
                               "剩余 pending；resume 把 paused 放回 Frontier；cancel 取消"
                               "Intent（给 task_id 则取消该任务全部 pending/paused）；"
                               "kill 终止单个运行中 Worker。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["start", "drain", "stop", "pause",
                                            "resume", "cancel", "kill"]},
                        "task_id": {"type": "string", "description": "留空取活跃任务"},
                        "intent_id": {"type": "string",
                                      "description": "kill / 单条 cancel 必填"},
                        "reason": {"type": "string", "description": "动作原因（留痕）"},
                        "timeout": {"type": "number",
                                    "description": "drain / stop 的等待秒数（默认 5）"},
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "artex_goal",
                "description": "ARTEX 目标与范围治理（任务收敛的**唯一依据**）。"
                               "action=\"list\" 列出任务注册目标及各自证据；"
                               "action=\"set\" 补登记目标（goals=[\"…\"]）；"
                               "action=\"prove\" 把证据（本任务 fact/finding 节点 id）"
                               "连到某条目标上并标记达成，全部目标达成即任务可收敛；"
                               "action=\"goal_met\" 绕过逐条证明、全局宣布任务收官"
                               "（需 reason、有权重、审计可辨）；"
                               "action=\"anchor\" 把资产纳入任务范围（覆盖度分母）——"
                               "不传 asset_ids 则只报当前覆盖度与未覆盖数，"
                               "remove=true 摘除锚点。量化目标（覆盖度达 X%）必须据此核对。"
                               "任务终态由目标态派生：仍有未达成目标时任务不会判完成。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["list", "set", "prove", "goal_met", "anchor"]},
                        "task_id": {"type": "string", "description": "留空取活跃任务"},
                        "goals": {
                            "type": "array",
                            "description": "set 用：目标清单",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "vulnclass": {"type": "string"},
                                },
                                "required": ["text"],
                            },
                        },
                        "asset_ids": {"type": "array", "items": {"type": "string"},
                                      "description": "set/anchor 用：资产范围（set=目标适用"
                                                     "范围；anchor=纳入任务测试范围）"},
                        "goal_id": {"type": "string",
                                    "description": "prove 必填：目标节点 id"},
                        "evidence_ids": {
                            "type": "array", "items": {"type": "string"},
                            "description": "prove 用：本任务 fact/finding 节点 id",
                        },
                        "reason": {"type": "string",
                                   "description": "prove / goal_met 的依据说明"},
                        "remove": {"type": "boolean",
                                   "description": "anchor 用：true=从任务范围摘除这些资产"},
                        "note": {"type": "string",
                                 "description": "anchor 用：锚定原因（留痕）"},
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "artex_fact",
                "description": "ARTEX 事实库检索（只读）。action=\"list\" 列本任务事实"
                               "（含 confidence 与来源意图，可按 intent_id / confidence 筛、"
                               "分页）；action=\"get\" 取某条事实的完整内容（detail/evidence/"
                               "关联资产/血缘边）。"
                               "用途：artex_status 只给最近几条标题，规划态势里事实动辄几十条，"
                               "要判断「这个方向是否已试过、结论是直接看到还是推断」必须按需翻。"
                               "**否定结论（端口关闭/不可注入/无入口）同样在事实库里**，"
                               "排方向前先查一遍能避免重复劳动。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "get"]},
                        "task_id": {"type": "string", "description": "留空取活跃任务"},
                        "id": {"type": "string",
                               "description": "get 必填：探索图事实节点 id（非资产 id）"},
                        "intent_id": {"type": "string",
                                      "description": "list 用：只看某条意图产出的事实"},
                        "confidence": {"type": "string", "enum": ["observed", "inferred"],
                                       "description": "list 用：按置信度过滤（留空不筛）"},
                        "limit": {"type": "integer", "description": "list 用：返回上限，默认 20"},
                        "offset": {"type": "integer", "description": "list 用：分页偏移"},
                        "detail_limit": {"type": "integer",
                                         "description": "list 用：每条的详情截断字数，默认 200"},
                    },
                    "required": ["action"],
                },
            },
        ]

    def _delegatable_experts(self) -> list[str]:
        """返回本线程可委派的唯一专家值域。

        激进主控采用固定的隔离 worker，不把专家池枚举泄露给模型；ARTEX 主控
        使用当前轨道的专家池并启用先返先触发；标准主控保持原有按轨专家池语义。
        """
        if self.is_aggressive:
            return ["worker"]
        return [n for n in self.expert_names
                if n not in CONTROLLER_IDS and n != "_generalist"]

    # ---------- 系统提示 ----------

    def _system_prompt(self) -> str:
        # 降级不中断（2026-10-03）：系统提示是「增强注入」，专家 yaml / 规则链
        # 任一读取失败（文件被删、Windows AV/索引瞬时锁、packs 目录缺失）都不该
        # 让整轮对话炸成「执行异常 unknown」——回退空人设 / 空规则串继续跑。
        try:
            expert = load_expert(self.packs_root, self.agent_id, self.track)
        except Exception:  # noqa: BLE001 —— 专家与兜底 yaml 双缺才抛，降级空人设
            log.exception("chat 专家加载失败，回退空人设 thread=%s agent=%s",
                          self.thread_id, self.agent_id)
            expert = {}
        try:
            rules_preamble = build_rules_preamble(
                self.packs_root, track=self.track,
                capabilities=self.capabilities,
                owner_tags=self.owner_tags, role=self.agent_id,
                rule_profiles=self.rule_profiles)
        except Exception:  # noqa: BLE001 —— 规则链读取失败降级为空串
            log.exception("chat 规则链构建失败，降级为空 thread=%s", self.thread_id)
            rules_preamble = ""
        # 规则链放最前（与任务链 stable_parts[0] 同构）：红线+owner 叠加+评级
        # 口径——子专家 bb_add_finding 判级引用 rating:<tag> 条款，主控汇总
        # 判级同样有据；role 传 agent_id，role-rules 文件存在时自动叠加
        parts = [rules_preamble,
                 f"# 角色：{expert.get('name', self.agent_id)}\n{expert.get('persona') or expert.get('description') or ''}"]
        if self.is_orchestrator:
            if self.is_aggressive:
                fctx = self._workspace_files_context()
                if fctx:
                    parts.append(fctx)
                # 激进主控：探针海 + 先返先触发（与 chat-orchestrator 的
                # 蛙式排队互斥，二选一注入）
                parts.append(_AGGRESSIVE_CONTROLLER_STYLE)
            elif self.is_artex:
                parts.append(_ARTEX_CONTROLLER_STYLE)
                parts.append(self._situation_context())
            else:
                parts.append(
                    "## 工作方式\n"
                    "1. 接到测试意图 → todo_write 拆待办（每项一条，状态实时更新）；\n"
                    "2. 将相互独立的待办通过多个 call_expert 同轮并发委派；有前后依赖的待办按顺序委派，任务描述必须自包含；\n"
                    "3. 子专家摘要回来后推进下一项；全部完成后向人类汇总署名输出。\n"
                    "委派前可用 kb_search/kb_open 查知识库确认打法方向、bb_query 了解"
                    "黑板已有资产/发现；资产清理与合并可直接使用受控的"
                    "bb_delete_asset/bb_merge_assets（必须先查询，合并必须给出 reason）；"
                    "不直接执行扫描/利用等实操。")
                parts.append(self._situation_context())
        else:
            if getattr(self, "probe_mode", False) or getattr(self, "intent_free", False):
                # intent_free worker 只消费本轮 task；不注入要求先声明意图的
                # 工作方式，避免模型按通用专家纪律创建意图。
                pass
            else:
                parts.append(
                    "## 工作方式\n"
                    "你是被主控委派执行具体任务的专家：按任务描述执行，需要方法细节时"
                    "kb_search/kb_open 查知识库、skill_open 打开技能手册；过程结论落"
                    "黑板（bb_add_asset/bb_add_finding）；干完给一段**自包含的收尾"
                    "摘要**（做了什么/关键证据/结论），主控会原样转述给人类。")
            if not (getattr(self, "probe_mode", False) or getattr(self, "intent_free", False)):
                parts.append(self._mission_context())
        # 经验沉淀纪律（kb-iteration，2026-10-14）：propose_pack_edit 对主控与子专家均开放
        # （详见工具描述）；提示何时该提 + 证据要求 + 上限，与任务链 STRICT_PROMPT_TAIL 同口径。
        # **受项目级开关门禁（2026-10-15）**：关闭时整段不注入 + 工具面剔除
        # （_tool_specs/_build_dispatcher）——开关关 = Agent 不提任何提案，提示也不诱导。
        if self._kb_iteration_on() and not (self.is_aggressive or self.probe_mode or self.is_worker):
            parts.append(
                "## 经验沉淀（可选）\n"
                "发现知识库/技能文档**内容有误**、**文档缺失**、某手法**已在本轮验证有效**，"
                "或产出**新的 Web 漏洞**时，可用 propose_pack_edit 提变更提案（kb 文档与技能的"
                "新建/改写/删除，删除应用时进回收站）——只落 pending 待人类审批，**绝不自行改"
                "文件**。reason 必须附本轮证据（引用的文档 / 失败的工具 / 产出的发现 id）；"
                "每会话最多 3 条，禁止凑数。任务较难、试错较多时也值得回看是否有可沉淀的坑。")
        # 联网搜索指引（v43，web-search-toggle）：开关开 = 工具面已放行
        # web_search（_tool_specs/_build_dispatcher 双边同步），给一段使用纪律；
        # 关着整段不注入（工具不下发，提示也不诱导）。主控与子代理同口径——
        # 子代理线程的开关位由 spawn 时从父线程继承。
        if self._web_search_on():
            parts.append(
                "## 联网搜索（已启用）\n"
                "你持有 web_search：查本地知识库之外的公开信息（CVE 详情、公开利用"
                "代码/EXP、最新安全情报、工具/命令用法、报错含义）直接调它，一次给足"
                "关键词（关键术语 + 编号，中英文都可）。多引擎聚合、免 Key；结果只有"
                "标题/链接/摘要，深读单个页面用 browser_navigate。检索结论注明来源"
                "URL。**不要把内网/涉密目标的敏感细节（域名、路径、凭据、样本特征）"
                "拼进搜索词外传**——联网只用于查公开资料。")
        # Worker 间过程级信息交换（v46，artex-traces）：与工具面同口径——只有
        # ARTEX Worker（有 intent_id）才持有三件套，故只有它们注入使用纪律。
        # getattr 兜底：_system_prompt 的契约是「降级不中断」，任何缺属性的
        # 壳（测试构造 / 旧实例）都不该在这里抛。
        if getattr(self, "artex_intent_id", "") and not self.is_orchestrator:
            parts.append(
                "## 同任务其他 work 的执行过程（可检索）\n"
                "你只领到本任务的一条意图，但同任务里别的 work 的执行过程留有大量"
                "**过程级观察**（某次报错、某段响应、某个隐藏参数），它们未必被写成"
                "正式 fact。开工前若怀疑「别人可能已经碰过这块」，可用 "
                "artex_worker_trace_list 看有哪些 work 跑过、artex_worker_trace_get "
                "翻某条的步骤摘要（先摘要流定位 step_id，再用 step_ids 取完整输出，"
                "一次最多 5 个）；已知明确关键字（路径/token/报错片段）时用 "
                "artex_worker_trace_search 直接跨 work 搜——它会自动排除你自己这条"
                "意图的步骤。**但不要为了「看一眼」而翻**：系统已把该给的信息给你了，"
                "只有在确实缺一块信息、且猜别人见过时才查，查到就够、不必穷举。"
                "边界不变：你仍只做自己领到的这条意图，别人的过程只是参考。")
            # 事实写回纪律（artex-record-fact，2026-10-19）：与工具面同口径——
            # 只有 ARTEX Worker 持有 record_fact，故只有它注入写回纪律。
            # 这段是「探索图为什么值得写」的动机说明：只给工具不给纪律，模型会
            # 一路探测到收尾才想起写，那时已经丢了大半中间观察。
            parts.append(
                "## 事实写回（record_fact）\n"
                "你这条意图的价值**不在最终那段总结，而在落进探索图的事实**——"
                "规划者看不到你的对话，只看到你写下的事实。所以：\n"
                "- **边做边写，不要攒到最后**。每确认一个结论就 record_fact 一次；"
                "收尾才写的话，前面探过的细节基本已经忘了。\n"
                "- **一次探索汇成一条事实**，别把状态码、指纹、标题各记一条——"
                "summary 写一句结论，detail 把所有细节塞进去。拆太碎会让图谱爆炸。\n"
                "- **否定结论同样要写**：「8080 关闭」「该参数不可注入」「未发现登录"
                "入口」都是规划者最需要的信息（它据此排除方向）。写「观察 + 试探性"
                "读法」，不要直接下「已放弃」的结论。\n"
                "- **confidence 如实标**：observed=输出里直接看到；inferred=据现象"
                "推断。否定结论除非确已穷尽手段且直接看到，否则标 inferred——"
                "标错 observed 会让规划者以为该方向已穷尽而永久跳过。\n"
                "- **evidence 只写一行**（命令 + 最能证明的那一两行输出），"
                "不要把大段输出粘进去（细节放 detail）。\n"
                "- 只写你**真实看到**的结论，不要脑补。同一条结论重复写会按幂等"
                "更新，不必担心重复。")
        # CTF 题目上下文（challenge-asset v38）：主控与子专家都注入（子专家与主控
        # 同题，都要知道该题的归属与建题纪律）；非 CTF 轨返空串不注入。
        if not (getattr(self, "probe_mode", False) or self.is_aggressive or self.is_worker):
            cctx = self._challenge_context()
            if cctx:
                parts.append(cctx)
            # 蓝图注入（R4 消费面接线 M1）：主控与子专家都摊项目蓝图（紧凑形态），
            # 无蓝图返空串不注入。
            bp_ctx = self._blueprint_context()
            if bp_ctx:
                parts.append(bp_ctx)
        # 技能全量描述（K8 同款渐进披露层1：name+description，正文 skill_open）。
        # 激进主控及其 worker 明确禁用技能，不能因专家配置缺省而回退全量。
        try:
            if SkillRegistry is not None and not (self.is_aggressive or self.probe_mode or self.is_worker):
                reg = SkillRegistry(self.packs_root)
                reg.load()
                # None=全量，[]/空集合=明确不加载；保留三态语义。
                role_skills = expert.get("skills") if "skills" in expert else None
                enabled = [s for s in reg.all()
                           if s.enabled and (role_skills is None or s.name in role_skills)]
                if enabled:
                    # 消歧（2026-10-01）：技能名与专家名曾同屏混淆（模型把
                    # recon-asset-enum 当专家名传给 call_expert），标题显式标注边界。
                    parts.append("## 可用技能清单（skill_open(\"<name>\") 打开全量正文；"
                                 "下列是【技能名】，不是专家——委派专家请用 call_expert "
                                 "并填专家 id）\n"
                                 + "\n".join(f"- {s.name}：{s.description or ''}"
                                             for s in enabled))
        except Exception:  # noqa: BLE001 —— 技能清单失败不阻断对话
            log.exception("chat 技能清单加载失败 thread=%s", self.thread_id)
        return "\n\n".join(parts)

    # ---------- 消息装载与落库 ----------

    def _load_history(self) -> list[dict[str, Any]]:
        rows = chat_store.list_messages(self.bb, self.thread_id)
        # 同 id **后写优先**（激进主控后台探针：主线程先落「[后台继续]」占位
        # 行，worker 完成后以同 tool_use_id 续写真实结果——续写行可能落在**后面
        # 的 flush 组**里，直接按行序回放会被 _sanitize_history 当孤儿
        # tool_result 丢弃）。故先按 id 汇总最终内容（真实胜出、占位不反向
        # 覆盖），回放时统一取汇总值；占位行本身即携带真实结果。单结果行的
        # 历史不受影响。
        final_by_id: dict[str, str] = {}
        for r in rows:
            if r["role"] != "tool":
                continue
            tid = str(r["tool_use_id"] or "")
            if not tid:
                continue
            text = r["content"] or "(空)"
            prev = final_by_id.get(tid)
            if prev is None or not text.startswith(_PROBE_PENDING):
                final_by_id[tid] = text
        messages: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        pending_idx: dict[str, int] = {}   # tool_use_id → pending 下标

        def _flush_tools() -> None:
            if pending:
                messages.append({"role": "user",
                                 "content": list(pending)})
                pending.clear()
                pending_idx.clear()

        for r in rows:
            if r["role"] == "user":
                _flush_tools()
                messages.append({"role": "user", "content": r["content"]})
            elif r["role"] == "assistant":
                _flush_tools()
                blocks: list[dict[str, Any]] = []
                if r["content"]:
                    blocks.append({"type": "text", "text": r["content"]})
                for tc in r["tool_calls"]:
                    name = str(tc.get("name") or "").strip()
                    call_id = str(tc.get("id") or "").strip()
                    if not name or not call_id:
                        continue
                    blocks.append({"type": "tool_use", "id": call_id,
                                   "name": name,
                                   "input": tc.get("args") or {}})
                if blocks:
                    messages.append({"role": "assistant", "content": blocks})
            elif r["role"] == "tool":
                # 同组内同 id 的多行（占位+续写）只入一块，内容取 final_by_id
                tid = str(r["tool_use_id"] or "")
                if tid and tid in pending_idx:
                    continue
                block = {"type": "tool_result",
                         "tool_use_id": r["tool_use_id"],
                         "content": final_by_id.get(tid) or "(空)"}
                if tid:
                    pending_idx[tid] = len(pending)
                pending.append(block)
        _flush_tools()
        return _sanitize_history(messages)

    # ---------- 上下文治理 ----------

    def _clip_tool_result(self, text: str, tc) -> tuple[str, str]:
        """① 单条工具结果截断（2026-09-30）：超过 _MAX_TOOL_RESULT_CHARS 的
        巨物，完整原文落 artifacts 文件（事件流记路径），入历史的只留前 N 字符
        + 截断说明。返回 (入历史文本, 完整原文落盘路径或空串)。

        截断必须落在「写 DB」这一步：DB 是下一轮 _load_history 的回放源，若只
        截断本轮 messages 而 DB 存全量，下轮仍会读回巨物 → input 再爆。"""
        n = len(text)
        if n <= _MAX_TOOL_RESULT_CHARS:
            return text, ""
        path = ""
        if self.artifacts_dir is not None:
            try:
                d = Path(self.artifacts_dir) / "chat-results"
                d.mkdir(parents=True, exist_ok=True)
                fp = d / f"{self.thread_id}-{tc.id}.txt"
                fp.write_text(text, encoding="utf-8")
                path = str(fp)
            except Exception:  # noqa: BLE001 —— 落盘失败不阻断（保截断版）
                log.exception("chat 工具结果落盘失败 thread=%s", self.thread_id)
        head = text[:_MAX_TOOL_RESULT_CHARS]
        tail = (f"\n\n…（结果过长已截断：原文 {n} 字符，本条仅保留前 "
                f"{_MAX_TOOL_RESULT_CHARS} 字符；完整结果见"
                f"{path or '事件流'}）")
        return head + tail, path

    def _ctx_window(self) -> tuple[int, int]:
        """(硬窗口, 有效软上限)：优先取 provider 配置（model_window/ctx_soft_budget），
        缺省回落模块常量。前端面板据此定分母与警戒线（ct-8）。"""
        win = int(getattr(self.llm, "context_tokens", None) or _MODEL_WINDOW_FALLBACK)
        soft = int(getattr(self.llm, "ctx_soft_budget", None) or _CTX_SOFT_BUDGET)
        return win, soft

    def _ctx_trigger_tokens(self) -> int:
        """发送前压缩的触发阈值 = min(比例×硬窗口, 软上限)。硬窗口优先取
        llm.context_tokens（provider 配置的真实窗口），缺省回落 _MODEL_WINDOW_
        FALLBACK。软上限保证即使模型支持 1M 也在 512K 前主动压缩。"""
        win, soft = self._ctx_window()
        return int(min(_CTX_TRIGGER_RATIO * win, soft))

    def _context_tokens(self, messages: list[dict[str, Any]],
                        system: str | None,
                        tools: list[dict[str, Any]]) -> int:
        return (_est_tokens(system or "")
                + _est_tokens(json.dumps(tools or [], ensure_ascii=False))
                + sum(_est_tokens(_msg_text(m)) for m in messages))

    def _ctx_summary_cache_path(self) -> Path | None:
        if self.artifacts_dir is None:
            return None
        return Path(self.artifacts_dir) / "chat-ctx" / f"{self.thread_id}.json"

    def _ctx_cache_get(self, key: str) -> str | None:
        """摘要缓存（跨轮持久，落 artifacts/chat-ctx/<thread>.json）：以旧区
        内容哈希为 key，避免每步/每轮重跑昂贵的 LLM 摘要。"""
        cache = getattr(self, "_ctx_cache", None)
        if cache is None:
            cache = {}
            fp = self._ctx_summary_cache_path()
            if fp is not None and fp.exists():
                try:
                    loaded = json.loads(fp.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        cache = loaded
                except Exception:  # noqa: BLE001 —— 缓存损坏则弃用重算
                    log.exception("chat 摘要缓存读取失败 thread=%s", self.thread_id)
            self._ctx_cache = cache
        return cache.get(key)

    def _ctx_cache_put(self, key: str, summary: str) -> None:
        self._ctx_cache_get("")  # 确保缓存已加载
        self._ctx_cache[key] = summary
        fp = self._ctx_summary_cache_path()
        if fp is None:
            return
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(self._ctx_cache, ensure_ascii=False),
                          encoding="utf-8")
        except Exception:  # noqa: BLE001 —— 缓存写失败不阻断本轮
            log.exception("chat 摘要缓存写入失败 thread=%s", self.thread_id)

    def _span_text(self, msgs: list[dict[str, Any]]) -> str:
        """拼旧区文本并做长度保护（首尾保留、中段省略）。一级压缩会就地改写
        messages，故二级摘要必须在占位化「之前」对原文快照调用本方法，否则摘要
        只能看到占位符、丢失真实工具结果细节。"""
        parts: list[str] = []
        total = 0
        for m in msgs:
            t = _msg_text(m)
            parts.append(t)
            total += len(t) + 2
            if total > _CTX_SUMMARY_MAX_INPUT * 2:
                break
        text = "\n\n".join(parts)
        if len(text) > _CTX_SUMMARY_MAX_INPUT:
            half = _CTX_SUMMARY_MAX_INPUT // 2
            text = text[:half] + "\n\n…（中段省略）…\n\n" + text[-half:]
        return text

    def _summarize_span(self, text: str) -> str:
        """二级压缩：LLM 把旧区原文压成逆向增强结构化摘要。失败/中止返回空串。"""
        if self.llm is None or not text:
            return ""
        try:
            kw: dict[str, Any] = {}
            sm = getattr(self.llm, "summarizer_model", None)
            if sm:
                kw["model"] = sm  # 专用摘要模型（providers.json summarizer_model）
            resp = self.llm.chat(
                [{"role": "user", "content": text}],
                system=_CTX_SUMMARY_SYS, should_cancel=self._aborted, **kw)
        except Exception:  # noqa: BLE001 —— 摘要失败不阻断（交由 reactive 兜底）
            log.exception("chat 上下文摘要失败 thread=%s", self.thread_id)
            return ""
        return (resp.text or "").strip()

    def _prepare_context(self, messages: list[dict[str, Any]],
                         system: str | None,
                         tools: list[dict[str, Any]], *,
                         force: bool = False
                         ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """发送前两级联动压缩（2026-09-30）。

        一级（零 LLM 成本）：把最近 _KEEP_RECENT_STEPS 步之外的旧 tool_result
        内容换成占位符（保留 tool_use 记录与 assistant 文本）。
        二级（LLM 摘要）：若一级后仍超阈值，把旧区整体压成结构化摘要，替换为
        一条 user 摘要消息；摘要按旧区内容哈希缓存，跨步/跨轮复用。

        force=True：reactive 兜底路径（收到 ContextOverflowError）无视阈值强压。
        返回 (messages', info)；info 用于事件流/面板展示。"""
        budget = self._ctx_trigger_tokens()
        est = self._context_tokens(messages, system, tools)
        if not force and est <= budget:
            return messages, {"compacted": False, "level": 0,
                              "est": est, "budget": budget}
        keep = _KEEP_RECENT_STEPS
        boundary = _recent_boundary(messages, keep)
        if force and boundary == 0 and len(messages) > 1:
            keep = max(1, _KEEP_RECENT_STEPS // 2)
            boundary = _recent_boundary(messages, keep)
        old = messages[:boundary]
        # 二级摘要必须基于原文：一级占位化会就地改写 messages，故先快照旧区文本
        old_text = self._span_text(old) if old else ""
        masked = _mask_old_tool_results(messages, boundary)
        est1 = self._context_tokens(messages, system, tools)
        if not force and est1 <= budget:
            return messages, {"compacted": True, "level": 1, "masked": masked,
                              "est": est1, "budget": budget}
        if old_text:
            key = hashlib.sha1(old_text.encode("utf-8")).hexdigest()
            summary = self._ctx_cache_get(key)
            cache_hit = summary is not None
            if summary is None:
                summary = self._summarize_span(old_text)
                if summary:
                    self._ctx_cache_put(key, summary)
            if summary:
                new_msgs = [
                    {"role": "user",
                     "content": (f"{_CTX_SUMMARY_TAG}（此前 {len(old)} 条消息"
                                 f"已压缩为摘要；原始记录见线程历史与事件流）\n{summary}")},
                    *messages[boundary:],
                ]
                est2 = self._context_tokens(new_msgs, system, tools)
                return new_msgs, {"compacted": True, "level": 2,
                                  "masked": masked, "cached": cache_hit,
                                  "est": est2, "budget": budget}
        return messages, {"compacted": bool(masked), "level": 1 if masked else 0,
                          "masked": masked, "est": est1, "budget": budget}

    def _chat(self, messages: list[dict[str, Any]], system: str | None,
              tools: list[dict[str, Any]] | None, on_text=None,
              on_thinking=None, on_retry=None, on_provider_retry=None):
        """调 LLM（发送前已压缩）。reactive 兜底（ct-5）：若仍因 input 超限被
        网关 400/413 拒绝（ContextOverflowError），强制压缩一次后重试；再失败
        则抛出交给 run() 落 error。对应 Claude Code 五级级联的最末「reactive
        compact」——静态预算估算与实际 token 计数总有偏差，需运行时兜底。

        截断整轮重试（2026-10-01 事故修复）：LLMError.truncated（工具参数流残缺/
        截断，如网关 SSE 尾部冗余）时整轮重发 ≤_CHAT_TRUNC_RETRIES 次——传输层流
        不可重放，只能在轮级重试（与 agent 循环同口径）。每次重试前调 on_retry()
        重置 delta 缓冲，防「半截+新流」拼接上屏。"""
        for attempt in range(_CHAT_TRUNC_RETRIES + 1):
            try:
                return self._chat_once(messages, system, tools, on_text,
                                       on_thinking, on_provider_retry)
            except LLMError as e:
                if not getattr(e, "truncated", False) or attempt >= _CHAT_TRUNC_RETRIES:
                    raise
                log.warning("chat 工具参数流截断，整轮重试 %d/%d thread=%s: %s",
                            attempt + 1, _CHAT_TRUNC_RETRIES, self.thread_id, e)
                if on_retry is not None:
                    on_retry()
        raise RuntimeError("unreachable")  # pragma: no cover

    def _chat_once(self, messages: list[dict[str, Any]], system: str | None,
                   tools: list[dict[str, Any]] | None, on_text=None,
                   on_thinking=None, on_provider_retry=None):
        def provider_kwargs() -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "system": system, "tools": tools, "on_text": on_text,
                "should_cancel": self._aborted,
            }
            # 思考流式是 provider 的可选扩展。对支持 **kwargs 的测试替身和
            # 新 provider 直接传递；旧的窄签名实现则保持兼容。
            if on_thinking is not None:
                try:
                    params = inspect.signature(self.llm.chat).parameters
                    supports_thinking = (
                        "on_thinking" in params
                        or any(p.kind == inspect.Parameter.VAR_KEYWORD
                               for p in params.values()))
                except (TypeError, ValueError):
                    supports_thinking = False
                if supports_thinking:
                    kwargs["on_thinking"] = on_thinking
            # 只有支持该扩展回调的 provider（当前为 OpenAI 兼容层）接收它；
            # 保持测试替身和其他协议 provider 的旧接口兼容。
            if on_provider_retry is not None:
                try:
                    supports_retry = "on_retry" in inspect.signature(
                        self.llm.chat).parameters
                except (TypeError, ValueError):
                    supports_retry = False
                if supports_retry:
                    kwargs["on_retry"] = on_provider_retry
            return kwargs

        try:
            return self.llm.chat(messages, **provider_kwargs())
        except ContextOverflowError:
            log.warning("chat 输入超限，强制压缩后重试 thread=%s", self.thread_id)
            compacted, info = self._prepare_context(messages, system,
                                                    tools or [], force=True)
            self._emit("chat.ctx", {"phase": "reactive", **info})
            return self.llm.chat(compacted, **provider_kwargs())

    # ---------- 主流程 ----------

    def run(self, user_text: str, refs: dict[str, list[str]] | None = None,
            attachments: list[dict] | None = None) -> str:
        self._refs = refs or {}
        # 附件随发（2026-10-08，工作台 composer）：与直播间同口径——只把附件渲染成
        # 📎 路径行拼进用户消息（Agent 自行 run_cmd/read_file 读取），不做多模态。
        # 复用 core.agent.loop 的渲染器，防文案漂移（loop.py 不 import core.chat，无环）。
        text = user_text
        if attachments:
            from core.agent.loop import _attachment_lines
            lines = _attachment_lines(attachments)
            if lines:
                text = f"{user_text}\n\n附件：\n" + "\n".join(lines)
        chat_store.append_message(self.bb, self.thread_id, "user", text)
        thread = chat_store.get_thread(self.bb, self.thread_id)
        if thread is not None and not thread.get("title"):
            title = user_text.strip()[:40] or next(
                (str(a.get("name") or "") for a in (attachments or []) if a.get("name")), "")
            if title:
                chat_store.update_thread(self.bb, self.thread_id, title=title[:40])
        # 新轮开始：置 running 并清空上一轮的错误（error={} 为 falsy → 落 ''）
        chat_store.update_thread(self.bb, self.thread_id, status="running",
                                 error={})
        # 上下文用量基底（跨轮累计 output/steps；input=最近一步占用）
        self._usage: dict[str, int] = dict(thread.get("usage") or {}) if thread else {}
        self._emit("chat.message", {"role": "user", "text": text[:2000],
                                    "message_id": None})
        # kb-iteration（2026-10-14）：记本轮起点（事件 id + 时间），轮末据此汇总信号
        # （含子专家事件——见 core/skills/kbiteration.py 的窗口口径）
        try:
            self._round_start_event_id = self.bb.conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE project_id=?",
                (self.project_id,)).fetchone()[0]
        except Exception:  # noqa: BLE001
            self._round_start_event_id = 0
        try:
            from core.blackboard.store import now as _bb_now
            self._round_start_iso = _bb_now()
        except Exception:  # noqa: BLE001
            self._round_start_iso = None
        try:
            final = self._loop(text)
            chat_store.update_thread(self.bb, self.thread_id, status="idle")
            # 正常收尾：清剪本轮流式 delta（终稿全文已在 chat_messages，事件只承担
            # 实时可见）——不删则每条携带累计全文的 delta 行持续膨胀事件表。异常/
            # 中断路径不调用（残留 delta = 被中断思考的现场审计）。
            try:
                self.bb.prune_chat_thread_deltas(self.project_id, self.thread_id)
            except Exception:  # noqa: BLE001 —— 清剪失败不影响收尾
                log.exception("chat delta 清剪失败 thread=%s", self.thread_id)
            self._maybe_kb_iteration()
            return final
        except ChatStopped:
            # 人类中止（2026-10-09）：不是故障，归位 idle 且不落错误卡。中止说明
            # 在此落库（步间/工具后的中止点走 `_loop` 内的 `_persist_stopped`
            # 正常返回，只有 LLM 调用中途被掐断才经本分支穿出）。delta 清剪与
            # 正常收尾同口径：本轮已产出的终稿全文都在 chat_messages。
            final = self._persist_stopped()
            chat_store.update_thread(self.bb, self.thread_id, status="idle")
            try:
                self.bb.prune_chat_thread_deltas(self.project_id, self.thread_id)
            except Exception:  # noqa: BLE001 —— 清剪失败不影响收尾
                log.exception("chat delta 清剪失败 thread=%s", self.thread_id)
            return final
        except Exception as e:  # noqa: BLE001 —— 失败落结构化错误+事件，不静默
            log.exception("chat 轮失败 thread=%s", self.thread_id)
            info = _classify_error(e)
            # 结构化错误落库（前端据此渲染错误卡片；之前只亮「出错」徽标、
            # 错误原文既不落库也不展示，用户无从判断原因）
            chat_store.update_thread(self.bb, self.thread_id, status="error",
                                     error=info)
            self._emit("chat.error", {"thread_id": self.thread_id, **info})
            raise

    def _web_search_on(self) -> bool:
        """线程级「联网搜索」开关**实时读**（每步从 chat_threads 行读，不缓存）。

        v43（web-search-toggle）：composer「联网」打开 = 该线程的主控与其
        spawn 的子代理（call_expert / ARTEX Worker 建线程时从父线程继承）
        拿 web_search 工具。关 = 工具面与执行白名单都不给。读失败按关
        （宁严勿松：不让线程在开关状态不明时默认外发检索）。"""
        try:
            row = chat_store.get_thread(self.bb, self.thread_id)
            return bool((row or {}).get("web_search"))
        except Exception:  # noqa: BLE001 —— 读开关失败按关闭处理
            return self.web_search

    def _kb_iteration_on(self) -> bool:
        """项目级「知识库迭代」开关**实时读**（每步从黑板 projects 行读，不缓存）。

        与轮末自动提案（_maybe_kb_iteration）同口径：关 = Agent 不提任何提案——
        工具面不给 propose_pack_edit、系统提示不注入经验沉淀段、dispatcher 白名单
        剔除（执行层 ToolDispatcher 内另有硬闸双保险）。读失败/非法按关。"""
        try:
            from core.skills import kbiteration
            proj = self.bb.get_project(self.project_id)
            return kbiteration.is_enabled((proj or {}).get("config"))
        except Exception:  # noqa: BLE001 —— 读开关失败按关闭处理
            return False

    def _maybe_kb_iteration(self) -> None:
        """轮末知识库迭代（kb-iteration，2026-10-14，可选开关）：**best-effort**。

        仅**主控线程**触发（其轮次涵盖本轮全部子专家工作）；开关**实时重读**项目
        config（不缓存）；命中规则信号才由后台 daemon 线程调一次轻量 LLM 生成提案
        落 pending。**绝不阻塞会话轮、绝不改会话正文、异常只吞不抛**（详见
        core/skills/kbiteration.py）。"""
        if not self.is_orchestrator:
            return
        if not self._kb_iteration_on():
            return
        sid = f"chat-{self.thread_id[-12:]}"
        args = dict(
            packs_root=self.packs_root, llm=self.llm,
            session_id=sid, author=sid, thread_id=self.thread_id,
            capabilities=self.capabilities,
            since_event_id=int(getattr(self, "_round_start_event_id", 0) or 0),
            since_iso=getattr(self, "_round_start_iso", None))
        try:
            threading.Thread(
                target=lambda: kbiteration.run_iteration(
                    self.bb, self.project_id, **args),
                name=f"kb-iter-{self.thread_id[-8:]}", daemon=True).start()
        except Exception:  # noqa: BLE001 —— 起线程失败也不影响会话
            log.exception("kb-iteration 线程启动失败 thread=%s", self.thread_id)

    def _loop(self, user_text: str) -> str:
        """入口包装：整轮标记为本线程会话作用域（请求体留痕归属，2026-10-17）。"""
        from core.llm.dump import scope as dump_scope
        with dump_scope(self.project_id, self.thread_id):
            return self._loop_turn(user_text)

    def _loop_turn(self, user_text: str) -> str:
        system = self._system_prompt()
        # 系统提示快照（diagnostics-system-prompt，2026-10-16）：诊断页「查看系统
        # 提示词」的数据源。每轮覆盖式 UPSERT，best-effort（异常只 log 不阻断）。
        save_system_prompt(
            self.bb, project_id=self.project_id, session_id=self.thread_id,
            kind="chat", system=system, agent_id=self.agent_id,
            model=getattr(self.llm, "model", None))
        # 人类引用指定（/skill、/mcp chip）：当轮注入系统提示，不入库不污染历史
        refs_tail = ""
        if self._refs.get("skills") or self._refs.get("mcps"):
            parts = ["", "## 人类本轮指定"]
            if self._refs.get("skills"):
                parts.append("- 优先按技能 " + "、".join(self._refs["skills"])
                             + " 的打法执行；需要细节用 skill_open 读正文")
            if self._refs.get("mcps"):
                parts.append("- 优先使用 MCP server "
                             + "、".join(self._refs["mcps"]) + " 的工具完成相关任务")
            refs_tail = "\n".join(parts)
            system += refs_tail
        # 装载期瞬时 IO/DB 错误退避重试 1 次（2026-10-03）：历史读库与工具规格
        # 都会读盘（专家 yaml / 技能注册表），Windows 下偶发文件锁不该炸整轮。
        messages = _retry_transient(self._load_history)
        tools = _retry_transient(self._tool_specs)
        max_steps = _ORCH_MAX_STEPS if self.is_orchestrator else _EXPERT_MAX_STEPS
        # 任务级 deadline 夹逼（artex-taskclock）：把任务剩余时间折进本次 run 的
        # 墙钟预算，并记下「任务 deadline 是否是约束」。无 deadline 时
        # (eff=自有预算, clamped=False)，下面整段等价于引入前的行为。
        eff_budget, self._run_budget_clamped = clamp_max_duration(
            getattr(self, "task_deadline_at", 0.0),
            getattr(self, "run_budget_s", 0.0))
        run_started = time.monotonic()
        final = ""
        # 截断续写状态（2026-10-01）：continue_n=已续写次数；text_acc=逐段文本拼接
        continue_n = 0
        interrupt_n = 0   # 流中断续跑次数（2026-10-08）
        empty_response_n = 0
        dimension_nudges = 0  # 子专家维度收敛闸连续推回数（2026-10-09）
        echo_nudges = 0       # 推理回声推回数（2026-10-09）
        reasoning_nudges = 0  # 推理膨胀推回数（2026-10-09）
        text_acc: list[str] = []
        for _step in range(max_steps):
            if self._aborted():  # 步间中止：不落新 LLM 调用
                final = self._persist_stopped()
                break
            # 墙钟预算耗尽（artex-taskclock）：与步数预算并列的第二个收尾触发点。
            # 上游 harness 的 MaxDuration 同款——到点**自己**停并进收尾，而不是
            # 等 Engine 从外面 future.cancel()：硬砍时 Worker 连一句总结都来不及
            # 写，settlement 根本进不去，「已识别未写回」照样蒸发。
            if eff_budget > 0 and time.monotonic() - run_started >= eff_budget:
                self.budget_reason = REASON_TIMEOUT
                self._emit("chat.budget", {"phase": "timeout",
                                           "budget_s": eff_budget,
                                           "clamped": self._run_budget_clamped,
                                           "step": _step})
                break
            # ARTEX Worker 方向调整：在步边界取一次控制，steer 作为 user 消息
            # 注入下一轮（不落库、不污染历史；事件流留痕供前端展示）。
            self._poll_worker_control(force=True)
            for steer in self._take_pending_steer():
                messages.append({"role": "user",
                                 "content": f"[Worker 方向调整] {steer}"})
                self._emit("chat.steer", {"text": steer[:500]})
            step_nudges: list[str] = []  # 本步事后追加的系统提示（推理膨胀闸等）
            acc: list[str] = []
            pub = {"chars": 0, "seq": 0, "at": 0.0}
            thinking_acc: list[str] = []
            thinking_pub = {"chars": 0, "seq": 0, "at": 0.0}
            thinking_started = time.monotonic()

            def on_text(delta: str, _acc=acc, _pub=pub) -> None:
                _acc.append(delta)
                text = "".join(_acc)
                now = time.monotonic()
                grown = len(text) - _pub["chars"]
                if not text or (grown < _DELTA_MIN_CHARS
                                and (now - _pub["at"] < _DELTA_MIN_INTERVAL
                                     or grown <= 0)):
                    return
                _pub["chars"] = len(text)
                _pub["seq"] += 1
                _pub["at"] = now
                self._emit("chat.delta", {"text": text, "seq": _pub["seq"]})

            def on_thinking(delta: str, _acc=thinking_acc,
                            _pub=thinking_pub) -> None:
                """思考增量按累计全文发布，前端可直接覆盖当前内容。"""
                if not delta:
                    return
                _acc.append(delta)
                thinking = "".join(_acc)
                now = time.monotonic()
                grown = len(thinking) - _pub["chars"]
                if not thinking or (grown < _DELTA_MIN_CHARS
                                    and (now - _pub["at"] < _DELTA_MIN_INTERVAL
                                         or grown <= 0)):
                    return
                _pub["chars"] = len(thinking)
                _pub["seq"] += 1
                _pub["at"] = now
                self._emit("chat.thinking.delta", {
                    "text": thinking, "seq": _pub["seq"]})

            def _reset_delta(_acc=acc, _pub=pub) -> None:
                """截断整轮重试前：清空累加与节流基准，防「半截+新流」拼接上屏
                （前端取最后一条 chat.delta 的累计全文，从零重流即整段覆盖）。"""
                _acc.clear()
                _pub["chars"] = 0
                thinking_acc.clear()
                thinking_pub["chars"] = 0
                thinking_pub["seq"] = 0
                thinking_pub["at"] = 0.0

            def _provider_retry(attempt: int, total: int, status: int) -> None:
                """传输层重试进度：落事件让工作台在等待期间显示具体次数。"""
                self._emit("chat.retry", {
                    "phase": "retry", "attempt": attempt, "total": total,
                    "status": status,
                })

            # 末步强制终稿（2026-10-01）：最后一步不传工具，逼模型只能输出纯文本
            # 终稿，避免「步数耗尽但全程只调工具」→ 落「本轮未产出文本回复」。
            step_tools = None if _step == max_steps - 1 else (tools or None)
            # 发送前两级联动压缩（旧 tool_result 占位化 → 必要时 LLM 摘要）
            messages, ctx_info = self._prepare_context(messages, system,
                                                       step_tools or [])
            if ctx_info.get("compacted"):
                self._emit("chat.ctx", {"phase": "proactive", **ctx_info})
            # 上下文构成估算（每步重估：messages 随 tool_result 增长）；
            # 真值归一：块比例来自字符估算，总和强制等于 LLM 报告的窗口占用
            sys_len = len(system) - len(refs_tail)
            est_system = max(1, sys_len // 3) if sys_len else 0
            est_refs = _est_tokens(refs_tail)
            est_tools = _est_tokens(json.dumps(tools, ensure_ascii=False))
            est_msgs = sum(_est_tokens(_msg_text(m)) for m in messages)
            # 本 step 的 LLM 调用 + 无文本增量流中断的「原样重发」环（2026-10-09）。
            # 只吐了 thinking/工具参数就断流时（partial 但文本为空），无可续写的
            # 半截文本，但也没有内容提交——清 delta 后原样重发本 step 安全（前端按
            # 最新累计全文覆盖显示）。有半截文本的 partial 不走此环，交给下方续写。
            resp = None
            pending_err: LLMError | None = None
            for _resend in range(_CHAT_RESEND_MAX + 1):
                try:
                    resp = self._chat(messages, system, step_tools, on_text,
                                      on_thinking=on_thinking,
                                      on_retry=_reset_delta,
                                      on_provider_retry=_provider_retry)
                    break
                except LLMError as e:
                    # 人类中止优先于一切重试（2026-10-09）：流被 should_cancel
                    # 掐断时抛的也是 LLMError("已中断")，若不先判中止就会被当成
                    # 流中断反复重发/续写，停止迟迟不生效。
                    if self._aborted():
                        raise ChatStopped() from None
                    if (getattr(e, "partial", False) and not "".join(acc).strip()
                            and _resend < _CHAT_RESEND_MAX):
                        self._emit("chat.interrupted", {
                            "phase": "resend", "continues": _resend + 1,
                            "text_head": ""})
                        _reset_delta()
                        thinking_started = time.monotonic()
                        continue
                    pending_err = e
                    break
            if resp is None:
                if self._aborted():
                    # 中止落到本分支（重发环耗尽/非 partial 错误）时同样直接收尾
                    raise ChatStopped()
                e = pending_err
                # 流中断续跑（2026-10-08）：已吐增量后连接中断——半截文本已上屏、
                # 流不可重放。把它作为 assistant 前缀落库 + 追续写指令继续拼接
                # （与终稿截断续写同构）；无半截文本或超上限才如实失败。
                partial_text = "".join(acc)
                if not getattr(e, "partial", False) or not partial_text.strip():
                    raise e
                if self._aborted():  # 续写前再判（partial 半截文本分支）
                    raise ChatStopped()
                if interrupt_n >= _CHAT_INTERRUPT_MAX:
                    self._emit("chat.interrupted", {
                        "phase": "giveup", "continues": interrupt_n,
                        "text_head": partial_text[:200]})
                    raise e
                interrupt_n += 1
                self._emit("chat.interrupted", {
                    "phase": "continue", "continues": interrupt_n,
                    "text_head": partial_text[:200]})
                row = chat_store.append_message(
                    self.bb, self.thread_id, "assistant", partial_text,
                    thinking="".join(thinking_acc))
                self._flush_final_delta(partial_text, row["id"])
                messages.append({"role": "assistant", "content": partial_text})
                messages.append({"role": "user", "content": _INTERRUPT_NUDGE})
                text_acc.append(partial_text)
                continue
            # provider 可能只在响应终稿中返回 thinking（例如非流式替身或网关
            # 没有发送增量帧）。先补齐最后一小段 delta，再发终稿事件；前端在
            # 收到终稿前即可看到实时内容，收到终稿后得到完整文本。
            thinking = "".join(thinking_acc) or (resp.thinking or "")
            if thinking and len(thinking) > thinking_pub["chars"]:
                thinking_pub["chars"] = len(thinking)
                thinking_pub["seq"] += 1
                self._emit("chat.thinking.delta", {
                    "text": thinking, "seq": thinking_pub["seq"]})
            if thinking:
                self._emit("chat.thinking", {
                    "thinking": thinking,
                    "duration_s": round(time.monotonic() - thinking_started, 2),
                })
            # 上下文用量：input(+cache)=当步窗口占用，output/steps 跨步累计
            u = resp.usage
            input_total = u.input_tokens + u.cache_read_tokens \
                + u.cache_creation_tokens
            scale = input_total / max(1, est_system + est_refs
                                      + est_tools + est_msgs)
            b_system = round(est_system * scale)
            b_refs = round(est_refs * scale)
            b_tools = round(est_tools * scale)
            win, soft = self._ctx_window()
            self._usage = {
                "input": input_total,
                # 累计口径（2026-10-13 缓存命中率）：跨步累加，供面板算「会话整体」
                # 命中率（当步键 input/cache_read/cache_creation 不动，向后兼容）。
                "input_total": self._usage.get("input_total", 0) + input_total,
                "output": self._usage.get("output", 0) + u.output_tokens,
                "steps": self._usage.get("steps", 0) + 1,
                "cache_read": u.cache_read_tokens,
                "cache_read_total": self._usage.get("cache_read_total", 0)
                                    + u.cache_read_tokens,
                "cache_creation": u.cache_creation_tokens,
                "cache_creation_total": self._usage.get("cache_creation_total", 0)
                                        + u.cache_creation_tokens,
                "compaction": ctx_info,
                # 面板分母（硬窗口）与警戒线（软上限）——后端下发真实值（ct-8）
                "ctx_limit": win,
                "ctx_soft": soft,
                "breakdown": {
                    "system": b_system,
                    "refs": b_refs,
                    "tools": b_tools,
                    # 余数兜底：四块之和恒等于 input_total
                    "messages": max(0, input_total - b_system - b_refs
                                    - b_tools),
                },
            }
            chat_store.update_thread(self.bb, self.thread_id, usage=self._usage)
            self._emit("chat.usage", dict(self._usage))
            if self._aborted():  # 流式中止：本步作废
                final = self._persist_stopped()
                break
            # 推理膨胀闸（2026-10-09）：本步思考超长 → 把「别再盘算」并入本轮提示
            # （工具路径随 tool_result 下发；无工具时由下方空响应/回声分支带上）。
            if (len(thinking) > _REASONING_BLOAT_CHARS
                    and reasoning_nudges < _REASONING_NUDGE_MAX):
                reasoning_nudges += 1
                step_nudges.append(_REASONING_NUDGE)
                self._emit("chat.reasoning_gate", {
                    "phase": "nudge", "bloat_chars": len(thinking),
                    "nudges": reasoning_nudges, "total": _REASONING_NUDGE_MAX})
            text = resp.text or ""
            if not text.strip() and not resp.tool_calls:
                if empty_response_n < _EMPTY_RESPONSE_RETRIES:
                    empty_response_n += 1
                    self._emit("chat.retry", {
                        "phase": "empty_response",
                        "attempt": empty_response_n,
                        "total": _EMPTY_RESPONSE_RETRIES,
                        "reason": "empty_response",
                    })
                    # 独立续问（2026-10-11）：新起一条 user 消息打破上下文相同性，
                    # 而非并入 tool_result——后者让「确定性空响应」的重试等同空转。
                    nudge = "\n\n".join(
                        [_empty_response_nudge(empty_response_n), *step_nudges])
                    _append_standalone_nudge(messages, nudge)
                    # 重试前一级占位化（2026-10-16 事故修复）：空响应最常见成因
                    # 是「大输入 → 推理爆额度 → 无正文」（实测：416 条资产全量
                    # 枚举后做去重分析，空转 3 次共 6 分 26 秒才落兜底）。nudge
                    # 只加在消息末尾，不改变「巨输入」结构 → 重试等同空转。keep
                    # 收紧到 _EMPTY_RETRY_KEEP_STEPS 步，把区外的旧 tool_result
                    # 换成占位符（零 LLM 成本，幂等）；不触发二级 LLM 摘要——
                    # 空响应场景再花一次 LLM 调用不划算。仅命中空响应重试路径。
                    masked = _mask_old_tool_results(
                        messages,
                        _recent_boundary(messages, _EMPTY_RETRY_KEEP_STEPS))
                    if masked:
                        self._emit("chat.ctx", {
                            "phase": "empty_retry", "level": 1,
                            "masked": masked})
                    continue
                # 止血（2026-10-16 事故修复）：重试耗尽时若本步有 thinking 累积，
                # 降级为可见终稿——「光想不写」的轮次至少交付推理过程（前缀声明
                # 这是推理而非结论），用户可基于它继续追问；thinking 也为空才落
                # 兜底文案。
                thinking_text = thinking if thinking.strip() else ""
                if thinking_text:
                    final = (
                        "（本轮模型未产出正式回复；以下为其推理过程，供参考）\n\n"
                        + thinking_text)
                    chat_store.append_message(
                        self.bb, self.thread_id, "assistant", final,
                        thinking=thinking_text)
                else:
                    final = _EMPTY_FINAL_TEXT
                    chat_store.append_message(self.bb, self.thread_id,
                                              "assistant", final)
                break
            else:
                empty_response_n = 0
            if resp.tool_calls:
                call_ids = [str(tc.id or "").strip() for tc in resp.tool_calls]
                if (any(not cid for cid in call_ids)
                        or len(call_ids) != len(set(call_ids))
                        or any(not str(tc.name or "").strip()
                               or not isinstance(tc.arguments, dict)
                               for tc in resp.tool_calls)):
                    raise LLMError("工作台工具调用 ID/名称/参数不满足序列协议")
                row = chat_store.append_message(
                    self.bb, self.thread_id, "assistant", text,
                    thinking=thinking,
                    tool_calls=[{"id": tc.id, "name": tc.name,
                                 "args": tc.arguments} for tc in resp.tool_calls])
                self._flush_final_delta(text, row["id"])
                messages.append({"role": "assistant", "content": [
                    *( [{"type": "text", "text": text}] if text else []),
                    *[{"type": "tool_use", "id": tc.id, "name": tc.name,
                       "input": tc.arguments} for tc in resp.tool_calls],
                ]})
                tool_result_blocks: list[dict[str, Any]] = []
                aborted = False
                serial_results: dict[str, tuple[bool, str, float]] = {}
                # 先执行混合批次中的普通工具；只有它们完成后才启动专家线程，
                # 避免 todo_write/bb_query 与专家并发写共享状态。
                for tc in resp.tool_calls:
                    if tc.name == "call_expert" or self._aborted():
                        continue
                    started = time.perf_counter()
                    self._emit("chat.tool", {
                        "phase": "start", "name": tc.name,
                        "tool_call_id": tc.id,
                        "args_head": json.dumps(tc.arguments, ensure_ascii=False,
                                                 separators=(",", ":"))[:300]})
                    ok, result = self._dispatch(tc)
                    serial_results[tc.id] = (ok, result, started)
                parallel_results, probe_pending = (
                    self._parallel_expert_dispatch(
                        resp.tool_calls, first_return=self.is_first_return_controller))
                for tc in resp.tool_calls:
                    # 并行专家：worker 内已完成并**落库**（见 _parallel_expert_dispatch），
                    # 这里只按原始 tool_call 顺序补 history block——前端靠 2s 轮询
                    # messages 就能逐个看到完成，不必依赖会滚出窗口的实时事件。
                    if tc.id in parallel_results:
                        ok, result = parallel_results[tc.id]
                        tool_result_blocks.append({
                            "type": "tool_result", "tool_use_id": tc.id,
                            "content": result})
                        continue
                    if aborted or self._aborted():
                        # 中止：为当前及剩余 tool_calls 补落占位结果，保证
                        # assistant(tool_calls) 与 tool 消息配对完整（2026-10-01
                        # 健壮性）。否则首个工具前中止会留下悬空 tool_calls，
                        # 下一轮回放被网关 400 拒收（insufficient tool messages），
                        # 线程永久损坏。
                        aborted = True
                        if tc.name == "call_expert":
                            # call_expert 的工具结果恒由 _parallel_expert_dispatch 的
                            # run_one 落库（含中止摘要，主线程已不再等待它收尾）——
                            # 此处不补占位，避免同 tool_use_id 双写。悬空由 run_one
                            # 或 _repair_dangling_tool_calls 兜底。
                            continue
                        result = "[错误] 已停止：工具未执行"
                        chat_store.append_message(self.bb, self.thread_id, "tool",
                                                  result, tool_use_id=tc.id)
                        tool_result_blocks.append({
                            "type": "tool_result", "tool_use_id": tc.id,
                            "content": result})
                        continue
                    if tc.id in probe_pending:
                        # 先返先触发：该并发专家已被提前断候、仍在后台跑。补占位
                        # tool 结果保 pairing 合法（DB+内存同写）；真实结果由
                        # worker 完成后以同 id 续写 + _drain_probe_overflow
                        # 在本轮后续步边界并入上下文。
                        note = (_PROBE_PENDING + " 该并发专家仍在后台执行；"
                                "完成后结果将以「[后台并发结果]」消息补充送达。")
                        chat_store.append_message(self.bb, self.thread_id, "tool",
                                                  note, tool_use_id=tc.id)
                        tool_result_blocks.append({
                            "type": "tool_result", "tool_use_id": tc.id,
                            "content": note})
                        continue
                    t0 = time.perf_counter()
                    if tc.id in serial_results:
                        ok, result, started = serial_results[tc.id]
                    else:
                        # 单个 call_expert 保留原有串行路径；普通工具已在前置阶段执行。
                        started = time.perf_counter()
                        self._emit("chat.tool", {
                            "phase": "start", "name": tc.name,
                            "tool_call_id": tc.id,
                            "args_head": json.dumps(
                                tc.arguments, ensure_ascii=False,
                                separators=(",", ":"))[:300]})
                        ok, result = self._dispatch(tc)
                    result, artifact_path = self._clip_tool_result(result, tc)
                    chat_store.append_message(
                        self.bb, self.thread_id, "tool", result,
                        tool_use_id=tc.id)
                    tool_result_blocks.append({
                        "type": "tool_result", "tool_use_id": tc.id,
                        "content": result})
                    # 串行/单专家路径补 done 事件（单专家此前漏发，行会一直「运行中」
                    # 直到轮末落库；现与并行路径一致，实时即可收尾）。
                    self._emit("chat.tool", {
                        "phase": "done",
                        "name": tc.name, "tool_call_id": tc.id,
                        "args_head": json.dumps(
                            tc.arguments, ensure_ascii=False,
                            separators=(",", ":"))[:300],
                        "result_head": result[:_TOOL_RESULT_EVENT_HEAD],
                        "ok": ok,
                        "artifact_path": artifact_path,
                        "duration_s": round(time.perf_counter() - t0, 2)})
                if tool_result_blocks:
                    messages.append({"role": "user",
                                     "content": tool_result_blocks})
                if self.is_first_return_controller:
                    # 先返先触发：把窗口内后台完成的并发结果并进下一轮 LLM
                    # 请求（skip=本批已作为正式 tool_result 交回的 id，防双落盘）
                    self._drain_probe_overflow(
                        messages, skip_ids=set(parallel_results))
                if step_nudges:
                    _inject_nudge(messages, "\n\n".join(step_nudges))
                if aborted:
                    # 中止说明落库（在补落的 tool 结果之后，保持配对顺序合法）
                    final = self._persist_stopped()
                    break
                if final:
                    break
                continue
            # 纯文本回复 = 轮终稿（**2026-10-01 截断续写**）：若被输出上限截断
            # （stop_reason=length/max_tokens），不当作终稿收尾——把半截文本作为
            # assistant 前缀入历史 + 追一条 user 催续，继续循环拼接，避免「话说
            # 一半就正常终止」。续写次数超上限才接受现有文本并落 chat.truncated。
            # 推理回声守卫（2026-10-09 事故）：终稿其实只是整段推理的复制品时
            # （上游把同一段推理同时灌进内容与思考两个通道），不当终稿收轮——推回
            # 让模型给结论。解析层已做同类清空，这里兜未经该路径的 provider；上限后
            # 放行（宁可落一次正文，也不无限推回）。
            if text.strip() and is_reasoning_echo(text, thinking):
                if echo_nudges < _REASONING_ECHO_MAX:
                    echo_nudges += 1
                    self._emit("chat.reasoning_echo", {
                        "phase": "nudge", "chars": len(text),
                        "nudges": echo_nudges, "total": _REASONING_ECHO_MAX})
                    _inject_nudge(messages, "\n\n".join(
                        [_REASONING_ECHO_NUDGE, *step_nudges]))
                    continue
                self._emit("chat.reasoning_echo", {
                    "phase": "giveup", "chars": len(text), "nudges": echo_nudges})
            if _is_truncated_final(resp) and text.strip():
                if continue_n >= _CHAT_CONTINUE_MAX:
                    self._emit("chat.truncated", {
                        "phase": "giveup", "continues": continue_n,
                        "text_head": text[:200]})
                else:
                    continue_n += 1
                    self._emit("chat.truncated", {
                        "phase": "continue", "continues": continue_n,
                        "text_head": text[:200]})
                    row = chat_store.append_message(
                        self.bb, self.thread_id, "assistant", text,
                        thinking=thinking)
                    self._flush_final_delta(text, row["id"])
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": _CONTINUE_NUDGE})
                    text_acc.append(text)
                    continue
            # 收尾前拦未关意图（2026-10-01 intent-tools-chat）：子专家声明过意图
            # 就必须收尾，否则链路图永远挂着 open 意图（对话链没有任务管线的
            # finish 拦截）。仅拦子专家——主控是调度者，其意图由委派流程负责。
            open_ids = self._open_intents_of_self()
            if open_ids and not self.is_orchestrator:
                nudge = ("你还有未收尾的意图：" + ", ".join(open_ids)
                         + "。链路图要求意图必收尾——先 close_intent"
                         "（outcome=vuln/finding/dead_end）收尾每条意图，"
                         "再给最终摘要。若意图不再成立，用 bb_delete_intent 物理删除。")
                messages.append({"role": "user", "content": nudge})
                self._emit("chat.intent_guard",
                           {"phase": "blocked", "open_intents": open_ids})
                continue
            # 维度收敛闸（2026-10-09 agent-path-intent-loop M2）：spawn 委派的
            # 子专家收尾前，检查已锚定资产的测试维度覆盖；有未覆盖面（或根本没
            # plan）推回 plan，而不是允许提前给最终摘要。
            dim_status = self._dimension_gate_status()
            if dim_status is not None and not dim_status.get("ok"):
                if dimension_nudges < _DIMENSION_GATE_MAX:
                    dimension_nudges += 1
                    messages.append({"role": "user",
                                     "content": self._dimension_gate_nudge(dim_status)})
                    self._emit("chat.dimension_gate", {
                        "phase": "nudge", "nudges": dimension_nudges, **dim_status})
                    continue
                self._emit("chat.dimension_gate", {
                    "phase": "giveup", "nudges": dimension_nudges, **dim_status})
            row = chat_store.append_message(self.bb, self.thread_id,
                                            "assistant", text,
                                            thinking=thinking)
            self._flush_final_delta(text, row["id"])
            self._emit("chat.message", {"role": "assistant",
                                        "text": text[:2000],
                                        "message_id": row["id"]})
            text_acc.append(text)
            final = "".join(text_acc)
            break
        if not final:
            # 走到这里 = 主循环没拿到终稿：两种收尾触发点都落在这里——
            # ① for 循环跑完 max_steps（撞**步数**上限，上游 ReasonMaxTurns）；
            # ② 步间墙钟检查命中（撞**墙钟**预算，上游 ReasonTimeout）。
            # 两者都属「没做完」（artex-exhausted），但收尾词不同（见 _wrapup）。
            # 中止路径（_persist_stopped）会先设好 final 并 break，不进这里，
            # 故人工/控制停不会被误标 exhausted。
            self.budget_exhausted = True
            if not self.budget_reason:
                self.budget_reason = REASON_MAX_TURNS
            # 步数耗尽但有累积文本（截断续写中途用尽）→ 用累积文本收尾并标记截断
            if text_acc and any(t.strip() for t in text_acc):
                final = "".join(text_acc)
                self._emit("chat.truncated", {
                    "phase": "giveup", "reason": "steps_exhausted",
                    "continues": continue_n, "text_head": final[:200]})
            else:
                final = _EMPTY_FINAL_TEXT
                chat_store.append_message(self.bb, self.thread_id,
                                          "assistant", final)
            # 收尾阶段（artex-wrapup，对齐上游 Settlement）：撞上限后**先捞回**
            # 已识别未写回的内容，再收尾——否则这段观察随轮次结束一起蒸发。
            # 中止（_aborted）不进来：人工/控制停就该立刻停，不给它再跑 10 轮。
            if not self._aborted():
                salvaged = self._wrapup(messages, system, tools)
                if salvaged:
                    final = salvaged
        return final

    def _wrapup(self, messages: list[dict[str, Any]], system: str,
                tools: list[dict[str, Any]] | None) -> str:
        """撞预算后的收尾阶段：只许写回，跑独立轮数预算，返回终稿文本。

        上游 ``agent/wrapup.go`` + ``worker.go:425`` 的三条固定策略：
        ① 注入收尾提示词；② 独立轮数预算（``WRAPUP_MAX_TURNS``）；③ 禁用
        探测类工具、保留落库工具（**代码固定，不开放配置**——用户不能把这个
        guardrail 改掉）。

        收尾词按**实际 reason** 现场挑（``wrapup_prompt_for_reason``，对齐上游
        ``PromptByReason``）：任务到点说「整个任务结束、做最后判定」，本次 run
        预算耗尽说「本轮结束、系统还会唤醒你」——两套语义常相反，用错会把
        planner 推向反方向（任务到点还在派永远不执行的新意图）。

        返回值：收尾产出的终稿文本；收尾失败/无产出时返回空串，由调用方保留
        原兜底文本（收尾是**尽力而为**，绝不能因为它失败把整轮炸掉）。
        """
        if not tools:
            return ""
        agent_key = "worker" if self.is_worker else (
            "planner" if self.is_artex else self.agent_id)
        reason = str(getattr(self, "budget_reason", "") or REASON_MAX_TURNS)
        prompt = wrapup_prompt_for_reason(
            agent_key, reason,
            clamped=bool(getattr(self, "_run_budget_clamped", False)),
            is_orchestrator=self.is_orchestrator)
        self._wrapup_active = True
        # 重建工具面与执行白名单（此刻 _wrapup_active=True，两处裁剪生效）
        try:
            wrap_tools = self._tool_specs()
            self._dispatcher = self._build_dispatcher()
        except Exception:  # noqa: BLE001 —— 裁剪失败就用原工具面继续
            log.exception("收尾阶段工具面重建失败 thread=%s", self.thread_id)
            wrap_tools = tools
        self._emit("chat.wrapup", {"phase": "start", "agent": agent_key,
                                   "max_turns": wrapup_max_turns(agent_key),
                                   "reason": reason,
                                   "clamped": bool(getattr(
                                       self, "_run_budget_clamped", False)),
                                   "tools": len(wrap_tools or [])})
        # 收尾提示注入：用 `_inject_nudge` 而非裸 append——主循环结束时尾部
        # 通常是 user（tool_result 块），直接 append 会产生**连续两条 user**，
        # Anthropic 严格角色交替会 400 拒收（本仓既有坑，见 _inject_nudge 注释）。
        _inject_nudge(messages, prompt)
        # 只统计**收尾阶段自己产出**的文本：主循环的 text_acc 是截断收尾的旧文本，
        # 混进来会让「收尾无产出」也误报成捞回成功。
        wrap_text: list[str] = []
        salvaged = ""
        try:
            for _step in range(wrapup_max_turns(agent_key)):
                if self._aborted():
                    break
                # 末步强制纯文本终稿（同主循环口径），逼它给出那句总结
                step_tools = None if _step == wrapup_max_turns(agent_key) - 1 \
                    else (wrap_tools or None)
                resp = self._chat(messages, system, step_tools, None)
                text = str(getattr(resp, "text", "") or "").strip()
                thinking = str(getattr(resp, "thinking", "") or "")
                calls = getattr(resp, "tool_calls", None) or []
                if not calls:
                    # 无工具调用 = 收尾终稿（可能为空：模型没话可说，直接结束）
                    if text:
                        row = chat_store.append_message(
                            self.bb, self.thread_id, "assistant", text,
                            thinking=thinking)
                        self._emit("chat.message", {"role": "assistant",
                                                    "text": text[:2000],
                                                    "message_id": row["id"],
                                                    "wrapup": True})
                        wrap_text.append(text)
                        salvaged = "".join(wrap_text)
                    break
                # 落库调用：assistant(tool_calls) → 逐个执行 → tool_result 配对
                row = chat_store.append_message(
                    self.bb, self.thread_id, "assistant", text,
                    thinking=thinking,
                    tool_calls=[{"id": tc.id, "name": tc.name,
                                 "args": tc.arguments} for tc in calls])
                self._flush_final_delta(text, row["id"])
                if text:
                    wrap_text.append(text)
                blocks = []
                for tc in calls:
                    ok, result = self._dispatch(tc)
                    result, _artifact = self._clip_tool_result(result, tc)
                    chat_store.append_message(self.bb, self.thread_id, "tool",
                                              result, tool_use_id=tc.id)
                    blocks.append({"type": "tool_result", "tool_use_id": tc.id,
                                   "content": result})
                messages.append({"role": "assistant", "content": [
                    *([{"type": "text", "text": text}] if text else []),
                    *[{"type": "tool_use", "id": tc.id, "name": tc.name,
                       "input": tc.arguments} for tc in calls]]})
                messages.append({"role": "user", "content": blocks})
            else:
                # 10 轮都只发工具调用、始终没给终稿 → 用收尾期累积文本兜底
                salvaged = "".join(wrap_text)
        except Exception:  # noqa: BLE001 —— 收尾尽力而为，失败不炸整轮
            log.exception("收尾阶段执行失败 thread=%s", self.thread_id)
        finally:
            self._wrapup_active = False
            try:  # 复原工具面/白名单（本轮之后可能还有下一轮对话）
                self._dispatcher = self._build_dispatcher()
            except Exception:  # noqa: BLE001
                log.exception("收尾后工具面复原失败 thread=%s", self.thread_id)
        self._emit("chat.wrapup", {"phase": "done",
                                   "salvaged_chars": len(salvaged)})
        return salvaged

    def _persist_stopped(self) -> str:
        """中止收尾：落一条可见的停止说明（消息+事件），线程状态由 run() 归位。"""
        if self._worker_kill_reason:
            text = f"（Worker 已按控制指令终止：{self._worker_kill_reason}）"
        else:
            text = "（已按人类要求停止本轮执行；可继续追问或重新发起）"
        row = chat_store.append_message(self.bb, self.thread_id,
                                        "assistant", text)
        self._emit("chat.message", {"role": "assistant", "text": text,
                                    "message_id": row["id"], "stopped": True,
                                    "kill_reason": self._worker_kill_reason})
        return text

    def _flush_final_delta(self, text: str, message_id: int) -> None:
        """终稿前的兜底 delta（不足节流阈值的尾部也要发，前端才能收满）。"""
        if text:
            self._emit("chat.delta", {"text": text, "seq": 10_000,
                                      "final_message_id": message_id})

    # ---------- 工具分发 ----------

    def _artex_ctx(self) -> SteeringContext:
        """装配 ARTEX 操舵上下文（每次调用重建，保证读到最新引擎与专家池）。"""
        return SteeringContext(
            bb=self.bb, project_id=self.project_id, track=self.track,
            llm=self.artex_planner_llm, expert_names=self._delegatable_experts(),
            engine_provider=self.artex_engine_provider,
            author=f"chat-{self.thread_id[-12:]}",
            task_id=self.artex_task_id,
            thread_id=self.thread_id)

    def _dispatch(self, tc) -> tuple[bool, str]:
        """工具分发：返回 (ok, 结果文本)。失败文案统一以「[错误]」前缀，
        是持久化消息与前端渲染共用的单点失败判定约定。

        **兜底红线（2026-10-03）**：整个分发体包一层 try——工具是 LLM 驱动的
        循环，任何工具侧异常（含 `call_expert` 的 ChatTurn 构造读专家 yaml 失败
        这类「非 _dispatcher 分支」）都必须**转成 tool_result 文本回给 LLM**，
        让它自行改道/重试；此前只有 `_dispatcher.dispatch` 分支有 try，其余分支
        （mcp__ / todo_write / call_expert）抛出的异常会直接穿出 `_loop` 把整轮
        炸成「执行异常 unknown」——工具失败不该是轮失败。"""
        if self._aborted():
            return False, "[错误] 已停止：工具未执行"
        name = getattr(tc, "name", "") or ""
        # 收尾阶段硬闸（artex-wrapup，对齐上游 Settlement.DisabledTools）：收尾只
        # 允许「写回已识别内容」，不许借收尾继续探测。工具面与白名单已双边裁剪，
        # 这里是第三道闸——模型可能从历史里记得某个工具名直接调，schema 撤了也照调。
        # getattr 兜底：_dispatch 是所有工具调用的热路径，任何缺属性的壳（测试
        # 构造 / 旧实例）都不该在这里抛。
        if getattr(self, "_wrapup_active", False) and not wrapup_allowed(name):
            return False, (
                f"[错误] 收尾阶段不可用 {name}：预算已耗尽，现在只能把已识别的"
                "内容写回（资产/事实/漏洞/待办），不能再发起新的探测。"
                "请立即写回，然后用一句话总结。")
        try:
            args = tc.arguments or {}
            # 探针子任务（probe_mode）硬拒面（2026-10-17）：_PROBE_EXCLUDED_TOOLS
            # 里的 intent/plan 类工具带 plan/control flag——ToolDispatcher 的
            # 白名单检查对收尾原语**恒放行**（§6.6），只从 allowed_tools 裁
            # 挡不住模型按惯性调 declare_intent。故在对话链入口显式拒，文案
            # 说明「为什么没有、该干嘛」，模型读一次即改道，不烧熔断步数。
            if (self.probe_mode or self.intent_free) and name in _PROBE_EXCLUDED_TOOLS:
                r = (f"[错误] 工具 {name} 在本线程不可用：这是激进主控派出的"
                     "快速子任务——不声明意图、不查询黑板；按任务描述直接"
                     "执行，结论以 [命中]/[未命中] 开头回传。")
                self._record_failure(name, r, args)
                return False, r
            if name.startswith("mcp__"):
                r = self._dispatch_mcp(name, args)
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            if name == "todo_write":
                r = self._tool_todo_write(args)
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            if name == "call_expert":
                r = self._tool_call_expert(args)
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            if self.is_artex and is_artex_tool(name):
                r = artex_steering_dispatch(self._artex_ctx(), name, args)
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            # Worker 间过程级信息交换（v46）：ARTEX Worker trace 查询直接走
            # artex_traces，不经过 ToolDispatcher（后者无这些工具的 handler）。
            if name in _WORKER_TRACE_TOOLS and self.artex_intent_id:
                r = self._tool_worker_trace(name, args)
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            # 事实写回（artex-record-fact）：同走 artex_facts，不经 ToolDispatcher。
            if name == RECORD_FACT_TOOL_NAME and self.artex_intent_id:
                r = dispatch_record_fact(
                    self.bb, self.project_id, self.artex_task_id,
                    self.artex_intent_id, args,
                    author=f"chat-{self.thread_id[-12:]}")
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            if self._dispatcher is not None:
                r = self._dispatcher.dispatch(name, args)
                # agent 工具面失败文本约定（[错误] 为主，兼容 [工具异常]/[参数格式]）
                # 注意：dispatcher 分支的失败由 ToolDispatcher.dispatch 自记
                # tool.failed（diagnostics-log），此处不重复记。
                return not r.startswith(("[错误", "[工具异常", "[参数格式")), r
            self._record_failure(name, f"[错误] 工具 {name} 不可用：本线程未装配工具面", args)
            return False, f"[错误] 工具 {name} 不可用：本线程未装配工具面"
        except Exception as e:  # noqa: BLE001 —— 工具异常回文本，绝不炸整轮
            log.exception("chat 工具分发异常 name=%s thread=%s",
                          name, self.thread_id)
            self._record_failure(name, f"[错误] 工具 {name} 执行异常: {e}", args)
            return False, f"[错误] 工具 {name} 执行异常: {e}"

    def _tool_worker_trace(self, name: str, args: dict[str, Any]) -> str:
        """Worker 间过程级信息交换：list/get/search 三件套。"""
        try:
            if name == "artex_worker_trace_list":
                rows = list_worker_traces(
                    self.bb, self.project_id,
                    task_id=self.artex_task_id or "",
                    q=str(args.get("q") or "").strip(),
                    limit=int(args.get("limit") or 50))
                return json.dumps({"works": rows}, ensure_ascii=False, default=str)
            if name == "artex_worker_trace_get":
                result = get_worker_trace(
                    self.bb, self.project_id,
                    intent_id=str(args.get("intent_id") or "").strip(),
                    q=str(args.get("q") or "").strip(),
                    step_ids=args.get("step_ids"),
                    limit=int(args.get("limit") or 100))
                return json.dumps(result, ensure_ascii=False, default=str)
            if name == "artex_worker_trace_search":
                result = search_all_worker_traces(
                    self.bb, self.project_id,
                    task_id=self.artex_task_id or "",
                    q=str(args.get("q") or "").strip(),
                    exclude_intent_id=self.artex_intent_id or "",
                    limit=int(args.get("limit") or 100))
                return json.dumps(result, ensure_ascii=False, default=str)
            return f"[错误] 未知 trace 工具: {name}"
        except Exception as exc:  # noqa: BLE001
            log.exception("artex_worker_trace 失败 name=%s thread=%s", name, self.thread_id)
            return f"[错误] {name} 执行异常: {exc}"

    def _record_failure(self, name: str, result: str,
                        args: dict | None = None) -> None:
        """chat 侧失败落诊断日志（diagnostics-log，2026-10-14）。

        只记**不经 ToolDispatcher.dispatch 的分支**——mcp__（server 离线/协议
        错误/工具报错，记 mcp.failed）、todo_write/call_expert/分发异常/无
        工具面（记 tool.failed）。dispatcher 分支由 dispatch 自记，避免双写。
        best-effort：记录失败不影响工具返回（failog 内已兜底不抛）。
        """
        if not result.startswith("[错误"):
            return
        # 诊断记录不是主路径：未装配黑板的半成品对象（object.__new__ 回放/测试）
        # 直接跳过，绝不能因为「记录失败」把工具错误放大成整轮异常。
        bb = getattr(self, "bb", None)
        if bb is None:
            return
        author = f"chat-{self.thread_id[-12:]}"
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            if len(parts) == 3:
                record_mcp_failure(
                    bb, self.project_id, server=parts[1], tool=parts[2],
                    error=result, session_id=author, author=author,
                    thread_id=self.thread_id)
                return
        record_tool_failure(
            bb, self.project_id, name=name, args=args, error=result,
            session_id=author, author=author, thread_id=self.thread_id)

    def _parallel_expert_dispatch(
            self, tool_calls,
            first_return: bool = False) -> tuple[dict[str, tuple[bool, str]],
                                                 list[str]]:
        """并发执行同一批中的 call_expert。

        返回 ``(results, pending_ids)``：``results`` 按 tool_call id 索引的
        已完成结果；``pending_ids`` 为**仍在后台运行**的调用 id 列表（仅
        ``first_return`` 模式可能非空，恒为空表示整批已收齐）。

        只要有专家调用就走并发路径（含单个——2026-10-12 起，此前 <2 个回落主线程
        串行）：单个专家卡在长工具（MCP ≤60s / 浏览器 / 大扫描）时，主线程串行执行
        会一并卡住，点「停止」要等它自然收尾才生效。其他工具不参与并行，仍由主线程
        按原序执行，确保黑板写入工具的调用顺序与既有语义不变。ThreadPoolExecutor
        的 worker 数有上限，超出的子专家在队列中等待。

        **每个专家一完成即持久化工具结果**（不等整批）：前端 2s 轮询 messages
        即可逐个反映完成态，不再依赖会滚动出窗的 chat.tool 实时事件——5 专家轮
        里子线程刷屏数万条事件，早期 done 事件会被前端水合窗口（HYDRATE_LIMIT）
        挤掉，只显部分「执行完成」（2026-10-06 实测）。乱序落库安全：
        `_load_history` 装载时按 tool_use 顺序重排（`_sanitize_history`）。

        **中止即返（2026-10-12 事故修复）**：主线程只轮询中止信号，一旦置位**立即
        返回**，绝不等待专家收尾——`with ThreadPoolExecutor` 退出时的 `shutdown(
        wait=True)` 会一直阻塞到所有子专家自然收尾（含卡在不可中断工具里的），
        点停止迟迟不生效（用户实测）。现改为显式 `shutdown(wait=False)`：主线程
        马上回到步边界结束本轮；未完成的专家线程跑完当前 future 后自行退出。结果
        落库职责单一化——**call_expert 的工具结果恒由 `run_one` 落库**（含中止摘要），
        主循环对 call_expert 不再补「已停止」占位（见 `_loop` 中止分支），故无双写。

        **先返先触发（first_return，2026-10-17 激进主控专属）**：只要**第一个**
        专家完成就停止等待并把已收结果交回主控（顺手用 timeout=0 再捞一轮窗口内
        刚好收尾的），剩余探针标记进 `_probe_abandoned` 进 pending_ids：
        主循环为它们补**占位** tool 结果（`_PROBE_PENDING`，保 tool_use 配对
        合法——缺 result 的 tool_use 会被网关 400 拒收），worker 完成后**不再
        独占该 id**：真实结果推 `_probe_overflow`（主循环步边界
        `_drain_probe_overflow` 并入下一轮 LLM 上下文）+ 以**同 tool_use_id
        续写**真实 tool 行（`_load_history` 与前端都按「同 id 后写优先」选取，
        跨轮回放不丢、审计完整）。中止路径**不**标记断候（worker 照旧落真实
        结果，与原语义一致）。chat-orchestrator 恒走等整批的默认路径。
        """
        experts = [tc for tc in tool_calls if tc.name == "call_expert"]
        # 混合批次中，非 call_expert 工具仍由主线程按原序执行；所有专家调用
        # 默认视为相互独立并发。结果稍后按原始 tool_call 顺序拼 history block，
        # 保证重放稳定。
        if not experts:
            return {}, []
        results: dict[str, tuple[bool, str]] = {}

        def run_one(tc):
            started = time.perf_counter()
            self._emit("chat.tool", {
                "phase": "start", "name": tc.name,
                "tool_call_id": tc.id,
                "args_head": json.dumps(tc.arguments, ensure_ascii=False,
                                         separators=(",", ":"))[:300],
                "parallel": True,
            })
            try:
                ok, result = self._dispatch(tc)
            except Exception as e:  # noqa: BLE001
                ok, result = False, f"[错误] 工具异常: {e}"
            # 完成即落库（乱序安全，见方法 docstring）。落库失败不阻断其余专家。
            result, artifact_path = self._clip_tool_result(result, tc)
            # 先返先触发：已被主线程提前断候的探针，真实结果照旧以同 id 续写
            # tool 行（跨轮回放/审计），另推 overflow 供本轮内上下文补达。
            with self._probe_lock:
                dropped = tc.id in self._probe_abandoned
                if dropped:
                    self._probe_overflow.append({
                        "id": tc.id,
                        "expert": str((tc.arguments or {}).get("expert") or ""),
                        "ok": ok, "result": result})
            try:
                chat_store.append_message(self.bb, self.thread_id, "tool",
                                          result, tool_use_id=tc.id)
            except Exception:  # noqa: BLE001
                log.exception("chat 并行专家结果落库失败 thread=%s id=%s",
                              self.thread_id, tc.id)
            self._emit("chat.tool", {
                "phase": "done", "name": tc.name,
                "tool_call_id": tc.id,
                "args_head": json.dumps(tc.arguments, ensure_ascii=False,
                                         separators=(",", ":"))[:300],
                "result_head": result[:_TOOL_RESULT_EVENT_HEAD], "ok": ok,
                "parallel": True,
                "artifact_path": artifact_path,
                "background": dropped,
                "duration_s": round(time.perf_counter() - started, 2),
            })
            return tc.id, (ok, result)

        executor = ThreadPoolExecutor(
            max_workers=min(_MAX_PARALLEL_EXPERTS, len(experts)),
            thread_name_prefix="chat-expert")
        try:
            pending = [executor.submit(run_one, tc) for tc in experts]
            # 中止门（2026-10-09 引入，2026-10-12 修正）：裸 as_completed 会一直等到
            # 所有专家子轮自然收尾——长工具（浏览器/MCP ≤60s/大扫描）能拖很久，主控
            # 线程卡在这里无法回到步边界判中止。改为限时等待 + 轮询中止，置位即跳出。
            pending_set = set(pending)
            while pending_set:
                if self._aborted():
                    break
                done, pending_set = wait(pending_set, timeout=0.3)
                for future in done:
                    call_id, result = future.result()
                    results[call_id] = result
                if first_return and done:
                    # 先返先触发：首批结果到手即止。timeout=0 非阻塞再捞一轮
                    # （窗口内刚好收尾的一并交回，减少主控空跑 round-trip），
                    # 剩下的才标记断候转后台。
                    done2, pending_set = wait(pending_set, timeout=0)
                    for future in done2:
                        call_id, result = future.result()
                        results[call_id] = result
                    break
            if first_return and pending_set and not self._aborted():
                # 断候登记必须在「再捞一轮」之后：之后完成的探针才走
                # overflow 路径；窗口内已完成的不标记、不双落盘。
                with self._probe_lock:
                    self._probe_abandoned.update(
                        tc.id for tc in experts if tc.id not in results)
        finally:
            # **不阻塞**：wait=False 让主线程立刻返回（关键修复——旧的 with 退出走
            # shutdown(wait=True)，中止时仍等所有专家收尾）。已提交的 future 仍会跑完
            # （worker 处理完队列剩余项再退出），其工具结果由 run_one 自行落库。
            executor.shutdown(wait=False)
        pending_ids = [tc.id for tc in experts if tc.id not in results]
        return results, pending_ids

    def _drain_probe_overflow(self, messages: list[dict[str, Any]],
                              skip_ids: set[str] | None = None) -> None:
        """把后台完成的探针结果并入**下一轮** LLM 上下文（先返先触发补达）。

        仅激进主控产生 overflow（见 `_parallel_expert_dispatch`）。DB 侧真实
        tool 行已由 worker 落库（同 tool_use_id 续写），这里只做**内存注入**：
        `_inject_nudge` 并入尾部 user 消息——连续两条 user 会被网关 400 拒收，
        绝不能新起一条 user 消息。`skip_ids`：本批已作为正式 tool_result 交回
        模型的调用（时间窗内收尾的双落盘），避免同一结果进两遍上下文。
        """
        if not self._probe_overflow:
            return
        skip = skip_ids or set()
        with self._probe_lock:
            items = [it for it in self._probe_overflow if it["id"] not in skip]
            self._probe_overflow = [it for it in self._probe_overflow
                                    if it["id"] in skip]
        if not items:
            return
        lines = ["[后台并发结果] 以下并发专家已在后台跑完，请按先返先触发规则"
                 "处置（[命中]→立即派深挖专家；[未命中]→放弃该方向）："]
        for it in items:
            body = it.get("result") or ""
            if len(body) > _PROBE_OVERFLOW_HEAD:
                body = (body[:_PROBE_OVERFLOW_HEAD]
                        + f"\n…（截断；全文见该专家的工具结果 {it['id']}）")
            lines.append(f"◆ 专家 {it.get('expert') or '?'}"
                         f"（调用 {str(it.get('id') or '')[-8:]}）：\n{body}")
        _inject_nudge(messages, "\n".join(lines))

    def _dispatch_mcp(self, name: str, args: dict) -> str:
        if self.mcp_bridge is None:
            return "[错误] MCP 未启用：无可用 server"
        parts = name.split("__", 2)
        if len(parts) != 3:
            return f"[错误] 非法 MCP 工具名: {name}"
        if not self.is_orchestrator:
            try:
                allow = load_expert(self.packs_root, self.agent_id, self.track).get("mcp_servers")
                if isinstance(allow, list) and parts[1] not in {str(x).strip() for x in allow}:
                    return f"[错误] MCP server {parts[1]} 未被该专家勾选启动"
            except Exception:  # noqa: BLE001
                log.exception("chat MCP 专家白名单校验失败 thread=%s", self.thread_id)
        return self.mcp_bridge.call(
            parts[1], parts[2], args, session_id=self.thread_id)

    def _tool_todo_write(self, args: dict) -> str:
        items = args.get("items")
        if not isinstance(items, list):
            return "[错误] todo_write 失败：items 必须是数组"
        todo = []
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not str(it.get("title", "")).strip():
                continue
            status = str(it.get("status") or "pending")
            if status not in chat_store.TODO_STATUSES:
                status = "pending"
            todo.append({"id": f"todo-{i + 1}",
                         "title": str(it["title"]).strip()[:200],
                         "status": status})
        chat_store.update_thread(self.bb, self.thread_id, todo=todo)
        done = sum(1 for t in todo if t["status"] == "completed")
        self._emit("chat.todo", {"todo": todo})
        return f"待办已更新：{len(todo)} 项（{done} 完成）"

    def _tool_call_expert(self, args: dict) -> str:
        expert = str(args.get("expert") or "").strip()
        task = str(args.get("task") or "").strip()
        if not expert or not task:
            return "[错误] call_expert 失败：expert 与 task 必填"
        if not self.is_orchestrator or self._is_spawned_subagent:
            return "[错误] call_expert 失败：只有主控能委派（spawn 深度=1）"
        if expert in CONTROLLER_IDS:
            return ("[错误] call_expert 失败：主控类智能体不可被委派"
                    "（spawn 深度=1）；请派专家池专家")
        if self.is_aggressive and expert != "worker":
            return "[错误] call_expert 失败：激进主控只能委派固定子代理 worker"
        if not self.is_aggressive and expert not in self._delegatable_experts():
            available = ", ".join(self._delegatable_experts())
            return (f"[错误] call_expert 失败：未知专家 '{expert}'；"
                    f"可用：{available}（这些是专家 id，不是技能名）")
        if self.llm is None:
            return "[错误] call_expert 失败：LLM 未装配"
        # 分配资产（asset_ids，2026-10-09 严格单一派发）：主控把该子专家负责的
        # 资产显式划给它——子专家只测这些资产、按测试维度收敛；未分配（查询/登记
        # 类任务）不强制面收敛。id 必须存在且属于本项目（宁严勿松）。
        raw_assets = args.get("asset_ids")
        if raw_assets in (None, "", []):
            assigned: list[str] = []
        elif isinstance(raw_assets, list):
            assigned = []
            for a in raw_assets:
                aid = str(a or "").strip()
                if not aid:
                    continue
                if aid not in assigned:
                    assigned.append(aid)
        else:
            return "[错误] call_expert 失败：asset_ids 必须是资产 id 数组"
        # CTF 的附件与题目文件由项目工作区直接提供，允许主控把路径/文件名
        # 作为任务引用，不要求先伪造一条黑板 asset；worker 的执行边界仍由
        # Docker 项目工作区和 threat_class/runtime 闸门承载。非 CTF 轨继续
        # 强制使用项目内黑板资产 id，避免改变普通渗透任务的派发纪律。
        if self.track != "ctf":
            for aid in assigned:
                if self.bb.get_asset(aid) is None:
                    return (f"[错误] call_expert 失败：分配资产不存在或不属于本项目 "
                            f"{aid}（先 bb_add_asset 落资产，再按 id 分配）")
        # spawn 持久子线程（留档），隔离上下文跑完 → 摘要回传。
        # **构造也必须包在 try 内**（2026-10-03）：子 ChatTurn 构造会读专家 yaml /
        # 装工具面，构造抛错（如 packs 缺文件）此前直接穿出把主控整轮炸掉；现
        # 回文本，主控可改派或向人类说明。
        sub_id = ""
        artex_intent_id = ""
        try:
            artex_intent = create_artex_intent(
                self.bb, self.project_id, task, task_id=self.thread_id,
                expert=expert, asset_ids=assigned,
                constraints={"track": self.track or "", "controller": self.agent_id})
            artex_intent_id = artex_intent.get("id", "")
            claim_artex_intent(self.bb, self.project_id, artex_intent_id,
                               f"chat-{self.thread_id[-12:]}")
            transition_artex_intent(self.bb, self.project_id, artex_intent_id, "running")
            worker_agent = "worker" if self.is_aggressive else expert
            sub = chat_store.create_thread(
                self.bb, self.project_id, worker_agent,
                title=task[:40], parent_thread_id=self.thread_id,
                spawned_task=task[:500],
                # 联网搜索继承（v43，web-search-toggle）：主控开着「联网」时
                # 子代理默认也启用——子线程落同一开关位，其 ChatTurn 工具面/
                # 白名单据此双边放行；主控关着则子代理也不拿该工具。
                web_search=self._web_search_on())
            sub_id = sub["id"]
            self._emit("chat.spawn", {"thread_id": sub_id, "expert": worker_agent,
                                      "task_head": task[:200],
                                      "asset_ids": assigned})
            sub_turn = ChatTurn(
                bb=self.bb, llm=self.llm, project_id=self.project_id,
                thread_id=sub_id, packs_root=self.packs_root,
                track=self.track, capabilities=self.capabilities,
                mcp_bridge=self.mcp_bridge, expert_names=[],
                abort_event=self.abort_event,  # 停主控连带停执行中的子专家轮
                owner_tags=self.owner_tags,
                rule_profiles=self.rule_profiles,
                artifacts_dir=self.artifacts_dir,
                browser_pool=self.browser_pool,
                decompiler_factory=self.decompiler_factory,
                containers=self.containers,
                # 激进主控的子代理 = 快速子任务（probe_mode）：不声明意图、
                # 不查黑板（用户 2026-10-17 要求）。chat-orchestrator 的委派
                # 不置位——蛙式排队的深耕子专家仍按完整纪律跑。
                probe_mode=self.is_aggressive,
                assigned_asset_ids=[] if self.is_aggressive else assigned,
                # 子专家共享同一项目级恢复器：去重键在实例内，跨线程共享才能
                # 避免「多个 worker 同时给同一故障装一遍」。
                recovery=self.recovery)
            summary = sub_turn.run(task)
        except Exception as e:  # noqa: BLE001 —— 专家失败回文本，主控可改派
            log.exception("chat 子专家线程失败 expert=%s thread=%s",
                          expert, self.thread_id)
            if artex_intent_id:
                try:
                    transition_artex_intent(self.bb, self.project_id, artex_intent_id,
                                            "failed", error=str(e))
                except Exception:
                    log.exception("ARTEX Intent 失败回写失败 id=%s", artex_intent_id)
            return (f"[错误] call_expert 专家线程 {expert} 执行失败：{e}"
                    + (f"（线程 {sub_id} 已留档）" if sub_id else ""))
        if artex_intent_id:
            try:
                transition_artex_intent(self.bb, self.project_id, artex_intent_id,
                                        "completed", result=summary[:12000])
            except Exception:
                log.exception("ARTEX Intent 完成回写失败 id=%s", artex_intent_id)
        return (f"专家线程 {sub_id}（{expert}）已完成。\n"
                f"收尾摘要：\n{summary[:4000]}\n"
                f"（人类可在工作台打开该线程查看全过程与追问）")

    # ---------- 事件 ----------

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self.bb.append_event(
                self.project_id, kind,
                {"thread_id": self.thread_id, "agent_id": self.agent_id,
                 **({"intent_id": self.artex_intent_id} if self.artex_intent_id else {}),
                 **({"task_id": self.artex_task_id} if self.artex_task_id else {}),
                 **payload},
                session_id=None, author=f"chat-{self.thread_id[-12:]}")
        except Exception:  # noqa: BLE001 —— 事件失败不阻断对话轮
            log.exception("chat 事件落库失败 kind=%s thread=%s",
                          kind, self.thread_id)


def new_thread_id() -> str:
    return f"chat-{uuid.uuid4().hex[:16]}"
