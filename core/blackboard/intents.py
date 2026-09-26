"""意图写入口（渗透链路图 v3，website-attack-path-graph，2026-09-24）。

意图 = 思考/规划阶段的产物（一句可证伪假设）。纪律：

- **意图必收尾**：open → closed 时 ``outcome_type`` 三选一——
  ``vuln`` / ``finding``（漏洞 / 有效发现，outcome_refs 指向 findings 行）/
  ``dead_end``（死路，dead_reason 死因 + evidence_refs 证据引用）；
- **收尾必带证据**：漏洞/发现收尾引用的 finding 必须存在、同项目、非误报且
  category 匹配；死路必须说明什么证据排除了假设；
- **basis_refs = 推导依据**：「后者必须由前者逻辑上推导出」的边数据源，
  所有引用在写入时做存在性校验（防幻觉，悬空/跨项目一律 ValueError）。

仿 assets.py / traces.py：模块函数经 ``bb._tx()`` 写（仍是黑板单一写入口，
不旁路存储）。事件 intent.declared / intent.closed / intent.reopened。
"""

from __future__ import annotations

import json
import re

from core.blackboard.store import _loads, _row_to_dict, new_id, now

INTENT_STATUSES = ("open", "closed")
INTENT_OUTCOMES = ("vuln", "finding", "dead_end")

# 引用 kind 白名单（basis_refs / evidence_refs）
REF_KINDS = ("asset", "finding", "artifact", "http", "event")
_REF_RE = re.compile(r"^([a-z]+):(\S+)$")
STATEMENT_MAX = 500


# ---------- 引用归一化 + 存在性 ----------

def _parse_ref(ref: object) -> tuple[str, str]:
    if not isinstance(ref, str):
        raise ValueError(f"非法引用: {ref!r}（必须是字符串）")
    m = _REF_RE.match(ref.strip())
    if not m or m.group(1) not in REF_KINDS:
        raise ValueError(
            f"非法引用: {ref}（形如 finding:<id> / asset:<id> / http:<历史id> /"
            f" event:<事件id> / artifact:<id>）")
    return m.group(1), m.group(2)


def _ref_exists(conn, project_id: str, kind: str, ref: str) -> bool:
    if kind in ("asset", "finding", "artifact"):
        table = {"asset": "assets", "finding": "findings",
                 "artifact": "artifacts"}[kind]
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND project_id=?",
            (ref, project_id),
        ).fetchone() is not None
    # http/event 主键为自增整数：ref 必须是纯数字
    if not ref.isdigit():
        return False
    table = "http_history" if kind == "http" else "events"
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE id=? AND project_id=?",
        (int(ref), project_id),
    ).fetchone() is not None


def normalize_refs(bb, project_id: str, refs) -> list[str]:
    """引用列表 → 排序保持、去重；逐条做同项目存在性校验，悬空/非法 ValueError。"""
    if not refs:
        return []
    if not isinstance(refs, (list, tuple)):
        raise ValueError("引用必须是数组 [\"finding:<id>\", …]")
    out: list[str] = []
    for raw in refs:
        kind, ref = _parse_ref(raw)
        if not _ref_exists(bb.conn, project_id, kind, ref):
            raise ValueError(
                f"引用不存在或不属于本项目: {raw}（先登记/先产生被引用对象，再建立推导）")
        norm = f"{kind}:{ref}"
        if norm not in out:
            out.append(norm)
    return out


def _str_list(values, field: str) -> list[str]:
    if not values:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field} 必须是字符串数组")
    out = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{field} 含空值/非字符串")
        s = v.strip()
        if s not in out:
            out.append(s)
    return out


# ---------- declare ----------

def declare_intent(bb, project_id: str, statement: str, *,
                   target_asset_id: str | None = None,
                   basis_refs=None, author: str = "system") -> dict:
    """声明意图（规划产物）。同项目存在 status=open 的同陈述意图 → 返回既有行
    （merged=True，不重复登记、不发事件）。target_asset_id 与 basis_refs 都做
    同项目存在性校验。"""
    stmt = statement.strip() if isinstance(statement, str) else ""
    if not stmt:
        raise ValueError("意图陈述 statement 必填非空（一句可证伪假设）")
    if len(stmt) > STATEMENT_MAX:
        raise ValueError(f"意图陈述过长（上限 {STATEMENT_MAX} 字）——浓缩成一句假设")
    basis = normalize_refs(bb, project_id, basis_refs)
    ts = now()
    with bb._tx():
        if target_asset_id:
            owned = bb.conn.execute(
                "SELECT 1 FROM assets WHERE id=? AND project_id=?",
                (target_asset_id, project_id),
            ).fetchone()
            if not owned:
                raise ValueError(
                    f"目标资产不存在或不属于本项目: {target_asset_id}")
        row = bb.conn.execute(
            "SELECT * FROM intents WHERE project_id=? AND status='open'"
            " AND statement=?",
            (project_id, stmt),
        ).fetchone()
        if row:
            out = _row_to_dict(row) or {}
            out["merged"] = True
            return _hydrate(out)
        intent_id = new_id("intent")
        bb.conn.execute(
            "INSERT INTO intents(id,project_id,statement,target_asset_id,basis_refs,"
            "status,author,created_at,updated_at)"
            " VALUES(?,?,?,?,?, 'open', ?,?,?)",
            (intent_id, project_id, stmt, target_asset_id,
             json.dumps(basis, ensure_ascii=False), author, ts, ts),
        )
    bb.append_event(project_id, "intent.declared",
                    {"intent_id": intent_id, "statement": stmt,
                     "target_asset_id": target_asset_id, "basis_refs": basis},
                    session_id=author if author.startswith("sess-") else None,
                    author=author)
    return get_intent(bb, project_id, intent_id) or {}


# ---------- close ----------

def close_intent(bb, project_id: str, intent_id: str, outcome: str, *,
                 finding_ids=None, evidence_refs=None, dead_reason=None,
                 author: str = "system") -> dict:
    """收尾意图：outcome=vuln/finding/dead_end。

    - vuln/finding：finding_ids 至少一条，存在、同项目、非 FP、category 匹配
      （vuln→vuln 类，finding→intel 类）；
    - dead_end：dead_reason 非空（什么证据排除假设）+ evidence_refs 至少一条
      http/event/artifact 引用。
    意图须为 open（重复收尾 ValueError，先 reopen）。"""
    if outcome not in INTENT_OUTCOMES:
        raise ValueError(
            f"非法收尾类型: {outcome}（允许 {INTENT_OUTCOMES}）")
    fids = _str_list(finding_ids, "finding_ids")
    evidence = normalize_refs(bb, project_id, evidence_refs)
    ts = now()
    with bb._tx():
        row = bb.conn.execute(
            "SELECT * FROM intents WHERE id=? AND project_id=?",
            (intent_id, project_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"意图不存在: {intent_id}")
        if row["status"] != "open":
            raise ValueError(
                f"意图已关闭，不能重复收尾: {intent_id}"
                "（有新证据先 reopen_intent 重开）")
        if outcome in ("vuln", "finding"):
            if not fids:
                raise ValueError(
                    "收尾为漏洞/有效发现时必须带 finding_ids（至少一条已登记发现）")
            want_cat = "vuln" if outcome == "vuln" else "intel"
            for fid in fids:
                f = bb.conn.execute(
                    "SELECT category, status FROM findings"
                    " WHERE id=? AND project_id=?",
                    (fid, project_id),
                ).fetchone()
                if f is None:
                    raise ValueError(f"收尾发现不存在或不属于本项目: {fid}")
                if f["status"] == "false-positive":
                    raise ValueError(
                        f"不能以误报发现收尾: {fid}——误报支持的是死路收尾"
                        "（outcome=dead_end，写清 dead_reason）")
                if f["category"] != want_cat:
                    raise ValueError(
                        f"收尾类别不符: {fid} category={f['category']}"
                        f"（{outcome} 收尾要求 {want_cat}——发现类别不对请换引用或另案）")
        else:
            reason = dead_reason.strip() if isinstance(dead_reason, str) else ""
            if not reason:
                raise ValueError(
                    "死路收尾必须填 dead_reason（什么证据排除了假设、已试过什么）")
            if not evidence:
                raise ValueError(
                    "死路收尾必须带 evidence_refs（至少一条 http:/event:/artifact:"
                    " 证据引用——防后人重走弯路）")
        bb.conn.execute(
            "UPDATE intents SET status='closed', outcome_type=?, outcome_refs=?,"
            " dead_reason=?, evidence_refs=?, closed_at=?, updated_at=?,"
            " revision=revision+1 WHERE id=?",
            (outcome, json.dumps(fids, ensure_ascii=False),
             dead_reason.strip() if isinstance(dead_reason, str) else "",
             json.dumps(evidence, ensure_ascii=False), ts, ts, intent_id),
        )
    bb.append_event(project_id, "intent.closed",
                    {"intent_id": intent_id, "outcome": outcome,
                     "finding_ids": fids,
                     "dead_reason": (dead_reason or "")[:200]},
                    session_id=author if author.startswith("sess-") else None,
                    author=author)
    return get_intent(bb, project_id, intent_id) or {}


# ---------- reopen ----------

def reopen_intent(bb, project_id: str, intent_id: str, *,
                  author: str = "system", note: str = "") -> dict:
    """重开已关闭意图（收尾漏洞被标 FP / 有新证据）：清收尾字段、回 open；
    evidence_refs 保留（事实引用不删）。已经是 open → 原样幂等返回。"""
    with bb._tx():
        row = bb.conn.execute(
            "SELECT * FROM intents WHERE id=? AND project_id=?",
            (intent_id, project_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"意图不存在: {intent_id}")
        if row["status"] == "open":
            return _hydrate(_row_to_dict(row) or {})
        bb.conn.execute(
            "UPDATE intents SET status='open', outcome_type='', outcome_refs='[]',"
            " dead_reason='', closed_at=NULL, updated_at=?,"
            " revision=revision+1 WHERE id=?",
            (now(), intent_id),
        )
    bb.append_event(project_id, "intent.reopened",
                    {"intent_id": intent_id, "note": note[:200]},
                    session_id=author if author.startswith("sess-") else None,
                    author=author)
    return get_intent(bb, project_id, intent_id) or {}


# ---------- read ----------

def _hydrate(d: dict) -> dict:
    for col in ("basis_refs", "outcome_refs", "evidence_refs"):
        if col in d:
            d[col] = _loads(d[col], [])
    return d


def get_intent(bb, project_id: str, intent_id: str) -> dict | None:
    row = bb.conn.execute(
        "SELECT * FROM intents WHERE id=? AND project_id=?",
        (intent_id, project_id),
    ).fetchone()
    d = _row_to_dict(row)
    return _hydrate(d) if d else None


def dead_end_backing_target(conn, project_id: str, asset_id: str) -> str | None:
    """tested_clean 背书查询（2026-09-25 门禁）：返回覆盖 asset_id 的
    closed/dead_end 意图 target_asset_id，无背书 None。

    - 直接背书：意图 target == asset_id；
    - 批次背书：asset_id 位于意图 target 的资产子树内（沿 parent_id 向上查
      祖先，服务端校验子树关系）。
    纯读、**收 conn**——调用方可能已在 ``_tx()`` 内，不能再开 bb 事务。
    target 为空的意图不参与背书。
    """
    rows = conn.execute(
        "SELECT target_asset_id FROM intents"
        " WHERE project_id=? AND status='closed' AND outcome_type='dead_end'"
        " AND target_asset_id IS NOT NULL AND target_asset_id!=''",
        (project_id,),
    ).fetchall()
    targets = {r[0] for r in rows}
    if not targets:
        return None
    if asset_id in targets:
        return asset_id
    parent_of: dict[str, str] = {}
    for a in conn.execute(
        "SELECT id, parent_id FROM assets WHERE project_id=?", (project_id,)
    ):
        if a["parent_id"]:
            parent_of[a["id"]] = a["parent_id"]
    ancestor = parent_of.get(asset_id)
    seen: set[str] = set()
    while ancestor and ancestor not in seen:  # parent 链不成环，seen 兜底
        if ancestor in targets:
            return ancestor
        seen.add(ancestor)
        ancestor = parent_of.get(ancestor)
    return None


def list_intents(bb, project_id: str, *, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM intents WHERE project_id=?"
    params: list = [project_id]
    if status:
        if status not in INTENT_STATUSES:
            raise ValueError(f"非法状态: {status}（允许 {INTENT_STATUSES}）")
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at"
    out = []
    for r in bb.conn.execute(sql, params):
        out.append(_hydrate(_row_to_dict(r) or {}))
    return out
