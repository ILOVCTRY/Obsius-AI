"""执行轨迹链路（docs/plans/execution-trace-chain.md，2026-09-22 实施 M1+M2+M3）。

双轨制（方案 D3 定稿）：
- **轨 A 轨迹视图**：`build_task_trace` 查询时现算（零写入）——events 按会话任务
  区间切分（R1）+ 过程聚合（R2），API `GET /api/projects/{pid}/trace/{task_id}` 直出。
- **轨 B 持久链**：`materialize_task_trace` 把聚合结果物化进 chains/chain_links
  （origin='trace' 自动链，node_type 扩 task/step，trace_ref 幂等键），触发点 =
  任务收尾 done（tasks._finish）与 finding verified（store 两条升级路径），
  try/except 旁路不挡主流程（R3）。
- **效果榜（M3/R4）**：`effect_stats` 基于物化侧统计 (skill × kb) × verified finding。

任务归属口径（R1）：一个会话同时只持一个任务（AgentSession.current_task_id 单值），
会话事件流按 `task.claimed → task.done/task.failed` 区间良构切分；区间内
command/tool.call/kb.open/skill.routed/finding.new 归属该任务；区间外 = 会话游离段
（idle，chat 轮命令等），只进轨迹视图、不进任务链。纯 claimed_by 列不可靠
（reopen 重跑可换会话），事件切分是唯一可靠口径。
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


def _claimed_sessions(conn, pid: str, task_id: str) -> list[str]:
    """认领过本任务的会话（事件回放；claimed_by 列被 reopen 重跑覆盖不可靠）。"""
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM events"
        " WHERE project_id=? AND kind='task.claimed'"
        " AND json_extract(payload,'$.task_id')=? AND session_id IS NOT NULL",
        (pid, task_id)).fetchall()
    return [r["session_id"] for r in rows]


def _task_at(conn, pid: str, session_id: str, event_id: int) -> str | None:
    """事件 id 落在该会话哪个任务区间（verified 触发时按 finding.new 反查归属）。"""
    for w in _session_task_windows(conn, pid, session_id):
        if event_id > w["lo"] and (w["hi"] is None or event_id <= w["hi"]):
            return w["task_id"]
    return None


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

def build_task_trace(bb: Blackboard, pid: str, task_id: str) -> dict | None:
    """R1+R2 现算任务轨迹（零写入）。任务不存在返 None；从未被认领返空 windows。

    返回 {task, windows, steps, idle, truncated}——steps 跨会话/跨 attempt 按时间序
    合并（reopen 重跑的多段区间天然串成一条）；idle=各认领会话的游离段聚合。"""
    task = bb.conn.execute(
        "SELECT * FROM tasks WHERE id=? AND project_id=?", (task_id, pid)).fetchone()
    if task is None:
        return None
    windows: list[dict] = []
    steps: list[dict] = []
    idle: list[dict] = []
    truncated = False
    for sid in _claimed_sessions(bb.conn, pid, task_id):
        wins = _session_task_windows(bb.conn, pid, sid)
        mine_idx = {i for i, w in enumerate(wins) if w["task_id"] == task_id}
        windows.extend({**wins[i], "session_id": sid} for i in sorted(mine_idx))
        events = _session_events(bb.conn, pid, sid)
        results = _results_by_call(bb, pid, sid)

        def label_of(e: dict) -> int:
            """事件所属窗口下标（本任务的窗口）；游离段/他人任务段 = -1。"""
            for i, w in enumerate(wins):
                if i not in mine_idx:
                    continue
                if e["id"] > w["lo"] and (w["hi"] is None or e["id"] <= w["hi"]):
                    return i
            return -1

        # 按连续段聚合（label 相同才算相邻）：工具组不跨窗口边界（R2「组不跨任务」
        # 的窗口侧保证——任务前后的游离命令不会隔着整个任务被并成一组）
        runs: list[tuple[int, list[dict]]] = []
        for e in events:
            lb = label_of(e)
            if runs and runs[-1][0] == lb:
                runs[-1][1].append(e)
            else:
                runs.append((lb, [e]))
        for lb, run in runs:
            aggregated = _aggregate(run, results)
            if lb == -1:
                if len(idle) < TRACE_MAX_IDLE:
                    idle.extend(aggregated)
            else:
                steps.extend(aggregated)
    steps.sort(key=lambda s: s["ts"])
    _enrich_findings(bb, pid, steps)
    if len(steps) > TRACE_MAX_STEPS:
        steps = steps[:TRACE_MAX_STEPS]
        truncated = True
    idle.sort(key=lambda s: s["ts"])
    return {
        "task": {"id": task["id"], "objective": task["objective"],
                 "task_type": task["task_type"], "status": task["status"],
                 "priority": task["priority"], "claimed_by": task["claimed_by"],
                 "result_note": _short(task["result_note"], 300)},
        "windows": windows, "steps": steps,
        "idle": idle[:TRACE_MAX_IDLE], "truncated": truncated,
    }


# ---------------- 轨 B：持久链物化（R3） ----------------

def materialize_task_trace(bb: Blackboard, pid: str, task_id: str,
                           author: str = "system") -> str | None:
    """R3：把任务轨迹物化进 origin='trace' 自动链（幂等）。

    - 幂等键 chain_links.trace_ref='task-<task_id>'：单事务 DELETE+重插，重跑安全；
      人工补挂的链边（trace_ref=''）零感知保留。
    - 链 status 只升不降：区间内存在 verified finding → validated；已是 validated
      不因后续重物化降级（exploited 仅人工语义，自动链不判）。
    - 从未被认领的任务无区间 → 返回 None 不建链。
    触发点：tasks._finish done 分支 / store 两条 verified 升级路径，均 try/except 旁路。
    """
    trace = build_task_trace(bb, pid, task_id)
    if trace is None or not trace["windows"]:
        return None
    task = trace["task"]
    trace_ref = f"task-{task_id}"
    # --- 组装节点（D2 聚合级：同 (kind,value) 合并；结构化计数，末尾统一拼 note） ---
    merged: dict[str, dict] = {}
    finding_ids: list[str] = []
    has_verified = False
    for s in trace["steps"]:
        if s["kind"] == "skill":
            key = "skill:" + (s.get("name") or "none")
            if key not in merged:  # skill 每任务一条（重复 routed 不重复记）
                merged[key] = {"kind": "skill", "label": _short(s.get("name"), 60),
                               "score": s.get("score"), "hit": s.get("hit")}
        elif s["kind"] == "kb":
            key = f"kb:{s.get('module')}"
            m = merged.setdefault(key, {"kind": "kb",
                                        "label": _short(s.get("module"), 80), "count": 0})
            m["count"] += int(s.get("count") or 1)
        elif s["kind"] == "tools":
            key = f"tools:{s.get('name')}"
            m = merged.setdefault(key, {"kind": "tools",
                                        "label": _short(s.get("name"), 40),
                                        "count": 0, "ok": 0, "fail": 0})
            m["count"] += int(s.get("count") or 1)
            m["ok"] += int(s.get("ok") or 0)
            m["fail"] += int(s.get("fail") or 0)
        elif s["kind"] == "finding":
            fid = s.get("finding_id")
            if fid and fid not in finding_ids:
                finding_ids.append(fid)
                if s.get("status") == "verified":
                    has_verified = True
    links: list[tuple[str, str, str]] = [
        ("task", task_id, f"意图：{_short(task['objective'], 120)}")]
    for key, m in merged.items():
        if m["kind"] == "skill":
            note = (f"技能命中 {m['label']}（score {m['score']}）" if m["hit"]
                    else "未命中技能（反例节点）")
        elif m["kind"] == "kb":
            note = f"知识库模块 {m['label']} ×{m['count']}"
        else:
            stat = f"（ok {m['ok']}/fail {m['fail']}）" if (m["ok"] or m["fail"]) else ""
            note = f"工具组 {m['label']} ×{m['count']}{stat}"
        links.append(("step", f"{task_id}#{key}", note))
    for s in trace["steps"]:
        if s.get("kind") == "finding" and s.get("finding_id") in finding_ids:
            fid = s["finding_id"]
            note = (f"任务区间内产出：{_short(s.get('title') or s.get('vuln_class'), 80)}"
                    f"（{s.get('severity')}{'，✓ verified' if s.get('status') == 'verified' else ''}）")
            links.append(("finding", fid, note))
            finding_ids.remove(fid)
    links = links[:TRACE_MAX_LINKS]

    created = False
    with bb._tx():
        row = bb.conn.execute(
            "SELECT l.chain_id FROM chain_links l JOIN chains c ON c.id=l.chain_id"
            " WHERE l.trace_ref=? AND c.project_id=? LIMIT 1", (trace_ref, pid)).fetchone()
        chain_id = row["chain_id"] if row else None
        old_status = "hypothesis"
        if chain_id is None:
            chain_id = new_id("chain")
            bb.conn.execute(
                "INSERT INTO chains(id,project_id,name,goal,status,origin,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (chain_id, pid, _short(task["objective"], 60) or task["task_type"],
                 "", "hypothesis", "trace", now(), now()))
            created = True
        else:
            old = bb.conn.execute(
                "SELECT status FROM chains WHERE id=?", (chain_id,)).fetchone()
            old_status = old["status"] if old else "hypothesis"
        bb.conn.execute(
            "DELETE FROM chain_links WHERE chain_id=? AND trace_ref=?", (chain_id, trace_ref))
        for i, (ntype, nid, note) in enumerate(links, 1):
            bb.conn.execute(
                "INSERT INTO chain_links(id,chain_id,seq,node_type,node_id,edge_note,trace_ref,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (new_id("clink"), chain_id, i, ntype, nid, note, trace_ref, now()))
        # status 只升不降（R3）：validated 保级、exploited 不降（exploited 仅人工语义）
        rank = {"hypothesis": 0, "validated": 1, "exploited": 2}
        target = "validated" if has_verified else "hypothesis"
        status = (target if rank.get(target, 0) > rank.get(old_status, 0) else old_status)
        bb.conn.execute(
            "UPDATE chains SET status=?, updated_at=? WHERE id=?", (status, now(), chain_id))
    bb.append_event(
        pid, "chain.created" if created else "chain.updated",
        {"chain_id": chain_id, "name": _short(task["objective"], 60),
         "origin": "trace", "trace_ref": trace_ref, "links": len(links),
         "status": status}, author=author)
    return chain_id


def materialize_for_finding(bb: Blackboard, pid: str, finding_id: str,
                            author: str = "system") -> str | None:
    """verified 升级触发（R3）：优先重物化已包含该发现的轨迹链；链上没有则按
    finding.new 事件反查归属任务。都找不到（对话轮产出/人工创建/无任务区间）
    返回 None——不强行入链。"""
    row = bb.conn.execute(
        "SELECT l.trace_ref FROM chain_links l JOIN chains c ON c.id=l.chain_id"
        " WHERE c.project_id=? AND c.origin='trace' AND l.node_type='finding'"
        " AND l.node_id=? LIMIT 1", (pid, finding_id)).fetchone()
    if row and str(row["trace_ref"] or "").startswith("task-"):
        return materialize_task_trace(bb, pid, row["trace_ref"][5:], author=author)
    ev = bb.conn.execute(
        "SELECT id, session_id FROM events WHERE project_id=? AND kind='finding.new'"
        " AND json_extract(payload,'$.finding_id')=? AND session_id IS NOT NULL"
        " ORDER BY id DESC LIMIT 1", (pid, finding_id)).fetchone()
    if ev is None:
        return None
    tid = _task_at(bb.conn, pid, ev["session_id"], ev["id"])
    if tid:
        return materialize_task_trace(bb, pid, tid, author=author)
    return None


# ---------------- M3/R4：打法效果榜（物化侧） ----------------

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
