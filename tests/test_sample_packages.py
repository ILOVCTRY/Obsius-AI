import io
import json
import struct
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.sample_packages import SamplePackageError, SamplePackageStore


def _app(tmp_path: Path):
    return create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                      mission_poll_interval=0)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buf.getvalue()


def test_package_store_rejects_archive_traversal(tmp_path):
    source = tmp_path / "bad.zip"
    source.write_bytes(_zip_bytes({"../escape.txt": b"no"}))
    store = SamplePackageStore(tmp_path / "workspaces")
    with pytest.raises(SamplePackageError, match="路径穿越"):
        store.import_source(tmp_path / "workspaces" / "p", source)


def test_package_api_directory_targets_dependencies_and_dedup(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "package-test"})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        # The small PE-like headers are enough for format discovery.  The import
        # name is intentionally present to exercise the dependency edge builder.
        exe = b"MZ" + b"\0" * 32 + b"helper.dll\0"
        dll = b"MZ" + b"\0" * 32
        response = client.post(
            f"/api/projects/{pid}/sample-packages",
            files=[
                ("files", ("Game/game.exe", exe, "application/octet-stream")),
                ("files", ("Game/helper.dll", dll, "application/octet-stream")),
            ],
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["file_count"] == 2
        assert {row["path"] for row in body["targets"]} == {"Game/game.exe", "Game/helper.dll"}
        assert body["dependencies"] == [{"from": "Game/game.exe", "to": "Game/helper.dll", "kind": "binary-import"}]
        # 回归（2026-10-12）：导入响应必须是**完整**分析包对象（含 entries/origin）
        # ——前端 setPkg(响应) 据此渲染文件树，此前只回摘要致 entries 缺失，用户
        # 上传成功却看不到分析包、须手动刷新页面。
        assert {row["path"] for row in body["entries"]} == {"Game/game.exe", "Game/helper.dll"}
        assert body["origin"]["source_type"] == "folder"
        assert body["created_at"]

        package_id = body["package_id"]
        version_id = body["version_id"]
        selected = client.post(
            f"/api/projects/{pid}/sample-packages/{package_id}/versions/{version_id}/targets",
            json={"target_ids": [body["targets"][0]["target_id"]]},
        )
        assert selected.status_code == 200
        assert len(selected.json()["target_ids"]) == 1

        listed = client.get(f"/api/projects/{pid}/sample-packages").json()
        assert len(listed) == 1
        detail = client.get(f"/api/projects/{pid}/sample-packages/{package_id}").json()
        assert detail["version_id"] == version_id
        assert Path(detail["tree_ref"]).is_dir()
        # 内容树落**项目工作区内**（workspaces/<slug>/trees/<hash>/），不再写全局 sample-store
        assert Path(detail["tree_ref"]).is_relative_to(tmp_path / "workspaces")
        assert Path(detail["tree_ref"]).parent.name == "trees"
        assert not (tmp_path / "data" / "sample-store").exists()

        # Same bytes and paths resolve to the same immutable package version.
        again = client.post(
            f"/api/projects/{pid}/sample-packages",
            files=[
                ("files", ("Game/game.exe", exe, "application/octet-stream")),
                ("files", ("Game/helper.dll", dll, "application/octet-stream")),
            ],
        )
        assert again.status_code == 202
        assert again.json()["manifest_sha256"] == body["manifest_sha256"]
        listed_again = client.get(f"/api/projects/{pid}/sample-packages").json()
        assert len(listed_again) == 1 and listed_again[0]["import_count"] == 2


def test_package_upload_stays_inside_project_workspace(tmp_path):
    """上传物落**项目工作区内**（2026-10-13）：内容树 → `workspaces/<slug>/trees/<hash>/`、
    原始上传物 → `workspaces/<slug>/raw/`；全局 `data/sample-store/` 不产生任何写入。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "inside-ws"}).json()["id"]
        archive = _zip_bytes({"bin/app.exe": b"MZ" + b"\0" * 32})
        body = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("app.zip", archive, "application/zip")},
        ).json()
        project_dir = tmp_path / "workspaces" / "inside-ws"
        tree = Path(body["tree_ref"])
        assert tree.is_relative_to(project_dir / "trees")
        assert (tree / "bin/app.exe").is_file()
        # 原始上传物也落项目内 raw/
        assert Path(body["raw_ref"]).is_relative_to(project_dir / "raw")
        assert Path(body["raw_ref"]).is_file()
        # 全局存储根完全不产生
        assert not (tmp_path / "data" / "sample-store").exists()


def test_package_target_register_asset_no_copy(tmp_path):
    """登记为资产（2026-10-12）：PE 目标 → binary 资产，meta 带包坐标 + **包内相对路径**，
    且**不复制**文件进项目 samples/（引用全局内容树）；重复登记幂等；非二进制 422；
    删除该资产不动全局共享树文件。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "register-asset"}).json()["id"]
        exe = b"MZ" + b"\0" * 32
        body = client.post(
            f"/api/projects/{pid}/sample-packages",
            files=[("files", ("Game/game.exe", exe, "application/octet-stream")),
                   ("files", ("Game/notes.txt", b"hi", "text/plain"))],
        ).json()
        exe_target = next(t for t in body["targets"] if t["path"] == "Game/game.exe")
        txt_target = next(t for t in body["targets"] if t["path"] == "Game/notes.txt")
        base = (f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/"
                f"{body['version_id']}/targets")

        # 非二进制格式拒绝（APK/DEX/文本仍走 .../analyze）
        assert client.post(f"{base}/{txt_target['target_id']}/register").status_code == 422

        r = client.post(f"{base}/{exe_target['target_id']}/register")
        assert r.status_code == 200, r.text
        sha = r.json()["binary_sha256"]
        asset = next(a for a in client.get(f"/api/projects/{pid}/assets?type=binary").json()
                     if a["value"] == sha)
        meta = asset["meta"]
        assert meta["source"] == "sample-package"
        assert meta["package_id"] == body["package_id"]
        assert meta["version_id"] == body["version_id"]
        assert meta["target_id"] == exe_target["target_id"]
        assert meta["path"] == "Game/game.exe"          # 包内相对路径
        # **不复制**：任何项目 samples/ 下都没有该 sha 命名的文件
        assert list((tmp_path / "workspaces").glob(f"*/samples/{sha}*")) == []
        # 内容在**项目内容树**里（tree_ref 绝对路径，落在项目工作区内）
        assert (Path(body["tree_ref"]) / "Game/game.exe").is_file()
        assert Path(body["tree_ref"]).is_relative_to(tmp_path / "workspaces")

        # 重复登记幂等（不新增资产行）
        assert client.post(f"{base}/{exe_target['target_id']}/register").status_code == 200
        assert len([a for a in client.get(f"/api/projects/{pid}/assets?type=binary").json()
                    if a["value"] == sha]) == 1

        # 删除资产不删项目内容树文件（_purge_sample_files 护栏）
        assert client.delete(f"/api/projects/{pid}/binaries/{sha}").status_code == 200
        assert (Path(body["tree_ref"]) / "Game/game.exe").is_file()


def test_package_registered_asset_triage_uses_global_tree(tmp_path):
    """登记为资产后，逆向工作台「开始分析」（/binaries/{sha}/triage）解析到**全局包树**
    里的文件（项目外、无复制桥），而非项目内 samples/。"""
    app = _app(tmp_path)
    seen: list[str] = []

    class _RecService:
        def headless_backends(self):
            return [object()]

        def export_to_cache(self, binary, *, progress=None, stop_event=None,
                            engine=None):
            seen.append(str(binary))
            return ({"functions": [{"address": 4096, "name": "main"}],
                     "sections": [], "imports": {}},
                    {"name": f"{engine}-headless", "db_path": None})

    app.state.rev_service_factory = lambda _project: _RecService()
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "triage-reg"}).json()["id"]
        body = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("main.elf", b"\x7fELF" + b"\0" * 16, "application/octet-stream")},
        ).json()
        target = next(t for t in body["targets"] if t["format"] == "elf")
        reg = client.post(
            f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/"
            f"{body['version_id']}/targets/{target['target_id']}/register").json()
        sha = reg["binary_sha256"]
        start = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
        assert start.status_code == 202, start.text
        job_id = start.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] != "running":
                break
        assert job["status"] == "done", job
        # 导出的是全局内容树里的文件（项目外）
        assert seen and Path(seen[0]).is_relative_to(Path(body["tree_ref"]))


def test_package_oversize_target_blocks_auto_triage(tmp_path):
    """自动分析上限（2026-10-13）：≥10MB 样本「开始分析」（/binaries/{sha}/triage）被拒 422，
    引导改走 IDA 拉取（避免 headless 全量分析拖垮时间）。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "oversize"}).json()["id"]
        big = b"\x7fELF" + b"\0" * (10 * 1024 * 1024)          # ≥10MB
        body = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("big.elf", big, "application/octet-stream")},
        ).json()
        target = next(t for t in body["targets"] if t["format"] == "elf")
        reg = client.post(
            f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/"
            f"{body['version_id']}/targets/{target['target_id']}/register").json()
        sha = reg["binary_sha256"]
        r = client.post(f"/api/projects/{pid}/binaries/{sha}/triage")
        assert r.status_code == 422, r.text
        assert "10MB" in r.json()["detail"]


def test_folder_upload_allows_empty_files(tmp_path):
    """回归（2026-10-12）：文件夹上传含 0 字节文件时不再让**整批** 422「空文件」
    失败——空文件是合法目录成员（压缩包导入同样允许空文件）。单文件上传仍拒绝
    空文件（无意义上传）。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "empty-folder"}).json()["id"]

        def multipart(entries):
            parts = []
            for name, content in entries:
                parts.append(("files", (name, content, "application/octet-stream")))
                parts.append(("relative_paths", (None, name)))
            return parts

        r = client.post(f"/api/projects/{pid}/sample-packages",
                        files=multipart([("Game/game.exe", b"MZ" + b"\0" * 32),
                                         ("Game/empty.txt", b"")]))
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["file_count"] == 2
        assert {t["path"] for t in body["targets"]} == {"Game/game.exe", "Game/empty.txt"}

        # 追加上传同样允许空文件
        r2 = client.post(f"/api/projects/{pid}/sample-packages/files",
                         files=multipart([("extra/blank.bin", b"")]))
        assert r2.status_code == 202, r2.text

        # 单文件上传空文件仍拒绝（无意义上传）
        single = client.post(f"/api/projects/{pid}/sample-packages",
                             files={"file": ("empty.bin", b"", "application/octet-stream")})
        assert single.status_code == 422


def test_package_api_archive(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "archive-test"}).json()["id"]
        archive = _zip_bytes({"lib/libgame.so": b"\x7fELF" + b"\0" * 8,
                              "assets/config.json": b"{}"})
        response = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("game.zip", archive, "application/zip")},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["file_count"] == 2
        assert body["raw_sha256"]


def test_package_resumable_upload(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "chunk-test"}).json()["id"]
        payload = b"MZ" + b"payload"
        created = client.post(
            f"/api/projects/{pid}/sample-packages/uploads",
            json={"filename": "app.exe", "total_size": len(payload), "total_chunks": 2},
        )
        assert created.status_code == 201, created.text
        upload_id = created.json()["upload_id"]
        assert client.put(
            f"/api/projects/{pid}/sample-packages/uploads/{upload_id}/chunks/1",
            content=payload[4:],
        ).status_code == 200
        assert client.put(
            f"/api/projects/{pid}/sample-packages/uploads/{upload_id}/chunks/0",
            content=payload[:4],
        ).status_code == 200
        done = client.post(
            f"/api/projects/{pid}/sample-packages/uploads/{upload_id}/complete")
        assert done.status_code == 202, done.text
        assert done.json()["file_count"] == 1
        assert client.get(
            f"/api/projects/{pid}/sample-packages/uploads/{upload_id}").json()["status"] == "complete"


class _FakeStaticService:
    def headless_backends(self):
        return [object()]

    def export_to_cache(self, binary, *, engine=None):
        return ({
            "functions": [{"address": 4096, "name": "main"}],
            "sections": [{"name": ".text"}],
            "imports": {"libc.so.6": ["puts"]},
        }, {"name": f"{engine}-headless", "db_path": None})


def test_package_target_static_analysis_uses_existing_headless_service(tmp_path):
    app = _app(tmp_path)
    app.state.rev_service_factory = lambda _project: _FakeStaticService()
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "target-test"}).json()["id"]
        response = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("main.elf", b"\x7fELF" + b"\0" * 16, "application/octet-stream")},
        )
        body = response.json()
        target = next(item for item in body["targets"] if item["format"] == "elf")
        start = client.post(
            f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/{body['version_id']}/targets/{target['target_id']}/analyze",
            json={"engine": "ghidra"},
        )
        assert start.status_code == 202, start.text
        job_id = start.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] != "running":
                break
        assert job["status"] == "done", job
        assert job["result"]["function_count"] == 1
        detail = client.get(
            f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/{body['version_id']}/targets/{target['target_id']}"
        ).json()
        assert detail["analysis"]["status"] == "ok"
        assert detail["analysis"]["engine"] == "ghidra"
        assert detail["analysis"]["binary_sha256"]
        assets = client.get(f"/api/projects/{pid}/assets?type=binary").json()
        binary = next(item for item in assets if item["type"] == "binary")
        assert binary["value"] == detail["analysis"]["binary_sha256"]
        assert binary["meta"]["source"] == "sample-package"


def test_delete_sample_package_removes_package_and_binary_dependencies(tmp_path):
    app = _app(tmp_path)
    app.state.rev_service_factory = lambda _project: _FakeStaticService()
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "delete-package-test"}).json()["id"]
        response = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("main.elf", b"\x7fELF" + b"\0" * 16, "application/octet-stream")},
        )
        package = response.json()
        target = package["targets"][0]
        start = client.post(
            f"/api/projects/{pid}/sample-packages/{package['package_id']}/versions/{package['version_id']}/targets/{target['target_id']}/analyze",
            json={"engine": "ghidra"},
        ).json()
        for _ in range(100):
            job = client.get(f"/api/jobs/{start['job_id']}").json()
            if job["status"] != "running":
                break
        assert job["status"] == "done", job
        deleted = client.delete(f"/api/projects/{pid}/sample-packages")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] == package["package_id"]
        assert client.get(f"/api/projects/{pid}/sample-packages/current").status_code == 404
        assert client.get(f"/api/projects/{pid}/assets?type=binary").json() == []


def test_package_target_non_binary_returns_structured_unavailable(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "resource-test"}).json()["id"]
        response = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("metadata.json", b"{}", "application/json")},
        )
        body = response.json()
        target = body["targets"][0]
        start = client.post(
            f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/{body['version_id']}/targets/{target['target_id']}/analyze"
        )
        assert start.status_code == 202
        job_id = start.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] != "running":
                break
        assert job["status"] == "done"
        assert job["result"]["status"] == "analyzer-unavailable"


def test_package_preview_text_hex_archive_and_content(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "preview-test"}).json()["id"]
        archive = _zip_bytes({"inside.txt": b"hello"})
        response = client.post(
            f"/api/projects/{pid}/sample-packages",
            files=[
                ("files", ("notes.json", b'{"ok": true}', "application/json")),
                ("files", ("bundle.zip", archive, "application/zip")),
            ],
        )
        assert response.status_code == 202, response.text
        text_preview = client.get(
            f"/api/projects/{pid}/sample-packages/preview?path=notes.json").json()
        assert text_preview["kind"] == "text"
        assert '"ok": true' in text_preview["text"]
        assert text_preview["hex_rows"][0]["ascii"].startswith('{"ok"')
        content = client.get(
            f"/api/projects/{pid}/sample-packages/content?path=notes.json")
        assert content.status_code == 200 and content.content == b'{"ok": true}'
        archive_preview = client.get(
            f"/api/projects/{pid}/sample-packages/preview?path=bundle.zip").json()
        assert archive_preview["kind"] == "structure"
        assert archive_preview["archive"]["entry_count"] == 1


def _dex_bytes(*, classes=3, methods=7):
    data = bytearray(0x70)
    data[:8] = b"dex\n039\0"
    struct.pack_into("<I", data, 0x20, len(data))
    struct.pack_into("<I", data, 0x24, 0x70)
    struct.pack_into("<I", data, 0x28, 0x12345678)
    struct.pack_into("<I", data, 0x58, methods)
    struct.pack_into("<I", data, 0x60, classes)
    return bytes(data)


def test_android_registered_analyzers(tmp_path):
    app = _app(tmp_path)
    manifest = b'''<?xml version="1.0"?><manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.demo.game" android:versionCode="9"><uses-permission android:name="android.permission.INTERNET"/><application android:debuggable="true"><activity android:name=".MainActivity"/><service android:name=".GameService"/></application></manifest>'''
    apk = _zip_bytes({"AndroidManifest.xml": manifest, "classes.dex": _dex_bytes(),
                      "lib/arm64-v8a/libgame.so": b"\x7fELF" + b"\0" * 20,
                      "lib/x86_64/libgame.so": b"\x7fELF" + b"\0" * 20})
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "android-test"}).json()["id"]
        response = client.post(
            f"/api/projects/{pid}/sample-packages",
            files={"file": ("game.apk", apk, "application/vnd.android.package-archive")},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        targets = {row["path"]: row for row in body["targets"]}
        assert body["source_analysis"]["analyzer"] == "android-package"
        assert body["source_analysis"]["package"]["dex_files"] == ["classes.dex"]
        assert set(body["source_analysis"]["package"]["native_abis"]) == {"arm64-v8a", "x86_64"}
        assert {"AndroidManifest.xml", "classes.dex", "lib/arm64-v8a/libgame.so"} <= set(targets)

        for wanted in ("AndroidManifest.xml", "classes.dex"):
            target = targets[wanted]
            start = client.post(
                f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/{body['version_id']}/targets/{target['target_id']}/analyze"
            )
            assert start.status_code == 202, start.text
            job_id = start.json()["job_id"]
            for _ in range(100):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] != "running":
                    break
            assert job["status"] == "done", job
            report = job["result"]
            assert report["status"] == "ok"
            if wanted == "classes.dex":
                assert report["analyzer"] == "dex-static"
                assert report["dex"]["class_defs_size"] == 3
                assert report["dex"]["method_ids_size"] == 7
            else:
                assert report["analyzer"] == "android-manifest"
                assert report["manifest"]["package"] == "com.demo.game"
                assert report["manifest"]["permissions"] == ["android.permission.INTERNET"]
                assert report["manifest"]["components"]["activity"] == [".MainActivity"]

        apk_target = targets["classes.dex"]
        detail = client.get(
            f"/api/projects/{pid}/sample-packages/{body['package_id']}/versions/{body['version_id']}/targets/{apk_target['target_id']}"
        ).json()
        assert detail["analysis"]["status"] == "ok"
