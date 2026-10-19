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
                  spawned_task: str = "",
                  provider: str = "", model: str = "",
                  challenge_asset_id: str = "",
                  web_search: bool = False,
                  artex_intent_id: str = "",
                  artex_task_id: str = "") -> dict[str, Any]:
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
            " parent_thread_id, spawned_task, todo, provider, model,"
            " challenge_asset_id, web_search, artex_intent_id, artex_task_id,"
            " created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, project_id, agent_id, title[:_TITLE_MAX], "idle",
             parent_thread_id, spawned_task, "[]",
             (provider or "").strip(), (model or "").strip(),
             (challenge_asset_id or "").strip(),
             1 if web_search else 0,
             (artex_intent_id or "").strip(),
             (artex_task_id or "").strip(),
             ts, ts))
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
                  error: dict[str, Any] | None = None,
                  provider: str | None = None,
                  model: str | None = None,
                  challenge_asset_id: str | None = None,
                  artex_task_id: str | None = None,
                  web_search: bool | None = None) -> dict[str, Any] | None:
    sets: list[str] = ["updated_at=?"]
    args: list[Any] = [now()]
    if title is not None:
        sets.append("title=?")
        args.append(title[:_TITLE_MAX])
    if challenge_asset_id is not None:
        # '' = 解绑（v38 线程归属题目）
        sets.append("challenge_asset_id=?")
        args.append((challenge_asset_id or "").strip())
    if artex_task_id is not None:
        # '' = 解绑（v47 主控线程绑定 ARTEX 任务：一个主控只绑一个任务）
        sets.append("artex_task_id=?")
        args.append((artex_task_id or "").strip())
    if web_search is not None:
        # v43 联网搜索开关（线程级；主控开=spawn 子代理默认继承）
        sets.append("web_search=?")
        args.append(1 if web_search else 0)
    if provider is not None:
        # '' = 清除覆写（回跟随全局默认）；线程级模型覆写（v37）
        sets.append("provider=?")
        args.append((provider or "").strip())
    if model is not None:
        sets.append("model=?")
        args.append((model or "").strip())
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


def resolve_challenge_asset_id(bb, thread_id: str, *, max_depth: int = 16) -> str:
    """本线程（或其最近的祖先线程）绑定的题目资产 id（v38，challenge-asset）。

    CTF 轨「一个主控线程只发一道题」——主控线程声明题目时把 id 落自己行上；
    主控 ``call_expert`` spawn 的子线程不重复声明，沿 ``parent_thread_id`` 向上
    找最近一个非空值（子专家与主控同题）。链上全空/线程不存在返回 ``''``。
    """
    walker = thread_id
    seen: set[str] = set()
    for _ in range(max_depth):
        if not walker or walker in seen:
            return ""
        seen.add(walker)
        row = bb.conn.execute(
            "SELECT challenge_asset_id, parent_thread_id FROM chat_threads"
            " WHERE id=?", (walker,)).fetchone()
        if row is None:
            return ""
        cid = str(row["challenge_asset_id"] or "").strip()
        if cid:
            return cid
        walker = str(row["parent_thread_id"] or "").strip()
    return ""


def recover_running_threads(bb, running: set[str] | None = None, *,
                            note: str = "（⚠ 进程重启，本轮执行中断；可继续追问或重新发起）",
                            skip_ids: set[str] | None = None) -> list[str]:
    """僵尸 running 清扫：执行轮次已消失（进程重启 / 线程被硬杀），DB status=running
    残留 → 工作台永久「执行中」（输入框禁用、停止 409）。归位 idle + 落中断 assistant
    消息（与手动停止同款观感，历史消息保留可续聊）。

    - ``running``：本进程仍在执行的 thread_id 集合（app.state.chat_running）——清扫
      前逐线程核对，集合内的活轮跳过不杀，杜绝「执行中刷新页面→活轮被误判僵尸」。
    - ``skip_ids``：额外跳过集合（如已在本轮处理过的 tid），用于在线对账分批调用。
    - ``note``：落库的中断说明文案（重启 vs 运行期异常各有措辞）。

    2026-10-09：归位前先补悬空 tool_calls 的占位结果（`_repair_dangling_tool_calls`）
    ——光把 status 归 idle 不够，被打断的轮次里 assistant 已落 tool_calls 但结果行
    没落，前端 `done=!!tr` 便永久显示「运行中」。先补结果再落中断消息，回放序干净
    （tool_use → tool_result → 中断 assistant）。"""
    rows = bb.conn.execute(
        "SELECT id FROM chat_threads WHERE status='running'").fetchall()
    ids = [r["id"] for r in rows
           if not (running and r["id"] in running)
           and not (skip_ids and r["id"] in skip_ids)]
    # 先补结果行：覆盖陈旧（已 idle 但悬空）与本次僵尸两类线程，幂等
    _repair_dangling_tool_calls(bb, running)
    if not ids:
        return []
    ts = now()
    with bb._tx():
        for tid in ids:
            bb.conn.execute(
                "INSERT INTO chat_messages(thread_id, role, content, tool_calls,"
                " tool_use_id, created_at) VALUES(?,?,?,?,?,?)",
                (tid, "assistant", note, "[]", "", ts))
            bb.conn.execute(
                "UPDATE chat_threads SET status='idle', updated_at=? WHERE id=?",
                (ts, tid))
    return ids


def _repair_dangling_tool_calls(bb, running: set[str] | None = None) -> int:
    """补齐悬空 tool_calls 的占位结果（重启中断遗留，2026-10-09）。

    进程重启会打断执行中的轮次：assistant 行已落 tool_calls，对应的 tool 结果行
    却没来得及落库（工具结果全文写 chat_messages 是最后一步，尤其并行专家 worker
    在子线程里跑，进程一死全灭）。此前只有本模块的僵尸清扫把 status 归位 idle，
    **没补结果行**——前端 `buildWbItems` 判定 `done = !!tr`（存在同 id 的 tool 结果
    行才算完成），悬空调用便永久显示「运行中」，即便线程早已 idle（用户实测：主控
    并发两个 call_expert 卡「运行中」）。`runtime._sanitize_history` 的回放兜底只修
    内存不改审计表，故必须在落库层补齐。

    与中止路径（runtime `_loop`）同口径：未执行的 tool_call 补 `[错误] …` 占位结果，
    否则下轮回放会出现 tool_use 无配对 result。

    跳过 `running`（本进程仍在执行的活轮）——悬空是「正在执行」的中间态，不是僵尸。
    单事务、幂等（已有同 id 结果的不动）。返回补齐条数。"""
    rows = bb.conn.execute(
        "SELECT DISTINCT thread_id FROM chat_messages WHERE role='assistant'"
        " AND tool_calls NOT IN ('', '[]')").fetchall()
    ts = now()
    fixed = 0
    with bb._tx():
        for r in rows:
            tid = r["thread_id"]
            if running and tid in running:
                continue
            msgs = bb.conn.execute(
                "SELECT role, tool_calls, tool_use_id FROM chat_messages"
                " WHERE thread_id=? ORDER BY id", (tid,)).fetchall()
            answered = {m["tool_use_id"] for m in msgs if m["role"] == "tool"}
            for m in msgs:
                if m["role"] != "assistant":
                    continue
                for tc in (_loads(m["tool_calls"], []) or []):
                    tcid = str((tc or {}).get("id") or "").strip()
                    if tcid and tcid not in answered:
                        bb.conn.execute(
                            "INSERT INTO chat_messages(thread_id, role, content,"
                            " tool_calls, tool_use_id, created_at)"
                            " VALUES(?,?,?,?,?,?)",
                            (tid, "tool",
                             "[错误] 进程重启，本轮工具执行中断（未完成）",
                             "[]", tcid, ts))
                        answered.add(tcid)
                        fixed += 1
    return fixed


def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    out["todo"] = _loads(out.get("todo"), [])
    out["usage"] = _loads(out.get("usage"), {})
    out["error"] = _loads(out.get("error"), None)
    # v43 联网搜索开关：DB 整数 0/1 → bool（前端 toggle 直接绑）
    out["web_search"] = bool(out.get("web_search"))
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
