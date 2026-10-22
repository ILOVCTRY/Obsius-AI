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
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from core.agent.tool_registry import (_CONTROL_TOOLS, _PLAN_TOOLS,
                                      bb_query_schema_for,
                                      bb_query_schema_for_artex)
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
from core.ctf.session import key_of as ctf_key_of
from core.diagnostics.failog import record_mcp_failure, record_tool_failure
from core.diagnostics.system_prompt import save_system_prompt
from core.dimensions import (
    dimension_coverage,
    dimensions_for_type,
    load_track_dimensions,
)
from core.llm.provider import (ContextOverflowError, LLMError, TransientStreamError,
                               is_reasoning_echo, tool_result_content)
from core.llm.pseudo_toolcall import (PSEUDO_TC_FINAL_TEXT, PSEUDO_TC_MAX,
                                      PSEUDO_TC_NUDGE, has_marker_prefix_tail,
                                      has_pseudo_tool_call,
                                      strip_pseudo_tool_calls)
from core.runtime.gateway import ExecutionGateway
from core.runtime.recovery import EnvironmentUnavailable
from core.skills import kspec
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
# CTF 解题主控（ctf-controller，2026-10-19）：第三个控制器类智能体，**仅 CTF 轨**。
# 与另外两个的根本区别是**决定权归属**：
#
# - chat-orchestrator：LLM 决定派谁、派几批、何时收工（蛙式排队）；
# - aggressive-controller：LLM 决定派谁，代码只管「先返先触发」的并发语义；
# - artEX-controller：LLM 只操舵，派发权全在项目 Engine（Planner 生成 Intent）；
# - **ctf-controller：方向由 LLM 提，但「准不准派、派几轮、何时放弃」由代码判**。
#
# 为什么 CTF 需要这个：CTF 的失败模式不是「不会解」，而是「时间分配错了」——
# 死磕一道已探尽的题到比赛结束，或一次快检没命中就放弃一道再挖十分钟就出的题。
# 把这两个判断交给模型 = 把决定比赛得分的判断交给最不擅长计时的组件。代码侧
# 判据见 `core/ctf/scheduler.py`（13 条可解释放弃判据），方向互斥见
# `core/ctf/lease.py`，执行自证见 `core/ctf/attest.py`，上下文降级见
# `core/ctf/fold.py`，四者由 `core/ctf/session.py` 合流。
CTF_CONTROLLER_ID = "ctf-controller"
CONTROLLER_IDS = frozenset({ORCHESTRATOR_ID, AGGRESSIVE_CONTROLLER_ID,
                            ARTEX_CONTROLLER_ID, CTF_CONTROLLER_ID})

# 子专家不可用的控制原语（任务队列/计划闸/收尾/私信/HITL——
# 对话线程没有任务上下文与收件箱，这些工具既无语义也危险：publish/claim
# 会污染任务队列）
# 意图三件套（declare/close/reopen_intent）自 2026-10-01 起**开放给对话链**
# （见 intent-tools-chat）：对话主控/子专家同样要按「声明假设→执行→收尾」走，
# 否则对话产出的发现不落链路图（用户实测：任务跑完但在「黑板→发现→链路」看不到
# 任何意图——对话链整条链路从未声明过意图）。
# **propose_pack_edit 自 2026-10-14（kb-iteration）起开放给对话链**；
# **2026-10-21 收窄为「只有主控」**：子代理（call_expert 派生）不再持有。
# 原因不是子代理没证据（它恰恰有一手证据），而是它只看得到自己那一段——
# 不知道别的方向试过什么、也不知道主控怎么收敛，提上来的常是「我这个方向
# 该写进手册」而全局看并不值得。提案权收归主控（它看得到全部子线程摘要），
# 子代理的贡献路径是写进收尾摘要、由主控 ask_expert 追问后统一提。
# 三处同口径：工具面（`_tool_specs`）、执行白名单（`_build_dispatcher`）、
# 系统提示（`_system_prompt` 的经验沉淀段）——外加执行层身份硬闸
# （`ToolDispatcher._is_spawned_subagent`）。详见 _ORCH_BASE_TOOLS。
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

# 续跑注入（ask-expert，2026-10-21 Nan 要求）：主控可以给**已派发过**的专家
# 线程补一句话让它接着跑。
#
# 为什么需要它：`call_expert` 是「一次性派发」——专家跑完就 idle，主控只剩
# 它那句摘要。摘要不够用时主控只有两条烂路：① 重新 `call_expert` 派一个新
# 专家（丢掉了原来那一轮的上下文，等于从头再试一遍）；② 自己上手干（主控
# 不实操是红线）。有了本工具，主控可以回到**原来那条线程**追问细节，或让
# 那个已经摸熟了现场的专家去准备变更提案。
#
# 与 `call_expert` 的边界（两者不可互相替代）：
# - `call_expert`：新任务、新上下文、新线程；产出是摘要。
# - `ask_expert`：同一任务、沿用原上下文；产出是追问答案或后续动作。
ASK_EXPERT_TOOL = "ask_expert"

# 走 `_dispatch_inner` **早拦截分支**的工具：它们的 schema 由
# `_orch_tool_specs` 单独挂（带动态枚举/说明），**不能**再经
# `_specs_by_names` 重复下发，执行白名单也要把它们剔掉（否则同一工具
# 会有两条注册路径）。新增早拦截工具必须同时加进这里——
# `_build_dispatcher` 与 `_tool_specs` 三处过滤共用本常量，避免漏改。
_EARLY_ROUTE_TOOLS = ("todo_write", "call_expert", ASK_EXPERT_TOOL)

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
    "run_cmd", "read_file", "view_image", "search_files", "unpack_attachment",
    "bb_add_asset", "bb_asset_status",
    "bb_add_finding", "bb_update_finding", "bb_add_artifact",
    "ctf_submit_flag", "ctf_ammo", "ctf_abandon", "ctf_conclude",
    "kb_search", "kb_open", "route_lookup",
    "browser_navigate", "browser_click", "browser_type",
    "browser_screenshot", "browser_content", "browser_back", "browser_replay",
    "decompile", "disasm", "list_symbols", "strings_search", "func_xrefs",
    "bb_upsert_func",
)
_AGGRESSIVE_CONTROLLER_TOOLS = ("run_cmd", "read_file", "search_files")

# CTF flag 通道（ctf-flag-channel，2026-10-19 修 P0 撕裂）：CTF 轨上「拿到 flag
# 怎么报」的唯一出口是 ``ctf_submit_flag``（判定+入台账），候选线索走
# ``ctf_ammo``（生成候选字典）。这两个是**轨道语义**不是专家能力——和
# record_fact / trace 三件套同一定位，因此**不写进任何专家 yaml**，而是在
# 工具面装配时按「本线程是否归属一道题」动态追加。
#
# 为什么必须动态追加（真事故）：``_CTF_CORE_TOOLS`` 里虽然列了它们，但那条
# 分支只在 ``self.is_worker`` 为真时命中；而 ``is_worker`` 判的是
# ``agent_id == "worker"``，全仓**没有任何地方建 agent_id="worker" 的线程**
# ——ARTEX Worker 走的是 ``agent_id=<专家 id>``（artex_worker.py:89 传
# expert 名），于是它恒 False，实际走专家 yaml 白名单。而所有 CTF 专家 yaml
# 都没声明这两个工具 → 提示词反复要求「一律用 ctf_submit_flag 登记」，模型
# 一调就被「越界拒绝」，**flag 台账在真实 ARTEX 执行链上完全不可达**。
# 测试没抓到是因为测试直接 new ToolDispatcher，绕过了 ChatTurn 的工具面裁剪。
_CTF_FLAG_TOOLS = ("ctf_submit_flag", "ctf_ammo")

# CTF「整题放弃」出口（ctf-abandon，2026-10-20）：与 flag 通道同一定位——
# **轨道语义不是专家能力**，故同样按轨动态追加、不写进任何专家 yaml。
#
# 此前 `set_abandoned` 只挂在 HTTP API（`author="human"`），Agent 侧**没有
# 任何工具能表达「这题我不解了」**。于是模型想收尾时唯一可见的出口是
# `bb_asset_status("tested_clean")`——而 CTF 轨终态判据是 flag 台账，那个
# 状态在 CTF 轨不对应任何东西，必然撞两道门禁（题目行「父节点由子树派生」、
# 附件子资产「须死路背书」），再为凑 dead_end 去查 event id，白烧三四轮。
# 补上这个出口，「合法地停」才有地方去。
#
# 只给**执行侧**（Worker / 子专家 / 激进主控）：放弃是解题过程中的判断，
# 纯委派主控（chat-orchestrator）与 ARTEX 主控只操舵、不判定题目死活。
_CTF_ABANDON_TOOLS = ("ctf_abandon",)

# CTF「主动收尾」出口（ctf-conclude，2026-10-20，方案 A 配套）：与 abandon 同
# 定位——轨道语义不是专家能力，按轨动态追加、不写进任何专家 yaml。
#
# **为什么必须有这个出口**：`pending` 不计 solved 之后，AI 判定通过只能把题目
# 停在 partial（等人工确认）。有人值守时正确，但**无人值守通宵跑题会全卡在
# pending 白烧预算**——明明解出来了、没人点按钮，调度层就一直派 Worker 重跑。
# 于是给一个显式收尾动作：`meta.concluded` → 停派工，但状态仍是 partial，
# **不伪装成 solved**（「这条 flag 对不对」只有人能判）。
#
# 发放面比 abandon 宽一点：**纯委派主控（chat-orchestrator）也给**——它虽然
# 不判定题目死活，但它掌握全局（知道哪几道题已经有候选、哪几道还在找），
# 恰恰是最该说「这题别再派了」的角色。ARTEX 主控不给（它连登记都不做）。
_CTF_CONCLUDE_TOOLS = ("ctf_conclude",)

# 需要**执行能力配合**的 flag 工具（artex-controller-no-exec，2026-10-19）：
# ``ctf_ammo`` 只产候选表，真正消费它要 ``run_cmd``（喂 hydra/ffuf）。给一个
# 没有 ``run_cmd`` 的角色发 ``ctf_ammo`` = 教它调一个半截工具。所以它只发给
# **执行侧**（Worker / 子专家 / 激进主控 / 探针），不发给纯委派主控。
#
# ``ctf_submit_flag`` 则相反：它是**登记动作**，纯委派主控也该有（人工可能直接
# 口述「flag 是 flag{xxx}，登记一下」）。ARTEX 主控两者都不给——它连登记都不做，
# 只观察台账（`artex_status` 的题目进度段）。
_CTF_FLAG_EXEC_TOOLS = ("ctf_ammo",)

# CTF 解题主控（ctf-controller）的工具面：**代码调度 + LLM 只定方向**。
#
# 它**不拿任何执行工具**（没有 run_cmd / read_file / browser_* / decompile）——
# 与 ARTEX 主控同理：主控一旦能自己跑命令就会直接解题，绕过整套调度。
# 但它比 ARTEX 主控多两样东西，因为它的角色是**决定往哪走**而不是纯操舵：
#
# - `ctf_dispatch`：把「我想派这些方向」提交给代码判据，拿回「准派/拦下+理由」。
#   这是**唯一**的派发入口——`call_expert` 的每条派发都要过 `allowed()` 校验，
#   越权（不在本轮计划内）直接被拒并给出原因。
# - `ctf_review`：看代码侧的 verdict 表（轮次/已得/放弃判据），据此决定下一批
#   方向。**放弃判定不可推翻**：判据命中就是 drop，模型换措辞重派会被识别成
#   同一方向键并拒收。
#
# `ctf_submit_flag` 给（人工可能直接口述「flag 是 flag{xxx}，登记一下」）；
# `ctf_ammo` 不给（要 run_cmd 配合，主控没有）。
_CTF_CONTROLLER_TOOLS = (
    "bb_query", "kb_search", "kb_open", "route_lookup",
    "bb_declare_challenge", "ctf_submit_flag", "ctf_conclude",
    "todo_write", "call_expert", ASK_EXPERT_TOOL,
    "ctf_dispatch", "ctf_review", "ctf_inspect",
    *_INTENT_TOOLS,
)

_CTF_DISPATCH_TOOL_SPEC = {
    "name": "ctf_dispatch",
    "description": (
        "【唯一的派发入口】把你认为该做的方向提交给代码调度器，拿回「准派 / 拦下」"
        "计划。\n"
        "代码会做四件事（你无权推翻）：\n"
        "① **方向归一互斥**——同一方向换个说法不算新方向，会被合并拒收；\n"
        "② **放弃判据**——已判死的方向直接拦下（13 条可解释判据，见 ctf_review）；\n"
        "③ **轮次记账**——每派一次记一轮，超过上限自动放弃；\n"
        "④ **执行自证**——worker 上轮声称拿到执行时，本轮任务里会自动附加"
        "「让目标机自证」的题目。\n"
        "拿到计划后，**同一轮**用多个 call_expert 并发派出准派的方向（task 直接用"
        "返回里的 task 文本，不要自己改写——里面有代码拼的上下文与自证段）。"),
    "input_schema": {
        "type": "object",
        "properties": {
            "directions": {
                "type": "array",
                "description": "你要派的方向（每个只覆盖一个测试面）",
                "items": {
                    "type": "object",
                    "properties": {
                        "subgoal": {"type": "string",
                                    "description": "方向描述（一句话，说清查什么）"},
                        "expert": {"type": "string",
                                   "description": "派给哪个专家（默认 worker）"},
                        "need": {"type": "integer",
                                 "description": "该方向需要几个 flag（多 flag 题才填）"},
                    },
                    "required": ["subgoal"],
                },
            },
            "breadth_starved": {
                "type": "boolean",
                "description": "是否还有「没开过的题」在等槽位（机会成本判据用；"
                               "为真时零产出方向会更早让位）",
            },
        },
        "required": ["directions"],
    },
}

_CTF_REVIEW_TOOL_SPEC = {
    "name": "ctf_review",
    "description": (
        "看代码侧的复核视图：每个方向的轮次、已得/需要的 flag、状态、以及"
        "**代码给出的下一步判定与理由**（revive 续投 / requeue 让位 / drop 放弃）。\n"
        "同时给出方向互斥表（谁在做哪个方向、还剩多久）。\n"
        "**判为 drop 的方向不要再派**——换措辞重派会被识别成同一方向键并拒收。"
        "判为 requeue 表示「还有救但先让别人上」，稍后可以再来。"),
    "input_schema": {"type": "object", "properties": {}},
}

_CTF_INSPECT_TOOL_SPEC = {
    "name": "ctf_inspect",
    "description": (
        "【只读核对通道】你现在手里只有 worker 的**自述摘要**。判 flag 真假、"
        "判文件形态，都需要一手依据——用这个工具拿。\n"
        "- `what=\"evidence\"`：把台账里**每条 flag 的完整证据链原文**摊开"
        "（解题路径原文、transform、置信、判定者、关联 finding 的复现步骤）。"
        "不摘要、不评价，你自己比。\n"
        "- `what=\"files\"`：题目子树下的文件清单 + 来源标注（题目原件/上轮产物/"
        "样本/未知）+ 每个文件的**头部 32 字节魔数**。\n"
        "**它只能核对，不能执行**：没有任意路径读取、不返回文件全文、不跑命令。"
        "要读全文或跑脚本，**派 worker**。\n"
        "典型用法：worker 说「拿到了 flag」→ 先 evidence 看它的解题路径原文是否"
        "真能推出那个串；worker 说「这是 pcapng」→ 用 files 看魔数对不对。"),
    "input_schema": {
        "type": "object",
        "properties": {
            "what": {
                "type": "string",
                "enum": ["evidence", "files"],
                "description": "evidence=flag 证据链原文（判真假）；"
                               "files=文件清单+来源+魔数（判形态）",
            },
            "challenge_id": {
                "type": "string",
                "description": "题目资产 id（默认取本线程绑定的题目）",
            },
        },
        "required": ["what"],
    },
}

_PROBE_EXCLUDED_TOOLS = ("bb_query", *_NO_SKILL_OR_INTENT_TOOLS)

_ORCH_BASE_TOOLS = ("todo_write", "call_expert", ASK_EXPERT_TOOL, "skill_open",
                    "kb_search", "kb_open", "bb_query",
                    "bb_delete_asset", "bb_delete_assets", "bb_merge_assets",
                    "bb_declare_challenge",
                    # 经验沉淀提案（kb-iteration，2026-10-14）：主控可提 kb/技能变更提案
                    # （只落 pending，人审）；每会话上限 3 条（ToolDispatcher 内）。
                    "propose_pack_edit",
                    *_INTENT_TOOLS)

# ARTEX 主控工具面（artex-controller-slim，2026-10-20）：**只留「观察 + 操舵 + 登记」**。
#
# 起因：19 个工具的 schema 共 16201 字符（约 8100 token），单轮光工具定义就吃掉
# 小半上下文。实测该主控线程（hxbai 项目）的全部调用分布是：
#
#     bb_query 95 · artex_status 88 · artex_hint 7 · artex_plan 5 · artex_fact 5
#     bb_declare_challenge 1 · artex_control 1
#
# 其余 11 个工具**一次都没调用过**，且职责上与主控冲突：
#
#   todo_write / skill_open / kb_search / kb_open
#       —— 主控不干活。`artex_plan` 自带 `todos` 参数（规划即记待办）；
#          查打法、读技能手册是 Worker 的事（Worker 工具面里有这两个）。
#          给主控只会诱导它「先自己查一查」，即越权的前奏。
#   declare_intent / close_intent / reopen_intent / bb_delete_intent
#       —— 意图链是 Worker 的执行纪律；主控是**调度者**，它自己的假设由
#          Planner 拆成 Intent。给主控这套工具等于允许它绕过 Planner 自建链路。
#   bb_delete_asset / bb_delete_assets / bb_merge_assets
#       —— 资产维护是人工/Worker 的活。主控删资产会直接破坏覆盖度分母
#          （任务范围锚点），而它自己无法判断该不该删。
#
# 保留 bb_query 是因为它承担唯一的「看执行事件」通道（93/95 次是 what=events）；
# 保留 bb_declare_challenge 是 CTF 轨登记题目的入口。
_ARTEX_CONTROLLER_TOOLS = ("bb_query", "bb_declare_challenge")

# ARTEX 主控的**执行侧硬闸**（artex-controller-slim，2026-10-20 → 改造于
# controller-tool-select）：撤下 schema 还不够——执行侧有三条旁路能把它们放进来，
# schema 撤了也挡不住（模型从历史里记得工具名就会照调，主控历史里必然出现过
# todo_write）：
#
#   ① `_dispatch` 的早拦截分支（todo_write / call_expert）**根本不经**
#      ToolDispatcher，白名单压根没机会判；
#   ② `ToolDispatcher._dispatch_once`（tools.py:572）对 `_PLAN_TOOLS`
#      （declare_intent）与 `_CONTROL_TOOLS`（close_intent / reopen_intent /
#      finish / request_steps）**恒放行**——§6.6 的收尾原语语义，防止模型被
#      「先立意」死锁；代价是任何角色的白名单都裁不掉它们；
#   ③ 收尾阶段 `filter_wrapup_whitelist` 是「白名单优先 + 其余放行」，
#      todo_write 还在 `_WRAPUP_KEEP_TOOLS` 里。
#
# 实测（主控线程带完整参数直调）：`todo_write` 返回 `ok=True | 待办已更新：1 项`
# ——真的把线程待办覆写了；`run_cmd` 则正确回「越界拒绝」。
#
# **闸的判据是「yaml 勾选面」而不是写死的黑名单**：用户要的是「勾选启动」，
# 勾了就该能调、没勾就该被拒。写死黑名单会把勾选变成装饰。
#
# 但有一类工具是**结构性**不该给某个角色的（不是「没勾」，是「勾了也不对」），
# 它们必须**先于**勾选面判据被拒，否则会被下面这条恒放行地板救回来：
#
#   计划/控制原语（`_PLAN_TOOLS` ∪ `_CONTROL_TOOLS`）**恒放行**，不进勾选面
#   ——它们是「收尾/立意」的兜底出口，裁掉会死锁成「必须先声明一条假意图」，
#   与 ToolDispatcher 的恒放行同口径。代价是任何角色的白名单都裁不掉它们，
#   于是 ARTEX 主控的意图链禁令会被这条地板抵消（实测：`declare_intent` 从
#   「被拒」变成「放行」）。故把结构性禁令提到勾选面判据**之前**。
#
# `controller_structural_denied()` 是这份清单的唯一出处，设置页候选集也从
# 它扣——界面不该显示一个勾了也不生效的工具（那是另一种「界面在骗人」）。
_ARTEX_CONTROLLER_DENIED = frozenset({
    # 早拦截分支（不经过白名单）：ARTEX 主控派活只能走 artex_plan。
    # call_expert 是实测事故（artex-plan-only，2026-10-19）——主控直接委派
    # 一个专家会绕过 Planner，于是目标、探索图、任务范围全空，那条 Intent
    # 的 task_id 还是线程 id 拼出来的假任务。todo_write 同理：待办由
    # artex_plan 的 todos 参数承载，单独维护会与规划脱节。
    "todo_write", "call_expert", ASK_EXPERT_TOOL,
    # 意图链：主控是调度者，它自己的假设由 Planner 拆成 Intent，
    # 给它这套工具等于允许它绕过 Planner 自建链路。
    "declare_intent", "close_intent", "reopen_intent", "bb_delete_intent",
    # 控制原语：对话链没有任务队列语义（finish 会置位 finished、
    # request_steps 改步数预算）；主控靠 artex_status 判态，不靠 finish 收尾。
    "finish", "request_steps",
})


def controller_structural_denied(agent_id: str) -> frozenset[str]:
    """该控制器**结构性**不可用的工具（勾了也不下发/不执行）。

    只有 ARTEX 主控有这一档：它的角色是纯操舵（Planner 独占 Intent 生成），
    意图链/待办/委派都是「绕过 Planner 自建链路」。其余控制器为空——
    它们的勾选面就是最终裁决。
    """
    if str(agent_id or "") == ARTEX_CONTROLLER_ID:
        return _ARTEX_CONTROLLER_DENIED
    return frozenset()

# ARTEX 主控的操舵六件套（artex-fusion / artex-goal / artex-fact）。
# 它们**不在** `_ARTEX_CONTROLLER_TOOLS` 里：那六件套走
# `artex_steering_dispatch` 直连分支（不经 ToolDispatcher，无 REGISTRY 条目），
# 由 `_orch_tool_specs` → `_artex_tool_specs` 在运行时追加 schema。
# 这里单独列出是为了让 `controller_tool_face`（默认勾选面）与
# `controller_tool_candidates`（可勾选范围）都能覆盖它们——
# 否则界面只显示 2 个，实际能调 8 个。**改 `_artex_tool_specs` 必须同步改这里。**
_ARTEX_STEERING_TOOLS = (
    "artex_plan", "artex_status", "artex_hint", "artex_control",
    "artex_goal", "artex_fact",
)


def controller_tool_face(agent_id: str,
                         track: str | None = None) -> list[str] | None:
    """控制器类智能体的**代码默认工具面**（yaml `tools` 未设置时的回落值）。

    背景（controller-tool-face 事故，2026-10-20）：控制器（chat-orchestrator /
    aggressive-controller / artex-controller / ctf-controller）的工具面历史上
    由**代码常量**决定，**完全不读**专家 yaml 的 `tools` 字段（那个字段只对
    子专家生效，见 `expert_tool_names`）。于是设置页「工具白名单」对控制器
    显示的是 `tools: null` → 「全部（未限制）」，与运行期真实工具面完全脱节：
    用户以为主控能调 54 个工具，实际只有 8 个。

    **当前语义（用户两次纠错后）**：设置页是**可勾选启动**的开关，不是只读
    展示。yaml 的 `tools` 现在是控制器的**真实开关**（`ChatTurn.
    _controller_tool_face()` 优先读它）；本函数只提供「未勾选过时用什么」的
    默认值，同时作为设置页的**回填初值**（否则界面又是 null → 全选）。

    与 `controller_tool_candidates()` 的分工：那个是「可勾选范围」（更大），
    这个是「默认勾选」。**候选必须 ⊇ 本函数**，否则界面勾不到默认没开的工具，
    又变回固定了。

    **清单必须含动态挂载的工具**：ARTEX 主控的 `artex_*` 六件套不在
    `_ARTEX_CONTROLLER_TOOLS` 里，由 `_orch_tool_specs` 在运行时追加
    （它们不经 ToolDispatcher，走 `artex_steering_dispatch` 直连分支）。
    只列 `_ARTEX_CONTROLLER_TOOLS` 会显示 2 个，而运行期实际 8 个。

    `track`：CTF 轨会给纯委派主控（chat-orchestrator）额外追加 flag 通道
    （`ctf_submit_flag` / `ctf_conclude`）。这是**轨道语义**不是专家能力，
    原本由 `_tool_specs` 的按轨追加块负责；控制器改为「勾选面即最终裁决」后
    那段整段跳过，故默认值必须自己含上，否则 CTF 轨的纯委派主控会突然失去
    登记 flag 的入口。传 None = 取并集（设置页回填用，避免按单轨回填后
    保存把另一条轨的工具清掉）。

    返回 None = 该 agent 不是控制器（调用方应回落 yaml 的 tools 字段）。

    **`propose_pack_edit` 不在任何默认清单里**：它受项目级 kb 迭代开关实时
    门禁（`_kb_iteration_on`），开关关就不下发。静态清单无法表达「看开关」，
    写进默认会让界面显示一个此刻调不动的工具——那正是本函数要消灭的脱节。
    （它在**候选**清单里，用户可显式勾选。）
    """
    aid = str(agent_id or "")
    if aid == ARTEX_CONTROLLER_ID:
        # 与 `_tool_specs` 的 is_artex 分支同源：base ∪ _orch_tool_specs
        # 追加的 artex_* 六件套（`_artex_tool_specs` 的 key 集合）。
        return [*_ARTEX_CONTROLLER_TOOLS, *_ARTEX_STEERING_TOOLS]
    if aid == CTF_CONTROLLER_ID:
        return list(_CTF_CONTROLLER_TOOLS)
    if aid == AGGRESSIVE_CONTROLLER_ID:
        # `_orch_tool_specs` 对激进主控**照给** todo_write + call_expert
        # （只有 ARTEX 主控才撤），且它的 base 走 `_CTF_CORE_TOOLS`；
        # 同时 `is_aggressive` 命中 `_PROBE_EXCLUDED_TOOLS` 剔除面。
        face = [*_CTF_CORE_TOOLS, "bb_declare_challenge",
                "todo_write", "call_expert", ASK_EXPERT_TOOL]
        face = [t for t in face if t not in _PROBE_EXCLUDED_TOOLS]
        return list(dict.fromkeys(face))
    if aid == ORCHESTRATOR_ID:
        face = [t for t in _ORCH_BASE_TOOLS if t != "propose_pack_edit"]
        # 纯委派主控原本靠 `_tool_specs` 的按轨追加拿到 flag 登记/收尾出口
        # （`ctf_ammo` 除外——要 run_cmd 配合，它没有）。控制器改勾选面裁决后
        # 那段跳过，故默认值补上。None = 并集（设置页回填用）。
        if track in (None, "ctf"):
            face = [*face, "ctf_submit_flag", *_CTF_CONCLUDE_TOOLS]
        return list(dict.fromkeys(face))
    return None


_MCP_TOOL_PREFIX = "mcp__"
_MCP_CONFIG_PATH = Path("config/mcp.json")   # 与 core/api/app.py 同口径（cwd=项目根）


def mcp_server_of(name: str) -> str:
    """``mcp__<server>__<tool>`` → server 名；不是 MCP 工具返回空串。"""
    parts = str(name or "").split("__", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "mcp" else ""


def mcp_allowed_by_face(name: str, face: set[str]) -> bool:
    """MCP 工具是否在控制器勾选面内（controller-mcp-face）。

    两种勾选粒度都认：
    - 精确名 ``mcp__rea__compare_functions``（只开这一个工具）；
    - server 通配 ``mcp__rea__*``（开整个 server 的全部工具）。

    通配是必须的：rea 一个 server 就 215 个工具，逐个勾选在设置页里没法用，
    而用户的真实决策本来就是「这个主控要不要用这个 server」。
    """
    if not name:
        return False
    if name in face:
        return True
    srv = mcp_server_of(name)
    return bool(srv) and f"mcp__{srv}__*" in face


def enabled_mcp_servers() -> list[str]:
    """当前**已启用**的 MCP server 名（供设置页列出可勾选的 server 通配）。

    读的是配置文件本身而非 MCPBridge 的实例状态：`controller_tool_candidates`
    没有项目上下文（拿不到 domains），也不该为列候选去起 MCP 子进程。
    """
    try:
        cfg = json.loads(_MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[str] = []
    for entry in cfg.get("servers") or []:
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        name = str(entry.get("name") or "").strip()
        if name:
            out.append(name)
    return out


def _track_filter(names: list[str], track: str) -> list[str]:
    """控制器勾选面的**轨道适用性**过滤（controller-tool-select）。

    yaml 的 `tools` 是跨轨共享的一份清单，但 `ctf_*` 是 CTF 轨语义：用户在
    CTF 项目里勾了 `ctf_submit_flag`，pentest 轨的主控不该因此多出一个
    「提交 flag」的入口（那里没有题目台账，调了只会报错）。

    只做**单向**裁剪（非 CTF 轨剔 `ctf_*`），不反向剔——逆向专属工具（如
    `decompile`）在 CTF 轨也有用（re/pwn 题），反向剔会误伤。
    """
    if track == "ctf":
        return list(dict.fromkeys(names))
    return list(dict.fromkeys(t for t in names if not t.startswith("ctf_")))


def controller_tool_candidates(agent_id: str) -> list[str] | None:
    """控制器在设置页**可勾选的候选工具全集**（controller-tool-select）。

    与 `controller_tool_face` 的区别：那个是「当前实际生效的面」，这个是
    「允许勾选的范围」。用户要的是**勾选启动**（自己决定开哪些），所以候选
    必须**大于等于**当前面——否则界面上勾不到默认没开的工具，等于又变回固定。

    候选构成三块，缺一块就会让某类工具在界面上永远勾不到：

    ① `AGENT_TOOLS` 里未进 `_EXPERT_EXCLUDED` 的通用工具——注意
       `_EXPERT_EXCLUDED` 里有 `bb_declare_challenge`/`finish`/`request_steps`，
       但**控制器本来就在用** `bb_declare_challenge`（CTF 轨登记题目），
       所以控制器的候选**不看** `_EXPERT_EXCLUDED`，直接用全量 `AGENT_TOOLS`。
    ② 无 REGISTRY 条目、不在 `AGENT_TOOLS` 的**控制器专属**工具：
       `todo_write` / `call_expert`（`_dispatch` 早拦截分支）与 `artex_*`
       操舵六件套（`artex_steering_dispatch` 直连分支）。
    ③ CTF 调度三件套 `ctf_dispatch`/`ctf_review`/`ctf_inspect`（同上，直连）。

    **专属工具按角色给，不能全量并集**：`artex_*` 只对 ARTEX 主控有意义
    （`_orch_tool_specs` 的 `is_artex` 分支才挂），`ctf_*` 三件套只对
    CTF 解题主控有意义。全量并集会让 CTF 主控的界面出现 `artex_plan`——
    勾了也不下发，又是一次「界面在骗人」。故按 `controller_tool_face` 的
    角色分支分别给。

    **结构性禁令必须从候选里扣掉**：`controller_structural_denied()` 里的工具
    对该控制器是「勾了也不对」（ARTEX 主控的意图链/待办/委派会绕过 Planner），
    显示出来只会让用户勾了不生效——那是另一种「界面在骗人」。

    非控制器返回 None（调用方回落 yaml 的 `tools`，语义与子专家一致）。
    """
    if controller_tool_face(agent_id) is None:
        return None
    aid = str(agent_id or "")
    denied = controller_structural_denied(agent_id)
    names: list[str] = [s["name"] for s in AGENT_TOOLS]
    # 专属工具按角色给（见 docstring ③ 与「按角色给」段）
    if aid == ARTEX_CONTROLLER_ID:
        names += list(_ARTEX_STEERING_TOOLS)
    elif aid == CTF_CONTROLLER_ID:
        names += ["ctf_dispatch", "ctf_review", "ctf_inspect"]
    # todo_write / call_expert 走 `_dispatch` 早拦截分支，任何控制器都可能
    # 被勾选启用（ARTEX 主控由结构性禁令扣掉，见下）。
    names += ["todo_write", "call_expert", ASK_EXPERT_TOOL]
    # MCP server 按**通配**给（controller-mcp-face）：逐个工具列会把 200+ 项
    # 塞进设置页（rea 一个 server 就 215 个工具、549K 字符 schema），而勾选
    # 粒度本来就该是「这个主控要不要用这个 server」。见 `mcp_allowed_by_face`。
    names += [f"{_MCP_TOOL_PREFIX}{s}__*" for s in enabled_mcp_servers()]
    # `propose_pack_edit` 受项目级 kb 迭代开关实时门禁——开关关着时勾了也
    # 下发不了。仍列进候选（用户可以勾，开项目开关即生效），但界面会提示。
    return list(dict.fromkeys(t for t in names if t not in denied))


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
    "**你没有实操工具**（没有 run_cmd / read_file / search_files，也没有 "
    "ctf_submit_flag / ctf_ammo）。这不是权限缺失，是角色分工：一旦你亲自跑命令、"
    "读文件、解 flag，任务树与探索图就会被绕过，Worker 的并发调度也白搭——"
    "整条 ARTEX 自主链路退化成你一个人单干。**用户看到主控自己解题就是这里出的错**。\n"
    "要执行 → artex_plan 建 Intent；要纠偏 → artex_hint；要停 → artex_control。\n"
    "1. **盘点**：bb_query 看已登记资产与发现；artex_status 看当前任务的 runtime "
    "状态、**注册目标及达成情况**、Intent 计数、活跃 Worker 与探索图里的事实/发现。"
    "已有方向不要重复规划。\n"
    "2. **规划**：首次建任务用 artex_plan(goal=\"…\", mode=\"new\")（Planner 会把目标"
    "拆成可核验的离散目标并登记）；对当前任务用 artex_plan(mode=\"extend\") "
    "让 Planner 依据新事实追加方向，或用 artex_plan(intents=[…]) 直接指定方向。"
    "CTF 特别纪律：needs_review 只表示探索停摆但题目未解；exhausted 表示方向未完成，"
    "都不是任务成功。先 artex_status，之后 retry exhausted Intent 或在原任务 extend；"
    "当前题目未 solved/abandoned 时严禁 mode=\"new\"。只有用户明确要求切换/新建任务时，"
    "才用 mode=\"new\" 并传 new_task_confirmed=true。"
    "Planner 是唯一 Intent 生成者——**你的工具面里没有 call_expert**，派活只有"
    "artex_plan 这一条路。这不是限制，是保证目标分解、范围锚定与探索图三者"
    "不会与执行脱节。\n"
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
    "7. **边界**：只在登记的授权范围内扩展子域、站点和端口，跨出范围立即暂停并请求"
    "确认；漏洞利用、爆破、上传、提权、横向移动、业务写入和 CTF 提交等高风险动作"
    "必须逐项审批，发现线索不等于获得执行授权。\n"
    "8. **证据**：命中结论必须有实际输出、请求响应、文件路径或复现步骤支撑；"
    "没有证据的内容只能标为假设——目标达成同样要有证据，模型自述不算数。")

# CTF 解题主控的工作方式（ctf-controller，2026-10-19）。
#
# 这段与另外两个主控的文案是**互斥**的：它们都强调「你来决定派什么」，本段
# 强调「你只提方向，派不派由代码判」。写清这一点很关键——模型从训练数据里
# 带来的默认行为是「我来统筹一切」，不明说就会绕过 ctf_dispatch 直接
# call_expert，于是整套判据（放弃决策、互斥、自证、轮次记账）全被绕开。
_CTF_CONTROLLER_STYLE = (
    "## 工作方式（CTF 解题主控 · 代码调度 + 你只定方向）\n"
    "你的职责是**判断往哪走**，不是判断「什么时候别走了」——后者由代码判。\n"
    "这是刻意的分工：CTF 的失败模式不是不会解，而是时间分配错了（死磕已探尽的\n"
    "方向到比赛结束，或一次快检没命中就放弃再挖十分钟就出的题）。这两个判断\n"
    "交给代码，因为代码会记账、会计时、不会因为上一轮的沉没成本而不肯放手。\n"
    "\n"
    "### 你没有执行工具\n"
    "没有 run_cmd / read_file / browser_* / decompile。一旦你亲自跑命令、读文件，\n"
    "整套调度（方向互斥、轮次记账、自证核验）就全被绕过——**用户看到主控自己\n"
    "解题就是这里出的错**。要执行 → call_expert 派给专家。\n"
    "\n"
    "### 每轮流程（三步，不许跳）\n"
    "1. **提方向**：`ctf_dispatch(directions=[{subgoal, expert, need}])` 把你认为\n"
    "   该做的方向交给代码。每个方向只覆盖一个测试面（例：robots.txt 备份、\n"
    "   登录表单 SQL 注入、附件隐写、二进制反编译），**不要**把多个面塞进一条。\n"
    "   代码会回你「准派 N 个 / 拦下 M 个 + 理由」。\n"
    "   **第 1 轮必须是分诊**（代码会硬拦）：派一条廉价方向搞清楚这是什么题\n"
    "   （file / 魔数 / linktype / 题面关键句），拿到结论再派深挖。盲派深挖会\n"
    "   各自重写一遍解析器——实测同题 3 份重复脚本，方向还完全重叠。\n"
    "2. **照计划派**：拿返回里 `task` 文本**原样**填进 call_expert（里面有代码拼的\n"
    "   上下文与自证段，改写会丢掉这些）。准派几个就同轮并发派几个。\n"
    "3. **看复核**：`ctf_review()` 看每个方向的轮次、已得 flag、耗时、以及代码的\n"
    "   下一步判定（revive 续投 / requeue 让位 / drop 放弃）。据此提下一批方向。\n"
    "\n"
    "### 你要一手核实，不要只信自述\n"
    "worker 的回执是**它的自述**，可能自相矛盾（实测同一份摘要里 linktype 既写\n"
    "249 又写 220）。判 flag 真假、判文件形态，用 `ctf_inspect` 拿一手依据：\n"
    "- `ctf_inspect(what=\"evidence\")`：台账里每条 flag 的**完整证据链原文**\n"
    "  （解题路径、transform、置信、复现步骤）。逐条看它能不能真推出那个串。\n"
    "- `ctf_inspect(what=\"files\")`：文件清单 + 来源（题目原件/上轮产物/样本）\n"
    "  + **头部魔数**。魔数与自述不符 → 以魔数为准。\n"
    "**它只能核对不能执行**（没有任意路径读取、不返回全文、不跑命令）。要读全文\n"
    "或跑脚本 → 派 worker。清单里**出现** `[上轮产物]` 的 `solve_*.py` 说明这题\n"
    "很可能已被解过：先派 worker 复现它，别从零重写。**没出现**就是从零分析，\n"
    "不要臆测「也许解过」而去翻找不存在的旧产物。\n"
    "\n"
    "### 三条硬纪律\n"
    "- **drop 的方向别再派**。换措辞重派会被归一成同一方向键并拒收——这是设计，\n"
    "  不是 bug。要救它就先看 `ctf_review` 里的理由，换**真正不同**的面。\n"
    "- **requeue ≠ 失败**。它表示「这题还有救，但先让别人上」；稍后（约 30 分钟）\n"
    "  可以再来。别把它当判死，也别马上重派。\n"
    "- **自证不是形式**。worker 上轮声称「拿到执行 / uid=0 / getshell」时，本轮\n"
    "  task 里会自动附加一道让**目标机自己算**的哈希题。回执里出现正确答案才算\n"
    "  真的打通了；只在 worker 自述里出现正确答案 = 编造，代码会拒收并记账。\n"
    "\n"
    "### 题目与 flag\n"
    "- 新题先 `bb_declare_challenge(name=…, description=…, tags=[题型])`；\n"
    "- 拿到疑似 flag 一律 `ctf_submit_flag`（人工可能直接口述 flag，你也用它登记）；\n"
    "- flag 台账是进度**唯一真值**：拿满即解出，代码不再为该方向派工。\n"
    "\n"
    "### 收尾\n"
    "所有方向都已 solved / drop，或时间进收尾窗口（见下方时间预算）时收尾，\n"
    "汇总：走过的方向、正式 flag、候选线索、各自证据、仍未确认的内容。\n"
    "**不要把候选线索写成正式 flag**，也不要在还有 open 方向时说「已全部完成」。")


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

# 「上一轮已写过解题脚本」的形态判据（ctf-inspect-provenance，2026-10-20）。
# 命中即说明这题很可能已被解过——文件清单标题要弹硬提示，让主控先派人**读并
# 复现**旧脚本，而不是从零重写一遍解析器（用户实测：同一题 3 个 agent 各写了
# 一遍 USB 解析器，就因为清单里没标注这些是上轮产物）。
_REUSE_SCRIPT_RX = re.compile(
    r"(?:^|[_\-.])"
    r"(?:solve|solver|extract|extractor|decode|decoder|parse|parser|"
    r"carve|dump|recover|unpack|exp|exploit|poc)"
    r"(?:[_\-. ]|$)", re.I)

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
    # 外部通知箱的类级兜底（chat-inbox）：`object.__new__` 轻量构造的壳没有
    # `__init__`，`_take_notices`/`_release_notice_box` 读这两个属性不该抛。
    notice_inbox = None
    _owns_notice_box = False
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
                 recovery=None,
                 notice_inbox=None):
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
        # 外部投递箱（chat-inbox，2026-10-20）：给**正在跑**的这一轮从外面塞话。
        # 与 abort_event 语义相反——abort 是「立刻停，别干了」，本箱是「情况变了，
        # 你自己决定怎么收尾」（例：人工确认 flag 后让主控尽快收工，而不是被掐断
        # 在半个工具调用里）。
        #
        # **默认自己申请一个**（2026-10-20 修正，用户实测「点了确认子 agent 还在
        # 跑」）：此前只有 `app._chat_launch_turn` 传箱，而 `ChatTurn` 有三个构造
        # 点——spawn 的子专家（`_tool_call_expert`）与 ARTEX Worker
        # （`artex_worker.make_worker`）都不传，于是它们的 `_take_notices()` 恒返回
        # 空：**子 agent 从来收不到任何通知**。改成构造即登记，三处自动接上，且
        # 「有箱 = 这一轮在跑」成为比 `app.state.chat_running`（只登记顶层轮次）
        # 更准的存活判据。
        #
        # 显式传 None 仍可关掉（`_owns_notice_box` 随之 False，用于测试/独立调用）。
        _created_box = False
        if notice_inbox is None:
            try:
                from core.chat.inbox import box_for as _box_for
                notice_inbox = _box_for(thread_id)
                _created_box = notice_inbox is not None
            except Exception:  # noqa: BLE001 —— 通知通道故障不阻断轮次
                log.exception("外部通知箱登记失败 thread=%s", thread_id)
                notice_inbox = None
        self.notice_inbox = notice_inbox
        # 只释放**自己登记**的箱（外部传入的归外部管，代删会把对端的箱一起摘掉）。
        self._owns_notice_box = _created_box
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
        #
        # 判据必须按字面量解析，**不能用 ``bool()``**：专家 yaml 走的是自研的
        # 极简解析（`core.skills.experts._parse_expert`），它只把整数/列表/null
        # 转成 Python 类型，布尔值原样留着**字符串**——盘上 `intent_free: true`
        # 读出来是 `"true"`。目前 serializer 只落 true 所以 `bool()` 碰巧没错，
        # 但那是巧合而非契约：谁手写一次 `intent_free: false` 想关掉它，
        # `bool("false") is True` 会静默保持开启（免意图闸是个「放宽」型开关，
        # 反了就是绕过校验）。按字面量白名单取值最稳。
        try:
            worker_cfg = load_expert(self.packs_root, self.agent_id, self.track)
            self.intent_free = str(
                worker_cfg.get("intent_free", "") or "").strip().lower() in {
                    "true", "yes", "1", "on"}
        except Exception:  # noqa: BLE001
            self.intent_free = False
        # 控制器类智能体（chat-orchestrator + 激进主控）：拿主控工具面、
        # 可委派专家池。两者只在调度策略上不同（见 _system_prompt 与
        # _parallel_expert_dispatch 的 first_return）。
        self.is_orchestrator = self.agent_id in CONTROLLER_IDS
        self.is_aggressive = self.agent_id == AGGRESSIVE_CONTROLLER_ID
        self.is_artex = self.agent_id == ARTEX_CONTROLLER_ID
        self.is_ctf_controller = self.agent_id == CTF_CONTROLLER_ID
        # 先返先触发（first_return）的启用面：激进主控 / ARTEX 主控 / **CTF 解题
        # 主控**（2026-10-20 补上，用户实测真问题）。
        #
        # **为什么 CTF 主控必须启用**：它一批派 3 个 call_expert 探 3 个方向，
        # 旧行为是等**全部**结束才唤醒——第一个方向 40 秒就回了「[未命中] 该方向
        # 证伪」，主控却要等另两个跑满 10 分钟才拿到任何信息，中间这段时间它既
        # 不能改道也不能收尾，纯空转。而 CTF 主控的派发本来就是**独立方向并发**，
        # 没有任何「必须收齐才能判断」的语义。
        #
        # **轮次记账不受影响**（曾被误判为阻塞理由）：`record()` 的调用点在
        # `_tool_call_expert` 尾部，由 `_dispatch(tc)` 在**每个 worker 自己的线程**
        # 里执行（`_parallel_expert_dispatch::run_one`），本来就是「单方向完成即
        # 记账」，从不依赖整批收齐。提前返回只是主线程不等了，worker 照旧跑完
        # 自己的记账与落库。
        self.is_first_return_controller = (self.is_aggressive or self.is_artex
                                           or self.is_ctf_controller)
        # CTF 解题主控的代码侧调度状态（ctf-controller，2026-10-19）：懒建——
        # 只有真正调用 ctf_dispatch/ctf_review 时才构造（Pacing 要读项目配置与
        # 线程创建时间，构造有 IO）。**恒 None 是合法状态**（非该主控线程）。
        self._ctf_session = None
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
        # 技能先行提醒状态（skill-first nudge，2026-10-21）：见 _reset_skill_nudge。
        self._reset_skill_nudge()
        self._dispatcher = self._build_dispatcher()

    @property
    def _is_spawned_subagent(self) -> bool:
        """被 call_expert spawn 的子专家线程（有父线程 + 派发任务留档）。

        控制器类智能体若被当专家委派（expert_names=[] 本就堵死），这层是
        语义兜底：spawn 深度恒为 1，子线程一律不可再委派。"""
        return bool(self.thread.get("parent_thread_id")
                    and self.thread.get("spawned_task"))

    def _take_notices(self) -> list[str]:
        """取走外部投递的通知（无箱 → 空）。取走即清，同一句不会注入两遍。"""
        inbox = getattr(self, "notice_inbox", None)
        if inbox is None:
            return []
        try:
            return inbox.drain()
        except Exception:  # noqa: BLE001 —— 通知通道故障不阻断轮次
            log.exception("外部通知读取失败 thread=%s", self.thread_id)
            return []

    def _has_hit_note(self) -> bool:
        """箱里是否有**人类插话**（只探不取，投递方用同一把锁）。

        与 `_take_notices` 的区别：本方法在「等待专家返回」的循环里被高频调用
        （每 0.3s 一次），**绝不能取走**——取走就等于把话吃了，主控还没读到。
        真正取走仍由步边界的 `_take_notices()` 完成，这里只回答「有没有」。
        """
        inbox = getattr(self, "notice_inbox", None)
        if inbox is None:
            return False
        try:
            return inbox.pending() > 0
        except Exception:  # noqa: BLE001 —— 探活失败按「没有」处理，不阻断等待
            return False

    def _release_notice_box(self) -> None:
        """释放本实例登记的通知箱（轮次收尾调用，幂等）。

        **必须在收尾释放**：登记表里「有箱」等价于「这一轮还在跑」，`_notify_*`
        据此判定投递范围。不释放的话，一个已结束的线程会永远被当成活轮次——
        通知投进去没人读，下一轮若沿用同一 tid 还会被这条旧通知污染。
        """
        if not getattr(self, "_owns_notice_box", False):
            return
        self._owns_notice_box = False
        try:
            from core.chat.inbox import drop_box
            drop_box(self.thread_id)
        except Exception:  # noqa: BLE001 —— 释放失败不影响轮次结果
            log.exception("外部通知箱释放失败 thread=%s", self.thread_id)

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
        文，不做收敛强制。读取失败降级为占位文本，不阻断。

        **CTF 跨题隔离（challenge-isolation，2026-10-19）**：台账里别题的附件/
        入口子资产整批剪掉（`chat_store.challenge_isolation_scope`）——多题共跑
        时子专家本该只看本题文件，全项目台账把别题的 pcap/镜像也摊进来，等于
        开局就把别人的题面灌进上下文。别题的**题目行**保留（知道还有哪些题）。
        """
        try:
            all_assets = self.bb.list_assets(self.project_id)
        except Exception:  # noqa: BLE001 —— 注入失败不阻断委派
            log.exception("chat 子专家资产台账读取失败 thread=%s", self.thread_id)
            all_assets = []
        if self.track == "ctf" and self.thread_id:
            try:
                scope = chat_store.challenge_isolation_scope(
                    self.bb, self.project_id, self.thread_id)
            except Exception:  # noqa: BLE001 —— 隔离失败按全量（宁松勿误空）
                scope = None
            if scope is not None:
                hide = scope["other_assets"] - scope["other_roots"]
                all_assets = [a for a in all_assets if a["id"] not in hide]
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
        elif self.track == "ctf":
            # CTF 轨的 plan→execute→plan 是**解题**节奏，不是「逐面覆盖」节奏
            # （ctf-intent-discipline，2026-10-20）：按面推回在 CTF 轨会变成机械
            # 凑数——`packs/kb/ctf/dimensions.yaml` 的注释已明写这一点（「把它切成
            # 10 个面会变成机械凑数：模型被迫为不适用本题的面写 dead_end，纯烧
            # 轮次」），但这里的通用文案仍在要求「已声明资产还有适用维度未覆盖时
            # 先补 plan」。两处口径必须一致，否则维度清单的克制被提示词推翻。
            lines.append(
                "### plan→execute→plan 纪律（CTF 解题节奏）\n"
                "先 plan：用 declare_intent(target_asset_id=…, statement=…, "
                "dimension=…) 把**当前解题假设**落成一句可证伪的话；再 execute；"
                "每条意图用 close_intent 收尾（finding / dead_end，见题目上下文里的"
                "「CTF 意图纪律」）。\n"
                "**不要按测试面凑覆盖**：CTF 的收敛判据是 flag 台账，不是「每个面都"
                "有收尾意图」。某个面（如 binary 的 analyze）对本题没有意义就跳过，"
                "不要为了「补齐维度」去声明一条空意图再给它写 dead_end。")
        else:
            lines.append(
                "### plan→execute→plan 纪律\n"
                "先 plan：对目标资产用 declare_intent(target_asset_id=..., dimension=..., "
                "statement=...) 逐面声明测试假设；再 execute；每条意图用 close_intent "
                "收尾。收尾摘要前自检：已声明资产还有适用维度未覆盖时，先补 plan 再执行，"
                "不要提前给最终摘要。")
        return "\n".join(lines)

    # 文件清单的来源标注（ctf-inspect-provenance，2026-10-20）。
    #
    # **为什么需要 provenance**（用户实测真问题）：清单里同时躺着
    # `solve_usb_pcapng.py` / `solve_shenbi_traffic.py` / `usbpcap_extract.py`
    # ——这题**之前已经解过、脚本都在**。主控不知道哪些是「用户给的原件」、
    # 哪些是「上一轮 agent 的产物」，于是从零派了 3 条方向，3 个 agent 各写了
    # 一遍解析器（同一题 3 份重复劳动）。
    #
    # 标注只需四档，判据全是**路径前缀**（确定性、无猜测）：
    _FILE_ORIGIN_RULES = (
        ("artifacts/attachments/", "题目原件", "用户上传，**不可信也不可改**"),
        ("scratch/", "上轮产物", "agent 自己产的，**可复用可覆盖**"),
        ("work/", "上轮产物", "agent 自己产的，**可复用可覆盖**"),
        ("samples/", "样本", "样本库"),
    )

    def _file_origin(self, rel: str) -> tuple[str, str]:
        """路径 → (标签, 一句说明)。未知来源单独一档（需人工确认）。"""
        low = rel.lower()
        for prefix, label, note in self._FILE_ORIGIN_RULES:
            if low.startswith(prefix):
                return label, note
        return "未知来源", "需人工确认来历"

    def _workspace_files_context(self) -> str:
        """CTF 主控可见的项目文件清单 + **来源标注**（容器路径）。

        清单的用途不只是「免得重复 find」，更是让主控判断**这题是否已被解过**。
        所以每行必须带 provenance（`[题目原件]` / `[上轮产物]` / `[样本]` /
        `[未知来源]`）——没有它，`solve_xxx.py` 和 `题目.zip` 在模型眼里一样，
        它就会从零重写一遍解析器。见 `_FILE_ORIGIN_RULES` 的注释。
        """
        if not self.artifacts_dir:
            return ""
        root = Path(self.artifacts_dir).parent
        rows: list[str] = []
        reuse_hit = False
        for rel_root in ("samples", "artifacts/attachments", "artifacts",
                         "scratch", "work"):
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
                        st = p.stat()
                        size, mtime = st.st_size, st.st_mtime
                    except OSError:
                        size, mtime = 0, 0.0
                    label, _note = self._file_origin(rel)
                    # 上轮产物里出现「解题脚本」形态 = 这题很可能已被解过，
                    # 触发清单标题里的硬提示（比逐行标注更醒目）。
                    if label == "上轮产物" and _REUSE_SCRIPT_RX.search(p.name):
                        reuse_hit = True
                    stamp = (time.strftime("%m-%d %H:%M",
                                           time.localtime(mtime)) if mtime else "")
                    rows.append(f"- [{label}] /workspace/{rel} ({size} bytes"
                                + (f", {stamp}" if stamp else "") + ")")
                    if len(rows) >= 100:
                        break
            except OSError:
                continue
            if len(rows) >= 100:
                break
        if not rows:
            return "## CTF 项目文件清单\n- （samples/ 与 artifacts/ 下暂无文件）"
        head = ["## CTF 项目文件清单（容器内路径，优先直接使用，不要重复 find）",
                "- 标注含义：`[题目原件]`=用户上传（不可信不可改）；"
                "`[上轮产物]`=上一轮 agent 产出（**可复用可覆盖**）；"
                "`[样本]`=样本库；`[未知来源]`=来历不明，需人工确认。"]
        if reuse_hit:
            # 这条单独就能把「3 个 agent / 3 份解析器」压到「1 个 agent / 5 分钟」。
            head.append(
                "- ⚠ 清单里有 `[上轮产物]` 的 `solve_*.py` / `*_extract.py` / "
                "`*decode*.py` —— **这题很可能已被解过**。第一动作应该是派一个 "
                "worker 去**读并复现**它（跑一遍看输出是否就是答案），"
                "而不是从零重新发现题型、重写解析器。")
        return "\n".join([*head, *rows])

    def _vision_context(self) -> str:
        """看图纪律注入（vision 通道，2026-10-19）：模型能看图时才返回正文。

        动机（用户实测真问题）：`deepseek-v4.1-flash` 所在网关支持图片输入，
        但此前全链路没有图像通道 + 知识库把 `tesseract`/`zbarimg` 写成唯一解，
        模型于是**有眼睛却不用**，一路 OCR。这里把「先看，再决定要不要 OCR」
        写成显式纪律，并划清 OCR 仍然更优的三种场合——不是禁用 OCR，是让它
        别默认选 OCR。

        与 `view_image` 的工具描述**互为冗余**（工具描述是模型调工具时才看到，
        系统提示是每步都在）：用户踩过的坑，双保险值得。
        """
        if not getattr(self.llm, "supports_vision", False):
            return ""
        return "\n".join([
            "## 看图能力（你的模型能直接看图片）",
            "- 你**有视觉通道**：`view_image(path=…)` 会把图片本体送进你的上下文，"
            "你看到的是真像素，不是文字描述。",
            "- **题目给的是图/截图/二维码/频谱图/位平面图/拼图碎片/UI 截图时，"
            "第一个动作就是 view_image 看一眼**，不要先跑 OCR。先看清楚「这是什么"
            "形态的题」，再决定用什么工具——方向错了，OCR 再多也只是把错误方向"
            "读得更细。",
            "- OCR（`tesseract` / `zbarimg`）仍然更优的三种场合：① **批量**扫几十"
            "上百张候选（位平面暴力枚举、逐帧扫描）——肉眼看不完；② 需要**机器可"
            "比的精确字符串**（逐字符 diff、逐帧对比）；③ 图糊/残缺到人眼也难辨，"
            "只能靠算法容错。这三种之外，一律先 view_image。",
            "- 隐写题的常见误区：直接 `strings`/`binwalk` 扫一遍就下结论「没东西」。"
            "先看图——LSB 改动、色彩异常、边界错位、拼图接缝，人眼一秒能看出，"
            "命令行要试十次。",
        ])

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
            # 资产登记边界（challenge-asset 兜底，2026-10-21 实测事故）：子专家把
            # zip 解压后把「zip 内文件」当题目附件登记（bb_add_asset value=<sha256>，
            # parent_id 不传）→ 根级 binary → 前端「CTF 轨根资产即题目」渲染成
            # 「又声明了一道题」（用户实测 ctf-2：3 条孤儿 binary）。后端已兜底 +
            # 拒绝，这里把口径讲清，省一轮无用调用。
            lines.append(
                "- **只有题目附件本体才登记 binary**（bb_add_asset type=binary "
                "value=<附件真实 sha256>，不用传 parent_id，会自动挂到本题下）；"
                "unpack_attachment 解压出来的 zip 内文件**不要登记**——路径已在解压"
                "回执里，直接 read_file 用，结论走 bb_add_finding。")
            # 台账现状 + 冲突主动上报（ctf-conflict-push，2026-10-20，P1-1）。
            #
            # 要解决的问题（用户实测）：两个 agent 都能通过 bb_query 看到台账
            # （所以都说「2/1 已满」），但没有一个提出「已经有另一个 flag 了，
            # 我们冲突了」。根因是台账对 worker 只是**只读数据**，没有「你发现
            # 异常要上报」的通道——`conflict` 只在**提交时**触发，只看不交就
            # 永远不触发。
            #
            # 所以把异常变成**每轮注入的主动推送**：注入在 system 里（不污染
            # 消息历史），每轮重投影，状态一出现就被看到。
            lines.extend(self._ledger_context(cid))
            # 交付纪律（deliver，2026-10-21 Nan 要求）：**只对子 agent 注入**——
            # 主控没有 deliver 工具（它的交付对象是人类，直接回话即可），给它写
            # 这段等于教它调一个不存在的工具（本仓典型事故：提示词与工具面不同口径）。
            #
            # 为什么要写进 system 而不只靠工具描述：模型对「任务完成」有很强的
            # 收尾惯性——一交东西就停。工具描述只在它翻到那个工具时才起作用，
            # 而「现在要不要收尾」的判断发生在它准备写最后那段话的时候。
            if not self.is_orchestrator:
                lines.append(
                    "- **交付 ≠ 收尾**（`deliver`）：手上有能站住的东西（拿到 flag、"
                    "定位到密码/密钥位置、排除了几条死路）但还想继续挖时，**先 deliver "
                    "交出去**——主控那边 call_expert 仍显示运行中，你可以接着做，"
                    "不必攒到最后。**也别因为交过就停**：只要还能拿到 flag，或手里有与"
                    "flag 强相关的线索（能定位到关键凭据、有可回环的变换链），就继续；"
                    "确实已无更大成果，再用一段文本收尾。")
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
        # 跨题隔离（challenge-isolation，2026-10-19）：把「你看到的黑板已经按题
        # 裁过了」说清楚——否则模型会以为「findings 只有这么几条」是黑板真的空，
        # 反复换参数重查；也会在查不到某条（其实属于别题）时误判自己没登记成功。
        lines.append(
            "- **黑板查询默认只看本题**：bb_query 的 findings/assets/events 已按"
            "「本线程题目子树 + 未归属条目」裁过，**别的题目**的发现、附件子资产、"
            "执行事件都不回传（`assets` 里仍能看到别的题的题目行，便于你知道项目"
            "还有哪些题）。这不是「黑板是空的」——是别的题的结论与你无关，看到它们"
            "只会把别人的打法当成自己的线索。确需跨题对照时显式传 "
            "`cross_challenge=true`。")
        # 以下「怎么解题」的纪律**只给执行侧**（Worker / 子专家 / 激进主控的
        # 探针），**不给 ARTEX 主控**（artex-controller-no-exec，2026-10-19）。
        # 原因：主控是观察者+操舵者，它看到「拿到 flag 用 ctf_submit_flag、
        # 弱口令用 ctf_ammo 写 scratch 再 run_cmd 消费」这类执行级指令后，
        # 会直接照着做——用户实测「ARTEX 主控自己在解题，不派 Worker」。
        # 主控需要的只有：怎么建题、怎么操舵、怎么判收敛（见 _ARTEX_CONTROLLER_STYLE）。
        if self.is_artex:
            lines.append(
                "- **你不解题**：本线程没有 run_cmd/read_file 等实操工具。看到题目"
                "要求后，用 artex_plan 让 Planner 拆成 Intent、由 Engine 派 Worker 执行；"
                "Worker 找到 flag 会自己用 ctf_submit_flag 入台账。你只观察 "
                "artex_status 的题目进度并按需操舵。")
            lines.append(
                "- 建题只需 bb_declare_challenge 一次（题目由 Engine 调度时自动"
                "锚进任务范围）；**不要**为了「赶紧开跑」去直接派专家——本线程"
                "没有 call_expert，跳过 artex_plan 会让目标、范围与探索图全空。")
            pctx = self._pacing_context()
            if pctx:
                lines.append(pctx)
            return "\n".join(lines)
        if self.is_ctf_controller:
            # CTF 解题主控（ctf-controller，2026-10-19）：有 ctf_submit_flag
            # （人工可能直接口述 flag）但**没有执行工具**。故讲「怎么登记」
            # 不讲「怎么消费 ctf_ammo」——后者要 run_cmd 配合，它没有。
            lines.append(
                "- **你只定方向，不做执行**：本线程没有 run_cmd/read_file。要跑命令"
                "就 ctf_dispatch 提方向、再按计划 call_expert 派专家；专家找到 flag "
                "会自己用 ctf_submit_flag 入台账。人工直接口述 flag 时你也用它登记。")
            lines.append(
                "- flag 台账是进度**唯一真值**：`ctf_review` 的「已得/需要」就是台账"
                "算出来的，拿满即解出，代码不再为该方向派工——别重复派。")
            pctx = self._pacing_context()
            if pctx:
                lines.append(pctx)
            return "\n".join(lines)
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
        # 弱口令候选字典（ctf-ammo）：只有**真能执行**的角色才讲「写入 scratch
        # 再用 run_cmd 消费」。chat-orchestrator 这类纯委派主控没有 run_cmd，
        # 讲这句等于教它调一个它没有的工具（同类撕裂，2026-10-19 一并修）。
        if self.is_orchestrator:
            lines.append(
                "- 弱口令/默认口令题：把题面线索（产品名、年份、管理员名字）交给"
                "负责该题的**专家**，让它用 `ctf_ammo(hint_text=…, obs_text=…)` "
                "展开候选表——你本线程没有执行工具，不要自己跑。")
        else:
            lines.append(
                "- 弱口令/默认口令题不要只凭记忆试 `admin/admin`：把题面线索（产品名、"
                "年份、管理员名字）交给 `ctf_ammo(hint_text=…, obs_text=…)` 生成候选表"
                "（`pairs=true` 给 `user:pass`），写入 scratch 后再用 run_cmd 消费。"
                "它只产候选，不爆破、不发请求。")
        # CTF 意图纪律（ctf-intent-discipline，2026-10-20）：**替换**渗透轨那套
        # 「逐资产独立立意 / 判净须 dead_end 背书」。
        #
        # 要解决的问题（用户实测）：通用意图纪律（`agent/loop.py` 的
        # STRICT_PROMPT_TAIL 第 10 条）是**渗透语义**——它要求「一个子目标要判净，
        # 须它名下意图全部收尾且至少一条 dead_end」，因为渗透的收敛靠
        # `tested_clean`、而 `tested_clean` 靠 dead_end 背书。CTF 轨的收敛靠
        # **flag 台账**，根本没有 dead_end 背书的消费方。模型被那段文案指挥着
        # 去为「本题不适用的面」补 dead_end，纯烧轮次——用户原话「导致在结束时
        # 需要死意图背书这方面浪费时间」。
        #
        # 这段只给执行侧（ARTEX 主控/CTF 解题主控已提前 return，拿不到这里）：
        # 主控不声明意图，讲了只会诱导越权。
        lines.append(
            "## CTF 意图纪律（与渗透轨不同，按本轨口径执行）\n"
            "- **一题一意图就够了**：意图 = 你的解题假设（「验证输入是否触发栈溢出」"
            "「验证 pcap 里是否有明文凭据」）。解题过程中换方向就再声明一条，"
            "**不要**为「本题不适用的测试面」凑意图。\n"
            "- **收尾三选一，按实际结果**：① 解出了 → "
            "`close_intent(outcome=\"finding\", finding_ids=[flag 留痕那条 finding])`"
            "（ctf_submit_flag 的候选线索回执里给了 finding id；正式 flag 也会落一条"
            " flag-capture 留痕）——CTF 轨**不校验** vuln/intel 类别，题目级并入可能让"
            "该 id 实际是 category=vuln 的条目，照样传即可，别改 outcome=\"vuln\""
            "（flag 不是漏洞）；② 这条路不通但题还能解 → "
            "`close_intent(outcome=\"dead_end\", dead_reason=…)`，只写清卡点即可，"
            "**不必**去凑 `event:`/`http:` 证据引用；③ 整道题放弃 → "
            "`ctf_abandon(reason=…)`。\n"
            "- **CTF 轨没有「资产测干净（tested_clean）」**：本题终态由 flag 台账判定，"
            "不要用 `bb_asset_status` 标题目或附件为 tested_clean——它不会让任何东西"
            "收敛，只会撞门禁。\n"
            "- **不需要 dead_end 背书**：那是渗透轨给 `tested_clean` 做背书的机制，"
            "CTF 轨没有消费者。解出题就直接收尾，别为了「留个死路记录」再补一条。")
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

    # ---------- CTF 解题主控（ctf-controller）的代码侧调度 ----------

    def _ctf_session_for(self):
        """惰性构造本线程的 `CtfSession`（ctf-controller，2026-10-19）。

        **Pacing 的构造要读项目 config 与线程创建时间**（有 IO），故不在
        `__init__` 里建——绝大多数线程（子专家、其他主控）永远用不到它。
        构造失败返回 None：调度不可用时主控仍能靠 call_expert 干活，不该整轮炸。

        复用 `_pacing_context` 的配置读取口径（deadline 优先、无则阶梯推断），
        但**不复用它的返回值**——那里返回的是给人看的文本，这里要的是 Pacing
        对象本身（放弃判据要用 `hard_total_cap_s()` 等数值）。
        """
        if not self.is_ctf_controller:
            return None
        if self._ctf_session is not None:
            return self._ctf_session
        try:
            from core.ctf import pacing as ctf_pacing
            from core.ctf.session import CtfSession
            proj = self.bb.get_project(self.project_id) or {}
            cfg = (proj.get("config") or {}).get("ctf") or {}
            n_tasks = int(cfg.get("tasks") or 0) or ctf_pacing.ANCHOR_NTASKS
            started = self._thread_started_at()
            deadline = cfg.get("deadline")
            ts = _to_epoch(deadline) if deadline else None
            p = (ctf_pacing.Pacing.from_deadline(ts, n_tasks=n_tasks,
                                                 started_at=started)
                 if ts else ctf_pacing.Pacing(n_tasks=n_tasks, started_at=started))
            self._ctf_session = CtfSession(p, holder=f"chat-{self.thread_id[-12:]}")
        except Exception:  # noqa: BLE001 —— 调度不可用不阻断主控基本能力
            log.exception("chat ctf 会话构造失败 thread=%s", self.thread_id)
            return None
        return self._ctf_session

    def _tool_ctf_dispatch(self, args: dict) -> str:
        """`ctf_dispatch`：把 LLM 提的方向交给代码判据，返回准派/拦下计划。"""
        sess = self._ctf_session_for()
        if sess is None:
            return ("[错误] ctf_dispatch 不可用：本线程没有装配 CTF 调度器"
                    "（只有 ctf-controller 主控可用）")
        # 已解出闸（challenge-confirm-notice，2026-10-20 修正）：
        #
        # 「人工确认 → 投递通知 → 模型自己收尾」是**软**路径，依赖模型读懂那条
        # 通知。但派方向是**代码判据**的地盘，不该让「模型有没有听话」成为唯一
        # 防线：实测人工确认后主控照样派完剩下方向（用户截图：确认已落账，界面
        # 上仍在跑 `curl -sL -o libc-2.23.so`），白烧十几分钟。
        #
        # 所以这里加一道**硬**闸：台账已 solved 就直接拒，并指回收尾动作。
        # 判据取台账派生值（`challenge_state`），与前端徽标、覆盖度同源——
        # 不看 LLM 自述，也不看本会话里记过什么。
        blocked = self._solved_challenge_block()
        if blocked:
            return blocked
        dirs = args.get("directions")
        if not isinstance(dirs, list) or not dirs:
            return "[错误] ctf_dispatch 失败：directions 必须是非空数组"
        clean: list[dict] = []
        for d in dirs:
            if not isinstance(d, dict):
                continue
            sg = str(d.get("subgoal") or "").strip()
            if not sg:
                continue
            item: dict = {"subgoal": sg}
            if d.get("expert"):
                item["expert"] = str(d["expert"]).strip()
            if d.get("need") not in (None, ""):
                item["need"] = d["need"]
            clean.append(item)
        if not clean:
            return "[错误] ctf_dispatch 失败：所有方向都缺 subgoal"
        plan = sess.plan(clean, breadth_starved=bool(args.get("breadth_starved")))
        if plan.triage_blocked:
            # 分诊闸不是「你这几个方向都不行」，是「先改编排」——不要报
            # 「准派 0 拦下 N」，那会让模型以为方向本身有问题去换措辞。
            return "[代码调度] 分诊还没得出结论：整批方向暂不派。\n" + plan.render()
        head = (f"[代码调度] 准派 {len(plan.granted)} 个，拦下 "
                f"{len(plan.rejected)} 个。\n")
        return head + plan.render()

    def _solved_challenge_block(self) -> str:
        """本题台账已 solved → 返回拒派文案；否则返回空串。

        **为什么需要它**（2026-10-20 用户实测）：人工确认 flag 后只投递了一条
        通知，而通知是给模型读的软约束。实测主控忽略它、继续把剩下方向派完
        （界面截图：确认已落账，仍在 `curl -sL -o libc-2.23.so`），白烧预算。
        「派不派方向」本来就在代码判据手里（`ctf_dispatch` 的准入、`allowed()`
        的越权校验），已解出这件事必须同样由代码兜住。

        判据取台账派生值（`challenge_state`），**不看本会话记过什么**：人工确认
        发生在任意时刻、甚至上一轮，会话内状态不该是唯一真值来源。
        """
        # 只有 CTF 轨才有题目归属（`challenge_asset_id` 是 CTF 专用位）。非 CTF
        # 轨道直接放行——这道闸挂在 `call_expert` 热路径上，不该让渗透/红队每次
        # 派活都多付一次 DB 查询。
        if getattr(self, "track", "") != "ctf":
            return ""
        try:
            from core.blackboard.challenge_flags import challenge_state
            from core.chat import store as chat_store
            cid = chat_store.resolve_challenge_asset_id(self.bb, self.thread_id)
            if not cid:
                return ""
            st = challenge_state(self.bb, self.project_id, cid)
            if not st.get("solved"):
                return ""
            return ("[代码调度] 本题**已解出**（台账已确认 "
                    f"{st.get('accepted')}/{st.get('quota')} 条 flag），不再派新方向。\n"
                    "现在只做三件事：① 把手上已识别的结论写回；② 不要再派 "
                    "call_expert；③ 用一句话说明「本题已解出，收工」。\n"
                    "若你判断这条确认是错的（flag 不对），让人工在台账里撤销，"
                    "不要在这里重派方向。")
        except Exception:  # noqa: BLE001 —— 判据故障不该锁死主控
            log.exception("已解出闸检查失败 thread=%s", self.thread_id)
            return ""

    def _tool_ctf_review(self, args: dict) -> str:
        """`ctf_review`：代码侧 verdict 表 + 互斥表。"""
        sess = self._ctf_session_for()
        if sess is None:
            return ("[错误] ctf_review 不可用：本线程没有装配 CTF 调度器"
                    "（只有 ctf-controller 主控可用）")
        return sess.review()

    # 只读头部字节数：够判魔数（zip=PK\x03\x04、png=\x89PNG、elf=\x7fELF、
    # pcapng=\x0a\x0d\x0d\x0a），不够解题。**这不是随便定的**——P0-2 的红线是
    # 「给它核对的能力，不给它执行的能力」。
    _INSPECT_HEAD_BYTES = 32
    # 单次最多摊几个文件（防清单瀑）
    _INSPECT_MAX_FILES = 80

    def _tool_ctf_inspect(self, args: dict) -> str:
        """`ctf_inspect`：**只读的裁决通道**（ctf-inspect，2026-10-20，P0-2）。

        **为什么需要它**（用户复盘的核心矛盾）：
        > 系统给了主控「必须一手核实」的纪律，却没给它任何核实的工具。

        实测那一轮里，主控对「这条 flag 是不是误报」的判断**唯一依据是另一个
        agent 的自述摘要**——它没看过 `solve_usb_pcapng.py`、没看过 `out.txt`、
        没看过 `flag.zip`。于是自述里的 linktype 249 与 220 互相矛盾，它无法发现。

        **为什么不能直接给 `read_file`/`run_cmd`**：主控一旦能读文件、跑命令，
        就会自己解题，绕过整套调度（ARTEX 主控事故 `3d4834f`：用户实测主控自己
        在解题、不派 Worker）。这是硬红线，不松。

        **所以这条通道的设计原则是「给核对，不给执行」**：
        - `what="evidence"`：把台账里每条 flag 的**完整证据链原文**摊开（source
          提交时的解题路径原文、transform、confidence、judged_by、关联 finding
          的 evidence）。**不做摘要、不做评价**——让主控自己比。这些内容本来就
          在台账里，主控本来就该看得见。
        - `what="files"`：题目子树下的文件清单 + provenance + **每个文件的头部
          32 字节魔数**。只读头部，**绝不返回全文**——够判断「这是不是 zip/图片/
          流量包」，不够解题。

        没有任意路径读取（只认本题目子树 + 工作区内已登记文件）、没有全文、
        没有命令执行。这三条是它区别于 `read_file` 的全部意义。
        """
        what = str(args.get("what") or "evidence").strip().lower()
        cid = str(args.get("challenge_id") or "").strip()
        if not cid:
            # 只做**读**的兜底解析（沿 parent_thread_id 向上找题目绑定），
            # **不按线程标题补建**——核对通道不该有副作用。`ToolDispatcher`
            # 上的 `_challenge_fallback_asset()` 会补建，这里刻意不用它。
            try:
                cid = chat_store.resolve_challenge_asset_id(
                    self.bb, self.thread_id) or ""
            except Exception:  # noqa: BLE001 —— 解析失败按「无题」处理
                cid = ""
        if not cid:
            return ("[拒绝] 无法确定这道题：先 bb_declare_challenge 登记题目"
                    "（或显式传 challenge_id）")
        asset = self.bb.get_asset(cid)
        if asset is None or asset.get("project_id") != self.project_id:
            return f"[错误] 题目不存在: {cid}"
        if str(asset.get("type") or "") != "challenge":
            return (f"[拒绝] {cid} 不是题目（type={asset.get('type')}）"
                    "——核对是题目级动作。")
        if what in ("evidence", "flag", "flags"):
            return self._inspect_evidence(cid, asset)
        if what in ("files", "file"):
            return self._inspect_files(cid, asset)
        return (f"[拒绝] what 只能是 evidence / files，收到 {what!r}。"
                "evidence=看台账里每条 flag 的完整证据链原文（判真假用）；"
                "files=看题目文件清单+来源标注+头部魔数（判形态用）。")

    def _inspect_evidence(self, cid: str, asset: dict) -> str:
        """摊开台账证据链原文（不摘要不评价）。"""
        from core.blackboard.challenge_flags import (challenge_state, list_flags)
        name = str(asset.get("value") or cid)
        rows = list_flags(self.bb, self.project_id, cid)
        st = challenge_state(self.bb, self.project_id, cid)
        head = [f"## {name} 的 flag 证据链（原文，未摘要）",
                f"- 状态 {st['state']}｜已确认 {st['accepted']}/{st['quota']}"
                f"｜候选 {st['candidate']}｜否决 {st['rejected']}"]
        if st.get("conflict"):
            head.append("- ⚠ **格式冲突**：同题内出现前缀互不相同的已确认 flag——至少一条是假的。")
        if st.get("concluded"):
            head.append("- 主控已收尾（不再派工），但 flag 仍待人工确认。")
        if not rows:
            head.append("- （台账为空：还没有任何 flag 被登记）")
            return "\n".join(head)
        findings = {str(f.get("id")): f
                    for f in self.bb.list_findings(self.project_id)}
        for i, r in enumerate(rows, 1):
            verdict = str(r.get("verdict") or "")
            tag = {"accepted": "人工确认", "rejected": "已否决"}.get(verdict, "候选")
            head.append(f"\n### {i}. {r.get('flag')}　[{tag}]")
            head.append(f"- 置信 {float(r.get('confidence') or 0):.1f}"
                        f"｜判定者 {r.get('judged_by') or 'ai'}"
                        f"｜登记 {r.get('created_at') or ''}")
            fmt = str(r.get("flag_format") or "")
            if fmt:
                head.append(f"- 题目声明格式 {fmt}")
            transform = str(r.get("transform") or "")
            head.append(f"- 变换 {transform or '**（未提供 → 没跑回环验证）**'}")
            if r.get("verdict_reason"):
                head.append(f"- 裁决理由 {r.get('verdict_reason')}")
            src = str(r.get("source") or "").strip()
            # **原文照贴**，只截长度（防一条超长把整段挤爆），不重述不总结。
            if src:
                head.append("- 解题路径原文：")
                head.append("```")
                head.append(src[:2000])
                head.append("```")
            else:
                head.append("- 解题路径原文：**（空——这条没有任何一手依据）**")
            fid = str(r.get("finding_id") or "")
            f = findings.get(fid)
            if f:
                ev = f.get("evidence") or {}
                steps = ev.get("repro_steps") if isinstance(ev, dict) else None
                if steps:
                    head.append(f"- 关联留痕 finding {fid} 的复现步骤：")
                    for s in steps[:3]:
                        code = str((s or {}).get("code") or "")[:400]
                        head.append(f"  - {str((s or {}).get('desc') or '')[:80]}")
                        if code:
                            head.append("    ```")
                            head.append("    " + code.replace("\n", "\n    "))
                            head.append("    ```")
        head.append("\n**怎么用这份材料**：逐条比 `source`/`transform` 是否真能推出"
                    "该 flag。没有 transform 的条目**只是形态像**，不是证据。"
                    "两条前缀不同 → 至少一条是假的，标掉错的那条。")
        return "\n".join(head)

    def _inspect_files(self, cid: str, asset: dict) -> str:
        """题目子树文件清单 + provenance + 头部魔数（不返回全文）。

        **两个来源都要列**（这是它区别于「只查资产表」的地方）：
        ① 题目子树下的**已登记资产**（附件子资产 / 入口点）；
        ② 工作区里的 `artifacts/attachments`（用户直接丢进输入框的）与
           `scratch`/`work`（**上一轮 agent 自己的产物**）。

        ②不可省：用户复盘里主控看不到 `solve_usb_pcapng.py` 就是因为它不是
        资产、只在 `scratch/` 里躺着——而这恰好是「这题已经解过」的最强信号。
        仍不做全盘遍历（只认这几个已知根），「没有任意路径读取」不变。
        """
        if not self.artifacts_dir:
            return "[错误] 本线程未装配工作目录，无法列出文件"
        root = Path(self.artifacts_dir).parent
        # 题目子树内的资产（附件子资产 / 入口点）——只认这些，不做全盘遍历，
        # 这就是「没有任意路径读取」的落实处。
        try:
            assets = self.bb.list_assets(self.project_id)
        except Exception:  # noqa: BLE001
            assets = []
        parent_of = {a["id"]: a.get("parent_id") for a in assets}
        by_id = {a["id"]: a for a in assets}

        def _in_subtree(aid: str) -> bool:
            walker, seen = aid, set()
            while walker and walker not in seen:
                seen.add(walker)
                if walker == cid:
                    return True
                walker = parent_of.get(walker)
            return False

        name = str(asset.get("value") or cid)
        lines = [f"## {name} 的文件（来源标注 + 头部魔数；**只给头部，不给全文**）"]
        rows: list[str] = []
        # 「这题已经解过」只能**基于本次清单的事实**说（ctf-inspect-reuse-hint，
        # 2026-10-20）。此前这段提示是无条件拼在尾巴上的固定文案，于是新开的
        # 项目、新派的题（清单里只有 `[题目原件]`）也会被告知「这题已经解过，
        # 别再派从零分析」——直接与事实相反，还会把主控推去「复现」不存在的产物。
        reuse: list[str] = []
        # 两个来源会**指向同一个文件**（资产行登记的就是 `artifacts/attachments/x`
        # 里的那个），不跨源去重就会把同一个 pcapng 列两遍——主控会以为有两份。
        seen_rel: set[str] = set()

        def _rel(p: Path) -> str:
            try:
                return p.relative_to(root).as_posix()
            except ValueError:
                return str(p)

        def _note_reuse(p: Path) -> None:
            rel = _rel(p)
            if (self._file_origin(rel)[0] == "上轮产物"
                    and _REUSE_SCRIPT_RX.search(p.name)):
                reuse.append(rel)

        for a in assets:
            if a["id"] == cid or not _in_subtree(a["id"]):
                continue
            val = str(a.get("value") or "").strip()
            if not val:
                continue
            p = Path(val)
            if not p.is_absolute():
                p = root / val
            if not p.is_file():
                continue
            rel = _rel(p)
            if rel in seen_rel:
                continue
            seen_rel.add(rel)
            rows.append(self._inspect_one(p, root))
            _note_reuse(p)
            if len(rows) >= self._INSPECT_MAX_FILES:
                break
        # 附件目录 + 上轮产物目录：即使资产行缺失（用户直接丢附件进输入框、
        # agent 自己写的脚本）也要能看见。**只认这三个已知根**，不做全盘遍历。
        for sub, label in (("artifacts/attachments", "题目原件"),
                           ("scratch", ""), ("work", "")):
            base = root / sub
            if not base.is_dir():
                continue
            try:
                for p in sorted(base.rglob("*"), key=lambda x: str(x).lower()):
                    if not p.is_file():
                        continue
                    rel = _rel(p)
                    if rel in seen_rel:
                        continue
                    seen_rel.add(rel)
                    rows.append(self._inspect_one(p, root, origin=label))
                    _note_reuse(p)
                    if len(rows) >= self._INSPECT_MAX_FILES:
                        break
            except OSError:
                pass
            if len(rows) >= self._INSPECT_MAX_FILES:
                break
        if not rows:
            lines.append("- （题目子树下没有可读文件：附件可能还没上传，"
                         "或资产值不是文件路径）")
            return "\n".join(lines)
        lines.extend(rows)
        lines.append("\n**怎么用**：`PK\\x03\\x04`=zip、`\\x89PNG`=PNG、`\\x7fELF`=ELF、"
                     "`\\x0a\\x0d\\x0d\\x0a`=pcapng、`\\xd4\\xc3\\xb2\\xa1`=pcap。"
                     "魔数与 agent 自述不符 → 以魔数为准（这是你的一手依据）。"
                     "需要读全文/跑脚本请**派 worker**，不要试图自己执行。")
        if reuse:
            # 只有清单里**真的**存在上轮解题脚本时才这么说。这条单独就能把
            # 「3 个 agent / 3 份解析器」压到「1 个 agent / 5 分钟」。
            shown = "、".join(reuse[:3]) + ("…" if len(reuse) > 3 else "")
            lines.append(f"- ⚠ 上面有 `[上轮产物]` 的解题脚本（{shown}）——"
                         "**这题很可能已被解过**。第一动作应该是派 worker 去"
                         "**读并复现**它（跑一遍看输出是否就是答案），而不是从零"
                         "重新发现题型、重写解析器。")
        else:
            # 反向也要明说，否则模型会自己脑补「也许已经解过」而绕路。
            lines.append("- 清单里**没有** `[上轮产物]` 的解题脚本（`solve_*` / "
                         "`extract_*` / `parse_*`）→ 这题**没有可复用的既有成果**，"
                         "按新题从零分析是正常路径，不要去找不存在的旧脚本。")
        return "\n".join(lines)

    def _inspect_one(self, p: Path, root: Path, origin: str = "") -> str:
        """单文件一行：来源 + 路径 + 大小 + 头部魔数（hex + 可读形态）。"""
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = str(p)
        if not origin:
            origin = self._file_origin(rel)[0]
        try:
            size = p.stat().st_size
            with p.open("rb") as fh:
                head = fh.read(self._INSPECT_HEAD_BYTES)
        except OSError as e:
            return f"- [{origin}] /workspace/{rel} （读取失败: {e}）"
        magic = self._magic_of(head)
        return (f"- [{origin}] /workspace/{rel} ({size} bytes) "
                f"头部={head.hex(' ') or '<空>'}　→ {magic}")

    @staticmethod
    def _magic_of(head: bytes) -> str:
        """魔数 → 人话（判形态用；认不出就说认不出，不猜）。"""
        if not head:
            return "空文件"
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            return "**ZIP 压缩包**"
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "**PNG 图片**"
        if head.startswith(b"\xff\xd8\xff"):
            return "**JPEG 图片**"
        if head.startswith(b"GIF8"):
            return "**GIF 图片**"
        if head.startswith(b"%PDF"):
            return "**PDF 文档**"
        if head.startswith(b"\x7fELF"):
            return "**ELF 可执行**"
        if head.startswith(b"MZ"):
            return "**PE 可执行**"
        if head.startswith(b"\x0a\x0d\x0d\x0a"):
            return "**pcapng 流量包**"
        if head.startswith(b"\xd4\xc3\xb2\xa1") or head.startswith(b"\xa1\xb2\xc3\xd4"):
            return "**pcap 流量包（小端）**"
        if head.startswith(b"\x4d\x3c\xb2\xa1") or head.startswith(b"\xa1\xb2\x3c\x4d"):
            return "**pcap 流量包（大端）**"
        if head.startswith(b"Rar!"):
            return "**RAR 压缩包**"
        if head.startswith(b"7z\xbc\xaf\x27\x1c"):
            return "**7z 压缩包**"
        if head.startswith(b"\x1f\x8b"):
            return "**gzip 压缩**"
        if head.startswith(b"BZh"):
            return "**bzip2 压缩**"
        if head.startswith(b"ustar") or head[257:262] == b"ustar":
            return "**tar 归档**"
        if head.startswith(b"SQLite format 3\x00"):
            return "**SQLite 数据库**"
        if head.startswith(b"\x00\x00\x00\x00"):
            return "疑似全零（可能是被填充/损坏）"
        try:
            text = head.decode("utf-8")
            if text.isprintable() or text.replace("\n", "").isprintable():
                return "文本（可打印 ASCII/UTF-8 前缀）"
        except UnicodeDecodeError:
            pass
        return "**未知形态**（认不出，别猜——派 worker 看）"

    def _accepted_flag_lines(self, cid: str, got: int, quota: int) -> list[str]:
        """已确认 flag 清单（原文 + 题面提示），用于防重复提交（flag-hint，2026-10-21）。

        与 `_solved_notice`（一次性通知）互补：这条是**每轮都注入**的常规事实，
        因为 worker 可能在通知投递之后才起轮、或压根没读那条通知。
        """
        try:
            from core.blackboard.challenge_flags import accepted_flag_briefs
            briefs = accepted_flag_briefs(self.bb, self.project_id, cid)
        except Exception:  # noqa: BLE001
            log.exception("读取已确认 flag 清单失败 challenge=%s", cid)
            return []
        if not briefs:
            return []
        lines = [f"- **本题已确认 {got}/{quota} 条 flag**（下面这些已经拿到，"
                 "**不要再提交同一串**，也不用再往它们各自的方向挖）："]
        for b in briefs[:12]:
            hint = str(b.get("hint") or "").strip()
            flag = str(b.get("flag") or "").strip()
            if hint:
                lines.append(f"  - #{b.get('index')} `{flag}` —— 题面提示：{hint}")
            else:
                lines.append(f"  - #{b.get('index')} `{flag}`（题面未登记提示）")
        if len(briefs) > 12:
            lines.append(f"  - …（另有 {len(briefs) - 12} 条，用 ctf_review 看全量）")
        lines.append(
            f"- 还差 **{max(quota - got, 0)} 条**才解出。剩下的按题面未覆盖的提示去找；"
            "若你手上这条**不在上面**且方向也不重叠，正常提交即可。")
        return lines

    def _ledger_context(self, cid: str) -> list[str]:
        """台账现状 + **冲突主动上报**（ctf-conflict-push，2026-10-20，P1-1）。

        只报**异常**，不报常规进度：常规进度用 `ctf_review` 查，注入里再列一遍
        会稀释真信号（模型对每轮都出现的长文本会开始跳过）。

        三条异常各有不同的**该做什么**，必须分开写：
        - `conflict`：同题内前缀互不相同的已确认 flag → 至少一条是假的。要它
          用 `ctf_submit_flag` 附一手证据链，否则别提交。
        - `over_quota`：确认数超过配额 → 多半是用户主动下调了 `flag_count`。
          **不当错误**，只提示「按新的 M 算」。
        - `concluded`：主控已收尾 → 别再派工，有异议先撤销收尾。

        **唯一允许的常规进度**（flag-hint，2026-10-21）：多 flag 题已确认 ≥1 条时，
        列出「已确认哪几条（原文 + 题面提示）」。这不是为了汇报进度，是**防重复
        提交**的必需事实——worker 各自在不同线程里干活，看不到别人的台账；不给它
        这份清单，它会把自己已经交过的那条再交一遍，或者继续啃已被覆盖的方向。
        单 flag 题不列（确认即解出，已由硬闸拦，列了纯属噪音）。
        """
        try:
            from core.blackboard.challenge_flags import challenge_state
            st = challenge_state(self.bb, self.project_id, cid)
        except Exception:  # noqa: BLE001 —— 注入失败不阻断解题
            return []
        out: list[str] = []
        quota = int(st.get("quota") or 0)
        got = int(st.get("accepted") or 0)
        cand = int(st.get("candidate") or 0)
        if quota > 1 and got:
            out.extend(self._accepted_flag_lines(cid, got, quota))
        if st.get("conflict"):
            keys = [str((c or {}).get("flag") or "") for c in (st.get("conflict") or [])]
            keys = [k for k in keys if k]
            out.append(
                "- ⚠ **本题已有格式不同的已确认 flag**"
                f"（{' / '.join(keys[:4])}）——**至少一条是错的**。"
                "若你手上的是其中一条，请用 ctf_submit_flag 附**一手证据链**"
                "（source + transform + 可复现命令）再提交；否则不要提交。"
                "两条都像真的时，让代码按前缀判冲突，不要靠感觉挑。")
        if st.get("over_quota"):
            out.append(
                f"- 已确认 {got} 条 > 配额 {quota} 条：多半是配额被下调过。"
                f"按新的 {quota} 条算进度，**不要**把多出来的那条当错报。")
        if st.get("concluded"):
            out.append(
                "- 本题已被主控**收尾**（不再派新 Worker）：别再开新方向；"
                "若你确认还有没做完的面，先 ctf_conclude(concluded=false) 撤销。")
        if cand and not got:
            out.append(
                f"- 本题有 **{cand} 条候选 flag 待人工确认**（候选不计解出）："
                "若你能补上一手证据链，用 ctf_submit_flag 重新提交同一条把它升级；"
                "补不出来就继续找——别停在候选上。")
        return out

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
            "**已测干净**资产不再派发。"
            + ("委派时用 artex_plan(asset_ids=[…]) 把要测的资产划进任务范围"
               "（它同时是覆盖度分母），Planner 会据此派方向——你本线程没有 "
               "call_expert。"
               if self.is_artex else
               "委派时用 call_expert(asset_ids=...) 显式划定"
               "该子专家的资产范围，避免多个子专家重复测试同一资产。"))
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

    def _controller_tool_face(self) -> list[str] | None:
        """控制器工具面：**yaml `tools` 优先**，未设置才回落到代码默认。

        用户语义（2026-10-20 纠错）：设置页「专家池 → 工具白名单」是**可勾选启动**
        的开关，不是只读展示。此前控制器完全不读 yaml 的 `tools`（`is_orchestrator`
        分支直接吃代码常量），于是：

        - 界面上控制器一律显示「全部（未限制）」——因为 yaml 里没这个键；
        - 用户在那儿勾选保存，**对控制器毫无影响**，改的是死字段。

        现在把 `tools` 变成控制器的**真实开关**：
        - yaml 有 `tools` 列表 → 用它（UI 勾了什么就是什么）；
        - 没有 / 为 null → 回落 `controller_tool_face()` 的代码默认值
          （保持既有行为，不因本次改动让老项目突然变宽或变窄）。

        `[]`（显式空）也照用——语义是「一个业务工具都不开」，只留恒放行的
        计划/控制原语。这与 `expert_tool_names` 对子专家的口径一致。

        返回 None = 不是控制器。

        **yaml 的 `tools` 是最终裁决**：勾了就下发、没勾就不下发（schema 与
        执行白名单同步），不再被「按轨强制追加」补回来——否则勾选只是装饰。
        代价是默认值必须自己含齐各轨必需项（见 `controller_tool_face` 的
        `track` 参数说明）。

        **轨道适用性过滤（controller-tool-select）**：`ctf_*` 是 CTF 轨语义，
        非 CTF 轨即便勾了也不下发（yaml 的 `tools` 跨轨共享，用户在 CTF 项目
        勾的 `ctf_submit_flag` 不该出现在 pentest 轨的工具面里）。同理 CTF 轨
        也不下发逆向专属工具——那条由各角色默认面天然不含来保证。
        """
        default = controller_tool_face(self.agent_id, self.track)
        if default is None:
            return None
        try:
            expert = load_expert(self.packs_root, self.agent_id, self.track)
        except FileNotFoundError:
            return _track_filter(default, self.track)
        tools = expert.get("tools")
        if isinstance(tools, list):
            return _track_filter([str(t) for t in tools], self.track)
        return _track_filter(default, self.track)

    def _build_dispatcher(self) -> ToolDispatcher | None:
        """复用 ToolDispatcher 的黑板/技能/网关工具处理器（run_cmd/http/
        kb/skill/bb_* 全套安全机制免费拿）；allowed_tools 按线程角色收敛。"""
        try:
            if self.is_orchestrator:
                # 控制器工具面：**yaml `tools` 优先**（可勾选启动），未设置才
                # 回落各角色的代码默认值。见 `_controller_tool_face` 的注释
                # ——此前控制器完全不读 yaml，设置页勾选对它们毫无影响。
                face = self._controller_tool_face()
                base = tuple(face or ())
                allowed = [t for t in base if t not in _EARLY_ROUTE_TOOLS]
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
            allowed = [t for t in allowed if t not in _EARLY_ROUTE_TOOLS]
            # 探针子任务（激进主控子代理，probe_mode）：执行层与 _tool_specs
            # 同口径剔除 bb_query 与意图三件套——双边同步防止「schema 有、
            # 执行拒」或「执行放行、schema 无」的撕裂。
            if self.probe_mode or self.is_aggressive or self.is_worker or self.intent_free:
                allowed = [t for t in allowed
                           if t not in _PROBE_EXCLUDED_TOOLS]
            # 知识库迭代开关（2026-10-15）：关 = Agent 不提任何提案 → 工具面剔除
            # propose_pack_edit（与 _maybe_kb_iteration/_system_prompt 同口径实时读）；
            # ToolDispatcher 执行层另有一道硬闸，双保险。
            #
            # **开 = 动态追加**（controller-tool-select）：控制器改勾选面裁决后，
            # `propose_pack_edit` 不再在默认面里（它受开关门禁，静态清单表达不了
            # 「看开关」）。只保留上面的「关就剔」会让它永远消失——开关开了也
            # 拿不到，`test_chat_kb_iteration_flag_gates_proposal_toolface` 实测
            # 直接挂。故按 web_search 同款：开就补，与 `_tool_specs` 双边同步。
            #
            # **子代理不追加（2026-10-21 Nan：只有主控能提提案）**：这个「开就补」
            # 是绕过专家 yaml 的硬追加——不排除子代理的话，开关一开所有子专家
            # 都拿到提案工具，与「提案权收归主控」直接矛盾。执行层另有身份硬闸
            # （`ToolDispatcher._is_spawned_subagent`）兜底，但工具面必须一起收口：
            # 模型看得见就会调，调了被拒 = 白烧一步。
            if self._kb_iteration_on() and not self._is_spawned_subagent:
                if "propose_pack_edit" not in allowed:
                    allowed = [*allowed, "propose_pack_edit"]
            else:
                allowed = [t for t in allowed if t != "propose_pack_edit"]
            # 联网搜索开关（v43，web-search-toggle）：线程级，主控开 = 子代理
            # 继承（call_expert/ARTEX Worker 建线程时从父线程带位）。工具面与
            # 执行白名单双边同步（与 probe 剔除同纪律），专家 yaml 无需声明——
            # 这是会话级用户决定，不是专家能力。
            if self._web_search_on() and "web_search" not in allowed:
                allowed = [*allowed, "web_search"]
            # 交付工具（deliver，2026-10-21）：与 `_tool_specs` **双边同步**——
            # schema 有、执行层不在白名单 = 「看得见、一调就被越界拒」，
            # 正是本仓最典型的撕裂形态。只给子 agent（理由同 _tool_specs）。
            if not self.is_orchestrator and "deliver" not in allowed:
                allowed = [*allowed, "deliver"]
            # Worker 间过程级信息交换（v46）：ARTEX Worker 放行 trace 查询三件套。
            if self.artex_intent_id and not self.is_orchestrator:
                for tname in _WORKER_TRACE_TOOLS:
                    if tname not in allowed:
                        allowed = [*allowed, tname]
                # 事实写回（artex-record-fact）：与 schema 追加**双边同步**。
                if RECORD_FACT_TOOL_NAME not in allowed:
                    allowed = [*allowed, RECORD_FACT_TOOL_NAME]
            # CTF flag 通道（ctf-flag-channel）：与 `_tool_specs` 的追加**双边
            # 同步**——只加 schema 不加白名单 = 模型看得见却调不动（越界拒绝），
            # 只加白名单不加 schema = 模型不知道有这工具。
            # **ARTEX 主控除外**（artex-controller-no-exec）：主控不判定 flag。
            # **控制器整段跳过**（controller-tool-select）：控制器已经由 yaml
            # 勾选面装配好 `allowed`，这里再「按轨强制追加」会把勾选变成装饰
            # （用户取消勾选 ctf_conclude，白名单里又被加回来 = 界面在骗人）。
            # 子专家没有勾选面语义变化，照旧按轨追加。
            if self.track == "ctf" and not self.is_artex and not self.is_orchestrator:
                want = [*_CTF_FLAG_TOOLS, *_CTF_ABANDON_TOOLS,
                        *_CTF_CONCLUDE_TOOLS]
                for tname in want:
                    if tname not in allowed:
                        allowed = [*allowed, tname]
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
                recovery=self.recovery,
                # vision 通道（2026-10-19）：模型能否看图由 provider 实例的
                # supports_vision 决定（providers.json `vision: true`）→ 注入工具层
                supports_vision=bool(getattr(self.llm, "supports_vision", False)))
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
            # 与 `_build_dispatcher` **双边同步**：控制器工具面 = yaml `tools`
            # 优先，未设置才回落代码默认（见 `_controller_tool_face`）。
            face = self._controller_tool_face() or []
            names = {t for t in face if t not in _EARLY_ROUTE_TOOLS}
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
            if self.is_orchestrator:
                # 控制器：MCP **同样受勾选面裁决**（controller-mcp-face，
                # 2026-10-20）。此前这段只对子专家按 yaml `mcp_servers` 过滤，
                # 控制器分支无条件全量注入——实测 CTF 主控勾了 16 个工具
                # （13.4K 字符），却被塞进 rea server 的全部 215 个工具
                # （549K 字符 / 约 183K token），单 `compare_web_captures`
                # 一个 schema 就 62K（$defs 里 93 层嵌套结构定义）。
                # 面板「工具定义 156.2K」不是估算失真，是真实下发量。
                # 现在与业务工具同口径：勾了才下发（schema 与 `_dispatch`
                # 的白名单检查天然双边一致——那里也是按同一 face 判）。
                face = set(self._controller_tool_face() or [])
                mcp_specs = [s for s in mcp_specs
                             if mcp_allowed_by_face(str(s.get("name") or ""), face)]
            else:
                # 专家显式勾选 MCP server 时，仅暴露这些 server；缺省沿用全局已启用集合。
                try:
                    mcp_allow = load_expert(self.packs_root, self.agent_id, self.track).get("mcp_servers")
                    if isinstance(mcp_allow, list):
                        allowed_servers = {str(x).strip() for x in mcp_allow if str(x).strip()}
                        mcp_specs = [s for s in mcp_specs
                                     if mcp_server_of(str(s.get("name") or ""))
                                     in allowed_servers]
                except Exception:  # noqa: BLE001
                    log.exception("chat MCP 专家白名单读取失败 thread=%s", self.thread_id)
            specs = [*specs, *mcp_specs]
        # 知识库迭代开关（2026-10-15）：关 = 不下发 propose_pack_edit 的 schema
        # （LLM 看不到工具就不会调；执行层硬闸 + dispatcher 白名单三重保险）。
        # 开 = **动态追加**（controller-tool-select，与 `_build_dispatcher` 双边
        # 同步）：控制器改勾选面裁决后它不在默认面里，只留「关就剔」会让开关
        # 开了也拿不到。按 web_search 同款补上。
        # **子代理不追加（2026-10-21）**：与 `_build_dispatcher` 同口径——
        # 提案权只归主控，子代理既拿不到 schema 也过不了执行闸。
        if self._kb_iteration_on() and not self._is_spawned_subagent:
            if not any(s.get("name") == "propose_pack_edit" for s in specs):
                specs = [*specs, *_specs_by_names({"propose_pack_edit"})]
        else:
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
        # CTF flag 通道（ctf-flag-channel）：按轨追加，不写进专家 yaml。
        # 见 `_CTF_FLAG_TOOLS` 的注释——此前漏了这一步导致 flag 台账在真实
        # ARTEX 执行链上不可达（提示词要求调、工具面没有、一调就被越界拒）。
        #
        # **ARTEX 主控除外**（artex-controller-no-exec）：主控不执行、不判定
        # flag，flag 由 Worker 报。给它这两个工具会诱导它自己去解题（用户实测
        # 主控自己在解题、不派 Worker）。激进主控/探针是执行角色，照给。
        #
        # **控制器整段跳过**（controller-tool-select）：控制器已由 yaml 勾选面
        # 装配好 specs，这里再按轨强制追加会把勾选变成装饰——用户取消勾选
        # `ctf_conclude`，schema 又被加回来，界面与运行期再次脱节。
        # 子专家没有勾选面语义变化，照旧按轨追加。
        if self.track == "ctf" and not self.is_artex and not self.is_orchestrator:
            want = [*_CTF_FLAG_TOOLS, *_CTF_ABANDON_TOOLS, *_CTF_CONCLUDE_TOOLS]
            have = {s.get("name") for s in specs}
            missing = [t for t in want if t not in have]
            if missing:
                specs = [*specs, *_specs_by_names(set(missing))]
        # 交付工具（deliver，2026-10-21 Nan 要求）：**只给子 agent**，硬追加、不写
        # 进专家 yaml——这是「会话语义」而不是专家能力，100+ 个 yaml 逐个加既容易
        # 漏，也会让能力清单变成噪声。
        #
        # 主控不追加：主控的交付对象是人类，直接回一段话即可；而且它的工具面本来
        # 就紧（ARTEX 主控 slim 面有 8000 字符上限），多一个只会挤掉真工具。
        if not self.is_orchestrator and not any(
                s.get("name") == "deliver" for s in specs):
            specs = [*specs, *_specs_by_names({"deliver"})]
        # bb_query 的 what 面按轨裁剪（track-query-split，2026-10-13）：
        # 渗透/逆向互用不到的面不下发——与 AgentSession 同口径（tool_registry 家族表）。
        # ARTEX 主控再收一档（artex-controller-slim，2026-10-20）：它只查
        # events/assets/findings，逆向那四面（func/blueprint/feature_tree/
        # logic_block）与另外十几个参数对它全是噪声。
        if self.is_artex:
            specs = [bb_query_schema_for_artex() if s.get("name") == "bb_query" else s
                     for s in specs]
        else:
            specs = [bb_query_schema_for(self.track) if s.get("name") == "bb_query" else s
                     for s in specs]
        # 收尾阶段（artex-wrapup）：裁成「只能写回」的子集。与 _build_dispatcher
        # 的白名单裁剪**双边同步**。放最后，确保裁掉上面所有分支刚追加的工具。
        if getattr(self, "_wrapup_active", False):
            specs = filter_wrapup_tools(specs)
        return specs

    def _orch_tool_specs(self) -> list[dict[str, Any]]:
        """主控专属工具规格：todo_write + call_expert（专家清单动态生成）。

        ARTEX 主控**不给 call_expert**（artex-plan-only，2026-10-19 事故）：
        派活只能走 artex_plan（Planner → Engine → Worker）。给第二条旁路的后
        果是实测过的——主控直接 call_expert 派一个专家，绕过了 Planner，
        于是**目标、探索图、任务范围全空**，而那条 Intent 的 ``task_id`` 是
        线程 id 拼出来的假任务。此处与 ``_build_dispatcher`` 的
        「``allowed`` 剔除 call_expert」**双边同步**：只剔一边会撕裂成
        「schema 有、执行拒」（模型看得见却调不动，白烧步数）。

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
        # todo_write / call_expert 是否下发，**以 yaml `tools` 勾选为准**
        # （controller-tool-select，2026-10-20）：用户要的是「勾选启动」，
        # 所以这两个也不该由代码按角色硬性决定。`face` 就是 yaml 勾选结果
        # （未设置时是代码默认值，默认值里 ARTEX 主控不含这两个）。
        face = set(self._controller_tool_face() or ())
        specs: list[dict[str, Any]] = [
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
        ] if "todo_write" in face else []
        # call_expert 同理：勾了就挂，没勾就不挂。
        if "call_expert" in face:
            specs.append({
                "name": "call_expert",
                "description": "委派专家池专家执行一项任务：spawn 隔离的专家线程"
                               "（持久留档，人类可打开追问），专家跑完返回摘要。"
                               "相互独立的任务可在同一轮并发调用；有依赖的任务按顺序委派。"
                               "任务描述必须自包含（目标/范围/已知"
                               "信息/期望交付物）。你在委派前不直接实操——实操是"
                               "专家的事。（expert 只填下方专家 id，勿填技能名；"
                               "测试类任务用 asset_ids 把该专家负责的资产 id 明确划给它）"
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
                        # enum（2026-10-01）：模型曾把技能名 资产侦察手法 当专家名
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
            })
        # 续跑注入（ask-expert，2026-10-21 Nan 要求）：勾了就挂。
        # **串行**工具（不进 `_parallel_expert_dispatch`）：它写的是已有线程的
        # 上下文，同轮并发两条 ask 打同一条线程会互相踩历史。
        if ASK_EXPERT_TOOL in face:
            specs.append({
                "name": ASK_EXPERT_TOOL,
                "description":
                    "给**已派发过**的专家线程补一条消息，让它带着原上下文接着跑，"
                    "返回它的新答复。\n"
                    "用途（这两件事 call_expert 做不到）：\n"
                    "① **追问细节**：专家摘要太粗，问它「你刚才是怎么判定"
                    "那个入口无效的」「把 payload 构造再写具体点」——它还记得"
                    "自己跑过什么，不必从头再试。\n"
                    "② **问清可沉淀的内容**：问它「这轮有什么判据/坑值得写进手册」"
                    "「哪份手册哪一段跟实际不符」——**提案由你自己提**"
                    "（子代理没有提案权，只有主控能提），所以要把细节问够，"
                    "否则你只能凭一句摘要猜。\n"
                    "边界：thread_id 只填你**自己派发过**的专家线程（见 "
                    "call_expert 返回值里的「专家线程 chat-xxx」）。**要新任务/新"
                    "方向用 call_expert**，不要拿本工具当第二次派发——它不换专家、"
                    "不换上下文。同一条线程可多次追问。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "thread_id": {
                            "type": "string",
                            "description": "要续跑的专家线程 id（形如 chat-xxxxxxxx，"
                                           "取自 call_expert 的返回）",
                        },
                        "message": {
                            "type": "string",
                            "description": "要追加给该专家的消息（自包含：说清"
                                           "你要什么、为什么需要）",
                        },
                    },
                    "required": ["thread_id", "message"],
                },
            })
        # artex_* / ctf_* 这些**控制器专属**工具同样按勾选下发
        # （controller-tool-select，2026-10-20）。它们不在 AGENT_TOOLS 里
        # （走专用 dispatch 分支、无 REGISTRY 条目），故 `_specs_by_names`
        # 捞不到，必须在这里按名字过滤。
        if self.is_artex:
            specs = [*specs,
                     *[s for s in self._artex_tool_specs()
                       if s.get("name") in face]]
        if self.is_ctf_controller:
            # CTF 解题主控的三个调度原语（ctf-controller，2026-10-19；
            # ctf_inspect 2026-10-20）。执行层由 `_dispatch` 的专用分支处理，
            # 不经 ToolDispatcher。
            for spec in (_CTF_DISPATCH_TOOL_SPEC, _CTF_REVIEW_TOOL_SPEC,
                         _CTF_INSPECT_TOOL_SPEC):
                if spec.get("name") in face:
                    specs = [*specs, spec]
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
                        "new_task_confirmed": {"type": "boolean",
                                                "description": "明确切换到新任务时设为 true；CTF 当前题目未解时必需"},
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
                               "剩余 pending；resume 把 paused 放回 Frontier；retry 重开单个"
                               "失败/耗尽/阻塞 Intent；cancel 取消 Intent（给 task_id 则取消该任务全部"
                               "pending/paused）；kill 终止单个运行中 Worker。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["start", "drain", "stop", "pause",
                                            "resume", "retry", "cancel", "kill"]},
                        "task_id": {"type": "string", "description": "留空取活跃任务"},
                        "intent_id": {"type": "string",
                                      "description": "kill / retry / 单条 cancel 必填；retry 仅支持 failed/exhausted/blocked"},
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
        # 技能清单**紧跟角色、先于工作方式**（skill-first，2026-10-21）：见
        # _skill_block() 的注释——原先放在提示最末尾，模型读到它时行为已被前面
        # 的纪律段定型，实测几乎不 skill_open。
        sk_block = self._skill_block()
        if sk_block:
            parts.append(sk_block)
        if self.is_orchestrator:
            if self.is_aggressive:
                fctx = self._workspace_files_context()
                if fctx:
                    parts.append(fctx)
                # 激进主控：探针海 + 先返先触发（与 chat-orchestrator 的
                # 蛙式排队互斥，二选一注入）
                parts.append(_AGGRESSIVE_CONTROLLER_STYLE)
            elif self.is_ctf_controller:
                # CTF 解题主控：代码调度 + LLM 只定方向（ctf-controller，
                # 2026-10-19）。与激进主控同样需要工作区文件上下文（题目附件
                # 由用户丢进输入框，主控要能看见有哪些文件可派）。
                fctx = self._workspace_files_context()
                if fctx:
                    parts.append(fctx)
                parts.append(_CTF_CONTROLLER_STYLE)
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
                    "不直接执行扫描/利用等实操。\n"
                    "**摘要不够细时用 ask_expert 回到原线程追问**（2026-10-21）："
                    "call_expert 返回里有「专家线程 chat-xxx」，把它和你的问题传给 "
                    "ask_expert，那个专家会带着自己跑过的全过程接着回答——"
                    "问它「凭什么判定这条路不通」「把 payload 构造写具体」都行。"
                    "**不要为了问细节重新 call_expert**：新线程从零开始，"
                    "原来那轮的观察全丢，还得把活重干一遍。"
                    "**提案只能由你提**：子代理没有提案权（只有主控能提），"
                    "所以沉淀细节要靠 ask_expert 问够，再自己写 propose_pack_edit。")
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
        # 经验沉淀纪律（kb-iteration，2026-10-14）。
        # **2026-10-21 Nan 改为「只有主控能提提案」**：子代理（call_expert 派生，
        # 有 parent_thread_id）不再注入本段、执行层另有一道身份硬闸
        # （`ToolDispatcher._is_spawned_subagent`）。原因不是子代理没证据——它
        # 恰恰有一手证据；问题是它**只看得到自己那一段**：不知道别的方向试过
        # 什么、也不知道主控怎么收敛，提上来的常是「我这个方向该写进手册」，
        # 全局看可能根本不值得写（方向本身就是死路）。提案权收归主控，它看得到
        # 全部子线程摘要，判断「什么值得沉淀」才准。
        #
        # 子代理的贡献路径：把判据/坑写进**收尾摘要**，主控用 `ask_expert`
        # 追问细节后统一提。
        #
        # **受项目级开关门禁（2026-10-15）**：关闭时整段不注入 + 工具面剔除
        # （_tool_specs/_build_dispatcher）——开关关 = Agent 不提任何提案，提示也不诱导。
        # **ARTEX 主控除外**（artex-controller-slim，2026-10-20）：它的工具面里已经
        # 没有 propose_pack_edit（slim 只留观察+操舵+登记），而这里只看开关不看角色
        # → 开关一开就注入「用 propose_pack_edit 提提案」，模型调了必然撞越界拒绝，
        # 白烧步数。**提示词必须与工具面同口径**，这是第三处需要同步的地方。
        if self._kb_iteration_on() and not (
                self.is_aggressive or self.probe_mode or self.is_worker
                or self.is_artex or self._is_spawned_subagent):
            parts.append(
                "## 经验沉淀（可选）\n"
                "发现知识库/技能文档**内容有误**、**文档缺失**、某手法**已在本轮验证有效**，"
                "或产出**新的 Web 漏洞**时，可用 propose_pack_edit 提变更提案（kb 文档与技能的"
                "新建/改写/删除，删除应用时进回收站）——只落 pending 待人类审批，**绝不自行改"
                "文件**。reason 必须附本轮证据（引用的文档 / 失败的工具 / 产出的发现 id）；"
                "每会话最多 20 条，禁止凑数。任务较难、试错较多时也值得回看是否有可沉淀的坑。\n"
                "**两个明确的「该停下来想想沉淀」时点**（2026-10-21）：\n"
                "① **派发满 3 轮**——注意「一轮」= 你的一次并发派发决策，同轮并发派 5 个"
                "专家仍只算 1 轮。满 3 轮意味着你已经换过三次方向还没打通，此时先想"
                "「是不是缺判据/手册写错了」，而不是继续盲目派第四轮。\n"
                "② **本轮耗时超过 30 分钟**——久攻不下同理。\n"
                "这两个时点不强制你提提案（可能确实是题难），但**必须停下来评估一次**："
                "有可沉淀的就提，没有就说明为什么没有。\n"
                "**提 kb 提案前先读下面「提案写作格式」**——格式不对会被机械质检直接拒。"
                "格式就在本提示里，不必先去检索。")
            # 写作规范（2026-10-21 Nan 报 bug）：模型拿不到模板只能反复空搜。
            # 根因见 core/skills/kspec.py 模块注释（规范在 packs/kb 根目录 → 三条读路全断）。
            # **只在持有 propose_pack_edit 时注入**，与工具面同口径。
            parts.append(kspec.spec_block())
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
        # 看图纪律（vision 通道，2026-10-19）：**只有模型真能看图时才注入**——
        # 不能看图却被告知「你有眼睛」，模型会反复 view_image 拿到同一句
        # 「未声明视觉能力」，白烧步数。反过来，能看图却不告诉它，它会按知识库
        # 惯性一路 tesseract/zbarimg（用户实测的真问题：图明明能看却老是 OCR）。
        vctx = self._vision_context()
        if vctx:
            parts.append(vctx)
        # 技能清单**已前移到「工作方式」之前**（skill-first，2026-10-21），
        # 见 _skill_block()。这里不再追加。
        return "\n\n".join(parts)

    # ---------- 技能清单注入（skill-first，2026-10-21） ----------

    def _skill_block(self) -> str:
        """本轮可用技能清单 + **动手前必读硬规则**。

        为什么独立成方法并前移（2026-10-21 用户实测问题）：技能清单原先追加在
        system 提示的**最末尾**（实测 4707 字里的第 4488 字，即倒数 5%）。而这份
        提示前面全是「场景红线 → 角色 → 工作方式 → 委派上下文（一堆资产 id /
        维度清单）→ 沉淀纪律 → CTF 上下文」——模型读到技能清单时行为模式已经被
        前面的「plan→execute」定型了，于是 `run_cmd` 连失败 24 次也不回头查。
        实测该项目全生命周期只有 1 次 skill_open、2 次 kb_search。

        两处修正：
        1. **位置**：紧跟角色之后、工作方式之前（首尾强、中间弱，模型对开头段
           的遵循度显著高于末尾）。
        2. **措辞**：从「需要方法细节时」（软条件，模型永远觉得自己不需要）改成
           可判定的硬规则——「命中当前介质/题型的技能，第一步必须 skill_open，
           不要先跑命令再回头找方法」。

        同时上报 `skill.routed`（ChatTurn 侧此前完全没发，导致诊断页「路由零命中」
        统计恒为空、直播间也看不到技能路由）。payload 与 `loop.py::skill_context_for`
        同口径（injected + count），另带 source=chat 区分链路。
        """
        if SkillRegistry is None or self.is_aggressive or self.probe_mode or self.is_worker:
            return ""
        try:
            expert = load_expert(self.packs_root, self.agent_id, self.track)
        except Exception:  # noqa: BLE001 —— 专家读取失败时按全量（与 _system_prompt 同口径）
            expert = {}
        try:
            reg = SkillRegistry(self.packs_root)
            reg.load()
            # None=全量，[]/空集合=明确不加载；保留三态语义。
            role_skills = expert.get("skills") if "skills" in expert else None
            enabled = [s for s in reg.all()
                       if s.enabled and (role_skills is None or s.name in role_skills)]
        except Exception:  # noqa: BLE001 —— 技能清单失败不阻断对话
            log.exception("chat 技能清单加载失败 thread=%s", self.thread_id)
            return ""
        if not enabled:
            return ""
        # 审计事件（best-effort：统计失败不能拖垮提示构建）
        try:
            self.bb.append_event(
                self.project_id, "skill.routed",
                {"name": None, "injected": [s.name for s in enabled],
                 "count": len(enabled), "source": "chat",
                 "agent_id": self.agent_id, "task_id": None},
                session_id=self.thread_id, author=self.agent_id)
        except Exception:  # noqa: BLE001
            log.exception("skill.routed 事件失败 thread=%s", self.thread_id)
        names = [s.name for s in enabled]
        lines = "\n".join(f"- {s.name}：{s.description or ''}" for s in enabled)
        # 派活指引按线程类型分叉（沿用原有消歧文案：技能名 vs 专家名）
        dispatch_hint = ("本线程无 call_expert，派活走 artex_plan"
                         if self.is_artex else
                         "委派专家请用 call_expert 并填专家 id")
        return (
            "## 技能清单与打开时机（动手前必读）\n"
            f"下列是【技能名】，不是专家——{dispatch_hint}。\n"
            f"{lines}\n\n"
            "**硬规则**：\n"
            "1. 收到任务先看上面的清单，判断有没有命中你手上这种介质/题型的技能；\n"
            f"   命中就先 skill_open(\"<技能名>\") 读正文，**再**开始 run_cmd / 写脚本。\n"
            "   不要先跑一堆命令、卡住了才回头找方法。\n"
            "2. **读技能之后、动手之前，先 kb_search 一次**——查「载体 + 想做的事」，\n"
            "   例如 kb_search(\"upx 脱壳\")、kb_search(\"zip 逆向 入口点\")、\n"
            "   kb_search(\"pcap 提取 凭据\")、kb_search(\"图片 隐写 LSB\")。\n"
            "   技能给的是打法纪律，知识库给的是**判据/命令/payload/踩坑记录**；\n"
            "   命中就 kb_open 读正文，别凭记忆拼参数。\n"
            "   一次没命中就换个说法再查一次（中文/英文、术语/现象各试一次），\n"
            "   两次都不对口才转自己探索——**不要因为一次零命中就再也不查**。\n"
            "3. 已知测试点名称时，route_lookup 可直接定位到对应模块。\n"
        )

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
        截断本轮 messages 而 DB 存全量，下轮仍会读回巨物 → input 再爆。

        **带图结果（ToolOutput，vision 通道 2026-10-19）**：未超限时**原样返回
        对象**（保住 ``.images``，内存里的 tool_result 块才带得上图）；超限时退回
        普通 str——但那种结果本来就不可能带图（图片结果文本只有一两百字），
        真超限说明是别的大文本，截断无妨。"""
        n = len(text)
        if n <= _MAX_TOOL_RESULT_CHARS:
            return text, ""
        path = ""
        if self.artifacts_dir is not None:
            try:
                d = Path(self.artifacts_dir) / "chat-results"
                d.mkdir(parents=True, exist_ok=True)
                fp = d / f"{self.thread_id}-{tc.id}.txt"
                fp.write_text(str(text), encoding="utf-8")
                path = str(fp)
            except Exception:  # noqa: BLE001 —— 落盘失败不阻断（保截断版）
                log.exception("chat 工具结果落盘失败 thread=%s", self.thread_id)
        head = str(text)[:_MAX_TOOL_RESULT_CHARS]
        tail = (f"\n\n…（结果过长已截断：原文 {n} 字符，本条仅保留前 "
                f"{_MAX_TOOL_RESULT_CHARS} 字符；完整结果见"
                f"{path or '事件流'}）")
        return head + tail, path

    @staticmethod
    def _db_text(result: Any) -> str:
        """工具结果 → 落库文本（**绝不含 base64**，vision 通道 2026-10-19）。

        chat_messages.content 是 TEXT 列，base64 塞进去既撑爆库也毫无用处——
        下一轮 _load_history 只回放文本。故带图结果落库时追加一行标记，让模型
        知道「这条结果原本带图，历史回放里没有像素」，需要再看就重新 view_image。
        这样图不会静默消失（那会让模型以为「我明明看过却想不起来」）。
        """
        text = str(result)
        imgs = getattr(result, "images", None) or []
        if not imgs:
            return text
        return (f"{text}\n（本条结果附带 {len(imgs)} 张图片；历史回放只保留文字、"
                "不含像素——需要再看请重新 view_image 该文件）")

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
                                       on_thinking, on_provider_retry,
                                       on_failover=on_retry)
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
                   on_thinking=None, on_provider_retry=None,
                   on_failover=None):
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
            # 跨供应商兜底（provider-failover，2026-10-21）：换家前清掉上游增量
            # 缓冲（复用 on_retry=_reset_delta，语义就是「从零重流」）——不清会拼出
            # 「失败那家的半截 + 新家的全文」。只有挂了兜底链的 provider 才收该参数。
            if on_failover is not None:
                try:
                    supports_failover = "on_failover" in inspect.signature(
                        self.llm.chat).parameters
                except (TypeError, ValueError):
                    supports_failover = False
                if supports_failover:
                    kwargs["on_failover"] = lambda _info=None: on_failover()
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
        # 派发轮次与耗时的本轮基线（2026-10-21 Nan 要求）：
        # - `_round_start_msg_id`：本轮开始时的最大消息 id。轮末只数这之后落的
        #   assistant 消息（一条 = 一次并发派发决策 = 一轮），不把历史轮次的派发
        #   算进来。
        # - `_round_start_ts`：墙钟起点，轮末算 elapsed。
        try:
            self._round_start_msg_id = self.bb.conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM chat_messages WHERE thread_id=?",
                (self.thread_id,)).fetchone()[0]
        except Exception:  # noqa: BLE001
            self._round_start_msg_id = 0
        try:
            import time as _time
            self._round_start_ts = _time.monotonic()
        except Exception:  # noqa: BLE001
            self._round_start_ts = None
        try:
            final = self._loop(text)
            final = self._compose_final(final)
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
        except chat_store.ThreadGone:
            # 人类在轮次跑到一半时把这条线程删了（2026-10-19 事故）：不是故障，
            # 是「这条线程不要了」。线程行已不存在 → 无处落消息、也不必归位状态
            # （update_thread 也写不进不存在的行）。安静退出，返回一句说明让调用
            # 方（call_expert 的 spawn 方 / 工作台后台任务）按中止处理。
            log.info("chat 线程已被删除，本轮安静退出 thread=%s", self.thread_id)
            return "（本条线程已被删除，本轮中止；未产生新的消息记录）"
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
        finally:
            # 通知箱释放必须在**所有**出口执行（正常/删除/中止/异常）：登记表里
            # 「有箱」是投递方的存活判据，漏释放 = 死线程永久被当活轮次。
            self._release_notice_box()

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

    def _round_elapsed_s(self) -> float:
        """本轮已跑秒数（`run()` 里用 monotonic 记的起点）。

        用 monotonic 而非 wall clock：系统时间被校正时 wall clock 会跳，
        跳出来的「耗时」既可能是负的也可能是几小时——这条信号会直接决定
        要不要提提案，不能受时钟同步影响。取不到起点（旧实例/测试壳）→ 0。
        """
        start = getattr(self, "_round_start_ts", None)
        if start is None:
            return 0.0
        try:
            import time as _time
            return max(0.0, _time.monotonic() - float(start))
        except Exception:  # noqa: BLE001
            return 0.0

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
            since_iso=getattr(self, "_round_start_iso", None),
            # 派发轮次（2026-10-21）：只数**本轮**落的 assistant 消息里带
            # call_expert 的条数——一条消息 = 一次并发派发决策 = 一轮。
            since_message_id=int(getattr(self, "_round_start_msg_id", 0) or 0),
            # 耗时：本轮起点墙钟到现在的秒数（起点在 run() 里取）。
            elapsed_s=self._round_elapsed_s())
        try:
            threading.Thread(
                target=lambda: kbiteration.run_iteration(
                    self.bb, self.project_id, **args),
                name=f"kb-iter-{self.thread_id[-8:]}", daemon=True).start()
        except Exception:  # noqa: BLE001 —— 起线程失败也不影响会话
            log.exception("kb-iteration 线程启动失败 thread=%s", self.thread_id)

    def _compose_final(self, final: str) -> str:
        """本轮返回值 = **最后一次交付** + 收尾说明（deliver，2026-10-21）。

        组装逻辑与任务链共用 `core.agent.tools.compose_delivery_summary`——两处
        各写一份必然漂移，同一次交付在 `call_expert` 与直播间就会长得不一样。
        """
        try:
            from core.agent.tools import compose_delivery_summary
            return compose_delivery_summary(
                getattr(self._dispatcher, "deliveries", None), final)
        except Exception:  # noqa: BLE001 —— 组装失败退回原文，绝不炸整轮
            log.exception("chat 收尾组装失败 thread=%s", self.thread_id)
            return final

    def _loop(self, user_text: str) -> str:
        """入口包装：整轮标记为本线程会话作用域（请求体留痕归属，2026-10-17）。"""
        from core.llm.dump import scope as dump_scope
        with dump_scope(self.project_id, self.thread_id):
            return self._loop_turn(user_text)

    def _loop_turn(self, user_text: str) -> str:
        # 每轮复位技能提醒状态（用户新提一轮 = 新任务，上一轮读过技能不代表这轮读过）
        self._reset_skill_nudge()
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
        pseudo_tc_nudges = 0  # 伪工具调用（特殊 token 泄漏进正文）推回数（2026-10-20）
        reasoning_nudges = 0  # 推理膨胀推回数（2026-10-09）
        text_acc: list[str] = []
        for _step in range(max_steps):
            if self._aborted():  # 步间中止：不落新 LLM 调用
                final = self._persist_stopped()
                break
            # 外部通知（chat-inbox，2026-10-20）：在**步边界**取一次，作为 user
            # 消息并入上下文，让模型自己决定怎么收尾。
            #
            # 为什么不直接 abort：人工确认 flag 后要让主控「尽快收工并总结」，
            # 而不是被掐在半个工具调用里——`_persist_stopped` 只落一句「已按人类
            # 要求停止」，模型没有任何机会写回手上已识别的结论。这里给的是
            # 「情况变了」，收尾动作仍由模型完成（它知道自己在哪个方向、拿到什么）。
            #
            # 只在步边界取（不在流式帧里）：注入必须落在两次 LLM 请求之间，
            # 中途改 messages 会让当前这次请求的半截响应失去对应上下文。
            for _notice in self._take_notices():
                # **必须走 `_inject_nudge`，不能裸 append**：步边界上 messages
                # 尾部通常是 user（tool_result 块），再 append 一条 user 就是连续
                # 两条 user——Anthropic 严格角色交替会直接 400 拒收（本仓既有坑，
                # 见 `_inject_nudge` 的注释）。
                _inject_nudge(messages, _notice)
                self._emit("chat.notice", {"text": _notice[:1500]})
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
            pub = {"chars": 0, "seq": 0, "at": 0.0,
                   "leak": False}  # leak：本步正文已判为伪工具调用，停发增量
            thinking_acc: list[str] = []
            thinking_pub = {"chars": 0, "seq": 0, "at": 0.0}
            thinking_started = time.monotonic()

            def on_text(delta: str, _acc=acc, _pub=pub) -> None:
                _acc.append(delta)
                text = "".join(_acc)
                # 伪工具调用闸（2026-10-20 事故）：正文里出现 `<｜｜DSML｜｜ …>`
                # 这类特殊 token 时**停止上屏**——chat.delta 携带累计全文，一旦
                # 继续发就会把整段标记反复重发刷屏。闸死后本步不再发布，终稿守卫
                # 负责剥掉标记并推回模型用工具通道重发。尾部恰好是标记前缀时也
                # 先压住这一拍（下一块到了再判），免得「半截 `<｜｜`」闪一下。
                if not pub["leak"] and has_pseudo_tool_call(text):
                    pub["leak"] = True
                    _pub["chars"] = len(text)
                if pub["leak"] or has_marker_prefix_tail(text):
                    return
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
                _pub["leak"] = False
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
            est_tools = _est_tokens(json.dumps(step_tools or [], ensure_ascii=False))
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
                    # 落库只存文本（DB 是 TEXT 列，base64 会撑爆且回放用不上）；
                    # 内存里的块保留 ToolOutput → 本轮下一步模型能真正看到图。
                    chat_store.append_message(
                        self.bb, self.thread_id, "tool", self._db_text(result),
                        tool_use_id=tc.id)
                    tool_result_blocks.append({
                        "type": "tool_result", "tool_use_id": tc.id,
                        "content": tool_result_content(result)})
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
                if self.is_first_return_controller or probe_pending:
                    # 先返先触发：把窗口内后台完成的并发结果并进下一轮 LLM
                    # 请求（skip=本批已作为正式 tool_result 交回的 id，防双落盘）
                    #
                    # `or probe_pending`（2026-10-21）：人类插话也能让主控提前
                    # 返回，此时非 first_return 主控同样有专家在后台跑。它们补了
                    # `_PROBE_PENDING` 占位，真实结果必须靠这里补达——否则占位
                    # 永远停在「仍在后台执行」，主控再也看不到真实结论。
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
            # 伪工具调用守卫（2026-10-20 事故）：正文里出现 `<｜｜DSML｜｜ calls>`
            # 这类**工具调用特殊 token** = 网关没把它解析进 tool_calls，而是漏进了
            # content。旧行为是「text 非空 → 纯文本终稿 → 落库 + break」，线程以
            # status=idle / error='' 收场——系统认为一切正常，用户只看到一坨标记
            # 吐在屏幕上对话就停了（hxbai 实测，全仓 7 条污染行）。
            # 处置：① 剥掉标记，绝不落库（否则回放会教模型模仿该格式）；② 推回
            # 要求改用工具通道重发；③ 剥完仍有实质正文 → 直接用剩余正文收轮（不
            # 白烧一轮）。末步 `step_tools=None` 时模型手上没有工具，推回也调不出来
            # → 直接落明确文案，不重试。超上限同样落明确文案（**不能静默收轮**）。
            if text.strip() and has_pseudo_tool_call(text):
                clean = strip_pseudo_tool_calls(text)
                can_retry = (pseudo_tc_nudges < PSEUDO_TC_MAX
                             and step_tools is not None)
                self._emit("chat.pseudo_toolcall", {
                    "phase": "nudge" if can_retry else "giveup",
                    "chars": len(text), "clean_chars": len(clean),
                    "nudges": pseudo_tc_nudges, "total": PSEUDO_TC_MAX})
                if clean:
                    # 正文里除调用块外还有真内容（如「先看下黑板」+ 调用块）：
                    # 用剥净后的文本继续走终稿流程，不再多烧一轮。
                    text = clean
                elif can_retry:
                    pseudo_tc_nudges += 1
                    # 清掉本步已上屏的前缀（标记出现前可能已发过「好的，」这类
                    # 片段）：发一条空累计全文让前端整段覆盖，否则那半句会挂在
                    # 时间线尾部直到下一轮流式覆盖它。
                    pub["seq"] += 1
                    self._emit("chat.delta", {"text": "", "seq": pub["seq"]})
                    _inject_nudge(messages, "\n\n".join(
                        [PSEUDO_TC_NUDGE, *step_nudges]))
                    continue
                else:
                    text = PSEUDO_TC_FINAL_TEXT
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
                if self.track == "ctf":
                    # CTF 轨的收尾出口与渗透不同（ctf-intent-discipline，
                    # 2026-10-20）：不要求 dead_end 背书，也不引导去标
                    # tested_clean（那会撞门禁）。按实际结果给三条路。
                    nudge = ("你还有未收尾的意图：" + ", ".join(open_ids)
                             + "。按实际情况选一个收尾——① 已解出："
                             "close_intent(outcome=\"finding\", finding_ids=[flag 留痕发现])"
                             "（CTF 轨不校验 vuln/intel 类别，题目级并入可能让该 id 实际是 "
                             "category=vuln，照样传即可、别改 outcome=vuln）；"
                             "② 这条路不通但题还能解：close_intent(outcome=\"dead_end\", "
                             "dead_reason=卡点)（**不必**凑 evidence_refs）；"
                             "③ 整道题放弃：ctf_abandon(reason=...)。"
                             "不要用 bb_asset_status 标 tested_clean——CTF 轨没有这个概念。"
                             "收尾后再给最终摘要。")
                else:
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

    # ---------- 技能先行提醒（skill-first nudge，2026-10-21） ----------

    #: 触发提醒的「实质动作」工具组（执行 + 文件）。知识组（skill_open/kb_*/
    #: route_lookup）本身就是查阅动作，不提醒。
    _SKILL_NUDGE_GROUPS = ("执行", "文件")

    #: 查阅类工具：调用它们本身就是「先查再做」的动作，不触发提醒。
    _LOOKUP_TOOLS = ("skill_open", "kb_search", "kb_open", "route_lookup")

    def _reset_skill_nudge(self) -> None:
        """每轮复位提醒状态（**实例属性**，不能写类属性——类属性会在所有线程
        实例间共享，A 线程 skill_open 过会让 B 线程永远收不到提醒）。"""
        self._skill_nudge_sent = False
        self._skill_opened_round = set()
        self._kb_nudge_sent = False
        self._kb_searched_round = False

    def _skill_first_nudge(self, name: str) -> str:
        """本轮首次实质动作前缺哪一步，就在回执尾部追加哪一句（否则空串）。

        **两段式**（2026-10-21 二期）：实测技能清单前置后模型会 skill_open 了
        （1 次），但仍然**从不查知识库**——某 CTF 项目 151 次工具调用里
        `kb_search` 0 次、`run_cmd` 130 次。原因是技能正文给的是「流程」，
        读完照样照着流程跑命令，没有「去查具体判据」这一步。所以提醒拆成两级：
        - 缺技能 → 提技能（一期）；
        - 技能已读但没查库 → 只提知识库（二期，本轮新增）。

        两级各自只提醒一次（`_skill_nudge_sent` / `_kb_nudge_sent`）。**软闸语义**：
        不阻断、不重试，只把提醒并进这次 tool_result 尾部。硬拒会误伤（`file`
        一把就有结论的任务确实不需要查库），且会白烧一轮。

        为什么不阻断：这是「提示注意力」问题，不是「规则缺失」问题——模型不是
        故意违反，是没意识到该查。把话说在它刚要动手的那一屏，成本最低。
        """
        if name in self._LOOKUP_TOOLS:
            return ""          # 查阅动作本身：不提醒
        try:
            from core.agent.tool_registry import agent_tool_group
            if agent_tool_group(name) not in self._SKILL_NUDGE_GROUPS:
                return ""
        except Exception:  # noqa: BLE001 —— 分组查不到就不提醒（保守）
            return ""
        parts: list[str] = []
        # 一级：还没读过技能
        if not self._skill_opened_round and not getattr(
                self, "_skill_nudge_sent", False):
            names = self._skill_block_names()
            if names:      # 只有真配了技能的线程才提醒（激进主控/probe 无技能面）
                self._skill_nudge_sent = True
                parts.append(
                    f"[提示] 你还没打开过技能手册就动手了。本线程可用技能："
                    f"{'、'.join(names)}。\n"
                    "若上面这份清单里有覆盖当前介质/题型的技能，建议先 "
                    "skill_open 读正文再继续。")
        # 二级：技能已读（或本轮没技能可读）但仍没查知识库
        if not getattr(self, "_kb_searched_round", False) and not getattr(
                self, "_kb_nudge_sent", False):
            self._kb_nudge_sent = True
            parts.append(
                "[提示] 本轮还没查过知识库。知识库里是历史沉淀的**具体判据、"
                "命令、payload 和踩坑记录**，比凭记忆拼参数可靠。\n"
                "动手前建议先 kb_search 一次，查「载体 + 想做的事」，例如 "
                "kb_search(\"upx 脱壳\")、kb_search(\"zip 逆向 入口点\")、"
                "kb_search(\"pcap 提取 凭据\")；命中就 kb_open 读正文。\n"
                "一次零命中可以换个说法再查一次；确实不对口就继续自己做。")
        if not parts:
            return ""
        return "\n\n---\n" + "\n".join(parts)

    def _skill_block_names(self) -> list[str]:
        """本线程技能名清单（供提醒用；与 _skill_block 同口径，失败返空）。"""
        if SkillRegistry is None or self.is_aggressive or self.probe_mode or self.is_worker:
            return []
        try:
            expert = load_expert(self.packs_root, self.agent_id, self.track)
            reg = SkillRegistry(self.packs_root)
            reg.load()
            role_skills = expert.get("skills") if "skills" in expert else None
            return [s.name for s in reg.all()
                    if s.enabled and (role_skills is None or s.name in role_skills)]
        except Exception:  # noqa: BLE001
            return []

    def _dispatch(self, tc) -> tuple[bool, str]:
        """技能先行提醒包装器（skill-first nudge，2026-10-21）。

        **为什么包一层而不是改内部 4 处 return**：`_dispatch` 内部有 4 条返回
        路径（ctf_* 分支 / artex 分支 / trace 分支 / dispatcher 分支）加异常
        兜底，逐处追加提醒既易漏又难维护。这里统一在出口处补一次。

        提醒只追加到**成功**结果尾部（失败文本本身已带 [错误]，再拼提醒会干扰
        模型对错误的判读）。`skill_open` 成功即记入本轮已读集合，后续不再提醒。
        """
        ok, result = self._dispatch_inner(tc)
        name = getattr(tc, "name", "") or ""
        if ok and name == "skill_open":
            self._skill_opened_round.add(str((tc.arguments or {}).get("name") or ""))
            return ok, result
        if ok and name == "kb_search":
            # 零命中也算「查过了」：模型履行了「先查再做」，提醒的目标已达成。
            # 若按「有命中才算」判定，检索质量差时会反复提醒，反而像在逼它重试。
            self._kb_searched_round = True
            return ok, result
        if ok:
            nudge = self._skill_first_nudge(name)
            if nudge:
                result = f"{result}{nudge}"
        return ok, result

    def _dispatch_inner(self, tc) -> tuple[bool, str]:
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
            # 控制器执行侧闸（controller-tool-select，2026-10-20）：
            # **按 yaml 勾选面判**，而不是按写死的黑名单判。
            #
            # 为什么必须有这道闸：撤 schema 挡不住执行——执行侧有三条旁路
            # （`_dispatch` 早拦截分支根本不经 ToolDispatcher；`_PLAN_TOOLS` /
            # `_CONTROL_TOOLS` 在 tools.py:572 恒放行；收尾白名单「其余放行」）。
            # 实测未加闸时 `todo_write` 带完整参数直调会真的覆写线程待办。
            #
            # 为什么按勾选面判而不是黑名单：用户要的是「**勾选启动**」——
            # 勾了就该能调、没勾就该被拒。写死黑名单会把勾选变成装饰。
            # 勾选面 = yaml `tools`（未设置时回落各角色代码默认值）。
            if self.is_orchestrator:
                # ① **结构性禁令先判**（勾选面判据之前）：这类工具是「勾了也不对」
                #    ——ARTEX 主控的意图链/待办/委派会绕过 Planner 自建链路。
                #    必须先于下面 ② 的恒放行地板，否则 `declare_intent` 会被
                #    `_PLAN_TOOLS` 恒放行救回来（实测从「被拒」变「放行」）。
                denied = controller_structural_denied(self.agent_id)
                if name in denied:
                    r = (f"[错误] {name} 不适用于 ARTEX 主控：它的角色是纯操舵——"
                         "Intent 由 Planner 独占生成、待办由 artex_plan 的 todos "
                         "承载、派活只能走 artex_plan。该走的通道：派方向用 "
                         "artex_plan / 观察用 artex_status 与 bb_query / "
                         "纠偏用 artex_hint。")
                    self._record_failure(name, r, args)
                    return False, r
                face = set(self._controller_tool_face() or ())
                # ② 计划/控制原语恒放行（§6.6）：它们是「收尾/立意」的兜底出口，
                # 裁掉会死锁成「必须先声明一条假意图」。这条与 ToolDispatcher
                # 的 `_CONTROL_TOOLS`/`_PLAN_TOOLS` 恒放行同口径。
                # MCP 工具走 `mcp_allowed_by_face`：它支持 `mcp__<server>__*`
                # 通配勾选，与 `_tool_specs` 的裁剪同源——否则勾了 server 会
                # 出现「schema 下发、执行被拒」的撕裂。
                if (name not in face and name not in _CONTROL_TOOLS
                        and name not in _PLAN_TOOLS
                        and not (name.startswith(_MCP_TOOL_PREFIX)
                                 and mcp_allowed_by_face(name, face))):
                    r = (f"[错误] 工具 {name} 未在本控制器的工具白名单内"
                         "（设置 → 专家池 → 该专家 → 工具白名单 勾选后保存即启用）。"
                         "本次不改白名单的话，按角色该走的通道是：派方向用 "
                         "artex_plan / 观察用 artex_status 与 bb_query / "
                         "纠偏用 artex_hint。")
                    self._record_failure(name, r, args)
                    return False, r
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
            # 续跑注入（ask-expert）：**串行**，不参与 _parallel_expert_dispatch
            # ——它写的是已有线程的上下文，同轮并发两条 ask 打同一线程会互相
            # 踩历史。放在 call_expert 之后：混合批次里先建新线程再追问旧线程，
            # 顺序与模型书写顺序一致，审计更好读。
            if name == ASK_EXPERT_TOOL:
                r = self._tool_ask_expert(args)
                self._record_failure(name, r, args)
                return not r.startswith(("[错误", "[拒绝")), r
            # CTF 解题主控的调度原语（ctf-controller，2026-10-19）：不经
            # ToolDispatcher（那里没有 handler），走本类的专用方法。
            # 放在 call_expert 之后：两者同轮出现时先落计划再派发——否则
            # `_tool_call_expert` 的 allowed() 校验会看到「还没 plan」而放行
            # 越权派发（顺序反了就等于没有闸）。
            if self.is_ctf_controller and name == "ctf_dispatch":
                r = self._tool_ctf_dispatch(args)
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            if self.is_ctf_controller and name == "ctf_review":
                r = self._tool_ctf_review(args)
                self._record_failure(name, r, args)
                return not r.startswith("[错误"), r
            # 只读核对通道（ctf-inspect，2026-10-20，P0-2）：同样不经
            # ToolDispatcher——它刻意**不给** read_file/run_cmd 的能力面。
            if self.is_ctf_controller and name == "ctf_inspect":
                r = self._tool_ctf_inspect(args)
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
                                          self._db_text(result), tool_use_id=tc.id)
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
            hit_broke = False
            while pending_set:
                if self._aborted():
                    break
                done, pending_set = wait(pending_set, timeout=0.3)
                for future in done:
                    call_id, result = future.result()
                    results[call_id] = result
                # 人类插话（chat-hit，2026-10-21）：主控在等 call_expert 返回时是
                # **整段阻塞**的——通知原本只在步边界取，这段等待里一次都读不到，
                # 于是「主控在等专家」的几分钟内，人发什么都进不来（用户实测：
                # 只能干等或点停止）。这里在等待循环里也探一次：有插话就**提前
                # 返回**，把已收结果交回主控，剩下的专家转后台继续跑（复用
                # first_return 的 overflow 通道，结果不丢）。
                #
                # **不打断 call_expert**：专家线程一个都没被取消，`shutdown(
                # wait=False)` 照旧让它们跑完；只是主控不再干等，先回去读人的话。
                if not done and self._has_hit_note():
                    log.info("人类插话：主控提前结束等待，专家转后台 thread=%s",
                             self.thread_id)
                    hit_broke = True
                    break
                if first_return and done:
                    # 先返先触发：首批结果到手即止。timeout=0 非阻塞再捞一轮
                    # （窗口内刚好收尾的一并交回，减少主控空跑 round-trip），
                    # 剩下的才标记断候转后台。
                    done2, pending_set = wait(pending_set, timeout=0)
                    for future in done2:
                        call_id, result = future.result()
                        results[call_id] = result
                    break
            if (first_return or hit_broke) and pending_set and not self._aborted():
                # 断候登记必须在「再捞一轮」之后：之后完成的探针才走
                # overflow 路径；窗口内已完成的不标记、不双落盘。
                #
                # hit_broke 也要登记：插话提前返回后，剩下的专家同样在后台跑，
                # 其结果必须走 overflow 补达，否则**没有 tool_result 配对**——
                # 主循环会为它们补 `_PROBE_PENDING` 占位（保 pairing 合法），
                # 真实结果靠 overflow 在下一步并进上下文。不登记的话这些结果
                # 永远没人取，主控再也看不到（比「晚到」糟得多）。
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
        """把后台完成的并发专家结果并入**下一轮** LLM 上下文（先返先触发补达）。

        仅启用 first_return 的主控产生 overflow（见 `_parallel_expert_dispatch`）。
        DB 侧真实 tool 行已由 worker 落库（同 tool_use_id 续写），这里只做**内存
        注入**：`_inject_nudge` 并入尾部 user 消息——连续两条 user 会被网关 400
        拒收，绝不能新起一条 user 消息。`skip_ids`：本批已作为正式 tool_result
        交回模型的调用（时间窗内收尾的双落盘），避免同一结果进两遍上下文。

        **文案按轨分叉**（2026-10-20）：这段注入文案原本写死激进主控的语义
        （「[命中]→立即派深挖专家；[未命中]→放弃该方向」——那是渗透探针的
        纪律）。CTF 主控的 worker 回执用 `[命中]/[未命中]` 只是约定俗成的
        前缀，它该做的动作完全不同：命中该走 `ctf_submit_flag` 登记、未命中该
        走 `ctf_review` 看判据再决定换方向还是收尾。照搬渗透口径会让 CTF 主控
        在一个「[命中]」之后去「派深挖专家」——把方向互斥/轮次记账整套调度
        绕过去了。
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
        if self.is_ctf_controller:
            lines = ["[后台并发结果] 以下并发专家已在后台跑完（其余方向仍在跑，"
                     "不必等它们）："]
        elif self.is_first_return_controller:
            lines = ["[后台并发结果] 以下并发专家已在后台跑完，请按先返先触发规则"
                     "处置（[命中]→立即派深挖专家；[未命中]→放弃该方向）："]
        else:
            # 非激进/非 ARTEX 主控走到这里只有一个原因：**人类中途插话**让它
            # 提前结束等待（2026-10-21）。此时不能套「先返先触发」那套渗透纪律
            # ——它本来就没启用该模式，读到的动作指令会与自己的调度方式矛盾。
            # 只需如实告知「你之前等的那批跑完了」，判断交给模型。
            lines = ["[后台并发结果] 你先前派出的并发专家已跑完（你当时因人类插话"
                     "提前返回，没有继续等它们）："]
        for it in items:
            body = it.get("result") or ""
            if len(body) > _PROBE_OVERFLOW_HEAD:
                body = (body[:_PROBE_OVERFLOW_HEAD]
                        + f"\n…（截断；全文见该专家的工具结果 {it['id']}）")
            lines.append(f"◆ 专家 {it.get('expert') or '?'}"
                         f"（调用 {str(it.get('id') or '')[-8:]}）：\n{body}")
        if self.is_ctf_controller:
            # 处置纪律必须落在**这里**：主控刚收到一条异步结果，最容易的动作
            # 就是直接再 call_expert 派下一批——那会绕过 ctf_dispatch 的准派闸。
            lines.append(
                "处置：① 拿到 flag → `ctf_submit_flag` 登记（别口头声称拿到）；"
                "② 某方向已证伪 → `close_intent(outcome=\"dead_end\")` 记下来，"
                "再 `ctf_dispatch` 提新方向（**不要直接 call_expert**——不在准派"
                "计划内会被拒）；③ 全部方向都做完、答案已登记 → `ctf_conclude` "
                "收尾；④ 这题确实打不动 → `ctf_abandon`。")
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
        # 已解出闸（challenge-confirm-notice，2026-10-20 修正）：台账已 solved
        # 时**任何**新派发都拒——包括绕过 ctf_dispatch 直调 call_expert 的路径。
        # 与 `_tool_ctf_dispatch` 里那道闸同源（`_solved_challenge_block`）：
        # 一处管「提方向」，一处管「真派出去」，缺任一处都能被绕过。
        #
        # **必须排在专家名校验之前**：这是一道「题目层面已无活可干」的结论，
        # 与「你派的是哪个专家」无关。排在后面时，一个拼错专家名的调用会先被
        # 「未知专家」拒掉，模型据此以为换个名字就能派——而真正的结论是
        # 「这题不用再派了」。
        solved_block = self._solved_challenge_block()
        if solved_block:
            return solved_block
        if expert in CONTROLLER_IDS:
            return ("[错误] call_expert 失败：主控类智能体不可被委派"
                    "（spawn 深度=1）；请派专家池专家")
        if self.is_aggressive and expert != "worker":
            return "[错误] call_expert 失败：激进主控只能委派固定子代理 worker"
        if not self.is_aggressive and expert not in self._delegatable_experts():
            available = ", ".join(self._delegatable_experts())
            return (f"[错误] call_expert 失败：未知专家 '{expert}'；"
                    f"可用：{available}（这些是专家 id，不是技能名）")
        # CTF 解题主控的越权派发闸（ctf-controller，2026-10-19）：每条派发必须
        # 在本轮 ctf_dispatch 的准派计划内。没有这道闸，模型可以跳过 ctf_dispatch
        # 直接 call_expert——方向互斥、放弃判据、轮次记账、自证核验全部失效，
        # 整套代码调度退化成装饰。文案要给出**下一步动作**（先 dispatch），
        # 否则模型会反复重试同一次派发，白烧一轮。
        # getattr 兜底：这是委派热路径，`object.__new__` 构造的壳（测试 / 旧实例）
        # 没有该属性，不该在这里 AttributeError 炸成「工具异常」。
        ctf_sess = (self._ctf_session_for()
                    if getattr(self, "is_ctf_controller", False) else None)
        if ctf_sess is not None and not ctf_sess.allowed(task):
            return ("[错误] call_expert 失败：该方向不在本轮准派计划内。"
                    "先用 ctf_dispatch(directions=[…]) 提交方向——代码会判定"
                    "「准派/拦下」并给出理由（重复方向、已判死方向、被他人占用的"
                    "方向都会被拦），然后按计划里的 task 文本原样派发。")
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
            # ARTEX Intent 记账（**尽力而为**，2026-10-19 事故修复）：
            #
            # 此前这里硬写 `task_id=self.thread_id`——线程 id 冒充任务 id，于是
            # 每次委派都在 ARTEX 任务列表里凭空多出一个 `chat-xxxxxxxx` 的假任务：
            # 它没有 Planner、没有目标（goal_stats 全 0）、没有探索图节点、没有
            # 范围锚定，用户在工作台看到的就是「目标 0/1、探索图空白」。而且
            # `claim_next_intent` **不按 task_id 过滤**，这种假任务里的 Intent
            # 会被项目 Engine 当正常任务领取，调度语义彻底错乱。
            #
            # 现在：只有**本线程已绑定真实 ARTEX 任务**（artex_plan 建的）才记账，
            # 且用那个真实 task_id；没有真任务就不建 Intent——call_expert 是
            # 「不值得建 Intent 的一次性委派」，本来就不该在任务树里留痕。
            # 记账失败也绝不阻断委派（它是审计附加项，不是执行前提）。
            artex_task = str(getattr(self, "artex_task_id", "") or "").strip()
            if artex_task:
                try:
                    artex_intent = create_artex_intent(
                        self.bb, self.project_id, task, task_id=artex_task,
                        expert=expert, asset_ids=assigned,
                        constraints={"track": self.track or "",
                                     "controller": self.agent_id})
                    artex_intent_id = artex_intent.get("id", "")
                    claim_artex_intent(self.bb, self.project_id, artex_intent_id,
                                       f"chat-{self.thread_id[-12:]}")
                    transition_artex_intent(self.bb, self.project_id,
                                            artex_intent_id, "running")
                except Exception:  # noqa: BLE001 —— 记账失败不影响委派本身
                    log.exception("call_expert ARTEX Intent 记账失败 thread=%s",
                                  self.thread_id)
                    artex_intent_id = ""
            worker_agent = "worker" if self.is_aggressive else expert
            sub = chat_store.create_thread(
                self.bb, self.project_id, worker_agent,
                title=task[:40], parent_thread_id=self.thread_id,
                spawned_task=task[:500],
                # CTF 题目归属显式继承（challenge-asset，2026-10-19）：子线程
                # 虽可沿 parent_thread_id 向上解析到题目，但那是**弱引用**——
                # 父线程一旦被删/被改绑，子线程就退化成「无题目」，届时
                # `_challenge_fallback_asset()` 会按线程标题**再建一道题**
                # （用户实测：一道题变两道）。显式带位让它自持归属。
                challenge_asset_id=(
                    chat_store.resolve_challenge_asset_id(self.bb, self.thread_id)
                    or ""),
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
            # 子线程被删（2026-10-19 事故）：`ChatTurn.run` 捕获 ThreadGone 后
            # 返回一句说明而非抛错，此处识别出来回一条**中性**文案——不写成
            # 「执行失败」，因为这不是专家能力问题，是人类主动删了线程。
            if chat_store.thread_exists(self.bb, sub_id) is False:
                raise chat_store.ThreadGone(sub_id)
        except chat_store.ThreadGone:
            # 人类把子线程删了：Intent 归 cancelled（不是 failed——它没有出错，
            # 是任务被撤销），主控收到中性说明，可决定重派或不重派。
            log.info("call_expert 子线程已被删除 expert=%s thread=%s",
                     expert, sub_id)
            if artex_intent_id:
                try:
                    transition_artex_intent(self.bb, self.project_id,
                                            artex_intent_id, "cancelled",
                                            error="子线程被人工删除")
                except Exception:  # noqa: BLE001
                    log.exception("ARTEX Intent 取消失败 id=%s", artex_intent_id)
            return (f"专家线程 {sub_id}（{expert}）已被人工删除，本次委派中止。"
                    "需要这个方向请重新委派或改用 artex_plan 建 Intent。")
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
        # CTF 解题主控的轮次记账（ctf-controller，2026-10-19）：这是放弃判据的
        # 分母，必须由代码记——LLM 自述「我试了 2 轮」和「我试了 5 轮」在上下文
        # 里长得一模一样，而判据的阈值就卡在 2 轮和 8 轮上。
        #
        # ``flags_added`` 取**台账实际增量**（派发前后各数一次 accepted+pending），
        # 不信 worker 摘要里说自己拿了几个 flag。读库失败记 0（宁可少算进展，
        # 也不要因为一次 DB 抖动就误判方向有进展而续投）。
        if ctf_sess is not None:
            try:
                # 键必须走 `ctf_key_of`（先剥【方向】/【要求】壳）：`task` 常是
                # 计划里回贴的整段文本，直接 `direction_key` 会得到另一个键，
                # 于是既查不到已有 state（flags_got 基线读成 0 → 增量虚高），
                # 又在 `record` 里新建一个 passes=0 的孤立方向（放弃判据失效）。
                dkey = ctf_key_of(task)
                before = ctf_sess.dirs.get(dkey)
                n_before = before.flags_got if before else 0
                cid = chat_store.resolve_challenge_asset_id(self.bb, self.thread_id) or ""
                n_after = n_before
                if cid:
                    from core.blackboard.challenge_flags import accepted_flags
                    n_after = len(accepted_flags(self.bb, self.project_id, cid))
                added = max(0, n_after - n_before) if cid else 0
                rec = ctf_sess.record(task, summary, flags_added=added)
                v = rec["verdict"]
                self._emit("chat.ctf_verdict", {
                    "direction": dkey,
                    "action": v.action, "code": v.code, "reason": v.reason,
                    "passes": rec["state"].passes,
                    "attest_issued": rec["attest_issued"],
                    "attest_note": rec["attest_note"],
                    # 耗时记账（ctf-cost，2026-10-20，P1-2）：不参与任何判据，
                    # 只让「这一轮跑了多久」可被复盘。`first_at` 由代码在首次
                    # 派发时写，故 elapsed 是**该方向累计墙钟**而非本轮耗时——
                    # 复盘要看的是「这个方向烧了多久」，不是「最后一次多久」。
                    "elapsed_s": round(max(
                        0.0, ctf_sess._clock() - (rec["state"].first_at or 0.0)), 1),
                })
            except Exception:  # noqa: BLE001 —— 记账失败不阻断派发结果回传
                log.exception("chat ctf 轮次记账失败 thread=%s", self.thread_id)
        return (f"专家线程 {sub_id}（{expert}）已完成。\n"
                f"收尾摘要：\n{summary[:4000]}\n"
                f"（人类可在工作台打开该线程查看全过程与追问；"
                f"要追问细节（或问清可沉淀的判据），用 ask_expert(thread_id=\"{sub_id}\", ...)）")

    # ---------- 续跑注入（ask-expert） ----------

    def _thread_assigned_assets(self, thread_id: str) -> list[str]:
        """从意图记录反推该线程被分配的资产（ask_expert 续跑用）。

        **为什么不读线程行**：`assigned_asset_ids` 只是 `ChatTurn` 的内存字段，
        `chat_threads` 表里根本没有这一列（`store.create_thread` 的入参里也没有）
        ——读 `row.get("assigned_asset_ids")` 恒得空列表，续跑出来的专家会
        变成「无分配资产」，维度收敛闸与资产边界全部失效。

        意图是持久事实：子专家派的活必留 `intents.target_asset_id`
        （`declare_intent` 落库）。按 author 取本线程声明的资产并集即可。
        读库失败返回空列表（宁可少分配，不要因为一次 DB 抖动让续跑炸掉）。
        """
        author = f"chat-{thread_id[-12:]}"
        try:
            rows = self.bb.conn.execute(
                "SELECT DISTINCT target_asset_id FROM intents"
                " WHERE project_id=? AND author=? AND target_asset_id IS NOT NULL",
                (self.project_id, author)).fetchall()
        except Exception:  # noqa: BLE001
            log.exception("ask_expert 反推分配资产失败 thread=%s", thread_id)
            return []
        return [str(r[0]).strip() for r in rows if str(r[0] or "").strip()]

    def _tool_ask_expert(self, args: dict) -> str:
        """给**已派发过**的专家线程补一条消息，让它带原上下文续跑。

        **为什么不是「再 call_expert 一次」**：`call_expert` 每次都 spawn 一条
        全新线程，专家从零开始——原线程里「我试过什么、哪条路为什么不通」的
        上下文全丢。而主控追问的恰恰是那些细节。这里复用**同一条线程**：
        `ChatTurn.run(text)` 会先 `_load_history()` 再跑，等于在原上下文后面
        追加一句用户消息。

        **与 `call_expert` 的硬边界**（不做就成了第二套派发入口）：
        - 只接受**本线程自己 spawn 出来的**子线程（`parent_thread_id == 自己`）。
          否则主控能借它唤醒任意线程（含别的项目、人类手建的会话），
          绕开「spawn 深度=1」的委派纪律。
        - 不换专家、不换工具面、不重新分配资产——继承原线程配置。
        - 已解出闸照旧生效（题目 solved 后不该再让任何专家跑）。

        **为什么串行**：它写的是**已有线程的上下文**。同轮并发两条 ask 打同一
        线程会互相踩历史（各自 `_load_history` 后追加，后写覆盖前写的顺序）。
        `_dispatch_inner` 走单线程分支，不进 `_parallel_expert_dispatch`。
        """
        thread_id = str(args.get("thread_id") or "").strip()
        message = str(args.get("message") or "").strip()
        if not thread_id or not message:
            return "[错误] ask_expert 失败：thread_id 与 message 必填"
        if not self.is_orchestrator or self._is_spawned_subagent:
            return "[错误] ask_expert 失败：只有主控能续跑专家（spawn 深度=1）"
        # 已解出闸：与 call_expert 同源（`_solved_challenge_block`）。题目已解出
        # 后让专家继续跑，产出的是对本题无用的动作——与再派一个新专家同样该拦。
        solved_block = self._solved_challenge_block()
        if solved_block:
            return solved_block
        if self.llm is None:
            return "[错误] ask_expert 失败：LLM 未装配"
        row = None
        try:
            row = chat_store.get_thread(self.bb, thread_id)
        except Exception:  # noqa: BLE001 —— 读库失败按「不存在」处理
            row = None
        if row is None:
            return (f"[错误] ask_expert 失败：线程 {thread_id} 不存在"
                    "（只接受 call_expert 返回里给出的 chat-xxx）")
        # 归属校验（红线）：必须是自己 spawn 的子线程。用 parent_thread_id
        # 判定而不是「在本项目内」——后者太宽，主控能借此唤醒人类手建的会话。
        if str(row.get("parent_thread_id") or "") != self.thread_id:
            return (f"[错误] ask_expert 失败：{thread_id} 不是本主控派发的线程"
                    "（只能续跑自己 call_expert 出来的专家；"
                    "新任务请用 call_expert）")
        if row.get("project_id") and str(row["project_id"]) != self.project_id:
            return f"[错误] ask_expert 失败：线程 {thread_id} 不属于本项目"
        # 正在跑的线程不能续（会与那一轮的 messages 局部变量打架：那一轮
        # 已经 `_load_history` 完了，我们追加的消息它读不到，等它收尾后
        # 才可见——但那时本轮 run 已经用旧历史跑完，等于白写）。
        if str(row.get("status") or "") == "running":
            return (f"[拒绝] ask_expert：线程 {thread_id} 正在运行，"
                    "它的上下文此刻锁在那一轮里，追加消息读不进去。"
                    "等它跑完（工作台显示空闲）再问；或先用 call_expert 派新任务。")
        try:
            sub_turn = ChatTurn(
                bb=self.bb, llm=self.llm, project_id=self.project_id,
                thread_id=thread_id, packs_root=self.packs_root,
                track=self.track, capabilities=self.capabilities,
                mcp_bridge=self.mcp_bridge, expert_names=[],
                abort_event=self.abort_event,
                owner_tags=self.owner_tags,
                rule_profiles=self.rule_profiles,
                artifacts_dir=self.artifacts_dir,
                browser_pool=self.browser_pool,
                decompiler_factory=self.decompiler_factory,
                containers=self.containers,
                probe_mode=self.is_aggressive,
                # 资产分配沿用原线程（续跑不重新分配，否则专家会「忘记」
                # 它原本负责哪些资产，或反过来被塞进不属于它的资产）。
                # **从意图记录反推**：`assigned_asset_ids` 只是 ChatTurn 的
                # 内存字段、`chat_threads` 表里没有，读 `row[...]` 恒为空。
                # 子专家派的活必留意图（intents.target_asset_id），那是持久事实。
                assigned_asset_ids=(
                    [] if self.is_aggressive
                    else self._thread_assigned_assets(thread_id)),
                recovery=self.recovery)
        except Exception as e:  # noqa: BLE001 —— 构造失败回文本，别炸整轮
            log.exception("ask_expert 子轮构造失败 thread=%s", thread_id)
            return f"[错误] ask_expert 续跑失败（线程构造）：{e}"
        self._emit("chat.ask_expert", {"thread_id": thread_id,
                                       "message_head": message[:200]})
        try:
            reply = sub_turn.run(message)
        except chat_store.ThreadGone:
            return (f"专家线程 {thread_id} 已被人工删除，本次续跑中止。"
                    "需要这个方向请重新 call_expert 派发。")
        except Exception as e:  # noqa: BLE001 —— 续跑失败回文本，主控可改道
            log.exception("ask_expert 续跑失败 thread=%s", thread_id)
            return (f"[错误] ask_expert 续跑失败：{e}"
                    f"（线程 {thread_id} 已留档，可改用 call_expert 重派）")
        expert = str(row.get("agent_id") or "")
        return (f"专家线程 {thread_id}（{expert}）已续跑完成。\n"
                f"答复：\n{reply[:4000]}\n"
                "（如需继续追问同一线程，可再调 ask_expert）")

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
