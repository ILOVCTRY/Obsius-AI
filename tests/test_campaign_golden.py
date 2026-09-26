"""campaign 召回黄金集回归（retrieval-upgrade M4，2026-09-23）。

黄金集 tests/fixtures/campaign-golden.yaml：mission 口吻 query → 期望召回的
沉淀条目（top-3 必含）。打分链任何改动（切分/停用词/字段加权/衰减/热度）
必须跑本集防退化；与 test_retrieval_golden（检索链）同套维护纪律。
"""
from pathlib import Path

import pytest
import yaml

from core.blackboard.campaign import CampaignMemory

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests" / "fixtures" / "campaign-golden.yaml"


def _load() -> tuple[list[dict], list[dict]]:
    data = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    return data["entries"], data["cases"]


_CASES = _load()[1]


@pytest.mark.parametrize("case_idx", range(len(_CASES)))
def test_campaign_golden_recall(tmp_path, case_idx):
    entries, cases = _load()
    case = cases[case_idx]
    camp = CampaignMemory(tmp_path / "campaign.db")  # per-case 独立库防 usage 互染
    try:
        slug_to_id: dict[str, str] = {}
        for e in entries:
            row = camp.add("proj-golden", e["track"], e.get("capability", ""),
                           e.get("task_type", ""), e["title"], e["content"],
                           tags=e.get("tags") or [])
            slug_to_id[e["id"]] = row["id"]
        hits = camp.recall(case["query"], track=case.get("track", ""),
                           capability=case.get("capability", ""))
        top3 = {h["id"] for h in hits[:3]}
        want = {slug_to_id[s] for s in case["expect"]}
        missing = want - top3
        assert not missing, (
            f"query={case['query']!r} miss={sorted(missing)}\n"
            f"top3={[h['title'] for h in hits[:3]]}")
    finally:
        camp.close()
