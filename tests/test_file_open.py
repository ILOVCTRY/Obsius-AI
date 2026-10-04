"""本地文件动作端点（会话流文件卡）测试：scope 白名单 + 四动作。

`open`/`reveal` 会真的拉起资源管理器/系统程序，此处**不测**——只覆盖
resolve/content 与全部拒绝路径。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    # packs 内含相对路径配置，须在 tmp 下当 cwd（与 tests/test_api.py 同做法）
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "workspaces"
    packs = tmp_path / "packs"
    tools = tmp_path / "tools"
    # 最小 packs 夹具：建项校验轨/包存在性（list_packs 扫 tracks/ 与 capabilities/）
    (packs / "tracks" / "ctf").mkdir(parents=True)
    (packs / "tracks" / "ctf" / "track.yaml").write_text("label: CTF\n", encoding="utf-8")
    (packs / "capabilities").mkdir()
    tools.mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = create_app(workspace_root=str(ws), packs_root=str(packs),
                     tools_root=str(tools), static_dir=str(dist),
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        yield c, ws, packs, tools


def _open(client, pid, path, action):
    return client.post(f"/api/projects/{pid}/files/open",
                       json={"path": path, "action": action})


def _proj_dir(ws) -> Path:
    """项目工作区落盘目录（slug 命名，非 pid）——workspaces/ 下唯一子目录。"""
    subs = [d for d in ws.iterdir() if d.is_dir() and not d.name.startswith(".")]
    assert len(subs) == 1, subs
    return subs[0]


def test_resolve_returns_abs_path_without_side_effect(app_env):
    client, ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    r = _open(client, pid, "notes/readme.md", "resolve")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["action"] == "resolve"
    # 只解析不落盘：文件并未被创建
    assert not (_proj_dir(ws) / "notes" / "readme.md").exists()
    assert body["abs_path"].endswith("readme.md")


def test_content_reads_text(app_env):
    client, ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    target = _proj_dir(ws) / "a.txt"
    target.write_text("hello 文件卡", encoding="utf-8")
    r = _open(client, pid, "a.txt", "content")
    assert r.status_code == 200
    assert r.json()["content"] == "hello 文件卡"
    assert r.json()["truncated"] is False


def test_content_on_directory_is_422(app_env):
    client, ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    (_proj_dir(ws) / "sub").mkdir(parents=True, exist_ok=True)
    r = _open(client, pid, "sub", "content")
    assert r.status_code == 422 and "目录" in r.json()["detail"]


def test_missing_file_is_404(app_env):
    client, _ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    r = _open(client, pid, "nope.txt", "open")
    assert r.status_code == 404


def test_traversal_rejected(app_env):
    client, _ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    for bad in ("../../etc/passwd", "..\\..\\windows\\system32\\drivers\\etc\\hosts",
                "sub/../../../outside.txt"):
        r = _open(client, pid, bad, "resolve")
        assert r.status_code == 422, bad
        assert "越界" in r.json()["detail"]


def test_absolute_path_outside_scope_rejected(app_env):
    client, _ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    r = _open(client, pid, "C:/Windows/System32/drivers/etc/hosts", "resolve")
    assert r.status_code == 422 and "越界" in r.json()["detail"]


def test_packs_and_tools_are_in_scope(app_env):
    client, _ws, packs, tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    (packs / "capabilities" / "web" / "skills" / "x").mkdir(parents=True)
    (packs / "capabilities" / "web" / "skills" / "x" / "SKILL.md").write_text("# x", encoding="utf-8")
    (tools / "registry.json").write_text("{}", encoding="utf-8")
    assert _open(client, pid, str(packs / "capabilities" / "web" / "skills" / "x" / "SKILL.md"),
                 "content").status_code == 200
    assert _open(client, pid, str(tools / "registry.json"), "content").status_code == 200


def test_unknown_action_is_422(app_env):
    client, _ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/files/open",
                    json={"path": "a.txt", "action": "exec"})
    assert r.status_code == 422  # pydantic Literal 校验


def test_empty_path_is_422(app_env):
    client, _ws, _packs, _tools = app_env
    pid = client.post("/api/projects", json={"name": "P", "track": "ctf"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/files/open",
                    json={"path": "", "action": "resolve"})
    assert r.status_code == 422


def test_unknown_project_is_404(app_env):
    client, _ws, _packs, _tools = app_env
    r = _open(client, "no-such-project", "a.txt", "resolve")
    assert r.status_code == 404
