"""诊断：会话系统提示词快照（diagnostics-system-prompt，2026-10-16）。

动因：system prompt 此前只活在内存里（构建 → 下发 LLM → 丢弃），诊断页看不到。
排查「红线/规则到底进没进上下文」时只能靠 token 反推（breakdown.system）或模型
自述——而模型**无法可靠内省自己的系统提示**，问它「你提示里有什么」会得到先验
答案而非实际内容。本模块把每会话**最近一次实际下发的完整 system 正文**落一份快照，
供诊断页直接查看。

存储：``session_system_prompts``（v39，session_id 主键，UPSERT 只留最新一份）。
之所以不复用 events 表：events 已 25 万行量级，按 kind 检索 2s+；且系统提示每轮
都变，落事件会线性膨胀。快照语义（一会话一行）才是这个功能的正确形状。

**best-effort 红线**：落库绝不影响对话/任务轮——任何异常只 log 不抛。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from core.blackboard.store import now

log = logging.getLogger(__name__)


def _flatten(system: Any) -> str:
    """system → 单一文本。

    chat 轮传 str；任务轮传 blocks 列表（``[{"type":"text","text":…}]``，见
    ``AgentSession.build_system_blocks``）。列表按序拼接，块间空行分隔。
    """
    if isinstance(system, str):
        return system
    if isinstance(system, (list, tuple)):
        parts: list[str] = []
        for b in system:
            if isinstance(b, dict):
                t = b.get("text")
                if t:
                    parts.append(str(t))
            elif isinstance(b, str):
                parts.append(b)
        return "\n\n".join(parts)
    return str(system or "")


def _count_tokens(text: str, model: str | None) -> int:
    """折算 token 数（tokenizer 返回等价字符数，除 2 还原）。失败退 0。"""
    try:
        from core.llm.tokenizer import get_counter
        return get_counter(model or "").count_text(text) // 2
    except Exception:  # noqa: BLE001 —— 计数失败不影响快照本体
        return 0


def save_system_prompt(bb, *, project_id: str, session_id: str, kind: str,
                       system: Any, agent_id: str = "",
                       model: str | None = None) -> None:
    """落库最近一次系统提示快照（UPSERT，一会话一行）。

    ``session_id``：chat 线程用 ``chat-<tid>``；任务链会话用 ``sess-<id>``。
    ``kind``：``chat`` / ``agent``。空正文直接跳过（不写空快照）。
    任何异常静默吞掉——诊断快照炸掉对话轮是不可接受的。
    """
    text = _flatten(system)
    if not text:
        return
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with bb._tx():
            bb.conn.execute(
                "INSERT INTO session_system_prompts"
                "(session_id,project_id,kind,agent_id,text,chars,tokens,sha256,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " project_id=excluded.project_id, kind=excluded.kind,"
                " agent_id=excluded.agent_id, text=excluded.text,"
                " chars=excluded.chars, tokens=excluded.tokens,"
                " sha256=excluded.sha256, updated_at=excluded.updated_at",
                (session_id, project_id or "", kind or "", agent_id or "",
                 text, len(text), _count_tokens(text, model), digest, now()))
    except Exception:  # noqa: BLE001 —— best-effort，绝不阻断主流程
        log.exception("系统提示快照落库失败 session=%s kind=%s", session_id, kind)


def get_system_prompt(bb, session_id: str) -> dict[str, Any] | None:
    """读会话系统提示快照；无记录返回 None（读失败也返 None，不抛）。"""
    try:
        row = bb.conn.execute(
            "SELECT session_id,project_id,kind,agent_id,text,chars,tokens,"
            "sha256,updated_at FROM session_system_prompts WHERE session_id=?",
            (session_id,)).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("系统提示快照读取失败 session=%s", session_id)
        return None
    if row is None:
        return None
    return {
        "session_id": row["session_id"],
        "project_id": row["project_id"],
        "kind": row["kind"],
        "agent_id": row["agent_id"],
        "text": row["text"],
        "chars": int(row["chars"] or 0),
        "tokens": int(row["tokens"] or 0),
        "sha256": row["sha256"],
        "updated_at": row["updated_at"],
    }
