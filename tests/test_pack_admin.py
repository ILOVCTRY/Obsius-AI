"""阶段 3A：技能 CRUD + enabled 开关 + .history 版本管理 + doctor 端点。

角色 CRUD 已退役（expert-pool M2，2026-09-21）：写端点 410 护栏保留，
角色盘上夹具随之移除；doctor 悬空引用改走专家口径。"""

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

    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     packs_root=str(packs), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        c.packs = packs  # type: ignore[attr-defined]
        yield c


# ---------------- 角色 CRUD 退役（expert-pool M2）：写端点 410，读端点切 experts 源 ----------------

def test_role_crud_retired_410(client):
    """角色由 packs/experts/ 单文件池管理，轨角色写端点退役返 410（专家管理 UI 随 M3 落地）。"""
    assert client.post("/api/tracks/ctf/roles", json={"name": "spare"}).status_code == 410
    assert client.put("/api/tracks/ctf/roles/recon",
                      json={"description": "x"}).status_code == 410
    assert client.delete("/api/tracks/ctf/roles/recon").status_code == 410
    # 读端点仍在：数据源已切 experts/ 池（本夹具无专家 → 空清单）
    assert client.get("/api/tracks/ctf/roles").status_code == 200
    assert client.get("/api/tracks/ctf/roles").json() == []


# ---------------- 专家池 CRUD（expert-pool M3：packs/experts/ 单文件池管理） ----------------

def _expert_payload(**over):
    body = {"name": "测试专家", "description": "M3 CRUD 测试", "persona": "测试 persona",
            "tracks": ["ctf"], "skills": ["demo-skill"], "task_types": ["generic"],
            "tools": ["run_cmd"], "max_steps": 120,
            "variants": {"ctf": {"persona": "ctf 专属 persona"}}}
    body.update(over)
    return body


def test_expert_crud_lifecycle(client):
    """新建（yaml 平铺序列化 + variants 平铺键）→ 读视图 → PUT 全字段覆写
    （None 不落键=全量语义）→ 删除进回收站；重名 409、protected 拒删。"""
    _write(client.packs / "experts/_generalist.yaml",
           "name: 通用\nskills: null\nprotected: true\n")
    r = client.post("/api/experts", json={"id": "test-expert", **_expert_payload()})
    assert r.status_code == 201, r.text
    raw = (client.packs / "experts/test-expert.yaml").read_text(encoding="utf-8")
    assert "name: 测试专家" in raw and "skills: [demo-skill]" in raw
    assert "variant_ctf_persona: ctf 专属 persona" in raw and "max_steps: 120" in raw

    view = client.get("/api/experts/test-expert").json()
    assert view["name"] == "测试专家" and view["tracks"] == ["ctf"]
    assert view["skills"] == ["demo-skill"] and view["max_steps"] == 120
    assert view["variants"] == {"ctf": {"persona": "ctf 专属 persona"}}
    assert view["file"] == "experts/test-expert.yaml"
    assert {"test-expert", "_generalist"} <= {e["id"] for e in client.get("/api/experts").json()}

    # PUT 全字段覆写：未提交字段不落键（skills 缺键 = 全量专家语义）
    r = client.put("/api/experts/test-expert",
                   json={"name": "改名", "tracks": ["ctf"], "variants": {}})
    assert r.status_code == 200
    raw = (client.packs / "experts/test-expert.yaml").read_text(encoding="utf-8")
    assert "name: 改名" in raw and "skills" not in raw and "persona" not in raw
    view = client.get("/api/experts/test-expert").json()
    assert view["skills"] is None and view["variants"] == {}

    assert client.post("/api/experts",
                       json={"id": "test-expert", **_expert_payload()}).status_code == 409
    assert client.delete("/api/experts/_generalist").status_code == 409
    assert client.delete("/api/experts/test-expert").status_code == 200
    assert client.get("/api/experts/test-expert").status_code == 404
    assert client.delete("/api/experts/test-expert").status_code == 404
    assert list((client.packs / "experts/.history/trash").glob("test-expert.*.bak"))


def test_expert_crud_validation(client):
    """写端点前置校验（doctor 同口径前移）：id 形制 / 未知技能 / 未知轨 /
    轨变体轨名 / 未注册任务类型 / max_steps / PUT 不存在 404。"""
    r = client.post("/api/experts", json={"id": "Bad_ID", "name": "x"})
    assert r.status_code == 422 and "专家 id" in r.json()["detail"]
    r = client.post("/api/experts", json={"id": "t1", "skills": ["ghost-skill"]})
    assert r.status_code == 422 and "未知技能" in r.json()["detail"]
    r = client.post("/api/experts", json={"id": "t1", "tracks": ["nope"]})
    assert r.status_code == 422 and "场景轨" in r.json()["detail"]
    r = client.post("/api/experts",
                    json={"id": "t1", "variants": {"pentest": {"persona": "x"}}})
    assert r.status_code == 422 and "轨变体" in r.json()["detail"]
    r = client.post("/api/experts", json={"id": "t1", "task_types": ["nope-type"]})
    assert r.status_code == 422 and "任务类型" in r.json()["detail"]
    assert client.post("/api/experts", json={"id": "t1", "max_steps": 0}).status_code == 422
    assert client.put("/api/experts/ghost", json={"name": "x"}).status_code == 404


def test_track_profiles_endpoint(client):
    """场景档清单端点（M4a）：目录缺省=空；档案=文件，读视图带 id/name 五件套字段。"""
    assert client.get("/api/tracks/ctf/profiles").json() == []
    _write(client.packs / "tracks/ctf/profiles/p1.yaml",
           "name: 档一\nexperts: [_generalist]\nboard_view: board\n"
           "playbook: 一句话剧本\nartifacts: [writeup]\n")
    profs = client.get("/api/tracks/ctf/profiles").json()
    assert len(profs) == 1 and profs[0]["id"] == "p1"
    assert profs[0]["name"] == "档一" and profs[0]["board_view"] == "board"
    assert profs[0]["experts"] == ["_generalist"] and profs[0]["playbook"] == "一句话剧本"


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
    assert "mode: self-contained" in raw
    assert "skill_open" in raw

    # cc 风格附属资源只从技能自身目录读取，不能穿越到 packs/kb。
    resource = client.packs / "capabilities/web/skills/new-skill/references/method.md"
    _write(resource, "自包含参考资料")
    r = client.get("/api/capabilities/web/skills/new-skill/resources/references/method.md")
    assert r.status_code == 200 and r.json()["content"] == "自包含参考资料"
    assert client.get(
        "/api/capabilities/web/skills/new-skill/resources/../SKILL.md"
    ).status_code in {404, 422}

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
    # PUT 编辑自动留版本（<ts>_<file> 命名；角色 PUT 已退役 M2，改用技能 PUT 验同一备份链路）
    client.put("/api/capabilities/web/skills/demo-skill",
               json={"content": _SKILL.replace("BODY_MARKER", "改过了")})
    versions = client.get("/api/packs/history", params={
        "file": "capabilities/web/skills/demo-skill/SKILL.md"}).json()["versions"]
    assert len(versions) == 1 and versions[0]["version"].endswith("_SKILL.md")

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
    # expert-pool M2：悬空引用体检走专家口径（role-skill-missing 随 roles/ 退役）
    _write(client.packs / "experts/bad-expert.yaml",
           "name: bad-expert\nskills: [ghost-skill]\n")
    rep = client.get("/api/packs/doctor").json()
    assert rep["counts"]["error"] >= 1
    assert any(i["code"] == "expert-skill-missing" and "ghost-skill" in i["message"]
               for i in rep["issues"])


def test_doctor_kb_structure_thin(client):
    """M3 丁结构体检（experience-sedimentation）：>800 字符且标题行 <2 的单段长文
    报 warning；有分节/refs/ 子树豁免；短条目不检。"""
    _write(client.packs / "kb/web/poc/单段.md", "# 单段手册\n" + "正文内容一长串。" * 150)
    _write(client.packs / "kb/web/poc/分节.md",
           "# 分节手册\n\n## 已验证路径\n" + "正文一长串。" * 150)
    _write(client.packs / "kb/web/refs/上游.md", "# 上游快照\n" + "原文一长串。" * 150)
    _write(client.packs / "kb/web/poc/短条目.md", "# 短条目\n存根。")
    rep = client.get("/api/packs/doctor").json()
    targets = [i["target"] for i in rep["issues"] if i["code"] == "kb-structure-thin"]
    assert any("单段.md" in t for t in targets)
    assert not any("分节.md" in t or "上游.md" in t or "短条目.md" in t for t in targets)
