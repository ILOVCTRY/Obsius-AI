"""后台专家结果补达唤醒（expert-bg-wake，2026-10-22）。

**问题**（用户实测，2026-10-22）：CTF 解题主控说「等 forensics 回执到达后我做
最后判定并收尾」，然后轮次就结束了——右下角停止钮变回发送钮，主控一动不动。
十几分钟后 forensics 跑完，结果**躺在 DB 里没人读**。

链路断在三个地方，缺一个都通不了：

1. CTF/激进/ARTEX 主控启用**先返先触发**（`is_first_return_controller`）：
   `_parallel_expert_dispatch` 等到首个方向返回就提前断候，剩余专家转后台
   （`executor.shutdown(wait=False)`），主线程给它们补 `_PROBE_PENDING` 占位。
2. 后台 worker 完成时（`run_one`）把真实结果以**同 `tool_use_id` 续写进 DB**
   ✓，并推 `_probe_overflow`——但 overflow 只在**本轮步边界**被
   `_drain_probe_overflow` 排干，而且它是**实例内存字段**。主控轮次一收尾，
   实例就没了，那条结果再没有人取。
3. 唯一的现成唤醒通道 `ctf_wake` 只认「候选 flag 落库」
   （`challenge.flag.recorded`）——worker 没提交 flag（纯结论 / 未命中 /
   取证链回执）就**完全没有触发器**；而且它的语义是「解释一条候选」，不是
   「收结果继续判定」。

**为什么不在 `run_one` 里直接起轮**：`core/chat/` 拿不到 `app.state`（同
`inbox.py` 的教训——登记表放 app 上会静默漏掉子线程）。core 只广播事件，
由 app 层订阅后决定要不要起轮。这也让「谁在跑、该不该叫醒」这类判断留在
唯一能看到线程状态的那一层。

**两个触发点，判据都是「主控真的没读到」，不靠时间窗口猜**：

- ``EVENT_DONE``：`run_one` 落库后广播。主控**已 idle** → 唤醒；主控**在跑**
  → 跳过（它自己的 overflow 通道会在下一个步边界补达，别多开一轮）。
- ``EVENT_UNREAD``：`ChatTurn.run()` 收尾时若 `_probe_overflow` **仍有剩余**
  → 广播。这覆盖「主控正在收尾、worker 恰好完成」的窄窗口：那一刻触发点 1
  判「在跑」跳过了，但那个实例已经不会再 drain 了。两个触发点合起来才是闭环。

**为什么不靠「等 N 秒再判一次」**：延时复查要么漏（窗口更长）、要么误唤醒
（主控正常收尾后又多跑一轮）。而「收尾时手上还剩哪些没交出去」是主控自己
知道的确切事实，问它比猜它准。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 触发点 1：后台专家跑完（主控可能已收尾）。
EVENT_DONE = "chat.expert.background_done"

# 触发点 2：主控收尾时手上还有未交出的后台结果。
EVENT_UNREAD = "chat.background_unread"

# 唤醒轮注入的 user 消息前缀。前端据此渲染成「系统提示」而不是「我说的话」
# （`ChatTurn.run` 无条件把入参落成 role=user，没有别的区分通道）。
WAKE_TAG = "〔系统补达〕"

# 去重记账事件 kind（按 tool_use_id 记，重启后仍有效）。
WAKE_EVENT = "chat.expert_bg_wake"

# 单条结果在唤醒文案里保留的正文长度。全文在 DB 的 tool 行里，模型下一步
# 用 `_load_history` 就能读到，这里只需要让它**认出是哪一条**。
_HEAD_CHARS = 600


def _short_id(tool_use_id: str) -> str:
    """tool_use_id 的短尾——文案里给人看，别让模型去比对长串。"""
    return str(tool_use_id or "")[-8:]


def _clip(text: str) -> str:
    body = str(text or "").strip()
    if len(body) <= _HEAD_CHARS:
        return body
    return body[:_HEAD_CHARS] + "\n…（后面还有，全文见本线程里该调用的工具结果）"


def done_text(*, expert: str, tool_use_id: str, result: str = "",
              controller_hint: str = "") -> str:
    """触发点 1 的唤醒文案：一条后台专家结果回来了，主控该接着判定。

    **必须写明「这不是新任务」**：主控刚收尾，最容易被这条消息理解成「人类
    又派了新活」，于是重新分诊、重派一遍已经跑完的方向——那正是我们想避免的
    浪费（这条结果本身就是那个方向的答案）。

    **必须写明「不要重派同一方向」**：结果已在历史里，重派只会得到第二份
    同样的摘要，还占掉一个方向额度。

    ``controller_hint``：按轨/角色分叉的「接下来能做什么」。CTF 主控的动作面
    与激进主控完全不同（前者要走 ctf_submit_flag / ctf_conclude，后者是派
    深挖专家），写死一套会让模型做越界动作。
    """
    lines = [
        f"{WAKE_TAG}你派出的专家已在后台跑完",
        "",
        "你在上一轮「先返先触发」提前结束等待时，这个专家还在后台执行；它现在"
        "跑完了，真实结果**已经以同一个 tool_use_id 续写进本线程历史**——就是"
        "你上下文里那条「[后台继续] 该并发专家仍在后台执行…」的位置。",
        "",
        f"- 专家：{expert or '（未记录）'}",
        f"- 调用：{_short_id(tool_use_id)}",
    ]
    if result:
        lines += ["", "结果开头（全文在历史里）：", "```", _clip(result), "```"]
    lines += [
        "",
        "**这不是新任务**：它是你上一轮已经派出去的工作的回执，不是人类新提的要求。",
        "**不要重派这个方向**——它已经跑完了，重派只会再烧一遍并占掉一个方向额度。",
        "",
        "这一轮要做的是：读完那条结果，按你收尾前的判断往下走。",
    ]
    if controller_hint:
        lines += ["", controller_hint]
    return "\n".join(lines)


def unread_text(*, pending: list[dict], controller_hint: str = "") -> str:
    """触发点 2 的唤醒文案：主控收尾时手上还有没交出的后台结果。

    与 `done_text` 的区别在**语气与归属**：这里主控自己「以为」收尾是完整的，
    实际上漏了几条。文案要说清「你收尾时它们还没回来」，而不是「你又派了新活」。
    """
    n = len(pending)
    lines = [
        f"{WAKE_TAG}你收尾时还有 {n} 条后台结果没回来",
        "",
        "你上一轮判定收尾时，这批专家仍在后台执行；它们现在都跑完了，真实结果"
        "**已经以各自 tool_use_id 续写进本线程历史**。你当时的收尾结论可能没有"
        "把这部分算进去。",
        "",
    ]
    for it in pending[:8]:
        lines.append(f"- 专家 {it.get('expert') or '（未记录）'}"
                     f"（调用 {_short_id(it.get('tool_use_id') or '')}）"
                     f"{'' if it.get('ok', True) else ' · 执行失败'}")
    if n > 8:
        lines.append(f"- 另有 {n - 8} 条（见历史里的工具结果）")
    lines += [
        "",
        "**这不是新任务**：是上一轮已派出工作的回执。读完它们，把结论补进你"
        "刚才的判定——如果某条改变了结论，以它为准。",
    ]
    if controller_hint:
        lines += ["", controller_hint]
    return "\n".join(lines)


def already_woken(bb, project_id: str, tool_use_id: str) -> bool:
    """该调用是否已因补达唤醒过（查事件表，重启后仍然有效）。

    去重粒度是 **tool_use_id** 而不是线程：同一轮可能同时有多条后台结果，
    第一条唤醒主控后，后续几条已在同一轮上下文里被读到，不该各自再叫一次。
    """
    tid = str(tool_use_id or "").strip()
    if not tid:
        return False
    try:
        hit = bb.conn.execute(
            "SELECT 1 FROM events WHERE project_id=? AND kind=?"
            " AND json_extract(payload, '$.tool_use_id')=? LIMIT 1",
            (project_id, WAKE_EVENT, tid)).fetchone()
    except Exception:  # noqa: BLE001 —— json_extract 不可用（老 sqlite）时退回不拦
        return False
    return hit is not None
