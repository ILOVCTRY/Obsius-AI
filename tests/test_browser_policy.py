"""F6 目标白名单（core/browser/policy.py）单测：纯逻辑零 playwright，恒跑。

资产用 register_asset 统一入口造（与生产同路径；测试对 DNS 必须 monkeypatch
断网——见 core/blackboard/CLAUDE.md 坑）。
"""

import socket

import pytest

from core.blackboard import Blackboard
from core.blackboard.assets import register_asset
from core.browser.policy import check_target, normalize_target


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def pid(bb, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("断网")))
    p = bb.create_project("白名单测试", "pentest", ["web"])
    return p["id"]


def _reg(bb, pid, value, type_="auto"):
    return register_asset(bb, pid, value, type_=type_, author="test")


# ---------- normalize_target ----------

def test_normalize_no_scheme():
    t = normalize_target("1.2.3.4:8080/admin")
    assert t.host == "1.2.3.4" and t.port == 8080 and t.scheme == "http"
    assert t.path == "/admin"


def test_normalize_host_lower_and_ipv6():
    t = normalize_target("http://[::1]:443/x")
    assert t.host == "::1" and t.port == 443 and t.is_ip
    t2 = normalize_target("http://Example.COM")
    assert t2.host == "example.com" and not t2.is_ip


# ---------- check_target ----------

def test_ip_host_asset_hit(pid, bb):
    _reg(bb, pid, "10.0.0.8")
    v = check_target(bb, pid, "http://10.0.0.8:8080/login")
    assert v.allowed and v.host == "10.0.0.8" and v.asset_id


def test_ip_not_registered_denied(pid, bb):
    v = check_target(bb, pid, "http://10.9.9.9")
    assert not v.allowed and "未登记" in v.reason and v.host == "10.9.9.9"


def test_domain_exact_and_subdomain(pid, bb):
    _reg(bb, pid, "target.example.com")
    assert check_target(bb, pid, "http://target.example.com/a").allowed
    # 子域随主域放行（默认 subdomain 档）
    assert check_target(bb, pid, "http://api.target.example.com").allowed
    # 未登记的其它主域拒绝
    assert not check_target(bb, pid, "http://evil.com").allowed


def test_domain_exact_scope(pid, bb, monkeypatch):
    _reg(bb, pid, "target.example.com")
    v = check_target(bb, pid, "http://api.target.example.com", domain_scope="exact")
    assert not v.allowed


def test_url_asset_hostname_hit(pid, bb):
    _reg(bb, pid, "http://only-url.example.com:9000/path")
    v = check_target(bb, pid, "http://only-url.example.com/other")
    assert v.allowed


def test_subdomain_suffix_depth_limit(pid, bb):
    # 只登记 example.com：a.b.example.com 逐级取父 ≤2 层可命中 example.com 需 2 步
    _reg(bb, pid, "example.com")
    assert check_target(bb, pid, "http://a.b.example.com").allowed
    # 3 层以上不再回溯（宁严勿松）
    assert not check_target(bb, pid, "http://x.a.b.example.com").allowed


def test_empty_host_denied(pid, bb):
    assert not check_target(bb, pid, "").allowed
