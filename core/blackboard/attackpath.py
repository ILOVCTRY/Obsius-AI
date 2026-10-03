"""单站攻击链路图 v3（website-attack-path-graph，2026-09-24）。

纯函数查询层（仿 core/coverage.py，不 import 编排器）：从 intents + http_history
+ assets + findings 构建「思考→规划→执行→收尾」的逻辑推导图。

主脊：``目标 → 子目标/意图 → 收尾（漏洞 | 发现 | 死路）→ 意图 → 收尾 → …``
——是一条可循环延伸的链：收尾产出的发现/漏洞可再作新意图的派生依据（derive）
继续往下长。布局按依赖层级自动分层（不固定"意图一列、目标一列"）。

节点（nodes）：
- **target**：选定根目标资产（host/domain，单根）；
- **subtarget**（2026-10-01）：根的直接子资产（子域/路径/端口……）——第二层
  「子目标」显式化；带终态徽章（settled=子树意图全部收尾且至少一条 dead_end）；
  意图按资产锚点归属到其所在子目标子树；
- **intent**：intents 行——open/closed；closed vuln/finding 挂收尾发现，
  closed dead_end 是意图的关闭态（默认隐藏 + bypass 穿通边）；
- **finding**：子树内非误报发现——漏洞（category=vuln）/有效发现（intel）。

边（edges，kind）：
- **outcome**：intent → finding（执行产出了什么）；
- **derive**：target/subtarget/finding/intent → intent（后者必须由前者逻辑推出；
  basis_refs 是边数据源；归属子目标的意图由该子目标起边，否则由 target 起边）；
- **bypass**：死路默认隐藏时，其前驱直连后继（穿通被剪掉的枝）；
- **exec**：执行层 attempt 相邻边（展开意图时才看的时间序细节）。

执行层（attempts）：沿用 M1 测试点归一化（method+路径模板+query 键、30min
折叠、五档结果、curl 缺口合成节点）；读时按意图存活时间窗
[intent.created_at, closed_at] 归属，重叠窗归最新声明的意图（执行者焦点）。

全图服务端做 DAG 断言（Kahn）。站点边界 = 目标资产子树，跨任务跨会话不切割。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from core.blackboard.intents import list_intents

# 同测试点折叠时间窗
FOLD_GAP_S = 30 * 60
_PAGE = 1000
_HARD_CAP = 20000

# 执行层结果档（rank=显著度，attempt 取组内最高档）
_SIGNAL_RANK = {"no_reaction": 0, "blocked": 1, "hint": 2}

_ERROR_HINTS = (
    "sql syntax", "syntax error", "warning: mysql", "mysql_fetch",
    "sqlite error", "odbc sql server", "ora-0", "psql:",
    "postgresql error", "unclosed quotation", "stack trace",
    "java.lang.", "python traceback", "fatal error:",
)


# ---------- 路径模板化 ----------

def _template_segment(seg: str) -> str:
    if not seg:
        return seg
    if seg.isdigit():
        return "{id}"
    try:
        int(seg, 16)
    except ValueError:
        pass
    else:
        return "{hash}" if len(seg) >= 8 else seg
    if len(seg) == 36 and seg[8] == "-" and seg[13] == "-":  # UUID
        return "{uuid}"
    return seg


def templated_url(url: str) -> tuple[str, tuple[str, ...]]:
    """URL → (路径模板, 排序去重 query 参数名元组)。解析失败退原值。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, ()
    path = parts.path or "/"
    tpl = "/".join(_template_segment(s) for s in path.split("/"))
    try:
        qkeys = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    except ValueError:
        qkeys = []
    return tpl, tuple(qkeys)


def attempt_key(method: str | None, url: str) -> tuple[str, str, tuple[str, ...]]:
    tpl, qkeys = templated_url(url)
    return ((method or "GET").upper(), tpl, qkeys)


# ---------- 单请求判档 ----------

def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _reflection(value: str, resp_body: str) -> bool:
    v = value.strip()
    return len(v) >= 3 and v in resp_body


def _request_signal(row: dict) -> str:
    """单请求 → 结果档（found 由 finding 挂接另行覆盖）。"""
    status = row.get("status")
    body = row.get("resp_body") or ""
    if status is None:
        return "blocked"
    if status == 404:
        return "no_reaction"
    if status >= 400:
        return "blocked"
    low = body.lower()
    if any(h in low for h in _ERROR_HINTS):
        return "hint"
    try:
        qvals = [v for _, v in parse_qsl(urlsplit(row.get("url", "")).query,
                                        keep_blank_values=True)]
    except ValueError:
        qvals = []
    if any(_reflection(v, body) for v in qvals):
        return "hint"
    return "hint" if body else "no_reaction"


# ---------- 主入口 ----------

def build_attack_path(bb, project_id: str, target_root_id: str, *,
                      assets: list[dict] | None = None,
                      findings: list[dict] | None = None,
                      history: list[dict] | None = None,
                      intents: list[dict] | None = None) -> dict:
    """单站攻击链路图 v3。

    返回 {target, nodes, edges, attempts, exec_edges, counts}。
    bb 鸭子依赖（list_assets/list_findings/list_http_history）；
    intents 走 intents.list_intents（与写入面同源）；
    各输入可注入快照（测试与跨库复用）。目标不存在 → LookupError。
    """
    if assets is None:
        assets = bb.list_assets(project_id)
    if findings is None:
        findings = bb.list_findings(project_id)
    if intents is None:
        intents = list_intents(bb, project_id)
    by_id = {a["id"]: a for a in assets}
    target = by_id.get(target_root_id)
    if target is None:
        raise LookupError(f"目标资产不存在: {target_root_id}")

    subtree = _subtree_ids(assets, target_root_id)
    host_names = {
        (a.get("value") or "").lower()
        for a in assets
        if a["id"] in subtree and a.get("type") in ("host", "domain")
    }
    host_names.discard("")

    # ---- 执行层：attempt 归一化（M1 口径原样） ----
    site_findings_all = [f for f in findings
                         if f.get("target_asset_id") in subtree]
    key_findings: dict[tuple, list[dict]] = {}
    for f in site_findings_all:
        brief = {"id": f["id"], "title": f.get("title", ""),
                 "severity": f.get("severity", "info"),
                 "status": f.get("status", "unverified"),
                 "category": f.get("category", "vuln")}
        for k in _finding_keys(f, subtree, by_id) or [None]:
            if k is None:
                continue
            key_findings.setdefault(k, []).append(brief)

    matched = _match_history(bb, project_id, host_names, history)
    attempts, active = _normalize_attempts(matched, key_findings)

    # ---- 站点意图 + 收尾发现归属 ----
    site_intents = [it for it in intents if _intent_in_site(it, subtree)]
    intent_by_id = {it["id"]: it for it in site_intents}
    # finding → 首个收尾它的已关闭意图
    finding_owner: dict[str, str] = {}
    for it in sorted(site_intents, key=lambda x: x.get("closed_at") or "z"):
        for fid in it.get("outcome_refs") or []:
            finding_owner.setdefault(fid, it["id"])
    # 证据引用 → 携带它的意图（死路的 evidence_refs 是后继推导依据）
    evidence_owner: dict[str, str] = {}
    for it in site_intents:
        for ref in it.get("evidence_refs") or []:
            evidence_owner.setdefault(ref, it["id"])

    _attribute_attempts(attempts, site_intents, finding_owner)

    # ---- 子目标节点（2026-10-01）：根的直接子资产 ----
    # 第二层「子目标」显式化——根的直接下级资产（子域/路径/端口等）各自成节点，
    # 意图按资产锚点归属到其所在子目标子树；形成 根 → 子目标/意图 → 收尾 → 意图 → …
    # 子目标自带终态徽章（子树意图全部收尾且至少一条 dead_end → 已测清）。
    direct_children = [a for a in assets if a.get("parent_id") == target_root_id]
    subtarget_ids = {a["id"] for a in direct_children}
    # 每个子目标的子树（含自身），用于意图归属
    subtarget_subtree: dict[str, set] = {
        a["id"]: _subtree_ids(assets, a["id"]) for a in direct_children}
    # 意图 → 子目标（首个命中其子树的子目标；未命中=挂根）
    intent_subtarget: dict[str, str | None] = {}
    for it in site_intents:
        anchors = _anchors_of(it)
        owner = None
        for sid in subtarget_ids:
            if anchors & subtarget_subtree[sid]:
                owner = sid
                break
        intent_subtarget[it["id"]] = owner

    # ---- 逻辑节点 ----
    nodes: list[dict] = [{
        "id": target["id"], "type": "target",
        "label": target.get("value", ""), "asset_type": target.get("type"),
    }]
    for a in direct_children:
        anchored = [it for it in site_intents if intent_subtarget.get(it["id"]) == a["id"]]
        nodes.append({
            "id": a["id"], "type": "subtarget",
            "label": a.get("value", ""), "asset_type": a.get("type"),
            "status": a.get("status", "open"),
            "settled": _subtarget_settled(anchored),
            "findings": sum(
                1 for f in site_findings_all
                if f.get("target_asset_id") in subtarget_subtree[a["id"]]
                and f.get("status") != "false-positive"),
        })
    # 子树非误报发现全部上图（含无主的早期发现）
    graph_findings = [f for f in site_findings_all
                      if f.get("status") != "false-positive"]
    finding_ids = {f["id"] for f in graph_findings}
    req_count_by_intent: dict[str, int] = {}
    for a in attempts:
        iid = a.get("intent_id")
        if iid:
            req_count_by_intent[iid] = \
                req_count_by_intent.get(iid, 0) + a["request_count"]

    for it in site_intents:
        nodes.append(_intent_node(it, req_count_by_intent.get(it["id"], 0)))
    for f in graph_findings:
        nodes.append({
            "id": f["id"], "type": "finding",
            "title": f.get("title", ""),
            "severity": f.get("severity", "info"),
            "status": f.get("status", "unverified"),
            "category": f.get("category", "vuln"),
        })

    # ---- 逻辑边 ----
    edges: list[dict] = []
    edge_keys: set[tuple] = set()

    def add_edge(src: str, dst: str, kind: str) -> None:
        if src == dst:
            return
        key = (src, dst, kind)
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({"source": src, "target": dst, "kind": kind})

    # outcome / 无主发现 → 由 target 起边
    for f in graph_findings:
        owner = finding_owner.get(f["id"])
        if owner and owner in intent_by_id:
            add_edge(owner, f["id"], "outcome")
        else:
            add_edge(target["id"], f["id"], "derive")

    # derive：每个意图的推导前驱。有子目标归属 → 从子目标节点起边；
    # 无 basis 前驱时落点：归属子目标则挂该子目标，否则挂根。
    for it in site_intents:
        fallback = intent_subtarget.get(it["id"]) or target["id"]
        pres = _intent_predecessors(
            it, fallback, finding_owner, evidence_owner,
            intent_by_id, finding_ids, subtree)
        for p in pres:
            add_edge(p, it["id"], "derive")

    # bypass：穿通默认隐藏的死路节点
    hidden = {it["id"] for it in site_intents
              if it.get("status") == "closed"
              and it.get("outcome_type") == "dead_end"}
    bypass = _bypass_edges(edges, hidden)
    for src, dst in bypass:
        add_edge(src, dst, "bypass")

    _assert_dag(nodes, edges)

    # ---- 执行层相邻边（同意图桶内时间相邻） ----
    exec_edges = []
    order = [a["id"] for a in attempts]
    bucket: dict[str | None, str] = {}
    for aid in order:
        a = next(x for x in attempts if x["id"] == aid)
        key = a.get("intent_id")
        prev = bucket.get(key)
        if prev:
            exec_edges.append({"source": prev, "target": aid, "kind": "exec"})
        bucket[key] = aid

    closed = [it for it in site_intents if it.get("status") == "closed"]
    return {
        "target": {"id": target["id"], "type": target.get("type"),
                   "value": target.get("value")},
        "nodes": nodes,
        "edges": edges,
        "attempts": attempts,
        "exec_edges": exec_edges,
        "counts": {
            "subtargets": len(direct_children),
            "intents": len(site_intents),
            "open": sum(1 for it in site_intents if it.get("status") == "open"),
            "closed": len(closed),
            "dead_end": len(hidden),
            "findings": len(graph_findings),
            "total_requests": len(matched),
        },
    }


# ---------- 执行层组装（M1 口径） ----------

def _key_label(key: tuple) -> str:
    q = ("?" + "&".join(key[2])) if len(key) > 2 and key[2] else ""
    return f"{key[0]} {key[1]}{q}"


def _normalize_attempts(matched, key_findings):
    nodes: dict[tuple, dict] = {}
    active: dict[tuple, dict] = {}
    variants: dict[tuple, int] = {}
    for row in matched:
        key = attempt_key(row.get("method"), row.get("url", ""))
        ts = _parse_ts(row.get("created_at"))
        cur = active.get(key)
        if cur is not None and (ts is None or cur["_last_ts"] is None
                                or ts - cur["_last_ts"]
                                <= timedelta(seconds=FOLD_GAP_S)):
            _fold(cur, row, ts)
        else:
            node = _new_attempt(row, ts, key_findings.get(key, []))
            if cur is None:
                nodes[key] = node
            else:
                variants[key] = variants.get(key, 0) + 1
                nodes[(*key, variants[key])] = node
            active[key] = node
    for key, flist in key_findings.items():
        if key in active:
            continue
        non_fp = next((f for f in flist if f["status"] != "false-positive"), None)
        nodes[key] = _synthetic_attempt(key, flist, bool(non_fp))

    attempts = sorted(nodes.values(), key=lambda n: n["_sort"])
    for i, n in enumerate(attempts):
        n["id"] = f"attempt-{i + 1:02d}"
        for k2 in ("_last_ts", "_sort", "_sig"):
            n.pop(k2, None)
    return attempts, active


def _new_attempt(row: dict, ts: datetime | None, flist: list[dict]) -> dict:
    key = attempt_key(row.get("method"), row.get("url", ""))
    non_fp = [f for f in flist if f["status"] != "false-positive"]
    sig = _request_signal(row)
    return {
        "id": "",
        "key": _key_label(key),
        "method": key[0], "path_template": key[1],
        "query_keys": list(key[2]),
        "started_at": row.get("created_at"), "ended_at": row.get("created_at"),
        "request_count": 1,
        "result": "found" if non_fp else sig,
        "status_codes": [] if row.get("status") is None else [row.get("status")],
        "finding_ids": [f["id"] for f in flist],
        "intent_id": None,
        "session_ids": [row["session_id"]] if row.get("session_id") else [],
        "sources": [row["source"]] if row.get("source") else [],
        "representative": _representative(row),
        "_last_ts": ts, "_sig": sig,
        "_sort": (0 if ts is not None else 1, ts or datetime.min, _key_label(key)),
    }


def _fold(node: dict, row: dict, ts: datetime | None) -> None:
    sig = _request_signal(row)
    node["request_count"] += 1
    node["ended_at"] = row.get("created_at")
    node["_last_ts"] = ts
    if row.get("status") is not None:
        node["status_codes"] = sorted(set(node["status_codes"]) | {row["status"]})
    if row.get("session_id") and row["session_id"] not in node["session_ids"]:
        node["session_ids"].append(row["session_id"])
    if row.get("source") and row["source"] not in node["sources"]:
        node["sources"].append(row["source"])
    if node["result"] != "found" and _SIGNAL_RANK[sig] > _SIGNAL_RANK[node["_sig"]]:
        node["result"] = sig
        node["representative"] = _representative(row)
    if node["result"] != "found":
        node["_sig"] = sig if _SIGNAL_RANK[sig] > _SIGNAL_RANK[node["_sig"]] \
            else node["_sig"]


def _synthetic_attempt(key: tuple, flist: list[dict], has_non_fp: bool) -> dict:
    first = non_fp = None
    for f in flist:
        if first is None:
            first = f
        if f["status"] != "false-positive":
            non_fp = f
            break
    return {
        "id": "", "key": _key_label(key),
        "method": key[0], "path_template": key[1],
        "query_keys": list(key[2]),
        "started_at": None, "ended_at": None,
        "request_count": 0,
        "result": "found" if has_non_fp else "no_reaction",
        "status_codes": [], "finding_ids": [f["id"] for f in flist],
        "intent_id": None,
        "session_ids": [], "sources": [],
        "representative": {"title": (non_fp or first)["title"],
                           "note": "结论由命令行探测产出（明细见事件流）"},
        "_last_ts": None, "_sig": "no_reaction",
        "_sort": (1, datetime.max, _key_label(key)),
    }


def _representative(row: dict) -> dict:
    return {
        "method": (row.get("method") or "GET").upper(),
        "url": row.get("url", ""),
        "status": row.get("status"),
        "resp_mime": row.get("resp_mime", ""),
        "snippet": (row.get("resp_body") or "")[:160],
        "history_id": row.get("id"),
    }


def _finding_keys(finding: dict, subtree: set, by_id: dict) -> list[tuple]:
    aid = finding.get("target_asset_id")
    if aid not in subtree:
        return []
    asset = by_id.get(aid, {})
    if asset.get("type") == "url":
        return [attempt_key("GET", asset.get("value", ""))]
    keys: list[tuple] = []
    for req in (finding.get("evidence") or {}).get("requests") or []:
        if isinstance(req, str):
            url, method = req, "GET"
        else:
            url, method = (req or {}).get("url", ""), (req or {}).get("method", "GET")
        if url:
            keys.append(attempt_key(method, url))
    return keys


def _subtree_ids(assets: list[dict], root_id: str) -> set:
    subtree = {root_id}
    while True:
        add = {a["id"] for a in assets
               if a.get("parent_id") in subtree and a["id"] not in subtree}
        if not add:
            return subtree
        subtree |= add


def _match_history(bb, project_id: str, host_names: set,
                   history: list[dict] | None) -> list[dict]:
    if history is None:
        rows: list[dict] = []
        since = 0
        while True:
            page = bb.list_http_history(project_id, since_id=since, limit=_PAGE)
            if not page:
                break
            rows.extend(page)
            since = page[-1]["id"]
            if len(rows) >= _HARD_CAP:
                break
    else:
        rows = history

    def _host_of(url: str) -> str:
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""

    return [r for r in rows if _host_of(r.get("url", "")) in host_names]


# ---------- v3 意图归属 / 逻辑推导 ----------

def _intent_in_site(intent: dict, subtree: set) -> bool:
    """意图与本站相关：目标在子树，或推导依据命中子树资产/发现。"""
    tgt = intent.get("target_asset_id")
    if tgt and tgt in subtree:
        return True
    for ref in intent.get("basis_refs") or []:
        kind, _, rid = _split_ref(ref)
        if kind in ("asset", "finding") and rid in subtree:
            return True
    return False


def _anchors_of(intent: dict) -> set:
    """意图的资产锚点集合：target_asset_id ∪ basis_refs 的 asset:<id>
    （与 intents._intent_anchors 同口径，用于子目标归属）。"""
    out: set = set()
    tgt = intent.get("target_asset_id")
    if tgt:
        out.add(tgt)
    for ref in intent.get("basis_refs") or []:
        kind, _, rid = _split_ref(ref)
        if kind == "asset" and rid:
            out.add(rid)
    return out


def _subtarget_settled(anchored_intents: list[dict]) -> bool:
    """子目标是否已收口：其名下意图**全部收尾**且至少一条 dead_end
    （与 store/intents 的 tested_clean 背书同口径；无意图=未收口）。"""
    if not anchored_intents:
        return False
    if any(it.get("status") == "open" for it in anchored_intents):
        return False
    return any(it.get("status") == "closed"
               and it.get("outcome_type") == "dead_end"
               for it in anchored_intents)


def _split_ref(ref: str) -> tuple[str, str, str]:
    kind, _, rid = ref.partition(":")
    return kind, kind, rid


def _attribute_attempts(attempts: list[dict], intents: list[dict],
                        finding_owner: dict[str, str]) -> None:
    """读时归属：attempt 起始时间落在意图存活窗 [created, closed] 内则归之，
    重叠窗归最新声明的意图（执行者焦点）；无时间的合成节点按收尾发现归属。"""
    windows = []
    for it in intents:
        lo = _parse_ts(it.get("created_at"))
        hi = _parse_ts(it.get("closed_at"))
        if lo is not None:
            windows.append((lo, hi, it["id"]))
    windows.sort(key=lambda w: w[0])
    for a in attempts:
        ts = _parse_ts(a.get("started_at"))
        if ts is None:
            owner = next((finding_owner[fid] for fid in a["finding_ids"]
                          if fid in finding_owner), None)
            a["intent_id"] = owner
            continue
        match = None
        for lo, hi, iid in windows:
            if lo <= ts and (hi is None or ts <= hi):
                match = iid  # 不 break：取窗内最新声明的意图
        a["intent_id"] = match


def _intent_node(intent: dict, request_count: int) -> dict:
    outcome = intent.get("outcome_type") or ""
    return {
        "id": intent["id"], "type": "intent",
        "statement": intent.get("statement", ""),
        "status": intent.get("status", "open"),
        "outcome": outcome,
        "finding_ids": list(intent.get("outcome_refs") or []),
        "dead_reason": intent.get("dead_reason", ""),
        "created_at": intent.get("created_at"),
        "closed_at": intent.get("closed_at"),
        "request_count": request_count,
        # 死路默认隐藏（穿通边 bypass；显示开关在前端）
        "default_hidden": intent.get("status") == "closed"
                          and outcome == "dead_end",
    }


def _intent_predecessors(intent, target_id, finding_owner, evidence_owner,
                         intent_by_id, finding_ids, subtree) -> list[str]:
    refs = intent.get("basis_refs") or []
    if not refs:
        return [target_id]
    pres: list[str] = []
    for ref in refs:
        kind, _, rid = _split_ref(ref)
        if kind == "finding":
            if rid not in finding_ids:
                continue  # 跨站依据不进单站图
            owner = finding_owner.get(rid)
            pres.append(owner if owner in intent_by_id else target_id)
        elif kind == "asset":
            if rid in subtree:
                pres.append(target_id)
        else:  # http/event/artifact：死路证据可作后继推导依据
            owner = evidence_owner.get(ref)
            pres.append(owner if owner in intent_by_id else target_id)
    if not pres:
        pres = [target_id]
    return list(dict.fromkeys(pres))


def _bypass_edges(edges, hidden) -> list[tuple[str, str]]:
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


def _assert_dag(nodes, edges) -> None:
    """服务端无环断言（Kahn）：bypass 是 DAG 上的路径复合，不参与判定。"""
    ids = {n["id"] for n in nodes}
    adj: dict[str, set] = {i: set() for i in ids}
    indeg = {i: 0 for i in ids}
    for e in edges:
        if e["kind"] == "bypass":
            continue
        s, t = e["source"], e["target"]
        if s not in ids or t not in ids:
            raise AssertionError(f"链路边端点不在节点集: {s} → {t}")
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
        raise AssertionError("攻击链路图出现环（逻辑推导必须是 DAG）")
