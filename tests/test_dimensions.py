"""测试维度清单（agent-path-intent-loop M1）：解析 / 加载 / 面覆盖判定。"""

from pathlib import Path

import pytest

from core.dimensions import (
    dimension_coverage,
    dimension_spec,
    dimensions_for_type,
    is_converged,
    load_track_dimensions,
    parse_dimensions_file,
    render_intent,
)

PACKS = Path(__file__).resolve().parents[1] / "packs"


# ---------- 真实包断言 ----------

def test_pentest_dimensions_load():
    dims = load_track_dimensions(PACKS, "pentest")
    ids = [d["id"] for d in dims]
    assert "unauth" in ids and "sqli" in ids and "upload" in ids
    # 顺序稳定、id 唯一
    assert len(ids) == len(set(ids))
    for d in dims:
        assert d["applies_to"], d
        assert d["intent"], d


def test_dimensions_for_type():
    dims = load_track_dimensions(PACKS, "pentest")
    url_dims = {d["id"] for d in dimensions_for_type(dims, "url")}
    svc_dims = {d["id"] for d in dimensions_for_type(dims, "service")}
    assert "sqli" in url_dims and "upload" in url_dims
    assert "weakpass" in svc_dims
    assert "sqli" not in svc_dims  # sqli 只适用 url
    assert dimensions_for_type(dims, None) == []


def test_render_intent():
    dims = load_track_dimensions(PACKS, "pentest")
    spec = next(d for d in dims if d["id"] == "sqli")
    out = render_intent(spec, "https://a.example.com/login")
    assert "https://a.example.com/login" in out
    assert "{asset}" not in out


# ---------- 面覆盖判定 ----------

def _dims():
    return [{"id": "a", "name": "A", "applies_to": ["url"], "intent": "", "evidence_hint": ""},
            {"id": "b", "name": "B", "applies_to": ["url"], "intent": "", "evidence_hint": ""}]


def test_coverage_closed_covers():
    intents = [{"dimension": "a", "status": "closed"}]
    cov = dimension_coverage(intents, _dims())
    assert cov["covered"] == ["a"]
    assert cov["uncovered"] == ["b"]
    assert cov["open"] == []


def test_coverage_open_blocks():
    # 面 a 有 open 意图在跑 → 不算覆盖，且进 open
    intents = [{"dimension": "a", "status": "closed"},
               {"dimension": "a", "status": "open"}]
    cov = dimension_coverage(intents, _dims())
    assert "a" in cov["uncovered"] and "a" in cov["open"]


def test_coverage_no_intent_uncovered():
    cov = dimension_coverage([], _dims())
    assert cov["covered"] == []
    assert cov["uncovered"] == ["a", "b"]


def test_coverage_ignores_dimensionless_intent():
    # 未标面归属的意图不参与判定
    intents = [{"status": "closed"}, {"dimension": "", "status": "closed"}]
    cov = dimension_coverage(intents, _dims())
    assert cov["covered"] == []


def test_is_converged():
    dims = _dims()
    assert is_converged([], dims) is False
    assert is_converged([{"dimension": "a", "status": "closed"},
                         {"dimension": "b", "status": "closed"}], dims) is True
    assert is_converged([{"dimension": "a", "status": "closed"}], dims) is False
    # 无维度机制 → 不拦
    assert is_converged([], []) is True


# ---------- 解析 / 覆写 / 容错 ----------

def test_parse_and_override(tmp_path):
    (tmp_path / "tracks" / "pentest").mkdir(parents=True)
    (tmp_path / "tracks" / "pentest" / "dimensions.yaml").write_text(
        "dimensions:\n"
        "  - id: x\n"
        "    name: X\n"
        "    applies_to: [url]\n"
        "    intent: test {asset}\n",
        encoding="utf-8")
    dims = load_track_dimensions(tmp_path, "pentest")
    assert [d["id"] for d in dims] == ["x"]
    # 项目覆写：同名整体替换 + 新增
    dims2 = load_track_dimensions(tmp_path, "pentest", {
        "dimensions": [
            {"id": "x", "name": "X2", "applies_to": ["host"]},
            {"id": "y", "name": "Y", "applies_to": ["url"]},
        ]})
    assert [d["id"] for d in dims2] == ["x", "y"]
    assert dims2[0]["name"] == "X2" and dims2[0]["applies_to"] == ["host"]


def test_missing_file_is_empty(tmp_path):
    assert load_track_dimensions(tmp_path, "pentest") == []


def test_bad_yaml_skipped(tmp_path):
    (tmp_path / "tracks" / "pentest").mkdir(parents=True)
    p = tmp_path / "tracks" / "pentest" / "dimensions.yaml"
    p.write_text("dimensions:\n  - id: ok\n    applies_to: [url]\n"
                 "  - name: no-id\n    applies_to: [url]\n", encoding="utf-8")
    dims = load_track_dimensions(tmp_path, "pentest")
    assert [d["id"] for d in dims] == ["ok"]  # 缺 id 的条目跳过


def test_spec_rejects_bad_asset_type():
    with pytest.raises(ValueError):
        dimension_spec({"id": "z", "applies_to": ["nonsense"]})
    with pytest.raises(ValueError):
        dimension_spec({"name": "no id", "applies_to": ["url"]})


def test_parse_rejects_unknown_top_key(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text("bogus:\n  - id: x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_dimensions_file(p)
