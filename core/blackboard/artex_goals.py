"""ARTEX 目标注册与 prove 闭环（2026-10-19，artex-goals）。

原生 ARTEX 的目标治理（``agent/goals.go`` + ``server/goals_api.go``）：

- **目标是注册的实体**，不是一句描述文本。目标分解器（goals agent）从任务
  目标里拆出「最终可交付/可核验的结果」，逐条 ``set_goals`` 落库；
- **prove_goal**：当某个发现/事实证明某条目标达成时，把证据节点连到目标
  节点并标记 met；系统在每条目标 met 后检查是否**全部** met，是则任务自动
  判完成（上游 ``prove_goal`` 尾部逻辑）；
- **goal_met**：绕过逐条证明、从全局宣布整个任务收官（上游默认不绑任何
  agent，有权重且有误判风险）。

本项目落地时的三处口径：

1. 目标节点复用探索图 ``goal`` kind + ``proves`` 边（证据 → 目标，与
   ``record_intent_result`` 里 fact → finding 的 ``proves`` 同向：证明方
   指向被证明方）。注册目标带 ``metadata.source`` 标记，与 Planner 为根
   Intent 建的探索节点（同 kind 但无标记）区分——后者是任务目标的可视化
   锚点，不进目标注册表。
2. ``prove_goal`` 工具级调用沿用上游信任模型（finding/fact 证据均可，
   由调用方判断）；**LLM 批量声明**（``plan_followup`` 的 ``goals_met``
   字段）走确定性硬闸：只接受已有系统关联证据（``record_intent_result``
   在 Worker 收尾时按资产重叠挂链）的目标，无证据的声明记入 reason 但不
   翻转——模型说自己证明了不算，证据链说了算。
3. ``goal_met`` 同时做两件事：把全部 open 目标标 met（``metadata.met_via``
   留痕「未经逐条证明」），并在 runtime metadata 落任务级宣告。后者经
   ``evaluate_task`` 跨轮保留，覆盖「任务零目标也要能收官」的上游行为。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from core.blackboard.artex_exploration import (
    add_edge,
    create_node,
    get_node,
    list_edges,
    list_nodes,
    upsert_node,
)

log = logging.getLogger(__name__)

GOAL_STATUSES = frozenset({"open", "met"})
MAX_GOALS = 20
# 证据节点允许的 kind：prove_goal 的 evidence 必须是这两类。
_EVIDENCE_KINDS = frozenset({"fact", "finding"})
# 注册目标标记：把目标节点与 Planner 给根 Intent 建的探索节点区分开。
_SOURCE_KEY = "source"


def fingerprint(text: str) -> str:
    """目标文本归一化指纹（与 Planner Intent 去重同口径：去空白与常见标点）。"""
    return re.sub(r"[\s，。；、,.;:：!！?？\-—_/\\]+", "", str(text or "")).lower()


def _normalize_items(goals: Any) -> list[dict]:
    """把入参归一成 [{"text","vulnclass"}]；单条字符串/对象都接受。"""
    if goals is None:
        return []
    raw = goals if isinstance(goals, list) else [goals]
    items: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            text, vulnclass = entry, ""
        elif isinstance(entry, dict):
            text = str(entry.get("text") or entry.get("objective") or "").strip()
            vulnclass = str(entry.get("vulnclass") or entry.get("vuln_class") or "").strip()
        else:
            continue
        text = text.strip()
        if text:
            items.append({"text": text[:500], "vulnclass": vulnclass[:80]})
    return items


def _rule_split(goal: str, *, limit: int = 5) -> list[dict]:
    """无 LLM 时的规则兜底：按标点/连接词切分，保证目标节点始终存在。

    上游 ``DecomposeGoals`` 在 provider 缺失或产出为空时同样退回规则拆分，
    口径一致：宁粗勿无——没有目标节点，终态判定就退回「有 fact 即达成」的
    粗糙代理，那正是本轮要消灭的行为。
    """
    text = str(goal or "").strip()
    if not text:
        return []
    pieces = [p.strip(" ，,。;；\n\t") for p in re.split(
        r"[，,。;；\n]+|以及|并且", text)]
    pieces = [p for p in pieces if p]
    if not pieces:
        pieces = [text]
    return [{"text": p[:500], "vulnclass": ""} for p in pieces[:max(1, limit)]]


_GOALS_SYSTEM = """你是 ARTEX 目标分解器。从任务目标里识别**最终要达成的结果**，而不是攻击步骤。
不是目标的内容（禁止列出）：信息收集、侦察、端点扫描、漏洞分析与验证过程、攻击步骤、结果验证步骤。
拆分原则：最终目标只有一个 → 输出一个；多个相互独立的最终交付物 → 分别列出；严禁臆造用户未提及的目标；
能对应明确漏洞类的标注 vulnclass，信息收集/业务逻辑类目标留空。
只输出 JSON：{"goals":[{"text":"...","vulnclass":"..."}]}，不要 Markdown。"""


def decompose_goals(*, llm: Any, goal: str, track: str = "") -> list[dict]:
    """把任务目标拆成离散可核验目标；LLM 失败/未配置时走规则兜底。"""
    text = str(goal or "").strip()
    if not text:
        return []
    if llm is not None:
        try:
            response = llm.chat([
                {"role": "system", "content": _GOALS_SYSTEM},
                {"role": "user", "content": f"轨道: {track or '未指定'}\n任务目标: {text}"},
            ])
            raw = str(getattr(response, "text", response) or "").strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                payload = json.loads(raw[start:end + 1])
                items = _normalize_items(payload.get("goals"))
                if items:
                    return items[:MAX_GOALS]
        except Exception as exc:  # noqa: BLE001 —— 分解失败不许炸掉建任务
            log.warning("ARTEX 目标分解 LLM 失败，退回规则拆分: %s", exc)
    return _rule_split(text)


def set_goals(*, bb, project_id: str, task_id: str, goals: Any,
              author: str = "system", asset_ids=None,
              max_goals: int = MAX_GOALS) -> dict:
    """登记任务目标（幂等，按指纹去重）。返回 {"added","skipped","total","goals"}。

    ``asset_ids`` 是目标的适用范围（通常继承任务的初始资产），证据挂链时用来
    判定「这条事实是否与目标相关」；为空表示不限资产。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    items = _normalize_items(goals)
    if not items:
        raise ValueError("goals 必须是非空数组（或单条 text）")
    scoped = [str(x).strip() for x in (asset_ids or []) if str(x).strip()]
    existing = registered_goals(bb, project_id, task_id)
    by_fp = {fingerprint(n.get("title") or ""): n for n in existing}
    added: list[dict] = []
    skipped: list[str] = []
    for item in items:
        fp = fingerprint(item["text"])
        if not fp:
            continue
        if fp in by_fp:
            skipped.append(item["text"])
            continue
        if len(existing) + len(added) >= max(1, int(max_goals)):
            skipped.append(f"{item['text']}（超过目标数上限 {max_goals}）")
            continue
        node = create_node(
            bb, project_id, "goal",
            node_id=f"{task_id}:goal:{hashlib.sha1(fp.encode('utf-8')).hexdigest()[:10]}",
            title=item["text"], content=item["text"], task_id=task_id,
            status="open", confidence=1.0, asset_ids=scoped,
            metadata={_SOURCE_KEY: str(author or "system")[:80],
                      "vulnclass": item["vulnclass"]})
        by_fp[fp] = node
        added.append(node)
    return {"added": added, "skipped": skipped,
            "total": len(existing) + len(added),
            "goals": existing + added}


def registered_goals(bb, project_id: str, task_id: str) -> list[dict]:
    """本任务注册过的目标节点（排除 Planner 给根 Intent 建的探索锚点）。"""
    return [n for n in list_nodes(bb, project_id, task_id=task_id, kind="goal")
            if str((n.get("metadata") or {}).get(_SOURCE_KEY) or "")]


def list_goals(bb, project_id: str, task_id: str) -> list[dict]:
    """目标清单 + 各自已关联的 proves 证据（含 kind/title，供 UI 与提示词）。"""
    edges = list_edges(bb, project_id, relation="proves")
    incoming: dict[str, list[dict]] = {}
    for edge in edges:
        incoming.setdefault(str(edge.get("to_node_id") or ""), []).append(edge)
    result = []
    for goal in registered_goals(bb, project_id, task_id):
        evidence = []
        for edge in incoming.get(str(goal.get("id") or ""), []):
            node = get_node(bb, project_id, str(edge.get("from_node_id") or ""))
            if node is None or str(node.get("task_id") or "") != str(task_id or "").strip():
                continue
            evidence.append({"id": node.get("id"), "kind": node.get("kind"),
                             "title": node.get("title"),
                             "note": str((edge.get("metadata") or {}).get("reason") or "")})
        result.append({**goal, "evidence": evidence})
    return result


def goals_stats(bb, project_id: str, task_id: str) -> dict:
    """{"total","met","open"}；无注册目标时 total=0。"""
    goals = registered_goals(bb, project_id, task_id)
    met = sum(1 for g in goals if str(g.get("status")) == "met")
    return {"total": len(goals), "met": met, "open": len(goals) - met}


def prove_goal(*, bb, project_id: str, task_id: str, goal_id: str,
               evidence_ids: Any, reason: str = "", author: str = "") -> dict:
    """把证据节点连到目标并标记 met；全部目标 met 时返回 all_met=True。

    证据必须是本任务的 fact/finding 节点（上游同口径：GetNodeWithSources
    后校验 kind）。目标与证据跨项目/跨任务一律拒绝——血缘不能靠猜。
    """
    goal_id = str(goal_id or "").strip()
    if not goal_id:
        raise ValueError("goal_id 必填")
    ids = [str(x).strip() for x in (evidence_ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("evidence_ids 必须是非空数组")
    goal = get_node(bb, project_id, goal_id)
    if goal is None or str(goal.get("kind")) != "goal" or not str(
            (goal.get("metadata") or {}).get(_SOURCE_KEY) or ""):
        raise ValueError("goal_id 必须是本任务注册的目标节点")
    if str(goal.get("task_id") or "") != str(task_id or "").strip():
        raise ValueError("目标节点不属于该任务")
    linked: list[dict] = []
    for eid in ids:
        node = get_node(bb, project_id, eid)
        if node is None or str(node.get("kind")) not in _EVIDENCE_KINDS:
            raise ValueError(f"evidence {eid} 不是本任务的事实/发现节点")
        if str(node.get("task_id") or "") != str(task_id or "").strip():
            raise ValueError(f"证据 {eid} 不属于该任务")
        add_edge(bb, project_id, eid, goal_id, "proves",
                 metadata={"reason": str(reason or "")[:500],
                           "author": str(author or "")[:80]})
        linked.append({"id": node.get("id"), "kind": node.get("kind")})
    if str(goal.get("status")) != "met":
        goal = upsert_node(bb, project_id, goal_id, "goal", status="met",
                           content=str(goal.get("content") or "")[:2000])
    stats = goals_stats(bb, project_id, task_id)
    return {"goal": {"id": goal.get("id"), "status": goal.get("status"),
                     "title": goal.get("title")},
            "linked": linked, "reason": str(reason or "")[:500],
            "all_met": bool(stats["total"]) and stats["open"] == 0,
            **stats}


def link_evidence_to_goals(*, bb, project_id: str, task_id: str, evidence_id: str,
                           reason: str = "", auto_prove: bool = True) -> dict:
    """Worker 收尾时把新证据按「资产重叠 / 漏洞类命中」自动挂到相关目标上。

    这是**系统级**关联，也是 ``verify_goals_met`` 硬闸的判定依据：模型声明的
    目标达成必须已经挂着这类证据，否则驳回。默认只挂链不翻转；仅当证据是
    finding 且**精确命中**目标的 ``vulnclass`` 时才允许自动标 met（``auto_prove``
    可关）。上游同口径：证据证明目标，模型自述不算。

    返回 ``{"linked":[...], "proved":[...], "all_met":bool}``。
    """
    task_id = str(task_id or "").strip()
    evidence_id = str(evidence_id or "").strip()
    if not task_id or not evidence_id:
        return {"linked": [], "proved": [], "all_met": False}
    node = get_node(bb, project_id, evidence_id)
    if node is None or str(node.get("kind")) not in _EVIDENCE_KINDS:
        return {"linked": [], "proved": [], "all_met": False}
    if str(node.get("task_id") or "") != task_id:
        return {"linked": [], "proved": [], "all_met": False}
    evidence_assets = {str(x) for x in (node.get("asset_ids") or []) if str(x)}
    evidence_meta = node.get("metadata") or {}
    evidence_class = str(evidence_meta.get("vuln_class") or "").strip().lower()
    # vuln_class 仅作精确分类自动证明条件；不再依据笼统 category 猜目标达成。
    linked: list[dict] = []
    proved: list[dict] = []
    for goal in registered_goals(bb, project_id, task_id):
        goal_assets = {str(x) for x in (goal.get("asset_ids") or []) if str(x)}
        # 目标限定资产时，证据必须至少带一个同范围资产；未知范围不能被当成命中。
        if goal_assets and (not evidence_assets or goal_assets.isdisjoint(evidence_assets)):
            continue
        goal_class = str((goal.get("metadata") or {}).get("vulnclass") or "").strip().lower()
        add_edge(bb, project_id, evidence_id, str(goal.get("id")), "proves",
                 metadata={"reason": str(reason or "")[:500], "author": "system"})
        linked.append(str(goal.get("id")))
        if not auto_prove or str(goal.get("status")) == "met":
            continue
        if goal_class and evidence_class and goal_class == evidence_class:
            upsert_node(bb, project_id, str(goal.get("id")), "goal", status="met")
            proved.append(str(goal.get("id")))
    if linked:
        bb.append_event(project_id, "artex.goal.evidence.linked", {
            "task_id": task_id, "evidence_id": evidence_id,
            "goal_ids": linked, "proved_goal_ids": proved})
    stats = goals_stats(bb, project_id, task_id)
    return {"linked": linked, "proved": proved,
            "all_met": bool(stats["total"]) and stats["open"] == 0, **stats}


def verify_goals_met(bb, project_id: str, task_id: str,
                     claimed_ids: Any) -> dict:
    """LLM 批量声明的确定性硬闸：只接受已有系统关联证据的目标。

    ``plan_followup`` 让 Planner 在 JSON 里声明 ``goals_met``；模型说「证明
    了」不算数——目标必须已经挂着至少一条 proves 证据（``record_intent_result``
    在 Worker 收尾时按资产重叠挂链，声明前就存在）才允许翻转。无证据的声明
    进 ``rejected``，由调用方写进 reason 让下一轮 Planner 看见。
    """
    task_id = str(task_id or "").strip()
    known = {str(g.get("id") or ""): g for g in list_goals(bb, project_id, task_id)}
    accepted: list[dict] = []
    rejected: list[dict] = []
    for raw in claimed_ids or []:
        gid = str(raw or "").strip()
        goal = known.get(gid)
        if goal is None:
            rejected.append({"goal_id": gid, "why": "不是本任务注册的目标"})
            continue
        if str(goal.get("status")) == "met":
            accepted.append({"goal_id": gid, "why": "已是 met"})
            continue
        if not goal.get("evidence"):
            rejected.append({"goal_id": gid, "why": "无系统关联证据（proves 链为空）"})
            continue
        upsert_node(bb, project_id, gid, "goal", status="met",
                    metadata={**(goal.get("metadata") or {}),
                              "met_via": "planner_evidence",
                              "met_evidence_ids": [str(ev.get("id") or "")
                                                   for ev in goal.get("evidence") or []
                                                   if ev.get("id")]})
        accepted.append({"goal_id": gid,
                         "why": f"{len(goal['evidence'])} 条系统关联证据"})
    return {"accepted": accepted, "rejected": rejected}


def declare_goal_met(*, bb, project_id: str, task_id: str, reason: str,
                     author: str = "") -> dict:
    """goal_met：绕过逐条证明，全局宣布任务收官（上游默认不绑、有权重）。

    两件事：① 把全部 open 目标标 met，metadata 留痕 ``met_via=goal_met``
    （未经逐条证明，审计可辨）；② 任务级宣告落 runtime metadata（跨轮保留，
    覆盖「零目标任务」场景）。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("goal_met 需要 reason（目标真正达成的依据，不能是「本轮无新方向」）")
    marked = []
    for goal in registered_goals(bb, project_id, task_id):
        if str(goal.get("status")) == "met":
            continue
        metadata = dict(goal.get("metadata") or {})
        metadata.update({"met_via": "goal_met", "met_reason": reason[:500],
                         "met_author": str(author or "")[:80]})
        upsert_node(bb, project_id, str(goal.get("id")), "goal", status="met",
                    metadata=metadata)
        marked.append(str(goal.get("id")))
    return {"marked": marked, "reason": reason[:500],
            "stats": goals_stats(bb, project_id, task_id)}
