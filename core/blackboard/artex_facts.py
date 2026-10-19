"""ARTEX 探索事实写回（2026-10-19，artex-record-fact）。

对齐上游 ``agent/tools.go:1353`` ``recordFact`` / ``recordOneFact``：

- **一次探索汇成一条事实**，不拆碎——``summary`` 是结论的一句话概括，
  ``detail`` 承载该次探索的全部具体观察（多个项写在同一段里）。上游原文
  「拆太碎会让图谱无限膨胀」，因此写入侧按 ``summary`` 指纹做**意图内幂等**：
  同一意图重复写同一结论只更新不新增。
- **``evidence`` 只写一行关键证据**（命令 + 最能证明结论的一两行输出），
  细节已在 ``detail``，这里再粘大段输出等于把 fact 变回日志。
- **``confidence`` 区分「直接看到」与「据现象推断」**（``observed`` /
  ``inferred``）。否定结论（端口关闭 / 参数不可注入 / 未发现入口）必须如实
  标 ``inferred``——除非确已穷尽手段且直接看到。
- **批量 ``facts[]``**：一次写多条**彼此不同**的结论时用，避免多轮往返。

为什么必须有它（本项目此前的最大语义损失）：

此前事实由 ``record_intent_result`` 在 Worker **收尾时**把整段 result 物化成
**一条** ``f"{intent_id}:fact"`` 节点，``confidence`` 由状态硬给（completed=1.0）。
后果有四条：①否定结论无法落图——「8080 关闭」只存在于一段散文里，下次规划
读不到「这是一条可检索的 observed 事实」；②中间观察丢失——一次 work 探了 5 个
端点只留一条 blob；③``confidence`` 语义失真——1.0 表示「跑完了」而不是「直接
看到」，而覆盖度分子（``artex_coverage.tested_asset_ids`` 只认 ``kind='fact'``）
与目标证据硬闸都建立在这条 fact 上；④事实不可去重合并。

本模块与 ``record_intent_result`` 的分工：

- Worker **过程中**显式写 → ``record_fact``（``metadata.source='worker'``）；
- 整轮跑完**没有任何显式事实**时 → ``record_intent_result`` 仍落一条兜底
  汇总事实（``metadata.source='intent_result'``），保证「跑过就有事实」的
  覆盖度下限语义不破——但一旦 Worker 自己写了事实，就不再叠加兜底节点。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from core.blackboard.artex_exploration import (
    _hydrate,
    add_edge,
    create_node,
    get_node,
    list_nodes,
    upsert_node,
)
from core.blackboard.store import now

log = logging.getLogger(__name__)

# ---------- confidence ----------

CONFIDENCE_OBSERVED = "observed"
CONFIDENCE_INFERRED = "inferred"
CONFIDENCE_LEVELS = (CONFIDENCE_OBSERVED, CONFIDENCE_INFERRED)

# 落到 ``artex_exploration_nodes.confidence``（REAL）的分值。
# 与 ``record_intent_result`` 的历史口径保持可比：completed 曾是 1.0、其余 0.5。
_CONFIDENCE_SCORE = {CONFIDENCE_OBSERVED: 1.0, CONFIDENCE_INFERRED: 0.5}

# 常见同义写法 → 标准枚举。模型经常按中文或近似词作答，硬拒会白烧一轮。
_CONFIDENCE_ALIASES = {
    "observed": CONFIDENCE_OBSERVED,
    "observe": CONFIDENCE_OBSERVED,
    "direct": CONFIDENCE_OBSERVED,
    "seen": CONFIDENCE_OBSERVED,
    "confirmed": CONFIDENCE_OBSERVED,
    "直接看到": CONFIDENCE_OBSERVED,
    "直接观察": CONFIDENCE_OBSERVED,
    "实测": CONFIDENCE_OBSERVED,
    "确认": CONFIDENCE_OBSERVED,
    "inferred": CONFIDENCE_INFERRED,
    "infer": CONFIDENCE_INFERRED,
    "inference": CONFIDENCE_INFERRED,
    "guess": CONFIDENCE_INFERRED,
    "推断": CONFIDENCE_INFERRED,
    "推测": CONFIDENCE_INFERRED,
    "猜测": CONFIDENCE_INFERRED,
    "疑似": CONFIDENCE_INFERRED,
}


def normalize_confidence(raw: Any) -> str:
    """把入参归一成 ``observed`` / ``inferred``；无法判定时保守取 ``inferred``。

    保守方向的理由与上游一致：否定结论误标 observed 会让规划者以为「该方向
    已穷尽」从而永久跳过——代价远大于把一条真观察降级成推断。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return CONFIDENCE_INFERRED
    if text in _CONFIDENCE_ALIASES:
        return _CONFIDENCE_ALIASES[text]
    for key, level in _CONFIDENCE_ALIASES.items():
        if key in text:
            return level
    return CONFIDENCE_INFERRED


def confidence_score(level: str) -> float:
    return _CONFIDENCE_SCORE.get(str(level or ""), 0.5)


# ---------- 指纹 / 去重 ----------

# 与 Planner Intent 去重同口径：去空白与常见标点后小写。
_FP_STRIP = re.compile(r"[\s，。；、,.;:：!！?？\-—_/\\（）()【】\[\]\"'`]+")


def fact_fingerprint(summary: str) -> str:
    return _FP_STRIP.sub("", str(summary or "")).lower()


def _fact_node_id(intent_id: str, summary: str) -> str:
    """意图内确定性节点 id：同意图同结论重复写只更新（幂等）。"""
    fp = fact_fingerprint(summary)
    digest = hashlib.sha1(fp.encode("utf-8")).hexdigest()[:10]
    return f"{intent_id}:fact:{digest}"


# 单意图事实数软上限：超过只在返回文案里提醒合并，不阻断写入
# （收尾阶段宁可多写一条也不该因为配额丢掉已识别的结论）。
FACT_SOFT_CAP = 40

MAX_SUMMARY = 500
MAX_DETAIL = 8000
MAX_EVIDENCE = 1200


# ---------- 写入 ----------

def _ensure_intent_node(bb, project_id: str, task_id: str, intent: dict) -> str:
    """确保意图的探索图节点存在（fact 的 yields 边要挂在它上面）。"""
    intent_id = str(intent.get("id") or "").strip()
    node_id = f"{intent_id}:explore"
    if get_node(bb, project_id, node_id) is None:
        create_node(bb, project_id, "intent", node_id=node_id,
                    title=str(intent.get("objective") or ""),
                    content=str(intent.get("objective") or ""),
                    task_id=task_id, intent_id=intent_id,
                    status=str(intent.get("status") or "open"),
                    asset_ids=intent.get("asset_ids") or [])
    return node_id


def _load_intent(bb, project_id: str, intent_id: str) -> dict | None:
    row = bb.conn.execute(
        "SELECT * FROM artex_intents WHERE project_id=? AND id=?",
        (project_id, intent_id),
    ).fetchone()
    if row is None:
        return None
    from core.blackboard.store import _row_to_dict
    item = _row_to_dict(row) or {}
    try:
        item["asset_ids"] = json.loads(item.get("asset_ids") or "[]")
    except (TypeError, ValueError):
        item["asset_ids"] = []
    return item


def record_fact(bb, project_id: str, *, task_id: str, intent_id: str,
                summary: str, detail: str = "", evidence: str = "",
                confidence: Any = None, asset_ids=None,
                metadata: dict | None = None,
                author: str = "worker") -> dict:
    """写一条事实到探索图，连到产生它的意图。

    参数语义照抄上游 ``factItem``；额外约束：

    - ``intent_id`` 必须是本任务的意图（跨任务写入会污染覆盖度与目标证据链）；
    - ``asset_ids`` 缺省继承意图的锚定资产——**这是覆盖度分子能算对的前提**
      （``tested_asset_ids`` 只数 fact 节点上的资产）；
    - ``summary`` 指纹在意图内幂等，重复写同结论只更新内容。

    返回 ``{"fact": <node>, "created": bool}``；校验失败抛 ``ValueError``。
    """
    intent_id = str(intent_id or "").strip()
    summary = str(summary or "").strip()
    if not summary:
        raise ValueError("summary 不能为空（用一句话概括本次探索的结论）")
    if not intent_id:
        raise ValueError("intent_id 必填（这条事实由哪次探索产生）")
    task_id = str(task_id or "").strip()
    intent = _load_intent(bb, project_id, intent_id)
    if intent is None:
        raise ValueError(f"intent_id {intent_id} 不存在于本项目")
    intent_task = str(intent.get("task_id") or "")
    # 只有**两边都非空且不一致**才算跨任务：手工建树/历史 Intent 的 task_id
    # 可能是空串（未挂任务），此时按调用方传入的 task_id 归属，不硬拒。
    if task_id and intent_task and intent_task != task_id:
        raise ValueError(f"intent_id {intent_id} 不属于任务 {task_id}（跨任务写入会污染覆盖度）")
    task_id = task_id or intent_task

    level = normalize_confidence(confidence)
    assets = [str(x).strip() for x in (asset_ids or []) if str(x).strip()]
    if not assets:
        assets = [str(x).strip() for x in (intent.get("asset_ids") or [])
                  if str(x).strip()]

    node_id = _fact_node_id(intent_id, summary)
    existing = get_node(bb, project_id, node_id)
    meta = {
        "source": "worker",
        "author": str(author or "worker"),
        "confidence_level": level,
        "summary": summary[:MAX_SUMMARY],
    }
    if evidence:
        meta["evidence"] = str(evidence).strip()[:MAX_EVIDENCE]
    if detail:
        meta["detail"] = str(detail).strip()[:MAX_DETAIL]
    if metadata:
        meta.update({k: v for k, v in metadata.items() if k not in meta})

    fields = {
        "task_id": task_id,
        "intent_id": intent_id,
        # title 用 summary 而非 intent objective——事实的标题必须是结论本身，
        # 否则前端/概览里一排「Worker 结果：xxx」分不出哪条是哪条。
        "title": summary[:MAX_SUMMARY],
        # content 承载 detail（无 detail 时退回 summary），保持与既有
        # 「fact 的 content 是正文」的读取口径兼容。
        "content": (str(detail).strip() or summary)[:MAX_DETAIL],
        "status": "closed",
        "confidence": confidence_score(level),
        "asset_ids": assets,
        "metadata": meta,
    }
    if existing is None:
        node = create_node(bb, project_id, "fact", node_id=node_id, **fields)
    else:
        node = upsert_node(bb, project_id, node_id, "fact", **fields)

    intent_node_id = _ensure_intent_node(bb, project_id, task_id, intent)
    add_edge(bb, project_id, intent_node_id, node_id, "yields",
             metadata={"source": "record_fact", "confidence": level})

    # 目标证据挂链（与 record_intent_result 同口径）：事实落库即按资产重叠 /
    # 漏洞类精确命中挂到相关目标，作为 prove 的系统关联依据。
    if task_id:
        try:
            from core.blackboard.artex_goals import link_evidence_to_goals
            link_evidence_to_goals(bb=bb, project_id=project_id, task_id=task_id,
                                   evidence_id=node_id,
                                   reason=f"Worker 记录事实 {intent_id}")
        except Exception:  # noqa: BLE001 —— 挂链失败不影响事实落库
            log.exception("ARTEX 事实挂目标失败 intent=%s", intent_id)

    bb.append_event(project_id, "artex.exploration.fact.recorded",
                    {"intent_id": intent_id, "task_id": task_id,
                     "fact_id": node_id, "finding_ids": [],
                     "confidence": level, "created": existing is None,
                     "source": "record_fact"})
    return {"fact": node, "created": existing is None}


def record_facts(bb, project_id: str, *, task_id: str, intent_id: str,
                 facts: list[dict], author: str = "worker") -> dict:
    """批量写多条**彼此不同**的结论；逐条独立成败，不因一条失败整体回滚。

    返回的 ``ids`` 与入参 ``facts`` **等长同序**（失败位为空串），与上游
    「返回 ids 数组，与 facts 等长同序」的契约一致——模型靠位置对应结果。
    """
    items = list(facts or [])
    ids: list[str] = ["" for _ in items]
    errors: dict[str, str] = {}
    created = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors[str(index)] = "每条事实必须是对象"
            continue
        try:
            result = record_fact(
                bb, project_id,
                task_id=task_id,
                intent_id=str(item.get("intent_id") or intent_id or ""),
                summary=str(item.get("summary") or ""),
                detail=str(item.get("detail") or ""),
                evidence=str(item.get("evidence") or ""),
                confidence=item.get("confidence"),
                asset_ids=item.get("asset_ids"),
                author=author)
        except (ValueError, TypeError) as exc:
            errors[str(index)] = str(exc)
            continue
        ids[index] = str((result.get("fact") or {}).get("id") or "")
        created += 1 if result.get("created") else 0
    return {"ids": ids, "created": created, "errors": errors}


# ---------- 读取 ----------

def _fact_view(node: dict, *, detail_limit: int = 0) -> dict:
    meta = node.get("metadata") or {}
    level = str(meta.get("confidence_level") or "")
    if level not in CONFIDENCE_LEVELS:
        # 兜底汇总事实（record_intent_result 落的）没有 level，用数值反推。
        level = CONFIDENCE_OBSERVED if float(node.get("confidence") or 0) >= 1.0 \
            else CONFIDENCE_INFERRED
    content = str(node.get("content") or "")
    item = {
        "id": str(node.get("id") or ""),
        "summary": str(meta.get("summary") or node.get("title") or ""),
        "evidence": str(meta.get("evidence") or ""),
        "confidence": level,
        "from_intent": str(node.get("intent_id") or ""),
        "asset_ids": list(node.get("asset_ids") or []),
        "created_at": str(node.get("created_at") or ""),
        "source": str(meta.get("source") or ""),
    }
    if detail_limit > 0:
        truncated = len(content) > detail_limit
        item["detail"] = content[:detail_limit]
        if truncated:
            item["detail_truncated"] = True
    else:
        item["detail"] = content
    return item


def list_facts(bb, project_id: str, task_id: str, *, intent_id: str | None = None,
               limit: int = 20, offset: int = 0,
               detail_limit: int = 0) -> dict:
    """列任务（或单意图）的事实，带 ``from_intent`` 血缘与 confidence。

    返回 ``{"facts": [...], "total": n, "has_more": bool}``。上游 ``list_facts``
    的等价物——planner / 主控据此按需翻事实库（Worker 不给这个工具，它的
    事实来源是自己的探索）。

    排序用 SQLite 隐式 ``rowid DESC`` 而不是 ``created_at``：时间戳只到秒，
    同一秒内连写多条事实时按时间排会退化成按 id 字典序（哈希 id，语义混乱）。
    ``rowid`` 就是插入顺序，是这里唯一可靠的「新近」判据。
    """
    from core.blackboard.store import _row_to_dict

    task_id = str(task_id or "").strip()
    clauses = ["project_id=?", "task_id=?", "kind='fact'"]
    args: list[Any] = [project_id, task_id]
    if intent_id:
        clauses.append("intent_id=?")
        args.append(str(intent_id).strip())
    where = " AND ".join(clauses)
    total = int(bb.conn.execute(
        f"SELECT COUNT(*) FROM artex_exploration_nodes WHERE {where}", args
    ).fetchone()[0] or 0)
    limit = max(1, min(int(limit or 20), 200))
    offset = max(0, int(offset or 0))
    rows = bb.conn.execute(
        f"SELECT * FROM artex_exploration_nodes WHERE {where} "
        "ORDER BY rowid DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    facts = [_fact_view(_hydrate(_row_to_dict(r) or {}), detail_limit=detail_limit)
             for r in rows]
    return {"facts": facts, "total": total,
            "has_more": offset + len(facts) < total}


def count_worker_facts(bb, project_id: str, task_id: str,
                       intent_id: str) -> int:
    """该意图由 Worker **显式**写下的事实条数（决定是否还要落兜底汇总事实）。"""
    intent_id = str(intent_id or "").strip()
    if not intent_id:
        return 0
    nodes = list_nodes(bb, project_id, task_id=str(task_id or ""), kind="fact")
    return sum(1 for n in nodes
               if str(n.get("intent_id") or "") == intent_id
               and str((n.get("metadata") or {}).get("source") or "") == "worker")


# ---------- 工具面 ----------

RECORD_FACT_TOOL_NAME = "record_fact"

RECORD_FACT_TOOL_SPEC = {
    "name": RECORD_FACT_TOOL_NAME,
    "description": (
        "把探索【事实/结论】写入探索图，连到产生它的意图（intent_id）。用于记录探索结果"
        "——包括指纹/枚举等【正向结论】，和「端口关闭」/「参数不可注入」/「未发现登录入口」"
        "等【否定结论】。\n"
        "⚠️ 一次探索的多个观察要【汇总成一条事实】，不要拆成多条：summary=对本次结论的"
        "总结性一句话，detail=相关细节（可含多个具体项）。例：指纹意图→一条事实 "
        "{summary:'识别了 X 站点的技术栈与响应特征', detail:'nginx 1.25 / Vue3 / 200 / "
        "title=.. / body_len=..'}，而不是状态码、指纹、标题各记一条。一条意图通常只产出"
        "一条事实，拆太碎会让图谱无限膨胀。\n"
        "★ facts 数组用于一次写多条【彼此不同】的结论（每条可省略 intent_id，默认用顶层 "
        "intent_id）。返回 ids 数组，与 facts 等长同序。\n"
        "⚠️ 只写你在工具输出里【真实看到】的结论，不要脑补。evidence 与 confidence 用来"
        "防止不准确的结论污染图谱：\n"
        "  · evidence=支撑本结论的【一行】关键证据（命令 + 最能证明的那一两行输出），"
        "**务必简洁**——细节已在 detail，这里不要再粘大段输出。\n"
        "  · confidence=observed（输出里直接看到）| inferred（据现象推断）。\n"
        "  · **否定类结论**（不可注入/端口关闭/未发现入口等）只写「观察 + 试探性读法」"
        "——陈述你实际看到什么，方向是否放弃由规划者综合全局定；务必给 evidence，"
        "手段没穷尽或证据弱（含只探一次、看起来像）标 inferred，确已穷尽且直接看到才标 "
        "observed。\n"
        "  · asset_ids 可省略（默认继承本意图的锚定资产）；跨资产结论才需要显式给。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "description": ("【有多条不同结论时用】事实数组，元素字段同下方顶层字段"
                                "（summary/detail/evidence/confidence/intent_id/asset_ids）；"
                                "省略 intent_id 则用顶层 intent_id。返回 ids 与本数组等长、同序。"),
                "items": {"type": "object"},
            },
            "summary": {"type": "string",
                        "description": "对本次探索结论的【总结性一句话】（是对 detail 的概括）"},
            "intent_id": {"type": "string",
                          "description": "产生本事实的意图 id（你领到的意图；批量时作为各条默认）"},
            "detail": {"type": "string",
                       "description": "本事实的相关细节：把这次探索的多个观察事实都写进这里"},
            "evidence": {"type": "string",
                         "description": ("【一行】关键证据：命令 + 最能证明结论的那一两行输出。"
                                         "务必简洁，不要粘大段输出（细节放 detail）。")},
            "confidence": {"type": "string",
                           "enum": list(CONFIDENCE_LEVELS),
                           "description": ("observed（输出里直接看到）| inferred（据现象推断）。"
                                           "否定结论务必如实标注。")},
            "asset_ids": {"type": "array", "items": {"type": "string"},
                          "description": "相关资产 id（可选）：该事实涉及哪些资产"},
        },
        "required": ["summary"],
    },
}


def dispatch_record_fact(bb, project_id: str, task_id: str, intent_id: str,
                         args: dict, *, author: str = "worker") -> str:
    """执行 ``record_fact`` 工具调用，返回给模型的文本结果。

    失败一律以 ``[错误]`` 前缀返回（本仓工具失败文本约定），绝不抛给循环。
    """
    args = args or {}
    raw_batch = args.get("facts")
    top_intent = str(args.get("intent_id") or intent_id or "").strip()
    try:
        if isinstance(raw_batch, list) and raw_batch:
            result = record_facts(
                bb, project_id, task_id=task_id, intent_id=top_intent,
                facts=raw_batch, author=author)
            ids = [i for i in result["ids"] if i]
            if not ids and result["errors"]:
                detail = "；".join(f"#{k} {v}" for k, v in
                                   list(result["errors"].items())[:3])
                return f"[错误] 批量写事实全部失败：{detail}"
            lines = [f"已记录 {len(ids)} 条事实：" + "，".join(ids)]
            if result["errors"]:
                lines.append("失败条目：" + "；".join(
                    f"#{k} {v}" for k, v in result["errors"].items()))
            lines.append("这些事实已进入探索图（规划者会据此推进），"
                         "无需再重复写同一条结论。")
            return "\n".join(lines)
        single = record_fact(
            bb, project_id, task_id=task_id, intent_id=top_intent,
            summary=str(args.get("summary") or ""),
            detail=str(args.get("detail") or ""),
            evidence=str(args.get("evidence") or ""),
            confidence=args.get("confidence"),
            asset_ids=args.get("asset_ids"),
            author=author)
    except (ValueError, TypeError) as exc:
        return f"[错误] 记录事实失败：{exc}"
    except Exception as exc:  # noqa: BLE001 —— 写图异常不得炸整轮
        log.exception("record_fact 失败 project=%s intent=%s", project_id, top_intent)
        return f"[错误] 记录事实异常：{exc}"
    node = single.get("fact") or {}
    level = str((node.get("metadata") or {}).get("confidence_level") or "")
    note = "已更新（同结论重复写按幂等处理）" if not single.get("created") else "已记录"
    total = count_worker_facts(bb, project_id, task_id, top_intent)
    tail = (f"｜本意图已有 {total} 条事实，注意合并同类结论、别把一次探索拆太碎"
            if total > FACT_SOFT_CAP else "")
    return (f"[ARTEX] {note}事实 {node.get('id')}"
            f"（confidence={level or 'inferred'}）{tail}")
