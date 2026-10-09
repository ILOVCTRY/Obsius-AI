"""覆盖度对账单元测试（M3 B1，orchestrator-efficiency）。

口径拍板记录在 docs/plans/orchestrator-efficiency.md §0-6：
终态 = tested_clean/na ∪ dead_end ∪ finding 挂链（FP-only=死路味）；
budget_stop 算 open（预算停 ≠ 测完）；父收敛由子树全收敛读时派生（D3，
不要求自身 visited）；group_key 沿父链到根 domain 为主 host 兜底、孤儿自成组。
"""

import pytest

from core.blackboard import Blackboard
from core.coverage import (asset_terminal_state, attach_effective_status,
                           coverage_report, effective_status_map,
                           situation_snapshot)


@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "cov.db"))
    project = bb.create_project("覆盖测试", "pentest")
    yield bb, project
    bb.close()


def _mark_clean(bb, pid, aid, note="测完"):
    """标 tested_clean 的测试快捷路（2026-09-25 门禁）：对资产立死路意图
    （http 证据）再经状态机标 clean。"""
    from core.blackboard.intents import close_intent, declare_intent
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url=f"http://probe.invalid/{aid}", status=404,
                              resp_body="")
    iid = declare_intent(bb, pid, f"无洞假设 {aid}",
                         target_asset_id=aid)["id"]
    close_intent(bb, pid, iid, "dead_end", dead_reason="探测无异常",
                 evidence_refs=[f"http:{hid}"])
    return bb.set_asset_status(aid, "tested_clean", note=note)


def test_terminal_state_flavors():
    """对账态判定，判定顺序：状态机终态 > 死路 > 发现挂链 > 半程 > open。
    scanning 并入 visited（三态归并，2026-10-09）。"""
    assert asset_terminal_state({"status": "tested_clean"}) == "terminal-tested_clean"
    assert asset_terminal_state({"status": "na"}) == "terminal-na"
    # budget_stop 算 open（预算停 ≠ 测完，宁严勿松）
    assert asset_terminal_state({"status": "budget_stop"}) == "open"
    assert asset_terminal_state({"status": "visited"}) == "visited"
    # scanning 并入 visited（无任务认领，无孤儿半程，读时不区分在跑/半程）
    assert asset_terminal_state({"status": "scanning"}) == "visited"
    assert asset_terminal_state({}) == "open"
    # meta.dead_end 标记
    assert asset_terminal_state({"status": "visited", "meta": {"dead_end": True}}) \
        == "terminal-dead_end"
    # FP-only 发现 = 死路味（这条路被否了）
    fp = [{"status": "false-positive"}]
    assert asset_terminal_state({"status": "open"}, fp) == "terminal-dead_end"
    # 有非 FP 发现挂链即收口
    fnd = [{"status": "unverified"}, {"status": "verified"}]
    assert asset_terminal_state({"status": "open"}, fnd) == "terminal-finding"
    # 判定顺序：状态机终态压过发现；dead_end 标记压过发现
    assert asset_terminal_state({"status": "tested_clean"}, fnd) == "terminal-tested_clean"
    assert asset_terminal_state({"status": "open", "meta": {"dead_end": True}}, fnd) \
        == "terminal-dead_end"
    # 纯 FP + dead_end 二选一都是死路味；非 FP 压过 FP-only 的死路判定
    assert asset_terminal_state({"status": "open"}, fp + [{"status": "verified"}]) \
        == "terminal-finding"


def test_group_key_domain_tree_and_orphans(env):
    """归组：url→host→domain 沿父链到根 domain 为主；独立 host 自成组；
    无 parent 的孤儿 url/service 自成组；binary 不入对账面。"""
    bb, proj = env
    pid = proj["id"]
    dom = bb.upsert_asset(pid, "domain", "Example.com")["id"]
    host = bb.upsert_asset(pid, "host", "www.example.com", parent_id=dom)["id"]
    url = bb.upsert_asset(pid, "url", "http://www.example.com/admin",
                          parent_id=host)["id"]
    bare = bb.upsert_asset(pid, "host", "10.0.0.9")["id"]
    orphan = bb.upsert_asset(pid, "url", "http://10.0.0.9/x")["id"]  # 无 parent
    bb.upsert_asset(pid, "binary", "abc123")  # 非对账面

    rep = coverage_report(bb, pid)
    keys = {g["group"]: g for g in rep["by_group"]}
    # 根 domain 吸收子树（value 小写归一）；独立 host / 孤儿 url 各自成组
    assert set(keys) == {"domain:example.com", "host:10.0.0.9", "url:http://10.0.0.9/x"}
    assert keys["domain:example.com"]["total"] == 3
    assert rep["overall"]["assets"] == 5  # binary 不计
    # 全 open：converged 0，uncovered 含全组员（open 优先序）
    g = keys["domain:example.com"]
    assert g["converged"] == 0 and g["done"] is False
    assert {u["id"] for u in g["uncovered"]} == {dom, host, url}
    assert {u["id"] for u in keys["host:10.0.0.9"]["uncovered"]} == {bare}


def test_propagation_derived_from_children(env):
    """D3 收敛传播（2026-09-24）：父节点收敛只看子树——url 全终态即逐层
    带出 host/domain 收敛，不要求自身 visited（父显式 tested_clean 由
    store 写入门拒绝，见 test_asset_status_parent_clean_rejected）。"""
    bb, proj = env
    pid = proj["id"]
    dom = bb.upsert_asset(pid, "domain", "site.com")["id"]
    host = bb.upsert_asset(pid, "host", "www.site.com", parent_id=dom)["id"]
    u1 = bb.upsert_asset(pid, "url", "http://www.site.com/a", parent_id=host)["id"]
    u2 = bb.upsert_asset(pid, "url", "http://www.site.com/b", parent_id=host)["id"]

    def _group():
        rep = coverage_report(bb, pid)
        return next(g for g in rep["by_group"] if g["group"] == "domain:site.com")

    # url 一终一 open：host、domain 均未收敛（子未全终）
    _mark_clean(bb, pid, u1, "测完无发现")
    g = _group()
    assert g["converged"] == 1 and g["done"] is False
    assert {u["id"] for u in g["uncovered"]} == {dom, host, u2}
    # url 全终态 → host、domain 逐层派生收敛（本体 open 不挡，组直接收口）
    _mark_clean(bb, pid, u2, "测完无发现")
    g = _group()
    assert g["terminal"] == 2
    assert g["converged"] == 4 and g["done"] is True
    assert g["uncovered"] == []
    assert rep_overall_done(coverage_report(bb, pid)) is True


def rep_overall_done(rep: dict) -> bool:
    return rep["overall"]["groups_done"] == rep["overall"]["groups"]


def test_finding_attachment_not_settled_and_fp_only(env):
    """**有发现 ≠ 测干净**（2026-10-09 三态）：非 FP 发现挂链 → 不收敛、留 uncovered；
    FP-only = 死路味，仍算收口（这条路被否了）。"""
    bb, proj = env
    pid = proj["id"]
    h1 = bb.upsert_asset(pid, "host", "10.1.1.1")["id"]
    h2 = bb.upsert_asset(pid, "host", "10.1.1.2")["id"]
    bb.add_finding(pid, vuln_class="rce", title="RCE", target_asset_id=h1,
                   severity="high", status="verified")
    bb.add_finding(pid, vuln_class="info", title="误报", target_asset_id=h2,
                   status="false-positive")

    rep = coverage_report(bb, pid)
    keys = {g["group"]: g for g in rep["by_group"]}
    # 出洞资产不算已测干净 → 留 uncovered（state=visited），未收敛
    g1 = keys["host:10.1.1.1"]
    assert g1["terminal"] == 0 and g1["done"] is False
    assert [u["state"] for u in g1["uncovered"]] == ["visited"]
    # FP-only = 死路味 = 收口
    assert keys["host:10.1.1.2"]["terminal"] == 1 and keys["host:10.1.1.2"]["done"] is True
    assert rep["overall"]["converged"] == 1 and rep["overall"]["assets"] == 2


# ---------- effective_status 读时派生（asset-tree-derived-clean M2，⑥-⑪） ----------

def _tree(bb, pid):
    """host 10.0.0.1 → u1/u2 两个 url 子节点；返回 (host, u1, u2)。"""
    host = bb.upsert_asset(pid, "host", "10.0.0.1")["id"]
    u1 = bb.upsert_asset(pid, "url", "http://10.0.0.1/a", parent_id=host)["id"]
    u2 = bb.upsert_asset(pid, "url", "http://10.0.0.1/b", parent_id=host)["id"]
    return host, u1, u2


def test_effective_leaf_explicit_not_derived(env):
    """⑩ 零子资产（叶子）：显式状态原样、basis=explicit，不自动派生（D5）。"""
    bb, proj = env
    pid = proj["id"]
    leaf = bb.upsert_asset(pid, "host", "10.7.7.7")["id"]
    m = effective_status_map(bb.list_assets(pid))
    assert m[leaf] == {"status": "open", "basis": "explicit",
                       "settled": False, "has_findings": False}
    _mark_clean(bb, pid, leaf, "单点全测")
    m = effective_status_map(bb.list_assets(pid))
    assert m[leaf]["status"] == "tested_clean" and m[leaf]["basis"] == "explicit"
    assert m[leaf]["settled"] is True


def test_effective_derived_clean_when_all_terminal(env):
    """⑥ 子节点全部终态 → 父 tested_clean / basis=derived / settled。"""
    bb, proj = env
    pid = proj["id"]
    host, u1, u2 = _tree(bb, pid)
    _mark_clean(bb, pid, u1)
    _mark_clean(bb, pid, u2)
    m = effective_status_map(bb.list_assets(pid))
    assert m[host]["status"] == "tested_clean" and m[host]["basis"] == "derived"
    assert m[host]["settled"] is True
    assert m[u1]["basis"] == "explicit"  # 叶子仍是显式口径


def test_effective_any_child_unsettled_is_visited(env):
    """⑦ 子节点有收口也有未收口（混合）→ 父 visited（已访问未测尽，三态）。"""
    bb, proj = env
    pid = proj["id"]
    host, u1, u2 = _tree(bb, pid)
    _mark_clean(bb, pid, u1)
    bb.set_asset_status(u2, "visited", note="摸过半程")
    m = effective_status_map(bb.list_assets(pid))
    assert m[host]["status"] == "visited" and m[host]["settled"] is False


def test_effective_all_children_untested_is_open(env):
    """⑦b 全子未测 → 父 open（未测试，从没碰过）。"""
    bb, proj = env
    pid = proj["id"]
    host, u1, u2 = _tree(bb, pid)
    m = effective_status_map(bb.list_assets(pid))
    assert m[host]["status"] == "open" and m[host]["settled"] is False


def test_effective_new_child_breaks_clean(env):
    """⑧ 新增子节点立即破坏 derived clean（读时派生，无需事件联动）。"""
    bb, proj = env
    pid = proj["id"]
    host, u1, u2 = _tree(bb, pid)
    _mark_clean(bb, pid, u1)
    _mark_clean(bb, pid, u2)
    assert effective_status_map(bb.list_assets(pid))[host]["status"] == "tested_clean"
    # 新登记一个 open url 子节点 → 根立刻掉出 clean（已访问）
    u3 = bb.upsert_asset(pid, "url", "http://10.0.0.1/c", parent_id=host)["id"]
    m = effective_status_map(bb.list_assets(pid))
    assert m[host]["status"] == "visited" and m[u3]["settled"] is False


def test_effective_na_dead_end_children_settle(env):
    """⑨ na / dead_end 子节点同样收口，不阻塞父派生 clean。"""
    bb, proj = env
    pid = proj["id"]
    host = bb.upsert_asset(pid, "host", "10.6.6.6")["id"]
    c1 = bb.upsert_asset(pid, "url", "http://10.6.6.6/a", parent_id=host)["id"]
    c2 = bb.upsert_asset(pid, "url", "http://10.6.6.6/b", parent_id=host,
                        meta={"dead_end": True})["id"]
    bb.set_asset_status(c1, "na", note="该面不适用")
    m = effective_status_map(bb.list_assets(pid))
    assert m[c1]["settled"] and m[c2]["settled"]
    assert m[host]["status"] == "tested_clean" and m[host]["settled"] is True


def test_effective_service_port_face_and_findings(env):
    """⑪ 端口面：service 子节点未收口时 host 不得 clean；findings 沿树向上传播。
    **有 finding 的子树不派生 clean**（有洞 ≠ 测干净，2026-10-09）。"""
    bb, proj = env
    pid = proj["id"]
    host = bb.upsert_asset(pid, "host", "10.5.5.5")["id"]
    svc = bb.upsert_asset(pid, "service", "10.5.5.5:8080", parent_id=host)["id"]
    m = effective_status_map(bb.list_assets(pid))
    assert m[host]["status"] == "open"
    bb.set_asset_status(svc, "na", note="端口关闭")
    assert effective_status_map(bb.list_assets(pid))[host]["status"] == "tested_clean"
    # finding 挂叶子 → has_findings 向父传播；叶子降级 visited、父不派生 clean
    url = bb.upsert_asset(pid, "url", "http://10.5.5.5/x", parent_id=host)["id"]
    bb.add_finding(pid, vuln_class="sqli", title="注入", target_asset_id=url,
                   severity="high", status="verified")
    m = effective_status_map(bb.list_assets(pid), bb.list_findings(pid))
    assert m[url]["has_findings"] and m[host]["has_findings"]
    assert m[url]["status"] == "visited" and m[url]["settled"] is False
    assert m[host]["status"] == "visited" and m[host]["settled"] is False


def test_effective_finding_overrides_explicit_clean(env):
    """**发现压过显式 clean（读时降级）**：叶子显式 tested_clean 后补挂发现 →
    effective 降级 visited、settled=False——历史行不动，读时兜（asset-tri-state）。"""
    bb, proj = env
    pid = proj["id"]
    leaf = bb.upsert_asset(pid, "host", "10.8.8.8")["id"]
    _mark_clean(bb, pid, leaf, "单点全测")
    assert effective_status_map(bb.list_assets(pid))[leaf]["status"] == "tested_clean"
    # 落库显式 tested_clean 不动，后补一个非 FP 发现
    bb.add_finding(pid, vuln_class="rce", title="后补洞", target_asset_id=leaf,
                   severity="high", status="verified")
    m = effective_status_map(bb.list_assets(pid), bb.list_findings(pid))
    assert m[leaf]["status"] == "visited" and m[leaf]["settled"] is False
    assert m[leaf]["has_findings"] is True
    # 库里的显式 status 仍是 tested_clean（读时降级，零迁移）
    assert bb.get_asset(leaf)["status"] == "tested_clean"


def test_attach_effective_status_fields(env):
    """attach_effective_status：派生字段挂回浅拷贝；非 coverage 类型原样透传。"""
    bb, proj = env
    pid = proj["id"]
    host, u1, u2 = _tree(bb, pid)
    _mark_clean(bb, pid, u1)
    out = {a["id"]: a for a in attach_effective_status(bb.list_assets(pid))}
    assert out[host]["effective_status"] == "visited"   # 混合子态 → 已访问（未测尽）
    assert out[host]["status_basis"] == "derived" and out[host]["settled"] is False
    assert out[u1]["effective_status"] == "tested_clean"
    # 原对象未被修改
    assert "effective_status" not in bb.get_asset(host)
    # binary 非对账面：不挂字段
    b = bb.upsert_asset(pid, "binary", "z" * 64)
    out_b = next(a for a in attach_effective_status(bb.list_assets(pid))
                 if a["id"] == b["id"])
    assert "effective_status" not in out_b


def test_uncovered_open_first_ordering_and_cap(env):
    """uncovered 排序：组内 open（未摸）优先于 visited（半程）；cap 生效。"""
    bb, proj = env
    pid = proj["id"]
    dom = bb.upsert_asset(pid, "domain", "cap.com")["id"]
    kids = [bb.upsert_asset(pid, "host", f"10.9.0.{i}", parent_id=dom)["id"]
            for i in range(5)]
    bb.set_asset_status(kids[0], "visited", note="半程")
    bb.set_asset_status(kids[1], "budget_stop", note="预算停")
    rep = coverage_report(bb, pid, uncovered_cap=3)
    g = next(g for g in rep["by_group"] if g["group"] == "domain:cap.com")
    assert g["total"] == 6 and len(g["uncovered"]) == 3  # cap 截断
    # open（含 budget_stop）在前，visited 垫后
    assert g["uncovered"][0]["state"] == "open"
    assert all(u["state"] == "open" for u in g["uncovered"])


def test_situation_snapshot_buckets_caps_and_binary_excluded(env):
    """态势快照（asset-tri-state D8）：三态分桶 + 带洞标 has_findings +
    clean 只给 id + cap 截断 + binary 等非对账面不入。"""
    bb, proj = env
    pid = proj["id"]
    h_open = bb.upsert_asset(pid, "host", "10.1.0.1")["id"]           # 未测试
    h_vuln = bb.upsert_asset(pid, "host", "10.1.0.2")["id"]           # 有洞→visited
    u = bb.upsert_asset(pid, "url", "http://10.1.0.2/a",
                        parent_id=h_vuln)["id"]
    bb.add_finding(pid, vuln_class="rce", title="RCE", target_asset_id=u,
                   severity="high", status="verified")
    h_clean = bb.upsert_asset(pid, "host", "10.1.0.3")["id"]
    _mark_clean(bb, pid, h_clean, "全测无洞")
    bb.upsert_asset(pid, "binary", "a" * 64)                          # 不入组

    snap = situation_snapshot(bb.list_assets(pid), bb.list_findings(pid))
    assert snap["counts"] == {"open": 1, "visited": 2, "tested_clean": 1}
    assert [x["id"] for x in snap["untested"]] == [h_open]
    assert {x["id"] for x in snap["visited"]} == {h_vuln, u}
    assert all(x["has_findings"] for x in snap["visited"])   # 带洞标
    assert snap["clean_ids"] == [h_clean]                    # clean 只给 id
    assert snap["untested_truncated"] is False
    assert snap["visited_truncated"] is False

    # cap 截断：再加 5 个未测试 host
    for i in range(5):
        bb.upsert_asset(pid, "host", f"10.2.0.{i}")
    snap2 = situation_snapshot(bb.list_assets(pid), bb.list_findings(pid),
                               untested_cap=3)
    assert len(snap2["untested"]) == 3 and snap2["untested_truncated"] is True
    assert snap2["counts"]["open"] == 6                      # 计数不受 cap 影响
