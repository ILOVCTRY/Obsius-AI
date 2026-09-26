"""FOFA 中转客户端测试（cyberspace-mapping M2）：全部 fake transport 零触网。

覆盖：qbase64/fields/size·page clamp、行归一化 products 拆分、错误四态分类
（配额耗尽/key 错/base url 错立即停——不换端点重试；官方错误换备用）、
主备切换（网络错误/HTTP 非 200/非 JSON）、config 读写与 key 脱敏。
"""

import base64

import pytest

from core import fofa

FALLBACK = "http://107.173.248.139:18999"
MAIN = "https://fofoapi.com"


class _Boom(Exception):
    """模拟网络层异常（httpx.ConnectError 之流）。"""


class FakeResp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeTransport:
    """按调用序回放响应/异常；记录每次 get 的 url+params 供断言。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _search_resp(rows):
    return FakeResp(data={"error": False, "size": len(rows), "page": 1,
                          "results": rows})


def _err_resp(msg):
    return FakeResp(data={"error": True, "errmsg": msg})


def _client(transport):
    return fofa.FofaClient(MAIN, "key12345678", transport=transport)


# ---------- search：参数与归一化 ----------

def test_search_encodes_query_and_normalizes_rows():
    t = FakeTransport([_search_resp(
        [["1.2.3.4", "443", "tcp", "", "example.com", "首页", "Nginx, MySQL"]])])
    res = _client(t).search('domain="example.com"', size=100)
    call = t.calls[0]
    assert call["url"] == f"{MAIN}/api/v1/search/all"
    assert call["params"]["qbase64"] == base64.b64encode(
        'domain="example.com"'.encode()).decode()
    assert call["params"]["fields"] == "ip,port,protocol,host,domain,title,product"
    assert call["params"]["key"] == "key12345678"
    assert res["total"] == 1
    row = res["rows"][0]
    assert row["ip"] == "1.2.3.4"
    assert row["domain"] == "example.com"
    assert row["title"] == "首页"
    assert row["products"] == ["Nginx", "MySQL"]  # 拆分 + 去空格


def test_search_size_and_page_clamp():
    t = FakeTransport([_search_resp([]), _search_resp([])])
    c = _client(t)
    c.search("q", size=5000)          # 单次硬上限 1000
    c.search("q", size=100, page=999)  # 页×条≤1 万 → 页≤100
    assert t.calls[0]["params"]["size"] == 1000
    assert t.calls[1]["params"]["page"] == 100


def test_search_without_key_raises_config_error():
    with pytest.raises(fofa.ConfigError):
        fofa.FofaClient(MAIN, "").search("q")


# ---------- 错误四态：语义错误立即停（不换端点） ----------

def test_quota_exhausted_stops_immediately():
    t = FakeTransport([_err_resp("会员或F点已用完，请续费")])
    with pytest.raises(fofa.QuotaExhausted):
        _client(t).search("q")
    assert len(t.calls) == 1  # 防封号：绝不重试


def test_auth_error_stops_immediately():
    t = FakeTransport([_err_resp("key 不存在")])
    with pytest.raises(fofa.AuthError):
        _client(t).search("q")
    assert len(t.calls) == 1


def test_config_error_means_wrong_base_url():
    t = FakeTransport([_err_resp("账号无效")])
    with pytest.raises(fofa.ConfigError):
        _client(t).search("q")
    assert len(t.calls) == 1


def test_official_error_switches_to_fallback():
    t = FakeTransport([_err_resp("[官方错误信息] [-501] 服务错误,请稍候重试"),
                       _search_resp([["1.2.3.4", "80", "tcp", "", "", "t", ""]])])
    res = _client(t).search("q")
    assert t.calls[0]["url"].startswith(MAIN)
    assert t.calls[1]["url"].startswith(FALLBACK)
    assert res["rows"][0]["ip"] == "1.2.3.4"


def test_non_json_with_quota_signature_stops():
    t = FakeTransport([FakeResp(data=None, text="今日次数已用完")])
    with pytest.raises(fofa.QuotaExhausted):
        _client(t).search("q")
    assert len(t.calls) == 1


# ---------- 主备切换：网络错误 / HTTP 非 200 ----------

def test_network_error_switches_to_fallback():
    t = FakeTransport([_Boom("conn refused"), _search_resp([])])
    _client(t).search("q")
    assert len(t.calls) == 2
    assert t.calls[1]["url"].startswith(FALLBACK)


def test_http_500_switches_to_fallback():
    t = FakeTransport([FakeResp(status_code=500, data={}), _search_resp([])])
    _client(t).search("q")
    assert len(t.calls) == 2


def test_both_fail_raises_last_error():
    t = FakeTransport([_Boom("a"), _Boom("b")])
    with pytest.raises(fofa.FofaError):
        _client(t).search("q")
    assert len(t.calls) == 2


def test_unknown_error_msg_no_fallback():
    t = FakeTransport([_err_resp("奇怪的错误")])
    with pytest.raises(fofa.FofaError) as ei:
        _client(t).search("q")
    assert "奇怪的错误" in str(ei.value)
    assert len(t.calls) == 1


# ---------- info_my（免费） ----------

def test_info_my_nested_fofa_info():
    t = FakeTransport([FakeResp(data={"error": False, "fofa_info": {
        "fofa_num": 1234, "expire_time": "2026-12-31"}})])
    info = _client(t).info_my()
    assert info["remain"] == 1234
    assert info["expire"] == "2026-12-31"


def test_info_my_top_level_lenient():
    t = FakeTransport([FakeResp(data={"error": False, "fofa_num": 7})])
    info = _client(t).info_my()
    assert info["remain"] == 7
    assert info["expire"] is None


# ---------- config 与脱敏 ----------

def test_load_missing_or_broken_returns_empty(tmp_path):
    assert fofa.load_fofa_config(tmp_path / "nope.json") == {}
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    assert fofa.load_fofa_config(p) == {}


def test_config_roundtrip(tmp_path):
    p = tmp_path / "fofa.json"
    fofa.save_fofa_config(p, {"base_url": "https://x.example", "key": "k" * 32})
    cfg = fofa.load_fofa_config(p)
    assert cfg["key"] == "k" * 32
    assert cfg["base_url"] == "https://x.example"


def test_from_config_defaults(tmp_path):
    p = tmp_path / "fofa.json"
    fofa.save_fofa_config(p, {"key": "kk1234567890"})
    c = fofa.FofaClient.from_config(p)
    assert c.key == "kk1234567890"
    assert c.base_url == fofa.DEFAULT_BASE_URL
    assert c.configured


def test_mask_key():
    assert fofa.mask_key("abcd1234efgh5678") == "abcd********5678"
    assert fofa.mask_key("short") == "*****"
    assert fofa.mask_key("") == ""
