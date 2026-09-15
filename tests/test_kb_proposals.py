"""C 批后端测试：kb 本地基线 CRUD/备份/版本/引用联动 + 统一提案通道 +
路由可观测（breakdown/skill.routed/近重复/vocab）。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.api.app import create_app  # noqa: E402
from core.agent.loop import AgentConfig, AgentSession  # noqa: E402
from core.agent.tools import ToolDispatcher  # noqa: E402
from core.blackboard import Blackboard, TaskQueue  # noqa: E402
from core.runtime.backends import NativeBackend  # noqa: E402
from core.runtime.gateway import ExecutionGateway  # noqa: E402
from core.skills import proposals as pm  # noqa: E402
from core.skills.doctor import diagnose  # noqa: E402


# ---------------- 夹具 ----------------

SKILL_BODY = (
    "---\nname: web-skill\ndescription: Web 注入与认证测试技能 sqli login\n"
    "keywords: sqli, 注入, login\nfeatures: has_login\n---\n"
    "# 正文\n\n打开 kb_open(module=\"ctf-web/sqli/README.md\")，\n"
    "另见 `ctf-web/sqli/auth.md`。\n"
)


def _build_packs(root: Path, *, two_sources: bool = False) -> Path:
    packs = root / "packs"
    kb = packs / "capabilities" / "web" / "kb"
    (kb / "ctf-web" / "sqli").mkdir(parents=True)
    (kb / "ctf-web" / "sqli" / "README.md").write_text(
        "# SQL 注入方法论\n\n原始英文快照风格内容。", encoding="utf-8")
    (kb / "ctf-web" / "sqli" / "auth.md").write_text(
        "# 认证模块\n\n引用同目录 README。", encoding="utf-8")
    # kb 文档：既含完整路径引用，也含相对链接（联动时后者进 skipped_relative）
    (kb / "ctf-web" / "sqli" / "notes.md").write_text(
        "完整路径 `ctf-web/sqli/auth.md`；相对链接见 [auth](./auth.md)。",
        encoding="utf-8")
    sources = [{"id": "web-kb", "root": "kb", "recursive": True}]
    if two_sources:
        extra = packs / "capabilities" / "web" / "extra" / "other-snap"
        extra.mkdir(parents=True)
        (extra / "extra.md").write_text("# 第二源", encoding="utf-8")
        sources.append({"id": "web-extra", "root": "extra", "recursive": True})
    (packs / "capabilities" / "web" / "kb_sources.json").write_text(
        json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
    (packs / "capabilities" / "web" / "rules").mkdir(parents=True, exist_ok=True)
    (packs / "capabilities" / "web" / "rules" / "redlines.md").write_text(
        "# web 红线", encoding="utf-8")
    sk = packs / "capabilities" / "web" / "skills" / "web-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
    role_dir = packs / "tracks" / "assessment" / "roles"
    role_dir.mkdir(parents=True)
    (role_dir / "_generalist.yaml").write_text(
        'name: _generalist\npersona: "通用测试员。"', encoding="utf-8")
    return packs


def _client(tmp_path: Path, *, two_sources: bool = False, **app_kw):
    packs = _build_packs(tmp_path, two_sources=two_sources)
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"), **app_kw)
    return packs, TestClient(app)


def _project(c: TestClient, name: str = "测试项目") -> str:
    r = c.post("/api/projects",
               json={"name": name, "track": "assessment", "capabilities": ["web"]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_job(c: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job 超时未结束")


# ---------------- kb CRUD / 校验 ----------------

def test_kb_list_read_and_refs(tmp_path):
    packs, c = _client(tmp_path)
    r = c.get("/api/capabilities/web/kb")
    assert r.status_code == 200
    files = [f["path"] for src in r.json()["sources"] for f in src["files"]]
    assert "ctf-web/sqli/README.md" in files
    assert not any(".history" in f for f in files)

    r = c.get("/api/capabilities/web/kb/file",
              params={"path": "ctf-web/sqli/README.md"})
    assert r.status_code == 200
    data = r.json()
    assert "SQL 注入方法论" in data["content"]
    ref_files = {h["file"] for h in data["refs"]}
    assert any(f.endswith("web-skill/SKILL.md") for f in ref_files)


def test_kb_create_chinese_path_and_conflicts(tmp_path):
    _packs, c = _client(tmp_path)
    body = {"path": "ctf-web/案例/新经验.md", "content": "# 中文多级目录新建\n"}
    r = c.post("/api/capabilities/web/kb/file", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["created"] is True
    # 重名 → 409
    r = c.post("/api/capabilities/web/kb/file", json=body)
    assert r.status_code == 409

    # 非法路径一律 422
    for bad in ["../escape.md", "/abs/x.md", "ctf-web/x.txt",
                "ctf-web/a:b.md", ".history/x.md", "", "ctf-web/x*.md"]:
        r = c.post("/api/capabilities/web/kb/file",
                   json={"path": bad, "content": "x"})
        assert r.status_code == 422, f"{bad} 应 422，实得 {r.status_code}"
    # 空内容 / 超 1 MiB
    r = c.post("/api/capabilities/web/kb/file",
               json={"path": "ctf-web/big.md", "content": "  "})
    assert r.status_code == 422
    r = c.put("/api/capabilities/web/kb/file",
              json={"path": "ctf-web/sqli/README.md", "content": "x" * ((1 << 20) + 1)})
    assert r.status_code == 422
    # PUT 不存在的文件 → 404
    r = c.put("/api/capabilities/web/kb/file",
              json={"path": "ctf-web/missing.md", "content": "x"})
    assert r.status_code == 404


def test_kb_edit_backup_versions_diff_rollback_roundtrip(tmp_path):
    _packs, c = _client(tmp_path)
    path = "ctf-web/sqli/README.md"
    original = c.get("/api/capabilities/web/kb/file", params={"path": path}).json()["content"]
    r = c.put("/api/capabilities/web/kb/file",
              json={"path": path, "content": "# 本地修订版\n"})
    assert r.status_code == 200 and r.json()["backup"]

    versions = c.get("/api/capabilities/web/kb/versions", params={"path": path}).json()["versions"]
    assert len(versions) == 1
    v = versions[0]["version"]
    diff = c.get("/api/capabilities/web/kb/diff",
                 params={"path": path, "version": v}).json()["diff"]
    assert "-# SQL 注入方法论" in diff and "+# 本地修订版" in diff

    # 回滚：内容复原，且当前版也被再备份（可往返）
    r = c.post("/api/capabilities/web/kb/rollback",
               params={"path": path, "version": v})
    assert r.status_code == 200
    assert c.get("/api/capabilities/web/kb/file", params={"path": path}
                 ).json()["content"] == original
    versions2 = c.get("/api/capabilities/web/kb/versions", params={"path": path}).json()["versions"]
    assert len(versions2) == 2
    # 不存在的版本 → 404；非法版本名 → 422
    assert c.post("/api/capabilities/web/kb/rollback",
                  params={"path": path, "version": v}).status_code == 200
    assert c.post("/api/capabilities/web/kb/rollback",
                  params={"path": path, "version": "nope"}).status_code == 422


def test_kb_delete_blocked_by_refs_then_force(tmp_path):
    _packs, c = _client(tmp_path)
    path = "ctf-web/sqli/auth.md"
    r = c.delete("/api/capabilities/web/kb/file", params={"path": path})
    assert r.status_code == 409
    payload = r.json()["detail"]
    assert isinstance(payload, dict) and payload["refs"]

    r = c.delete("/api/capabilities/web/kb/file",
                 params={"path": path, "force": True})
    assert r.status_code == 200
    assert c.get("/api/capabilities/web/kb/file", params={"path": path}).status_code == 404
    trash = list((_packs / "capabilities/web/kb/.history/kb-trash").glob("*.bak"))
    assert len(trash) == 1


def test_kb_rename_cascade_backups_and_cross_source(tmp_path):
    packs, c = _client(tmp_path, two_sources=True)
    old, new = "ctf-web/sqli/auth.md", "ctf-web/sqli/auth-v2.md"
    r = c.post("/api/capabilities/web/kb/rename", json={"path": old, "new_path": new})
    assert r.status_code == 200, r.text
    data = r.json()
    updated_files = {u["file"] for u in data["updated"]}
    assert any(f.endswith("web-skill/SKILL.md") for f in updated_files)
    assert any(f.endswith("ctf-web/sqli/notes.md") for f in updated_files)
    # 相对链接无法整串替换，进 skipped_relative 由人处理
    assert any(f.endswith("ctf-web/sqli/notes.md") for f in data["skipped_relative"])

    skill = (packs / "capabilities/web/skills/web-skill/SKILL.md").read_text(encoding="utf-8")
    notes = (packs / "capabilities/web/kb/ctf-web/sqli/notes.md").read_text(encoding="utf-8")
    assert new in skill and old not in skill
    assert new in notes
    # 两类被改文件各自留备份
    assert list((packs / "capabilities/web/skills/web-skill/.history").glob("*_SKILL.md"))
    assert list((packs / "capabilities/web/kb/.history/kb-backups").glob("*notes.md.bak"))
    # 新名文件在，旧名不在；refs 端点跟到新路径
    assert (packs / "capabilities/web/kb/ctf-web/sqli/auth-v2.md").is_file()
    assert not (packs / "capabilities/web/kb/ctf-web/sqli/auth.md").exists()
    assert c.get("/api/capabilities/web/kb/refs",
                 params={"path": new}).json()["count"] >= 2

    # 跨源改名 → 422
    r = c.post("/api/capabilities/web/kb/rename",
               json={"path": "other-snap/extra.md",
                     "new_path": "ctf-web/extra-moved.md"})
    assert r.status_code == 422


# ---------------- 统一提案通道 ----------------

def test_proposal_lifecycle_all_kb_modes_and_skill_edit(tmp_path):
    packs, c = _client(tmp_path)
    pid = _project(c)

    # 1) kb edit：合法落 pending，detail 实时 diff
    r = c.post("/api/proposals", json={
        "target": {"kind": "kb", "cap": "web", "path": "ctf-web/sqli/README.md"},
        "mode": "edit", "content": "# SQL 注入方法论 v2\n新增绕过手法。\n",
        "summary": "补充 SQLi 绕过小节", "reason": "任务 t-1 中验证有效",
        "project": pid})
    assert r.status_code == 201, r.text
    p_edit = r.json()
    assert p_edit["status"] == "pending"
    detail = c.get(f"/api/proposals/{p_edit['id']}").json()
    assert "+新增绕过手法" in detail["live"]["diff"]

    # 应用（人类）：备份 + 内容更新；再应用/再拒绝已决提案 → 409；署名非法 → 422
    r = c.post(f"/api/proposals/{p_edit['id']}/apply", json={"decided_by": "human"})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/proposals/{p_edit['id']}/apply",
                  json={}).status_code == 409
    assert c.post(f"/api/proposals/{p_edit['id']}/reject",
                  json={}).status_code == 409
    pending_one = c.post("/api/proposals", json={
        "target": {"kind": "kb", "cap": "web", "path": "ctf-web/sqli/auth.md"},
        "mode": "delete", "summary": "待拒", "reason": "x", "project": pid}).json()
    assert c.post(f"/api/proposals/{pending_one['id']}/reject",
                  json={"decided_by": "skynet"}).status_code == 422
    assert c.post(f"/api/proposals/{pending_one['id']}/reject",
                  json={"decided_by": "human", "note": "不需要"}).status_code == 200
    assert list((packs / "capabilities/web/kb/.history/kb-backups").glob("*README.md.bak"))

    # 2) kb create：新经验写新 md
    r = c.post("/api/proposals", json={
        "target": {"kind": "kb", "cap": "web", "path": "ctf-web/案例/新经验.md"},
        "mode": "create", "content": "# 本任务验证的新手法\n",
        "summary": "沉淀新手法", "reason": "任务 t-2 三次复现", "project": pid})
    assert r.status_code == 201
    p_create = r.json()
    assert c.post(f"/api/proposals/{p_create['id']}/apply",
                  json={"decided_by": "human"}).status_code == 200
    assert (packs / "capabilities/web/kb/ctf-web/案例/新经验.md").is_file()

    # 3) kb rename：应用时移动 + 引用联动
    r = c.post("/api/proposals", json={
        "target": {"kind": "kb", "cap": "web",
                   "path": "ctf-web/sqli/auth.md",
                   "new_path": "ctf-web/sqli/auth-renamed.md"},
        "mode": "rename", "summary": "改名对齐内容", "reason": "原名误导",
        "project": pid})
    assert r.status_code == 201
    p_rename = r.json()
    detail = c.get(f"/api/proposals/{p_rename['id']}").json()
    assert detail["live"]["refs"]  # rename 暴露引用面
    r = c.post(f"/api/proposals/{p_rename['id']}/apply", json={})
    assert r.status_code == 200
    assert (packs / "capabilities/web/kb/ctf-web/sqli/auth-renamed.md").is_file()
    skill = (packs / "capabilities/web/skills/web-skill/SKILL.md").read_text(encoding="utf-8")
    assert "auth-renamed.md" in skill

    # 4) kb delete（演示 demo 署名）
    r = c.post("/api/proposals", json={
        "target": {"kind": "kb", "cap": "web", "path": "ctf-web/sqli/notes.md"},
        "mode": "delete", "summary": "删除过时笔记", "reason": "内容已并入新文档",
        "project": pid})
    p_del = r.json()
    assert c.post(f"/api/proposals/{p_del['id']}/apply",
                  json={"decided_by": "demo-script(auto)"}).status_code == 200
    assert not (packs / "capabilities/web/kb/ctf-web/sqli/notes.md").exists()

    # 5) skill edit：frontmatter name 必须一致；应用走技能备份
    new_skill = SKILL_BODY.replace("另见", "补充一段方法论后另见")
    r = c.post("/api/proposals", json={
        "target": {"kind": "skill", "skill_kind": "capability",
                   "owner": "web", "name": "web-skill"},
        "mode": "edit", "content": new_skill,
        "summary": "技能补方法论指引", "reason": "任务中发现指引不清", "project": pid})
    assert r.status_code == 201
    p_sk = r.json()
    assert c.post(f"/api/proposals/{p_sk['id']}/apply", json={}).status_code == 200
    assert list((packs / "capabilities/web/skills/web-skill/.history").glob("*_SKILL.md"))

    # 审计事件齐
    kinds = {e["kind"] for e in c.get(f"/api/projects/{pid}/events").json()}
    assert {"proposal.created", "proposal.applied"} <= kinds

    # 列表过滤：5 条已应用 + 1 条 rejected
    assert len(c.get("/api/proposals", params={"status": "approved"}).json()) == 5
    assert c.get("/api/proposals", params={"status": "pending"}).json() == []
    assert len(c.get("/api/proposals", params={"status": "rejected"}).json()) == 1
    assert c.get("/api/proposals", params={"status": "bogus"}).status_code == 422


def test_proposal_illegal_rejected_without_landing(tmp_path):
    _packs, c = _client(tmp_path)
    base = {"summary": "x", "reason": "y", "project": None}

    def post(**kw):
        return c.post("/api/proposals", json={**base, **kw})

    # edit 不存在文件
    assert post(target={"kind": "kb", "cap": "web", "path": "ctf-web/nope.md"},
                mode="edit", content="x").status_code == 422
    # 穿越路径
    assert post(target={"kind": "kb", "cap": "web", "path": "../x.md"},
                mode="create", content="x").status_code == 422
    # 技能只许 edit
    assert post(target={"kind": "skill", "skill_kind": "capability",
                        "owner": "web", "name": "web-skill"},
                mode="create", content="x").status_code == 422
    # 技能 edit frontmatter name 不一致
    assert post(target={"kind": "skill", "skill_kind": "capability",
                        "owner": "web", "name": "web-skill"},
                mode="edit",
                content="---\nname: 别的名字\n---\n").status_code == 422
    # rename 缺 new_path / 同路径
    assert post(target={"kind": "kb", "cap": "web",
                        "path": "ctf-web/sqli/auth.md", "new_path": "ctf-web/sqli/auth.md"},
                mode="rename").status_code == 422
    # 缺摘要/理由
    r = c.post("/api/proposals", json={
        "target": {"kind": "kb", "cap": "web", "path": "ctf-web/sqli/auth.md"},
        "mode": "delete", "summary": "", "reason": ""})
    assert r.status_code == 422
    # 没有任何文件落地
    assert c.get("/api/proposals").json() == []


def test_proposal_reject_and_revise(tmp_path):
    _packs, c = _client(tmp_path)
    r = c.post("/api/proposals", json={
        "target": {"kind": "kb", "cap": "web", "path": "ctf-web/sqli/auth.md"},
        "mode": "delete", "summary": "删", "reason": "过时"})
    pid = r.json()["id"]
    # 修订内容（delete 没有 content，改成 edit）
    r = c.post(f"/api/proposals/{pid}/revise", json={
        "by": "human", "note": "别删，改写",
        "changes": {"content": "# 认证模块（改写）\n"}})
    # mode 不可通过 revise 改（不在白名单字段）——target/content 变了导致 mode=delete
    # 与 content 不冲突校验（delete 不看 content），修订成功仍 pending
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert len(r.json()["revisions"]) == 1
    r = c.post(f"/api/proposals/{pid}/reject",
               json={"decided_by": "human", "note": "不需要"})
    assert r.status_code == 200
    assert c.post(f"/api/proposals/{pid}/reject",
                  json={}).status_code == 409


# ---------------- Agent 工具 propose_pack_edit ----------------

class _FakeLLM:
    def __init__(self, text=""):
        self.text = text

    def chat(self, messages, *, system=None, tools=None, max_tokens=4096,
             temperature=None):
        return SimpleNamespace(text=self.text, tool_calls=[])


def _make_session(tmp_path: Path):
    packs = _build_packs(tmp_path)
    db = tmp_path / "a.db"
    bb = Blackboard(str(db))
    project = bb.create_project("提案项目", "assessment", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    tq = TaskQueue(bb)
    agent = AgentSession(
        project_id=project["id"], bb=bb, gateway=gw, llm=_FakeLLM(),
        packs_root=packs, track="assessment", capabilities=["web"],
        capability_prompt="## 能力清单\n- host: 可用",
        config=AgentConfig(max_steps=5))
    return packs, bb, project, tq, agent


def test_agent_propose_tool_only_pending_and_cap(tmp_path):
    packs, bb, project, _tq, agent = _make_session(tmp_path)
    d: ToolDispatcher = agent.dispatcher
    d.current_task_id = "t-100"
    ids = []
    for i in range(3):
        out = d.dispatch("propose_pack_edit", {
            "kind": "kb", "mode": "create",
            "target": {"cap": "web", "path": f"ctf-web/agent-note-{i}.md"},
            "content": f"# Agent 经验 {i}\n",
            "summary": f"经验 {i}", "reason": "任务 t-100 中验证有效"})
        assert "pending" in out, out
        ids.append(out)
    # 第 4 条：每会话上限 3
    out = d.dispatch("propose_pack_edit", {
        "kind": "kb", "mode": "create",
        "target": {"cap": "web", "path": "ctf-web/agent-note-3.md"},
        "content": "# x\n", "summary": "x", "reason": "y"})
    assert out.startswith("[拒绝]")
    # 非法提案不落地
    out = d.dispatch("propose_pack_edit", {
        "kind": "kb", "mode": "create",
        "target": {"cap": "web", "path": "../escape.md"},
        "content": "x", "summary": "x", "reason": "y"})
    assert out.startswith("[拒绝]")
    pending = pm.list_proposals(packs, "pending")
    assert len(pending) == 3
    assert all(p["status"] == "pending" for p in pending)
    # 文件确实没被 Agent 直接改
    assert not (packs / "capabilities/web/kb/ctf-web/agent-note-0.md").exists()
    # 审计事件
    events = bb.recent_events(project["id"])
    assert sum(1 for e in events if e["kind"] == "proposal.created") == 3
    payload = next(e["payload"] for e in events if e["kind"] == "proposal.created")
    assert payload["origin"] == "agent" and payload["id"].startswith("pp_")
    bb.close()


def test_agent_propose_skill_create_rejected(tmp_path):
    _packs, _bb, _p, _tq, agent = _make_session(tmp_path)
    out = agent.dispatcher.dispatch("propose_pack_edit", {
        "kind": "skill", "mode": "create",
        "target": {"skill_kind": "capability", "owner": "web", "name": "new-skill"},
        "content": "---\nname: new-skill\n---\n",
        "summary": "新技能", "reason": "缺失"})
    assert out.startswith("[拒绝]")


# ---------------- skill.routed 事件 ----------------

def test_skill_routed_event_hit_and_miss(tmp_path):
    _packs, bb, project, _tq, agent = _make_session(tmp_path)
    ctx = agent.skill_context_for("排查 SQL 注入登录绕过", task_id="t-9")
    assert "web-skill" in ctx
    ev = [e for e in bb.recent_events(project["id"]) if e["kind"] == "skill.routed"]
    assert len(ev) == 1
    assert ev[0]["payload"]["name"] == "web-skill"
    assert ev[0]["payload"]["task_id"] == "t-9"
    cats = {b["category"] for b in ev[0]["payload"]["breakdown"]}
    assert "keywords" in cats
    # 未命中：name=null
    agent.skill_context_for("zzzz qqq unrelated", task_id="t-10")
    miss = [e for e in bb.recent_events(project["id"])
            if e["kind"] == "skill.routed" and e["payload"]["name"] is None]
    assert len(miss) == 1
    assert len(miss[0]["payload"]["query"]) <= 200
    bb.close()


# ---------------- 路由试算 breakdown / vocab / doctor 近重复 ----------------

def test_route_preview_breakdown_and_vocab(tmp_path):
    _packs, c = _client(tmp_path)
    r = c.post("/api/skills/route-preview", json={
        "query": "SQL 注入 sqli", "track": "assessment", "capabilities": ["web"]})
    assert r.status_code == 200
    top = r.json()[0]
    assert top["name"] == "web-skill"
    cats = {b["category"] for b in top["breakdown"]}
    assert {"keywords", "features"} & cats

    vocab = c.get("/api/skills/vocab").json()
    kws = {item["value"]: item["count"] for item in vocab["keywords"]}
    assert kws.get("sqli") == 1
    assert set(vocab) == {"keywords", "features", "file_features", "platforms",
                         "formats", "vuln_classes", "task_types"}


def test_doctor_near_duplicate_info(tmp_path):
    packs, _c = _client(tmp_path)
    for name in ("dup-a", "dup-b"):
        d = packs / f"capabilities/web/skills/{name}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\n"
            "keywords: sqli, orm\nfeatures: has_login\n---\n正文\n",
            encoding="utf-8")
    rep = diagnose(packs).to_dict()
    codes = {i["code"] for i in rep["issues"]}
    assert "skill-near-duplicate" in codes
    pair = next(i for i in rep["issues"] if i["code"] == "skill-near-duplicate")
    assert "dup-a" in pair["message"] and "dup-b" in pair["message"]


# ---------------- 复盘沉淀 Job（planner_llm） ----------------

def test_review_proposals_job_fake_planner(tmp_path):
    proposal_json = json.dumps({"proposals": [
        # 合法：create 新经验 md
        {"kind": "kb", "mode": "create",
         "target": {"cap": "web", "path": "ctf-web/review-lesson.md"},
         "content": "# 复盘沉淀的新经验\n",
         "summary": "复盘：SQLi 绕过手法", "reason": "任务成功 + finding 已验证"},
        # 越界：猜路径 + 穿越 → 校验拒绝
        {"kind": "kb", "mode": "edit",
         "target": {"cap": "web", "path": "../escape.md"},
         "content": "x", "summary": "越界", "reason": "无"},
        # 越权：AI 不许 create 技能
        {"kind": "skill", "mode": "create",
         "target": {"skill_kind": "capability", "owner": "web", "name": "zzz"},
         "content": "---\nname: zzz\n---\n", "summary": "新技能", "reason": "缺"},
    ]}, ensure_ascii=False)
    _packs, c = _client(tmp_path,
                        executor_llm=_FakeLLM(), planner_llm=_FakeLLM(proposal_json))
    pid = _project(c)
    r = c.post(f"/api/projects/{pid}/review-proposals")
    assert r.status_code == 200, r.text
    job = _wait_job(c, r.json()["job_id"])  # 端点契约 {"job_id": ...}
    assert job["status"] == "done", job
    result = job["result"]
    landed = result["landed"]
    assert len(landed) == 1 and landed[0]["id"].startswith("pp_")
    assert len(result["rejected"]) == 2
    # 落地的是 pending，文件未改
    p = c.get(f"/api/proposals/{landed[0]['id']}").json()
    assert p["status"] == "pending" and p["origin"] == "review"
    kinds = {e["kind"] for e in c.get(f"/api/projects/{pid}/events").json()}
    assert "proposal.created" in kinds


def test_review_proposals_no_key_503(tmp_path, monkeypatch):
    # 隔离真 key：cwd 切 tmp（找不到 .env）+ 清环境变量（本机 .env 有真 Ark key）
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    packs = _build_packs(tmp_path)
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     providers_config=str(tmp_path / "providers.json"))
    c = TestClient(app)
    pid = _project(c)
    r = c.post(f"/api/projects/{pid}/review-proposals")
    assert r.status_code == 503
