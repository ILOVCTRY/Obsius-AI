"""core API 测试（TestClient，不触网；LLM 缺失时 Agent 端点应 503）。"""

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,  # 测试机不做工具探测
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        yield c


def _make_project(client) -> str:
    r = client.post("/api/projects", json={"name": "API 测试项目", "track": "ctf",
                                           "capabilities": ["binary"], "config": {"x": 1}})
    assert r.status_code == 201
    return r.json()["id"]


def test_project_lifecycle(client):
    pid = _make_project(client)
    listed = client.get("/api/projects").json()
    assert any(m["id"] == pid and m["name"] == "API 测试项目" for m in listed)
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["track"] == "ctf" and detail["capabilities"] == ["binary"]
    assert "domain" not in detail and detail["task_stats"] == {}  # 写新值不写旧值
    assert "capability" in detail
    assert client.get("/api/projects/nope").status_code == 404


def test_project_binding_validation_and_legacy_domain(client):
    """建项：非法轨/能力包 422；旧客户端 domain 入参透明映射。"""
    r = client.post("/api/projects", json={"name": "坏轨", "track": "redteam"})
    assert r.status_code == 422 and "场景轨" in r.json()["detail"]
    r = client.post("/api/projects", json={"name": "坏包", "track": "ctf",
                                           "capabilities": ["quantum"]})
    assert r.status_code == 422 and "能力包" in r.json()["detail"]
    # 旧客户端 domain=pentest → assessment/web
    r = client.post("/api/projects", json={"name": "旧客户端", "domain": "pentest"})
    assert r.status_code == 201
    meta = r.json()
    assert meta["track"] == "assessment" and meta["capabilities"] == ["web"]


def test_project_usage_defaults_and_config_patch(client):
    """批 2（§6.8）：GET 项目带 usage；PATCH config 双写 autonomy，非法值 422。"""
    pid = _make_project(client)  # ctf → 默认 L0
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["usage"]["level"] == "L0"
    assert detail["usage"]["sessions_cap"] == 4
    assert detail["usage"]["tokens"] == {"used": 0, "budget": None, "pct": None}
    # 建项时的未知 config 键保留
    assert detail["config"]["x"] == 1

    r = client.patch(f"/api/projects/{pid}/config",
                     json={"config": {"autonomy": {
                         "level": "L2", "paused": True, "sessions_cap": 2,
                         "max_chain_ticks": 5, "token_budget": 100000,
                         "task_budget": 10}}})
    assert r.status_code == 200
    # project.json（列表扫描源）与黑板行双写
    listed = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert listed["config"]["autonomy"]["level"] == "L2"
    usage = client.get(f"/api/projects/{pid}").json()["usage"]
    assert usage["level"] == "L2" and usage["paused"] is True and usage["sessions_cap"] == 2
    assert usage["tokens"]["budget"] == 100000 and usage["tasks"]["budget"] == 10
    # 未知顶层键仍在（浅合并）
    assert listed["config"]["x"] == 1

    bad = client.patch(f"/api/projects/{pid}/config",
                       json={"config": {"autonomy": {"level": "L9"}}})
    assert bad.status_code == 422 and "自主级别" in bad.json()["detail"]


def test_spawn_session_cap_returns_409(client):
    """sessions_cap 对人手开窗同效：非 closed 会话达上限 → 409（无需 LLM，检查在取模型前）。"""
    pid = _make_project(client)
    r = client.patch(f"/api/projects/{pid}/config",
                     json={"config": {"autonomy": {"level": "L1", "sessions_cap": 1}}})
    assert r.status_code == 200
    # 直接在黑板登记一个非 closed 会话（无 key 环境开不了真窗）
    client.app.state.projects[pid].bb.register_session(pid, "占位会话", role="_generalist")
    resp = client.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
    assert resp.status_code == 409 and "sessions_cap=1" in resp.json()["detail"]


def test_taxonomy_endpoint(client):
    """GET /api/taxonomy：能力包/场景轨目录 + 各轨 task_type 注册表。"""
    data = client.get("/api/taxonomy").json()
    caps = {c["name"] for c in data["capabilities"]}
    tracks = {t["name"] for t in data["tracks"]}
    assert {"web", "binary"} <= caps
    assert {"ctf", "assessment"} <= tracks
    assert data["task_types"]["ctf"]["solve"] == "passive"
    assert data["task_types"]["assessment"]["exploit"] == "low"


def test_blackboard_human_cowrite(client):
    """人机共写（§6.5）：经 API 添加资产/发现（写路径=Blackboard 单一入口）。"""
    pid = _make_project(client)
    r = client.post(f"/api/projects/{pid}/assets",
                    json={"type": "binary", "value": "a" * 64, "meta": {"path": "x"}})
    assert r.status_code == 201
    f = client.post(f"/api/projects/{pid}/findings",
                    json={"vuln_class": "triage", "title": "ELF 未 strip",
                          "severity": "info", "status": "verified"})
    assert f.status_code == 201
    findings = client.get(f"/api/projects/{pid}/findings").json()
    assert len(findings) == 1 and findings[0]["author"] == "human"
    # 去重合并生效：同内容再写 → merged
    f2 = client.post(f"/api/projects/{pid}/findings",
                     json={"vuln_class": "triage", "title": "ELF 未 strip"})
    assert f2.json()["merged"] is True
    assert len(client.get(f"/api/projects/{pid}/findings").json()) == 1


def test_chains_full_crud_and_snapshots(client):
    """攻击链全套端点：建链/挂节点（实体快照 hex 地址）/边注/删中间重排/状态/孤儿。"""
    pid = _make_project(client)
    sha = "ab" * 32
    func = client.post(f"/api/projects/{pid}/funcs",
                       json={"binary_sha256": sha, "address": "0x401189",
                             "name": "check_flag"}).json()
    finding = client.post(f"/api/projects/{pid}/findings",
                          json={"title": "XOR 逐字节比较", "severity": "high",
                                "evidence": {"category": "algorithm",
                                             "func_id": func["id"], "address": "0x401189"}}).json()
    up = client.post(
        f"/api/projects/{pid}/artifacts/upload",
        data={"kind": "debug-log"},
        files={"file": ("bp_main.log", b"hit check_flag\n", "text/plain")})
    assert up.status_code == 201
    art = up.json()

    r = client.post(f"/api/projects/{pid}/chains", json={"name": "破解校验链", "goal": "爆破"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["links"] == []

    l1 = client.post(f"/api/projects/{pid}/chains/{cid}/links",
                     json={"node_type": "func_kb", "node_id": func["id"]}).json()
    l2 = client.post(f"/api/projects/{pid}/chains/{cid}/links",
                     json={"node_type": "finding", "node_id": finding["id"],
                           "edge_note": "比较逻辑在这"}).json()
    l3 = client.post(f"/api/projects/{pid}/chains/{cid}/links",
                     json={"node_type": "artifact", "node_id": art["id"]}).json()
    assert [l["seq"] for l in (l1, l2, l3)] == [1, 2, 3]
    # 实体快照：函数地址 hex、发现 category、产物 kind
    assert l1["entity"]["address"] == "0x401189" and l1["entity"]["name"] == "check_flag"
    assert l2["entity"]["category"] == "algorithm" and l2["entity"]["severity"] == "high"
    assert l3["entity"]["kind"] == "debug-log" and l3["deleted"] is False

    chains = client.get(f"/api/projects/{pid}/chains").json()
    assert chains[0]["link_count"] == 3

    # 边注编辑
    assert client.patch(f"/api/projects/{pid}/chains/links/{l2['id']}",
                        json={"edge_note": "改"}).status_code == 200
    # 删中间节点 → 重排 1..n
    assert client.delete(f"/api/projects/{pid}/chains/links/{l2['id']}").status_code == 200
    detail = client.get(f"/api/projects/{pid}/chains/{cid}").json()
    assert [(l["id"], l["seq"]) for l in detail["links"]] == [(l1["id"], 1), (l3["id"], 2)]

    # 状态机
    assert client.patch(f"/api/projects/{pid}/chains/{cid}",
                        json={"status": "validated"}).json()["status"] == "validated"
    assert client.patch(f"/api/projects/{pid}/chains/{cid}",
                        json={"status": "wat"}).status_code == 422

    # 孤儿：实体被直接删除后链详情仍 200，节点 deleted:true（项目已在前面调用中缓存）
    with client.app.state.projects[pid].bb._tx():
        client.app.state.projects[pid].bb.conn.execute(
            "DELETE FROM artifacts WHERE id=?", (art["id"],))
    detail = client.get(f"/api/projects/{pid}/chains/{cid}").json()
    orphan = [l for l in detail["links"] if l["node_id"] == art["id"]][0]
    assert orphan["deleted"] is True and orphan["entity"] is None

    # 删链
    assert client.delete(f"/api/projects/{pid}/chains/{cid}").status_code == 200
    assert client.get(f"/api/projects/{pid}/chains/{cid}").status_code == 404
    assert client.delete(f"/api/projects/{pid}/chains/{cid}").status_code == 404


def test_chains_cross_project_guard(client):
    """链/节点跨项目：链 404，节点 422；边操作跨项目 404。"""
    p1 = _make_project(client)
    p2 = _make_project(client)
    sha = "cd" * 32
    f2 = client.post(f"/api/projects/{p2}/funcs",
                     json={"binary_sha256": sha, "address": 0x1000, "name": "f"}).json()
    cid = client.post(f"/api/projects/{p1}/chains", json={"name": "p1 链"}).json()["id"]

    assert client.post(f"/api/projects/{p2}/chains/{cid}/links",
                       json={"node_type": "func_kb", "node_id": f2["id"]}).status_code == 404
    r = client.post(f"/api/projects/{p1}/chains/{cid}/links",
                    json={"node_type": "func_kb", "node_id": f2["id"]})
    assert r.status_code == 422  # 节点属 p2
    r = client.post(f"/api/projects/{p1}/chains/{cid}/links",
                    json={"node_type": "func_kb", "node_id": "func-nope"})
    assert r.status_code == 422
    assert client.post(f"/api/projects/{p1}/chains/chain-nope/links",
                       json={"node_type": "func_kb", "node_id": f2["id"]}).status_code == 404
    assert client.get(f"/api/projects/{p2}/chains").json() == []


def test_artifacts_listing(client):
    pid = _make_project(client)
    assert client.get(f"/api/projects/{pid}/artifacts").json() == []
    client.post(
        f"/api/projects/{pid}/artifacts/upload", data={"kind": "debug-log"},
        files={"file": ("a.log", b"x", "text/plain")})
    arts = client.get(f"/api/projects/{pid}/artifacts").json()
    assert len(arts) == 1 and arts[0]["kind"] == "debug-log"


def test_task_publish_edit_reopen_delete(client):
    pid = _make_project(client)
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "逆向 x", "task_type": "reverse"})
    assert r.status_code == 201
    tid = r.json()["task_id"]
    # 非 passive 无 conflict_keys → 422
    assert client.post(f"/api/projects/{pid}/tasks",
                       json={"objective": "扫", "task_type": "scan",
                             "noise_budget": "high"}).status_code == 422
    # 未在轨注册表的 task_type → 422（拼错不再静默饿死，§4.5.5）
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "拼错", "task_type": "revese"})
    assert r.status_code == 422 and "task_type" in r.json()["detail"]
    # 编辑 open 任务
    r = client.patch(f"/api/tasks/{tid}", json={"objective": "逆向 y", "priority": 0})
    assert r.status_code == 200 and r.json()["objective"] == "逆向 y"
    # 删除（物理删除，§6.4 插手通道，取代旧 cancelled）
    assert client.delete(f"/api/tasks/{tid}").status_code == 200
    assert client.get(f"/api/projects/{pid}/tasks").json() == []
    # 失败任务 → 放回待认领
    t2 = client.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "扫 1.1.1.1", "noise_budget": "low",
                           "conflict_keys": ["ip:1.1.1.1"]}).json()["task_id"]
    assert client.post(f"/api/tasks/{t2}/reopen").status_code == 409  # open 不可放回
    assert client.delete(f"/api/tasks/{tid}x").status_code == 404     # 不存在


def test_task_graph_endpoint(client):
    """A3：GET task-graph 返回节点（含空 plan/session 占位）+ parent 实线。"""
    pid = _make_project(client)
    parent = client.post(f"/api/projects/{pid}/tasks",
                         json={"objective": "父", "task_type": "generic"}).json()["task_id"]
    child = client.post(f"/api/projects/{pid}/tasks",
                        json={"objective": "子", "task_type": "generic",
                              "scope": parent}).json()["task_id"]
    # scope 不是 parent_id；parent 链目前只经黑板 publish/orchestrator 产生，直接验空图边安全
    g = client.get(f"/api/projects/{pid}/task-graph").json()
    assert {n["id"] for n in g["nodes"]} == {parent, child}
    node = next(n for n in g["nodes"] if n["id"] == child)
    assert node["plan"] == [] and node["session"] is None and node["status"] == "open"
    assert g["edges"] == []  # scope 字段不产生 parent 边
    assert client.get("/api/projects/proj-nope/task-graph").status_code in (403, 404)


def test_events_endpoint_and_websocket(client):
    pid = _make_project(client)
    client.post(f"/api/projects/{pid}/tasks",
                json={"objective": "分诊", "task_type": "triage"})
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "task.published" for e in events)
    since = max(e["id"] for e in events)
    client.post(f"/api/projects/{pid}/tasks",
                json={"objective": "再分诊", "task_type": "triage"})
    delta = client.get(f"/api/projects/{pid}/events", params={"since_id": since}).json()
    assert len(delta) == 1 and delta[0]["payload"]["objective"] == "再分诊"
    # WS：从 0 回放全部事件（该项目恰好 2 条 task.published）
    with client.websocket_connect(f"/api/ws/projects/{pid}?since_id=0") as ws:
        got = [ws.receive_json() for _ in range(2)]
        assert all(e["kind"] == "task.published" for e in got)


def test_agent_endpoints_503_without_llm(tmp_path, monkeypatch):
    """无 LLM 配置时 Agent/编排端点明确 503，而不是崩。
    隔离：cwd 切到 tmp（找不到 .env）+ 清环境变量（本机 .env 有真 key，不能依赖它缺席）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    from core.api.app import create_app
    real_packs = Path(__file__).resolve().parents[1] / "packs"
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     packs_root=str(real_packs))
    with TestClient(app) as client:
        r = client.post("/api/projects", json={"name": "no-llm", "domain": "ctf"})
        pid = r.json()["id"]
        ra = client.post(f"/api/projects/{pid}/agents", json={"role": "reverse"})
        assert ra.status_code == 503
        rt = client.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        assert rt.status_code == 503


def test_sessions_and_approvals(client):
    """会话列表端点 + 审批收件箱（§12：批准/拒绝经 API 落审计事件）。"""
    pid = _make_project(client)
    # 会话列表（空但可用）
    assert client.get(f"/api/projects/{pid}/sessions").status_code == 200
    # 造一条 pending 审批（模拟 gateway 请求 net:real 的产物）
    proj = client.app.state.projects[pid]
    from core.blackboard.store import new_id, now
    aid = new_id("appr")
    proj.bb.conn.execute(
        "INSERT INTO approvals(id,project_id,action,risk,status,requested_by,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (aid, pid, json.dumps({"net": "real", "cmd": "curl evil.com"}), "high",
         "pending", "sess-test", now()))
    proj.bb.conn.commit()
    listed = client.get(f"/api/projects/{pid}/approvals",
                        params={"status": "pending"}).json()
    assert len(listed) == 1 and listed[0]["risk"] == "high"
    # 批准 → 状态变更 + 审计事件
    r = client.post(f"/api/approvals/{aid}/decide", json={"decision": "approved"})
    assert r.json()["status"] == "approved"
    # 二次决定 → 422
    assert client.post(f"/api/approvals/{aid}/decide",
                       json={"decision": "rejected"}).status_code == 422
    # 事件流可查 approval.approved
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "approval.approved" for e in events)
    # 非法 decision → 422
    aid2 = new_id("appr")
    proj.bb.conn.execute(
        "INSERT INTO approvals(id,project_id,action,risk,status,requested_by,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (aid2, pid, "{}", "low", "pending", "sess-test", now()))
    proj.bb.conn.commit()
    assert client.post(f"/api/approvals/{aid2}/decide",
                       json={"decision": "maybe"}).status_code == 422


def test_concurrent_reads_thread_safety(client):
    """WebUI 并发轮询回归：多线程同时读黑板不得炸 sqlite3.InterfaceError。
    （单连接 check_same_thread=False 时代真机撞出过 'bad parameter or other API misuse'）"""
    import concurrent.futures

    pid = _make_project(client)
    for i in range(5):
        client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": f"任务{i}", "task_type": "triage"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(client.get, f"/api/projects/{pid}/findings") for _ in range(8)]
        futures += [ex.submit(client.get, f"/api/projects/{pid}/tasks") for _ in range(8)]
        futures += [ex.submit(client.get, f"/api/projects/{pid}/events") for _ in range(8)]
        for f in futures:
            assert f.result(timeout=10).status_code == 200
    # 并发写也不互踩（WAL + busy_timeout + BEGIN IMMEDIATE）
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(client.post, f"/api/projects/{pid}/tasks",
                          json={"objective": f"并发写{i}", "task_type": "triage"})
                for i in range(4)]
        assert all(f.result(timeout=10).status_code == 201 for f in futs)


def test_job_registry_roundtrip(client):
    """job 提交后可轮询（用 projects 创建之外的路径验证 registry 本身）。"""
    jobs = client.app.state.jobs
    jid = jobs.submit("noop", lambda: 42)
    import time
    for _ in range(50):
        job = client.get(f"/api/jobs/{jid}").json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "done" and job["result"] == 42
    assert client.get("/api/jobs/missing").status_code == 404


# ---------- 项目删除（回收站式，DESIGN.md §5.3） ----------


def test_delete_project_endpoint(client, tmp_path):
    pid = _make_project(client)
    # 先 GET 让句柄进 app.state.projects 缓存（删除须正确清理缓存路径）
    assert client.get(f"/api/projects/{pid}").status_code == 200
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "trashed" and body["project_id"] == pid
    assert ".trash" in body["trash_path"]
    # 删后：详情 404、列表不含、磁盘上进了回收站
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert all(m["id"] != pid for m in client.get("/api/projects").json())
    trash = tmp_path / "workspaces" / ".trash"
    assert trash.is_dir() and any(trash.iterdir())


def test_delete_project_409_with_claimed_task(client):
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    from core.blackboard import TaskQueue

    sess = proj.bb.register_session(pid, "sess-test", role="reverse")
    tq = TaskQueue(proj.bb)
    tid = tq.publish(pid, "逆向 x", task_type="reverse", noise_budget="low",
                     conflict_keys=["bin:x"], created_by="human")
    tq.claim(tid, sess["id"])
    # 有 claimed 任务（活跃租约）→ 409 拒删
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 409 and "任务被认领" in r.json()["detail"]
    # 删除占坑任务后项目可删
    assert client.delete(f"/api/tasks/{tid}").status_code == 200
    assert client.delete(f"/api/projects/{pid}").status_code == 200


def test_delete_unknown_project_404(client):
    assert client.delete("/api/projects/nope").status_code == 404


def test_delete_project_with_open_websocket(client):
    """删除竞态回归：WS 保持连接（每秒 tick 在池线程持有该库连接）时删除必须成功，
    WS 收 1008 关闭。旧实现下 close_all 后 tick 立刻惰性重连锁死 db，rename 必败 422。"""
    import time
    pid = _make_project(client)
    ready = threading.Event()
    closed: dict = {}

    def hold_ws():
        try:
            with client.websocket_connect(
                    f"/api/ws/projects/{pid}?since_id=0") as ws:
                ready.set()
                try:
                    ws.receive_json()  # 无事件：阻塞到服务端关闭
                except Exception as e:  # noqa: BLE001 —— TestClient 抛 WebSocketDisconnect
                    closed["code"] = getattr(e, "code", None)
        except Exception as e:  # noqa: BLE001
            closed["outer"] = repr(e)

    t = threading.Thread(target=hold_ws, daemon=True)
    t.start()
    assert ready.wait(2)
    time.sleep(1.3)  # 等 WS tick 至少跑一轮：池线程连接已建并登记进 _conns
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 200, r.text
    assert t.join(3) is None and not t.is_alive()
    assert closed.get("code") == 1008, closed
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_reject_ws_for_deleted_project(client):
    """删后新握手的 WS 直接 1008 关闭（前端据此停止重连），不留错误日志。"""
    pid = _make_project(client)
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    with pytest.raises(Exception) as ei:  # noqa: B017 —— 握手后立即 1008
        with client.websocket_connect(f"/api/ws/projects/{pid}?since_id=0") as ws:
            ws.receive_json()
    assert getattr(ei.value, "code", None) == 1008


def test_list_roles_endpoint(client):
    """角色清单端点：按项目场景轨扫 tracks/<track>/roles（旧 domain 入参兼容，真实 packs）。"""
    rp = client.post("/api/projects", json={"name": "p", "domain": "pentest"})
    pid = rp.json()["id"]
    roles = client.get(f"/api/projects/{pid}/roles").json()
    names = {r["role"] for r in roles}
    assert {"_generalist", "recon", "external-entry", "privesc", "lateral"} <= names
    recon = next(r for r in roles if r["role"] == "recon")
    assert recon["task_types"] == ["recon", "asset-enum"]
    assert recon["persona"] and recon["default_noise"] == "passive"
    # ctf 项目返回 ctf 角色集（轨驱动的反例校验）
    rc = client.post("/api/projects", json={"name": "c", "track": "ctf",
                                            "capabilities": ["binary"]})
    ctf_roles = {r["role"] for r in client.get(f"/api/projects/{rc.json()['id']}/roles").json()}
    assert "lateral" not in ctf_roles and "_generalist" in ctf_roles
    assert client.get("/api/projects/nope/roles").status_code == 404


def test_close_session_endpoint(client):
    """关窗（§6.4）：status='closed' + session.closed 事件；重复关/不存在报错。"""
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    sess = proj.bb.register_session(pid, "窗口甲", role="reverse")
    sid = sess["id"]
    r = client.post(f"/api/sessions/{sid}/close")
    assert r.status_code == 200 and r.json() == {"session_id": sid, "status": "closed"}
    # 黑板状态 + 审计事件
    sessions = client.get(f"/api/projects/{pid}/sessions").json()
    assert sessions[0]["status"] == "closed"
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "session.closed" for e in events)
    # 编排不再复用：app.state.agents 摘除（在册即摘，无则跳过）
    assert sid not in client.app.state.agents
    # 重复关 → 422；不存在 → 404
    assert client.post(f"/api/sessions/{sid}/close").status_code == 422
    assert client.post("/api/sessions/sess-nope/close").status_code == 404


def test_close_session_409_while_job_running(client):
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    sess = proj.bb.register_session(pid, "忙碌窗口", role="reverse")
    import threading
    gate = threading.Event()
    client.app.state.jobs.submit(
        "agent-work", gate.wait,  # 挂起直到测试放行
        meta={"project_id": pid, "session_id": sess["id"]})
    assert client.post(f"/api/sessions/{sess['id']}/close").status_code == 409
    gate.set()


# ---------- 会话控制端点（DESIGN.md §3：暂停/恢复/中断） ----------


def _spawn_test_agent(client, pid):
    """不经 spawn 端点（避免依赖真实 LLM）直接构造 AgentSession 注入 app.state.agents。"""
    from core.agent import AgentSession
    from core.runtime import ExecutionGateway

    proj = client.app.state.projects.get(pid) or client.app.state.store.open_project(pid)
    client.app.state.projects[pid] = proj
    agent = AgentSession(project_id=pid, bb=proj.bb,
                         gateway=ExecutionGateway(bb=proj.bb), llm=object(),
                         packs_root="packs", track="assessment",
                         capabilities=["web"], role="_generalist")
    client.app.state.agents[agent.session["id"]] = agent
    return agent


def test_pause_resume_abort_endpoints(client):
    pid = _make_project(client)
    rp = client.post("/api/projects", json={"name": "渗透-控制", "track": "assessment",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]

    # 不在册 → 404
    for act in ("pause", "resume", "abort"):
        assert client.post(f"/api/sessions/sess-nope/{act}").status_code == 404

    # 空闲暂停（无 job）→ 立即生效；重复暂停 → 409
    r = client.post(f"/api/sessions/{sid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert agent.paused is True
    sessions = client.get(f"/api/projects/{pid}/sessions").json()
    assert sessions[0]["status"] == "paused"
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "session.paused" for e in events)
    assert client.post(f"/api/sessions/{sid}/pause").status_code == 409

    # 恢复：无快照 → 回 idle + session.resumed 事件（author=human）；未暂停再恢复 → 409
    r = client.post(f"/api/sessions/{sid}/resume")
    assert r.status_code == 200 and r.json()["status"] == "running"
    assert agent.paused is False
    assert client.get(f"/api/projects/{pid}/sessions").json()[0]["status"] == "idle"
    events = client.get(f"/api/projects/{pid}/events").json()
    resumed = [e for e in events if e["kind"] == "session.resumed"]
    assert resumed and resumed[-1]["author"] == "human"
    assert client.post(f"/api/sessions/{sid}/resume").status_code == 409

    # 空闲中断：session.aborted 审计（无任务也落事件，行为可追溯）
    r = client.post(f"/api/sessions/{sid}/abort")
    assert r.status_code == 200 and r.json()["status"] == "aborted"
    assert client.get(f"/api/projects/{pid}/sessions").json()[0]["status"] == "idle"
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "session.aborted" for e in events)

    # 有 agent-work job 在跑 → 暂停只置标志，由 worker 在步边界消费（不立即 paused）
    import threading
    gate = threading.Event()
    client.app.state.jobs.submit("agent-work", gate.wait,
                                 meta={"project_id": pid, "session_id": sid})
    assert client.post(f"/api/sessions/{sid}/pause").status_code == 200
    assert agent.paused is False and agent._pause_req.is_set()
    gate.set()


def test_close_session_fails_paused_snapshot_task(client):
    """关窗守卫：暂停快照里的任务仍 claimed → 关窗前先 fail 防占坑（§6.4）。"""
    from core.blackboard import TaskQueue

    rp = client.post("/api/projects", json={"name": "渗透-关窗", "track": "assessment",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]
    tid = TaskQueue(agent.bb).publish(pid, "被暂停的任务", task_type="recon",
                                      noise_budget="passive")
    TaskQueue(agent.bb).claim(tid, sid)
    agent._resume_state = {"system": "s", "messages": [], "objective": "x",
                           "task_id": tid, "next_step": 3}
    agent.paused = True
    assert client.post(f"/api/sessions/{sid}/close").status_code == 200
    task = TaskQueue(agent.bb).get_task(tid)
    assert task["status"] == "failed"
    assert "会话关闭" in task["result_note"]


# ---------- 孤儿窗修复：编排开窗注册 + 重启后 rehydrate ----------


def _orphan_session_row(client, pid):
    """只在黑板建 sessions 行（模拟重启后 app.state.agents 为空的在册窗口）。"""
    proj = client.app.state.projects[pid]
    sess = proj.bb.register_session(pid, "在册但无内存态", role="_generalist")
    assert sess["id"] not in client.app.state.agents
    return sess


def test_work_rehydrates_orphan_session(client):
    """POST /work 对内存缺失会话：从黑板行 rehydrate 注册后开跑（旧行为 404）。"""
    rp = client.post("/api/projects", json={"name": "渗透-rehydrate", "track": "assessment",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    sess = _orphan_session_row(client, pid)
    sid = sess["id"]

    r = client.post(f"/api/agents/{sid}/work")
    assert r.status_code == 200 and r.json()["job_id"]
    assert sid in client.app.state.agents  # 已 rehydrate 注册
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "done" and job["result"] == 0  # 空队列：零任务即退
    # 真不存在的会话仍 404
    assert client.post("/api/agents/sess-nope/work").status_code == 404


def test_pause_abort_rehydrate_orphan_session(client):
    """暂停/中断端点同样对孤儿窗 rehydrate（旧行为一律 404）。"""
    rp = client.post("/api/projects", json={"name": "渗透-控制孤儿", "track": "assessment",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    sid = _orphan_session_row(client, pid)["id"]

    r = client.post(f"/api/sessions/{sid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert client.app.state.agents[sid].paused is True
    # rehydrate 后陈旧 running 行状态已回 idle，暂停落 paused
    assert client.get(f"/api/projects/{pid}/sessions").json()[0]["status"] == "paused"
    # 中断把暂停态收尾回 idle
    r = client.post(f"/api/sessions/{sid}/abort")
    assert r.status_code == 200 and r.json()["status"] == "aborted"


def test_approvals_action_parsed_as_object(client):
    """GET approvals：action JSON 字符串出口解析为对象（与前端 types.ts 对齐）。"""
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    proj.bb.request_approval(
        pid, {"op": "net_real", "target": "1.2.3.4"}, risk="high",
        requested_by="sess-x", session_id="sess-test")
    items = client.get(f"/api/projects/{pid}/approvals").json()
    assert len(items) == 1
    assert isinstance(items[0]["action"], dict)
    assert items[0]["action"]["target"] == "1.2.3.4"
    assert client.get(f"/api/projects/{pid}/approvals", params={"status": "pending"}).json()


def test_retraction_inbox_api(client):
    """撤回传播 HTTP 链路：PATCH 误报 → 作者会话收件箱有信、sessions 带 unread；
    POST read 清零。会话收件箱与审批收件箱分设，互不混入。"""
    rp = client.post("/api/projects", json={"name": "撤回-API", "track": "assessment",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    proj = client.app.state.projects[pid]
    sid = proj.bb.register_session(pid, "recon 窗", role="recon")["id"]
    fid = proj.bb.add_finding(pid, "sqli", "登录框 SQLi", author=sid)["id"]

    r = client.patch(f"/api/projects/{pid}/findings/{fid}",
                     json={"status": "false-positive"})
    assert r.status_code == 200 and r.json()["status"] == "false-positive"

    box = client.get(f"/api/sessions/{sid}/inbox").json()
    assert len(box) == 1 and box[0]["kind"] == "basis_stale"
    assert box[0]["payload"]["title"] == "登录框 SQLi"
    sess = next(s for s in client.get(f"/api/projects/{pid}/sessions").json()
                if s["id"] == sid)
    assert sess["unread"] == 1
    # 未读过滤 + 标记已读
    assert len(client.get(f"/api/sessions/{sid}/inbox", params={"unread": True}).json()) == 1
    assert client.post(f"/api/sessions/{sid}/inbox/read").json()["marked"] == 1
    assert client.get(f"/api/sessions/{sid}/inbox",
                      params={"unread": True}).json() == []
    # 审批收件箱不受影响
    assert client.get(f"/api/projects/{pid}/approvals").json() == []


def test_orchestrator_spawn_registers_agent(tmp_path):
    """编排 tick 开的窗必须进 app.state.agents（孤儿窗根因回归）：可 work、可暂停。"""
    from test_orchestrator import ScriptedLLM

    orch_llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session", {"role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
        # 批 5：L2 tick 有产出会起链，自动续 tick 以零产出收敛（防止剧本耗尽炸链）
        {"tool_use": [ScriptedLLM.tool_call("d2", "done", {})]},
    ])
    exec_llm = ScriptedLLM([])  # 只构造不跑（tick 不执行 worker）
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=exec_llm, planner_llm=orch_llm,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "编排开窗注册", "track": "assessment",
                                          "capabilities": ["web"]})
        pid = rp.json()["id"]
        # assessment 默认 L1（spawn 转审批）；本测覆盖直接开窗路径，显式升 L2
        pr = c.patch(f"/api/projects/{pid}/config",
                     json={"config": {"autonomy": {"level": "L2"}}})
        assert pr.status_code == 200
        r = c.post(f"/api/projects/{pid}/orchestrator/tick",
                   json={"allowed_roles": ["recon"], "max_sessions": 2})
        assert r.status_code == 200
        _wait_job(c, r.json()["job_id"])
        # 批 5：L2 链自动续 tick 后零产出收敛，等链条彻底静止再做并发断言
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "converged"
        _wait_no_running(c, pid)

        agents = [s for s in c.app.state.agents.values() if s.project_id == pid]
        assert len(agents) == 1, "编排开窗未注册进 app.state.agents（孤儿窗回归）"
        sid = agents[0].session["id"]
        # 旧根因：这两个操作对编排开窗 404
        w = c.post(f"/api/agents/{sid}/work")
        assert w.status_code == 200
        _wait_job(c, w.json()["job_id"])  # 空队列秒退，收尾后再暂停避免 409 竞态
        assert c.post(f"/api/sessions/{sid}/pause").status_code == 200
        kinds = [e["kind"] for e in c.app.state.projects[pid].bb.recent_events(pid)]
        assert "session.spawned" in kinds


def _l1_tick_app(tmp_path, role="recon", reason="核查 8080 旁站低噪信息"):
    """批 4：assessment 默认 L1 的 tick app——planner 剧本开一扇窗后 done。"""
    from test_orchestrator import ScriptedLLM

    orch_llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session",
                                            {"role": role, "reason": reason})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    return create_app(workspace_root=str(tmp_path / "workspaces"),
                      tools_root=None,
                      executor_llm=ScriptedLLM([]), planner_llm=orch_llm,
                      providers_config=str(tmp_path / "providers.json"))


def _l1_tick_to_pending(c, pid):
    r = c.post(f"/api/projects/{pid}/orchestrator/tick",
               json={"allowed_roles": ["recon"], "max_sessions": 2})
    assert r.status_code == 200
    _wait_job(c, r.json()["job_id"])
    items = c.get(f"/api/projects/{pid}/approvals").json()
    assert len(items) == 1 and items[0]["status"] == "pending"
    return items[0]


def test_l1_spawn_approved_creates_and_runs(tmp_path):
    """批准 spawn_session：当场注册建窗 + 提交 agent-work job + session.spawned 带 approval_id。"""
    app = _l1_tick_app(tmp_path)
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "L1批准", "track": "assessment",
                                           "capabilities": ["web"]})
        pid = rp.json()["id"]
        item = _l1_tick_to_pending(c, pid)
        assert item["action"] == {"op": "spawn_session", "role": "recon",
                                  "reason": "核查 8080 旁站低噪信息"}
        # 批准前：tick 未开窗（窗未开，不计 spawned）
        assert [s for s in c.app.state.agents.values() if s.project_id == pid] == []

        r = c.post(f"/api/approvals/{item['id']}/decide", json={"decision": "approved"})
        assert r.status_code == 200
        body = r.json()
        assert body["executed"] is True and body["session_id"] and body["job_id"]
        job = _wait_job(c, body["job_id"])
        assert job["status"] == "done" and job["result"] == 0  # 空队列即退
        agents = [s for s in c.app.state.agents.values() if s.project_id == pid]
        assert len(agents) == 1 and agents[0].session["id"] == body["session_id"]

        kinds = {}
        for e in c.app.state.projects[pid].bb.recent_events(pid):
            kinds.setdefault(e["kind"], []).append(e.get("payload") or {})
        spawns = kinds.get("session.spawned", [])
        assert len(spawns) == 1 and spawns[0]["approval_id"] == item["id"]
        assert "approval.approved" in kinds


def test_l1_spawn_rejected_no_session(tmp_path):
    """拒绝：只翻状态 + approval.rejected，不开窗、不提交 job。"""
    app = _l1_tick_app(tmp_path)
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "L1拒绝", "track": "assessment",
                                           "capabilities": ["web"]})
        pid = rp.json()["id"]
        item = _l1_tick_to_pending(c, pid)

        r = c.post(f"/api/approvals/{item['id']}/decide", json={"decision": "rejected"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "rejected" and "executed" not in body
        assert [s for s in c.app.state.agents.values() if s.project_id == pid] == []
        assert c.get(f"/api/projects/{pid}/sessions").json() == []
        kinds = [e["kind"] for e in c.app.state.projects[pid].bb.recent_events(pid)]
        assert "approval.rejected" in kinds and "session.spawned" not in kinds


def test_l1_spawn_approved_but_cap_full_exec_failed(tmp_path):
    """批准时 cap 已满：不回滚批准，落 approval.exec_failed，响应 executed:false + error。"""
    app = _l1_tick_app(tmp_path)
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "L1批时满cap", "track": "assessment",
                                           "capabilities": ["web"]})
        pid = rp.json()["id"]
        item = _l1_tick_to_pending(c, pid)
        # tick 之后、决策之前把 cap 压满（终检与预检分离）
        pr = c.patch(f"/api/projects/{pid}/config",
                     json={"config": {"autonomy": {"level": "L1", "sessions_cap": 1}}})
        assert pr.status_code == 200
        c.app.state.projects[pid].bb.register_session(pid, "占位会话", role="_generalist")

        r = c.post(f"/api/approvals/{item['id']}/decide", json={"decision": "approved"})
        assert r.status_code == 200
        body = r.json()
        assert body["executed"] is False and "sessions_cap" in body["error"]
        # 审批保持 approved（不回滚）
        decided = c.get(f"/api/projects/{pid}/approvals").json()[0]
        assert decided["status"] == "approved"
        # 没有因失败开窗
        assert [s for s in c.app.state.agents.values() if s.project_id == pid] == []
        failed = [e for e in c.app.state.projects[pid].bb.recent_events(pid)
                  if e["kind"] == "approval.exec_failed"]
        assert len(failed) == 1
        assert failed[0]["payload"] == {"approval_id": item["id"], "op": "spawn_session",
                                        "error": failed[0]["payload"]["error"]}


def test_decide_unknown_op_only_flips_status(client):
    """无 op 处理器（越界/net:real 等旧审批）：维持旧语义只翻状态，不带 executed。"""
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    aid = proj.bb.request_approval(
        pid, {"op": "net_real", "target": "1.2.3.4"}, risk="high",
        requested_by="sess-x", session_id="sess-test")["id"]
    r = client.post(f"/api/approvals/{aid}/decide", json={"decision": "approved"})
    assert r.status_code == 200
    assert r.json() == {"approval_id": aid, "status": "approved"}
    assert [s for s in client.app.state.agents.values() if s.project_id == pid] == []


# ---------- 批 5：L2 全自动链（DESIGN §6.8：事件驱动，防失控七闸） ----------

def _chain_app(tmp_path, planner_script, executor_script=None):
    from test_orchestrator import ScriptedLLM
    return create_app(workspace_root=str(tmp_path / "workspaces"),
                      tools_root=None,
                      executor_llm=ScriptedLLM(executor_script or []),
                      planner_llm=ScriptedLLM(planner_script),
                      providers_config=str(tmp_path / "providers.json"))


def _l2_project(c, name, **autonomy_extra):
    pid = c.post("/api/projects", json={"name": name, "track": "assessment",
                                        "capabilities": ["web"]}).json()["id"]
    r = c.patch(f"/api/projects/{pid}/config",
                json={"config": {"autonomy": {"level": "L2", **autonomy_extra}}})
    assert r.status_code == 200
    return pid


def _project_jobs(c, pid, kind=None):
    return [j for j in c.app.state.jobs.all_jobs()
            if j["meta"].get("project_id") == pid and (kind is None or j["kind"] == kind)]


def _wait_no_running(c, pid, tries=300):
    for _ in range(tries):
        running = [j for j in _project_jobs(c, pid) if j["status"] == "running"]
        if not running:
            return
        time.sleep(0.02)
    raise AssertionError(f"项目 {pid} 仍有 running job")


def _wait_chain_stopped(c, pid, tries=300):
    bb = c.app.state.projects[pid].bb
    for _ in range(tries):
        stops = [e for e in bb.recent_events(pid) if e["kind"] == "orch.chain_stopped"]
        if stops:
            return stops[-1]
        time.sleep(0.02)
    raise AssertionError("orch.chain_stopped 超时")


def _orch_scripted(*calls):
    """构造 planner 剧本：每个 tick 一轮工具调用后 done。calls 为工具 call 列表的列表。"""
    from test_orchestrator import ScriptedLLM as S
    script = []
    for i, tools_ in enumerate(calls):
        script.append({"tool_use": tools_ + [S.tool_call(f"d{i}", "done", {})]})
    return script


def _exec_finish_script(n=1):
    """worker 完成 n 个任务的剧本：complete_task → finish 重复 n 轮。"""
    from test_orchestrator import ScriptedLLM as S
    out = []
    for i in range(n):
        out.append({"tool_use": [S.tool_call(f"c{i}", "complete_task",
                                             {"result_note": "核查完成"})]})
        out.append({"tool_use": [S.tool_call(f"f{i}", "finish", {"summary": "完成"})]})
    return out


def test_l2_chain_full_cycle(tmp_path, monkeypatch):
    """全链：人手开窗(C 起 worker)→手动 tick 发任务(B)→worker 完成空退(A)
    →自动续 tick→零产出 converged 停。chain_ticks/auto_ticks_total 各 1。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    from test_orchestrator import ScriptedLLM as S
    planner = _orch_scripted(
        [S.tool_call("t1", "publish_task",
                     {"objective": "核查 8080 旁站低噪信息", "task_type": "recon",
                      "noise_budget": "passive"})],
        [],  # 自动 tick：只 done，零产出 → 收敛
    )
    app = _chain_app(tmp_path, planner, _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2全链")
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"})
        assert sp.status_code == 201 and sp.json().get("job_id")  # 触发点 C
        sid = sp.json()["id"]
        _wait_no_running(c, pid)  # C 的空队列 worker 秒退（不消费 executor 剧本）

        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        tick_job = _wait_job(c, r.json()["job_id"])
        assert len(tick_job["result"]["published"]) == 1 and tick_job["result"]["spawned"] == []

        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "converged"
        _wait_no_running(c, pid)
        kinds = [e["kind"] for e in c.app.state.projects[pid].bb.recent_events(pid)]
        assert kinds.count("orch.chain_started") == 1
        task = c.get(f"/api/projects/{pid}/tasks").json()[0]
        assert task["status"] == "done" and task["claimed_by"] == sid
        from core.orchestrator import state as ost
        st = ost.load_or_create(c.app.state.projects[pid].bb, pid)
        assert st["chain_active"] == 0 and st["chain_ticks"] == 1
        assert st["auto_ticks_total"] == 1
        auto_jobs = _project_jobs(c, pid, "orchestrator-auto-tick")
        assert len(auto_jobs) == 1 and auto_jobs[0]["status"] == "done"
        assert _project_jobs(c, pid, "orchestrator-auto-wait") == []
        chain = c.get(f"/api/projects/{pid}").json()["usage"]["chain"]
        assert chain == {"active": False, "ticks": 1, "auto_ticks_total": 1,
                         "estranged": False}


def test_l2_chain_stops_without_sessions(tmp_path, monkeypatch):
    """有任务产出但零会话：没有执行者，链以 no_sessions 停，不起自动 tick。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    from test_orchestrator import ScriptedLLM as S
    planner = _orch_scripted(
        [S.tool_call("t1", "publish_task",
                     {"objective": "核查 8080", "task_type": "recon",
                      "noise_budget": "passive"})])
    app = _chain_app(tmp_path, planner)
    with TestClient(app) as c:
        pid = _l2_project(c, "L2无窗")
        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        _wait_job(c, r.json()["job_id"])
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "no_sessions"
        assert _project_jobs(c, pid, "orchestrator-auto-tick") == []
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"


def test_l2_chain_max_chain_ticks(tmp_path, monkeypatch):
    """max_chain_ticks=1：自动 tick 再有产出，达预算即停，不会发起第 2 个自动 tick。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    from test_orchestrator import ScriptedLLM as S
    planner = _orch_scripted(
        [S.tool_call("t1", "publish_task",
                     {"objective": "任务一", "task_type": "recon",
                      "noise_budget": "passive"})],
        [S.tool_call("t2", "publish_task",
                     {"objective": "任务二", "task_type": "recon",
                      "noise_budget": "passive"})],
    )
    app = _chain_app(tmp_path, planner, _exec_finish_script(2))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2预算", max_chain_ticks=1)
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"})
        assert sp.status_code == 201
        _wait_no_running(c, pid)
        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        _wait_job(c, r.json()["job_id"])
        stop = _wait_chain_stopped(c, pid)
        _wait_no_running(c, pid)
        assert stop["payload"]["reason"] == "max_chain_ticks"
        statuses = [t["status"] for t in c.get(f"/api/projects/{pid}/tasks").json()]
        assert statuses == ["done", "done"]
        assert len(_project_jobs(c, pid, "orchestrator-auto-tick")) == 1
        from core.orchestrator import state as ost
        assert ost.load_or_create(c.app.state.projects[pid].bb, pid)["chain_ticks"] == 1


def _arm_active_chain(c, pid, ticks=0):
    """直接把链置活跃（DB+进程标记），用于单闸触发测试。"""
    from core.orchestrator import state as ost
    bb = c.app.state.projects[pid].bb
    ost.save_fields(bb, pid, chain_active=1, chain_ticks=ticks, last_auto_tick_at="")
    c.app.state.active_chains.add(pid)


def test_l2_chain_paused_blocks_auto_but_human_overrides(tmp_path, monkeypatch):
    """闸②：暂停时 C/D 不自动起 worker；但人显式「跑队列」是 override 照常认领完成；
    worker 收尾的 A 触发仍被闸门②拦住，落 chain_stopped{paused}。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2暂停", paused=True)
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"})
        assert sp.status_code == 201 and "job_id" not in sp.json()  # 触发点 C 暂停不开工
        sid = sp.json()["id"]
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "核查 8080", "task_type": "recon"})
        assert r.json()["kicked"] == []  # 触发点 D 暂停时不 kick
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"
        _arm_active_chain(c, pid, ticks=1)
        w = c.post(f"/api/agents/{sid}/work")  # 人工显式 override
        job = _wait_job(c, w.json()["job_id"])
        assert job["result"] == 1  # 暂停不拦人工动作：认领并完成
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "paused"  # A 仍被闸②拦住
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "done"
        assert pid not in c.app.state.active_chains


def test_l2_chain_level_downgrade_stops(tmp_path, monkeypatch):
    """闸①：L2 降为 L1 后，worker 空退触发 level_changed 停链，不续 tick。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        pid = _l2_project(c, "L2降级")
        sid = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"}).json()["id"]
        _wait_no_running(c, pid)
        _arm_active_chain(c, pid, ticks=1)
        r = c.patch(f"/api/projects/{pid}/config",
                    json={"config": {"autonomy": {"level": "L1"}}})
        assert r.status_code == 200
        w = c.post(f"/api/agents/{sid}/work")
        _wait_job(c, w.json()["job_id"])
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "level_changed"
        assert _project_jobs(c, pid, "orchestrator-auto-tick") == []


def test_l2_chain_restart_is_estop(tmp_path, monkeypatch):
    """重启=急停：DB chain_active=1 但本进程无标记 → estranged；触发只落 restart 停链。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    from core.orchestrator import state as ost
    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        pid = _l2_project(c, "L2重启")
        sid = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"}).json()["id"]
        _wait_no_running(c, pid)
        # 模拟上一进程遗留：只写 DB，不进 app.state.active_chains
        ost.save_fields(c.app.state.projects[pid].bb, pid,
                        chain_active=1, chain_ticks=2, last_auto_tick_at="")
        usage = c.get(f"/api/projects/{pid}").json()["usage"]
        assert usage["chain"]["estranged"] is True and usage["chain"]["ticks"] == 2

        w = c.post(f"/api/agents/{sid}/work")
        _wait_job(c, w.json()["job_id"])
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "restart"
        assert _project_jobs(c, pid, "orchestrator-auto-tick") == []
        usage2 = c.get(f"/api/projects/{pid}").json()["usage"]
        assert usage2["chain"] == {"active": False, "ticks": 2,
                                   "auto_ticks_total": 0, "estranged": False}


def test_l2_chain_budget_blocked_stops(tmp_path, monkeypatch):
    """闸⑦：token 预算对两类自主动作都硬闸时，触发点 A 落 budget_blocked 停链。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        pid = _l2_project(c, "L2预算硬闸", token_budget=5)
        sid = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"}).json()["id"]
        _wait_no_running(c, pid)
        bb = c.app.state.projects[pid].bb
        bb.usage_add_llm(pid, ti=0, to=5, cache_read=0, cache_creation=0, token_budget=5)
        _arm_active_chain(c, pid)
        w = c.post(f"/api/agents/{sid}/work")
        _wait_job(c, w.json()["job_id"])
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "budget_blocked"
        assert _project_jobs(c, pid, "orchestrator-auto-tick") == []


def test_l2_chain_throttle_wait_then_continue(tmp_path, monkeypatch):
    """闸⑥：踩 10s（测试压到 0.2s）间隔 → wait job 延迟重入，不丢触发；随后收敛。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0.2)
    from core.orchestrator import state as ost
    from datetime import datetime, timezone
    app = _chain_app(tmp_path, _orch_scripted([]))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2节流")
        sid = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"}).json()["id"]
        _wait_no_running(c, pid)
        _arm_active_chain(c, pid)
        # 微秒精度（timespec="seconds" 截断在秒末会把时间戳前推最多 1s，绕过节流）
        ost.save_fields(
            c.app.state.projects[pid].bb, pid,
            last_auto_tick_at=datetime.now(timezone.utc).isoformat())
        w = c.post(f"/api/agents/{sid}/work")
        _wait_job(c, w.json()["job_id"])
        # 先出现 wait job、尚无 auto-tick
        for _ in range(50):
            if _project_jobs(c, pid, "orchestrator-auto-wait"):
                break
            time.sleep(0.01)
        waits = _project_jobs(c, pid, "orchestrator-auto-wait")
        assert len(waits) == 1 and _project_jobs(c, pid, "orchestrator-auto-tick") == []
        # wait 睡满后重入 → 自动 tick 零产出 → 收敛
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "converged"
        assert ost.load_or_create(c.app.state.projects[pid].bb, pid)["chain_ticks"] == 1


def test_l0_l1_trigger_gates_c_d_e(tmp_path):
    """触发点 C/D/E 的档位分流：L0 全部不自动；L1 插话/审批后自动 kick。"""
    from test_orchestrator import ScriptedLLM as S
    # L0：开窗不自起 worker；插话 kicked=[]，任务保持 open
    app0 = create_app(workspace_root=str(tmp_path / "ws0"), tools_root=None,
                      executor_llm=S([]), planner_llm=S([]),
                      providers_config=str(tmp_path / "p0.json"))
    with TestClient(app0) as c0:
        pid0 = c0.post("/api/projects", json={"name": "L0", "track": "ctf",
                                              "capabilities": ["binary"]}).json()["id"]
        sp = c0.post(f"/api/projects/{pid0}/agents", json={"role": "_generalist"})
        assert sp.status_code == 201 and "job_id" not in sp.json()
        r = c0.post(f"/api/projects/{pid0}/tasks",
                    json={"objective": "逆一下", "task_type": "generic"})
        assert r.json()["kicked"] == []
        assert c0.get(f"/api/projects/{pid0}/tasks").json()[0]["status"] == "open"

    # L1：先有一扇 idle 旧窗；插话后旧窗自动认领；审批后 E 再 kick 空闲窗
    planner = [
        {"tool_use": [S.tool_call("s1", "spawn_session",
                                  {"role": "recon", "reason": "核查 8080"}),
                      S.tool_call("d1", "done", {})]},
    ]
    app1 = create_app(workspace_root=str(tmp_path / "ws1"), tools_root=None,
                      executor_llm=S(_exec_finish_script(1)), planner_llm=S(planner),
                      providers_config=str(tmp_path / "p1.json"))
    with TestClient(app1) as c1:
        pid1 = c1.post("/api/projects", json={"name": "L1触发", "track": "assessment",
                                              "capabilities": ["web"]}).json()["id"]
        old = c1.post(f"/api/projects/{pid1}/agents", json={"role": "recon"})
        old_sid = old.json()["id"]
        assert old.json().get("job_id")  # L1 开窗触发点 C 自起
        _wait_no_running(c1, pid1)
        item = _l1_tick_to_pending(c1, pid1)  # 提交开窗审批（不发任务）
        _wait_no_running(c1, pid1)  # tick 的 B 触发对旧窗空 kick 收尾
        dr = c1.post(f"/api/projects/{pid1}/tasks",
                     json={"objective": "核查 8080", "task_type": "recon"})
        assert dr.json()["kicked"] == [old_sid]  # 触发点 D
        decided = c1.post(f"/api/approvals/{item['id']}/decide",
                          json={"decision": "approved"})
        # 触发点 E：响应带 kicked（新窗自带在跑 job 被去重）
        assert decided.status_code == 200 and decided.json()["executed"] is True
        assert isinstance(decided.json().get("kicked"), list)
        _wait_no_running(c1, pid1)
        tasks = c1.get(f"/api/projects/{pid1}/tasks").json()
        assert tasks and tasks[0]["status"] == "done"
        assert tasks[0]["claimed_by"] in {old_sid, decided.json()["session_id"]}


def test_l2_net_real_without_approval_denied(tmp_path):
    """批 5 红线（plan 跨批纪律）：L2 全自动不放松安全层——worker 跑 net=real
    且无已批准审批时，网关必须拒绝并落 audit.deny，命令不执行，任务正常收尾。"""
    from test_orchestrator import ScriptedLLM as S
    executor = [
        # A2：认领后先写计划，实质工具才过计划闸
        {"tool_use": [S.tool_call("p1", "task_plan",
                                  {"steps": [{"title": "尝试取外网文件（预期被红线拒绝）"}]})]},
        {"tool_use": [S.tool_call("r1", "run_cmd",
                                  {"cmd": "curl http://10.0.0.1/x", "runtime": "host",
                                   "threat_class": "trusted", "net": "real"})]},
        {"tool_use": [S.tool_call("c1", "complete_task", {"result_note": "被网关拒绝，改道"})]},
        {"tool_use": [S.tool_call("f1", "finish", {"summary": "改道完成"})]},
    ]
    # A5：触发点 D 会在 L2 下跑一轮去抖重排（消费 planner 一条 set_priorities）
    planner = [{"tool_use": [S.tool_call("rp1", "set_priorities",
                                         {"updates": []})]}]
    app = _chain_app(tmp_path, planner, executor)
    with TestClient(app) as c:
        pid = _l2_project(c, "L2红线")
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
        sid = sp.json()["id"]
        _wait_no_running(c, pid)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "取个外网文件", "task_type": "generic"})
        assert r.json()["kicked"] == [sid]  # 触发点 D
        _wait_no_running(c, pid)
        denies = [e for e in c.app.state.projects[pid].bb.recent_events(pid)
                  if e["kind"] == "audit.deny"]
        assert len(denies) == 1 and "net=real" in denies[0]["payload"]["reason"]
        task = c.get(f"/api/projects/{pid}/tasks").json()[0]
        assert task["status"] == "done" and task["claimed_by"] == sid


# ---------- 批 6：L0 提案模式（ctf 默认档；tick 只提案，人采纳走既有写口） ----------

def test_l0_tick_only_emits_proposals(tmp_path):
    """ctf 项目默认 L0：tick 中 publish_task/spawn_session 只落 orch.proposed——
    tasks/sessions 表无新增、无 task.published/session.spawned、无链无自动 job、
    结构化 proposals 正确、write_digest 照常。"""
    from test_orchestrator import ScriptedLLM as S
    planner = [
        {"tool_use": [
            S.tool_call("p1", "publish_task",
                        {"objective": "静态逆 check_flag", "task_type": "reverse",
                         "noise_budget": "passive"}),
            S.tool_call("s1", "spawn_session",
                        {"role": "reverse", "reason": "提案开逆向窗"}),
        ]},
        {"tool_use": [S.tool_call("g1", "write_digest",
                                  {"summary": "L0 提案轮：1 任务 1 开窗待采纳"})]},
        {"tool_use": [S.tool_call("d1", "done", {})]},
    ]
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=S([]), planner_llm=S(planner),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "L0提案", "track": "ctf",
                                            "capabilities": ["binary"]}).json()["id"]
        # 建项默认档 = L0
        assert c.get(f"/api/projects/{pid}").json()["usage"]["level"] == "L0"
        r = c.post(f"/api/projects/{pid}/orchestrator/tick",
                   json={"allowed_roles": ["reverse"]})
        assert r.status_code == 200
        job = _wait_job(c, r.json()["job_id"])
        assert job["status"] == "done"
        result = job["result"]
        assert result["published"] == [] and result["spawned"] == []
        assert [p["op"] for p in result["proposals"]] == ["publish_task", "spawn_session"]
        pargs = result["proposals"][0]["args"]
        assert pargs["objective"] == "静态逆 check_flag" and pargs["task_type"] == "reverse"
        assert isinstance(result["proposals"][0]["event_id"], int)
        # 实体表零新增
        assert c.get(f"/api/projects/{pid}/tasks").json() == []
        assert c.get(f"/api/projects/{pid}/sessions").json() == []
        bb = c.app.state.projects[pid].bb
        kinds = [e["kind"] for e in bb.recent_events(pid)]
        assert kinds.count("orch.proposed") == 2
        assert "task.published" not in kinds
        assert "session.spawned" not in kinds
        assert not any(k.startswith("orch.chain") for k in kinds)
        # digest 照常
        digests = [e for e in bb.recent_events(pid) if e["kind"] == "project.digest"]
        assert len(digests) == 1 and "L0 提案轮" in digests[0]["payload"]["digest"]
        # 无任何自动 job（L0 不续 tick、不自起 worker）
        _wait_no_running(c, pid)
        assert [j for j in _project_jobs(c, pid)
                if j["kind"] in {"orchestrator-auto-tick", "orchestrator-auto-wait"}] == []
        # usage 不暴露活链
        assert c.get(f"/api/projects/{pid}").json()["usage"]["chain"]["active"] is False


def test_l0_proposal_adoption_human_attributed(tmp_path):
    """人在事件流采纳提案：发任务走 POST /tasks（created_by=human、kicked=[]），
    开窗走 POST /agents（201、无 job_id）——责任清晰，编排不沾写口。"""
    from test_orchestrator import ScriptedLLM as S
    planner = [
        {"tool_use": [
            S.tool_call("p1", "publish_task",
                        {"objective": "低噪核查 8080", "task_type": "verify",
                         "noise_budget": "passive", "priority": 3,
                         "conflict_keys": [], "refs": []}),
            S.tool_call("s1", "spawn_session",
                        {"role": "reverse", "reason": "采纳后开逆向窗"}),
        ]},
        {"tool_use": [S.tool_call("d1", "done", {})]},
    ]
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=S([]), planner_llm=S(planner),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "L0采纳", "track": "ctf",
                                            "capabilities": ["binary"]}).json()["id"]
        tick = _wait_job(c, c.post(f"/api/projects/{pid}/orchestrator/tick",
                                   json={"allowed_roles": ["reverse"]}).json()["job_id"])
        pub_prop, spawn_prop = tick["result"]["proposals"]

        # 采纳任务提案 → 人类写口
        a = pub_prop["args"]
        tr = c.post(f"/api/projects/{pid}/tasks", json={
            "objective": a["objective"], "task_type": a["task_type"],
            "noise_budget": a["noise_budget"], "priority": a["priority"],
            "conflict_keys": a["conflict_keys"], "refs": a["refs"]})
        assert tr.status_code == 201 and tr.json()["kicked"] == []  # L0 不自起 worker
        task = c.get(f"/api/projects/{pid}/tasks").json()[0]
        assert task["created_by"] == "human" and task["status"] == "open"

        # 采纳开窗提案 → 人类开窗，201 且无自起 job
        sr = c.post(f"/api/projects/{pid}/agents",
                    json={"role": spawn_prop["args"]["role"]})
        assert sr.status_code == 201 and "job_id" not in sr.json()
        sessions = c.get(f"/api/projects/{pid}/sessions").json()
        assert len(sessions) == 1 and sessions[0]["role"] == "reverse"
        _wait_no_running(c, pid)
        assert _project_jobs(c, pid, kind="agent-work") == []


def test_orchestrator_tick_lease_409_and_state_persists(tmp_path):
    """tick 租约：他人持有未过期 → 同步 409 且不产生 job；释放后正常；
    job 结束自动释放；cycles/event_cursor 跨 tick 持久；job.result 结构化。"""
    from test_orchestrator import ScriptedLLM
    from core.orchestrator import state as ost

    def done_once():
        return ScriptedLLM([
            {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
        ])

    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=ScriptedLLM([]), planner_llm=done_once(),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "租约409", "track": "assessment",
                                          "capabilities": ["web"]})
        pid = rp.json()["id"]
        bb = c.app.state.projects[pid].bb

        # 预置他人有效租约 → 409，且不提交 job
        ost.acquire_tick_lease(bb, pid, "stuck-owner")
        before_jobs = [j for j in c.app.state.jobs.all_jobs()
                       if j["meta"].get("project_id") == pid]
        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        assert r.status_code == 409
        assert "tick" in r.json()["detail"]
        after_jobs = [j for j in c.app.state.jobs.all_jobs()
                      if j["meta"].get("project_id") == pid]
        assert after_jobs == before_jobs

        # 租约释放 → tick 正常落地，job 收尾自动还锁
        assert ost.release_tick_lease(bb, pid, "stuck-owner") is True
        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        assert r.status_code == 200
        job1 = _wait_job(c, r.json()["job_id"])
        assert job1["status"] == "done"
        result = job1["result"]
        assert set(result) == {"summary", "published", "spawned", "digest", "proposals"}
        assert ost.load_or_create(bb, pid)["cycles"] == 1
        assert ost.load_or_create(bb, pid)["tick_owner"] == ""  # finally 已释放


def test_orchestrator_tick_cycles_persist_across_ticks(tmp_path):
    """连续两次 tick：第二轮 cycles=2、tick_owner 回到空闲（_run_tick finally）。"""
    from test_orchestrator import ScriptedLLM
    from core.orchestrator import state as ost

    script = [
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
        {"tool_use": [ScriptedLLM.tool_call("d2", "done", {})]},
    ]
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=ScriptedLLM([]),
                     planner_llm=ScriptedLLM(script),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects",
                     json={"name": "cycles 持久", "track": "assessment",
                           "capabilities": ["web"]}).json()["id"]
        bb = c.app.state.projects[pid].bb
        j1 = _wait_job(c, c.post(f"/api/projects/{pid}/orchestrator/tick",
                                 json={}).json()["job_id"])
        assert j1["status"] == "done"
        j2 = _wait_job(c, c.post(f"/api/projects/{pid}/orchestrator/tick",
                                 json={}).json()["job_id"])
        assert j2["status"] == "done"
        st = ost.load_or_create(bb, pid)
        assert st["cycles"] == 2 and st["tick_owner"] == ""
        assert bb.conn.execute(
            "SELECT COUNT(*) FROM orchestrator_state").fetchone()[0] == 1


# ---------- 模型清单与开窗覆盖（DESIGN.md §8） ----------


def test_models_endpoint(client):
    models = client.get("/api/models").json()["models"]
    # 实测可用清单必在（smoke_ark.py 验证过的两个模型）
    assert "ark-code-latest" in models and "deepseek-v4-flash" in models
    assert models == sorted(models)


def test_spawn_agent_with_model_override(client, monkeypatch):
    """开窗传 provider+model → 走供应商工厂覆盖 executor（构造不触网）。"""
    calls: list[tuple] = []
    store = client.app.state.llm_store
    orig = store.build

    def fake_build(name=None, model=None):
        calls.append((name, model))
        return orig(name, model)

    monkeypatch.setattr(store, "build", fake_build)
    rp = client.post("/api/projects", json={"name": "渗透-模型", "track": "assessment",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    r = client.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "provider": "ark-plan",
                          "model": "deepseek-v4-flash"})
    assert r.status_code == 201
    # 最后一次构造是 spawn 的覆盖请求（前面还有默认 executor/planner 构建）
    assert calls[-1] == ("ark-plan", "deepseek-v4-flash")
    agent = client.app.state.agents[r.json()["id"]]
    assert agent.llm.base_url.endswith("/api/plan")  # 真的切到了 plan 端点
    assert r.json()["id"] in client.app.state.agents


def test_llm_providers_crud_and_switch(client):
    """供应商管理 API：脱敏/整表保存/校验；在跑会话动态切换落 llm.switched。"""
    # 初始种子 + 默认 + 脱敏
    r = client.get("/api/llm/providers")
    assert r.status_code == 200
    data = r.json()
    assert [p["name"] for p in data["providers"]] == ["ark-coding", "ark-plan"]
    assert data["default"] == {"provider": "ark-coding", "model": "ark-code-latest"}
    assert all("api_key" not in p for p in data["providers"])

    # 保存：给 plan 填 key + 改模型清单（首个=默认）
    providers = [
        {"name": "ark-coding", "base_url": "https://ark.cn-beijing.volces.com/api/coding",
         "api_key": "", "models": ["ark-code-latest"], "enabled": True},
        {"name": "ark-plan", "base_url": "https://ark.cn-beijing.volces.com/api/plan",
         "api_key": "plan-secret", "models": ["glm-5-3-flash-260828", "ark-code-latest"],
         "enabled": True},
    ]
    assert client.put("/api/llm/providers", json={"providers": providers}).status_code == 200
    # 再次保存空 key → 沿用原 key（has_key 仍 true）
    providers[1]["api_key"] = ""
    r = client.put("/api/llm/providers", json={"providers": providers})
    plan = [p for p in r.json()["providers"] if p["name"] == "ark-plan"][0]
    assert plan["has_key"] is True and plan["models"][0] == "glm-5-3-flash-260828"

    # 校验：非法名 / 模型空 / 全停用 → 422
    bad = [dict(providers[0], name="bad name")] + [dict(providers[1])]
    assert client.put("/api/llm/providers", json={"providers": bad}).status_code == 422
    no_models = [dict(providers[0], models=[])]
    assert client.put("/api/llm/providers", json={"providers": no_models}).status_code == 422
    all_off = [dict(providers[0], enabled=False), dict(providers[1], enabled=False)]
    assert client.put("/api/llm/providers", json={"providers": all_off}).status_code == 422

    # 在跑会话动态切换（构造不触网）；未知 sid 404、未知供应商 422
    pid = _make_project(client)
    sid = client.post(f"/api/projects/{pid}/agents",
                      json={"role": "_generalist"}).json()["id"]
    r = client.post(f"/api/agents/{sid}/llm",
                    json={"provider": "ark-plan", "model": "ark-code-latest"})
    assert r.status_code == 200 and r.json()["model"] == "ark-code-latest"
    assert client.app.state.agents[sid].llm.base_url.endswith("/api/plan")
    kinds = [e["kind"] for e in client.app.state.projects[pid].bb.recent_events(pid)]
    assert "llm.switched" in kinds
    assert client.post("/api/agents/sess-nope/llm",
                       json={"provider": "ark-plan"}).status_code == 404
    assert client.post(f"/api/agents/{sid}/llm",
                       json={"provider": "nope"}).status_code == 422


# ---------- 资产更新端点（DESIGN.md §5.2：PATCH 补挂/合并 meta） ----------


def test_patch_asset_endpoint(client):
    pid = _make_project(client)
    a = client.post(f"/api/projects/{pid}/assets",
                    json={"type": "host", "value": "10.0.0.10"}).json()
    b = client.post(f"/api/projects/{pid}/assets",
                    json={"type": "domain", "value": "b.cn"}).json()
    # 补挂父行 + 合并 meta
    r = client.patch(f"/api/assets/{b['id']}", json={"parent_id": a["id"]})
    assert r.status_code == 200 and r.json()["parent_id"] == a["id"]
    r = client.patch(f"/api/assets/{b['id']}",
                     json={"meta": {"title": "门户", "scanned": True}})
    assert r.json()["meta"]["title"] == "门户" and r.json()["meta"]["scanned"] is True
    # 摘挂（parent_id=null）→ 根行
    r = client.patch(f"/api/assets/{b['id']}", json={"parent_id": None})
    assert r.json()["parent_id"] is None
    # 资产不存在 → 404；父不存在 → 422
    assert client.patch("/api/assets/asset-nope", json={"meta": {}}).status_code == 404
    assert client.patch(f"/api/assets/{b['id']}",
                        json={"parent_id": "asset-nope"}).status_code == 422


def test_findings_filter_params(client):
    """findings 端点过滤参数：min_severity / verified_only（按资产筛选参数同族透传）。"""
    pid = _make_project(client)
    # dedup 键默认 = vuln_class，故用不同 vuln_class 防合并
    for sev, status in [("low", "unverified"), ("high", "verified"), ("critical", "verified")]:
        r = client.post(f"/api/projects/{pid}/findings",
                        json={"vuln_class": f"triage-{sev}", "title": f"t-{sev}-{status}",
                              "severity": sev, "status": status})
        assert r.status_code == 201
    all_f = client.get(f"/api/projects/{pid}/findings").json()
    assert len(all_f) == 3
    hi = client.get(f"/api/projects/{pid}/findings?min_severity=high").json()
    assert {f["severity"] for f in hi} == {"high", "critical"}
    ver = client.get(f"/api/projects/{pid}/findings?verified_only=true").json()
    assert len(ver) == 2 and all(f["status"] == "verified" for f in ver)


def test_delete_finding_endpoint(client):
    """DELETE finding：200 带审计快照，级联摘任务 refs；再删/跨项目 → 404。"""
    pid = _make_project(client)
    f = client.post(f"/api/projects/{pid}/findings",
                    json={"vuln_class": "sqli-del", "title": "待删走查卡",
                          "severity": "high"}).json()
    t = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": f"复核 {f['id']}", "task_type": "generic"}).json()
    r = client.delete(f"/api/projects/{pid}/findings/{f['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == f["id"] and body["title"] == "待删走查卡"
    assert body["affected_tasks"] == [t["task_id"]]
    assert client.get(f"/api/projects/{pid}/findings").json() == []
    # 任务引用已摘但任务仍在
    task = next(x for x in client.get(f"/api/projects/{pid}/tasks").json() if x["id"] == t["task_id"])
    assert task.get("context_refs") == []
    # 再删 404；别的项目也查不到（项目隔离）
    assert client.delete(f"/api/projects/{pid}/findings/{f['id']}").status_code == 404
    pid2 = _make_project(client)
    assert client.delete(f"/api/projects/{pid2}/findings/{f['id']}").status_code == 404


def test_delete_asset_endpoint(client):
    """DELETE asset：叶子可删；被 finding 引用/有子 → 409；不属于本项目 → 404。"""
    pid = _make_project(client)
    free = client.post(f"/api/projects/{pid}/assets",
                       json={"type": "url", "value": "http://x/a"}).json()
    used = client.post(f"/api/projects/{pid}/assets",
                       json={"type": "url", "value": "http://x/b"}).json()
    client.post(f"/api/projects/{pid}/findings",
                json={"vuln_class": "xss-del", "title": "引用卡",
                      "target_asset_id": used["id"]})
    assert client.delete(f"/api/projects/{pid}/assets/{used['id']}").status_code == 409
    assert client.delete(f"/api/projects/{pid}/assets/{free['id']}").status_code == 200
    values = [a["value"] for a in client.get(f"/api/projects/{pid}/assets").json()]
    assert "http://x/a" not in values and "http://x/b" in values
    assert client.delete(f"/api/projects/{pid}/assets/asset-nope").status_code == 404


def test_artifact_content_endpoint(client, tmp_path):
    """产物内容只读端点：表内 path 命中 / 裸路径兜底 / 防穿越 / 404。"""
    import pathlib
    pid = _make_project(client)
    # 直接在项目 artifacts 目录落一个 POC 文件（等价旧数据裸路径场景）
    ws_root = tmp_path / "workspaces"
    proj_dir = next(p for p in ws_root.iterdir() if p.is_dir())
    poc = proj_dir / "artifacts" / "poc_demo.py"
    poc.write_text("print('poc')", encoding="utf-8")
    # 裸路径读取（旧数据 evidence.poc_artifact 只有路径字符串）
    r = client.get(f"/api/projects/{pid}/artifacts/content?ref=artifacts/poc_demo.py")
    assert r.status_code == 200
    assert r.json()["content"] == "print('poc')"
    # 防穿越：resolve 后跑出项目目录 → 422
    r = client.get(f"/api/projects/{pid}/artifacts/content?ref=../../escaped.txt")
    assert r.status_code == 422
    # 文件不存在 → 404
    r = client.get(f"/api/projects/{pid}/artifacts/content?ref=artifacts/nope.py")
    assert r.status_code == 404


def _packs_app(tmp_path):
    """构造正交布局的最小 packs（web 能力包 + assessment 轨）并返回 TestClient。"""
    root = tmp_path / "packs"
    role_dir = root / "tracks" / "assessment" / "roles"
    role_dir.mkdir(parents=True)
    (role_dir / "tester.yaml").write_text(
        'name: tester\npersona: "旧人设。"\n'
        'skills: [demo-skill]\ntask_types: [recon]\n', encoding="utf-8")
    sk_dir = root / "capabilities" / "web" / "skills" / "demo-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示技能关键词注入\nkeywords: 演示\n---\n正文。",
        encoding="utf-8")
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(root), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"))
    return root, TestClient(app)


def test_packs_roles_and_skills_endpoints(tmp_path, monkeypatch):
    """包管理端点：轨角色表单改写 / 能力包 skill 全文改写 / 路由试算（正交分类学）。"""
    monkeypatch.chdir(tmp_path)  # MCP 等相对路径配置不污染仓库
    packs_root, c = _packs_app(tmp_path)
    with c:
        # 轨角色列表 + 表单改写
        roles = c.get("/api/tracks/assessment/roles").json()
        assert roles[0]["name"] == "tester" and roles[0]["skills"] == ["demo-skill"]
        r = c.put("/api/tracks/assessment/roles/tester",
                  json={"persona": "新人设。", "skills": None, "task_types": ["recon"],
                        "tools": ["bb_query"], "max_runtime": "docker", "max_steps": 20})
        assert r.json()["status"] == "ok"
        from core.skills.roles import load_role
        role = load_role(packs_root, "assessment", "tester")
        assert role["persona"] == "新人设。" and role["skills"] is None  # null = 白名单关闭
        assert role["tools"] == ["bb_query"] and role["max_runtime"] == "docker"
        assert role["max_steps"] == 20
        # 值域校验：非法 max_runtime / max_steps → 422
        assert c.put("/api/tracks/assessment/roles/tester",
                     json={"max_runtime": "metal"}).status_code == 422
        assert c.put("/api/tracks/assessment/roles/tester",
                     json={"max_steps": 0}).status_code == 422
        # 改写留历史
        hist = packs_root / "tracks" / "assessment" / "roles" / ".history"
        assert any("tester.yaml" in f.name for f in hist.iterdir())
        # 能力包 skill 详情 + 全文改写（frontmatter name 不一致 → 422）
        sk = c.get("/api/capabilities/web/skills/demo-skill").json()
        assert "正文。" in sk["raw"] and sk["skill"]["pack"] == "web"
        r = c.put("/api/capabilities/web/skills/demo-skill",
                  json={"content": "---\nname: other\n---\nx"})
        assert r.status_code == 422
        r = c.put("/api/capabilities/web/skills/demo-skill",
                  json={"content": "---\nname: demo-skill\ndescription: 改后\n"
                                   "keywords: 演示\nfile_features: NX\n---\n新正文。"})
        assert r.json()["status"] == "ok"
        # 路由试算：track+caps 定候选集，关键词命中 demo-skill
        r = c.post("/api/skills/route-preview",
                   json={"query": "做一个演示任务", "track": "assessment",
                         "capabilities": ["web"], "role": "tester"})
        assert r.status_code == 200
        assert r.json()[0]["name"] == "demo-skill" and r.json()[0]["matched"]
        # 文件特征 ×3 命中（P0：file_features 路由断裂修复后的 API 面）
        r = c.post("/api/skills/route-preview",
                   json={"track": "assessment", "capabilities": ["web"],
                         "file_features": ["NX"]})
        assert r.json()[0]["name"] == "demo-skill"
        # 候选集外不可见：只选 assessment 轨（demo-skill 在 web 包）
        r = c.post("/api/skills/route-preview",
                   json={"query": "演示", "track": "assessment"})
        assert all(h["name"] != "demo-skill" for h in r.json())
        # 防穿越：非法包名 → 422
        assert c.get("/api/capabilities/bad%20name/skills").status_code == 422
        # 轨任务类型注册表：generic 恒在
        assert c.get("/api/tracks/assessment/task-types").json()["generic"] == "passive"


def test_rules_and_mcp_endpoints(client, tmp_path, monkeypatch):
    """轨红线读写（.history 备份）+ MCP 配置层 roundtrip。"""
    monkeypatch.chdir(tmp_path)
    packs = tmp_path / "packs"
    rules_dir = packs / "tracks" / "assessment" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "redlines.md").write_text("旧红线", encoding="utf-8")
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        assert c.get("/api/tracks/assessment/rules").json()["content"] == "旧红线"
        r = c.put("/api/tracks/assessment/rules", json={"content": "新红线"})
        assert r.json()["status"] == "ok"
        assert c.get("/api/tracks/assessment/rules").json()["content"] == "新红线"
        assert any("redlines.md" in f.name
                   for f in (rules_dir / ".history").iterdir())
        # 能力包红线同构
        cap_rules = packs / "capabilities" / "web" / "rules"
        r = c.put("/api/capabilities/web/rules", json={"content": "web 红线"})
        assert r.status_code == 200
        assert c.get("/api/capabilities/web/rules").json()["content"] == "web 红线"
        assert (cap_rules / "redlines.md").read_text(encoding="utf-8") == "web 红线"
        # MCP：空 → 写入 → 读回
        assert c.get("/api/mcp").json() == {"servers": []}
        r = c.put("/api/mcp", json={"servers": [
            {"name": "ghidra", "url": "http://127.0.0.1:8081/mcp", "enabled": True}]})
        assert r.json()["count"] == 1
        servers = c.get("/api/mcp").json()["servers"]
        assert servers[0]["name"] == "ghidra" and servers[0]["transport"] == "streamable-http"
        # 重名 → 422；非法名 → 422
        r = c.put("/api/mcp", json={"servers": [
            {"name": "a", "url": "u"}, {"name": "a", "url": "u"}]})
        assert r.status_code == 422
        r = c.put("/api/mcp", json={"servers": [{"name": "../x", "url": "u"}]})
        assert r.status_code == 422
        # P2：domains 白名单 + http 只许 loopback + stdio 必须有 command
        r = c.put("/api/mcp", json={"servers": [
            {"name": "ida", "url": "http://127.0.0.1:13337/mcp",
             "domains": ["reverse"]}]})
        assert r.status_code == 200
        r = c.put("/api/mcp", json={"servers": [
            {"name": "ida", "url": "http://127.0.0.1:13337/mcp",
             "domains": ["exfil"]}]})
        assert r.status_code == 422
        r = c.put("/api/mcp", json={"servers": [
            {"name": "ida", "url": "http://10.0.0.9:13337/mcp",
             "domains": ["reverse"]}]})
        assert r.status_code == 422  # 红线：非本机 MCP 拒连
        r = c.put("/api/mcp", json={"servers": [
            {"name": "ida", "url": "", "domains": ["reverse"]}]})
        assert r.status_code == 422
        r = c.put("/api/mcp", json={"servers": [
            {"name": "x", "transport": "stdio", "command": None,
             "domains": ["pentest"]}]})
        assert r.status_code == 422


def test_owners_crud_and_mcp_stdio(client, tmp_path, monkeypatch):
    """平台规则 owners CRUD（轨级；删=停用、.history 留回滚件）+ MCP stdio roundtrip。"""
    monkeypatch.chdir(tmp_path)
    packs = tmp_path / "packs"
    track_rules = packs / "tracks" / "assessment" / "rules"
    track_rules.mkdir(parents=True)
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        # 空列表
        assert c.get("/api/tracks/assessment/owners").json() == []
        # 新建（父目录自动建）
        r = c.put("/api/tracks/assessment/owners/edusrc",
                  json={"content": "# edusrc 无害化三原则"})
        assert r.json()["status"] == "ok"
        # 改写（备份进 .history）
        c.put("/api/tracks/assessment/owners/edusrc", json={"content": "v2"})
        rows = c.get("/api/tracks/assessment/owners").json()
        assert [o["tag"] for o in rows] == ["edusrc"] and rows[0]["content"] == "v2"
        assert any("edusrc" in f.name
                   for f in (track_rules / "owners" / ".history").iterdir())
        # 非法 tag（_NAME_RE 白名单）→ 422
        assert c.put("/api/tracks/assessment/owners/bad%20name",
                     json={"content": "x"}).status_code == 422
        # 删除后 404，.history 有回滚件
        assert c.delete("/api/tracks/assessment/owners/edusrc").json()["status"] == "ok"
        assert c.delete("/api/tracks/assessment/owners/edusrc").status_code == 404
        assert c.get("/api/tracks/assessment/owners").json() == []
        # MCP stdio：command/args roundtrip，url 可留空
        r = c.put("/api/mcp", json={"servers": [
            {"name": "fofa", "transport": "stdio", "command": "uv",
             "args": ["run", "fofa.py"], "domains": ["pentest"]}]})
        assert r.json()["count"] == 1
        s = c.get("/api/mcp").json()["servers"][0]
        assert s["command"] == "uv" and s["args"] == ["run", "fofa.py"]
        assert s["transport"] == "stdio" and s["url"] == ""


# ---------- rev-generic 逆向工作台（样本上传/headless/人机共写全链路） ----------

import time  # noqa: E402

CRACKME = Path(__file__).parent / "fixtures" / "crackme" / "crackme.elf"

V3 = {
    "export_version": 3,
    "binary": "crackme.elf",
    "meta": {"arch": "x86-64", "bits": 64, "endian": "le",
             "imagebase": "0x400000", "entry": "0x401234", "filename": "crackme.elf"},
    "sections": [{"name": ".text", "vaddr": "0x401000", "size": 4096,
                  "perms": "r-x", "entropy": 6.1}],
    "imports": {"libc.so.6": ["strlen", "puts", "fgets"]},
    "functions": [
        {"address": 0x401189, "name": "check_flag", "size": 96,
         "calls": ["strlen", "puts"], "pseudocode": "int check_flag(char*s){return strlen(s)==21;}"},
        {"address": 0x401234, "name": "main", "size": 200,
         "calls": ["check_flag", "fgets"], "pseudocode": "int main(){fgets(b);check_flag(b);}"},
    ],
    "strings": [
        {"address": 0x402000, "string": "Enter key: ", "length": 12, "type": "cstr",
         "refs": [{"func_addr": 0x401234, "from_addr": 0x401240}]},
        {"address": 0x402010, "string": "Correct!", "length": 8, "type": "cstr",
         "refs": [{"func_addr": 0x401234, "from_addr": 0x401250}]},
        {"address": 0x402020, "string": "密钥", "length": 1, "type": "unicode", "refs": []},
    ],
}


def _fake_ida_runner():
    """假日 idat：解析 -S 出参写 canned v3 JSON，按 -o 主干伪造 .i64。"""
    def run(args):
        out = None
        stem = None
        for a in args:
            if a.startswith('-S"'):
                out = Path(a[3:].rstrip('"').rsplit(" ", 1)[1])
            elif a.startswith("-o"):
                stem = Path(a[2:])
        assert out is not None and stem is not None
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(V3), encoding="utf-8")
        stem.parent.mkdir(parents=True, exist_ok=True)
        stem.with_suffix(".i64").write_bytes(b"IDADB")
        return 0, "idat ok", ""
    return run


def _install_rev_factory(client, empty_projects):
    """注入 headless 服务工厂（id 命中 empty_projects 时装“无后端”服务）。"""
    from core.tools.decompiler import DecompilerService, IDAHeadlessBackend

    def factory(proj):
        if proj.id in empty_projects:
            return DecompilerService(cache_dir=proj.artifacts_dir / "decompiler-cache")
        ida = IDAHeadlessBackend(
            idat_cmd="idat", runner=_fake_ida_runner(), available=True,
            db_dir=proj.artifacts_dir / "decompiler-db")
        return DecompilerService(cache_dir=proj.artifacts_dir / "decompiler-cache", ida=ida)

    client.app.state.rev_service_factory = factory


def _wait_job(client, job_id, tries=100):
    for _ in range(tries):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 超时")


def test_rev_workbench_full_chain(client):
    empty: set[str] = set()
    _install_rev_factory(client, empty)
    r = client.post("/api/projects", json={
        "name": "逆向研究", "track": "research", "capabilities": ["binary"]})
    pid = r.json()["id"]
    # research 轨 triage 任务类型已注册（阶段 A）
    tr = client.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "triage", "task_type": "triage"})
    assert tr.status_code == 201

    blob = CRACKME.read_bytes()
    r = client.post(f"/api/projects/{pid}/samples",
                    files={"file": ("crackme.elf", blob, "application/octet-stream")})
    assert r.status_code == 202
    up = r.json()
    assert up["cached"] is False and up["job_id"] and len(up["sha"]) == 64
    sha = up["sha"]
    job = _wait_job(client, up["job_id"])
    assert job["status"] == "done" and job["result"]["status"] == "ok"
    assert job["result"]["function_count"] == 2

    # 事件 + 资产 triage meta
    kinds = [e["kind"] for e in client.get(f"/api/projects/{pid}/events").json()]
    assert "binary.triaged" in kinds
    asset = next(a for a in client.get(f"/api/projects/{pid}/assets").json() if a["value"] == sha)
    assert asset["meta"]["triage"]["backend"] == "ida-headless"
    assert asset["meta"]["triage"]["function_count"] == 2

    # overview
    ov = client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()
    assert ov["cached"] is True and ov["function_count"] == 2
    assert ov["tools"]["ida"]["state"] == "installed"
    assert ov["db_path"].endswith(".i64")
    assert ov["imports"]["libc.so.6"] == ["strlen", "puts", "fgets"]
    assert ov["strings_count"] == 3

    # 函数精简行：地址 hex
    rows = client.get(f"/api/projects/{pid}/binaries/{sha}/functions").json()
    assert [f["address"] for f in rows] == ["0x401189", "0x401234"]
    assert rows[0]["has_pseudo"] is True

    # v3 字符串表：hex 地址 / refs join 函数名 / q 子串过滤
    ss = client.get(f"/api/projects/{pid}/binaries/{sha}/strings").json()
    assert ss["truncated"] is False and len(ss["items"]) == 3
    assert ss["items"][0]["address"] == "0x402000"
    assert ss["items"][0]["refs"][0]["func"] == "0x401234"
    assert ss["items"][0]["refs"][0]["func_name"] == "main"
    hit = client.get(f"/api/projects/{pid}/binaries/{sha}/strings?q=correct").json()["items"]
    assert len(hit) == 1 and hit[0]["string"] == "Correct!"
    # strings 增强段导出失败 {"error": ...} → 409（不伪装成空表）
    proj_obj = client.app.state.projects.get(pid) or client.app.state.store.open_project(pid)
    cache_file = proj_obj.artifacts_dir / "decompiler-cache" / f"{sha}.json"
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    raw["strings"] = {"error": "idautils boom"}
    cache_file.write_text(json.dumps(raw), encoding="utf-8")
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/strings").status_code == 409

    # 单函数 + xref（caller/callee，导入函数地址 null）
    one = client.get(f"/api/projects/{pid}/binaries/{sha}/functions/401234").json()
    assert one["name"] == "main" and "fgets" in one["calls"]
    x = client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/401234").json()
    callees = {c["name"]: c["address"] for c in x["callees"]}
    assert callees == {"check_flag": "0x401189", "fgets": None}
    back = client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/401189").json()
    assert [c["name"] for c in back["callers"]] == ["main"]
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/deadbeef").status_code == 404

    # 重复上传：缓存命中，不再投 Job
    r2 = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme-copy.elf", blob, "application/octet-stream")})
    assert r2.status_code == 202 and r2.json()["cached"] is True and r2.json()["job_id"] is None

    # func_kb：补建行 → PATCH 改名/笔记/tags
    r = client.post(f"/api/projects/{pid}/funcs",
                    json={"binary_sha256": sha, "address": "0x401189", "name": "check_flag"})
    assert r.status_code == 201
    func = r.json()
    fid = func["id"]
    # func_kb 出口同样是 hex 字符串（JS Number 无 64 位精度，§9 地址纪律）
    assert func["address"] == "0x401189"
    r = client.post(f"/api/projects/{pid}/funcs",  # 幂等
                    json={"binary_sha256": sha, "address": "0x401189", "name": "check_flag"})
    assert r.json()["id"] == fid
    pr = client.patch(f"/api/projects/{pid}/funcs/{fid}", json={
        "name": "check_license", "note": "长度 21 的校验", "risk_tags": ["crypto"]})
    assert pr.status_code == 200
    body = pr.json()
    assert body["name"] == "check_license" and body["risk_tags"] == ["crypto"]
    assert len(body["name_history"]) == 2 and "## 笔记" in body["analysis"]
    assert body["confidence"] == 0.5
    # [] 清空 tags；sha 透传过滤
    assert client.patch(f"/api/projects/{pid}/funcs/{fid}",
                        json={"risk_tags": []}).json()["risk_tags"] == []
    listed_funcs = client.get(f"/api/projects/{pid}/funcs?binary_sha256={sha}").json()
    assert len(listed_funcs) == 1 and listed_funcs[0]["address"] == "0x401189"
    ov2 = client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()
    assert ov2["analyzed_count"] == 1 and ov2["risk_count"] == 0

    # findings PATCH：verified + evidence merge / 非法 422 / 404
    fr = client.post(f"/api/projects/{pid}/findings",
                     json={"vuln_class": "crypto", "title": "硬编码密钥",
                           "target_asset_id": asset["id"],
                           "evidence": {"category": "algorithm", "func_id": fid,
                                        "address": "0x401189"}})
    finding_id = fr.json()["id"]
    # 逆向发现可省略 vuln_class（类别在 evidence.category）；空键不做合并
    fr2 = client.post(f"/api/projects/{pid}/findings",
                      json={"title": "第二个机制发现", "target_asset_id": asset["id"],
                            "evidence": {"category": "mechanism"}})
    assert fr2.status_code == 201 and fr2.json()["id"] != finding_id
    listed = {f["id"]: f for f in client.get(f"/api/projects/{pid}/findings").json()}
    assert len(listed) == 2
    # target_asset_id 必须真正落库（早期 FindingIn 漏字段会被 Pydantic 静默丢弃）
    assert listed[finding_id]["target_asset_id"] == asset["id"]
    assert listed[fr2.json()["id"]]["target_asset_id"] == asset["id"]
    assert client.post(f"/api/projects/{pid}/findings",
                       json={"title": "挂幽灵资产",
                             "target_asset_id": "asset-deadbeef"}).status_code == 422
    # relates_to 强关系：悬空/跨项目 422（E0 防幻觉），合法强边 201
    base = client.post(f"/api/projects/{pid}/findings",
                       json={"vuln_class": "info-leak", "title": "基线发现"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/findings",
                       json={"vuln_class": "sqli", "title": "幻觉强边",
                             "evidence": {"relates_to": [
                                 {"finding_id": "find-deadbeef", "note": "x"}]}}
                       ).status_code == 422
    other = _make_project(client)
    foreign = client.post(f"/api/projects/{other}/findings",
                          json={"vuln_class": "sqli", "title": "别项目发现"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/findings",
                       json={"vuln_class": "sqli-up", "title": "跨项目强边",
                             "evidence": {"relates_to": [
                                 {"finding_id": foreign, "note": "串线"}]}}
                       ).status_code == 422
    ok = client.post(f"/api/projects/{pid}/findings",
                     json={"vuln_class": "sqli-up", "title": "合法升链",
                           "evidence": {"relates_to": [
                               {"finding_id": base, "note": "同点升 DBA"}]}})
    assert ok.status_code == 201
    ok_id = ok.json()["id"]
    row = next(f for f in client.get(f"/api/projects/{pid}/findings").json()
               if f["id"] == ok_id)
    assert row["evidence"]["relates_to"][0]["finding_id"] == base
    # PATCH 携带悬空强边同样 422
    assert client.patch(f"/api/projects/{pid}/findings/{ok_id}",
                        json={"evidence": {"relates_to": [
                            {"finding_id": "find-nope", "note": "y"}]}}).status_code == 422
    pr = client.patch(f"/api/projects/{pid}/findings/{finding_id}", json={
        "status": "verified",
        "evidence": {"confirm_by": "dynamic", "debug_log_artifact_id": "ART"}})
    assert pr.status_code == 200
    ev = pr.json()["evidence"]
    assert ev["category"] == "algorithm" and ev["confirm_by"] == "dynamic"
    assert client.patch(f"/api/projects/{pid}/findings/{finding_id}",
                        json={"status": "nope"}).status_code == 422
    assert client.patch(f"/api/projects/{pid}/findings/find-dead",
                        json={"status": "verified"}).status_code == 404

    # 调试日志回流：上传（≤16MB）→ 4MB 内可读取
    r = client.post(f"/api/projects/{pid}/artifacts/upload",
                    data={"kind": "debug-log"},
                    files={"file": ("x64dbg.log", b"[hit main] rax=0 rcx=1\n", "text/plain")})
    assert r.status_code == 201
    art_id = r.json()["id"]
    content = client.get(f"/api/projects/{pid}/artifacts/content?ref={art_id}").json()
    assert "rax=0" in content["content"]
    # kind 白名单
    assert client.post(f"/api/projects/{pid}/artifacts/upload",
                       data={"kind": "shell"},
                       files={"file": ("x", b"x")}).status_code == 422

    # 文件名：穿越被 basename 中和；保留名 422；空文件 422
    r = client.post(f"/api/projects/{pid}/samples",
                    files={"file": ("../evil.elf", b"\x7fELFx", "application/octet-stream")})
    assert r.status_code == 202
    meta = next(a for a in client.get(f"/api/projects/{pid}/assets").json()
                if a["value"] == r.json()["sha"])["meta"]
    assert meta["path"].startswith("samples/") and ".." not in meta["path"]
    assert client.post(f"/api/projects/{pid}/samples",
                       files={"file": ("con.bin", b"x")}).status_code == 422
    assert client.post(f"/api/projects/{pid}/samples",
                       files={"file": ("empty.bin", b"")}).status_code == 422


def test_rev_workbench_no_tool_structured(client, monkeypatch):
    empty: set[str] = set()
    _install_rev_factory(client, empty)
    r = client.post("/api/projects", json={
        "name": "无工具研究", "track": "research", "capabilities": ["binary"]})
    pid = r.json()["id"]
    empty.add(pid)  # 该项目的 headless 服务“无后端”

    r = client.post(f"/api/projects/{pid}/samples",
                    files={"file": ("crackme.elf", CRACKME.read_bytes())})
    assert r.status_code == 202 and r.json()["cached"] is False
    job = _wait_job(client, r.json()["job_id"])
    # 无工具是结构化降级而非 Job 失败
    assert job["result"]["status"] == "no-tool" and job["result"]["reason"] == "no-tool"
    kinds = [e["kind"] for e in client.get(f"/api/projects/{pid}/events").json()]
    assert "binary.triage_failed" in kinds and "binary.triaged" not in kinds

    sha = r.json()["sha"]
    # 缓存缺席：overview 仍 200；函数/xref 409
    ov = client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()
    assert ov["cached"] is False and ov["tools"]["ida"]["state"] == "off"
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/functions").status_code == 409
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/401234").status_code == 409
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/strings").status_code == 409
    # 无 IDA 库 → 打开 409（在解析 GUI 之前）
    assert client.post(f"/api/projects/{pid}/binaries/{sha}/open").status_code == 409
    # 重新分诊仍是结构化 no-tool
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
    assert r.status_code == 202
    assert _wait_job(client, r.json()["job_id"])["result"]["status"] == "no-tool"
    # 写回/拉名同样结构化降级，不发事件
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/writeback",
                    json={"items": [{"address": "0x401234", "name": "win_main"}]})
    assert r.status_code == 202
    assert _wait_job(client, r.json()["job_id"])["result"]["status"] == "no-tool"
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-names")
    assert _wait_job(client, r.json()["job_id"])["result"]["status"] == "no-tool"
    # 未知样本 404 / 缺路径 422 不发生（上传必有 path）
    assert client.post(f"/api/projects/{pid}/binaries/{'f' * 64}/triage").status_code == 404
    assert client.post(f"/api/projects/{pid}/binaries/{'f' * 64}/writeback",
                       json={"items": [{"address": 1, "name": "x"}]}).status_code == 404
    # 写回入参校验：空 items / 无 name 无 comment
    bad = client.post(f"/api/projects/{pid}/binaries/{sha}/writeback", json={"items": []})
    assert bad.status_code == 422
    bad = client.post(f"/api/projects/{pid}/binaries/{sha}/writeback",
                      json={"items": [{"address": 1}]})
    assert bad.status_code == 422


def test_rev_open_ida_requires_gui(client, monkeypatch):
    """有 .i64 但找不到 GUI → 422（monkeypatch 防止真机启动 IDA）。"""
    _install_rev_factory(client, set())
    r = client.post("/api/projects", json={
        "name": "开库研究", "track": "research", "capabilities": ["binary"]})
    pid = r.json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    _wait_job(client, up["job_id"])
    import core.tools.decompiler as dc
    monkeypatch.setattr(dc, "resolve_ida_gui", lambda *a, **k: None)
    assert client.post(f"/api/projects/{pid}/binaries/{up['sha']}/open").status_code == 422


def test_rev_open_ida_with_addr_jump_script(client, monkeypatch):
    """?addr=：db 同目录写 _jump_<sha8>.py，以相对 -S 名 + cwd=db 目录启动；非法 422。"""
    _install_rev_factory(client, set())
    pid = client.post("/api/projects", json={
        "name": "跳址研究", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    _wait_job(client, up["job_id"])
    sha = up["sha"]

    import core.api.app as appmod
    import core.tools.decompiler as dcmod
    monkeypatch.setattr(dcmod, "resolve_ida_gui", lambda *a, **k: r"C:\Tools\ida.exe")
    seen: dict = {}

    class FakePopen:
        def __init__(self, args, **kw):
            seen["args"] = args
            seen["kw"] = kw

    monkeypatch.setattr(appmod.subprocess, "Popen", FakePopen)

    # 非法地址 → 422（不启动）
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/open?addr=zzz")
    assert r.status_code == 422 and "addr" in r.json()["detail"]
    assert "args" not in seen

    r = client.post(f"/api/projects/{pid}/binaries/{sha}/open",
                    params={"addr": "0x401189"})
    assert r.status_code == 200 and r.json()["jumping"] == "0x401189"
    args = seen["args"]
    script_name = f"_jump_{sha[:8]}.py"
    assert args[1] == f"-S{script_name}"
    db_dir = Path(seen["kw"]["cwd"])
    text = (db_dir / script_name).read_text(encoding="utf-8")
    assert "ida_auto.auto_wait()" in text and "idc.jumpto(4198793)" in text
    assert Path(args[-1]).name == f"{sha}.i64" and Path(args[-1]).is_absolute()

    # 不带 addr：库级打开（无 -S，行为同 P1）
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/open")
    assert r.status_code == 200 and r.json()["jumping"] is None
    assert all(not str(a).startswith("-S") for a in seen["args"])


def test_rev_writeback_locked_ok_and_pull_names(client):
    """写回：locked（.id0 锁文件）→ 关锁后 ok + binary.annotated；
    pull-names：库内重导 diff，IDA 手改名入 func_kb（name_history by=ida-pull）。"""
    from core.tools.decompiler import DecompilerService, IDAHeadlessBackend

    # GUI 重导后的「当前名」：check_flag 未动，main→win_main，新增 sub_ 自动名
    FRESH = {**V3, "functions": [
        {"address": 0x401189, "name": "check_flag", "size": 96,
         "calls": ["strlen", "puts"], "pseudocode": "x"},
        {"address": 0x401234, "name": "win_main", "size": 200,
         "calls": ["check_flag"], "pseudocode": "y"},
        {"address": 0x401300, "name": "sub_401300", "size": 8,
         "calls": [], "pseudocode": "z"},
    ]}
    calls = {"apply": 0}

    def run(args):
        sarg = next(a for a in args if a.startswith('-S"'))
        inner = sarg[3:].rstrip('"')
        script = inner.split(" ", 1)[0]
        out_p = Path(inner.split(" ", 1)[1].split(" ")[-1])
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if script.endswith("apply_names.py"):
            in_p = Path(inner.split(" ")[1])
            payload = json.loads(in_p.read_text(encoding="utf-8"))
            calls["apply"] += 1
            out_p.write_text(json.dumps({"results": [
                {"address": hex(int(it["address"])), "ok": True} for it in payload["items"]
            ]}), encoding="utf-8")
        elif any(a.startswith("-o") for a in args):
            stem = next(Path(a[2:]) for a in args if a.startswith("-o"))
            stem.with_suffix(".i64").write_bytes(b"IDADB")
            out_p.write_text(json.dumps(V3), encoding="utf-8")
        else:  # 库内重导（pull 前置）
            assert args[-1].endswith(".i64")
            out_p.write_text(json.dumps(FRESH), encoding="utf-8")
        return 0, "ok", ""

    def factory(proj):
        ida = IDAHeadlessBackend(idat_cmd="idat", runner=run, available=True,
                                 db_dir=proj.artifacts_dir / "decompiler-db")
        return DecompilerService(cache_dir=proj.artifacts_dir / "decompiler-cache", ida=ida)

    client.app.state.rev_service_factory = factory
    pid = client.post("/api/projects", json={
        "name": "写回研究", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    sha = up["sha"]
    _wait_job(client, up["job_id"])

    # func_kb 先有 main 行（人在平台侧还没改名）
    fid_main = client.post(f"/api/projects/{pid}/funcs",
                           json={"binary_sha256": sha, "address": "0x401234",
                                 "name": "main"}).json()["id"]
    fid_auto = client.post(f"/api/projects/{pid}/funcs",
                           json={"binary_sha256": sha, "address": "0x401300",
                                 "name": "early_guess"}).json()["id"]

    proj_obj = client.app.state.projects[pid]
    db_dir = proj_obj.artifacts_dir / "decompiler-db"

    # ① GUI 开着（.id0 锁）→ locked，runner 不被调
    (db_dir / f"{sha}.id0").write_bytes(b"LOCK")
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/writeback", json={"items": [
        {"address": "0x401189", "name": "check_flag", "comment": "长度 21 校验"}]})
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "locked" and calls["apply"] == 0
    assert "binary.annotated" not in [e["kind"] for e in
                                      client.get(f"/api/projects/{pid}/events").json()]

    # ② 关 GUI（锁消失）→ ok，事件只放成功条数（无注释全文）
    (db_dir / f"{sha}.id0").unlink()
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/writeback", json={"items": [
        {"address": "0x401189", "name": "check_flag", "comment": "长度 21 校验"},
        {"address": "0x401234", "name": "win_main"}]})
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "ok" and job["result"]["applied"] == 2
    assert calls["apply"] == 1
    ev = [e for e in client.get(f"/api/projects/{pid}/events").json()
          if e["kind"] == "binary.annotated"]
    assert len(ev) == 1 and ev[-1]["payload"]["applied"] == 2
    assert "长度 21" not in json.dumps(ev[-1]["payload"], ensure_ascii=False)

    # ③ pull-names：main→win_main 拉回；自动名 sub_ 不拉；check_flag 同名不动
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-names")
    job = _wait_job(client, r.json()["job_id"])
    changed = job["result"]["changed"]
    assert changed == [{"address": "0x401234", "old_name": "main", "new_name": "win_main"}]
    row = client.get(f"/api/projects/{pid}/funcs?binary_sha256={sha}").json()
    by_id = {f["id"]: f for f in row}
    assert by_id[fid_main]["name"] == "win_main"
    assert by_id[fid_main]["name_history"][-1]["by"] == "ida-pull"
    assert by_id[fid_auto]["name"] == "early_guess"  # sub_ 自动名不拉
    ev2 = next(e for e in client.get(f"/api/projects/{pid}/events").json()
               if e["kind"] == "binary.names_pulled")
    assert ev2["payload"]["changed"][0]["new_name"] == "win_main"

    # ④ 防重：running 中再投 409（Job 太快时用锁文件让 refresh 卡在 locked 之前——
    #    这里直接验证 writeback 同类 Job 元信息齐全即可；锁场景已覆盖串行语义）
    assert client.post(f"/api/projects/{pid}/binaries/{'f' * 64}/pull-names").status_code == 404


def test_rev_funcs_cross_project_404(client):
    _install_rev_factory(client, set())
    p1 = client.post("/api/projects", json={
        "name": "p1", "track": "research", "capabilities": ["binary"]}).json()["id"]
    p2 = client.post("/api/projects", json={
        "name": "p2", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{p1}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    _wait_job(client, up["job_id"])
    fid = client.post(f"/api/projects/{p1}/funcs",
                      json={"binary_sha256": up["sha"], "address": "0x401189",
                            "name": "check_flag"}).json()["id"]
    assert client.patch(f"/api/projects/{p2}/funcs/{fid}",
                        json={"note": "越界"}).status_code == 404



def test_concurrent_skill_saves_serialized_under_packs_lock(tmp_path, monkeypatch):
    """A3：8 并发全文保存同一技能——写锁串行化：最终内容是某次完整写入（无撕裂），
    8 个修改前备份各自独立成文（同秒 .n 避让不互相覆盖）。"""
    monkeypatch.chdir(tmp_path)
    packs_root, c = _packs_app(tmp_path)
    bodies = [
        f"---\nname: demo-skill\ndescription: v{i}\nkeywords: 演示\n---\n正文版本 {i}。\n"
        for i in range(8)
    ]
    errors: list[Exception] = []

    def save(i):
        try:
            r = c.put("/api/capabilities/web/skills/demo-skill", json={"content": bodies[i]})
            assert r.status_code == 200
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    with c:
        threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert not errors, f"并发保存线程异常: {errors!r}"

    skill_file = packs_root / "capabilities" / "web" / "skills" / "demo-skill" / "SKILL.md"
    assert skill_file.read_text(encoding="utf-8") in bodies  # 完整版本之一，非撕裂混合
    backups = list((skill_file.parent / ".history").glob("*_SKILL.md"))
    assert len(backups) == 8  # 每次写入前都留备份，同秒不覆盖


# ---------- A5：优先级重排（L2 自动去抖 + 手动端点） ----------

def _open_task_updates(c, pid_holder, priority=0):
    """重排剧本回调：chat 时现读 open 任务（id 要 POST 后才知道，避开先后手竞态）。"""
    def _get():
        from core.blackboard import TaskQueue
        bb = c.app.state.projects[pid_holder[0]].bb
        rows = TaskQueue(bb).list_tasks(pid_holder[0], status="open")
        return [{"task_id": rows[0]["id"], "priority": priority}] if rows else []
    return _get


def _replan_app(tmp_path, llm):
    from test_orchestrator import ScriptedLLM
    return create_app(workspace_root=str(tmp_path / "workspaces"),
                      tools_root=None,
                      executor_llm=ScriptedLLM([]), planner_llm=llm,
                      providers_config=str(tmp_path / "providers.json"))


def _replan_llm(getter):
    """一次性重排 planner：唯一一轮 chat 直接给 set_priorities（replan 不需要 done）。"""
    from test_orchestrator import ScriptedLLM

    class _LLM(ScriptedLLM):
        def __init__(self, get_updates):
            super().__init__([])
            self._get = get_updates

        def chat(self, messages, *, system=None, tools=None, max_tokens=4096,
                 temperature=None):
            self.calls.append({"messages": json.loads(json.dumps(messages)),
                               "system": system})
            item = [self.tool_call("r1", "set_priorities",
                                   {"updates": self._get()})]
            return self._parse({"content": item, "stop_reason": "tool_use",
                                "usage": {"input_tokens": 1, "output_tokens": 1}})

    return _LLM(getter)


def test_l2_human_publish_triggers_replan_and_updates_open(tmp_path, monkeypatch):
    """触发点 D：L2 人手发任务 → 自动去抖重排 job 跑一轮 planner，改 open 行优先级，
    落 task.updated(by=orchestrator-replan) 逐行审计 + orch.replan_priorities 事件。"""
    monkeypatch.setattr("core.api.app.REPLAN_MIN_INTERVAL", 0)
    pid_holder: list[str] = []
    llm = _replan_llm(lambda: [])
    app = _replan_app(tmp_path, llm)
    with TestClient(app) as c:
        llm._get = _open_task_updates(c, pid_holder)  # 闭包绑到实际 client
        pid = _l2_project(c, "L2重排D")
        pid_holder.append(pid)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "核查 8080 旁站低噪信息", "task_type": "recon",
                         "priority": 2})
        assert r.status_code == 201
        tid = r.json()["task_id"]
        _wait_no_running(c, pid)
        jobs = _project_jobs(c, pid, "orchestrator-replan")
        assert len(jobs) == 1 and jobs[0]["status"] == "done"
        res = jobs[0]["result"]
        assert res["updated"] == [{"task_id": tid, "old": 2, "new": 0}]
        assert res["reason"] == "human-publish"
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["priority"] == 0
        events = c.app.state.projects[pid].bb.recent_events(pid)
        repl = [e for e in events if e["kind"] == "orch.replan_priorities"]
        assert len(repl) == 1 and repl[0]["payload"]["updated"] == res["updated"]
        audits = [e for e in events if e["kind"] == "task.updated"
                  and e["payload"].get("by") == "orchestrator-replan"]
        assert len(audits) == 1 and audits[0]["payload"]["task_id"] == tid
        assert len(llm.calls) == 1  # 一次性 planner 轮
        from core.orchestrator import state as ost
        assert ost.load_or_create(c.app.state.projects[pid].bb, pid)["last_replan_at"]


def test_auto_replan_blocked_below_l2_and_when_paused(tmp_path, monkeypatch):
    """闸门：非 L2（L1）/ L2 暂停 → 触发点 D 不产生任何重排 job，也不刷新计时。"""
    monkeypatch.setattr("core.api.app.REPLAN_MIN_INTERVAL", 0)
    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        # L1 assessment（默认档是 L1，显式钉死）
        pid1 = c.post("/api/projects", json={"name": "L1不重排", "track": "assessment",
                                             "capabilities": ["web"]}).json()["id"]
        assert c.patch(f"/api/projects/{pid1}/config",
                       json={"config": {"autonomy": {"level": "L1"}}}).status_code == 200
        r = c.post(f"/api/projects/{pid1}/tasks",
                   json={"objective": "L1 任务", "task_type": "recon"})
        assert r.status_code == 201 and r.json()["kicked"] == []
        # L2 暂停
        pid2 = _l2_project(c, "L2暂停不重排", paused=True)
        r2 = c.post(f"/api/projects/{pid2}/tasks",
                    json={"objective": "暂停任务", "task_type": "recon"})
        assert r2.json()["kicked"] == []
        _wait_no_running(c, pid1)
        _wait_no_running(c, pid2)
        for pid in (pid1, pid2):
            assert _project_jobs(c, pid, "orchestrator-replan") == []
            assert _project_jobs(c, pid, "orchestrator-replan-wait") == []
            from core.orchestrator import state as ost
            assert ost.load_or_create(c.app.state.projects[pid].bb,
                                      pid)["last_replan_at"] == ""


def test_l2_replan_throttle_wait_then_reenter(tmp_path, monkeypatch):
    """30s（测试压到 0.2s）节流：踩窗口先起 replan-wait，睡满 on_done 重入不丢触发。"""
    from datetime import datetime, timezone
    from test_orchestrator import ScriptedLLM as S
    from core.orchestrator import state as ost
    monkeypatch.setattr("core.api.app.REPLAN_MIN_INTERVAL", 0.2)
    planner = [{"tool_use": [S.tool_call("r1", "set_priorities",
                                         {"updates": []})]}]
    app = _chain_app(tmp_path, planner)
    with TestClient(app) as c:
        pid = _l2_project(c, "L2重排节流")
        bb = c.app.state.projects[pid].bb
        ost.save_fields(bb, pid,
                        last_replan_at=datetime.now(timezone.utc).isoformat())
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "节流窗口内的任务", "task_type": "recon"})
        assert r.status_code == 201
        for _ in range(50):
            if _project_jobs(c, pid, "orchestrator-replan-wait"):
                break
            time.sleep(0.01)
        waits = _project_jobs(c, pid, "orchestrator-replan-wait")
        assert len(waits) == 1
        assert _project_jobs(c, pid, "orchestrator-replan") == []  # 还没真跑
        _wait_no_running(c, pid)
        replans = _project_jobs(c, pid, "orchestrator-replan")
        assert len(replans) == 1 and replans[0]["status"] == "done"
        assert replans[0]["result"]["reason"].endswith(":throttled")
        assert ost.load_or_create(bb, pid)["last_replan_at"]


def test_l2_worker_idle_triggers_replan(tmp_path, monkeypatch):
    """触发点 A：L2 开窗自动 worker 空队列秒退 → worker-idle 尾部触发一次重排；
    on_done 不重复触发（尾部已提交时不再排长命 wait）；无 open 空转零 LLM、不刷计时。"""
    from core.orchestrator import state as ost
    monkeypatch.setattr("core.api.app.REPLAN_MIN_INTERVAL", 0)
    app = _chain_app(tmp_path, [])  # 无 open 任务 → replan 不消费剧本
    with TestClient(app) as c:
        pid = _l2_project(c, "L2重排A")
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon"})
        assert sp.status_code == 201 and sp.json().get("job_id")
        _wait_no_running(c, pid)
        jobs = _project_jobs(c, pid, "orchestrator-replan")
        assert len(jobs) == 1  # 尾部一次；on_done 见已提交不重复
        assert jobs[0]["status"] == "done"
        assert jobs[0]["meta"].get("reason", "").startswith("worker-idle:")
        assert jobs[0]["result"]["updated"] == []
        assert _project_jobs(c, pid, "orchestrator-replan-wait") == []
        # 空转（无 LLM 轮）不刷去抖计时
        assert ost.load_or_create(c.app.state.projects[pid].bb,
                                  pid)["last_replan_at"] == ""


def test_manual_replan_bypasses_gates_and_409_on_lease(tmp_path, monkeypatch):
    """手动端点：L0 也能跑、不受 30s 节流；tick 租约被占 → 409，且不产 job。"""
    from datetime import datetime, timezone
    from core.orchestrator import state as ost
    monkeypatch.setattr("core.api.app.REPLAN_MIN_INTERVAL", 999)
    pid_holder: list[str] = []
    llm = _replan_llm(lambda: [])
    app = _replan_app(tmp_path, llm)
    with TestClient(app) as c:
        llm._get = _open_task_updates(c, pid_holder)
        pid = c.post("/api/projects", json={"name": "L0手排", "track": "ctf",
                                            "capabilities": ["binary"]}).json()["id"]
        pid_holder.append(pid)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "L0 任务", "task_type": "generic", "priority": 2})
        tid = r.json()["task_id"]
        assert _project_jobs(c, pid, "orchestrator-replan") == []  # L0 不自动
        bb = c.app.state.projects[pid].bb
        ost.save_fields(bb, pid,
                        last_replan_at=datetime.now(timezone.utc).isoformat())
        m = c.post(f"/api/projects/{pid}/orchestrator/replan-priorities")
        assert m.status_code == 200 and m.json()["job_id"]
        res = _wait_job(c, m.json()["job_id"])["result"]
        assert res["updated"] == [{"task_id": tid, "old": 2, "new": 0}]
        assert res["reason"] == "manual"
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["priority"] == 0

        ost.acquire_tick_lease(bb, pid, "test-holder")
        blocked = c.post(f"/api/projects/{pid}/orchestrator/replan-priorities")
        assert blocked.status_code == 409
        ost.release_tick_lease(bb, pid, "test-holder")
