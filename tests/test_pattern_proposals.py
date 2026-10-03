from pathlib import Path

from core.skills import proposals
from scripts.propose_patterns import propose_patterns


def _case(path: Path, title: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nkind: case\nstatus: approved\nvuln_class: [upload]\n---\n# {title}\n",
        encoding="utf-8")


def test_pattern_scan_creates_pending_proposal(tmp_path):
    packs = tmp_path / "packs"
    _case(packs / "kb/web/cases/one.md", "一")
    _case(packs / "kb/web/cases/two.md", "二")
    result = propose_patterns(packs, min_cases=2)
    assert len(result["proposals"]) == 1
    pending = proposals.list_proposals(packs, "pending")
    assert pending[0]["target"] == {"kind": "pattern", "cap": "web", "path": "patterns/upload.md"}
    assert (packs / "kb/web/patterns/upload.md").exists() is False
