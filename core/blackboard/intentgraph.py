"""项目级意图推导图（逆向轨，2026-10-13）。

纯函数只读层（仿 `attackpath.py`，不 import 编排器）：把 intents + assets +
findings 摊成一张**单向**的「样本 → 意图 → 收尾（发现 | 死路）」推导图，供逆向轨
「意图」subnav 的链路视角查看——列表视图看单条，链路视图看推导关系与续链。

与 `attackpath.py` 的差别（逆向轨口径）：

- **项目级而非单站**：根不是某个 host/domain，而是项目里的 binary 样本；一个项目
  一张图（与「意图」列表同为项目级口径，不分样本）。
- **无子目标层**：binary 资产可以互相挂 parent（样本嵌套，实测有），但不做
  「子目标」语义——嵌套样本各自成节点，不套一层 sha256 标签的中间节点。
- **无执行层**：渗透的 attempts 来自 http_history 按站点 host 名匹配；逆向轨资产
  是 binary，匹配集恒空，故本图只产逻辑层（derive / outcome / bypass）。

节点（nodes）：

- **sample**：binary 资产（样本）；意图/发现锚在非 binary 资产上时，沿 parent 上溯
  到最近的 binary 作其样本，上溯不到则退回该资产链顶（`asset_type` 随节点带出，
  前端据此标注）；完全无锚点的意图/发现落到「未归属样本」兜底节点（有需要才建）。
- **intent**：intents 行——open/closed；closed vuln/finding 挂收尾发现，
  closed dead_end 是意图的关闭态（`default_hidden`，前端默认隐藏 + bypass 穿通）。
- **finding**：非误报发现（含无主的早期发现）。

边（edges，kind）：

- **outcome**：intent → finding（这条意图产出了什么）；
- **derive**：sample/finding/intent → intent（后者必须由前者逻辑推出，数据源
  `basis_refs`）；sample → finding（无主发现直接挂样本）；
- **bypass**：死路默认隐藏时，其前驱直连后继（穿通被剪掉的枝）。

全图服务端做 DAG 断言（Kahn，口径同 attackpath：bypass 是路径复合不参与判定）。
"""

from __future__ import annotations

from core.blackboard.intents import list_intents
from core.blackboard.store import _loads

#: 兜底样本节点：意图/发现没有任何可归资产锚点时挂这里（有需要才建）
UNATTACHED_ID = "__unattached__"
UNATTACHED_LABEL = "未归属样本"


# ---------- 主入口 ----------

def build_intent_graph(bb, project_id: str, *,
                       assets: list[dict] | None = None,
                       findings: list[dict] | None = None,
                       intents: list[dict] | None = None) -> dict:
    """项目级意图推导图。

    返回 ``{nodes, edges, counts}``。bb 鸭子依赖（list_assets/list_findings）；
    intents 走 `intents.list_intents`（与写入面同源）；各输入可注入快照
    （测试与跨库复用）。**纯读**，不写任何状态。
    """
    if assets is None:
        assets = bb.list_assets(project_id)
    if findings is None:
        findings = bb.list_findings(project_id)
    if intents is None:
        intents = list_intents(bb, project_id)

    by_id = {a["id"]: a for a in assets}

    def sample_of(asset_id: str | None) -> str | None:
        """资产 → 所属样本节点 id：沿 parent 上溯找最近的 binary；找不到则退回链顶
        资产（非 binary 锚点如 host/domain 也有自己的节点，不静默丢弃）。"""
        if not asset_id:
            return None
        chain: list[str] = []
        seen: set[str] = set()
        cur: str | None = asset_id
        while cur and cur not in seen:
            seen.add(cur)
            a = by_id.get(cur)
            if a is None:
                break
            chain.append(cur)
            if a.get("type") == "binary":
                return cur
            cur = a.get("parent_id")
        return chain[-1] if chain else None

    # ---- 归属：意图/发现 → 样本 ----
    intent_sample: dict[str, str | None] = {}
    for it in intents:
        sid = None
        for aid in _anchor_ids(it):
            sid = sample_of(aid)
            if sid:
                break
        intent_sample[it["id"]] = sid

    graph_findings = [f for f in findings
                      if f.get("status") != "false-positive"]
    finding_ids = {f["id"] for f in graph_findings}
    finding_sample: dict[str, str | None] = {
        f["id"]: sample_of(f.get("target_asset_id")) for f in graph_findings}

    # ---- 兜底节点（按需建）----
    needs_unattached = any(v is None for v in intent_sample.values()) \
        or any(v is None for v in finding_sample.values())
    if needs_unattached:
        for it in intents:
            if intent_sample[it["id"]] is None:
                intent_sample[it["id"]] = UNATTACHED_ID
        for f in graph_findings:
            if finding_sample[f["id"]] is None:
                finding_sample[f["id"]] = UNATTACHED_ID

    # ---- 节点：样本层（全部 binary + 被用到的锚点资产）----
    nodes: list[dict] = []
    sample_ids: list[str] = []
    for a in assets:
        if a.get("type") == "binary":
            sample_ids.append(a["id"])
    for sid in list(intent_sample.values()) + list(finding_sample.values()):
        if sid and sid not in sample_ids:
            sample_ids.append(sid)

    for sid in sample_ids:
        if sid == UNATTACHED_ID:
            nodes.append({"id": sid, "type": "sample",
                          "label": UNATTACHED_LABEL, "asset_type": ""})
            continue
        a = by_id.get(sid) or {}
        nodes.append({
            "id": sid, "type": "sample",
            "label": _sample_label(a),
            "asset_type": a.get("type", ""),
            "value": a.get("value", ""),
        })

    # ---- 节点：意图层 + 发现层 ----
    for it in intents:
        outcome = it.get("outcome_type") or ""
        nodes.append({
            "id": it["id"], "type": "intent",
            "statement": it.get("statement", ""),
            "status": it.get("status", "open"),
            "outcome": outcome,
            "finding_ids": list(it.get("outcome_refs") or []),
            "dead_reason": it.get("dead_reason", ""),
            "dimension": it.get("dimension", ""),
            "author": it.get("author", ""),
            "created_at": it.get("created_at"),
            "closed_at": it.get("closed_at"),
            "sample_id": intent_sample[it["id"]],
            # 死路默认隐藏（前端穿通开关；bypass 边服务端预复合）
            "default_hidden": it.get("status") == "closed" and outcome == "dead_end",
        })
    for f in graph_findings:
        nodes.append({
            "id": f["id"], "type": "finding",
            "title": f.get("title", ""),
            "severity": f.get("severity", "info"),
            "status": f.get("status", "unverified"),
            "category": f.get("category", "vuln"),
            "vuln_class": f.get("vuln_class", ""),
            "sample_id": finding_sample[f["id"]],
        })

    # ---- 边 ----
    edges: list[dict] = []
    edge_keys: set[tuple] = set()

    def add_edge(src: str | None, dst: str, kind: str) -> None:
        if not src or src == dst:
            return
        key = (src, dst, kind)
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({"source": src, "target": dst, "kind": kind})

    intent_by_id = {it["id"]: it for it in intents}
    # finding → 首个收尾它的已关闭意图
    finding_owner: dict[str, str] = {}
    for it in sorted((i for i in intents if i.get("status") == "closed"),
                     key=lambda x: x.get("closed_at") or "z"):
        for fid in it.get("outcome_refs") or []:
            finding_owner.setdefault(fid, it["id"])
    # 证据引用 → 携带它的意图（死路的 evidence_refs 是后继推导依据）
    evidence_owner: dict[str, str] = {}
    for it in intents:
        for ref in it.get("evidence_refs") or []:
            evidence_owner.setdefault(ref, it["id"])

    # outcome / 无主发现 → 由样本起边
    for f in graph_findings:
        owner = finding_owner.get(f["id"])
        if owner and owner in intent_by_id:
            add_edge(owner, f["id"], "outcome")
        else:
            add_edge(finding_sample[f["id"]], f["id"], "derive")

    # derive：每条意图的推导前驱（无 basis / 依据落空 → 落所属样本）
    for it in intents:
        fallback = intent_sample[it["id"]]
        pres = _intent_predecessors(
            it, fallback, finding_owner, evidence_owner, intent_by_id,
            finding_ids)
        for p in pres:
            add_edge(p, it["id"], "derive")

    # bypass：穿通默认隐藏的死路节点
    hidden = {it["id"] for it in intents
              if it.get("status") == "closed"
              and (it.get("outcome_type") or "") == "dead_end"}
    for src, dst in _bypass_edges(edges, hidden):
        add_edge(src, dst, "bypass")

    _assert_dag(nodes, edges)

    closed = [it for it in intents if it.get("status") == "closed"]
    return {
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "samples": len(sample_ids),
            "intents": len(intents),
            "open": sum(1 for it in intents if it.get("status") == "open"),
            "closed": len(closed),
            "dead_end": len(hidden),
            "findings": len(graph_findings),
        },
    }


# ---------- 归属 / 推导 ----------

def _sample_label(asset: dict) -> str:
    """样本节点标签：binary 优先用样本文件名（meta.filename），退回 sha 前 12；
    非 binary 锚点资产直接用其值。meta 可能是 dict（bb.list_assets 已 hydrate）
    也可能是原始 JSON 串（注入快照），两种都吃。"""
    meta = asset.get("meta") or {}
    if not isinstance(meta, dict):
        meta = _loads(meta, {}) or {}
    name = meta.get("filename")
    if isinstance(name, str) and name.strip():
        return name.strip()
    value = asset.get("value") or ""
    if asset.get("type") == "binary":
        return f"{value[:12]}…" if len(value) > 12 else value
    return value


def _anchor_ids(intent: dict) -> list[str]:
    """意图的资产锚点（有序）：target_asset_id 优先，再 basis_refs 的 asset:<id>。
    与 `intents._intent_anchors` 同口径，但保留顺序以便确定性归属。"""
    out: list[str] = []
    tgt = intent.get("target_asset_id")
    if tgt:
        out.append(tgt)
    for ref in intent.get("basis_refs") or []:
        kind, _, rid = _split_ref(ref)
        if kind == "asset" and rid and rid not in out:
            out.append(rid)
    return out


def _split_ref(ref: str) -> tuple[str, str, str]:
    kind, _, rid = ref.partition(":")
    return kind, kind, rid


def _intent_predecessors(intent: dict, fallback: str | None,
                         finding_owner: dict[str, str],
                         evidence_owner: dict[str, str],
                         intent_by_id: dict[str, dict],
                         finding_ids: set[str]) -> list[str]:
    """意图的推导前驱（边源）。依据落空/自环一律退回 fallback（所属样本），
    保证每个意图至少有一条入边（不产孤点）。"""
    self_id = intent["id"]
    refs = intent.get("basis_refs") or []
    if not refs or not fallback:
        return [fallback] if fallback else []
    pres: list[str] = []
    for ref in refs:
        kind, _, rid = _split_ref(ref)
        if kind == "finding":
            if rid not in finding_ids:
                continue  # 图外依据（误报/已删）不进图
            owner = finding_owner.get(rid)
            pres.append(owner if owner in intent_by_id else fallback)
        elif kind == "asset":
            pres.append(fallback)
        else:  # http/event/artifact：死路证据可作后继推导依据
            owner = evidence_owner.get(ref)
            pres.append(owner if owner in intent_by_id else fallback)
    pres = [p for p in dict.fromkeys(pres) if p and p != self_id]
    return pres or [fallback]


def _bypass_edges(edges: list[dict], hidden: set[str]) -> list[tuple[str, str]]:
    """穿通隐藏死路：对每条 (p → d)、(d → s)（d∈hidden）补 p→s。"""
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    direct = {(e["source"], e["target"]) for e in edges}
    for e in edges:
        if e["target"] in hidden:
            incoming.setdefault(e["target"], []).append(e["source"])
        if e["source"] in hidden:
            outgoing.setdefault(e["source"], []).append(e["target"])
    out: list[tuple[str, str]] = []
    for d in hidden:
        for p in incoming.get(d, []):
            for s in outgoing.get(d, []):
                if p != s and (p, s) not in direct:
                    out.append((p, s))
    return list(dict.fromkeys(out))


def _assert_dag(nodes: list[dict], edges: list[dict]) -> None:
    """服务端无环断言（Kahn）：bypass 是 DAG 上的路径复合，不参与判定。"""
    ids = {n["id"] for n in nodes}
    adj: dict[str, set] = {i: set() for i in ids}
    indeg = {i: 0 for i in ids}
    for e in edges:
        if e["kind"] == "bypass":
            continue
        s, t = e["source"], e["target"]
        if s not in ids or t not in ids:
            raise AssertionError(f"意图链路边端点不在节点集: {s} → {t}")
        if t not in adj[s]:
            adj[s].add(t)
            indeg[t] += 1
    queue = [i for i in ids if indeg[i] == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for t in adj[n]:
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    if seen != len(ids):
        raise AssertionError("意图链路图出现环（逻辑推导必须是 DAG）")
