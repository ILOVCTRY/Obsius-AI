"""任务尝试树 v2 测试（docs/plans/归档-已完成/task-attempt-tree.md，2026-09-27）。

v2 意图驱动改版：意图嵌套（basis_refs finding 引用→子意图挂发现下）、
发现归属（outcome_refs 显式 > 存活窗+作者 > 游离兜底桶）、current=open 意图、
bb_add_finding 必挂意图门禁、API 端点 /tree/{task_id} 与 task-graph 410 过渡。
"""

import time

import pytest

from core.blackboard import Blackboard, TaskQueue
from core.blackboard import tasktree
from core.blackboard.intents import close_intent, declare_intent


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def pid(bb):
    return bb.create_project("尝试树-测试", "ctf", ["binary"])["id"]


def _session(bb, pid, name="S1"):
    return bb.register_session(pid, name)["id"]


def _run_task(bb, pid, sid, objective="拿 flag"):
    tq = TaskQueue(bb)
    tid = tq.publish(pid, objective)
    tq.claim(tid, sid)
    return tid


def _emit(bb, pid, sid, kind, payload):
    bb.append_event(pid, kind, payload, session_id=sid, author=sid)


def _intent_node(tree, iid):
    return next(n for n in tree["nodes"] if n["id"] == iid)


def _finding_nodes(tree):
    return [n for n in tree["nodes"] if n["kind"] == "finding"]


def test_tree_intent_dead_end_and_current(bb, pid):
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    _emit(bb, pid, sid, "command", {"call_id": "c1", "cmd": "probe", "runtime": "host"})
    eid = bb.conn.execute(
        "SELECT id FROM events WHERE project_id=? AND kind='command' ORDER BY id DESC LIMIT 1",
        (pid,)).fetchone()["id"]
    intent = declare_intent(bb, pid, "flag 在主校验函数里", author=sid)
    i2 = declare_intent(bb, pid, "对入口函数进行 hook 尝试", author=sid)

    tree = tasktree.build_task_tree(bb, pid, tid)
    assert tree["task"]["id"] == tid
    intents = [n for n in tree["nodes"] if n["kind"] == "intent"]
    assert len(intents) == 2 and all(n["parent"] == "" for n in intents)
    assert tree["current"]["intent_id"] == i2["id"]  # 最新声明的 open 意图=当前

    close_intent(bb, pid, intent["id"], "dead_end", dead_reason="全量检索无 flag 字符串",
                 evidence_refs=[f"event:{eid}"])
    tree = tasktree.build_task_tree(bb, pid, tid)
    it = _intent_node(tree, intent["id"])
    assert it["status"] == "closed" and it["outcome_type"] == "dead_end"
    assert "flag 字符串" in it["dead_reason"]
    assert tree["current"]["intent_id"] == i2["id"]  # 收尾不指向死路

    # 收尾后 current 归位（任务终态）
    TaskQueue(bb).complete(tid, sid, "flag 到手")
    tree = tasktree.build_task_tree(bb, pid, tid)
    assert tree["task"]["status"] == "done" and tree["current"]["intent_id"] is None


def test_tree_finding_attachment_and_child_intents(bb, pid):
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    root = declare_intent(bb, pid, "对登录口进行 sql 注入尝试", author=sid)
    fid = bb.add_finding(pid, "sqli", "联合查询注入点", severity="high",
                         dedup_key="dk-1", author=sid, track="ctf")["id"]
    # 收尾把发现显式挂到根意图（outcome_refs 归属①）
    close_intent(bb, pid, root["id"], "vuln", finding_ids=[fid])
    # 从新发现长出两个子意图（basis_refs 归属）
    c1 = declare_intent(bb, pid, "用注入点拖库导出管理员表", author=sid,
                        basis_refs=[f"finding:{fid}"])
    c2 = declare_intent(bb, pid, "对注入点进行写文件尝试", author=sid,
                        basis_refs=[f"finding:{fid}"])

    tree = tasktree.build_task_tree(bb, pid, tid)
    f = next(n for n in _finding_nodes(tree) if n["id"] == fid)
    assert f["parent"] == root["id"]
    assert f["title"] == "联合查询注入点" and f["severity"] == "high"
    assert _intent_node(tree, c1["id"])["parent"] == fid
    assert _intent_node(tree, c2["id"])["parent"] == fid
    assert tree["current"]["intent_id"] == c2["id"]


def test_tree_finding_window_attribution_and_orphans(bb, pid):
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    i1 = declare_intent(bb, pid, "对 8080 端口进行弱口令尝试", author=sid)
    fid = bb.add_finding(pid, "weak-password", "tomcat 弱口令", severity="info",
                         dedup_key="dk-2", author=sid, track="ctf")["id"]
    # 收尾不引用发现 → 走存活窗归属②（finding.new 落在意图 open 窗内、同会话）
    close_intent(bb, pid, i1["id"], "finding", finding_ids=[fid])

    tree = tasktree.build_task_tree(bb, pid, tid)
    f = next(n for n in _finding_nodes(tree) if n["id"] == fid)
    assert f["parent"] == i1["id"]  # outcome_refs 归属（同窗优先级①的显式路径）

    # 游离发现（历史数据形态：直接绕过工具层落库）→ 兜底桶
    time.sleep(1.1)  # now() 秒级精度：保证 finding.new 时刻落在意图窗之外
    fid2 = bb.add_finding(pid, "info-leak", "目录遍历", severity="low",
                          dedup_key="dk-3", author="human", track="ctf")["id"]
    _emit(bb, pid, sid, "finding.new", {"finding_id": fid2, "severity": "low"})
    tree = tasktree.build_task_tree(bb, pid, tid)
    f2 = next(n for n in _finding_nodes(tree) if n["id"] == fid2)
    assert f2["parent"] == ""  # 游离 → 前端归 _orphan 兜底桶
    bucket = next(n for n in tree["nodes"] if n["kind"] == "bucket")
    assert "历史" in bucket["title"]


def test_tree_empty_and_missing(bb, pid):
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    tree = tasktree.build_task_tree(bb, pid, tid)
    assert tree["nodes"] == []  # 未声明任何意图=空树（只有根由前端画）
    assert tree["current"]["intent_id"] is None
    assert tasktree.build_task_tree(bb, pid, "task-nope") is None


# ---------- bb_add_finding 必挂意图门禁 ----------

def test_add_finding_requires_open_intent(bb, pid):
    """bb_add_finding 必挂意图门禁（2026-09-27 agent-loop 修复后口径：门禁对
    任务上下文生效——对话轮/无委托窗无任务树可挂，不设拦）。"""
    from core.agent.tools import ToolDispatcher
    sid = _session(bb, pid)
    tid = _run_task(bb, pid, sid)
    d = ToolDispatcher(bb, gateway=None, tq=TaskQueue(bb),
                       project_id=pid, session_id=sid, author=sid, track="ctf")
    d.current_task_id = tid  # 认领任务上下文（此前门禁不区分任务/对话轮）
    d.dispatch("task_plan", {"steps": [{"id": "p1", "title": "探测", "status": "todo"}]})  # 先过 A2 计划闸

    out = d.dispatch("bb_add_finding", {"vuln_class": "sqli", "title": "x", "severity": "high"})
    assert out.startswith("[拒绝]") and "declare_intent" in out  # 无 open 意图 → 拒
    assert bb.conn.execute("SELECT COUNT(*) c FROM findings WHERE project_id=?", (pid,)
                           ).fetchone()["c"] == 0

    declare_intent(bb, pid, "对登录口进行 sql 注入尝试", author=sid)
    out = d.dispatch("bb_add_finding", {"vuln_class": "sqli", "title": "x", "severity": "high"})
    assert out.startswith("finding=")  # 挂上意图后放行

    # 无任务（对话轮）登记发现不受意图门禁约束（agent-loop 修复）
    d.current_task_id = None
    out = d.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "y",
                                        "severity": "low"})
    assert out.startswith("finding=")


# ---------- API 端点 ----------

@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient
    from core.api.app import create_app
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        yield c


def test_tree_endpoint_and_task_graph_410(client, tmp_path):
    r = client.post("/api/projects", json={"name": "树-端点", "track": "ctf",
                                           "capabilities": ["binary"]})
    assert r.status_code == 201
    pid = r.json()["id"]
    proj = client.app.state.projects[pid]
    tid = _run_task(proj.bb, pid, _session(proj.bb, pid))

    resp = client.get(f"/api/projects/{pid}/tree/{tid}")
    assert resp.status_code == 200
    tree = resp.json()
    assert tree["task"]["id"] == tid and tree["task"]["status"] == "claimed"
    assert tree["nodes"] == []  # 未声明意图前只有根

    assert client.get(f"/api/projects/{pid}/tree/task-nope").status_code == 404
    # task-graph 退役过渡：410 指路任务树
    r410 = client.get(f"/api/projects/{pid}/task-graph")
    assert r410.status_code == 410 and "tree" in r410.json()["detail"]
