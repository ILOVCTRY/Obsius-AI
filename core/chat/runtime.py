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

_TOOL_RESULT_HEAD = 400


def _est_tokens(text: str) -> int:
    """粗估 token 数（中英混合 ~3 字符/token）。仅用于上下文构成分解的比例
    估算，不做精确计数——真值由 LLM usage 归一校准（见 _loop 的 breakdown）。"""
    return max(1, len(text) // 3) if text else 0


def _msg_text(m: dict[str, Any]) -> str:
    """提取消息可估文本：字符串直取；assistant blocks 取 text+tool_use 入参。"""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out: list[str] = []
        for b in c:
            if isinstance(b, dict):
                out.append(str(b.get("text") or ""))
                if b.get("input"):
                    out.append(json.dumps(b["input"], ensure_ascii=False))
        return "".join(out)
    return ""


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
                 rule_profiles: dict[str, Any] | None = None):
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
            return ToolDispatcher(
                self.bb, ExecutionGateway(bb=self.bb), TaskQueue(self.bb),
                project_id=self.project_id,
                session_id=f"chat-{self.thread_id[-12:]}",
                author=f"chat-{self.thread_id[-12:]}",
                packs_root=self.packs_root, track=self.track,
                capabilities=self.capabilities,
                allowed_tools=allowed,
                max_runtime=expert.get("max_runtime"),
                abort_event=self.abort_event)
        except Exception:  # noqa: BLE001 —— 装配失败降级为无黑板工具
            log.exception("chat ToolDispatcher 装配失败 thread=%s", self.thread_id)
            return None

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
        for r in rows:
            if r["role"] == "user":
                messages.append({"role": "user", "content": r["content"]})
            elif r["role"] == "assistant":
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
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result",
                                 "tool_use_id": r["tool_use_id"],
                                 "content": r["content"] or "(空)"}],
                })
        return messages

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

            # 上下文构成估算（每步重估：messages 随 tool_result 增长）；
            # 真值归一：块比例来自字符估算，总和强制等于 LLM 报告的窗口占用
            sys_len = len(system) - len(refs_tail)
            est_system = max(1, sys_len // 3) if sys_len else 0
            est_refs = _est_tokens(refs_tail)
            est_tools = _est_tokens(json.dumps(tools, ensure_ascii=False))
            est_msgs = sum(_est_tokens(_msg_text(m)) for m in messages)
            resp = self.llm.chat(messages, system=system, tools=tools or None,
                                 on_text=on_text,
                                 should_cancel=self._aborted)
            # 上下文用量：input(+cache)=当步窗口占用，output/steps 跨步累计
            u = resp.usage
            input_total = u.input_tokens + u.cache_read_tokens \
                + u.cache_creation_tokens
            scale = input_total / max(1, est_system + est_refs
                                      + est_tools + est_msgs)
            b_system = round(est_system * scale)
            b_refs = round(est_refs * scale)
            b_tools = round(est_tools * scale)
            self._usage = {
                "input": input_total,
                "output": self._usage.get("output", 0) + u.output_tokens,
                "steps": self._usage.get("steps", 0) + 1,
                "cache_read": u.cache_read_tokens,
                "cache_creation": u.cache_creation_tokens,
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
                    chat_store.append_message(
                        self.bb, self.thread_id, "tool", result,
                        tool_use_id=tc.id)
                    messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": tc.id,
                         "content": result}]})
                    self._emit("chat.tool", {
                        "phase": "done",
                        "name": tc.name, "args_head": json.dumps(
                            tc.arguments, ensure_ascii=False,
                            separators=(",", ":"))[:300],
                        "result_head": result[:_TOOL_RESULT_HEAD],
                        "ok": ok,
                        "duration_s": round(time.perf_counter() - t0, 2)})
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
            rule_profiles=self.rule_profiles)
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
