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
