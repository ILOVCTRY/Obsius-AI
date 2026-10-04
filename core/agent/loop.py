"""Agent 主循环（DESIGN.md §3）。

单 Agent + Skill 上下文注入：
  系统提示 = 领域红线(规则链) + 能力清单(detector) + 角色人设 + 任务目标 + 工具纪律
  循环 = LLM 调用 → 工具分发 → 结果回填 → （卡死则召唤策略顾问） → 直至 finish；
  步数耗尽不再自动 fail，而是自动步数暂停（E8）：快照 + 任务保持 claimed，等人类恢复
上下文预算：超限裁剪旧工具输出（防淹没全局目标）。
会话收尾安全：显式结束会话时任务未收尾自动 fail（防 lease 占坑，§6.4）。
会话控制（§3）：暂停/恢复/中断检查点一律落在 LLM 步之间；暂停存 messages 快照，
恢复从快照续跑当前任务；中断任务 fail（人工中断）不回队列。
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.agent.retention import omitted_note, retain
from core.agent.session_state import SessionState, state_proxy
from core.agent.tools import (
    AGENT_TOOLS,
    HARD_REJECT_PREFIXES,
    PLAN_GATE_PREFIX,
    ToolDispatcher,
    _CLOSING_MAX_ROUNDS,
    _CONTROL_TOOLS,
    _PLAN_TOOLS,
)
from core.autonomy import record_llm_usage
from core.blackboard import Blackboard, TaskQueue
from core.blackboard.intents import list_intents
from core.llm.provider import LLMError, assistant_message, tool_results_message
from core.llm.tokenizer import get_counter
from core.runtime.gateway import ExecutionGateway
from core.skills import (
    SkillRegistry,
    build_rules_preamble,
    load_task_types,
)
from core.skills.judge import judge_finding
from core.skills.kbindex import kb_module_hints
from core.skills.experts import expert_exists, load_expert
from core.skills.routeindex import read_kb_module

log = logging.getLogger(__name__)

# 空闲对话轮（2026-09-19）工具集：AGENT_TOOLS 去掉 finish——对话轮无任务可收尾，
# 纯文本回复即终止；其余工具照常（查黑板/知识库/资产回答人类提问）。
CHAT_TOOLS: list[dict[str, Any]] = [t for t in AGENT_TOOLS if t.get("name") != "finish"]

# 工具参数流截断的整轮重试上限（2026-09-20 事故修复）：GLM 长思考会话输出预算
# 耗尽/网关断流 → 工具参数 JSON 残缺。传输层流不可重放，只能在 agent 轮级重建
# 消息重发；上限 2 次，仍截断才放给 _fail_task_on_error。
_CHAT_TRUNC_RETRIES = 2

# E2 硬拒绝熔断阈值（orchestrator-efficiency，2026-09-22；2026-09-24 口径重构，
# plan-gate-breaker-refine）：连续 N 个**模型步**含硬拒绝（越界/网关/服务端拒收，
# tools.HARD_REJECT_PREFIXES）= 撞闸循环（行为错误）。计数单位从「回执张数」改为
# 「模型步」——并行批 3 张拒绝票只计 1，模型始终拿到下一轮改道机会；任何不含硬
# 拒绝的步（含纯文本步）清零。触发即置 awaiting_human 走 C1 挂起
# （快照+fail(awaiting_human)+人工承接），不是 budget paused（拒绝循环是行为错误
# 不是预算问题，宁严勿松交人工）。
_REJECT_BREAK_LIMIT = 3

# 计划闸教练阈值（2026-09-24）：同一任务累计 N 个**模型步**含计划闸回执 →
# 进入 plan-only 模式（工具面收缩 + 强提示注入）；模式内下一步末计划仍空 → 挂人。
_PLAN_GATE_STEPS = 2
_PLAN_ONLY_NUDGE = (
    "【强提示】你已两次在未写计划的情况下尝试实质动作并被计划闸挡回。"
    "下一步**只能调用 task_plan** 写下本任务的解决计划（3-8 个可验证小步）；"
    "其余工具已临时收回。若确实无法规划，任务将挂起交由人类承接。")

# plan-only 模式下发给 LLM 的工具面：计划原语 + 收尾控制原语
_PLAN_ONLY_TOOL_NAMES = _PLAN_TOOLS | _CONTROL_TOOLS

# stuck-convergence D9（2026-09-24）：观察窗到期但仍在活跃探索时的静默延长上限。
# 真打转 12 步照走顾问链；活跃长任务最多延 2 个窗（12/24 步不打断），之后顾问
# 介入、第 60 步硬闸兜底——断路器始终不拆。
_STUCK_MAX_EXTENSIONS = 2
# M6 F1 高质量复盘档（orchestrator-efficiency §0-11）：done 零产出但收尾总结
# ≥500 字 → 只复盘；每会话上限 1 条防刷量（内存计数，重启清零可接受——人工审批关兜底）
_SEDIMENT_LITE_MIN_NOTE = 500
_SEDIMENT_LITE_CAP = 1

# ■ 收件箱订阅声明（2026-09-21）：kind → 消费场景。四处消费点（认领期 claim /
# 恢复期 resume / 步边界 step / 对话轮 chat）统一从 AgentSession._drain_inbox
# 取信，渲染顺序=表内声明序；新增 kind 必须先在此登记——未登记的私信滞留
# 收件箱红点可见，绝不被静默吞。
# - basis_stale（强制三选一）与 finding_update（发现增补）须任务上下文才能响应，
#   对话轮不消费（留收件箱等下一轮认领期/步边界）。
# - human_note 轮末语义（2026-09-19）：不在步边界打断思路，留到认领期/恢复期。
_INBOX_SUBSCRIPTIONS: dict[str, tuple[str, ...]] = {
    "basis_stale": ("claim", "resume", "step"),
    "finding_update": ("claim", "resume", "step"),
    "authorization_result": ("claim", "resume", "step", "chat"),
    "approval_rejected": ("claim", "resume", "step", "chat"),
    "human_note": ("claim", "resume", "chat"),
    "agent_message": ("claim", "resume", "step", "chat"),
    "task_receipt": ("claim", "resume", "step", "chat"),
}


class _StepInterrupted(Exception):
    """■ 即点即停（2026-09-19）：LLM 调用等待中被人手中断——等待方即刻收到，
    不等响应返回。旁路线程随 HTTP 超时自行结束，结果丢弃。"""

STRICT_PROMPT_TAIL = """
## 工具纪律（硬规则）
1. 一切命令经 run_cmd，runtime 按威胁等级选择；被网关拒绝后改道，不得重试同一动作。
2. 动手前先 bb_query 看黑板——别人可能已做过（去重是硬规则）。
3. **认领任务后第一件事是 task_plan 写下解决计划（3-8 个可验证小步）**，之后才允许
   run_cmd / bb_add_* 等实质动作（服务端有计划闸，未交计划会被回填引导）；情况变化时
   再调 task_plan 修订（保留进度的步带原 id，rev_reason 写原因），每开始/完成一步用
   task_step 置 doing/done——任意时刻至多一个 doing，被阻塞置 blocked 必写原因。
   **实质动作还必须先有 open 意图**（服务端有意图先行闸，见第 10 条）：认领后=先
   task_plan 粗规划 → 再 declare_intent 把第一步方向落成假设 → 然后才 run_cmd。
4. 发现即落 bb_add_finding（须挂在 open 意图下且意图后有执行动作，见第 10 条）；
   无证据 status=unverified。**边干边写**：执行中每确认一条认知（端口/版本/未授权
   状态/接口行为/凭据线索等观察）立即以 category=intel + status=unverified 落一条
   发现——抗中断、抗上下文压缩、跨意图可复用；close_intent 收尾时再把它升 verified
   或随死路一并了结，不要攒到最后一次性补记。
5. 卡住时如实 fail_task，不要空转。
6. 经验沉淀只走 propose_pack_edit 提案（绝不直接改技能/知识库）：仅限三种情形——
   文档互相矛盾、文档缺失、某手法已在本任务中验证有效；reason 必须附任务证据
   （任务 id + 关键观察）。知识库按「阶段/测试包」组织，每个测试包一册手册 +
   payloads/ 弹药目录。沉淀去向优先 **测试包手册**：新经验补进对应测试包 手册.md
   的「已验证路径」「坑」段；本任务整条 verified 攻击链/跑通 payload 值得复用时，
   提 kind=case 提案（成功案例.md 补段或 payloads/ 补弹药）——这是打穿路径的
   首选复用源；若新增了值得索引的测试点手册，再提一条 index 提案同步
   route_index.yaml（mode=edit 增补本域条目，条目 kb 路径带域前缀）。认为某技能
   粒度过粗时，可提 mode=suggest 的技能拆分建议提案
   （产建议文档，实际拆分由人执行）。英文快照原文不翻译、不覆盖；
   每个会话最多 3 条，禁止凑数。提案批准权在人类。
7. 产物落盘纪律：正式产物（POC/报告/有效载荷/验证有效的关键结果）一律
   bb_add_artifact（永久保存+进证据链）；临时中间文件用**相对路径**写当前工作
   目录（服务端已固定到本项目 scratch，可随时清理，系统 TEMP 已重定向到项目内）；
   向工作区外绝对路径写文件会被网关拒绝——不要重试同一写法。
8. 工具调用策略：无依赖的调用在同一轮**并行发出**（如同时查多个资产或多个函数）；
   专用工具优先于 run_cmd 拼命令（查黑板走 bb_query，读取当前方法走 skill_open）；
   Skill 正文和同目录 references/scripts/examples/assets 是本任务的首选方法来源，按需读取，
   不要凭记忆复述文件。旧 kb_search/kb_open/route_lookup 只用于兼容尚未迁移的历史资料。
9. 执行环境速查：cwd 已由服务端固定到本项目 scratch（host/wsl/docker 均是，docker
   容器内对应 /workspace/scratch）——命令一律 **用相对路径，禁止手动 cd**（尤其不要拼
   cd /mnt/...）。runtime 语义：docker=Linux 渗透工具箱（bash + nmap/sqlmap/dirsearch/
   ffuf/python3 全套，能力清单显示 pentest-box 镜像就绪时渗透/扫描/文本命令首选）；
   host=宿主原生（Windows 上是 PowerShell，sed/awk 等 unix 语法在此不可用——仅 Windows
   目标特调）；wsl=bash + unix 工具链（docker 不可用时的兜底）；sandbox=加固沙箱
   （不可信/活体样本；铁律：样本绝不跑 host/wsl）。run_cmd 输出上限
   2000 字符（截断有标注，不要靠语义猜）；读工作区文件用 read_file（带行号、可分段）；
   超大工具结果自动落盘 spill/（回填含定位器，按提示分段取回）。
10. **意图纪律（渗透链路图：目标 → 子目标/意图 → 收尾 → 意图 → 收尾 → …）**：
    侦察/测绘（bb_query、kb_search、list_symbols/decompile、read_file/search_files、
    browser_navigate 等**只读**信息收集）自由先行；**会话第一次实质动作（run_cmd、
    浏览器点击/输入、写黑板产物等）前必须先 declare_intent**，把方向落成一句
    可证伪假设（服务端有意图先行闸：无 open 意图时实质动作直接拒），再围绕它执行。
    **意图必须绑资产锚点**（服务端硬门禁）：填 target_asset_id，或在 basis_refs
    里至少给一条 asset:<资产id>——游离意图落不到链路图子目标下，其收尾也无法
    为资产背书 tested_clean，会被直接拒绝。http/工具动作按时间归入该意图（执行层
    是图的展开细节）；发现/漏洞是检验的产物——bb_add_finding 前意图声明后必须有
    真实执行动作（服务端有时间线闸：declare 后直接落发现会被拒）。
    **每个意图必须 close_intent 收尾，三选一**：vuln（漏洞，引用已登记的非误报
    vuln 发现）/ finding（有效发现，引用非误报 intel 发现，一意图可挂多条同类）/
    dead_end（死路：写清死因+至少一条 http:/event: 证据引用，且零发现）。证据不足
    就保持 open（宁严勿松）——收尾所依据的发现后来被标误报、或有新证据，先
    reopen_intent 重开再收。不得留下悬挂意图（会话现场会列出未收尾项）。
    **逐资产独立立意（绑定 tested_clean）**：意图粒度=一个具体资产+一个具体攻击面
    假设；一个子目标要判净，须它名下意图**全部收尾**且至少一条 dead_end——「同模板/
    基线一致」式推断不能替代独立测试；收尾判据=没有可立的新意图，而非「意图都关了」；
    拦截页（WAF/WebVPN 488/403）≠源站状态，判死路须注明探测视角。
    **发现可再生长意图**：收尾产出的发现/漏洞本身也是推导依据——基于某个发现
    可以再 declare_intent（basis_refs 引用 finding:<id>）继续深挖，链路据此
    循环延伸，不要停在第一个发现上。
"""

# G3 结构化摘要压缩（2026-09-19，对齐 Claude Code /compact 与 HackSynth）：
# 旧历史不机械截断，让 LLM 按固定九要素骨架压成一段——用户原话逐字保留、
# 黑板对象引用 id 不内联内容（细节在黑板/事件流里，摘要只是索引）。
SUMMARY_SYSTEM = (
    "你是 Agent 会话压缩器：把执行历史压缩成一段摘要，供 Agent 无损续跑。"
    "只输出摘要正文（Markdown，中文），不要评论或客套。用户/人类引导的原话必须逐字保留，"
    "资产/发现/任务等黑板对象一律引用 id（如 as-xxxx、find-xxxx），不要内联全文。"
)

# fail 路径抢救收尾（2026-09-20，借鉴 Intentest arXiv:2609.07344 两段式降级：主阶段
# 失败后先经降级段抢救残余事实再回收）：任务异常终止前的一轮**无工具纯文本**提炼，
# 把已验证事实/失败方向落盘为 salvage 产物，接手者免重走。
SALVAGE_SYSTEM = (
    "你是故障抢救员：任务即将因异常终止，你没有任何工具、不能执行任何操作，"
    "只把执行历史里已获得的部分结论提炼成 Markdown 交接文本（中文，≤400 字）。"
    "只引用历史中真实出现过的黑板 id（如 find-xxxx、as-xxxx），绝不编造；"
    "历史太薄没有可抢救内容就直说，不要发挥。"
)


@dataclass
class AgentConfig:
    # E8：默认步数预算 200（角色 yaml 取 min 可更严；request_steps 可自助 +200）
    max_steps: int = 200
    # 对话轮步数上限（2026-09-20 会话窗对话化）：run_chat 取
    # min(dispatcher.max_steps, chat_max_steps)。24 够 Claude Code 式复合干活
    # （查黑板 2-3 + 跑命令 3-5 + bb_add_finding 登记结论 1-2 + 撰写回复），
    # 同时天然防对话轮无限膨胀；不做对话轮 request_steps 自助增补
    chat_max_steps: int = 24
    context_char_budget: int = 120_000
    # G3 结构化摘要压缩触发阈值（2026-09-19）：超过即把旧历史经 LLM 压成九要素
    # 摘要（近 8 条逐字保留）；硬上限仍由 context_char_budget 的机械 _trim 兜底
    context_summary_chars: int = 60_000
    stuck_after: int = 12         # 连续 N 步无进展 → 召唤策略顾问（2026-09-24：8→12，用户拍板）
    # D10（2026-09-24）：D9 静默延长上限与 D6 收尾确认轮项目级可配
    stuck_max_extensions: int = _STUCK_MAX_EXTENSIONS  # 0=关闭活跃探索静默延长
    closing_max_rounds: int = _CLOSING_MAX_ROUNDS      # 0=首次 complete 申报即放行
    owner_tags: list[str] = field(default_factory=list)
    rule_profiles: dict | None = None     # F11：评级/owner 生效档案三态（None=缺省自动）
    # 角色增强（§6.6，由 yaml 注入；None = 不限制）
    allowed_tools: list[str] | None = None
    lease_minutes: int = 30               # 认领/续租的租约 TTL
    lease_renew_seconds: int = 600        # 租约心跳间隔（TTL 的 1/3，留两次余量）


# 未声明 model_context 的模型假定上下文（token）。当前主力模型（GLM-5.3-Flash /
# deepseek-v4-flash / ark-code-latest）窗口 ≥256k；配小了会频繁触发摘要压缩
# （每次烧一步 LLM 调用还丢细节），宁可宽——真超窗由供应商报错兜底。
DEFAULT_MODEL_CONTEXT_TOKENS = 256_000


def apply_context_budget(config: AgentConfig, llm) -> None:
    """模型最大上下文（providers.json model_context，单位 token）→ 会话预算换算：
    context_char_budget ≈ tokens×2（中英混合启发：CJK≈1 token/字、EN≈1 token/4 字符），
    摘要压缩阈值取其一半。未声明（None/0）用 DEFAULT_MODEL_CONTEXT_TOKENS（256K）；
    切换模型后重跑即随新模型生效（__init__ 与切换端点共用）。
    只覆写仍处 dataclass 默认值的字段——显式传入的自定义预算（测试/未来 per-session
    配置）优先于模型推导。"""
    tokens = getattr(llm, "context_tokens", None) or DEFAULT_MODEL_CONTEXT_TOKENS
    d = {f.name: f.default for f in dc_fields(AgentConfig)}
    if config.context_char_budget == d["context_char_budget"]:
        config.context_char_budget = int(tokens) * 2
    if config.context_summary_chars == d["context_summary_chars"]:
        config.context_summary_chars = int(tokens)


def persisted_snapshot_path(artifacts_dir, sid: str) -> "Path | None":
    """快照落盘路径的模块级形态（API 层判 resumable 用）：
    <workspace>/<pid>/snapshots/<sid>.json；artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"{sid}.json"


def task_transcript_path(artifacts_dir, task_id: str) -> "Path | None":
    """C10 任务现场文件路径（模块级形态，API 层删任务清理用）：
    <workspace>/<pid>/snapshots/task-<tid>.json——与 E8 会话快照同目录。
    文件名由 task_id 确定性推导，agent 层读写无需查 DB 指针；
    artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"task-{task_id}.json"


def session_chat_path(artifacts_dir, sid: str) -> "Path | None":
    """会话级对话历史文件路径（2026-09-20 会话窗对话化）：
    <workspace>/<pid>/snapshots/chat-<sid>.json——与 task-<tid>.json 同目录
    （结构 {"session_id":..., "messages":[...]}，只存对话轮问答对）。
    无绑定任务的纯 chat 窗历史持久化用；artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"chat-{sid}.json"


def task_resume_path(artifacts_dir, task_id: str) -> "Path | None":
    """C6 任务键断点快照路径（模块级形态，API 层判 resume_mode/清理用）：
    <workspace>/<pid>/snapshots/task-<tid>.resume.json——**跨会话/跨角色复活凭证**。
    与 C10 task-<tid>.json（transcript 接手素材）同目录不同文件；
    不存 system（跨角色安全，system 由认领会话按角色重建）；
    artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"task-{task_id}.resume.json"


def clear_task_resume(artifacts_dir, task_id: str) -> None:
    """C6 任务键快照清理（模块级，API 层复用）：done 消费/objective 不匹配降级/
    任务删除/reopen(drop_scene)/重启孤儿对账时调用；缺失静默、OSError 只 log。"""
    path = task_resume_path(artifacts_dir, task_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("任务键快照清理失败（任务 %s）", task_id)


def _is_tool_result(m: dict) -> bool:
    """消息是否带 tool_result 块（C10 截断与 v0.64 快照尾部 sanitize 共用判定）。"""
    c = m.get("content")
    return (isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c))


def _tool_use_blocks(m: dict) -> list[dict]:
    c = m.get("content")
    if m.get("role") != "assistant" or not isinstance(c, list):
        return []
    return [b for b in c if isinstance(b, dict) and b.get("type") == "tool_use"]


def _tool_result_ids(m: dict) -> list[str]:
    c = m.get("content")
    if not isinstance(c, list):
        return []
    return [b.get("tool_use_id") for b in c
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and b.get("tool_use_id")]


def sanitize_snapshot_tail(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清理任务现场尾部的半步、重复或错配工具消息。"""
    msgs = list(messages)
    # 从末尾寻找最近一个 assistant tool_use；现场通常只在最后一轮不完整，
    # 发现严格不配对就丢掉该轮及其结果，保留此前安全边界。
    for idx in range(len(msgs) - 1, -1, -1):
        uses = _tool_use_blocks(msgs[idx])
        if not uses:
            continue
        ids = [str(b.get("id") or "").strip() for b in uses]
        if any(not x for x in ids) or len(ids) != len(set(ids)):
            return msgs[:idx]
        result_msgs = []
        j = idx + 1
        while j < len(msgs) and _is_tool_result(msgs[j]):
            result_msgs.append(msgs[j]); j += 1
        result_ids = [tid for m in result_msgs for tid in _tool_result_ids(m)]
        if len(result_ids) != len(ids) or set(result_ids) != set(ids) \
                or len(result_ids) != len(set(result_ids)):
            return msgs[:idx]
        return msgs
    return msgs


def _fmt_size(n: Any) -> str:
    """附件大小人读格式（_attachment_lines 用）。"""
    if not isinstance(n, int) or n < 0:
        return ""
    if n >= 1048576:
        return f"{n / 1048576:.1f}MB"
    if n >= 1024:
        return f"{n // 1024}KB"
    return f"{n}B"


def _attachment_lines(attachments: Any) -> list[str]:
    """附件清单行（2026-09-19 附件随发）：任务 context.attachments 与
    human_note payload.attachments 共用渲染——工作区相对路径 + 原始文件名，
    Agent 可直接 run_cmd 读取（读路径无 pathguard 拦截）。"""
    lines: list[str] = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or "").strip()
        if not path:
            continue
        name = str(a.get("name") or "").strip() or path
        size_s = _fmt_size(a.get("size"))
        lines.append(f"- 📎 {path}（{name}{('，' + size_s) if size_s else ''}）")
    return lines


class _LeaseHeartbeat(threading.Thread):
    """任务租约守护心跳（§6.4）：认领后周期 renew_lease，防长任务超过 TTL 被回收双跑。

    - pause 期间继续续租（任务仍被会话占有，快照恢复后接着跑）；
    - 任务 complete/fail/abort/被人类删除时由会话 stop()；
    - 续租抛 ClaimError（任务已不属于本会话）→ 自行退出；其他异常记下日志下一周期再试，
      绝不抛进主线程。
    """

    def __init__(self, tq: TaskQueue, session_id: str, task_id: str, interval_seconds: int):
        super().__init__(daemon=True, name=f"lease-hb-{session_id[:12]}-{task_id[:12]}")
        self.tq = tq
        self.session_id = session_id
        self.task_id = task_id
        self.interval = max(0.05, float(interval_seconds))
        self._stop_evt = threading.Event()

    def run(self) -> None:
        from core.blackboard.tasks import ClaimError

        while not self._stop_evt.wait(self.interval):
            try:
                self.tq.renew_lease(self.task_id, self.session_id)
            except ClaimError:
                log.info("租约心跳退出：任务 %s 已不由会话 %s 持有",
                         self.task_id, self.session_id)
                return
            except Exception:  # noqa: BLE001 —— 心跳是安全网，瞬时失败下一周期补偿
                log.warning("租约续租失败（任务 %s），下一周期重试", self.task_id, exc_info=True)

    def stop(self) -> None:
        self._stop_evt.set()
        # 有界等线程退出：Event.wait 命中置位即返，正常毫秒级收摊；负载下若不 join，
        # 观测方（测试/收尾断言）在 is_alive() 上会滞后一拍误判未停
        self.join(timeout=2.0)


class AgentSession:
    def __init__(
        self,
        *,
        project_id: str,
        bb: Blackboard,
        gateway: ExecutionGateway,
        llm: Any,                       # LLMProvider（executor 模型）
        planner_llm: Any | None = None,  # 策略顾问（planner 模型，缺省禁用）
        gate_llm: Any | None = None,    # C6 漏洞核对 hook 的 LLM（缺省=不挂 hook）
        packs_root: str | Path = "packs",
        track: str = "ctf",
        capabilities: list[str] | None = None,
        role: str = "_generalist",
        allowed_roles: list[str] | None = None,  # 发布链 publish_task 值域（expert-pool M2，§4.6）
        session_name: str | None = None,
        capability_prompt: str = "",
        config: AgentConfig | None = None,
        decompiler=None,
        artifacts_dir: str | Path | None = None,
        existing_session: dict | None = None,
        enable_sediment: bool = False,  # v0.65：done 自动提案开关（app 工厂按配置开；直构默认关）
        campaign=None,                  # ⑥ 战役记忆全局库（CampaignMemory；None=不沉淀）
    ):
        self.project_id = project_id
        self.bb = bb
        self.gateway = gateway
        self.llm = llm
        self.planner_llm = planner_llm
        self.packs_root = Path(packs_root)
        self.track = track
        self.capabilities = capabilities or []
        self.config = config or AgentConfig()
        apply_context_budget(self.config, self.llm)  # 模型声明上下文 → 预算换算（无声明=默认）
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None

        role_data = load_expert(self.packs_root, role, track)
        # 专家缺失时 load_expert 已回退 _generalist；role-rules/会话名按实际专家走
        self.role_name = role if (
            self.packs_root / "experts" / f"{role}.yaml").is_file() \
            else "_generalist"
        self.role = role_data
        self._apply_role_limits(role_data)
        # v14 认领即换装（DESIGN §6.4 定稿块）：底色角色=开窗选定，永不改；
        # 认领带 role 的任务时按任务角色换装（_apply_task_persona），跑完恢复。
        self.base_role_name = self.role_name
        self.base_role_data = role_data
        self._persona_saved: dict | None = None  # 换装现场（None=当前即底色）
        # v0.71 任务即窗口：中途改角色热换装——prompt 段下个步进重建的脏标记
        # 与最近一次 system 的 skill_context 快照（重建时复用，避免重跑路由）
        self._persona_dirty = False
        self._last_skill_context = ""

        if existing_session is not None:
            # rehydrate（服务重启/孤儿窗）：附着既有 sessions 行，不新建、不重置状态。
            # 角色 yaml 仍按当盘文件重载（软边界可能已变）；E8 起落盘的暂停快照
            # 会经 sessions.meta 指针载回（见 _load_persisted_snapshot）。
            self.session = dict(existing_session)
        else:
            self.session = bb.register_session(
                project_id, session_name or f"{track}/{self.role_name}", role=self.role_name)
        self.tq = TaskQueue(bb)
        # 会话控制面（DESIGN.md §3）：API 线程只置 Event，worker 线程消费——
        # 事件对象须先于 dispatcher 创建（■ 即点即停 2026-09-19：abort_event
        # 传给 dispatcher，run_cmd 执行中置位即杀进程树）
        self._pause_req = threading.Event()
        self._abort_req = threading.Event()
        self.dispatcher = ToolDispatcher(
            bb, gateway, self.tq,
            project_id=project_id, session_id=self.session["id"], author=self.session["id"],
            decompiler=decompiler, artifacts_dir=artifacts_dir,
            packs_root=packs_root, track=track, capabilities=self.capabilities,
            allowed_tools=self.config.allowed_tools,
            allowed_task_types=load_task_types(self.packs_root, track).keys(),
            max_steps=self.config.max_steps,
            stuck_after=self.config.stuck_after,
            closing_max_rounds=self.config.closing_max_rounds,
            abort_event=self._abort_req,
            role_skills=self.role.get("skills"),
            allowed_roles=allowed_roles,
        )
        self.registry: SkillRegistry | None = None
        self.capability_prompt = capability_prompt
        # C6 漏洞核对 hook（仅渗透/红队轨接线）：AI 触发 category=vuln 时，
        # 用本会话 LLM 对照红线/评级规则自我核对——不合格降级有效发现，不进漏洞视图。
        if track in ("pentest", "redteam") and gate_llm is not None:
            self.dispatcher.vuln_gate = self._build_vuln_gate(gate_llm)
        # v0.65 done 自动提案：任务完成时复盘验证过的有效手法 → 自动产提案草稿
        # （planner_llm 缺席或 enable_sediment=False 不接线；人类审批后进 kb 并更新索引）
        if planner_llm is not None and enable_sediment:
            self.dispatcher.sediment_hook = self._sediment_proposal
        # ⑥ 战役记忆（简版）：任务 done 后沉淀打法进全局库（跨项目召回；
        # 纯确定性拼接不需要 LLM，campaign 注入即接线）
        self.campaign = campaign
        if campaign is not None:
            self.dispatcher.campaign_hook = self._campaign_memory

        # 会话控制面（DESIGN.md §3）：API 线程只置 Event，worker 线程在步边界消费
        # （_pause_req/_abort_req 已上移到 dispatcher 构造前——■ 即点即停要传 abort_event）
        # session-state 收敛（2026-10-03）：本会话与 dispatcher 的任务/闸门状态统一
        # 落 SessionState，两处各以 property 代理原属性名（见文件末 _state_proxies）。
        # dispatcher 构造已在其上方完成，此处注入同一实例（单向共享，无反向依赖）。
        self.state = SessionState()
        self.dispatcher._state = self.state
        # 快照 = {system, messages, objective, task_id, next_step}——暂停时保存，恢复续跑
        # v0.64：_loop 在册的当前任务现场（system/messages 引用/objective）——暂停请求
        # 时刻 API 线程据此即时落盘，关后端不再丢「暂停未到步边界」窗口期的现场。
        # 字段语义与复位归属见 SessionState 各字段注释。
        self.paused = False            # 暂停态（含快照暂停与空闲暂停），恢复/中断后复位
        self._stop_after_task = False  # 硬中断后让 worker 循环退出的一次性闸门
        # 批 5（§6.8）：worker 退出原因信号——True=最后一次 claim 队列为空（可触发
        # L2 续 tick）；暂停/中断退出保持 False。run_next_task 每次认领前置 False。
        self.last_claim_idle = False
        self._heartbeat: _LeaseHeartbeat | None = None  # 当前任务的租约心跳
        if existing_session is not None:
            # E8：暂停快照已落盘 → 载回 _resume_state 并置回暂停态
            #（服务重启后 resume 仍可从快照+步数断点续跑；无快照行为同旧版）
            self._resume_state = self._load_persisted_snapshot()
            if self._resume_state is not None:
                self.paused = True

    # ---------- 状态代理（session-state 收敛，2026-10-03） ----------
    # 原属性名全部保留（读写点与测试断言零改动），存储落共享 SessionState。
    # 每任务复位见 SessionState.reset_for_task；未列的字段不随任务复位。

    _resume_state = state_proxy("resume_state")
    _live_state = state_proxy("live_state")
    _salvage_ctx = state_proxy("salvage_ctx")
    _reject_streak = state_proxy("reject_streak")
    _plan_gate_count = state_proxy("plan_gate_count")
    _stuck_waves = state_proxy("stuck_waves")
    _stuck_extensions = state_proxy("stuck_extensions")
    _cadence_last_rev = state_proxy("cadence_last_rev")
    _cadence_last_hint_step = state_proxy("cadence_last_hint_step")
    _cadence_hinted_findings = state_proxy("cadence_hinted_findings")
    _stale_alerted = state_proxy("stale_alerted")

    # ---------- 租约心跳（A1：长任务防 30 分钟 TTL 过期被重领双跑） ----------

    def _start_heartbeat(self, task_id: str) -> None:
        self._stop_heartbeat()
        hb = _LeaseHeartbeat(
            self.tq, self.session["id"], task_id, self.config.lease_renew_seconds)
        self._heartbeat = hb
        hb.start()

    def _stop_heartbeat(self) -> None:
        hb, self._heartbeat = self._heartbeat, None
        if hb is not None:
            hb.stop()

    def _apply_role_limits(self, role_data: dict) -> None:
        """角色 yaml 增强字段 → AgentConfig（§6.6）。

        角色限制只可能比全局/工厂配置更严：max_steps 取 min；tools 以角色值为准
        （null/缺省 = 不加该层过滤）。"""
        max_steps = role_data.get("max_steps")
        if isinstance(max_steps, int) and max_steps > 0:
            self.config.max_steps = min(self.config.max_steps, max_steps)
        tools = role_data.get("tools")
        if tools:  # 非空列表才限制（null/[] = 不限制）
            self.config.allowed_tools = list(tools)

    # ---------- 认领即换装（v14 任务绑定角色，DESIGN §6.4 定稿块） ----------

    def _apply_task_persona(self, task: dict | None = None,
                            task_id: str | None = None) -> None:
        """认领带 role 的任务时按任务角色换装（动态 persona）。

        换装四面：角色 prompt 段/技能路由白名单（self.role/self.role_name，build_system_prompt
        按调用时点取值）+ config.allowed_tools（经 _apply_role_limits 收敛）+
        dispatcher 对应边界。**不动 max_steps**（会话级资源，E8）。窗口不设
        role 限制（2026-09-18）：认领无角色过滤，本方法即任务 role 的唯一生效点。
        幂等：无 role / 与当前执行角色一致 → no-op；
        角色文件缺失（发布后被删）→ 防御性按当前角色跑。"""
        want = str((task or {}).get("role") or "").strip()
        if not want or want == self.role_name:
            return
        if not expert_exists(self.packs_root, want, self.track):
            return
        saved = {
            "role_name": self.role_name, "role": self.role,
            "allowed_tools": self.config.allowed_tools,
            "dispatcher_allowed_tools": self.dispatcher.allowed_tools,
        }
        rd = load_expert(self.packs_root, want, self.track)
        self.role_name, self.role = want, rd
        self.config.allowed_tools = None
        self._apply_role_limits(rd)
        self.dispatcher.allowed_tools = self.config.allowed_tools
        self.dispatcher.current_persona_role = want
        self._persona_saved = saved
        self.bb.append_event(
            self.project_id, "session.persona_switched",
            {"session_id": self.session["id"], "task_id": task_id or (task or {}).get("id"),
             "from": saved["role_name"], "to": want},
            session_id=self.session["id"], author=self.session["id"])

    def _restore_base_persona(self) -> None:
        """恢复底色 persona（幂等复位口，v14 换装状态机统一出口）。"""
        self._persona_dirty = False  # v0.71：换装层已整体复位，脏标记作废（防串任务）
        if self._persona_saved is None:
            return
        s = self._persona_saved
        self._persona_saved = None
        self.role_name = s["role_name"]
        self.role = s["role"]
        self.config.allowed_tools = list(s["allowed_tools"]) if s["allowed_tools"] else None
        self.dispatcher.allowed_tools = (
            list(s["dispatcher_allowed_tools"]) if s["dispatcher_allowed_tools"] else None)
        self.dispatcher.current_persona_role = None

    def apply_role_change(self, task: dict) -> None:
        """v0.71 任务即窗口：任务绑定角色中途修改（claimed 态仅放行 role），立即
        热换装——config/dispatcher 执行边界即时生效，prompt 段在下个步边界由
        _loop_body 重建（_persona_dirty）。

        **不得直接重跑 _apply_task_persona**：已在换装中（_persona_saved 非空）
        时它会重存底色，恢复时会回到中间角色（底色丢失）。正确姿势：已换装→
        只更新换装层；未换装（任务原无 role 被中途改出 role）→ 走标准换装路径。
        角色文件缺失/同名 → no-op（与认领换装同口径）。"""
        task_id = (task or {}).get("id") or self.dispatcher.current_task_id
        want = str((task or {}).get("role") or "").strip()
        if not want or want == self.role_name:
            return
        if not expert_exists(self.packs_root, want, self.track):
            return
        prev = self.role_name
        if self._persona_saved is None:
            self._apply_task_persona(task, task_id)
        else:
            rd = load_expert(self.packs_root, want, self.track)
            self.role_name, self.role = want, rd
            self.config.allowed_tools = None
            self._apply_role_limits(rd)
            self.dispatcher.allowed_tools = self.config.allowed_tools
            self.dispatcher.current_persona_role = want
        self._persona_dirty = True
        self.bb.append_event(
            self.project_id, "session.persona_switched",
            {"session_id": self.session["id"], "task_id": task_id,
             "from": prev, "to": want, "reason": "role-change"},
            session_id=self.session["id"], author=self.session["id"])

    def switch_session_role(self, role: str, reason: str = "manual-switch") -> bool:
        """会话级中途换人（会话中心化 §4.4）：重定义「这个窗是谁」——区别于
        apply_role_change（改在跑委托的建议角色、委托完恢复底色）：本切换把底色
        身份本身重定义，**跨委托保留**，对话历史与黑板全保留。

        - 无换装层（idle/对话态）：直接按新专家重装配底色 config/dispatcher。
        - 有换装层（委托在跑）：换装层换为新身份接手当前委托，saved 底色同步
          重定义并落具体边界（否则收尾 _restore_base_persona 回旧底色/空边界）。
        专家不存在 → ValueError（API 层转 422）；同名/空 → no-op 返 False。"""
        want = str(role or "").strip()
        if not want or want == self.role_name:
            return False
        if not expert_exists(self.packs_root, want, self.track):
            raise ValueError(f"专家不在池内或不可服务该轨: {want}")
        rd = load_expert(self.packs_root, want, self.track)
        prev = self.role_name
        task_id = self.dispatcher.current_task_id
        if self._persona_saved is not None:
            # 委托在跑：不重存底色，换装层直接换身份；saved 底色一并重定义
            s = self._persona_saved
            s["role_name"] = want
            s["role"] = rd
        # 当前身份重装配（底色或换装层同一套四面）
        self.role_name, self.role = want, rd
        self.config.allowed_tools = None
        self._apply_role_limits(rd)
        self.dispatcher.allowed_tools = self.config.allowed_tools
        self.dispatcher.current_persona_role = want
        if self._persona_saved is not None:
            # 存具体边界（restore 不重跑 _apply_role_limits）
            s = self._persona_saved
            s["allowed_tools"] = (
                list(self.config.allowed_tools) if self.config.allowed_tools else None)
            s["dispatcher_allowed_tools"] = self.dispatcher.allowed_tools
        self._persona_dirty = True
        self.bb.append_event(
            self.project_id, "session.persona_switched",
            {"session_id": self.session["id"], "task_id": task_id,
             "from": prev, "to": want, "reason": reason},
            session_id=self.session["id"], author=self.session["id"])
        return True

    # ---------- 系统提示 ----------

    def build_system_parts(self, objective: str, skill_context: str = "") -> tuple[str, str]:
        """system 拆分（M1 prompt caching，2026-09-23）：stable=规则链+角色+能力
        清单（会话生命周期内稳定，换装后变一次缓存 miss 一次可接受）；dynamic=
        技能指引+任务目标+纪律尾（每任务变化，置于缓存断点之后不破稳定前缀）。
        组装内容与顺序同旧 build_system_prompt，纯拆分零语义变化。"""
        stable_parts = [
            build_rules_preamble(
                self.packs_root, track=self.track, capabilities=self.capabilities,
                owner_tags=self.config.owner_tags, role=self.role_name,
                rule_profiles=self.config.rule_profiles),
            self._role_block(),
        ]
        if self.capability_prompt:
            stable_parts.append(self.capability_prompt)
        dynamic_parts: list[str] = []
        if skill_context:
            dynamic_parts.append(f"# 技能指引\n{skill_context}")
        dynamic_parts.append(f"# 当前任务\n{objective}")
        dynamic_parts.append(STRICT_PROMPT_TAIL)
        return "\n\n".join(stable_parts), "\n\n".join(dynamic_parts)

    def build_system_blocks(self, objective: str, skill_context: str = "") -> list[dict]:
        """M1 prompt caching（retrieval-upgrade，2026-09-23）：system 两块——
        stable 大块末尾打 cache_control ephemeral 断点（Anthropic 协议显式缓存
        标记，Ark 前缀缓存同按前缀命中），dynamic 每任务变化不破稳定前缀。
        provider 侧网关拒收 cache_control 自动降级（400 文案匹配）。"""
        stable, dynamic = self.build_system_parts(objective, skill_context)
        return [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic},
        ]

    def build_system_prompt(self, objective: str, skill_context: str = "") -> str:
        stable, dynamic = self.build_system_parts(objective, skill_context)
        return f"{stable}\n\n{dynamic}"

    def _role_block(self) -> str:
        """角色块：职责（description）+ 人设（persona）+ 软边界自陈。"""
        lines = ["# 角色"]
        if self.role.get("description"):
            lines.append(f"职责：{self.role['description']}")
        if self.role.get("persona"):
            lines.append(str(self.role["persona"]))
        bounds = []
        if self.config.allowed_tools is not None:
            bounds.append(f"工具白名单 {', '.join(self.config.allowed_tools)}")
        if bounds:
            lines.append("软边界（越界须停止并请人类处理）：" + "；".join(bounds))
        return "\n".join(lines)

    def load_skills(self) -> None:
        self.registry = SkillRegistry(self.packs_root)
        self.registry.load()

    def skill_context_for(self, query: str, features: list[str] | None = None,
                          file_features: list[str] | None = None,
                          task_id: str | None = None,
                          task_type: str | None = None,
                          scope: str | None = None) -> str:
        """注入当前项目可用的自包含 Skill 清单（cc 风格渐进披露）。

        角色白名单优先、generalist 全量——每技能注入 name+description 一行
        （渐进披露层 1）；完整正文和同目录资源仍靠 skill_open 按需打开（层 2）。
        全局 packs/kb 不再作为新 Skill 的默认上下文来源。

        审计：skill.routed 事件改记录本轮注入技能清单（payload injected+count，
        历史 route_points/kb_hits 数据仍在旧事件里，旁路统计自然降级）。
        features/file_features 保留签名（调用方透传），路由废弃后不再参与选择。
        E8 会话键快照恢复路径不重注入（回放暂停时点 system，快照自洽）。"""
        if self.registry is None:
            self.load_skills()
        assert self.registry is not None
        role_skills = self.role.get("skills")
        pack_set = set(self.capabilities) | {self.track}
        route_query = f"{query}\n{scope}".strip() if scope else (query or "")
        # 候选集：角色白名单优先；白名单空（generalist）→ 全部启用技能（能力包∪轨）
        enabled = [s for s in self.registry.all()
                   if s.enabled and s.pack in pack_set]
        if role_skills:
            enabled = [s for s in enabled if s.name in role_skills]
        # cc 风格渐进披露：每个技能自带完整 SKILL.md，正文和附属资源仍按需
        # skill_open 读取，避免把所有方法论一次塞进 system。
        if enabled:
            skill_lines = []
            for s in enabled:
                marker = "自包含" if s.is_self_contained else "兼容旧技能"
                resources = len(s.resources)
                suffix = f"；资源 {resources} 个" if resources else ""
                skill_lines.append(f"- {s.name}：{s.description or ''}（{marker}{suffix}）")
            skill_block = (
                "可用技能清单（cc 风格；动手前用 skill_open(name=\"<name>\") "
                "打开完整 SKILL.md，附属资料用 path 按需读取）\n" + "\n".join(skill_lines))
        else:
            skill_block = "（当前无可用技能）"
        # 审计：记录本轮注入技能清单（可观测；旁路统计靠旧事件数据自动降级）
        self.bb.append_event(
            self.project_id, "skill.routed",
            {"name": None, "injected": [s.name for s in enabled],
             "count": len(enabled), "task_id": task_id,
             "query": route_query[:200], "task_type": task_type},
            session_id=self.session["id"], author=self.session["id"])
        return skill_block

    def _skill_digest(self, sk: Any) -> str:
        """命中技能的渐进披露摘要（G1）：正文不整段进 system——给目录 + skill_open
        指针；无标题结构的短正文降级为首段截断。"""
        try:
            body = sk.body()
        except OSError:
            body = ""
        if not body.strip():
            return f"当前命中技能: {sk.name}（{sk.description}；无正文）"
        headings = [l.strip().lstrip("#").strip()
                    for l in body.splitlines()
                    if l.strip().startswith("#") and l.strip().lstrip("#").strip()]
        if headings:
            toc = "\n".join(f"- {h}" for h in headings[:40])
            return (f"当前命中技能: {sk.name}（{sk.description}；正文 {len(body)} 字，"
                    f"目录如下——动手前用 skill_open(\"{sk.name}\") 打开全量）\n{toc}")
        cut = body[:600]
        more = "…（正文截断，用 skill_open(\"%s\") 打开全量）" % sk.name \
            if len(body) > 600 else ""
        return f"当前命中技能: {sk.name}（{sk.description}）\n\n{cut}{more}"

    # ---------- 阶段一：黑板上下文物化（2026-09-28） ----------

    # 物化器注册表（轨道感知分类型物化）：键=内容类型，值为实例方法名。
    # 新增类型（如蓝图 blueprint 落地后）只加一行注册 + 一个 _mat_* 方法，
    # 物化入口与注入点零改动。
    _MATERIALIZERS: dict[str, str] = {
        "assets": "_mat_assets",       # 渗透/CTF 轨：资产（host/domain/service/url/binary）
        "findings": "_mat_findings",   # 两轨：渗透=vuln/intel 发现；逆向=intel 功能发现
        "func_kb": "_mat_func_kb",     # 逆向轨：关键函数 + 功能描述
        "chains": "_mat_chains",       # 两轨：攻击链/假设
        "dead_end": "_mat_dead_end",   # 两轨：已排除方向（false-positive 路标）
        "blueprint": "_mat_blueprint",  # 蓝图轨（R4 表，2026-09-28）：模块级业务功能↔函数锚点
    }

    # 轨道 → 优先物化类型（其余类型仍按命中注入，只是此表类型排前 + 提高 cap）
    _TRACK_PREFERRED: dict[str, tuple[str, ...]] = {
        "research": ("blueprint", "func_kb", "findings", "chains", "dead_end"),
        "pentest": ("assets", "findings", "chains", "dead_end"),
        "redteam": ("assets", "findings", "chains", "dead_end"),
        "ctf": ("assets", "findings", "chains", "dead_end"),
    }

    def _blackboard_materialize(self, query: str, *, cap_chars: int = 2200) -> str:
        """黑板上下文物化（阶段一，仅检索式）：任务/对话启动时按 objective 检索
        黑板强相关内容，分级注入 system——① 一级：verified + confidence≥0.6 +
        强匹配（标题/值精确子串），注入明细；② 二级：同 host/binary 关联，一行
        摘要；③ 三级：仅计数提示行（控制 token，Agent 需要时 bb_query 自取）。
        按轨道差异化分发（research→func_kb 优先，其余→assets/findings 优先）；
        blueprint 物化器预留（挂起未实施返回空）。任一物化器异常静默降级。
        返回物化文本块（无命中返回空串）。"""
        if not query or not query.strip():
            return ""
        q = query.strip()
        try:
            # 本会话最近一次物化计数（事件流可观测：命中类型/条数）
            hits: dict[str, int] = {}
            blocks: list[str] = []
            order = self._TRACK_PREFERRED.get(self.track, ("assets", "findings", "chains", "dead_end"))
            for key in order:
                method = getattr(self, self._MATERIALIZERS[key], None)
                if method is None:
                    continue
                try:
                    block, n = method(q)
                except Exception:  # noqa: BLE001 —— 单个物化器失败不影响其他类型
                    log.exception("黑板物化失败 type=%s", key)
                    continue
                if block:
                    blocks.append(block)
                    hits[key] = n
            if not blocks:
                return ""
            # 落一条物化审计事件（可观测：命中类型与条数；不落正文防事件膨胀）
            try:
                self.bb.append_event(
                    self.project_id, "bb.materialized",
                    {"session_id": self.session["id"], "query": q[:150],
                     "hits": hits, "chars": sum(len(b) for b in blocks)},
                    session_id=self.session["id"], author=self.session["id"])
            except Exception:  # noqa: BLE001
                pass
            joined = "\n\n".join(blocks)
            if len(joined) > cap_chars:
                joined = joined[:cap_chars] + "\n…（黑板物化超长截断，可 bb_query 取详情）"
            return f"## 🧠 黑板物化（本次相关，动手前先读）\n{joined}"
        except Exception:  # noqa: BLE001 —— 物化整体失败不影响任务主链
            log.exception("黑板物化失败（整体）")
            return ""

    # ---- 各类型物化器：返回 (文本块, 命中条数)；无命中返回 ("", 0) ----

    def _mat_assets(self, q: str) -> tuple[str, int]:
        """渗透/CTF 轨：资产物化——type/value 子串匹配 + 父子链聚合。
        一级：强匹配资产明细；二级：同 host 聚合摘要。"""
        try:
            assets = self.bb.list_assets(self.project_id)
        except Exception:  # noqa: BLE001
            return "", 0
        if not assets:
            return "", 0
        ql = q.lower()
        by_id = {a["id"]: a for a in assets}

        def host_val(aid: str | None) -> str:
            a = by_id.get(aid or "")
            while a:
                if a["type"] == "host":
                    return str(a["value"])
                a = by_id.get(a.get("parent_id") or "")
            return ""

        strong: list[str] = []
        by_host: dict[str, list[str]] = {}
        for a in assets:
            val = str(a.get("value") or "")
            if val.lower() and val.lower() in ql:  # 一级：值精确子串命中
                strong.append(f"- {a['type']}:{val}（{a.get('status') or 'open'}）")
            elif host_val(a.get("id")):
                by_host.setdefault(host_val(a.get("id")), []).append(
                    f"{a['type']}:{val}")
        lines: list[str] = []
        for s in strong[:8]:
            lines.append(s)
        for hv, vals in list(by_host.items())[:6]:
            lines.append(f"- 📦 {hv}：{'、'.join(vals[:4])}" +
                         (f"（+{len(vals) - 4}）" if len(vals) > 4 else ""))
        if not lines:
            return "", 0
        return ("### 资产\n" + "\n".join(lines)), len(strong) + sum(len(v) for v in by_host.values())

    def _mat_findings(self, q: str) -> tuple[str, int]:
        """发现物化：verified + 标题/危害子串匹配（一级），同 host 聚合（二级）。
        渗透=vuln/intel；逆向=intel（功能发现）。severity 高优先展示。"""
        try:
            findings = self.bb.list_findings(self.project_id, verified_only=True)
        except Exception:  # noqa: BLE001
            return "", 0
        if not findings:
            return "", 0
        ql = q.lower()
        assets = {}
        try:
            assets = {a["id"]: a for a in self.bb.list_assets(self.project_id)}
        except Exception:  # noqa: BLE001
            pass
        sev = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        ranked = sorted(findings, key=lambda f: sev.get(f.get("severity"), 0), reverse=True)
        strong: list[str] = []
        by_host: dict[str, list[str]] = {}
        for f in ranked:
            title = str(f.get("title") or "")
            impact = str(f.get("impact") or "")
            av = (assets.get(f.get("target_asset_id")) or {}).get("value", "")
            blob = (title + " " + impact).lower()
            if title.lower() and title.lower() in ql:  # 一级：标题精确子串
                strong.append(
                    f"- [{f.get('severity')}] {title}" +
                    (f" → {impact[:60]}" if impact else ""))
            elif av and str(av).lower() in ql:
                by_host.setdefault(str(av), []).append(
                    f"[{f.get('severity')}]{title[:40]}")
        lines: list[str] = []
        for s in strong[:8]:
            lines.append(s)
        for av, titles in list(by_host.items())[:6]:
            lines.append(f"- 📦 {av}：{'、'.join(titles[:4])}" +
                         (f"（+{len(titles) - 4}）" if len(titles) > 4 else ""))
        if not lines:
            return "", 0
        return ("### 已验证发现\n" + "\n".join(lines)), len(strong) + sum(len(v) for v in by_host.values())

    def _mat_func_kb(self, q: str) -> tuple[str, int]:
        """逆向轨：关键函数物化——函数名/功能描述子串匹配（一级），按 confidence 排序。"""
        try:
            funcs = self.bb.list_funcs(self.project_id)
        except Exception:  # noqa: BLE001
            return "", 0
        if not funcs:
            return "", 0
        ql = q.lower()
        hits: list[dict] = []
        for f in funcs:
            name = str(f.get("name") or "")
            analysis = str(f.get("analysis") or "")
            if name.lower() and name.lower() in ql:
                hits.append(f)
            elif analysis.lower() and any(t in analysis.lower() for t in ql.split() if len(t) >= 3):
                hits.append(f)
        hits.sort(key=lambda f: float(f.get("confidence") or 0), reverse=True)
        if not hits:
            return "", 0
        lines = []
        for f in hits[:10]:
            name = f.get("name") or ""
            addr = f.get("address")
            addr_s = f"@{addr:#x}" if isinstance(addr, int) else ""
            tags = (f.get("risk_tags") or [])[:3]
            tag_s = f" 风险:{'、'.join(tags)}" if tags else ""
            conf = float(f.get("confidence") or 0)
            conf_s = f" 置信:{conf:.0%}" if conf else ""
            desc = str(f.get("analysis") or "")[:80]
            lines.append(f"- {name}{addr_s}{tag_s}{conf_s}" + (f"｜{desc}" if desc else ""))
        return ("### 关键函数（func_kb）\n" + "\n".join(lines)), len(hits)

    def _mat_chains(self, q: str) -> tuple[str, int]:
        """攻击链/假设物化：goal/name 子串匹配，未 exploited 的假设优先提示。"""
        try:
            chains = self.bb.list_chains(self.project_id)
        except Exception:  # noqa: BLE001
            return "", 0
        if not chains:
            return "", 0
        ql = q.lower()
        lines = []
        n = 0
        for c in chains:
            blob = (str(c.get("name") or "") + " " + str(c.get("goal") or "")).lower()
            if not blob or ql not in blob:
                continue
            status = c.get("status") or "hypothesis"
            mark = "🔄" if status == "exploited" else "🔬"
            lines.append(f"- {mark} {c.get('name')}（{status}）｜{str(c.get('goal') or '')[:60]}")
            n += 1
            if n >= 6:
                break
        if not lines:
            return "", 0
        return ("### 攻击链/假设\n" + "\n".join(lines)), n

    def _mat_dead_end(self, q: str) -> tuple[str, int]:
        """已排除方向物化：false-positive 路标，scope/目标子串匹配。"""
        try:
            fps = [f for f in self.bb.list_findings(self.project_id)
                   if f["status"] == "false-positive"]
        except Exception:  # noqa: BLE001
            return "", 0
        if not fps:
            return "", 0
        ql = q.lower()
        lines = []
        n = 0
        for f in fps:
            title = str(f.get("title") or "")
            if not title.lower() or title.lower() not in ql:
                continue
            lines.append(f"- ⛔ {title[:70]}")
            n += 1
            if n >= 5:
                break
        if not lines:
            return "", 0
        return ("### 已排除方向（勿重走）\n" + "\n".join(lines)), n

    def _mat_blueprint(self, q: str) -> tuple[str, int]:
        """蓝图物化（R4 blueprints 表，2026-09-28）：模块级业务功能↔函数锚点。
        按 objective 子串匹配蓝图 name/goal 与模块 desc/spec；research 轨优先。
        展示：蓝图名（目标+状态）→ 命中模块（desc + func_addresses 锚点数）。
        无蓝图/无命中返回空。"""
        try:
            bps = self.bb.list_blueprints(self.project_id)
        except Exception:  # noqa: BLE001
            return "", 0
        if not bps:
            return "", 0
        ql = q.lower()
        lines: list[str] = []
        n = 0
        for bp in bps:
            name = str(bp.get("name") or "")
            goal = str(bp.get("goal") or "")
            status = str(bp.get("status") or "draft")
            head_hit = (name.lower() and name.lower() in ql) \
                or (goal.lower() and goal.lower() in ql)
            mods = bp.get("modules") or []
            hit_mods: list[str] = []
            for m in mods:
                if not isinstance(m, dict):
                    continue
                mname = str(m.get("name") or "")
                mdesc = str(m.get("desc") or "")
                if (mname.lower() and mname.lower() in ql) \
                        or (mdesc.lower() and any(
                            t in mdesc.lower() for t in ql.split() if len(t) >= 3)):
                    anchors = len(m.get("func_addresses") or [])
                    hit_mods.append(f"    - {mname}" +
                                    (f"（{anchors} 函数锚点）" if anchors else ""))
            if not head_hit and not hit_mods:
                continue
            lines.append(f"- 📐 {name}（{status}）" + (f"｜{goal[:50]}" if goal else ""))
            lines += hit_mods[:4]
            n += 1
            if n >= 4:
                break
        if not lines:
            return "", 0
        return ("### 蓝图（模块级业务功能）\n" + "\n".join(lines)), n

    # ---------- C6 漏洞核对 hook（AI 登记漏洞前自我对照红线/评级规则） ----------

    def _build_vuln_gate(self, gate_llm: Any):
        """漏洞核对 hook 工厂：Agent 触发 category=vuln 登记时，先让 LLM 对照
        红线/评级规则自我核对——不合格降级 intel（不进漏洞视图）。
        LLM 失败/不可用 → 降级跳过（照常登记、回执标注），与 F11 降级哲学一致。
        LLM 调用面复用 core.skills.judge.judge_finding（与存量清洗脚本同口径）。"""
        def gate(draft: dict) -> tuple[bool, str]:
            sys_p = (
                "你是漏洞合规审核员。候选发现要登记为「漏洞」，请对照以下红线与"
                "评级规则核对其是否够格：有可验证的安全问题、方向未被禁止、"
                "证据链成立。证据不足/方向被禁/属于信息提示或合规提示而非漏洞 → "
                "compliant=false。只输出 JSON："
                '{"compliant": true|false, "reason": "一句话依据"}')
            v = judge_finding(gate_llm, rules_text=build_rules_preamble(
                self.packs_root, track=self.track, capabilities=self.capabilities,
                owner_tags=self.config.owner_tags,
                rule_profiles=self.config.rule_profiles),
                draft=draft, sys_prompt=sys_p)
            if v is None:
                return True, "核对跳过：审核响应无 JSON"
            return bool(v.get("compliant")), str(v.get("reason") or "未通过漏洞标准核对")
        return gate

    # ---------- 运行 ----------

    def request_pause(self) -> None:
        """请求软暂停（DESIGN.md §3）：当前 LLM 步做完即停，恢复后从快照续跑。"""
        self._pause_req.set()

    def request_abort(self) -> None:
        """请求硬中断：当前步做完 → 任务 fail（人工中断）→ 会话空闲。"""
        self._abort_req.set()
        self._pause_req.clear()  # 中断优先，避免同一检查点被当成暂停

    def run_task(self, objective: str, features: list[str] | None = None,
                 file_features: list[str] | None = None,
                 task_id: str | None = None) -> str:
        """执行一个目标（任务）。task_id 给定时：open 则自动认领，已被他人持有则拒绝。
        返回空串 = 暂停/中断退出（任务收尾已由检查点处理，不得再走 _finalize）。"""
        stale_refs: list[str] = []
        old_plan_notice: str | None = None  # C1：重认领旧任务的计划/发现注入
        dead_end_notice: str | None = None  # C3：跨轨路标三级注入
        resumed: dict[str, Any] | None = None  # C6：任务键断点快照（认领即复活）
        if task_id:
            task = self.tq.get_task(task_id)
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")
            if task["claimed_by"] != self.session["id"]:
                if task["status"] == "open":
                    self.tq.claim(task_id, self.session["id"],
                                  lease_minutes=self.config.lease_minutes)
                    task = self.tq.get_task(task_id)
                else:
                    raise ValueError(
                        f"任务 {task_id} 由 {task['claimed_by']} 持有（{task['status']}），"
                        f"本会话不可执行")
            # 撤回传播（§6.7 的 1.6）：认领/接手时若依据已被推翻，首条消息必带
            # 强制自警告警（open 任务只挂标无私信，这里按 stale_refs 现场补水）。
            # 同会话同任务只告一次，暂停快照续跑不重复（快照里已有告警）。
            stale_refs = list(task.get("stale_refs") or [])
            self.dispatcher.current_task_id = task_id
            self._start_heartbeat(task_id)
            # C6 认领即复活：任务键断点快照命中（objective 未被改）→ 跳过
            # handover/old_plan/dead_end/objective 注入链（快照历史里已有）。
            resumed = self._load_task_resume(task_id, task)
            if resumed is None:  # 无快照/快照被清（objective 被改降级）→ 常规注入链
                old_plan_notice = self._old_plan_notice(task)
                dead_end_notice = self._dead_end_notice(task)
            # v14 认领即换装：任务带 role 且 ≠ 当前执行角色 → 先换装再建 system prompt
            #（build_system_prompt/_role_block/skill_context_for 按调用时点取值，全链生效）
            self._apply_task_persona(task, task_id)
        skill_ctx = self.skill_context_for(
            objective, features, file_features, task_id=task_id,
            # 认领路径增强：task_type 加分 + scope 拼路由 query（task 行已取过）
            task_type=(task or {}).get("task_type") if task_id else None,
            scope=(task or {}).get("scope") if task_id else None)
        self._last_skill_context = skill_ctx  # v0.71：热换装重建 system 时复用
        # 阶段一（2026-09-28）：任务认领期黑板物化——检索式注入强相关事实（资产/
        # 发现/函数/链/死路），objective 为 query；E8 快照恢复不物化（快照自洽）。
        if not resumed:
            materialized = self._blackboard_materialize(objective)
            if materialized:
                skill_ctx = f"{skill_ctx}\n\n{materialized}".strip()
        system = self.build_system_blocks(objective, skill_ctx)
        # C6：复活时 messages 整体取快照（不与 transcript 拼接，防 tool_use/result
        # 错位）；否则 C10 transcript 接手（末 60 条）。E8 resume 路径不经 run_task，互斥。
        transcript_msgs: list[dict[str, Any]] = []
        if task_id and resumed is None:
            transcript_msgs = self._load_task_transcript(task_id)
        messages: list[dict[str, Any]] = list(resumed["messages"]) if resumed \
            else list(transcript_msgs)
        start_step = int(resumed["next_step"]) if resumed else 1
        if resumed is not None:
            self.dispatcher.max_steps = int(resumed["max_steps"])  # 预算断点还原
            clear_task_resume(self.artifacts_dir, task_id or "")  # 消费即删
        # 认领期消费收件箱（2026-09-19 轮末语义：human_note 不再于步边界中途
        # 注入，延迟到此处/恢复期——认领即拿到上一轮期间投递的人类引导）。
        # 口径统一走订阅声明表 _INBOX_SUBSCRIPTIONS（2026-09-21）。basis_stale
        # （强制三选一）合并任务挂标（open 任务只挂标无私信，按 stale_refs 现场
        # 补水）与收件箱私信两条来源；_stale_alerted 去重只对任务挂标部分生效
        # ——纯私信形态（任务行 stale_refs 已清）不再被 stale_refs 门控吞掉。
        if task_id:
            notices, _drained = self._drain_inbox("claim", stale_refs=stale_refs)
            for _kind, notice in notices:
                messages.append({"role": "user", "content": notice})
            if stale_refs and any(k == "basis_stale" for k, _ in notices):
                self._stale_alerted.add(task_id)
        if resumed is not None:
            # ⚡ 续跑标记：历史工具结果属旧现场不可重放，需重新取证
            messages.append({"role": "user", "content":
                f"⚡ 已从中断现场续跑（第 {resumed['next_step']} 步起；"
                "历史工具结果属旧现场不可重放，需重新取证）。"})
        else:
            handover = (self._handover_notice(task, bool(transcript_msgs))
                        if task_id else None)  # C10：第 N 次尝试接手提示
            if handover:
                messages.append({"role": "user", "content": handover})
            if task_id:
                # 阶段四 M1 跨窗方向指纹（2026-09-28）：本任务历次认领会话的命令
                # 聚合（精确计数排行 cap 10 + 首词聚类 cap 15，≤20 行）——接手窗
                # 进场即知已试方向，避免跨窗重踩（stuck-convergence M2 定稿落地）。
                fp = self._direction_fingerprint(task_id)
                if fp:
                    messages.append({"role": "user", "content": fp})
                # 阶段四 M3 接手黑板视角（2026-09-28）：接手窗无本会话记忆，把黑板
                # 知识侧（已验证 finding + 已排除死路，跨窗沉淀）作为 user 消息注入
                # ——与 transcript（对话现场）互补，与阶段一 system 物化（通用检索）
                # 区分：此处聚焦跨窗已验证结论，接手即知哪些已坐实。
                bv = self._handover_blackboard_view(task)
                if bv:
                    messages.append({"role": "user", "content": bv})
            if old_plan_notice:
                messages.append({"role": "user", "content": old_plan_notice})
            if dead_end_notice:
                messages.append({"role": "user", "content": dead_end_notice})
            att_lines = _attachment_lines(
                ((task or {}).get("context") or {}).get("attachments")) \
                if task_id else []
            if att_lines:  # 附件随发（2026-09-19）：发布时经 API 层校验的 artifact 引用
                messages.append({"role": "user", "content":
                    "📎 任务附件（可用 run_cmd 读取，只读勿改写，工作区相对路径）：\n"
                    + "\n".join(att_lines)})
            pref_runtime = str(task.get("preferred_runtime") or "") if task_id else ""
            if pref_runtime:
                # v23（TRAE 新壳 M3）：任务默认运行时——run_cmd 省略 runtime 即按此
                # 执行；单条命令显式传 runtime 仍可临时覆盖
                messages.append({"role": "user", "content":
                    f"🎛️ 本任务默认执行运行时={pref_runtime}：run_cmd 可省略 runtime"
                    f"（按 {pref_runtime} 执行）；单条命令确需其他运行时时显式传 "
                    "runtime 即临时覆盖。"})
            messages.append({"role": "user", "content": objective})
        if task_id:
            self._checkpoint_task_transcript(messages, objective)  # 认领即有现场
        try:
            summary = self._loop(system, messages, objective, start_step=start_step)
        except Exception as e:
            # 兜底：异常穿出主循环（典型=LLM 传输层重试耗尽）必须收尾任务并停心跳，
            # 否则任务悬在 claimed + 孤儿心跳续租占坑（本进程内永久「执行中」）。
            self._fail_task_on_error(e)
            raise
        if summary is None:  # 暂停（心跳保留，继续占任务）/中断（_abort_current_task 已停心跳）
            return ""
        if self.dispatcher.finished:
            self._finalize()
            return summary
        # 委托收尾、会话存活：先恢复底色 persona（防带任务角色待命），再把
        # 「委托目标 + 收尾摘要」沉淀进会话对话历史——后续对话轮即带本件上下文
        # （格式与 run_chat 问答对一致）。
        self._restore_base_persona()
        self._append_chat_to_session(
            [f"🧭 委托：{objective}"],
            summary or self.dispatcher.last_delegation_note or "委托已收尾")
        return summary

    def run_session(self) -> str | None:
        """会话轮（会话中心化编排，docs/plans/session-centric-orchestration.md §4.2）：
        ① 窗内有 open 委托 → 取队首起跑执行（完整工具面）；
        ② 无委托但收件箱有可回应消息 → 对话回应（工具面一致）；
        ③ 都没有 → None 空退（窗回待命，事件驱动，非常驻进程）。
        委托做完由 worker while 再入本方法，自动接窗内队列下一件。

        暂停/中断请求 → 不领新任务并落状态；有快照 → 优先续跑被暂停的任务。
        快照续跑（E8）不经 run_task，与任务现场（transcript）接手路径天然互斥。"""
        self._restore_base_persona()  # v14 兜底闸：任何遗漏恢复路径不得带 persona 接新委托
        if self._abort_req.is_set():
            self._abort_current_task()   # 空闲路径：无任务则只清标志 + 落审计
            return None
        if self._pause_req.is_set():
            self._pause_req.clear()
            self._enter_paused()         # 任务间暂停：无快照，恢复后正常取队列
            return None
        if self._stop_after_task or self.paused:
            self._stop_after_task = False
            if self.paused:
                # 暂停闸空退留审计（2026-09-27 修「引导石沉大海」排查难）：
                # 此前静默 return，出问题只能靠快照 mtime 反推；正常流不该
                # 踢到暂停会话（note 端点已改走恢复语义、调度器跳过 paused
                # 行），触发即说明上游有闸漏，事件流必须可见。
                try:
                    self.bb.append_event(
                        self.project_id, "session.work_state",
                        {"session_id": self.session["id"], "armed": False,
                         "paused": True, "note": "暂停闸拦截：踢起 worker 空退"},
                        session_id=self.session["id"], author=self.session["id"])
                except Exception:  # noqa: BLE001 —— 审计失败不影响空退路径
                    log.exception("暂停闸审计事件落盘失败 sid=%s", self.session["id"])
            return None                  # 中断收尾闸门 / 暂停态不领新任务
        if self._resume_state is not None:
            st, self._resume_state = self._resume_state, None
            self._clear_snapshot()  # 快照已消费（文件+指针清理；失败留垃圾不影响主流程）
            # C6 生命周期：E8 会话键恢复消费时同步删任务键快照（防「恢复后又 fail」
            # 时 rewind 到旧暂停点——现场以最新一次暂停为准）
            clear_task_resume(self.artifacts_dir, st.get("task_id") or "")
            task = self.tq.get_task(st["task_id"])
            if (task and task["claimed_by"] == self.session["id"]
                    and task["status"] == "claimed"):
                self.dispatcher.current_task_id = st["task_id"]
                # v14：重启/恢复后先按任务行重换装（快照 system 生成于暂停时点、
                # 已含任务角色 prompt 段；此处补齐 config/dispatcher 执行边界一致）。
                # 同进程暂停恢复时 persona 未摘 → 幂等 no-op。
                self._apply_task_persona(task, st["task_id"])
                if self._heartbeat is None:  # 兜底：暂停期心跳本应保留，缺失则补起
                    self._start_heartbeat(st["task_id"])
                # E8：暂停期积压的私信（含 human_note 人类引导 / agent_message 私信 /
                # task_receipt 回执，2026-09-20 对话化）随快照恢复一并注入；口径统一
                # 走订阅声明表（2026-09-21）——原全量 drain 会吞掉不显示，本起补齐。
                notices, _dr = self._drain_inbox("resume")
                for _kind, notice in notices:
                    st["messages"].append({"role": "user", "content": notice})
                pending_intents = st.get("open_intents") or []
                if pending_intents:  # 意图纪律①：续跑即提醒照单收尾
                    lines = "\n".join(
                        f"- {it.get('statement', '')}（{it.get('id', '')}）"
                        for it in pending_intents[:10]
                        if isinstance(it, dict))
                    st["messages"].append({"role": "user", "content":
                        f"🧾 收尾提醒：本项目有 {len(pending_intents)} 个意图未收尾，"
                        f"续跑中必须逐个 close_intent：\n{lines}"})
                try:
                    summary = self._loop(st["system"], st["messages"], st["objective"],
                                         start_step=st["next_step"])
                except Exception as e:
                    self._fail_task_on_error(e)
                    raise
                if summary is None:
                    return None          # 恢复后立刻又被暂停/中断
                if self.dispatcher.finished:
                    self._finalize()
                else:  # 委托收尾、会话存活：恢复底色 + 沉淀进会话对话历史
                    self._restore_base_persona()
                    self._append_chat_to_session(
                        [f"🧭 委托：{st['objective']}"],
                        summary or self.dispatcher.last_delegation_note or "委托已收尾")
                return summary
            # 快照失效（租约被回收/他人持有）：丢弃快照，落到正常取队列
        self.last_claim_idle = False  # 进入会话轮：暂停/中断早退路径不得残留旧 True
        task_id = self.tq.take_session_next(
            self.project_id, self.session["id"],
            lease_minutes=self.config.lease_minutes)
        if task_id is not None:
            task = self.tq.get_task(task_id)
            try:
                return self.run_task(task["objective"], task_id=task_id)
            except Exception as e:
                # 覆盖 run_task 内起跑后、_loop 前的异常窗口（_fail_task_on_error 幂等，
                # _loop 内已兜过则此处只重复停心跳）
                self._fail_task_on_error(e)
                raise
        # 无委托：对话回应——无消息可回应 → run_chat None → 真空闲空退
        reply = self.run_chat()
        if reply is not None:
            return reply
        # 真空闲：DB 状态回 idle（旧流路由 _finalize 落，现委托收尾不结束会话，
        # 在此统一落）。closed/paused 不覆盖。
        cur = self.bb.get_session(self.session["id"])
        if (cur or {}).get("status") not in ("closed", "paused"):
            try:
                self.bb.set_session_status(self.session["id"], "idle")
            except Exception:  # noqa: BLE001
                log.exception("set_session_status(idle) 失败")
        self.last_claim_idle = True  # 批 5：仅真空闲退出才允许触发 L2 续 tick
        return None

    def run_chat(self) -> str | None:
        """会话对话轮（会话中心化编排，docs/plans/session-centric-orchestration.md
        §4.2）：窗内无 open 委托且收件箱有可回应消息（human_note 人类引导 /
        agent_message 私信 / 回执等，口径走 _INBOX_SUBSCRIPTIONS chat 档）时，
        drain 消息 → LLM 循环（完整工具面，与委托轮一致）→ 纯文本回复落
        agent.chat 事件并返回文本。

        历史统一存会话级 chat-<sid>.json（末 60 条上下文、轮末回写问答对；
        已完成委托的「目标+收尾摘要」由 run_task 收尾时沉淀进同一文件，对话
        天然带着干过的活的上下文）。回复流式（stream_text=True，
        agent.chat.delta 过渡行 + 终稿清剪）；步数上限
        min(dispatcher.max_steps, config.chat_max_steps)；干活纪律：有价值的
        阶段性结论 bb_add_finding 入图（无委托上下文 → 无 basis 边，图上以
        targets/relates_to 连通，DESIGN §6）。

        返回 None = 无消息可回应 / 暂停·中断置位不消费 / 对话中被中断（drain
        掉的消息已消费、回复丢弃）。本方法不落快照、不起心跳、不动
        dispatcher.current_task_id / finished；finish 不在 CHAT_TOOLS，纯文本
        回复即终止。worker while 在返回非 None 后再入会话轮——连发消息逐轮回
        应，天然成连续对话。"""
        if self._abort_req.is_set() or self._pause_req.is_set() or self.paused:
            return None  # 暂停/中断/暂停态：不消费引导（留给任务轮/恢复期）
        # 收件箱消费口径统一走订阅声明表 _INBOX_SUBSCRIPTIONS（2026-09-21）：对话
        # 轮只取无任务上下文也能响应的 kind——basis_stale（强制三选一）与
        # finding_update（发现增补）留收件箱，等下一轮认领期/步边界消费。
        notices, drained = self._drain_inbox("chat")
        parts = [p for _k, p in notices]
        if not parts:
            return None
        note_text = "\n".join(
            str((r.get("payload") or {}).get("text", "")).strip()
            for r in drained if r.get("kind") == "human_note").strip()
        # 对话即指令（阶段二，2026-09-28）：人类引导首轮意图判定——「任务」语义
        # 自动升级为绑定本会话的任务（worker 下一轮 take_session_next 自动起跑
        # run_task），「对话」语义走原对话流程。判定失败回落对话（宁当对话不误派活）。
        if note_text:
            intent, objective = self._classify_human_intent(note_text)
            if intent == "task" and objective:
                atts = [a for r in drained
                        for a in ((r.get("payload") or {}).get("attachments") or [])]
                try:
                    self.tq.publish(
                        self.project_id, objective,
                        task_type="generic", noise_budget="passive",
                        target_session=self.session["id"],
                        attachments=atts or None,
                        created_by="human")
                    self.bb.append_event(
                        self.project_id, "agent.intent",
                        {"session_id": self.session["id"], "intent": "task",
                         "objective": objective[:300],
                         "note": note_text[:200]},
                        session_id=self.session["id"], author=self.session["id"])
                    return f"已受理为任务，自动起跑（目标：{objective[:80]}）"
                except Exception as e:  # noqa: BLE001 —— 发布失败不阻断对话
                    log.warning("对话转任务发布失败，回落对话: %s", e)
                    parts.append(f"（你这句话按任务处理失败：{e}；可改述后重发）")
        # 历史：复盘窗口优先读取任务现场，普通窗口读取会话级 chat 文件。
        # 两者都经 sanitize_snapshot_tail 清理悬空 tool-use 半对，避免把历史污染带回上游。
        meta = self.session.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        context_task_id = meta.get("context_task_id") if isinstance(meta, dict) else None
        history_source = self._load_task_transcript(str(context_task_id)) if context_task_id else self._load_session_chat()
        history = sanitize_snapshot_tail(history_source)
        if history:
            chat_mode = (("任务复盘模式：以下是该任务执行现场的历史消息（仅作事实参考，"
                          "不要自动重放历史工具/命令）；" if context_task_id else "") +
                         "延续模式：以下是本会话既往对话记录（截尾保留，含此前委托的"
                         "收尾摘要），接着此上下文回应；可继续用工具查黑板/读工作区文件、"
                         "跑命令核实。阶段性结论要落发现时同样走意图纪律：先 "
                         "declare_intent 声明（侦察结论登记可在 statement 写明依据），"
                         "做一次核实动作后再 bb_add_finding（带 evidence，relates_to "
                         "串起推导链自动上图；没验证过的标 unverified）。干完必须给"
                         "人类文字结论。回答简洁，直接回应人类。")
        else:
            chat_mode = ("与人类直接对话（Claude Code 式工作窗）：回答引导/提问，需要时"
                         "用工具查黑板（资产/发现/事件/知识库）或跑命令核实再作答。"
                         "阶段性结论要落发现时同样走意图纪律：先 declare_intent 声明"
                         "（侦察结论登记可在 statement 写明依据），做一次核实动作后再 "
                         "bb_add_finding（带 evidence，relates_to 串起推导链自动上图；"
                         "没验证过的标 unverified）。干完必须给人类文字结论。回答简洁，"
                         "直接回应人类。")
        system = self.build_system_blocks(chat_mode, self.skill_context_for(note_text))
        # 阶段一（2026-09-28）：对话轮黑板物化——human_note 为 query 注入强相关
        # 事实（与任务认领期同管线），让对话/引导也能吃到黑板沉淀。
        materialized = self._blackboard_materialize(note_text)
        if materialized:
            system = [*system,
                      {"type": "text", "text": materialized}]
        messages: list[dict[str, Any]] = list(history)
        messages.append({"role": "user", "content": "\n".join(parts)})
        final_text: str | None = None
        max_steps = min(self.dispatcher.max_steps, self.config.chat_max_steps)
        step = 0
        while step < max_steps:
            if self._abort_req.is_set():
                self._abort_req.clear()  # 无任务：只清标志，不走 _abort_current_task
                return None  # drain 已消费，回复丢弃（abort 语义：本轮作废）
            step += 1
            try:
                resp = self._chat_interruptible(messages, system=system,
                                                tools=CHAT_TOOLS, stream_text=True)
            except _StepInterrupted:
                # 对话中被中断（LLM 等待期间置位）：半截回复已由
                # _flush_interrupted_reply 落盘（interrupted=true），引导已 drain
                self._abort_req.clear()
                return None
            if self._abort_req.is_set():
                # 对话中被中断（LLM 调用期间置位，含用户 ■ 硬中断）：回复丢弃
                self._abort_req.clear()
                return None
            self._record_usage(resp, source="agent", llm_obj=self.llm)
            self._emit_final_thinking(resp, step, None)
            messages.append(assistant_message(resp))
            if not resp.tool_calls:
                text = resp.text.strip()
                if text:  # 回复落事件流（💬 Agent 回复），空文本防死循环直接退
                    sid = resp.raw.get("_stream_id") if isinstance(resp.raw, dict) else None
                    self.bb.append_event(
                        self.project_id, "agent.chat",
                        {"session_id": self.session["id"], "text": text,
                         **({"stream_id": sid} if sid else {})},
                        session_id=self.session["id"], author=self.session["id"])
                    if sid:
                        try:
                            self.bb.prune_chat_deltas(self.project_id, sid)
                        except Exception:  # noqa: BLE001 —— 只多留过渡行
                            log.exception("agent.chat.delta 清剪失败 stream_id=%s", sid)
                final_text = text or None
                break
            tool_results = []
            for tc in resp.tool_calls:
                result_text = self.dispatcher.dispatch(tc.name, tc.arguments)
                tool_results.append(self.llm.tool_result_message(tc, result_text)["content"][0])
            messages.append(tool_results_message(tool_results))
            self._trim(messages)
        if final_text:
            # 问答对回写会话历史（下轮对话/重启后仍带全上下文）；本轮带工具的
            # 中间步不回写（只留问答对）。
            self._append_chat_to_session(parts, final_text)
        return final_text

    def _classify_human_intent(self, note_text: str) -> tuple[str, str]:
        """对话即指令（阶段二）：判定人类引导是「任务」还是「对话」。

        返回 (intent, objective)：intent ∈ {"task","chat"}；task 时 objective 为
        提炼后的可执行目标（失败回落原文）。判定模型优先 planner_llm（省主模型
        token），缺失回落 self.llm；任何异常回落 chat（宁当对话不误派活）。
        """
        llm = self.planner_llm or self.llm
        prompt = (
            "你是任务意图分类器。判断一条发给 Agent 的人类消息是「任务」还是「对话」。\n"
            "任务（task）：明确要求执行具体工作（分析/扫描/编写/测试/修改/调查某个目标等），"
            "有可执行的目标；\n"
            "对话（chat）：闲聊、提问、澄清、确认、询问状态、给背景、讨论方案、说谢谢/好的等。\n"
            "只输出一个 JSON 对象，不要任何其他文字：\n"
            '{"intent": "task"|"chat", "objective": "task 时的一句话可执行目标，chat 时留空"}'
        )
        try:
            resp = llm.chat(
                [{"role": "user", "content": f"人类消息：\n{note_text[:1500]}"}],
                system=prompt)
            self._record_usage(resp, source="planner", llm_obj=llm)
            m = re.search(r"\{.*\}", resp.text or "", re.DOTALL)
            data = json.loads(m.group(0)) if m else None
            intent = str((data or {}).get("intent", "")).strip().lower()
            if intent == "task":
                objective = str((data or {}).get("objective", "")).strip()
                return "task", objective or note_text
            return "chat", ""
        except Exception as e:  # noqa: BLE001 —— 判定失败回落对话
            log.warning("对话意图判定失败，回落对话: %s", e)
            return "chat", ""

    def _load_session_chat(self) -> list[dict[str, Any]]:
        """会话级对话历史（2026-09-20 对话化）：非绑定窗 run_chat 的上下文来源。
        防御同 _load_task_transcript：JSON 损坏/结构异常 → 空列表降级；只取末
        60 条（更早内容以黑板 finding/事件为准）。"""
        path = session_chat_path(self.artifacts_dir, self.session["id"])
        if path is None or not path.exists():
            return []
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        msgs = st.get("messages") if isinstance(st, dict) else None
        if not isinstance(msgs, list):
            return []
        return [m for m in msgs if isinstance(m, dict)][-60:]

    def _append_chat_to_session(self, notes: list[str], reply: str) -> None:
        """把一轮对话问答追加进 chat-<sid>.json（2026-09-20 对话化；原子写，
        文件不存在则创建，120 条滚动上限，失败降级不影响回复送达）。messages
        只追加 user/assistant 两条，与任务现场文件同构（sanitize_snapshot_tail
        可直接消费）。"""
        path = session_chat_path(self.artifacts_dir, self.session["id"])
        if path is None:
            return
        try:
            if path.exists():
                st = json.loads(path.read_text(encoding="utf-8"))
                if not (isinstance(st, dict) and isinstance(st.get("messages"), list)):
                    st = {"session_id": self.session["id"], "messages": []}
            else:
                st = {"session_id": self.session["id"], "messages": []}
            st["messages"].extend([
                {"role": "user", "content": "\n".join(notes)},
                {"role": "assistant", "content": reply},
            ])
            if len(st["messages"]) > 120:
                st["messages"] = st["messages"][-120:]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            log.exception("会话对话历史回写失败（会话 %s）", self.session["id"])

    def _chat_interruptible(self, messages: list[dict[str, Any]],
                            *, system: str | list[dict[str, Any]],
                            tools: list[dict[str, Any]],
                            stream_text: bool = False, step: int | None = None):
        """■ 即点即停（2026-09-19）：chat 在旁路线程跑，主线程每 0.3s 查 abort——
        置位即刻抛 _StepInterrupted 放弃等待（旁路线程 daemon 随 HTTP 超时自行
        结束，结果丢弃）；正常完成原样返回响应，LLM 自身异常原样抛出。
        思考流式（2026-09-19）：llm 声明 stream_capable 时带 on_thinking/should_cancel
        ——旁路线程只攒 delta 缓冲，主线程轮询间隙节流落 llm.thinking.delta 事件
        （累计全文+seq，前端按 stream_id 组装成一行滚动思考）；should_cancel 接
        _abort_req，中断时流式连接即刻掐断。返回的响应带 _stream_id 供终稿落库
        关联（llm.thinking payload.stream_id → 清剪 delta 行）。
        回复流式（2026-09-20 对话化）：stream_text=True 时同机制带 on_text，
        节流落 agent.chat.delta（累计全文+seq，节流阈值 ≥80 新字符或 1.0s）——
        任务轮叙述不流式（爆炸半径控制），只有对话轮开。终稿 agent.chat 带
        stream_id，落库后清剪该流 delta 行。
        回复缓冲恒攒（2026-09-21 中断落盘）：on_text 在流式分支内恒传（任务轮也
        攒，只是 _flush_text 被 stream_text 守卫不落 delta 事件）——abort 时
        _flush_interrupted_reply 把半截回复落 agent.chat 终稿（interrupted=true；
        对话轮带 stream_id 清剪 delta，任务轮带 step 截 2000）。
        截断整轮重试（2026-09-20 事故修复）：LLMError.truncated（工具参数流截断）
        时重建消息整轮重发 ≤_CHAT_TRUNC_RETRIES 次——传输层流不可重放，只能在
        轮级重试；重试轮 thinking/text 缓冲清零重流（stream_id/seq 延续，终稿落库
        时统一清剪 delta，审计不留中间轮）。最后一轮重试降级**非流式**（不带
        on_thinking/on_text，走 L 层内建非流式路径）：网关 SSE 稳定性差导致连续
        截断时整包响应不受流断影响——流式轮耗尽才降级，思考行照常由终稿落库。"""
        box: dict[str, Any] = {}
        stream_id = uuid.uuid4().hex[:12]
        acc: list[str] = []  # thinking delta 缓冲（daemon 写 / 主线程读，GIL 下 append 原子）
        pub = {"chars": 0, "seq": 0, "at": 0.0}  # 上次发布状态（节流）
        text_acc: list[str] = []  # 回复 delta 缓冲（对话轮 stream_text=True 才有写入方）
        pub_text = {"chars": 0, "seq": 0, "at": 0.0}

        def _worker() -> None:
            try:
                kwargs: dict[str, Any] = {"system": system, "tools": tools}
                if stream_rounds_left > 0 and getattr(self.llm, "stream_capable", False):
                    kwargs["on_thinking"] = acc.append
                    kwargs["should_cancel"] = self._abort_req.is_set
                    # on_text 恒传（2026-09-21 中断落盘）：任务轮只攒不发 delta，
                    # abort 时半截叙述有货可捞；对话轮由 _flush_text 节流发布。
                    kwargs["on_text"] = text_acc.append
                box["resp"] = self.llm.chat(messages, **kwargs)
            except BaseException as e:  # noqa: BLE001 —— 原样转抛给等待方
                box["err"] = e

        def _flush_thinking() -> None:
            """节流发布增量：≥120 新字符，或文本有变且距上次 ≥1.0s（0.3s 轮询天然限频）。"""
            text = "".join(acc)
            now = time.monotonic()
            grown = len(text) - pub["chars"]
            if not text or (grown < 120 and (now - pub["at"] < 1.0 or grown <= 0)):
                return
            pub["chars"] = len(text)
            pub["seq"] += 1
            pub["at"] = now
            self.bb.append_event(
                self.project_id, "llm.thinking.delta",
                {"stream_id": stream_id, "thinking": text, "seq": pub["seq"]},
                session_id=self.session["id"], author=self.session["id"])

        def _flush_text() -> None:
            """回复增量节流发布（2026-09-20 对话化）：≥80 新字符，或有变化且距上次
            ≥1.0s——比 thinking 阈值低（回复短、期望近打字机观感）。任务轮
            （stream_text=False）只攒不发（2026-09-21 中断落盘：缓冲供 abort 时
            _flush_interrupted_reply 捞半截叙述）。"""
            if not stream_text:
                return
            text = "".join(text_acc)
            now = time.monotonic()
            grown = len(text) - pub_text["chars"]
            if not text or (grown < 80 and (now - pub_text["at"] < 1.0 or grown <= 0)):
                return
            pub_text["chars"] = len(text)
            pub_text["seq"] += 1
            pub_text["at"] = now
            self.bb.append_event(
                self.project_id, "agent.chat.delta",
                {"stream_id": stream_id, "text": text, "seq": pub_text["seq"]},
                session_id=self.session["id"], author=self.session["id"])

        resp = None
        stream_rounds_left = _CHAT_TRUNC_RETRIES  # 最后一轮降级非流式（整包响应无 SSE 断流）
        for attempt in range(_CHAT_TRUNC_RETRIES + 1):
            box.clear()
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            while t.is_alive():
                if self._abort_req.is_set():
                    self._flush_interrupted_reply(stream_id, text_acc, step)
                    raise _StepInterrupted()
                t.join(0.3)
                _flush_thinking()
                _flush_text()
            _flush_thinking()  # 收尾最后一拍（不足节流阈值的尾差也发出去，终稿随后到达）
            _flush_text()
            if "err" not in box:
                resp = box["resp"]
                break
            err = box["err"]
            if self._abort_req.is_set():
                # 流被 should_cancel 掐断（LLMError「已中断」等）：先捞半截回复再抛
                self._flush_interrupted_reply(stream_id, text_acc, step)
                raise err
            if (not (isinstance(err, LLMError) and getattr(err, "truncated", False))
                    or attempt >= _CHAT_TRUNC_RETRIES):
                raise err
            log.warning("LLM 工具参数流截断，整轮重试 %d/%d（stream_id=%s）: %s",
                        attempt + 1, _CHAT_TRUNC_RETRIES, stream_id, err)
            acc.clear()  # 重试轮思考重新累计（前端按最新 seq 的累计全文覆盖显示）
            pub["chars"] = 0
            text_acc.clear()
            pub_text["chars"] = 0
            if stream_rounds_left > 0:
                stream_rounds_left -= 1
                if stream_rounds_left == 0:
                    log.warning("截断重试耗尽流式轮，最后一轮降级非流式（stream_id=%s）", stream_id)
        resp.raw["_stream_id"] = stream_id  # 终稿落库关联 + 清剪 delta 用
        return resp

    def _flush_interrupted_reply(self, stream_id: str, text_acc: list[str],
                                 step: int | None) -> None:
        """■ 中断落盘（2026-09-21）：abort 时半截回复不再丢弃——text_acc 有存货就
        落 agent.chat 终稿（interrupted=true 标记，前端可辨识）。对话轮（step=None）
        带 stream_id 并清剪该流 delta——turn 分组有收口回复、审计只留一条；任务轮
        带 step、文本截 2000（同任务叙述终稿口径）。thinking 残留 delta 维持现状
        （被中断思考的现场审计，不落终稿，2026-09-19 定稿）。"""
        text = "".join(text_acc).strip()
        if not text:
            return
        payload: dict[str, Any] = {"session_id": self.session["id"],
                                   "interrupted": True}
        if step is not None:
            payload["step"] = step
            payload["text"] = text[:2000]
        else:
            payload["text"] = text
            payload["stream_id"] = stream_id
        self.bb.append_event(
            self.project_id, "agent.chat", payload,
            session_id=self.session["id"], author=self.session["id"])
        if step is None:
            try:
                self.bb.prune_chat_deltas(self.project_id, stream_id)
            except Exception:  # noqa: BLE001 —— 清剪失败只多留过渡行
                log.exception("agent.chat.delta 清剪失败 stream_id=%s", stream_id)

    # ---------- 内部 ----------

    def _emit_final_thinking(self, resp, step: int, duration_s: float | None) -> None:
        """终稿 llm.thinking（带 stream_id）+ 清剪该流的 llm.thinking.delta 过渡行
        ——审计仍只留终稿一条，事件表与流式化之前一样干净。中断/LLM 异常路径不
        走到这里（残留 delta = 被中断思考的现场审计，前端照常组装显示）。"""
        if not (resp.thinking and resp.thinking.strip()):
            return
        sid = resp.raw.get("_stream_id") if isinstance(resp.raw, dict) else None
        self.bb.append_event(
            self.project_id, "llm.thinking",
            {"thinking": resp.thinking, "step": step, "duration_s": duration_s,
             **({"stream_id": sid} if sid else {})},
            session_id=self.session["id"], author=self.session["id"])
        if sid:
            try:
                self.bb.prune_thinking_deltas(self.project_id, sid)
            except Exception:  # noqa: BLE001 —— 清剪失败只多留过渡行，不影响主流程
                log.exception("llm.thinking.delta 清剪失败 stream_id=%s", sid)

    def _dead_end_notice(self, task: dict) -> str | None:
        """C3 跨轨路标三级注入（§5.2 定稿）：已排除方向/死路（status=false-positive
        findings）注入认领会话——① scope/资产精确匹配全文（cap 5）② 同 host 聚合
        一行动态摘要（跨端口可见、未覆盖目标显形）③ 项目级计数 + 查询纪律。
        渗透=已排除攻击路径、CTF=死路线索、逆向=已排除假设。"""
        fps = [f for f in self.bb.list_findings(self.project_id)
               if f["status"] == "false-positive"]
        if not fps:
            return None
        assets = {a["id"]: a for a in self.bb.list_assets(self.project_id)}

        def host_val(aid: str | None) -> str:
            a = assets.get(aid)
            while a:
                if a["type"] == "host":
                    return a["value"]
                a = assets.get(a.get("parent_id") or "")
            return ""

        scope_text = ((task.get("scope") or "") + " " +
                      (task.get("objective") or "")).lower()
        exact: list[str] = []
        by_host: dict[str, list[str]] = {}
        for f in fps:
            av = (assets.get(f.get("target_asset_id")) or {}).get("value", "").lower()
            hv = host_val(f.get("target_asset_id"))
            blob = (f["title"] + " " + json.dumps(f.get("evidence", {}),
                                                  ensure_ascii=False)[:200]).lower()
            line = f"⛔ {f['title']}"
            if av and (av in scope_text or (hv and hv in scope_text)):
                exact.append(line)
            elif hv:
                by_host.setdefault(hv, []).append(f["title"][:40])
            else:
                by_host.setdefault("(其他)", []).append(f["title"][:40])
        lines: list[str] = ["🚫 路标——以下方向已被排除（勿重走，动手前先 bb_query 查死路）："]
        for f in exact[:5]:
            lines.append(f"  {f}")
        for hv, titles in list(by_host.items())[:10]:
            lines.append(f"  ⛔ {hv}：已排除 {len(titles)} 条（{'、'.join(titles[:2])}…）"
                         if len(titles) > 2 else
                         f"  ⛔ {hv}：已排除 {len(titles)} 条（{'、'.join(titles)}）")
        if len(exact) > 5:
            lines.append(f"  （另有 {len(exact) - 5} 条精确匹配路标未展开）")
        return "\n".join(lines)

    def _old_plan_notice(self, task: dict) -> str | None:
        """C1：重新认领曾执行过的任务时，注入旧计划与依据发现摘要——
        done 步骤带真实性注记（未经本会话验证，不视为已验证），防新会话误读。"""
        old_plan = task.get("plan") or []
        if not old_plan:
            return None
        lines = ["♻ 该任务曾被执行过并中途停止（现重新认领）。以下旧计划仅供参考——"
                 "done 步骤未经你验证，重做前先核实，不要盲目沿用："]
        for s in old_plan:
            line = f"- {s.get('id')} [{s.get('status')}] {s.get('title')}"
            if s.get("note"):
                line += f"（{s['note']}）"
            lines.append(line)
        fsums: list[str] = []
        for fid in (task.get("context_refs") or [])[:5]:
            try:
                f = self.bb.get_finding(self.project_id, fid)
            except Exception:  # noqa: BLE001
                f = None
            if f:
                fsums.append(f"- {fid} [{f['status']}/{f['severity']}] {f['title']}")
        if fsums:
            lines.append("该任务依据的发现摘要：")
            lines += fsums
        return "\n".join(lines)

    def _handover_notice(self, task: dict, has_transcript: bool) -> str | None:
        """C10 跨会话接手提示：有现场或履历才注入。对上面的对话现场做元说明
        （可续做、已有结论不必重查；历史工具结果属于当时现场不可重放），
        并注入历次尝试履历（outcome/result_note/blocked_reason，黑板侧落库）。"""
        ctx = task.get("context") or {}
        attempts = ctx.get("attempts") or []
        if not has_transcript and not attempts:
            return None
        n = len(attempts) + 1
        lines: list[str] = []
        if has_transcript:
            lines.append(
                f"🔁 第 {n} 次尝试接手：以上对话现场来自此前执行（可直接续做，"
                "已有结论不必重查）；注意历史里的工具结果属于当时现场，"
                "不可重放，需要时重新取证。")
        if attempts:
            lines.append(f"本任务共 {n - 1} 次历史尝试：")
            from core.blackboard.tasks import render_attempts_lines
            lines += render_attempts_lines(attempts)
        return "\n".join(lines) if lines else None

    def _handover_blackboard_view(self, task: dict) -> str:
        """阶段四 M3 接手黑板视角（2026-09-28）：接手窗无本会话记忆，把黑板知识
        侧（跨窗已验证 finding + 已排除死路）注入——接手即知哪些结论已坐实、哪些
        方向勿重走。与阶段一 system 物化（按 objective 通用检索）区分：此处聚焦
        本任务相关的跨窗已验证沉淀，cap 紧凑防膨胀。无沉淀返回空串。"""
        q = str((task.get("objective") or "") + " " + str(task.get("scope") or "")).strip()
        if not q:
            return ""
        ql = q.lower()
        try:
            findings = self.bb.list_findings(self.project_id)
            assets = {a["id"]: a for a in self.bb.list_assets(self.project_id)}
        except Exception:  # noqa: BLE001
            return ""
        verified: list[str] = []
        dead: list[str] = []
        for f in findings:
            title = str(f.get("title") or "")
            av = (assets.get(f.get("target_asset_id")) or {}).get("value", "")
            blob = (title + " " + av).lower()
            if not blob or (ql not in blob and av.lower() not in ql):
                continue
            if f.get("status") == "false-positive":
                dead.append(f"⛔ {title[:70]}")
            elif f.get("status") == "verified":
                verified.append(
                    f"- [{f.get('severity')}] {title[:70]}" +
                    (f"｜{str(f.get('impact') or '')[:50]}" if f.get("impact") else ""))
        lines: list[str] = []
        if verified:
            lines.append("📌 黑板已验证结论（接手可采信，勿重复验证）：")
            lines += verified[:8]
        if dead:
            lines.append("🚫 黑板已排除方向（接手勿重走）：")
            lines += dead[:5]
        if not lines:
            return ""
        return "\n".join(lines)[:1400]

    def _loop(self, system: str, messages: list[dict[str, Any]], objective: str,
              start_step: int = 1) -> str | None:
        """薄包装：在册/注销 `_live_state`（v0.64 暂停请求即时落盘的现场源），
        主体见 `_loop_body`。退出（含暂停/中断/异常）必清 None，防陈旧现场被快照。
        另在册 `_salvage_ctx`（fail 抢救收尾用）：messages 与 _live_state 同为就地
        变异的同一列表，异常穿出后引用仍有效——正常退出（含暂停/即点即停）立即
        清除，只有异常路径保留给 `_fail_task_on_error` 消费。"""
        self._live_state = {"system": system, "messages": messages,
                            "objective": objective}
        # system 字段供 429 挂起快照（_fail_task_on_error LLMError 分支，
        # 2026-09-22）——_salvage_attempt 不读它，多带字段无害
        self._salvage_ctx = {"objective": objective, "messages": messages,
                             "system": system}
        try:
            result = self._loop_body(system, messages, objective, start_step)
        finally:
            self._live_state = None
        self._salvage_ctx = None
        return result

    def _loop_body(self, system: str, messages: list[dict[str, Any]], objective: str,
                   start_step: int = 1) -> str | None:
        """返回 None = 暂停/中断退出（调用方不得 finalize）；其余返回任务总结。

        步数上界读 dispatcher.max_steps（E8）：request_steps 增补写在那里，
        本会话内跨任务生效。"""
        max_steps = self.dispatcher.max_steps
        step = start_step
        # 每任务复位（session-state 收敛，2026-10-03）：原先散在这里的十余行逐字段
        # 赋值 + reset_closing() 收敛为单入口 SessionState.reset_for_task（字段与
        # 复位值逐字段等价）。覆盖：收尾标志（潜伏 bug 修复：finish 后同会话再认领
        # 的任务会在首步命中 stale finished → 返回旧总结并被 _finalize 误标失败）、
        # 意图先行闸放行标志（口径 Y，2026-10-01：每任务都要「先立意再动手」；
        # chat 链无任务，跨对话轮保持一次性）、E2/D1/D9 计数、阶段三规划节拍状态
        # （2026-09-28）、D6 收尾确认轮状态（上一任务残留的确认态不得带进新任务）。
        self.state.reset_for_task()
        # while 而非 range（E8）：request_steps 在步内增补预算后，循环上界随之
        # 前移——耗尽轮当场自救（步号不增）也成立，不会因 range 预计算被截断。
        while step <= max_steps:
            if self._persona_dirty:
                # v0.71 中途改角色热换装：下个步进前重建 system prompt
                #（config/dispatcher 边界已在 apply_role_change 即时生效）
                system = self.build_system_blocks(objective, self._last_skill_context)
                self._persona_dirty = False
            self.dispatcher.set_step(step)
            # 步数感知（E8）：剩余 <20 步起每步边界注入提醒——模型对预算无感是
            # 步数耗尽事故的第一根因；预算吃紧时由模型自行 request_steps 或收尾。
            remaining = max_steps - step
            if remaining < 20:
                messages.append({"role": "user", "content":
                    f"⏳ 预算剩余 {remaining} 步，请规划收尾；如确需更多步数，"
                    "调 request_steps 申请增补（一次 +200，剩余 ≤20 步才放行）。"})
            # 阶段三规划节拍（2026-09-28）：双源机械触发计划修订提示——新发现
            # 未入计划 / 计划长期未修订。只提示不自动改（计划语义仍由模型决定，
            # 与计划闸 A2 不冲突）；修订本身走 task_plan（rev_reason 落因）。
            self._planning_cadence(step, messages)
            if self._stuck(step):
                if self._stuck_waves >= 2:
                    # D7 硬闸：顾问裁决「继续」后又干满一个观察窗仍无进展——
                    # 机械终止兜底（断路器不拆），awaiting_human 由下方 C1 分支落快照
                    self._stuck_escalate(step)
                elif self._stuck_waves == 1:
                    # D7：同任务第 2 轮卡死交顾问裁决（终止/继续/请求人工）
                    decision = self._advisor_verdict(messages, objective, step)
                    if decision == "continue":
                        self.dispatcher.last_progress_step = step  # 裁决继续：重开观察窗
                        self._stuck_waves = 2
                else:
                    # waves=0：D9 先做零成本机械预检——命令/读文件在演进=活跃探索
                    # 且延长次数未满 → 静默延长；否则召唤顾问建议（现有路径）
                    signals = (self._active_exploration(
                                   self.dispatcher.last_progress_step, step)
                               if self._stuck_extensions < self.config.stuck_max_extensions
                               else None)
                    if signals is not None:
                        self._extend_stuck_window(step, signals)
                    else:
                        messages.append({"role": "user", "content": self._advisor_prompt(messages, objective)})
                        self.dispatcher.last_progress_step = step  # 顾问干预后重置观察窗
                        self._stuck_waves += 1
            _chat_t0 = time.monotonic()
            try:
                resp = self._chat_interruptible(messages, system=system,
                                                tools=self._task_tool_schemas(), step=step)
            except _StepInterrupted:
                # ■ 即点即停（2026-09-19）：LLM 调用等待中被人手中断——不等本步
                # 完成，立刻按硬中断收尾（任务 fail「人工中断」，快照保留可续跑）。
                # usage/thinking 不落（响应已丢弃）；半截叙述已由 _flush_interrupted_reply
                # 在抛出前落盘（2026-09-21 中断落盘）。
                self._abort_current_task()
                return None
            chat_s = round(time.monotonic() - _chat_t0, 3)
            self._record_usage(resp, source="agent", llm_obj=self.llm)
            # 思考终稿落事件流（DESIGN.md §12）：折叠一行摘要、展开看全文；
            # duration_s = 整次 chat 墙钟（含网络+生成），前端显示「思考 Ns」；
            # 流式增量行（llm.thinking.delta）由 _emit_final_thinking 一并清剪
            self._emit_final_thinking(resp, step, chat_s)
            messages.append(assistant_message(resp))
            # 任务叙述落事件流（2026-09-19 直播间终端化，与 run_chat 的 agent.chat 对称）：
            # assistant 在工具调用之间说的话=「做了什么/进度」叙述行；有 tool_calls 的步也落
            # （叙述常出现在调用前）。截 2000 字符防事件表膨胀；键用 step 不用 step_id
            # （避免撞 A2 计划事件的前端摘要分支）。
            _narr = resp.text
            if _narr.strip():
                self.bb.append_event(
                    self.project_id, "agent.chat",
                    {"session_id": self.session["id"], "text": _narr[:2000], "step": step},
                    session_id=self.session["id"], author=self.session["id"])
            if not resp.tool_calls:
                # 纯文本步也过拒绝分类：硬拒绝计数清零；plan-only 模式下纯文本
                # 而计划仍空 → 挂人（2026-09-24）
                self._classify_step_rejections([], messages)
                if not self.dispatcher.awaiting_human:
                    # 纯文本回复：视为停等，提示其用 finish 或继续干活
                    messages.append({"role": "user",
                                     "content": "（请继续执行：调用工具干活，或调用 finish 结束并总结）"})
            else:
                _progress_before = self.dispatcher.last_progress_step
                step_results: list[str] = []
                tool_results = []
                for tc in resp.tool_calls:
                    result_text = self.dispatcher.dispatch(tc.name, tc.arguments)
                    step_results.append(result_text)
                    tool_results.append(self.llm.tool_result_message(tc, result_text)["content"][0])
                messages.append(tool_results_message(tool_results))
                # 拒绝按「模型步」分类处理（2026-09-24 口径重构，
                # plan-gate-breaker-refine；原逻辑在工具内联处按回执张数计，
                # 并行批可当场熔断，模型拿不到改道机会）
                self._classify_step_rejections(step_results, messages)
                # D1：夹一次实质进展，卡死波次清零（镜像 E2 夹非拒绝即清零）
                if self.dispatcher.last_progress_step > _progress_before:
                    self._stuck_waves = 0
                    self._stuck_extensions = 0  # D9：延长计数随真进展一并清零
                self._trim(messages)
                # G3：过阈值先 LLM 摘要压缩，_trim 硬上限兜底；任务已收尾不再压缩
                if not self.dispatcher.finished:
                    self._maybe_summarize(messages)
            self._checkpoint_task_transcript(messages, objective)  # C10 每步落盘任务现场
            if getattr(self.dispatcher, "awaiting_human", False):
                # C1：Agent 自主挂起（awaiting_human）——快照落盘 + 任务 fail
                # （blocked_reason=awaiting_human，resumable=True），现场保留，
                # 人类可经看板「▶ 续跑」（E12 revive）或「✅ 已解决，放回继续」承接。
                # 会话不结束（不走 finished/_finalize），worker 继续认领下一个任务。
                self.dispatcher.awaiting_human = False
                self._resume_state = {
                    "system": system, "messages": messages, "objective": objective,
                    "task_id": self.dispatcher.current_task_id,
                    "next_step": step + 1, "max_steps": self.dispatcher.max_steps,
                    "reason": "awaiting",
                    "open_intents": self._open_intent_snapshot(),
                }
                self._persist_snapshot()
                try:
                    self.tq.fail(
                        self.dispatcher.current_task_id, self.session["id"],
                        self.dispatcher.summary or "等待人工输入",
                        resumable=True, blocked_reason="awaiting_human",
                        persona_role=self.dispatcher.current_persona_role)
                except Exception:  # noqa: BLE001
                    log.exception("awaiting_human fail 失败")
                self.dispatcher.current_task_id = None
                self._resume_state = None  # 快照只留在磁盘（内存态泄漏会被下轮 run_next_task 误消费清盘）
                self._stop_heartbeat()
                return None  # 任务收尾已处理（同暂停/中断语义，不走 _finalize）
            if self.dispatcher.delegation_just_finished:
                # 委托真收尾（done/failed/删除）：本轮结束、会话保留待命——run_task
                # 不走 _finalize；worker while 再入会话轮接窗内队列下一件。
                self.dispatcher.delegation_just_finished = False
                self._stop_heartbeat()
                return self.dispatcher.last_delegation_note or "委托已收尾"
            if self.dispatcher.finished:
                return self.dispatcher.summary
            ctrl = self._control_point(system, messages, objective, step)
            if ctrl is not None:
                return None  # 检查点消费了暂停/中断（§3 会话控制）
            step += 1
            max_steps = self.dispatcher.max_steps  # 步内 request_steps 增补 → 上界前移
        # 步数耗尽（E8）：不再自动 fail——快照 + 自动步数暂停，任务保持 claimed、
        # 心跳继续，等人类在直播间「继续」（恢复时可附引导语/追加预算）。
        self._budget_pause(system, messages, objective, self.dispatcher.max_steps)
        return None

    # ---------- 阶段三规划节拍（2026-09-28，planner-cadence） ----------

    _CADENCE_K = 8  # 周期校验触发步距：每 K 步且近 K 步未修订过 → 提示一次

    def _planning_cadence(self, step: int, messages: list[dict[str, Any]]) -> None:
        """双源机械触发计划修订提示（阶段三件 1）：

        源 A 新发现触发：本任务区间新登记 finding 且当前 plan 无任何步 refs 引用
        （refs 形如 finding:<id>）→ 注入提示（同一 finding 只提示一次）。
        源 B 周期校验触发：每 K 步且自上次 task.plan_revised 后已推进 ≥K 步 →
        注入轻量校验提示（提醒模型对照 objective 检查 plan 是否仍有效）。

        只提示不自动改——计划语义仍由模型决定（与计划闸 A2 不冲突）；修订走
        task_plan（rev_reason 落因，task.plan_revised 事件为去重游标）。
        任一查询异常静默跳过（规划节拍是增强，不阻断主循环）。"""
        if not self.dispatcher.current_task_id:
            return
        cur = self.dispatcher.current_task_id
        task = self.tq.get_task(cur) if cur else None
        if task is None or task["status"] != "claimed":
            return
        try:
            # ---- 源 A：新发现未入计划 ----
            self._cadence_finding_hint(task, messages)
            # ---- 源 B：计划长期未修订（周期校验） ----
            self._cadence_periodic_hint(step, messages)
        except Exception:  # noqa: BLE001 —— 规划节拍失败不阻断主循环
            log.exception("规划节拍异常（忽略）")

    def _cadence_finding_hint(self, task: dict,
                              messages: list[dict[str, Any]]) -> None:
        """源 A：任务区间新 finding 未入计划 → 提示。refs 引用集=全部计划步 refs。"""
        from core.blackboard.traces import _session_task_windows
        task_id = task["id"]
        wins = [w for w in _session_task_windows(
                    self.bb.conn, self.project_id, self.session["id"])
                if w["task_id"] == task_id and w.get("hi")]
        if not wins:
            return
        w = wins[-1]
        rows = self.bb.conn.execute(
            "SELECT DISTINCT json_extract(payload,'$.finding_id') AS fid,"
            " json_extract(payload,'$.title') AS title, id AS ev"
            " FROM events WHERE project_id=? AND session_id=? AND kind='finding.new'"
            " AND id>? AND id<=? ORDER BY id",
            (self.project_id, self.session["id"], w["lo"], w["hi"])).fetchall()
        if not rows:
            return
        # 计划步 refs 引用集（finding:<id>）
        plan_refs = set()
        for s in task.get("plan") or []:
            for ref in (s.get("refs") or []):
                if isinstance(ref, str) and ref.startswith("finding:"):
                    plan_refs.add(ref[len("finding:"):])
        new_hits: list[str] = []
        for r in rows:
            fid = r["fid"]
            if not fid or fid in plan_refs or fid in self._cadence_hinted_findings:
                continue
            self._cadence_hinted_findings.add(fid)
            title = str(r["title"] or fid)[:80]
            new_hits.append(f"- {title}（{fid}）")
        if not new_hits:
            return
        messages.append({"role": "user", "content":
            "📋 规划节拍：黑板新登记了本任务相关的发现，当前计划步未引用它们——"
            "若这些发现改变了打法，请调 task_plan 修订计划（保留进度的步带原 id，"
            "rev_reason 注明原因）；若不相关可忽略：\n" + "\n".join(new_hits[:5])})

    def _cadence_periodic_hint(self, step: int,
                               messages: list[dict[str, Any]]) -> None:
        """源 B：每 K 步且自上次 plan_revised 后推进 ≥K 步 → 轻量校验提示。"""
        if step - self._cadence_last_hint_step < self._CADENCE_K:
            return  # 距上次周期提示不足 K 步
        # 上次 task.plan_revised 事件 id（跨任务统一按 session 查，取全局最大）
        row = self.bb.conn.execute(
            "SELECT COALESCE(MAX(id),0) AS maxid FROM events"
            " WHERE project_id=? AND session_id=? AND kind='task.plan_revised'",
            (self.project_id, self.session["id"])).fetchone()
        last_rev = int(row["maxid"] if row else 0)
        if last_rev > self._cadence_last_rev:
            # 本任务以来有过修订 → 重置游标与步距，本次不提示
            self._cadence_last_rev = last_rev
            self._cadence_last_hint_step = step
            return
        self._cadence_last_hint_step = step
        messages.append({"role": "user", "content":
            "📋 规划节拍：本任务已推进多步且计划未再修订——请对照目标快速检查："
            "计划步是否仍与当前进展一致？需修订则调 task_plan（保留进度步带原 id，"
            "rev_reason 注明原因）；计划仍有效则继续执行（忽略本条）。"})

    # ---------- 拒绝分类（2026-09-24，plan-gate-breaker-refine） ----------

    def _task_tool_schemas(self) -> list[dict[str, Any]]:
        """plan-only 教练模式：发给 LLM 的工具面收缩到计划/控制原语；其余全量。"""
        if not self.dispatcher.plan_only_mode:
            return AGENT_TOOLS
        return [t for t in AGENT_TOOLS if t.get("name") in _PLAN_ONLY_TOOL_NAMES]

    def _classify_step_rejections(self, results: list[str],
                                  messages: list[dict[str, Any]]) -> None:
        """按**模型步**（非回执张数）分类处理本步拒绝：

        - 计划本步已落黑板（并行批里 task_plan 与被闸调用并存）→ 教练态全清；
        - 硬拒绝（越界/网关/拒收）→ 硬熔断计数，连续 ≥3 步 → awaiting_human；
          不含硬拒绝的步（含纯文本步）→ 计数清零；
        - 计划闸 → 教练计数，累计 2 步 → 进 plan-only（强提示注入 + 工具面收缩）；
          plan-only 之后下一模型步末计划仍空 → awaiting_human（进入模式的当步不挂）。
        """
        cur = self.dispatcher.current_task_id
        task = self.tq.get_task(cur) if cur else None
        plan_written = task is not None and bool(task.get("plan"))
        if plan_written:
            self._plan_gate_count = 0
            self.dispatcher.plan_only_mode = False

        hard_hit = any(r.startswith(HARD_REJECT_PREFIXES) for r in results)
        gate_hit = any(r.startswith(PLAN_GATE_PREFIX) for r in results)

        if hard_hit:
            self._reject_streak += 1
            if self._reject_streak >= _REJECT_BREAK_LIMIT \
                    and not self.dispatcher.awaiting_human:
                last = next(r for r in results
                            if r.startswith(HARD_REJECT_PREFIXES))
                self.dispatcher.summary = (
                    f"拒绝熔断挂起：连续 {self._reject_streak} 个模型步出现硬拒绝，"
                    f"最后回执：{last[:200]}")
                self.dispatcher.awaiting_human = True
                self.bb.append_event(
                    self.project_id, "agent.reject_breaker",
                    {"session_id": self.session["id"], "task_id": cur,
                     "streak": self._reject_streak, "last_result": last[:300]},
                    session_id=self.session["id"], author=self.session["id"])
        else:
            self._reject_streak = 0

        entered_mode = False
        if gate_hit and not plan_written \
                and not self.dispatcher.plan_only_mode:
            self._plan_gate_count += 1
            if self._plan_gate_count >= _PLAN_GATE_STEPS:
                self.dispatcher.plan_only_mode = True
                entered_mode = True
                messages.append({"role": "user", "content": _PLAN_ONLY_NUDGE})
                self.bb.append_event(
                    self.project_id, "agent.plan_nudge",
                    {"session_id": self.session["id"], "task_id": cur,
                     "count": self._plan_gate_count},
                    session_id=self.session["id"], author=self.session["id"])

        # plan-only 强提示后的模型步结束、计划仍空 → 挂人（进入模式的当步除外）
        if self.dispatcher.plan_only_mode and not entered_mode \
                and not plan_written and not self.dispatcher.awaiting_human:
            self.dispatcher.summary = (
                "计划闸强提示后仍未写出 task_plan，挂起待人工承接")
            self.dispatcher.awaiting_human = True
            self.bb.append_event(
                self.project_id, "agent.plan_gate_block",
                {"session_id": self.session["id"], "task_id": cur,
                 "count": self._plan_gate_count},
                session_id=self.session["id"], author=self.session["id"])

    # ---------- 会话控制（DESIGN.md §3：暂停/恢复/中断） ----------

    def _control_point(self, system: str, messages: list[dict[str, Any]],
                       objective: str, step: int) -> str | None:
        """LLM 步边界检查点。返回 None=继续；否则消费请求并落状态，_loop 以 None 退出。"""
        if self._abort_req.is_set():
            self._abort_current_task()
            return "aborted"
        if self._pause_req.is_set():
            self._pause_req.clear()
            self._resume_state = {
                "system": system, "messages": messages, "objective": objective,
                "task_id": self.dispatcher.current_task_id, "next_step": step + 1,
                "max_steps": self.dispatcher.max_steps,  # E8：暂停时预算随快照走
                "reason": "pause",
                "open_intents": self._open_intent_snapshot(),
            }
            self._enter_paused()
            return "paused"
        if self._task_gone():
            # A1：任务在看板被删除（claimed 步边界取消）——当前步已做完，按中断收尾，
            # 不再调 fail（行已不存在，task.deleted 即审计）。
            self._abort_current_task()
            return "aborted"
        # 系统私信（不打断当前工具调用；drain 已原子标记已读）：下一个步边界注入。
        # 口径统一走订阅声明表 _INBOX_SUBSCRIPTIONS（2026-09-21）：human_note 轮末
        # 语义（不在任务中途打断思路，留收件箱等认领期/恢复期注入）由订阅表表达；
        # 未登记 kind 滞留不吞。
        notices, _dr = self._drain_inbox("step")
        for _kind, notice in notices:
            messages.append({"role": "user", "content": notice})
        self._checkpoint_task_transcript(messages, objective)  # C10：注入的私信/引导也进现场
        return None

    def _drain_inbox(self, scenario: str, *, stale_refs: list[str] | None = None
                     ) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
        """■ 收件箱订阅声明化（2026-09-21）：按场景消费收件箱的唯一入口。

        消费口径只认模块级 _INBOX_SUBSCRIPTIONS 声明表：scenario ∈ {claim, resume,
        step, chat} 决定取哪些 kind（only_kinds 从表构造——未登记 kind 滞留收件箱
        红点可见，绝不被静默吞）；渲染顺序=表内声明序；除 basis_stale（签名不同，
        需任务挂标 stale_refs 现场补水）外，渲染器恰为 _<kind>_notice 命名约定。

        返回 ([(kind, notice 文本), …], 原始 drained 行)——后者供调用方提取
        human_note 正文（run_chat 的 note_text）等。"""
        kinds = tuple(k for k, scs in _INBOX_SUBSCRIPTIONS.items() if scenario in scs)
        drained: list[dict[str, Any]] = \
            self.bb.inbox_drain(self.project_id, self.session["id"], only_kinds=kinds) \
            if kinds else []
        out: list[tuple[str, str]] = []
        for kind in _INBOX_SUBSCRIPTIONS:  # 固定声明序
            if kind not in kinds:
                continue
            if kind == "basis_stale":
                notice = self._basis_stale_notice(stale_refs or [], drained)
            else:
                notice = getattr(self, f"_{kind}_notice")(drained)
            if notice:
                out.append((kind, notice))
        return out, drained

    def _human_note_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「人类引导」消息（E8，kind='human_note'）：信息式注入；多条按序
        各占一行。payload.attachments（2026-09-19 附件随发）渲染成 📎 行——
        纯附件无文字的引导同样成立。注入点=认领期/恢复期（轮末语义，
        2026-09-19 起）：不在任务中途打断思路。"""
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "human_note":
                continue
            payload = r.get("payload") or {}
            text = str(payload.get("text", "")).strip()
            if text:
                lines.append(f"- {text}")
            lines.extend(_attachment_lines(payload.get("attachments")))
        if not lines:
            return None
        return "\n".join(["💬 人类引导："] + lines)

    def _finding_update_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「发现增补」信息式消息（A4，kind='finding_update'）：不强制任何动作。
        未读去重已保证同 ref 至多一条，这里仍按 ref 防御性聚合。"""
        items: dict[str, dict[str, Any]] = {}
        for row in inbox_rows:
            if row.get("kind") != "finding_update" or not row.get("ref_id"):
                continue
            items.setdefault(row["ref_id"], row.get("payload") or {})
        if not items:
            return None
        lines = [
            "ℹ 发现增补通知（信息式，无需中断当前工作；若增补内容影响你的路线可自行调整）：",
        ]
        for ref, p in items.items():
            head = f"- {ref}「{p.get('title', '')}」"
            changes = p.get("changes") or []
            if changes:
                head += f"：{ '、'.join(str(c) for c in changes) }"
            lines.append(head)
        return "\n".join(lines)

    def _authorization_result_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「授权申请已批准」消息（M5 D2，kind='authorization_result'）：
        request_authorization 经人类批准后回流——信息式注入，Agent 按批准内容
        继续作业。批准后无平台动作：scope_expand 自行 bb_add_asset 登记。"""
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "authorization_result":
                continue
            p = r.get("payload") or {}
            kind = str(p.get("kind", ""))
            req = str(p.get("scope_request", ""))[:300]
            lines.append(f"✅ 授权申请已批准（{kind}）：{req}\n"
                         "按批准内容继续作业（scope_expand 类请先 bb_add_asset 登记）。")
        if not lines:
            return None
        return "\n---\n".join(lines)

    def _approval_rejected_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「申请被拒绝」消息（M5 D2 搭车，kind='approval_rejected'）：
        authorization 被人类拒绝后回流（此前 rejected 无回流=Agent 空等）
        ——信息式注入，Agent 换路不要再等。"""
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "approval_rejected":
                continue
            p = r.get("payload") or {}
            op = str(p.get("op", ""))
            head = {"authorization": "授权申请"}.get(op, op)
            note = str(p.get("note", "")).strip()
            tail = f"：{note[:300]}" if note else "（未附理由）"
            lines.append(f"❌ {head}被人类拒绝{tail}——不要继续等待，按当前边界换路。")
        if not lines:
            return None
        return "\n---\n".join(lines)

    def _agent_message_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「Agent 私信」消息（2026-09-20 会话窗对话化，§17 B1，kind='agent_message'）：
        会话窗间知会（intel/handoff/assist 三分类，异步模型不做同步接力）——信息式
        注入，收件方按需回应不强制。payload.from 是发件窗会话 id，这里解析出
        会话显示名便于识别。"""
        labels = {"intel": "情报", "handoff": "移交", "assist": "协助请求"}
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "agent_message":
                continue
            p = r.get("payload") or {}
            sub = str(p.get("subkind") or "intel")
            text = str(p.get("text", "")).strip()
            head = f"- [{labels.get(sub, sub)}] 来自 {self._session_label(str(p.get('from', '')))}：{text}"
            refs = p.get("refs") or []
            if refs:
                head += f"（引用：{', '.join(str(x) for x in refs[:5])}）"
            lines.append(head)
        if not lines:
            return None
        return "\n".join(["📨 Agent 私信（信息式；涉你路线可自行调整，assist 类尽快回应）："] + lines)

    def _task_receipt_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「子任务回执」消息（2026-09-20 P3 多智能体协调，kind='task_receipt'）：
        派生任务收尾自动回执父窗——信息式注入，父 agent 拿到摘要继续原路线。"""
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "task_receipt":
                continue
            p = r.get("payload") or {}
            ok = p.get("status") == "done"
            head = (f"{'✅' if ok else '❌'} 子任务{'完成' if ok else '失败'}"
                    f" {p.get('task_id', '')}「{p.get('objective', '')}」")
            note = str(p.get("result_note", "")).strip()
            if note:
                head += f"：{note}"
            if not ok and p.get("blocked_reason"):
                head += f"（失败原因：{p['blocked_reason']}）"
            finds = p.get("findings") or []
            if finds:
                head += "\n  相关发现：" + "；".join(
                    f"{f.get('severity', '?')}「{f.get('title', '')}」"
                    for f in finds if isinstance(f, dict))
            lines.append(head)
        if not lines:
            return None
        return "\n---\n".join(lines)

    def _session_label(self, sid: str) -> str:
        """会话 id → 显示名（未知/异常回退原 id）。"""
        if not sid:
            return "?"
        try:
            row = self.bb.conn.execute(
                "SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
            if row and row["name"]:
                return f"{row['name']}({sid})"
        except Exception:  # noqa: BLE001 —— 显示名失败不致信令丢失
            pass
        return sid

    def _basis_stale_notice(
        self, task_refs: list[str], inbox_rows: list[dict[str, Any]],
    ) -> str | None:
        """拼「依据被推翻」强制自评消息。task_refs 从 finding 行现场补水
        （open 任务认领时无私信行）；inbox_rows 是撤回路由投递的私信（信息更全）。
        同一 ref 优先用私信 payload；返回 None=本会话没有待告内容。"""
        items: dict[str, dict[str, Any]] = {}
        for ref in task_refs:
            f = self.bb.get_finding(self.project_id, ref)
            if f is None or f.get("status") != "false-positive":
                continue
            ev = f.get("evidence") or {}
            items[ref] = {
                "finding_id": ref, "title": f.get("title", ""),
                "vuln_class": f.get("vuln_class", ""),
                "by": "人工/系统复核", "note": str(ev.get("note", "")),
            }
        for row in inbox_rows:
            if row.get("kind") != "basis_stale" or not row.get("ref_id"):
                continue
            ref = row["ref_id"]
            p = row.get("payload") or {}
            items.setdefault(ref, p)
            if p.get("by"):
                items[ref]["by"] = p["by"]
        if not items:
            return None
        lines = [
            "⚠ 依据撤回（系统强制自评，DESIGN §6.7 的 1.6）",
            "本任务引用的以下发现刚被标记为误报：",
        ]
        for it in items.values():
            head = f"- {it.get('finding_id', '')}「{it.get('title', '')}」"
            if it.get("vuln_class"):
                head += f"（{it['vuln_class']}）"
            lines.append(head)
            meta = []
            if it.get("by"):
                meta.append(f"推翻人：{it['by']}")
            note = str(it.get("note", "")).strip()
            if note:
                meta.append(f"误报理由：{note}")
            if meta:
                lines.append("  " + "；".join(meta))
        lines.append(
            "你必须立即自评并在下一步明确三选一：① 带理由继续（说明为何你的结论"
            "不依赖该依据仍成立）；② 调用 fail_task 终止本任务；③ 改道其他攻击面。")
        return "\n".join(lines)

    def _enter_paused(self) -> None:
        """落 paused 状态 + 审计事件。任务保持 claimed，恢复后从快照（如有）续跑。
        E8：有快照时同步落盘（workspace 文件 + sessions.meta 指针），重启可恢复。"""
        self.paused = True
        try:
            self.bb.set_session_status(self.session["id"], "paused")
        except Exception:  # noqa: BLE001
            log.exception("set_session_status(paused) 失败")
        self._persist_snapshot()
        self.bb.append_event(
            self.project_id, "session.paused",
            {"session_id": self.session["id"], "task_id": self.dispatcher.current_task_id},
            session_id=self.session["id"], author=self.session["id"])

    def _budget_pause(self, system: str, messages: list[dict[str, Any]],
                      objective: str, max_steps: int) -> None:
        """步数耗尽自动暂停（E8）：复用软暂停设施，快照标 reason=budget，
        next_step 停在耗尽步（恢复不增补预算时也至少还能走一轮对话，
        模型可当场 request_steps 自救）。任务保持 claimed、心跳继续。"""
        self._resume_state = {
            "system": system, "messages": messages, "objective": objective,
            "task_id": self.dispatcher.current_task_id,
            "next_step": self.dispatcher.step,  # 耗尽步号 = 恢复断点
            "max_steps": max_steps, "reason": "budget",
            "open_intents": self._open_intent_snapshot(),
        }
        self._enter_paused()
        self.bb.append_event(
            self.project_id, "session.budget_paused",
            {"session_id": self.session["id"], "task_id": self.dispatcher.current_task_id,
             "max_steps": max_steps},
            session_id=self.session["id"], author=self.session["id"])

    # ---------- 暂停快照落盘（E8：修纯内存不恢复缺口） ----------

    def _snapshot_path(self) -> Path | None:
        """快照文件路径：<workspace>/<pid>/snapshots/<sid>.json；无 artifacts_dir
        （部分测试/直跑）时返回 None = 保持纯内存。"""
        return persisted_snapshot_path(self.artifacts_dir, self.session["id"])

    def _persist_snapshot(self, st: dict[str, Any] | None = None) -> None:
        """暂停快照落盘：写 workspace 文件并在 sessions.meta 存指针。
        C6 双写：有任务时同步落任务键 `task-<tid>.resume.json`（**不含 system**，
        跨角色安全）——任何 failed 卡可跨会话/跨角色「⚡ 带现场续跑」；再次暂停
        覆盖前移断点。落盘失败只降级为纯内存快照（本进程内 resume 仍可用）。
        v0.64：st 缺省取 self._resume_state（步边界暂停路径）；显式传入供
        pause_snapshot_now 直接落盘（不碰 _resume_state，worker 生命周期不受扰）。"""
        if st is None:
            st = self._resume_state
        path = self._snapshot_path()
        if st is None or path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({**st, "session_id": self.session["id"]},
                           ensure_ascii=False),
                encoding="utf-8")
            self.bb.set_session_meta(self.session["id"],
                                     {"resume_snapshot": path.name})
        except Exception:  # noqa: BLE001
            log.exception("暂停快照落盘失败（会话 %s）", self.session["id"])
        # C6 任务键双写（覆盖前移断点）：快照不含 system——认领会话按角色重建
        task_id = st.get("task_id")
        if not task_id:
            return  # 纯 objective 直跑无任务，不落任务键
        task_path = task_resume_path(self.artifacts_dir, task_id)
        if task_path is None:
            return
        try:
            task_path.write_text(
                json.dumps({k: v for k, v in st.items() if k != "system"},
                           ensure_ascii=False),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.exception("任务键快照双写失败（任务 %s）", task_id)

    def _open_intent_snapshot(self) -> list[dict[str, str]]:
        """意图纪律①：快照带未收尾意图清单（停机/关窗后照单收尾，不留悬挂）。
        只摘 id/陈述/时间三字段；异常降级空清单不阻快照。"""
        try:
            return [{"id": it["id"], "statement": it["statement"],
                     "created_at": it.get("created_at") or ""}
                    for it in list_intents(self.bb, self.project_id, status="open")]
        except Exception:  # noqa: BLE001
            log.exception("未收尾意图清单构建失败（会话 %s）", self.session.get("id"))
            return []

    def pause_snapshot_now(self) -> bool:
        """v0.64 暂停请求即时落盘（关「暂停未到步边界就关后端」的窗口期）：
        从 _loop 在册的 `_live_state` 构建断点快照（尾部 sanitize 掉半步悬空的
        tool_use/tool_result），经 _persist_snapshot 双写会话键 + 任务键文件。
        **不动行状态、不置 paused、不碰 _resume_state**——worker 生命周期不受扰：
        后端活着，步边界 `_enter_paused` 会以干净现场覆盖；后端死了，这份快照
        兜底（重启清扫按 resume_snapshot 指针归位 paused + 豁免其 claimed 任务）。
        幂等可重复调用；无在跑现场（空闲/已到步边界暂停）返回 False 不动作。"""
        live = self._live_state
        if not live or self.dispatcher.current_task_id is None:
            return False
        st = {
            "system": live["system"],
            "messages": sanitize_snapshot_tail(live["messages"]),
            "objective": live["objective"],
            "task_id": self.dispatcher.current_task_id,
            "next_step": self.dispatcher.step + 1,
            "max_steps": self.dispatcher.max_steps,
            "reason": "pause",
            "open_intents": self._open_intent_snapshot(),
        }
        self._persist_snapshot(st)
        return True

    # ---------- 任务现场落盘（C10：上下文归任务所有，跨会话接手） ----------

    def _checkpoint_task_transcript(self, messages: list[dict[str, Any]],
                                    objective: str) -> None:
        """C10 每步任务现场落盘：写 <snapshots>/task-<tid>.json（temp+replace
        原子替换，防崩溃撕裂/并发读者读到半截）。只认 current_task_id——无任务
        （纯 objective 直跑）不落盘；失败降级不影响主循环。"""
        task_id = self.dispatcher.current_task_id
        path = task_transcript_path(self.artifacts_dir, task_id or "") if task_id else None
        if path is None:
            return  # 无任务（纯 objective 直跑）不落盘；防 task-.json 空名残file（走查发现）
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"task_id": task_id, "objective": objective,
                 "session_id": self.session["id"], "messages": messages,
                 "updated_at": datetime.now(timezone.utc).isoformat()},
                ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            log.exception("任务现场落盘失败（任务 %s）", task_id)

    def _load_task_resume(self, task_id: str, task: dict) -> dict | None:
        """C6 认领即复活：读任务键断点快照（task-<tid>.resume.json）。
        objective 匹配 → 返回快照（messages 整体/next_step/max_steps 还原，
        由调用方消费即删）；objective 被改/损坏/任务不符 → 删快照、返回 None
        （降级 C10 transcript 接手）。"""
        path = task_resume_path(self.artifacts_dir, task_id)
        if path is None or not path.exists():
            return None
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = None
        if (not isinstance(st, dict) or st.get("task_id") != task_id
                or not isinstance(st.get("messages"), list)):
            clear_task_resume(self.artifacts_dir, task_id)  # 损坏/不符 → 清理降级
            return None
        if str(st.get("objective") or "").strip() != \
                str(task.get("objective") or "").strip():
            clear_task_resume(self.artifacts_dir, task_id)  # objective 被改 → 降级接手
            return None
        if not isinstance(st.get("max_steps"), int) or st["max_steps"] <= 0:
            clear_task_resume(self.artifacts_dir, task_id)
            return None
        return st

    def _load_task_transcript(self, task_id: str) -> list[dict[str, Any]]:
        """C10 跨会话接手：读任务现场文件为初始对话历史。防御点：JSON 损坏/
        task_id 不符/messages 结构异常 → 空列表降级（履历仍经接手提示注入）；
        只取末尾 60 条（≈30 步完整对话，更早内容以黑板 finding/事件为准）。"""
        path = task_transcript_path(self.artifacts_dir, task_id)
        if path is None:
            return []
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(st, dict) or st.get("task_id") != task_id:
            return []
        msgs = st.get("messages")
        if not isinstance(msgs, list):
            return []
        msgs = [m for m in msgs
                if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
                and m.get("content") is not None]
        if not msgs:
            return []

        omitted = len(msgs) - 60
        if omitted > 0:
            msgs = msgs[-60:]
            # 截断边界不得拆开 tool_use/tool_result 对（首条若是 tool_result，
            # 其配对的 tool_use 已在被省略段里，API 会拒）——继续丢弃到非 result 为止
            while msgs and _is_tool_result(msgs[0]):
                msgs.pop(0)
                omitted += 1
            msgs = [{"role": "user",
                     "content": f"（此前 {omitted} 条对话历史已省略，"
                                "完整结论以黑板 finding/事件为准）"}] + msgs
        return msgs

    def _load_persisted_snapshot(self) -> dict | None:
        """rehydrate：按 sessions.meta 指针读回暂停快照；命中即置回暂停态。
        文件缺失/损坏 → 返回 None（走旧版无快照路径，任务靠租约过期回队列）。"""
        path = self._snapshot_path()
        if path is None:
            return None
        meta = self.session.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        if not isinstance(meta, dict) or not meta.get("resume_snapshot"):
            return None
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("暂停快照读取失败（会话 %s）", self.session["id"])
            return None
        if not isinstance(st, dict) or "messages" not in st:
            return None
        if isinstance(st.get("max_steps"), int) and st["max_steps"] > 0:
            self.dispatcher.max_steps = st["max_steps"]  # 暂停时的预算随快照还原
        return st

    def _clear_snapshot(self) -> None:
        """快照已消费（续跑/中断）：删文件 + 清 meta 指针。失败只留垃圾文件，
        不影响主流程（下次暂停会覆盖同名文件）。"""
        path = self._snapshot_path()
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("暂停快照文件清理失败（会话 %s）", self.session["id"])
        try:
            self.bb.set_session_meta(self.session["id"], {"resume_snapshot": None})
        except Exception:  # noqa: BLE001
            log.exception("暂停快照指针清理失败（会话 %s）", self.session["id"])

    def revive_snapshot(self, task_id: str) -> dict | None:
        """E12：人工中断后从落盘快照复活（限原会话）——刷新会话行取最新快照
        指针，载回 _resume_state 并校验任务匹配；不匹配/无快照返回 None。
        任务的 reopen/claim 与 worker 提交由 API 层编排（POST /tasks/{tid}/resume）。"""
        try:
            row = self.bb.get_session(self.session["id"])
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            self.session = dict(row)  # 中断可能发生在本进程外：meta 指针要现读
        st = self._load_persisted_snapshot()
        if st is None or st.get("task_id") != task_id:
            return None
        self._resume_state = st
        self.paused = False
        return st

    def _task_gone(self) -> bool:
        """A1：当前任务行是否已从看板删除（claimed 任务可被人工取消，步边界感知）。"""
        task_id = self.dispatcher.current_task_id
        return bool(task_id) and self.tq.get_task(task_id) is None

    def _abort_current_task(self) -> None:
        """硬中断收尾：任务 fail（人工中断，不回队列）→ 会话空闲 + 审计。
        任务行已被删除（看板取消）时跳过 fail——task.deleted 事件即审计。
        E12：人工中断**保留落盘快照**（任务续跑凭证，看板 failed 卡可「▶ 续跑」
        原会话复活）；仅任务已删除/无快照时清理，防孤儿文件。
        C6：resumable 修复——原会话键快照**或**任务键断点快照任一存在即可续跑
        （旧实现只看会话键，暂停后中断的孤儿场景会误判不可续跑）。"""
        task_id = self.dispatcher.current_task_id
        if task_id is None and self._resume_state:
            task_id = self._resume_state.get("task_id")  # 空闲暂停态被中断：快照任务也要收尾
        self._resume_state = None
        task_alive = bool(task_id) and self.tq.get_task(task_id) is not None
        resumable = False
        if task_alive:
            path = self._snapshot_path()
            resumable = ((path is not None and path.exists())  # E8 会话键快照
                         or task_resume_path(self.artifacts_dir,
                                             task_id) is not None)  # C6 任务键快照
        if not resumable:
            self._clear_snapshot()  # 无任务/任务已删/无落盘快照：清理防孤儿
            clear_task_resume(self.artifacts_dir, task_id or "")
        self.paused = False
        self._pause_req.clear()
        self._abort_req.clear()
        self._stop_after_task = True  # 让 worker 循环退出，不再领新任务
        self._stop_heartbeat()
        note = "人工中断"
        if task_id:
            if not task_alive:
                note = "任务已被删除"
            else:
                try:
                    self.tq.fail(task_id, self.session["id"], "人工中断",
                                 resumable=resumable,
                                 blocked_reason="aborted",
                                 persona_role=self.dispatcher.current_persona_role)
                except Exception:  # noqa: BLE001
                    log.exception("中断 fail 任务失败")
            self.dispatcher.current_task_id = None
        self._restore_base_persona()  # v14：硬中断恢复底色（下一单按新任务重新换装）
        self.dispatcher._close_browser_session()  # F6-v3：任务结束清浏览器 Page
        try:
            self.bb.set_session_status(self.session["id"], "idle")
        except Exception:  # noqa: BLE001
            log.exception("set_session_status(idle) 失败")
        self.bb.append_event(
            self.project_id, "session.aborted",
            {"session_id": self.session["id"], "task_id": task_id, "note": note},
            session_id=self.session["id"], author=self.session["id"])

    def _stuck(self, step: int) -> bool:
        return (self.planner_llm is not None
                and step - self.dispatcher.last_progress_step >= self.config.stuck_after)

    def _active_exploration(self, lps: int, step: int) -> dict | None:
        """D9 卡死预检（纯机械、零 LLM）：窗 (lps,step] 内本会话的 command /
        file.read 事件命中以下任一信号 → 返回信号明细；否则 None（走顾问链）。

        ① 新文件：读到本会话此前（step≤lps）未读过的路径；
        ② 命令演进：窗内命令 n≥2、去重后 ≥2、最高重复次数 ≤n/2——半数以上同一条
           即判打转。预检在步**开始**执行，窗内动作最多 stuck_after−1 个（本步尚未
           跑），故下限取 2。无 step 字段的旧事件一律不参与（宁叫顾问也不假延长）。"""
        events = self.bb.recent_events(self.project_id, tail=200,
                                       session_id=self.session["id"])
        cmds: list[str] = []
        new_paths: list[str] = []
        old_paths: set[str] = set()
        for e in events:
            st = int(e["payload"].get("step") or 0)
            if e["kind"] == "command":
                cmd = str(e["payload"].get("cmd", "")).strip()
                if st > lps and cmd:
                    cmds.append(cmd)
            elif e["kind"] == "file.read":
                path = str(e["payload"].get("path", "")).strip()
                if not path:
                    continue
                if st > lps:
                    new_paths.append(path)
                elif 0 < st <= lps:
                    old_paths.add(path)
        signals: dict[str, Any] = {}
        unseen = sorted({p for p in new_paths if p not in old_paths})
        if unseen:
            signals["new_files"] = unseen[:5]
        n = len(cmds)
        if n >= 2:
            counts: dict[str, int] = {}
            for c in cmds:
                counts[c] = counts.get(c, 0) + 1
            unique, top = len(counts), max(counts.values())
            if unique >= 2 and top <= n // 2:
                signals["command_count"] = n
                signals["unique_commands"] = unique
                signals["top_repeat"] = top
        return signals or None

    def _extend_stuck_window(self, step: int, signals: dict) -> None:
        """D9：活跃探索静默延长——重置观察窗、extensions+1、落 agent.stuck_extend
        事件。不叫顾问、不注入消息、零 LLM 成本。"""
        self.dispatcher.last_progress_step = step
        self._stuck_extensions += 1
        self.bb.append_event(
            self.project_id, "agent.stuck_extend",
            {"session_id": self.session["id"],
             "task_id": self.dispatcher.current_task_id,
             "step": step, "extension": self._stuck_extensions,
             "signals": signals},
            session_id=self.session["id"], author=self.session["id"])

    def _stuck_escalate(self, step: int, verdict_reason: str = "") -> None:
        """卡死升级停轮：落 ``agent.stuck_escalate`` + awaiting_human，复用 C1/E2
        停轮保护语义（快照+fail resumable，人工可续跑/重开/改目标），宁停不烧。
        三种触发（payload.trigger）：
        - advisor_terminate：D7 顾问裁决终止（带裁决理由 verdict_reason）；
        - hard_backstop：D7 顾问裁决「继续」后又满窗，第 3 轮机械硬闸；
        - mechanical：无顾问介入的机械升级（兼容路径）。"""
        waves = self._stuck_waves + 1
        lps = self.dispatcher.last_progress_step
        if verdict_reason:
            trigger, tail_text = (
                "advisor_terminate", f"策略顾问裁决终止：{verdict_reason}")
        elif self._stuck_waves >= 2:
            trigger, tail_text = (
                "hard_backstop", "策略顾问裁决「继续」后仍无进展，硬闸停轮")
        else:
            trigger, tail_text = (
                "mechanical", "策略顾问已介入一轮未打破僵局")
        self.dispatcher.summary = (
            f"卡死升级挂起：第 {waves} 轮卡死仍无进展"
            f"（上次进展在第 {lps} 步），{tail_text}")
        self.dispatcher.awaiting_human = True
        self.bb.append_event(
            self.project_id, "agent.stuck_escalate",
            {"session_id": self.session["id"],
             "task_id": self.dispatcher.current_task_id,
             "waves": waves, "step": step,
             "last_progress_step": lps,
             "trigger": trigger, "verdict_reason": verdict_reason,
             "repeated_commands": self._command_repeat_stats(),
             "recent_commands": self._recent_command_summary()},
            session_id=self.session["id"], author=self.session["id"])

    def _command_repeat_stats(self, event_window: int = 100,
                              cap: int = 10) -> list[dict]:
        """D2：本会话最近事件窗内 **完全相同命令 ×N** 排行（从 command 事件
        机械聚合，只保留 ×≥2，cap 10）——纯计数不做归一化/相似度，收敛性
        判断交 LLM。"""
        events = self.bb.recent_events(self.project_id, tail=event_window,
                                       session_id=self.session["id"])
        counts: dict[str, int] = {}
        for e in events:
            if e["kind"] == "command":
                cmd = str(e["payload"].get("cmd", "")).strip()
                if cmd:
                    counts[cmd] = counts.get(cmd, 0) + 1
        rows = sorted(((n, c) for c, n in counts.items() if n >= 2),
                      key=lambda x: -x[0])[:cap]
        return [{"cmd": c[:200], "times": n} for n, c in rows]

    def _direction_fingerprint(self, task_id: str) -> str:
        """阶段四 M1 跨窗方向指纹（stuck-convergence M2 定稿落地，2026-09-28）：
        聚合本任务历次认领会话的命令——精确计数排行（完全相同命令 ×N，cap 10）
        + 首词聚类（cap 15，去重后计数），全段 ≤20 行。接手窗进场即知已试方向，
        避免跨窗重踩；无命令/无历史零注入。"""
        from core.blackboard.traces import _claimed_sessions, _session_task_windows
        try:
            sessions = _claimed_sessions(self.bb.conn, self.project_id, task_id)
            if not sessions:
                return ""
            cmds: list[str] = []
            for sid in sessions:
                wins = [w for w in _session_task_windows(
                            self.bb.conn, self.project_id, sid)
                        if w["task_id"] == task_id]
                for w in wins:
                    lo, hi = w["lo"], w.get("hi")
                    if hi is None:
                        continue  # 进行中的区间（当前窗）不纳入指纹（只给历史方向）
                    rows = self.bb.conn.execute(
                        "SELECT payload FROM events WHERE project_id=? AND session_id=?"
                        " AND kind='command' AND id>? AND id<=?",
                        (self.project_id, sid, lo, hi)).fetchall()
                    for r in rows:
                        try:
                            cmd = str((json.loads(r["payload"] or "{}")
                                       or {}).get("cmd", "")).strip()
                        except (ValueError, TypeError):
                            continue
                        if cmd:
                            cmds.append(cmd)
            if not cmds:
                return ""
            # 精确计数排行（×≥2，cap 10）
            counts: dict[str, int] = {}
            for c in cmds:
                counts[c] = counts.get(c, 0) + 1
            exact = sorted(((n, c) for c, n in counts.items() if n >= 2),
                           key=lambda x: -x[0])[:10]
            # 首词聚类（cap 15）：去重计数
            first = {}
            for c in cmds:
                head = c.split()[0][:30] if c.split() else c[:30]
                first[head] = first.get(head, 0) + 1
            heads = sorted(first.items(), key=lambda x: -x[1])[:15]
            lines: list[str] = ["🧭 方向指纹——本任务此前已试方向（接手免重踩，勿重复已失败路径）："]
            if exact:
                lines.append("重复命令（完全相同的 ×N，多在原地打转）：")
                for n, c in exact:
                    lines.append(f"  ×{n} {c[:120]}")
            if heads:
                lines.append("已用命令前缀分布（首词聚类，覆盖已试方向）：")
                for head, n in heads:
                    lines.append(f"  ×{n} {head}")
            return "\n".join(lines)[:1600]
        except Exception:  # noqa: BLE001 —— 指纹失败不影响认领主链
            log.exception("跨窗方向指纹失败（忽略）")
            return ""

    def _recent_command_summary(self, event_window: int = 50,
                                cap: int = 5) -> list[str]:
        """D1：最近命令摘要（stuck_escalate 事件给人工快速判断卡在哪）。"""
        events = self.bb.recent_events(self.project_id, tail=event_window,
                                       session_id=self.session["id"])
        cmds = [str(e["payload"].get("cmd", ""))[:200]
                for e in events if e["kind"] == "command"]
        return cmds[-cap:]

    def _advisor_prompt(self, messages: list[dict], objective: str) -> str:
        """策略顾问（§3）：规划上下文看执行摘要，给换思路建议——不是换 Agent。
        v0.65 卡壳召回：用对话尾部做 query 检索知识库，把最相关 1-2 篇手册摘要
        注入顾问视野——优先参考已验证路径，避免顾问建议重复试错。
        2026-09-20 两修：①视野收窄到**本会话**事件（此前全项目 limit=30，多窗时
        顾问满眼别人的上下文，执行者正确地把建议当跨上下文噪声拒掉，白烧一次
        调用）；②顾问发言落 `advisor.intervention` 事件——此前只进 messages，
        直播间只见 token 行不见说了什么（人工无法判断顾问干了什么、建议是否
        被采纳）。"""
        events = self.bb.recent_events(self.project_id, tail=30,
                                       session_id=self.session["id"])
        digest = json.dumps(
            [{"kind": e["kind"], "payload_head": json.dumps(e["payload"], ensure_ascii=False)[:120]}
             for e in events], ensure_ascii=False)
        kb_recall = self._kb_recall_for_advisor(messages)
        # D2：完全相同命令 ×N 排行注入（收敛性判断交 LLM，不自研相似度算法）
        repeats = self._command_repeat_stats()
        repeat_section = ""
        if repeats:
            lines = "\n".join(f"  ×{r['times']} {r['cmd']}" for r in repeats)
            repeat_section = ("\n最近重复命令（完全相同 ×N——自查是否在原地打转，"
                              f"建议必须避开这些路径）：\n{lines}\n")
        try:
            resp = self.planner_llm.chat(
                [{"role": "user", "content":
                  f"目标: {objective}\n最近事件摘要: {digest}\n"
                  f"{kb_recall}{repeat_section}\n执行已多步无进展。给出 3 条以内换思路建议，"
                  "直接可执行。若知识库经验段有已验证路径，优先参考。"}],
                system="你是策略顾问，负责打破执行僵局。简洁、具体、不重复已失败路径。")
            advice = resp.text
            self._record_usage(resp, source="planner", llm_obj=self.planner_llm)
        except Exception as e:  # noqa: BLE001
            log.warning("策略顾问调用失败: %s", e)
            advice = "（顾问不可用）回顾黑板去重情况，换一个未尝试的攻击面。"
        self.bb.append_event(
            self.project_id, "advisor.intervention",
            {"text": advice[:2000]},
            session_id=self.session["id"], author=self.session["id"])
        return f"[策略顾问]\n{advice}"

    def _kb_recall_for_advisor(self, messages: list[dict], cap: int = 2,
                               chars: int = 800) -> str:
        """卡壳召回（v0.65）：对话尾部文本 → kbindex 匹配 top 手册 → 截摘要。
        任一环节失败/无命中返空串，顾问流程不受影响。"""
        if not self.packs_root:
            return ""
        try:
            tail_texts: list[str] = []
            for m in messages[-8:]:
                content = m.get("content")
                if isinstance(content, str):
                    tail_texts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") in (
                                "text", "tool_result"):
                            tail_texts.append(str(block.get("content", "")))
            query = " ".join(tail_texts)[-600:]
            if not query.strip():
                return ""
            hints = kb_module_hints(self.packs_root, self.capabilities, query,
                                    cap=cap)
            parts: list[str] = []
            for h, sec in hints:
                body = read_kb_module(self.packs_root, self.capabilities,
                                      h.module, max_chars=chars)
                if body:
                    loc = f"，相关段落：{sec}" if sec else ""
                    parts.append(
                        f"### {h.title}（kb_open: {h.module}{loc}）\n{body}")
            if not parts:
                return ""
            return ("## 知识库相关经验（优先参考已验证路径，避免重复试错）\n"
                    + "\n\n".join(parts))
        except Exception as e:  # noqa: BLE001 —— 召回失败静默降级
            log.warning("卡壳 kb 召回失败（忽略）: %s", e)
            return ""

    # ---------- D7 顾问裁决模式（2026-09-24；D1 修订） ----------

    def _advisor_verdict(self, messages: list[dict], objective: str,
                         step: int) -> str:
        """波次 2 顾问**裁决模式**（advisor.verdict，与建议模式 advisor.suggest
        分立）：对照上次建议与之后的实际执行，结构化三选一——
        terminate / continue / human。

        - terminate / human：本方法已置 awaiting_human（C1 分支落快照+fail
          resumable）；terminate 复用 `_stuck_escalate` 落点并带裁决理由。
        - continue：半强制指令注入消息，调用方重开观察窗、waves=2。
        任何 LLM/解析失败一律回落 terminate（宁严勿松——回落即 D7 之前的
        机械停轮行为，绝不回落 continue）。全程留 ``advisor.verdict`` 事件。"""
        events = self.bb.recent_events(self.project_id, tail=40,
                                       session_id=self.session["id"])
        prior_advice = ""
        for e in events:  # 升序：取最后一条顾问建议
            if e["kind"] == "advisor.intervention":
                prior_advice = str(e["payload"].get("text", ""))
        window_cmds = self._recent_command_summary(event_window=40, cap=12)
        lps = self.dispatcher.last_progress_step
        kb_recall = self._kb_recall_for_advisor(messages)
        cmd_block = "\n".join(f"  - {c}" for c in window_cmds) \
            or "  （无命令记录）"
        prompt = (
            f"目标: {objective}\n"
            f"## 你上次给出的建议\n{prior_advice or '（未检索到上次建议记录）'}\n"
            f"## 上次建议之后执行者实际执行的命令（最近）\n{cmd_block}\n"
            f"## 硬事实\n自第 {lps} 步起无黑板写入，当前第 {step} 步"
            f"（已连续 {step - lps} 步无进展）。\n{kb_recall}\n"
            "## 裁决\n对照上次建议与后续事实判断：建议已被执行但仍失败 → "
            "terminate；建议被无视或未被正确执行 → continue，且 instruction 必须"
            "是与上次不同、具体可执行的新指令；关键信息缺失、你无法判断 → human。\n"
            "只输出一个 JSON 对象，不要任何其他文字：\n"
            '{"decision": "terminate|continue|human", "reason": "一句话理由",'
            ' "instruction": "continue 时必填的具体新指令，否则留空"}')
        try:
            resp = self.planner_llm.chat(
                [{"role": "user", "content": prompt}],
                system="你是策略顾问，对卡死任务做最终裁决。诚实、克制，"
                       "不为失败的路线辩护，不重复已失败路径。")
            decision, reason, instruction = self._parse_verdict(resp.text)
            self._record_usage(resp, source="planner", llm_obj=self.planner_llm)
        except Exception as e:  # noqa: BLE001
            log.warning("策略顾问裁决调用失败，回落终止: %s", e)
            decision, reason, instruction = (
                "terminate",
                f"顾问裁决调用失败（{type(e).__name__}），按终止处理", "")
        self.bb.append_event(
            self.project_id, "advisor.verdict",
            {"session_id": self.session["id"],
             "task_id": self.dispatcher.current_task_id,
             "wave": self._stuck_waves + 1, "step": step,
             "decision": decision, "reason": reason,
             "instruction": instruction[:300]},
            session_id=self.session["id"], author=self.session["id"])
        if decision == "continue":
            messages.append({"role": "user", "content":
                f"[策略顾问·裁决：继续]\n裁决理由：{reason}\n"
                "新方向（半强制——本轮先回应将如何执行，或说明为何不适用）：\n"
                f"{instruction}"})
            return "continue"
        if decision == "human":
            self.dispatcher.summary = (
                f"顾问裁决请求人工：{reason or '策略顾问认为关键信息不足，无法裁决'}")
            self.dispatcher.awaiting_human = True
            return "human"
        self._stuck_escalate(step, verdict_reason=reason)
        return "terminate"

    def _parse_verdict(self, raw_text: str) -> tuple[str, str, str]:
        """解析顾问裁决 JSON；任何不合规一律回落 terminate（宁严勿松）。
        continue 缺具体 instruction → 同样回落 terminate。"""
        fallback = ("terminate", "顾问裁决输出无法解析，按终止处理", "")
        m = re.search(r"\{.*\}", raw_text or "", re.DOTALL)
        if not m:
            return fallback
        try:
            data = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return fallback
        if not isinstance(data, dict):
            return fallback
        decision = str(data.get("decision", "")).strip().lower()
        if decision not in ("terminate", "continue", "human"):
            return fallback
        reason = str(data.get("reason", "")).strip()[:500]
        instruction = str(data.get("instruction", "")).strip()[:800]
        if decision == "continue" and not instruction:
            return ("terminate", "顾问判继续但未给出具体指令，按终止处理", "")
        return decision, reason, instruction

    # ---------- v0.65 done 自动提案（T4 沉淀飞轮；experience-sedimentation 提纯） ----------

    def _task_finding_ids(self, task_id: str) -> list[str]:
        """本任务区间（最近一次认领→收尾）内本会话产出的 finding id（事件反查）。
        用 _session_task_windows 的 (lo,hi] 区间 + session_id=author 双约束精确到
        本任务——多任务会话的历史 verified 不误伤后续常规任务。判定失败按无产出
        处理（宁少勿滥）。"""
        from core.blackboard.traces import _session_task_windows
        try:
            wins = [w for w in _session_task_windows(
                        self.bb.conn, self.project_id, self.session["id"])
                    if w["task_id"] == task_id and w.get("hi")]
            if not wins:
                return []
            w = wins[-1]
            rows = self.bb.conn.execute(
                "SELECT DISTINCT json_extract(payload,'$.finding_id') AS fid FROM events"
                " WHERE project_id=? AND session_id=? AND kind IN"
                " ('finding.new','finding.merged') AND id>? AND id<=?",
                (self.project_id, self.session["id"], w["lo"], w["hi"])).fetchall()
            return [r["fid"] for r in rows if r["fid"]]
        except Exception:  # noqa: BLE001
            return []

    def _sediment_verdict(self, task_id: str, result_note: str = "") -> dict:
        """沉淀条件判定（experience-sedimentation §4.1 口径表，单一实现——campaign
        条件写入与复盘条件触发共用，勿在别处再写第二份口径）：
        - done + 本任务产出 verified finding（或含本任务节点的 exploited 链）→
          campaign 写入 + 复盘（打法沉淀）；
        - done + 本任务产出 FP finding 且无 verified/exploited 链 → campaign 写入
          死路条目（tags=["dead_end"]）、不复盘（M6 F1：负知识跨项目复用——
          死路的结构化口径就是 status='false-positive'，orchestrator-efficiency §0-11）；
        - done 无产出但 result_note ≥500 字 → 只复盘（高质量收尾，每会话上限
          _SEDIMENT_LITE_CAP 条防刷量）；
        - failed + error（真失败，Agent 自填归因）→ 只复盘（失败模式，提炼避坑教训）；
        - failed + aborted/awaiting_human、done 其余零产出 → 都不（非方法论性失败
          与常规收尾不值得沉淀）。
        返回 {campaign, review, finding_ids, dead_end}。"""
        task = self.tq.get_task(task_id) or {}
        status = task.get("status")
        if status == "failed":
            real = (task.get("blocked_reason") or "error") == "error"
            return {"campaign": False, "review": real, "finding_ids": [],
                    "dead_end": False}
        if status != "done":
            return {"campaign": False, "review": False, "finding_ids": [],
                    "dead_end": False}
        finding_ids = self._task_finding_ids(task_id)
        if finding_ids:
            marks = ",".join("?" for _ in finding_ids)
            row = self.bb.conn.execute(
                f"SELECT 1 FROM findings WHERE project_id=? AND status='verified'"
                f" AND id IN ({marks}) LIMIT 1",
                (self.project_id, *finding_ids)).fetchone()
            if row is not None:
                return {"campaign": True, "review": True, "finding_ids": finding_ids,
                        "dead_end": False}
        # exploited 链兜底：任务无 verified finding 但轨迹链打穿（链节点 id =
        # `{task_id}#{kind}:{value}`，origin='trace' 为任务轨迹自动链）
        try:
            row = self.bb.conn.execute(
                "SELECT 1 FROM chains c JOIN chain_links l ON l.chain_id=c.id"
                " WHERE c.project_id=? AND c.origin='trace' AND c.status='exploited'"
                " AND l.node_id LIKE ? LIMIT 1",
                (self.project_id, f"{task_id}#%")).fetchone()
        except Exception:  # noqa: BLE001
            row = None
        verified = row is not None
        if verified:
            return {"campaign": True, "review": True, "finding_ids": finding_ids,
                    "dead_end": False}
        # M6 F1 死路记账档：本任务 FP-only → 负知识入 campaign（content 取死路
        # 原因），不产 kb 复盘提案（避坑方法论可走下方高质量复盘档）
        if finding_ids:
            marks = ",".join("?" for _ in finding_ids)
            fp = self.bb.conn.execute(
                f"SELECT 1 FROM findings WHERE project_id=? AND status='false-positive'"
                f" AND id IN ({marks}) LIMIT 1",
                (self.project_id, *finding_ids)).fetchone()
            if fp is not None:
                return {"campaign": True, "review": False,
                        "finding_ids": finding_ids, "dead_end": True}
        # M6 F1 高质量复盘档：零产出但收尾总结够长 → 只复盘（每会话上限防刷量；
        # 额度在 _sediment_proposal 实际产提案时消耗，这里只读）
        note = (result_note or "").strip()
        if len(note) >= _SEDIMENT_LITE_MIN_NOTE:
            used = getattr(self, "_sediment_lite_used", 0)
            if used < _SEDIMENT_LITE_CAP:
                return {"campaign": False, "review": True, "finding_ids": [],
                        "dead_end": False}
        return {"campaign": False, "review": False, "finding_ids": [],
                "dead_end": False}

    def _campaign_memory(self, task_id: str, result_note: str) -> None:
        """⑥ 战役记忆写入（complete_task 并联 hook；experience-sedimentation M1
        条件收窄）：campaign 只存「成功打法索引层」——产出过 verified 发现或
        exploited 链的任务才写；无产出任务/常规操作全量退役（失败教训走 kb 复盘）。
        M6 F1 扩死路记账档（tags=["dead_end"]，§0-11）：FP-only 任务负知识入全局库，
        content 优先 result_note、空则 fallback FP finding title。
        content=result_note 纯净收尾（M1 瘦身：发现明细本就在黑板，砍「最近 5 条」
        拼接——那是检索语境噪声）。写全局库（跨项目召回）+ 项目事件流
        campaign.memory（双写不保强一致）。"""
        verdict = self._sediment_verdict(task_id, result_note)
        if not verdict["campaign"]:
            return
        content = (result_note or "").strip()
        if not content and verdict.get("dead_end"):
            # 死路条目无收尾总结 → 取 FP finding title 兜底（负知识必须有内容可召回）
            fid = verdict["finding_ids"]
            if fid:
                marks = ",".join("?" for _ in fid)
                try:
                    row = self.bb.conn.execute(
                        f"SELECT title FROM findings WHERE project_id=? AND"
                        f" status='false-positive' AND id IN ({marks})"
                        f" ORDER BY created_at DESC LIMIT 1",
                        (self.project_id, *fid)).fetchone()
                    content = f"〔死路〕{row['title']}" if row else ""
                except Exception:  # noqa: BLE001
                    content = ""
        if not content:
            return  # 无收尾总结=无可复用打法
        task = self.tq.get_task(task_id) or {}
        try:
            row = self.campaign.add(
                project_id=self.project_id, track=self.track,
                capability="/".join(self.capabilities),
                task_type=task.get("task_type", ""),
                title=task.get("objective", "")[:200] or task_id,
                content=content,
                target_key=(task.get("scope") or "")[:200],
                tags=["dead_end"] if verdict.get("dead_end") else None)
        except Exception as e:  # noqa: BLE001
            log.warning("战役记忆写入失败（跳过）: %s", e)
            return
        try:
            self.bb.append_event(
                self.project_id, "campaign.memory",
                {"id": row["id"], "title": row["title"],
                 "content_head": row["content"][:120],
                 "dead_end": bool(verdict.get("dead_end"))},
                session_id=self.session["id"], author=self.session["id"])
        except Exception:  # noqa: BLE001 —— 事件失败不回滚全局库
            log.warning("campaign.memory 事件落库失败（全局库已写入）")

    def _sediment_proposal(self, task_id: str, result_note: str,
                           *, failure: bool = False) -> None:
        """任务收尾自动复盘（§4 沉淀飞轮；experience-sedimentation M1 条件触发 +
        M2 提纯）：条件见 _sediment_verdict（done+产出=打法沉淀；failed+error=失败
        避坑教训；aborted/无产出 done 不跑）。planner LLM 复盘 → kb/index/case/skill
        提案草稿（origin=agent，人批准后进知识库/技能）。M2 提纯三件：①复盘前对账
        （kb 对账注入既有相关模块 + K7 skill 对账注入会话技能清单，已有手册/技能引导
        edit 补段不 create 重复新建）；②失败模式 instruction（只提炼方法论教训，环境
        问题 NONE）；③reason 证据锚点要求（解析后机器校验，缺引用静默跳过）。输出
        NONE / 任何失败都静默跳过——绝不影响任务收尾；与 AI 自主提案共用每会话 3 条
        pending 上限。"""
        from core.skills import proposals  # 延迟导入避开 tools↔loop 环
        if not self.packs_root:
            return
        verdict = self._sediment_verdict(task_id, result_note)
        if not verdict["review"]:
            return
        # M6 高质量复盘档额度：零产出长收尾的复盘实际要跑 LLM，产提案前占用额度
        # （verified/failed 档不占——它们不是防刷量对象）
        lite = (not verdict["finding_ids"] and not failure)
        if lite:
            if getattr(self, "_sediment_lite_used", 0) >= _SEDIMENT_LITE_CAP:
                return
            self._sediment_lite_used = getattr(self, "_sediment_lite_used", 0) + 1
        try:
            pending = [p for p in proposals.list_proposals(self.packs_root, "pending")
                       if p.get("session") == self.session["id"]]
            if len(pending) >= ToolDispatcher.PROPOSE_LIMIT_PER_SESSION:
                return
        except Exception:  # noqa: BLE001
            return
        task = self.tq.get_task(task_id) or {}
        # M2 kb 对账前置：既有相关模块清单（K3 同款口径），已有手册 → edit 补段
        kb_block = ""
        try:
            pairs = kb_module_hints(self.packs_root, self.capabilities,
                                    task.get("objective") or result_note, cap=3)
            lines = []
            for entry, _sec in pairs:
                row = f"- {entry.module} —— {entry.title}"
                if entry.summary:
                    row += f"｜{entry.summary}"
                lines.append(row)
            if lines:
                kb_block = ("已有相关知识库模块（相关经验请走 mode=edit 补「已验证路径」"
                            "「坑」段，不要 create 重复新建）：\n" + "\n".join(lines))
        except Exception:  # noqa: BLE001
            pass
        # K7 skill 对账前置：当前会话技能清单注入——LLM 只能对清单内技能提
        # edit（防猜名/对不存在技能 edit 被后端拒），清单外方向只能提 suggest
        skill_block = ""
        try:
            if self.registry is None:
                self.load_skills()
            if self.registry is not None:
                pack_set = set(self.capabilities) | {self.track}
                rows = [f"- {s.name}（{s.pack}/{s.kind}）{s.description or ''}"
                        for s in self.registry.all()
                        if s.enabled and s.pack in pack_set]
                if rows:
                    skill_block = (
                        "已有技能清单（打法沉淀只走 mode=edit 补段或 mode=suggest "
                        "提拆分/新方向建议，不要 create；edit 目标必须在此清单内）：\n"
                        + "\n".join(rows))
        except Exception:  # noqa: BLE001
            skill_block = ""
        events = self.bb.recent_events(self.project_id, tail=30)
        digest = json.dumps(
            [{"kind": e["kind"], "payload_head": json.dumps(e["payload"], ensure_ascii=False)[:120]}
             for e in events], ensure_ascii=False)
        caps = "、".join(self.capabilities) or "(无)"
        if failure:
            mode_line = (
                "本任务以真失败告终（blocked_reason=error）：只提炼可复用的避坑教训"
                "（错误假设/被证伪的手法/更优路径）；环境或临时性问题（网络超时、配额"
                "限流、平台异常等非方法论因素）一律输出 NONE。"
                "reason 必须引用本任务 id（task-xxxxxxxxxxxx）作为证据锚点。")
        else:
            mode_line = (
                "reason 必须引用本任务产出的 finding id（形如 find-xxxxxxxxxxxx）"
                "作为证据锚点；无产出证据引用的提案不合法。")
        instruction = (
            "复盘任务执行过程：是否验证了可复用的有效手法、踩坑或更优路径？\n"
            f"{mode_line}\n"
            "有则只输出一个提案 JSON（无其他文字），四类去向四选一：\n"
            '① kb 经验沉淀：场景经验/坑/产品指纹/现场笔记 → {"kind": "kb", '
            '"mode": "create" 或 "edit", '
            '"target": {"kind": "kb", "cap": "能力包", "path": "kb内相对路径.md"}, '
            '"content": "提案文件全文（须有「## 」段落结构，含「已验证路径」或「坑」段）", '
            '"summary": "一句话", "reason": "证据锚点+关键观察"}\n'
            '② index 增补：本任务验证的测试点在 kb 中有对应手册、且全局 route_index.yaml '
            '缺本域这条目 → {"kind": "index", "mode": "edit", '
            '"target": {"kind": "index", "cap": "能力包"}, '
            '"content": "本域条目全文（kb 路径带域前缀，如 web/webapp/…/手册.md）", "summary": "…", '
            '"reason": "…"}\n'
            '③ case 成功链沉淀（K6）：本任务整条 verified 攻击链/跑通 payload 值得复用 →'
            ' {"kind": "case", "mode": "edit"（对应测试包 成功案例.md 补「已验证路径」段）'
            ' 或 "create"（payloads/ 下新建弹药文件 .md/.py/.txt/.json）, '
            '"target": {"kind": "case", "cap": "能力包", "path": "测试包内相对路径"}, '
            '"content": "文件全文", "summary": "…", "reason": "…"}\n'
            '④ skill 打法沉淀：本任务验证了新的通用打法（对一类漏洞通用的做法）'
            '或对既有技能有方法论修正 → '
            '{"kind": "skill", "mode": "edit"（既有技能补「已验证路径」「坑」段，'
            '目标必须在下方技能清单内）或 "suggest"（提出拆分/新打法方向建议，'
            '产建议文档由人执行）, '
            '"target": {"kind": "skill", "skill_kind": "capability|track", '
             '"owner": "包/轨名", "name": "技能名"}, '
             '"content": "技能正文全文（edit 须含 frontmatter，name 与技能名一致；'
             'suggest 为建议文档全文，须有「## 」段落结构）", '
             '"summary": "…", "reason": "证据锚点+关键观察"}\n'
            f"cap 只能取本会话能力包之一（{caps}）；kb 新经验补对应测试包手册的"
            "「已验证路径」「坑」段或写成 payloads/ 弹药；英文快照原文不覆盖不翻译。"
            + (f"\n{kb_block}" if kb_block else "")
            + (f"\n{skill_block}" if skill_block else "")
            + "\n无则只输出 NONE。")
        try:
            resp = self.planner_llm.chat(
                [{"role": "user", "content":
                  f"任务: {task_id}\n结果: {result_note[:500]}\n"
                  f"最近事件摘要: {digest}\n\n{instruction}"}],
                system="你是经验沉淀复盘员：只提炼本任务中验证过的事实，"
                       "不臆测、不复述常识。")
            self._record_usage(resp, source="planner", llm_obj=self.planner_llm)
            text = (resp.text or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("收尾复盘调用失败（跳过沉淀）: %s", e)
            return
        if not text or text.upper().startswith("NONE"):
            return
        text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text,
                      flags=re.MULTILINE).strip()
        try:
            payload = json.loads(text)
            tgt = payload.get("target") or {}
            # K4/K7：复盘沉淀允许 kb / index / case / skill 四类去向
            # （index 增补走 edit；skill 仅 edit/suggest 由 create_proposal 再校验；
            #  其余一律按 kb 处理）
            kind = payload.get("kind") or tgt.get("kind") or "kb"
            if kind not in {"kb", "index", "case", "skill"}:
                kind = "kb"
            payload["kind"] = kind
            tgt["kind"] = kind
            payload["target"] = tgt
            payload["project"] = self.project_id
            payload["session"] = self.session["id"]
            payload["task"] = task_id
            label = "failed（error）自动复盘沉淀" if failure else "done 自动复盘沉淀"
            payload["evidence"] = f"任务 {task_id}（{label}）"
            # M2 解析后证据校验：打法沉淀（kb/skill 类）reason 必须引用任务真实
            # 产出的 finding id——机器可校验的部分在这里拦，防 LLM 编造/漏引用
            if kind in {"kb", "skill"} and not failure:
                fids = verdict["finding_ids"]
                if fids and not any(f in (payload.get("reason") or "") for f in fids):
                    log.warning("done 复盘提案缺 finding 证据引用（跳过沉淀）task=%s",
                                task_id)
                    return
            p = proposals.create_proposal(self.packs_root, payload, origin="agent")
        except Exception as e:  # noqa: BLE001  # JSONDecodeError/ProposalError 均静默
            log.warning("收尾自动提案未落地（跳过）: %s", e)
            return
        self.bb.append_event(
            self.project_id, "proposal.created",
            {"id": p["id"], "kind": p["target"].get("kind"),
             "mode": p["mode"], "target": p["target"], "summary": p["summary"],
             "origin": "sediment"},
            session_id=self.session["id"], author=self.session["id"])

    def _record_usage(self, resp: Any, *, source: str, llm_obj: Any) -> None:
        """每次 chat 后用量记账（§6.8）：累加 orchestrator_state + llm.usage 事件；
        全 0 用量（ScriptedLLM/厂商未回）在底层跳过。记账失败不阻断主循环。"""
        try:
            record_llm_usage(
                self.bb, self.project_id, resp.usage, source=source,
                session_id=self.session["id"], model=getattr(llm_obj, "model", ""))
        except Exception:  # noqa: BLE001
            log.exception("用量记账失败")

    def _count_tokens(self, messages: list[dict[str, Any]]) -> int:
        """上下文预算计量（token 分层计数，2026-10-03）：按当前模型选计数器
        （OpenAI 系 tiktoken 精确 / 其余加权估算 / 不可用退化字符数），返回
        **等价字符数**——口径见 core/llm/tokenizer.py 模块文档。阈值
        （context_char_budget / context_summary_chars）语义与取值一字未动，
        升级的只是「这段历史值多少」的算法。"""
        return get_counter(getattr(self.llm, "model", None)).count_messages(messages)

    def _trim(self, messages: list[dict[str, Any]]) -> None:
        """上下文预算：超限则把旧 tool_result 内容替换为占位（保留结构）。"""
        total = self._count_tokens(messages)
        if total <= self.config.context_char_budget:
            return
        for m in messages[:-8]:  # 保留最近 8 条完整
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for block in m["content"]:
                    if block.get("type") == "tool_result" and len(block.get("content", "")) > 200:
                        block["content"] = block["content"][:200] + "…[已截断]"

    def _maybe_summarize(self, messages: list[dict[str, Any]], *,
                         keep_recent: int = 8) -> bool:
        """G3 结构化摘要压缩（2026-09-19）：历史超过 context_summary_chars 时，
        把旧消息经 LLM 按九要素骨架压成一段摘要替换（近 keep_recent 条逐字保留；
        tool_use/tool_result 配对不拆——切割点回退到非 tool_result 消息）。
        就地生效（messages[:] = …，_live_state/快照引用同一列表不受影响）。
        摘要失败静默返回 False（机械 _trim 仍是硬上限兜底）。"""
        total = self._count_tokens(messages)
        threshold = min(self.config.context_summary_chars,
                        self.config.context_char_budget)
        if total <= threshold or len(messages) <= keep_recent + 2:
            return False
        cut = len(messages) - keep_recent
        while cut > 0 and _is_tool_result(messages[cut]):
            cut -= 1  # 边界不得落在 tool_result 上（防拆散 tool_use/result 配对）
        old = messages[:cut]
        if len(old) < 4:
            return False  # 没多少可压的，不值得一次 LLM 调用
        # H1 剪枝先行（借鉴 dsh pruning-before-summary）：旧区间超长 tool_result
        # 先机械剪枝（head 400 + tail 200 + 精确省略计数），常可不动用 LLM 就回到
        # 触发线下——摘要有损不可逆，剪枝只是收紧预览，二者正交。
        pruned = 0
        for m in old:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    c = str(b.get("content", ""))
                    if len(c) > 1000:
                        kept, om = retain(c, head=400, tail=200)
                        if om:
                            b["content"] = kept + omitted_note(om)
                            pruned += 1
        total = self._count_tokens(messages)
        if total <= threshold:
            self.bb.append_event(
                self.project_id, "llm.compact",
                {"before_msgs": len(messages), "after_msgs": len(messages),
                 "summarized": 0, "pruned": pruned, "chars_before": total,
                 "chars_after": total},
                session_id=self.session["id"], author=self.session["id"])
            return True
        lines: list[str] = []
        for m in old:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        lines.append(f"[{role}] {b.get('text', '')}")
                    elif b.get("type") == "tool_result":
                        lines.append(f"[工具结果] {str(b.get('content', ''))[:300]}")
                    elif b.get("type") == "tool_use":
                        args = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
                        lines.append(f"[调用工具] {b.get('name')}({args})")
            else:
                lines.append(f"[{role}] {content}")
        prompt = (
            "请把以下 Agent 执行历史压缩成摘要，九个要素各一小节（无内容的省略）：\n"
            "1. 任务目标与用户意图（用户引导/人类原话**逐字保留**）\n"
            "2. 关键技术概念与目标信息\n"
            "3. 涉及的黑板对象（资产/发现/任务 id 列表，只引用 id）\n"
            "4. 已尝试的步骤与结果（含失败/死路方向——防重走）\n"
            "5. 遇到的错误与修复\n"
            "6. 问题解决过程要点\n"
            "7. 待办与未完成方向\n"
            "8. 当前进行到哪一步\n"
            "9. 建议的下一步\n\n"
            "待压缩历史：\n" + "\n".join(lines))
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}],
                                 system=SUMMARY_SYSTEM)
        except Exception:  # noqa: BLE001 —— 压缩失败不影响主循环
            log.exception("上下文摘要压缩失败（跳过本次）")
            return False
        self._record_usage(resp, source="agent", llm_obj=self.llm)
        text = "".join(b.get("text", "") for b in resp.raw.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text").strip()
        if not text:
            return False
        head = (f"[历史摘要（原 {len(old)} 条消息由系统压缩；黑板对象只存 id，"
                f"细节用 bb_query/kb_open 现查）]\n")
        replaced = [{"role": "user", "content": head + text},
                    {"role": "assistant", "content": "已读摘要，从上一步继续。"}]
        before_msgs = len(messages)
        messages[:] = replaced + messages[cut:]
        # 压完仍超触发线（近 8 条里的大 tool_result 原样保留压不下去）→ 对保留段
        # 超长 tool_result 机械截断兜底（2000 一道、仍超再 200 一道；生产阈值 60k
        # 量级下必回线下，摘要文本/tool_use 块等结构性内容为不可压下限）。
        # 不截就会下一步立即再触发压缩，形成「压一次烧一步 LLM、细节越压越少」的
        # 死循环（实测 5 分钟连压 4 次，chars_after 63k-99k 全部高于 60k 触发线）。
        for cap in (2000, 200):
            total_after = self._count_tokens(messages)
            if total_after <= threshold:
                break
            for m in messages:
                if m.get("role") == "user" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_result" \
                                and len(str(b.get("content", ""))) > cap:
                            b["content"] = str(b["content"])[:cap] + "…[已截断]"
        self.bb.append_event(
            self.project_id, "llm.compact",
            {"before_msgs": before_msgs, "after_msgs": len(messages),
             "summarized": len(old), "chars_before": total,
             "chars_after": self._count_tokens(messages)},
            session_id=self.session["id"], author=self.session["id"])
        return True

    def _salvage_attempt(self, reason: str) -> None:
        """fail 路径抢救收尾（2026-09-20，借鉴 Intentest 降级段）：worker 异常兜底
        fail 前，用一轮**无工具纯文本** LLM 把现场尾部的部分结论落盘为 salvage
        产物——任务虽败，已验证事实/失败方向不陪葬，接手者免重走。
        全程 try/except 全吞：抢救失败绝不影响 fail 本身；人工中止（即点即停，
        _abort_current_task）与 budget pause 不走这里。"""
        ctx = self._salvage_ctx
        self._salvage_ctx = None
        if not ctx:
            return
        messages = ctx.get("messages") or []
        if len(messages) < 4:
            return  # 现场太薄，没什么可抢救
        task_id = self.dispatcher.current_task_id
        lines: list[str] = []
        for m in messages[-14:]:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        lines.append(f"[{role}] {b.get('text', '')}")
                    elif b.get("type") == "tool_result":
                        lines.append(f"[工具结果] {str(b.get('content', ''))[:200]}")
                    elif b.get("type") == "tool_use":
                        args = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
                        lines.append(f"[调用工具] {b.get('name')}({args})")
            else:
                lines.append(f"[{role}] {content}")
        prompt = (
            f"任务「{ctx.get('objective', '')}」因异常即将被终止（原因：{reason}）。"
            "以下是执行历史的尾部。请抢救目前已获得的部分结论，只输出 Markdown 正文，"
            "分三节：## 已确认事实（只写已验证结论，黑板对象引用 id）\n"
            "## 失败/死路方向（避免接手者重走）\n## 接手建议（下一步从哪里续）。\n\n"
            "执行历史尾部：\n" + "\n".join(lines))
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}],
                                 system=SALVAGE_SYSTEM)
        except Exception:  # noqa: BLE001 —— 抢救 LLM 也炸（如配额故障）不影响 fail
            log.exception("salvage 抢救 LLM 调用失败（跳过）task=%s", task_id)
            return
        try:
            self._record_usage(resp, source="agent", llm_obj=self.llm)
            text = "".join(b.get("text", "") for b in resp.raw.get("content", [])
                           if isinstance(b, dict) and b.get("type") == "text").strip()
            if not text or not self.artifacts_dir:
                return
            out_dir = Path(self.artifacts_dir) / "salvage"
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = task_id or self.session["id"]
            path = out_dir / f"salvage-{stem}.md"
            n = 2
            while path.exists():
                path = out_dir / f"salvage-{stem}-{n}.md"
                n += 1
            path.write_text(
                f"# 抢救收尾：{ctx.get('objective', '')}\n\n终止原因：{reason}\n\n{text}",
                encoding="utf-8")
            rel = f"salvage/{path.name}"
            meta = {"session_id": self.session["id"]}
            if task_id:
                meta["task_id"] = task_id
            artifact_id = self.bb.add_artifact(
                self.project_id, rel, kind="salvage",
                description=f"异常终止前的部分结论抢救（{reason[:120]}）",
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                author=self.session["id"], meta=meta)
            self.bb.append_event(
                self.project_id, "task.salvaged",
                {"task_id": task_id, "artifact_id": artifact_id, "path": rel,
                 "reason": reason[:200]},
                session_id=self.session["id"], author=self.session["id"])
            log.info("salvage 抢救落盘 task=%s artifact=%s", task_id, artifact_id)
        except Exception:  # noqa: BLE001
            log.exception("salvage 抢救落盘失败（不影响 fail）task=%s", task_id)

    def _fail_task_on_error(self, exc: BaseException) -> None:
        """Worker 异常兜底：llm.chat 传输层重试耗尽 / 未预期异常穿出 _loop 时，
        任务绝不能悬在 claimed——① 停心跳（否则孤儿心跳线程继续每 10 分钟续租，
        租约永不过期、expire_leases 不回收，任务在本进程内永久卡「执行中」）；
        ② 传输类异常（LLMError，429/5xx/超时重试耗尽）转 **awaiting_human 挂起**
        （orchestrator-efficiency，2026-09-22）：外部条件失败不是打法失败——现场
        快照双写落盘（任务键+会话键，C6 resume 可「⚡ 带现场续跑」），恢复靠人工
        （换模型/等配额），替代原 fail(aborted) 直接终态化（曾致任务只能去
        看板人工兜底）；挂起链路自身失败再退回 aborted 终态兜底。
        ③ 非传输类异常：salvage 抢救收尾（2026-09-20）把现场部分结论落盘为产物
        再 fail(aborted) 落审计——非方法论性失败（experience-sedimentation M1：
        不进失败复盘）。④ 清 current_task_id。
        幂等：current_task_id 已清时只停心跳。"""
        self._stop_heartbeat()
        task_id = self.dispatcher.current_task_id
        if task_id:
            note = f"worker 异常退出（{type(exc).__name__}: {exc}）"[:400]
            if isinstance(exc, LLMError):
                # 不 salvage：提炼也要调 LLM 必炸；快照本身已是完整现场
                hung = False
                try:
                    live = self._salvage_ctx or {}
                    self._persist_snapshot({
                        "system": live.get("system", ""),
                        "messages": sanitize_snapshot_tail(live.get("messages") or []),
                        "objective": live.get("objective", ""),
                        "task_id": task_id,
                        "next_step": self.dispatcher.step + 1,
                        "max_steps": self.dispatcher.max_steps,
                        "reason": "awaiting",
                        "open_intents": self._open_intent_snapshot(),
                    })
                    self.tq.fail(task_id, self.session["id"], note, resumable=True,
                                 blocked_reason="awaiting_human",
                                 persona_role=self.dispatcher.current_persona_role)
                    hung = True
                except Exception:  # noqa: BLE001
                    log.exception("传输异常 awaiting_human 挂起失败，退回 aborted task=%s",
                                  task_id)
                if not hung:
                    try:
                        self.tq.fail(task_id, self.session["id"], note,
                                     blocked_reason="aborted",
                                     persona_role=self.dispatcher.current_persona_role)
                    except Exception:  # noqa: BLE001
                        log.exception("异常兜底 fail 任务失败 task=%s", task_id)
            else:
                self._salvage_attempt(note)
                try:
                    self.tq.fail(task_id, self.session["id"], note,
                                 blocked_reason="aborted",
                                 persona_role=self.dispatcher.current_persona_role)
                except Exception:  # noqa: BLE001
                    log.exception("异常兜底 fail 任务失败 task=%s", task_id)
            self.dispatcher.current_task_id = None
        self._restore_base_persona()  # v14：异常兜底恢复底色（幂等）
        self.dispatcher._close_browser_session()  # F6-v3：任务结束清浏览器 Page

    def _finalize(self) -> None:
        """会话收尾：任务未收尾 → fail（防 lease 占坑）；落 session.finished 事件。"""
        self._salvage_ctx = None  # 会话收尾不做抢救（_fail_task_on_error 专属）
        self._stop_heartbeat()
        if self.dispatcher.current_task_id:
            try:
                self.tq.fail(self.dispatcher.current_task_id, self.session["id"],
                             "会话结束但任务未收尾，自动标记失败",
                             blocked_reason="aborted",
                             persona_role=self.dispatcher.current_persona_role)
            except Exception:  # noqa: BLE001
                log.exception("自动 fail 任务失败")
        self._restore_base_persona()  # v14：会话结束恢复底色（防状态残留）
        self.dispatcher._close_browser_session()  # F6-v3：会话结束清浏览器 Page
        self.bb.append_event(
            self.project_id, "session.finished",
            {"session_id": self.session["id"], "summary": self.dispatcher.summary[:500]},
            session_id=self.session["id"], author=self.session["id"],
        )
        # 状态机对齐（2026-09-24）：正常收尾 DB status 也要回 idle——此前只落
        # session.finished 事件，status 残留 running（session_list 与事件流打架，
        # 也误占活跃语义）。closed 窗不覆盖（排水关窗不经 _finalize，双保险）。
        cur = self.bb.get_session(self.session["id"])
        if (cur or {}).get("status") != "closed":
            try:
                self.bb.set_session_status(self.session["id"], "idle")
            except Exception:  # noqa: BLE001
                log.exception("set_session_status(idle) 失败")
