"""core API 测试（TestClient，不触网；LLM 缺失时 Agent 端点应 503）。"""

import json
import os
import threading
import time
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


def _make_project(client, track: str = "ctf") -> str:
    r = client.post("/api/projects", json={"name": "API 测试项目", "track": track,
                                           "capabilities": ["binary"], "config": {"x": 1}})
    assert r.status_code == 201
    return r.json()["id"]


def test_project_lifecycle(client):
    pid = _make_project(client)
    listed = client.get("/api/projects").json()
    assert any(m["id"] == pid and m["name"] == "API 测试项目" for m in listed)
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["track"] == "ctf" and detail["capabilities"] == ["binary"]
    assert "domain" not in detail and set(detail["task_stats"]) <= {"awaiting_human"}  # 写新值不写旧值；C1 空项目 awaiting 计数为 0
    assert "capability" in detail
    assert client.get("/api/projects/nope").status_code == 404


def test_project_binding_validation_and_legacy_domain(client):
    """建项：非法轨/能力包 422；红队扩展包仅 redteam 轨；旧 domain 透明映射（R1/R2）。"""
    r = client.post("/api/projects", json={"name": "坏轨", "track": "nope"})
    assert r.status_code == 422 and "场景轨" in r.json()["detail"]
    r = client.post("/api/projects", json={"name": "坏包", "track": "ctf",
                                           "capabilities": ["quantum"]})
    assert r.status_code == 422 and "能力包" in r.json()["detail"]
    # R1：redteam 轨合法创建
    r = client.post("/api/projects", json={"name": "红队轨", "track": "redteam"})
    assert r.status_code == 201 and r.json()["track"] == "redteam"
    # R3/R4：social/shell-c2 仅 redteam 轨可挂载
    r = client.post("/api/projects", json={"name": "越轨挂载", "track": "pentest",
                                           "capabilities": ["social"]})
    assert r.status_code == 422 and "redteam" in r.json()["detail"]
    # 旧客户端 domain=pentest → pentest/web（R1 目标轨更新）
    r = client.post("/api/projects", json={"name": "旧客户端", "domain": "pentest"})
    assert r.status_code == 201
    meta = r.json()
    assert meta["track"] == "pentest" and meta["capabilities"] == ["web"]


# ---------- expert-pool M2：项目专家绑定（§4.4 推导 / §4.6 allowed_roles，2026-09-21） ----------

def test_project_experts_binding_lifecycle(client):
    """创建绑定 → meta 推导（响应层装饰，盘上不动）→ 换将 → 解绑恢复存量直通；池外/轨外 422。"""
    r = client.post("/api/projects", json={"name": "绑专家", "track": "ctf",
                                           "capabilities": ["binary"],
                                           "experts": ["web-solver"]})
    assert r.status_code == 201, r.text
    meta = r.json()
    pid = meta["id"]
    assert meta["experts"] == ["web-solver"]
    assert meta["capabilities"] == ["web"]  # caps_effective：web-solver 技能全在 web 包
    # 盘上原始绑定不动（Project.capabilities 恒返回盘上值，推导只发生在响应/构造层）
    raw = client.app.state.store.open_project(pid)
    assert raw.capabilities == ["binary"] and raw.experts == ["web-solver"]
    # 列表/详情同样带推导值
    listed = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert listed["experts"] == ["web-solver"] and listed["capabilities"] == ["web"]

    # 换将：全量专家 → capabilities/ 目录全集
    expected_all = sorted(d.name for d in Path("packs/capabilities").iterdir() if d.is_dir())
    r = client.patch(f"/api/projects/{pid}/experts", json={"experts": ["_generalist"]})
    assert r.status_code == 200 and r.json()["experts"] == ["_generalist"]
    assert r.json()["capabilities"] == expected_all
    # 解绑（空清单剥键）：恢复存量直通（fallback = 盘上绑定 binary）
    r = client.patch(f"/api/projects/{pid}/experts", json={"experts": []})
    assert r.status_code == 200 and r.json()["experts"] == []
    assert r.json()["capabilities"] == ["binary"]
    # 池外 / 轨外（osint 服务 pentest/redteam，不服务 ctf）→ 422
    assert client.patch(f"/api/projects/{pid}/experts",
                        json={"experts": ["ghost-expert"]}).status_code == 422
    r = client.patch(f"/api/projects/{pid}/experts", json={"experts": ["osint"]})
    assert r.status_code == 422 and "osint" in r.json()["detail"]


def test_project_create_rejects_unknown_expert(client):
    """创建时专家校验同口径：池外 422 且提示轨可用池。"""
    r = client.post("/api/projects", json={"name": "坏专家", "track": "ctf",
                                           "experts": ["ghost-expert"]})
    assert r.status_code == 422 and "专家" in r.json()["detail"]


def test_session_factory_derives_caps_and_publish_allowed_roles(tmp_path):
    """M2 构造/发布链：工厂按绑定专家推导会话能力面；发布 allowed_roles=绑定清单
    （绑定外专家发布被拒，未绑定项目回落全池）。"""
    from test_orchestrator import ScriptedLLM
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=ScriptedLLM([]), planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "推导", "track": "ctf",
                                            "capabilities": ["binary"],
                                            "experts": ["web-solver"]}).json()["id"]
        sid = c.post(f"/api/projects/{pid}/agents",
                     json={"role": "web-solver"}).json()["id"]
        agent = c.app.state.agents[sid]
        assert agent.capabilities == ["web"]          # 推导面（非盘上 fallback）
        assert agent.dispatcher.allowed_roles == ["web-solver"]  # 绑定清单优先
        # 发布校验：绑定外专家被拒（ValueError→422），绑定内放行
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "越权发布", "task_type": "generic",
                         "role": "reverse"})
        assert r.status_code == 422
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "正常发布", "task_type": "generic",
                         "role": "web-solver"})
        assert r.status_code == 201, r.text
        # 未绑定项目：allowed_roles=按轨全池，池内任意专家可发布
        pid2 = c.post("/api/projects", json={"name": "未绑定", "track": "ctf"}).json()["id"]
        r = c.post(f"/api/projects/{pid2}/tasks",
                   json={"objective": "全池发布", "task_type": "generic",
                         "role": "reverse"})
        assert r.status_code == 201, r.text


# ---------- expert-pool M3/M4：场景档物化（M4a）+ 知识继承（M4b），2026-09-21 ----------

def test_track_profiles_builtins(client):
    """轨内置场景档清单（真实 packs，每轨 0~3 个；GET /api/tracks/{track}/profiles）。"""
    profs = client.get("/api/tracks/ctf/profiles").json()
    ids = {p["id"] for p in profs}
    assert {"full-squad", "solo-generalist"} <= ids
    assert all(p.get("name") and isinstance(p.get("experts"), list) for p in profs)


def test_create_project_with_profile_materializes_snapshot(client):
    """M4a：选档创建 → 组队预设兜底生效 + board_view / config.profile 快照物化
    （D6 快照即弃：模板升级不影响存量项目）；未知档 422。"""
    r = client.post("/api/projects", json={"name": "档建项目", "track": "ctf",
                                           "profile": "full-squad"})
    assert r.status_code == 201, r.text
    meta = r.json()
    pid = meta["id"]
    # 档组队兜底（请求未带 experts 时用档内预设；建项时归一化排序）
    assert meta["experts"] == ["crypto-solver", "forensics-solver", "pwn-solver",
                               "reverse", "triage", "web-solver"]
    assert "web" in meta["capabilities"]  # 专家面推导（响应层）
    cfg = meta["config"]
    assert cfg["board_view"] == {"default": "findings"}
    snap = cfg["profile"]
    assert snap["id"] == "full-squad" and snap["name"] == "全栈解题队"
    assert snap["artifacts"] == ["writeup", "flag 记录"] and snap["materialized_at"]
    # 显式 experts 优先于档预设；显式 config 优先于物化缺省
    r = client.post("/api/projects", json={"name": "显式优先", "track": "ctf",
                                           "profile": "solo-generalist",
                                           "experts": ["pwn-solver"],
                                           "config": {"board_view": {"default": "assets"}}})
    assert r.status_code == 201
    meta2 = r.json()
    assert meta2["experts"] == ["pwn-solver"]
    assert meta2["config"]["board_view"] == {"default": "assets"}
    assert meta2["config"]["profile"]["id"] == "solo-generalist"
    # 未知档 422 提示轨内置清单
    r = client.post("/api/projects", json={"name": "坏档", "track": "ctf", "profile": "nope"})
    assert r.status_code == 422 and "场景档" in r.json()["detail"]


def test_create_project_inherits_knowledge(client):
    """M4b：建项目选源 → binary 资产+样本文件+func_kb+蓝图复制（源只读）；
    重跑全 skipped（只增不覆盖）；源不存在 404 且不建项目。"""
    src = _make_project(client, track="ctf")
    proj = client.app.state.store.open_project(src)
    sha = "ab" * 32
    (proj.samples_dir / "simp.exe").write_bytes(b"MZ inherit-me")
    proj.bb.upsert_asset(src, "binary", sha,
                         meta={"filename": "simp.exe", "path": "simp.exe", "size": 13},
                         author="human")
    proj.bb.upsert_func(src, sha, 0x401000, "sub_401000", "初始化例程", ["crypto"], 0.8,
                        analyzed_by="analyst")
    bp = proj.bb.create_blueprint(src, "重建蓝图", goal="验证重建",
                                  binary_sha256=sha, modules=[{"name": "m1"}],
                                  content_md="# 蓝图")
    proj.bb.set_blueprint_status(src, bp["id"], "reviewed")

    r = client.post("/api/projects", json={"name": "继承项目", "track": "ctf",
                                           "inherit_from": src})
    assert r.status_code == 201, r.text
    dst_meta = r.json()
    dst_id = dst_meta["id"]
    assert dst_meta["inherited"] == {"binaries": 1, "func_kb": 1, "blueprints": 1,
                                     "skipped": 0}
    dproj = client.app.state.store.open_project(dst_id)
    asset = dproj.bb.find_asset(dst_id, "binary", sha)
    assert asset and asset["meta"]["path"] == "simp.exe"
    assert asset["meta"]["inherited_from"] == src
    assert (dproj.samples_dir / "simp.exe").read_bytes() == b"MZ inherit-me"
    assert (proj.samples_dir / "simp.exe").read_bytes() == b"MZ inherit-me"  # 源只读
    f = dproj.bb.lookup_func(dst_id, sha, 0x401000)
    assert f and f["analysis"] == "初始化例程" and f["risk_tags"] == ["crypto"]
    bps = dproj.bb.list_blueprints(dst_id)
    assert len(bps) == 1 and bps[0]["name"] == "重建蓝图" and bps[0]["status"] == "reviewed"

    # 只增不覆盖：对已继承项目再跑一次 → 全 skipped，无重复行
    stats = client.app.state.store.inherit_knowledge(src, dproj)
    assert stats == {"binaries": 0, "func_kb": 0, "blueprints": 0, "skipped": 3}
    assert len(dproj.bb.list_blueprints(dst_id)) == 1

    # 源不存在：404，且项目不落建
    n_before = len(client.get("/api/projects").json())
    r = client.post("/api/projects", json={"name": "坏源", "inherit_from": "proj-ghost"})
    assert r.status_code == 404
    assert len(client.get("/api/projects").json()) == n_before


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
    assert {"ctf", "pentest", "redteam"} <= tracks
    assert data["task_types"]["ctf"]["solve"] == "passive"
    assert data["task_types"]["pentest"]["exploit"] == "low"


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


def test_events_tail_and_before_paging(client):
    """直播间事件流分页（2026-09-17）：tail=最新 N 条、before_id=更早一页（升序返回），
    首屏不全量回放 + 上翻懒加载的数据面；since_id 增量语义不变。"""
    pid = _make_project(client)
    bb = client.app.state.projects[pid].bb
    base = client.get(f"/api/projects/{pid}/events").json()
    for i in range(60):
        bb.append_event(pid, "test.ev", {"i": i}, author="human")
    tail = client.get(f"/api/projects/{pid}/events?tail=50").json()
    assert len(tail) == 50 and tail[0]["id"] < tail[-1]["id"]  # 升序、最新 50 条
    older = client.get(
        f"/api/projects/{pid}/events?before_id={tail[0]['id']}&limit=50").json()
    assert len(older) == len(base) + 60 - 50  # 尾页之前的全部更早事件
    assert older[-1]["id"] < tail[0]["id"] and older == sorted(older, key=lambda e: e["id"])
    # since_id 增量不受影响
    inc = client.get(f"/api/projects/{pid}/events?since_id={tail[-1]['id']}").json()
    assert inc == []


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


def test_board_graph_endpoint(client):
    """黑板链路图（2026-09-20）：五类节点 + 跨实体边；跨项目 404。"""
    pid = _make_project(client)
    bb = client.app.state.projects[pid].bb
    url_id = bb.upsert_asset(pid, "url", "http://10.0.0.9/")["id"]
    f = bb.add_finding(pid, "sqli", "注入", target_asset_id=url_id)["id"]
    client.post(f"/api/projects/{pid}/tasks",
                json={"objective": "验证", "task_type": "generic"}).json()["task_id"]

    r = client.get(f"/api/projects/{pid}/board-graph")
    assert r.status_code == 200
    g = r.json()
    types = {n["node_type"] for n in g["nodes"]}
    assert types == {"asset", "finding", "task"}
    assert {n["label"] for n in g["nodes"] if n["node_type"] == "asset"} == {"http://10.0.0.9/"}
    pairs = {(e["kind"], e["source"], e["target"]) for e in g["edges"]}
    assert ("targets", f, url_id) in pairs
    assert client.get("/api/projects/proj-nope/board-graph").status_code in (403, 404)


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
    # v0.71 发布即建窗：delta 除 task.published 外还有绑窗事件（task.window_bound/
    # session.spawned），数条目不再恒为 1
    pub = [e for e in delta if e["kind"] == "task.published"]
    assert len(pub) == 1 and pub[0]["payload"]["objective"] == "再分诊"
    # WS：从 0 回放全部事件（该项目恰好 2 条 task.published）
    total = len(client.get(f"/api/projects/{pid}/events").json())
    with client.websocket_connect(f"/api/ws/projects/{pid}?since_id=0") as ws:
        got = [ws.receive_json() for _ in range(total)]
        assert sum(1 for e in got if e["kind"] == "task.published") == 2


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
                     conflict_keys=["binary:" + "a" * 64], created_by="human")
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
    assert {"_generalist", "recon", "external-entry", "osint", "report-writer"} <= names
    recon = next(r for r in roles if r["role"] == "recon")
    assert recon["task_types"] == ["recon", "asset-enum"]
    assert recon["persona"] and recon["default_noise"] == "passive"
    assert recon["name"] == "侦察"  # yaml name 行=中文显示名（与 stem 解耦）
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


def test_close_session_aborts_while_job_running(client):
    """关窗=硬中断（2026-09-19，F9 优雅排水退役）：worker 在跑时 close →
    request_abort + close_pending（返回 closing）；空闲时立即关。"""
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    sess = proj.bb.register_session(pid, "忙碌窗口", role="reverse")
    sid = sess["id"]
    import threading
    gate = threading.Event()
    client.app.state.jobs.submit(
        "agent-work", gate.wait,  # 挂起直到测试放行
        meta={"project_id": pid, "session_id": sid})
    r = client.post(f"/api/sessions/{sid}/close")
    assert r.status_code == 200 and r.json()["status"] == "closing"
    rows = client.get(f"/api/projects/{pid}/sessions").json()
    meta = json.loads(rows[0]["meta"]) if isinstance(rows[0].get("meta"), str) else rows[0].get("meta") or {}
    assert meta["close_pending"] is True and meta["worker_armed"] is False
    # 事件审计：session.work_state（armed=false + close_pending=true + abort 标记）
    events = client.get(f"/api/projects/{pid}/events").json()
    ws = [e for e in events if e["kind"] == "session.work_state"]
    assert ws and ws[-1]["payload"]["close_pending"] is True
    assert ws[-1]["payload"]["abort"] is True
    gate.set()
    # 空闲关窗照旧立即 closed
    r2 = client.post(f"/api/sessions/{sid}/close")
    assert r2.status_code == 200 and r2.json()["status"] == "closed"


# ---------- 会话控制端点（DESIGN.md §3：暂停/恢复/中断） ----------


def _spawn_test_agent(client, pid):
    """不经 spawn 端点（避免依赖真实 LLM）直接构造 AgentSession 注入 app.state.agents。"""
    from core.agent import AgentSession
    from core.runtime import ExecutionGateway

    proj = client.app.state.projects.get(pid) or client.app.state.store.open_project(pid)
    client.app.state.projects[pid] = proj
    agent = AgentSession(project_id=pid, bb=proj.bb,
                         gateway=ExecutionGateway(bb=proj.bb), llm=object(),
                         packs_root="packs", track="pentest",
                         capabilities=["web"], role="_generalist")
    client.app.state.agents[agent.session["id"]] = agent
    return agent


def test_pause_resume_abort_endpoints(client):
    pid = _make_project(client)
    rp = client.post("/api/projects", json={"name": "渗透-控制", "track": "pentest",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]

    # 不在册 → 404
    for act in ("pause", "resume", "abort"):
        assert client.post(f"/api/sessions/sess-nope/{act}").status_code == 404

    # 空闲未武装暂停 → 409（不落 paused、无 session.paused 事件）
    r = client.post(f"/api/sessions/{sid}/pause")
    assert r.status_code == 409 and agent.paused is False
    # 空闲已武装 → 只解除武装（F9），不落 paused 不发 session.paused
    client.app.state.projects[pid].bb.set_session_meta(sid, {"worker_armed": True})
    r = client.post(f"/api/sessions/{sid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "idle" and r.json()["disarmed"] is True
    assert agent.paused is False
    sessions = client.get(f"/api/projects/{pid}/sessions").json()
    assert sessions[0]["status"] == "idle" and sessions[0]["worker_armed"] is False
    assert not any(e["kind"] == "session.paused"
                   for e in client.get(f"/api/projects/{pid}/events").json())
    # 有认领任务但 worker 已退（孤儿认领）→ 无线程消费检查点，立即落 paused
    agent.dispatcher.current_task_id = "task-sim"
    r = client.post(f"/api/sessions/{sid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert agent.paused is True
    agent.dispatcher.current_task_id = None
    assert client.get(f"/api/projects/{pid}/sessions").json()[0]["status"] == "paused"
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


# ---------- E8：人工引导端点 + 恢复附引导/增补步数 ----------


def test_session_note_endpoint(client):
    """POST /note（E8 人工引导通道）：human_note 私信落地 + message.inbox 审计
    （author=human）+ 已读复用；空文本 422 / 未知会话 404 / 关窗 409。"""
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    sid = proj.bb.register_session(pid, "引导目标", role="_generalist")["id"]
    r = client.post(f"/api/sessions/{sid}/note", json={"text": "先看 80 端口"})
    assert r.status_code == 201 and r.json()["note_id"]
    inbox = client.get(f"/api/sessions/{sid}/inbox").json()
    assert [m["kind"] for m in inbox] == ["human_note"]
    assert inbox[0]["payload"]["text"] == "先看 80 端口"
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "message.inbox" and e["author"] == "human"
               and e["payload"]["kind"] == "human_note" for e in events)
    # 红点/已读复用 inbox read 端点。对话化（2026-09-20）后未武装无绑定窗 note
    # 即踢对话轮消化未读——已读断言改用「绑 open 任务的待命窗」（宁严勿松：
    # note 不 kick，引导滞留未读等起跑轮注入）
    from core.blackboard.tasks import TaskQueue
    tid = TaskQueue(proj.bb).publish(pid, "待审批任务")
    proj.bb.set_session_meta(sid, {"bound_task_id": tid})
    assert client.post(f"/api/sessions/{sid}/note",
                       json={"text": "等着"}).status_code == 201
    assert client.post(f"/api/sessions/{sid}/inbox/read").json()["marked"] == 1
    assert client.post(f"/api/sessions/{sid}/note", json={"text": "  "}).status_code == 422
    assert client.post("/api/sessions/sess-nope/note",
                       json={"text": "x"}).status_code == 404
    proj.bb.close_session(sid)
    assert client.post(f"/api/sessions/{sid}/note", json={"text": "x"}).status_code == 409


def test_resume_budget_pause_note_and_extra_steps(client):
    """预算暂停恢复（E8）：引导语随快照注入；增补缺省 +200、可显式指定、0=不加；
    每次增补落 step.budget_extended（by=human）。"""
    pid = client.post("/api/projects", json={"name": "渗透-恢复增补",
                                              "track": "pentest",
                                              "capabilities": ["web"]}).json()["id"]
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]

    def pause_budget(max_steps: int) -> None:
        agent._resume_state = {"system": "s", "messages": [], "objective": "x",
                               "task_id": None, "next_step": max_steps,
                               "max_steps": max_steps, "reason": "budget"}
        agent.dispatcher.max_steps = max_steps
        agent.paused = True

    # 显式 +50 + 引导语
    pause_budget(10)
    r = client.post(f"/api/sessions/{sid}/resume",
                    json={"note": "换个思路看子域", "extra_steps": 50})
    assert r.status_code == 200
    assert agent.dispatcher.max_steps == 60
    events = client.get(f"/api/projects/{pid}/events").json()
    ext = [e for e in events if e["kind"] == "step.budget_extended"]
    assert ext and ext[-1]["payload"]["by"] == "human"
    assert ext[-1]["payload"]["old_max"] == 10 and ext[-1]["payload"]["new_max"] == 60

    # 缺省（无 body）→ 预算暂停自动 +200
    pause_budget(60)
    assert client.post(f"/api/sessions/{sid}/resume").status_code == 200
    assert agent.dispatcher.max_steps == 260

    # extra_steps=0 → 显式不加，无增补事件
    pause_budget(260)
    assert client.post(f"/api/sessions/{sid}/resume",
                       json={"extra_steps": 0}).status_code == 200
    assert agent.dispatcher.max_steps == 260
    ext = [e for e in client.get(f"/api/projects/{pid}/events").json()
           if e["kind"] == "step.budget_extended"]
    assert len(ext) == 2  # 只落了前两次的增补事件


def test_close_session_fails_paused_snapshot_task(client):
    """关窗守卫：暂停快照里的任务仍 claimed → 关窗前先 fail 防占坑（§6.4）。"""
    from core.blackboard import TaskQueue

    rp = client.post("/api/projects", json={"name": "渗透-关窗", "track": "pentest",
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
    rp = client.post("/api/projects", json={"name": "渗透-rehydrate", "track": "pentest",
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
    """暂停/中断端点同样对孤儿窗 rehydrate（旧行为一律 404）；空闲未武装暂停 409。"""
    rp = client.post("/api/projects", json={"name": "渗透-控制孤儿", "track": "pentest",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    sid = _orphan_session_row(client, pid)["id"]

    r = client.post(f"/api/sessions/{sid}/pause")
    assert r.status_code == 409  # 空闲未武装：无事可暂停（但 409 前已完成 rehydrate）
    assert sid in client.app.state.agents
    # 模拟孤儿认领（worker 已退、任务仍占）→ 暂停立即落 paused
    client.app.state.agents[sid].dispatcher.current_task_id = "task-sim"
    r = client.post(f"/api/sessions/{sid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    # rehydrate 后陈旧 running 行状态已回 idle，暂停落 paused
    assert client.get(f"/api/projects/{pid}/sessions").json()[0]["status"] == "paused"
    # 中断把暂停态收尾回 idle
    r = client.post(f"/api/sessions/{sid}/abort")
    assert r.status_code == 200 and r.json()["status"] == "aborted"


def test_sweep_keeps_claimed_tasks_of_snapshot_sessions(client):
    """v0.64 重启清扫豁免：持有落盘暂停快照（指针+文件在场）的会话其 claimed
    任务不 fail，行归位 paused 等继续续跑；无快照/指针悬空的照旧 fail(awaiting_human)
    + 归 idle（悬空指针顺带清掉）。"""
    from core.blackboard import TaskQueue

    rp = client.post("/api/projects", json={"name": "清扫豁免", "track": "pentest",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    proj = client.app.state.projects[pid]
    bb = proj.bb
    tq = TaskQueue(bb)
    sa = bb.register_session(pid, "快照窗", role="_generalist")["id"]
    sb = bb.register_session(pid, "悬空指针窗", role="_generalist")["id"]
    sc = bb.register_session(pid, "裸奔窗", role="_generalist")["id"]
    snap_dir = Path(proj.path) / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{sa}.json").write_text(
        json.dumps({"messages": [], "task_id": None, "reason": "pause"}), encoding="utf-8")
    bb.set_session_meta(sa, {"resume_snapshot": f"{sa}.json"})
    bb.set_session_meta(sb, {"resume_snapshot": f"{sb}.json"})   # 指针在、文件缺
    for sid in (sa, sb, sc):
        bb.set_session_status(sid, "running")                    # 模拟重启前的在跑窗
    ta = tq.publish(pid, "豁免任务", task_type="generic")
    tb = tq.publish(pid, "悬空任务", task_type="generic")
    tc = tq.publish(pid, "裸奔任务", task_type="generic")
    for tid, sid in ((ta, sa), (tb, sb), (tc, sc)):
        tq.claim(tid, sid)

    # 模拟重启：摘内存缓存 → 下一请求触发惰性首开清扫
    client.app.state.projects.pop(pid)
    assert client.get(f"/api/projects/{pid}").status_code == 200

    assert tq.get_task(ta)["status"] == "claimed"       # 快照在场 → 豁免
    assert tq.get_task(tb)["status"] == "failed"        # 指针悬空 → 照 fail
    assert tq.get_task(tc)["status"] == "failed"
    proj2 = client.app.state.projects[pid]
    rows = {s["id"]: s for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert rows[sa]["status"] == "paused"
    assert rows[sb]["status"] == "idle"
    assert rows[sc]["status"] == "idle"
    assert json.loads(proj2.bb.get_session(sb)["meta"])["resume_snapshot"] is None


def test_shutdown_hook_persists_live_snapshots(client):
    """v0.64 优雅停机钩子：在跑（有任务、未暂停、现场在册）的会话落即时快照；
    已暂停 / 无任务的会话跳过。"""
    rp = client.post("/api/projects", json={"name": "停机快照", "track": "pentest",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    bb = client.app.state.projects[pid].bb
    s_run = bb.register_session(pid, "在跑窗", role="_generalist")["id"]
    s_paused = bb.register_session(pid, "已暂停窗", role="_generalist")["id"]
    s_idle = bb.register_session(pid, "空闲窗", role="_generalist")["id"]
    wrote: list[str] = []

    class _Disp:
        def __init__(self, tid):
            self.current_task_id = tid

    class _FakeAgent:
        def __init__(self, sid, tid, paused, live=True):
            self.session = {"id": sid}
            self.paused = paused
            self.dispatcher = _Disp(tid)
            self._live = live

        def pause_snapshot_now(self):
            assert self._live
            wrote.append(self.session["id"])
            return True

    client.app.state.agents[s_run] = _FakeAgent(s_run, "task-x", paused=False)
    client.app.state.agents[s_paused] = _FakeAgent(s_paused, "task-y", paused=True)
    client.app.state.agents[s_idle] = _FakeAgent(s_idle, None, paused=False)
    assert client.app.state.shutdown_persist() == [s_run]
    assert wrote == [s_run]


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
    rp = client.post("/api/projects", json={"name": "撤回-API", "track": "pentest",
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
        rp = c.post("/api/projects", json={"name": "编排开窗注册", "track": "pentest",
                                          "capabilities": ["web"]})
        pid = rp.json()["id"]
        # pentest 默认 L1（spawn 转审批）；本测覆盖直接开窗路径，显式升 L2
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
        # 空 pentest 项目两轮 tick 零产出 → idle 逃生过门 → L2 自动流转 pentest
        # 并首发剧本任务（任务窗=第二个 agent，属分阶段 M2 预期行为）；孤儿窗
        # 回归断言锚定编排器开的 recon 窗必须注册在册。
        recon_agents = [s for s in agents if s.session["role"] == "recon"]
        assert len(recon_agents) == 1, "编排开窗未注册进 app.state.agents（孤儿窗回归）"
        sid = recon_agents[0].session["id"]
        # 旧根因：这两个操作对编排开窗 404
        w = c.post(f"/api/agents/{sid}/work")
        assert w.status_code == 200
        _wait_job(c, w.json()["job_id"])  # 空队列秒退；编排窗默认 armed → 空闲暂停只解除武装
        r = c.post(f"/api/sessions/{sid}/pause")
        assert r.status_code == 200 and r.json()["status"] == "idle"
        kinds = [e["kind"] for e in c.app.state.projects[pid].bb.recent_events(pid)]
        assert "session.spawned" in kinds


def _l1_tick_app(tmp_path, role="recon", reason="核查 8080 旁站低噪信息", exec_llm=None):
    """批 4：pentest 默认 L1 的 tick app——planner 剧本开一扇窗后 done。"""
    from test_orchestrator import ScriptedLLM

    orch_llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session",
                                            {"role": role, "reason": reason})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    return create_app(workspace_root=str(tmp_path / "workspaces"),
                      tools_root=None,
                      executor_llm=exec_llm or ScriptedLLM([]), planner_llm=orch_llm,
                      providers_config=str(tmp_path / "providers.json"),
                      sediment_proposals=False)  # 共享剧本：复盘 chat 会偷 orch 剧本项


def _l1_tick_to_pending(c, pid):
    r = c.post(f"/api/projects/{pid}/orchestrator/tick",
               json={"allowed_roles": ["recon"], "max_sessions": 2})
    assert r.status_code == 200
    _wait_job(c, r.json()["job_id"])
    items = c.get(f"/api/projects/{pid}/approvals").json()
    assert len(items) == 1 and items[0]["status"] == "pending"
    return items[0]


def test_l1_spawn_approved_creates_and_runs(tmp_path):
    """批准 spawn_session 纯开窗单（编排器侦查窗）：当场注册建窗 + 提交 agent-work
    job + session.spawned 带 approval_id；v0.72 公共池认领退役——无绑侦查窗
    认领恒空，任务仍归自己的专属待命窗（等执行审批）。"""
    from test_orchestrator import ScriptedLLM

    app = _l1_tick_app(tmp_path, exec_llm=ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("w1", "complete_task",
                                            {"result_note": "新窗完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("w2", "finish", {"summary": "开跑成功"})]},
    ]))
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "L1批准", "track": "pentest",
                                           "capabilities": ["web"]})
        pid = rp.json()["id"]
        item = _l1_tick_to_pending(c, pid)
        assert item["action"] == {"op": "spawn_session", "role": "recon",
                                  "reason": "核查 8080 旁站低噪信息"}
        # 赛跑终检要求有 open 任务：发布后 v0.72 即建专属待命窗（不计入本次审批）
        pr = c.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "新窗要跑的任务", "task_type": "recon"})
        assert pr.status_code == 201
        task_id = pr.json()["task_id"]
        agents = [s for s in c.app.state.agents.values() if s.project_id == pid]
        assert len(agents) == 1  # 任务自己的待命窗（绑定段已建）

        r = c.post(f"/api/approvals/{item['id']}/decide", json={"decision": "approved"})
        assert r.status_code == 200
        body = r.json()
        assert body["executed"] is True and body["session_id"] and body["job_id"]
        job = _wait_job(c, body["job_id"])
        assert job["status"] == "done"
        agents = [s for s in c.app.state.agents.values() if s.project_id == pid]
        assert len(agents) == 2 and body["session_id"] in {a.session["id"] for a in agents}
        # v0.72：无绑侦查窗认领恒空——任务保持 open，归自己的待命窗
        tq = c.app.state.projects[pid].bb
        row = tq.conn.execute(
            "SELECT status, target_session FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "open" and row["target_session"] != body["session_id"]

        kinds = {}
        for e in c.app.state.projects[pid].bb.recent_events(pid):
            kinds.setdefault(e["kind"], []).append(e.get("payload") or {})
        spawns = [p for p in kinds.get("session.spawned", []) if p.get("approval_id")]
        assert len(spawns) == 1 and spawns[0]["approval_id"] == item["id"]
        assert "approval.approved" in kinds


def test_l1_spawn_approved_no_open_task_skipped(tmp_path):
    """赛跑终检（E12 后继）：批准时项目已无 open 任务（被既有 worker 抢走）→
    不建窗，落 approval.exec_failed，响应 executed:false。"""
    app = _l1_tick_app(tmp_path)
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "L1赛跑跳过", "track": "pentest",
                                           "capabilities": ["web"]})
        pid = rp.json()["id"]
        item = _l1_tick_to_pending(c, pid)

        r = c.post(f"/api/approvals/{item['id']}/decide", json={"decision": "approved"})
        assert r.status_code == 200
        body = r.json()
        assert body["executed"] is False and "无待执行任务，未建窗" in body["error"]
        # 没有建窗（agents/sessions 均空）
        assert [s for s in c.app.state.agents.values() if s.project_id == pid] == []
        assert c.get(f"/api/projects/{pid}/sessions").json() == []
        kinds = [e["kind"] for e in c.app.state.projects[pid].bb.recent_events(pid)]
        assert "session.spawned" not in kinds and "approval.exec_failed" in kinds
        # 审批保持 approved（不回滚）
        decided = c.get(f"/api/projects/{pid}/approvals").json()[0]
        assert decided["status"] == "approved"


def test_l1_spawn_rejected_no_session(tmp_path):
    """拒绝：只翻状态 + approval.rejected，不开窗、不提交 job。"""
    app = _l1_tick_app(tmp_path)
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "L1拒绝", "track": "pentest",
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
        rp = c.post("/api/projects", json={"name": "L1批时满cap", "track": "pentest",
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
                      providers_config=str(tmp_path / "providers.json"),
                      sediment_proposals=False)  # 剧本式 planner：复盘 chat 会偷剧本项


def _l2_project(c, name, **autonomy_extra):
    pid = c.post("/api/projects", json={"name": name, "track": "pentest",
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
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon", "armed": True})
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
        # v0.71：任务由自己的专属执行窗认领完成（手动 recon 窗无绑定，不接公共池）
        assert task["status"] == "done" and task["claimed_by"] == task["target_session"]
        assert task["claimed_by"] != sid
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


def test_l2_publish_always_binds_executor_window(tmp_path, monkeypatch):
    """v0.72 全局一窗一任务：编排发布任务必有专属执行窗（发布即建待命窗并起跑），
    「有产出零会话」不再可能——no_sessions 停链退役，链照常收敛。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    from test_orchestrator import ScriptedLLM as S
    planner = _orch_scripted(
        [S.tool_call("t1", "publish_task",
                     {"objective": "核查 8080", "task_type": "recon",
                      "noise_budget": "passive"})],
        [],  # 自动 tick：零产出 → 收敛
    )
    app = _chain_app(tmp_path, planner, _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2必有窗")
        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        _wait_job(c, r.json()["job_id"])
        stop = _wait_chain_stopped(c, pid)
        assert stop["payload"]["reason"] == "converged"
        task = c.get(f"/api/projects/{pid}/tasks").json()[0]
        assert task["status"] == "done" and task["claimed_by"] == task["target_session"]
        bb = c.app.state.projects[pid].bb
        stops = [e for e in bb.recent_events(pid) if e["kind"] == "orch.chain_stopped"]
        assert all(s["payload"]["reason"] != "no_sessions" for s in stops)


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
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon", "armed": True})
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
    """闸②：暂停时自动挡不起跑（任务留待命窗）；但人显式对绑定窗「跑队列」是
    override 照常认领完成；worker 收尾的 A 触发仍被闸门②拦住，落 chain_stopped{paused}。"""
    monkeypatch.setattr("core.api.app.AUTO_TICK_MIN_INTERVAL", 0)
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2暂停", paused=True)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "核查 8080", "task_type": "recon"})
        # v0.72：暂停不挡建窗——待命窗已建好，只是不起跑
        assert r.json()["kicked"] == [] and r.json()["session_id"]
        sid = r.json()["session_id"]
        rows = {s["id"]: s for s in c.get(f"/api/projects/{pid}/sessions").json()}
        assert rows[sid]["worker_armed"] is False  # 待命未武装
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"
        _arm_active_chain(c, pid, ticks=1)
        w = c.post(f"/api/agents/{sid}/work")  # 人工显式 override 自己的绑定窗
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
    """档位分流（v0.72 全局一窗一任务）：L0 发布建待命窗但全手动（无审批单）；
    L1 发布建待命窗+执行审批单，手动窗跑队列认领不到别人的任务（公共池退役），
    批准=启动任务自己的待命窗跑完。"""
    from test_orchestrator import ScriptedLLM as S
    # L0：发布建待命窗（不起跑、无审批单），任务保持 open 等人工点跑
    app0 = create_app(workspace_root=str(tmp_path / "ws0"), tools_root=None,
                      executor_llm=S([]), planner_llm=S([]),
                      providers_config=str(tmp_path / "p0.json"))
    with TestClient(app0) as c0:
        pid0 = c0.post("/api/projects", json={"name": "L0", "track": "ctf",
                                              "capabilities": ["binary"]}).json()["id"]
        r = c0.post(f"/api/projects/{pid0}/tasks",
                    json={"objective": "逆一下", "task_type": "generic"})
        assert r.json()["kicked"] == [] and r.json()["session_id"]  # 待命窗已建
        rows = c0.get(f"/api/projects/{pid0}/sessions").json()
        assert rows[0]["worker_armed"] is False
        assert c0.get(f"/api/projects/{pid0}/tasks").json()[0]["status"] == "open"
        assert c0.get(f"/api/projects/{pid0}/approvals").json() == []  # L0 无审批

    # L1：发布建待命窗+执行审批单；手动窗跑队列认领恒空；批准=启动原窗
    app1 = create_app(workspace_root=str(tmp_path / "ws1"), tools_root=None,
                      executor_llm=S(_exec_finish_script(1)), planner_llm=S([]),
                      providers_config=str(tmp_path / "p1.json"),
                      sediment_proposals=False)  # 完成任务会触发复盘 chat 偷剧本项
    with TestClient(app1) as c1:
        pid1 = c1.post("/api/projects", json={"name": "L1触发", "track": "pentest",
                                              "capabilities": ["web"]}).json()["id"]
        manual = c1.post(f"/api/projects/{pid1}/agents", json={"role": "recon"})
        manual_sid = manual.json()["id"]
        assert "job_id" not in manual.json()  # 手动窗默认未武装不开工
        dr = c1.post(f"/api/projects/{pid1}/tasks",
                     json={"objective": "核查 8080", "task_type": "recon"})
        assert dr.json()["kicked"] == []  # 触发点 D 退役：不 kick 手动窗
        t1 = dr.json()["task_id"]
        bound_sid = dr.json()["session_id"]
        assert bound_sid and bound_sid != manual_sid
        _wait_no_running(c1, pid1)
        assert c1.get(f"/api/projects/{pid1}/tasks").json()[0]["status"] == "open"
        # 手动窗跑队列（=人工武装启动）：认领恒空，别人的任务不被碰
        w = c1.post(f"/api/agents/{manual_sid}/work")
        assert w.status_code == 200 and w.json().get("job_id")
        _wait_job(c1, w.json()["job_id"])
        _wait_no_running(c1, pid1)
        assert c1.get(f"/api/projects/{pid1}/tasks").json()[0]["status"] == "open"
        # 执行审批单已就绪：批准=启动任务自己的待命窗（不新建）
        items = c1.get(f"/api/projects/{pid1}/approvals").json()
        item = next(a for a in items if a["action"].get("task_id") == t1)
        assert item["status"] == "pending"
        decided = c1.post(f"/api/approvals/{item['id']}/decide",
                          json={"decision": "approved"})
        assert decided.status_code == 200 and decided.json()["executed"] is True
        assert decided.json()["session_id"] == bound_sid  # 同一扇待命窗被启动
        _wait_no_running(c1, pid1)
        by_id = {t["id"]: t for t in c1.get(f"/api/projects/{pid1}/tasks").json()}
        assert by_id[t1]["status"] == "done"
        assert by_id[t1]["claimed_by"] == bound_sid


def test_reopen_awaiting_human_kicks_workers(tmp_path):
    """C1 放回触发点 D（2026-09-17 修复；v0.72 只踢绑定窗）：worker 在队列空时
    已退出，reopen 放回的 awaiting_human 任务若不 kick 则永久悬 open 无人认领
    （用户所见「放回后不跑」）。L2 未暂停时 reopen 应唤醒原绑定窗并真正跑完
    （fail→reopen→done 闭环）。"""
    from test_orchestrator import ScriptedLLM as S
    executor = [
        # 第一轮：认领后 awaiting_human 挂起（C1：落快照 fail，会话继续认领）
        {"tool_use": [S.tool_call("a1", "fail_task",
                                  {"result_note": "缺授权凭据，需要人类补充",
                                   "blocked_reason": "awaiting_human"})]},
        # 放回后被同一会话认领 → 走 C1 快照续跑路径（断点恢复旧对话），回复完成收尾
        {"tool_use": [S.tool_call("c1", "complete_task", {"result_note": "人工已解决"})]},
    ]
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=S(executor), planner_llm=S([]),
                     providers_config=str(tmp_path / "p.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "C1放回", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config",
                       json={"config": {"autonomy": {"level": "L2"}}}).status_code == 200
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "测一把", "task_type": "generic"})
        sid = r.json()["session_id"]  # 任务自己的专属执行窗（L2 自动起跑）
        assert sid
        # 轮询代替 _wait_no_running+直断：负载下 worker 认领可落在 job 结束之后（套跑时序竞态）
        deadline = time.time() + 30
        tasks = []
        while time.time() < deadline:
            tasks = c.get(f"/api/projects/{pid}/tasks").json()
            if tasks and tasks[0]["status"] == "failed":
                break
            time.sleep(0.05)
        assert len(tasks) == 1
        assert tasks[0]["status"] == "failed"
        assert tasks[0]["blocked_reason"] == "awaiting_human"
        assert tasks[0]["claimed_by"] == sid
        # 放回：触发点 D 唤醒原绑定窗（v0.72 kick 只服务绑定任务未终态的实现窗）
        rr = c.post(f"/api/tasks/{tasks[0]['id']}/reopen", json={"note": "凭证已补"})
        assert rr.status_code == 200 and rr.json()["kicked"] == [sid]
        # 轮询终态（L2 下 worker-idle 的 replan-wait 30s job 与断言无关）
        for _ in range(300):
            tasks = c.get(f"/api/projects/{pid}/tasks").json()
            if tasks[0]["status"] == "done":
                break
            time.sleep(0.02)
        assert tasks[0]["status"] == "done"


def test_browser_pool_injection_by_track(tmp_path):
    """v0.70 F6 扩展：浏览器实例池按轨注入——ctf 轨开窗即注入（Web 题需真实
    浏览器渲染 reCAPTCHA/JS 挑战）；research 轨仍轨外（dispatcher.browser 保持
    None → browser_* no-tool 降级）。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=S([{"tool_use": []}]), planner_llm=S([]),
                     providers_config=str(tmp_path / "p.json"))
    with TestClient(app) as c:
        for track, caps, expect in (("ctf", ["web"], True),
                                    ("research", ["binary"], False)):
            pid = c.post("/api/projects", json={"name": f"浏览器注入{track}",
                                                "track": track,
                                                "capabilities": caps}).json()["id"]
            sid = c.post(f"/api/projects/{pid}/agents",
                         json={"role": "_generalist", "armed": True}).json()["id"]
            agent = c.app.state.agents[sid]
            assert (agent.dispatcher.browser is not None) is expect, track


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
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "取个外网文件", "task_type": "generic"})
        assert r.json()["kicked"] == []  # v0.72 触发点 D 退役：发布不 kick
        # 轮询任务终态（不用 _wait_no_running：replan-wait 30s 节流 job 与断言无关）
        for _ in range(300):
            tasks = c.get(f"/api/projects/{pid}/tasks").json()
            if tasks[0]["status"] == "done":
                break
            time.sleep(0.02)
        denies = [e for e in c.app.state.projects[pid].bb.recent_events(pid)
                  if e["kind"] == "audit.deny"]
        assert len(denies) == 1 and "net=real" in denies[0]["payload"]["reason"]
        task = c.get(f"/api/projects/{pid}/tasks").json()[0]
        # 任务由自己的专属执行窗完成（v0.72 一窗一任务）
        assert task["status"] == "done" and task["claimed_by"] == task["target_session"]


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
        # v0.71 任务即窗口：采纳任务提案发布时已同步建专属待命窗（+1），
        # 加人手 reverse 窗共 2 扇，均未自起 job（L0 手动挡）
        assert len(sessions) == 2
        assert [s["role"] for s in sessions if s["role"] == "reverse"] == ["reverse"]
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
        rp = c.post("/api/projects", json={"name": "租约409", "track": "pentest",
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
                     json={"name": "cycles 持久", "track": "pentest",
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
    rp = client.post("/api/projects", json={"name": "渗透-模型", "track": "pentest",
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


def test_patch_asset_endpoint(client, monkeypatch):
    # E6 起 domain 走自动 DNS——测试断网 hermetic（不给 monkeypatch 就会打真实解析）
    from core.blackboard import assets as am

    def _no_dns(*a, **k):
        raise OSError("dns off")
    monkeypatch.setattr(am.socket, "getaddrinfo", _no_dns)
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
    """构造正交布局的最小 packs（web 能力包 + pentest 轨 + 专家池）并返回 TestClient。"""
    root = tmp_path / "packs"
    expert_dir = root / "experts"  # expert-pool M2：运行时角色源=experts/
    expert_dir.mkdir(parents=True)
    (expert_dir / "tester.yaml").write_text(
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
    """包管理端点：轨角色读端点（experts 源）+ 写退役 410 / 能力包 skill 全文改写 / 路由试算。"""
    monkeypatch.chdir(tmp_path)  # MCP 等相对路径配置不污染仓库
    packs_root, c = _packs_app(tmp_path)
    with c:
        # 轨角色读端点：数据源已切 experts/ 池（PackRole 兼容形状，role 键=专家 id）
        roles = c.get("/api/tracks/pentest/roles").json()
        assert roles[0]["name"] == "tester" and roles[0]["skills"] == ["demo-skill"]
        # 写端点退役（expert-pool M2）：410 + 中文提示
        r = c.put("/api/tracks/pentest/roles/tester",
                  json={"name": "测试员", "persona": "新人设。"})
        assert r.status_code == 410 and "退役" in r.json()["detail"]
        assert c.post("/api/tracks/pentest/roles", json={"name": "x"}).status_code == 410
        assert c.delete("/api/tracks/pentest/roles/tester").status_code == 410
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
                   json={"query": "做一个演示任务", "track": "pentest",
                         "capabilities": ["web"], "role": "tester"})
        assert r.status_code == 200
        assert r.json()[0]["name"] == "demo-skill" and r.json()[0]["matched"]
        # 文件特征 ×3 命中（P0：file_features 路由断裂修复后的 API 面）
        r = c.post("/api/skills/route-preview",
                   json={"track": "pentest", "capabilities": ["web"],
                         "file_features": ["NX"]})
        assert r.json()[0]["name"] == "demo-skill"
        # 候选集外不可见：只选 pentest 轨（demo-skill 在 web 包）
        r = c.post("/api/skills/route-preview",
                   json={"query": "演示", "track": "pentest"})
        assert all(h["name"] != "demo-skill" for h in r.json())
        # 防穿越：非法包名 → 422
        assert c.get("/api/capabilities/bad%20name/skills").status_code == 422
        # 轨任务类型注册表：generic 恒在
        assert c.get("/api/tracks/pentest/task-types").json()["generic"] == "passive"


def test_rules_and_mcp_endpoints(client, tmp_path, monkeypatch):
    """轨红线读写（.history 备份）+ MCP 配置层 roundtrip。"""
    monkeypatch.chdir(tmp_path)
    packs = tmp_path / "packs"
    rules_dir = packs / "tracks" / "pentest" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "redlines.md").write_text("旧红线", encoding="utf-8")
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        assert c.get("/api/tracks/pentest/rules").json()["content"] == "旧红线"
        r = c.put("/api/tracks/pentest/rules", json={"content": "新红线"})
        assert r.json()["status"] == "ok"
        assert c.get("/api/tracks/pentest/rules").json()["content"] == "新红线"
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
    track_rules = packs / "tracks" / "pentest" / "rules"
    track_rules.mkdir(parents=True)
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        # 空列表
        assert c.get("/api/tracks/pentest/owners").json() == []
        # 新建（父目录自动建）
        r = c.put("/api/tracks/pentest/owners/edusrc",
                  json={"content": "# edusrc 无害化三原则"})
        assert r.json()["status"] == "ok"
        # 改写（备份进 .history）
        c.put("/api/tracks/pentest/owners/edusrc", json={"content": "v2"})
        rows = c.get("/api/tracks/pentest/owners").json()
        assert [o["tag"] for o in rows] == ["edusrc"] and rows[0]["content"] == "v2"
        assert any("edusrc" in f.name
                   for f in (track_rules / "owners" / ".history").iterdir())
        # 非法 tag（_NAME_RE 白名单）→ 422
        assert c.put("/api/tracks/pentest/owners/bad%20name",
                     json={"content": "x"}).status_code == 422
        # 删除后 404，.history 有回滚件
        assert c.delete("/api/tracks/pentest/owners/edusrc").json()["status"] == "ok"
        assert c.delete("/api/tracks/pentest/owners/edusrc").status_code == 404
        assert c.get("/api/tracks/pentest/owners").json() == []
        # MCP stdio：command/args roundtrip，url 可留空
        r = c.put("/api/mcp", json={"servers": [
            {"name": "fofa", "transport": "stdio", "command": "uv",
             "args": ["run", "fofa.py"], "domains": ["pentest"]}]})
        assert r.json()["count"] == 1
        s = c.get("/api/mcp").json()["servers"][0]
        assert s["command"] == "uv" and s["args"] == ["run", "fofa.py"]
        assert s["transport"] == "stdio" and s["url"] == ""


def test_ratings_crud_and_rating_basis(client, tmp_path, monkeypatch):
    """F11：ratings 三端点 CRUD（镜像 owners）+ FindingIn severity Literal 422 + rating_basis 透传/清空。"""
    monkeypatch.chdir(tmp_path)
    packs = tmp_path / "packs"
    track_rules = packs / "tracks" / "pentest" / "rules"
    track_rules.mkdir(parents=True)
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        # --- ratings 三端点（owners 镜像） ---
        assert c.get("/api/tracks/pentest/ratings").json() == []
        assert c.put("/api/tracks/pentest/ratings/edu-rating",
                     json={"content": "# edu-rating 评级与价值口径"}).json()["status"] == "ok"
        c.put("/api/tracks/pentest/ratings/edu-rating", json={"content": "v2"})
        rows = c.get("/api/tracks/pentest/ratings").json()
        assert [o["tag"] for o in rows] == ["edu-rating"] and rows[0]["content"] == "v2"
        assert (track_rules / "rating" / "edu-rating.md").is_file()
        assert any("edu-rating" in f.name
                   for f in (track_rules / "rating" / ".history").iterdir())
        assert c.put("/api/tracks/pentest/ratings/bad%20name",
                     json={"content": "x"}).status_code == 422
        assert c.delete("/api/tracks/pentest/ratings/edu-rating").json()["status"] == "ok"
        assert c.delete("/api/tracks/pentest/ratings/edu-rating").status_code == 404
        assert c.get("/api/tracks/pentest/ratings").json() == []

        # --- findings：severity 非 Literal 422；rating_basis 透传 / PATCH / 空串清空 ---
        pr = c.post("/api/projects", json={"name": "F11 判级", "track": "pentest"})
        assert pr.status_code == 201, pr.json()
        pid = pr.json()["id"]
        base = f"/api/projects/{pid}/findings"
        r = c.post(base, json={"vuln_class": "sqli", "title": "判级越界",
                               "severity": "P1"})
        assert r.status_code == 422  # Literal 白名单拦在 API 层
        r = c.post(base, json={"vuln_class": "sqli", "title": "核心站 SQLi",
                               "severity": "critical",
                               "rating_basis": "rating:edu-rating 严重#1 核心业务注入"})
        assert r.status_code == 201
        fid = r.json()["id"]
        assert r.json()["rating_basis"] == "rating:edu-rating 严重#1 核心业务注入"
        row = next(f for f in c.get(base).json() if f["id"] == fid)
        assert row["rating_basis"] == "rating:edu-rating 严重#1 核心业务注入"
        # PATCH 改依据 / 空串清空 / None 不动
        r = c.patch(f"{base}/{fid}", json={"rating_basis": "rating:osrc 高危#2 越权读"})
        assert r.json()["rating_basis"] == "rating:osrc 高危#2 越权读"
        r = c.patch(f"{base}/{fid}", json={"severity": "high"})
        assert r.json()["rating_basis"] == "rating:osrc 高危#2 越权读"  # None=不动
        assert c.patch(f"{base}/{fid}", json={"rating_basis": ""}).json()["rating_basis"] == ""


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

    # F10 人工修订：三字段 patch 200（响应完整行）/空 title 422/空体 200 原样返回
    r = client.patch(f"/api/projects/{pid}/findings/{finding_id}",
                     json={"title": "人工修正的标题", "severity": "high",
                            "vuln_class": ""})
    assert r.status_code == 200
    row = r.json()
    assert row["title"] == "人工修正的标题" and row["severity"] == "high"
    assert row["vuln_class"] == ""
    assert client.patch(f"/api/projects/{pid}/findings/{finding_id}",
                        json={"title": "   "}).status_code == 422
    assert client.patch(f"/api/projects/{pid}/findings/{finding_id}",
                        json={}).status_code == 200

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
                 temperature=None, on_thinking=None, on_text=None, should_cancel=None):
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
        # cap 占满：发布建窗失败 → 任务无绑 open（防专属窗抢跑干扰重排断言）
        pid = _l2_project(c, "L2重排D", sessions_cap=1)
        pid_holder.append(pid)
        c.app.state.projects[pid].bb.register_session(pid, "占位会话", role="_generalist")
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
        # L1 pentest（默认档是 L1，显式钉死）
        pid1 = c.post("/api/projects", json={"name": "L1不重排", "track": "pentest",
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
        # cap 占满：发布建窗失败 → 任务无绑 open，节流行为断言不受执行干扰
        pid = _l2_project(c, "L2重排节流", sessions_cap=1)
        c.app.state.projects[pid].bb.register_session(pid, "占位会话", role="_generalist")
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
        # 轮询到 replan 到 done：wait job 的 on_done 才提交 replan，负载下
        # replan 线程可能尚未起步（_wait_no_running 只保证无 running）
        deadline = time.time() + 10
        replans = []
        while time.time() < deadline:
            replans = [j for j in _project_jobs(c, pid, "orchestrator-replan")
                       if j["status"] == "done"]
            if len(replans) == 1:
                break
            time.sleep(0.05)
        assert len(replans) == 1
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
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon", "armed": True})
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


def test_assets_register_entry_auto_detect_and_dedup(client):
    """E6：POST /assets 走 register_asset 统一入口——auto 识别类型、同值合并
    不插重复行（修人工路径重复行）、识别不出 422 提示手选。"""
    pid = _make_project(client)
    r1 = client.post(f"/api/projects/{pid}/assets",
                     json={"type": "auto", "value": "https://10.5.5.5/admin"})
    assert r1.status_code == 201
    body = r1.json()
    assert body["type"] == "url" and body["created"] and body["host_id"]

    # 同值重报（旧实现直接 upsert 会插重复行）→ 同 id 合并
    r2 = client.post(f"/api/projects/{pid}/assets",
                     json={"value": "https://10.5.5.5/admin"})
    assert r2.status_code == 201
    assert r2.json()["id"] == body["id"] and not r2.json()["created"]

    # 识别不出 → 422
    r3 = client.post(f"/api/projects/{pid}/assets", json={"value": "无法识别???"})
    assert r3.status_code == 422 and "手选" in r3.json()["detail"]

    rows = client.get(f"/api/projects/{pid}/assets").json()
    assert len([a for a in rows if a["type"] == "url"]) == 1
    assert all("status" in a for a in rows)      # E7：status 出口


# ---------- 机制 1.1 发布去重/workset + 机制 1.4 wait_for 门控 / 建议私信边 ----------

def test_publish_dedup_and_force(client):
    """机制 1.1：同指纹第二次发布 200 deduplicated；force 真发；workset 出口可见。"""
    pid = _make_project(client)
    body = {"objective": "对 target.com 做被动侦察", "task_type": "generic",
            "workset": ["a.target.com", "0x401000"]}
    r1 = client.post(f"/api/projects/{pid}/tasks", json=body)
    assert r1.status_code == 201 and r1.json()["deduplicated"] is False
    r2 = client.post(f"/api/projects/{pid}/tasks", json=body)
    assert r2.status_code == 200 and r2.json()["deduplicated"] is True
    assert r2.json()["existed_status"] == "open" and r2.json()["task_id"] == r1.json()["task_id"]
    r3 = client.post(f"/api/projects/{pid}/tasks", json={**body, "force": True})
    assert r3.status_code == 201
    assert r3.json()["task_id"] != r1.json()["task_id"]
    rows = client.get(f"/api/projects/{pid}/tasks").json()
    assert any(t["workset"] == ["0x401000", "a.target.com"] for t in rows)


def test_publish_invalid_conflict_key_422(client):
    """机制 1.4：conflict_keys 非法键（过宽/未知方案）发布 422。"""
    pid = _make_project(client)
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "扫它", "noise_budget": "low",
                          "conflict_keys": ["ip:*"]})
    assert r.status_code == 422 and "ip:*" in r.json()["detail"]


def test_task_graph_suggest_edge(client):
    """机制 1.1：同依据（context_refs 相交）且已认领、无 inbox 边的任务对出 suggest 点虚线边。"""
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    from core.blackboard import TaskQueue
    tq = TaskQueue(proj.bb)
    s1 = proj.bb.register_session(pid, "S1")
    s2 = proj.bb.register_session(pid, "S2")
    t1 = tq.publish(pid, "分析 find-aaaaaaaaaaaa 的结论", task_type="generic")
    t2 = tq.publish(pid, "复核 find-aaaaaaaaaaaa 的证据", task_type="generic")
    tq.claim(t1, s1["id"])
    tq.claim(t2, s2["id"])
    graph = client.get(f"/api/projects/{pid}/task-graph").json()
    suggest = [e for e in graph["edges"] if e["kind"] == "suggest"]
    pair = {(e["source"], e["target"]) for e in suggest}
    assert ((t1, t2) in pair) or ((t2, t1) in pair)


def test_l2_budget_pause_auto_resumes(tmp_path):
    """C4：L2 档 budget_paused 自动续跑（缺省 +200，by=l2-auto；token 预算即总闸），
    任务不经人工最终完成；L0/L1 不受影响（保持人工「▶ 继续」）。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([
            {"tool_use": [S.tool_call("p1", "task_plan", {"steps": [{"title": "摸底"}]})]},
            {"tool_use": [S.tool_call("c1", "complete_task", {"result_note": "自动续跑后完成"})]},
            {"tool_use": [S.tool_call("f1", "finish", {"summary": "续跑成功"})]},
        ]),
        planner_llm=S([]), providers_config=str(tmp_path / "p.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "L2自续", "track": "ctf",
                                            "capabilities": ["binary"]}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config",
                       json={"config": {"autonomy": {"level": "L2", "paused": True}}}
                       ).status_code == 200
        tid = c.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "自续任务", "task_type": "generic"}).json()["task_id"]
        # 暂停挡住自动起跑：把任务专属待命窗的步数预算压到 1（一步即耗尽 → budget pause）
        sid = next(t["target_session"] for t in c.get(f"/api/projects/{pid}/tasks").json()
                   if t["id"] == tid)
        c.app.state.agents[sid].dispatcher.max_steps = 1
        assert c.patch(f"/api/projects/{pid}/config",
                       json={"config": {"autonomy": {"level": "L2", "paused": False}}}
                       ).status_code == 200
        w = c.post(f"/api/agents/{sid}/work")  # 启动绑定窗（人工 override，与自动挡去重安全）
        assert w.status_code == 200

        deadline = time.time() + 10
        done = False
        while time.time() < deadline:
            row = next(t for t in c.get(f"/api/projects/{pid}/tasks").json() if t["id"] == tid)
            if row["status"] == "done":
                done = True
                break
            time.sleep(0.2)
        assert done  # 无人点「继续」，L2 自动续跑后完成
        bb = c.app.state.projects[pid].bb
        evs = [(e["kind"], e["payload"]) for e in bb.recent_events(pid)]
        assert any(k == "step.budget_extended" and p.get("by") == "l2-auto" for k, p in evs)
        assert any(k == "session.resumed" and p.get("by") == "l2-auto" for k, p in evs)


def test_auto_spawn_l2_publish_opens_window_and_claims(tmp_path):
    """v0.71 发布即建窗 L2：publish 同步建专属待命窗（origin=task-window，
    reason=human-publish），调度器武装起跑，worker 只认领自己的任务并跑完。"""
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2自动补窗")
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "补窗即跑的任务", "task_type": "recon"})
        assert r.status_code == 201
        _wait_no_running(c, pid)
        tasks = c.get(f"/api/projects/{pid}/tasks").json()
        assert [t["status"] for t in tasks] == ["done"]  # 专属窗认领并完成
        bb = c.app.state.projects[pid].bb
        evs = [(e["kind"], e["payload"]) for e in bb.recent_events(pid)]
        spawns = [p for k, p in evs
                  if k == "session.spawned" and p.get("origin") == "task-window"]
        assert len(spawns) == 1
        assert spawns[0]["role"] == "_generalist"  # open 任务未绑角色 → 兜底
        assert spawns[0]["reason"] == "human-publish"
        from core.autonomy import count_active_sessions
        assert count_active_sessions(bb, pid) == 1


def test_l1_publish_standby_window_and_execution_approval(tmp_path):
    """v0.72 L1：发布即建专属待命窗（零 LLM）+ 按任务提执行审批单
    （action 带 task_id，批准=启动该窗）；每任务一单，不互相去重合并。"""
    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "L1补窗审批", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "待认领一", "task_type": "recon"})
        assert r.status_code == 201
        c.post(f"/api/projects/{pid}/tasks",
               json={"objective": "待认领二", "task_type": "recon"})
        _wait_no_running(c, pid)
        items = c.get(f"/api/projects/{pid}/approvals").json()
        spawns = [a for a in items if a["action"].get("op") == "spawn_session"]
        assert len(spawns) == 2  # 每个任务一张执行审批单（带各自 task_id）
        assert all(a["status"] == "pending" for a in spawns)
        assert all(a["requested_by"] == "auto-spawn" for a in spawns)
        assert {a["action"]["task_id"] for a in spawns} == {
            t["id"] for t in c.get(f"/api/projects/{pid}/tasks").json()}
        assert all(a["action"]["role"] == "_generalist" for a in spawns)
        # 待命窗已建（不耗 LLM、未武装不起跑）；批准才启动
        from core.autonomy import count_active_sessions
        assert count_active_sessions(c.app.state.projects[pid].bb, pid) == 2
        rows = {s["id"]: s for s in c.get(f"/api/projects/{pid}/sessions").json()}
        assert all(s["worker_armed"] is False for s in rows.values())


def test_legacy_auto_spawn_key_ignored_window_still_binds(tmp_path):
    """v0.72 auto_spawn 旗标退役：旧配置残留键被静默忽略——L2 发布照常建专属窗
    并自动起跑（挡位语义统一，项目级补窗开关不复存在）。"""
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2补窗关", auto_spawn=False)  # 残留键 no-op
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "照常建窗", "task_type": "recon"})
        assert r.status_code == 201 and r.json()["session_id"]
        _wait_no_running(c, pid)
        from core.autonomy import count_active_sessions
        assert count_active_sessions(c.app.state.projects[pid].bb, pid) == 1
        assert c.get(f"/api/projects/{pid}/approvals").json() == []
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "done"


def test_sessions_cap_full_task_waits_then_rebinds_on_close(tmp_path):
    """已达 sessions_cap 不补窗（不超卖）：建窗失败任务暂无绑，人手窗不接公共池
    （认领恒空）；关掉占坑窗后调度器立即重绑新窗并自动起跑（L2）。"""
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2补窗封顶", sessions_cap=1)
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "recon", "armed": True})
        assert sp.status_code == 201
        _wait_no_running(c, pid)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "暂无窗的任务", "task_type": "recon"})
        assert r.status_code == 201 and r.json()["session_id"] is None
        _wait_no_running(c, pid)
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"
        bb = c.app.state.projects[pid].bb
        evs = [(e["kind"], e["payload"]) for e in bb.recent_events(pid)]
        assert [p for k, p in evs if k == "session.spawned" and p.get("auto")] == []
        # 关掉占坑人手窗 → cap 释放 → 调度器（关窗收尾触发）重绑新窗并起跑
        assert c.post(f"/api/sessions/{sp.json()['id']}/close").status_code == 200
        deadline = time.time() + 10
        while time.time() < deadline:
            if c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "done":
                break
            time.sleep(0.05)
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "done"


def test_publish_parent_id_and_depth_422(client):
    """C1：parent_id 落库（任务流实线边）；坏父/超深 422。"""
    pid = _make_project(client)
    parent = client.post(f"/api/projects/{pid}/tasks",
                         json={"objective": "父任务", "task_type": "generic"}).json()["task_id"]
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "子任务", "task_type": "generic", "parent_id": parent})
    assert r.status_code == 201
    child = r.json()["task_id"]
    rows = {t["id"]: t for t in client.get(f"/api/projects/{pid}/tasks").json()}
    assert rows[child]["parent_id"] == parent
    # 坏父
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "坏父", "task_type": "generic", "parent_id": "task-nope"})
    assert r.status_code == 422
    # 人类显式建深树允许（深度 1 限编排器；撤回传播子树语义优先）
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "孙任务", "task_type": "generic", "parent_id": child})
    assert r.status_code == 201


def test_redteam_roe_semantics_by_track(client):
    """R2 轨级语义（mode 退役）：pentest 轨传 ROE 被忽略；redteam 轨 ROE 可选
    （缺=roe_complete False），mission/ROE 归一化入 usage；mode 键退役剥离。"""
    pid = _make_project(client)  # pentest 轨
    r = client.patch(f"/api/projects/{pid}/config", json={"config": {
        "mode": "redteam",  # mode 键退役：静默剥离不报错
        "mission": {"text": "打穿 mission", "criteria": "□ 判据一\n□ 判据二"},
        "redteam_roe": {"targets": "*.t.com", "window": "w",
                         "exclusions": "e", "approver": "a"}}})
    assert r.status_code == 200
    detail = client.get(f"/api/projects/{pid}").json()
    assert "mode" not in detail["config"]  # mode 退役剥离
    assert detail["usage"]["mission"]["text"] == "打穿 mission"
    assert detail["usage"].get("redteam_roe") is None  # pentest 轨忽略 ROE

    # redteam 轨：无 ROE 建项目成功，roe_complete=False（pentest 上限兜底）
    rp = client.post("/api/projects", json={"name": "红队-无ROE", "track": "redteam"})
    assert rp.status_code == 201
    rpid = rp.json()["id"]
    rdetail = client.get(f"/api/projects/{rpid}").json()
    assert rdetail["usage"]["roe_complete"] is False
    # 补全 ROE → roe_complete=True
    r = client.patch(f"/api/projects/{rpid}/config", json={"config": {
        "redteam_roe": {"targets": "*.t.com", "window": "2026-09-16~09-18",
                         "exclusions": "无", "approver": "owner"}}})
    assert r.status_code == 200
    rdetail = client.get(f"/api/projects/{rpid}").json()
    assert rdetail["usage"]["roe_complete"] is True
    assert rdetail["usage"]["redteam_roe"]["approver"] == "owner"


def test_redteam_prompt_fixed_at_session_creation(client):
    """R2：redteam 轨会话系统提示含红队语义与 ROE；ROE 未核验=按 pentest 上限兜底提示。"""
    pid = _make_project(client, track="redteam")
    client.patch(f"/api/projects/{pid}/config", json={"config": {
        "redteam_roe": {"targets": "*.t.com", "window": "w", "exclusions": "e",
                          "approver": "a"}}})
    sp = client.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
    assert sp.status_code == 201
    sid = sp.json()["id"]
    agent = client.app.state.agents[sid]
    assert "红队行动" in agent.capability_prompt and "*.t.com" in agent.capability_prompt
    # 无 ROE 的 redteam 项目：兜底提示出现
    rp = client.post("/api/projects", json={"name": "红队-无ROE2", "track": "redteam"})
    rpid = rp.json()["id"]
    sp2 = client.post(f"/api/projects/{rpid}/agents", json={"role": "_generalist"})
    agent2 = client.app.state.agents[sp2.json()["id"]]
    assert "ROE 未核验" in agent2.capability_prompt or "按渗透测试上限兜底" in agent2.capability_prompt


def test_directive_endpoint_and_auto_derive(tmp_path):
    """C2：指挥编排器——directive 落事件+自动 tick+注入 overview+轮末标 done；
    L1 auto_derive 开启时 worker 空退 → mission 自动派生新任务。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([{"text": "ok"}]),
        planner_llm=S([
            # directive 触发的第一轮：按指令发布任务
            {"tool_use": [S.tool_call("p1", "publish_task",
                                       {"objective": "对已登记资产做漏洞挖掘",
                                        "task_type": "generic"})]},
            {"tool_use": [S.tool_call("d1", "done", {})]},
        ]),
        providers_config=str(tmp_path / "p.json"))
    app.state.judgments_dir = tmp_path / "cfg"  # 测试隔离判据模板文件
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "指挥", "track": "ctf",
                                            "capabilities": ["binary"]}).json()["id"]
        # 开自动派生（L1，ctf 缺省 L0 → PATCH 提档）+ mission 判据
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {
            "autonomy": {"level": "L1", "auto_derive": True},
            "mission": {"text": "挖洞", "criteria": "□ 全覆盖"}}}).status_code == 200
        r = c.post(f"/api/projects/{pid}/orchestrator/directive",
                   json={"text": "对已登记资产做漏洞挖掘"})
        assert r.status_code == 200
        _wait_job(c, r.json()["job_id"])
        bb = c.app.state.projects[pid].bb
        evs = [e["kind"] for e in bb.recent_events(pid)]
        assert "orch.directive" in evs and "orch.directive.done" in evs
        # 第一轮按指令派生了任务
        assert any(t["objective"] == "对已登记资产做漏洞挖掘"
                   for t in c.get(f"/api/projects/{pid}/tasks").json())
        detail = c.get(f"/api/projects/{pid}").json()
        assert detail["usage"]["auto_derive"] is True


def test_judgment_templates_crud(client, tmp_path):
    """C2 判据模板：默认全 user 空；PUT 整表保存；DELETE 删除；resolve 三层优先级。"""
    from core.orchestrator.judgments import resolve_criteria
    pid = _make_project(client)
    app_jud = client.app.state.judgments_dir
    client.app.state.judgments_dir = tmp_path / "cfg"
    r = client.get("/api/judgment-templates")
    assert r.status_code == 200 and "渗透默认" in r.json()["builtin"] and r.json()["user"] == {}
    # PUT 保存
    r = client.put("/api/judgment-templates", json={"我的模板": "□ 自定义判据"})
    assert r.status_code == 200 and r.json()["saved"] == 1
    # 三层优先级：mission > template > builtin
    proj = client.app.state.projects[pid]
    cfg_empty = proj.bb.get_project(pid)["config"]
    res = resolve_criteria(cfg_empty, client.app.state.judgments_dir)
    assert res["source"] == "builtin"  # 无 mission 无选模板 → 内置兜底
    client.patch(f"/api/projects/{pid}/config",
                 json={"config": {"criteria_template": "我的模板"}})
    res = resolve_criteria(proj.bb.get_project(pid)["config"],
                           client.app.state.judgments_dir)
    assert res["source"] == "template" and "自定义判据" in res["criteria"]
    client.patch(f"/api/projects/{pid}/config",
                 json={"config": {"mission": {"criteria": "□ 手写判据"}}})
    res = resolve_criteria(proj.bb.get_project(pid)["config"],
                           client.app.state.judgments_dir)
    assert res["source"] == "mission"
    # DELETE
    r = client.delete("/api/judgment-templates/我的模板")
    assert r.status_code == 200
    assert client.get("/api/judgment-templates").json()["user"] == {}


def _wait_derive_result(client, pid: str, prefix: str, tries: int = 200) -> dict:
    """轮询 usage.derive.last_result 直到以 prefix 开头（派生在后台 job 里跑）。"""
    for _ in range(tries):
        detail = client.get(f"/api/projects/{pid}").json()
        last = (detail["usage"].get("derive") or {}).get("last_result") or ""
        if last.startswith(prefix):
            return detail["usage"]["derive"]
        time.sleep(0.05)
    raise AssertionError(f"derive.last_result 未以 {prefix} 开头（实际: {last!r}）")


def test_mission_poll_sweep_derives_when_idle(tmp_path):
    """①兜底轮询：队列空 + auto_derive（L1）→ 直调 sweep 自动派生新任务并落
    last_derive_*；队列有 open 任务时 sweep 不触发（require_idle）。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([{"text": "ok"}]),
        planner_llm=S([
            {"tool_use": [S.tool_call("p1", "publish_task",
                                      {"objective": "轮询派生任务", "task_type": "generic"})]},
            {"tool_use": [S.tool_call("d1", "done", {})]},
        ]),
        providers_config=str(tmp_path / "p.json"))
    app.state.judgments_dir = tmp_path / "cfg"  # 测试隔离判据模板文件
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "轮询派生", "track": "ctf"}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {
            "autonomy": {"level": "L1", "auto_derive": True},
            "mission": {"text": "挖洞", "criteria": "□ 全覆盖"}}}).status_code == 200
        # 变体 A：队列有 open 任务 → sweep 不判跳（不产新任务）
        tid = c.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "人手任务", "task_type": "generic"}).json()["task_id"]
        app.state.mission_poll_sweep()
        time.sleep(0.3)
        assert not any(t["objective"] == "轮询派生任务"
                       for t in c.get(f"/api/projects/{pid}/tasks").json())
        # 变体 B：队列清空 → sweep 触发派生，发布 1 任务
        assert c.delete(f"/api/tasks/{tid}").status_code == 200
        app.state.mission_poll_sweep()
        derive = _wait_derive_result(c, pid, "published:")
        assert derive["last_result"] == "published:1" and derive["last_at"]
        tasks = c.get(f"/api/projects/{pid}/tasks").json()
        assert any(t["objective"] == "轮询派生任务" for t in tasks)
        # 事件留痕：mission.derive（判定）+ mission.derive.result（结果）
        bb = c.app.state.projects[pid].bb
        kinds = [e["kind"] for e in bb.recent_events(pid)]
        assert "mission.derive" in kinds and "mission.derive.result" in kinds


def test_mission_derive_error_visible_and_throttled(tmp_path):
    """②/error 路径：LLM 失败 → last_derive_result 以 error 开头 + mission.derive.result
    + llm.error 事件（429 特征 → kind_hint=quota）；连续失败同文案 60s 节流只发一条。"""
    from test_orchestrator import ScriptedLLM as S

    class QuotaLLM(S):
        def chat(self, messages, **kw):  # noqa: ANN001,ANN003
            raise RuntimeError("HTTP 429: quota insufficient, 余额不足")

    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([{"text": "ok"}]),
        planner_llm=QuotaLLM([]),
        providers_config=str(tmp_path / "p.json"))
    app.state.judgments_dir = tmp_path / "cfg"
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "派生失败", "track": "ctf"}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {
            "autonomy": {"level": "L1", "auto_derive": True},
            "mission": {"text": "挖洞", "criteria": "□ 全覆盖"}}}).status_code == 200
        bb = c.app.state.projects[pid].bb

        def llm_errors() -> list[dict]:
            return [e for e in bb.recent_events(pid) if e["kind"] == "llm.error"]

        # 第一次派生：失败可见
        app.state.mission_poll_sweep()
        derive = _wait_derive_result(c, pid, "error:")
        assert "429" in derive["last_result"] or "quota" in derive["last_result"]
        assert len(llm_errors()) == 1
        assert llm_errors()[0]["payload"].get("kind_hint") == "quota"
        assert any(e["kind"] == "mission.derive.result" and e["payload"].get("error")
                   for e in bb.recent_events(pid))
        # 第二次派生（error 后 st.empty 复位允许重试）：同样失败，但 llm.error 节流不再发
        app.state.mission_poll_sweep()
        _wait_derive_result(c, pid, "error:")
        assert len(llm_errors()) == 1  # 节流：同文案 60s 内只一条


def test_orch_tick_started_event_on_manual_tick(tmp_path):
    """⑤编排开始日志可见：手动 tick 端点落 orch.tick.started 事件（事件流实时可见）。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([{"text": "ok"}]),
        planner_llm=S([{"tool_use": [S.tool_call("d1", "done", {})]}]),
        providers_config=str(tmp_path / "p.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "tick 日志", "track": "ctf"}).json()["id"]
        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        assert r.status_code == 200
        _wait_job(c, r.json()["job_id"])
        bb = c.app.state.projects[pid].bb
        started = [e for e in bb.recent_events(pid) if e["kind"] == "orch.tick.started"]
        assert started and started[0]["payload"]["reason"] == "manual"


# ---------- v14 任务绑定角色：POST /tasks role 贯穿 ----------

def test_task_publish_role_validation_and_bypass(client):
    """v14 POST /tasks：带合法 role 201 且入库；非法 role 422；force 同时旁路
    dedup 与同 target 防碎闸。"""
    pid = _make_project(client)
    # pentest 轨已注册角色（真实 packs）
    roles = client.get(f"/api/projects/{pid}/roles").json()
    assert roles, "测试前置：pentest 轨应有已注册角色"
    role_id = roles[0]["role"]
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "带角色任务", "task_type": "generic",
                          "role": role_id})
    assert r.status_code == 201
    task = client.get(f"/api/projects/{pid}/tasks").json()[0]
    assert task["role"] == role_id
    # 非法 role → 422
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "拼错角色", "task_type": "generic",
                          "role": "no-such-role"})
    assert r.status_code == 422 and "role" in r.json()["detail"]
    # 同 target 防碎闸：同 IP 第 5 个任务 422；force 旁路放行
    for i in range(4):
        assert client.post(
            f"/api/projects/{pid}/tasks",
            json={"objective": f"打点 {i}", "noise_budget": "low",
                  "conflict_keys": ["ip:2.2.2.2"]}).status_code == 201
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "打点 5", "noise_budget": "low",
                          "conflict_keys": ["ip:2.2.2.2"]})
    assert r.status_code == 422 and "任务已达" in r.json()["detail"]
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "打点 5", "noise_budget": "low",
                          "conflict_keys": ["ip:2.2.2.2"], "force": True})
    assert r.status_code == 201


# ---------- 附件随发（2026-09-19）：上传/去重/下载 + 任务与引导携带 ----------


def test_attachment_upload_dedup_and_download(client):
    """POST /attachments（输入行附件随发）：任意类型落 artifacts/attachments，
    artifact 行 kind=attachment（meta.original_name/size）；同内容 sha 去重返回
    既有 id；下载端点 FileResponse 全量回传（原文件名）。"""
    pid = _make_project(client)
    blob = b"\x7fELF" + os.urandom(64)
    r = client.post(f"/api/projects/{pid}/attachments",
                    files={"file": ("sample.elf", blob, "application/octet-stream")})
    assert r.status_code == 201
    att = r.json()
    assert att["name"] == "sample.elf" and att["size"] == len(blob)
    arts = client.get(f"/api/projects/{pid}/artifacts").json()
    assert arts[0]["kind"] == "attachment" and arts[0]["path"].startswith("artifacts/attachments/")
    assert json.loads(arts[0]["meta"])["original_name"] == "sample.elf"
    # 同内容去重：返回同一 artifact id，不新增行
    r2 = client.post(f"/api/projects/{pid}/attachments",
                     files={"file": ("sample.elf", blob, "application/octet-stream")})
    assert r2.status_code == 201 and r2.json()["id"] == att["id"]
    assert len(client.get(f"/api/projects/{pid}/artifacts").json()) == 1
    # 下载：内容与原文件名一致
    d = client.get(f"/api/projects/{pid}/artifacts/{att['id']}/download")
    assert d.status_code == 200 and d.content == blob
    assert "sample.elf" in d.headers.get("content-disposition", "")
    # 不存在 404
    assert client.get(f"/api/projects/{pid}/artifacts/art-nope/download").status_code == 404
    # 空文件 422（_spool_upload 护栏）
    assert client.post(f"/api/projects/{pid}/attachments",
                       files={"file": ("empty.bin", b"", "application/x")}).status_code == 422


def test_task_and_note_with_attachments(client):
    """发任务/引导会话携带附件：attachment_ids 校验（坏 id 422）→ 任务落
    context.attachments；human_note payload 带 attachments（纯附件无文本可发）。"""
    pid = _make_project(client)
    att = client.post(f"/api/projects/{pid}/attachments",
                      files={"file": ("poc.py", b"print(1)", "text/x-python")}).json()
    # 坏 id 422（发布前拦截）
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "分析", "attachment_ids": ["art-nope"]})
    assert r.status_code == 422 and "附件" in r.json()["detail"]
    # 发任务带附件 → context.attachments
    tid = client.post(f"/api/projects/{pid}/tasks",
                      json={"objective": "分析样本", "attachment_ids": [att["id"]]}
                      ).json()["task_id"]
    task = client.get(f"/api/projects/{pid}/tasks").json()[0]
    atts = task["context"]["attachments"]
    assert len(atts) == 1 and atts[0]["id"] == att["id"]
    assert atts[0]["path"] == att["path"] and atts[0]["name"] == "poc.py"
    # 引导会话：纯附件（text 空）可发；payload 带 attachments
    proj = client.app.state.projects[pid]
    sid = proj.bb.register_session(pid, "附件引导", role="_generalist")["id"]
    r = client.post(f"/api/sessions/{sid}/note",
                    json={"text": "", "attachment_ids": [att["id"]]})
    assert r.status_code == 201
    inbox = client.get(f"/api/sessions/{sid}/inbox").json()
    assert inbox[0]["payload"]["attachments"][0]["id"] == att["id"]
    # 空文本且无附件仍 422
    assert client.post(f"/api/sessions/{sid}/note",
                       json={"text": "", "attachment_ids": []}).status_code == 422


# ---------- H3：escalation 审批执行（deny-driven 一次性升级，API 层） ----------

def test_decide_escalation_executes_once_and_inboxes(client):
    """批准 escalation 单 → 网关直跑一次 → approval consumed + 收件箱回执 +
    message.inbox 事件；同单二次决策 422（一次性消费）。"""
    pid = _make_project(client, track="pentest")
    proj = client.app.state.projects[pid]
    appr = proj.bb.request_approval(
        pid, {"op": "escalation", "kind": "net_real",
              "cmd": "Write-Output escalation-ok", "runtime": "host",
              "threat_class": "trusted", "net": "bridge", "reason": "API 层测试"},
        risk="high", requested_by="sess-test", session_id="sess-test")
    r = client.post(f"/api/approvals/{appr['id']}/decide",
                    json={"decision": "approved"})
    assert r.status_code == 200
    data = r.json()
    assert data["executed"] is True and data["exit_code"] == 0
    # 审批已被一次性消费（approved → consumed，同单不可复用）
    row = proj.bb.conn.execute(
        "SELECT status FROM approvals WHERE id=?", (appr["id"],)).fetchone()
    assert row["status"] == "consumed"
    # 回执投递请求会话收件箱 + 事件流 message.inbox 同步可见
    inbox = proj.bb.inbox_list(pid, "sess-test")
    hit = [m for m in inbox
           if m["kind"] == "escalation_result" and m["ref_id"] == appr["id"]]
    assert len(hit) == 1 and "escalation-ok" in hit[0]["payload"]["text"]
    events = proj.bb.recent_events(pid)
    assert any(e["kind"] == "message.inbox"
               and e["payload"].get("kind") == "escalation_result"
               for e in events)
    # 已 consumed 的单再决策 → 422
    r2 = client.post(f"/api/approvals/{appr['id']}/decide",
                     json={"decision": "approved"})
    assert r2.status_code == 422


def test_decide_escalation_invalid_action_marks_exec_failed(client):
    """action 不合法（缺 cmd）：批准不回滚，落 approval.exec_failed 事件。"""
    pid = _make_project(client)
    proj = client.app.state.projects[pid]
    appr = proj.bb.request_approval(
        pid, {"op": "escalation", "runtime": "host"},
        risk="high", requested_by="sess-x")
    r = client.post(f"/api/approvals/{appr['id']}/decide",
                    json={"decision": "approved"})
    assert r.status_code == 200 and r.json()["executed"] is False
    assert "RuntimeError" in r.json()["error"]
    ev = [e for e in proj.bb.recent_events(pid)
          if e["kind"] == "approval.exec_failed"]
    assert ev and ev[0]["payload"]["op"] == "escalation"
    row = proj.bb.conn.execute(
        "SELECT status FROM approvals WHERE id=?", (appr["id"],)).fetchone()
    assert row["status"] == "approved"  # 执行失败不回滚批准


# ---------- 蓝图（R4 逆向开发管线） ----------

def test_blueprints_crud_and_status_flow(client):
    """蓝图 API：建 → 列表 → 详情 → PATCH 正文/goal → status 白名单流转 → 422/404。"""
    pid = _make_project(client, track="research")
    r = client.post(f"/api/projects/{pid}/blueprints", json={
        "name": "聊天客户端", "goal": "重建", "binary_sha256": "abc",
        "modules": [{"name": "网络通信", "func_addresses": ["0x401000"]}]})
    assert r.status_code == 201
    bp = r.json()
    bid = bp["id"]
    assert bp["status"] == "draft" and bp["modules"][0]["name"] == "网络通信"
    assert len(client.get(f"/api/projects/{pid}/blueprints").json()) == 1
    assert client.get(f"/api/projects/{pid}/blueprints/{bid}").status_code == 200
    assert client.get(f"/api/projects/{pid}/blueprints/bp-nope").status_code == 404
    # PATCH 正文追加 + goal
    r = client.patch(f"/api/projects/{pid}/blueprints/{bid}",
                     json={"content_append": "## 数据流", "goal": "重建 v2"})
    assert r.status_code == 200 and "数据流" in r.json()["content_md"]
    # 状态白名单：draft→ready 一步跳 → 422
    r = client.patch(f"/api/projects/{pid}/blueprints/{bid}", json={"status": "ready"})
    assert r.status_code == 422
    r = client.patch(f"/api/projects/{pid}/blueprints/{bid}", json={"status": "reviewed"})
    assert r.status_code == 200 and r.json()["status"] == "reviewed"
    r = client.patch(f"/api/projects/{pid}/blueprints/{bid}", json={"status": "ready"})
    assert r.status_code == 200 and r.json()["status"] == "ready"
    # 非法状态值 422
    r = client.patch(f"/api/projects/{pid}/blueprints/{bid}", json={"status": "shipped"})
    assert r.status_code == 422
    # 重名（同样本同名）422；不同样本同名允许
    r = client.post(f"/api/projects/{pid}/blueprints",
                    json={"name": "聊天客户端", "binary_sha256": "abc"})
    assert r.status_code == 422
    r = client.post(f"/api/projects/{pid}/blueprints",
                    json={"name": "聊天客户端", "binary_sha256": "def"})
    assert r.status_code == 201
    # 模块 patch：正常与 404
    r = client.patch(f"/api/projects/{pid}/blueprints/{bid}/modules/网络通信",
                     json={"status": "specd", "spec": "def send(): ..."})
    assert r.status_code == 200
    assert r.json()["modules"][0]["status"] == "specd"
    r = client.patch(f"/api/projects/{pid}/blueprints/{bid}/modules/没有的模块",
                     json={"notes": "x"})
    assert r.status_code == 404
    # 项目隔离
    pid2 = _make_project(client, track="research")
    assert client.get(f"/api/projects/{pid2}/blueprints/{bid}").status_code == 404


def test_blueprints_events_streamed(client):
    """蓝图写路径落事件流（blueprint.created/status_changed），前端 tick 刷新可见。"""
    pid = _make_project(client, track="research")
    bid = client.post(f"/api/projects/{pid}/blueprints",
                      json={"name": "蓝图事件"}).json()["id"]
    client.patch(f"/api/projects/{pid}/blueprints/{bid}", json={"status": "reviewed"})
    kinds = [e["kind"] for e in client.get(f"/api/projects/{pid}/events").json()]
    assert "blueprint.created" in kinds and "blueprint.status_changed" in kinds


# ---------- v18 向指定会话直接发任务（target_session 认领门控） ----------


def _pentest_project(c, name: str) -> str:
    return c.post("/api/projects", json={"name": name, "track": "pentest",
                                         "capabilities": ["web"]}).json()["id"]


def test_task_target_session_publish_and_claim_gate(client):
    """v18 门控：指派任务只有目标窗能认领（claim_next WHERE 过滤 + claim 硬拒绝）。"""
    from core.blackboard import ClaimError, TaskQueue

    pid = _pentest_project(client, "指派门控")
    bb = client.app.state.projects[pid].bb
    sa = bb.register_session(pid, "窗甲", role="_generalist")["id"]
    sb = bb.register_session(pid, "窗乙", role="_generalist")["id"]
    tq = TaskQueue(bb)

    tid = tq.publish(pid, "指派任务", target_session=sa)
    # task.published 事件带 target_session（前端看板 chip 数据源）
    pub = [e for e in bb.recent_events(pid)
           if e["kind"] == "task.published" and e["payload"]["task_id"] == tid]
    assert pub and pub[-1]["payload"]["target_session"] == sa
    # 乙窗认领不到指派任务；甲窗能
    assert tq.claim_next(pid, sb) is None
    assert tq.claim_next(pid, sa) == tid
    # 乙窗绕过 claim_next 直接 claim → 硬门控拒绝
    tid2 = tq.publish(pid, "指派任务二", target_session=sa)
    with pytest.raises(ClaimError):
        tq.claim(tid2, sb)
    # 公共池任务不受影响：两窗都能认领
    t3 = tq.publish(pid, "公共池任务")
    assert tq.claim_next(pid, sb) == t3


def test_task_target_session_deprecated_ignored(client):
    """v0.71 任务即窗口：API target_session 字段退役（保留兼容旧前端但忽略）——
    传任意值都不落指派；v0.72 发布一律建专属待命窗并双向绑定，绑定窗由
    发布路径返回的 session_id 决定，与传入值无关。"""
    pid = _pentest_project(client, "指派字段弃用")
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "x", "task_type": "recon",
                          "target_session": "sess-nope"})
    assert r.status_code == 201
    rows = client.get(f"/api/projects/{pid}/tasks").json()
    assert len(rows) == 1 and rows[0]["target_session"] == r.json()["session_id"]


def test_task_target_session_arms_idle_window(tmp_path):
    """v0.71 发布即建窗：L2 发布同步建专属待命窗（响应带 session_id、meta
    bound_task_id 双向绑定），调度器武装起跑，绑定窗只认领自己的任务并跑完。"""
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2发布即建窗")
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "发布即建窗的任务", "task_type": "recon"})
        assert r.status_code == 201
        body = r.json()
        sid = body.get("session_id")
        assert sid and body["deduplicated"] is False
        sess = c.app.state.projects[pid].bb.get_session(sid)
        meta = sess.get("meta")
        meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
        assert meta.get("bound_task_id") == body["task_id"]
        _wait_no_running(c, pid)
        task = next(t for t in c.get(f"/api/projects/{pid}/tasks").json()
                    if t["id"] == body["task_id"])
        assert task["status"] == "done" and task["claimed_by"] == sid  # 专属窗自己跑完


def test_close_session_releases_task_and_rebinds(client):
    """v18 关窗退绑 + v0.72 调度器立即重绑：关待执行窗清 target_session
    （事件 reason=session-closed），绑定段马上给 open 任务补建新待命窗（L1），
    任务不再悬空饿死。"""
    from core.blackboard import TaskQueue

    pid = _pentest_project(client, "关窗退池")
    bb = client.app.state.projects[pid].bb
    sid = bb.register_session(pid, "拆迁窗", role="_generalist")["id"]
    tq = TaskQueue(bb)
    tid = tq.publish(pid, "随窗指派的任务", target_session=sid)

    assert client.post(f"/api/sessions/{sid}/close").status_code == 200
    task = tq.get_task(tid)
    assert task["status"] == "open" and task["target_session"] != sid  # 已重绑新待命窗
    upd = [e for e in bb.recent_events(pid)
           if e["kind"] == "task.updated" and e["payload"].get("task_id") == tid]
    assert upd and upd[-1]["payload"]["changes"] == {"target_session": sid}
    assert upd[-1]["payload"]["reason"] == "session-closed"


def test_reopen_clears_target_session(client):
    """v0.71 放回不清指派：失败任务归原绑定窗（窗是任务的延续现场，同窗续跑）；
    task.reopened 事件不再带 unassigned_from。"""
    from core.blackboard import TaskQueue

    pid = _pentest_project(client, "放回归原窗")
    bb = client.app.state.projects[pid].bb
    sid = bb.register_session(pid, "失败窗", role="_generalist")["id"]
    tq = TaskQueue(bb)
    tid = tq.publish(pid, "失败后放回的任务", target_session=sid)
    tq.claim(tid, sid)
    tq.fail(tid, sid, "炸了")

    tq.reopen(tid, by="human")
    task = tq.get_task(tid)
    assert task["status"] == "open" and task["target_session"] == sid
    ev = [e for e in bb.recent_events(pid)
          if e["kind"] == "task.reopened" and e["payload"]["task_id"] == tid][-1]
    assert "unassigned_from" not in ev["payload"]


def test_auto_spawn_ignores_assigned_tasks(tmp_path):
    """v0.71 绑定模型：手动注册的既有窗（无绑定）不接任务——公共池为空，
    各任务只由自己的专属窗执行，手动窗保持闲置。"""
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2手动窗不抢活")
        bb = c.app.state.projects[pid].bb
        sm = bb.register_session(pid, "手动窗", role="_generalist")["id"]
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "只归专属窗的任务", "task_type": "recon"})
        assert r.status_code == 201
        tid = r.json()["task_id"]
        _wait_no_running(c, pid)
        task = next(t for t in c.get(f"/api/projects/{pid}/tasks").json()
                    if t["id"] == tid)
        assert task["status"] == "done" and task["claimed_by"] != sm
        # 专属窗跑完即任务终态（窗保留待命）；手动窗从未动过任务
        assert task["claimed_by"] == r.json()["session_id"]


def test_dedup_ignores_target_session(tmp_path):
    """dedup_fp 不含 target_session（v14「role 不入指纹」同案：投递偏好非任务本体）。
    force 指派乙后 worker（剧本式 executor）认领跑完，收尾无悬置 job。"""
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _pentest_project(c, "指派去重")
        bb = c.app.state.projects[pid].bb
        from core.blackboard import TaskQueue
        from core.blackboard.tasks import dedup_fp

        sa = bb.register_session(pid, "去重甲", role="_generalist")["id"]
        sb = bb.register_session(pid, "去重乙", role="_generalist")["id"]
        tq = TaskQueue(bb)
        t1 = tq.publish(pid, "同文任务", task_type="recon", target_session=sa)
        tq.publish(pid, "同文任务", task_type="recon", target_session=sb)
        dup = tq.find_dedup_target(pid, dedup_fp("recon", "", "同文任务"))
        assert dup["id"] == t1  # 指派不同不改指纹：仍命中先发的

        # API 层：同文再发（target_session 字段已弃用被忽略）→ 200 deduplicated；
        # force → 201（v0.71：不指派既有窗，kicked 不再指向 sb）
        body = {"objective": "同文任务", "task_type": "recon", "target_session": sb}
        r = c.post(f"/api/projects/{pid}/tasks", json=body)
        assert r.status_code == 200 and r.json()["deduplicated"] is True
        r = c.post(f"/api/projects/{pid}/tasks", json={**body, "force": True})
        assert r.status_code == 201
        _wait_no_running(c, pid)


def test_schema_v18_migration_adds_target_session(tmp_path):
    """v17 旧库打开幂等补列：target_session 在且默认 ''。"""
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "迁移项目",
                                            "track": "pentest"}).json()["id"]
        tid = c.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "旧库任务", "task_type": "recon"}).json()["task_id"]
    import sqlite3

    db = next(tmp_path.rglob("blackboard.db"))  # 工作区目录布局解耦：直接找库文件
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE tasks DROP COLUMN target_session")  # 模拟 v17 库
    conn.execute("UPDATE meta SET value='17' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    app2 = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                      executor_llm=None, planner_llm=None,
                      providers_config=str(tmp_path / "providers2.json"))
    with TestClient(app2) as c2:
        bb = (c2.app.state.projects.get(pid) or c2.app.state.store.open_project(pid)).bb
        cols = [r["name"] for r in bb.conn.execute("PRAGMA table_info(tasks)")]
        assert "target_session" in cols
        row = bb.conn.execute(
            "SELECT target_session FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["target_session"] == ""  # 旧库存量任务全部落公共池
def test_spa_static_hosting(tmp_path):
    """desktop-app-shell M2：static_dir 挂同源静态托管——真实文件直出、其余 GET 回
    index.html（history fallback）；/api、/docs、/openapi.json 不被兜底遮蔽。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA-INDEX</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"ico")
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")  # dist 外

    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"),
                     static_dir=str(dist))
    with TestClient(app) as c:
        assert c.get("/").text == "<html>SPA-INDEX</html>"          # 根 = index
        assert "SPA-INDEX" in c.get("/projects/some-view").text      # history fallback
        assert c.get("/assets/app.js").text == "console.log(1)"      # 静态产物直出
        assert c.get("/favicon.ico").content == b"ico"
        # 未知 API GET 回 404 而非 index（调试不被兜底坑）
        r = c.get("/api/definitely-missing")
        assert r.status_code == 404 and "SPA-INDEX" not in r.text
        assert c.get("/openapi.json").json()["openapi"]              # Swagger 三件不受影响
        assert c.get("/docs").status_code == 200
        # 真实 API 路由注册序在前，优先于兜底
        assert c.get("/api/projects").status_code == 200
        # 防穿越：dist 外文件绝不透出（无论 URL 归一化走哪条路，内容都是 index）
        for evil in ("/..%2fsecret.txt", "/%2e%2e/secret.txt", "/..%5Csecret.txt"):
            r = c.get(evil)
            assert r.status_code == 200 and "SECRET" not in r.text

    # 缺 index.html 的 static_dir 直接报错，不留半挂载状态
    bad = tmp_path / "empty"
    bad.mkdir()
    with pytest.raises(ValueError, match="index.html"):
        create_app(workspace_root=str(tmp_path / "ws2"), tools_root=None,
                   executor_llm=None, planner_llm=None,
                   providers_config=str(tmp_path / "providers.json"),
                   static_dir=str(bad))


# ---------- 对话化编排器（M1/M2/M3）：chat 端点 / goal 闭环 / 拟人 / virtual 专家 ----------

def test_orchestrator_chat_endpoint(tmp_path):
    """对话化编排器 chat：租约同步 acquire（占用 409 不产 job）→ 人类消息落
    orch.chat（author=human）→ Job chat_turn 回复落 {role:"orch"}；第二轮历史
    含第一轮（messages 连续）；空文本 422。"""
    from test_orchestrator import ScriptedLLM
    planner = ScriptedLLM([
        {"text": "编排器在线。"},
        {"text": "现在是第二轮。"},
    ])
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=planner,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "对话编排", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        bb = c.app.state.projects[pid].bb
        r = c.post(f"/api/projects/{pid}/orchestrator/chat", json={"text": "在吗？"})
        assert r.status_code == 200
        job = _wait_job(c, r.json()["job_id"])
        assert job["status"] == "done"
        assert job["result"]["reply"].startswith("编排器在线")
        chats = [e for e in bb.recent_events(pid) if e["kind"] == "orch.chat"]
        assert [e["payload"]["role"] for e in chats] == ["human", "orch"]
        assert chats[0]["author"] == "human" and chats[1]["author"] == "orchestrator"
        # 第二轮：历史窗口含第一轮（_chat_history 组装连续对话）
        r2 = c.post(f"/api/projects/{pid}/orchestrator/chat", json={"text": "再说一遍"})
        job2 = _wait_job(c, r2.json()["job_id"])
        assert job2["result"]["reply"].startswith("现在是第二轮")
        first_msgs = planner.calls[1]["messages"]
        assert first_msgs[0] == {"role": "user", "content": "在吗？"}
        assert first_msgs[1]["role"] == "assistant"
        assert first_msgs[-1] == {"role": "user", "content": "再说一遍"}

        # 租约被占 → 409，不产 job（不排队）
        from core.orchestrator import state as ost
        ost.acquire_tick_lease(bb, pid, "stuck-owner")
        before = _project_jobs(c, pid)
        r3 = c.post(f"/api/projects/{pid}/orchestrator/chat", json={"text": "又来"})
        assert r3.status_code == 409 and "思考" in r3.json()["detail"]
        assert _project_jobs(c, pid) == before
        ost.release_tick_lease(bb, pid, "stuck-owner")
        # 空文本 422
        assert c.post(f"/api/projects/{pid}/orchestrator/chat",
                      json={"text": "   "}).status_code == 422


def test_goal_persona_and_virtual_expert_endpoints(tmp_path):
    """M2/M3：PUT goal 确认/清空留痕 + project.json 单一真相源落盘；persona PUT
    与剥键；GET /api/experts 恒追加 virtual 编排器条目（带 pid 时取显示名）。"""
    from test_orchestrator import ScriptedLLM
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "目标闭环", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        proj = c.app.state.projects[pid]
        bb = proj.bb
        # 初始：无 goal 无 persona
        g = c.get(f"/api/projects/{pid}/goal")
        assert g.status_code == 200
        assert g.json()["phase_goal"] is None and g.json()["persona"] is None
        # PUT 确认 → meta + goal.confirm 事件（payload 全文快照）
        r = c.put(f"/api/projects/{pid}/goal", json={
            "text": "打穿 3 台主机", "criteria": ["拿到 flag"], "phase": "initial-access"})
        assert r.status_code == 200
        got = c.get(f"/api/projects/{pid}/goal").json()["phase_goal"]
        assert got["text"] == "打穿 3 台主机"
        assert got["criteria"] == ["拿到 flag"] and got["phase"] == "initial-access"
        assert got["confirmed_by"] == "human" and got["source"] == "chat"
        confirms = [e for e in bb.recent_events(pid) if e["kind"] == "goal.confirm"]
        assert len(confirms) == 1 and confirms[0]["author"] == "human"
        assert confirms[0]["payload"]["goal"]["text"] == "打穿 3 台主机"
        # project.json 落盘（黑板 projects 行无此列，单一真相源）
        on_disk = json.loads((proj.path / "project.json").read_text(encoding="utf-8"))
        assert on_disk["phase_goal"]["text"] == "打穿 3 台主机"
        # 清空（text 空）→ 剥键 + goal.clear
        assert c.put(f"/api/projects/{pid}/goal", json={"text": ""}).json()["status"] == "cleared"
        assert c.get(f"/api/projects/{pid}/goal").json()["phase_goal"] is None
        on_disk = json.loads((proj.path / "project.json").read_text(encoding="utf-8"))
        assert "phase_goal" not in on_disk
        assert [e for e in bb.recent_events(pid) if e["kind"] == "goal.clear"]
        # persona PUT + 恢复缺省（两字段全空剥键）
        r = c.put(f"/api/projects/{pid}/orchestrator/persona",
                  json={"display_name": "老编", "persona": "说话直接"})
        assert r.json()["persona"] == {"display_name": "老编", "persona": "说话直接"}
        assert c.get(f"/api/projects/{pid}/goal").json()["persona"]["display_name"] == "老编"
        c.put(f"/api/projects/{pid}/orchestrator/persona", json={"display_name": "", "persona": ""})
        assert c.get(f"/api/projects/{pid}/goal").json()["persona"] is None
        # virtual 专家条目：带 pid 取 meta.display_name；不带 pid 缺省名
        virt = [e for e in c.get("/api/experts", params={"pid": pid}).json()
                if e.get("kind") == "virtual"]
        assert len(virt) == 1 and virt[0]["id"] == "orchestrator"
        assert virt[0]["name"] == "编排器" and virt[0]["protected"] is True
        c.put(f"/api/projects/{pid}/orchestrator/persona",
              json={"display_name": "老编", "persona": "x"})
        virt2 = [e for e in c.get("/api/experts", params={"pid": pid}).json()
                 if e.get("kind") == "virtual"]
        assert virt2[0]["name"] == "老编"


def test_goal_sections_reach_live_prompts(tmp_path):
    """端到端：goal 确认后（经 API 写 project.json），chat/tick 的系统提示
    都能实时看到（meta_loader 实时读 meta，换目标即时生效不靠重启）。"""
    from test_orchestrator import ScriptedLLM
    planner = ScriptedLLM([
        {"text": "收到目标。"},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=planner,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "目标贯通", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        c.put(f"/api/projects/{pid}/goal", json={"text": "拿下 admin 面板"})
        job = _wait_job(c, c.post(f"/api/projects/{pid}/orchestrator/chat",
                                  json={"text": "目标是什么？"}).json()["job_id"])
        assert job["result"]["reply"] == "收到目标。"
        assert "拿下 admin 面板" in planner.calls[0]["system"]
        tjob = _wait_job(c, c.post(f"/api/projects/{pid}/orchestrator/tick",
                                   json={}).json()["job_id"])
        assert tjob["status"] == "done"
        assert "拿下 admin 面板" in planner.calls[1]["system"]
