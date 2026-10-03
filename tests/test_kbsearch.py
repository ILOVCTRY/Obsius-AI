from pathlib import Path

from core.skills.kbsearch import KbSearchIndex


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "packs"
    for rel, body in (
        ("refs/raw.md", "# 原始资料\n文件上传 SQL"),
        ("cases/verified.md", "---\nkind: case\n---\n# 已验证案例\n文件上传"),
        ("patterns/upload.md", "---\nkind: pattern\n---\n# 上传模式\n文件上传"),
        ("playbooks/upload.md", "---\nkind: playbook\n---\n# 上传方法论\n文件上传"),
    ):
        path = root / "kb" / "web" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (root / "kb" / "web" / "refs" / "screenshot.png").write_bytes(b"PNG")
    return root


def test_incremental_index_layers_and_attachments(tmp_path):
    root = _tree(tmp_path)
    index = KbSearchIndex(root)
    assert index.sync(["web"]) == {"added": 5, "updated": 0, "removed": 0, "total": 5}
    rows = index.search("文件上传", capabilities=["web"])
    assert [row["kind"] for row in rows[:4]] == ["playbook", "pattern", "case", "ref"]
    with index._connect() as con:
        assert con.execute("SELECT is_attachment FROM kb_documents WHERE path='web/refs/screenshot.png'").fetchone()[0] == 1

    (root / "kb" / "web" / "refs" / "raw.md").write_text("# 原始资料\nSSRF", encoding="utf-8")
    (root / "kb" / "web" / "cases" / "verified.md").unlink()
    result = index.sync(["web"])
    assert result["updated"] == 1 and result["removed"] == 1
    assert index.search("SSRF", capabilities=["web"])[0]["path"] == "refs/raw.md"
