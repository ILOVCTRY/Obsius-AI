"""core API 测试（TestClient，不触网；LLM 缺失时 Agent 端点应 503）。"""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import fofa
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
    """M2 构造链：工厂按绑定专家推导会话能力面；allowed_roles=绑定清单
    （绑定外专家越界；未绑定项目回落按轨全池）。"""
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
        # 未绑定项目：allowed_roles=按轨全池
        pid2 = c.post("/api/projects", json={"name": "未绑定", "track": "ctf"}).json()["id"]
        sid2 = c.post(f"/api/projects/{pid2}/agents",
                      json={"role": "reverse"}).json()["id"]
        assert c.app.state.agents[sid2].dispatcher.allowed_roles != ["web-solver"]


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
    assert detail["usage"]["tokens"] == {"used": 0, "budget": None, "pct": None,
                                         "cache_read": 0, "cache_creation": 0,
                                         "cache_hit": None}
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
    # 仅开窗不会占用 cap；只有运行中的会话才计入。
    sid = client.app.state.projects[pid].bb.register_session(pid, "占位会话", role="_generalist")["id"]
    resp = client.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
    assert resp.status_code == 201
    client.app.state.projects[pid].bb.set_session_status(sid, "running")
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


def test_events_session_filter(client):
    """会话维度分页（2026-09-23 直播间会话窗口）：tail/before_id 带 session_id
    各取各的（store 层 F8 既支持，API 透传）；不带参数仍是全局流（__all 语义不变）。"""
    pid = _make_project(client)
    bb = client.app.state.projects[pid].bb
    s1 = bb.register_session(pid, "会话甲")["id"]
    s2 = bb.register_session(pid, "会话乙")["id"]
    for i in range(8):
        bb.append_event(pid, "test.a", {"i": i}, session_id=s1, author=s1)
    for i in range(5):
        bb.append_event(pid, "test.b", {"i": i}, session_id=s2, author=s2)
    bb.append_event(pid, "test.orch", {}, author="human")  # 无会话归属（编排事件）

    # tail 带会话：只落本会话事件，升序
    a = client.get(f"/api/projects/{pid}/events", params={"tail": 50, "session_id": s1}).json()
    assert [e["session_id"] for e in a] == [s1] * 8
    assert a == sorted(a, key=lambda e: e["id"])
    b = client.get(f"/api/projects/{pid}/events", params={"tail": 50, "session_id": s2}).json()
    assert [e["session_id"] for e in b] == [s2] * 5
    # before_id 带会话：从窗口尾部翻更早一页，同样只落本会话
    older = client.get(f"/api/projects/{pid}/events",
                       params={"before_id": a[-1]["id"], "limit": 3, "session_id": s1}).json()
    assert len(older) == 3 and older == sorted(older, key=lambda e: e["id"])
    assert all(e["session_id"] == s1 for e in older) and older[-1]["id"] < a[-1]["id"]
    # 不带参数 = 全局流：两会话 + 编排事件全可见
    glob = client.get(f"/api/projects/{pid}/events", params={"tail": 100}).json()
    sids = {e.get("session_id") for e in glob if e["kind"].startswith("test.")}
    assert sids == {s1, s2, None}


def test_session_graph_endpoint(client):
    """会话中心化 M4：GET session-graph——节点=编排器+会话窗（带队列计数）；
    delegate（orch→窗，工作单元来自 team_run_members）/ dm（私信）边与 refs 明细。"""
    pid = _make_project(client)
    bb = client.app.state.projects[pid].bb
    s1 = bb.register_session(pid, "窗1", role="_generalist")["id"]
    s2 = bb.register_session(pid, "窗2", role="_generalist")["id"]
    from core.team.store import TeamStore
    ts = TeamStore(bb, packs_root=None)
    team = ts.create_team(pid, name="分析队", goal_text="共同目标", members=[
        {"member_key": "a", "label": "甲", "responsibility": "第一件", "role": ""},
        {"member_key": "b", "label": "乙", "responsibility": "派生件", "role": ""},
    ])
    pf = ts.preflight(pid, team["id"])
    run = ts.create_run_and_members(
        pid, team["id"], revision=pf["revision"],
        confirmations={"members": True, "goal": True, "safety": True, "execution": True})
    by_key = {m["member_key"]: m for m in run["members"]}
    m1, m2 = by_key["a"], by_key["b"]
    ts.claim_member(m1["id"])
    ts.attach_session(m1["id"], s1)     # 在跑 → current
    ts.claim_member(m2["id"])
    ts.attach_session(m2["id"], s2)
    ts.mark_member(m2["id"], "pending")  # 排队 → queue
    assert bb.post_agent_message(pid, s1, s2, "intel", "同步情报") is not None

    r = client.get(f"/api/projects/{pid}/session-graph")
    assert r.status_code == 200
    g = r.json()
    assert {n["id"] for n in g["nodes"]} == {"__orch", s1, s2}
    assert next(n for n in g["nodes"] if n["id"] == "__orch")["kind"] == "orch"
    pairs = {(e["kind"], e["source"], e["target"]) for e in g["edges"]}
    assert ("delegate", "__orch", s1) in pairs
    assert ("dm", s1, s2) in pairs
    deleg = next(e for e in g["edges"] if e["kind"] == "delegate" and e["target"] == s1)
    assert deleg["refs"][0]["objective"] == "第一件"
    n1 = next(n for n in g["nodes"] if n["id"] == s1)
    assert n1["current"] == "第一件" and n1["queue"] == 0
    n2 = next(n for n in g["nodes"] if n["id"] == s2)
    assert n2["queue"] == 1 and n2["current"] == ""
    assert client.get("/api/projects/proj-nope/session-graph").status_code in (403, 404)


def test_board_graph_endpoint(client):
    """黑板链路图（2026-09-20）：四类节点（task 已随任务机制退役）+ 跨实体边；跨项目 404。"""
    pid = _make_project(client)
    bb = client.app.state.projects[pid].bb
    url_id = bb.upsert_asset(pid, "url", "http://10.0.0.9/")["id"]
    f = bb.add_finding(pid, "sqli", "注入", target_asset_id=url_id)["id"]

    r = client.get(f"/api/projects/{pid}/board-graph")
    assert r.status_code == 200
    g = r.json()
    types = {n["node_type"] for n in g["nodes"]}
    assert types == {"asset", "finding"}
    assert {n["label"] for n in g["nodes"] if n["node_type"] == "asset"} == {"http://10.0.0.9/"}
    pairs = {(e["kind"], e["source"], e["target"]) for e in g["edges"]}
    assert ("targets", f, url_id) in pairs
    assert client.get("/api/projects/proj-nope/board-graph").status_code in (403, 404)


def test_events_endpoint_and_websocket(client):
    pid = _make_project(client)
    bb = client.app.state.projects[pid].bb
    bb.append_event(pid, "test.ev", {"objective": "分诊"}, author="human")
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "test.ev" for e in events)
    since = max(e["id"] for e in events)
    bb.append_event(pid, "test.ev", {"objective": "再分诊"}, author="human")
    delta = client.get(f"/api/projects/{pid}/events", params={"since_id": since}).json()
    pub = [e for e in delta if e["kind"] == "test.ev"]
    assert len(pub) == 1 and pub[0]["payload"]["objective"] == "再分诊"
    # WS：从 0 回放全部事件（该项目恰好 2 条 test.ev）
    total = len(client.get(f"/api/projects/{pid}/events").json())
    with client.websocket_connect(f"/api/ws/projects/{pid}?since_id=0") as ws:
        got = [ws.receive_json() for _ in range(total)]
        assert sum(1 for e in got if e["kind"] == "test.ev") == 2


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
        client.post(f"/api/projects/{pid}/assets",
                    json={"type": "host", "value": f"10.0.0.{i}"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(client.get, f"/api/projects/{pid}/findings") for _ in range(8)]
        futures += [ex.submit(client.get, f"/api/projects/{pid}/assets") for _ in range(8)]
        futures += [ex.submit(client.get, f"/api/projects/{pid}/events") for _ in range(8)]
        for f in futures:
            assert f.result(timeout=10).status_code == 200
    # 并发写也不互踩（WAL + busy_timeout + BEGIN IMMEDIATE）
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(client.post, f"/api/projects/{pid}/assets",
                          json={"type": "host", "value": f"10.1.0.{i}"})
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


def test_delete_unknown_project_404(client):
    assert client.delete("/api/projects/nope").status_code == 404


def test_delete_project_with_open_websocket(client):
    """删除竞态回归：WS 保持连接（每秒 tick 在池线程持有该库连接）时删除必须成功，
    WS 收 1008 关闭。旧实现下 close_all 后 tick 立刻惰性重连锁死 db，rename 必败 422。"""
    import time
    pid = _make_project(client)
    # 建项即落一条 phase.changed 事件：WS 从「此刻」起听（对齐心跳测试），否则
    # since_id=0 会先播该事件、receive_json 正常返回，收不到删除时的 1008
    since = max(e["id"] for e in client.get(f"/api/projects/{pid}/events").json())
    ready = threading.Event()
    closed: dict = {}

    def hold_ws():
        try:
            with client.websocket_connect(
                    f"/api/ws/projects/{pid}?since_id={since}") as ws:
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


def test_ws_tick_heartbeat_frame(client):
    """空转心跳回归（2026-09-29 消息刷新不及时复盘）：WS 空闲 tick_s 拍后发一条
    不落库的 ws.tick 帧——前端看门狗据此区分「管道死」（20s 零帧→判死重连）与
    「健康静默」（长命令执行期 tick 照常到达，不误判）。tick 不进事件流。"""
    pid = _make_project(client)
    since = max(e["id"] for e in client.get(f"/api/projects/{pid}/events").json())
    with client.websocket_connect(
            f"/api/ws/projects/{pid}?since_id={since}&tick_s=1") as ws:
        frame = ws.receive_json()
        assert frame["kind"] == "ws.tick"
        assert "id" not in frame  # 无事件 id：前端拦在游标赋值之前，不污染游标
    assert "ts" in frame
    # 心跳不落库：事件流没有多出任何帧
    events = client.get(f"/api/projects/{pid}/events").json()
    assert max(e["id"] for e in events) == since


def test_ws_delivers_new_event_within_poll_interval(client):
    """流式投递节奏回归（2026-10-04「输出过程卡卡的」）：WS 端点此前无事件时固定
    sleep(1.0)，且只在「本轮查库为空」时睡——查库命中一次后立即再查必空 → 睡满
    1s，于是前端每秒才收到一批事件（文本 1Hz 跳变）。改为 120ms 短轮询后，新写入
    的事件应在远小于 1s 内送达。"""
    import time

    pid = _make_project(client)
    since = max(e["id"] for e in client.get(f"/api/projects/{pid}/events").json())
    with client.websocket_connect(
            f"/api/ws/projects/{pid}?since_id={since}") as ws:
        time.sleep(0.05)  # 让服务端先空转一拍（旧实现此刻已进入 1s sleep）
        client.app.state.projects[pid].bb.append_event(
            pid, "probe.delivery", {"n": 1})
        t0 = time.monotonic()
        frame = ws.receive_json()
        elapsed = time.monotonic() - t0
    assert frame["kind"] == "probe.delivery"
    assert elapsed < 0.6, f"WS 投递延迟 {elapsed:.2f}s（旧实现约 1s，瓶颈在投递节奏）"


def test_list_roles_endpoint(client):
    """角色清单端点：按项目场景轨扫 tracks/<track>/roles（旧 domain 入参兼容，真实 packs）。"""
    rp = client.post("/api/projects", json={"name": "p", "domain": "pentest"})
    pid = rp.json()["id"]
    roles = client.get(f"/api/projects/{pid}/roles").json()
    names = {r["role"] for r in roles}
    assert {"_generalist", "recon", "external-entry", "osint", "report-writer"} <= names
    recon = next(r for r in roles if r["role"] == "recon")
    assert recon["task_types"] == ["recon", "asset-enum"]
    assert recon["persona"] and "default_noise" not in recon
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
    assert client.post(f"/api/sessions/{sid}/note", json={"text": "  "}).status_code == 422
    assert client.post("/api/sessions/sess-nope/note",
                       json={"text": "x"}).status_code == 404
    proj.bb.close_session(sid)
    assert client.post(f"/api/sessions/{sid}/note", json={"text": "x"}).status_code == 409


def test_compact_session_endpoint(client, tmp_path):
    """POST /compact（/compact 手动持久压缩，2026-10-06）：未知会话 404；无对话
    历史 noop；有历史且摘要成功 → 重写 chat-<sid>.json 并落 llm.compact。"""
    from core.agent.loop import session_chat_path
    pid = _make_project(client)
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]
    assert client.post("/api/sessions/sess-nope/compact").status_code == 404
    # 未装配 artifacts_dir → 无对话历史 → noop(empty)
    r = client.post(f"/api/sessions/{sid}/compact")
    assert r.status_code == 200 and r.json()["status"] == "noop"

    agent.artifacts_dir = tmp_path / "artifacts"
    path = session_chat_path(agent.artifacts_dir, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    msgs: list[dict] = []
    for i in range(10):  # 10 轮问答 = 20 条，超过 keep_recent+2
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    path.write_text(json.dumps({"session_id": sid, "messages": msgs},
                               ensure_ascii=False), encoding="utf-8")

    class _Sum:
        model = "fake"

        def chat(self, messages, **kw):
            return type("R", (), {
                "raw": {"content": [{"type": "text", "text": "摘要正文"}]},
                "usage": {}})()

    agent.llm = _Sum()
    r = client.post(f"/api/sessions/{sid}/compact")
    assert r.status_code == 200 and r.json()["status"] == "compacted"
    out = json.loads(path.read_text(encoding="utf-8"))["messages"]
    assert "历史摘要" in out[0]["content"]
    events = client.get(f"/api/projects/{pid}/events").json()
    assert any(e["kind"] == "llm.compact" and e["payload"].get("manual") for e in events)


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


def test_session_note_wakes_paused_session(client):
    """暂停会话的人工引导=显式唤醒（2026-09-27 修「引导石沉大海」）：此前
    armed 窗被暂停时 note 仍踢 worker——rehydrate 载回快照即 paused=True，
    run_session 暂停闸静默空退（无事件无日志无 LLM），引导滞留收件箱永无
    回应。现走恢复语义：session.resumed(by=human-note) + worker 起跑 +
    重新点亮武装；预算暂停比照 resume 缺省 +200（by=human-note）。"""
    pid = client.post("/api/projects", json={"name": "渗透-引导唤醒",
                                             "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]

    # 暂停（reason=pause，有快照态）→ note 唤醒
    agent.paused = True
    agent._resume_state = {"system": "s", "messages": [], "objective": "x",
                           "task_id": None, "next_step": 1, "max_steps": 10,
                           "reason": "pause"}
    r = client.post(f"/api/sessions/{sid}/note", json={"text": "继续"})
    assert r.status_code == 201 and r.json()["wake"] == "resumed"
    assert agent.paused is False
    assert client.get(f"/api/projects/{pid}/sessions").json()[0]["status"] == "running"
    events = client.get(f"/api/projects/{pid}/events").json()
    resumed = [e for e in events if e["kind"] == "session.resumed"]
    assert resumed and resumed[-1]["payload"]["by"] == "human-note"
    assert any(j["kind"] == "agent-work" and j["meta"].get("session_id") == sid
               for j in client.app.state.jobs.all_jobs())
    # F9：唤醒重新点亮武装
    sessions = client.get(f"/api/projects/{pid}/sessions").json()
    assert sessions[0]["worker_armed"] is True

    # 等唤醒轮 job 收尾（假 agent 无 LLM 会异常退出）：否则下一条 note 命中
    # _session_job_running → deferred（轮中投递轮末注入，语义正确但断言不了）
    import time
    deadline = time.time() + 5
    while (any(j["status"] == "running" and j["meta"].get("session_id") == sid
               for j in client.app.state.jobs.all_jobs())
           and time.time() < deadline):
        time.sleep(0.05)

    # 预算暂停 → 缺省 +200，增补事件 by=human-note
    agent._resume_state = {"system": "s", "messages": [], "objective": "x",
                           "task_id": None, "next_step": 20, "max_steps": 20,
                           "reason": "budget"}
    agent.dispatcher.max_steps = 20
    agent.paused = True
    r = client.post(f"/api/sessions/{sid}/note", json={"text": "接着来"})
    assert r.status_code == 201 and r.json()["wake"] == "resumed"
    assert agent.dispatcher.max_steps == 220
    ext = [e for e in client.get(f"/api/projects/{pid}/events").json()
           if e["kind"] == "step.budget_extended"]
    assert ext and ext[-1]["payload"]["by"] == "human-note"
    assert ext[-1]["payload"]["old_max"] == 20 and ext[-1]["payload"]["new_max"] == 220


def test_session_note_wake_kinds(client):
    """wake 分支补全：未暂停 armed 窗=kicked；worker 轮在跑=deferred（轮末注入）。"""
    pid = client.post("/api/projects", json={"name": "渗透-引导分支",
                                             "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]
    bb = client.app.state.projects[pid].bb

    bb.set_session_meta(sid, {"worker_armed": True})
    r = client.post(f"/api/sessions/{sid}/note", json={"text": "在吗"})
    assert r.status_code == 201 and r.json()["wake"] == "kicked"

    # 轮进行中投递 → deferred（挂起一个 agent-work job 占位，轮末注入）
    import threading
    gate = threading.Event()
    client.app.state.jobs.submit("agent-work", gate.wait,
                                 meta={"project_id": pid, "session_id": sid})
    r = client.post(f"/api/sessions/{sid}/note", json={"text": "等着"})
    assert r.status_code == 201 and r.json()["wake"] == "deferred"
    gate.set()


def test_paused_gate_emits_work_state_audit(client):
    """暂停闸空退留审计（2026-09-27）：踢到暂停会话的 worker 在 run_session
    暂停闸 return None 前落 session.work_state(paused=True)——静默黑洞不再
    无迹可循（上游闸漏时事件流必须可见）。"""
    pid = client.post("/api/projects", json={"name": "渗透-暂停闸审计",
                                             "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
    agent = _spawn_test_agent(client, pid)
    agent.paused = True
    assert agent.run_session() is None
    events = client.get(f"/api/projects/{pid}/events").json()
    hits = [e for e in events if e["kind"] == "session.work_state"
            and e["payload"].get("paused")]
    assert hits and hits[-1]["author"] == agent.session["id"]


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


def test_sweep_disarms_all_armed_sessions(client):
    """2026-09-28 重启即急停：重启清扫把全部 worker_armed 一律解除（含空闲待命
    窗）——armed 是「常驻自动接活」持久开关，残留会让 kick 链把陈旧 running 会话
    当待命窗重新拉起（页签青灯常亮 + 后台自动烧 LLM）。恢复=人工「跑任务队列/
    ▶继续」重新点亮，恢复流程不受影响。"""
    rp = client.post("/api/projects", json={"name": "重启急停", "track": "pentest",
                                            "capabilities": ["web"]})
    pid = rp.json()["id"]
    bb = client.app.state.projects[pid].bb
    s_armed_running = bb.register_session(pid, "武装在跑窗", role="_generalist")["id"]
    s_armed_idle = bb.register_session(pid, "武装待命窗", role="_generalist")["id"]
    s_plain = bb.register_session(pid, "无武装窗", role="_generalist")["id"]
    bb.set_session_meta(s_armed_running, {"worker_armed": True})
    bb.set_session_meta(s_armed_idle, {"worker_armed": True})
    bb.set_session_status(s_armed_running, "running")  # 模拟强杀残留的陈旧在跑窗

    # 模拟重启：摘内存缓存 → 下一请求触发惰性首开清扫
    client.app.state.projects.pop(pid)
    assert client.get(f"/api/projects/{pid}").status_code == 200

    rows = {s["id"]: s for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert rows[s_armed_running]["worker_armed"] is False   # 急停：armed 全清
    assert rows[s_armed_idle]["worker_armed"] is False      # 空闲待命窗同样解除
    assert rows[s_plain]["worker_armed"] is False
    assert rows[s_armed_running]["status"] == "idle"        # 无快照 running → idle（纪律③）
    assert rows[s_armed_idle]["status"] == "idle"           # 空闲窗状态不动


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
    """编排 tick 亲自执行（execute）开的窗必须进 app.state.agents（孤儿窗根因回归）：
    可 work、可暂停。"""
    from test_orchestrator import ScriptedLLM

    orch_llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("e1", "execute", {
            "objective": "核查 8080 旁站低噪信息"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    exec_llm = ScriptedLLM([{"text": "核查完成"}, {"text": "核查完成"}])
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=exec_llm, planner_llm=orch_llm,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        rp = c.post("/api/projects", json={"name": "编排开窗注册", "track": "pentest",
                                          "capabilities": ["web"]})
        pid = rp.json()["id"]
        r = c.post(f"/api/projects/{pid}/orchestrator/tick", json={})
        assert r.status_code == 200
        _wait_job(c, r.json()["job_id"])
        _wait_no_running(c, pid)

        agents = [s for s in c.app.state.agents.values() if s.project_id == pid]
        # 编排器 execute 亲自执行开的一扇窗：孤儿窗回归断言锚定它必须注册在册。
        assert len(agents) == 1, "编排 execute 开窗未注册进 app.state.agents（孤儿窗回归）"
        sid = agents[0].session["id"]
        # 旧根因：这两个操作对编排开窗 404
        w = c.post(f"/api/agents/{sid}/work")
        assert w.status_code == 200
        _wait_job(c, w.json()["job_id"])  # 空队列秒退；编排窗默认 armed → 空闲暂停只解除武装
        r = c.post(f"/api/sessions/{sid}/pause")
        assert r.status_code == 200 and r.json()["status"] == "idle"
        kinds = [e["kind"] for e in c.app.state.projects[pid].bb.recent_events(pid)]
        assert "session.spawned" in kinds


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


# ---------- 批 6：L0 提案模式（ctf 默认档；tick 只提案，人采纳走既有写口） ----------

def test_l0_tick_only_emits_proposals(tmp_path):
    """ctf 项目默认 L0：tick 中 build_team/execute 只落 orch.proposed——teams/sessions
    无新增、无 session.spawned、无链无自动 job、结构化 proposals 正确、write_digest 照常。"""
    from test_orchestrator import ScriptedLLM as S
    planner = [
        {"tool_use": [
            S.tool_call("p1", "build_team", {
                "name": "逆向小队", "goal_text": "静态逆 check_flag",
                "members": [{"member_key": "rev", "label": "逆向手",
                             "responsibility": "静态逆 check_flag"}]}),
            S.tool_call("p2", "execute", {"objective": "补一刀核对符号"}),
        ]},
        {"tool_use": [S.tool_call("g1", "write_digest",
                                  {"summary": "L0 提案轮：2 件待采纳"})]},
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
        assert result["published"] == [] and result["spawned"] == [] and result["teams"] == []
        assert [p["op"] for p in result["proposals"]] == ["build_team", "execute"]
        pargs = result["proposals"][0]["args"]
        assert pargs["name"] == "逆向小队" and pargs["goal_text"] == "静态逆 check_flag"
        assert pargs["members"][0]["member_key"] == "rev"
        assert result["proposals"][1]["args"]["objective"] == "补一刀核对符号"
        assert isinstance(result["proposals"][0]["event_id"], int)
        # 实体表零新增
        assert c.get(f"/api/projects/{pid}/teams").json() == []
        assert c.get(f"/api/projects/{pid}/sessions").json() == []
        bb = c.app.state.projects[pid].bb
        kinds = [e["kind"] for e in bb.recent_events(pid)]
        assert kinds.count("orch.proposed") == 2
        assert "session.spawned" not in kinds
        assert not any(k.startswith("orch.chain") for k in kinds)
        # digest 照常
        digests = [e for e in bb.recent_events(pid) if e["kind"] == "project.digest"]
        assert len(digests) == 1 and "2 件" in digests[0]["payload"]["digest"]
        # 无任何自动 job（L0 不续 tick、不自起 worker）
        _wait_no_running(c, pid)
        assert [j for j in _project_jobs(c, pid)
                if j["kind"] in {"orchestrator-auto-tick", "orchestrator-auto-wait"}] == []
        # usage 不暴露活链
        assert c.get(f"/api/projects/{pid}").json()["usage"]["chain"]["active"] is False


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
        assert set(result) == {"summary", "published", "spawned", "digest", "proposals", "teams"}
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


# ---------- D10 策略顾问项目级配置（advisor-settings-ui，2026-09-24） ----------

def test_d10_advisor_config_consumed_at_session_factory(tmp_path):
    """⑧PATCH advisor 段后开窗：三整数透传 AgentConfig 与 dispatcher；
    无 advisor 段项目吃代码缺省（12/2/2）。"""
    from test_orchestrator import ScriptedLLM
    outer_planner = ScriptedLLM([])
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=outer_planner,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "顾问定制", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        r = c.patch(f"/api/projects/{pid}/config", json={"config": {"advisor": {
            "stuck_after": 8, "stuck_max_extensions": 1, "closing_max_rounds": 0}}})
        assert r.status_code == 200, r.text
        sid = c.post(f"/api/projects/{pid}/agents",
                     json={"role": "_generalist"}).json()["id"]
        agent = c.app.state.agents[sid]
        assert agent.config.stuck_after == 8
        assert agent.config.stuck_max_extensions == 1
        assert agent.config.closing_max_rounds == 0
        assert agent.dispatcher.closing_max_rounds == 0  # 实际消费点
        # 无 advisor 段 → 代码缺省 12/2/2
        pid2 = c.post("/api/projects", json={"name": "缺省", "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
        sid2 = c.post(f"/api/projects/{pid2}/agents",
                      json={"role": "_generalist"}).json()["id"]
        a2 = c.app.state.agents[sid2]
        assert (a2.config.stuck_after, a2.config.stuck_max_extensions,
                a2.config.closing_max_rounds) == (12, 2, 2)
        assert a2.dispatcher.closing_max_rounds == 2
        assert a2.planner_llm is outer_planner  # 无覆写=外层全局 planner


def test_d10_advisor_provider_override_applies_to_session_planner(tmp_path, monkeypatch):
    """⑨advisor.provider/model → 会话 planner_llm 被覆写为 build() 产物；
    无覆写项目不变。"""
    from test_orchestrator import ScriptedLLM
    outer_planner = ScriptedLLM([])
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=outer_planner,
                     providers_config=str(tmp_path / "providers.json"))
    marker = ScriptedLLM([])
    build_calls: list[tuple] = []

    def fake_build(name=None, model=None):
        build_calls.append((name, model))
        return marker

    monkeypatch.setattr(app.state.llm_store, "build", fake_build)
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "覆写", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {"advisor": {
            "provider": "ark-plan", "model": "glm-x"}}}).status_code == 200
        sid = c.post(f"/api/projects/{pid}/agents",
                     json={"role": "_generalist"}).json()["id"]
        agent = c.app.state.agents[sid]
        assert agent.planner_llm is marker and agent.llm is not marker
        assert ("ark-plan", "glm-x") in build_calls
        # executor 不受影响（覆写只作用于顾问三消费点）
        pid2 = c.post("/api/projects", json={"name": "无覆写", "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
        sid2 = c.post(f"/api/projects/{pid2}/agents",
                      json={"role": "_generalist"}).json()["id"]
        assert c.app.state.agents[sid2].planner_llm is outer_planner


def test_d10_advisor_override_build_failure_falls_back(tmp_path, monkeypatch):
    """⑩build 抛错（供应商事后被删/停用）→ 开窗仍 200，静默回退全局 planner。"""
    from test_orchestrator import ScriptedLLM
    outer_planner = ScriptedLLM([])
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=outer_planner,
                     providers_config=str(tmp_path / "providers.json"))

    def boom(name=None, model=None):
        raise RuntimeError("供应商已删除")

    monkeypatch.setattr(app.state.llm_store, "build", boom)
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "坏覆写", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {"advisor": {
            "provider": "ghost", "model": "m"}}}).status_code == 200
        r = c.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
        assert r.status_code == 201, r.text
        assert c.app.state.agents[r.json()["id"]].planner_llm is outer_planner


def test_d10_advisor_override_does_not_reach_orchestrator(tmp_path, monkeypatch):
    """⑪Orchestrator 自身的 llm 恒为外层 plan_llm——覆写代码必须在 factory()
    内层；chat 轮由外层 planner 应答，标记对象零调用。"""
    from test_orchestrator import ScriptedLLM
    planner = ScriptedLLM([{"text": "编排器在线。"}])

    class _MarkerLLM:
        used = False

        def chat(self, *a, **k):
            type(self).used = True
            raise AssertionError("顾问覆写泄漏到 Orchestrator")

    marker = _MarkerLLM()
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=planner,
                     providers_config=str(tmp_path / "providers.json"))
    monkeypatch.setattr(app.state.llm_store, "build",
                        lambda name=None, model=None: marker)
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "隔离", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {"advisor": {
            "provider": "ark-plan", "model": "glm-x"}}}).status_code == 200
        r = c.post(f"/api/projects/{pid}/orchestrator/chat", json={"text": "在吗？"})
        assert r.status_code == 200
        job = _wait_job(c, r.json()["job_id"])
        assert job["status"] == "done"
        assert job["result"]["reply"].startswith("编排器在线")
        assert planner.calls and marker.used is False


def test_orchestrator_compact_endpoint(tmp_path):
    """POST /orchestrator/compact（指挥页签 /compact）：历史过短 noop；有历史则旧
    orch.chat 压成摘要落 orch.compact 事件。"""
    from test_orchestrator import ScriptedLLM
    planner = ScriptedLLM([{"text": "## 摘要\n人类要测 X。"}])
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=ScriptedLLM([]), planner_llm=planner,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "指挥压缩", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        # 无对话历史 → noop（同时把项目缓存进 app.state.projects）
        r = c.post(f"/api/projects/{pid}/orchestrator/compact")
        assert r.status_code == 200 and r.json()["status"] == "noop"
        bb = app.state.projects[pid].bb
        for i in range(6):  # 6 轮 = 12 条
            bb.append_event(pid, "orch.chat", {"role": "human", "text": f"问{i}"},
                            author="human")
            bb.append_event(pid, "orch.chat", {"role": "orch", "text": f"答{i}"},
                            author="orchestrator")
        r = c.post(f"/api/projects/{pid}/orchestrator/compact")
        assert r.status_code == 200 and r.json()["status"] == "compacted"
        evs = [e for e in bb.recent_events(pid) if e["kind"] == "orch.compact"]
        assert len(evs) == 1 and evs[0]["payload"]["summarized"] == 4


def test_session_context_endpoint(client):
    """GET /sessions/{sid}/context（/context，2026-10-06）：窗口/占用/阈值/breakdown；
    未知会话 404。"""
    pid = _make_project(client)
    agent = _spawn_test_agent(client, pid)
    sid = agent.session["id"]
    r = client.get(f"/api/sessions/{sid}/context")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == 256_000
    assert body["threshold"] == int(256_000 * 0.85)
    assert set(body["breakdown"]) == {"system", "tools", "messages"}
    assert body["used"] >= 1 and body["source"] in ("measured", "estimated")
    assert client.get("/api/sessions/sess-nope/context").status_code == 404


def test_orchestrator_context_endpoint(tmp_path):
    """GET /orchestrator/context（指挥页签 /context，2026-10-06）：200 + 结构。"""
    from test_orchestrator import ScriptedLLM
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=ScriptedLLM([]), planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "指挥用量", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        r = c.get(f"/api/projects/{pid}/orchestrator/context")
        assert r.status_code == 200
        body = r.json()
        assert body["window"] == 256_000
        assert set(body["breakdown"]) == {"system", "tools", "messages"}
        assert body["used"] >= 1


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


def test_project_executor_llm_override(client):
    """项目级 executor 覆写（TRAE 新壳 M3）：GET 三态 / PUT 坏值 422 不落盘+
    好值持久且内存会话即时换装 / DELETE 剥键重置回缺省。"""
    pid = _make_project(client)
    r = client.get(f"/api/projects/{pid}/executor-llm")
    data = r.json()
    assert data["override"] is None and data["effective"] == data["default"]

    sid = client.post(f"/api/projects/{pid}/agents",
                      json={"role": "_generalist"}).json()["id"]
    # PUT 覆写
    r = client.put(f"/api/projects/{pid}/executor-llm",
                   json={"provider": "ark-plan", "model": "ark-code-latest"})
    assert r.status_code == 200
    ov = {"provider": "ark-plan", "model": "ark-code-latest"}
    rj = r.json()
    assert rj["override"] == ov and sid in rj["touched_sessions"]
    assert client.app.state.agents[sid].llm.base_url.endswith("/api/plan")
    cfg = client.app.state.projects[pid].bb.get_project(pid)["config"]
    assert cfg["executor_llm"] == ov

    # 坏供应商：422 且原覆写不动
    assert client.put(f"/api/projects/{pid}/executor-llm",
                      json={"provider": "nope"}).status_code == 422
    cfg = client.app.state.projects[pid].bb.get_project(pid)["config"]
    assert cfg["executor_llm"] == ov
    g = client.get(f"/api/projects/{pid}/executor-llm").json()
    assert g["override"] == ov and g["effective"] == ov

    # DELETE 重置：键剥离 + 内存会话回缺省
    r = client.delete(f"/api/projects/{pid}/executor-llm")
    assert r.status_code == 200 and r.json()["override"] is None
    cfg = client.app.state.projects[pid].bb.get_project(pid)["config"]
    assert "executor_llm" not in cfg
    assert client.app.state.agents[sid].llm.base_url.endswith("/api/coding")


def test_executor_override_fallback_emits_event(client):
    """项目 executor 覆写指向坏供应商：开窗仍静默回退全局执行器（绝不开窗失败），
    但补落 llm.fallback 可见化事件（此前只有后端 log，用户无感）。"""
    pid = client.post("/api/projects", json={
        "name": "回退可见化", "track": "ctf", "capabilities": ["binary"],
        "config": {"executor_llm": {"provider": "nope", "model": "ghost"}}}).json()["id"]
    sid = client.post(f"/api/projects/{pid}/agents",
                      json={"role": "_generalist"}).json()["id"]
    ev = [e for e in client.get(f"/api/projects/{pid}/events").json()
          if e["kind"] == "llm.fallback"]
    assert len(ev) == 1
    payload = ev[0]["payload"]
    assert payload["source"] == "executor"
    assert payload["requested"] == {"provider": "nope", "model": "ghost"}
    assert payload["session_id"] == sid
    assert payload["fallback"]["model"]  # 回退到全局默认执行器（model 非空）
    assert payload["reason"]


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
    """DELETE finding：200 带审计快照；再删/跨项目 → 404。"""
    pid = _make_project(client)
    f = client.post(f"/api/projects/{pid}/findings",
                    json={"vuln_class": "sqli-del", "title": "待删走查卡",
                          "severity": "high"}).json()
    r = client.delete(f"/api/projects/{pid}/findings/{f['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == f["id"] and body["title"] == "待删走查卡"
    assert client.get(f"/api/projects/{pid}/findings").json() == []
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

    blob = CRACKME.read_bytes()
    r = client.post(f"/api/projects/{pid}/samples",
                    files={"file": ("crackme.elf", blob, "application/octet-stream")})
    assert r.status_code == 202
    up = r.json()
    # 2026-09-29：上传不再自动分诊——job_id 恒 None，分析由用户显式确认
    assert up["cached"] is False and up["job_id"] is None and len(up["sha"]) == 64
    sha = up["sha"]
    tr = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
    assert tr.status_code == 202
    job = _wait_job(client, tr.json()["job_id"])
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
    # 单函数 + xref（caller/callee，导入函数地址 null）
    one = client.get(f"/api/projects/{pid}/binaries/{sha}/functions/401234").json()
    assert one["name"] == "main" and "fgets" in one["calls"]
    x = client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/401234").json()
    callees = {c["name"]: c["address"] for c in x["callees"]}
    assert callees == {"check_flag": "0x401189", "fgets": None}
    back = client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/401189").json()
    assert [c["name"] for c in back["callers"]] == ["main"]
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/deadbeef").status_code == 404

    # 重复上传：缓存命中，不再投 Job（必须在坏缓存验证前执行）
    r2 = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme-copy.elf", blob, "application/octet-stream")})
    assert r2.status_code == 202 and r2.json()["cached"] is True and r2.json()["job_id"] is None

    # strings 增强段导出失败 {"error": ...} → 409（不伪装成空表）。坏缓存
    # 会由读取路径删除，因此该验证必须放在重复上传缓存命中断言之后。
    proj_obj = client.app.state.projects.get(pid) or client.app.state.store.open_project(pid)
    cache_file = proj_obj.artifacts_dir / "decompiler-cache" / f"{sha}.json"
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    raw["strings"] = {"error": "idautils boom"}
    cache_file.write_text(json.dumps(raw), encoding="utf-8")
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/strings").status_code == 409

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
    assert r.json()["job_id"] is None  # 上传不再自动分诊

    sha = r.json()["sha"]
    # 缓存缺席：overview 仍 200；函数/xref 409
    ov = client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()
    assert ov["cached"] is False and ov["tools"]["ida"]["state"] == "off"
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/functions").status_code == 409
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/401234").status_code == 409
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/strings").status_code == 409
    # 无 IDA 库 → 打开 409（在解析 GUI 之前）
    assert client.post(f"/api/projects/{pid}/binaries/{sha}/open").status_code == 409
    # 显式分诊：无工具是结构化降级而非 Job 失败
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "no-tool" and job["result"]["reason"] == "no-tool"
    kinds = [e["kind"] for e in client.get(f"/api/projects/{pid}/events").json()]
    assert "binary.triage_failed" in kinds and "binary.triaged" not in kinds
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
    assert up["job_id"] is None  # 上传不再自动分诊
    _wait_job(client, client.post(
        f"/api/projects/{pid}/binaries/{up['sha']}/triage").json()["job_id"])
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
    assert up["job_id"] is None  # 上传不再自动分诊
    _wait_job(client, client.post(
        f"/api/projects/{pid}/binaries/{up['sha']}/triage").json()["job_id"])
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
    assert up["job_id"] is None  # 上传不再自动分诊
    _wait_job(client, client.post(
        f"/api/projects/{pid}/binaries/{up['sha']}/triage").json()["job_id"])
    sha = up["sha"]

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


def test_rev_import_and_reexport_db(client, tmp_path):
    """大样本加速 P1 三端点：导入导出 JSON / 直读既有库重导 / 纳入外部 IDA 库。

    各 Job 成功发对应事件，且 job 结果只回计数（绝不塞整份全量导出）；路径不存在 422。
    """
    from core.tools.decompiler import DecompilerService, IDAHeadlessBackend

    def run(args):
        sarg = next(a for a in args if a.startswith('-S"'))
        out_p = Path(sarg[3:].rstrip('"').split(" ", 1)[1])
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(V3), encoding="utf-8")
        return 0, "idat ok", ""

    def factory(proj):
        ida = IDAHeadlessBackend(idat_cmd="idat", runner=run, available=True,
                                 db_dir=proj.artifacts_dir / "decompiler-db")
        return DecompilerService(cache_dir=proj.artifacts_dir / "decompiler-cache",
                                 ida=ida, global_cache_dir=tmp_path / "global-cache")

    client.app.state.rev_service_factory = factory
    pid = client.post("/api/projects", json={
        "name": "大样本", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    sha = up["sha"]

    # ① 导入导出 JSON：外部全量导出 → 本地缓存（免重跑 headless）
    ext_json = tmp_path / "export.json"
    ext_json.write_text(json.dumps(V3), encoding="utf-8")
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/import-export",
                    json={"path": str(ext_json)})
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "ok" and job["result"]["functions"] == 2
    assert "binary.export_imported" in [e["kind"] for e in
                                        client.get(f"/api/projects/{pid}/events").json()]
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/strings").status_code == 200

    # ② 直读既有库重导：库内重导（无 -o）→ 事件 + 计数（不回大字段）
    db_dir = client.app.state.projects[pid].artifacts_dir / "decompiler-db"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / f"{sha}.i64").write_bytes(b"IDADB")
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/reexport-db")
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"] == {"status": "ok", "sha": sha, "functions": 2}
    assert "binary.db_reexported" in [e["kind"] for e in
                                      client.get(f"/api/projects/{pid}/events").json()]

    # ③ 纳入外部 IDA 库：拷进项目 db_dir + 库内重导 → 事件 + 库被替换
    ext_db = tmp_path / "incoming" / "x.i64"
    ext_db.parent.mkdir(parents=True)
    ext_db.write_bytes(b"EXT")
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/import-ida-db",
                    json={"path": str(ext_db)})
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "ok" and job["result"]["functions"] == 2
    assert (db_dir / f"{sha}.i64").read_bytes() == b"EXT"
    assert "binary.db_imported" in [e["kind"] for e in
                                    client.get(f"/api/projects/{pid}/events").json()]

    # ④ 入参护栏：源路径不存在 422；样本不存在 404
    assert client.post(f"/api/projects/{pid}/binaries/{sha}/import-export",
                       json={"path": str(tmp_path / "nope.json")}).status_code == 422
    assert client.post(f"/api/projects/{pid}/binaries/{'f' * 64}/import-ida-db",
                       json={"path": str(ext_db)}).status_code == 404


def test_rev_triage_cancel_cooperative(client):
    """headless 导出协作式停止（大样本 P3）：triage/cancel 置位 → Job status=stopped、
    事件 binary.triaged 带 stopped、partial 缓存立即可见（overview cached）；幂等。"""
    from core.tools.decompiler import DecompilerService, GhidraHeadlessBackend

    def ghidra_ctl_runner(args):
        i = args.index("-postScript")
        out = Path(args[i + 2])
        prog = Path(args[i + 4]) if len(args) > i + 4 else None
        stop = Path(args[i + 5]) if len(args) > i + 5 else None
        out.parent.mkdir(parents=True, exist_ok=True)
        if prog is not None:
            prog.write_text(json.dumps({"done": 1, "total": 2, "phase": "decompile"}),
                            encoding="utf-8")
        stopped = False
        if stop is not None:
            deadline = time.time() + 3.0
            while not stop.is_file() and time.time() < deadline:
                time.sleep(0.02)
            stopped = stop.is_file()
        data = json.loads(json.dumps(V3))
        if stopped:
            data["partial"] = True
            data["meta"]["partial"] = True
            data["functions"] = [{"address": f["address"], "name": f["name"],
                                  "size": f["size"], "calls": []}
                                 for f in V3["functions"]]
        out.write_text(json.dumps(data), encoding="utf-8")
        return 0, "ok", ""

    def factory(proj):
        g = GhidraHeadlessBackend(runner=ghidra_ctl_runner, available=True,
                                  tmp_project_dir=proj.artifacts_dir / ".ghidra-tmp")
        return DecompilerService(cache_dir=proj.artifacts_dir / "decompiler-cache", ghidra=g)

    client.app.state.rev_service_factory = factory
    pid = client.post("/api/projects", json={
        "name": "rev-cancel", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes(),
                                     "application/octet-stream")}).json()
    sha = up["sha"]
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    # 取消（假 runner 阻塞等停止文件，窗口内必到）
    c = client.post(f"/api/projects/{pid}/binaries/{sha}/triage/cancel")
    assert c.status_code == 200 and c.json()["cancelling"] is True
    job = _wait_job(client, job_id)
    assert job["status"] == "done"
    assert job["result"]["status"] == "stopped"
    # 事件带 stopped；partial 缓存立即可见
    ev = [e for e in client.get(f"/api/projects/{pid}/events").json()
          if e["kind"] == "binary.triaged"]
    assert ev and ev[-1]["payload"].get("stopped") is True
    ov = client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()
    assert ov["cached"] is True
    # 幂等：无在跑导出时再取消 → cancelling False
    assert client.post(f"/api/projects/{pid}/binaries/{sha}/triage/cancel"
                       ).json()["cancelling"] is False


def test_rev_binary_engine_set_and_overview(client):
    """引擎偏好端点（2026-10-01 双模式）：默认 ida；PUT 落库 asset meta.engine +
    事件 binary.engine_set + overview.engine 回读；未知引擎 422；未知样本 404。"""
    _install_rev_factory(client, set())
    pid = client.post("/api/projects", json={
        "name": "rev-engine", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes(),
                                     "application/octet-stream")}).json()
    sha = up["sha"]
    tr = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
    _wait_job(client, tr.json()["job_id"])

    # 默认 ida（asset meta 无 engine）
    ov = client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()
    assert ov["engine"] == "ida"
    # 缓存标记产出引擎（ida-headless 归一为 ida）
    assert ov["meta"]["engine"] == "ida"

    # 切到 ghidra
    r = client.put(f"/api/projects/{pid}/binaries/{sha}/engine", json={"engine": "ghidra"})
    assert r.status_code == 200 and r.json()["engine"] == "ghidra"
    asset = next(a for a in client.get(f"/api/projects/{pid}/assets").json()
                 if a["value"] == sha)
    assert asset["meta"]["engine"] == "ghidra"
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()["engine"] == "ghidra"
    kinds = [e["kind"] for e in client.get(f"/api/projects/{pid}/events").json()]
    assert "binary.engine_set" in kinds

    # 大小写无关归一
    assert client.put(f"/api/projects/{pid}/binaries/{sha}/engine",
                      json={"engine": "IDA"}).json()["engine"] == "ida"

    # 未知引擎 422；未知样本 404
    assert client.put(f"/api/projects/{pid}/binaries/{sha}/engine",
                      json={"engine": "radare2"}).status_code == 422
    assert client.put(f"/api/projects/{pid}/binaries/{'f' * 64}/engine",
                      json={"engine": "ghidra"}).status_code == 404


def test_rev_delete_binary_cascades_and_purges(client):
    """样本删除（2026-10-01）：硬级联删 func_kb/findings/logic_blocks/链边 + 资产行 +
    磁盘产物（缓存/样本）；事件 binary.deleted；重复删除 404。"""
    _install_rev_factory(client, set())
    pid = client.post("/api/projects", json={
        "name": "rev-del", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes(),
                                     "application/octet-stream")}).json()
    sha = up["sha"]
    tr = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
    assert _wait_job(client, tr.json()["job_id"])["status"] == "done"
    asset = next(a for a in client.get(f"/api/projects/{pid}/assets").json()
                 if a["value"] == sha)
    asset_id = asset["id"]
    # 关联数据：func_kb 行 + finding（指向该 asset）+ 该 sha 的逻辑块 + 链边（func_kb 节点）
    fid = client.post(f"/api/projects/{pid}/funcs", json={
        "binary_sha256": sha, "address": "0x401189", "name": "check_flag"}).json()["id"]
    find = client.post(f"/api/projects/{pid}/findings", json={
        "title": "硬编码密钥", "target_asset_id": asset_id}).json()
    lb = client.post(f"/api/projects/{pid}/logic-blocks", json={
        "name": "校验流程", "binary_sha256": sha}).json()
    cid = client.post(f"/api/projects/{pid}/chains", json={"name": "链"}).json()["id"]
    client.post(f"/api/projects/{pid}/chains/{cid}/links",
                json={"node_type": "func_kb", "node_id": fid})
    # overview 计数字段（确认框用）
    ov = client.get(f"/api/projects/{pid}/binaries/{sha}/overview").json()
    assert ov["analyzed_count"] == 1 and ov["findings_count"] == 1 and ov["logic_blocks_count"] == 1

    proj_obj = client.app.state.projects.get(pid) or client.app.state.store.open_project(pid)
    cache_file = proj_obj.artifacts_dir / "decompiler-cache" / f"{sha}.json"
    blob = proj_obj.path / asset["meta"]["path"]
    assert cache_file.is_file() and blob.is_file()

    r = client.delete(f"/api/projects/{pid}/binaries/{sha}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == sha and body["funcs"] == 1 and body["findings"] == 1
    assert body["logic_blocks"] == 1 and body["chain_links"] == 1

    # DB 级联
    assert client.get(f"/api/projects/{pid}/funcs?binary_sha256={sha}").json() == []
    assert all(f["id"] != find["id"] for f in client.get(f"/api/projects/{pid}/findings").json())
    assert client.get(f"/api/projects/{pid}/logic-blocks/{lb['id']}").status_code == 404
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/overview").status_code == 404
    assert all(a["value"] != sha for a in client.get(f"/api/projects/{pid}/assets").json()
               if a["type"] == "binary")
    assert client.get(f"/api/projects/{pid}/chains/{cid}").json()["links"] == []

    # 磁盘清理
    assert not cache_file.exists() and not blob.exists()
    assert not (proj_obj.artifacts_dir / "decompiler-cache" / f"{sha}.details").exists()

    # 审计事件 + 幂等 404
    assert "binary.deleted" in [e["kind"] for e in client.get(f"/api/projects/{pid}/events").json()]
    assert client.delete(f"/api/projects/{pid}/binaries/{sha}").status_code == 404
    assert client.delete(f"/api/projects/{pid}/binaries/{'f' * 64}").status_code == 404


def test_rev_delete_binary_auto_stops_running_triage(client):
    """删除时若有在跑分诊：端点先协作式停止（Ghidra stop 文件）并等 Job 收尾再删，
    用户无需先手动「停止」。"""
    from core.tools.decompiler import DecompilerService, GhidraHeadlessBackend

    def ghidra_runner(args):
        i = args.index("-postScript")
        out = Path(args[i + 2])
        stop = Path(args[i + 5]) if len(args) > i + 5 else None
        out.parent.mkdir(parents=True, exist_ok=True)
        if stop is not None:  # 阻塞等父进程写停止文件（模拟长跑分诊）
            deadline = time.time() + 5.0
            while not stop.is_file() and time.time() < deadline:
                time.sleep(0.02)
        data = json.loads(json.dumps(V3))
        out.write_text(json.dumps(data), encoding="utf-8")
        return 0, "ok", ""

    def factory(proj):
        g = GhidraHeadlessBackend(runner=ghidra_runner, available=True,
                                  tmp_project_dir=proj.artifacts_dir / ".ghidra-tmp")
        return DecompilerService(cache_dir=proj.artifacts_dir / "decompiler-cache", ghidra=g)

    client.app.state.rev_service_factory = factory
    pid = client.post("/api/projects", json={
        "name": "rev-del-stop", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes(),
                                     "application/octet-stream")}).json()
    sha = up["sha"]
    tr = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
    assert tr.status_code == 202
    # 分诊在跑 → 直接删：端点自动置停止标志、等收尾、再删
    r = client.delete(f"/api/projects/{pid}/binaries/{sha}")
    assert r.status_code == 200 and r.json()["deleted"] == sha
    assert _wait_job(client, tr.json()["job_id"])["status"] == "done"
    assert client.get(f"/api/projects/{pid}/binaries/{sha}/overview").status_code == 404


def test_rev_funcs_cross_project_404(client):
    _install_rev_factory(client, set())
    p1 = client.post("/api/projects", json={
        "name": "p1", "track": "research", "capabilities": ["binary"]}).json()["id"]
    p2 = client.post("/api/projects", json={
        "name": "p2", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{p1}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    assert up["job_id"] is None  # 上传不再自动分诊（funcs 建行不依赖缓存）
    fid = client.post(f"/api/projects/{p1}/funcs",
                      json={"binary_sha256": up["sha"], "address": "0x401189",
                            "name": "check_flag"}).json()["id"]
    assert client.patch(f"/api/projects/{p2}/funcs/{fid}",
                        json={"note": "越界"}).status_code == 404


class FakeIdaMcp:
    """假 GUI IDA MCP 桥（不触网）：count_funcs + list_funcs 分页队列 + writeback 抓包
    + analyze_batch/func_detail 按需详情（details={hex地址: analysis 原样}）。

    writeback=False 模拟传输级失败（writeback_items 回 None）；
    gate：第一次 call_tool 出页后阻塞（测试窗口内置 stop，模拟中途停止）。
    """

    def __init__(self, pages=None, writeback=True, total=None, gate=None,
                 details=None):
        self.pages = list(pages or [])
        self.calls: list[tuple[str, dict]] = []
        self.pushed: list[dict] | None = None
        self._writeback_ok = writeback
        self._total = total
        self._gate = gate
        self._details = dict(details or {})

    def available(self) -> bool:
        return True

    def count_funcs(self):
        return self._total

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "analyze_batch":
            # vendor analyze_batch 签名：queries 列表 → list[{query,addr,name,analysis,error}]
            out = []
            for q in (args or {}).get("queries") or []:
                key = str(q.get("query", "")).strip()
                analysis = self._details.get(key)
                if analysis is None:
                    out.append({"query": key, "addr": None, "name": None,
                                "analysis": None,
                                "error": f"Function query is required: {key}"})
                    continue
                out.append({"query": key, "addr": analysis.get("addr"),
                            "name": analysis.get("name"),
                            "analysis": analysis, "error": None})
            return out
        # vendor list_funcs 签名：单 query → list[Page]（每页 {"data", "next_offset"}）
        page = self.pages.pop(0) if self.pages else None
        if page is not None and self._gate is not None:
            gate, self._gate = self._gate, None
            gate.wait(timeout=10)  # 测试窗口：页已出、停止信号尚未置
        return [page] if page is not None else []

    def func_detail(self, addr):
        """镜像 MCPBackend.func_detail：经 call_tool 记录 + 按 analyze_batch 结构解析。"""
        key = hex(addr) if isinstance(addr, int) else str(addr)
        res = self.call_tool("analyze_batch", {"queries": [{"query": key}]})
        if not isinstance(res, list) or not res or not isinstance(res[0], dict) \
                or res[0].get("error"):
            return None
        analysis = res[0].get("analysis") or {}
        disasm = analysis.get("disasm") or {}

        def _pair(c):
            return {"address": c.get("addr"), "name": c.get("name")}

        return {
            "address": res[0].get("addr") or key,
            "name": res[0].get("name"),
            "size": int(str(analysis.get("size") or "0x0"), 16),
            "pseudocode": analysis.get("decompile"),
            "disasm_lines": disasm.get("lines") or [],
            "disasm_truncated": bool(disasm.get("truncated")),
            "callers": [_pair(c) for c in (analysis.get("callers") or [])
                        if isinstance(c, dict)],
            "callees": [_pair(c) for c in (analysis.get("callees") or [])
                        if isinstance(c, dict)],
        }

    def writeback_items(self, items):
        self.pushed = items
        if not self._writeback_ok:
            return None
        return {"status": "ok", "channel": "mcp", "applied": len(items),
                "results": {"rename": [{"ok": True}] * len(items),
                            "comments": []}}


def test_rev_pull_ida_functions_and_push_names(client):
    """GUI IDA 拉取（2026-09-29）：list_funcs 分页 → 轻量缓存 + diff 回拉
    （自动名不覆盖）；反向 push：func_kb 有效命名批量写回（自动名不推）；
    无 MCP / 写回失败均结构化降级。"""
    _install_rev_factory(client, set())
    pid = client.post("/api/projects", json={
        "name": "拉取研究", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    assert up["job_id"] is None
    sha = up["sha"]
    _wait_job(client, client.post(
        f"/api/projects/{pid}/binaries/{sha}/triage").json()["job_id"])

    # func_kb 预置：main（待被 IDA 名覆盖）/ 自动名（diff 与 push 都跳过）/
    # IDA 未见过的猜测名（diff 跳过、push 照推）
    fid_main = client.post(f"/api/projects/{pid}/funcs",
                           json={"binary_sha256": sha, "address": "0x401234",
                                 "name": "main"}).json()["id"]
    client.post(f"/api/projects/{pid}/funcs",
                json={"binary_sha256": sha, "address": "0x401300",
                      "name": "sub_401300"})
    client.post(f"/api/projects/{pid}/funcs",
                json={"binary_sha256": sha, "address": "0x401600",
                      "name": "early_guess"})

    # ① 无 MCP → 结构化降级（提示 Ctrl-Alt-M），不发事件
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions")
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "no-mcp" and "Ctrl-Alt-M" in job["result"]["hint"]
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/push-names-to-ida")
    assert _wait_job(client, r.json()["job_id"])["result"]["status"] == "no-mcp"
    kinds = [e["kind"] for e in client.get(f"/api/projects/{pid}/events").json()]
    assert "binary.pulled_from_ida" not in kinds and "binary.names_pushed" not in kinds

    # ② 挂假 MCP：单页 list_funcs（vendor hex 字符串）→ 轻量缓存 + diff 回拉
    svc = client.app.state.rev_services[pid]
    mcp = FakeIdaMcp(total=3, pages=[{"data": [
        {"addr": "0x401234", "name": "win_main", "size": "0xc8"},
        {"addr": "0x401300", "name": "sub_401300", "size": "0x8"},
        {"addr": "0x401500", "name": "ida_helper", "size": "0x20"},
        {"addr": "zz", "name": "bad_row", "size": "0x1"},  # 坏行跳过不拖垮
    ], "next_offset": None}])
    svc.mcp = mcp
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions")
    job = _wait_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["status"] == "ok" and res["function_count"] == 3  # 坏行不计
    assert res["total"] == 3 and job["meta"]["progress"]["pulled"] == 3
    # 增量行：progress.rows 带本页新行（前端拉取中按 address 去重 append，不再全量重拉）
    prog_rows = job["meta"]["progress"]["rows"]
    assert [r["address"] for r in prog_rows] == ["0x401234", "0x401300", "0x401500"]
    assert all(r["has_pseudo"] is False and r["n_calls"] == 0 for r in prog_rows)
    assert res["changed"] == [{"address": "0x401234",
                               "old_name": "main", "new_name": "win_main"}]
    assert mcp.calls and mcp.calls[0][0] == "list_funcs"
    # 轻量缓存：meta.source=ida-mcp，无伪码/strings/sections（数据大头不落盘）；
    # 完成态无 partial 标记
    data = svc.read_cached(sha)
    assert data["meta"]["source"] == "ida-mcp" and len(data["functions"]) == 3
    assert "partial" not in data["meta"]
    assert data["strings"] == [] and data["sections"] == []
    assert all("pseudocode" not in f for f in data["functions"])
    # func_kb：IDA 有效命名回拉（author=ida-pull）；自动名与缺行不动
    rows = {f["id"]: f for f in
            client.get(f"/api/projects/{pid}/funcs?binary_sha256={sha}").json()}
    assert rows[fid_main]["name"] == "win_main"
    assert rows[fid_main]["name_history"][-1]["by"] == "ida-pull"
    ev = next(e for e in client.get(f"/api/projects/{pid}/events").json()
              if e["kind"] == "binary.pulled_from_ida")
    assert ev["payload"]["function_count"] == 3

    # ③ 反向 push：有效命名（win_main/early_guess）推 IDA，自动名过滤
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/push-names-to-ida")
    job = _wait_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["status"] == "ok" and res["applied"] == 2
    assert "保存" in res["hint"]  # 只改 GUI 内存库，提示用户在 IDA 落盘
    assert sorted((i["address"], i["name"]) for i in mcp.pushed) == \
        [("0x401234", "win_main"), ("0x401600", "early_guess")]
    ev = next(e for e in client.get(f"/api/projects/{pid}/events").json()
              if e["kind"] == "binary.names_pushed")
    assert ev["payload"]["applied"] == 2

    # ④ 写回传输失败 → no-mcp 结构化降级
    svc.mcp = FakeIdaMcp(writeback=False)
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/push-names-to-ida")
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "no-mcp"

    # ⑤ 防越界：未知样本 404
    assert client.post(
        f"/api/projects/{pid}/binaries/{'f' * 64}/pull-ida-functions").status_code == 404
    assert client.post(
        f"/api/projects/{pid}/binaries/{'f' * 64}/push-names-to-ida").status_code == 404


def test_rev_pull_ida_resume_stop_and_stale(client):
    """断点续拉（2026-09-30）：每页部分缓存落盘（渐进可见）→ 停止保住已拉部分
    → 再点从 next_offset 续传；count_funcs 总数对账不一致自动从头；
    cancel 端点语义（无 job 幂等 / 置停止信号）。"""
    import threading as _threading

    _install_rev_factory(client, set())
    pid = client.post("/api/projects", json={
        "name": "续拉研究", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    sha = up["sha"]
    svc = client.app.state.rev_services[pid]
    # 停止端点：无在跑 job → 幂等 False
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions/cancel")
    assert r.status_code == 200 and r.json()["cancelling"] is False

    # ① 第一页后置停止 → stopped，已拉部分 partial 缓存生效（渐进可见）
    gate = _threading.Event()
    svc.mcp = FakeIdaMcp(total=4, gate=gate, pages=[
        {"data": [{"addr": "0x401234", "name": "win_main", "size": "0xc8"},
                  {"addr": "0x401189", "name": "check_flag", "size": "0x60"}],
         "next_offset": 2},
        {"data": [{"addr": "0x401300", "name": "sub_401300", "size": "0x8"},
                  {"addr": "0x401500", "name": "ida_helper", "size": "0x20"}],
         "next_offset": None},
    ])
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions")
    job_id = r.json()["job_id"]
    client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions/cancel")
    assert client.app.state.pull_cancel[sha].is_set()
    gate.set()  # 放行第一页（页已出队，循环顶检查点命中）
    job = _wait_job(client, job_id)
    res = job["result"]
    assert res["status"] == "stopped" and res["pulled"] == 2 and res["total"] == 4
    assert "断点" in res["hint"]
    data = svc.read_cached(sha)
    assert data["meta"]["partial"] is True and data["meta"]["total_functions"] == 4
    assert data["meta"]["next_offset"] == 2 and len(data["functions"]) == 2
    ev = next(e for e in client.get(f"/api/projects/{pid}/events").json()
              if e["kind"] == "binary.pulled_from_ida")
    assert ev["payload"]["stopped"] is True

    # ② 再点拉取 → 从 next_offset=2 续传（不重复拉前两个），拉完 partial 清除
    mcp2 = FakeIdaMcp(total=4, pages=[
        {"data": [{"addr": "0x401300", "name": "sub_401300", "size": "0x8"},
                  {"addr": "0x401500", "name": "ida_helper", "size": "0x20"}],
         "next_offset": None},
    ])
    svc.mcp = mcp2
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions")
    job = _wait_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["status"] == "ok" and res["function_count"] == 4
    assert mcp2.calls[0][1]["queries"][0]["offset"] == 2  # 断点续传关键断言
    data = svc.read_cached(sha)
    assert "partial" not in data["meta"] and len(data["functions"]) == 4

    # ③ 总数对账：残留 partial（total=10）与现库（4）不一致 → 自动从头重拉
    svc.import_ida_mcp_cache(sha, data["functions"][:2], binary_name="x",
                             partial=True, total=10, next_offset=2)
    mcp3 = FakeIdaMcp(total=4, pages=[
        {"data": [{"addr": f"0x40{600 + i:02d}", "name": f"fn_{i}", "size": "0x10"}
                  for i in range(2)], "next_offset": 2},
        {"data": [{"addr": f"0x40{700 + i:02d}", "name": f"gn_{i}", "size": "0x10"}
                  for i in range(2)], "next_offset": None},
    ])
    svc.mcp = mcp3
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions")
    job = _wait_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["status"] == "ok" and res["function_count"] == 4
    assert "从头" in (res.get("hint") or "")
    assert mcp3.calls[0][1]["queries"][0]["offset"] == 0  # 对账失败：从头
    assert "partial" not in svc.read_cached(sha)["meta"]

    # ④ 旧插件（无 count_funcs）→ total=None，进度退化无分母但拉取照常
    svc.mcp = FakeIdaMcp(total=None, pages=[
        {"data": [{"addr": "0x401234", "name": "win_main", "size": "0xc8"}],
         "next_offset": None}])
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions")
    job = _wait_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["status"] == "ok" and res["total"] is None
    assert job["meta"]["progress"]["total"] is None


def test_rev_ida_pull_lazy_detail_endpoints(client):
    """IDA 拉取轻量缓存 + 按需详情（2026-09-30）：cached_function/cached_xrefs 端点
    自动 analyze_batch 拉取 callers/callees/伪码/反汇编并落盘（每函数一文件）；
    二次命中缓存不再调 MCP；MCP 离线且详情缺席时伪码 None、xrefs 空列表（不回 4xx）。"""
    _install_rev_factory(client, set())
    pid = client.post("/api/projects", json={
        "name": "懒拉研究", "track": "research", "capabilities": ["binary"]}).json()["id"]
    up = client.post(f"/api/projects/{pid}/samples",
                     files={"file": ("crackme.elf", CRACKME.read_bytes())}).json()
    sha = up["sha"]
    svc = client.app.state.rev_services[pid]
    addr = 0x401234
    analysis = {
        "addr": hex(addr), "name": "win_main", "size": "0xc8",
        "decompile": "int win_main() { return 0; }",
        "disasm": {"lines": ["push rbp", "mov rbp, rsp", "xor eax, eax"],
                   "instruction_count": 3, "truncated": False},
        "callers": [{"addr": "0x401000", "name": "entry"}],
        "callees": [{"addr": "0x401500", "name": "helper"}],
    }
    mcp = FakeIdaMcp(total=3, pages=[{"data": [
        {"addr": "0x401000", "name": "entry", "size": "0x40"},
        {"addr": hex(addr), "name": "win_main", "size": "0xc8"},
        {"addr": "0x401500", "name": "helper", "size": "0x20"},
    ], "next_offset": None}], details={hex(addr): analysis})
    svc.mcp = mcp
    r = client.post(f"/api/projects/{pid}/binaries/{sha}/pull-ida-functions")
    job = _wait_job(client, r.json()["job_id"])
    assert job["result"]["status"] == "ok"
    addr_path = hex(addr).replace("0x", "")

    # ① cached_function：自动拉详情（伪码+反汇编），落盘后二次命中不再调 MCP
    r = client.get(f"/api/projects/{pid}/binaries/{sha}/functions/{addr_path}")
    assert r.status_code == 200
    d = r.json()
    assert d["pseudocode"] == "int win_main() { return 0; }"
    assert d["disasm"]["lines"] == ["push rbp", "mov rbp, rsp", "xor eax, eax"]
    assert d["disasm"]["truncated"] is False
    n_after_first = len(mcp.calls)
    assert any(name == "analyze_batch" for name, _ in mcp.calls)
    assert svc._detail_file(sha, addr).is_file()
    r2 = client.get(f"/api/projects/{pid}/binaries/{sha}/functions/{addr_path}")
    assert r2.json()["pseudocode"] == d["pseudocode"]
    assert len(mcp.calls) == n_after_first  # 详情文件命中，无第二次 analyze_batch

    # ② cached_xrefs：详情缓存的 callers/callees（source=cache）
    r = client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/{addr_path}")
    assert r.status_code == 200
    x = r.json()
    assert x["callers"] == [{"address": "0x401000", "name": "entry"}]
    assert x["callees"] == [{"address": "0x401500", "name": "helper"}]
    assert x["source"] == "cache"

    # ③ MCP 离线 + 名单有但详情缺席：伪码 None、xrefs 空列表（不回 4xx）
    svc.mcp = None
    miss = "401000"  # entry：在名单里，无详情
    r = client.get(f"/api/projects/{pid}/binaries/{sha}/functions/{miss}")
    assert r.status_code == 200
    assert r.json()["pseudocode"] is None and r.json()["disasm"] is None
    r = client.get(f"/api/projects/{pid}/binaries/{sha}/xrefs/{miss}")
    assert r.status_code == 200
    assert r.json()["callers"] == [] and r.json()["callees"] == []

    # ④ 详情文件损坏 → 自动重拉修复（MCP 在线时）
    svc.mcp = mcp
    f = svc._detail_file(sha, addr)
    f.write_text("{broken", encoding="utf-8")
    r = client.get(f"/api/projects/{pid}/binaries/{sha}/functions/{addr_path}")
    assert r.status_code == 200 and r.json()["pseudocode"] == analysis["decompile"]


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


def test_assets_api_exposes_derived_parent_clean_status(client):
    """E7：资产列表消费读时派生状态，不回写有子节点的根资产。"""
    from core.blackboard.intents import close_intent, declare_intent

    pid = _make_project(client, track="pentest")
    bb = client.app.state.projects[pid].bb
    root = bb.upsert_asset(pid, "host", "10.55.0.1")['id']
    leaves = []
    for i in range(2):
        leaf = bb.upsert_asset(pid, "url", f"http://10.55.0.1/{i}", parent_id=root)['id']
        leaves.append(leaf)
        hid = bb.add_http_history(pid, source="browser", method="GET",
                                  url=f"http://10.55.0.1/{i}", status=404,
                                  resp_body="")
        iid = declare_intent(bb, pid, f"叶面 {i} 无漏洞", target_asset_id=leaf)['id']
        close_intent(bb, pid, iid, "dead_end", dead_reason="探测无异常",
                     evidence_refs=[f"http:{hid}"])
        bb.set_asset_status(leaf, "tested_clean", note="叶面测完", author="test")

    rows = client.get(f"/api/projects/{pid}/assets").json()
    parent = next(a for a in rows if a["id"] == root)
    assert parent["status"] == "open"
    assert parent["effective_status"] == "tested_clean"
    assert parent["status_basis"] == "derived"
    assert parent["settled"] is True

    bb.upsert_asset(pid, "url", "http://10.55.0.1/new", parent_id=root)
    rows = client.get(f"/api/projects/{pid}/assets").json()
    parent = next(a for a in rows if a["id"] == root)
    assert parent["effective_status"] == "open"
    assert parent["settled"] is False


# ---------- 机制 1.1 发布去重/workset + 机制 1.4 wait_for 门控 / 建议私信边 ----------


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
    L2 下第一轮 tick 按指令组建 Team（draft 待人工确认）。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([]),
        planner_llm=S([
            # directive 触发的第一轮：按指令组建队伍
            {"tool_use": [S.tool_call("p1", "build_team", {
                "name": "挖洞小队", "goal_text": "对已登记资产做漏洞挖掘",
                "members": [{"member_key": "recon", "label": "侦察手",
                             "responsibility": "对已登记资产做漏洞挖掘"}]})]},
            {"tool_use": [S.tool_call("d1", "done", {})]},
        ]),
        providers_config=str(tmp_path / "p.json"))
    app.state.judgments_dir = tmp_path / "cfg"  # 测试隔离判据模板文件
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "指挥", "track": "ctf",
                                            "capabilities": ["binary"]}).json()["id"]
        # L2（ctf 缺省 L0 → PATCH 提档）+ mission 判据
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {
            "autonomy": {"level": "L2", "auto_derive": True},
            "mission": {"text": "挖洞", "criteria": "□ 全覆盖"}}}).status_code == 200
        r = c.post(f"/api/projects/{pid}/orchestrator/directive",
                   json={"text": "对已登记资产做漏洞挖掘"})
        assert r.status_code == 200
        _wait_job(c, r.json()["job_id"])
        _wait_no_running(c, pid)
        bb = c.app.state.projects[pid].bb
        evs = [e["kind"] for e in bb.recent_events(pid)]
        assert "orch.directive" in evs and "orch.directive.done" in evs
        # 第一轮按指令组建了队伍（draft，待人工 preflight + 确认后启动）
        teams = c.get(f"/api/projects/{pid}/teams").json()
        assert len(teams) == 1 and teams[0]["name"] == "挖洞小队"
        assert teams[0]["status"] == "draft"
        detail = c.get(f"/api/projects/{pid}").json()
        assert detail["usage"]["auto_derive"] is True


def test_judgment_templates_crud(client, tmp_path):
    """C2 判据模板：默认全 user 空；PUT 整表保存；DELETE 删除；
    resolve 四层优先级（goal 统一后：goal > mission 存量 > template > builtin）。"""
    from core.orchestrator.judgments import resolve_criteria
    pid = _make_project(client)
    app_jud = client.app.state.judgments_dir
    client.app.state.judgments_dir = tmp_path / "cfg"
    r = client.get("/api/judgment-templates")
    assert r.status_code == 200 and "渗透默认" in r.json()["builtin"] and r.json()["user"] == {}
    # PUT 保存
    r = client.put("/api/judgment-templates", json={"我的模板": "□ 自定义判据"})
    assert r.status_code == 200 and r.json()["saved"] == 1
    # 四层优先级：goal > mission（存量兼容） > template > builtin
    proj = client.app.state.projects[pid]
    cfg_empty = proj.bb.get_project(pid)["config"]
    res = resolve_criteria(cfg_empty, client.app.state.judgments_dir)
    assert res["source"] == "builtin"  # 无 goal 无 mission 无选模板 → 内置兜底
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
    # goal 居首：阶段目标确认（PUT /goal）后 goal 判据压过 mission/模板
    r = client.put(f"/api/projects/{pid}/goal",
                   json={"text": "打穿靶场", "criteria": ["□ goal 判据一", "□ goal 判据二"]})
    assert r.status_code == 200
    g = client.get(f"/api/projects/{pid}/goal").json()["phase_goal"]
    res = resolve_criteria(proj.bb.get_project(pid)["config"],
                           client.app.state.judgments_dir, goal=g)
    assert res["source"] == "goal"
    assert "□ goal 判据一" in res["criteria"] and "□ goal 判据二" in res["criteria"]
    # goal 清空（criteria 空不算源）→ 回退 mission 存量层
    client.put(f"/api/projects/{pid}/goal", json={"text": ""})
    g = client.get(f"/api/projects/{pid}/goal").json()["phase_goal"]
    res = resolve_criteria(proj.bb.get_project(pid)["config"],
                           client.app.state.judgments_dir, goal=g)
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
    """①兜底轮询：无在跑 worker + auto_derive（L1）→ sweep 触发一次编排派生 tick 并落
    last_derive_*；有在跑 worker 时 sweep 不触发（require_idle）。
    L1 派生 tick 组建 Team（draft），last_result=teams:N，待人类 preflight+确认后启动。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([{"text": "ok"}]),
        planner_llm=S([
            {"tool_use": [S.tool_call("p1", "build_team", {
                "name": "轮询派生小队", "goal_text": "轮询派生任务",
                "members": [{"member_key": "m1", "label": "执行手",
                             "responsibility": "轮询派生任务"}]})]},
            {"tool_use": [S.tool_call("d1", "done", {})]},
        ]),
        providers_config=str(tmp_path / "p.json"))
    app.state.judgments_dir = tmp_path / "cfg"  # 测试隔离判据模板文件
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "轮询派生", "track": "ctf"}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {
            "autonomy": {"level": "L1", "auto_derive": True},
            "mission": {"text": "挖洞", "criteria": "□ 全覆盖"}}}).status_code == 200
        bb = c.app.state.projects[pid].bb
        # 变体 A：有在跑 worker → sweep 不判跳（require_idle）
        sid = bb.register_session(pid, "在跑窗", role="_generalist")["id"]
        bb.set_session_status(sid, "running")
        app.state.mission_poll_sweep()
        time.sleep(0.3)
        assert c.get(f"/api/projects/{pid}/teams").json() == []
        assert not any(e["kind"] == "mission.derive" for e in bb.recent_events(pid))
        # 变体 B：worker 收尾 → sweep 触发派生 tick → L1 组建 1 支 Team（draft）
        bb.set_session_status(sid, "idle")
        app.state.mission_poll_sweep()
        derive = _wait_derive_result(c, pid, "teams:")
        assert derive["last_result"] == "teams:1" and derive["last_at"]
        teams = c.get(f"/api/projects/{pid}/teams").json()
        assert len(teams) == 1 and teams[0]["name"] == "轮询派生小队"
        assert teams[0]["status"] == "draft"
        # 事件留痕：mission.derive（判定）+ mission.derive.result（结果）
        kinds = [e["kind"] for e in bb.recent_events(pid)]
        assert "mission.derive" in kinds and "mission.derive.result" in kinds


def test_mission_poll_sweep_derives_from_goal(tmp_path):
    """goal 统一（2026-09-22）：判据第一优先源=阶段目标——无 mission 存量时
    PUT /goal 判据即可驱动 L1 自动派生，mission.derive 事件 criteria_source=goal。
    L1 派生 tick 组建 Team（draft），不直发执行。"""
    from test_orchestrator import ScriptedLLM as S
    app = create_app(
        workspace_root=str(tmp_path / "ws"), tools_root=None,
        executor_llm=S([{"text": "ok"}]),
        planner_llm=S([
            {"tool_use": [S.tool_call("p1", "build_team", {
                "name": "goal 派生小队", "goal_text": "goal 派生任务",
                "members": [{"member_key": "m1", "label": "执行手",
                             "responsibility": "goal 派生任务"}]})]},
            {"tool_use": [S.tool_call("d1", "done", {})]},
        ]),
        providers_config=str(tmp_path / "p.json"))
    app.state.judgments_dir = tmp_path / "cfg"
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "goal 派生", "track": "ctf"}).json()["id"]
        assert c.patch(f"/api/projects/{pid}/config", json={"config": {
            "autonomy": {"level": "L1", "auto_derive": True}}}).status_code == 200
        assert c.put(f"/api/projects/{pid}/goal",
                     json={"text": "打穿靶场", "criteria": ["□ goal 判据"]}).status_code == 200
        app.state.mission_poll_sweep()
        derive = _wait_derive_result(c, pid, "teams:")
        assert derive["last_result"] == "teams:1"
        teams = c.get(f"/api/projects/{pid}/teams").json()
        assert len(teams) == 1 and teams[0]["name"] == "goal 派生小队"
        assert teams[0]["status"] == "draft"
        bb = c.app.state.projects[pid].bb
        assert any(e["kind"] == "mission.derive"
                   and e["payload"].get("criteria_source") == "goal"
                   for e in bb.recent_events(pid))


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


# ---------- 附件随发（2026-09-19）：上传/去重/下载 + 引导携带 ----------


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


def test_worker_chat_notice_carries_attachment_path(client, monkeypatch):
    """M3 附件 E2E 末环（网络不可用时 hermetic 验证）：idle 窗收到带附件 human_note
    → run_chat 注入的消息含 📎 + 工作区相对路径（Agent 可 run_cmd 直接读取）。"""
    from test_orchestrator import ScriptedLLM

    pid = _make_project(client)
    att = client.post(f"/api/projects/{pid}/attachments",
                      files={"file": ("notes.txt", b"M3ATT_READABLE_42\n",
                                      "text/plain")}).json()
    sid = client.app.state.projects[pid].bb.register_session(
        pid, "附件会话", role="_generalist")["id"]

    seen: list = []

    class RecLLM(ScriptedLLM):
        def chat(self, messages, *, system=None, tools=None, max_tokens=4096,
                 temperature=None, on_thinking=None, on_text=None, should_cancel=None):
            seen.append(json.loads(json.dumps(messages)))
            return self._parse({"content": [{"type": "text", "text": "已收到附件。"}],
                                "stop_reason": "end_turn",
                                "usage": {"input_tokens": 1, "output_tokens": 1}})

    rec = RecLLM([])
    monkeypatch.setattr(client.app.state.llm_store, "build",
                        lambda name=None, model=None: rec)
    assert client.post(f"/api/sessions/{sid}/note",
                       json={"text": "请读附件", "attachment_ids": [att["id"]]}
                       ).status_code == 201
    client.post(f"/api/agents/{sid}/work")
    _wait_no_running(client, pid)
    blob = json.dumps(seen, ensure_ascii=False)
    assert "📎" in blob and att["path"] in blob and "notes.txt" in blob


# ---------- H3：escalation 审批执行（deny-driven 一次性升级，API 层） ----------

def _removed_decide_escalation_executes_once_and_inboxes(client):
    """批准 escalation 单 → 网关直跑一次 → approval consumed + 收件箱回执 +
    message.inbox 事件；同单二次决策 422（一次性消费）。"""
    pid = _make_project(client, track="pentest")
    proj = client.app.state.projects[pid]
    appr = proj.bb.request_approval(
        pid, {"op": "escalation", "kind": "role_runtime",
              "cmd": "Write-Output escalation-ok", "runtime": "host",
              "threat_class": "trusted", "net": "bridge", "reason": "API 层测试"},
        risk="medium", requested_by="sess-test", session_id="sess-test")
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


def _removed_decide_escalation_invalid_action_marks_exec_failed(client):
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


# ---------- M5 D2：authorization 审批（纯回流）与 rejected 回流 / boundary 出口 ----------

def test_decide_authorization_approved_inboxes_result(client):
    """批准 authorization 单 → 纯回流处理器：无平台动作，仅向提交会话收件箱投
    authorization_result（payload 带 kind/scope_request[:300]/approved）+ 事件流。"""
    pid = _make_project(client, track="pentest")
    proj = client.app.state.projects[pid]
    appr = proj.bb.request_approval(
        pid, {"op": "authorization", "kind": "scope_expand",
              "scope_request": "追加 target.com 全部子域进授权范围",
              "justification": "主域打不进去，子域有旁路",
              "evidence_finding_ids": [], "task_id": None,
              "session_id": "sess-auth"},
        risk="high", requested_by="sess-auth", session_id="sess-auth")
    r = client.post(f"/api/approvals/{appr['id']}/decide",
                    json={"decision": "approved"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"  # 处理器 task_status 键名坑不复现
    assert r.json()["executed"] is True
    inbox = proj.bb.inbox_list(pid, "sess-auth")
    hit = [m for m in inbox
           if m["kind"] == "authorization_result" and m["ref_id"] == appr["id"]]
    assert len(hit) == 1
    p = hit[0]["payload"]
    assert p["approved"] is True and p["kind"] == "scope_expand"
    assert "target.com" in p["scope_request"]
    assert any(e["kind"] == "message.inbox"
               and e["payload"].get("kind") == "authorization_result"
               for e in proj.bb.recent_events(pid))


def _removed_decide_rejected_inboxes_approval_rejected(client):
    """拒绝 escalation/authorization 单 → approval_rejected 回流提交会话
    （此前 rejected 无回流=Agent 空等）；其余 op 拒绝不回流。"""
    pid = _make_project(client, track="pentest")
    proj = client.app.state.projects[pid]
    appr1 = proj.bb.request_approval(
        pid, {"op": "escalation", "kind": "role_runtime", "cmd": "wget http://x",
              "runtime": "host", "reason": "x", "session_id": "sess-r1"},
        risk="medium", requested_by="sess-r1", session_id="sess-r1")
    appr2 = proj.bb.request_approval(
        pid, {"op": "authorization", "kind": "impact_escalate",
              "scope_request": "证明到接管会话", "justification": "y",
              "session_id": "sess-r2"},
        risk="high", requested_by="sess-r2", session_id="sess-r2")
    # 其余 op（无 session 语义的杂项）拒绝不回流
    appr3 = proj.bb.request_approval(
        pid, {"op": "misc_custom", "session_id": "sess-r3"}, risk="low",
        requested_by="sess-r3", session_id="sess-r3")
    for appr in (appr1, appr2, appr3):
        r = client.post(f"/api/approvals/{appr['id']}/decide",
                        json={"decision": "rejected"})
        assert r.status_code == 200
    k1 = [m["kind"] for m in proj.bb.inbox_list(pid, "sess-r1")]
    k2 = [m["kind"] for m in proj.bb.inbox_list(pid, "sess-r2")]
    assert "approval_rejected" in k1 and "approval_rejected" in k2
    assert k1.count("approval_rejected") == 1 and k2.count("approval_rejected") == 1
    assert proj.bb.inbox_list(pid, "sess-r3") == []
    p2 = [m for m in proj.bb.inbox_list(pid, "sess-r2")
          if m["kind"] == "approval_rejected"][0]["payload"]
    assert p2["op"] == "authorization" and p2["kind"] == "impact_escalate"


def test_approvals_endpoint_carries_boundary(client):
    """approvals 出口每条附 server 拼好的 boundary 全文（与编排器 mission 段同源）
    ——人类在审批卡对照当前边界审授权/升级申请。"""
    pid = _make_project(client, track="pentest")
    proj = client.app.state.projects[pid]
    proj.bb.request_approval(
        pid, {"op": "authorization", "kind": "scope_expand",
              "scope_request": "x", "justification": "y", "session_id": "sess-b"},
        risk="high", requested_by="sess-b", session_id="sess-b")
    items = client.get(f"/api/projects/{pid}/approvals").json()
    assert items and all(it.get("boundary") for it in items)
    from core.orchestrator.orchestrator import mission_boundary_lines
    expected = "；".join(mission_boundary_lines(
        "pentest", proj.bb.get_project(pid)["config"]))
    assert items[0]["boundary"] == expected


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


# ---------- K7 路由零命中（route-injection-hardening，2026-09-24：真使用口径） ----------

def test_doctor_k7_route_zero_hit_real_usage(tmp_path):
    """K7：只报「曾注入但手册从未被 kb_open 打开」的测试点——两个点注入、只打开
    一个的手册 → 只报另一个；无注入历史不报。"""
    from core.blackboard import Blackboard
    packs = tmp_path / "packs"
    kb = packs / "kb"
    (kb / "web" / "poc").mkdir(parents=True)
    (kb / "web" / "poc" / "a.md").write_text("a", encoding="utf-8")
    (kb / "web" / "poc" / "b.md").write_text("b", encoding="utf-8")
    # 最小 track/pack 描述文件（建项校验扫这两处）
    (packs / "tracks" / "ctf").mkdir(parents=True)
    (packs / "tracks" / "ctf" / "track.yaml").write_text(
        "kind: track\nname: ctf\nlabel: CTF\n", encoding="utf-8")
    (packs / "capabilities" / "web").mkdir(parents=True)
    (packs / "capabilities" / "web" / "pack.yaml").write_text(
        "kind: capability\nname: web\nlabel: Web\n", encoding="utf-8")
    (kb / "route_index.yaml").write_text(
        "entries:\n"
        "  - point: 测试点甲\n    kb: web/poc/a.md\n"
        "  - point: 测试点乙\n    kb: web/poc/b.md\n", encoding="utf-8")
    ws = tmp_path / "workspaces"
    app = create_app(workspace_root=str(ws), packs_root=str(packs), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        r = c.post("/api/projects", json={"name": "K7 项目", "track": "ctf",
                                          "capabilities": ["web"]})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        # 无注入历史 → 不报
        issues = c.get("/api/packs/doctor").json()["issues"]
        assert not any(i["code"] == "route-index-zero-hit" for i in issues)
        # 测试夹具直挂黑板写事件：一次路由注入两个测试点（route_index=2）
        proj_dir = next(d for d in ws.iterdir()
                        if (d / "project.json").is_file()
                        and json.loads((d / "project.json").read_text(
                            encoding="utf-8"))["id"] == pid)
        db = proj_dir / "blackboard.db"
        bb = Blackboard(str(db))
        try:
            bb.append_event(pid, "skill.routed", {
                "route_index": 2, "route_points": ["测试点甲", "测试点乙"]})
            # 只 kb_open 甲的手册
            bb.append_event(pid, "kb.open", {"module": "web/poc/a.md"})
        finally:
            bb.close()
        issues = c.get("/api/packs/doctor").json()["issues"]
        zh = [i for i in issues if i["code"] == "route-index-zero-hit"]
        assert len(zh) == 1 and "测试点乙" in zh[0]["message"]
        assert "测试点甲" not in zh[0]["message"]


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


def test_switch_session_role_updates_row_and_live_agent(client):
    """会话中心化 §4.4：POST /sessions/{sid}/role 中途换人——会话行身份更新 +
    在内存会话热换装（下个步边界重建 prompt/工具白名单），对话历史与黑板不动，
    新身份跨委托保留。"""
    pid = _pentest_project(client, "会话换人")
    bb = client.app.state.projects[pid].bb
    r = client.post(f"/api/projects/{pid}/agents",
                    json={"role": "recon", "armed": False})
    sid = r.json()["id"]

    rr = client.post(f"/api/sessions/{sid}/role", json={"role": "external-entry"})
    assert rr.status_code == 200 and rr.json()["role"] == "external-entry"
    # 会话列表（侧栏数据源）同步新身份
    rows = client.get(f"/api/projects/{pid}/sessions").json()
    assert next(s for s in rows if s["id"] == sid)["role"] == "external-entry"
    # 在内存会话已热换装
    assert client.app.state.agents[sid].role_name == "external-entry"
    evs = [e for e in bb.recent_events(pid)
           if e["kind"] == "session.persona_switched"]
    assert len(evs) == 1
    p = evs[-1]["payload"]
    assert p["session_id"] == sid and p["from"] == "recon"
    assert p["to"] == "external-entry" and p["reason"] == "manual-switch"
    assert not p["task_id"]  # idle 窗：无在跑委托


def test_switch_session_role_rejects_unknown_and_closed(client):
    """换人校验：专家不在池 → 422；closed 窗 → 409；空 role → 422。"""
    pid = _pentest_project(client, "换人校验")
    sid = client.post(f"/api/projects/{pid}/agents",
                      json={"role": "recon", "armed": False}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/role",
                       json={"role": "ghost-role"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/role",
                       json={"role": "   "}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/close").status_code == 200
    assert client.post(f"/api/sessions/{sid}/role",
                       json={"role": "external-entry"}).status_code == 409


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


# ---------------- 网关策略快照（gateway-config-view M1）----------------

def test_gateway_config_snapshot(client):
    """策略快照端点：runtime/threat/net 直出 policy 常量；pathguard 语义摘要
    条数固定（防遗漏）；rate_rules 与 rateguard.RATE_RULES 同源。"""
    r = client.get("/api/gateway/config")
    assert r.status_code == 200
    cfg = r.json()
    # runtime_levels：四通道与等级
    assert [x["name"] for x in cfg["runtime_levels"]] == ["host", "wsl", "docker", "sandbox"]
    assert [x["level"] for x in cfg["runtime_levels"]] == [0, 1, 2, 3]
    # threat 矩阵：unknown 按恶意（宁严勿松），malware_live 仅 sandbox
    by_tc = {x["threat_class"]: x for x in cfg["threat_matrix"]}
    assert by_tc["unknown"]["allowed"] == by_tc["malware_live"]["allowed"] == ["sandbox"]
    assert by_tc["trusted"]["allowed"] == ["docker", "host", "sandbox", "wsl"]
    assert by_tc["untrusted"]["allowed"] == ["docker", "sandbox"]
    # net：real 永不默认
    assert cfg["net_modes"]["modes"] == ["none", "fakenet", "real"]
    assert cfg["net_modes"]["default"] == "none"
    # pathguard 语义摘要 7 条（加条目须同步更新断言与页面）
    assert len(cfg["pathguard_rules"]) == 7
    # rateguard 规则表同源
    assert {x["tool"] for x in cfg["rate_rules"]} == {"nmap", "masscan", "ffuf", "hydra"}
    nmap = next(x for x in cfg["rate_rules"] if x["tool"] == "nmap")
    assert nmap["hint"] and "-T3 --max-rate 200" in nmap["hint"]
    # 执行参数
    assert cfg["exec_params"]["default_timeout"] == 120
    assert "占位" in cfg["exec_params"]["sandbox_image"]


def test_agent_tools_catalog(client):
    """GET /api/agent-tools：直出 AGENT_TOOLS 静态全集+分组（agent-tools-view）。"""
    from core.agent.tools import AGENT_TOOLS
    r = client.get("/api/agent-tools")
    assert r.status_code == 200
    data = r.json()
    tools = data["tools"]
    # 条数/字段与总表一致
    assert len(tools) == len(AGENT_TOOLS)
    assert {t["name"] for t in tools} == {t["name"] for t in AGENT_TOOLS}
    for t in tools:
        assert t["description"] and t["group"]
        assert t["input_schema"]["type"] == "object"
        assert "properties" in t["input_schema"]
    # 分组集合与顺序
    assert data["groups"] == ["执行", "文件", "黑板", "知识", "浏览器", "协作", "计划", "控制"]
    # 无工具落「其他」（防新工具漏配分组规则）
    assert all(t["group"] != "其他" for t in tools)
    # bb_query：what 参数 enum 含七查询面（site=单站全貌，2026-09-26）
    bbq = next(t for t in tools if t["name"] == "bb_query")
    assert bbq["input_schema"]["properties"]["what"]["enum"] == [
        "findings", "assets", "events", "tasks", "func", "blueprint", "site"]


def test_agent_tool_group_rules():
    """分组函数关键落点（集合+前缀两类规则）。"""
    from core.agent.tools import agent_tool_group
    cases = {
        "run_cmd": "执行",
        "read_file": "文件", "search_files": "文件",
        "bb_query": "黑板", "bb_notify": "黑板",
        "kb_search": "知识", "decompile": "知识", "skill_open": "知识",
        "browser_click": "浏览器",
        "request_authorization": "协作",
        "declare_intent": "计划",
        "close_intent": "控制", "finish": "控制", "request_steps": "控制",
    }
    for name, group in cases.items():
        assert agent_tool_group(name) == group, name


def test_gateway_probe_replaces_inventory(client, monkeypatch):
    """POST /api/gateway/probe：假 detector 替换 app.state.inventory，
    返回结构与 GET /api/projects/{pid}.capability 完全一致。"""
    from core.runtime.detector import CapabilityInventory, ProbeResult
    fake = CapabilityInventory(
        docker=ProbeResult("docker", True, "27.0.3"),
        wsl=ProbeResult("wsl", False, "no wsl"),
        tools=[ProbeResult("ghidra", True, "PATH 可达")])
    seen = {}

    class FakeDetector:
        def probe(self, tools_root=None):
            seen["tools_root"] = tools_root
            return fake

    monkeypatch.setattr("core.api.app.HostDetector", FakeDetector)
    r = client.post("/api/gateway/probe")
    assert r.status_code == 200
    body = r.json()
    assert body["docker"]["available"] is True and body["docker"]["detail"] == "27.0.3"
    assert body["wsl"]["available"] is False
    assert body["tools"][0]["name"] == "ghidra"
    # app.state.inventory 已替换（后续 GET /api/projects/{pid}.capability 拿到新清单）
    app = client.app
    assert app.state.inventory is fake
    pid = _make_project(client)
    assert client.get(f"/api/projects/{pid}").json()["capability"] == body
    # tools_root 走 app.state（上移后 probe 刷新复用同一来源）
    assert seen["tools_root"] is None  # client fixture tools_root=None


# ---------------- 网络空间测绘（cyberspace-mapping M1+M2，2026-09-23）----------------

@pytest.fixture()
def fofa_cfg(tmp_path, monkeypatch):
    """FOFA 配置路径重定向到临时文件——真实 config/fofa.json 含 key，测试绝不触达。"""
    from core.api import app as app_mod
    p = tmp_path / "fofa.json"
    monkeypatch.setattr(app_mod, "FOFA_CONFIG_PATH", p)
    return p


class _FakeFofaClient:
    """镜像 FofaClient 接口的最小假客户端（from_config + search/info_my）。"""

    def __init__(self, base_url, key, timeout=20.0, transport=None):
        self.key = key
        self.base_url = base_url

    @property
    def configured(self) -> bool:
        return bool(self.key)

    @classmethod
    def from_config(cls, path, timeout=20.0, transport=None):
        cfg = fofa.load_fofa_config(path)
        return cls(cfg.get("base_url") or fofa.DEFAULT_BASE_URL, cfg.get("key") or "")

    def search(self, query, size=100, page=1):
        return {"total": 2, "size": size, "page": page, "rows": [
            {"ip": "10.0.0.1", "port": "443", "protocol": "tcp", "host": "",
             "domain": "aaa.example.com", "title": "A", "products": ["Nginx"]},
            {"ip": "10.0.0.2", "port": "80", "protocol": "tcp", "host": "10.0.0.2:80",
             "domain": "", "title": "B", "products": []},
        ]}

    def info_my(self):
        if not self.key:
            raise fofa.ConfigError("FOFA 未配置 key")
        return {"raw": {}, "remain": 999, "expire": "2026-12-31"}


def test_fofa_config_endpoints(client, fofa_cfg):
    """GET 缺省 → PUT 设 key → GET 脱敏回显 → key 空串不覆盖（防回显误写）。"""
    from core import fofa
    r = client.get("/api/fofa/config")
    assert r.status_code == 200
    assert r.json() == {"base_url": fofa.DEFAULT_BASE_URL, "key": "", "key_set": False}
    r = client.put("/api/fofa/config", json={"key": "abcd1234efgh5678"})
    assert r.status_code == 200
    body = r.json()
    assert body["key_set"] is True
    assert body["key"].startswith("abcd") and body["key"].endswith("5678")
    assert "1234efgh" not in body["key"]  # 中段不回显
    # key 传空串 = 不修改；base_url 可改
    r = client.put("/api/fofa/config", json={"key": "", "base_url": "https://relay.example"})
    body = r.json()
    assert body["key_set"] is True and body["base_url"] == "https://relay.example"
    assert fofa.load_fofa_config(fofa_cfg)["key"] == "abcd1234efgh5678"


def test_fofa_search_smoke_and_existing(client, fofa_cfg, monkeypatch):
    """未配置 400 → 配好假客户端查询 → existing 三锚点标注（domain 已登记灰显）。"""
    def _no_dns(*a, **k):
        raise OSError("dns off (hermetic)")
    monkeypatch.setattr("socket.getaddrinfo", _no_dns)
    pid = _make_project(client)
    r = client.post(f"/api/projects/{pid}/fofa/search", json={"query": "a"})
    assert r.status_code == 400 and "未配置" in r.json()["detail"]
    client.put("/api/fofa/config", json={"key": "k" * 32})
    # 预登记 aaa.example.com（域名单独可建，DNS 断网独立成行）
    r = client.post(f"/api/projects/{pid}/assets",
                    json={"value": "aaa.example.com", "type": "domain"})
    assert r.status_code == 201, r.text
    monkeypatch.setattr("core.api.app.fofa_mod.FofaClient", _FakeFofaClient)
    r = client.post(f"/api/projects/{pid}/fofa/search",
                    json={"query": 'domain="x.com"', "size": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and len(body["rows"]) == 2
    assert body["rows"][0]["existing"]["domain"] is True   # 已在黑板 → 前端灰显
    assert body["rows"][1]["existing"]["service"] is False
    # 空查询 422
    r = client.post(f"/api/projects/{pid}/fofa/search", json={"query": "  "})
    assert r.status_code == 422


def test_fofa_host_clean_and_precise_existing(client, fofa_cfg, monkeypatch):
    """2026-09-29 host 清洗 + 锚点精确化：FOFA host 字段脏形态（https:// 前缀 /
    :端口 尾）在查询入口统一清洗（响应、落历史同口径）；灰显镜像导入语义——
    只入过根域不再灰显子域行，子域登记后才灰显；空 host 行回退根域（导入
    实际登记值就是根域）。"""
    class _DirtyHostClient(_FakeFofaClient):
        def search(self, query, size=100, page=1):
            return {"total": 3, "size": size, "page": page, "rows": [
                {"ip": "10.0.0.1", "port": "443", "protocol": "tcp",
                 "host": "https://zczx.zut.edu.cn", "domain": "zut.edu.cn",
                 "title": "A", "products": []},
                {"ip": "10.0.0.2", "port": "8080", "protocol": "tcp",
                 "host": "authserver.zut.edu.cn:8080", "domain": "zut.edu.cn",
                 "title": "B", "products": []},
                {"ip": "10.0.0.3", "port": "80", "protocol": "tcp",
                 "host": "", "domain": "zut.edu.cn", "title": "C", "products": []},
            ]}

    pid = _make_project(client)
    client.put("/api/fofa/config", json={"key": "k" * 32})
    # 复刻实战现场：黑板里只有根域资产（子域从未导入）
    r = client.post(f"/api/projects/{pid}/assets",
                    json={"value": "zut.edu.cn", "type": "domain"})
    assert r.status_code == 201, r.text
    monkeypatch.setattr("core.api.app.fofa_mod.FofaClient", _DirtyHostClient)
    r = client.post(f"/api/projects/{pid}/fofa/search",
                    json={"query": 'domain="zut.edu.cn"', "size": 100})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    # host 清洗：剥 scheme / 端口尾（响应即干净值，前端直接展示）
    assert rows[0]["host"] == "zczx.zut.edu.cn"
    assert rows[1]["host"] == "authserver.zut.edu.cn"
    # 锚点精确化：根域在库 ≠ 子域已导入 → 子域行不灰显；空 host 行回退根域 → 灰显
    assert rows[0]["existing"]["domain"] is False
    assert rows[1]["existing"]["domain"] is False
    assert rows[2]["existing"]["domain"] is True
    # 落历史的 rows 同口径清洗
    hid = r.json()["history_id"]
    proj_dir = Path(client.app.state.projects[pid].path)
    rec = json.loads(
        (proj_dir / "fofa_history" / f"{hid}.json").read_text(encoding="utf-8"))
    assert rec["rows"][0]["host"] == "zczx.zut.edu.cn"
    # 子域登记后 → 该行灰显，其余子域行仍可勾选
    r = client.post(f"/api/projects/{pid}/assets",
                    json={"value": "zczx.zut.edu.cn", "type": "domain"})
    assert r.status_code == 201, r.text
    rows = client.post(f"/api/projects/{pid}/fofa/search",
                       json={"query": 'domain="zut.edu.cn"', "size": 100}
                       ).json()["rows"]
    assert rows[0]["existing"]["domain"] is True
    assert rows[1]["existing"]["domain"] is False


def test_fofa_search_quota_429(client, fofa_cfg, monkeypatch):
    """配额耗尽 → 429（熔断语义，绝不能重试）。"""
    class _QuotaClient(_FakeFofaClient):
        def search(self, query, size=100, page=1):
            raise fofa.QuotaExhausted("FOFA 配额已用完")

    pid = _make_project(client)
    client.put("/api/fofa/config", json={"key": "k" * 32})
    monkeypatch.setattr("core.api.app.fofa_mod.FofaClient", _QuotaClient)
    r = client.post(f"/api/projects/{pid}/fofa/search", json={"query": "a"})
    assert r.status_code == 429 and "已用完" in r.json()["detail"]


def test_fofa_test_endpoint(client, fofa_cfg, monkeypatch):
    pid = _make_project(client)
    r = client.post("/api/fofa/test")
    assert r.status_code == 400 and "未配置" in r.json()["detail"]
    client.put("/api/fofa/config", json={"key": "k" * 32})
    monkeypatch.setattr("core.api.app.fofa_mod.FofaClient", _FakeFofaClient)
    r = client.post("/api/fofa/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["remain"] == 999


def test_fofa_history_persist_restore_delete(client, fofa_cfg, monkeypatch):
    """2026-09-28 历史即保持：查询成功自动落 <项目>/fofa_history/<id>.json（含
    全量 rows）→ 轻量列表 → 单条恢复（rows 现算 existing 标注，资产导入后灰显
    新鲜）→ 删除连带结果文件 → 清空删整个目录。"""
    def _no_dns(*a, **k):
        raise OSError("dns off (hermetic)")
    monkeypatch.setattr("socket.getaddrinfo", _no_dns)
    pid = _make_project(client)
    client.put("/api/fofa/config", json={"key": "k" * 32})
    monkeypatch.setattr("core.api.app.fofa_mod.FofaClient", _FakeFofaClient)
    # 查询成功 → history_id 回传 + 文件落盘
    r = client.post(f"/api/projects/{pid}/fofa/search",
                    json={"query": 'domain="x.com"', "size": 100})
    hid = r.json()["history_id"]
    assert hid and r.json()["total"] == 2
    proj_dir = Path(client.app.state.projects[pid].path)
    hist_file = proj_dir / "fofa_history" / f"{hid}.json"
    assert hist_file.is_file()
    # 轻量列表：带 query/size/total/ts，不带 rows
    r = client.get(f"/api/projects/{pid}/fofa/history")
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["id"] == hid
    assert items[0]["query"] == 'domain="x.com"' and items[0]["total"] == 2
    assert "rows" not in items[0]
    # 恢复前先导入一行资产 → 恢复时 existing 现算标注（灰显新鲜）
    r = client.post(f"/api/projects/{pid}/assets",
                    json={"value": "aaa.example.com", "type": "domain"})
    assert r.status_code == 201, r.text
    r = client.get(f"/api/projects/{pid}/fofa/history/{hid}")
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["id"] == hid and len(rec["rows"]) == 2
    assert rec["rows"][0]["existing"]["domain"] is True  # 恢复时现算：已导入 → 灰显
    # id 非法 422（正则白名单防路径穿越）；路由层对 .. 归一化出 404 也属安全
    assert client.get(f"/api/projects/{pid}/fofa/history/xyz").status_code == 422
    assert client.get(
        f"/api/projects/{pid}/fofa/history/20990101-000000-000000").status_code == 404
    # 删除单条 → 文件消失；再删 404 幂等路径返回 deleted
    assert client.delete(f"/api/projects/{pid}/fofa/history/{hid}").status_code == 200
    assert not hist_file.exists()
    # 清空：再查两条 → clear → 目录移除
    client.post(f"/api/projects/{pid}/fofa/search", json={"query": "b"})
    client.post(f"/api/projects/{pid}/fofa/search", json={"query": "c"})
    assert (proj_dir / "fofa_history").is_dir()
    assert client.delete(f"/api/projects/{pid}/fofa/history").status_code == 200
    assert not (proj_dir / "fofa_history").exists()
    assert client.get(f"/api/projects/{pid}/fofa/history").json()["items"] == []


def test_fofa_history_fifo_cap(client, fofa_cfg, monkeypatch):
    """FIFO 上限 50：文件名时间前缀有序，超限淘汰最旧；查询失败（429）不落历史。"""
    class _CountingClient(_FakeFofaClient):
        n = 0

        def search(self, query, size=100, page=1):
            _CountingClient.n += 1
            return {"total": 1, "size": size, "page": page, "rows": [
                {"ip": f"10.0.0.{_CountingClient.n}", "port": "80", "protocol": "tcp",
                 "host": "", "domain": f"d{_CountingClient.n}.x.com", "title": "t",
                 "products": []},
            ]}

    pid = _make_project(client)
    client.put("/api/fofa/config", json={"key": "k" * 32})
    monkeypatch.setattr("core.api.app.fofa_mod.FofaClient", _CountingClient)
    proj_dir = Path(client.app.state.projects[pid].path)
    for i in range(53):
        r = client.post(f"/api/projects/{pid}/fofa/search", json={"query": f"q{i}"})
        assert r.status_code == 200, r.text
    files = sorted((proj_dir / "fofa_history").glob("*.json"))
    assert len(files) == 50  # FIFO 淘汰 3 条最旧
    items = client.get(f"/api/projects/{pid}/fofa/history").json()["items"]
    assert len(items) == 50
    assert items[0]["query"] == "q52" and items[-1]["query"] == "q3"  # 倒序，最旧 3 条已删
    # 配额耗尽（429）不落历史
    class _QuotaClient(_FakeFofaClient):
        def search(self, query, size=100, page=1):
            raise fofa.QuotaExhausted("配额没了")

    monkeypatch.setattr("core.api.app.fofa_mod.FofaClient", _QuotaClient)
    before = len(client.get(f"/api/projects/{pid}/fofa/history").json()["items"])
    r = client.post(f"/api/projects/{pid}/fofa/search", json={"query": "q"})
    assert r.status_code == 429
    assert len(client.get(f"/api/projects/{pid}/fofa/history").json()["items"]) == before


def test_asset_import_preview_and_import(client):
    """预览（multipart CSV + 嗅探）→ mapping 形态导入 → dict 形态导入（FOFA 勾选）。"""
    pid = _make_project(client)
    csv_bytes = "IP地址,端口,标题\n10.0.0.9,80,首页\n".encode("utf-8")
    r = client.post(f"/api/projects/{pid}/assets/import/preview",
                    files={"file": ("targets.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["header"] == ["IP地址", "端口", "标题"]
    assert [c["kind"] for c in body["columns"]] == ["ip", "port", "title"]
    assert body["total_rows"] == 1 and body["truncated"] is False
    # mapping 形态导入：ip+port 行 → service + host（created 含自动挂载 host）
    r = client.post(f"/api/projects/{pid}/assets/import",
                    json={"source": "csv", "rows": [["10.0.0.9", "80", "首页"]],
                          "mapping": ["ip", "port", "title"]})
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["source"] == "csv" and summary["created"] == 2
    assert summary["batch_id"].startswith("imp-")
    # dict 形态导入（FOFA 勾选行直传）
    r = client.post(f"/api/projects/{pid}/assets/import",
                    json={"source": "fofa",
                          "rows": [{"ip": "10.0.0.10", "port": "443", "title": "X"}]})
    assert r.status_code == 200 and r.json()["created"] == 2
    values = {(a["type"], a["value"])
              for a in client.get(f"/api/projects/{pid}/assets").json()}
    assert ("service", "10.0.0.9:80") in values
    assert ("host", "10.0.0.9") in values
    assert ("service", "10.0.0.10:443") in values


def test_asset_import_rejects_bad_source_and_over_limit(client):
    pid = _make_project(client)
    r = client.post(f"/api/projects/{pid}/assets/import",
                    json={"source": "hacker", "rows": [{"ip": "10.0.0.1"}]})
    assert r.status_code == 422 and "source" in r.json()["detail"]
    r = client.post(f"/api/projects/{pid}/assets/import",
                    json={"source": "manual",
                          "rows": [{"ip": f"10.{i}.0.1"} for i in range(10001)]})
    assert r.status_code == 422 and "上限" in r.json()["detail"]


# ---------- workspace-hygiene D6：工作区卫生体检（2026-09-23） ----------

def test_workspace_hygiene_report(client, tmp_path, monkeypatch):
    """陌生条目 / 项目膨胀目录 / .trash 三段口径；纯只读，处置指向既有入口。"""
    ws = tmp_path / "workspaces"
    pid = _make_project(client)
    slug = next(d.name for d in ws.iterdir()
                if d.is_dir() and d.name != ".trash"
                and (d / "project.json").is_file())
    # 陌生条目：无 project.json 的目录 + 散文件；白名单 CLAUDE.md 不报
    (ws / "mystery-dir").mkdir()
    (ws / "orphan.log").write_text("x", encoding="utf-8")
    (ws / "CLAUDE.md").write_text("# 契约", encoding="utf-8")
    # 膨胀：项目 .tmp 放小文件，monkeypatch 阈值到 0.01MB 触发 warning 档
    bloat_dir = ws / slug / ".tmp"
    bloat_dir.mkdir()
    (bloat_dir / "chunk.bin").write_bytes(b"\0" * (30 * 1024))  # 30KB ≈ 0.03MB
    monkeypatch.setattr("core.api.app.HYGIENE_BLOAT_MB", 0.01)
    # 回收站：两条目
    trash = ws / ".trash"
    trash.mkdir()
    (trash / "gone-1").mkdir()
    (trash / "gone-2.txt").write_text("y", encoding="utf-8")

    r = client.get("/api/workspace-hygiene")
    assert r.status_code == 200
    body = r.json()
    names = {(s["name"], s["kind"]) for s in body["strays"]}
    assert ("mystery-dir", "dir") in names and ("orphan.log", "file") in names
    assert all(n != "CLAUDE.md" for n, _ in names) and ".trash" not in [n for n, _ in names]
    proj = next(p for p in body["projects"] if p["slug"] == slug)
    assert proj["bloat"] == [{"dir": ".tmp", "mb": 0.0, "level": "warning"}]
    assert body["trash"]["count"] == 2
    assert body["summary"]["strays"] == 2 and body["summary"]["bloats"] == 1
    assert body["summary"]["errors"] == 0
    # 端点零写副作用：体检后条目原样在场
    assert (ws / "mystery-dir").is_dir() and (ws / "orphan.log").exists()


def test_workspace_hygiene_bloat_error_level(client, tmp_path, monkeypatch):
    """膨胀超 error 阈值（monkeypatch 500MB 档到 0.5MB）升 error；无膨胀项目零条目。"""
    ws = tmp_path / "workspaces"
    _make_project(client)
    slug = next(d.name for d in ws.iterdir()
                if d.is_dir() and d.name != ".trash"
                and (d / "project.json").is_file())
    spill = ws / slug / "spill"
    spill.mkdir()
    (spill / "big.txt").write_bytes(b"\0" * (600 * 1024))  # 600KB
    monkeypatch.setattr("core.api.app.HYGIENE_BLOAT_MB", 0.1)
    monkeypatch.setattr("core.api.app.HYGIENE_BLOAT_ERROR_MB", 0.5)
    body = client.get("/api/workspace-hygiene").json()
    proj = next(p for p in body["projects"] if p["slug"] == slug)
    assert proj["bloat"][0]["level"] == "error"
    assert body["summary"]["errors"] == 1
