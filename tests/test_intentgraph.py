"""项目级意图推导图测试（intent-graph，2026-10-13）。

覆盖：样本归属（binary 直挂 / 嵌套 binary 取最近 / 非 binary 锚点自成节点 /
完全无锚点落兜底）/ 样本标签口径 / outcome 边 / 无主发现挂样本 / FP 不上图 /
derive 续链（经收尾意图起边、无主落样本、自环退回样本）/ 死路 default_hidden +
bypass 穿通 / counts / 空项目 / Kahn DAG 断言（bypass 除外）。
"""

import pytest

from core.blackboard.intentgraph import (
    UNATTACHED_ID, _assert_dag, build_intent_graph,
)

T0 = "2026-10-13T10:00:00+00:00"


def _asset(aid: str, type_: str, value: str, *, parent=None,
           filename=None) -> dict:
    return {"id": aid, "type": type_, "value": value, "parent_id": parent,
            "meta": {"filename": filename} if filename else {}}


def _finding(fid: str, title: str, *, target=None, severity="medium",
             status="unverified", category="intel", vuln_class="") -> dict:
    return {"id": fid, "title": title, "target_asset_id": target,
            "severity": severity, "status": status, "category": category,
            "vuln_class": vuln_class}


def _intent(iid: str, statement: str, *, target=None, status="open",
            outcome="", refs=None, basis=None, evidence=None,
            created=T0, closed=None, author="chat-1") -> dict:
    return {"id": iid, "statement": statement, "target_asset_id": target,
            "dimension": "", "basis_refs": basis or [], "status": status,
            "outcome_type": outcome, "outcome_refs": refs or [],
            "dead_reason": "", "evidence_refs": evidence or [],
            "author": author, "created_at": created, "closed_at": closed}


@pytest.fixture()
def samples():
    """主样本 + 无 meta.filename 的样本 + 嵌套其下的样本。"""
    return [
        _asset("s1", "binary", "a" * 64, filename="main.exe"),
        _asset("s2", "binary", "b" * 64),
        _asset("s2c", "binary", "c" * 64, parent="s2", filename="inner.dll"),
    ]


def _build(assets, *, findings=None, intents=None) -> dict:
    return build_intent_graph(None, "p1", assets=assets,
                              findings=findings or [], intents=intents or [])


def _edges(out: dict, kind: str) -> list[tuple[str, str]]:
    return [(e["source"], e["target"]) for e in out["edges"] if e["kind"] == kind]


def _node(out: dict, nid: str) -> dict:
    return next(n for n in out["nodes"] if n["id"] == nid)


# ---------- 样本归属 ----------

def test_intent_attaches_to_sample(samples):
    out = _build(samples, intents=[_intent("i1", "假设", target="s1")])
    assert _edges(out, "derive") == [("s1", "i1")]
    assert _node(out, "i1")["sample_id"] == "s1"


def test_nested_binary_resolves_to_nearest_sample(samples):
    """发现挂嵌套 binary → 归到最近的 binary，不是链顶样本。"""
    out = _build(samples, findings=[_finding("f1", "x", target="s2c")])
    assert _edges(out, "derive") == [("s2c", "f1")]
    assert _node(out, "f1")["sample_id"] == "s2c"


def test_non_binary_anchor_gets_own_sample_node():
    """锚在 host/domain（无 binary 祖先）→ 该资产自成样本节点，不静默丢弃。"""
    assets = [_asset("h1", "host", "10.0.0.1")]
    out = _build(assets, intents=[_intent("i1", "云端点", target="h1")])
    assert _node(out, "h1")["asset_type"] == "host"
    assert _edges(out, "derive") == [("h1", "i1")]


def test_unattached_fallback_node():
    """无任何资产锚点 → 落「未归属样本」兜底节点（有需要才建）。"""
    assets = [_asset("s1", "binary", "a" * 64)]
    out = _build(assets, findings=[_finding("f1", "项目级", target=None)])
    assert _node(out, UNATTACHED_ID)["type"] == "sample"
    assert _edges(out, "derive") == [(UNATTACHED_ID, "f1")]


def test_no_unattached_node_when_everything_anchored(samples):
    out = _build(samples, intents=[_intent("i1", "假设", target="s1")])
    assert not [n for n in out["nodes"] if n["id"] == UNATTACHED_ID]


def test_sample_label_prefers_filename_then_sha(samples):
    out = _build(samples)
    assert _node(out, "s1")["label"] == "main.exe"
    assert _node(out, "s2")["label"] == "bbbbbbbbbbbb…"


def test_isolated_binary_sample_still_listed(samples):
    """项目级总览：没有意图/发现的样本也成节点（人类能看出「这个样本还没动」）。"""
    out = _build(samples)
    assert {n["id"] for n in out["nodes"] if n["type"] == "sample"} \
        == {"s1", "s2", "s2c"}


# ---------- outcome / 无主发现 ----------

def test_outcome_edge_intent_to_finding(samples):
    out = _build(
        samples,
        findings=[_finding("f1", "绕过", target="s1", status="verified",
                           category="vuln")],
        intents=[_intent("i1", "假设", target="s1", status="closed",
                         outcome="vuln", refs=["f1"])])
    assert _edges(out, "outcome") == [("i1", "f1")]
    assert _node(out, "i1")["finding_ids"] == ["f1"]
    assert _node(out, "i1")["outcome"] == "vuln"


def test_orphan_finding_hangs_off_sample(samples):
    out = _build(samples, findings=[_finding("f1", "无主", target="s1")])
    assert _edges(out, "derive") == [("s1", "f1")]


def test_fp_finding_not_on_graph(samples):
    out = _build(samples, findings=[_finding("f1", "疑似", target="s1",
                                             status="false-positive")])
    assert not [n for n in out["nodes"] if n["type"] == "finding"]
    assert out["counts"]["findings"] == 0


# ---------- derive 续链 ----------

def test_derive_chain_via_closing_intent(samples):
    """意图 basis=finding → 由收尾该 finding 的意图起 derive 边（续链本体）。"""
    findings = [_finding("f1", "发现", target="s1", status="verified",
                         category="vuln")]
    i1 = _intent("i1", "一", target="s1", status="closed", outcome="vuln",
                 refs=["f1"])
    i2 = _intent("i2", "二", target="s1", basis=["finding:f1"])
    out = _build(samples, findings=findings, intents=[i1, i2])
    pairs = _edges(out, "derive")
    assert ("s1", "i1") in pairs and ("i1", "i2") in pairs
    assert ("s1", "i2") not in pairs  # 有依据就不退回样本


def test_derive_basis_finding_without_owner_falls_to_sample(samples):
    findings = [_finding("f1", "无主发现", target="s1")]
    i1 = _intent("i1", "一", target="s1", basis=["finding:f1"])
    out = _build(samples, findings=findings, intents=[i1])
    assert _edges(out, "derive") == [("s1", "f1"), ("s1", "i1")]


def test_derive_self_reference_falls_to_sample(samples):
    """basis 指向自己收尾的 finding（owner=自身）→ 退回样本，不产自环。"""
    findings = [_finding("f1", "发现", target="s1", status="verified",
                         category="vuln")]
    i1 = _intent("i1", "一", target="s1", status="closed", outcome="vuln",
                 refs=["f1"], basis=["finding:f1"])
    out = _build(samples, findings=findings, intents=[i1])
    assert ("i1", "i1") not in _edges(out, "derive")
    assert ("s1", "i1") in _edges(out, "derive")


def test_derive_skips_finding_outside_graph(samples):
    """basis 指向误报发现（不在图上）→ 该依据作废，退回样本。"""
    findings = [_finding("f1", "疑似", target="s1", status="false-positive")]
    i1 = _intent("i1", "一", target="s1", basis=["finding:f1"])
    out = _build(samples, findings=findings, intents=[i1])
    assert _edges(out, "derive") == [("s1", "i1")]


# ---------- 死路隐藏 + bypass ----------

def test_dead_end_hidden_and_bypass_through(samples):
    findings = [_finding("f1", "发现", target="s1", status="verified",
                         category="vuln")]
    i1 = _intent("i1", "一", target="s1", status="closed", outcome="vuln",
                 refs=["f1"])
    d1 = _intent("d1", "死路", target="s1", status="closed", outcome="dead_end",
                 evidence=["artifact:a1"])
    i2 = _intent("i2", "二", target="s1", basis=["artifact:a1"])
    out = _build(samples, findings=findings, intents=[i1, d1, i2])
    assert _node(out, "d1")["default_hidden"] is True
    assert _node(out, "i1")["default_hidden"] is False
    assert _node(out, "i2")["default_hidden"] is False
    # 死路前驱（样本）→ 其后继（i2）：bypass 预复合穿通边
    assert _edges(out, "bypass") == [("s1", "i2")]
    assert out["counts"]["dead_end"] == 1


# ---------- counts / 空项目 / DAG ----------

def test_counts(samples):
    findings = [_finding("f1", "a", target="s1"), _finding("f2", "b", target="s1")]
    intents = [
        _intent("i1", "一", target="s1"),
        _intent("i2", "二", target="s1", status="closed", outcome="dead_end"),
        _intent("i3", "三", target="s1", status="closed", outcome="finding",
                refs=["f1"]),
    ]
    out = _build(samples, findings=findings, intents=intents)
    assert out["counts"] == {"samples": 3, "intents": 3, "open": 1,
                             "closed": 2, "dead_end": 1, "findings": 2}


def test_empty_project():
    out = _build([])
    assert out["nodes"] == [] and out["edges"] == []
    assert out["counts"]["intents"] == 0 and out["counts"]["samples"] == 0


def test_dag_assertion_rejects_cycle():
    nodes = [{"id": "a"}, {"id": "b"}]
    edges = [{"source": "a", "target": "b", "kind": "derive"},
             {"source": "b", "target": "a", "kind": "derive"}]
    with pytest.raises(AssertionError):
        _assert_dag(nodes, edges)


def test_dag_assertion_ignores_bypass():
    """bypass 是 DAG 上的路径复合，不参与无环判定。"""
    nodes = [{"id": "a"}, {"id": "b"}]
    edges = [{"source": "a", "target": "b", "kind": "derive"},
             {"source": "b", "target": "a", "kind": "bypass"}]
    _assert_dag(nodes, edges)
