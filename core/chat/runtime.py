"""智能体工作台对话运行时（K9，2026-09-29）：独立轻量 agentic loop。

与 AgentLoop（会话窗/任务队列体系）完全解耦：无会话行、无任务认领、无
计划闸——每线程一个 ChatTurn 跑一轮（LLM 循环到纯文本回复为止），消息
按 API 数组序落 chat_messages（core/chat/store.py）。

分工（蛙池式主控轻专家重）：
- 主控线程（agent_id=chat-orchestrator）：todo_write / call_expert /
  skill_open / kb_search / kb_open / bb_query + mcp__*；不碰实操。
- 子专家线程（agent_id=专家池 id）：AGENT_TOOLS 全量裁剪（去掉任务队列/
  计划/意图/收尾控制原语）+ mcp__*；call_expert 不可用（spawn 深度=1）。

call_expert：spawn 持久子线程（parent_thread_id 留档）→ 隔离上下文跑完
→ 摘要+线程引用回传主控（不审批，全程事件流可见）。

流式：llm 流式回调攒 delta → 节流落 chat.delta 事件（前端按 thread_id
组装）；工具调用落 chat.tool 事件；终稿落 chat.message 事件。消息全文
以 chat_messages 表为准，事件只承担实时可见性。
"""

from __future__ import annotations

import json
import hashlib
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.agent.tools import AGENT_TOOLS, ToolDispatcher
from core.blackboard.tasks import TaskQueue
from core.chat import store as chat_store
from core.chat.mcp_bridge import MCPBridge
from core.llm.provider import ContextOverflowError
from core.runtime.gateway import ExecutionGateway
from core.skills.experts import load_expert
from core.skills.registry import SkillRegistry
from core.skills.rules import build_rules_preamble

log = logging.getLogger(__name__)

ORCHESTRATOR_ID = "chat-orchestrator"

# 子专家不可用的控制原语（任务队列/计划闸/意图纪律/收尾/私信/提案/HITL——
# 对话线程没有任务上下文与收件箱，这些工具既无语义也危险：publish/claim
# 会污染任务队列）
_EXPERT_EXCLUDED = {
    "task_plan", "task_step", "task_reconcile", "publish_task",
    "complete_task", "fail_task", "finish", "request_steps",
    "propose_pack_edit", "declare_intent", "close_intent", "reopen_intent",
    "bb_notify", "request_authorization", "request_escalation",
}

_ORCH_BASE_TOOLS = ("todo_write", "call_expert", "skill_open",
                    "kb_search", "kb_open", "bb_query")

_ORCH_MAX_STEPS = 24
_EXPERT_MAX_STEPS = 32

_DELTA_MIN_CHARS = 80
_DELTA_MIN_INTERVAL = 1.0

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

    def _build_dispatcher(self) -> ToolDispatcher | None:
        """复用 ToolDispatcher 的黑板/技能/网关工具处理器（run_cmd/http/
        kb/skill/bb_* 全套安全机制免费拿）；allowed_tools 按线程角色收敛。"""
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
        allowed = [t for t in allowed if t not in ("todo_write", "call_expert")]
        if not allowed:
            return None
        expert = load_expert(self.packs_root, self.agent_id, self.track) \
            if not self.is_orchestrator else {}
        try:
            dispatcher = ToolDispatcher(
                self.bb, ExecutionGateway(bb=self.bb), TaskQueue(self.bb),
                project_id=self.project_id,
                session_id=f"chat-{self.thread_id[-12:]}",
                author=f"chat-{self.thread_id[-12:]}",
                packs_root=self.packs_root, track=self.track,
                capabilities=self.capabilities,
                allowed_tools=allowed,
                max_runtime=expert.get("max_runtime"),
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
        for eid in self.expert_names:
            if eid in (ORCHESTRATOR_ID, "_generalist"):
                continue  # 主控自身不委派自己；_generalist 是兜底角色不进清单
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
                               "一次委派一项；任务描述必须自包含（目标/范围/已知"
                               "信息/期望交付物）。你在委派前不直接实操——实操是"
                               "专家的事。" + ("\n可用专家：\n" + "\n".join(expert_rows)
                                               if expert_rows else ""),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "expert": {"type": "string",
                                   "description": "专家 id（见可用专家清单）"},
                        "task": {"type": "string",
                                 "description": "自包含的任务描述"},
                    },
                    "required": ["expert", "task"],
                },
            },
        ]

    # ---------- 系统提示 ----------

    def _system_prompt(self) -> str:
        expert = load_expert(self.packs_root, self.agent_id, self.track)
        # 规则链放最前（与任务链 stable_parts[0] 同构）：红线+owner 叠加+评级
        # 口径——子专家 bb_add_finding 判级引用 rating:<tag> 条款，主控汇总
        # 判级同样有据；role 传 agent_id，role-rules 文件存在时自动叠加
        parts = [build_rules_preamble(
                     self.packs_root, track=self.track,
                     capabilities=self.capabilities,
                     owner_tags=self.owner_tags, role=self.agent_id,
                     rule_profiles=self.rule_profiles),
                 f"# 角色：{expert.get('name', self.agent_id)}\n{expert.get('persona') or expert.get('description') or ''}"]
        if self.is_orchestrator:
            parts.append(
                "## 工作方式\n"
                "1. 接到测试意图 → todo_write 拆待办（每项一条，状态实时更新）；\n"
                "2. 逐项 call_expert 委派专家执行（一次一项，任务描述自包含）；\n"
                "3. 子专家摘要回来后推进下一项；全部完成后向人类汇总署名输出。\n"
                "委派前可用 kb_search/kb_open 查知识库确认打法方向、bb_query 了解"
                "黑板已有资产/发现；不直接执行扫描/利用等实操。")
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
                    parts.append("## 可用技能清单（skill_open(\"<name>\") 打开全量正文）\n"
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
                    blocks.append({"type": "tool_use", "id": tc["id"],
                                   "name": tc["name"],
                                   "input": tc.get("args") or {}})
                if blocks:
                    messages.append({"role": "assistant", "content": blocks})
            elif r["role"] == "tool":
                pending.append({"type": "tool_result",
                                "tool_use_id": r["tool_use_id"],
                                "content": r["content"] or "(空)"})
        _flush_tools()
        return messages

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
              tools: list[dict[str, Any]] | None, on_text=None):
        """调 LLM（发送前已压缩）。reactive 兜底（ct-5）：若仍因 input 超限被
        网关 400/413 拒绝（ContextOverflowError），强制压缩一次后重试；再失败
        则抛出交给 run() 落 error。对应 Claude Code 五级级联的最末「reactive
        compact」——静态预算估算与实际 token 计数总有偏差，需运行时兜底。"""
        try:
            return self.llm.chat(messages, system=system, tools=tools,
                                 on_text=on_text, should_cancel=self._aborted)
        except ContextOverflowError:
            log.warning("chat 输入超限，强制压缩后重试 thread=%s", self.thread_id)
            compacted, info = self._prepare_context(messages, system,
                                                    tools or [], force=True)
            self._emit("chat.ctx", {"phase": "reactive", **info})
            return self.llm.chat(compacted, system=system, tools=tools,
                                 on_text=on_text, should_cancel=self._aborted)

    # ---------- 主流程 ----------

    def run(self, user_text: str, refs: dict[str, list[str]] | None = None) -> str:
        self._refs = refs or {}
        chat_store.append_message(self.bb, self.thread_id, "user", user_text)
        thread = chat_store.get_thread(self.bb, self.thread_id)
        if thread is not None and not thread.get("title"):
            chat_store.update_thread(self.bb, self.thread_id,
                                     title=user_text.strip()[:40])
        chat_store.update_thread(self.bb, self.thread_id, status="running")
        # 上下文用量基底（跨轮累计 output/steps；input=最近一步占用）
        self._usage: dict[str, int] = dict(thread.get("usage") or {}) if thread else {}
        self._emit("chat.message", {"role": "user", "text": user_text[:2000],
                                    "message_id": None})
        try:
            final = self._loop(user_text)
            chat_store.update_thread(self.bb, self.thread_id, status="idle")
            return final
        except Exception as e:  # noqa: BLE001 —— 失败落线程状态+事件，不静默
            log.exception("chat 轮失败 thread=%s", self.thread_id)
            chat_store.update_thread(self.bb, self.thread_id, status="error")
            self._emit("chat.message", {"role": "assistant",
                                        "text": f"[轮次失败] {e}",
                                        "message_id": None})
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
        messages = self._load_history()
        tools = self._tool_specs()
        max_steps = _ORCH_MAX_STEPS if self.is_orchestrator else _EXPERT_MAX_STEPS
        final = ""
        for _step in range(max_steps):
            if self._aborted():  # 步间中止：不落新 LLM 调用
                final = self._persist_stopped()
                break
            acc: list[str] = []
            pub = {"chars": 0, "seq": 0, "at": 0.0}

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

            # 发送前两级联动压缩（旧 tool_result 占位化 → 必要时 LLM 摘要）
            messages, ctx_info = self._prepare_context(messages, system, tools)
            if ctx_info.get("compacted"):
                self._emit("chat.ctx", {"phase": "proactive", **ctx_info})
            # 上下文构成估算（每步重估：messages 随 tool_result 增长）；
            # 真值归一：块比例来自字符估算，总和强制等于 LLM 报告的窗口占用
            sys_len = len(system) - len(refs_tail)
            est_system = max(1, sys_len // 3) if sys_len else 0
            est_refs = _est_tokens(refs_tail)
            est_tools = _est_tokens(json.dumps(tools, ensure_ascii=False))
            est_msgs = sum(_est_tokens(_msg_text(m)) for m in messages)
            resp = self._chat(messages, system, tools or None, on_text)
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
            if resp.tool_calls:
                row = chat_store.append_message(
                    self.bb, self.thread_id, "assistant", text,
                    tool_calls=[{"id": tc.id, "name": tc.name,
                                 "args": tc.arguments} for tc in resp.tool_calls])
                self._flush_final_delta(text, row["id"])
                messages.append({"role": "assistant", "content": [
                    *( [{"type": "text", "text": text}] if text else []),
                    *[{"type": "tool_use", "id": tc.id, "name": tc.name,
                       "input": tc.arguments} for tc in resp.tool_calls],
                ]})
                tool_result_blocks: list[dict[str, Any]] = []
                for tc in resp.tool_calls:
                    if self._aborted():
                        final = self._persist_stopped()
                        break
                    t0 = time.perf_counter()
                    self._emit("chat.tool", {
                        "phase": "start", "name": tc.name,
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
                    self._emit("chat.tool", {
                        "phase": "done",
                        "name": tc.name, "args_head": json.dumps(
                            tc.arguments, ensure_ascii=False,
                            separators=(",", ":"))[:300],
                        "result_head": result[:_TOOL_RESULT_EVENT_HEAD],
                        "ok": ok,
                        "artifact_path": artifact_path,
                        "duration_s": round(time.perf_counter() - t0, 2)})
                if tool_result_blocks:
                    messages.append({"role": "user",
                                     "content": tool_result_blocks})
                if final:
                    break
                continue
            # 纯文本回复 = 轮终稿
            row = chat_store.append_message(self.bb, self.thread_id,
                                            "assistant", text)
            self._flush_final_delta(text, row["id"])
            self._emit("chat.message", {"role": "assistant",
                                        "text": text[:2000],
                                        "message_id": row["id"]})
            final = text
            break
        if not final:
            final = "（本轮未产出文本回复，请重试或改述）"
            chat_store.append_message(self.bb, self.thread_id, "assistant", final)
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
        是持久化消息与前端渲染共用的单点失败判定约定。"""
        if self._aborted():
            return False, "[错误] 已停止：工具未执行"
        name, args = tc.name, tc.arguments or {}
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
            try:
                r = self._dispatcher.dispatch(name, args)
                # agent 工具面失败文本约定（[错误] 为主，兼容 [工具异常]/[参数格式]）
                return not r.startswith(("[错误", "[工具异常", "[参数格式")), r
            except Exception as e:  # noqa: BLE001 —— 工具异常回文本不断轮
                return False, f"[错误] 工具异常: {e}"
        return False, f"[错误] 工具 {name} 不可用：本线程未装配工具面"

    def _dispatch_mcp(self, name: str, args: dict) -> str:
        if self.mcp_bridge is None:
            return "[错误] MCP 未启用：无可用 server"
        parts = name.split("__", 2)
        if len(parts) != 3:
            return f"[错误] 非法 MCP 工具名: {name}"
        return self.mcp_bridge.call(parts[1], parts[2], args)

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
            return (f"[错误] call_expert 失败：未知专家 '{expert}'；"
                    f"可用：{', '.join(n for n in self.expert_names if n != ORCHESTRATOR_ID)}")
        if self.llm is None:
            return "[错误] call_expert 失败：LLM 未装配"
        # spawn 持久子线程（留档），隔离上下文跑完 → 摘要回传
        sub = chat_store.create_thread(
            self.bb, self.project_id, expert,
            title=task[:40], parent_thread_id=self.thread_id,
            spawned_task=task[:500])
        self._emit("chat.spawn", {"thread_id": sub["id"], "expert": expert,
                                  "task_head": task[:200]})
        sub_turn = ChatTurn(
            bb=self.bb, llm=self.llm, project_id=self.project_id,
            thread_id=sub["id"], packs_root=self.packs_root,
            track=self.track, capabilities=self.capabilities,
            mcp_bridge=self.mcp_bridge, expert_names=[],
            abort_event=self.abort_event,  # 停主控连带停执行中的子专家轮
            owner_tags=self.owner_tags,
            rule_profiles=self.rule_profiles,
            artifacts_dir=self.artifacts_dir,
            browser_pool=self.browser_pool,
            decompiler_factory=self.decompiler_factory)
        try:
            summary = sub_turn.run(task)
        except Exception as e:  # noqa: BLE001 —— 专家失败回文本，主控可改派
            summary = f"[专家线程执行失败：{e}]"
        return (f"专家线程 {sub['id']}（{expert}）已完成。\n"
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
