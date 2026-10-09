"""F6 重发/爆破引擎单测（core/browser/replay.py，纯 httpx 零 playwright，恒跑）。

本地 http.server 起真实回环服务（127.0.0.1 随机端口），资产白名单用
register_asset 登记回环 IP。
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from core.blackboard import Blackboard
from core.blackboard.assets import register_asset
from core.browser.gmhttp import gm_available
from core.browser.pool import BrowserConfig, BrowserError
from core.browser.replay import Intruder, ReplayClient, ReplayOptions


def test_browser_config_ignore_https_default_and_override(tmp_path):
    """证书错误开关：默认放行（2026-09-24 用户定稿），config 可显式关回严格。"""
    assert BrowserConfig().ignore_https_errors is True
    cfg_path = tmp_path / "browser.json"
    cfg_path.write_text(json.dumps({"ignore_https_errors": False}), encoding="utf-8")
    assert BrowserConfig.from_file(cfg_path).ignore_https_errors is False
    cfg_path.write_text(json.dumps({"ignore_https_errors": True}), encoding="utf-8")
    assert BrowserConfig.from_file(cfg_path).ignore_https_errors is True


@pytest.fixture()
def server():
    """回环 echo 服务：/echo 回显 method/path/query；/login 对 admin/123456 返回 200。"""

    class H(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/echo":
                self._reply(200, {"method": "GET", "path": u.path,
                                  "query": {k: v for k, v in parse_qs(u.query).items()}})
            elif u.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/echo")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif u.path == "/slow":
                time.sleep(1.2)
                self._reply(200, {"slow": True})
            elif u.path == "/big":
                self._reply(200, {"pad": "x" * 400})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            u = urlparse(self.path)
            if u.path == "/login":
                q = parse_qs(raw.decode("utf-8", "replace"))
                ok = q.get("user", [""])[0] == "admin" and q.get("pass", [""])[0] == "123456"
                self._reply(200 if ok else 403, {"ok": ok})
            else:
                self._reply(200, {"method": "POST", "path": u.path,
                                  "body": raw.decode("utf-8", "replace")})

        def log_message(self, *a):  # 静默
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


_real_getaddrinfo = socket.getaddrinfo


def _loopback_only_getaddrinfo(host, *args, **kwargs):
    """DNS 断网（黑名单纪律），但放行回环字面量——httpx 连 127.0.0.1 也要走解析。"""
    if host in ("127.0.0.1", "localhost", "::1"):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise OSError("断网")


@pytest.fixture()
def bb(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _loopback_only_getaddrinfo)
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def pid(bb):
    p = bb.create_project("重放测试", "pentest", ["web"])
    pid = p["id"]
    register_asset(bb, pid, "127.0.0.1", author="test")
    return pid


# ---------- httpmsg（F6-v4 原始报文解析/渲染，重放与拦截共用） ----------

class TestHttpMsg:
    def test_parse_request_absolute_url(self):
        from core.browser.httpmsg import parse_raw_request
        raw = ("POST http://127.0.0.1:9/api/x HTTP/1.1\n"
               "Host: 127.0.0.1:9\n"
               "Cookie: a=1\nCookie: b=2\n"
               "\n"
               "user=admin&pass=123")
        p = parse_raw_request(raw)
        assert p["method"] == "POST"
        assert p["url"] == "http://127.0.0.1:9/api/x"
        assert p["headers"]["Cookie"] == "a=1, b=2"
        assert p["body"] == "user=admin&pass=123"
        assert p["is_binary"] is False

    def test_parse_request_relative_with_base_url(self):
        from core.browser.httpmsg import parse_raw_request
        p = parse_raw_request("GET /echo?a=1 HTTP/1.1\nHost: x\n\n",
                              base_url="http://127.0.0.1:9/root")
        assert p["url"] == "http://127.0.0.1:9/echo?a=1"

    def test_parse_request_relative_host_fallback(self):
        """origin-form 报文（相对路径 + Host 头）无 base_url 时用 Host 兜底。"""
        from core.browser.httpmsg import parse_raw_request
        p = parse_raw_request(
            "POST /gemini/x.do HTTP/1.1\n"
            "Host: i.zut.edu.cn\n"
            "Content-Type: application/x-www-form-urlencoded\n"
            "\n"
            "ACCOUNTID=admin")
        assert p["url"] == "http://i.zut.edu.cn/gemini/x.do"
        assert p["method"] == "POST"
        assert p["body"] == "ACCOUNTID=admin"

    def test_parse_request_host_fallback_case_insensitive(self):
        from core.browser.httpmsg import parse_raw_request
        p = parse_raw_request("GET /a HTTP/1.1\nhost: 127.0.0.1:9\n\n")
        assert p["url"] == "http://127.0.0.1:9/a"

    def test_parse_request_base_url_wins_over_host(self):
        """有模板 base_url 时以模板为准（Host 头不参与，保持既有改包语义）。"""
        from core.browser.httpmsg import parse_raw_request
        p = parse_raw_request("GET /echo HTTP/1.1\nHost: other:1\n\n",
                              base_url="http://127.0.0.1:9/root")
        assert p["url"] == "http://127.0.0.1:9/echo"

    def test_parse_request_relative_no_host_no_base(self):
        """相对路径既无 Host 头也无 base_url → 仍报错（不猜目标）。"""
        from core.browser.httpmsg import parse_raw_request
        with pytest.raises(ValueError):
            parse_raw_request("GET /a HTTP/1.1\nAccept: */*\n\n")

    def test_parse_request_bad_lines(self):
        from core.browser.httpmsg import parse_raw_request
        with pytest.raises(ValueError):
            parse_raw_request("")  # 空报文
        with pytest.raises(ValueError):
            parse_raw_request("GETonly\n\n")  # 请求行缺 URL
        with pytest.raises(ValueError):
            parse_raw_request("GET / HTTP/1.1\nBadHeaderNoColon\n\n")

    def test_parse_response(self):
        from core.browser.httpmsg import parse_raw_response
        p = parse_raw_response("HTTP/1.1 404 Not Found\nContent-Type: text/plain\n\nnope")
        assert p["status"] == 404 and p["body"] == "nope"
        with pytest.raises(ValueError):
            parse_raw_response("HTTP/1.1 abc\n\n")

    def test_render_binary_placeholder(self):
        from core.browser.httpmsg import render_raw_request, BINARY_PLACEHOLDER_RE
        raw, editable = render_raw_request("POST", "http://x/", {"Host": "x"}, b"\xff\xfe\x01")
        assert editable is False and BINARY_PLACEHOLDER_RE.match(raw.split("\n\n")[-1])


# ---------- ReplayClient（F6-v4：raw 原始报文 / capture_id 模板） ----------

def test_replay_raw_get_and_history(bb, pid, server):
    rc = ReplayClient(bb, config=BrowserConfig())
    raw = f"GET {server}/echo?a=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    row = rc.replay(pid, raw=raw, author="human")
    assert row["status"] == 200 and row["source"] == "replay"
    assert json.loads(row["resp_body"])["query"] == {"a": ["1"]}
    assert row["meta"]["modified"] is True
    hist = bb.list_http_history(pid, source="replay")
    assert len(hist) == 1 and hist[0]["batch_id"] == row["batch_id"]


def test_replay_template_with_raw_override(bb, pid, server):
    """capture_id 模板 + raw 相对路径改写（base_url 用模板 url 兜底）。"""
    cid = bb.add_http_history(pid, source="browser", method="GET",
                              url=f"{server}/echo?x=0", status=200)
    rc = ReplayClient(bb, config=BrowserConfig())
    row = rc.replay(pid, capture_id=cid)  # 原样重放
    assert row["meta"]["modified"] is False and row["meta"]["replayed_from"] == cid
    row2 = rc.replay(pid, capture_id=cid,
                     raw="GET /echo?x=2 HTTP/1.1\nHost: x\n\n")
    assert row2["meta"]["modified"] is True
    assert json.loads(row2["resp_body"])["query"] == {"x": ["2"]}


def test_replay_raw_bad_message(bb, pid):
    rc = ReplayClient(bb, config=BrowserConfig())
    with pytest.raises(BrowserError, match="原始报文解析失败"):
        rc.replay(pid, raw="GETonly\n\n")


def test_replay_recomputes_content_length(bb, pid, server):
    """报文 Content-Length 与实际 body 不符（改包常态）→ 剥 framing 头由客户端重算，
    不再报「Too little data for declared Content-Length」（2026-10-07）。"""
    rc = ReplayClient(bb, config=BrowserConfig())
    raw = (f"POST {server}/login HTTP/1.1\r\n"
           "Host: 127.0.0.1\r\n"
           "Content-Type: application/x-www-form-urlencoded\r\n"
           "Content-Length: 999\r\n"        # 故意大于实际 body（22 字节）
           "\r\n"
           "user=admin&pass=123456")
    row = rc.replay(pid, raw=raw, author="human")
    assert row["status"] == 200
    assert json.loads(row["resp_body"])["ok"] is True
    # 入库 headers 剥掉 framing 头（记录实际发出的请求，与 capture._store 同口径）
    keys = {k.lower() for k in (row.get("req_headers") or {})}
    assert "content-length" not in keys
    assert "transfer-encoding" not in keys


def test_replay_ignores_system_proxy(bb, pid, server, monkeypatch):
    """重放流量不交系统代理（trust_env=False，2026-09-23）：假代理指向死端口，
    直连本地 server 应照常成功——若 httpx 回落 trust_env=True 会走假代理而失败。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    rc = ReplayClient(bb, config=BrowserConfig())
    row = rc.replay(pid, raw=f"GET {server}/echo?p=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
                    author="human")
    assert row["status"] == 200
    assert json.loads(row["resp_body"])["query"] == {"p": ["1"]}


def test_replay_missing_capture(bb, pid):
    rc = ReplayClient(bb, config=BrowserConfig())
    with pytest.raises(ValueError):
        rc.replay(pid, capture_id=99999)


# ---------- 重发传输选项（2026-10-07 重放工作台，全量对齐 Yakit 式 Repeater 控件） ----------

def test_replay_options_defaults_match_legacy_behavior():
    """默认值必须等价于 2026-10-07 之前的硬编码行为（跟随重定向 / 无代理 / httpx）。"""
    o = ReplayOptions()
    assert o.force_https is False
    assert o.follow_redirects is True
    assert o.proxy is None and o.body_max_bytes is None
    assert o.insecure is False and o.gm_tls is False
    assert o.timeout_s == 15.0


def test_force_https_rewrites_only_plain_http():
    from core.browser.replay import _apply_force_https
    assert _apply_force_https("http://a/b") == "https://a/b"
    assert _apply_force_https("https://a/b") == "https://a/b"   # 已 https 不动
    assert _apply_force_https("ftp://a/b") == "ftp://a/b"       # 其它 scheme 不动


def test_replay_force_https_upgrades_request_line(bb, pid, server):
    """强制HTTPS：请求行 http:// 升级为 https://（打回环明文服务必失败，但入库 url 已是 https）。"""
    rc = ReplayClient(bb, config=BrowserConfig())
    with pytest.raises(BrowserError):
        rc.replay(pid, raw=f"GET {server}/echo HTTP/1.1\nHost: x\n\n",
                  opts=ReplayOptions(force_https=True, timeout_s=3))
    rows = bb.list_http_history(pid, source="replay")
    assert rows and rows[0]["url"].startswith("https://")


def test_replay_follow_redirects_toggle(bb, pid, server):
    rc = ReplayClient(bb, config=BrowserConfig())
    raw = f"GET {server}/redirect HTTP/1.1\nHost: 127.0.0.1\n\n"
    followed = rc.replay(pid, raw=raw, opts=ReplayOptions(follow_redirects=True))
    assert followed["status"] == 200
    not_followed = rc.replay(pid, raw=raw, opts=ReplayOptions(follow_redirects=False))
    assert not_followed["status"] == 302


def test_replay_body_max_override(bb, pid, server):
    rc = ReplayClient(bb, config=BrowserConfig())
    row = rc.replay(pid, raw=f"GET {server}/big HTTP/1.1\nHost: 127.0.0.1\n\n",
                    opts=ReplayOptions(body_max_bytes=16))
    assert row["body_truncated"] is True
    assert len(row["resp_body"]) <= 16


def test_replay_explicit_proxy_is_used(bb, pid, server):
    """显式代理（非系统代理）：指向死端口应失败——证明 proxy 参数确实生效。"""
    rc = ReplayClient(bb, config=BrowserConfig())
    with pytest.raises(BrowserError):
        rc.replay(pid, raw=f"GET {server}/echo HTTP/1.1\nHost: 127.0.0.1\n\n",
                  opts=ReplayOptions(proxy="http://127.0.0.1:1", timeout_s=3))
    rows = bb.list_http_history(pid, source="replay")
    assert rows and rows[0]["meta"].get("proxy") == "http://127.0.0.1:1"


def test_replay_stop_event_interrupts_and_records(bb, pid, server):
    """停止：慢响应期间置 stop_event → 中断并落失败行（status=None）。"""
    rc = ReplayClient(bb, config=BrowserConfig())
    stop = threading.Event()
    timer = threading.Timer(0.4, stop.set)
    timer.start()
    t0 = time.monotonic()
    try:
        with pytest.raises(BrowserError, match="中断"):
            rc.replay(pid, raw=f"GET {server}/slow HTTP/1.1\nHost: 127.0.0.1\n\n",
                      opts=ReplayOptions(timeout_s=10), stop_event=stop)
    finally:
        timer.cancel()
    assert time.monotonic() - t0 < 5      # 未被慢响应拖满
    rows = bb.list_http_history(pid, source="replay")
    assert rows and rows[0]["status"] is None


# ---------- 国密 sidecar（gmhttp） ----------

def test_gmhttp_unavailable_without_binary(tmp_path):
    from core.browser import gmhttp
    assert gmhttp.resolve_gmhttp(tmp_path) is None
    assert gmhttp.gm_available(tmp_path) is False
    with pytest.raises(gmhttp.GmHttpError, match="不可用"):
        gmhttp.gm_request({"method": "GET", "url": "https://x"}, tools_root=tmp_path)


def test_replay_gm_without_sidecar_stores_failure(bb, pid, tmp_path):
    """国密不可用 → 明确报错 + 落失败行，**绝不静默降级成普通 TLS**。"""
    rc = ReplayClient(bb, config=BrowserConfig(), tools_root=str(tmp_path))
    with pytest.raises(BrowserError, match="重放请求失败"):
        rc.replay(pid, raw="GET https://127.0.0.1:9/ HTTP/1.1\nHost: x\n\n",
                  opts=ReplayOptions(gm_tls=True, timeout_s=3))
    rows = bb.list_http_history(pid, source="replay")
    assert rows and rows[0]["status"] is None
    assert rows[0]["meta"]["gm_tls"] is True


@pytest.mark.skipif(not gm_available(), reason="gmhttp sidecar 未构建（跑 scripts/build_gmhttp.py）")
def test_gm_sidecar_end_to_end_plain_http(bb, pid, server):
    """真 sidecar 端到端跑通进程协议（stdin JSON → stdout JSON）。
    打回环明文 http 不触发 GM 握手（DialTLSContext 只用于 https），故必成功——
    本用例验的是「协议/进程链路可用」，非 GM 握手本身（无国密服务端可测）。"""
    rc = ReplayClient(bb, config=BrowserConfig())
    row = rc.replay(pid, raw=f"GET {server}/echo?gm=1 HTTP/1.1\nHost: 127.0.0.1\n\n",
                    opts=ReplayOptions(gm_tls=True, timeout_s=10))
    assert row["status"] == 200
    assert json.loads(row["resp_body"])["query"] == {"gm": ["1"]}
    assert row["meta"]["gm_tls"] is True


# ---------- Intruder ----------

def test_intruder_marker_missing_rejected(bb, pid, server):
    intr = Intruder(bb, config=BrowserConfig())
    with pytest.raises(ValueError):
        intr.run(pid, {"method": "GET", "url": f"{server}/echo"}, [],
                 batch_id="in-x")


def test_intruder_runs_and_groups_by_batch(bb, pid, server):
    intr = Intruder(bb, config=BrowserConfig())
    users = ["admin", "root", "test"]
    summary = intr.run(
        pid, {"method": "GET", "url": f"{server}/echo?u=§U§"},
        [{"position": "U", "type": "list", "values": users}],
        batch_id="in-t1", concurrency=3, rate_per_sec=50)
    assert summary == {"batch_id": "in-t1", "total": 3, "done": 3,
                       "failed": 0, "stopped": False}
    rows = bb.list_http_history(pid, batch_id="in-t1")
    assert len(rows) == 3 and all(r["source"] == "intruder" for r in rows)
    payloads = sorted(r["meta"]["payload"]["U"] for r in rows)
    assert payloads == ["admin", "root", "test"]
    # 批次级审计
    kinds = [e["kind"] for e in bb.recent_events(pid)]
    assert "browser.intruder.start" in kinds and "browser.intruder.done" in kinds


def test_intruder_proxy_recorded_in_meta_and_event(bb, pid, server):
    """爆破显式代理（2026-10-07 放开红线）：proxy 进 start 事件与每行 meta.proxy。
    指向死端口 → 连接失败，正好证明 proxy 参数确实生效。"""
    intr = Intruder(bb, config=BrowserConfig())
    summary = intr.run(
        pid, {"method": "GET", "url": f"{server}/echo?u=§U§"},
        [{"position": "U", "type": "list", "values": ["a"]}],
        batch_id="in-px", concurrency=1, rate_per_sec=50,
        proxy="http://127.0.0.1:1")
    assert summary["total"] == 1 and summary["failed"] == 1  # 死代理 → 失败
    rows = bb.list_http_history(pid, batch_id="in-px")
    assert rows and rows[0]["meta"].get("proxy") == "http://127.0.0.1:1"
    starts = [e for e in bb.recent_events(pid, kinds=["browser.intruder.start"])]
    assert starts and starts[-1]["payload"].get("proxy") == "http://127.0.0.1:1"


def test_intruder_range_and_two_markers(bb, pid, server):
    intr = Intruder(bb, config=BrowserConfig())
    summary = intr.run(
        pid, {"method": "GET", "url": f"{server}/echo?a=§A§&b=§B§"},
        [{"position": "A", "type": "range", "start": 1, "stop": 3},
         {"position": "B", "type": "list", "values": ["x", "y"]}],
        batch_id="in-t2", concurrency=4, rate_per_sec=100)
    assert summary["total"] == 4  # 2×2 笛卡尔积
    rows = bb.list_http_history(pid, batch_id="in-t2")
    urls = {r["url"] for r in rows}
    assert f"{server}/echo?a=1&b=x" in urls


def test_intruder_max_requests_cap(bb, pid, server):
    intr = Intruder(bb, config=BrowserConfig())
    big = [str(i) for i in range(50)]
    summary = intr.run(
        pid, {"method": "GET", "url": f"{server}/echo?u=§U§"},
        [{"position": "U", "type": "list", "values": big}],
        batch_id="in-t3", max_requests=5, rate_per_sec=100)
    assert summary["total"] == 5


def test_intruder_stop_event(bb, pid, server):
    intr = Intruder(bb, config=BrowserConfig())
    stop = threading.Event()
    seen = {"count": 0}

    def progress(done, total):
        seen["count"] = done
        if done >= 3:
            stop.set()

    values = [str(i) for i in range(40)]
    summary = intr.run(
        pid, {"method": "GET", "url": f"{server}/echo?u=§U§"},
        [{"position": "U", "type": "list", "values": values}],
        batch_id="in-t5", concurrency=2, rate_per_sec=100,
        stop_event=stop, progress_cb=progress)
    assert summary["stopped"] is True and summary["done"] < 40


def test_intruder_consecutive_failures_stop(bb, pid, server):
    # 未监听端口：连续连接失败 ≥10 触发自动停止
    dead = f"http://127.0.0.1:1"
    intr = Intruder(bb, config=BrowserConfig())
    summary = intr.run(
        pid, {"method": "GET", "url": f"{dead}/?u=§U§"},
        [{"position": "U", "type": "list", "values": [str(i) for i in range(30)]}],
        batch_id="in-t6", concurrency=1, rate_per_sec=100)
    assert summary["stopped"] is True and summary["failed"] >= 10


# ---------- DNS 解析失败识别（2026-10-08） ----------

def test_dns_failure_detection():
    """_dns_failure：socket.gaierror / 异常链 / 已知文案三路都识别；_host_of 取主机。"""
    from core.browser.replay import _dns_failure, _host_of

    assert _dns_failure(socket.gaierror(11002, "getaddrinfo failed"))
    assert _dns_failure(RuntimeError("[Errno 11002] getaddrinfo failed"))  # 文案兜底
    try:
        try:
            raise socket.gaierror(11002, "getaddrinfo failed")
        except socket.gaierror as inner:
            raise RuntimeError("connect failed") from inner
    except RuntimeError as exc:
        assert _dns_failure(exc)                                  # 异常链
    assert not _dns_failure(RuntimeError("connection refused"))
    assert _host_of("http://a.b:8080/x") == "a.b"


def test_replay_dns_failure_clear_message(bb, pid, monkeypatch):
    """DNS 解析失败 → BrowserError 文案点明「域名解析失败」，失败行仍入库。"""
    rc = ReplayClient(bb)

    def boom(*_a, **_k):
        raise httpx.ConnectError("[Errno 11002] getaddrinfo failed")

    monkeypatch.setattr(rc, "_execute", boom)
    with pytest.raises(BrowserError, match="域名解析失败"):
        rc.replay(pid, raw="GET /x HTTP/1.1\nHost: opsg-gateway-in.oppo.com\n\n")
    rows = bb.list_http_history(pid, source="replay")
    assert rows and rows[0]["status"] is None

