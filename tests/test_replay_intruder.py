"""F6 重发/爆破引擎单测（core/browser/replay.py，纯 httpx 零 playwright，恒跑）。

本地 http.server 起真实回环服务（127.0.0.1 随机端口），资产白名单用
register_asset 登记回环 IP。
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from core.blackboard import Blackboard
from core.blackboard.assets import register_asset
from core.browser.pool import BrowserConfig, BrowserError
from core.browser.replay import Intruder, ReplayClient


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


# ---------- httpmsg（F6-v3 原始报文解析/渲染，重放与拦截共用） ----------

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


# ---------- ReplayClient（F6-v3：raw 原始报文 / capture_id 模板） ----------

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


def test_replay_missing_capture(bb, pid):
    rc = ReplayClient(bb, config=BrowserConfig())
    with pytest.raises(ValueError):
        rc.replay(pid, capture_id=99999)


def test_replay_denied_unregistered_target(bb, pid, server):
    rc = ReplayClient(bb, config=BrowserConfig())
    with pytest.raises(BrowserError):
        rc.replay(pid, raw=f"GET http://203.0.113.5/echo HTTP/1.1\nHost: x\n\n")
    kinds = [e["kind"] for e in bb.recent_events(pid)]
    assert "browser.deny" in kinds


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


def test_intruder_denied_unregistered(bb, pid):
    intr = Intruder(bb, config=BrowserConfig())
    with pytest.raises(BrowserError):
        intr.run(pid, {"method": "GET", "url": "http://203.0.113.9/?u=§U§"},
                 [{"position": "U", "type": "list", "values": ["a"]}],
                 batch_id="in-t4")


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
