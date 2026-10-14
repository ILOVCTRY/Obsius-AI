"""CTF 题目即资产（challenge-asset，v38，2026-10-14）验证。

分两层：
- **store / tools 层**（不依赖 create_app）：题目登记与复用、线程绑定与向上解析、
  runtime 兜底（意图/发现自动归到线程题目下、无题按标题补建）、非 CTF 轨与任务链
  不受影响。这批在本机必须真跑过。
- **API 层**：题目清单计数、人工建题、改名、存量惰性补建。依赖 create_app——
  本机 FastAPI 0.141.1 / Starlette 1.6.0 已移除 `add_event_handler`，
  `create_app()` 必报 AttributeError（见 MEMORY.md 已知坑），故用 `client` 夹具
  在该环境下自动 skip，不污染本地信号；环境正常时照常执行。
"""

import pytest
from fastapi.testclient import TestClient

from core.agent.tool_registry import REGISTRY
from core.blackboard.assets import (CHALLENGE_TYPE, register_asset,
                                    register_challenge)
from core.chat.runtime import _EXPERT_EXCLUDED, _ORCH_BASE_TOOLS
from core.chat.store import (create_thread, resolve_challenge_asset_id,
                             update_thread)
from core.blackboard.store import Blackboard


@pytest.fixture()
def board(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    pid = bb.create_project("题目项目", "ctf")["id"]
    try:
        yield bb, pid
    finally:
        bb.close()


@pytest.fixture()
def client(tmp_path):
    from core.api.app import create_app
    try:
        app = create_app(workspace_root=str(tmp_path / "workspaces"),
                         tools_root=None, executor_llm=None, planner_llm=None,
                         providers_config=str(tmp_path / "providers.json"))
    except AttributeError:  # 本机 FastAPI/Starlette 环境不兼容（见 MEMORY.md）
        pytest.skip("create_app 在本机不可用（FastAPI add_event_handler 已移除）")
    with TestClient(app) as c:
        yield c


def _dispatcher(bb, pid, thread_id, track="ctf"):
    from core.agent.tools import ToolDispatcher
    from core.runtime.gateway import ExecutionGateway
    return ToolDispatcher(bb, ExecutionGateway(bb=bb), project_id=pid,
                          session_id=thread_id, author=thread_id, track=track,
                          thread_id=thread_id)


# ---------- 1. register_challenge ----------

def test_register_challenge_creates_root_and_dedups(board):
    bb, pid = board
    r = register_challenge(bb, pid, "树状化-签到题",
                           description="附件里藏了 flag", tags=["web", "misc"],
                           thread_id="chat-abc123def456")
    assert r["created"] and r["type"] == CHALLENGE_TYPE
    aid = r["id"]

    row = bb.get_asset(aid)
    assert row["parent_id"] is None                      # 根行
    assert row["type"] == CHALLENGE_TYPE
    assert row["value"] == "树状化-签到题"
    assert row["meta"]["title"] == "附件里藏了 flag"
    assert row["meta"]["tags"] == ["web", "misc"]
    assert row["meta"]["kind"] == CHALLENGE_TYPE
    assert row["meta"]["thread_id"] == "chat-abc123def456"

    # 同名复用（合并 meta，不插重复行）
    r2 = register_challenge(bb, pid, "树状化-签到题", description="更新后的描述")
    assert r2["id"] == aid and not r2["created"]
    assert bb.get_asset(aid)["meta"]["title"] == "更新后的描述"

    # 题目不在覆盖度三态内（不进编排器分桶，主控不会把它当待测资产反复派单）
    from core.coverage import COVERAGE_TYPES
    assert CHALLENGE_TYPE not in COVERAGE_TYPES

    with pytest.raises(ValueError):
        register_challenge(bb, pid, "   ")


# ---------- 2. 线程绑定与向上解析 ----------

def test_thread_challenge_binding_and_parent_walk(board):
    bb, pid = board
    cid = register_challenge(bb, pid, "crypto-易位密码")["id"]

    main = create_thread(bb, pid, "chat-orchestrator", title="crypto-易位密码",
                         challenge_asset_id=cid)
    assert main["challenge_asset_id"] == cid
    assert resolve_challenge_asset_id(bb, main["id"]) == cid

    # 子专家线程不重复声明 → 沿 parent 向上解析到同一道题
    sub = create_thread(bb, pid, "crypto-solver", title="子任务",
                        parent_thread_id=main["id"])
    assert sub["challenge_asset_id"] == ""
    assert resolve_challenge_asset_id(bb, sub["id"]) == cid

    # 未绑定线程解析为空
    assert resolve_challenge_asset_id(
        bb, create_thread(bb, pid, "chat-orchestrator", title="别的")["id"]) == ""

    # update_thread 改绑 / 解绑
    t = create_thread(bb, pid, "chat-orchestrator", title="改名题")
    update_thread(bb, t["id"], challenge_asset_id=cid)
    assert resolve_challenge_asset_id(bb, t["id"]) == cid
    update_thread(bb, t["id"], challenge_asset_id="")   # '' = 解绑
    assert resolve_challenge_asset_id(bb, t["id"]) == ""


# ---------- 3. runtime 兜底 ----------

def test_ctf_fallback_anchors_intent_and_autocreates(board):
    """未显式给锚点：第一次 declare 就按线程标题补建题目并绑定；发现走同一兜底。"""
    bb, pid = board
    t = create_thread(bb, pid, "chat-orchestrator", title="易位密码换表")
    d = _dispatcher(bb, pid, t["id"])

    out = d._tool_declare_intent(statement="确认换表规则")
    assert "[拒绝]" not in out, out
    assert "intent=" in out

    cid = resolve_challenge_asset_id(bb, t["id"])
    assert cid, "declare_intent 应触发题目补建并绑定线程"
    assert bb.get_asset(cid)["value"] == "易位密码换表"
    assert d._challenge_fallback_asset() == cid

    # 补建幂等：再 declare 不会插出第二道题
    d._tool_declare_intent(statement="再试一次")
    assert len(bb.list_assets(pid, type_=CHALLENGE_TYPE)) == 1


def test_ctf_fallback_does_not_override_explicit_anchor(board):
    """显式传 target_asset_id 的完全不受兜底影响。"""
    bb, pid = board
    cid = register_challenge(bb, pid, "第二题")["id"]
    t = create_thread(bb, pid, "chat-orchestrator", title="第二题",
                      challenge_asset_id=cid)
    d = _dispatcher(bb, pid, t["id"])
    sub = register_asset(bb, pid, "https://ctf.example.com/x",
                         type_="url", parent_id=cid)
    # 意图显式挂到附件上
    d._tool_declare_intent(statement="看源码", target_asset_id=sub["id"])
    intents = d.bb.conn.execute(
        "SELECT target_asset_id FROM intents WHERE project_id=?",
        (pid,)).fetchall()
    assert sub["id"] in {r["target_asset_id"] for r in intents}
    # 未给锚点的仍走兜底
    d._tool_declare_intent(statement="再声明一条")
    assert cid in {r["target_asset_id"] for r in d.bb.conn.execute(
        "SELECT target_asset_id FROM intents WHERE project_id=?", (pid,)).fetchall()}


def test_non_ctf_track_unchanged(board):
    """非 CTF 轨：兜底恒不介入，declare_intent 维持原有「必须给锚点」拒绝口径。"""
    bb, _ = board
    pid = bb.create_project("渗透项目", "pentest")["id"]
    t = create_thread(bb, pid, "chat-orchestrator", title="渗透目标")
    d = _dispatcher(bb, pid, t["id"], track="pentest")
    assert d._challenge_fallback_asset() is None
    out = d._tool_declare_intent(statement="扫一下")
    assert "[拒绝]" in out
    assert "[拒绝]" in out and "必须绑定资产锚点" in out
    assert bb.list_assets(pid) == []          # 没被自动建出题


def test_task_chain_session_has_no_challenge(board):
    """任务链会话（thread_id=''）无题目概念——兜底恒 None，不建题。"""
    bb, pid = board
    d = _dispatcher(bb, pid, "")
    assert d._challenge_fallback_asset() is None
    assert bb.list_assets(pid) == []


def test_declare_challenge_tool_gate_and_binding(board):
    """工具层：非 CTF 轨拒绝；CTF 轨建题并绑线程；重复调用复用同名题。"""
    bb, pid = board
    t = create_thread(bb, pid, "chat-orchestrator", title="第一题")
    d = _dispatcher(bb, pid, t["id"])
    out = d._tool_bb_declare_challenge(name="第一题", description="签到",
                                       tags=["web"])
    assert "challenge=" in out and "已绑定到本线程" in out
    cid = resolve_challenge_asset_id(bb, t["id"])
    assert cid and bb.get_asset(cid)["value"] == "第一题"

    out2 = d._tool_bb_declare_challenge(name="第一题")
    assert out2.startswith("[提示]") or "复用" in out2
    assert len(bb.list_assets(pid, type_=CHALLENGE_TYPE)) == 1

    # 非 CTF 轨拒绝
    d2 = _dispatcher(bb, pid, t["id"], track="pentest")
    assert d2._tool_bb_declare_challenge(name="x").startswith("[拒绝]")


def test_tool_surface_membership():
    """bb_declare_challenge 只归主控；子专家侧排除。"""
    assert "bb_declare_challenge" in REGISTRY
    assert "bb_declare_challenge" in _ORCH_BASE_TOOLS
    assert "bb_declare_challenge" in _EXPERT_EXCLUDED


# ---------- 4. API 层 ----------

def _proj(client, name: str, track: str = "ctf") -> str:
    r = client.post("/api/projects",
                    json={"name": name, "track": track,
                          "capabilities": ["binary"]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_challenge_api_list_and_counts(client):
    from core.projects import ProjectStore
    pid = _proj(client, "API 题目项目")
    ws = client.app.state.workspace_root

    body = client.get(f"/api/projects/{pid}/challenges").json()
    # 打开即惰性补建（存量语义：CTF 项目没有题目行就补一道）
    assert len(body) == 1
    ch = body[0]
    assert ch["type"] == CHALLENGE_TYPE and ch["value"] == "API 题目项目"
    assert ch["attachment_count"] == ch["finding_count"] == ch["intent_count"] == 0

    # 再取一次不重复补建（幂等）
    assert client.get(f"/api/projects/{pid}/challenges").json()[0]["id"] == ch["id"]

    # 人工建题 + 挂附件 + 登记发现 → 计数联动
    ch2 = client.post(f"/api/projects/{pid}/challenges",
                      json={"name": "第二题", "description": "一道 crypto",
                            "tags": ["crypto"]}).json()
    assert ch2["value"] == "第二题" and ch2["tags"] == ["crypto"]

    proj = ProjectStore(str(ws)).open_project(pid)
    bb = proj.bb
    client.post(f"/api/projects/{pid}/assets",
                json={"type": "url", "value": "https://x.example.com",
                      "parent_id": ch2["id"]})
    bb.add_finding(pid, "vuln", "挖到注入", severity="high",
                   target_asset_id=ch2["id"], author="human")

    got = {x["id"]: x for x in client.get(f"/api/projects/{pid}/challenges").json()}
    assert got[ch2["id"]]["attachment_count"] == 1
    assert got[ch2["id"]]["finding_count"] == 1


def test_challenge_patch_rename(client):
    from core.projects import ProjectStore
    pid = _proj(client, "待改名")
    ws = client.app.state.workspace_root
    ch = client.post(f"/api/projects/{pid}/challenges",
                     json={"name": "老名字"}).json()
    r = client.patch(f"/api/challenges/{ch['id']}",
                     json={"name": "新名字", "description": "改了描述",
                           "tags": ["web"]})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "新名字" and r.json()["description"] == "改了描述"

    proj = ProjectStore(str(ws)).open_project(pid)
    assert proj.bb.get_asset(ch["id"])["value"] == "新名字"

    # 撞同名 422（同项目同类型下已有该值）
    client.post(f"/api/projects/{pid}/challenges", json={"name": "占用名"})
    assert client.patch(f"/api/challenges/{ch['id']}",
                        json={"name": "占用名"}).status_code == 422

    # 改非题目资产 404
    other = client.post(f"/api/projects/{pid}/assets",
                        json={"type": "host", "value": "10.0.0.1"}).json()
    assert client.patch(f"/api/challenges/{other['id']}",
                        json={"name": "x"}).status_code == 404


def test_backfill_moves_project_level_findings(client):
    """存量：CTF 项目先落几条项目级发现 → 打开题目清单把它们归到补建的题目下。"""
    from core.projects import ProjectStore
    pid = _proj(client, "存量项目")
    ws = client.app.state.workspace_root
    proj = ProjectStore(str(ws)).open_project(pid)
    bb = proj.bb
    bb.add_finding(pid, "vuln", "后门账号列表", severity="critical", author="human")
    bb.add_finding(pid, "vuln", "元数据泄题", severity="high", author="human")
    assert len([f for f in bb.list_findings(pid)
                if not f.get("target_asset_id")]) == 2

    ch = client.get(f"/api/projects/{pid}/challenges").json()[0]
    assert ch["value"] == "存量项目" and ch["finding_count"] == 2
    assert [f for f in bb.list_findings(pid) if not f.get("target_asset_id")] == []

    # 幂等：再取不再补建
    assert client.get(f"/api/projects/{pid}/challenges").json()[0]["id"] == ch["id"]


def test_backfill_does_not_recreate_after_delete(client):
    """用户主动删光题目 → 不被补回来（补建只服务遗留状态，不是常驻机制）。"""
    pid = _proj(client, "会被删空")
    ch = client.get(f"/api/projects/{pid}/challenges").json()[0]
    assert client.request(
        "DELETE", f"/api/projects/{pid}/assets/{ch['id']}").status_code == 200
    assert client.get(f"/api/projects/{pid}/challenges").json() == []


def test_non_ctf_track_returns_empty_and_no_backfill(client):
    """非 CTF 轨：题目清单恒空，且绝不自动补建/改写发现归属。"""
    from core.projects import ProjectStore
    pid = _proj(client, "渗透项目", track="pentest")
    ws = client.app.state.workspace_root
    proj = ProjectStore(str(ws)).open_project(pid)
    bb = proj.bb
    bb.add_finding(pid, "vuln", "项目级结论", author="human")
    assert client.get(f"/api/projects/{pid}/challenges").json() == []
    assert [f for f in bb.list_findings(pid) if not f.get("target_asset_id")]
    assert client.post(f"/api/projects/{pid}/challenges",
                       json={"name": "x"}).status_code == 422


# ---------- 8. 存量补建：游离根资产收编（store 层，不依赖 create_app） ----------


def test_backfill_adopts_orphan_root_assets(tmp_path):
    """存量 CTF 项目：附件被直接登记成根资产（type=binary，题目节点缺失）→ 补建题目
    时把游离根资产收编为子资产，挂其上的发现随子树归到题目下。

    2026-10-14 用户实测「发现无法按题目过滤」的根因：旧项目只有 binary 根资产
    （附件 zip），前端筛选下拉的 host/domain 白名单什么都列不出；补建若只归拢
    target 为空的发现，题目会变空壳，挂在旧根资产上的发现仍游离。
    """
    from core.projects import ProjectStore
    from core.api.app import _backfill_challenges

    proj = ProjectStore(str(tmp_path / "ws")).create_project("存量题目", track="ctf")
    bb, pid = proj.bb, proj.id
    # 旧数据形态：附件直挂根（parent_id=NULL），题目节点不存在
    orphan = register_asset(bb, pid, "artifacts/attachments/x.zip",
                            type_="binary", author="agent")
    oid = orphan["id"]
    bb.add_finding(pid, "vuln", "后门触发词", severity="critical",
                   target_asset_id=oid, author="agent")
    assert bb.conn.execute(
        "SELECT parent_id FROM assets WHERE id=?", (oid,)).fetchone()["parent_id"] is None

    r = _backfill_challenges(proj)
    assert r.get("assets_adopted") == 1, r

    ch = bb.list_assets(pid, type_=CHALLENGE_TYPE)[0]
    # 游离根资产已被收编到题目下（成为附件子资产）
    assert bb.conn.execute(
        "SELECT parent_id FROM assets WHERE id=?", (oid,)).fetchone()["parent_id"] == ch["id"]
    # 题目名 = 项目名（Agent 归纳前的人类可读兜底）
    assert ch["value"] == "存量题目"
    # 挂在旧根资产上的发现仍在（未丢），且现在落在题目子树内
    fs = bb.list_findings(pid)
    assert [f["target_asset_id"] for f in fs] == [oid]
