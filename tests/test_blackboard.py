"""黑板系统测试：去重合并、认领互斥、lease 回收、链校验、事件增量、审计溯源。"""

import sqlite3
import threading

import pytest

from core.blackboard import Blackboard, ClaimError, TaskQueue


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def project(bb):
    return bb.create_project("渗透-测试项目", "pentest", ["web"],
                             config={"cross_target": "ask-orchestrator"})


def _session(bb, project, name="S1"):
    return bb.register_session(project["id"], name)


# ---------- 基础与审计 ----------

def test_project_and_session(bb, project):
    # v2 绑定：track + capabilities；旧 domain 列写轨名兜底
    assert project["track"] == "pentest"
    assert project["capabilities"] == ["web"]
    assert project["domain"] == "pentest"
    assert bb.get_project(project["id"])["config"]["cross_target"] == "ask-orchestrator"
    sess = _session(bb, project, "S1主攻")
    assert sess["role"] == "_generalist"


def test_legacy_db_row_domain_mapped(bb):
    """v1 库行（无 track/capabilities，只有 domain=pentest）经 get_project 读兼容映射。"""
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO projects(id,name,domain,track,capabilities,config,created_at)"
            " VALUES('proj-legacydb','旧库','pentest','','[]','{}','2026-01-01T00:00:00+00:00')")
    got = bb.get_project("proj-legacydb")
    assert got["track"] == "pentest" and got["capabilities"] == ["web"]
    assert got["domain"] == "pentest"


def test_schema_v7_migration(tmp_path):
    """旧库幂等升到当前版（v7：机制 1.1/机制 1.4 workset/dedup_fp/wait_for 列 + resource_leases 表）。"""
    from core.blackboard.schema import SCHEMA_VERSION
    from core.blackboard.store import Blackboard as BB
    from core.orchestrator import state as orch_state

    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT NOT NULL,
            track TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '[]',
            config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
            task_type TEXT NOT NULL DEFAULT 'generic', objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', priority INTEGER NOT NULL DEFAULT 2,
            noise_budget TEXT NOT NULL DEFAULT 'passive',
            conflict_keys TEXT NOT NULL DEFAULT '[]', claimed_by TEXT,
            lease_until TEXT, parent_id TEXT, created_by TEXT NOT NULL DEFAULT 'human',
            result_note TEXT NOT NULL DEFAULT '',
            context_refs TEXT NOT NULL DEFAULT '[]',
            stale_refs TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE orchestrator_state (
            project_id TEXT PRIMARY KEY, tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0, tokens_cache_read INTEGER NOT NULL DEFAULT 0,
            tokens_cache_creation INTEGER NOT NULL DEFAULT 0, llm_calls INTEGER NOT NULL DEFAULT 0,
            tasks_published INTEGER NOT NULL DEFAULT 0, budget_warned INTEGER NOT NULL DEFAULT 0,
            event_cursor INTEGER NOT NULL DEFAULT 0, cycles INTEGER NOT NULL DEFAULT 0,
            last_digest_cycle INTEGER NOT NULL DEFAULT -999,
            chain_active INTEGER NOT NULL DEFAULT 0, chain_ticks INTEGER NOT NULL DEFAULT 0,
            last_auto_tick_at TEXT NOT NULL DEFAULT '', auto_ticks_total INTEGER NOT NULL DEFAULT 0,
            tick_owner TEXT NOT NULL DEFAULT '', tick_lease_until TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '');
        """
    )
    raw.commit()
    raw.close()

    board = BB(str(db_path))
    try:
        ver = board.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert int(ver) == SCHEMA_VERSION == 24
        task_cols = {r[1] for r in board.conn.execute("PRAGMA table_info(tasks)")}
        os_cols = {r[1] for r in board.conn.execute(
            "PRAGMA table_info(orchestrator_state)")}
        assert "plan" in task_cols and "last_replan_at" in os_cols
        assert {"last_derive_at", "last_derive_result"} <= os_cols  # v13（mission 派生结果）
        assert "role" in task_cols  # v14（任务绑定角色：认领即换装）
        assert {"workset", "dedup_fp", "wait_for", "lease_cooldown_until"} <= task_cols
        assert "context" in task_cols  # v9（C10 任务执行履历）
        art_cols = {r[1] for r in board.conn.execute("PRAGMA table_info(artifacts)")}
        assert "meta" in art_cols  # v10（W3 产物归属元数据）
        fin_cols = {r[1] for r in board.conn.execute("PRAGMA table_info(findings)")}
        assert "category" in fin_cols  # v12（发现分两类）
        fin_cols = {r[1] for r in board.conn.execute("PRAGMA table_info(findings)")}
        assert "rating_basis" in fin_cols  # v11（F11 判级依据）
        assert {"impact", "remediation"} <= fin_cols  # v20（收录格式三件套）
        tables = {r[0] for r in board.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "resource_leases" in tables
        assert "intents" in tables  # v22（渗透链路图：意图规划产物）

        proj = board.create_project("迁移项目", "ctf", ["binary"])
        tq = TaskQueue(board)
        tid = tq.publish(proj["id"], "旧库新任务")
        assert tq.get_task(tid)["plan"] == []  # 旧行默认空计划
        assert tq.get_task(tid)["context"] == {"attachments": []}  # 空履历=仅空附件容器
        saved = orch_state.save_fields(
            board, proj["id"], last_replan_at="2026-09-15T00:00:00+00:00")
        assert saved["last_replan_at"] == "2026-09-15T00:00:00+00:00"
    finally:
        board.close()


def test_human_attribution_recorded(bb, project):
    """人机共写：author 必须可溯源（§6.5）。"""
    r = bb.add_finding(project["id"], "info-leak", "备份文件泄露", author="human")
    row = [f for f in bb.list_findings(project["id"]) if f["id"] == r["id"]][0]
    assert row["author"] == "human"


# ---------- 资产去重 ----------

def test_asset_upsert_dedup(bb, project):
    pid = project["id"]
    a1 = bb.upsert_asset(pid, "domain", "xxx.example.com", author="session-1")
    a2 = bb.upsert_asset(pid, "domain", "xxx.example.com", author="session-2")
    assert a1["created"] and not a2["created"]
    assert a1["id"] == a2["id"]
    assert len(bb.list_assets(pid, "domain")) == 1


# ---------- findings 去重合并 ----------

def test_finding_dedup_merge(bb, project):
    pid = project["id"]
    r1 = bb.add_finding(pid, "sqli", "登录框 SQL 注入", severity="high", confidence=0.6)
    r2 = bb.add_finding(pid, "sqli", "登录框 SQL 注入", severity="high", confidence=0.9)
    assert not r1["merged"] and r2["merged"]
    assert r1["id"] == r2["id"]
    rows = bb.list_findings(pid, min_severity="high")
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.9  # 取最大


def test_finding_dedup_respects_target(bb, project):
    """同 vuln_class 但不同目标资产 → 不合并。"""
    pid = project["id"]
    a = bb.upsert_asset(pid, "domain", "a.com")["id"]
    b = bb.upsert_asset(pid, "domain", "b.com")["id"]
    r1 = bb.add_finding(pid, "xss", "XSS", target_asset_id=a)
    r2 = bb.add_finding(pid, "xss", "XSS", target_asset_id=b)
    assert r1["id"] != r2["id"]


def test_rev_finding_without_vuln_class_no_merge(bb, project):
    """逆向发现类别在 evidence.category：省略 vuln_class 时各是各的，不做空键合并。"""
    pid = project["id"]
    r1 = bb.add_finding(pid, title="校验算法", evidence={"category": "algorithm"})
    r2 = bb.add_finding(pid, title="通信协议", evidence={"category": "protocol"})
    assert not r1["merged"] and not r2["merged"] and r1["id"] != r2["id"]
    assert len(bb.list_findings(pid)) == 2


# ---------- M5 D1 疑似重复警告（orchestrator-efficiency §0-9） ----------

def test_add_finding_dedup_warning(bb, project):
    """同目标+同类（vuln_class）但 dedup_key 不同（显式指纹分裂）→ 返回 dedup_warning
    指路旧条目；同 key 重报走合并无警告；无 target 或 vuln_class 空不查。"""
    pid = project["id"]
    a = bb.upsert_asset(pid, "domain", "a.com")["id"]
    r1 = bb.add_finding(pid, "sqli", "id 注入", target_asset_id=a, dedup_key="k-time")
    r2 = bb.add_finding(pid, "sqli", "order 注入", target_asset_id=a, dedup_key="k-order")
    assert not r2["merged"]
    assert r2["dedup_warning"] == [{"id": r1["id"], "title": "id 注入"}]
    # 同 key 重报走合并分支：并入的本体行（k-time）不算，但另一条分裂指纹（k-order）仍提示
    r3 = bb.add_finding(pid, "sqli", "id 注入（复现）", target_asset_id=a, dedup_key="k-time")
    assert r3["merged"]
    assert r3["dedup_warning"] == [{"id": r2["id"], "title": "order 注入"}]
    # 无 target → 不查
    r4 = bb.add_finding(pid, "sqli", "无目标发现", dedup_key="k-x")
    assert "dedup_warning" not in r4
    # vuln_class 空（逆向常态：同目标多函数发现）→ 不查
    r5 = bb.add_finding(pid, "", "校验算法", target_asset_id=a, dedup_key="k-y")
    assert "dedup_warning" not in r5


def test_add_finding_dedup_warning_cap3(bb, project):
    """警告列表 cap 3（最新优先）。"""
    pid = project["id"]
    a = bb.upsert_asset(pid, "domain", "a.com")["id"]
    for i in range(5):
        bb.add_finding(pid, "sqli", f"f{i}", target_asset_id=a, dedup_key=f"k{i}")
    r = bb.add_finding(pid, "sqli", "新发现", target_asset_id=a, dedup_key="k-new")
    assert len(r["dedup_warning"]) == 3


# ---------- relates_to 强关系（E0） ----------

def test_relates_to_stored_and_union_dedup(bb, project):
    """合法强边落 evidence.relates_to；重报同 finding 走并集：同边去重、异 note 保留。"""
    pid = project["id"]
    # 注意 dedup 指纹是 vuln_class+target（不含 title），基线与升级用不同 vuln_class
    base = bb.add_finding(pid, "info-leak", "备份文件泄露", severity="medium")["id"]

    r = bb.add_finding(pid, "sqli", "id 参数可堆叠注入", severity="high",
                       evidence={"relates_to": [{"finding_id": base, "note": "同点升级"}]})
    assert not r["merged"]
    f = bb.get_finding(pid, r["id"])
    assert f["evidence"]["relates_to"] == [{"finding_id": base, "note": "同点升级"}]

    # 重报同一 dedup（vuln_class 相同且同 target）：同内容边去重
    assert bb.add_finding(pid, "sqli", "id 参数可堆叠注入", severity="high",
                          evidence={"relates_to": [{"finding_id": base, "note": "同点升级"}]})["merged"]
    f = bb.get_finding(pid, r["id"])
    assert len(f["evidence"]["relates_to"]) == 1

    # 不同 note = 不同边，追加
    bb.add_finding(pid, "sqli", "id 参数可堆叠注入", severity="critical",
                   evidence={"relates_to": [{"finding_id": base, "note": "升到 DBA"}]})
    f = bb.get_finding(pid, r["id"])
    notes = {e["note"] for e in f["evidence"]["relates_to"]}
    assert notes == {"同点升级", "升到 DBA"}
    assert f["severity"] == "critical"  # 严重度就高


def test_relates_to_dangling_and_malformed_rejected(bb, project):
    """悬空 id / 形态非法 / note 非字符串 → ValueError（API 层 422），严格不静默丢。"""
    pid = project["id"]
    with pytest.raises(ValueError, match="不存在或不属于本项目"):
        bb.add_finding(pid, "x", "悬空强边",
                       evidence={"relates_to": [{"finding_id": "find-deadbeef", "note": "n"}]})
    with pytest.raises(ValueError, match="finding_id"):
        bb.add_finding(pid, "x", "缺 id", evidence={"relates_to": [{"note": "n"}]})
    with pytest.raises(ValueError, match="对象"):
        bb.add_finding(pid, "x", "非对象项", evidence={"relates_to": ["find-aaaa"]})
    with pytest.raises(ValueError, match="列表"):
        bb.add_finding(pid, "x", "非列表", evidence={"relates_to": {"finding_id": "x"}})
    with pytest.raises(ValueError, match="note 必须是字符串"):
        bb.add_finding(pid, "x", "note 非字符串",
                       evidence={"relates_to": [{"finding_id": "find-deadbeef", "note": 1}]})
    # 拒绝后无残留写入
    assert bb.list_findings(pid) == []


def test_relates_to_cross_project_rejected(bb, project):
    """引用别的项目的发现 → ValueError（防跨项目幻觉串线）。"""
    pid = project["id"]
    other = bb.create_project("别的项目", "ctf", ["binary"])["id"]
    foreign = bb.add_finding(other, "rev", "别项目的发现")["id"]
    with pytest.raises(ValueError, match="不存在或不属于本项目"):
        bb.add_finding(pid, "sqli", "串线发现",
                       evidence={"relates_to": [{"finding_id": foreign, "note": "跨项目"}]})


def test_patch_finding_relates_to_validation_and_preserve(bb, project):
    """PATCH 携带悬空强边 → ValueError；PATCH 其他 evidence 键不丢既有 relates_to。"""
    pid = project["id"]
    base = bb.add_finding(pid, "port-scan", "基线发现")["id"]
    fid = bb.add_finding(pid, "sqli", "升级发现",
                         evidence={"relates_to": [{"finding_id": base, "note": "升级"}]})["id"]

    with pytest.raises(ValueError, match="不存在或不属于本项目"):
        bb.patch_finding(pid, fid, evidence={"relates_to": [{"finding_id": "find-xxx", "note": "x"}]})

    # 浅层 merge：改别的键，relates_to 原样保留
    out = bb.patch_finding(pid, fid, evidence={"manual_note": "人工备注"})
    assert out["evidence"]["relates_to"] == [{"finding_id": base, "note": "升级"}]
    assert out["evidence"]["manual_note"] == "人工备注"

    # 显式带合法强边 = 键级覆盖（PATCH 语义不并集）
    base2 = bb.add_finding(pid, "info", "另一基线")["id"]
    out = bb.patch_finding(pid, fid, evidence={"relates_to": [{"finding_id": base2, "note": "改挂"}]})
    assert out["evidence"]["relates_to"] == [{"finding_id": base2, "note": "改挂"}]


def test_relates_to_self_merge_target_rejected(bb, project):
    """合并分支自引防护：同 dedup 键新报 relates_to 指向将并入的既有发现 → ValueError（防自环边）。"""
    pid = project["id"]
    first = bb.add_finding(pid, "sqli", "注入点")["id"]
    # 同 vuln_class+target(均无 target) 必命中合并；引用合并目标自身必须拒
    with pytest.raises(ValueError, match="不能引用发现自身"):
        bb.add_finding(pid, "sqli", "注入点升级",
                       evidence={"relates_to": [{"finding_id": first, "note": "自指"}]})
    # 原行未被污染（severity 也不应升级、无自边）
    got = bb.get_finding(pid, first)
    assert got["evidence"].get("relates_to", []) == []
    assert got["severity"] == "info"


# ---------- func_kb ----------

def test_func_kb_upsert_and_history(bb, project):
    pid = project["id"]
    sha = "ab3f" * 16
    r1 = bb.upsert_func(pid, sha, 0x401176, "sub_401176", "读 stdin", confidence=0.4)
    r2 = bb.upsert_func(pid, sha, 0x401176, "parse_user_input", "未检查长度", confidence=0.8)
    assert r1["created"] and not r2["created"]
    f = bb.lookup_func(pid, sha, 0x401176)
    assert f["confidence"] == 0.8
    names = [h["name"] for h in f["name_history"]]
    assert names == ["sub_401176", "parse_user_input"]  # 重命名演变史 = 分析笔记
    assert "读 stdin" in f["analysis"] and "未检查长度" in f["analysis"]  # 追加不覆盖


def test_func_kb_risk_filter(bb, project):
    pid, sha = project["id"], "cd" * 32
    bb.upsert_func(pid, sha, 0x1000, "f1", risk_tags=["stack-overflow"])
    bb.upsert_func(pid, sha, 0x2000, "f2", risk_tags=["fmt-str"])
    hits = bb.list_funcs_by_risk(pid, sha, "stack-overflow")
    assert [f["name"] for f in hits] == ["f1"]


# ---------- 攻击链 ----------

def test_chain_manual_build_seq_and_events(bb, project):
    """人工建链：seq 自动追加；改名/状态/边注/删中间重排；五类事件齐。"""
    pid = project["id"]
    func = bb.upsert_func(pid, "ab" * 32, 0x401176, "read_input")["id"]
    finding = bb.add_finding(pid, "stack-overflow", "read 无长度检查")["id"]
    art = bb.add_artifact(pid, "poc/exp.py", kind="poc", description="利用脚本")
    chain = bb.create_chain(pid, "栈溢出→shell", goal="获取 shell")
    l1 = bb.add_chain_link(pid, chain, "func_kb", func, "溢出可覆盖返回地址")
    l2 = bb.add_chain_link(pid, chain, "finding", finding, "需要先 leak libc")
    l3 = bb.add_chain_link(pid, chain, "artifact", art)
    d = bb.get_chain(chain)
    assert [l["seq"] for l in d["links"]] == [1, 2, 3]
    assert bb.list_chains(pid)[0]["link_count"] == 3

    bb.update_link_note(pid, l2, "先 leak 再 ret2libc")
    assert bb.get_chain(chain)["links"][1]["edge_note"] == "先 leak 再 ret2libc"

    # 删中间节点 → 剩余按旧序重排 1..n（线性序不留洞）
    assert bb.delete_chain_link(pid, l2)["chain_id"] == chain
    seqs = [(l["id"], l["seq"]) for l in bb.get_chain(chain)["links"]]
    assert seqs == [(l1, 1), (l3, 2)]

    bb.update_chain(pid, chain, name="破链", status="validated")
    assert bb.get_chain(chain)["status"] == "validated"
    with pytest.raises(ValueError):
        bb.update_chain(pid, chain, status="nonsense")

    chain_events = [e["kind"] for e in bb.recent_events(pid) if e["kind"].startswith("chain.")]
    assert chain_events == [
        "chain.created", "chain.link_added", "chain.link_added", "chain.link_added",
        "chain.updated", "chain.link_removed", "chain.updated",
    ]
    link_payload = [e for e in bb.recent_events(pid) if e["kind"] == "chain.link_added"][0]
    assert link_payload["payload"]["node_type"] == "func_kb"
    assert link_payload["payload"]["node_id"] == func


def test_chain_node_validation(bb, project):
    pid = project["id"]
    chain = bb.create_chain(pid, "链")
    with pytest.raises(ValueError, match="非法节点类型"):
        bb.add_chain_link(pid, chain, "host", "x")
    with pytest.raises(ValueError, match="禁止跨项目/引用未落库实体"):
        bb.add_chain_link(pid, chain, "func_kb", "func-不存在")  # 防幻觉校验


def test_chain_cross_project_isolation(bb, project):
    """跨项目：链不可见（LookupError/None/False），别项目节点不可挂（ValueError）。"""
    pid = project["id"]
    other = bb.create_project("别的项目", "ctf", ["binary"])["id"]
    func_other = bb.upsert_func(other, "ab" * 32, 0x1000, "f")["id"]
    chain = bb.create_chain(pid, "本项目链")

    with pytest.raises(LookupError):
        bb.add_chain_link(other, chain, "func_kb", func_other)  # 链对 other 不存在
    func_me = bb.upsert_func(pid, "cd" * 32, 0x2000, "g")["id"]
    with pytest.raises(ValueError):
        bb.add_chain_link(pid, chain, "func_kb", func_other)  # 节点不属本项目
    link = bb.add_chain_link(pid, chain, "func_kb", func_me)

    assert bb.update_chain(other, chain, status="exploited") is None
    assert bb.delete_chain(other, chain) is False
    assert bb.update_link_note(other, link, "x") is None
    assert bb.delete_chain_link(other, link) is None
    assert bb.list_chains(other) == []
    # 本项目链毫发无损
    assert bb.get_chain(chain)["links"][0]["id"] == link


def test_chain_orphan_node_and_delete(bb, project):
    """实体被删后链仍可读（孤儿节点占位由 API 层组装）；删链清头+links。"""
    pid = project["id"]
    finding = bb.add_finding(pid, "x", "临时发现")["id"]
    chain = bb.create_chain(pid, "孤儿链")
    bb.add_chain_link(pid, chain, "finding", finding)
    with bb._tx():
        bb.conn.execute("DELETE FROM findings WHERE id=?", (finding,))
    assert bb.get_chain(chain)["links"][0]["node_id"] == finding  # 不报错
    assert bb.delete_chain(pid, chain) is True
    assert bb.get_chain(chain) is None
    with bb._tx():
        n = bb.conn.execute("SELECT COUNT(*) c FROM chain_links").fetchone()["c"]
    assert n == 0
    assert bb.delete_chain(pid, "chain-missing") is False


# ---------- 任务队列 ----------

def test_task_claim_and_finish(bb, project):
    pid = project["id"]
    s1 = _session(bb, project)
    tq = TaskQueue(bb)
    t = tq.publish(pid, "枚举子域名", scope="domain:xxx.com", task_type="recon",
                   created_by="orchestrator")
    tq.claim(t, s1["id"])
    task = tq.get_task(t)
    assert task["status"] == "claimed" and task["claimed_by"] == s1["id"]
    assert task["lease_until"] is not None
    with pytest.raises(ClaimError):
        tq.claim(t, "sess-other")  # 已被认领
    tq.complete(t, s1["id"], "发现 12 个子域")
    assert tq.get_task(t)["status"] == "done"


def test_active_tasks_conflict_on_ip(bb, project):
    """active 任务按 conflict_keys 互斥（§6.2：同 IP 只许一个 active，防 WAF）。"""
    pid = project["id"]
    s1, s2 = _session(bb, project, "S1"), _session(bb, project, "S2")
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "漏扫 1.2.3.4", noise_budget="medium",
                    conflict_keys=["ip:1.2.3.4"], created_by="orchestrator")
    t2 = tq.publish(pid, "爆破同 IP 另一域名", noise_budget="medium",
                    conflict_keys=["ip:1.2.3.4"], created_by="orchestrator")
    tq.claim(t1, s1["id"])
    # 机制 1.4：冲突统一为资源租约语义——保持 open + wait_for 门控标记，消息带占用者
    with pytest.raises(ClaimError, match="认领被拒：资源 ip:1.2.3.4 已被"):
        tq.claim(t2, s2["id"])
    assert tq.get_task(t2)["wait_for"] == ["ip:1.2.3.4"]
    tq.complete(t1, s1["id"])
    tq.claim(t2, s2["id"])  # 前一个 done 后即可认领


def test_passive_tasks_share(bb, project):
    """passive 任务（逆向分析等）任意共享，不互斥。"""
    pid = project["id"]
    s1, s2 = _session(bb, project, "S1"), _session(bb, project, "S2")
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "分析战斗逻辑", task_type="analyze", created_by="orchestrator")
    t2 = tq.publish(pid, "分析通信逻辑", task_type="analyze", created_by="orchestrator")
    tq.claim(t1, s1["id"])
    tq.claim(t2, s2["id"])  # 无 conflict_keys，不冲突


def test_active_requires_conflict_keys(bb, project):
    tq = TaskQueue(bb)
    with pytest.raises(ValueError, match="conflict_keys"):
        tq.publish(project["id"], "漏扫", noise_budget="high")


def test_update_task_rules(bb, project):
    """任务编辑：open/failed 可改；claimed 仅可改 role（v0.71 热换装）/done 拒；
    非 passive 必填 conflict_keys。"""
    pid = project["id"]
    s = _session(bb, project)
    tq = TaskQueue(bb)
    t = tq.publish(pid, "原目标", task_type="recon", priority=2)
    out = tq.update_task(t, objective="新目标", priority=0)
    assert out["objective"] == "新目标" and out["priority"] == 0
    tq.claim(t, s["id"])
    with pytest.raises(ValueError, match="仅可修改角色"):
        tq.update_task(t, objective="改不动")  # claimed：objective 已固化进在跑会话
    # v0.71：claimed 改 role 放行（热换装），改完可读回
    out = tq.update_task(t, role="recon-lead")
    assert out["role"] == "recon-lead"
    tq.complete(t, s["id"])
    with pytest.raises(ValueError, match="不可编辑"):
        tq.update_task(t, objective="战果不可改")  # done
    # failed 可编辑但状态不变
    t2 = tq.publish(pid, "失败任务", noise_budget="low", conflict_keys=["ip:9.9.9.9"])
    tq.claim(t2, s["id"])
    tq.fail(t2, s["id"], "炸了")
    tq.update_task(t2, objective="失败任务 v2")
    assert tq.get_task(t2)["status"] == "failed"
    with pytest.raises(ValueError, match="conflict_keys"):
        tq.update_task(t2, noise_budget="high", conflict_keys=[])  # active 必须有互斥键
    with pytest.raises(ValueError, match="不可编辑字段"):
        tq.update_task(t2, claimed_by="sess-x")  # 白名单外字段
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.updated"]
    assert ev[-1]["payload"]["changes"]["objective"] == "失败任务 v2"


def test_reopen_failed_task(bb, project):
    """失败放回：failed→open，清持有方/租约，result_note 保留，落 task.reopened。"""
    pid = project["id"]
    s = _session(bb, project)
    tq = TaskQueue(bb)
    t = tq.publish(pid, "再试一次", noise_budget="low", conflict_keys=["ip:8.8.8.8"])
    tq.claim(t, s["id"])
    tq.fail(t, s["id"], "会话结束但任务未收尾", blocked_reason="aborted")
    tq.reopen(t)
    row = tq.get_task(t)
    assert row["status"] == "open" and row["claimed_by"] is None
    assert row["lease_until"] is None
    assert row["blocked_reason"] == "error"  # 放回复位上一轮 blocked（2026-09-24，防 open+aborted 死锁）
    assert row["result_note"] == "会话结束但任务未收尾"  # 保留在库（open 卡片不渲染）
    assert "task.reopened" in [e["kind"] for e in bb.recent_events(pid)]
    with pytest.raises(ValueError, match="仅失败任务可放回"):
        tq.reopen(t)  # open 不可放回
    tq.claim(t, s["id"])
    tq.complete(t, s["id"])
    with pytest.raises(ValueError, match="仅失败任务可放回"):
        tq.reopen(t)  # done 不可放回


def test_plan_first_set_and_revision(bb, project):
    """A2：首次写计划服务端发号 + task.plan_set；修订保留状态/ts、丢弃缺步、落 plan_revised。"""
    pid = project["id"]
    s = _session(bb, project)
    tq = TaskQueue(bb)
    t = tq.publish(pid, "分析样本", task_type="analyze")
    tq.claim(t, s["id"])

    plan = tq.set_plan(t, s["id"], [{"title": "查黑板"}, {"title": "静态分析"}, {"title": "落结论"}])
    assert [x["id"] for x in plan] == ["p1", "p2", "p3"]
    assert all(x["status"] == "todo" and x["ts"] for x in plan)
    assert tq.get_task(t)["plan"][1]["title"] == "静态分析"
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.plan_set"]
    assert len(ev) == 1 and ev[-1]["payload"]["task_id"] == t

    # p1 做完、p2 进行中，修订：保留 p1/p2（p1 done/ts 不变，p2 标题可改），
    # 丢弃 p3，新增一步发号 p4
    tq.step_plan(t, s["id"], "p1", "done")
    p1_ts_before = tq.get_task(t)["plan"][0]["ts"]
    tq.step_plan(t, s["id"], "p2", "doing")
    revised = tq.set_plan(t, s["id"], [
        {"id": "p1", "title": "查黑板"},
        {"id": "p2", "title": "静态分析（加深）"},
        {"title": "动态验证"},
    ], rev_reason="需要动态验证补充")
    assert [x["id"] for x in revised] == ["p1", "p2", "p4"]
    by_id = {x["id"]: x for x in revised}
    assert by_id["p1"]["status"] == "done" and by_id["p1"]["ts"] == p1_ts_before
    assert by_id["p2"]["status"] == "doing" and by_id["p2"]["title"] == "静态分析（加深）"
    assert by_id["p4"]["status"] == "todo"
    rev_ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.plan_revised"]
    assert len(rev_ev) == 1
    payload = rev_ev[-1]["payload"]
    assert payload["rev_reason"] == "需要动态验证补充"
    assert len(payload["old_plan"]) == 3 and payload["session_id"] == s["id"]
    # 首次事件只有一条（修订没有重发 plan_set）
    assert len([e for e in bb.recent_events(pid) if e["kind"] == "task.plan_set"]) == 1


def test_plan_step_auto_demote_and_blocked_note(bb, project):
    """A2：至多一个 doing（旧 doing 自动回 todo 记 auto_demoted）；blocked 无 note 拒。"""
    pid = project["id"]
    s = _session(bb, project)
    tq = TaskQueue(bb)
    t = tq.publish(pid, "打站点", task_type="web")
    tq.claim(t, s["id"])
    tq.set_plan(t, s["id"], [{"title": "a"}, {"title": "b"}, {"title": "c"}])

    tq.step_plan(t, s["id"], "p1", "doing")
    task = tq.step_plan(t, s["id"], "p2", "doing")  # 切 doing：p1 自动回 todo
    plan = {x["id"]: x["status"] for x in task["plan"]}
    assert plan == {"p1": "todo", "p2": "doing", "p3": "todo"}
    step_ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.step"]
    assert step_ev[-1]["payload"]["auto_demoted"] == ["p1"]

    with pytest.raises(ValueError, match="blocked 必须"):
        tq.step_plan(t, s["id"], "p2", "blocked")
    task = tq.step_plan(t, s["id"], "p2", "blocked", note="等目标账号")
    blocked = next(x for x in task["plan"] if x["id"] == "p2")
    assert blocked["status"] == "blocked" and blocked["note"] == "等目标账号"
    assert step_ev[-1]["payload"]["task_id"] == t

    with pytest.raises(ValueError, match="非法计划步状态"):
        tq.step_plan(t, s["id"], "p1", "wat")
    with pytest.raises(ValueError, match="计划步不存在"):
        tq.step_plan(t, s["id"], "p9", "doing")


def test_plan_permissions(bb, project):
    """A2：仅 claimed 持有者可写计划；空计划/空标题拒绝。"""
    pid = project["id"]
    s1, s2 = _session(bb, project, "S1"), _session(bb, project, "S2")
    tq = TaskQueue(bb)
    t = tq.publish(pid, "活", task_type="generic")
    with pytest.raises(ClaimError):
        tq.set_plan(t, s1["id"], [{"title": "x"}])  # open 态
    tq.claim(t, s1["id"])
    with pytest.raises(ClaimError):
        tq.set_plan(t, s2["id"], [{"title": "x"}])  # 非认领者
    with pytest.raises(ClaimError):
        tq.step_plan(t, s2["id"], "p1", "doing")
    with pytest.raises(ValueError, match="至少包含一个步骤"):
        tq.set_plan(t, s1["id"], [])
    tq.set_plan(t, s1["id"], [{"title": "x"}])
    with pytest.raises(ValueError, match="title 不能为空"):
        tq.set_plan(t, s1["id"], [{"title": "   "}])
    tq.complete(t, s1["id"])
    with pytest.raises(ClaimError):
        tq.set_plan(t, s1["id"], [{"title": "done 不可改"}])


def test_plan_step_refs_grounding(bb, project):
    """意图接地（2026-09-20，借鉴 Intentest）：计划步 refs 引用的黑板对象必须
    真实存在且同项目——悬空/编造/格式坏 id 服务端构造上被拒（反提示注入 +
    反意图漂移的机制级约束，不依赖模型自觉）。"""
    pid = project["id"]
    s = _session(bb, project)
    tq = TaskQueue(bb)
    t = tq.publish(pid, "利用注入", task_type="web")
    tq.claim(t, s["id"])
    f = bb.add_finding(pid, vuln_class="sqli", title="登录框注入",
                       severity="info", evidence={"http": "GET /login"})

    # 合法引用：通过且规范化落盘（plan JSON 里有 refs）
    plan = tq.set_plan(t, s["id"], [
        {"title": "侦察登录框"},
        {"title": "利用注入拿库", "refs": [f"finding:{f['id']}"]},
    ])
    assert plan[1]["refs"] == [f"finding:{f['id']}"]
    assert "refs" not in plan[0]  # 无引用步不添空字段
    assert tq.get_task(t)["plan"][1]["refs"] == [f"finding:{f['id']}"]

    # 修订去引用：不带 refs 的既有步保留但 refs 被清
    revised = tq.set_plan(t, s["id"], [{"id": "p2", "title": "改侦察"}])
    assert "refs" not in revised[0]

    # 悬空 id（不存在）拒绝，提示先查真实 id
    with pytest.raises(ValueError, match="悬空"):
        tq.set_plan(t, s["id"], [{"title": "x", "refs": ["finding:nope"]}])
    # 跨项目对象拒绝：同 id 形态存在于别的项目也不可引用（WHERE project_id 保证）
    other = bb.create_project("别的项目", "ctf", ["web"])
    other_t = TaskQueue(bb).publish(other["id"], "别家的任务")
    with pytest.raises(ValueError, match="悬空"):
        tq.set_plan(t, s["id"], [{"title": "x", "refs": [f"task:{other_t}"]}])
    # 本项目 task 可引用（task 类型也在白名单）
    ok = tq.set_plan(t, s["id"], [{"title": "x", "refs": [f"task:{t}"]}])
    assert ok[0]["refs"] == [f"task:{t}"]
    # 格式坏 / 类型不支持 / 超 5 条
    with pytest.raises(ValueError, match="格式非法"):
        tq.set_plan(t, s["id"], [{"title": "x", "refs": ["没有冒号"]}])
    with pytest.raises(ValueError, match="类型不支持"):
        tq.set_plan(t, s["id"], [{"title": "x", "refs": ["notebook:abc"]}])
    with pytest.raises(ValueError, match="≤5"):
        tq.set_plan(t, s["id"], [{"title": "x", "refs": [f"finding:{f['id']}"] * 6}])
    # 非法 refs 拒绝后计划不被破坏（校验在写库前抛出，_tx 回滚）
    assert tq.get_task(t)["plan"][0]["title"] == "x"

    # 接地容错（2026-09-23，task-0805336bbffc 三连拒熔断）：裸短 id 自动补实际
    # 前缀命中（finding→find-，kind≠id 前缀按实情映射）；规范化串落盘
    short = f["id"].removeprefix("find-")
    plan2 = tq.set_plan(t, s["id"], [{"title": "y", "refs": [f"finding:{short}"]}])
    assert plan2[0]["refs"] == [f"finding:{f['id']}"]  # 补前缀后存完整形态
    # 已带完整前缀的写法不受影响（不二次补）
    plan3 = tq.set_plan(t, s["id"], [{"title": "z", "refs": [f"finding:{f['id']}"]}])
    assert plan3[0]["refs"] == [f"finding:{f['id']}"]
    # 补前缀后仍不存在 → 照旧悬空拒绝（不吞编造 id）
    with pytest.raises(ValueError, match="悬空"):
        tq.set_plan(t, s["id"], [{"title": "x", "refs": ["finding:0123456789ab"]}])
    # event 为自增整数（无前缀歧义），裸 id 直接命中
    ev_id = bb.append_event(pid, "note.manual", {"text": "t"}, author="human")
    ok2 = tq.set_plan(t, s["id"], [{"title": "e", "refs": [f"event:{ev_id}"]}])
    assert ok2[0]["refs"] == [f"event:{ev_id}"]


def test_delete_task_physical_and_side_effects(bb, project):
    """删除：物理删行 + task.deleted 快照事件；done 不可删；claimed 删除释放互斥；子任务防护。"""
    pid = project["id"]
    s1, s2 = _session(bb, project, "S1"), _session(bb, project, "S2")
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "扫 1.2.3.4", noise_budget="medium",
                    conflict_keys=["ip:1.2.3.4"])
    tq.claim(t1, s1["id"])
    t2 = tq.publish(pid, "同 IP 排队", noise_budget="medium",
                    conflict_keys=["ip:1.2.3.4"])
    with pytest.raises(ClaimError):
        tq.claim(t2, s2["id"])
    tq.delete(t1)  # claimed 可删
    assert tq.get_task(t1) is None
    tq.claim(t2, s2["id"])  # conflict_keys/lease 随行走，互斥立即释放
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.deleted"]
    assert ev and ev[0]["payload"]["objective"] == "扫 1.2.3.4"
    assert ev[0]["payload"]["was_status"] == "claimed"
    # A1：done 也可删（战果不再强保留，快照事件留审计）
    t3 = tq.publish(pid, "已完成的战果")
    tq.claim(t3, s2["id"])
    tq.complete(t3, s2["id"], "收工")
    tq.delete(t3)
    assert tq.get_task(t3) is None
    done_ev = [e for e in bb.recent_events(pid)
               if e["kind"] == "task.deleted" and e["payload"]["task_id"] == t3]
    assert done_ev and done_ev[-1]["payload"]["was_status"] == "done"
    t4 = tq.publish(pid, "失败品")
    tq.claim(t4, s2["id"])
    tq.fail(t4, s2["id"], "炸了")
    tq.delete(t4)
    assert tq.get_task(t4) is None
    # 子任务防护（外键 parent_id 自引用）
    parent = tq.publish(pid, "父任务")
    tq.publish(pid, "子任务", parent_id=parent)
    with pytest.raises(ValueError, match="子任务"):
        tq.delete(parent)


def test_take_session_next_no_role_filter_priority(bb, project):
    """Worker 循环（会话中心化 2026-09-25）：窗内队列无类型过滤——exploit 底色窗
    照常按优先级取 P0 recon 委托（换装在起跑后生效）；P0 优先。"""
    pid = project["id"]
    s = _session(bb, project, "exploit-worker")
    tq = TaskQueue(bb)
    t_recon = tq.publish(pid, "信息收集", task_type="recon", priority=0,
                         created_by="orchestrator", target_session=s["id"])
    t_exploit = tq.publish(pid, "打点利用", task_type="exploit", priority=2,
                           created_by="orchestrator", target_session=s["id"])
    got = tq.take_session_next(pid, s["id"])
    assert got == t_recon  # 无类型过滤：P0 recon 直接可取
    got = tq.take_session_next(pid, s["id"])
    assert got == t_exploit


def test_lease_expiry_recycles(bb, project):
    """lease 过期 → 任务回 open，会话挂死不占坑（§6.4）。"""
    pid = project["id"]
    s1 = _session(bb, project)
    tq = TaskQueue(bb)
    t = tq.publish(pid, "长任务", created_by="orchestrator")
    tq.claim(t, s1["id"], lease_minutes=0)
    from core.blackboard.store import now
    with bb._tx():
        bb.conn.execute("UPDATE tasks SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (t,))
    assert tq.expire_leases() == [t]
    assert tq.get_task(t)["status"] == "open"
    s_new = _session(bb, project, "S-new")  # 外键约束：会话必须先登记
    tq.claim(t, s_new["id"])  # 新会话可接手


# ---------- 事件总线 ----------

def test_event_bus_subscriber_and_increment(bb, project):
    """落库即广播 + since_id 增量（§5.3 变更通知）。"""
    pid = project["id"]
    seen = []
    unsubscribe = bb.bus.subscribe(seen.append)
    try:
        bb.append_event(pid, "decision", {"thought": "检查 NX"})
        bb.append_event(pid, "command", {"cmd": "checksec"})
    finally:
        unsubscribe()
    assert [e["kind"] for e in seen] == ["decision", "command"]
    last = seen[-1]["id"]
    bb.append_event(pid, "finding.new", {"x": 1})
    incremental = bb.recent_events(pid, since_id=last)
    assert [e["kind"] for e in incremental] == ["finding.new"]


def test_subscriber_exception_does_not_block_writes(bb, project):
    """订阅者炸了不影响黑板写路径（通知是尽力而为）。"""
    pid = project["id"]

    def bad(_):
        raise RuntimeError("WS 断了")

    bb.bus.subscribe(bad)
    eid = bb.append_event(pid, "command", {"cmd": "nmap"})
    assert eid > 0


def test_prune_thinking_deltas_by_stream_id(bb, project):
    """思考流式增量清剪（2026-09-19）：只删同 project + 同 stream_id 的
    llm.thinking.delta，其它 kind / 其它流不受影响。"""
    pid = project["id"]
    bb.append_event(pid, "llm.thinking.delta", {"stream_id": "s1", "thinking": "a", "seq": 1})
    bb.append_event(pid, "llm.thinking.delta", {"stream_id": "s1", "thinking": "ab", "seq": 2})
    bb.append_event(pid, "llm.thinking.delta", {"stream_id": "s2", "thinking": "x", "seq": 1})
    bb.append_event(pid, "llm.thinking", {"thinking": "终稿", "stream_id": "s1"})
    bb.append_event(pid, "command", {"cmd": "id"})
    assert bb.prune_thinking_deltas(pid, "s1") == 2
    kinds = [(e["kind"], (e["payload"] or {}).get("stream_id"))
             for e in bb.recent_events(pid)]
    assert ("llm.thinking.delta", "s2") in kinds
    assert ("llm.thinking", "s1") in kinds
    assert not any(k == "llm.thinking.delta" and s == "s1" for k, s in kinds)
    # 幂等：再删返 0；跨 project 隔离
    assert bb.prune_thinking_deltas(pid, "s1") == 0


# ---------- 审批（request/decide 唯一 core 入口） ----------

def test_approval_request_and_decide(bb, project):
    """创建 pending 审批 → 人类批准（状态流转 + 审计事件）；重复决策报错。"""
    pid = project["id"]
    appr = bb.request_approval(
        pid, {"type": "net_real", "scope": "http://127.0.0.1:8085"},
        risk="low", requested_by="sess-x")
    assert appr["id"].startswith("appr-") and appr["status"] == "pending"
    row = bb.conn.execute("SELECT * FROM approvals WHERE id=?", (appr["id"],)).fetchone()
    assert row["risk"] == "low" and row["requested_by"] == "sess-x"
    # 批准：decided_by/author 可分离（演练自批标 demo，不冒充人类）
    done = bb.decide_approval(appr["id"], "approved",
                              decided_by="demo-script(auto)", author="demo")
    assert done["status"] == "approved" and done["decided_by"] == "demo-script(auto)"
    kinds = [e["kind"] for e in bb.recent_events(pid)]
    assert "approval.approved" in kinds
    # 重复决策 / 非法决策 / 不存在 → ValueError
    with pytest.raises(ValueError):
        bb.decide_approval(appr["id"], "rejected")
    with pytest.raises(ValueError):
        bb.decide_approval(appr["id"], "maybe")
    with pytest.raises(ValueError):
        bb.decide_approval("appr-nope", "approved")


# ---------- 资产树（DESIGN.md §5.2：host 主行 + domain/service 挂载） ----------

def test_list_assets_parses_meta(bb, project):
    """回归：list_assets 返回的 meta 必须是 dict（曾是 JSON 字符串）。"""
    pid = project["id"]
    bb.upsert_asset(pid, "domain", "x.com", meta={"source": "dns"}, author="human")
    a = bb.list_assets(pid)[0]
    assert a["meta"] == {"source": "dns"}


def test_asset_parent_tree(bb, project):
    pid = project["id"]
    host = bb.upsert_asset(pid, "host", "10.0.0.8")
    assert host["created"] is True
    d1 = bb.upsert_asset(pid, "domain", "portal.corp.cn", parent_id=host["id"])
    d2 = bb.upsert_asset(pid, "domain", "vpn.corp.cn", parent_id=host["id"])
    svc = bb.upsert_asset(pid, "service", "10.0.0.8:8443", parent_id=host["id"])
    # 孤儿写不进库（外键 parent_id → assets.id 强制挂载真实 host）
    with pytest.raises(sqlite3.IntegrityError):
        bb.upsert_asset(pid, "url", "http://x.com/a", parent_id="host-nope")
    free = bb.upsert_asset(pid, "binary", "a" * 64)

    assets = bb.list_assets(pid)
    by_id = {a["id"]: a for a in assets}
    roots = [a for a in assets if not (a["parent_id"] and a["parent_id"] in by_id)]
    kids = [a for a in assets if a["parent_id"] == host["id"]]
    assert {a["value"] for a in kids} == {"portal.corp.cn", "vpn.corp.cn", "10.0.0.8:8443"}
    assert host["id"] in {a["id"] for a in roots}
    assert free["id"] in {a["id"] for a in roots}
    # 去重：同 (type, value, parent) 再写 → 返回既有记录
    dup = bb.upsert_asset(pid, "domain", "vpn.corp.cn", parent_id=host["id"])
    assert dup["id"] == d2["id"] and dup["created"] is False


# ---------- 资产更新（DESIGN.md §5.2：补挂/改 meta 不走 upsert） ----------

def test_set_asset_parent(bb, project):
    pid = project["id"]
    host = bb.upsert_asset(pid, "host", "10.0.0.9")
    a = bb.upsert_asset(pid, "domain", "a.cn", parent_id=host["id"])
    b = bb.upsert_asset(pid, "url", "http://a.cn/")
    # 挂载 + 摘挂
    bb.set_asset_parent(b["id"], a["id"])
    assert bb.get_asset(b["id"])["parent_id"] == a["id"]
    bb.set_asset_parent(b["id"], None)
    assert bb.get_asset(b["id"])["parent_id"] is None
    # 非法操作一律 ValueError：自挂 / 父缺失 / 成环（host 挂到其子孙下）/ 资产不存在
    with pytest.raises(ValueError):
        bb.set_asset_parent(a["id"], a["id"])
    with pytest.raises(ValueError):
        bb.set_asset_parent(a["id"], "asset-nope")
    with pytest.raises(ValueError):
        bb.set_asset_parent(host["id"], a["id"])
    with pytest.raises(ValueError):
        bb.set_asset_parent("asset-nope", None)
    # 审计：asset.reparent 事件落流
    assert any(e["kind"] == "asset.reparent" for e in bb.recent_events(pid))


def test_update_asset_meta(bb, project):
    pid = project["id"]
    a = bb.upsert_asset(pid, "domain", "x.cn", meta={"source": "dns"})
    r = bb.update_asset_meta(a["id"], {"title": "门户首页", "scanned": True})
    assert r["meta"] == {"source": "dns", "title": "门户首页", "scanned": True}
    with pytest.raises(ValueError):
        bb.update_asset_meta("asset-nope", {"title": "t"})


def test_find_asset_ignores_parent(bb, project):
    """find_asset 按 (type, value) 命中（不看 parent）——重报合并路径的基础。"""
    pid = project["id"]
    host = bb.upsert_asset(pid, "host", "10.0.0.9")
    a = bb.upsert_asset(pid, "url", "http://10.0.0.9/x", parent_id=host["id"])
    hit = bb.find_asset(pid, "url", "http://10.0.0.9/x")
    assert hit and hit["id"] == a["id"] and hit["meta"] == {}
    assert bb.find_asset(pid, "url", "http://nope") is None


def test_finding_merge_backfills_poc_artifact(bb, project):
    """合并路径回填 POC 引用：旧行没有 poc_artifact_id 且本次带了 → 回填；已有则不覆盖。"""
    pid = project["id"]
    art = bb.add_artifact(pid, "poc/poc.py", kind="poc", sha256="a" * 64)
    art2 = bb.add_artifact(pid, "poc/poc2.py", kind="poc", sha256="b" * 64)
    r1 = bb.add_finding(pid, "sqli", "登录框注入", severity="high", confidence=0.6)
    r2 = bb.add_finding(pid, "sqli", "登录框注入", severity="high", confidence=0.8,
                        poc_artifact_id=art)
    assert r2["merged"]
    row = [f for f in bb.list_findings(pid) if f["id"] == r1["id"]][0]
    assert row["poc_artifact_id"] == art
    # 旧行已有引用时合并不覆盖
    r3 = bb.add_finding(pid, "sqli", "登录框注入", confidence=0.9, poc_artifact_id=art2)
    assert r3["merged"]
    row = [f for f in bb.list_findings(pid) if f["id"] == r1["id"]][0]
    assert row["poc_artifact_id"] == art


def test_owner_tags_distinct(bb, project):
    """meta.owner 去重清单：开窗注入 owner 规则的数据源（DESIGN.md §4 AI 自动打标）。"""
    pid = project["id"]
    assert bb.owner_tags(pid) == []  # 无打标返回空
    bb.upsert_asset(pid, "host", "10.0.0.1", meta={"owner": "edusrc"}, author="t")
    bb.upsert_asset(pid, "host", "10.0.0.2", meta={"owner": "osrc"}, author="t")
    bb.upsert_asset(pid, "host", "10.0.0.3", meta={"owner": "edusrc"}, author="t")  # 重复
    bb.upsert_asset(pid, "host", "10.0.0.4", meta={}, author="t")  # 无 owner
    assert bb.owner_tags(pid) == ["edusrc", "osrc"]


# ---------- 人机共写：patch_func / patch_finding（rev 工作台） ----------

def test_patch_func_rename_note_tags_history(bb, project):
    """改名入史 / 笔记分段追加 / tags 全量替换（含 []）/ confidence 人不动。"""
    pid = project["id"]
    sha = "a" * 64
    fid = bb.upsert_func(pid, sha, 0x401000, "sub_401000",
                         analysis="AI 初判", analyzed_by="agent")["id"]
    r = bb.patch_func(pid, fid, name="check_license", note="密钥比较循环",
                      risk_tags=["crypto", "anti-debug"], author="human")
    assert r["name"] == "check_license"
    assert [h["name"] for h in r["name_history"]] == ["sub_401000", "check_license"]
    assert "AI 初判" in r["analysis"]
    assert "## 笔记" in r["analysis"] and "密钥比较循环" in r["analysis"]
    assert r["risk_tags"] == ["crypto", "anti-debug"]
    assert r["confidence"] == 0.5  # 人不动 AI 字段

    # 第二条笔记分段；UNSET 不动 tags
    r2 = bb.patch_func(pid, fid, note="第二段", author="human")
    assert r2["analysis"].count("## 笔记") == 2
    assert r2["risk_tags"] == ["crypto", "anti-debug"]

    # [] = 显式清空
    r3 = bb.patch_func(pid, fid, risk_tags=[], author="human")
    assert r3["risk_tags"] == []

    events = bb.recent_events(pid)
    kinds = [e["kind"] for e in events]
    assert kinds.count("func.updated") == 3
    # 事件 payload 地址同样出 hex 字符串（不出 int，§9 地址纪律）
    for e in events:
        if e["kind"] == "func.updated":
            assert e["payload"]["address"] == "0x401000"


def test_patch_func_missing_and_cross_project(bb, project):
    other = bb.create_project("别的研究", "research", ["binary"])
    fid = bb.upsert_func(other["id"], "b" * 64, 1, "f")["id"]
    assert bb.patch_func(project["id"], fid, note="越界") is None  # 项目隔离
    assert bb.patch_func(project["id"], "func-deadbeef", note="x") is None


def test_patch_finding_status_whitelist_and_evidence_merge(bb, project):
    pid = project["id"]
    fid = bb.add_finding(pid, "crypto", "AES 常量",
                         evidence={"category": "algorithm", "address": "0x401000"})["id"]
    with pytest.raises(ValueError):
        bb.patch_finding(pid, fid, status="hacked")  # 白名单
    r = bb.patch_finding(pid, fid, status="verified", evidence={
        "confirm_by": "dynamic", "debug_log_artifact_id": "art-x"})
    assert r["status"] == "verified"
    assert r["evidence"]["category"] == "algorithm"  # 浅层 merge 保留旧字段
    assert r["evidence"]["confirm_by"] == "dynamic"
    assert "finding.updated" in [e["kind"] for e in bb.recent_events(pid)]

    other = bb.create_project("p2", "research", ["binary"])
    assert bb.patch_finding(other["id"], fid, status="verified") is None
    assert bb.patch_finding(pid, "find-deadbeef", status="verified") is None


def test_patch_finding_human_revision_fields(bb, project):
    """F10 人工修订：title 非空/severity 五档归一/vuln_class 可空；零打扰（无撤回/无私信）。"""
    pid = project["id"]
    fid = bb.add_finding(pid, "sqli", "误写的标题", severity="low",
                         evidence={"pocs": [{"http_raw": "GET /"}]})["id"]
    # severity 归一容错 + 字段修订
    out = bb.patch_finding(pid, fid, title="  真实注入点  ", severity=" HIGH ")
    assert out["title"] == "真实注入点"
    assert out["severity"] == "high"
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "finding.updated"][-1]
    assert set(ev["payload"]["changed"]) == {"title", "severity"}
    # vuln_class 允许空串（rev 轨合法态）
    out = bb.patch_finding(pid, fid, vuln_class="")
    assert out["vuln_class"] == ""
    # 空标题拒收
    with pytest.raises(ValueError, match="title 不能为空"):
        bb.patch_finding(pid, fid, title="   ")
    with pytest.raises(ValueError, match="severity"):
        bb.patch_finding(pid, fid, severity="hacked")
    # 零打扰：纯字段编辑不触发撤回传播（状态未动）也不发 finding_update 私信
    inbox = bb.inbox_list(pid, "sess-none", unread_only=True)
    assert inbox == []


# ---------- 租约续租原语（A1：renew_lease 此前零调用，心跳接线的底层护栏） ----------

def test_renew_lease_extends_and_shields_from_expiry(bb, project):
    """续租后 lease_until 前进；过期前续租的任务不被 expire_leases 回收。"""
    tq = TaskQueue(bb)
    s1 = _session(bb, project, "S1")["id"]
    tid = tq.publish(project["id"], "被动分析")
    tq.claim(tid, s1, lease_minutes=1)
    old_lease = tq.get_task(tid)["lease_until"]
    tq.renew_lease(tid, s1, lease_minutes=30)
    assert tq.get_task(tid)["lease_until"] > old_lease  # ISO 字符串字典序即时间序
    # 人为把租约回拨到过去（模拟 TTL 将到）后再续租 → 回收扫描跳过本任务
    with bb._tx():
        bb.conn.execute("UPDATE tasks SET lease_until=? WHERE id=?",
                        ("2000-01-01T00:00:00+00:00", tid))
    tq.renew_lease(tid, s1, lease_minutes=30)
    assert tq.expire_leases() == []
    row = tq.get_task(tid)
    assert row["status"] == "claimed" and row["claimed_by"] == s1


def test_expire_leases_recycles_stale_claim_and_allows_reclaim(bb, project):
    """未续租的过期任务回到 open、清持有方，可被其他会话重新认领。"""
    tq = TaskQueue(bb)
    s1 = _session(bb, project, "S1")["id"]
    tid = tq.publish(project["id"], "被动分析")
    tq.claim(tid, s1, lease_minutes=1)
    with bb._tx():
        bb.conn.execute("UPDATE tasks SET lease_until=? WHERE id=?",
                        ("2000-01-01T00:00:00+00:00", tid))
    assert tq.expire_leases() == [tid]
    row = tq.get_task(tid)
    assert (row["status"], row["claimed_by"], row["lease_until"]) == ("open", None, None)
    s2 = _session(bb, project, "S2")["id"]
    tq.claim(tid, s2)  # 回收后他人可认领，不抛 ClaimError


def test_renew_lease_rejects_non_owner_and_finished(bb, project):
    """续租只认持有者：非 claimed_by 拒绝；done/failed 后续租拒绝。"""
    tq = TaskQueue(bb)
    s1 = _session(bb, project, "S1")["id"]
    s2 = _session(bb, project, "S2")["id"]
    tid = tq.publish(project["id"], "被动分析")
    tq.claim(tid, s1)
    with pytest.raises(ClaimError):
        tq.renew_lease(tid, s2)  # 非持有者
    tq.complete(tid, s1)
    with pytest.raises(ClaimError):
        tq.renew_lease(tid, s1)  # 已收尾


# ---------- 重复发现：证据并集语义（A2，§5.3） ----------

def test_finding_merge_evidence_list_union_and_fill_only(bb, project):
    """列表键按内容去重追加；relates_to 同 finding_id+note 不重复；其余键只补空不覆盖。"""
    pid = project["id"]
    rel = bb.add_finding(pid, "info-leak", "被关联的基线发现")["id"]  # E0：强边须真有其 finding
    bb.add_finding(pid, "sqli", "登录框注入", evidence={
        "category": "algorithm", "pocs": [{"payload": "a1"}], "requests": ["r1"]})
    r = bb.add_finding(pid, "sqli", "登录框注入", evidence={
        "category": "other",  # 已有真值，不被新报告覆盖
        "confirm_by": "dynamic",
        "pocs": [{"payload": "a1"}, {"payload": "a2"}],
        "requests": ["r1", "r2"],
        "relates_to": [
            {"finding_id": rel, "note": "同源"},
            {"finding_id": rel, "note": "同源"},  # 同边去重
            {"finding_id": rel, "note": "升级"},
        ]})
    assert r["merged"] is True
    ev = bb.get_finding(pid, r["id"])["evidence"]  # rel 基线先建，list[0] 不再是 sqli
    assert ev["category"] == "algorithm"
    assert ev["confirm_by"] == "dynamic"
    assert ev["pocs"] == [{"payload": "a1"}, {"payload": "a2"}]
    assert ev["requests"] == ["r1", "r2"]
    assert ev["relates_to"] == [
        {"finding_id": rel, "note": "同源"},
        {"finding_id": rel, "note": "升级"}]


def test_finding_merge_severity_status_confidence_never_downgrade(bb, project):
    """severity 就高不就低；status verified 不被后续 unverified 报告降级；confidence 取大。"""
    pid = project["id"]
    bb.add_finding(pid, "sqli", "登录框注入", severity="high", confidence=0.6)
    assert bb.list_findings(pid)[0]["severity"] == "high"
    bb.add_finding(pid, "sqli", "登录框注入", severity="low", confidence=0.5)
    row = bb.list_findings(pid)[0]
    assert row["severity"] == "high" and row["confidence"] == 0.6
    bb.add_finding(pid, "sqli", "登录框注入", severity="critical", confidence=0.9,
                   status="verified")
    bb.add_finding(pid, "sqli", "登录框注入", severity="info", status="unverified")
    row = bb.list_findings(pid)[0]
    assert row["severity"] == "critical" and row["status"] == "verified"
    assert row["confidence"] == 0.9


def test_finding_merge_notes_segmented_append(bb, project):
    """notes 多来源分段追加，前人笔记不被覆盖。"""
    pid = project["id"]
    bb.add_finding(pid, "sqli", "登录框注入", evidence={"notes": "初报：报错页见 SQL 语法"})
    bb.add_finding(pid, "sqli", "登录框注入", evidence={"notes": "复核：time-based 可延迟"})
    notes = bb.list_findings(pid)[0]["evidence"]["notes"]
    assert "初报" in notes and "复核" in notes and notes.count("\n\n") == 1


# ---------- F11：severity 白名单 + rating_basis 判级依据 ----------

def test_schema_v11_migration_idempotent(tmp_path):
    """v11：findings 幂等补 rating_basis；重复初始化（重跑迁移）不炸不重复。"""
    from core.blackboard.schema import SCHEMA_VERSION

    db_path = tmp_path / "v11.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT NOT NULL,
            track TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '[]',
            config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
        CREATE TABLE findings (
            id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id),
            target_asset_id TEXT, vuln_class TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'info',
            status TEXT NOT NULL DEFAULT 'unverified', evidence TEXT NOT NULL DEFAULT '{}',
            poc_artifact_id TEXT, confidence REAL NOT NULL DEFAULT 0.5,
            dedup_key TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """
    )
    raw.commit()
    raw.close()
    for _ in range(2):  # 幂等：连续两次初始化迁移
        board = Blackboard(str(db_path))
        ver = board.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert int(ver) == SCHEMA_VERSION == 24
        cols = {r[1] for r in board.conn.execute("PRAGMA table_info(findings)")}
        assert "rating_basis" in cols
        assert "category" in cols  # v12（发现分两类）
        assert "revision" in cols  # v16（H2 乐观锁）
        assert "role" in {r[1] for r in board.conn.execute("PRAGMA table_info(tasks)")}  # v14
        assert "intents" in {r[0] for r in board.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}  # v22
        board.close()


def test_add_finding_severity_whitelist_and_normalize(bb, project):
    """F11 白名单硬化：add 入口拒未知值（此前只拦 patch）；strip+lower 归一容错。"""
    pid = project["id"]
    with pytest.raises(ValueError, match="非法 severity"):
        bb.add_finding(pid, "sqli", "乱级", severity="P1")
    r = bb.add_finding(pid, "sqli", "归一容错", severity="  HIGH  ")
    assert r["severity"] == "high"


def test_finding_merge_rating_basis_semantics(bb, project):
    """F11 合并语义表：basis 随 severity 就高覆盖；新报更高且 basis 空=清空；不高于旧级保留旧值。"""
    pid = project["id"]
    r = bb.add_finding(pid, "sqli", "登录框注入", severity="high", rating_basis="rating:edu-rating 高危#2")
    assert r["rating_basis"] == "rating:edu-rating 高危#2" and r["merged"] is False
    # 新报 critical 更高 → severity 与 basis 都取新报
    r = bb.add_finding(pid, "sqli", "登录框注入", severity="critical",
                       rating_basis="rating:edu-rating 严重#1 拖库", status="verified")
    assert r["merged"] and r["severity"] == "critical"
    assert r["rating_basis"] == "rating:edu-rating 严重#1 拖库"
    # 再报 medium（不高于旧级）→ 均保留旧值
    r = bb.add_finding(pid, "sqli", "登录框注入", severity="medium", rating_basis="rating:osrc 中危#3")
    assert r["severity"] == "critical"
    assert r["rating_basis"] == "rating:edu-rating 严重#1 拖库"
    # 新报 critical 同级（不高于）且 basis 空 → 保留旧 basis（同级不清）
    r = bb.add_finding(pid, "sqli", "登录框注入", severity="critical", rating_basis="")
    assert r["rating_basis"] == "rating:edu-rating 严重#1 拖库"


def test_finding_merge_higher_with_empty_basis_clears(bb, project):
    """升级但未给依据：basis 必须证成当前 severity → 空覆盖清空。"""
    pid = project["id"]
    bb.add_finding(pid, "xss", "存储 XSS", severity="low", rating_basis="rating:osrc 低危#1")
    r = bb.add_finding(pid, "xss", "存储 XSS", severity="high", rating_basis="")
    assert r["rating_basis"] == "" and r["severity"] == "high"
    assert bb.list_findings(pid)[0]["rating_basis"] == ""


def test_finding_rating_basis_roundtrip_and_events(bb, project):
    """rating_basis list/get/patch 往返 + finding.new/updated payload/changed 携带。"""
    pid = project["id"]
    r = bb.add_finding(pid, "sqli", "注入", severity="high", rating_basis="rating:edu-rating 高危#2")
    assert bb.get_finding(pid, r["id"])["rating_basis"] == "rating:edu-rating 高危#2"
    new_ev = [e for e in bb.recent_events(pid) if e["kind"] == "finding.new"][-1]
    assert new_ev["payload"]["rating_basis"] == "rating:edu-rating 高危#2"
    assert new_ev["payload"]["severity"] == "high"
    # live-stream-ux C1：payload 带 title/target_asset_id/status（事件行人话渲染）
    assert new_ev["payload"]["title"] == "注入"
    assert new_ev["payload"]["target_asset_id"] is None
    assert new_ev["payload"]["status"] == "unverified"
    # 人工 PATCH 改依据 + 事件 changed 携带
    bb.patch_finding(pid, r["id"], rating_basis="人工复核：改判 rating:osrc 高危#5", author="human")
    assert bb.get_finding(pid, r["id"])["rating_basis"] == "人工复核：改判 rating:osrc 高危#5"
    upd_ev = [e for e in bb.recent_events(pid) if e["kind"] == "finding.updated"][-1]
    assert "rating_basis" in upd_ev["payload"]["changed"]
    # patch 不传=不动；空串=清空
    bb.patch_finding(pid, r["id"], title="注入2")
    assert bb.get_finding(pid, r["id"])["rating_basis"] == "人工复核：改判 rating:osrc 高危#5"
    bb.patch_finding(pid, r["id"], rating_basis="")
    assert bb.get_finding(pid, r["id"])["rating_basis"] == ""


# ---------- A2：并发 read-modify-write 不丢更新（BEGIN IMMEDIATE 临界区） ----------

def _run_threads(n, fn):
    """起 n 线程各跑 fn(i)，透传线程内异常。"""
    errors: list[Exception] = []

    def worker(i):
        try:
            fn(i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发线程内异常: {errors!r}"


def test_concurrent_patch_func_notes_no_lost_segments(bb, project):
    """8 会话并发给同一函数追加笔记 → 8 段全留（旧实现锁外读→锁内写会丢段）。"""
    pid = project["id"]
    fid = bb.upsert_func(pid, "a" * 64, 0x401000, "sub_401000",
                         analysis="AI 初判", analyzed_by="agent")["id"]

    def add_note(i):
        bb.patch_func(pid, fid, note=f"第{i}段笔记内容", author=f"human-{i}")

    _run_threads(8, add_note)
    row = bb.get_func(pid, fid)
    assert row["analysis"].count("## 笔记") == 8
    for i in range(8):
        assert f"第{i}段笔记内容" in row["analysis"]


def test_concurrent_add_finding_union_no_lost_evidence(bb, project):
    """8 会话并发上报同一漏洞、各带不同 pocs/requests/relates_to → 最终并集无丢失。"""
    pid = project["id"]
    first = bb.add_finding(pid, "sqli", "登录框注入", severity="high")
    # E0：被引强边必须是本项目真实 finding（各用独立 vuln_class，避免与 sqli 互相合并）
    rel_ids = [bb.add_finding(pid, f"rel-class-{i}", f"关联基线 {i}")["id"]
               for i in range(8)]

    def report(i):
        bb.add_finding(pid, "sqli", "登录框注入", evidence={
            "pocs": [{"payload": f"payload-{i}"}],
            "requests": [f"GET /?id={i}"],
            "relates_to": [{"finding_id": rel_ids[i], "note": f"边-{i}"}]})

    _run_threads(8, report)
    assert len(bb.list_findings(pid)) == 9  # 1 主漏洞 + 8 个被引基线
    ev = bb.get_finding(pid, first["id"])["evidence"]
    assert len(ev["pocs"]) == 8 and len(ev["requests"]) == 8 and len(ev["relates_to"]) == 8
    assert {p["payload"] for p in ev["pocs"]} == {f"payload-{i}" for i in range(8)}
    assert {r for r in ev["requests"]} == {f"GET /?id={i}" for i in range(8)}


def test_concurrent_asset_meta_and_finding_patch_no_lost_keys(bb, project):
    """并发 PATCH 资产 meta（深合并）与 finding evidence（浅层 merge）：各键全留。"""
    pid = project["id"]
    aid = bb.upsert_asset(pid, "host", "10.0.0.1")["id"]
    fid = bb.add_finding(pid, "xss", "搜索框反射", evidence={"base": "v0"})["id"]

    def patch_both(i):
        bb.update_asset_meta(aid, {f"meta-{i}": i})
        bb.patch_finding(pid, fid, evidence={f"ev-{i}": i})

    _run_threads(8, patch_both)
    asset = bb.get_asset(aid)
    assert asset["meta"] == {f"meta-{i}": i for i in range(8)}
    ev = bb.get_finding(pid, fid)["evidence"]
    assert ev["base"] == "v0"
    assert all(ev[f"ev-{i}"] == i for i in range(8))


# ---------- 删除项目：close_all 关闭闸门（防惰性重连锁死 db） ----------

def test_close_session_clears_runtime_meta(bb, project):
    """关窗清运行期标记（2026-09-24）：worker_armed/close_pending 残留不进 closed
    态；其余 meta 原样保留；无运行期标记的窗走普通 UPDATE 路径。"""
    import json
    pid = project["id"]
    s1 = bb.register_session(pid, "窗一")
    bb.set_session_meta(s1["id"], {"worker_armed": True, "bound_task_id": "task-x"})
    bb.close_session(s1["id"])
    m1 = json.loads(bb.get_session(s1["id"])["meta"])
    assert "worker_armed" not in m1 and m1.get("bound_task_id") == "task-x"
    s2 = bb.register_session(pid, "窗二")
    bb.set_session_meta(s2["id"], {"close_pending": True})
    bb.close_session(s2["id"])
    assert "close_pending" not in json.loads(bb.get_session(s2["id"])["meta"])
    s3 = bb.register_session(pid, "窗三")
    bb.set_session_meta(s3["id"], {"note": "留着"})
    bb.close_session(s3["id"])
    assert json.loads(bb.get_session(s3["id"])["meta"]).get("note") == "留着"


def test_close_all_rejects_reconnect(bb):
    """close_all 后访问 .conn 必须抛 BlackboardClosedError，不再惰性重连。

    旧实现下仍持引用的 WS/轮询线程会在 close_all 后毫秒级重开 sqlite 连接，
    Windows 上重新锁死文件导致删项目 rename 必败（WinError 5）。"""
    from core.blackboard.store import BlackboardClosedError
    assert bb.conn is bb.conn  # 触发本线程惰性连接
    bb.close_all()
    with pytest.raises(BlackboardClosedError):
        _ = bb.conn
    # 写路径同样被挡（_tx 经 conn property）
    with pytest.raises(BlackboardClosedError):
        with bb._tx():
            pass

# ---------- finding/asset 物理删除（走查/垃圾数据清理，2026-09-15） ----------

def test_delete_finding_cascades_and_audits(bb, project):
    """删 finding：反向 relates_to 边摘除、任务 context_refs/stale_refs 摘引用、
    链边保留（孤儿占位）、私信保留审计、落 finding.deleted 事件；不触发撤回传播。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    base = bb.add_finding(pid, "sqli", "上游注入", severity="high")
    # 下游 finding 以强边引用上游（含一条历史悬空边，也应一并清掉）
    down = bb.add_finding(pid, "rce", "下游 getshell", severity="critical",
                          evidence={"relates_to": [
                              {"finding_id": base["id"], "note": "以前置注入为前提"}]})
    # 再注入一条 E0 校验上线前可能存在的历史悬空边（绕过校验直接落库模拟）
    import json as _json
    ev = bb.get_finding(pid, down["id"])["evidence"]
    ev["relates_to"].append({"finding_id": "find-deadbeefcafe", "note": "历史悬空"})
    with bb._tx():
        bb.conn.execute(
            "UPDATE findings SET evidence=? WHERE id=?",
            (_json.dumps(ev, ensure_ascii=False), down["id"]))
    # 任务依据 + 撤回挂标
    tid = tq.publish(pid, f"验证 {base['id']}", refs=[base["id"]])
    assert tq.add_stale_ref(tid, base["id"]) is True
    # 链边挂上游（删 finding 不级联链，链详情走孤儿占位）
    cid = bb.create_chain(pid, "演示链")
    bb.add_chain_link(pid, cid, "finding", base["id"], edge_note="入链")
    # 撤回私信（删 finding 保留行：payload 自包含，同关窗私信留审计）
    sess = bb.register_session(pid, "recon")["id"]
    bb.inbox_post(pid, sess, "basis_stale", base["id"], {"title": "上游注入"})

    out = bb.delete_finding(pid, base["id"], author="human")
    assert out["id"] == base["id"] and out["trimmed_relates_to"] == 1
    assert out["affected_tasks"] == [tid]
    assert bb.get_finding(pid, base["id"]) is None
    # 下游边被摘：只剩悬空边
    rels = bb.get_finding(pid, down["id"])["evidence"]["relates_to"]
    assert [r["finding_id"] for r in rels] == ["find-deadbeefcafe"]
    # 任务行保留但引用清空
    t = tq.get_task(tid)
    assert t["context_refs"] == [] and t["stale_refs"] == []
    # 链边行仍在（孤儿）
    detail = bb.get_chain(cid)
    assert [l["node_id"] for l in detail["links"]] == [base["id"]]
    # 私信行保留
    assert len(bb.inbox_list(pid, sess)) == 1
    # 审计事件；且删除不是撤回（无 retracted/message.inbox 新事件）
    kinds = [e["kind"] for e in bb.recent_events(pid, 0, limit=200)]
    assert kinds.count("finding.deleted") == 1
    assert "finding.retracted" not in kinds[kinds.index("finding.deleted"):]
    assert "message.inbox" not in kinds[kinds.index("finding.deleted"):]


def test_delete_finding_missing_returns_none(bb, project):
    assert bb.delete_finding(project["id"], "find-000000000000") is None


def test_delete_asset_guards(bb, project):
    """叶子资产可删；被 finding 引用/有子资产 → ValueError；不存在 → LookupError。"""
    pid = project["id"]
    host = bb.upsert_asset(pid, "host", "10.0.0.9")["id"]
    leaf_free = bb.upsert_asset(pid, "url", "http://10.0.0.9/a")["id"]
    leaf_used = bb.upsert_asset(pid, "url", "http://10.0.0.9/b", parent_id=host)["id"]
    bb.upsert_asset(pid, "url", "http://10.0.0.9/c", parent_id=host)  # host 的另一个子
    bb.add_finding(pid, "xss", "XSS", target_asset_id=leaf_used)

    with pytest.raises(ValueError, match="引用"):
        bb.delete_asset(leaf_used)               # 被 finding 引用
    with pytest.raises(ValueError, match="子资产"):
        bb.delete_asset(host)                    # 有子节点
    assert bb.delete_asset(leaf_free)["id"] == leaf_free
    assert bb.get_asset(leaf_free) is None
    with pytest.raises(LookupError):
        bb.delete_asset("asset-000000000000")
    # 审计事件
    kinds = [e["kind"] for e in bb.recent_events(pid, 0, limit=50)]
    assert "asset.deleted" in kinds

# ---------- A3：任务流图组装（parent 实线 / 私信虚线，set-based 无 N+1） ----------

def test_task_graph_parent_and_inbox_edges(bb, project):
    from core.blackboard.graph import task_graph
    pid = project["id"]
    tq = TaskQueue(bb)
    s1 = _session(bb, project, "worker1")
    s2 = _session(bb, project, "worker2")
    parent = tq.publish(pid, "父任务", task_type="generic")
    t1 = tq.publish(pid, "子一", task_type="generic", parent_id=parent, created_by=s1["id"])
    t2 = tq.publish(pid, "子二", task_type="generic")
    tq.claim(t1, s1["id"])
    tq.claim(t2, s2["id"])
    f = bb.add_finding(pid, "xss", "反射 XSS")["id"]

    # 同依据以两种私信触达两个在跑会话 → 同一任务对，多簇合并为一条虚线
    assert bb.inbox_post(pid, s1["id"], "basis_stale", f, {"title": "反射 XSS"})
    assert bb.inbox_post(pid, s2["id"], "basis_stale", f, {"title": "反射 XSS"})
    assert bb.inbox_post(pid, s1["id"], "finding_update", f, {"title": "反射 XSS"})
    assert bb.inbox_post(pid, s2["id"], "finding_update", f, {"title": "反射 XSS"})

    g = task_graph(bb, pid)
    edge_pairs = {(e["kind"], e["source"], e["target"]) for e in g["edges"]}
    assert ("parent", parent, t1) in edge_pairs
    inbox = [e for e in g["edges"] if e["kind"] == "inbox"]
    assert len(inbox) == 1 and {inbox[0]["source"], inbox[0]["target"]} == {t1, t2}
    kinds = {r["kind"] for r in inbox[0]["refs"]}
    assert kinds == {"basis_stale", "finding_update"}
    assert inbox[0]["refs"][0]["title"] == "反射 XSS"  # findings 表标题优先

    # 节点带会话信息（含 role，前端显示层映射中文名）与计划
    tq.set_plan(t1, s1["id"], [{"title": "a"}, {"title": "b"}])
    n1 = next(n for n in task_graph(bb, pid)["nodes"] if n["id"] == t1)
    assert n1["session"] == {"id": s1["id"], "name": "worker1", "role": "_generalist",
                             "status": "idle"}
    assert len(n1["plan"]) == 2
    # 完成的任务节点保留（节点持久），实线不断
    tq.complete(t2, s2["id"], "完")
    g2 = task_graph(bb, pid)
    assert {n["id"] for n in g2["nodes"]} == {parent, t1, t2}
    assert ("parent", parent, t1) in {(e["kind"], e["source"], e["target"]) for e in g2["edges"]}


def test_task_graph_closed_session_not_mapped(bb, project):
    from core.blackboard.graph import task_graph
    pid = project["id"]
    tq = TaskQueue(bb)
    s1 = _session(bb, project, "alive")
    s3 = _session(bb, project, "closed-one")
    t1 = tq.publish(pid, "活任务", task_type="generic")
    t3 = tq.publish(pid, "旧任务", task_type="generic")
    tq.claim(t1, s1["id"])
    tq.claim(t3, s3["id"])
    bb.close_session(s3["id"])
    f = bb.add_finding(pid, "sqli", "SQLi")["id"]
    bb.inbox_post(pid, s1["id"], "basis_stale", f, {"title": "SQLi"})
    bb.inbox_post(pid, s3["id"], "basis_stale", f, {"title": "SQLi"})

    edges = [e for e in task_graph(bb, pid)["edges"] if e["kind"] == "inbox"]
    assert edges == []  # closed 会话不映射 → 簇内不足两个任务，不出虚线


def test_task_graph_query_count_bounded(bb, project):
    """任务/私信量增长时组装查询条数固定（无 N+1）。"""
    from core.blackboard.graph import task_graph
    import sqlite3
    pid = project["id"]
    tq = TaskQueue(bb)
    f = bb.add_finding(pid, "xss", "XSS")["id"]

    def add_worker(i: int) -> None:
        s = _session(bb, project, f"w{i}")
        t = tq.publish(pid, f"任务{i}", task_type="generic")
        tq.claim(t, s["id"])
        bb.inbox_post(pid, s["id"], "finding_update", f, {"title": "XSS"})

    def count_queries() -> int:
        stmts: list[str] = []
        bb.conn.set_trace_callback(lambda sql: stmts.append(sql))
        try:
            task_graph(bb, pid)
        finally:
            bb.conn.set_trace_callback(None)
        return len([s for s in stmts if not s.startswith(("BEGIN", "COMMIT"))])

    add_worker(0)
    n1 = count_queries()                     # tasks / sessions / 窗口映射 / inbox / findings = 5
    for i in range(1, 60):
        add_worker(i)
    n2 = count_queries()
    assert n1 <= 6 and n2 == n1  # 查询条数不随任务量增长（无 N+1）
    g = task_graph(bb, pid)
    assert len(g["nodes"]) == 60
    # 60 个会话同簇 → 两两连边 C(60,2)=1770，边在合理规模内且不出自连
    inbox_edges = [e for e in g["edges"] if e["kind"] == "inbox"]
    assert len(inbox_edges) == 60 * 59 // 2
    assert all(e["source"] != e["target"] for e in inbox_edges)


# ---------- 黑板链路图（board_graph：五类对象 × 类型分层 DAG，只读组装无 N+1） ----------

def test_board_graph_nodes_and_edges(bb, project):
    from core.blackboard.graph import board_graph
    pid = project["id"]
    tq = TaskQueue(bb)
    bin_id = bb.upsert_asset(pid, "binary", "a" * 64)["id"]
    url_id = bb.upsert_asset(pid, "url", "http://10.0.0.9/", parent_id=bin_id)["id"]
    fid = bb.upsert_func(pid, "a" * 64, 0x401000, "vuln_check", risk_tags=["danger"])["id"]
    poc = bb.add_artifact(pid, "ws/poc.py", kind="script")
    f = bb.add_finding(pid, "sqli", "注入", target_asset_id=url_id,
                       poc_artifact_id=poc)["id"]
    f2 = bb.add_finding(pid, "sqli", "注入下游",
                        evidence={"relates_to": [{"finding_id": f, "note": "同源"}]})["id"]
    t = tq.publish(pid, "验证注入", task_type="generic", refs=[f])
    orphan = bb.add_artifact(pid, "ws/log.txt", meta={"task_id": t})

    g = board_graph(bb, pid)
    by_id = {n["id"]: n for n in g["nodes"]}
    assert set(by_id) == {bin_id, url_id, fid, poc, f, f2, orphan, t}
    assert {n["node_type"] for n in g["nodes"]} == {
        "asset", "func_kb", "finding", "artifact", "task"}
    # 节点公共字段齐全（label/sub 服务端拼好）
    for n in g["nodes"]:
        assert n["label"] and set(n) >= {"id", "node_type", "label", "sub", "status"}
    # 五类节点的形态抽查
    assert by_id[bin_id]["label"].startswith("binary:") and by_id[bin_id]["sub"] == "binary"
    assert by_id[fid]["label"] == "vuln_check" and by_id[fid]["binary_sha256"] == "a" * 64
    assert by_id[f]["severity"] == "high" or by_id[f]["sub"].count("/") == 1
    assert by_id[poc]["label"] == "ws/poc.py" and by_id[poc]["task_id"] is None
    assert by_id[t]["node_type"] == "task" and by_id[t]["objective"] == "验证注入"

    pairs = {(e["kind"], e["source"], e["target"]) for e in g["edges"]}
    assert ("asset_parent", bin_id, url_id) in pairs
    assert ("func_of", fid, bin_id) in pairs          # sha 匹配连到 binary 资产
    assert ("targets", f, url_id) in pairs
    assert ("relates_to", f2, f) in pairs             # note 作边 label
    assert ("poc", f, poc) in pairs
    assert ("basis", t, f) in pairs
    assert ("artifact_task", orphan, t) in pairs      # 孤儿产物 → 归属任务


def test_board_graph_finding_node_carries_author(bb, project):
    """P4（2026-09-20 对话化）：finding 节点透传 author——sess- 前缀即对话轮产出
    （前端据此标「对话产出」徽章），human/任务轮作者同样透传。"""
    from core.blackboard.graph import board_graph
    pid = project["id"]
    chat_f = bb.add_finding(pid, "info-leak", "对话轮登记的结论",
                            severity="medium", author="sess-abc123def456")["id"]
    human_f = bb.add_finding(pid, "sqli", "人类登记", author="human")["id"]
    g = board_graph(bb, pid)
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id[chat_f]["author"] == "sess-abc123def456"
    assert by_id[human_f]["author"] == "human"


def test_board_graph_orphan_artifact_only(bb, project):
    """artifact_task 只收孤儿产物：被 findings.poc_artifact_id 指回的不再连任务（防冗余三角）。"""
    from core.blackboard.graph import board_graph
    pid = project["id"]
    tq = TaskQueue(bb)
    t = tq.publish(pid, "做 POC", task_type="generic")
    poc = bb.add_artifact(pid, "ws/poc.py", meta={"task_id": t})
    bb.add_finding(pid, "sqli", "注入", poc_artifact_id=poc)
    g = board_graph(bb, pid)
    arts = [e for e in g["edges"] if e["kind"] == "artifact_task"]
    assert arts == []  # 有 poc 边指回 → 不再出 artifact_task 边


def test_board_graph_stale_basis_kept_and_marked(bb, project):
    """依据被撤回 → basis 边不丢，带 stale:true（全景审计视图：收录+标记不排除）。"""
    from core.blackboard.graph import board_graph
    pid = project["id"]
    tq = TaskQueue(bb)
    f = bb.add_finding(pid, "sqli", "注入")["id"]
    t = tq.publish(pid, "验证", task_type="generic", refs=[f])
    assert tq.add_stale_ref(t, f) is True
    edges = [e for e in board_graph(bb, pid)["edges"] if e["kind"] == "basis"]
    assert [(e["source"], e["target"]) for e in edges] == [(t, f)]
    assert edges[0]["stale"] is True


def test_board_graph_chain_edges(bb, project):
    """链边按 seq 相邻成对（带链名/note）；端点实体删除后该段消失（行不删、图不画）。"""
    from core.blackboard.graph import board_graph
    pid = project["id"]
    f1 = bb.add_finding(pid, "sqli", "注入点")["id"]
    f2 = bb.add_finding(pid, "lfi", "文件包含")["id"]
    art = bb.add_artifact(pid, "ws/shell.py")  # 链节点白名单 NODE_TABLES：finding/func_kb/artifact
    cid = bb.create_chain(pid, "内网链")
    bb.add_chain_link(pid, cid, "finding", f1, edge_note="入口")
    bb.add_chain_link(pid, cid, "finding", f2, edge_note="跳板")
    bb.add_chain_link(pid, cid, "artifact", art, edge_note="落点")

    edges = [e for e in board_graph(bb, pid)["edges"] if e["kind"] == "chain"]
    assert [(e["source"], e["target"]) for e in edges] == [(f1, f2), (f2, art)]
    assert all(e["chain_id"] == cid and e["chain_name"] == "内网链" for e in edges)
    assert [e["edge_note"] for e in edges] == ["入口", "跳板"]
    # 删中段实体：两段都消失（f1→f2 端点没了，f2→a 端点没了）
    bb.delete_finding(pid, f2, author="human")
    edges2 = [e for e in board_graph(bb, pid)["edges"] if e["kind"] == "chain"]
    assert edges2 == []


def test_board_graph_ids_unique_and_query_count_bounded(bb, project):
    """节点/边 id 全图唯一（防御）；数据量增长时组装查询条数固定（无 N+1）。"""
    from core.blackboard.graph import board_graph
    pid = project["id"]
    tq = TaskQueue(bb)
    f = bb.add_finding(pid, "xss", "XSS")["id"]

    def count_queries() -> int:
        stmts: list[str] = []
        bb.conn.set_trace_callback(lambda sql: stmts.append(sql))
        try:
            board_graph(bb, pid)
        finally:
            bb.conn.set_trace_callback(None)
        return len([s for s in stmts if not s.startswith(("BEGIN", "COMMIT"))])

    tq.publish(pid, "任务0", task_type="generic", refs=[f])
    n1 = count_queries()
    for i in range(1, 40):
        fid = bb.add_finding(pid, "xss", f"XSS{i}")["id"]
        tq.publish(pid, f"任务{i}", task_type="generic", refs=[fid])
    n2 = count_queries()
    assert n1 <= 6 and n2 == n1  # ≤6 条固定查询，不随对象量增长
    g = board_graph(bb, pid)
    node_ids = [n["id"] for n in g["nodes"]]
    edge_ids = [e["id"] for e in g["edges"]]
    assert len(node_ids) == len(set(node_ids))
    assert len(edge_ids) == len(set(edge_ids))
    # 边两端都在节点集（悬挂引用统一出口）
    ids = set(node_ids)
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])


# ---------- E8：会话 meta 合并 + 人工引导私信 ----------

def test_set_session_meta_merges(bb, project):
    s = _session(bb, project, "meta会话")
    bb.set_session_meta(s["id"], {"resume_snapshot": "a.json"})
    got = bb.set_session_meta(s["id"], {"other": 1})
    meta = got["meta"]
    assert meta["resume_snapshot"] == "a.json" and meta["other"] == 1
    # None 值 = 语义清除（键留存）
    bb.set_session_meta(s["id"], {"resume_snapshot": None})
    meta = bb.get_session(s["id"])
    import json as _json
    assert _json.loads(meta["meta"])["resume_snapshot"] is None


def test_set_session_meta_missing_session(bb):
    with pytest.raises(ValueError):
        bb.set_session_meta("sess-nope", {"x": 1})


def test_post_human_note_delivers_and_audits(bb, project):
    s = _session(bb, project, "引导会话")
    r1 = bb.post_human_note(project["id"], s["id"], "先看 80 端口")
    r2 = bb.post_human_note(project["id"], s["id"], "再试弱口令")
    assert r1 and r2 and r1["id"] != r2["id"]      # 每条都是新 note，不去重
    rows = bb.inbox_list(project["id"], s["id"])
    assert [r["kind"] for r in rows] == ["human_note", "human_note"]
    assert [r["payload"]["text"] for r in rows] == ["先看 80 端口", "再试弱口令"]
    evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "message.inbox"]
    assert len(evs) == 2
    assert all(e["author"] == "human" and e["payload"]["kind"] == "human_note"
               for e in evs)
    assert evs[0]["payload"]["title"] == "先看 80 端口"


def test_post_human_note_rejects_missing_and_closed(bb, project):
    with pytest.raises(ValueError):
        bb.post_human_note(project["id"], "sess-nope", "x")
    s = _session(bb, project, "将关窗")
    bb.close_session(s["id"])
    assert bb.post_human_note(project["id"], s["id"], "晚了") is None


def test_inbox_drain_exclude_kinds(bb, project):
    """exclude_kinds（2026-09-19 轮末语义）：指定 kind 滞留收件箱不被取走——
    步边界 drain 排除 human_note，引导延迟到认领期全量 drain 注入。"""
    s = _session(bb, project, "排空演练")
    sid = s["id"]
    pid = project["id"]
    bb.post_human_note(pid, sid, "等下一轮")
    bb.inbox_post(pid, sid, "basis_stale", "fid-1", {"title": "撤回"})
    drained = bb.inbox_drain(pid, sid, exclude_kinds=("human_note",))
    assert [r["kind"] for r in drained] == ["basis_stale"]
    assert [r["kind"] for r in bb.inbox_list(pid, sid, unread_only=True)] == ["human_note"]
    # 全量 drain 补走滞留引导
    rest = bb.inbox_drain(pid, sid)
    assert [r["kind"] for r in rest] == ["human_note"]


def test_inbox_drain_only_kinds(bb, project):
    """only_kinds（2026-09-19 空闲对话轮）：只取指定 kind——对话轮 drain 只取
    human_note（basis_stale/finding_update 留给任务认领注入）；与 exclude_kinds
    互斥。"""
    s = _session(bb, project, "对话轮排空")
    sid = s["id"]
    pid = project["id"]
    bb.post_human_note(pid, sid, "你好")
    bb.inbox_post(pid, sid, "basis_stale", "fid-2", {"title": "撤回2"})
    drained = bb.inbox_drain(pid, sid, only_kinds=("human_note",))
    assert [r["kind"] for r in drained] == ["human_note"]
    assert [r["kind"] for r in bb.inbox_list(pid, sid, unread_only=True)] == ["basis_stale"]
    # 互斥：两参同传 ValueError
    with pytest.raises(ValueError):
        bb.inbox_drain(pid, sid, exclude_kinds=("human_note",),
                       only_kinds=("human_note",))


# ---------- 会话窗对话化（2026-09-20）：agent_message 私信 / task_receipt 回执 ----------

def test_post_agent_message_delivers_and_audits(bb, project):
    """post_agent_message（P3 B1）：定向送达 + payload 全文（含 from）+
    message.inbox 审计（author=from_session）；ref_id=唯一 msg id 连发不去重。"""
    s_from = _session(bb, project, "发信窗")
    s_to = _session(bb, project, "收信窗")
    pid = project["id"]
    r = bb.post_agent_message(pid, s_from["id"], s_to["id"], "intel",
                              "8080 端口有 swagger 未鉴权",
                              refs=["find-0123456789ab"])
    assert r and r["subkind"] == "intel" and r["from"] == s_from["id"]
    assert r["id"].startswith("msg-")
    rows = bb.inbox_list(pid, s_to["id"])
    assert [x["kind"] for x in rows] == ["agent_message"]
    assert rows[0]["payload"]["text"] == "8080 端口有 swagger 未鉴权"
    assert rows[0]["payload"]["from"] == s_from["id"]
    assert rows[0]["payload"]["refs"] == ["find-0123456789ab"]
    evs = [e for e in bb.recent_events(pid) if e["kind"] == "message.inbox"]
    assert len(evs) == 1
    assert evs[0]["author"] == s_from["id"]
    assert evs[0]["payload"]["kind"] == "agent_message"
    r2 = bb.post_agent_message(pid, s_from["id"], s_to["id"], "assist", "要一份 nmap 结果")
    assert r2 and r2["id"] != r["id"]
    assert len(bb.inbox_list(pid, s_to["id"], unread_only=True)) == 2


def test_post_agent_message_guards_and_truncation(bb, project):
    """未知分类/from·to 不存在抛 ValueError；to 已关闭返 None；text 截 4000。"""
    s_from = _session(bb, project, "校验窗")
    s_to = _session(bb, project, "接收窗")
    pid = project["id"]
    with pytest.raises(ValueError):
        bb.post_agent_message(pid, s_from["id"], s_to["id"], "gossip", "x")
    with pytest.raises(ValueError):
        bb.post_agent_message(pid, "sess-nope", s_to["id"], "intel", "x")
    with pytest.raises(ValueError):
        bb.post_agent_message(pid, s_from["id"], "sess-nope", "intel", "x")
    bb.close_session(s_to["id"])
    assert bb.post_agent_message(pid, s_from["id"], s_to["id"], "intel", "晚了") is None
    s2 = _session(bb, project, "截断窗")
    r = bb.post_agent_message(pid, s_from["id"], s2["id"], "handoff", "长" * 5000)
    assert r and len(r["text"]) == 4000
    rows = bb.inbox_list(pid, s2["id"])
    assert len(rows[0]["payload"]["text"]) == 4000


def test_list_active_sessions_by_role_excludes_closed(bb, project):
    """广播送达名单：status!='closed'；role 空串返空（防全量误广播）。"""
    s1 = _session(bb, project, "广播A")
    s2 = _session(bb, project, "广播B")
    bb.close_session(s2["id"])
    ids = [r["id"] for r in bb.list_active_sessions_by_role(project["id"], "_generalist")]
    assert s1["id"] in ids and s2["id"] not in ids
    assert bb.list_active_sessions_by_role(project["id"], "") == []
    assert bb.list_active_sessions_by_role(project["id"], "no-such-role") == []


def test_task_receipt_on_complete_and_fail(bb, project):
    """子任务回执（P3）：父窗已认领 → 子任务 done/failed 收尾自动投 task_receipt
    到父窗收件箱，findings 带本会话登记，failed 带 blocked_reason。"""
    pid = project["id"]
    parent_s = _session(bb, project, "父窗")
    child_s = _session(bb, project, "子窗")
    tq = TaskQueue(bb)
    parent = tq.publish(pid, "父任务：打 target.com", task_type="generic",
                        created_by="orchestrator")
    tq.claim(parent, parent_s["id"])
    child = tq.publish(pid, "子任务：子域枚举", task_type="generic",
                       parent_id=parent, created_by=parent_s["id"])
    tq.claim(child, child_s["id"])
    f1 = bb.add_finding(pid, "info-leak", "悬空 CNAME 可接管",
                        severity="high", author=child_s["id"])["id"]
    tq.complete(child, child_s["id"], "枚举完成，接管成立")
    rcpts = [r for r in bb.inbox_list(pid, parent_s["id"]) if r["kind"] == "task_receipt"]
    assert len(rcpts) == 1
    p = rcpts[0]["payload"]
    assert p["task_id"] == child and p["status"] == "done"
    assert p["objective"] == "子任务：子域枚举"
    assert p["result_note"] == "枚举完成，接管成立"
    assert [f["id"] for f in p["findings"]] == [f1]
    evs = [e for e in bb.recent_events(pid) if e["kind"] == "message.inbox"
           and e["payload"]["kind"] == "task_receipt"]
    assert len(evs) == 1 and evs[0]["author"] == child_s["id"]
    # fail 路径：blocked_reason 随回执
    child2 = tq.publish(pid, "子任务：爆破", task_type="generic",
                        parent_id=parent, created_by=parent_s["id"])
    tq.claim(child2, child_s["id"])
    tq.fail(child2, child_s["id"], "口令字典耗尽", blocked_reason="error")
    rcpts2 = [r for r in bb.inbox_list(pid, parent_s["id"]) if r["kind"] == "task_receipt"]
    p2 = [r for r in rcpts2 if r["payload"]["task_id"] == child2][0]["payload"]
    assert p2["status"] == "failed" and p2["blocked_reason"] == "error"


def test_task_receipt_guards_and_dedup(bb, project):
    """父任务未认领/自收（父窗=收尾窗）不投递；同状态未读期间重复收尾不重复投
    （ref_id=<task_id>:<status>，E4-③ 状态后缀——fail→reopen→complete 的成功回执
    不再被旧 ref_id 未读去重吞掉，失败与成功各投一条）。"""
    pid = project["id"]
    s1 = _session(bb, project, "回执A")
    s2 = _session(bb, project, "回执B")
    tq = TaskQueue(bb)
    # 父任务未认领 → 不投
    parent = tq.publish(pid, "无主父任务", task_type="generic")
    child = tq.publish(pid, "子任务", task_type="generic",
                       parent_id=parent, created_by="orchestrator")
    tq.claim(child, s1["id"])
    tq.complete(child, s1["id"], "完成")
    assert [r for r in bb.inbox_list(pid, s1["id"]) if r["kind"] == "task_receipt"] == []
    # 父窗自己收尾子任务 → 不自寄
    parent2 = tq.publish(pid, "自收父任务", task_type="generic")
    tq.claim(parent2, s1["id"])
    child2 = tq.publish(pid, "自己子任务", task_type="generic",
                        parent_id=parent2, created_by=s1["id"])
    tq.claim(child2, s1["id"])
    tq.complete(child2, s1["id"], "完成")
    assert [r for r in bb.inbox_list(pid, s1["id"]) if r["kind"] == "task_receipt"] == []
    # reopen 重跑：失败回执先投 1 条；重跑完成后再收尾，E4-③ 状态后缀下
    # 成功回执是独立 ref_id（child3:done）→ 父窗共 2 条（1 failed + 1 done）
    parent3 = tq.publish(pid, "重跑父任务", task_type="generic")
    tq.claim(parent3, s2["id"])
    child3 = tq.publish(pid, "重跑子任务", task_type="generic",
                        parent_id=parent3, created_by=s2["id"])
    tq.claim(child3, s1["id"])
    tq.fail(child3, s1["id"], "第一次失败")
    assert len([r for r in bb.inbox_list(pid, s2["id"]) if r["kind"] == "task_receipt"]) == 1
    tq.reopen(child3, by=s2["id"])
    tq.claim(child3, s1["id"])
    tq.complete(child3, s1["id"], "重跑完成")
    rcpts = [r for r in bb.inbox_list(pid, s2["id"]) if r["kind"] == "task_receipt"]
    assert len(rcpts) == 2
    assert {r["payload"]["status"] for r in rcpts} == {"failed", "done"}


# ---------- E6 统一资产登记 ----------

def test_register_asset_auto_detect_and_dedup(bb, project):
    from core.blackboard.assets import detect_type, register_asset
    pid = project["id"]
    assert detect_type("https://a.com/x?y=1") == "url"
    assert detect_type("10.0.0.1") == "host"
    assert detect_type("10.0.0.1:8080") == "service"
    assert detect_type("a.example.com") == "domain"
    assert detect_type("a" * 64) == "binary"
    assert detect_type("随机 文本/nothing") is None

    r1 = register_asset(bb, pid, "https://10.0.0.1/admin")
    assert r1["type"] == "url" and r1["created"]
    # url 值含 IP → 自动建 host 并挂载
    assert bb.get_asset(r1["id"])["parent_id"] == r1["host_id"]
    assert bb.get_asset(r1["host_id"])["value"] == "10.0.0.1"

    # 同值重报（人工路径曾插重复行）→ 合并不插行 + meta merge
    r2 = register_asset(bb, pid, "https://10.0.0.1/admin", meta={"title": "后台"})
    assert r2["id"] == r1["id"] and not r2["created"]
    assert len(bb.list_assets(pid, type_="url")) == 1
    assert bb.get_asset(r1["id"])["meta"]["title"] == "后台"

    # 识别不出 → ValueError（API 422 提示手选）
    with pytest.raises(ValueError):
        register_asset(bb, pid, "不是资产的东西")


def test_register_asset_domain_dns_primary_alias(bb, project, monkeypatch):
    from core.blackboard.assets import register_asset
    from core.blackboard import assets as am
    monkeypatch.setattr(am.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    pid = project["id"]
    r1 = register_asset(bb, pid, "www.example.com")
    assert r1["dns"] and r1["host_id"]
    assert bb.get_asset(r1["host_id"])["meta"]["primary_domain"] == "www.example.com"

    r2 = register_asset(bb, pid, "dev.example.com")
    assert r2["host_id"] == r1["host_id"]            # host 按 IP 去重
    assert bb.get_asset(r2["id"])["meta"]["alias"] is True
    # 完整域名不降级：两行 domain 都在
    assert len(bb.list_assets(pid, type_="domain")) == 2

    # 解析失败 → 独立行
    def _boom(*a, **k):
        raise OSError("dns down")
    monkeypatch.setattr(am.socket, "getaddrinfo", _boom)
    r3 = register_asset(bb, pid, "dead.example.com")
    assert "host_id" not in r3
    assert bb.get_asset(r3["id"])["parent_id"] is None


def test_register_asset_url_domain_host_exact_match(bb, project):
    """url/service 主机部=既有 domain 资产 → 精确挂其下（不猜 DNS 不造行）；缺失保持独立行。"""
    from core.blackboard.assets import register_asset
    pid = project["id"]
    d = bb.upsert_asset(pid, "domain", "portal.corp.cn")["id"]
    r = register_asset(bb, pid, "http://portal.corp.cn/admin")
    assert bb.get_asset(r["id"])["parent_id"] == d
    r2 = register_asset(bb, pid, "portal.corp.cn:8443")
    assert r2["type"] == "service" and bb.get_asset(r2["id"])["parent_id"] == d
    r3 = register_asset(bb, pid, "http://ghost.corp.cn/x")
    assert bb.get_asset(r3["id"])["parent_id"] is None  # 无同名 domain：独立行不猜 DNS


# ---------- E7 扫描/测试状态机 ----------

def test_set_asset_status_whitelist_note_and_audit(bb, project):
    from core.blackboard.assets import register_asset
    pid = project["id"]
    aid = register_asset(bb, pid, "10.1.1.1")["id"]

    with pytest.raises(ValueError):
        bb.set_asset_status(aid, "pwned")
    with pytest.raises(ValueError):                    # tested_clean 必带 note
        bb.set_asset_status(aid, "tested_clean")
    with pytest.raises(LookupError):
        bb.set_asset_status("asset-000000000000", "visited")

    bb.set_asset_status(aid, "visited", author="s1")
    bb.set_asset_status(aid, "scanning", author="s1")
    # tested_clean 须死路意图背书（2026-09-25 门禁）
    from core.blackboard.intents import close_intent, declare_intent
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url="http://10.1.1.1/probe", status=404,
                              resp_body="")
    iid = declare_intent(bb, pid, "该主机入口无洞", target_asset_id=aid)["id"]
    close_intent(bb, pid, iid, "dead_end", dead_reason="全模板探测无异常",
                 evidence_refs=[f"http:{hid}"])
    bb.set_asset_status(aid, "tested_clean", note="nikto 全模板 + 手测上传点",
                        author="s1")
    assert bb.get_asset(aid)["status"] == "tested_clean"
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "asset.status_changed"]
    assert [(e["payload"]["old"], e["payload"]["new"]) for e in ev] == \
        [("open", "visited"), ("visited", "scanning"), ("scanning", "tested_clean")]
    assert ev[-1]["payload"]["note"].startswith("nikto")

    # 同状态 no-op：不发事件
    bb.set_asset_status(aid, "tested_clean", note="重复流转")
    assert len([e for e in bb.recent_events(pid)
                if e["kind"] == "asset.status_changed"]) == 3


def test_set_asset_status_budget_stop_and_na(bb, project):
    """六态扩展（借鉴 dsh AttackAtlas 覆盖态）：budget_stop/na 必带 note，
    让「哪里没挖完、为什么」可对账。"""
    from core.blackboard.assets import register_asset
    pid = project["id"]
    aid = register_asset(bb, pid, "10.9.9.9")["id"]

    with pytest.raises(ValueError):                    # budget_stop 必带 note
        bb.set_asset_status(aid, "budget_stop")
    with pytest.raises(ValueError):                    # na 必带 note
        bb.set_asset_status(aid, "na")

    bb.set_asset_status(aid, "budget_stop", note="token 预算耗尽，半量端口未测")
    assert bb.get_asset(aid)["status"] == "budget_stop"
    bb.set_asset_status(aid, "na", note="主机离线，ICMP/全端口均无响应")
    assert bb.get_asset(aid)["status"] == "na"
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "asset.status_changed"]
    assert [(e["payload"]["old"], e["payload"]["new"]) for e in ev] == \
        [("open", "budget_stop"), ("budget_stop", "na")]


def test_parent_clean_rejected_leaf_transitions_ok(bb, project):
    """⑫ 有子资产节点写 tested_clean 被 ValueError（状态由子树读时派生）；
    na 不挡（人工裁定）。⑬ 叶子节点流转完全不变。"""
    pid = project["id"]
    host = bb.upsert_asset(pid, "host", "10.2.2.2")["id"]
    leaf = bb.upsert_asset(pid, "url", "http://10.2.2.2/", parent_id=host)["id"]
    with pytest.raises(ValueError, match="子树"):
        bb.set_asset_status(host, "tested_clean", note="试图批量标干净")
    # na 人工裁定不被门拦
    bb.set_asset_status(host, "na", note="该 IP 整段不适用")
    assert bb.get_asset(host)["status"] == "na"
    # 叶子：tested_clean 正常流转（带死路意图背书）
    from core.blackboard.intents import close_intent, declare_intent
    hid = bb.add_http_history(pid, source="browser", method="GET",
                              url="http://10.2.2.2/", status=404, resp_body="")
    iid = declare_intent(bb, pid, "叶子面无洞", target_asset_id=leaf)["id"]
    close_intent(bb, pid, iid, "dead_end", dead_reason="探测无异常",
                 evidence_refs=[f"http:{hid}"])
    bb.set_asset_status(leaf, "tested_clean", note="叶子单点测完")
    assert bb.get_asset(leaf)["status"] == "tested_clean"


# ---------- 机制 1.1 发布去重 + workset / 机制 1.4 资源租约与 wait_for 门控 ----------

import pytest as _pytest

from core.blackboard.leases import normalize_key, normalize_keys
from core.blackboard.tasks import dedup_fp


def test_lease_key_normalization():
    assert normalize_key("IP:1.2.3.4") == "ip:1.2.3.4"
    assert normalize_key("host:Example.COM") == "host:example.com"
    assert normalize_key("domain:Portal.Corp.CN.") == "domain:portal.corp.cn"
    assert normalize_key("url:https://Ai.Example.COM/a/") == "url:ai.example.com/a"
    assert normalize_key("binary:" + "a" * 64) == "binary:" + "a" * 64
    assert normalize_key("func:" + "b" * 64 + ":0x401000") == "func:" + "b" * 64 + ":401000"
    assert normalize_key("tool:ida") == "tool:ida"
    assert normalize_key("user:edu-x:x.com") == "user:edu-x:x.com"
    for bad in ("ip:*", "1.2.3.4", "ip:", "domain:a b.com", "binary:xyz",
                "tool:IDA Pro", "user:Bad_NS:v", "func:a:z"):
        with _pytest.raises(ValueError):
            normalize_key(bad)
    assert normalize_keys(["IP:1.2.3.4", "ip:1.2.3.4"]) == ["ip:1.2.3.4"]


def test_publish_dedup_fp_and_workset(bb, project):
    """机制 1.1：publish 存指纹与 workset；find_dedup_target 命中 open/claimed、不命中 done。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "对 zut.edu.cn 做被动侦察", task_type="recon",
                    workset=["a.zut.edu.cn", "0x401000"])
    row = tq.get_task(t1)
    assert row["workset"] == ["0x401000", "a.zut.edu.cn"]
    fp = row["dedup_fp"]
    assert fp
    assert tq.find_dedup_target(pid, fp)["id"] == t1
    # claimed 也拦
    sid = _session(bb, project)["id"]
    tq.claim(t1, sid)
    assert tq.find_dedup_target(pid, fp)["id"] == t1
    # done 后不再命中（只拦 open/claimed）
    tq.complete(t1, sid, "完")
    assert tq.find_dedup_target(pid, fp) is None


def test_publish_scope_normalized_in_fp(bb, project):
    pid = project["id"]
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "枚举子域", scope="*.Zut.edu.cn;portal", task_type="recon")
    fp = tq.get_task(t1)["dedup_fp"]
    t2 = tq.publish(pid, "枚举子域", scope="*.zut.edu.cn;PORTAL", task_type="recon")
    assert tq.get_task(t2)["dedup_fp"] == fp  # scope 归一化后同指纹
    assert tq.find_dedup_target(pid, fp)["id"] == t1


def test_claim_grants_leases_x_x_conflict_marks_wait_for(bb, project):
    """机制 1.4：起跑转写 X 租约；第二个同键 active 委托起跑被拒并写 wait_for；
    前者收尾释放 → wait_for 清空、可起跑（释放重校验同事务，不重领）。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    s1, s2 = _session(bb, project, "A"), _session(bb, project, "B")
    k = ["ip:10.0.0.8"]
    t1 = tq.publish(pid, "打点 10.0.0.8", task_type="exploit", noise_budget="low",
                    conflict_keys=k, target_session=s1["id"])
    t2 = tq.publish(pid, "再打 10.0.0.8", task_type="exploit", noise_budget="low",
                    conflict_keys=k, target_session=s2["id"])
    tq.claim(t1, s1["id"])
    rows = bb.conn.execute(
        "SELECT * FROM resource_leases WHERE task_id=?", (t1,)).fetchall()
    assert len(rows) == 1 and rows[0]["mode"] == "X"
    assert rows[0]["resource_key"] == "ip:10.0.0.8"
    with _pytest.raises(ClaimError):
        tq.claim(t2, s2["id"])
    row2 = tq.get_task(t2)
    assert row2["wait_for"] == ["ip:10.0.0.8"]           # 门控标记（UI ⏳）
    assert tq.take_session_next(pid, s2["id"]) is None   # 预过滤：等待行不占线程
    tq.complete(t1, s1["id"], "完")                      # 释放 → 重校验清 wait_for
    assert tq.get_task(t2)["wait_for"] == []
    tq.claim(t2, s2["id"])
    assert tq.get_task(t2)["status"] == "claimed"


def test_lease_s_mode_shares_x_blocks(bb, project):
    """机制 1.4：passive 任务的键 = S 共享（两个 S 并行不冲突）；active X 与 S 互斥。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    ta = tq.publish(pid, "被动分析 A", task_type="recon", noise_budget="passive",
                    conflict_keys=["domain:target.com"])
    tb = tq.publish(pid, "被动分析 B", task_type="recon", noise_budget="passive",
                    conflict_keys=["domain:target.com"])
    tc = tq.publish(pid, "主动打点", task_type="exploit", noise_budget="low",
                    conflict_keys=["domain:target.com"])
    s1, s2, s3 = (_session(bb, project, "A"), _session(bb, project, "B"),
                  _session(bb, project, "C"))
    tq.claim(ta, s1["id"])
    tq.claim(tb, s2["id"])  # S+S 兼容
    with _pytest.raises(ClaimError):
        tq.claim(tc, s3["id"])  # X 与 S 冲突
    assert tq.get_task(tc)["wait_for"] == ["domain:target.com"]


def test_take_session_next_releases_then_claims(bb, project):
    """机制 1.4：占用者收尾后，等待者经 take_session_next 正常起跑（FIFO 不重领冲突）。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    s1, s2 = _session(bb, project, "A"), _session(bb, project, "B")
    tq.publish(pid, "占用者", task_type="exploit", noise_budget="low",
               conflict_keys=["ip:10.0.0.9"], target_session=s1["id"])
    tq.publish(pid, "等待者", task_type="exploit", noise_budget="low",
               conflict_keys=["ip:10.0.0.9"], target_session=s2["id"])
    assert tq.take_session_next(pid, s1["id"])          # 占用者起跑并拿 X 租约
    assert tq.take_session_next(pid, s2["id"]) is None  # 等待者被排除
    occupier = next(t["id"] for t in tq.list_tasks(pid) if t["objective"] == "占用者")
    tq.complete(occupier, s1["id"], "完")
    got = tq.take_session_next(pid, s2["id"])
    assert got and tq.get_task(got)["objective"] == "等待者"


def test_delete_and_expire_release_leases(bb, project):
    pid = project["id"]
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "被删的占用者", task_type="exploit", noise_budget="low",
                    conflict_keys=["ip:10.0.0.10"])
    t2 = tq.publish(pid, "等 10.0.0.10", task_type="exploit", noise_budget="low",
                    conflict_keys=["ip:10.0.0.10"])
    s1, s2 = _session(bb, project, "A"), _session(bb, project, "B")
    tq.claim(t1, s1["id"])
    with _pytest.raises(ClaimError):
        tq.claim(t2, s2["id"])
    tq.delete(t1, by="human")  # 删除释放租约 + 重校验
    assert tq.get_task(t2)["wait_for"] == []
    tq.claim(t2, s2["id"])
    assert bb.conn.execute("SELECT COUNT(*) c FROM resource_leases WHERE task_id=?",
                           (t1,)).fetchone()["c"] == 0


def test_wait_for_deadlock_detection_sacrifices_youngest(bb, project):
    """机制 1.4 六防死锁之 6（安全网）：构造 wait-for 环 → 牺牲者=最年轻任务，
    清 wait_for + 冷却 + lock.deadlock_victim 事件。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    s1, s2 = _session(bb, project, "A"), _session(bb, project, "B")
    ta = tq.publish(pid, "环 A", task_type="recon")
    tb = tq.publish(pid, "环 B", task_type="recon")
    tq.claim(ta, s1["id"])
    tq.claim(tb, s2["id"])
    # 人工构造不一致态（运行期动态锁未来路径的安全网）：两个 claimed 任务互等
    with bb._tx():
        bb.conn.execute("UPDATE tasks SET wait_for=? WHERE id=?",
                        ('["ip:1.1.1.1"]', ta))
        bb.conn.execute("UPDATE tasks SET wait_for=? WHERE id=?",
                        ('["ip:2.2.2.2"]', tb))
        for key, holder in (("ip:1.1.1.1", tb), ("ip:2.2.2.2", ta)):
            bb.conn.execute(
                "INSERT INTO resource_leases(project_id,resource_key,mode,task_id,"
                "session_id,granted_at) VALUES(?,?,?,?,?,?)",
                (pid, key, "X", holder, s2["id"] if holder == tb else s1["id"], "2026-01-01"))
    victims = tq.detect_wait_for_deadlock(pid)
    assert len(victims) == 1
    victim = victims[0]
    other = tb if victim == ta else ta
    assert victim == max((ta, tb), key=lambda t: tq.get_task(t)["created_at"])
    v = tq.get_task(victim)
    assert v["wait_for"] == [] and v["lease_cooldown_until"]
    assert tq.get_task(other)["wait_for"]  # 另一方保留
    assert "lock.deadlock_victim" in [e["kind"] for e in bb.recent_events(pid)]


def test_update_task_recomputes_fp_and_normalizes_keys(bb, project):
    pid = project["id"]
    tq = TaskQueue(bb)
    t = tq.publish(pid, "初版目标", task_type="recon", conflict_keys=["domain:X.com"])
    updated = tq.update_task(t, by="human", objective="新目标",
                             conflict_keys=["IP:10.0.0.1"])
    assert updated["conflict_keys"] == ["ip:10.0.0.1"]
    assert tq.get_task(t)["dedup_fp"] == dedup_fp("recon", "", "新目标")
    with _pytest.raises(ValueError):
        tq.update_task(t, by="human", conflict_keys=["ip:*"])


# ---------- C1 blocked_reason / reopen 附注 ----------

def test_fail_blocked_reason_recorded(bb, project):
    """C1：fail 落 blocked_reason 列与事件；非法值拒收。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "挂起任务", task_type="recon")
    s1 = _session(bb, project)
    tq.claim(t1, s1["id"])
    tq.fail(t1, s1["id"], "ROE 未核验", resumable=True, blocked_reason="awaiting_human")
    row = tq.get_task(t1)
    assert row["blocked_reason"] == "awaiting_human"
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.failed"][-1]
    assert ev["payload"]["blocked_reason"] == "awaiting_human"
    with _pytest.raises(ValueError):
        tq.fail(t1, s1["id"], "x", blocked_reason="bogus")


def test_reopen_note_appended(bb, project):
    """C1：放回附注追加进 result_note 并随 task.reopened 落审计。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "待人工任务", task_type="recon")
    s1 = _session(bb, project)
    tq.claim(t1, s1["id"])
    tq.fail(t1, s1["id"], "等 ROE", blocked_reason="awaiting_human")
    tq.reopen(t1, by="human", note="ROE 已核验，继续")
    row = tq.get_task(t1)
    assert row["status"] == "open"
    assert "人类补充（human）: ROE 已核验，继续" in row["result_note"]
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.reopened"][-1]
    assert ev["payload"]["note"] == "ROE 已核验，继续"


def test_cancel_task(bb, project):
    """M4 C1：cancel_task——open/claimed → failed（blocked_reason=cancelled），
    清持有方与租约（防 worker 事后收尾覆写取消态）、attempts 履历照记、
    资源租约同步释放；返回被清前的持有窗供调用方打断在跑会话；
    非 open/claimed（done/failed）不可取消（走 requeue）。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    s1 = _session(bb, project)
    # claimed 场景：返回打断目标 + 清持有方
    t1 = tq.publish(pid, "在跑任务", created_by="orchestrator")
    tq.claim(t1, s1["id"])
    res = tq.cancel_task(t1, by="orchestrator", reason="方向变更，作废重排")
    assert res["claimed_by"] == s1["id"]
    row = tq.get_task(t1)
    assert row["status"] == "failed" and row["blocked_reason"] == "cancelled"
    assert row["claimed_by"] is None and row["lease_until"] is None
    att = (row["context"] or {}).get("attempts") or []
    assert att and att[-1]["outcome"] == "failed"
    assert att[-1]["blocked_reason"] == "cancelled"
    assert att[-1]["session_id"] == s1["id"]
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.cancelled"][-1]
    assert ev["payload"]["by"] == "orchestrator"
    assert ev["payload"]["reason"] == "方向变更，作废重排"
    assert ev["payload"]["had_runner"] is True
    # open 场景：无持有窗
    t2 = tq.publish(pid, "排队任务")
    res2 = tq.cancel_task(t2, by="human", reason="人工收编")
    assert res2["claimed_by"] is None
    assert tq.get_task(t2)["blocked_reason"] == "cancelled"
    ev2 = [e for e in bb.recent_events(pid) if e["kind"] == "task.cancelled"][-1]
    assert ev2["payload"]["had_runner"] is False
    # 资源租约随取消释放（claimed 时占住的 host 冲突键可被新任务复用）
    t3 = tq.publish(pid, "占 1.2.3.4", conflict_keys=["ip:1.2.3.4"])
    tq.claim(t3, s1["id"])
    tq.cancel_task(t3, by="orchestrator", reason="换路")
    t4 = tq.publish(pid, "再占 1.2.3.4", conflict_keys=["ip:1.2.3.4"])
    s2 = _session(bb, project)
    tq.claim(t4, s2["id"])  # 不因租约未释放而 ClaimError
    # 终态不可取消
    t5 = tq.publish(pid, "会完成任务")
    tq.claim(t5, s1["id"])
    tq.complete(t5, s1["id"], "收工")
    with pytest.raises(ValueError, match="仅 open/claimed 可取消"):
        tq.cancel_task(t5)
    with pytest.raises(ValueError, match="仅 open/claimed 可取消"):
        tq.cancel_task(t1)  # 已是 failed（cancelled）


def test_lifecycle_events_carry_created_by(bb, project):
    """编排页签关联（2026-09-18）：claimed/done/reopened 生命周期事件 payload 带
    created_by（发布者），前端编排页签据此收录编排派单的全生命周期；人类发布的
    不进（created_by=human）。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    # 编排发布 → 生命周期事件 created_by=orchestrator
    t1 = tq.publish(pid, "编排派单任务", created_by="orchestrator")
    s1 = _session(bb, project)
    tq.claim(t1, s1["id"])
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.claimed"][-1]
    assert ev["payload"]["created_by"] == "orchestrator"
    tq.complete(t1, s1["id"], "收工")
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.done"][-1]
    assert ev["payload"]["created_by"] == "orchestrator"
    # 人类发布 → 失败放回后 created_by=human（不进编排页签口径）
    t2 = tq.publish(pid, "人类派单任务")
    tq.claim(t2, s1["id"])
    tq.fail(t2, s1["id"], "先放回")
    tq.reopen(t2)
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "task.reopened"][-1]
    assert ev["payload"]["created_by"] == "human"


# ---------- 收录门禁：pentest/redteam 轨 info 停收（2026-09-18，全类别） ----------

def test_gate_info_rejected_all_categories_on_pentest(bb, project):
    """渗透轨 severity=info 全类别拒收：category 兜底（info→intel）路径与
    显式 vuln+info 都被拦；low 正常入库。"""
    pid = project["id"]
    with _pytest.raises(ValueError, match="不再收录 severity=info"):
        bb.add_finding(pid, "info-point", "信息点", severity="info", track="pentest")
    with _pytest.raises(ValueError, match="不再收录 severity=info"):
        bb.add_finding(pid, "exposure", "暴露面", severity="info",
                       category="vuln", track="pentest")
    r = bb.add_finding(pid, "sqli", "注入", severity="low", track="pentest")
    assert r["severity"] == "low"


def test_gate_info_patch_rejected_but_title_edit_allowed(bb, project):
    """渗透轨 patch：显式把 severity 改成 info 拒；纯标题/备注变更放行。"""
    pid = project["id"]
    fid = bb.add_finding(pid, "sqli", "注入", severity="low", track="pentest")["id"]
    with _pytest.raises(ValueError, match="不再收录 severity=info"):
        bb.patch_finding(pid, fid, severity="info", track="pentest")
    r = bb.patch_finding(pid, fid, title="注入（改名）", track="pentest")
    assert r["title"] == "注入（改名）"


def test_gate_info_still_allowed_on_ctf(bb):
    """CTF 轨背景信息（info）语义不动：add/patch 均放行。"""
    pid = bb.create_project("CTF-测试", "ctf", ["binary"])["id"]
    r = bb.add_finding(pid, "信息点", "背景信息", severity="info", track="ctf")
    assert r["severity"] == "info"
    r2 = bb.patch_finding(pid, r["id"], severity="low", track="ctf")
    assert r2["severity"] == "low"


# ---------- 收录格式三件套（finding-report-format M1，2026-09-22） ----------

def test_verified_gate_repro_steps(bb, project):
    """渗透轨 verified 门禁新口径：repro_steps 至少一步 code/artifact_id 非空且
    该步 expected 非空；旧结构 poc/pocs/poc_artifact_id 兼容直通；add/patch 同口径。"""
    pid = project["id"]
    good = [{"desc": "登录低权账号", "type": "http",
             "code": "POST /login HTTP/1.1", "expected": "200 返回 token"}]
    r = bb.add_finding(pid, "sqli", "注入", severity="low", status="verified",
                       evidence={"repro_steps": good}, track="pentest")
    assert r["id"]
    # 只有 desc 无 code/artifact_id → 拒
    with pytest.raises(ValueError, match="复现证据"):
        bb.add_finding(pid, "sqli2", "注入2", severity="low", status="verified",
                       evidence={"repro_steps": [{"desc": "打开页面", "expected": "x"}]},
                       track="pentest")
    # 有 code 无 expected → 拒（复现自证锚点缺失）
    with pytest.raises(ValueError, match="复现证据"):
        bb.add_finding(pid, "sqli3", "注入3", severity="low", status="verified",
                       evidence={"repro_steps": [{"desc": "发包", "type": "cmd",
                                                  "code": "curl http://x"}]},
                       track="pentest")
    # image 步骤（artifact_id 但 expected 空）不算过门禁
    with pytest.raises(ValueError, match="复现证据"):
        bb.add_finding(pid, "sqli6", "注入6", severity="low", status="verified",
                       evidence={"repro_steps": [{"desc": "截图", "type": "image",
                                                  "artifact_id": "art-img"}]},
                       track="pentest")
    # 旧结构兼容直通：pocs / poc_artifact_id（artifact 外键，须真实行）
    assert bb.add_finding(pid, "sqli4", "注入4", severity="low", status="verified",
                          evidence={"pocs": [{"http_raw": "GET /"}]},
                          track="pentest")["id"]
    art = bb.add_artifact(pid, "poc/x.py", kind="poc")
    assert bb.add_finding(pid, "sqli5", "注入5", severity="low", status="verified",
                          poc_artifact_id=art, track="pentest")["id"]
    # patch 路径同口径：repro_steps 合规可升 verified，不合格拒
    fid = bb.add_finding(pid, "authz", "越权", severity="low", track="pentest")["id"]
    r = bb.patch_finding(pid, fid, status="verified", track="pentest",
                         evidence={"repro_steps": good})
    assert r["status"] == "verified"
    with pytest.raises(ValueError, match="复现证据"):
        bb.patch_finding(pid, fid, status="verified", track="pentest",
                         evidence={"repro_steps": [{"desc": "看看"}]})


def test_repro_steps_validation(bb, project):
    """repro_steps 结构校验（全轨写入口）：非数组/缺 desc/非法 type/image 缺
    artifact_id 一律 ValueError；旧值 http_raw 写入宽容（读时映射 http）。"""
    pid = project["id"]
    base = dict(severity="low", track="pentest")
    with pytest.raises(ValueError, match="必须是步骤数组"):
        bb.add_finding(pid, "s", "t", evidence={"repro_steps": "bad"}, **base)
    with pytest.raises(ValueError, match="desc"):
        bb.add_finding(pid, "s", "t",
                       evidence={"repro_steps": [{"type": "http"}]}, **base)
    with pytest.raises(ValueError, match="非法 type"):
        bb.add_finding(pid, "s", "t",
                       evidence={"repro_steps": [{"desc": "x", "type": "sql"}]}, **base)
    with pytest.raises(ValueError, match="artifact_id"):
        bb.add_finding(pid, "s", "t",
                       evidence={"repro_steps": [{"desc": "x", "type": "image"}]}, **base)
    r = bb.add_finding(pid, "s2", "t2",
                       evidence={"repro_steps": [{"desc": "x", "type": "http_raw"}]}, **base)
    assert r["id"]


def test_repro_steps_union_merge_and_impact_remediation(bb, project):
    """repro_steps 进证据并集（内容指纹去重追加，重复上报不堆步）；impact/
    remediation 合并旧值非空保留、空缺由新报补入；patch 修订（空串=清空）。"""
    pid = project["id"]
    s1 = {"desc": "步骤一", "type": "http", "code": "GET / HTTP/1.1", "expected": "200"}
    s2 = {"desc": "步骤二", "type": "cmd", "code": "curl -X POST http://x",
          "expected": "admin"}
    a = bb.add_finding(pid, "sqli", "注入", severity="low", impact="读库",
                       evidence={"repro_steps": [s1]}, dedup_key="k1")["id"]
    r = bb.add_finding(pid, "sqli", "注入", severity="medium", impact="读全库+拖库",
                       remediation="参数化查询",
                       evidence={"repro_steps": [dict(s1), s2]}, dedup_key="k1")
    assert r["merged"] is True and r["severity"] == "medium"
    row = bb.get_finding(pid, a)
    assert row["evidence"]["repro_steps"] == [s1, s2]
    assert row["impact"] == "读库"  # 旧值非空保留（不覆盖前人结论）
    assert row["remediation"] == "参数化查询"  # 空缺由新报补入
    # patch 修订：impact 清空 / remediation 改写，changed 进事件
    out = bb.patch_finding(pid, a, impact="", remediation="升级 ORM 后参数化")
    assert out["impact"] == "" and out["remediation"] == "升级 ORM 后参数化"
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "finding.updated"][-1]
    assert {"impact", "remediation"} <= set(ev["payload"]["changed"])


# ---------- v14 任务绑定角色 + 同 target 防碎闸 ----------

def test_publish_role_validation_and_stored(bb, project):
    """publish role 校验（allowed_roles 注入语义，对称 allowed_types）+ 入库带出。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    t = tq.publish(pid, "侦察", role="recon",
                   allowed_roles=["_generalist", "recon"])
    assert tq.get_task(t)["role"] == "recon"
    with pytest.raises(ValueError, match="未注册的 role"):
        tq.publish(pid, "侦察2", role="typo-role",
                   allowed_roles=["_generalist", "recon"])
    # allowed_roles=None（未接线）不校验；role 留空恒放行
    t2 = tq.publish(pid, "不限", role="")
    assert tq.get_task(t2)["role"] == ""


def test_dedup_fp_excludes_role(bb, project):
    """dedup_fp 指纹不含 role：同目标换角色不算新任务（防绕过发布去重）——
    只差 role 的两次发布，第二次命中既有 open 行指纹。"""
    from core.blackboard.tasks import dedup_fp
    pid = project["id"]
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "扫描", scope="ip:1.2.3.4", task_type="recon", role="recon")
    dup = tq.find_dedup_target(pid, dedup_fp("recon", "ip:1.2.3.4", "扫描"))
    assert dup is not None and dup["id"] == t1
    t2 = tq.publish(pid, "扫描", scope="ip:1.2.3.4", task_type="recon", role="web-exploit")
    assert t2 != t1  # 发布不因 role 被拦（去重判定在调用方），但指纹与 t1 相同
    assert tq.get_task(t2)["dedup_fp"] == tq.get_task(t1)["dedup_fp"]


def test_target_keys_of_scope_parsing():
    """target_keys_of 纯函数：conflict_keys 归一化取目标键 + url 派生 host +
    passive scope 切段识别（ip/URL/裸域）。"""
    from core.blackboard.tasks import target_keys_of
    assert target_keys_of("", ["ip:1.2.3.4", "url:http://x.com/a", "tool:nmap"]) == {
        "ip:1.2.3.4", "host:x.com"}
    assert target_keys_of("1.2.3.4 https://Y.com/path foo.tld", None) == {
        "ip:1.2.3.4", "host:y.com", "domain:foo.tld"}
    assert target_keys_of("no-target-here", []) == set()


def test_target_guard_blocks_fifth_task_and_bypass(bb, project):
    """同 target 防碎闸：第 5 个同目标任务拒收；bypass_target_guard（人类 force）
    放行；不同 IP 不误伤。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    for i in range(4):
        tq.publish(pid, f"任务 {i}", noise_budget="low",
                   conflict_keys=["ip:1.2.3.4"], created_by="orchestrator")
    with pytest.raises(ValueError, match="任务已达 4 个"):
        tq.publish(pid, "第 5 个", noise_budget="low",
                   conflict_keys=["ip:1.2.3.4"], created_by="orchestrator")
    # 不同 IP 不受影响
    tq.publish(pid, "别的 IP", noise_budget="low",
               conflict_keys=["ip:5.6.7.8"], created_by="orchestrator")
    # 人类 force 旁路
    t5 = tq.publish(pid, "第 5 个", noise_budget="low",
                    conflict_keys=["ip:1.2.3.4"], created_by="human",
                    bypass_target_guard=True)
    assert t5
    # done 一条后即可再发（在队口径只数 open+claimed）：放行任务收尾后再补
    # 一条别的，同 IP 原有 4 条 open 仍占坑——先收尾一条原任务腾位
    s = _session(bb, project)
    tq.claim(t5, s["id"])
    tq.complete(t5, s["id"])
    rows = [t for t in tq.list_tasks(pid)
            if t["status"] == "open" and t["conflict_keys"] == ["ip:1.2.3.4"]]
    tq.claim(rows[0]["id"], s["id"])
    tq.complete(rows[0]["id"], s["id"])
    tq.publish(pid, "第 6 个", noise_budget="low",
               conflict_keys=["ip:1.2.3.4"], created_by="orchestrator")


def test_target_guard_passive_scope_derived(bb, project):
    """passive 无 conflict_keys 时按 scope 切段识别目标：url 派生 host 键，
    同 host 的另一 URL 路径合并计数（裸域会归 domain: 键，与 host 分开）。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    for i in range(4):
        tq.publish(pid, f"被动侦察 {i}", scope="https://t.example.com/admin")
    with pytest.raises(ValueError, match="host:t.example.com"):
        tq.publish(pid, "第 5 个", scope="https://t.example.com/login")


def test_session_queue_ordering_by_priority_then_created_at(bb, project):
    """窗内队列读时派生（会话中心化 2026-09-25）：priority → created_at 排序；
    无 role 偏好（选窗在编排器 delegate 侧，队列只做排序）。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    s = _session(bb, project, "worker")
    t_late_p0 = tq.publish(pid, "P0 晚发", priority=0,
                           created_by="orchestrator", target_session=s["id"])
    t_early_p2 = tq.publish(pid, "P2 早发", priority=2,
                            created_by="orchestrator", target_session=s["id"])
    t_later_p2 = tq.publish(pid, "P2 更晚", priority=2,
                            created_by="orchestrator", target_session=s["id"])
    assert tq.take_session_next(pid, s["id"]) == t_late_p0
    assert tq.take_session_next(pid, s["id"]) == t_early_p2
    assert tq.take_session_next(pid, s["id"]) == t_later_p2
    assert tq.take_session_next(pid, s["id"]) is None


def test_untargeted_open_delegations_never_auto_run(bb, project):
    """会话中心化：target_session='' 的 open 行（L0 提案残留/关窗退回）不自动
    起跑——任何窗 take_session_next 都不可见；显式指给本窗的委托照常可取。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    s_manual = _session(bb, project, "manual")
    t_pool = tq.publish(pid, "未指派委托", task_type="generic", created_by="human")
    assert tq.take_session_next(pid, s_manual["id"]) is None
    assert tq.get_task(t_pool)["status"] == "open"
    t_bound = tq.publish(pid, "指派委托", task_type="generic", created_by="human",
                         target_session=s_manual["id"])
    assert tq.take_session_next(pid, s_manual["id"]) == t_bound
    s_other = _session(bb, project, "other")
    assert tq.take_session_next(pid, s_other["id"]) is None


def test_finish_records_persona_role(bb, project):
    """attempts 履历记录实际执行 persona（v14）：complete/fail 透传 persona_role，
    缺省回退会话底色 role。"""
    pid = project["id"]
    s = _session(bb, project, "底色窗")  # 底色 _generalist
    tq = TaskQueue(bb)
    t = tq.publish(pid, "带角色任务", role="recon")
    tq.claim(t, s["id"])
    tq.complete(t, s["id"], "完成", persona_role="recon")
    att = tq.get_task(t)["context"]["attempts"][0]
    assert att["role"] == "recon"
    t2 = tq.publish(pid, "无角色任务")
    tq.claim(t2, s["id"])
    tq.fail(t2, s["id"], "失败")
    att2 = tq.get_task(t2)["context"]["attempts"][0]
    assert att2["role"] == "_generalist"  # 缺省=会话底色


# ---------- v15 http_history（F6：浏览器抓包/重发/爆破统一入库） ----------

def test_http_history_add_list_get_clear(bb, project):
    pid = project["id"]
    r1 = bb.add_http_history(pid, source="browser", method="GET",
                             url="http://10.0.0.1/", status=200,
                             req_headers={"Accept": "*/*"},
                             resp_headers={"Content-Type": "text/html"},
                             resp_body="<h1>hi</h1>", resp_mime="text/html",
                             session_id="sess-a", task_id=None, duration_ms=12)
    r2 = bb.add_http_history(pid, source="intruder", method="POST",
                             url="http://10.0.0.1/login?u=§U§", status=403,
                             batch_id="in-1", meta={"payload": {"U": "admin"}},
                             resp_body="denied" * 200, body_truncated=True,
                             session_id=None, duration_ms=5)
    assert r1 < r2
    # 非法 source 拒收
    import pytest
    with pytest.raises(ValueError):
        bb.add_http_history(pid, source="nope", method="GET", url="x")
    # 增量游标 + 过滤
    rows = bb.list_http_history(pid, since_id=r1)
    assert [r["id"] for r in rows] == [r2]
    assert bb.list_http_history(pid, batch_id="in-1")[0]["meta"] == {"payload": {"U": "admin"}}
    assert bb.list_http_history(pid, source="browser")[0]["id"] == r1
    # 列表 body 截短预览（200 字符）且带截断标志
    short = bb.list_http_history(pid, batch_id="in-1")[0]
    assert len(short["resp_body"]) == 200 and short["body_truncated"] is True
    # 单条全量（含跨项目保护）
    full = bb.get_http_history(pid, r2)
    assert len(full["resp_body"]) == 1200
    other = bb.create_project("其他项目", "ctf", ["web"])
    assert bb.get_http_history(other["id"], r2) is None
    # 清理：按批 / 全部
    assert bb.clear_http_history(pid, batch_id="in-1") == 1
    assert bb.clear_http_history(other["id"]) == 0
    assert bb.clear_http_history(pid) == 1


def test_schema_v15_upgrade_from_v14(tmp_path):
    """v14 旧库打开：DDL IF NOT EXISTS 幂等补建 http_history，无 ALTER。"""
    import sqlite3
    from core.blackboard.schema import init_schema, SCHEMA_VERSION
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    # 伪造一个 v14 形态的库：只建 meta/projects/events 最小集 + 旧版本号
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version', '14');
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            domain TEXT NOT NULL, track TEXT NOT NULL DEFAULT '',
            capabilities TEXT NOT NULL DEFAULT '[]', config TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()
    bb = Blackboard(db)
    assert bb.conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                           ).fetchone()[0] == str(SCHEMA_VERSION)
    assert bb.conn.execute("SELECT 1 FROM http_history LIMIT 0")
    bb.close()


# ---------- H2 乐观锁 revision（2026-09-19） ----------

def test_revision_cas_on_finding_patch(bb, project):
    """H2：patch_finding 每次写 revision+1；expected_revision 不符抛冲突。
    并发写同一发现不再静默丢失更新——冲突方重新读取后再改。"""
    pid = project["id"]
    f = bb.add_finding(pid, "sqli", "注入点", severity="high")
    fid = f["id"]
    cur = bb.get_finding(pid, fid)
    assert cur["revision"] == 1
    r1 = bb.patch_finding(pid, fid, severity="critical", expected_revision=1)
    assert r1["revision"] == 2
    # 基于旧 revision 再改 → 冲突
    with pytest.raises(ValueError, match="乐观锁冲突"):
        bb.patch_finding(pid, fid, severity="low", expected_revision=1)
    # 不传 expected_revision = 不设防（人类 UI 单写者场景照常工作）
    assert bb.patch_finding(pid, fid, title="改名")["revision"] == 3


def test_revision_cas_on_asset_status(bb, project):
    """H2：资产状态流转 revision 语义——流转 +1、同态 no-op 不增、冲突抛 ValueError。"""
    from core.blackboard.assets import register_asset
    pid = project["id"]
    a = register_asset(bb, pid, "10.0.0.9")
    aid = a["id"]
    assert bb.get_asset(aid)["revision"] == 1
    bb.set_asset_status(aid, "visited", expected_revision=1)
    assert bb.get_asset(aid)["revision"] == 2
    # 同状态 no-op：不 bump、不冲突（幂等重试安全）
    bb.set_asset_status(aid, "visited", expected_revision=2)
    assert bb.get_asset(aid)["revision"] == 2
    with pytest.raises(ValueError, match="乐观锁冲突"):
        bb.set_asset_status(aid, "scanning", expected_revision=1)
    assert bb.get_asset(aid)["revision"] == 2  # 冲突未写
    # meta 合并写也推进 revision
    bb.update_asset_meta(aid, {"tags": ["高价值"]})
    assert bb.get_asset(aid)["revision"] == 3


# ---------- 蓝图（R4 逆向开发管线，DESIGN §9 R4） ----------

def test_blueprint_create_get_list_and_duplicate(bb, project):
    """创建/读取/列表/同项目同样本重名拒收。"""
    pid = project["id"]
    bp = bb.create_blueprint(
        pid, "聊天客户端", goal="重建一个同业务逻辑的客户端", binary_sha256="abc123",
        modules=[{"name": "网络通信", "desc": "TCP 协议与心跳",
                  "func_addresses": ["0x401000", "0x401100"]},
                 {"name": "加密校验", "desc": "登录包 AES", "status": "pending"}],
        author="human")
    assert bp["status"] == "draft" and bp["binary_sha256"] == "abc123"
    assert [m["name"] for m in bp["modules"]] == ["网络通信", "加密校验"]
    assert bp["modules"][0]["func_addresses"] == ["0x401000", "0x401100"]
    got = bb.get_blueprint(pid, bp["id"])
    assert got is not None and got["modules"][1]["status"] == "pending"
    assert len(bb.list_blueprints(pid)) == 1
    with pytest.raises(ValueError, match="重名"):
        bb.create_blueprint(pid, "聊天客户端", binary_sha256="abc123")
    # 不同样本同名允许（UNIQUE 三元组）
    other = bb.create_blueprint(pid, "聊天客户端", binary_sha256="def456")
    assert other["id"] != bp["id"]
    with pytest.raises(ValueError, match="name"):
        bb.create_blueprint(pid, "  ")
    # 跨项目读不到
    p2 = bb.create_project("另一项目", "research", ["binary"])
    assert bb.get_blueprint(p2["id"], bp["id"]) is None


def test_blueprint_status_whitelist(bb, project):
    """状态只进不退白名单：draft→reviewed→ready→building→built。"""
    pid = project["id"]
    bp = bb.create_blueprint(pid, "蓝图甲")
    bid = bp["id"]
    with pytest.raises(ValueError, match="不可从 draft 流转到 ready"):
        bb.set_blueprint_status(pid, bid, "ready")
    bb.set_blueprint_status(pid, bid, "reviewed")
    bb.set_blueprint_status(pid, bid, "ready")
    bb.set_blueprint_status(pid, bid, "building")
    bb.set_blueprint_status(pid, bid, "built")
    with pytest.raises(ValueError, match="已终态|不可从 built"):
        bb.set_blueprint_status(pid, bid, "draft")
    with pytest.raises(ValueError, match="非法蓝图状态"):
        bb.set_blueprint_status(pid, bid, "shipped")
    assert bb.get_blueprint(pid, bid)["status"] == "built"
    # 同状态幂等（built→built 白名单外但 status==cur 放行）
    bb.set_blueprint_status(pid, bid, "built")
    # 跨项目/不存在
    assert bb.set_blueprint_status(pid, "bp-nope", "reviewed") is None


def test_blueprint_module_patch(bb, project):
    """模块级 set（深析写回 spec/notes/status）；未知模块 404 语义；非法状态拒。"""
    pid = project["id"]
    bp = bb.create_blueprint(pid, "蓝图乙", modules=[
        {"name": "文件持久化"}, {"name": "许可校验"}])
    bid = bp["id"]
    r = bb.update_blueprint_module(pid, bid, "文件持久化",
                                   spec="def save(cfg): ...", notes="先解析再落盘",
                                   status="specd")
    mod = next(m for m in r["modules"] if m["name"] == "文件持久化")
    assert mod["spec"].startswith("def save") and mod["status"] == "specd"
    # 其他模块不动
    assert next(m for m in r["modules"] if m["name"] == "许可校验")["status"] == "pending"
    with pytest.raises(LookupError, match="模块不存在"):
        bb.update_blueprint_module(pid, bid, "不存在的模块", notes="x")
    with pytest.raises(ValueError, match="非法状态|非法"):
        bb.update_blueprint_module(pid, bid, "文件持久化", status="shipped")
    with pytest.raises(ValueError, match="func_addresses"):
        bb.update_blueprint_module(pid, bid, "文件持久化", func_addresses="0x401000")
    r = bb.update_blueprint_module(pid, bid, "文件持久化",
                                   func_addresses=["0x402000", "0x402100"])
    assert next(m for m in r["modules"] if m["name"] == "文件持久化")["func_addresses"] \
        == ["0x402000", "0x402100"]


def test_blueprint_content_and_modules_set(bb, project):
    """正文 append/替换与整表重划分；modules_set 重名拒收。"""
    pid = project["id"]
    bp = bb.create_blueprint(pid, "蓝图丙", content_md="# 概览\n")
    bid = bp["id"]
    r = bb.update_blueprint_content(pid, bid, content_append="## 数据流\n")
    assert r["content_md"] == "# 概览\n## 数据流\n"
    r = bb.update_blueprint_content(pid, bid, content_md="# 重写\n")
    assert r["content_md"] == "# 重写\n"
    r = bb.update_blueprint_content(pid, bid, modules_set=[
        {"name": "网络通信"}, {"name": "加密校验"}])
    assert {m["name"] for m in r["modules"]} == {"网络通信", "加密校验"}
    with pytest.raises(ValueError, match="重复"):
        bb.update_blueprint_content(pid, bid, modules_set=[{"name": "a"}, {"name": "a"}])
    r = bb.update_blueprint_content(pid, bid, modules_set=[
        {"name": "重组模块", "status": "analyzed"}])
    assert [m["name"] for m in r["modules"]] == ["重组模块"]
    bb.set_blueprint_status(pid, bid, "reviewed")
    # 事件流
    kinds = [e["kind"] for e in bb.recent_events(pid)]
    assert {"blueprint.created", "blueprint.updated", "blueprint.status_changed"} <= set(kinds)


def test_blueprint_modules_normalization_on_create(bb, project):
    """create 时模块归一化：缺 name 拒、func_addresses 非数组拒、非法 status 拒。"""
    pid = project["id"]
    with pytest.raises(ValueError, match="name"):
        bb.create_blueprint(pid, "B1", modules=[{"desc": "没名字"}])
    with pytest.raises(ValueError, match="func_addresses"):
        bb.create_blueprint(pid, "B2", modules=[{"name": "m", "func_addresses": "0x40"}])
    with pytest.raises(ValueError, match="非法状态"):
        bb.create_blueprint(pid, "B3", modules=[{"name": "m", "status": "done"}])


# ---------- recent_events kinds 过滤（bb-query-filters M2） ----------

def test_recent_events_kinds_filter(bb, project):
    pid = project["id"]
    s1 = bb.register_session(pid, "S1")["id"]
    s2 = bb.register_session(pid, "S2")["id"]
    bb.append_event(pid, "command", {"x": 1}, session_id=s1, author=s1)
    bb.append_event(pid, "finding.new", {"x": 2}, session_id=s1, author=s1)
    bb.append_event(pid, "command", {"x": 3}, session_id=s2, author=s2)
    bb.append_event(pid, "task.published", {"x": 4}, author="orchestrator")
    # 单值 / 多值
    only_cmd = bb.recent_events(pid, kinds=["command"])
    assert [e["payload"]["x"] for e in only_cmd] == [1, 3]
    two = bb.recent_events(pid, kinds=["command", "finding.new"])
    assert [e["payload"]["x"] for e in two] == [1, 2, 3]
    # 与 session_id 正交
    one_sess = bb.recent_events(pid, kinds=["command", "finding.new"],
                                session_id=s1)
    assert [e["payload"]["x"] for e in one_sess] == [1, 2]
    # 与 tail 正交：最新 2 条 command → x=3 之后……只有两条 command（1,3），
    # tail=1 取最新一条 command
    tail = bb.recent_events(pid, kinds=["command"], tail=1)
    assert [e["payload"]["x"] for e in tail] == [3]
    # 无命中
    assert bb.recent_events(pid, kinds=["nope"]) == []
    # None / 空列表 = 不过滤（默认行为零变化）
    assert len(bb.recent_events(pid, kinds=None)) == 4
    assert len(bb.recent_events(pid, kinds=[])) == 4
