"""项目工作区文件树端点测试。"""

import os
from pathlib import Path

from fastapi.testclient import TestClient

from core.api.app import create_app


def test_workspace_tree_filters_sensitive_entries_and_limits(tmp_path):
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "tree", "track": "ctf"}).json()["id"]
        root = Path(tmp_path / "workspaces").glob("*")
        project_root = next(p for p in root if (p / "project.json").exists())
        (project_root / "artifacts").mkdir(exist_ok=True)
        (project_root / "artifacts" / "report.md").write_text("ok", encoding="utf-8")
        (project_root / ".git").mkdir()
        (project_root / ".git" / "secret").write_text("no", encoding="utf-8")
        (project_root / ".env").write_text("KEY=x", encoding="utf-8")
        (project_root / "blackboard.db").write_text("db", encoding="utf-8")

        response = client.get(f"/api/projects/{pid}/workspace/tree")
        assert response.status_code == 200
        body = response.json()
        paths = {n["path"] for n in body["nodes"]}
        assert "artifacts" in paths
        artifacts = next(n for n in body["nodes"] if n["path"] == "artifacts")
        assert artifacts["children"][0]["path"] == "artifacts/report.md"
        assert ".git" not in paths
        assert ".env" not in paths
        assert "blackboard.db" not in paths
        assert all(not Path(n["path"]).is_absolute() for n in body["nodes"])


def test_workspace_tree_missing_project_is_404(tmp_path):
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as client:
        assert client.get("/api/projects/nope/workspace/tree").status_code == 404
