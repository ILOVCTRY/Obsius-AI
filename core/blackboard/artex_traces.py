"""Worker 间过程级信息交换（artex-traces，2026-10-19）。

上游核心机制：Worker 执行过程中的观察（报错、隐藏参数、响应片段）未必被写成
正式 fact/finding，后续 Worker 可通过搜索复用这些过程级信息——避免重复劳动、
让链路上的 Worker 站在彼此肩膀上。

实现利用已有 chat_messages 表（assistant/tool 消息即执行步骤），不新建表。
查询路径：chat_threads(artex_intent_id/task_id) → chat_messages(thread_id)。
"""
from __future__ import annotations

import json
from typing import Any


def _json_loads(raw: str) -> Any:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def _step_summary(msg: dict[str, Any]) -> dict[str, Any]:
    """把 chat_messages 的一行转成 trace step 摘要。"""
    role = msg.get("role") or ""
    if role == "assistant":
        tool_calls = _json_loads(msg.get("tool_calls") or "[]")
        if not isinstance(tool_calls, list):
            tool_calls = []
        names = [tc.get("name") or tc.get("function", {}).get("name") or "?"
                 for tc in tool_calls if isinstance(tc, dict)]
        content = str(msg.get("content") or "")[:200]
        return {
            "step_id": msg["id"],
            "kind": "tool_call",
            "tool": ",".join(names) if names else "think",
            "summary": content[:100],
            "is_error": False,
        }
    if role == "tool":
        content = str(msg.get("content") or "")[:200]
        return {
            "step_id": msg["id"],
            "kind": "tool_result",
            "tool": "",
            "summary": content[:100],
            "is_error": "[错误]" in content or "error" in content.lower()[:100],
        }
    return {
        "step_id": msg["id"],
        "kind": role,
        "tool": "",
        "summary": str(msg.get("content") or "")[:100],
        "is_error": False,
    }


def list_worker_traces(bb, project_id: str, task_id: str, *,
                       q: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """列出本任务里【已跑过】的 work（意图）索引。

    返回：intent_id + objective + status + thread_id + 步数，供 Worker 发现
    有哪些 work 值得翻看——再用 get_worker_trace 看步骤详情。
    只列 status 不是 pending 的（还没跑过的没有过程可 inspect）。
    """
    task_id = str(task_id or "").strip()
    limit = max(1, min(int(limit), 200))
    rows = bb.conn.execute(
        "SELECT id, title, spawned_task, status, artex_intent_id, artex_task_id FROM chat_threads "
        "WHERE project_id=? AND artex_task_id=? AND artex_intent_id!='' "
        "ORDER BY updated_at DESC LIMIT ?",
        (project_id, task_id, limit)).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        intent_id = row["artex_intent_id"]
        # 去重：同一 intent 可能对应多个线程（重试时新建线程）
        if any(r["intent_id"] == intent_id for r in results):
            continue
        # 统计该线程的步数
        cnt = bb.conn.execute(
            "SELECT COUNT(*) AS c FROM chat_messages WHERE thread_id=?",
            (row["id"],)).fetchone()
        results.append({
            "intent_id": intent_id,
            "objective": row["spawned_task"] or row["title"] or "",
            "status": row["status"] or "idle",
            "thread_id": row["id"],
            "steps": int(cnt["c"]) if cnt is not None else 0,
        })
    return results


def get_worker_trace(bb, project_id: str, intent_id: str, *,
                     q: str = "", step_ids: list[int] | None = None,
                     limit: int = 100) -> dict[str, Any]:
    """查看某条意图(work)的【执行过程】。

    三种用法：
    ① 只传 intent_id → 返回每一步的摘要流（summary≤100字，含 step_id）；
    ② intent_id + q → 只返回命中关键字的步骤摘要；
    ③ intent_id + step_ids → 返回这些步骤的完整内容(detail)；一次最多取 5 个。

    不含 user 角色消息（系统提示/指令），只取 assistant/tool（即实际执行动作）。
    """
    intent_id = str(intent_id or "").strip()
    if not intent_id:
        raise ValueError("intent_id 必填")
    row = bb.conn.execute(
        "SELECT id FROM chat_threads WHERE project_id=? AND artex_intent_id=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (project_id, intent_id)).fetchone()
    if row is None:
        return {"intent_id": intent_id, "steps": [],
                "notice": "该 Intent 尚无执行过程记录（可能还未跑过或线程已清理）。"}
    thread_id = row["id"]

    # ③ detail drill-down by step ids
    if step_ids:
        max_step = 5
        ids: list[int] = []
        for x in step_ids:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in ids:
                ids.append(n)
        omitted = ids[max_step:]
        ids = ids[:max_step]
        if not ids:
            return {"intent_id": intent_id, "steps": [],
                    "notice": "step_ids 无效或为空。"}
        msgs = bb.conn.execute(
            "SELECT id, role, content, tool_calls, tool_use_id FROM chat_messages "
            "WHERE thread_id=? AND id IN (" + ",".join("?" * len(ids)) + ")"
            " ORDER BY id",
            (thread_id, *ids)).fetchall()
        steps = []
        for m in msgs:
            content = str(m["content"] or "")
            steps.append({
                "step_id": m["id"],
                "kind": m["role"],
                "tool": "",
                "detail": content,
                "is_error": "[错误]" in content[:200],
            })
        result: dict[str, Any] = {
            "intent_id": intent_id, "steps": steps,
            "returned_step_ids": ids,
        }
        if omitted:
            result["omitted_step_ids"] = omitted
            result["notice"] = (
                f"每次最多取 {max_step} 个步骤的完整内容，"
                f"本次已返回前 {len(ids)} 个，未取的 {len(omitted)} 个为 {omitted}。"
                "若这些内容已足够定位则无需再取；确需继续时用这些 step_id 再调一次。")
        return result

    # ①/② summary stream, optionally keyword-filtered
    q = str(q or "").strip()
    if q:
        like = f"%{q}%"
        msgs = bb.conn.execute(
            "SELECT id, role, content, tool_calls, tool_use_id FROM chat_messages "
            "WHERE thread_id=? AND (content LIKE ? OR tool_calls LIKE ?) "
            " AND role IN ('assistant','tool')"
            " ORDER BY id LIMIT ?",
            (thread_id, like, like, limit)).fetchall()
    else:
        msgs = bb.conn.execute(
            "SELECT id, role, content, tool_calls, tool_use_id FROM chat_messages "
            "WHERE thread_id=? AND role IN ('assistant','tool')"
            " ORDER BY id LIMIT ?",
            (thread_id, limit)).fetchall()
    return {
        "intent_id": intent_id,
        "steps": [_step_summary(dict(m)) for m in msgs],
        "query": q if q else None,
    }


def search_all_worker_traces(bb, project_id: str, task_id: str, q: str, *,
                             exclude_intent_id: str = "",
                             limit: int = 100) -> dict[str, Any]:
    """在【本任务其他 work 的执行过程】里按关键字检索。

    用于找回某个 worker 见过、却没写进 fact 的东西（某路径/token/报错等）。
    已自动排除调用者自身这条意图的步骤（worker 的自有 trace 已在其上下文里）。
    只返回命中步骤的摘要(summary≤100字)，每条带 intent_id；据此再用
    get_worker_trace(intent_id, step_ids=[...]) 取完整内容。
    """
    task_id = str(task_id or "").strip()
    q = str(q or "").strip()
    if not q:
        raise ValueError("q 必填")
    limit = max(1, min(int(limit), 200))
    like = f"%{q}%"

    # 找出本任务下所有相关线程（排除调用者自身）
    sql = (
        "SELECT id, artex_intent_id FROM chat_threads "
        "WHERE project_id=? AND artex_task_id=? AND artex_intent_id!=''")
    params: list[Any] = [project_id, task_id]
    exclude = str(exclude_intent_id or "").strip()
    if exclude:
        sql += " AND artex_intent_id != ?"
        params.append(exclude)
    thread_rows = bb.conn.execute(sql, params).fetchall()

    hits: list[dict[str, Any]] = []
    for trow in thread_rows:
        tid = trow["id"]
        iid = trow["artex_intent_id"]
        msgs = bb.conn.execute(
            "SELECT id, role, content, tool_calls FROM chat_messages "
            "WHERE thread_id=? AND role IN ('assistant','tool') "
            " AND (content LIKE ? OR tool_calls LIKE ?)"
            " ORDER BY id LIMIT ?",
            (tid, like, like, limit)).fetchall()
        for m in msgs:
            summary = _step_summary(dict(m))
            hits.append({
                "intent_id": iid,
                "step_id": summary["step_id"],
                "kind": summary["kind"],
                "tool": summary["tool"],
                "summary": summary["summary"],
                "is_error": summary["is_error"],
            })
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break

    return {"query": q, "hits": hits[:limit]}
