"""分阶段工作流测试（pentest-phased-workflow M1+M2）。

覆盖：剧本解析/加载与项目覆写、门判定（含空闲逃生）、enter_phase 幂等去重与
抵达校准、gate_block_reason 各态、doctor phase-* 码、API 双层拦截与三档过门分流。
编排器侧（门拒收 / phase_section 注入）在 tests/test_orchestrator.py。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.blackboard import TaskQueue
from core.blackboard.assets import register_asset
from core.blackboard.store import Blackboard
from core.phases import (
    default_phase_id,
    enter_phase,
    evaluate_gate,
    forward_targets,
    gate_block_reason,
    gate_metrics,
    load_track_phases,
    parse_phase_file,
    phase_enabled,
    phase_spec,
    read_state,
)
from core.projects import ProjectStore
from core.skills.doctor import diagnose

# ---------- 夹具 ----------

_PHASE_YAMLS = {
    "recon": """name: 信息收集
goal: 摸清资产面
order: 1
focus:
  recon: 2
  asset-enum: 3
gate:
  min_assets: 10
  min_high_value: 1
  idle_rounds: 2
gate_types: [exploit]
tasks:
  - task_type: recon
    role: recon
    objective: 被动测绘产出资产清单
    acceptance: 资产清单登记
next: [pentest]
""",
    "pentest": """name: 渗透测试
goal: 产出 verified 发现
order: 2
gate:
  min_verified: 1
tasks:
  - task_type: exploit
    role: external-entry
    objective: 对高价值目标做漏洞验证
next: [recon, report]
""",
    "report": """name: 报告
goal: 汇总报告
order: 3
tasks:
  - task_type: report
    role: report-writer
    objective: 撰写渗透测试报告
next: [pentest]
""",
}


def _make_packs(root: Path) -> Path:
    """最小 pentest 轨 packs：阶段剧本 + task_types 注册表 + 专家池。"""
    ph = root / "tracks" / "pentest" / "phases"
    ph.mkdir(parents=True)
    for stem, text in _PHASE_YAMLS.items():
        (ph / f"{stem}.yaml").write_text(text, encoding="utf-8")
    (root / "tracks" / "pentest" / "task_types.yaml").write_text(
        "recon: passive\nasset-enum: passive\nexploit: low\nreport: passive\n",
        encoding="utf-8")
    ex = root / "experts"
    ex.mkdir()
    for eid in ("recon", "external-entry", "report-writer", "_generalist"):
        (ex / f"{eid}.yaml").write_text(
            f"name: {eid}\ndescription: 测试专家\ntracks: [pentest]\n",
            encoding="utf-8")
    # 无阶段剧本的第二轨（API 端「未启用」断言用；track.yaml 缺失=目录名兜底）
    ctf = root / "tracks" / "ctf"
    ctf.mkdir()
    (ctf / "task_types.yaml").write_text("generic: passive\n", encoding="utf-8")
    return root


@pytest.fixture()
def packs(tmp_path) -> Path:
    return _make_packs(tmp_path / "packs")


@pytest.fixture()
def proj(packs, tmp_path):
    store = ProjectStore(str(tmp_path / "workspaces"))
    return store.create_project("阶段测试", track="pentest")


def _seed_assets(bb: Blackboard, pid: str, n: int = 10, hv: int = 1,
                 net: str = "10.1") -> None:
    for i in range(n):
        register_asset(bb, pid, f"{net}.{i}.7", type_="host")
    for i in range(hv):
        register_asset(bb, pid, f"10.9.0.{i}", type_="host",
                       meta={"tags": ["高价值"]})


# ---------- 剧本解析 ----------

def test_parse_and_spec_full(packs):
    spec = phase_spec("recon", parse_phase_file(packs / "tracks/pentest/phases/recon.yaml"))
    assert spec["name"] == "信息收集" and spec["goal"] == "摸清资产面"
    assert spec["order"] == 1 and spec["next"] == ["pentest"]
    assert spec["focus"] == {"recon": 2, "asset-enum": 3}
    assert spec["gate"] == {"min_assets": 10, "min_high_value": 1, "idle_rounds": 2}
    assert spec["gate_types"] == ["exploit"]
    assert spec["tasks"] == [{"task_type": "recon", "role": "recon",
                              "objective": "被动测绘产出资产清单",
                              "acceptance": "资产清单登记"}]


def test_parse_rejects_bad_shapes(tmp_path):
    p = tmp_path / "bad.yaml"
    cases = [
        "- 顶层列表项\n",                     # 顶层列表项
        "focus: {a: 1}\n",                    # 内联 map
        "name: x\nnext:\n    - 过深缩进\n",   # 4 空格但无 tasks 项上下文
    ]
    for text in cases:
        p.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            parse_phase_file(p)
    # tasks 条目缺 task_type/objective → phase_spec 拒收（doctor 折入 phase-bad-yaml）
    p.write_text("name: x\ngoal: y\ntasks:\n  - task_type: recon\n    role: r\n",
                 encoding="utf-8")
    with pytest.raises(ValueError):
        phase_spec("bad", parse_phase_file(p))


def test_load_order_forward_and_override(packs):
    book = load_track_phases(packs, "pentest")
    assert list(book) == ["recon", "pentest", "report"]  # order 升序
    assert default_phase_id(book) == "recon"
    assert forward_targets(book, book["recon"]) == ["pentest"]      # 前向
    assert forward_targets(book, book["pentest"]) == ["report"]     # next 含回退目标但只取前向
    assert forward_targets(book, book["report"]) == []              # 终点（pentest 是回退）
    assert phase_enabled(packs, "pentest") and not phase_enabled(packs, "ctf")

    # 项目级覆写：同名整体替换 + 新增 + order 沿用基线；坏条目跳过
    ov = load_track_phases(packs, "pentest", {"phases": {
        "recon": {"name": "覆写阶段", "goal": "g", "gate": {"min_assets": 3},
                  "next": ["pentest"]},
        "scan": {"name": "追加阶段", "order": 2, "next": []},
        "broken": {"tasks": [{"task_type": "recon"}]},  # 缺 objective → 跳过
    }})
    assert ov["recon"]["name"] == "覆写阶段" and ov["recon"]["gate"] == {"min_assets": 3}
    assert ov["recon"]["order"] == 1 and "scan" in ov and "broken" not in ov


# ---------- 门判定 ----------

def test_evaluate_gate_matrix():
    gate = {"min_assets": 10, "min_high_value": 1, "idle_rounds": 2}
    m = {"assets": 3, "high_value": 0, "verified": 0, "idle_rounds": 0}
    met, unmet = evaluate_gate(gate, m)
    assert not met and unmet == ["资产 3/10", "高价值资产 0/1"]
    met, unmet = evaluate_gate(gate, {**m, "assets": 12, "high_value": 2})
    assert met and not unmet                                    # 非 idle 全达标
    met, _ = evaluate_gate(gate, {**m, "idle_rounds": 2})
    assert met                                                  # 收集饱和逃生
    met, unmet = evaluate_gate(gate, {**m, "idle_rounds": 1})
    assert not met                                              # 逃生未满
    assert evaluate_gate({}, m) == (True, [])                   # 无门=恒过


def test_gate_metrics_and_block_reason(proj, packs):
    bb, pid = proj.bb, proj.id
    _seed_assets(bb, pid, n=3, hv=0)
    m = gate_metrics(bb, pid, idle_rounds=1)
    assert m["assets"] == 3 and m["high_value"] == 0 and m["idle_rounds"] == 1
    # recon 门拦 exploit
    reason = gate_block_reason(bb, pid, proj.meta, "pentest", packs, task_type="exploit")
    assert reason and "入场门" in reason and "exploit" in reason
    assert gate_block_reason(bb, pid, proj.meta, "pentest", packs, task_type="recon") is None
    # 空闲逃生后放行
    assert gate_block_reason(bb, pid, proj.meta, "pentest", packs,
                             task_type="exploit", idle_rounds=2) is None
    # 指标达标后放行（异网段避免与首播种子撞去重）
    _seed_assets(bb, pid, n=7, hv=1, net="10.2")
    assert gate_block_reason(bb, pid, proj.meta, "pentest", packs,
                             task_type="exploit") is None
    # pentest 阶段无 gate_types：report 不拦（min_verified 门只驱动过门分流）
    assert gate_block_reason(bb, pid, {"current_phase": "pentest"}, "pentest", packs,
                             task_type="report") is None
    # report 阶段无 gate：全放行
    assert gate_block_reason(bb, pid, {"current_phase": "report"}, "pentest", packs,
                             task_type="exploit") is None


# ---------- enter_phase：首发 / 去重 / 抵达校准 ----------

def test_enter_phase_publishes_playbook_once(proj, packs):
    bb, pid = proj.bb, proj.id
    r = enter_phase(proj, "recon", by="system", packs_root=packs, reason="建项")
    assert r["from"] == "recon" and len(r["published"]) == 1  # 首进=默认阶段，首发 1 条
    rows = TaskQueue(bb).list_tasks(pid)
    assert len(rows) == 1 and rows[0]["created_by"] == "playbook"
    assert rows[0]["noise_budget"] == "passive" and rows[0]["role"] == "recon"
    assert read_state(proj.meta)["fired"]["recon"]  # 指纹已记
    evs = [dict(e) for e in bb.conn.execute(
        "SELECT kind, payload FROM events WHERE project_id=? AND kind='phase.changed'",
        (pid,)).fetchall()]
    assert evs and json.loads(evs[0]["payload"])["to"] == "recon"

    # 回退重进不重发：recon → pentest → recon → pentest
    r2 = enter_phase(proj, "pentest", by="human", packs_root=packs)
    assert len(r2["published"]) == 1 and r2["spec"]["name"] == "渗透测试"
    assert enter_phase(proj, "recon", by="human", packs_root=packs)["published"] == []
    assert enter_phase(proj, "pentest", by="human", packs_root=packs)["published"] == []
    assert len(TaskQueue(bb).list_tasks(pid)) == 2


def test_enter_phase_dedup_target_and_calibration(proj, packs):
    bb, pid = proj.bb, proj.id
    # 同款任务已在队：首发跳过（指纹仍计入 fired）
    TaskQueue(bb).publish(pid, "被动测绘产出资产清单", task_type="recon",
                          noise_budget="passive")
    r = enter_phase(proj, "recon", by="system", packs_root=packs)
    assert r["published"] == []
    assert len(read_state(proj.meta)["fired"]["recon"]) == 1
    # 抵达校准：recon 门已过（11 资产含 1 高价值）→ 抵达即标记已分流
    _seed_assets(bb, pid, n=10, hv=1)
    enter_phase(proj, "recon", by="system", packs_root=packs)
    assert read_state(proj.meta)["notified"] == "pentest"
    # 进入 pentest：其门（min_verified）未过 → 校准剥键（防 L2 秒弹回的对称面）
    enter_phase(proj, "pentest", by="human", packs_root=packs)
    assert read_state(proj.meta)["notified"] is None


# ---------- doctor phase-* 码 ----------

def test_doctor_phase_codes(tmp_path):
    root = _make_packs(tmp_path / "packs")
    assert not [i for i in diagnose(root).issues if i.code.startswith("phase-")]
    ph = root / "tracks" / "pentest" / "phases"

    def write(stem: str, text: str) -> None:
        (ph / f"{stem}.yaml").write_text(text, encoding="utf-8")

    write("bad1", "focus: {a: 1}\n")                       # phase-bad-yaml
    write("nofields", "next: [pentest]\n")                 # phase-field-missing ×2
    write("badtt", "name: x\ngoal: y\ntasks:\n  - task_type: nosuch\n    objective: o\n")
    write("badrole", "name: x\ngoal: y\ntasks:\n  - task_type: recon\n    role: ghost\n    objective: o\n")
    write("badnext", "name: x\ngoal: y\nnext: [ghost-stage]\n")
    write("badgate", "name: x\ngoal: y\ngate:\n  min_gold: 5\n")
    write("badgt", "name: x\ngoal: y\ngate_types: [nosuch]\n")
    codes = {(i.code, i.level) for i in diagnose(root).issues
             if i.code.startswith("phase-")}
    assert ("phase-bad-yaml", "error") in codes
    assert ("phase-field-missing", "warning") in codes
    assert ("phase-tasktype-unregistered", "error") in codes
    assert ("phase-expert-missing", "error") in codes
    assert ("phase-next-missing", "error") in codes
    assert ("phase-gate-unknown-key", "warning") in codes
    assert ("phase-gatetype-unregistered", "warning") in codes


# ---------- API：双层拦截 / 流转端点 / 三档过门分流 ----------

@pytest.fixture()
def client(tmp_path, packs):
    from core.api.app import create_app
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"),
                     sediment_proposals=False)
    with TestClient(app) as c:
        yield c


def _mk(client) -> str:
    r = client.post("/api/projects", json={"name": "阶段项目", "track": "pentest"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _events(client, pid: str, kind: str) -> list[dict]:
    proj = client.app.state.projects[pid]
    return [dict(r) for r in proj.bb.conn.execute(
        "SELECT * FROM events WHERE project_id=? AND kind=?", (pid, kind)).fetchall()]


def test_create_enters_initial_phase_without_publish(client):
    """建项只登记初始阶段（current_phase + history + 事件），不发剧本任务——
    mission/目标商议前不发静态任务，首发随显式流转触发。"""
    pid = _mk(client)
    r = client.get(f"/api/projects/{pid}/phase").json()
    assert r["enabled"] is True and r["current"] == "recon"
    assert r["spec"]["name"] == "信息收集" and r["gate"]["forward"] == ["pentest"]
    assert r["gate"]["metrics"]["assets"] == 0 and not r["gate"]["met"]
    assert client.get(f"/api/projects/{pid}/tasks").json() == []
    evs = _events(client, pid, "phase.changed")
    assert len(evs) == 1 and json.loads(evs[0]["payload"])["published"] == []
    # 显式流转时剧本首发照常（recon→pentest 首发 exploit 任务）
    r = client.post(f"/api/projects/{pid}/phase", json={"to": "pentest"})
    assert r.status_code == 200 and len(r.json()["published"]) == 1


def test_phase_disabled_track(client):
    r = client.post("/api/projects", json={"name": "无阶段", "track": "ctf"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.get(f"/api/projects/{pid}/phase").json() == {"enabled": False}
    r = client.post(f"/api/projects/{pid}/phase", json={"to": "recon"})
    assert r.status_code == 422 and "无阶段剧本" in r.json()["detail"]


def test_publish_gate_422_then_transition_allows(client):
    pid = _mk(client)
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "尝试利用", "task_type": "exploit"})
    assert r.status_code == 422 and "入场门" in r.json()["detail"]
    # 流转方向/目标校验
    r = client.post(f"/api/projects/{pid}/phase", json={"to": "report"})
    assert r.status_code == 422 and "不允许的流转" in r.json()["detail"]
    r = client.post(f"/api/projects/{pid}/phase", json={"to": "ghost"})
    assert r.status_code == 422 and "目标阶段不存在" in r.json()["detail"]
    # 人工流转到 pentest：剧本首发 exploit 任务 + 发布 API 放行
    r = client.post(f"/api/projects/{pid}/phase", json={"to": "pentest",
                                                        "reason": "资产够了"})
    assert r.status_code == 200 and r.json()["from"] == "recon"
    assert len(r.json()["published"]) == 1
    assert client.get(f"/api/projects/{pid}/phase").json()["current"] == "pentest"
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "尝试利用", "task_type": "exploit",
                          "conflict_keys": ["host:10.0.0.5"]})
    assert r.status_code == 201, r.text
    # phase.changed 留痕 by=human
    evs = _events(client, pid, "phase.changed")
    assert any(json.loads(e["payload"])["by"] == "human" for e in evs)


def test_l2_auto_transition(client):
    pid = _mk(client)
    assert client.patch(f"/api/projects/{pid}/config",
                        json={"config": {"autonomy": {"level": "L2"}}}).status_code == 200
    _seed_assets(client.app.state.projects[pid].bb, pid, n=10, hv=1)
    client.app.state.phase_gate_check(pid)  # 测试直调口（生产在 _post_tick 出口）
    r = client.get(f"/api/projects/{pid}/phase").json()
    assert r["current"] == "pentest" and r["history"][-1]["auto"] is True
    assert r["history"][-1]["by"] == "orchestrator"
    evs = _events(client, pid, "phase.changed")
    payload = json.loads(evs[-1]["payload"])
    assert payload["auto"] is True and payload["to"] == "pentest"


def test_l1_approval_flow_with_dedup(client):
    pid = _mk(client)
    _seed_assets(client.app.state.projects[pid].bb, pid, n=10, hv=1)
    client.app.state.phase_gate_check(pid)
    appr = client.get(f"/api/projects/{pid}/approvals?status=pending").json()
    assert len(appr) == 1
    action = appr[0]["action"]
    assert action["op"] == "phase_transition" and action["to"] == "pentest"
    assert "阶段流转" in action["summary"] and action["metrics"]["assets"] == 11
    # 同目标审批在途：不重复提单
    client.app.state.phase_gate_check(pid)
    assert len(client.get(f"/api/projects/{pid}/approvals?status=pending").json()) == 1
    # 批准即流转（enter_phase by=approval + 剧本首发）
    r = client.post(f"/api/approvals/{appr[0]['id']}/decide",
                    json={"decision": "approved"})
    assert r.status_code == 200 and r.json()["executed"] is True
    assert r.json()["phase"] == "pentest"
    assert client.get(f"/api/projects/{pid}/phase").json()["current"] == "pentest"


def test_l0_event_flow_and_notified_dedup(client):
    pid = _mk(client)
    assert client.patch(f"/api/projects/{pid}/config",
                        json={"config": {"autonomy": {"level": "L0"}}}).status_code == 200
    _seed_assets(client.app.state.projects[pid].bb, pid, n=10, hv=1)
    client.app.state.phase_gate_check(pid)
    evs = _events(client, pid, "phase.gate_open")
    assert len(evs) == 1 and json.loads(evs[0]["payload"])["to"] == "pentest"
    assert client.get(f"/api/projects/{pid}/phase").json()["notified"] == "pentest"
    client.app.state.phase_gate_check(pid)  # notified 去重：不再发
    assert len(_events(client, pid, "phase.gate_open")) == 1
    # 人工流转抵达目标 → 抵达校准剥 notified
    assert client.post(f"/api/projects/{pid}/phase",
                       json={"to": "pentest"}).status_code == 200
    assert not client.get(f"/api/projects/{pid}/phase").json()["notified"]


def test_idle_rounds_counting_and_escape(client):
    # 计数面（L0：逃生只发事件不提审批单，便于隔离断言）
    pid = _mk(client)
    assert client.patch(f"/api/projects/{pid}/config",
                        json={"config": {"autonomy": {"level": "L0"}}}).status_code == 200
    tick = client.app.state.phase_post_tick
    tick(pid, {"published": []})
    assert client.get(f"/api/projects/{pid}/phase").json()["gate"]["metrics"]["idle_rounds"] == 1
    tick(pid, {"published": []})  # idle=2 且指标未达 → 逃生过门（L0 → 事件提示）
    assert client.get(f"/api/projects/{pid}/phase").json()["gate"]["metrics"]["idle_rounds"] == 2
    assert len(_events(client, pid, "phase.gate_open")) == 1
    tick(pid, {"published": ["task-x"]})  # 有发布 → 计数复位
    assert client.get(f"/api/projects/{pid}/phase").json()["gate"]["metrics"]["idle_rounds"] == 0
    # 逃生审批面（L1 默认档）：资产达标但高价值缺，idle 满 2 轮 → 审批单
    pid2 = _mk(client)
    _seed_assets(client.app.state.projects[pid2].bb, pid2, n=10, hv=0)
    tick2 = client.app.state.phase_post_tick
    tick2(pid2, {"published": []})  # idle=1：逃生未满
    assert client.get(f"/api/projects/{pid2}/approvals?status=pending").json() == []
    tick2(pid2, {"published": []})  # idle=2：收集饱和逃生 → 审批单
    appr = client.get(f"/api/projects/{pid2}/approvals?status=pending").json()
    assert len(appr) == 1 and appr[0]["action"]["op"] == "phase_transition"
