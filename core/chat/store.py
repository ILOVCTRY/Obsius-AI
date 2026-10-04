"""智能体工作台对话线程（K9，2026-09-29）：新独立轻量对话运行时的持久化层。

不进 sessions/tasks 体系（与渗透会话窗完全解耦）。每 agent（主控
chat-orchestrator + 专家池专家）独立线程；主控 call_expert spawn 的子专家
线程经 parent_thread_id 留档关联。仿 intents.py：模块函数经 ``bb._tx()``
写（黑板单一写入口），读走 ``bb.conn``。线程删除时消息级联物理删除。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.blackboard.store import _loads, new_id, now

THREAD_STATUSES = ("idle", "running", "error")
TODO_STATUSES = ("pending", "in_progress", "completed")
_TITLE_MAX = 60


# ---------- 线程 ----------

def create_thread(bb, project_id: str, agent_id: str, *,
                  title: str = "", parent_thread_id: str | None = None,
                  spawned_task: str = "") -> dict[str, Any]:
    tid = new_id("chat")
    ts = now()
    with bb._tx():
        if parent_thread_id is not None:
            row = bb.conn.execute(
                "SELECT 1 FROM chat_threads WHERE id=? AND project_id=?",
                (parent_thread_id, project_id)).fetchone()
            if row is None:
                raise ValueError(f"父线程不存在: {parent_thread_id}")
        bb.conn.execute(
            "INSERT INTO chat_threads(id, project_id, agent_id, title, status,"
            " parent_thread_id, spawned_task, todo, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, project_id, agent_id, title[:_TITLE_MAX], "idle",
             parent_thread_id, spawned_task, "[]", ts, ts))
    return get_thread(bb, tid)


def get_thread(bb, thread_id: str) -> dict[str, Any] | None:
    row = bb.conn.execute(
        "SELECT * FROM chat_threads WHERE id=?", (thread_id,)).fetchone()
    return _row(row) if row is not None else None


def list_threads(bb, project_id: str, agent_id: str | None = None) -> list[dict[str, Any]]:
    if agent_id:
        rows = bb.conn.execute(
            "SELECT * FROM chat_threads WHERE project_id=? AND agent_id=?"
            " ORDER BY updated_at DESC, id DESC",
            (project_id, agent_id)).fetchall()
    else:
        rows = bb.conn.execute(
            "SELECT * FROM chat_threads WHERE project_id=?"
            " ORDER BY updated_at DESC, id DESC", (project_id,)).fetchall()
    return [_row(r) for r in rows]


def update_thread(bb, thread_id: str, *, title: str | None = None,
                  status: str | None = None,
                  todo: list[dict] | None = None,
                  spawned_task: str | None = None,
                  usage: dict[str, Any] | None = None,
                  error: dict[str, Any] | None = None) -> dict[str, Any] | None:
    sets: list[str] = ["updated_at=?"]
    args: list[Any] = [now()]
    if title is not None:
        sets.append("title=?")
        args.append(title[:_TITLE_MAX])
    if status is not None:
        if status not in THREAD_STATUSES:
            raise ValueError(f"非法线程状态: {status}")
        sets.append("status=?")
        args.append(status)
    if todo is not None:
        sets.append("todo=?")
        args.append(json.dumps(todo, ensure_ascii=False))
    if spawned_task is not None:
        sets.append("spawned_task=?")
        args.append(spawned_task)
    if usage is not None:
        sets.append("usage=?")
        args.append(json.dumps(usage, ensure_ascii=False))
    if error is not None:
        # '' 表示清空（新轮开始）；dict 表示结构化错误（分类/友好原因/建议/技术细节）
        sets.append("error=?")
        args.append(json.dumps(error, ensure_ascii=False) if error else "")
    args.append(thread_id)
    with bb._tx():
        bb.conn.execute(
            f"UPDATE chat_threads SET {', '.join(sets)} WHERE id=?", args)
    return get_thread(bb, thread_id)


def delete_thread(bb, thread_id: str) -> bool:
    """删除线程（手动递归级联）：先 BFS 收齐全部后代线程（call_expert spawn
    链可多层），消息全清后再删线程行——chat_messages.thread_id 外键无 CASCADE
    （schema 未 ALTER），而连接开了 foreign_keys pragma，DB 级联删子线程时会被
    子消息挡路报 FOREIGN KEY constraint failed（有子线程的线程删除必 500）。"""
    with bb._tx():
        ids = [thread_id]
        frontier = [thread_id]
        while frontier:
            ph = ",".join("?" * len(frontier))
            rows = bb.conn.execute(
                f"SELECT id FROM chat_threads WHERE parent_thread_id IN ({ph})",
                frontier).fetchall()
            frontier = [r["id"] for r in rows if r["id"] not in ids]
            ids.extend(frontier)
        ph = ",".join("?" * len(ids))
        bb.conn.execute(f"DELETE FROM chat_messages WHERE thread_id IN ({ph})", ids)
        cur = bb.conn.execute(f"DELETE FROM chat_threads WHERE id IN ({ph})", ids)
        return cur.rowcount > 0


def recover_running_threads(bb, running: set[str] | None = None) -> list[str]:
    """重启后僵尸 running 清扫：执行轮次随旧进程消失（abort_event/执行线程/
    run_cmd 子进程全灭），DB status=running 残留 → 工作台永久「执行中」（输入
    框禁用、停止 409）。只在「本进程首次打开该项目」时调用（_sweep_restarted_
    project 钩子）——新进程 chat_running 为空集，扫到的 running 必是僵尸：归位
    idle + 落中断 assistant 消息（与手动停止同款观感，历史消息保留可续聊）。
    running：本进程仍在执行的 thread_id 集合（app.state.chat_running）——双保险
    （2026-09-30 误报修复）：清扫前逐线程核对，集合内的活轮跳过不杀，杜绝
    「执行中刷新页面→GET 项目→活轮被误判僵尸落『进程重启』中断消息」。无僵尸
    时 no-op 返回空。"""
    rows = bb.conn.execute(
        "SELECT id FROM chat_threads WHERE status='running'").fetchall()
    ids = [r["id"] for r in rows if not (running and r["id"] in running)]
    if not ids:
        return []
    ts = now()
    with bb._tx():
        for tid in ids:
            bb.conn.execute(
                "INSERT INTO chat_messages(thread_id, role, content, tool_calls,"
                " tool_use_id, created_at) VALUES(?,?,?,?,?,?)",
                (tid, "assistant",
                 "（⚠ 进程重启，本轮执行中断；可继续追问或重新发起）",
                 "[]", "", ts))
            bb.conn.execute(
                "UPDATE chat_threads SET status='idle', updated_at=? WHERE id=?",
                (ts, tid))
    return ids


def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    out["todo"] = _loads(out.get("todo"), [])
    out["usage"] = _loads(out.get("usage"), {})
    out["error"] = _loads(out.get("error"), None)
    return out


# ---------- 消息 ----------

def append_message(bb, thread_id: str, role: str, content: str, *,
                   tool_calls: list[dict] | None = None,
                   tool_use_id: str = "",
                   thinking: str = "") -> dict[str, Any]:
    if role not in ("user", "assistant", "tool"):
        raise ValueError(f"非法消息角色: {role}")
    ts = now()
    with bb._tx():
        cur = bb.conn.execute(
            "INSERT INTO chat_messages(thread_id, role, content, thinking,"
            " tool_calls, tool_use_id, created_at) VALUES(?,?,?,?,?,?,?)",
            (thread_id, role, content, thinking,
             json.dumps(tool_calls or [], ensure_ascii=False),
             tool_use_id, ts))
        bb.conn.execute(
            "UPDATE chat_threads SET updated_at=? WHERE id=?", (ts, thread_id))
        mid = cur.lastrowid
    return {"id": mid, "thread_id": thread_id, "role": role, "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls or [], "tool_use_id": tool_use_id,
            "created_at": ts}


def list_messages(bb, thread_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
    rows = bb.conn.execute(
        "SELECT * FROM chat_messages WHERE thread_id=? AND id>? ORDER BY id",
        (thread_id, after_id)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tool_calls"] = _loads(d.get("tool_calls"), [])
        out.append(d)
    return out


def count_messages(bb, thread_id: str) -> int:
    return int(bb.conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE thread_id=?",
        (thread_id,)).fetchone()[0])
