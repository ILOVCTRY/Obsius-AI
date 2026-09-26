"""资产树重建脚本测试（asset-tree-derived-clean M2 配套，方案 §4 ⑭）。

断网：monkeypatch doh_resolve，不打 DoH。覆盖 dry-run 零写入、--apply
落库（reparent 挂树 + primary_domain、reset-open）、二次运行零变体
（幂等）；CDN 根行（CIDR/CNAME）无操作、fake-ip 只报跳过。
"""

import importlib.util
from pathlib import Path

import pytest

from core.blackboard.cdn import CdnLists
from core.projects import ProjectStore

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rebuild_asset_trees.py"
_spec = importlib.util.spec_from_file_location("rebuild_asset_trees", _SCRIPT)
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)  # type: ignore[union-attr]

CDN_LISTS = CdnLists(["104.16.0.0/13"], ["cloudfront.net"])

_ANSWERS = {
    # domain: (A 记录, CNAME 链)
    "a.com": ("10.0.0.5", []),
    "cdn.com": ("104.16.0.1", []),
    "edge.com": ("203.0.113.9", ["d1234.cloudfront.net"]),
    "fake.com": ("198.18.0.1", []),
    "dead.com": (None, []),
}


@pytest.fixture()
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "doh_resolve",
                        lambda domain, resolver: _ANSWERS[domain])
    store = ProjectStore(tmp_path / "ws")
    proj = store.create_project("重建测试", "pentest", ["web"])
    slug, pid = proj.meta["slug"], proj.id
    bb = proj.bb
    bb.upsert_asset(pid, "domain", "a.com")                 # 根域名待挂树
    bb.upsert_asset(pid, "domain", "cdn.com")              # CDN：保持根行
    bb.upsert_asset(pid, "domain", "edge.com")             # CNAME 命中 CDN
    bb.upsert_asset(pid, "domain", "fake.com")            # fake-ip：只报跳过
    bb.upsert_asset(pid, "domain", "dead.com")            # 解析失败：不动
    # 有子根行显式 tested_clean（裸 UPDATE 模拟门控上线前的存量脏值——
    # 现行状态机不允许有子父节点标 clean，也无意图背书）
    dirty = bb.upsert_asset(pid, "host", "10.0.0.9")["id"]
    bb.conn.execute("UPDATE assets SET status='tested_clean' WHERE id=?",
                    (dirty,))
    bb.conn.commit()
    bb.upsert_asset(pid, "url", "http://10.0.0.9/", parent_id=dirty)
    proj.close()
    return store, slug


def _actions(plan):
    return {value: action for _, value, action in plan}


def test_plan_dry_run_no_writes(scene):
    """dry-run：计划含 reparent/reset-open/跳过，CDN/解析失败无条目；盘上零改动。"""
    store, slug = scene
    proj = store.open_project(slug)
    try:
        plan = rt.plan_project(proj, rt.DEFAULT_RESOLVER, CDN_LISTS)
    finally:
        proj.close()
    actions = _actions(plan)
    assert actions["a.com"] == "reparent → host:10.0.0.5"
    assert actions["10.0.0.9"].startswith("reset-open")
    assert "fake-ip" in actions["fake.com"]
    # CDN（CIDR / CNAME）与解析失败：不出现在计划
    assert "cdn.com" not in actions and "edge.com" not in actions
    assert "dead.com" not in actions

    # dry-run 不写：重开核对原始形态
    proj = store.open_project(slug)
    try:
        bb, pid = proj.bb, proj.id
        assert bb.find_asset(pid, "domain", "a.com")["parent_id"] is None
        assert bb.find_asset(pid, "host", "10.0.0.9")["status"] == "tested_clean"
    finally:
        proj.close()


def test_apply_writes_and_idempotent(scene):
    """--apply：落 2 条变体（reparent + reset-open），fake 计跳过；
    二次运行零变体（幂等），fake-ip 仍报提示但不写入。"""
    store, slug = scene
    done, skipped = rt.apply_project(store, slug, rt.DEFAULT_RESOLVER, CDN_LISTS)
    assert done == 2 and skipped == 1

    proj = store.open_project(slug)
    try:
        bb, pid = proj.bb, proj.id
        # a.com 已挂 host:10.0.0.5，host 带 primary_domain
        a = bb.find_asset(pid, "domain", "a.com")
        host = bb.find_asset(pid, "host", "10.0.0.5")
        assert host is not None and a["parent_id"] == host["id"]
        assert host["meta"]["primary_domain"] == "a.com"
        # 脏根行复位 open
        assert bb.find_asset(pid, "host", "10.0.0.9")["status"] == "open"
        # CDN / fake 根行形态保持
        assert bb.find_asset(pid, "domain", "cdn.com")["parent_id"] is None
        assert bb.find_asset(pid, "domain", "fake.com")["parent_id"] is None
        # 再跑一次：无 reparent/reset-open 变体
        again = rt.plan_project(proj, rt.DEFAULT_RESOLVER, CDN_LISTS)
    finally:
        proj.close()
    mutating = [a for a in again if "reparent" in a[2] or "reset-open" in a[2]]
    assert mutating == []
    assert any("fake-ip" in a[2] for a in again)  # 提示项仍报，零写入


def test_apply_author_marker(scene):
    """落库 author=demo-script(rebuild-trees)：状态事件可对账。"""
    store, slug = scene
    rt.apply_project(store, slug, rt.DEFAULT_RESOLVER, CDN_LISTS)
    proj = store.open_project(slug)
    try:
        bb, pid = proj.bb, proj.id
        ev = [e for e in bb.recent_events(pid)
              if e["kind"] == "asset.status_changed"]
        note = next(e["payload"]["note"] for e in ev
                    if e["payload"]["new"] == "open")
        assert "资产树重建" in note
    finally:
        proj.close()
