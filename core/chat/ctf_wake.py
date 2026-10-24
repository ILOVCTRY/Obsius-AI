"""候选 flag 唤醒（ctf-flag-wake，2026-10-20）。

**问题**：CTF 解题主控的子代理（worker）提交候选 flag 时，如果主控本轮已经
收尾（idle），这条候选就躺在台账里没人管——用户看不到「有个候选等你确认」，
主控也不会主动说一句话。后台并发派发（`_parallel_expert_dispatch` 的
`executor.shutdown(wait=False)`）让这个窗口变成常态：主控先返，worker 后到，
`_probe_overflow` 又只在本轮步边界 drain，实例一丢就永远补不回来。

**做法**：`challenge.flag.recorded` 事件（verdict=pending、judged_by=ai）落库
即触发一次「唤醒主控跑一轮」——主控说一段话（谁提交了什么、证据是什么、
为什么值得确认），前端在 composer 上方挂候选 flag 卡（复制 + 确认/标记错误）。

**为什么挂事件总线而不是改 `ctf_submit_flag`**：`ToolDispatcher` 拿不到
`app.state.jobs`，也不该拿到——它是纯执行层。`record_flag` 已经发了
`challenge.flag.recorded`，订阅它是最短路径（同步回调，<100ms），且对「谁
提交的」完全无感：worker、主控自己、未来别的通道都一并覆盖。

**为什么不用轮询**：`_mission_poll_sweep` 是 60s 一拍，用户会以为没反应。

**四条边界**：
- 主控**正在跑**（status=running）→ 不唤醒。worker 是同步 spawn 的
  （`sub_turn.run(task)`），主控本轮自己就会看到这条 flag；硬塞第二轮只会撞
  状态冲突。
- 同一 `flag_id` 只唤醒一次（事件记账 `chat.ctf_flag_wake`，重启后仍有效）。
- 人工确认（`decide_flag` 的 accepted）**不**走这里——那是
  `_notify_project_chat_turns` 的地盘：向**该项目**在跑的轮次投递一条
  「题解出了，尽快收工」通知（`NoticeInbox`），而不是开一轮新对话去解释候选。
  两者语义不同：那个是「告诉正在跑的那一轮事实变了」，这个是「唤醒空闲主控
  说一段话」。
- 非 AI 提交（`judged_by != "ai"`）不唤醒——人工录入的 flag 不需要 AI 再解释。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# 唤醒轮注入的 user 消息前缀。前端据此把这条渲染成「系统提示」而不是「我说的话」
# （`ChatTurn.run` 无条件把入参落成 role=user，没有别的区分通道）。
#
# **2026-10-24 修正**：与 `background_wake.py` 同款失效——「前端会识别」只写在
# 文案侧注释里，渲染层从未实现。现统一由 `core/chat/origin.py` 裁决（后端在
# `store.list_messages` 派生 `origin` 字段，前端只认字段）。
from core.chat.origin import LABEL_CTF_WAKE, tag as _tag

WAKE_TAG = _tag(LABEL_CTF_WAKE)

# 唤醒事件 kind（去重锚点 + 前端可观测）。
WAKE_EVENT = "chat.ctf_flag_wake"

_NO_SOURCE = "未提供（worker 没写解题路径）"
_NO_TRANSFORM = "未提供（**仅形态判定，没有回环验证**）"


def find_controller_thread(bb, project_id: str, challenge_id: str) -> dict | None:
    """找这道题对应的 CTF 解题主控线程（没有则 None）。

    优先**直接绑定该题目的顶层线程**（`parent_thread_id` 为空）——那是人类开的
    那道题的主控会话；退一步接受任意绑定该题目的 ctf-controller 线程（主控也
    可能被别的通道建成子线程）。都不匹配就返回 None，**不按线程标题猜**：
    `_challenge_fallback_asset()` 式的按标题补建会把唤醒打到错题上。
    """
    from core.chat import store as chat_store
    from core.chat.runtime import CTF_CONTROLLER_ID

    cid = str(challenge_id or "").strip()
    if not cid:
        return None
    rows = chat_store.list_threads(bb, project_id, CTF_CONTROLLER_ID)
    for r in rows:
        if (str(r.get("challenge_asset_id") or "") == cid
                and not str(r.get("parent_thread_id") or "")):
            return r
    for r in rows:
        if str(r.get("challenge_asset_id") or "") == cid:
            return r
    return None


def get_flag(bb, project_id: str, flag_id: str) -> dict | None:
    """按 id 取一条 flag 行（带 project 隔离，取不到返回 None）。"""
    fid = str(flag_id or "").strip()
    if not fid:
        return None
    try:
        row = bb.conn.execute(
            "SELECT * FROM challenge_flags WHERE id=? AND project_id=?",
            (fid, project_id)).fetchone()
    except Exception:  # noqa: BLE001 —— 唤醒是附加项，读不到就放弃
        log.exception("读 flag 行失败 pid=%s fid=%s", project_id, fid)
        return None
    return dict(row) if row is not None else None


def already_woken(bb, project_id: str, flag_id: str) -> bool:
    """该 flag 是否已被唤醒过（查事件表，重启后仍然有效）。"""
    fid = str(flag_id or "").strip()
    if not fid:
        return False
    try:
        hit = bb.conn.execute(
            "SELECT 1 FROM events WHERE project_id=? AND kind=?"
            " AND json_extract(payload, '$.flag_id')=? LIMIT 1",
            (project_id, WAKE_EVENT, fid)).fetchone()
    except Exception:  # noqa: BLE001 —— json_extract 不可用（老 sqlite）时退回内存去重
        return False
    return hit is not None


def wake_text(bb, project_id: str, challenge_id: str, flag_row: dict) -> str:
    """拼唤醒轮的用户消息：说清「谁提交了什么、证据强度、要你做什么」。

    **主控在这轮里不该调工具**（它不是被叫来干活的，是被叫来说一句话的）——
    所以文案里显式禁止派方向，并要求它只输出一段面向人类的说明。
    """
    cid = str(challenge_id or "").strip()
    token = str(flag_row.get("flag") or "").strip()
    if not cid or not token:
        return ""
    name = cid
    try:
        asset = bb.get_asset(cid) or {}
        name = str(asset.get("value") or asset.get("name") or cid)
    except Exception:  # noqa: BLE001
        pass
    accepted = candidate = quota = 0
    try:
        from core.blackboard.challenge_flags import challenge_state
        st = challenge_state(bb, project_id, cid) or {}
        accepted = int(st.get("accepted") or 0)
        candidate = int(st.get("candidate") or 0)
        quota = int(st.get("quota") or 0)
    except Exception:  # noqa: BLE001 —— 台账摘要失败不阻断唤醒
        pass
    conf = float(flag_row.get("confidence") or 0.0)
    source = str(flag_row.get("source") or "").strip() or _NO_SOURCE
    transform = str(flag_row.get("transform") or "").strip() or _NO_TRANSFORM
    ledger = (f"已确认 {accepted}/{quota}，候选 {candidate} 条"
              if quota else f"已确认 {accepted}，候选 {candidate} 条")
    return "\n".join([
        f"{WAKE_TAG}候选 flag 待人工确认",
        "",
        "你的子代理刚提交了一条**候选** flag（已进台账，verdict=pending）。",
        "人类需要你说明它，然后由他点确认或标记为错误。",
        "",
        f"- 题目：{name}",
        f"- flag：`{token}`",
        f"- 置信：{conf:.1f}（格式置信，不是「已解出」的证明）",
        f"- 解题路径：{source}",
        f"- 变换链：{transform}",
        f"- 台账现状：{ledger}",
        "",
        "**这一轮只做一件事**：回复一段话给人类，不要调任何工具，不要派新方向。",
        "这段话要包含三句：",
        "1. 这条 flag 是怎么来的（用上面的解题路径，一句话）；",
        "2. 它能不能自证——有变换链说明可回环复算；只有形态判定说明**只是像**；",
        "3. 请人类点「确认有效」或「标记为错误」。",
        "",
        "**不要说「已解出」**：候选不计解出，只有人工确认（accepted）才算。",
    ])
