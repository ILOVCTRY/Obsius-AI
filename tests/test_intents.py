"""意图写入口测试（渗透链路图 v3，website-attack-path-graph，2026-09-24）。

四条硬规则的存储面护栏：
①意图必收尾——declare/close/reopen 状态机；
②收尾必带证据——vuln/finding 引用 finding 必须存在、同项目、非 FP、category
匹配；dead_end 必须带死因 + 证据引用；证据不足拒收（宁严勿松）；
③引用必落地——basis/evidence refs 写入时同项目存在性校验，悬空/编造 ValueError；
④死路是意图关闭态——reopen 清收尾字段但保留 evidence_refs。
"""

import pytest

from core.blackboard import Blackboard
from core.blackboard.intents import (
    INTENT_OUTCOMES, close_intent, dead_end_backing_target, declare_intent,
    get_intent, list_intents, normalize_refs, normalize_refs_verbose,
    reopen_intent,
)


@pytest.fixture()
def ctx(tmp_path):
    bb = Blackboard(str(tmp_path / "intents.db"))
    proj = bb.create_project("意图测试", "pentest")
    pid = proj["id"]
    host = bb.upsert_asset(pid, "host", "10.0.0.1")["id"]
    domain = bb.upsert_asset(pid, "domain", "site.com", parent_id=host)["id"]
    url = bb.upsert_asset(pid, "url", "http://site.com/login",
                         parent_id=domain)["id"]
    yield {"bb": bb, "pid": pid, "host": host, "domain": domain, "url": url}
    bb.close()


def _kinds(bb, pid):
    return [e["kind"] for e in bb.recent_events(pid, 0, 200)]


def _events(bb, pid, kind):
    return [e for e in bb.recent_events(pid, 0, 200) if e["kind"] == kind]


def _vuln_finding(bb, pid, target, *, status="unverified"):
    r = bb.add_finding(pid, "sqli", "登录框 SQL 注入", target_asset_id=target,
                       severity="high", category="vuln", status=status)
    return r["id"]


def _intel_finding(bb, pid, target):
    r = bb.add_finding(pid, title="框架版本泄露", target_asset_id=target,
                       category="intel")
    return r["id"]


# ---------- declare：陈述校验 ----------

@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None, 123])
def test_declare_blank_statement(ctx, bad):
    with pytest.raises(ValueError):
        declare_intent(ctx["bb"], ctx["pid"], bad)


def test_declare_statement_too_long(ctx):
    with pytest.raises(ValueError):
        declare_intent(ctx["bb"], ctx["pid"], "测" * 501)


def test_declare_ok_and_event(ctx):
    r = declare_intent(ctx["bb"], ctx["pid"], " 登录页可能存在 SQL 注入 ")
    assert r["status"] == "open" and r["statement"] == "登录页可能存在 SQL 注入"
    assert r["revision"] == 1 and r["outcome_type"] == ""
    ev = _events(ctx["bb"], ctx["pid"], "intent.declared")
    assert len(ev) == 1 and ev[0]["payload"]["intent_id"] == r["id"]


def test_declare_session_author_event_carries_session(ctx):
    r = declare_intent(ctx["bb"], ctx["pid"], "会话假设", author="sess-abcd")
    ev = _events(ctx["bb"], ctx["pid"], "intent.declared")[0]
    assert ev["session_id"] == "sess-abcd"
    assert r["author"] == "sess-abcd"


# ---------- declare：合并去重 ----------

def test_declare_open_identical_merges_no_event(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    first = declare_intent(bb, pid, "后台可能有未授权访问")
    n_events = len(_kinds(bb, pid))
    again = declare_intent(bb, pid, " 后台可能有未授权访问 ")
    assert again["id"] == first["id"] and again.get("merged") is True
    assert len(_kinds(bb, pid)) == n_events  # 不发事件


def test_declare_identical_after_closed_is_new(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    first = declare_intent(bb, pid, "假设 X")
    hid = _vuln_finding(bb, pid, ctx["url"])
    close_intent(bb, pid, first["id"], "vuln", finding_ids=[hid])
    second = declare_intent(bb, pid, "假设 X")
    assert second["id"] != first["id"] and "merged" not in second


def test_declare_cross_author_identical_not_merged(ctx):
    """2026-09-27 agent-loop 修复：合并按作者收口——B 撞上 A 的 open 同陈述
    意图必须各自落行，否则 B 永远建不出自己的 open 意图，bb_add_finding 门禁
    会把它锁死在硬拒绝循环里。"""
    bb, pid = ctx["bb"], ctx["pid"]
    a = declare_intent(bb, pid, "对登录口进行 sql 注入尝试", author="sess-a" * 3)
    b = declare_intent(bb, pid, "对登录口进行 sql 注入尝试", author="sess-b" * 3)
    assert b["id"] != a["id"] and "merged" not in b
    assert b["author"] == "sess-b" * 3 and a["author"] == "sess-a" * 3
    # 同作者重复声明仍去重复用（原语义保留）
    again = declare_intent(bb, pid, "对登录口进行 sql 注入尝试", author="sess-b" * 3)
    assert again["id"] == b["id"] and again.get("merged") is True


# ---------- declare：目标资产 + basis refs 存在性 ----------

def test_declare_target_asset_must_exist(ctx):
    with pytest.raises(ValueError):
        declare_intent(ctx["bb"], ctx["pid"], "假设", target_asset_id="asset-nope")


def test_declare_basis_refs_all_kinds_ok(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    fid = _vuln_finding(bb, pid, ctx["host"])
    art = bb.add_artifact(pid, "scratch/note.txt", kind="note")
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url="http://site.com/", status=404, resp_body="")
    r = declare_intent(bb, pid, "多依据假设", target_asset_id=ctx["host"],
                       basis_refs=[f"asset:{ctx['host']}", f"finding:{fid}",
                                   f"artifact:{art}", f"http:{hid}",
                                   f"asset:{ctx['host']}"])
    # 归一化：去重、strip、排序保持
    assert r["basis_refs"] == [f"asset:{ctx['host']}", f"finding:{fid}",
                               f"artifact:{art}", f"http:{hid}"]


@pytest.mark.parametrize("ref", [
    "nope", "finding:", "finding:find-nope", "http:abc", "http:999999",
    "event:xyz", "bogus:x",
])
def test_declare_basis_ref_invalid(ctx, ref):
    with pytest.raises(ValueError):
        declare_intent(ctx["bb"], ctx["pid"], "假设", basis_refs=[ref])


def test_basis_refs_must_be_list(ctx):
    with pytest.raises(ValueError):
        declare_intent(ctx["bb"], ctx["pid"], "假设", basis_refs="asset:x")


def test_normalize_refs_empty(ctx):
    assert normalize_refs(ctx["bb"], ctx["pid"], None) == []


def test_normalize_refs_stripped_prefix_autocomplete(ctx):
    """2026-09-30 宽容归一：id 自带 kind 前缀与 kind:<id> 语法结构性碰撞——
    模型稳定写出剥前缀形态 asset:6971xxx（sess-78df38d1741d 五连拒根因）。
    补全后同项目真实存在 → 放行并记录修正。"""
    bb, pid = ctx["bb"], ctx["pid"]
    full = ctx["host"]
    short = full.removeprefix("asset-")
    refs, corr = normalize_refs_verbose(bb, pid, [f"asset:{short}"])
    assert refs == [f"asset:{full}"]
    assert corr == [f"asset:{short} → asset:{full}"]


def test_normalize_refs_bare_id_and_find_alias(ctx):
    """裸 id（asset-xxx / find-xxx，无 kind: 前缀）与 find: 别名也收敛。"""
    bb, pid = ctx["bb"], ctx["pid"]
    fid = _vuln_finding(bb, pid, ctx["host"])
    refs, corr = normalize_refs_verbose(
        bb, pid, [ctx["host"], fid, f"find:{fid.removeprefix('find-')}"])
    assert refs == [f"asset:{ctx['host']}", f"finding:{fid}"]
    assert len(corr) == 3


def test_normalize_refs_dangling_still_rejected(ctx):
    """补全后仍不存在 → 照拒（错误文案带完整形态示例）。"""
    with pytest.raises(ValueError, match="完整 id"):
        normalize_refs_verbose(ctx["bb"], ctx["pid"], ["asset:deadbeef0000"])


def test_declare_ref_corrections_in_return_and_event(ctx):
    """修正记录透出：返回 dict ref_corrections + intent.declared 事件留痕。"""
    bb, pid = ctx["bb"], ctx["pid"]
    short = ctx["host"].removeprefix("asset-")
    r = declare_intent(bb, pid, "剥前缀引用假设", basis_refs=[f"asset:{short}"])
    assert r["ref_corrections"] == [f"asset:{short} → asset:{ctx['host']}"]
    ev = _events(bb, pid, "intent.declared")[-1]
    assert ev["payload"]["ref_corrections"] == r["ref_corrections"]


def test_close_evidence_bare_artifact_autocomplete(ctx):
    """close_intent 的 evidence_refs 同样走宽容归一（dead_end 收尾）；
    artifact 的 id 前缀是 art-（与 kind 不同名），artifact:剥前缀 也能补全。"""
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "死路假设")["id"]
    art = bb.add_artifact(pid, "scratch/n.txt", kind="note")
    short = art.removeprefix("art-")
    r = close_intent(bb, pid, iid, "dead_end", dead_reason="已排除",
                     evidence_refs=[f"artifact:{short}", art])
    assert r["ref_corrections"] == [f"artifact:{short} → artifact:{art}",
                                    f"{art} → artifact:{art}"]
    assert r["evidence_refs"] == [f"artifact:{art}"]


# ---------- close：入参校验 ----------

def test_close_bad_outcome(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "成功")


def test_close_missing_intent_lookup_error(ctx):
    with pytest.raises(LookupError):
        close_intent(ctx["bb"], ctx["pid"], "intent-nope", "dead_end",
                     dead_reason="x", evidence_refs=[f"asset:{ctx['host']}"])


def test_close_double_close(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    fid = _vuln_finding(bb, pid, ctx["url"])
    close_intent(bb, pid, iid, "vuln", finding_ids=[fid])
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "vuln", finding_ids=[fid])


# ---------- close：漏洞/发现收尾必带匹配证据 ----------

def test_close_vuln_requires_finding_ids(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "vuln")


def test_close_vuln_success(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "注入假设")["id"]
    fid = _vuln_finding(bb, pid, ctx["url"])
    r = close_intent(bb, pid, iid, "vuln", finding_ids=[fid])
    assert r["status"] == "closed" and r["outcome_type"] == "vuln"
    assert r["outcome_refs"] == [fid] and r["closed_at"]
    assert r["revision"] == 2
    ev = _events(bb, pid, "intent.closed")
    assert len(ev) == 1 and ev[0]["payload"]["outcome"] == "vuln"


def test_close_vuln_finding_missing(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "vuln", finding_ids=["find-nope"])


def test_close_vuln_with_intel_finding_category_mismatch(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    fid = _intel_finding(bb, pid, ctx["host"])
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "vuln", finding_ids=[fid])


def test_close_finding_requires_intel_category(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    fid = _vuln_finding(bb, pid, ctx["host"])
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "finding", finding_ids=[fid])


def test_close_finding_success_with_intel(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "信息泄露假设")["id"]
    fid = _intel_finding(bb, pid, ctx["host"])
    r = close_intent(bb, pid, iid, "finding", finding_ids=[fid])
    assert r["outcome_type"] == "finding" and r["outcome_refs"] == [fid]


def test_close_with_fp_finding_points_to_dead_end(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    fid = _vuln_finding(bb, pid, ctx["url"], status="false-positive")
    with pytest.raises(ValueError, match="dead_end"):
        close_intent(bb, pid, iid, "vuln", finding_ids=[fid])
    # 意图仍 open（拒绝后不残留半状态）
    assert get_intent(bb, pid, iid)["status"] == "open"


# ---------- close：死路 ----------

def test_close_dead_end_requires_reason(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "dead_end",
                     evidence_refs=[f"asset:{ctx['host']}"])


def test_close_dead_end_requires_evidence(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "dead_end",
                     dead_reason="目录返回 404，备份不存在")


def test_close_dead_end_success(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "备份文件假设")["id"]
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url="http://site.com/www.zip", status=404,
                              resp_body="")
    reason = "  两次 GET 均 404，备份命名不存在，排除此入口  "
    r = close_intent(bb, pid, iid, "dead_end", dead_reason=reason,
                     evidence_refs=[f"http:{hid}"])
    assert r["outcome_type"] == "dead_end"
    assert r["dead_reason"] == reason.strip()
    assert r["evidence_refs"] == [f"http:{hid}"]
    assert r["revision"] == 2


def test_close_dead_end_evidence_ref_dangling(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    with pytest.raises(ValueError):
        close_intent(bb, pid, iid, "dead_end", dead_reason="排除",
                     evidence_refs=["event:999999"])


# ---------- reopen ----------

def test_reopen_missing_lookup_error(ctx):
    with pytest.raises(LookupError):
        reopen_intent(ctx["bb"], ctx["pid"], "intent-nope")


def test_reopen_idempotent_when_open(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "假设")["id"]
    n = len(_kinds(bb, pid))
    r = reopen_intent(bb, pid, iid, note="再确认")
    assert r["status"] == "open" and len(_kinds(bb, pid)) == n  # 不发事件


def test_reopen_clears_outcome_keeps_evidence(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    iid = declare_intent(bb, pid, "备份假设")["id"]
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url="http://site.com/bak", status=404, resp_body="")
    close_intent(bb, pid, iid, "dead_end", dead_reason="404 排除",
                 evidence_refs=[f"http:{hid}"])
    r = reopen_intent(bb, pid, iid, author="human", note="发现新路径线索")
    assert r["status"] == "open"
    assert r["outcome_type"] == "" and r["outcome_refs"] == []
    assert r["dead_reason"] == "" and r["closed_at"] is None
    assert r["evidence_refs"] == [f"http:{hid}"]  # 事实引用保留
    assert r["revision"] == 3
    ev = _events(bb, pid, "intent.reopened")
    assert len(ev) == 1 and ev[0]["payload"]["note"] == "发现新路径线索"


# ---------- list ----------

def test_list_intents_filter_and_order(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    i1 = declare_intent(bb, pid, "假设一")["id"]
    i2 = declare_intent(bb, pid, "假设二")["id"]
    fid = _vuln_finding(bb, pid, ctx["url"])
    close_intent(bb, pid, i1, "vuln", finding_ids=[fid])
    assert {i["id"] for i in list_intents(bb, pid)} == {i1, i2}
    assert [i["id"] for i in list_intents(bb, pid, status="open")] == [i2]
    assert [i["id"] for i in list_intents(bb, pid, status="closed")] == [i1]


def test_list_intents_invalid_status(ctx):
    with pytest.raises(ValueError):
        list_intents(ctx["bb"], ctx["pid"], status="done")


def test_outcomes_contract(ctx):
    # 三选一口径钉死（前端 outcome 徽章同枚举）
    assert INTENT_OUTCOMES == ("vuln", "finding", "dead_end")


# ---------- tested_clean 意图死路背书门禁（2026-09-25） ----------

def _dead_end_on(bb, pid, target: str, statement="该面无洞假设"):
    """对 target 立意图并以死路收尾（证据=一条 http 历史），返回 target。"""
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url="http://site.com/probe", status=404,
                              resp_body="")
    iid = declare_intent(bb, pid, statement, target_asset_id=target)["id"]
    close_intent(bb, pid, iid, "dead_end",
                 dead_reason="探测均 404，排除此入口",
                 evidence_refs=[f"http:{hid}"])
    return target


def test_backing_helper_direct_target(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    assert dead_end_backing_target(bb.conn, pid, ctx["url"]) is None
    _dead_end_on(bb, pid, ctx["url"], "url 无洞")
    assert dead_end_backing_target(bb.conn, pid, ctx["url"]) == ctx["url"]


def test_backing_helper_ancestor_batch_removed(ctx):
    """2026-09-29 逐资产收紧：祖先链批次背书移除——父节点死路意图不再覆盖子树
    （曾致 sess-1d692817a5d0 一条「基线一致」意图批量误标 12 个活站）。"""
    bb, pid = ctx["bb"], ctx["pid"]
    _dead_end_on(bb, pid, ctx["host"], "宿主子树无洞")
    assert dead_end_backing_target(bb.conn, pid, ctx["host"]) == ctx["host"]
    # 子资产不被父节点意图背书：domain/url 须各自立意
    assert dead_end_backing_target(bb.conn, pid, ctx["url"]) is None
    assert dead_end_backing_target(bb.conn, pid, ctx["domain"]) is None


def test_backing_helper_basis_refs_direct(ctx):
    """basis_refs 明确含本资产 id 也算直接背书（意图围绕该资产的另一立意形态）。"""
    bb, pid = ctx["bb"], ctx["pid"]
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url=f"http://site.com/probe-{ctx['url']}",
                              status=404, resp_body="")
    iid = declare_intent(bb, pid, "针对该资产的假设",
                         target_asset_id=ctx["host"],
                         basis_refs=[f"asset:{ctx['url']}"])["id"]
    close_intent(bb, pid, iid, "dead_end", dead_reason="探测均 404，排除",
                 evidence_refs=[f"http:{hid}"])
    assert dead_end_backing_target(bb.conn, pid, ctx["url"]) == ctx["host"]


def test_backing_helper_open_or_non_dead_end_not_backing(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    # open 意图不背书
    declare_intent(bb, pid, "假设", target_asset_id=ctx["url"])
    assert dead_end_backing_target(bb.conn, pid, ctx["url"]) is None
    # vuln 收尾不背书
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url="http://site.com/", status=200, resp_body="")
    iid = declare_intent(bb, pid, "注入假设", target_asset_id=ctx["host"])["id"]
    fid = _vuln_finding(bb, pid, ctx["url"])
    close_intent(bb, pid, iid, "vuln", finding_ids=[fid])
    assert dead_end_backing_target(bb.conn, pid, ctx["url"]) is None


def test_backing_helper_side_tree_not_backing(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    other_host = bb.upsert_asset(pid, "host", "10.0.0.2")["id"]
    other_url = bb.upsert_asset(pid, "url", "http://other.com/",
                               parent_id=other_host)["id"]
    _dead_end_on(bb, pid, other_host, "旁支宿主无洞")
    # 旁支 host 的死路意图不覆盖本树 url；旁支子资产也不再被祖先链背书
    assert dead_end_backing_target(bb.conn, pid, ctx["url"]) is None
    assert dead_end_backing_target(bb.conn, pid, other_url) is None


def test_set_status_clean_allowed_with_direct_backing(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    with pytest.raises(ValueError, match="死路意图背书"):
        bb.set_asset_status(ctx["url"], "tested_clean", note="测完无洞")
    _dead_end_on(bb, pid, ctx["url"], "url 无洞")
    r = bb.set_asset_status(ctx["url"], "tested_clean", note="死路已背书")
    assert r["status"] == "tested_clean"


def test_set_status_clean_rejected_with_ancestor_batch_only(ctx):
    """2026-09-29：仅父节点死路意图（祖先链批次）不再放行子资产 tested_clean。"""
    bb, pid = ctx["bb"], ctx["pid"]
    _dead_end_on(bb, pid, ctx["host"], "宿主批次无洞")
    with pytest.raises(ValueError, match="直接围绕该资产"):
        bb.set_asset_status(ctx["url"], "tested_clean", note="随宿主批次收口")


def test_set_status_clean_noop_without_backing_preserves_legacy(ctx):
    bb = ctx["bb"]
    # 存量已 tested_clean（模拟门禁上线前的盘上值）：同状态 no-op 在背书
    # 检查之前返回，无背书也放行
    aid = bb.upsert_asset(ctx["pid"], "url", "http://legacy.com/")["id"]
    bb.conn.execute("UPDATE assets SET status='tested_clean' WHERE id=?", (aid,))
    bb.conn.commit()
    r = bb.set_asset_status(aid, "tested_clean", note="重复标")
    assert r["status"] == "tested_clean"


def test_set_status_na_and_budget_stop_need_no_backing(ctx):
    bb, pid = ctx["bb"], ctx["pid"]
    r1 = bb.set_asset_status(ctx["url"], "na", note="非目标协议")
    r2 = bb.set_asset_status(ctx["host"], "budget_stop", note="配额用尽停手")
    assert r1["status"] == "na" and r2["status"] == "budget_stop"
