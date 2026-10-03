"""单站攻击链路图 v3 测试（website-attack-path-graph，2026-09-24）。

主脊 目标 → 意图（规划产物）→ 收尾：漏洞 | 有效发现 | 死路。覆盖：
路径模板化 / 30min 折叠 / 超窗分裂 / 跨任务跨会话合并 / 五档结果 /
finding 挂接 / 明细不丢 / 执行层相邻边 / 站边界过滤 / curl 缺口合成节点；
v3：意图存活窗读时归属（重叠窗最新声明胜出）/ outcome·derive 边 /
死路默认隐藏 + bypass 穿通边 / 服务端 Kahn DAG 断言（bypass 除外）/ counts。
"""

import pytest

from core.blackboard import Blackboard
from core.blackboard.attackpath import (
    FOLD_GAP_S, _assert_dag, attempt_key, build_attack_path, templated_url,
)

T0 = "2026-09-24T10:00:00+00:00"


def _ts(offset_min: float) -> str:
    from datetime import datetime, timedelta, timezone
    dt = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_min)
    return dt.isoformat()


def _row(hid: int, url: str, *, method="GET", status=200, resp_body="ok",
         created=None, session="sess-1", source="browser", task=None) -> dict:
    return {
        "id": hid, "method": method, "url": url, "status": status,
        "resp_body": resp_body, "resp_mime": "text/html",
        "created_at": created or T0, "session_id": session,
        "source": source, "task_id": task,
    }


def _intent(iid: str, statement: str, *, created: str, closed: str | None = None,
            status="open", outcome="", refs=None, evidence=None,
            basis=None, target="d1") -> dict:
    return {
        "id": iid, "statement": statement, "status": status,
        "outcome_type": outcome, "outcome_refs": refs or [],
        "dead_reason": "", "evidence_refs": evidence or [],
        "basis_refs": basis or [], "target_asset_id": target,
        "created_at": created, "closed_at": closed,
    }


@pytest.fixture()
def tree():
    """host 根 + domain + 两 url；另一个独立 host 做边界对照。"""
    return [
        {"id": "h1", "type": "host", "value": "10.0.0.1", "parent_id": None},
        {"id": "d1", "type": "domain", "value": "site.com", "parent_id": "h1"},
        {"id": "u1", "type": "url", "value": "http://site.com/", "parent_id": "d1"},
        {"id": "h2", "type": "host", "value": "10.0.0.2", "parent_id": None},
    ]


def _build(tree, rows, target="h1", findings=None, intents=None):
    return build_attack_path(None, "p1", target, assets=tree,
                             findings=findings or [], history=rows,
                             intents=intents or [])


def _edge_pairs(out, kind):
    return [(e["source"], e["target"]) for e in out["edges"] if e["kind"] == kind]


# ---------- 模板化 ----------

def test_templated_url_segments_and_query():
    assert templated_url("http://x/a/123")[0] == "/a/{id}"
    assert templated_url("http://x/u/550e8400-e29b-41d4-a716-446655440000")[0] \
        == "/u/{uuid}"
    assert templated_url("http://x/h/deadbeefcafe0011")[0] == "/h/{hash}"
    # query 键排序去重；折叠值
    tpl, keys = templated_url("http://x/p?b=2&a=1&b=3")
    assert tpl == "/p" and keys == ("a", "b")
    assert attempt_key("get", "http://x/1")[0] == "GET"


# ---------- 折叠 / 分裂 / 执行层边 ----------

def test_fold_retries_within_window(tree):
    rows = [_row(1, "http://site.com/user/1", created=_ts(0)),
            _row(2, "http://site.com/user/2", created=_ts(5))]
    out = _build(tree, rows)
    assert len(out["attempts"]) == 1
    a = out["attempts"][0]
    assert a["path_template"] == "/user/{id}" and a["request_count"] == 2
    # 重试折叠：逻辑图无边，执行层也只有一个节点
    assert out["edges"] == [] and out["exec_edges"] == []


def test_window_split_exec_edge(tree):
    rows = [_row(1, "http://site.com/user/1", created=_ts(0)),
            _row(2, "http://site.com/user/2", created=_ts(FOLD_GAP_S / 60 + 1))]
    out = _build(tree, rows)
    assert [a["request_count"] for a in out["attempts"]] == [1, 1]
    # 超窗分裂：执行层相邻边（时间序细节），主脊逻辑图无边
    assert out["edges"] == []
    assert out["exec_edges"] == [{
        "source": out["attempts"][0]["id"],
        "target": out["attempts"][1]["id"], "kind": "exec"}]


def test_interleaved_other_point_no_back_edge(tree):
    """A B A（A 重试在窗内折叠）：节点序 A→B，无回边。"""
    rows = [_row(1, "http://site.com/a", created=_ts(0)),
            _row(2, "http://site.com/b", created=_ts(1)),
            _row(3, "http://site.com/a", created=_ts(2))]
    out = _build(tree, rows)
    assert [a["path_template"] for a in out["attempts"]] == ["/a", "/b"]
    assert out["attempts"][0]["request_count"] == 2
    assert out["exec_edges"] == [
        {"source": "attempt-01", "target": "attempt-02", "kind": "exec"}]


def test_cross_task_cross_session_merged(tree):
    rows = [_row(1, "http://site.com/login", created=_ts(0),
                 session="sess-1", task="t1"),
            _row(2, "http://site.com/login", created=_ts(3),
                 session="sess-2", task="t2")]
    out = _build(tree, rows)
    a = out["attempts"][0]
    assert a["request_count"] == 2
    assert set(a["session_ids"]) == {"sess-1", "sess-2"}


# ---------- 五档结果 ----------

def test_blocked_signals(tree):
    rows = [_row(1, "http://site.com/admin", status=403, created=_ts(0))]
    assert _build(tree, rows)["attempts"][0]["result"] == "blocked"
    rows = [_row(1, "http://site.com/x", status=None, created=_ts(0))]
    assert _build(tree, rows)["attempts"][0]["result"] == "blocked"


def test_no_reaction_404_and_blank_200(tree):
    assert _build(tree, [_row(1, "http://site.com/nope", status=404)])[
        "attempts"][0]["result"] == "no_reaction"
    assert _build(tree, [_row(1, "http://site.com/blank", resp_body="")])[
        "attempts"][0]["result"] == "no_reaction"


def test_hint_body_and_reflection_and_error(tree):
    assert _build(tree, [_row(1, "http://site.com/p", resp_body="hello")])[
        "attempts"][0]["result"] == "hint"
    r = _build(tree, [_row(1, "http://site.com/s?q=zzx", resp_body="zzx reflected")])
    assert r["attempts"][0]["result"] == "hint"
    r = _build(tree, [_row(1, "http://site.com/s",
                           resp_body="You have an error in your SQL syntax")])
    assert r["attempts"][0]["result"] == "hint"


def test_group_takes_strongest_signal(tree):
    """折叠组内先 404 后 200 → 整组 hint，代表请求取 200 那条。"""
    rows = [_row(1, "http://site.com/user/1", status=404,
                 resp_body="", created=_ts(0)),
            _row(2, "http://site.com/user/2", status=200,
                 resp_body="data", created=_ts(1))]
    a = _build(tree, rows)["attempts"][0]
    assert a["result"] == "hint"
    assert a["representative"]["status"] == 200
    assert set(a["status_codes"]) == {200, 404}


# ---------- finding 挂接 ----------

def test_found_finding_bidirectional(tree):
    findings = [{"id": "f1", "title": "SQL 注入", "severity": "high",
                 "status": "verified", "category": "vuln",
                 "target_asset_id": "u1"}]
    rows = [_row(1, "http://site.com/", status=200, resp_body="home")]
    out = _build(tree, rows, findings=findings)
    a = out["attempts"][0]
    assert a["result"] == "found" and a["finding_ids"] == ["f1"]
    # 反查链路：f1 finding 节点上图，代表明细关联 history id（前端双向跳转数据源）
    assert a["representative"]["history_id"] == 1
    fnodes = {n["id"]: n for n in out["nodes"] if n["type"] == "finding"}
    assert fnodes["f1"]["category"] == "vuln"


def test_fp_finding_does_not_flip_found(tree):
    findings = [{"id": "f1", "title": "疑似", "severity": "low",
                 "status": "false-positive", "category": "vuln",
                 "target_asset_id": "u1"}]
    rows = [_row(1, "http://site.com/", status=404, resp_body="")]
    out = _build(tree, rows, findings=findings)
    a = out["attempts"][0]
    assert a["result"] == "no_reaction" and a["finding_ids"] == ["f1"]
    # FP 发现不上逻辑图
    assert not [n for n in out["nodes"] if n["type"] == "finding"]


def test_finding_on_host_with_evidence_request(tree):
    findings = [{"id": "f1", "title": "XSS", "severity": "high",
                 "status": "verified", "category": "vuln",
                 "target_asset_id": "d1",
                 "evidence": {"requests": [
                     {"method": "GET",
                      "url": "http://site.com/search?q=<script>"}]}}]
    rows = [_row(1, "http://site.com/search?q=<script>", resp_body="x")]
    a = _build(tree, rows, findings=findings)["attempts"][0]
    assert a["result"] == "found" and a["finding_ids"] == ["f1"]


def test_site_level_finding_derive_from_target(tree):
    """挂 host/domain、无路径证据 → finding 节点由 target 起 derive 边（无主早期发现）。"""
    findings = [{"id": "f1", "title": "站点框架老旧", "severity": "medium",
                 "status": "verified", "category": "intel",
                 "target_asset_id": "h1"}]
    out = _build(tree, [], findings=findings)
    assert _edge_pairs(out, "derive") == [("h1", "f1")]
    fnodes = {n["id"] for n in out["nodes"] if n["type"] == "finding"}
    assert "f1" in fnodes


def test_curl_gap_synthetic_node(tree):
    """curl 侦察不入 http_history 的 finding → request_count=0 合成 found 节点。"""
    findings = [{"id": "f1", "title": ".git 泄露", "severity": "high",
                 "status": "verified", "category": "vuln",
                 "target_asset_id": "u1"}]
    out = _build(tree, [], findings=findings)
    a = out["attempts"][0]
    assert a["result"] == "found" and a["request_count"] == 0
    assert a["representative"]["title"] == ".git 泄露"


# ---------- v3：意图存活窗读时归属 ----------

def test_attempt_attributed_to_open_window(tree):
    # open 意图存活窗 [created, ∞)：窗内归属，声明之前无主
    it = _intent("i1", "登录页可能存在注入", created=_ts(2))
    rows = [_row(1, "http://site.com/before", created=_ts(1)),
            _row(2, "http://site.com/after", created=_ts(5))]
    out = _build(tree, rows, intents=[it])
    by_path = {a["path_template"]: a["intent_id"] for a in out["attempts"]}
    assert by_path["/before"] is None and by_path["/after"] == "i1"


def test_closed_window_bounds(tree):
    it = _intent("i1", "假设", created=_ts(0), closed=_ts(10), status="closed")
    rows = [_row(1, "http://site.com/in", created=_ts(5)),
            _row(2, "http://site.com/out", created=_ts(15))]
    out = _build(tree, rows, intents=[it])
    by_path = {a["path_template"]: a["intent_id"] for a in out["attempts"]}
    assert by_path["/in"] == "i1" and by_path["/out"] is None


def test_overlapping_windows_latest_declared_wins(tree):
    # 两窗重叠覆盖 t=8：最新声明的 i2 胜出（执行者焦点）
    i1 = _intent("i1", "假设一", created=_ts(0), closed=_ts(20), status="closed")
    i2 = _intent("i2", "假设二", created=_ts(5), closed=_ts(25), status="closed")
    rows = [_row(1, "http://site.com/x", created=_ts(8))]
    out = _build(tree, rows, intents=[i1, i2])
    assert out["attempts"][0]["intent_id"] == "i2"


def test_synthetic_attempt_owner_via_outcome(tree):
    """无时间的合成节点：按收尾发现归属到 close 它的意图。"""
    findings = [{"id": "f1", "title": ".git 泄露", "severity": "high",
                 "status": "verified", "category": "vuln",
                 "target_asset_id": "u1"}]
    it = _intent("i1", "测 .git", created=_ts(0), closed=_ts(5),
                 status="closed", outcome="vuln", refs=["f1"])
    out = _build(tree, [], findings=findings, intents=[it])
    a = out["attempts"][0]
    assert a["request_count"] == 0 and a["intent_id"] == "i1"
    # outcome 边：意图 → 收尾发现
    assert _edge_pairs(out, "outcome") == [("i1", "f1")]
    inode = next(n for n in out["nodes"] if n["type"] == "intent")
    assert inode["status"] == "closed" and inode["finding_ids"] == ["f1"]


def test_intent_request_count_aggregated(tree):
    it = _intent("i1", "假设", created=_ts(0))
    rows = [_row(1, "http://site.com/a", created=_ts(1)),
            _row(2, "http://site.com/a", created=_ts(2)),
            _row(3, "http://site.com/b", created=_ts(3))]
    out = _build(tree, rows, intents=[it])
    inode = next(n for n in out["nodes"] if n["type"] == "intent")
    assert inode["request_count"] == 3


def test_derive_edge_from_basis_finding(tree):
    """意图 basis_refs=finding：finding 无收尾主 → 意图归属子目标/根作前驱。"""
    findings = [{"id": "f1", "title": "框架版本", "severity": "medium",
                 "status": "verified", "category": "intel",
                 "target_asset_id": "h1"}]
    it = _intent("i1", "老框架可能有已知 CVE", created=_ts(1),
                 basis=["finding:f1"])
    out = _build(tree, [], findings=findings, intents=[it])
    pairs = _edge_pairs(out, "derive")
    # f1 无主：target→f1；i1 锚定 d1（子目标）→ d1 起边
    assert ("h1", "f1") in pairs and ("d1", "i1") in pairs


# ---------- v3.1：子目标节点（2026-10-01） ----------

def test_subtarget_nodes_from_direct_children(tree):
    """根的直接子资产渲染为 subtarget 节点；孙节点不上图。"""
    out = _build(tree, [], intents=[])
    types = {n["id"]: n["type"] for n in out["nodes"]}
    assert types.get("h1") == "target"
    assert types.get("d1") == "subtarget"      # h1 的直接子
    assert "u1" not in types                    # 孙节点（d1 的子）不上子目标层
    assert out["counts"]["subtargets"] == 1


def test_subtarget_settled_badge(tree):
    """子目标 settled：名下意图全部收尾且至少一条 dead_end。"""
    d = _intent("d", "无洞", created=_ts(0), closed=_ts(10),
                status="closed", outcome="dead_end", evidence=["http:1"])
    open_it = _intent("o", "待验证", created=_ts(11))
    out = _build(tree, [_row(1, "http://site.com/x", created=_ts(5))],
                 intents=[d])
    st = next(n for n in out["nodes"] if n["id"] == "d1")
    assert st["settled"] is True
    # 加一条 open 意图 → 不再 settled
    out2 = _build(tree, [_row(1, "http://site.com/x", created=_ts(5))],
                  intents=[d, open_it])
    st2 = next(n for n in out2["nodes"] if n["id"] == "d1")
    assert st2["settled"] is False


def test_intent_anchored_to_its_subtarget(tree):
    """意图按资产锚点归属到所在子目标子树：target=d1 → d1 起 derive 边。"""
    it = _intent("i1", "假设", created=_ts(1), target="d1")
    out = _build(tree, [], intents=[it])
    assert ("d1", "i1") in _edge_pairs(out, "derive")
    assert ("h1", "i1") not in _edge_pairs(out, "derive")


def test_intent_without_anchor_falls_back_to_root(tree):
    """无锚点/锚点在根 → 挂根（历史数据兼容）。"""
    it = _intent("i1", "假设", created=_ts(1), target="h1")
    out = _build(tree, [], intents=[it])
    assert ("h1", "i1") in _edge_pairs(out, "derive")


# ---------- v3：死路默认隐藏 + bypass ----------

def test_dead_end_hidden_and_bypass(tree):
    # d：死路（子目标 d1→d derive）；s：依据 d 的证据（d→s derive）→ bypass d1→s
    d = _intent("d", "走备份文件找入口", created=_ts(0), closed=_ts(10),
                status="closed", outcome="dead_end",
                evidence=["http:1"])
    s = _intent("s", "换路测上传", created=_ts(12),
                basis=["http:1"])
    out = _build(tree,
                 [_row(1, "http://site.com/backup", created=_ts(5))],
                 intents=[d, s])
    dnode = next(n for n in out["nodes"] if n["id"] == "d")
    assert dnode["default_hidden"] is True
    assert _edge_pairs(out, "bypass") == [("d1", "s")]
    assert out["counts"]["dead_end"] == 1
    # 隐藏是渲染侧口径：节点与 derive 边仍在（显示开关由前端控制）
    assert ("d1", "d") in _edge_pairs(out, "derive")
    assert ("d", "s") in _edge_pairs(out, "derive")


def test_bypass_not_duplicate_direct_edge(tree):
    # 已有直连边时 bypass 不重复造
    d = _intent("d", "死路", created=_ts(0), closed=_ts(10),
                status="closed", outcome="dead_end", evidence=["http:1"])
    s = _intent("s", "后继", created=_ts(12), basis=["http:1", "asset:d1"])
    out = _build(tree,
                 [_row(1, "http://site.com/x", created=_ts(5))],
                 intents=[d, s])
    # s 的前驱既有 d（http 证据主）又有子目标 d1（asset 锚点）→ d1→s 直连已存在
    pairs = _edge_pairs(out, "derive")
    assert ("d1", "s") in pairs
    assert _edge_pairs(out, "bypass") == []


# ---------- v3：服务端 DAG 断言 ----------

def test_assert_dag_endpoint_not_in_nodes():
    nodes = [{"id": "a"}, {"id": "b"}]
    with pytest.raises(AssertionError):
        _assert_dag(nodes, [{"source": "a", "target": "x", "kind": "derive"}])


def test_assert_dag_cycle_rejected():
    nodes = [{"id": "a"}, {"id": "b"}]
    with pytest.raises(AssertionError):
        _assert_dag(nodes, [{"source": "a", "target": "b", "kind": "derive"},
                            {"source": "b", "target": "a", "kind": "outcome"}])


def test_assert_dag_bypass_excluded():
    """bypass 是 DAG 路径复合，不参与无环判定。"""
    nodes = [{"id": "a"}, {"id": "b"}]
    _assert_dag(nodes, [{"source": "b", "target": "a", "kind": "bypass"}])


# ---------- counts ----------

def test_counts(tree):
    findings = [
        {"id": "f1", "title": "漏洞", "severity": "high", "status": "verified",
         "category": "vuln", "target_asset_id": "u1"},
        {"id": "f2", "title": "信息", "severity": "low", "status": "unverified",
         "category": "intel", "target_asset_id": "h1"},
    ]
    intents = [
        _intent("i1", "出洞假设", created=_ts(0), closed=_ts(5),
                status="closed", outcome="vuln", refs=["f1"]),
        _intent("i2", "待收尾假设", created=_ts(6)),
    ]
    rows = [_row(1, "http://site.com/", created=_ts(2))]
    out = _build(tree, rows, findings=findings, intents=intents)
    assert out["counts"] == {
        "subtargets": 1,
        "intents": 2, "open": 1, "closed": 1, "dead_end": 0,
        "findings": 2, "total_requests": 1}


# ---------- 边界 / 错误 ----------

def test_host_boundary_excludes_other_site(tree):
    rows = [_row(1, "http://site.com/a", created=_ts(0)),
            _row(2, "http://10.0.0.2/b", created=_ts(1)),
            _row(3, "http://other.else/c", created=_ts(2))]
    out = _build(tree, rows)
    assert out["counts"]["total_requests"] == 1
    assert out["attempts"][0]["representative"]["url"] == "http://site.com/a"


def test_cross_site_intent_excluded(tree):
    # 意图针对站外资产、依据也不在子树 → 不进单站图
    it = _intent("i1", "站外假设", created=_ts(0), target="h2")
    out = _build(tree, [_row(1, "http://site.com/a", created=_ts(1))], intents=[it])
    assert out["counts"]["intents"] == 0


def test_target_not_found(tree):
    with pytest.raises(LookupError):
        _build(tree, [], target="nope")


def test_topology_acyclic_many_nodes(tree):
    rows = [_row(i, f"http://site.com/p{i % 3}/{i}", created=_ts(i * 20))
            for i in range(8)]
    out = _build(tree, rows)
    ids = [a["id"] for a in out["attempts"]]
    for e in out["exec_edges"]:
        assert ids.index(e["source"]) < ids.index(e["target"])


# ---------- 真实黑板集成（分页 + 鸭子类型） ----------

def test_real_blackboard_pagination(tmp_path):
    bb = Blackboard(str(tmp_path / "ap.db"))
    proj = bb.create_project("链路", "pentest")
    pid = proj["id"]
    host = bb.upsert_asset(pid, "host", "10.0.0.1")["id"]
    bb.upsert_asset(pid, "domain", "site.com", parent_id=host)
    n = 1005  # 超过单页 1000
    for i in range(n):
        bb.add_http_history(pid, source="browser", method="GET",
                            url=f"http://site.com/x{i}",
                            status=200, resp_body="y")
    out = build_attack_path(bb, pid, host)
    assert out["counts"]["total_requests"] == n and len(out["attempts"]) == n
    assert len(out["exec_edges"]) == n - 1
    assert out["edges"] == []  # 无意图：主脊只有 target
    bb.close()
