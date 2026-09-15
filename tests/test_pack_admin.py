"""阶段 3A：角色/技能 CRUD + enabled 开关 + .history 版本管理 + doctor 端点。"""

import time

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_SKILL = ("---\nname: demo-skill\ndescription: 演示技能\nkeywords: a, b\n---\n"
          "# demo-skill\nBODY_MARKER 正文不得丢失\n")


@pytest.fixture()
def client(tmp_path):
    packs = tmp_path / "packs"
    _write(packs / "capabilities/web/pack.yaml", "kind: capability\nname: web\n")
    _write(packs / "capabilities/web/rules/redlines.md", "# web 红线\n")
    _write(packs / "capabilities/web/skills/demo-skill/SKILL.md", _SKILL)
    _write(packs / "tracks/ctf/track.yaml", "name: ctf\n")
    _write(packs / "tracks/ctf/rules/redlines.md", "# ctf 红线\n")
    _write(packs / "tracks/ctf/task_types.yaml", "generic: passive\nrecon: passive\n")
    _write(packs / "tracks/ctf/roles/_generalist.yaml",
           'name: _generalist\ndescription: "兜底"\nskills: null\ntask_types: null\n')
    _write(packs / "tracks/ctf/roles/recon.yaml",
           'name: recon\ndescription: "侦察"\nskills: [demo-skill]\ntask_types: [recon]\n')

    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        c.packs = packs  # type: ignore[attr-defined]
        yield c


# ---------------- 角色：新建 / 克隆 / 删除 ----------------

def test_role_create_clone_and_trash_delete(client):
    # 空白模板
    r = client.post("/api/tracks/ctf/roles", json={"name": "spare"})
    assert r.status_code == 201 and r.json()["cloned"] is None
    roles = {x["file"]: x for x in client.get("/api/tracks/ctf/roles").json()}
    assert "spare" in roles

    # 重名 409 / 非法名 422 / 未知轨 404
    assert client.post("/api/tracks/ctf/roles", json={"name": "spare"}).status_code == 409
    assert client.post("/api/tracks/ctf/roles", json={"name": "../x"}).status_code == 422
    assert client.post("/api/tracks/nope/roles", json={"name": "spare"}).status_code == 404

    # 克隆：字段照抄
    r = client.post("/api/tracks/ctf/roles",
                    json={"name": "recon-copy", "clone_from": "recon"})
    assert r.status_code == 201 and r.json()["cloned"] == "recon"
    copied = {x["file"]: x for x in client.get("/api/tracks/ctf/roles").json()}["recon-copy"]
    assert copied["skills"] == ["demo-skill"] and copied["task_types"] == ["recon"]
    assert client.post("/api/tracks/ctf/roles",
                       json={"name": "c2", "clone_from": "ghost"}).status_code == 404

    # _generalist 受保护
    assert client.delete("/api/tracks/ctf/roles/_generalist").status_code == 409
    # 删除进回收站，可在磁盘找到 .bak；再删 404
    r = client.delete("/api/tracks/ctf/roles/recon-copy")
    assert r.status_code == 200 and r.json()["trash"].endswith(".bak")
    assert client.delete("/api/tracks/ctf/roles/recon-copy").status_code == 404
    trash = list((client.packs / "tracks/ctf/roles/.history/trash").glob("*"))
    assert any(p.name.startswith("recon-copy.") and p.is_file() for p in trash)
    roles_now = client.get("/api/tracks/ctf/roles").json()
    assert all(x["file"] != "recon-copy" for x in roles_now)


def test_role_delete_same_second_never_overwrites(client):
    """同秒二次删除（删→建→删）回收站不得覆盖首件。"""
    client.post("/api/tracks/ctf/roles", json={"name": "temp"})
    client.delete("/api/tracks/ctf/roles/temp")
    client.post("/api/tracks/ctf/roles", json={"name": "temp"})
    client.delete("/api/tracks/ctf/roles/temp")
    trash = list((client.packs / "tracks/ctf/roles/.history/trash").glob("temp.*"))
    assert len(trash) == 2


# ---------------- 技能：向导新建 / enabled 开关 / 删除 ----------------

def test_skill_create_toggle_and_delete(client):
    body = {"name": "new-skill", "description": "新技能",
            "keywords": ["sqli", "注入"], "file_features": ["elf"]}
    r = client.post("/api/capabilities/web/skills", json=body)
    assert r.status_code == 201 and r.json()["path"].endswith("web/skills/new-skill")

    listed = {s["name"]: s for s in client.get("/api/capabilities/web/skills").json()}
    assert listed["new-skill"]["keywords"] == ["sqli", "注入"]
    raw = client.get("/api/capabilities/web/skills/new-skill").json()["raw"]
    assert "name: new-skill" in raw and "file_features: elf" in raw
    assert "kb_open" in raw  # 薄路由模板

    assert client.post("/api/capabilities/web/skills",
                       json={"name": "new-skill"}).status_code == 409
    assert client.post("/api/capabilities/nope/skills",
                       json={"name": "x"}).status_code == 404
    assert client.post("/api/capabilities/web/skills",
                       json={"name": "bad/name"}).status_code == 422

    # 禁用：registry 视角即时生效，正文不动，有备份
    r = client.patch("/api/capabilities/web/skills/new-skill/enabled",
                     json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    listed = {s["name"]: s for s in client.get("/api/capabilities/web/skills").json()}
    assert listed["new-skill"]["enabled"] is False
    raw = client.get("/api/capabilities/web/skills/new-skill").json()["raw"]
    assert "enabled: false" in raw
    # 重新启用
    r = client.patch("/api/capabilities/web/skills/new-skill/enabled",
                     json={"enabled": True})
    listed = {s["name"]: s for s in client.get("/api/capabilities/web/skills").json()}
    assert listed["new-skill"]["enabled"] is True

    # 删除整目录进回收站
    r = client.delete("/api/capabilities/web/skills/new-skill")
    assert r.status_code == 200
    trash = list((client.packs / "capabilities/web/skills/.history/trash").iterdir())
    assert any(p.name.startswith("new-skill.") and p.is_dir() for p in trash)
    assert client.delete("/api/capabilities/web/skills/new-skill").status_code == 404


def test_track_skill_create_and_disable_keeps_body(client):
    """轨级技能同样可建/禁用；禁用时正文与其它字段原样保留。"""
    r = client.post("/api/tracks/ctf/skills", json={"name": "triage-x"})
    assert r.status_code == 201
    client.patch("/api/tracks/ctf/skills/triage-x/enabled", json={"enabled": False})
    raw = client.get("/api/tracks/ctf/skills/triage-x").json()["raw"]
    assert "enabled: false" in raw and "# triage-x" in raw
    listed = {s["name"]: s for s in client.get("/api/tracks/ctf/skills").json()}
    assert listed["triage-x"]["enabled"] is False


# ---------------- .history：两种命名 / diff / 回滚往返 / 防穿越 ----------------

def test_history_list_diff_rollback_roundtrip(client):
    redlines = client.packs / "tracks/ctf/rules/redlines.md"
    hist = redlines.parent / ".history"
    _write(hist / "20200101T000000Z_redlines.md", "# 旧版一 OLD1\n")
    _write(hist / "redlines.md.20200102T000002Z.bak", "# 旧版二 OLD2\n")
    redlines.write_text("# 当前版 CUR\n", encoding="utf-8")

    r = client.get("/api/packs/history", params={"file": "tracks/ctf/rules/redlines.md"})
    assert r.status_code == 200
    versions = r.json()["versions"]
    assert [v["version"] for v in versions] == [
        "redlines.md.20200102T000002Z.bak", "20200101T000000Z_redlines.md"]  # ts 倒序

    diff = client.get("/api/packs/history/diff", params={
        "file": "tracks/ctf/rules/redlines.md",
        "version": "20200101T000000Z_redlines.md"}).json()["diff"]
    assert "OLD1" in diff and "CUR" in diff

    # 回滚到旧版一：当前版先被再备份（返回件即 CUR，可凭它往返）
    r = client.post("/api/packs/history/rollback", params={
        "file": "tracks/ctf/rules/redlines.md",
        "version": "20200101T000000Z_redlines.md"})
    assert r.status_code == 200
    cur_backup = r.json()["current_backed_up"]
    assert cur_backup and cur_backup.endswith("_redlines.md")
    assert redlines.read_text(encoding="utf-8") == "# 旧版一 OLD1\n"
    # 列表多了回滚前的当前版备份
    versions_after = client.get("/api/packs/history", params={
        "file": "tracks/ctf/rules/redlines.md"}).json()["versions"]
    assert len(versions_after) == 3
    # 立刻再回滚（同秒！）：新备份不得覆盖 cur_backup，内容必须仍是 CUR
    r2 = client.post("/api/packs/history/rollback", params={
        "file": "tracks/ctf/rules/redlines.md", "version": cur_backup})
    assert r2.status_code == 200
    assert redlines.read_text(encoding="utf-8") == "# 当前版 CUR\n"


def test_history_put_creates_version_and_rejects_traversal(client):
    # PUT 编辑自动留版本（<ts>_<file> 命名）
    client.put("/api/tracks/ctf/roles/recon", json={"description": "改过了"})
    versions = client.get("/api/packs/history", params={
        "file": "tracks/ctf/roles/recon.yaml"}).json()["versions"]
    assert len(versions) == 1 and versions[0]["version"].endswith("_recon.yaml")

    # 防穿越：越层 / 非 packs 前缀 / 伪造 version 段
    assert client.get("/api/packs/history",
                      params={"file": "../../../Windows/win.ini"}).status_code == 422
    assert client.get("/api/packs/history",
                      params={"file": "config/mcp.json"}).status_code == 422
    assert client.get("/api/packs/history/diff", params={
        "file": "tracks/ctf/rules/redlines.md",
        "version": "../app.py"}).status_code == 422
    assert client.post("/api/packs/history/rollback", params={
        "file": "tracks/ctf/rules/redlines.md",
        "version": "19990101T000000Z_redlines.md"}).status_code == 404


# ---------------- redlines 缺失语义：200 + exists 标志（不再 404） ----------------

def test_rules_missing_returns_200_with_exists_false(client):
    # web 红线在夹具里存在
    r = client.get("/api/capabilities/web/rules")
    assert r.status_code == 200 and r.json()["exists"] is True
    assert r.json()["content"] == "# web 红线\n"
    # 只建包目录、不建 redlines：缺失是预期态，不给 404
    _write(client.packs / "capabilities/binary/pack.yaml", "kind: capability\nname: binary\n")
    r = client.get("/api/capabilities/binary/rules")
    assert r.status_code == 200 and r.json() == {"content": "", "exists": False}
    # 轨红线缺失同理
    _write(client.packs / "tracks/research/track.yaml", "name: research\n")
    r = client.get("/api/tracks/research/rules")
    assert r.status_code == 200 and r.json() == {"content": "", "exists": False}


# ---------------- doctor 端点 ----------------

def test_doctor_endpoint_flags_dangling_reference(client):
    r = client.put("/api/tracks/ctf/roles/recon", json={"skills": ["ghost-skill"]})
    assert r.status_code == 200
    rep = client.get("/api/packs/doctor").json()
    assert rep["counts"]["error"] >= 1
    assert any(i["code"] == "role-skill-missing" and "ghost-skill" in i["message"]
               for i in rep["issues"])
