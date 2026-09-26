"""执行轨迹链路测试（docs/plans/execution-trace-chain.md，2026-09-22 M1+M2+M3）。

覆盖：R1 会话任务区间切分（含 reopen 双区间/游离段隔离）、R2 聚合（工具并窗/kb
归并/skill 节点）、R3 物化幂等 + verified 双触发 + 状态只升不降、R6 board_graph
origin 防御过滤、R4 效果榜计数口径、_finish done 自动物化、v19 两列迁移。

注意：store.add_finding 自身落 finding.new 事件（author=sess- 前缀时带
session_id）——测试勿再手工补发同 kind 事件，否则轨迹出现重复产出节点。
"""

import sqlite3

import pytest

from core.blackboard import Blackboard, TaskQueue
from core.blackboard.graph import board_graph
from core.blackboard import traces


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def pid(bb):
    return bb.create_project("轨迹-测试", "pentest", ["web"])["id"]


def _session(bb, pid, name="S1"):
    return bb.register_session(pid, name)["id"]


def _run_task(bb, pid, sid, objective="渗透主站"):
    """发布→认领，返回 task_id（收尾由用例自行决定 done/failed）。"""
    tq = TaskQueue(bb)
    tid = tq.publish(pid, objective)
    tq.claim(tid, sid)
    return tid


def _emit(bb, pid, sid, kind, payload):
    bb.append_event(pid, kind, payload, session_id=sid, author=sid)


def _work_events(bb, pid, sid):
    """模拟一段任务区间内的典型过程事件（命令对 + 工具 + kb + 技能命中）。"""
    _emit(bb, pid, sid, "command", {"call_id": "c1", "cmd": "nmap -sV 10.0.0.1", "runtime": "host"})
    _emit(bb, pid, sid, "command.result", {"call_id": "c1", "exit_code": 0})
    _emit(bb, pid, sid, "tool.call", {"name": "bb_add_finding", "ok": True, "duration_s": 0.1})
    _emit(bb, pid, sid, "kb.open", {"source": "pack", "module": "web/nmap-recipes", "path": "x.md"})
    _emit(bb, pid, sid, "skill.routed", {"query": "内网扫描", "name": "recon-net",
                                         "score": 3.0, "matched": ["扫描"]})


def _add_finding(bb, pid, author, title="SQL 注入", status="unverified", with_poc=False):
    # POC 走 evidence.poc（poc_artifact_id 是 artifacts 外键，测试不造产物行）
    evidence = {"poc": {"raw": "id=1' or '1'='1"}} if with_poc else {}
    return bb.add_finding(pid, vuln_class="sqli", title=title, severity="high",
                          status=status, evidence=evidence,
                          dedup_key=f"dk-{title}", author=author, track="pentest")["id"]


def _chain_status(bb, cid):
    return bb.conn.execute(
        "SELECT status FROM chains WHERE id=?", (cid,)).fetchone()["status"]


def _link_rows(bb, cid):
    return bb.conn.execute(
        "SELECT COUNT(*) AS n FROM chain_links WHERE chain_id=?", (cid,)).fetchone()["n"]


def _trace_chain(bb, pid):
    rows = bb.list_chains(pid, origin="trace")
    return rows[0] if rows else None


# ---------- R1 区间切分 + R2 聚合（轨 A 现算） ----------

def test_trace_split_and_aggregate(bb, pid):
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    _work_events(bb, pid, sid)
    fid = _add_finding(bb, pid, author=sid)  # 自动落 finding.new（带 session_id）
    TaskQueue(bb).complete(tid, sid)

    trace = traces.build_task_trace(bb, pid, tid)
    assert trace is not None
    assert trace["task"]["status"] == "done"
    assert len(trace["windows"]) == 1 and not trace["windows"][0]["open"]
    kinds = [s["kind"] for s in trace["steps"]]
    assert kinds.count("tools") == 1  # 相邻间隔内 command+tool.call 并一组
    tools = next(s for s in trace["steps"] if s["kind"] == "tools")
    assert tools["count"] == 2 and tools["ok"] == 1 and tools["fail"] == 0
    assert tools["cmds"][0].startswith("nmap")
    kb = next(s for s in trace["steps"] if s["kind"] == "kb")
    assert kb["module"] == "web/nmap-recipes" and kb["count"] == 1
    skill = next(s for s in trace["steps"] if s["kind"] == "skill")
    assert skill["name"] == "recon-net" and skill["hit"]
    assert trace["idle"] == []  # 区间内无游离段
    assert kinds.count("finding") == 1
    finding_step = next(s for s in trace["steps"] if s["kind"] == "finding")
    assert finding_step["finding_id"] == fid and finding_step["title"] == "SQL 注入"


def test_trace_idle_excluded_and_window_boundary(bb, pid):
    """区间外活动进 idle 不进任务步（R1）。"""
    sid = _session(bb, pid)
    _emit(bb, pid, sid, "command", {"call_id": "c0", "cmd": "ls", "runtime": "host"})  # 认领前=游离
    tid = _run_task(bb, pid, sid)
    _work_events(bb, pid, sid)
    TaskQueue(bb).complete(tid, sid)
    _emit(bb, pid, sid, "command", {"call_id": "c2", "cmd": "pwd", "runtime": "host"})  # 收尾后=游离

    trace = traces.build_task_trace(bb, pid, tid)
    assert [s["kind"] for s in trace["steps"]] == ["tools", "kb", "skill"]  # 区间内
    idle_cmds = [s["cmds"][0] for s in trace["idle"] if s["kind"] == "tools"]
    assert idle_cmds == ["ls", "pwd"]  # 前后各一组游离命令


def test_trace_reopen_multi_window_cross_session(bb, pid):
    """fail→reopen→换会话重跑：两段区间两会话，steps 时间序合并（R1）。"""
    tq = TaskQueue(bb)
    s1 = _session(bb, pid, "S1")
    tid = _run_task(bb, pid, s1)
    _emit(bb, pid, s1, "command", {"call_id": "a1", "cmd": "nmap -p80 10.0.0.1", "runtime": "host"})
    tq.fail(tid, s1, "中断")
    tq.reopen(tid)

    s2 = _session(bb, pid, "S2")
    tq.claim(tid, s2)
    _emit(bb, pid, s2, "kb.open", {"source": "pack", "module": "web/xss", "path": "y.md"})
    tq.complete(tid, s2)

    trace = traces.build_task_trace(bb, pid, tid)
    assert len(trace["windows"]) == 2
    assert {w["session_id"] for w in trace["windows"]} == {s1, s2}
    kinds = [s["kind"] for s in trace["steps"]]
    assert kinds == ["tools", "kb"]  # 跨会话按时间序
    assert trace["task"]["claimed_by"] == s2


def test_trace_task_not_found_and_never_claimed(bb, pid):
    assert traces.build_task_trace(bb, pid, "task-none") is None
    tid = TaskQueue(bb).publish(pid, "没人认领")
    trace = traces.build_task_trace(bb, pid, tid)
    assert trace is not None and trace["windows"] == [] and trace["steps"] == []


# ---------- R3 物化（轨 B 持久链） ----------

def test_finish_done_auto_materializes(bb, pid):
    """_finish done 分支自动物化（R3 双触发①）——不需要显式调 materialize。"""
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    _work_events(bb, pid, sid)
    TaskQueue(bb).complete(tid, sid)
    chain = _trace_chain(bb, pid)
    assert chain is not None and chain["link_count"] >= 2
    assert chain["origin"] == "trace" and chain["status"] == "hypothesis"
    ev = bb.conn.execute(
        "SELECT payload FROM events WHERE project_id=? AND kind='chain.created'"
        " ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert ev is not None and "trace" in ev["payload"]


def test_materialize_idempotent_and_preserves_manual_links(bb, pid):
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    _work_events(bb, pid, sid)
    TaskQueue(bb).complete(tid, sid)  # 首次物化已由 _finish 完成

    cid = _trace_chain(bb, pid)["id"]
    n_first = _link_rows(bb, cid)
    assert traces.materialize_task_trace(bb, pid, tid) == cid  # 幂等：同链
    assert bb.list_chains(pid, origin="trace") and _link_rows(bb, cid) == n_first

    # 人工补挂链边（trace_ref=''）：重物化零感知保留
    fid = _add_finding(bb, pid, author="human")
    bb.add_chain_link(pid, cid, "finding", fid, "人工补充")
    traces.materialize_task_trace(bb, pid, tid)
    assert _link_rows(bb, cid) == n_first + 1
    kept = bb.conn.execute(
        "SELECT COUNT(*) AS n FROM chain_links WHERE chain_id=? AND trace_ref=''",
        (cid,)).fetchone()["n"]
    assert kept == 1


def test_materialize_structure_and_no_windows(bb, pid):
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    _work_events(bb, pid, sid)
    fid = _add_finding(bb, pid, author=sid)
    TaskQueue(bb).complete(tid, sid)

    cid = _trace_chain(bb, pid)["id"]
    rows = bb.conn.execute(
        "SELECT node_type, node_id, trace_ref FROM chain_links WHERE chain_id=?"
        " ORDER BY seq", (cid,)).fetchall()
    assert rows[0]["node_type"] == "task" and rows[0]["node_id"] == tid
    assert all(r["trace_ref"] == f"task-{tid}" for r in rows)
    node_ids = {r["node_id"] for r in rows}
    assert f"{tid}#skill:recon-net" in node_ids  # 机器可读 node_id（R4 统计依赖）
    assert f"{tid}#kb:web/nmap-recipes" in node_ids
    assert any(r["node_type"] == "finding" and r["node_id"] == fid for r in rows)

    # 从未认领的任务不建链
    tid2 = TaskQueue(bb).publish(pid, "无主任务")
    assert traces.materialize_task_trace(bb, pid, tid2) is None
    assert len(bb.list_chains(pid, origin="trace")) == 1


def test_verified_via_patch_upgrades_chain(bb, pid):
    """patch_finding 升 verified → 链自动重物化升 validated（R3 双触发②）。"""
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    fid1 = _add_finding(bb, pid, author=sid)  # 无 POC
    fid2 = _add_finding(bb, pid, author=sid, with_poc=True, title="RCE")  # 带 POC
    TaskQueue(bb).complete(tid, sid)  # done 自动物化（hypothesis，两发现都入链）
    cid = _trace_chain(bb, pid)["id"]
    assert _chain_status(bb, cid) == "hypothesis"

    with pytest.raises(ValueError):  # 渗透轨门禁：verified 漏洞必须带 POC
        bb.patch_finding(pid, fid1, status="verified", track="pentest")
    assert _chain_status(bb, cid) == "hypothesis"

    # 带 POC 的发现升级 verified → 链升 validated
    bb.patch_finding(pid, fid2, status="verified", track="pentest")
    assert _chain_status(bb, cid) == "validated"

    # 只升不降：发现翻 FP 后重物化不降级
    bb.patch_finding(pid, fid2, status="false-positive", track="pentest")
    traces.materialize_task_trace(bb, pid, tid)
    assert _chain_status(bb, cid) == "validated"


def test_verified_via_merge_report_triggers_materialize(bb, pid):
    """add_finding 重报升级 verified（merge 分支）→ 触发重物化（R3 双触发②）。"""
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    _add_finding(bb, pid, author=sid, with_poc=True)
    TaskQueue(bb).complete(tid, sid)
    cid = _trace_chain(bb, pid)["id"]
    assert _chain_status(bb, cid) == "hypothesis"

    # 重报同 dedup 键升级 verified（merge 分支）——链应自动升 validated
    bb.add_finding(pid, vuln_class="sqli", title="SQL 注入", severity="high",
                   status="verified", evidence={"poc": {"raw": "id=1"}},
                   dedup_key="dk-SQL 注入", author=sid, track="pentest")
    assert _chain_status(bb, cid) == "validated"


def test_materialize_for_finding_reverse_lookup(bb, pid):
    """materialize_for_finding：链上没有该发现时按 finding.new 事件反查任务。"""
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    fid = _add_finding(bb, pid, author=sid, with_poc=True)
    TaskQueue(bb).complete(tid, sid)  # 已物化（此时该发现已随 finding.new 入链）
    cid = _trace_chain(bb, pid)["id"]

    # 走链上已有路径：直接重物化所属链
    assert traces.materialize_for_finding(bb, pid, fid) == cid

    # 删掉链上的 finding 节点模拟「发现晚于上次物化」→ 走事件反查路径补回
    with bb._tx():
        bb.conn.execute(
            "DELETE FROM chain_links WHERE chain_id=? AND node_type='finding'", (cid,))
    assert traces.materialize_for_finding(bb, pid, fid) == cid
    rows = bb.conn.execute(
        "SELECT node_id FROM chain_links WHERE chain_id=? AND node_type='finding'",
        (cid,)).fetchall()
    assert {r["node_id"] for r in rows} == {fid}

    # 对话轮产出（事件无任务区间）返 None 不强行入链（独立 dedup 键防撞合并）
    s2 = _session(bb, pid, "chat-only")
    fid2 = _add_finding(bb, pid, author=s2, title="闲聊发现")
    assert traces.materialize_for_finding(bb, pid, fid2) is None


# ---------- R6 board_graph 防御过滤 + R4 效果榜 ----------

def test_board_graph_excludes_trace_chains(bb, pid):
    """R6 双保险的「保险②」判别场景：trace 链只含 task+finding 两节点——两端都在
    全景节点集（没有 step 隔断），若无 origin 过滤会出 chain 边；过滤后必须为零。"""
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    _add_finding(bb, pid, author=sid, with_poc=True)  # 区间内产出（finding.new 落窗内）
    TaskQueue(bb).complete(tid, sid)
    trace_cid = _trace_chain(bb, pid)["id"]
    links = bb.conn.execute(
        "SELECT node_type FROM chain_links WHERE chain_id=? ORDER BY seq",
        (trace_cid,)).fetchall()
    assert [r["node_type"] for r in links] == ["task", "finding"]  # 判别前提

    manual_cid = bb.create_chain(pid, "手工链")
    f1 = _add_finding(bb, pid, author="human", title="人工甲")
    f2 = _add_finding(bb, pid, author="human", title="人工乙")
    bb.add_chain_link(pid, manual_cid, "finding", f1, "入口")
    bb.add_chain_link(pid, manual_cid, "finding", f2, "承接")  # 两节点才有相邻对成边

    g = board_graph(bb, pid)
    chain_edge_ids = {e["chain_id"] for e in g["edges"] if e.get("chain_id")}
    assert chain_edge_ids == {manual_cid}  # trace 链不出边（origin 过滤，R6）


def test_effect_stats_combo_counting(bb, pid):
    """R4：(skill × kb) 组合 × verified finding 计数（物化侧）。"""
    # 任务 1：recon-net + web/nmap-recipes + 1 verified
    s1 = _session(bb, pid, "S1")
    t1 = _run_task(bb, pid, s1, "任务一")
    _work_events(bb, pid, s1)
    f1 = _add_finding(bb, pid, author=s1, with_poc=True)
    TaskQueue(bb).complete(t1, s1)
    bb.patch_finding(pid, f1, status="verified", track="pentest")

    # 任务 2：同组合 + 2 findings（1 verified）→ 组合计数累加
    s2 = _session(bb, pid, "S2")
    t2 = _run_task(bb, pid, s2, "任务二")
    _work_events(bb, pid, s2)
    f2 = _add_finding(bb, pid, author=s2, with_poc=True, title="XSS")
    _add_finding(bb, pid, author=s2, title="仅记录")  # unverified 不计
    TaskQueue(bb).complete(t2, s2)
    bb.patch_finding(pid, f2, status="verified", track="pentest")

    stats = traces.effect_stats(bb, pid)
    assert stats["trace_chains"] == 2
    combos = {(c["skill"], c["kb"]): c for c in stats["combos"]}
    cell = combos[("recon-net", "web/nmap-recipes")]
    assert cell["chains"] == 2 and cell["verified_findings"] == 2
    assert stats["combos"][0]["verified_findings"] == 2


def test_effect_stats_ignores_unverified_only_chains(bb, pid):
    s1 = _session(bb, pid, "S1")
    t1 = _run_task(bb, pid, s1)
    _work_events(bb, pid, s1)
    TaskQueue(bb).complete(t1, s1)
    stats = traces.effect_stats(bb, pid)
    assert stats["trace_chains"] == 1 and stats["combos"] == []


# ---------- M3 kb 模块三象限反馈（experience-sedimentation） ----------

def test_kb_module_feedback_three_quadrants(bb, pid):
    """opened=kb.open 全历史计数；positive=「有 verified 发现的轨迹链」内 kb
    （链级去重）；negative=真失败（error）任务窗内 kb.open——aborted 失败不算。"""
    tq = TaskQueue(bb)
    # 成功任务：web/nmap-recipes 开 2 次 + verified 发现 → opened=2, positive=1
    s1 = _session(bb, pid, "S1")
    t1 = _run_task(bb, pid, s1, "成功任务")
    _emit(bb, pid, s1, "kb.open", {"source": "pack", "module": "web/nmap-recipes", "path": "a.md"})
    _emit(bb, pid, s1, "kb.open", {"source": "pack", "module": "web/nmap-recipes", "path": "a.md"})
    _add_finding(bb, pid, s1, status="verified", with_poc=True)
    tq.complete(t1, s1)

    # 真失败任务（error）：web/xss-anti 开 1 次 → negative=1
    s2 = _session(bb, pid, "S2")
    t2 = _run_task(bb, pid, s2, "失败任务")
    _emit(bb, pid, s2, "kb.open", {"source": "pack", "module": "web/xss-anti", "path": "b.md"})
    tq.fail(t2, s2, "payload 全被拦")  # blocked_reason 默认 error

    # aborted 失败：web/xss-anti 再开 1 次 → opened 计、negative 不计
    s3 = _session(bb, pid, "S3")
    t3 = _run_task(bb, pid, s3, "中断任务")
    _emit(bb, pid, s3, "kb.open", {"source": "pack", "module": "web/xss-anti", "path": "b.md"})
    tq.fail(t3, s3, "人工中断", blocked_reason="aborted")

    fb = traces.kb_module_feedback(bb.conn, pid)
    assert fb["web/nmap-recipes"] == {"opened": 2, "positive": 1, "negative": 0}
    assert fb["web/xss-anti"] == {"opened": 2, "positive": 0, "negative": 1}


# ---------- v19 迁移 ----------

def test_v19_columns_present_and_legacy_upgrade(tmp_path):
    """新库两列在位；旧库（v18 形态缺两列）幂等 ALTER 补齐且存量行 DEFAULT 兜底。"""
    from core.blackboard.schema import SCHEMA_VERSION

    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.executescript("""
        CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT, domain TEXT DEFAULT '',
                              track TEXT DEFAULT '', capabilities TEXT DEFAULT '[]',
                              config TEXT DEFAULT '{}', created_at TEXT);
        INSERT INTO projects VALUES('p1','旧','pentest','pentest','[]','{}','2026-01-01');
        CREATE TABLE chains(id TEXT PRIMARY KEY, project_id TEXT, name TEXT,
                            goal TEXT DEFAULT '', status TEXT DEFAULT 'hypothesis',
                            created_at TEXT, updated_at TEXT);
        CREATE TABLE chain_links(id TEXT PRIMARY KEY, chain_id TEXT, seq INTEGER,
                                 node_type TEXT, node_id TEXT,
                                 edge_note TEXT DEFAULT '', created_at TEXT);
    """)
    raw.commit()
    raw.close()

    bb2 = Blackboard(str(db))
    try:
        ver = bb2.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(ver["value"]) == SCHEMA_VERSION
        chain_cols = {r[1] for r in bb2.conn.execute("PRAGMA table_info(chains)")}
        clink_cols = {r[1] for r in bb2.conn.execute("PRAGMA table_info(chain_links)")}
        assert "origin" in chain_cols and "trace_ref" in clink_cols
        # 存量人工链行 DEFAULT 兜底且读取无碍
        cid = bb2.create_chain("p1", "存量语义链")
        row = bb2.conn.execute("SELECT origin FROM chains WHERE id=?", (cid,)).fetchone()
        assert row["origin"] == "manual"
    finally:
        bb2.close()
