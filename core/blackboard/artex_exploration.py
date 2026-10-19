"""ARTEX 探索图持久化。

探索图独立于证据链 ``intents`` 和调度 ``artex_intents``：Goal/Intent/Fact/
Finding/Hint 通过显式血缘边组成可查询的探索历史。所有查询和写入都以
project_id 隔离，写操作幂等并广播活动事件。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.blackboard.store import _row_to_dict, new_id, now

log = logging.getLogger(__name__)

NODE_KINDS = frozenset({"goal", "intent", "fact", "finding", "hint"})
RELATIONS = frozenset({"spawns", "yields", "derived_from", "proves"})


def _json(value: Any, default: Any) -> str:
    return json.dumps(value if value is not None else default, ensure_ascii=False)


def _hydrate(row: dict) -> dict:
    for key, default in (("asset_ids", []), ("metadata", {})):
        raw = row.get(key)
        try:
            row[key] = json.loads(raw) if isinstance(raw, str) else (raw or default)
        except (TypeError, ValueError):
            row[key] = default
    return row


def _check_kind(kind: str) -> str:
    kind = str(kind or "").strip().lower()
    if kind not in NODE_KINDS:
        raise ValueError(f"不支持的 ARTEX 探索节点类型: {kind}")
    return kind


def _check_relation(relation: str) -> str:
    relation = str(relation or "").strip().lower()
    if relation not in RELATIONS:
        raise ValueError(f"不支持的 ARTEX 探索边关系: {relation}")
    return relation


def get_node(bb, project_id: str, node_id: str) -> dict | None:
    row = bb.conn.execute(
        "SELECT * FROM artex_exploration_nodes WHERE project_id=? AND id=?",
        (project_id, node_id),
    ).fetchone()
    return _hydrate(_row_to_dict(row)) if row else None


def create_node(bb, project_id: str, kind: str, *, title: str = "",
                content: str = "", task_id: str = "", intent_id: str | None = None,
                status: str = "open", confidence: float = 0.0,
                asset_ids=None, metadata=None, node_id: str | None = None) -> dict:
    kind = _check_kind(kind)
    nid = str(node_id or new_id("explore"))
    ts = now()
    assets = [str(x).strip() for x in (asset_ids or []) if str(x).strip()]
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO artex_exploration_nodes(id,project_id,task_id,intent_id,kind,"
            "title,content,status,confidence,asset_ids,metadata,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, project_id, str(task_id or ""), intent_id, kind, str(title or ""),
             str(content or ""), str(status or "open"), float(confidence or 0),
             _json(assets, []), _json(metadata, {}), ts, ts),
        )
    bb.append_event(project_id, "artex.exploration.node.created",
                    {"node_id": nid, "kind": kind, "task_id": task_id,
                     "intent_id": intent_id, "asset_ids": assets})
    return get_node(bb, project_id, nid) or {}


def upsert_node(bb, project_id: str, node_id: str, kind: str, **fields) -> dict:
    existing = get_node(bb, project_id, node_id)
    if existing is None:
        return create_node(bb, project_id, kind, node_id=node_id, **fields)
    kind = _check_kind(kind)
    allowed = {"task_id", "intent_id", "title", "content", "status", "confidence",
               "asset_ids", "metadata"}
    values = {k: v for k, v in fields.items() if k in allowed}
    values["kind"] = kind
    if "asset_ids" in values:
        values["asset_ids"] = _json(values["asset_ids"], [])
    if "metadata" in values:
        values["metadata"] = _json(values["metadata"], {})
    if not values:
        return existing
    values["updated_at"] = now()
    assignments = ", ".join(f"{key}=?" for key in values)
    with bb._tx():
        bb.conn.execute(
            f"UPDATE artex_exploration_nodes SET {assignments} WHERE project_id=? AND id=?",
            (*values.values(), project_id, node_id),
        )
    bb.append_event(project_id, "artex.exploration.node.updated",
                    {"node_id": node_id, "kind": kind, "fields": list(values)})
    return get_node(bb, project_id, node_id) or {}


def add_edge(bb, project_id: str, from_node_id: str, to_node_id: str,
             relation: str, *, metadata=None, edge_id: str | None = None) -> dict:
    relation = _check_relation(relation)
    if from_node_id == to_node_id:
        raise ValueError("ARTEX 探索边不能连接节点自身")
    from_node = get_node(bb, project_id, from_node_id)
    to_node = get_node(bb, project_id, to_node_id)
    if from_node is None or to_node is None:
        raise ValueError("探索边两端节点必须属于同一项目且已存在")
    ts = now()
    eid = str(edge_id or new_id("edge"))
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO artex_exploration_edges(id,project_id,from_node_id,to_node_id,"
            "relation,metadata,created_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id,from_node_id,to_node_id,relation) DO UPDATE SET "
            "metadata=excluded.metadata",
            (eid, project_id, from_node_id, to_node_id, relation, _json(metadata, {}), ts),
        )
    row = bb.conn.execute(
        "SELECT * FROM artex_exploration_edges WHERE project_id=? AND from_node_id=? "
        "AND to_node_id=? AND relation=?",
        (project_id, from_node_id, to_node_id, relation),
    ).fetchone()
    result = _row_to_dict(row) or {}
    result["metadata"] = json.loads(result.get("metadata") or "{}")
    bb.append_event(project_id, "artex.exploration.edge.created",
                    {"edge_id": result.get("id"), "from_node_id": from_node_id,
                     "to_node_id": to_node_id, "relation": relation})
    return result


def list_nodes(bb, project_id: str, *, task_id: str | None = None,
               intent_id: str | None = None, kind: str | None = None) -> list[dict]:
    clauses = ["project_id=?"]
    args: list[Any] = [project_id]
    if task_id is not None:
        clauses.append("task_id=?"); args.append(task_id)
    if intent_id is not None:
        clauses.append("intent_id=?"); args.append(intent_id)
    if kind is not None:
        clauses.append("kind=?"); args.append(_check_kind(kind))
    rows = bb.conn.execute(
        "SELECT * FROM artex_exploration_nodes WHERE " + " AND ".join(clauses) +
        " ORDER BY CASE kind WHEN 'goal' THEN 0 WHEN 'intent' THEN 1 "
        "WHEN 'fact' THEN 2 WHEN 'finding' THEN 3 WHEN 'hint' THEN 4 ELSE 5 END, "
        "created_at ASC, id ASC", args).fetchall()
    return [_hydrate(_row_to_dict(row) or {}) for row in rows]


def list_edges(bb, project_id: str, *, node_id: str | None = None,
               relation: str | None = None) -> list[dict]:
    clauses = ["project_id=?"]
    args: list[Any] = [project_id]
    if node_id is not None:
        clauses.append("(from_node_id=? OR to_node_id=?)"); args.extend([node_id, node_id])
    if relation is not None:
        clauses.append("relation=?"); args.append(_check_relation(relation))
    rows = bb.conn.execute(
        "SELECT * FROM artex_exploration_edges WHERE " + " AND ".join(clauses) +
        " ORDER BY created_at ASC, id ASC", args).fetchall()
    result = []
    for row in rows:
        item = _row_to_dict(row) or {}
        try: item["metadata"] = json.loads(item.get("metadata") or "{}")
        except (TypeError, ValueError): item["metadata"] = {}
        result.append(item)
    return result


def record_hint(bb, project_id: str, *, task_id: str, intent_id: str,
                text: str, author: str = "human") -> dict | None:
    """把一条方向提示写成 Hint 节点，并记录 Intent 由它推导而来。"""
    text = str(text or "").strip()
    intent_id = str(intent_id or "").strip()
    if not text or not intent_id:
        return None
    node = create_node(
        bb, project_id, "hint",
        node_id=f"{intent_id}:hint:{new_id('h')}",
        title=text[:120], content=text, task_id=str(task_id or ""),
        intent_id=intent_id, status="open", confidence=0.5,
        metadata={"author": author},
    )
    intent_node_id = f"{intent_id}:explore"
    if get_node(bb, project_id, intent_node_id) is not None:
        add_edge(bb, project_id, intent_node_id, node["id"], "derived_from",
                 metadata={"task_id": str(task_id or ""), "source": "hint"})
    return node


def record_intent_result(bb, project_id: str, intent: dict) -> dict:
    """把 Worker 收尾结果物化为 Fact，并同步关联期间写入的 Finding。

    **兜底语义（artex-record-fact，2026-10-19）**：Worker 在过程中若已用
    ``record_fact`` 显式写过事实，这里就**不再**叠加一条整段 result 的汇总
    fact——否则同一意图会同时存在「结构化结论」与「一大坨原文」，规划者看到
    两份互相矛盾的事实，覆盖度分子也被重复计数。此时只做 Finding 关联。

    没有显式事实时仍落一条汇总 fact（``metadata.source='intent_result'``），
    保证「跑过就有事实」的覆盖度下限语义不破——这是历史任务与未升级 Worker
    的兼容路径。
    """
    intent_id = str(intent.get("id") or "").strip()
    if not intent_id:
        return {"fact": None, "findings": []}
    task_id = str(intent.get("task_id") or "")
    explicit = 0
    try:
        from core.blackboard.artex_facts import count_worker_facts
        explicit = count_worker_facts(bb, project_id, task_id, intent_id)
    except Exception:  # noqa: BLE001 —— 计数失败按「没有显式事实」处理（兜底优先）
        log.exception("ARTEX 显式事实计数失败 intent=%s", intent_id)
    if explicit:
        fact = None
    else:
        fact_id = f"{intent_id}:fact"
        fact = upsert_node(
            bb, project_id, fact_id, "fact",
            task_id=task_id, intent_id=intent_id,
            title=f"Worker 结果：{str(intent.get('objective') or '')[:120]}",
            content=str(intent.get("result") or intent.get("error") or ""),
            status="closed" if intent.get("status") == "completed" else str(intent.get("status") or "open"),
            confidence=1.0 if intent.get("status") == "completed" else 0.5,
            asset_ids=intent.get("asset_ids") or [],
            metadata={"intent_status": intent.get("status"),
                      "attempt": intent.get("attempt", 0),
                      "source": "intent_result",
                      # 兜底汇总事实没有真实 confidence 判定：标 inferred 语义，
                      # 让规划者知道「这条是整轮输出的概括，不是单条观察」。
                      "confidence_level": "inferred"},
        )
    intent_node_id = f"{intent_id}:explore"
    if get_node(bb, project_id, intent_node_id) is None:
        create_node(bb, project_id, "intent", node_id=intent_node_id,
                    title=str(intent.get("objective") or ""), content=str(intent.get("objective") or ""),
                    task_id=task_id, intent_id=intent_id, status=str(intent.get("status") or "open"),
                    asset_ids=intent.get("asset_ids") or [])
    # 已写显式事实时不落汇总 fact：Finding 的 proves 边改挂意图节点本身
    # （finding 仍由这次意图产出，血缘不丢）。
    anchor_id = str((fact or {}).get("id") or intent_node_id)
    if fact is not None:
        add_edge(bb, project_id, intent_node_id, anchor_id, "yields",
                 metadata={"status": intent.get("status")})
    mapped = []
    for finding in bb.list_findings(project_id):
        finding_id = str(finding.get("id") or "")
        if not finding_id:
            continue
        finding_assets = {str(finding.get("target_asset_id") or "")} - {""}
        if intent.get("asset_ids") and finding_assets.isdisjoint(set(intent["asset_ids"])):
            continue
        node = upsert_node(
            bb, project_id, f"finding:{finding_id}", "finding", task_id=task_id,
            intent_id=intent_id, title=str(finding.get("title") or finding.get("vuln_class") or ""),
            content=str(finding.get("summary") or finding.get("evidence") or ""),
            status=str(finding.get("status") or "open"),
            confidence=float(finding.get("confidence") or 0),
            asset_ids=list(finding_assets),
            metadata={"finding_id": finding_id, "severity": finding.get("severity"),
                      "category": finding.get("category"),
                      "vuln_class": finding.get("vuln_class")},
        )
        add_edge(bb, project_id, anchor_id, node["id"], "proves",
                 metadata={"finding_id": finding_id})
        mapped.append(node)
    bb.append_event(project_id, "artex.exploration.fact.recorded",
                    {"intent_id": intent_id, "task_id": task_id,
                     "fact_id": anchor_id if fact is not None else "",
                     "finding_ids": [n["id"] for n in mapped],
                     "source": "intent_result" if fact is not None else "explicit_facts"})
    # 目标证据挂链（artex-goals）：事实/发现落库即按资产重叠挂到相关目标上，
    # 作为 prove 的**系统关联**。没有这一步，Planner 声明的 goals_met 永远
    # 过不了 verify_goals_met 的证据硬闸。失败不阻断事实落库。
    if task_id:
        try:
            from core.blackboard.artex_goals import link_evidence_to_goals
            if fact is not None:
                link_evidence_to_goals(bb=bb, project_id=project_id, task_id=task_id,
                                       evidence_id=str(fact["id"]),
                                       reason=f"Worker 收尾 {intent_id}")
            for node in mapped:
                link_evidence_to_goals(bb=bb, project_id=project_id, task_id=task_id,
                                       evidence_id=str(node["id"]),
                                       reason=f"Worker 收尾发现 {intent_id}")
        except Exception:  # noqa: BLE001
            log.exception("ARTEX 目标证据挂链失败 intent=%s", intent_id)
    return {"fact": fact, "findings": mapped}


def exploration_graph(bb, project_id: str, *, task_id: str | None = None) -> dict:
    nodes = list_nodes(bb, project_id, task_id=task_id)
    ids = {n["id"] for n in nodes}
    edges = [e for e in list_edges(bb, project_id)
             if e["from_node_id"] in ids and e["to_node_id"] in ids]
    return {"project_id": project_id, "task_id": task_id, "nodes": nodes, "edges": edges}
