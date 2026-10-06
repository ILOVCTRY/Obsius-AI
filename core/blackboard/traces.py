"""执行轨迹链路（docs/plans/execution-trace-chain.md，2026-09-22 实施 M1+M2+M3）。

任务机制退役（2026-10-06）：原轨 A 轨迹视图（`build_task_trace`）与轨 B 持久链
（`materialize_task_trace`）随任务一并退役——任务区间切分与任务链物化不再存在。
本模块保留的只读统计口径：
- **效果榜（M3/R4）**：`effect_stats` 基于物化侧统计 (skill × kb) × verified finding。
- `kb_module_feedback` / `retrieval_stats`：检索质量四象限只读对账。
"""

import json
import logging
from datetime import datetime

from core.blackboard.store import Blackboard, new_id, now

log = logging.getLogger(__name__)

#: 工具组并窗阈值（R2）：同任务区间内相邻两条工具事件间隔 ≤ 该秒数即并组（可调常量）
TRACE_TOOL_WINDOW_S = 120
#: 单会话事件回放上限（防超长会话全表扫；正常任务区间远小于此）
TRACE_MAX_EVENTS = 5000
#: 轨迹视图单返回步数上限（前端时间链保护）
TRACE_MAX_STEPS = 200
#: 物化链 link 数上限（task 头 + 过程节点 + 产出节点）
TRACE_MAX_LINKS = 120
#: 游离段聚合步数上限（轨迹视图 idle 段）
TRACE_MAX_IDLE = 30


def _short(text: object, cap: int) -> str:
    s = str(text or "")
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _ts_s(a: str, b: str) -> float:
    """两个 UTC ISO 串的间隔秒数（解析失败按 0=并窗处理，宁聚合勿碎裂）。"""
    try:
        ta = datetime.fromisoformat(a)
        tb = datetime.fromisoformat(b)
        return abs((tb - ta).total_seconds())
    except (ValueError, TypeError):
        return 0.0


# ---------------- R1 会话任务区间切分 ----------------

def _session_task_windows(conn, pid: str, session_id: str) -> list[dict]:
    """会话的任务区间列表（R1）：按 events.id 升序线性扫描。

    返回 [{task_id, lo, hi, open}]：区间语义 (lo, hi]——lo=task.claimed 事件 id，
    hi=同任务收尾事件 id（open=True 表示仍 claimed 进行中，hi=None=到最新）。
    防御：一段未收尾又出现新 claimed（理论不发生，单任务持有）按硬闭合处理。"""
    rows = conn.execute(
        "SELECT id, kind, payload FROM events"
        " WHERE project_id=? AND session_id=? AND kind IN"
        " ('task.claimed','task.done','task.failed') ORDER BY id",
        (pid, session_id)).fetchall()
    windows: list[dict] = []
    cur: dict | None = None
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        tid = payload.get("task_id")
        if r["kind"] == "task.claimed":
            if cur is not None:  # 上一段未收尾：硬闭合（异常现场，非良构区间）
                windows.append({**cur, "hi": r["id"], "open": False})
            cur = {"task_id": tid, "lo": r["id"]}
        elif cur is not None and tid == cur["task_id"]:
            windows.append({**cur, "hi": r["id"], "open": False})
            cur = None
    if cur is not None:
        windows.append({**cur, "hi": None, "open": True})
    return windows


def _session_events(conn, pid: str, session_id: str) -> list[dict]:
    """会话近期过程/产出事件（升序；超限取最新段——正常任务区间远小于此）。"""
    rows = conn.execute(
        "SELECT id, kind, payload, created_at FROM events"
        " WHERE project_id=? AND session_id=? AND kind IN"
        " ('command','command.result','tool.call','kb.open','skill.routed','finding.new')"
        f" ORDER BY id DESC LIMIT {TRACE_MAX_EVENTS}",
        (pid, session_id)).fetchall()
    out = []
    for r in reversed(rows):  # 回升序
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        out.append({"id": r["id"], "kind": r["kind"],
                    "payload": payload, "ts": r["created_at"]})
    return out


# ---------------- R2 聚合 ----------------

def _new_tool_group(ev: dict) -> dict:
    name = "run_cmd" if ev["kind"] == "command" else str(ev["payload"].get("name") or "tool")
    return {"kind": "tools", "ts": ev["ts"], "ts_end": ev["ts"], "name": name,
            "count": 1, "ok": 0, "fail": 0, "cmds": [], "_last_ts": ev["ts"]}


def _aggregate(events: list[dict], results_by_call: dict[str, int]) -> list[dict]:
    """R2 聚合（时间序）：skill 每事件一节点；kb 按 module 归并；command/tool.call
    相邻间隔 ≤ TRACE_TOOL_WINDOW_S 并组（组名=首工具名）；finding.new 成产出节点。"""
    steps: list[dict] = []
    kb_by_module: dict[str, dict] = {}
    tool_group: dict | None = None

    def close_tools():
        nonlocal tool_group
        if tool_group is not None:
            g = tool_group
            steps.append({k: v for k, v in g.items() if not k.startswith("_")})
            tool_group = None

    for ev in events:
        kind, p = ev["kind"], ev["payload"]
        if kind == "command.result":
            continue  # 只作 ok 统计源（results_by_call），不上时间线
        if kind in ("command", "tool.call"):
            if tool_group is not None and _ts_s(tool_group["_last_ts"], ev["ts"]) > TRACE_TOOL_WINDOW_S:
                close_tools()
            if tool_group is None:
                tool_group = _new_tool_group(ev)
            else:
                tool_group["count"] += 1
                tool_group["ts_end"] = ev["ts"]
                tool_group["_last_ts"] = ev["ts"]
            if kind == "command":
                call_id = p.get("call_id")
                if call_id in results_by_call:
                    if results_by_call[call_id] == 0:
                        tool_group["ok"] += 1
                    else:
                        tool_group["fail"] += 1
                if len(tool_group["cmds"]) < 8:
                    tool_group["cmds"].append(_short(p.get("cmd"), 120))
            elif len(tool_group["cmds"]) < 8:
                tool_group["cmds"].append(f"tool:{p.get('name')}")
            continue
        close_tools()  # 非 tool 事件截断并窗（时间线混入 kb/skill/finding 即分组边界）
        if kind == "kb.open":
            module = str(p.get("module") or "(未知)")
            g = kb_by_module.get(module)
            if g is None:
                g = {"kind": "kb", "ts": ev["ts"], "module": module,
                     "source": p.get("source"), "count": 1}
                kb_by_module[module] = g
                steps.append(g)
            else:
                g["count"] += 1
        elif kind == "skill.routed":
            name = p.get("name")
            steps.append({"kind": "skill", "ts": ev["ts"], "name": name,
                          "hit": name is not None, "score": p.get("score"),
                          "matched": (p.get("matched") or [])[:5]})
        elif kind == "finding.new":
            steps.append({"kind": "finding", "ts": ev["ts"],
                          "finding_id": p.get("finding_id"),
                          "vuln_class": p.get("vuln_class"),
                          "severity": p.get("severity")})
    close_tools()
    steps.sort(key=lambda s: s["ts"])
    return steps


def _enrich_findings(bb: Blackboard, pid: str, steps: list[dict]) -> None:
    """产出节点补发现行字段（标题/状态/类别——finding.new payload 只有 id+类+级）。"""
    ids = [s["finding_id"] for s in steps
           if s.get("kind") == "finding" and s.get("finding_id")]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    rows = bb.conn.execute(
        f"SELECT id, title, severity, status, vuln_class, category FROM findings"
        f" WHERE project_id=? AND id IN ({marks})", (pid, *ids)).fetchall()
    by_id = {r["id"]: r for r in rows}
    for s in steps:
        if s.get("kind") == "finding" and s.get("finding_id") in by_id:
            r = by_id[s["finding_id"]]
            s.update({"title": r["title"], "severity": r["severity"],
                      "status": r["status"], "vuln_class": r["vuln_class"],
                      "category": r["category"]})


def _results_by_call(bb: Blackboard, pid: str, session_id: str) -> dict[str, int]:
    rows = bb.conn.execute(
        "SELECT payload FROM events WHERE project_id=? AND session_id=?"
        " AND kind='command.result'", (pid, session_id)).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        if p.get("call_id"):
            out[p["call_id"]] = int(p.get("exit_code") or 0)
    return out


# ---------------- 轨 A：轨迹视图（现算） ----------------

def effect_stats(bb: Blackboard, pid: str, top: int = 20) -> dict:
    """R4 正向效果榜：轨迹链（origin='trace'）内 (skill × kb) 组合 × verified finding。

    只算物化侧（已收尾任务，口径跨项目稳定）；step 节点 node_id 前缀
    `{task_id}#skill:…` / `{task_id}#kb:…` 直接解析，不碰 edge_note。"""
    chains = bb.conn.execute(
        "SELECT id, status FROM chains WHERE project_id=? AND origin='trace'",
        (pid,)).fetchall()
    combos: dict[tuple[str, str], dict] = {}
    n_validated = 0
    for c in chains:
        links = bb.conn.execute(
            "SELECT node_type, node_id FROM chain_links WHERE chain_id=? ORDER BY seq",
            (c["id"],)).fetchall()
        skills: set[str] = set()
        kbs: set[str] = set()
        verified = 0
        for l in links:
            if l["node_type"] == "step":
                key = str(l["node_id"]).split("#", 1)[-1]
                if key.startswith("skill:"):
                    v = key[6:]
                    if v != "none":
                        skills.add(v)
                elif key.startswith("kb:"):
                    kbs.add(key[3:])
            elif l["node_type"] == "finding":
                f = bb.conn.execute(
                    "SELECT status FROM findings WHERE id=? AND project_id=?",
                    (l["node_id"], pid)).fetchone()
                if f and f["status"] == "verified":
                    verified += 1
        if c["status"] == "validated":
            n_validated += 1
        if verified == 0:
            continue
        if not skills:
            skills.add("")
        if not kbs:
            kbs.add("")
        for sk in skills:
            for kb in kbs:
                cell = combos.setdefault((sk, kb), {"skill": sk or "(未命中技能)",
                                                    "kb": kb or "(未引知识库)",
                                                    "chains": 0, "verified_findings": 0})
                cell["chains"] += 1
                cell["verified_findings"] += verified
    ranked = sorted(combos.values(),
                    key=lambda x: (-x["verified_findings"], -x["chains"]))[:top]
    return {"trace_chains": len(chains), "validated_chains": n_validated,
            "combos": ranked}


def kb_module_feedback(conn, pid: str) -> dict[str, dict]:
    """kb 模块三象限反馈统计（experience-sedimentation M3 甲，效果榜负向侧）。

    纯只读聚合零写入零事件；conn 传 ``Blackboard.conn`` 或 doctor 跨项目扫描的
    只读 sqlite 连接均可（须 row_factory=sqlite3.Row）。口径：
    - opened：kb.open 事件按 module 计数（全历史累计，v1 拍板不做时间窗）；
    - positive：该模块出现在「有 verified finding 的轨迹链」中的链数
      （effect_stats 同源口径，链级去重）；
    - negative：真失败任务（failed + blocked_reason='error'）任务窗内的 kb.open
      次数——「打开且失败」≠「手册误导」（试错正常），只作弱信号供人工复核。
    返回 {module: {opened, positive, negative}}。"""
    # 任务机制退役（2026-10-06）：无「真失败任务」口径，negative 恒 0。
    failed_ids: set[str] = set()
    pos: dict[str, int] = {}
    for c in conn.execute(
            "SELECT id FROM chains WHERE project_id=? AND origin='trace'",
            (pid,)).fetchall():
        links = conn.execute(
            "SELECT node_type, node_id FROM chain_links WHERE chain_id=? ORDER BY seq",
            (c["id"],)).fetchall()
        kbs: set[str] = set()
        verified = 0
        for l in links:
            if l["node_type"] == "step":
                key = str(l["node_id"]).split("#", 1)[-1]
                if key.startswith("kb:"):
                    kbs.add(key[3:])
            elif l["node_type"] == "finding":
                f = conn.execute(
                    "SELECT status FROM findings WHERE id=? AND project_id=?",
                    (l["node_id"], pid)).fetchone()
                if f and f["status"] == "verified":
                    verified += 1
        if verified:
            for kb in kbs:
                pos[kb] = pos.get(kb, 0) + 1
    opened: dict[str, int] = {}
    neg: dict[str, int] = {}
    windows_cache: dict[str, dict[str, tuple[int, int]]] = {}
    for r in conn.execute(
            "SELECT id, session_id, payload FROM events"
            " WHERE project_id=? AND kind='kb.open' ORDER BY id", (pid,)).fetchall():
        try:
            module = str((json.loads(r["payload"] or "{}")).get("module") or "")
        except ValueError:
            continue
        if not module:
            continue
        opened[module] = opened.get(module, 0) + 1
        sid = r["session_id"]
        if not sid or not failed_ids:
            continue
        if sid not in windows_cache:
            windows_cache[sid] = {
                w["task_id"]: (w["lo"], w["hi"])
                for w in _session_task_windows(conn, pid, sid) if w.get("hi")}
        for tid, (lo, hi) in windows_cache[sid].items():
            if tid in failed_ids and lo < r["id"] <= hi:
                neg[module] = neg.get(module, 0) + 1
                break
    out: dict[str, dict] = {}
    for m in set(opened) | set(pos) | set(neg):
        out[m] = {"opened": opened.get(m, 0), "positive": pos.get(m, 0),
                  "negative": neg.get(m, 0)}
    return out


# ---------------- 检索对账三象限（M3 retrieval-upgrade，2026-09-23） ----------------

#: 对账事件回放窗口（与 TRACE_MAX_EVENTS 同量级；三象限是近期质量仪表不是全量审计）
RETRIEVAL_STATS_WINDOW = 5000


def retrieval_stats(bb: Blackboard, pid: str,
                    window: int = RETRIEVAL_STATS_WINDOW) -> dict:
    """漏召回对账（M3，纯只读离线统计）：per 任务对比「提示了什么」与
    「打开了什么」与「产出了什么 verified 发现」。

    数据源：skill.routed payload.kb_hits（G2 批次已随路由事件落库）+
    tool.call name=kb_open（Agent 主动检索）+ findings 表 verified 集。
    任务归属：会话事件按 task.claimed→done/failed 区间切分（同 R1 口径——
    一个会话同时只持一个任务），区间外的路由/打开（对话轮等）不归属。

    三象限（独立判定，一个任务可同时进多格）：
    - hinted_opened：kb_hits ∩ opened ≠ ∅——提示链路正常；
    - hinted_not_opened：有提示且全没打开——提示质量/摘要问题，低优；
    - opened_no_output：打开过但无 verified 产出——正常试错，不算 missed；
    - missed：打开的模块里 kb_hits 没提示的且任务有 verified 产出——**漏召回
      信号**（Agent 自己找到手册并打穿），审阅后经提案制增补同义词/keywords/
      route_index，与 M2 词表互喂。"""
    conn = bb.conn
    rows = conn.execute(
        "SELECT id, session_id, kind, payload FROM events"
        " WHERE project_id=? AND kind IN"
        " ('skill.routed','tool.call','finding.new','task.claimed','task.done','task.failed')"
        f" ORDER BY id DESC LIMIT {int(window)}", (pid,)).fetchall()
    evs: list[dict] = []
    for r in reversed(rows):  # 回升序
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        evs.append({"id": r["id"], "session_id": r["session_id"],
                    "kind": r["kind"], "payload": payload})
    verified = {r["id"]: (r["title"] or "") for r in conn.execute(
        "SELECT id, title FROM findings WHERE project_id=? AND status='verified'",
        (pid,)).fetchall()}
    tasks: dict[str, dict] = {}   # task_id -> 聚合记录
    cur: dict[str, dict] = {}     # session_id -> 进行中任务记录
    for ev in evs:
        sid = ev["session_id"]
        k = ev["kind"]
        if k == "task.claimed":
            tid = str(ev["payload"].get("task_id") or "")
            if tid and sid:
                cur[sid] = tasks.setdefault(
                    tid, {"task_id": tid, "kb_hits": set(), "opened": set(),
                          "verified": set(), "failed": False})
        elif k in ("task.done", "task.failed") and sid and sid in cur:
            rec = cur[sid]
            if str(ev["payload"].get("task_id") or "") == rec["task_id"]:
                rec["failed"] = k == "task.failed"
                cur.pop(sid)
        elif not sid or sid not in cur:
            continue  # 区间外（对话轮/游离）：不归属任务
        elif k == "skill.routed":
            rec = cur[sid]
            rec["kb_hits"] |= {str(m) for m in (ev["payload"].get("kb_hits") or [])
                               if isinstance(m, str) and m}
        elif k == "tool.call" and ev["payload"].get("name") == "kb_open":
            rec = cur[sid]
            args = ev["payload"].get("args") or {}
            module = str(args.get("module") or "").strip() if isinstance(args, dict) else ""
            if module:
                rec["opened"].add(module)
        elif k == "finding.new":
            rec = cur[sid]
            fid = str(ev["payload"].get("finding_id") or "")
            if fid in verified:
                rec["verified"].add(fid)
    missed: dict[str, dict] = {}
    hinted_not_opened: list[dict] = []
    opens_all: dict[str, int] = {}
    quad = {"hinted_opened": 0, "hinted_not_opened": 0, "opened_no_output": 0,
            "missed": 0}
    # 任务机制退役（2026-10-06）：tasks 表已删，objective 无从取；任务区间事件
    # （task.claimed/done/failed）亦不再产出，故本统计的四象限恒空，仅 top_modules
    # 仍按 kb_open 事件可用。
    task_rows: dict[str, str] = {}
    for rec in tasks.values():
        hits, opened = rec["kb_hits"], rec["opened"]
        for m in opened:
            opens_all[m] = opens_all.get(m, 0) + 1
        if hits & opened:
            quad["hinted_opened"] += 1
        elif hits:
            quad["hinted_not_opened"] += 1
            hinted_not_opened.append({
                "task_id": rec["task_id"],
                "objective": (task_rows.get(rec["task_id"]) or "")[:80],
                "kb_hits": sorted(hits)[:6]})
        if opened and not rec["verified"]:
            quad["opened_no_output"] += 1
        if not rec["verified"]:
            continue
        unseen = opened - hits
        if not unseen:
            continue
        quad["missed"] += 1
        for m in unseen:
            e = missed.setdefault(m, {"module": m, "task_id": rec["task_id"],
                                      "objective": (task_rows.get(rec["task_id"]) or "")[:80],
                                      "verified": 0, "finding": ""})
            e["verified"] += 1
            if not e["finding"]:
                e["finding"] = next((verified[f] for f in rec["verified"]), "")
    return {
        "tasks_scanned": len(tasks),
        "quadrants": quad,
        "missed": sorted(missed.values(),
                         key=lambda x: -x["verified"])[:20],
        "hinted_not_opened": hinted_not_opened[:10],
        "top_modules": sorted(
            ({"module": m, "opens": n} for m, n in opens_all.items()),
            key=lambda x: -x["opens"])[:10],
    }
