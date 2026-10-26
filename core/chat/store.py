"""智能体工作台对话线程（K9，2026-09-29）：新独立轻量对话运行时的持久化层。

不进 sessions/tasks 体系（与渗透会话窗完全解耦）。每 agent（主控
pentest-controller + 专家池专家）独立线程；主控 call_expert spawn 的子专家
线程经 parent_thread_id 留档关联。仿 intents.py：模块函数经 ``bb._tx()``
写（黑板单一写入口），读走 ``bb.conn``。线程删除时消息级联物理删除。
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from core.blackboard.store import _loads, new_id, now
# 系统注入消息判定（2026-10-24）：`list_messages` 据文案前缀派生 `origin` 字段，
# 前端据此渲染成折叠的系统行而不是人类发言。真源见 core/chat/origin.py。
from core.chat.origin import describe as origin_of

THREAD_STATUSES = ("idle", "running", "error")
TODO_STATUSES = ("pending", "in_progress", "completed")
_TITLE_MAX = 60


class ThreadGone(RuntimeError):
    """线程行已被删除（人类在工作台删掉了正在跑的线程）。

    2026-10-19 事故：`chat_messages.thread_id REFERENCES chat_threads(id)` 且
    连接开了 ``PRAGMA foreign_keys=ON``，用户在子专家跑到一半时删掉那条线程
    → 子 `ChatTurn.run()` 下一次 `append_message` 抛
    ``sqlite3.IntegrityError: FOREIGN KEY constraint failed``，一路穿成
    「专家线程执行失败：FOREIGN KEY constraint failed」回给主控，看起来像
    系统 bug 而不是「你自己把线程删了」。

    这是**人类意图**（不想要这个线程了）而不是故障：用独立异常让上层优雅
    收尾——不落错误卡、不再重试、不当成专家能力问题。
    """


def thread_exists(bb, thread_id: str) -> bool:
    """线程行是否还在（删线程后的写库前置检查，避免 FK 炸整轮）。"""
    tid = str(thread_id or "").strip()
    if not tid:
        return False
    try:
        row = bb.conn.execute(
            "SELECT 1 FROM chat_threads WHERE id=?", (tid,)).fetchone()
    except Exception:  # noqa: BLE001 —— 读失败按「在」处理，别误伤正常轮
        return True
    return row is not None



# ---------- 线程 ----------

def create_thread(bb, project_id: str, agent_id: str, *,
                  title: str = "", parent_thread_id: str | None = None,
                  spawned_task: str = "",
                  provider: str = "", model: str = "",
                  thinking_effort: str = "",
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
            " thinking_effort, challenge_asset_id, web_search, artex_intent_id,"
            " artex_task_id, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, project_id, agent_id, title[:_TITLE_MAX], "idle",
             parent_thread_id, spawned_task, "[]",
             (provider or "").strip(), (model or "").strip(),
             (thinking_effort or "").strip().lower(),
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
                  thinking_effort: str | None = None,
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
    if thinking_effort is not None:
        # '' = 清除覆写（回跟随供应商配置）；线程级思考强度（v48）
        sets.append("thinking_effort=?")
        args.append((thinking_effort or "").strip().lower())
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


def challenge_isolation_scope(bb, project_id: str, thread_id: str, *,
                              max_depth: int = 16) -> dict | None:
    """CTF 跨题隔离面（challenge-isolation，2026-10-19）：本题 vs 其它题目。

    返回 ``{"self_challenge": <本题 id 或 ''>,
             "other_roots": set[题目根 id],
             "other_assets": set[其它题目子树 id 全集],
             "other_sessions": set[chat-<tid12> 会话 id]}``；
    **没有别的题目**（或解析失败）返回 ``None`` = 不过滤，行为与改动前逐字一致。

    **动机**（用户实测）：多题共跑时 ``bb_query what=findings`` 回的是**全项目**
    发现——一道全新题刚上手，AI 顺手一查就翻到别的题的结论，上下文被污染、
    推理被带偏（把别人的打法当本题线索）。同理 ``what=assets`` 会翻到别题的
    附件清单、``what=events`` 会翻到别题跑过的命令。

    口径：**排除别的题，保留未归属条目**（不是白名单式只留本题——题目还没
    登记时那样会把正常查询全打空，把「新题上手先看黑板」这条正常流程堵死）。

    两个资产集合的用法差异：
    - ``other_assets`` 全集 → findings 用（挂在别题根上的结论同样要挡）；
    - ``other_assets - other_roots`` → assets 用（别题的**题目行**仍可见，AI
      据此知道项目里还有哪些题，不会误以为只有自己这道甚至重复建题）。

    会话面（events 查询用）：线程树归属到**别的题目**的线程，其
    ``chat-<tid12>`` 一并排除（子专家沿 parent_thread_id 继承题目）。
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return None
    try:
        # 线程行不存在（脏 thread_id / 已删线程）→ 无法判定归属，不过滤。
        # 不能把它当成「本题未绑定」——那会把所有题目的条目都排除掉，
        # 查询直接被打空（宁松勿误空）。
        exists = bb.conn.execute(
            "SELECT 1 FROM chat_threads WHERE id=? AND project_id=?",
            (tid, project_id)).fetchone()
        if exists is None:
            return None
        from core.blackboard.attackpath import _subtree_ids
        assets = bb.list_assets(project_id)
        roots = [a["id"] for a in assets
                 if a.get("type") == "challenge" and not a.get("parent_id")]
        if not roots:
            return None
        self_cid = resolve_challenge_asset_id(bb, tid, max_depth=max_depth) or ""
        other_roots: set[str] = set()
        other_assets: set[str] = set()
        for cid in roots:
            if cid == self_cid:
                continue
            other_roots.add(cid)
            other_assets |= _subtree_ids(assets, cid)
        if not other_roots:
            return None
        rows = bb.conn.execute(
            "SELECT id, parent_thread_id, challenge_asset_id FROM chat_threads"
            " WHERE project_id=?", (project_id,)).fetchall()
        by_tid = {str(r["id"]): (str(r["parent_thread_id"] or ""),
                                 str(r["challenge_asset_id"] or ""))
                  for r in rows}
        other_sessions: set[str] = set()
        for one in by_tid:
            walker, seen, cid = one, set(), ""
            for _ in range(max_depth):
                if not walker or walker in seen:
                    break
                seen.add(walker)
                parent, own = by_tid.get(walker, ("", ""))
                if own:
                    cid = own
                    break
                walker = parent
            if cid and cid != self_cid:
                other_sessions.add(f"chat-{one[-12:]}")
        return {"self_challenge": self_cid, "other_roots": other_roots,
                "other_assets": other_assets, "other_sessions": other_sessions}
    except Exception:  # noqa: BLE001 —— 隔离是减噪手段，失败按不过滤（宁松勿误空）
        return None


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
                             _dangling_placeholder(str((tc or {}).get("name") or "")),
                             "[]", tcid, ts))
                        answered.add(tcid)
                        fixed += 1
    return fixed


# 悬空 tool_call 的占位文案。**必须写清「怎么救」**（2026-10-26 事故）：
# 旧文案只说「中断（未完成）」，主控读到就以为活白干了，于是整批重派——
# 实测 4 个专家跑了 2h44m，其中两条已经 `deliver` 过完整报告、关掉全部
# intent、把资产标成 tested_clean，成果全在库里，却被从头重派一遍。
# 中断 ≠ 失败：占位行本身就是「没有结果」，而成果可能在 chat_messages 里。
#
# 前缀单独抽常量：`harvest_thread` 靠它认出「这一行是被打断的调用」（清扫
# 跑过之后 `unanswered` 恒为空，只剩这个信号）。两处各写一遍必然漂移。
_DANGLING_TOOL_PREFIX = "[错误] 本轮工具执行中断（进程重启，未完成）。"

# 识别用的**稳定子串**，不能拿 `_DANGLING_TOOL_PREFIX` 整串去匹配：
# 库里已经有一批用旧文案写的占位（「[错误] 进程重启，本轮工具执行中断（未完成）」），
# 按新前缀匹配会一条都认不出来——实测 5 条事故线程 `interrupted` 全空，
# 而它们明明都是被打断的。只认「本轮工具执行中断」这一段，新旧都覆盖。
_DANGLING_MARK = "本轮工具执行中断"

_DANGLING_TOOL_NOTE = (
    _DANGLING_TOOL_PREFIX
    + "**中断不等于失败**——该调用很可能已经跑完，成果仍在库里。"
    "重跑普通工具即可；若是 call_expert，先找到它派生的子线程"
    "（该线程的子线程，形如 chat-xxxxxxxx），用 "
    "ask_expert(thread_id=..., resume=true) 回收它的交付内容再决定要不要续跑，"
    "**不要直接重派**——那会把已经干完的活整轮白烧。"
)


def _dangling_placeholder(tool_name: str) -> str:
    """按工具类型给悬空占位文案：只有派发类工具才需要「去回收子线程」这段。"""
    if tool_name in ("call_expert", "ask_expert"):
        return _DANGLING_TOOL_NOTE
    return (_DANGLING_TOOL_PREFIX
            + "重跑一次即可——它是幂等读/写工具，没有需要回收的中间产物。")


# 交付回执的开头标记（`_tool_deliver` 生成，见 core/agent/tools.py）。
# 只认这个前缀：子线程里 role=tool 的行动辄上千条，靠前缀定位交付行比「取最后
# 一条 tool 行」稳——专家常在交付之后又跑几十步工具，最后一条通常是 run_cmd。
_DELIVERY_MARK = "[已交付 · 第 "

# 平台墓碑文案（`recover_running_threads` 的 note 与手动停止的收尾说明）。
# 回收「最后结论」时必须跳过：那不是专家说的话，是平台替它写的墓碑。
_INTERRUPT_NOTE_MARKERS = (
    "进程重启，本轮执行中断",
    "本轮执行线程已意外终止",
    "已按人类要求停止本轮执行",
)


def is_interrupt_note(text: str) -> bool:
    """这条 assistant 文本是不是平台写的中断墓碑（而非专家自己的结论）。"""
    t = (text or "").strip()
    return bool(t) and t.startswith("（") and any(
        m in t for m in _INTERRUPT_NOTE_MARKERS)


def is_interrupt_placeholder(text: str) -> bool:
    """这条 tool 结果是不是「被打断」的占位（而不是工具的真实输出）。

    兼容新旧两版文案：旧版是「[错误] 进程重启，本轮工具执行中断（未完成）」，
    新版在同样子串后面补了回收指引。判据只看 `_DANGLING_MARK`，别按整串匹配
    ——那样老库里的占位一条都认不出来。
    """
    t = (text or "")
    return t.startswith("[错误]") and _DANGLING_MARK in t[:160]


def split_delivery(content: str) -> tuple[int, str]:
    """从 `deliver` 的 tool 回执里剥出「第 N 次」与正文。

    回执结构（`_tool_deliver` 拼的）::

        [已交付 · 第 3 次] 主控已收到本次内容；...
        <空行>
        本次交付内容（原样回显，核对一下你交的是不是这个）：
        ---
        <正文，可能自带 --- 分隔线>
        ---
        [你说的下一步：...]
        <空行>
        接下来**你自己判断**：...

    **必须 `split` 再 `join(parts[1:-1])`**：取 `parts[1]` 会在正文自带
    Markdown 分隔线时截断（实测渗透/CTF 报告里普遍有 `---` 分段），而
    split+join 是无损的——正文里的分隔线被切进中间段，join 回来逐字还原。
    """
    m = re.match(r"\[已交付 · 第 (\d+) 次\]", content or "")
    n = int(m.group(1)) if m else 0
    parts = (content or "").split("\n---\n")
    if len(parts) >= 3:
        return n, "\n---\n".join(parts[1:-1]).strip()
    return n, (content or "").strip()


def harvest_thread(bb, thread_id: str) -> dict[str, Any]:
    """回收一条线程**已经产出**的成果——意外中断后的续跑依据（2026-10-26）。

    **为什么需要**（实测事故，`workspaces/pentest-20261011-c3c7`）：主控一轮
    并发 5 个 `call_expert`，进程在 2h44m 后重启，4 个专家的工具结果被
    `_repair_dangling_tool_calls` 补成「进程重启，本轮工具执行中断」。可那 4
    条线程**早就干完了**——其中两条 `deliver` 过完整报告、关掉全部 intent、
    把资产标成 `tested_clean`。成果全在 `chat_messages` 里，主控却只看到一句
    墓碑，只能从头重派（2h44m 白烧）。

    回收三样，都是「不重跑就能拿到」的事实：

    - ``delivery`` / ``delivery_n``：最后一次 `deliver` 的正文全文——专家自己
      认定要交给主控的东西，比事后猜它的结论可靠；
    - ``final_text`` / ``final_after_tools``：最后一条**实质** assistant 文本
      （跳过平台墓碑），以及它是否出现在最后一次工具调用**之后**——之后才算
      「结论」，之前只是中途的一句话（实测有线程最后一句非空文本是开头的
      「我先检查任务环境」，当结论转述给主控就是误导）；
    - ``unanswered`` / ``interrupted``：现场断点。前者是「落了 tool_calls 却没
      结果行」，后者是「结果行是中断占位」——**两者都要**：清扫跑过之后
      `_repair_dangling_tool_calls` 已经把前者填成占位，只看 `unanswered` 会
      恒为空（实测 5 条事故线程全是 []），真正被打断的是 `interrupted`。

    只读，不加锁，不写库。
    """
    msgs = list_messages(bb, thread_id)
    answered = {str(m.get("tool_use_id") or "")
                for m in msgs if m.get("role") == "tool"}
    names_by_id: dict[str, str] = {}
    delivery, delivery_n = "", 0
    final_text, final_idx = "", -1
    unanswered: list[dict[str, str]] = []
    last_tool_idx = -1
    for i, m in enumerate(msgs):
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool":
            last_tool_idx = i
            if content.startswith(_DELIVERY_MARK):
                n, body = split_delivery(content)
                if body and n >= delivery_n:   # 后写优先；同序号取后出现的
                    delivery, delivery_n = body, n
        elif role == "assistant":
            if content.strip() and not is_interrupt_note(content):
                final_text, final_idx = content.strip(), i
            for tc in (m.get("tool_calls") or []):
                tcid = str((tc or {}).get("id") or "").strip()
                name = str((tc or {}).get("name") or "")
                if not tcid:
                    continue
                names_by_id[tcid] = name
                if tcid not in answered:
                    unanswered.append({"id": tcid, "name": name})
    interrupted = [
        {"id": str(m.get("tool_use_id") or ""),
         "name": names_by_id.get(str(m.get("tool_use_id") or ""), "")}
        for m in msgs
        if m.get("role") == "tool"
        and is_interrupt_placeholder(str(m.get("content") or ""))
    ]
    return {
        "delivery": delivery,
        "delivery_n": delivery_n,
        "final_text": final_text,
        "final_after_tools": final_idx > last_tool_idx,
        "unanswered": unanswered,
        "interrupted": interrupted,
        "messages": len(msgs),
    }


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
    """落一条消息。线程已被删除时抛 ``ThreadGone``（而非裸 FK IntegrityError）。

    2026-10-19 事故：`chat_messages.thread_id` 有外键约束、连接开着
    ``PRAGMA foreign_keys=ON``。人类在子专家跑到一半时删掉线程 → 下一次写消息
    抛 ``FOREIGN KEY constraint failed``，穿到 `_tool_call_expert` 的 except
    变成「专家线程执行失败：FOREIGN KEY constraint failed」——用户看到的是
    一个看不懂的系统错误，而真实原因是他自己的删除操作。转成 ``ThreadGone``
    后上层可以「安静收尾」（线程都不要了，没地方写、也没必要写）。
    """
    if role not in ("user", "assistant", "tool"):
        raise ValueError(f"非法消息角色: {role}")
    ts = now()
    try:
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
    except sqlite3.IntegrityError as exc:
        # 只把「线程行没了」这一种翻译成 ThreadGone；其它完整性错误（脏数据）
        # 照旧上抛，别把真 bug 静音。
        if "FOREIGN KEY" in str(exc).upper():
            raise ThreadGone(f"线程 {thread_id} 已被删除，消息无处落库") from exc
        raise
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
        # 系统注入消息的派生标记（2026-10-24）。**在读出层派生，不落库**：
        # 判定依据是文案开头的中文方括号标签（既有事实），而落库层
        # `append_message` 不知道调用方是人类还是系统——`ChatTurn.run(text)` 是
        # 两者共用的唯一入口，加一列 origin 就得改 61 个调用点，且要用参数把
        # 「这条是谁说的」一路透传下来，漏一处就静默退化成人类发言。
        #
        # 放在这里的好处：前端只认 `origin` 字段（不复制前缀表），任何走
        # `list_messages` 的消费方自动同口径；`content` 保持审计原文不改写。
        origin = origin_of(d.get("content") or "") if d.get("role") == "user" else None
        if origin is not None:
            d["origin"] = origin
        out.append(d)
    return out


def count_messages(bb, thread_id: str) -> int:
    return int(bb.conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE thread_id=?",
        (thread_id,)).fetchone()[0])
