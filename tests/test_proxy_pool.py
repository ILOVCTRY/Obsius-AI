"""代理池测试（fir-proxy 托管）。

覆盖：ProxyPool 生命周期 + runner 控制通道（status/list/rotate/reload/stop）、
端到端真实转发（本地 HTTP 靶站 ← 假 SOCKS5 上游 ← 池的 HTTP 入口）、纯逻辑
（select_records / 池文件去重）、API 端点。**不触外网**——上游用测试内建的
极简 SOCKS5 中继，靶站是本地 http.server。
"""

import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.proxy import ProxyConfig, ProxyPool, proxy_available, select_records
from core.proxy.pool import _is_usable, _prune

TOOLS = str(Path(__file__).resolve().parent.parent / "tools")

pytestmark = pytest.mark.skipif(
    not proxy_available(TOOLS)[0],
    reason="fir-proxy 或解释器依赖（requests[socks]/bs4/lxml）不可用")


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def target_server():
    """回环靶站：任何请求都回 PROXY-OK。"""
    body = b"PROXY-OK"

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()
    srv.server_close()


class _FakeSocks5(threading.Thread):
    """极简 SOCKS5 上游：只支持 CONNECT，纯中继（测试用，无认证）。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv_exact(s, n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _handle(self, conn):
        try:
            _ver, n = self._recv_exact(conn, 2)
            self._recv_exact(conn, n)
            conn.sendall(b"\x05\x00")
            _ver, cmd, _rsv, atyp = self._recv_exact(conn, 4)
            if atyp == 1:
                host = socket.inet_ntoa(self._recv_exact(conn, 4))
            elif atyp == 3:
                ln = self._recv_exact(conn, 1)[0]
                host = self._recv_exact(conn, ln).decode()
            else:
                raise ValueError("atyp")
            port = struct.unpack("!H", self._recv_exact(conn, 2))[0]
            upstream = socket.create_connection((host, port), timeout=5)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            self._relay(conn, upstream)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _relay(a, b):
        def pump(x, y):
            try:
                while True:
                    data = x.recv(8192)
                    if not data:
                        break
                    y.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    y.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(a, b), daemon=True)
        t.start()
        pump(b, a)
        t.join(timeout=2)

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture()
def socks5_upstream():
    up = _FakeSocks5()
    up.start()
    yield up
    up.close()


@pytest.fixture()
def pool(tmp_path):
    p = ProxyPool(tmp_path / "ws", TOOLS, config=ProxyConfig())
    yield p
    p.close_all()


def _rec(addr: str, protocol: str = "socks5", **extra) -> dict:
    return {"protocol": protocol, "proxy": addr, "status": "Working", **extra}


# ---------------------------------------------------------------- 纯逻辑

def test_select_records_filters_and_sorts():
    rows = [
        _rec("1.1.1.1:1", score=10, location="CN", latency=0.5),
        _rec("2.2.2.2:2", score=99, location="US", latency=0.05),
        _rec("3.3.3.3:3", score=50, location="CN", latency=2.0, status="Unavailable"),
    ]
    top = select_records(rows, limit=2)
    assert [r["proxy"] for r in top] == ["2.2.2.2:2", "1.1.1.1:1"]  # 死代理被滤掉
    assert [r["proxy"] for r in select_records(rows, limit=5, region="CN")] == ["1.1.1.1:1"]
    assert [r["proxy"] for r in select_records(rows, limit=5, max_latency_ms=100)] \
        == ["2.2.2.2:2"]
    assert len(select_records(rows, limit=5, include_failed=True)) == 3


def test_config_from_file_missing_and_bad(tmp_path):
    assert ProxyConfig.from_file(tmp_path / "nope.json").http_port_base == 1800
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert ProxyConfig.from_file(bad).socks5_port_base == 1801
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"http_port_base": 9999, "region": "CN"}),
                    encoding="utf-8")
    cfg = ProxyConfig.from_file(good)
    assert cfg.http_port_base == 9999 and cfg.region == "CN"


def test_pool_file_roundtrip_and_dedup(pool):
    pid = "p-file"
    assert pool.read_pool(pid) == []
    assert pool.add_records(pid, [_rec("a:1"), _rec("a:1"), _rec("b:2")]) \
        == {"added": 2, "pool_size": 2}          # 同地址去重
    assert pool.add_records(pid, [_rec("a:1")]) == {"added": 0, "pool_size": 2}
    assert pool.remove_records(pid, ["b:2"]) == {"removed": 1, "pool_size": 1}
    assert [r["proxy"] for r in pool.read_pool(pid)] == ["a:1"]


# ---------------------------------------------------------------- 生命周期

def test_lifecycle_and_control_channel(pool):
    pid = "p-life"
    with pytest.raises(Exception):
        pool.start(pid)                          # 空池：明确拒绝
    pool.add_records(pid, [_rec("10.0.0.1:1080"), _rec("10.0.0.2:1080", "http")])

    st = pool.start(pid)
    assert st["running"] is True
    assert st["count"] == 2 and st["working"] == 2
    assert st["endpoint"]["http"].startswith("127.0.0.1:")
    assert st["endpoint"]["socks5"].split(":")[1] != st["endpoint"]["http"].split(":")[1]
    assert st["current"]["proxy"] in ("10.0.0.1:1080", "10.0.0.2:1080")

    first = pool.status(pid)["current"]["proxy"]
    rotated = pool.rotate(pid)["current"]["proxy"]
    assert rotated != first                       # 轮换确实换了一个

    listing = pool.list_proxies(pid)
    assert listing["source"] == "serve" and len(listing["proxies"]) == 2

    # 热增删（运行中经控制通道 /reload）
    pool.add_records(pid, [_rec("10.0.0.3:1080")])
    assert pool.status(pid)["count"] == 3
    pool.remove_records(pid, ["10.0.0.3:1080"])
    assert pool.status(pid)["count"] == 2

    assert pool.stop(pid) == {"running": False}
    assert pool.status(pid)["running"] is False
    assert pool.status(pid)["pool_size"] == 2     # 停服后池文件仍在


def test_rotate_without_service_rejected(pool):
    with pytest.raises(Exception):
        pool.rotate("p-nope")


# ---------------------------------------------------------------- 端到端转发

def test_end_to_end_forwarding(pool, target_server, socks5_upstream):
    """池的 HTTP 入口 → 假 SOCKS5 上游 → 本地靶站，真实取回 PROXY-OK。"""
    pid = "p-e2e"
    pool.add_records(pid, [_rec(f"127.0.0.1:{socks5_upstream.port}",
                                location="Local", latency=0.01)])
    st = pool.start(pid)
    http_endpoint = st["endpoint"]["http"]

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://{http_endpoint}"}))
    last_err = None
    for _ in range(20):                           # 上游中继起效需一拍
        try:
            with opener.open(target_server, timeout=8) as resp:
                assert resp.read() == b"PROXY-OK"
                return
        except (urllib.error.URLError, OSError) as exc:  # noqa: PERF203
            last_err = exc
            time.sleep(0.3)
    pytest.fail(f"经代理转发失败: {last_err}")


# ---------------------------------------------------------------- API 端点

@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from core.api.app import create_app
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=TOOLS,
                     executor_llm=None, planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        yield c


def test_api_endpoints(client):
    pid = client.post("/api/projects", json={
        "name": "代理池测试", "track": "pentest"}).json()["id"]

    assert client.get(f"/api/projects/{pid}/proxy/status").json()["running"] is False

    r = client.post(f"/api/projects/{pid}/proxy/add", json={"records": [
        {"protocol": "socks5", "proxy": "10.1.1.1:1080", "status": "Working",
         "location": "CN", "latency": 0.3},
        {"protocol": "http", "proxy": "10.1.1.2:8080", "status": "Working",
         "location": "US", "latency": 0.1},
    ]})
    assert r.json() == {"added": 2, "pool_size": 2}

    assert len(client.get(f"/api/projects/{pid}/proxy/proxies").json()["proxies"]) == 2
    sel = client.post(f"/api/projects/{pid}/proxy/select",
                      json={"limit": 1}).json()["proxies"]
    assert len(sel) == 1

    started = client.post(f"/api/projects/{pid}/proxy/start").json()
    assert started["running"] is True and started["count"] == 2
    assert client.post(f"/api/projects/{pid}/proxy/rotate").json()["ok"] is True
    assert client.post(f"/api/projects/{pid}/proxy/stop").json() == {"running": False}

    assert client.post(f"/api/projects/{pid}/proxy/remove",
                       json={"addresses": ["10.1.1.1:1080"]}).json()["removed"] == 1
    # 未知项目 404（不是 500）
    assert client.get("/api/projects/nope/proxy/status").status_code == 404


def test_api_start_empty_pool_is_503(client):
    pid = client.post("/api/projects", json={
        "name": "空池", "track": "pentest"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/proxy/start")
    assert r.status_code == 503 and "空" in r.json()["detail"]


def test_api_status_reports_running_job(client):
    """切页回来重挂：/proxy/status 带出运行中的代理 Job（id/kind/progress）。"""
    pid = client.post("/api/projects", json={
        "name": "进度", "track": "pentest"}).json()["id"]
    gate = threading.Event()
    progress = {"phase": "check", "done": 3, "total": 10, "eta_seconds": 7}
    jid = client.app.state.jobs.submit(
        "proxy-fetch", lambda: gate.wait(5),
        meta={"project_id": pid, "progress": progress})
    try:
        st = client.get(f"/api/projects/{pid}/proxy/status").json()
        assert st["job"] and st["job"]["id"] == jid
        assert st["job"]["kind"] == "proxy-fetch"
        assert st["job"]["progress"]["done"] == 3          # 可变 dict 引用，读到最新
    finally:
        gate.set()
    # 其它项目看不到（按 meta.project_id 过滤）
    pid2 = client.post("/api/projects", json={
        "name": "别的", "track": "pentest"}).json()["id"]
    assert client.get(f"/api/projects/{pid2}/proxy/status").json()["job"] is None


# ---------------------------------------------------------------- 清理判据（离线）

def test_is_usable_and_prune_criteria():
    ok = {"status": "Working", "latency": 0.5}
    assert _is_usable(ok, 5000)
    assert _is_usable(ok, None) and _is_usable(ok, 0)                 # 阈值关：不按延迟
    assert not _is_usable({"status": "Failed", "latency": 0.1}, 5000)
    assert not _is_usable({"status": "Working", "latency": 6.0}, 5000)   # >5s 删
    assert _is_usable({"status": "Working", "latency": 5.0}, 5000)       # 边界保留
    assert _is_usable({"status": "Working", "latency": 6.0}, None)       # 阈值关不删
    assert _is_usable({"status": "Working", "latency": None}, 5000)      # 缺延迟保留
    assert _is_usable({"status": "Working", "latency": float("inf")}, 5000)
    keep, dropped = _prune([ok, {"status": "Failed"},
                            {"status": "Working", "latency": 9}], 5000)
    assert keep == [ok] and len(dropped) == 2


# ---------------------------------------------------------------- 验证/抓取编排（离线，monkeypatch cli）

def test_validate_prunes_failed_and_merges(pool, monkeypatch):
    pid = "p-validate"
    pool.add_records(pid, [
        _rec("1.1.1.1:1", latency=0.2),
        _rec("2.2.2.2:2", latency=0.3),
        _rec("3.3.3.3:3", latency=0.1),
    ])
    canned = [
        {"proxy": "1.1.1.1:1", "protocol": "HTTP", "status": "Working", "latency": 0.25},
        {"proxy": "2.2.2.2:2", "protocol": "HTTP", "status": "Failed", "latency": None},
        {"proxy": "3.3.3.3:3", "protocol": "HTTP", "status": "Working", "latency": 9.0},
    ]
    monkeypatch.setattr(ProxyPool, "_run_cli",
                        lambda self, pid, args, timeout, script=None, on_progress=None: canned)
    res = pool.validate(pid)
    assert res["validated"] == 3 and res["removed"] == 2 and res["working"] == 1
    rows = pool.read_pool(pid)
    assert [r["proxy"] for r in rows] == ["1.1.1.1:1"]     # Failed 与慢代理被删
    assert rows[0]["latency"] == 0.25                      # 验证结果 merge 回写


def test_fetch_auto_validates_and_prunes(pool, monkeypatch):
    pid = "p-fetch"
    fetched = [{"protocol": "http", "proxy": f"9.9.9.{i}:80"} for i in range(3)]
    validated = [
        {"proxy": "9.9.9.0:80", "protocol": "HTTP", "status": "Working", "latency": 0.5},
        {"proxy": "9.9.9.1:80", "protocol": "HTTP", "status": "Failed", "latency": None},
        {"proxy": "9.9.9.2:80", "protocol": "HTTP", "status": "Working", "latency": 7.0},
    ]

    def fake(self, pid, args, timeout, script=None, on_progress=None):
        name = Path(script).name if script is not None else ""
        return fetched if name == "cyberstrike_fetch.py" else validated

    monkeypatch.setattr(ProxyPool, "_run_cli", fake)
    res = pool.fetch(pid)
    assert res["fetched"] == 3 and res["validated"] == 3
    assert res["removed"] == 2 and res["added"] == 1
    assert [r["proxy"] for r in pool.read_pool(pid)] == ["9.9.9.0:80"]


def test_fetch_without_auto_validate_adds_all(pool, monkeypatch):
    pid = "p-fetch-raw"
    fetched = [{"protocol": "http", "proxy": "9.9.9.9:80"}]
    monkeypatch.setattr(ProxyPool, "_run_cli",
                        lambda self, pid, args, timeout, script=None, on_progress=None: fetched)
    res = pool.fetch(pid, auto_validate=False)
    assert res == {"fetched": 1, "validated": 0, "removed": 0,
                   "added": 1, "pool_size": 1}
    assert len(pool.read_pool(pid)) == 1


def test_runner_build_sources():
    import importlib.util

    path = Path(TOOLS) / "fir-proxy" / "cyberstrike_fetch.py"
    spec = importlib.util.spec_from_file_location("cyberstrike_fetch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sources, scrapers = mod.build_sources(None)
    assert sources["http"] and sources["socks5"] and "https" in sources
    assert "free_proxy_list" not in scrapers and "kuaidaili" in scrapers
    s2, sc2 = mod.build_sources({"api": {"http": ["http://x/list.txt"]}, "scrapers": []})
    assert s2["http"] == ["http://x/list.txt"] and sc2 == []
    assert s2["socks5"] == sources["socks5"]                # 未覆盖的协议沿用默认


def test_runner_check_records():
    """check runner：注入假 checker 直测 check_records（不触网）。"""
    import importlib.util

    path = Path(TOOLS) / "fir-proxy" / "cyberstrike_check.py"
    spec = importlib.util.spec_from_file_location("cyberstrike_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeChecker:
        def __init__(self, ok_map):
            self.ok_map = ok_map

        def check_proxy_url(self, record, url, timeout):
            ok = self.ok_map[record["proxy"]]
            return {"ok": ok, "status_code": 200 if ok else None,
                    "error": "" if ok else "boom"}

    records = [{"protocol": "http", "proxy": "1.1.1.1:80"},
               {"protocol": "http", "proxy": "2.2.2.2:80"}]
    ok_map = {"1.1.1.1:80": True, "2.2.2.2:80": False}
    out = mod.check_records(records, "http://x/", 5, 2,
                            checker_factory=lambda: FakeChecker(ok_map))
    by = {r["proxy"]: r for r in out}
    assert by["1.1.1.1:80"]["status"] == "Working"
    assert isinstance(by["1.1.1.1:80"]["latency"], float)   # 通：记延迟
    assert by["2.2.2.2:80"]["status"] == "Failed"
    assert by["2.2.2.2:80"]["latency"] is None              # 不通：无延迟
    assert mod.check_records([], "http://x/", 5, 2) == []   # 空批次直接返 []


def test_limit_records_balanced():
    """fetch runner 限量：按协议轮转取、0=不限。"""
    import importlib.util

    path = Path(TOOLS) / "fir-proxy" / "cyberstrike_fetch.py"
    spec = importlib.util.spec_from_file_location("cyberstrike_fetch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fetched = {"http": ["h1:1", "h2:1", "h3:1"],
               "socks4": ["s4a:1"],
               "socks5": ["s5a:1", "s5b:1"]}
    protos3 = ["http", "socks4", "socks5"]
    out = mod.limit_records(fetched, protos3, 4)
    assert len(out) == 4
    kinds = [r["protocol"] for r in out]
    assert kinds.count("http") == 2 and kinds.count("socks4") == 1 and kinds.count("socks5") == 1
    assert out[0] == {"protocol": "http", "proxy": "h1:1"}   # 轮转：http 先
    assert len(mod.limit_records(fetched, protos3, 0)) == 6  # 0=不限
    assert mod.limit_records(fetched, ["http"], 100) == [
        {"protocol": "http", "proxy": "h1:1"},
        {"protocol": "http", "proxy": "h2:1"},
        {"protocol": "http", "proxy": "h3:1"},
    ]


def test_progress_reporter_eta():
    """进度回调：done/total 与 ETA 计算、相位切换重置计时、None=no-op。"""
    from core.proxy.pool import _progress_reporter

    calls: list[dict] = []
    report = _progress_reporter(calls.append)
    report("check", 0, 100, "验证中…")
    first = calls[-1]
    assert first["phase"] == "check" and first["done"] == 0 and first["total"] == 100
    assert first["eta_seconds"] is None                       # done=0 → 无 ETA
    time.sleep(0.2)
    report("check", 10, 100)
    second = calls[-1]
    assert second["done"] == 10 and second["elapsed_seconds"] > 0
    assert isinstance(second["eta_seconds"], int) and second["eta_seconds"] >= 0
    report("prune", 1, 1)
    third = calls[-1]
    assert third["phase"] == "prune" and third["eta_seconds"] is None
    assert third["elapsed_seconds"] < 0.5                     # 相位切换重置计时
    assert _progress_reporter(None)("x", 1, 1) is None        # no-op


def test_run_cli_streams_progress(pool, tmp_path):
    """_run_cli 流式分支：解析 runner 的 progress 行并回调，正常返回 -o 结果。"""
    script = tmp_path / "fake_runner.py"
    script.write_text(
        "import argparse, json\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('-o', '--output', required=True)\n"
        "p.add_argument('--output-format')\n"
        "p.add_argument('--quiet', action='store_true')\n"
        "a = p.parse_args()\n"
        "for k in (1, 2, 3):\n"
        "    print(json.dumps({'event': 'progress', 'phase': 'check',\n"
        "                      'done': k, 'total': 3}), flush=True)\n"
        "open(a.output, 'w', encoding='utf-8').write(\n"
        "    json.dumps([{'protocol': 'http', 'proxy': '1.1.1.1:80'}]))\n",
        encoding="utf-8")

    seen: list[dict] = []
    out = pool._run_cli("p-stream", [], 30.0, script=script, on_progress=seen.append)
    assert [r["proxy"] for r in out] == ["1.1.1.1:80"]
    assert [s["done"] for s in seen] == [1, 2, 3]
    assert all(s["phase"] == "check" for s in seen)


