"""资产批量导入测试（cyberspace-mapping M1）：列映射嗅探、CSV/xlsx 解析、
归一化、import_assets 行路由（url 补登 domain / host IP 形态分流 / 域名+端点
双锚点）与汇总事件（quiet 静音逐行 asset.new）。

DNS 断网纪律（黑板 CLAUDE.md）：涉及真实域名的测试一律 monkeypatch
socket.getaddrinfo——hermetic，不因测试机 DNS 环境漂移。
"""

import io
import socket
import sys

import pytest

from core import assetimport as ai
from core.blackboard import Blackboard
from core.blackboard.assets import import_assets, register_asset


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def pid(bb):
    return bb.create_project("导入测试", "pentest", ["web"])["id"]


@pytest.fixture()
def no_dns(monkeypatch):
    def _off(*a, **k):
        raise OSError("dns off (hermetic)")
    monkeypatch.setattr(socket, "getaddrinfo", _off)


# ---------- 嗅探 ----------

def test_sniff_with_header_aliases():
    parsed = ai.parse_table("IP地址,端口,标题,指纹\n1.2.3.4,80,Hello,Nginx\n"
                            .encode("utf-8"), ".csv")
    assert parsed["header"] == ["IP地址", "端口", "标题", "指纹"]
    assert [c["kind"] for c in parsed["columns"]] == ["ip", "port", "title",
                                                      "products"]


def test_sniff_content_vote_without_header():
    rows = [["1.2.3.4", "80", "hello world"],
            ["5.6.7.8", "443", "welcome world"]]
    cols = ai.sniff_columns(None, rows)
    assert [c["kind"] for c in cols] == ["ip", "port", "title"]  # 兜底 title
    assert cols[2]["confidence"] == 0.5


def test_sniff_url_column_vote():
    rows = [["http://a.com/x", "80"], ["https://b.com/y", "443"]]
    cols = ai.sniff_columns(None, rows)
    assert cols[0]["kind"] == "url"
    assert cols[1]["kind"] == "port"


# ---------- parse_table ----------

def test_parse_csv_gbk_fallback():
    data = "IP地址,端口\n1.2.3.4,80\n".encode("gbk")
    parsed = ai.parse_table(data, ".csv")
    assert parsed["header"][0] == "IP地址"
    assert parsed["rows"][0] == ["1.2.3.4", "80"]


def test_parse_truncation_marker():
    data = "\n".join(f"1.2.3.{i},80" for i in range(1, 250)).encode()
    parsed = ai.parse_table(data, ".csv", max_rows=100)
    assert parsed["truncated"] is True
    assert parsed["total_rows"] == 100


def test_parse_rejects_unknown_suffix():
    with pytest.raises(ValueError):
        ai.parse_table(b"x", ".txt")


def test_parse_empty_rows_dropped():
    parsed = ai.parse_table("ip,port\n1.2.3.4,80\n,,\n".encode(), ".csv")
    assert parsed["total_rows"] == 1


def test_xlsx_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(ai.XlsxUnavailable):
        ai.parse_table(b"x", ".xlsx")


def test_parse_xlsx():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ip", "port", "title"])
    ws.append(["1.2.3.4", 80, "hello"])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = ai.parse_table(buf.getvalue(), ".xlsx")
    assert parsed["header"] == ["ip", "port", "title"]
    assert parsed["rows"][0] == ["1.2.3.4", "80", "hello"]  # 数字 → str


# ---------- normalize_rows ----------

def test_normalize_rows_and_skipped():
    rows = [["1.2.3.4", "80", "hello", "Nginx,Redis"],
            ["", "", "", ""],
            ["5.6.7.8", "443", "", ""]]
    out, skipped = ai.normalize_rows(rows, ["ip", "port", "title", "products"])
    assert skipped == 1
    assert out[0] == {"ip": "1.2.3.4", "port": "80", "title": "hello",
                      "products": ["Nginx", "Redis"]}
    assert out[1] == {"ip": "5.6.7.8", "port": "443"}


def test_normalize_multi_products_columns_merge():
    out, _ = ai.normalize_rows([["Nginx", "Redis"]], ["products", "products"])
    assert out[0]["products"] == ["Nginx", "Redis"]  # 多列指纹保序归并


def test_normalize_mapping_shorter_than_width():
    out, _ = ai.normalize_rows([["1.2.3.4", "80", "junk"]],
                               ["ip", "port"])  # 第三列按 ignore 补齐
    assert out[0] == {"ip": "1.2.3.4", "port": "80"}


# ---------- import_assets（行路由 + 事件） ----------

def test_import_assets_row_routing(bb, pid, no_dns):
    rows = [
        {"domain": "aaa.example.com"},
        {"url": "http://bbb.example.com/admin", "title": "B"},
        {"ip": "10.0.0.5", "port": "6379"},
        {"ip": "10.0.0.6"},
        {"host": "10.0.0.5:3306"},   # host 是 IP 形态带端口 → 与上面 10.0.0.5 合并
        {"host": "ccc.example.com", "ip": "10.0.0.7", "port": "80"},  # 双锚点
        {"title": "只有标题没有锚点"},  # skipped
    ]
    summary = import_assets(bb, pid, rows, "fofa")
    assert summary["source"] == "fofa"
    assert summary["total"] == 7
    assert summary["skipped"] == 1
    assert summary["failed_count"] == 0
    # created：aaa + host.5（host 组排序在 ip 组前，「10.0.0.5」先到先建）
    #          + (ccc 域名, svc.7, host.7) + (bbb 域名, url) + svc6379 + host.6 = 9
    # （自动挂载新建的 host 也计入；host/service 同值跨行只建一次）
    assert summary["created"] == 9
    assert summary["merged"] == 0
    values = {(a["type"], a["value"]) for a in bb.list_assets(pid)}
    assert ("domain", "aaa.example.com") in values
    assert ("domain", "bbb.example.com") in values  # url 行补登 domain
    assert ("url", "http://bbb.example.com/admin") in values
    assert ("service", "10.0.0.5:6379") in values
    assert ("host", "10.0.0.5") in values
    assert ("host", "10.0.0.6") in values
    assert ("domain", "ccc.example.com") in values
    assert ("service", "10.0.0.7:80") in values
    # 事件面：整批单条 asset.imported，无逐行 asset.new 刷屏
    kinds = [e["kind"] for e in bb.recent_events(pid, limit=500)]
    assert kinds.count("asset.imported") == 1
    assert "asset.new" not in kinds


def test_import_assets_merge_existing(bb, pid, no_dns):
    register_asset(bb, pid, "aaa.example.com", type_="domain")
    summary = import_assets(bb, pid, [{"domain": "aaa.example.com"}], "manual")
    assert summary["merged"] == 1
    assert summary["created"] == 0


def test_import_assets_failed_row(bb, pid, no_dns):
    summary = import_assets(bb, pid, [{"url": "example.com/no-proto"}], "csv")
    assert summary["failed_count"] == 1
    assert summary["failed"][0]["reason"].startswith("url 缺协议")
    assert summary["created"] == 0


def test_import_assets_batch_id_stable(bb, pid, no_dns):
    s1 = import_assets(bb, pid, [{"ip": "10.1.0.1"}], "manual")
    s2 = import_assets(bb, pid, [{"ip": "10.1.0.2"}], "manual")
    assert s1["batch_id"] != s2["batch_id"]
    assert s1["batch_id"].startswith("imp-")


def test_import_host_column_full_url_goes_url_path(bb, pid, no_dns):
    """host 列内容是完整 URL（xlsx 列映射错误的实战坑）：改走 url 路径——
    补登裸 domain + 登完整 url，不得把「https://x」落成 domain 脏行丢端点。"""
    summary = import_assets(
        bb, pid, [{"host": "https://ddd.example.com:8443/path"}], "xlsx")
    assert summary["failed_count"] == 0
    values = {(a["type"], a["value"]) for a in bb.list_assets(pid)}
    assert ("domain", "ddd.example.com") in values
    assert ("url", "https://ddd.example.com:8443/path") in values
    assert not any(t == "domain" and "://" in v for t, v in values)


def test_register_asset_domain_defensive_normalization(bb, pid, no_dns):
    """显式 domain 直传的防御性规范化（API/Agent 兜底）：剥 scheme/尾斜/端口尾部；
    归一后为空抛 ValueError。"""
    r1 = register_asset(bb, pid, "https://foo.example.com", type_="domain")
    assert r1["type"] == "domain" and r1["value"] == "foo.example.com"
    r2 = register_asset(bb, pid, "bar.example.com:8080/", type_="domain")
    assert r2["value"] == "bar.example.com"
    with pytest.raises(ValueError, match="规范化后为空"):
        register_asset(bb, pid, "https://", type_="domain")
    stored = {a["value"] for a in bb.list_assets(pid, type_="domain")}
    assert stored == {"foo.example.com", "bar.example.com"}
