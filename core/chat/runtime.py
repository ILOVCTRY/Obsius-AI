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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from core.agent.tools import AGENT_TOOLS, ToolDispatcher
from core.blackboard.store import BlackboardClosedError
from core.chat import store as chat_store
from core.chat.mcp_bridge import MCPBridge
from core.llm.provider import ContextOverflowError, LLMError, TransientStreamError
from core.runtime.gateway import ExecutionGateway
from core.skills.experts import load_expert
from core.skills.registry import SkillRegistry
from core.skills.rules import build_rules_preamble

log = logging.getLogger(__name__)

ORCHESTRATOR_ID = "chat-orchestrator"

# 子专家不可用的控制原语（任务队列/计划闸/收尾/私信/提案/HITL——
# 对话线程没有任务上下文与收件箱，这些工具既无语义也危险：publish/claim
# 会污染任务队列）
# 意图三件套（declare/close/reopen_intent）自 2026-10-01 起**开放给对话链**
# （见 intent-tools-chat）：对话主控/子专家同样要按「声明假设→执行→收尾」走，
# 否则对话产出的发现不落链路图（用户实测：任务跑完但在「黑板→发现→链路」看不到
# 任何意图——对话链整条链路从未声明过意图）。
_EXPERT_EXCLUDED = {
    "task_plan", "task_step", "task_reconcile", "publish_task",
    "complete_task", "fail_task", "finish", "request_steps",
    "propose_pack_edit",
    "bb_notify", "request_authorization",
}

# 意图三件套（对话链也要，语义=规划产物与收尾纪律，不依赖任务队列）
_INTENT_TOOLS = ("declare_intent", "close_intent", "reopen_intent", "bb_delete_intent")

_ORCH_BASE_TOOLS = ("todo_write", "call_expert", "skill_open",
                    "kb_search", "kb_open", "bb_query",
                    "bb_delete_asset", "bb_merge_assets", *_INTENT_TOOLS)

# 对话轮步数上限（2026-10-01 由 24/32 提升至 200：修「步数耗尽但全程只调工具
# → 无终稿 → 落『本轮未产出文本回复』」；配合 _loop 末步强制终稿兜底）
_ORCH_MAX_STEPS = 200
_EXPERT_MAX_STEPS = 200

# 流式节流（2026-10-04 顺滑化）：由 80 字符/1s 一路压到 6 字符/0.05s（约 20Hz）——
# 文本按大块跳变是「一顿顿的」的直接来源。**真正的瓶颈不只在节流**：WS 投递节奏
# （app.py `ws_events` 轮询间隔）与前端 flush 窗口都要同步压密，只降节流=白降。
# 写入开销实测 2.7ms/次（synchronous=FULL），20Hz ≈ 5% 线程占用，可接受；每条携带
# 累计全文，故 run() 正常收尾后调 prune_chat_thread_deltas 清剪。
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
# 直接结束会话；达到上限后沿用现有 fallback 文案。
_EMPTY_RESPONSE_RETRIES = 3
_EMPTY_RESPONSE_NUDGE = (
    "上一条响应没有产生文本或工具调用。请继续处理当前请求，必须返回可见文本回复，"
    "或返回需要执行的工具调用；不要返回空响应。"
)
_CONTINUE_NUDGE = ("（上一条回复因输出长度上限被截断）请**接着上一句继续写完**，"
                   "不要重复已写内容，也不要重新开头。")

# 主控一批 tool_calls 中允许同时运行的子专家数。超过上限的调用会在
# executor 中排队，避免模型一次生成大量 call_expert 时无限创建线程。
# 2026-10-06 由 4 提到 8：主控常一次并发 5+ 个 call_expert，4 会让多出的排队
# 串行（实测「先跑 4 个再跑第 5 个」）；8 给常见批量留出余量，仍防无限建线程。
_MAX_PARALLEL_EXPERTS = 8

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


def _classify_error(e: BaseException) -> dict[str, str]:
    """异常 → 结构化错误（category/title/message/hint）。message 为原始异常
    文案（技术细节），title/hint 为面向人类的分类与建议。"""
    msg = str(e)
    low = msg.lower()
    status = getattr(e, "status", 0) or 0
    if getattr(e, "truncated", False) or "工具参数流截断" in msg:
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
    """子专家工具白名单：专家 yaml tools 字段优先；缺省=全量裁剪。"""
    expert = load_expert(packs_root, agent_id, track)
    tools = expert.get("tools")
    if isinstance(tools, list) and tools:
        return [t for t in tools if t not in _EXPERT_EXCLUDED]
    return None


class ChatTurn:
    """一轮对话的执行器（一个线程实例只跑一轮；消息历史自 store 装载）。"""

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
                 decompiler_factory=None):
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
        self.thread = chat_store.get_thread(bb, thread_id) or {}
        self.agent_id = str(self.thread.get("agent_id") or ORCHESTRATOR_ID)
        self.is_orchestrator = self.agent_id == ORCHESTRATOR_ID
        self._dispatcher = self._build_dispatcher()

    def _aborted(self) -> bool:
        return self.abort_event.is_set()

    # ---------- 装配 ----------

    def _open_intents_of_self(self) -> list[str]:
        """本线程（author=chat-<threadid>）的 open 意图 id 列表（收尾前拦截用）。"""
        author = f"chat-{self.thread_id[-12:]}"
        rows = self.bb.conn.execute(
            "SELECT id FROM intents WHERE project_id=? AND status='open'"
            " AND author=? ORDER BY created_at",
            (self.project_id, author)).fetchall()
        return [r["id"] for r in rows]

    def _build_dispatcher(self) -> ToolDispatcher | None:
        """复用 ToolDispatcher 的黑板/技能/网关工具处理器（run_cmd/http/
        kb/skill/bb_* 全套安全机制免费拿）；allowed_tools 按线程角色收敛。"""
        try:
            if self.is_orchestrator:
                allowed = [t for t in _ORCH_BASE_TOOLS if t != "todo_write"
                           and t != "call_expert"]
            else:
                names = expert_tool_names(self.packs_root, self.agent_id,
                                          self.track)
                if names is None:
                    # None=未配 tools 字段，契约语义是「全量裁剪」（与 _tool_specs
                    # 同一处理）——8cb819d 首版曾把 None or [] 当空白名单降级为
                    # 无工具面，专家线程所有工具报「不可用：本线程未装配工具面」
                    names = [s["name"] for s in AGENT_TOOLS
                             if s["name"] not in _EXPERT_EXCLUDED]
                allowed = names
            allowed = [t for t in allowed
                       if t not in ("todo_write", "call_expert")]
            if not allowed:
                return None
            expert = load_expert(self.packs_root, self.agent_id, self.track) \
                if not self.is_orchestrator else {}
        except Exception:  # noqa: BLE001 —— 白名单/专家 yaml 读取失败降级无工具面
            log.exception("chat 工具面白名单装配失败 thread=%s", self.thread_id)
            return None
        try:
            dispatcher = ToolDispatcher(
                self.bb, ExecutionGateway(bb=self.bb),
                project_id=self.project_id,
                session_id=f"chat-{self.thread_id[-12:]}",
                author=f"chat-{self.thread_id[-12:]}",
                packs_root=self.packs_root, track=self.track,
                capabilities=self.capabilities,
                allowed_tools=allowed,
                abort_event=self.abort_event,
                artifacts_dir=self.artifacts_dir,
                role_skills=expert.get("skills"))
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
        return dispatcher

    def _tool_specs(self) -> list[dict[str, Any]]:
        if self.is_orchestrator:
            names = {t for t in _ORCH_BASE_TOOLS if t not in
                     ("todo_write", "call_expert")}
            specs = _specs_by_names(names)
            specs = [*self._orch_tool_specs(), *specs]
        else:
            names = set(expert_tool_names(self.packs_root, self.agent_id,
                                          self.track)
                        or [s["name"] for s in AGENT_TOOLS
                            if s["name"] not in _EXPERT_EXCLUDED])
            specs = _specs_by_names(names)
        if self.mcp_bridge is not None:
            specs = [*specs, *self.mcp_bridge.tool_specs()]
        return specs

    def _orch_tool_specs(self) -> list[dict[str, Any]]:
        """主控专属工具规格：todo_write + call_expert（专家清单动态生成）。"""
        expert_rows = []
        for eid in self._delegatable_experts():
            try:
                e = load_expert(self.packs_root, eid, self.track)
            except FileNotFoundError:
                continue
            expert_rows.append(f"- {eid}：{e.get('name', eid)}——"
                               f"{e.get('description', '')}")
        return [
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
                               "专家的事。（expert 只填下方专家 id，勿填技能名）"
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
                    },
                    "required": ["expert", "task"],
                },
            },
        ]

    def _delegatable_experts(self) -> list[str]:
        """可委派专家 id（唯一口径）：按轨全池，排除主控自身与兜底角色。
        工具描述、expert enum、报错文案三处共用，防止口径漂移。"""
        return [n for n in self.expert_names
                if n not in (ORCHESTRATOR_ID, "_generalist")]

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
            parts.append(
                "## 工作方式\n"
                "1. 接到测试意图 → todo_write 拆待办（每项一条，状态实时更新）；\n"
                "2. 将相互独立的待办通过多个 call_expert 同轮并发委派；有前后依赖的待办按顺序委派，任务描述必须自包含；\n"
                "3. 子专家摘要回来后推进下一项；全部完成后向人类汇总署名输出。\n"
                "委派前可用 kb_search/kb_open 查知识库确认打法方向、bb_query 了解"
                "黑板已有资产/发现；资产清理与合并可直接使用受控的"
                "bb_delete_asset/bb_merge_assets（必须先查询，合并必须给出 reason）；"
                "不直接执行扫描/利用等实操。")
        else:
            parts.append(
                "## 工作方式\n"
                "你是被主控委派执行具体任务的专家：按任务描述执行，需要方法细节时"
                "kb_search/kb_open 查知识库、skill_open 打开技能手册；过程结论落"
                "黑板（bb_add_asset/bb_add_finding）；干完给一段**自包含的收尾"
                "摘要**（做了什么/关键证据/结论），主控会原样转述给人类。")
        # 技能全量描述（K8 同款渐进披露层1：name+description，正文 skill_open）
        try:
            if SkillRegistry is not None:
                reg = SkillRegistry(self.packs_root)
                reg.load()
                pack_set = set(self.capabilities) | {self.track}
                enabled = [s for s in reg.all()
                           if s.enabled and s.pack in pack_set]
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
        messages: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        def _flush_tools() -> None:
            if pending:
                messages.append({"role": "user",
                                 "content": list(pending)})
                pending.clear()

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
                pending.append({"type": "tool_result",
                                "tool_use_id": r["tool_use_id"],
                                "content": r["content"] or "(空)"})
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

    def run(self, user_text: str, refs: dict[str, list[str]] | None = None) -> str:
        self._refs = refs or {}
        chat_store.append_message(self.bb, self.thread_id, "user", user_text)
        thread = chat_store.get_thread(self.bb, self.thread_id)
        if thread is not None and not thread.get("title"):
            chat_store.update_thread(self.bb, self.thread_id,
                                     title=user_text.strip()[:40])
        # 新轮开始：置 running 并清空上一轮的错误（error={} 为 falsy → 落 ''）
        chat_store.update_thread(self.bb, self.thread_id, status="running",
                                 error={})
        # 上下文用量基底（跨轮累计 output/steps；input=最近一步占用）
        self._usage: dict[str, int] = dict(thread.get("usage") or {}) if thread else {}
        self._emit("chat.message", {"role": "user", "text": user_text[:2000],
                                    "message_id": None})
        try:
            final = self._loop(user_text)
            chat_store.update_thread(self.bb, self.thread_id, status="idle")
            # 正常收尾：清剪本轮流式 delta（终稿全文已在 chat_messages，事件只承担
            # 实时可见）——不删则每条携带累计全文的 delta 行持续膨胀事件表。异常/
            # 中断路径不调用（残留 delta = 被中断思考的现场审计）。
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

    def _loop(self, user_text: str) -> str:
        system = self._system_prompt()
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
        final = ""
        # 截断续写状态（2026-10-01）：continue_n=已续写次数；text_acc=逐段文本拼接
        continue_n = 0
        empty_response_n = 0
        text_acc: list[str] = []
        for _step in range(max_steps):
            if self._aborted():  # 步间中止：不落新 LLM 调用
                final = self._persist_stopped()
                break
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
            resp = self._chat(messages, system, step_tools, on_text,
                              on_thinking=on_thinking,
                              on_retry=_reset_delta,
                              on_provider_retry=_provider_retry)
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
                "output": self._usage.get("output", 0) + u.output_tokens,
                "steps": self._usage.get("steps", 0) + 1,
                "cache_read": u.cache_read_tokens,
                "cache_creation": u.cache_creation_tokens,
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
                    messages.append({"role": "user", "content": _EMPTY_RESPONSE_NUDGE})
                    continue
                final = "（本轮未产出文本回复，请重试或改述）"
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
                parallel_results = self._parallel_expert_dispatch(resp.tool_calls)
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
                        result = "[错误] 已停止：工具未执行"
                        chat_store.append_message(self.bb, self.thread_id, "tool",
                                                  result, tool_use_id=tc.id)
                        tool_result_blocks.append({
                            "type": "tool_result", "tool_use_id": tc.id,
                            "content": result})
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
            # 步数耗尽但有累积文本（截断续写中途用尽）→ 用累积文本收尾并标记截断
            if text_acc and any(t.strip() for t in text_acc):
                final = "".join(text_acc)
                self._emit("chat.truncated", {
                    "phase": "giveup", "reason": "steps_exhausted",
                    "continues": continue_n, "text_head": final[:200]})
            else:
                final = "（本轮未产出文本回复，请重试或改述）"
                chat_store.append_message(self.bb, self.thread_id,
                                          "assistant", final)
        return final

    def _persist_stopped(self) -> str:
        """中止收尾：落一条可见的停止说明（消息+事件），线程状态由 run() 归位。"""
        text = "（已按人类要求停止本轮执行；可继续追问或重新发起）"
        row = chat_store.append_message(self.bb, self.thread_id,
                                        "assistant", text)
        self._emit("chat.message", {"role": "assistant", "text": text,
                                    "message_id": row["id"], "stopped": True})
        return text

    def _flush_final_delta(self, text: str, message_id: int) -> None:
        """终稿前的兜底 delta（不足节流阈值的尾部也要发，前端才能收满）。"""
        if text:
            self._emit("chat.delta", {"text": text, "seq": 10_000,
                                      "final_message_id": message_id})

    # ---------- 工具分发 ----------

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
        try:
            args = tc.arguments or {}
            if name.startswith("mcp__"):
                r = self._dispatch_mcp(name, args)
                return not r.startswith("[错误"), r
            if name == "todo_write":
                r = self._tool_todo_write(args)
                return not r.startswith("[错误"), r
            if name == "call_expert":
                r = self._tool_call_expert(args)
                return not r.startswith("[错误"), r
            if self._dispatcher is not None:
                r = self._dispatcher.dispatch(name, args)
                # agent 工具面失败文本约定（[错误] 为主，兼容 [工具异常]/[参数格式]）
                return not r.startswith(("[错误", "[工具异常", "[参数格式")), r
            return False, f"[错误] 工具 {name} 不可用：本线程未装配工具面"
        except Exception as e:  # noqa: BLE001 —— 工具异常回文本，绝不炸整轮
            log.exception("chat 工具分发异常 name=%s thread=%s",
                          name, self.thread_id)
            return False, f"[错误] 工具 {name} 执行异常: {e}"

    def _parallel_expert_dispatch(self, tool_calls) -> dict[str, tuple[bool, str]]:
        """并发执行同一批中的多个 call_expert，返回按 tool_call id 索引的结果。

        只有至少两个子专家调用时才启用；单个调用保留原有路径，其他工具也不
        参与并行，确保黑板写入工具的调用顺序和既有语义不变。ThreadPoolExecutor
        的 worker 数有上限，超出的子专家在队列中等待。

        **每个专家一完成即持久化工具结果**（不等整批）：前端 2s 轮询 messages
        即可逐个反映完成态，不再依赖会滚动出窗的 chat.tool 实时事件——5 专家轮
        里子线程刷屏数万条事件，早期 done 事件会被前端水合窗口（HYDRATE_LIMIT）
        挤掉，只显部分「执行完成」（2026-10-06 实测）。乱序落库安全：
        `_load_history` 装载时按 tool_use 顺序重排（`_sanitize_history`）。
        """
        experts = [tc for tc in tool_calls if tc.name == "call_expert"]
        # 混合批次中，非 call_expert 工具仍由主线程按原序执行；所有专家调用
        # 默认视为相互独立并发。结果稍后按原始 tool_call 顺序拼 history block，
        # 保证重放稳定。
        if len(experts) < 2:
            return {}
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
                "duration_s": round(time.perf_counter() - started, 2),
            })
            return tc.id, (ok, result)

        with ThreadPoolExecutor(
                max_workers=min(_MAX_PARALLEL_EXPERTS, len(experts)),
                thread_name_prefix="chat-expert") as executor:
            pending = [executor.submit(run_one, tc) for tc in experts]
            for future in as_completed(pending):
                call_id, result = future.result()
                results[call_id] = result
        return results

    def _dispatch_mcp(self, name: str, args: dict) -> str:
        if self.mcp_bridge is None:
            return "[错误] MCP 未启用：无可用 server"
        parts = name.split("__", 2)
        if len(parts) != 3:
            return f"[错误] 非法 MCP 工具名: {name}"
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
        if not self.is_orchestrator:
            return "[错误] call_expert 失败：只有主控能委派（spawn 深度=1）"
        if expert not in self.expert_names or expert == ORCHESTRATOR_ID:
            available = ", ".join(self._delegatable_experts())
            return (f"[错误] call_expert 失败：未知专家 '{expert}'；"
                    f"可用：{available}（这些是专家 id，不是技能名）")
        if self.llm is None:
            return "[错误] call_expert 失败：LLM 未装配"
        # spawn 持久子线程（留档），隔离上下文跑完 → 摘要回传。
        # **构造也必须包在 try 内**（2026-10-03）：子 ChatTurn 构造会读专家 yaml /
        # 装工具面，构造抛错（如 packs 缺文件）此前直接穿出把主控整轮炸掉；现
        # 回文本，主控可改派或向人类说明。
        sub_id = ""
        try:
            sub = chat_store.create_thread(
                self.bb, self.project_id, expert,
                title=task[:40], parent_thread_id=self.thread_id,
                spawned_task=task[:500])
            sub_id = sub["id"]
            self._emit("chat.spawn", {"thread_id": sub_id, "expert": expert,
                                      "task_head": task[:200]})
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
                decompiler_factory=self.decompiler_factory)
            summary = sub_turn.run(task)
        except Exception as e:  # noqa: BLE001 —— 专家失败回文本，主控可改派
            log.exception("chat 子专家线程失败 expert=%s thread=%s",
                          expert, self.thread_id)
            return (f"[错误] call_expert 专家线程 {expert} 执行失败：{e}"
                    + (f"（线程 {sub_id} 已留档）" if sub_id else ""))
        return (f"专家线程 {sub_id}（{expert}）已完成。\n"
                f"收尾摘要：\n{summary[:4000]}\n"
                f"（人类可在工作台打开该线程查看全过程与追问）")

    # ---------- 事件 ----------

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self.bb.append_event(
                self.project_id, kind,
                {"thread_id": self.thread_id, "agent_id": self.agent_id,
                 **payload},
                session_id=None, author=f"chat-{self.thread_id[-12:]}")
        except Exception:  # noqa: BLE001 —— 事件失败不阻断对话轮
            log.exception("chat 事件落库失败 kind=%s thread=%s",
                          kind, self.thread_id)


def new_thread_id() -> str:
    return f"chat-{uuid.uuid4().hex[:16]}"
