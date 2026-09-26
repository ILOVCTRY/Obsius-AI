"""CDN/共享托管判定测试（asset-tree-derived-clean M1，方案 §4 ①-⑤）。

覆盖：CIDR/CNAME 命中、拿不准默认非 CDN、meta 人工覆盖、CDN 域名登记
保持根行、DNS 漂移重挂+asset.reparent 事件；清单文件合并与坏文件 fail-fast。
register 路径全部 monkeypatch DNS，零触网。
"""

import json

import pytest

from core.blackboard import assets as am
from core.blackboard.assets import register_asset
from core.blackboard.cdn import CdnLists, is_cdn, load_cdn_lists


@pytest.fixture()
def bb_env(tmp_path):
    from core.blackboard import Blackboard
    board = Blackboard(str(tmp_path / "cdn.db"))
    project = board.create_project("CDN测试", "pentest", ["web"])
    yield board, project
    board.close()


# ---------- ① CIDR / CNAME 后缀命中 ----------

def test_cdn_lists_ip_cidr_hit():
    lists = CdnLists(["104.16.0.0/13", "203.0.113.0/24"], [])
    assert lists.ip_hit("104.16.5.5") is True
    assert lists.ip_hit("104.23.255.255") is True  # /13 末端
    assert lists.ip_hit("104.24.0.1") is False
    assert lists.ip_hit("8.8.8.8") is False
    # 非法 IP / 空白：不命中（不抛）
    assert lists.ip_hit("not-an-ip") is False


def test_cdn_lists_cname_suffix_hit():
    lists = CdnLists([], ["cloudfront.net", "myqcloud.com"])
    assert lists.cname_hit("d1234.cloudfront.net") is True
    assert lists.cname_hit("cloudfront.net") is True            # 精确命中
    assert lists.cname_hit("x.kunlun.myqcloud.com") is True
    assert lists.cname_hit("x.cloudfront.net.evil.com") is False  # 只认后缀
    assert lists.cname_hit("notcloudfront.net") is False
    assert lists.cname_hit(None) is False
    # 前导点 / 尾点 / 大小写归一
    assert lists.cname_hit("D123.CloudFront.NET.") is True


# ---------- ② 拿不准默认非 CDN ----------

def test_uncertain_defaults_non_cdn():
    empty = CdnLists([], [])
    assert is_cdn("203.0.113.9", cdn_lists=empty) is False
    assert is_cdn(None, "orphan.example", cdn_lists=empty) is False
    # CNAME 给了但不命中清单：不并
    assert is_cdn("203.0.113.9", cname="host.unknown-cdn.net",
                  cdn_lists=empty) is False


# ---------- ③ 人工 meta 覆盖（最高优先） ----------

def test_meta_manual_override():
    lists = CdnLists(["104.16.0.0/13"], ["cloudfront.net"])
    # 私 IP 也可人工标 CDN
    assert is_cdn("192.168.1.1", meta={"cdn": True}, cdn_lists=lists) is True
    # 命中 CIDR 也可人工否决
    assert is_cdn("104.16.0.1", cname="x.cloudfront.net",
                  meta={"cdn": False}, cdn_lists=lists) is False
    # meta.cdn=None / 缺省 → 落回信号判定
    assert is_cdn("104.16.0.1", meta={"cdn": None}, cdn_lists=lists) is True


# ---------- ⑤ CDN 域名登记保持根行 ----------

def test_register_cdn_domain_stays_root(bb_env, monkeypatch):
    bb, proj = bb_env
    pid = proj["id"]
    monkeypatch.setattr(am.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("104.16.1.1", 0))])
    r = register_asset(bb, pid, "cdn.example.com", cdn_lists=__real_lists())
    assert r["cdn"] is True
    assert bb.get_asset(r["id"])["parent_id"] is None   # 不挂 host
    assert bb.find_asset(pid, "host", "104.16.1.1") is None  # 不造 host 行
    # 非 CDN 对照：同环境私有 IP 正常挂树
    monkeypatch.setattr(am.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("203.0.113.5", 0))])
    r2 = register_asset(bb, pid, "site.example.com", cdn_lists=__real_lists())
    assert r2["cdn"] is False and r2["host_id"]
    assert bb.get_asset(r2["id"])["parent_id"] == r2["host_id"]


def __real_lists() -> CdnLists:
    """基线清单（测试用 repo 文件，无 config 增补依赖）。"""
    from core.blackboard.cdn import _BASELINE
    return load_cdn_lists(_BASELINE, None)


# ---------- ④ DNS 漂移重挂 + 事件 ----------

def test_register_dns_drift_reparent_event(bb_env, monkeypatch):
    bb, proj = bb_env
    pid = proj["id"]
    empty = CdnLists([], [])
    monkeypatch.setattr(am.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.1", 0))])
    r1 = register_asset(bb, pid, "drift.example.com", cdn_lists=empty)
    assert bb.get_asset(r1["id"])["parent_id"] == r1["host_id"]

    # 重报解析漂移到 10.0.0.2 → set_asset_parent 改挂 + asset.reparent 事件
    monkeypatch.setattr(am.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.2", 0))])
    r2 = register_asset(bb, pid, "drift.example.com", cdn_lists=empty)
    assert r2["id"] == r1["id"] and not r2["created"]
    row = bb.get_asset(r1["id"])
    assert row["parent_id"] == r2["host_id"] and r2["host_id"] != r1["host_id"]
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "asset.reparent"]
    assert len(ev) == 1
    assert ev[0]["payload"]["old_parent_id"] == r1["host_id"]
    assert ev[0]["payload"]["parent_id"] == r2["host_id"]

    # 漂移到 CDN（meta 人工标 cdn）→ 摘挂为根行
    r3 = register_asset(bb, pid, "drift.example.com",
                        meta={"cdn": True}, cdn_lists=empty)
    assert bb.get_asset(r1["id"])["parent_id"] is None


# ---------- 清单文件：合并增补 / 坏文件 fail-fast ----------

def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_load_lists_merge_baseline_and_config(tmp_path):
    base = tmp_path / "base.json"
    cfg = tmp_path / "cdn.json"
    _write(base, {"cidr": ["104.16.0.0/13"], "cname_suffixes": ["cloudfront.net"]})
    _write(cfg, {"cidr": ["198.51.100.0/24"], "cname_suffixes": ["mycdn.example"]})
    lists = load_cdn_lists(base, cfg)
    assert lists.ip_hit("198.51.100.7") and lists.ip_hit("104.16.0.1")
    assert lists.cname_hit("a.mycdn.example") and lists.cname_hit("x.cloudfront.net")
    # config 缺失（打包形态）：只用基线
    lists2 = load_cdn_lists(base, tmp_path / "missing.json")
    assert lists2.ip_hit("104.16.0.1") and not lists2.ip_hit("198.51.100.7")


def test_load_lists_bad_file_raises(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_cdn_lists(bad_json, None)
    bad_struct = tmp_path / "struct.json"
    _write(bad_struct, ["不是对象"])
    with pytest.raises(ValueError, match="结构非法"):
        load_cdn_lists(bad_struct, None)
    bad_arrays = tmp_path / "arrays.json"
    _write(bad_arrays, {"cidr": "104.16.0.0/13"})
    with pytest.raises(ValueError, match="数组"):
        load_cdn_lists(bad_arrays, None)
    bad_cidr = tmp_path / "cidr.json"
    _write(bad_cidr, {"cidr": ["不是 CIDR"]})
    with pytest.raises(ValueError):
        load_cdn_lists(bad_cidr, None)
