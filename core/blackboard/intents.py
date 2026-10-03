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


def _event_session_id(author: str) -> str | None:
    """意图事件挂 session_id 的口径（2026-10-01 intent-tools-chat）：任务链
    author=sess-*、对话链 author=chat-*，两者都要挂——bb_add_finding 的「意图
    声明后有执行动作」门禁按 session_id 查 intent.declared 事件，chat 侧此前
    写 None 会导致门禁查不到声明、误判「无执行动作」而永久拒绝登记发现。"""
    return author if author.startswith(("sess-", "chat-")) else None

# 引用 kind 白名单（basis_refs / evidence_refs）
REF_KINDS = ("asset", "finding", "artifact", "http", "event")
_REF_RE = re.compile(r"^([a-z]+):(\S+)$")
STATEMENT_MAX = 500


# ---------- 引用归一化 + 存在性 ----------

# id 自带 kind 前缀（asset-…/find-…/art-…）；http/event 主键为纯数字。
# 注意 artifact 的 id 前缀是 art-（kind 与前缀不同名——同类不一致正是模型
# 引用写错的温床，见 normalize_refs_verbose 宽容归一）
_ID_PREFIX = {"asset": "asset-", "finding": "find-", "artifact": "art-"}
# 常见 kind 别名（模型常把 finding 写成 find / artifact 写成 art）
_KIND_ALIASES = {"find": "finding", "art": "artifact"}


def _parse_ref(ref: object) -> tuple[str, str]:
    if not isinstance(ref, str):
        raise ValueError(f"非法引用: {ref!r}（必须是字符串）")
    m = _REF_RE.match(ref.strip())
    if not m or m.group(1) not in REF_KINDS:
        raise ValueError(
            f"非法引用: {ref}（形如 finding:<id> / asset:<id> / http:<历史id> /"
            f" event:<事件id> / artifact:<id>；<id> 是**完整 id**——自带 kind"
            f" 前缀，如 asset:asset-6971f089d5fe / finding:find-c32f449cc7b5）")
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


def normalize_refs_verbose(bb, project_id: str, refs) -> tuple[list[str], list[str]]:
    """引用列表 → (归一后引用, 修正记录["raw → norm", …])。

    **宽容归一（2026-09-30，sess-78df38d1741d 五连拒复盘）**：id 自带 kind
    前缀（asset-xxx）与引用语法 kind:<id> 结构性碰撞——模型稳定写出剥前缀
    形态 asset:6971xxx（正确=asset:asset-6971xxx）或裸 id asset-xxx。收到
    此类形态按候选补全，**仅当补全后同项目真实存在才放行**并记录修正；
    悬空/乱写照抛（错误文案带完整形态示例，防静默吞真错误）。"""
    if not refs:
        return [], []
    if not isinstance(refs, (list, tuple)):
        raise ValueError("引用必须是数组 [\"finding:<id>\", …]")
    out: list[str] = []
    corrections: list[str] = []
    for raw in refs:
        norm = None
        try:
            kind, ref = _parse_ref(raw)
            if _ref_exists(bb.conn, project_id, kind, ref):
                norm = f"{kind}:{ref}"
        except ValueError:
            pass
        if norm is None:
            for kind, ref in _ref_candidates(str(raw)):
                if _ref_exists(bb.conn, project_id, kind, ref):
                    norm = f"{kind}:{ref}"
                    corrections.append(f"{raw} → {norm}")
                    break
        if norm is None:
            _parse_ref(raw)  # 形态非法则抛带完整形态示例的原始错误
            raise ValueError(
                f"引用不存在或不属于本项目: {raw}（先登记/先产生被引用对象，"
                f"再建立推导；注意 <id> 须是完整 id——自带 kind 前缀，如"
                f" asset:asset-6971f089d5fe / finding:find-c32f449cc7b5）")
        if norm not in out:
            out.append(norm)
    return out, corrections


def _ref_candidates(raw: str) -> list[tuple[str, str]]:
    """宽容归一候选：剥前缀 kind:short / 裸 id / find: 别名 → (kind, 完整id)。"""
    out: list[tuple[str, str]] = []
    s = raw.strip()
    m = re.match(r"^([a-z]+):(\S+)$", s)
    if m:
        kind = _KIND_ALIASES.get(m.group(1), m.group(1))
        ref = m.group(2)
        if kind in REF_KINDS:
            out.append((kind, ref))
            prefix = _ID_PREFIX.get(kind, "")
            if prefix and not ref.startswith(prefix):
                out.append((kind, prefix + ref))
        return out
    m = re.match(r"^(asset|find|finding|artifact|art)-\S+$", s)
    if m:
        kind = _KIND_ALIASES.get(m.group(1), m.group(1))
        out.append((kind, s))
    return out


def normalize_refs(bb, project_id: str, refs) -> list[str]:
    """兼容包装：只返回归一后引用（语义/修正记录见 normalize_refs_verbose）。"""
    return normalize_refs_verbose(bb, project_id, refs)[0]


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
    """声明意图（规划产物）。同作者存在 status=open 的同陈述意图 → 返回既有行
    （merged=True，不重复登记、不发事件）。target_asset_id 与 basis_refs 都做
    同项目存在性校验。

    **合并按作者收口（2026-09-27 agent-loop 修复）**：合并只认同作者——跨会话
    撞同陈述必须各自落意图行。此前按全项目去重，会话 B 撞上会话 A 的 open 意图
    会拿到 merged 复用（意图仍是 A 的），而 bb_add_finding 的「必挂本会话 open
    意图」门禁只认 author，B 从此永远无法登记发现（硬拒绝死循环 → E2 熔断挂
    任务）；close_intent 又不校验 author，B 还可能误关 A 的意图。作者收口后：
    同会话重复声明 → 去重复用；跨会话同陈述 → 各建各的意图，树按会话展示
    各自的尝试分支。"""
    stmt = statement.strip() if isinstance(statement, str) else ""
    if not stmt:
        raise ValueError("意图陈述 statement 必填非空（一句可证伪假设）")
    if len(stmt) > STATEMENT_MAX:
        raise ValueError(f"意图陈述过长（上限 {STATEMENT_MAX} 字）——浓缩成一句假设")
    basis, ref_corrections = normalize_refs_verbose(bb, project_id, basis_refs)
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
            " AND statement=? AND author=?",
            (project_id, stmt, author),
        ).fetchone()
        if row:
            out = _row_to_dict(row) or {}
            out["merged"] = True
            out = _hydrate(out)
            if ref_corrections:
                out["ref_corrections"] = ref_corrections
            return out
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
                     "target_asset_id": target_asset_id, "basis_refs": basis,
                     **({"ref_corrections": ref_corrections}
                        if ref_corrections else {})},
                    session_id=_event_session_id(author),
                    author=author)
    out = get_intent(bb, project_id, intent_id) or {}
    if ref_corrections:
        out["ref_corrections"] = ref_corrections
    return out


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
    evidence, ref_corrections = normalize_refs_verbose(bb, project_id,
                                                       evidence_refs)
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
                     "dead_reason": (dead_reason or "")[:200],
                     **({"ref_corrections": ref_corrections}
                        if ref_corrections else {})},
                    session_id=_event_session_id(author),
                    author=author)
    out = get_intent(bb, project_id, intent_id) or {}
    if ref_corrections:
        out["ref_corrections"] = ref_corrections
    return out


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
                    session_id=_event_session_id(author),
                    author=author)
    return get_intent(bb, project_id, intent_id) or {}


# ---------- delete ----------

def delete_intent(bb, project_id: str, intent_id: str, *,
                  author: str = "system", reason: str = "") -> dict:
    """物理删除意图（2026-10-01 intent-tools-chat）：误声明/目标取消时硬删该行。

    **带保护**（宁严勿松）：
    - 已收尾（status=closed）→ 拒删（收尾是链路图事实，须先 reopen 再删）；
    - 被别的意图当 basis_refs 引用 → 拒删（会断推导链）；
    - 已作为某 finding 的挂载意图（bb_add_finding 记录了 intent 关联）——实测
      发现本身不强制存 intent_id 列，故以 intent.declared→后续 finding 的
      事件关联/作者未硬绑；此处只拦「有 finding 归入本意图」的显式情形：
      intents 表若有 finding 引用（outcome_refs）也已由 closed 态覆盖。
    事件 intent.deleted（留痕：谁删了什么）。
    """
    with bb._tx():
        row = bb.conn.execute(
            "SELECT * FROM intents WHERE id=? AND project_id=?",
            (intent_id, project_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"意图不存在: {intent_id}")
        if row["status"] == "closed":
            raise ValueError(
                f"意图已收尾（{intent_id}），不能直接删除——收尾结论是链路图事实；"
                "如确要废弃请先 reopen_intent 重开，再评估删除")
        # 被其他意图引用为推导依据 → 拒删（防断链）
        rows = bb.conn.execute(
            "SELECT id, basis_refs FROM intents WHERE project_id=? AND id!=?",
            (project_id, intent_id),
        ).fetchall()
        for r in rows:
            try:
                refs = json.loads(r["basis_refs"] or "[]")
            except ValueError:
                refs = []
            if any(isinstance(x, str) and x == f"intent:{intent_id}" for x in refs):
                raise ValueError(
                    f"意图 {intent_id} 被 {r['id']} 当作推导依据引用，不能删除——"
                    "先处理引用它的意图")
        statement = row["statement"]
        bb.conn.execute(
            "DELETE FROM intents WHERE id=? AND project_id=?",
            (intent_id, project_id),
        )
    bb.append_event(project_id, "intent.deleted",
                    {"intent_id": intent_id, "statement": statement[:200],
                     "reason": (reason or "")[:200]},
                    session_id=_event_session_id(author),
                    author=author)
    return {"id": intent_id, "deleted": True, "statement": statement}


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


def _subtree_asset_ids(conn, project_id: str, root_id: str) -> set:
    """root_id 及其全部后代资产 id（沿 parent_id 展开）。纯读、收 conn。"""
    rows = conn.execute(
        "SELECT id, parent_id FROM assets WHERE project_id=?",
        (project_id,),
    ).fetchall()
    subtree = {root_id}
    changed = True
    while changed:
        changed = False
        for r in rows:
            if r["parent_id"] in subtree and r["id"] not in subtree:
                subtree.add(r["id"])
                changed = True
    return subtree


def _intent_anchors(intent_row) -> set:
    """意图的资产锚点集合：target_asset_id ∪ basis_refs 中的 asset:<id>。"""
    out: set = set()
    tid = intent_row["target_asset_id"]
    if tid:
        out.add(tid)
    try:
        refs = json.loads(intent_row["basis_refs"] or "[]")
    except ValueError:
        refs = []
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("asset:"):
            out.add(ref.split(":", 1)[1])
    return out


def dead_end_backing_target(conn, project_id: str, asset_id: str,
                            root_id: str | None = None) -> str | None:
    """tested_clean 背书查询（2026-09-25 门禁；2026-10-01 改读链路图口径）。

    口径与攻击链路图同源，**子目标级**：先把 asset_id 归到它所属的「子目标」
    （自根向下、离根最近的那层祖先；`root_id` 给定时用该根，缺省用资产的链顶），
    再判定该子目标的子树是否「**意图全部收尾**（无 open）且至少一条 dead_end」。
    满足则返回该子目标 id（即背书来源）；否则 None。

    - **子目标级**：根的直接子资产是子目标；对叶子（如 url）标净，实际校验的是
      它所属子目标的整棵子树——父/子树共识可背书后代，但不跨到无关旁支
      （保留 2026-09-29「同模板一致」批量误判的修复精神）；
    - **全部收尾**：子树内只要还有 open 意图即无背书（宁严勿松）；
    - 无 root_id 时用该资产的祖先链顶（无 parent 的顶层资产）作为根。
    纯读、**收 conn**——调用方可能已在 ``_tx()`` 内，不能再开 bb 事务。
    """
    subtarget_scope = _subtarget_scope(conn, project_id, asset_id, root_id)
    if subtarget_scope is None:
        return None
    scope_set = subtarget_scope[1]
    rows = conn.execute(
        "SELECT target_asset_id, basis_refs, status, outcome_type FROM intents"
        " WHERE project_id=?",
        (project_id,),
    ).fetchall()
    in_scope = [r for r in rows if _intent_anchors(r) & scope_set]
    if not in_scope:
        return None
    if any(r["status"] == "open" for r in in_scope):
        return None
    has_dead_end = any(
        r["status"] == "closed" and r["outcome_type"] == "dead_end"
        for r in in_scope)
    return subtarget_scope[0] if has_dead_end else None


def _subtarget_scope(conn, project_id: str, asset_id: str,
                     root_id: str | None) -> tuple[str, set] | None:
    """把 asset_id 归到「子目标」作用域，返回 (子目标 id, 其子树集合)。

    子目标 = 根的直接子资产（2026-10-01 链路图口径）。归法：
    - asset_id 本身就是根的直接子资产 → 它自己；
    - 否则沿 parent_id 上溯到根的最近一层后代；
    - asset_id 就是根（root 自身无子目标）→ 以 root 为作用域（等价整站，
      仅当调用方对着根资产标净时发生；父节点 clean 本就走读时派生，少用）。
    资产不存在 → None。
    """
    rows = conn.execute(
        "SELECT id, parent_id FROM assets WHERE project_id=?", (project_id,)
    ).fetchall()
    parent_of = {r["id"]: r["parent_id"] for r in rows}
    if asset_id not in parent_of:
        return None
    # 找链顶（无 parent 或 parent 不存在的祖先）
    root = root_id
    if not root:
        cur = asset_id
        while parent_of.get(cur):
            cur = parent_of[cur]
        root = cur
    if asset_id == root:
        return (root, _subtree_asset_ids(conn, project_id, root))
    # 上溯到 root 的直接子资产
    chain = []
    cur = asset_id
    while cur and cur != root:
        chain.append(cur)
        cur = parent_of.get(cur)
    if cur != root or not chain:
        # asset_id 不在 root 子树内 → 退回以自身为根的作用域
        return (asset_id, _subtree_asset_ids(conn, project_id, asset_id))
    subtarget = chain[-1]  # 离 root 最近的那一层
    return (subtarget, _subtree_asset_ids(conn, project_id, subtarget))


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
