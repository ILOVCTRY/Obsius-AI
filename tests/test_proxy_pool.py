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
from core.proxy.pool import (
    SOCKS_PROTOCOLS,
    _is_usable,
    _prune,
    auto_fetch_targets,
    protocol_counts,
)

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


def test_config_auto_maintenance_defaults(tmp_path):
    """自动巡检默认值（2026-10-11）：30min 间隔 + 阈值 50/20/20 + 冷却 600s。"""
    cfg = ProxyConfig()
    assert cfg.maintenance_interval_s == 1800.0
    assert cfg.auto_fetch_min_http == 50
    assert cfg.auto_fetch_min_https == 20
    assert cfg.auto_fetch_min_socks == 20
    assert cfg.auto_fetch_cooldown_s == 600.0
    assert cfg.validate_target_https == "https://i.zut.edu.cn/new/index.html"
    # 文件覆盖生效
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"auto_fetch_min_http": 200, "maintenance_interval_s": 60}),
                 encoding="utf-8")
    cfg2 = ProxyConfig.from_file(f)
    assert cfg2.auto_fetch_min_http == 200 and cfg2.maintenance_interval_s == 60


def test_protocol_counts():
    """协议分桶计数：http / socks(socks4+socks5) / https(能力) ，非可用不计。"""
    rows = [
        _rec("a:1", protocol="http"),                    # http Working
        _rec("b:2", protocol="socks5"),
        _rec("c:3", protocol="socks4"),
        _rec("d:4", protocol="http", status="Failed"),   # 死：不计入任何桶
        {**_rec("e:5", protocol="http"), "https_ok": True},   # http + https 能力
        {**_rec("f:6", protocol="socks5"), "https_ok": True},
        {**_rec("g:7", protocol="http"), "https_ok": False},
    ]
    c = protocol_counts(rows, 5000.0)
    assert c["http"] == 3          # a, e, g（Working 且 http）
    assert c["socks"] == 3         # b, c, f
    assert c["https"] == 2         # e, f（https_ok=True）
    assert c["total"] == 7
    # 无 prune 阈值时同样成立
    assert protocol_counts(rows, None)["http"] == 3


def test_protocol_counts_socks_is_union():
    assert SOCKS_PROTOCOLS == frozenset({"socks4", "socks5"})


def test_auto_fetch_targets():
    """阈值判定：不足才抓；https 不足也抓 http（能力靠探测补齐）；0=关。"""
    cfg = ProxyConfig()                                     # 50/20/20
    # http 不足 + https 不足 → 合并为一条 ["http"]
    assert auto_fetch_targets({"http": 30, "https": 5, "socks": 99}, cfg) == [["http"]]
    # 仅 socks 不足
    assert auto_fetch_targets({"http": 99, "https": 99, "socks": 10}, cfg) \
        == [["socks4", "socks5"]]
    # 三者都不足
    t = auto_fetch_targets({"http": 0, "https": 0, "socks": 0}, cfg)
    assert t == [["http"], ["socks4", "socks5"]]
    # 都充足 → 不抓
    assert auto_fetch_targets({"http": 99, "https": 99, "socks": 99}, cfg) == []
    # 阈值 0 = 该协议永不自动抓
    cfg0 = ProxyConfig(auto_fetch_min_http=0, auto_fetch_min_https=0, auto_fetch_min_socks=0)
    assert auto_fetch_targets({"http": 0, "https": 0, "socks": 0}, cfg0) == []


def test_pool_file_roundtrip_and_dedup(pool):
    assert pool.read_pool() == []
    assert pool.add_records([_rec("a:1"), _rec("a:1"), _rec("b:2")]) \
        == {"added": 2, "pool_size": 2}          # 同地址去重
    assert pool.add_records([_rec("a:1")]) == {"added": 0, "pool_size": 2}
    assert pool.remove_records(["b:2"]) == {"removed": 1, "pool_size": 1}
    assert [r["proxy"] for r in pool.read_pool()] == ["a:1"]


def test_data_dir_is_global(pool, tmp_path):
    """池落 data/proxy/（全局归位契约），**不在** workspaces/ 下。"""
    assert "workspaces" not in str(pool.pool_path())
    assert pool.pool_path().parent.name == "proxy"
    assert pool.pool_path().parent.parent.name == "data"


def test_merge_legacy_pools(pool, tmp_path):
    """首启迁移：旧 per-project 池并入全局，按地址去重、旧文件保留，且幂等。"""
    ws = tmp_path / "ws"
    for pid, recs in (("proj-a", [_rec("1.1.1.1:1"), _rec("dup:9")]),
                      ("proj-b", [_rec("2.2.2.2:2"), _rec("dup:9", location="A")])):
        d = ws / pid / "proxy"
        d.mkdir(parents=True, exist_ok=True)
        (d / "pool.json").write_text(json.dumps(recs), encoding="utf-8")
    assert pool.merge_legacy_pools() == 3                      # 1.1 / 2.2 / dup 去重
    addrs = {r["proxy"] for r in pool.read_pool()}
    assert addrs == {"1.1.1.1:1", "2.2.2.2:2", "dup:9"}
    assert (ws / "proj-a" / "proxy" / "pool.json").exists()    # 旧文件原地保留
    assert pool.merge_legacy_pools() == 0                      # 幂等：池非空不再并


# ---------------------------------------------------------------- 生命周期

def test_lifecycle_and_control_channel(pool):
    with pytest.raises(Exception):
        pool.start()                             # 空池：明确拒绝
    pool.add_records([_rec("10.0.0.1:1080"), _rec("10.0.0.2:1080", "http")])

    st = pool.start()
    assert st["running"] is True
    assert st["count"] == 2 and st["working"] == 2
    assert st["endpoint"]["http"].startswith("127.0.0.1:")
    assert st["endpoint"]["socks5"].split(":")[1] != st["endpoint"]["http"].split(":")[1]
    assert st["current"]["proxy"] in ("10.0.0.1:1080", "10.0.0.2:1080")

    first = pool.status()["current"]["proxy"]
    rotated = pool.rotate()["current"]["proxy"]
    assert rotated != first                       # 轮换确实换了一个

    listing = pool.list_proxies()
    assert listing["source"] == "serve" and len(listing["proxies"]) == 2

    # 热增删（运行中经控制通道 /reload）
    pool.add_records([_rec("10.0.0.3:1080")])
    assert pool.status()["count"] == 3
    pool.remove_records(["10.0.0.3:1080"])
    assert pool.status()["count"] == 2

    assert pool.stop() == {"running": False}
    assert pool.status()["running"] is False
    assert pool.status()["pool_size"] == 2        # 停服后池文件仍在


def test_rotate_without_service_rejected(pool):
    with pytest.raises(Exception):
        pool.rotate()


# ---------------------------------------------------------------- 端到端转发

def test_end_to_end_forwarding(pool, target_server, socks5_upstream):
    """池的 HTTP 入口 → 假 SOCKS5 上游 → 本地靶站，真实取回 PROXY-OK。"""
    pool.add_records([_rec(f"127.0.0.1:{socks5_upstream.port}",
                           location="Local", latency=0.01)])
    st = pool.start()
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
                     providers_config=str(tmp_path / "providers.json"),
                     proxy_config_path=str(tmp_path / "proxy.json"))
    with TestClient(app) as c:
        yield c


def test_api_endpoints(client):
    """全局池端点（无 pid）：状态/增删/挑选/起停轮换。"""
    assert client.get("/api/proxy/status").json()["running"] is False

    r = client.post("/api/proxy/add", json={"records": [
        {"protocol": "socks5", "proxy": "10.1.1.1:1080", "status": "Working",
         "location": "CN", "latency": 0.3},
        {"protocol": "http", "proxy": "10.1.1.2:8080", "status": "Working",
         "location": "US", "latency": 0.1},
    ]})
    assert r.json() == {"added": 2, "pool_size": 2}

    assert len(client.get("/api/proxy/proxies").json()["proxies"]) == 2
    sel = client.post("/api/proxy/select", json={"limit": 1}).json()["proxies"]
    assert len(sel) == 1

    started = client.post("/api/proxy/start").json()
    assert started["running"] is True and started["count"] == 2
    assert client.post("/api/proxy/rotate").json()["ok"] is True
    assert client.post("/api/proxy/stop").json() == {"running": False}

    assert client.post("/api/proxy/remove",
                       json={"addresses": ["10.1.1.1:1080"]}).json()["removed"] == 1
    # 旧的项目级路由已下线（全局化）：不存在此路径
    assert client.get("/api/projects/x/proxy/status").status_code == 404


def test_api_status_includes_counts(client):
    """status 带 counts（2026-10-11）：http/socks/https 三分桶，供前端过滤与阈值展示。"""
    client.post("/api/proxy/add", json={"records": [
        {"protocol": "http", "proxy": "1.1.1.1:1", "status": "Working",
         "latency": 0.1, "https_ok": True},
        {"protocol": "http", "proxy": "2.2.2.2:2", "status": "Working", "latency": 0.2},
        {"protocol": "socks5", "proxy": "3.3.3.3:3", "status": "Working", "latency": 0.3},
        {"protocol": "socks4", "proxy": "4.4.4.4:4", "status": "Working", "latency": 0.4},
    ]})
    counts = client.get("/api/proxy/status").json()["counts"]
    assert counts == {"http": 2, "socks": 2, "https": 1, "total": 4}


def _maint_app(tmp_path):
    from core.api.app import create_app
    return create_app(workspace_root=str(tmp_path / "workspaces"),
                      tools_root=TOOLS, executor_llm=None, planner_llm=None,
                      providers_config=str(tmp_path / "providers.json"),
                      proxy_maintenance=False)


def test_proxy_maintenance_tick_skips_when_busy(tmp_path):
    """巡检 tick：已有代理 Job 在跑 → 本轮跳过（不并发），不触发验证/抓取。"""
    import threading as _th

    from core.api.app import _proxy_maintenance_tick
    app = _maint_app(tmp_path)
    gate = _th.Event()
    try:
        app.state.jobs.submit("proxy-validate", lambda: gate.wait(5), meta={})
        out = _proxy_maintenance_tick(app)
        assert out["skipped"] == "busy" and out["fetched"] == []
    finally:
        gate.set()


def test_proxy_maintenance_tick_fetches_below_threshold(tmp_path):
    """巡检 tick：池空/低于阈值 → 提交 proxy-fetch Job（协议参数按阈值）。同步、零触网。"""
    from core.api.app import _proxy_maintenance_tick
    app = _maint_app(tmp_path)

    class FakePool:
        config = ProxyConfig()          # 默认 50/20/20

        def read_pool(self):
            return []                   # 空池 → http/https/socks 全不足

        def validate(self, **_):
            return {}

        def fetch(self, protocols=None, **_):
            return {}

    app.state.proxy_pool = FakePool()
    # 返回值 out["fetched"] 由 _proxy_auto_fetch 同步构建（Job 线程异步，故只断言它）
    out = _proxy_maintenance_tick(app)
    assert out["fetched"] == [["http"], ["socks4", "socks5"]]   # http/https 合并 + socks

    # 冷却：同协议组在 cooldown 内不重复提交
    out2 = _proxy_maintenance_tick(app)
    assert out2["fetched"] == []


def test_api_start_empty_pool_is_503(client):
    r = client.post("/api/proxy/start")
    assert r.status_code == 503 and "空" in r.json()["detail"]


def test_api_status_reports_running_job(client):
    """切页回来重挂：/api/proxy/status 带出运行中的代理 Job（id/kind/progress）。

    全局池：job 不再按项目过滤，任何调用方看到同一个全局 job。
    """
    gate = threading.Event()
    progress = {"phase": "check", "done": 3, "total": 10, "eta_seconds": 7}
    jid = client.app.state.jobs.submit(
        "proxy-fetch", lambda: gate.wait(5), meta={"progress": progress})
    try:
        st = client.get("/api/proxy/status").json()
        assert st["job"] and st["job"]["id"] == jid
        assert st["job"]["kind"] == "proxy-fetch"
        assert st["job"]["progress"]["done"] == 3          # 可变 dict 引用，读到最新
    finally:
        gate.set()


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
    pool.add_records([
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
                        lambda self, args, timeout, script=None, on_progress=None: canned)
    res = pool.validate()
    assert res["validated"] == 3 and res["removed"] == 2 and res["working"] == 1
    rows = pool.read_pool()
    assert [r["proxy"] for r in rows] == ["1.1.1.1:1"]     # Failed 与慢代理被删
    assert rows[0]["latency"] == 0.25                      # 验证结果 merge 回写


def test_validate_merges_https_ok(pool, monkeypatch):
    """HTTPS 能力探测结果 merge 回池（2026-10-11）：https_ok 随验证回写，不参与 status。"""
    pool.add_records([_rec("1.1.1.1:1"), _rec("2.2.2.2:2", protocol="socks5")])
    canned = [
        {"proxy": "1.1.1.1:1", "protocol": "http", "status": "Working",
         "latency": 0.2, "https_ok": True},
        {"proxy": "2.2.2.2:2", "protocol": "socks5", "status": "Working",
         "latency": 0.3, "https_ok": False},
    ]
    monkeypatch.setattr(ProxyPool, "_run_cli",
                        lambda self, args, timeout, script=None, on_progress=None: canned)
    pool.validate()
    by = {r["proxy"]: r for r in pool.read_pool()}
    assert by["1.1.1.1:1"]["https_ok"] is True
    assert by["2.2.2.2:2"]["https_ok"] is False
    # https_ok=False 不删记录（能力标注非生死判据）
    assert len(by) == 2


def test_list_proxies_includes_https_ok(pool):
    """列表视图必须带 https_ok（2026-10-11 回归）：`_proxy_view` 白名单曾漏该键，
    致 `/api/proxy/proxies` 过滤掉 https_ok——前端 HTTPS 过滤恒空（计数来自 read_pool 故正常）。
    """
    pool.add_records([
        {**_rec("1.1.1.1:1", protocol="http"), "https_ok": True},
        {**_rec("2.2.2.2:2", protocol="socks5"), "https_ok": False},
        _rec("3.3.3.3:3", protocol="http"),
    ])
    rows = pool.list_proxies()["proxies"]
    by = {r["proxy"]: r for r in rows}
    assert by["1.1.1.1:1"]["https_ok"] is True
    assert by["2.2.2.2:2"]["https_ok"] is False
    assert by["3.3.3.3:3"]["https_ok"] is None


def test_validate_passes_target_https(pool, monkeypatch):
    """validate 把 validate_target_https 透传给 check runner（--target-https）。"""
    pool.add_records([_rec("1.1.1.1:1")])
    seen: list[list[str]] = []

    def fake(self, args, timeout, script=None, on_progress=None):
        seen.append(list(args))
        return [{"proxy": "1.1.1.1:1", "protocol": "http", "status": "Working",
                 "latency": 0.1, "https_ok": True}]

    monkeypatch.setattr(ProxyPool, "_run_cli", fake)
    pool.validate()
    assert "--target-https" in seen[0]
    assert "https://i.zut.edu.cn/new/index.html" in seen[0]


def test_fetch_auto_validates_and_prunes(pool, monkeypatch):
    fetched = [{"protocol": "http", "proxy": f"9.9.9.{i}:80"} for i in range(3)]
    validated = [
        {"proxy": "9.9.9.0:80", "protocol": "HTTP", "status": "Working", "latency": 0.5},
        {"proxy": "9.9.9.1:80", "protocol": "HTTP", "status": "Failed", "latency": None},
        {"proxy": "9.9.9.2:80", "protocol": "HTTP", "status": "Working", "latency": 7.0},
    ]

    def fake(self, args, timeout, script=None, on_progress=None):
        name = Path(script).name if script is not None else ""
        return fetched if name == "cyberstrike_fetch.py" else validated

    monkeypatch.setattr(ProxyPool, "_run_cli", fake)
    res = pool.fetch()
    assert res["fetched"] == 3 and res["validated"] == 3
    assert res["removed"] == 2 and res["added"] == 1
    assert [r["proxy"] for r in pool.read_pool()] == ["9.9.9.0:80"]


def test_fetch_without_auto_validate_adds_all(pool, monkeypatch):
    fetched = [{"protocol": "http", "proxy": "9.9.9.9:80"}]
    monkeypatch.setattr(ProxyPool, "_run_cli",
                        lambda self, args, timeout, script=None, on_progress=None: fetched)
    res = pool.fetch(auto_validate=False)
    assert res == {"fetched": 1, "validated": 0, "removed": 0,
                   "added": 1, "pool_size": 1}
    assert len(pool.read_pool()) == 1


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


def test_runner_check_https_probe():
    """check runner HTTPS 能力探测（2026-10-11）：target_https 给定即标 https_ok，
    且 https 失败**不影响** status（能力标注非生死判据）。"""
    import importlib.util

    path = Path(TOOLS) / "fir-proxy" / "cyberstrike_check.py"
    spec = importlib.util.spec_from_file_location("cyberstrike_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeChecker:
        def __init__(self, ok_map):
            self.ok_map = ok_map

        def check_proxy_url(self, record, url, timeout):
            # https 目标（url 以 https 开头）用另一张表
            key = f"{'https' if url.startswith('https') else 'http'}:" + record["proxy"]
            ok = self.ok_map.get(key, False)
            return {"ok": ok, "status_code": 200 if ok else None,
                    "error": "" if ok else "boom"}

    records = [{"protocol": "http", "proxy": "1.1.1.1:80"},
               {"protocol": "http", "proxy": "2.2.2.2:80"}]
    m = {"http:1.1.1.1:80": True, "https:1.1.1.1:80": True,   # 全通
         "http:2.2.2.2:80": True, "https:2.2.2.2:80": False}  # HTTP 通但 HTTPS 不通
    out = mod.check_records(records, "http://x/", 5, 2,
                            checker_factory=lambda: FakeChecker(m),
                            target_https="https://y/")
    by = {r["proxy"]: r for r in out}
    assert by["1.1.1.1:80"]["https_ok"] is True
    assert by["1.1.1.1:80"]["status"] == "Working"
    assert by["2.2.2.2:80"]["https_ok"] is False
    assert by["2.2.2.2:80"]["status"] == "Working"          # https 失败不改 status
    # 不给 target_https → 不写 https_ok 键
    out2 = mod.check_records(records, "http://x/", 5, 2,
                             checker_factory=lambda: FakeChecker(m))
    assert all("https_ok" not in r for r in out2)


def test_content_fingerprint_shape():
    """内容指纹 = 状态码:长度:sha256前16hex；不同体/不同长→不同指纹（纯函数）。"""
    import importlib.util

    path = Path(TOOLS) / "fir-proxy" / "cyberstrike_check.py"
    spec = importlib.util.spec_from_file_location("cyberstrike_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fp1 = mod._content_fingerprint(200, b"hello")
    assert fp1.startswith("200:5:")
    assert mod._content_fingerprint(200, b"hello") == fp1     # 确定性
    assert mod._content_fingerprint(200, b"world") != fp1     # 内容不同
    assert mod._content_fingerprint(302, b"hello") != fp1     # 状态不同
    assert mod._content_fingerprint(200, b"hello!") != fp1    # 长度不同
    assert mod._content_fingerprint(200, b"") == mod._content_fingerprint(200, b"")


def test_runner_check_hijack_detection():
    """内容注入补丁（2026-10-11）：两个内容不同的目标经同一代理若返回**相同响应体
    指纹** → 判 hijacked、status=Failed；指纹不同（正常代理）→ Working 且 hijacked=False；
    探测失败不误杀（保留连通结论）。"""
    import importlib.util

    path = Path(TOOLS) / "fir-proxy" / "cyberstrike_check.py"
    spec = importlib.util.spec_from_file_location("cyberstrike_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeChecker:
        def check_proxy_url(self, record, url, timeout):
            return {"ok": True, "status_code": 200, "error": ""}

    # 注入型：两目标回同一伪造页；正常型：两目标回各自内容；半坏：第二目标探测失败
    forged = b"<html>blocked</html>"          # 334 字节伪造页的替身
    def probe(record, url, timeout):
        addr = record["proxy"]
        if addr == "9.9.9.9:80":              # 注入型：任意 target 回同一页
            return {"ok": True, "status": 200, "body": forged, "error": ""}
        if addr == "8.8.8.8:80":              # 正常型：内容随 target 变
            return {"ok": True, "status": 200, "body": b"real-" + url.encode(),
                    "error": ""}
        return {"ok": False, "status": None, "body": b"", "error": "timeout"}

    records = [{"protocol": "http", "proxy": "9.9.9.9:80"},
               {"protocol": "http", "proxy": "8.8.8.8:80"},
               {"protocol": "http", "proxy": "7.7.7.7:80"}]
    out = mod.check_records(records, "http://a.example/", 5, 3,
                            checker_factory=lambda: FakeChecker(),
                            target_b="http://b.example/",
                            probe_factory=probe)
    by = {r["proxy"]: r for r in out}
    assert by["9.9.9.9:80"]["hijacked"] is True
    assert by["9.9.9.9:80"]["status"] == "Failed"           # 注入型剔除
    assert "内容注入" in by["9.9.9.9:80"]["error"]
    assert by["8.8.8.8:80"]["hijacked"] is False
    assert by["8.8.8.8:80"]["status"] == "Working"          # 正常代理保留
    assert by["7.7.7.7:80"]["hijacked"] is False            # 探测失败不误杀
    assert by["7.7.7.7:80"]["status"] == "Working"
    # 不给 target_b → 不写 hijacked 键、不做内容探测
    out2 = mod.check_records(records, "http://a.example/", 5, 3,
                             checker_factory=lambda: FakeChecker())
    assert all("hijacked" not in r for r in out2)


def test_config_content_check_defaults():
    """ProxyConfig 内容注入交叉验证默认：第二目标 example.com；None=不校验。"""
    cfg = ProxyConfig()
    assert cfg.validate_target_b == "http://example.com"
    assert cfg.validate_target != cfg.validate_target_b


def test_validate_passes_target_b(pool, monkeypatch):
    """validate 把 validate_target_b 透传给 check runner（--target-b）。"""
    pool.add_records([_rec("1.1.1.1:1")])
    seen: list[list[str]] = []

    def fake_run(self, args, timeout, script=None, on_progress=None):
        seen.append(list(args))
        return [{"protocol": "http", "proxy": "1.1.1.1:1", "status": "Working",
                 "latency": 0.1}]

    monkeypatch.setattr(ProxyPool, "_run_cli", fake_run)
    pool.validate()
    assert "--target-b" in seen[0]
    idx = seen[0].index("--target-b")
    assert seen[0][idx + 1] == pool.config.validate_target_b


def test_validate_merges_hijacked(pool, monkeypatch):
    """hijacked 标志随验证 merge 回池；hijacked=True（status=Failed）被剔除。"""
    pool.add_records([_rec("1.1.1.1:1"), _rec("2.2.2.2:2")])
    canned = [
        {"proxy": "1.1.1.1:1", "protocol": "http", "status": "Failed",
         "latency": None, "hijacked": True, "error": "内容注入"},
        {"proxy": "2.2.2.2:2", "protocol": "http", "status": "Working",
         "latency": 0.2, "hijacked": False},
    ]
    monkeypatch.setattr(ProxyPool, "_run_cli",
                        lambda self, args, timeout, script=None, on_progress=None: canned)
    res = pool.validate()
    assert res["removed"] == 1
    rows = pool.read_pool()
    assert [r["proxy"] for r in rows] == ["2.2.2.2:2"]
    assert rows[0]["hijacked"] is False


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
    out = pool._run_cli([], 30.0, script=script, on_progress=seen.append)
    assert [r["proxy"] for r in out] == ["1.1.1.1:80"]
    assert [s["done"] for s in seen] == [1, 2, 3]
    assert all(s["phase"] == "check" for s in seen)




# ---------------------------------------------------------------- 孤儿 serve 回收（2026-10-12）

def test_reap_orphan_serves_ignores_live_parent(monkeypatch):
    """父进程存活的 serve 不动（绝不误杀在跑的池）；非 serve 命令也跳过。"""
    from core.proxy import pool as P

    monkeypatch.setattr(P, "_list_processes", lambda: [
        {"ProcessId": 111, "ParentProcessId": 222,
         "CommandLine": "python cyberstrike_serve.py -i x/pool.json"},
        {"ProcessId": 333, "ParentProcessId": 444,
         "CommandLine": "python -c print('not a serve')"},
    ])
    monkeypatch.setattr(P, "_pid_alive", lambda pid: True)  # 父 222 存活
    monkeypatch.setattr(P, "_taskkill_tree", lambda pid: pytest.fail("不该杀"))
    assert P.reap_orphan_serves() == []


def test_reap_orphan_serves_kills_dead_parent(monkeypatch):
    """父进程已死的 serve 被 taskkill 回收；父存活的 serve 不动。"""
    from core.proxy import pool as P

    killed_calls = []
    monkeypatch.setattr(P, "_list_processes", lambda: [
        {"ProcessId": 111, "ParentProcessId": 999,
         "CommandLine": "python cyberstrike_serve.py -i x/pool.json"},
        {"ProcessId": 222, "ParentProcessId": 333,
         "CommandLine": "python cyberstrike_serve.py -i y/pool.json"},
    ])
    monkeypatch.setattr(P, "_pid_alive", lambda pid: int(pid) != 999)  # 999 死、333 活
    monkeypatch.setattr(P, "_taskkill_tree", lambda pid: killed_calls.append(pid) or True)
    assert P.reap_orphan_serves() == [111]
    assert killed_calls == [111]


# ---------------------------------------------------------------- 指定网站可达性/WAF 检测（2026-10-12）

def _load_check_mod():
    import importlib.util
    path = Path(TOOLS) / "fir-proxy" / "cyberstrike_check.py"
    spec = importlib.util.spec_from_file_location("cyberstrike_check_cu", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_urls_baseline_and_block_detection():
    """check_urls：与直连基线对比判「可达/被封禁」。"""
    mod = _load_check_mod()

    # 直连基线：HTTP 200 + 正常内容
    def direct(url, timeout):
        return {"ok": True, "status": 200, "body": b"<html>real site</html>", "error": ""}

    # 各代理的响应
    def probe(record, url, timeout):
        addr = record["proxy"]
        if addr == "1.1.1.1:80":                 # 正常可达（与基线内容不同但非拦截）
            return {"ok": True, "status": 200, "body": b"<html>page A</html>", "error": ""}
        if addr == "2.2.2.2:80":                 # 403 → 被封禁
            return {"ok": True, "status": 403, "body": b"<html>forbidden</html>", "error": ""}
        if addr == "3.3.3.3:80":                 # 200 但 Cloudflare 拦截页 → 被封禁
            return {"ok": True, "status": 200, "body": b"Attention Required! Cloudflare",
                    "error": ""}
        if addr == "4.4.4.4:80":                 # 与基线指纹相同 → 正常（直连也这样）
            return {"ok": True, "status": 200, "body": b"<html>real site</html>", "error": ""}
        return {"ok": False, "status": None, "body": b"", "error": "timeout"}

    records = [{"protocol": "http", "proxy": p} for p in
               ("1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80", "4.4.4.4:80", "5.5.5.5:80")]
    out = mod.check_urls(records, "http://site.example/", 5, 5,
                         probe_factory=probe, direct_factory=direct)
    by = {r["proxy"]: r for r in out["results"]}
    assert out["baseline"] == {"ok": True, "status_code": 200, "error": ""}
    assert by["1.1.1.1:80"]["ok"] is True and by["1.1.1.1:80"]["blocked"] is False
    assert by["2.2.2.2:80"]["blocked"] is True and by["2.2.2.2:80"]["ok"] is True
    assert "403" in by["2.2.2.2:80"]["reason"]
    assert by["3.3.3.3:80"]["blocked"] is True       # 拦截页特征词
    assert by["4.4.4.4:80"]["blocked"] is False      # 与基线一致 → 不判封禁
    assert by["5.5.5.5:80"]["ok"] is False and by["5.5.5.5:80"]["blocked"] is False
    assert mod.check_urls([], "http://x/", 5, 2)["results"] == []   # 空批次


def test_check_urls_direct_baseline_blocked_no_false_positive():
    """直连本身就被该站拦（403）时，经代理拿到同样的拦截页**不判代理被封**。"""
    mod = _load_check_mod()

    def direct(url, timeout):
        return {"ok": True, "status": 403, "body": b"Access Denied", "error": ""}

    def probe(record, url, timeout):
        return {"ok": True, "status": 403, "body": b"Access Denied", "error": ""}

    out = mod.check_urls([{"protocol": "http", "proxy": "1.1.1.1:80"}],
                         "http://walled.example/", 5, 1,
                         probe_factory=probe, direct_factory=direct)
    row = out["results"][0]
    assert row["ok"] is True and row["blocked"] is False   # 与基线同指纹 → 不误判封禁


def test_pool_check_url_usable_sorted_and_readonly(pool, monkeypatch):
    """ProxyPool.check_url：可用代理按延迟升序取 limit；**不改动池文件**。"""
    pool.add_records([_rec(f"{i}.{i}.{i}.{i}:80", protocol="http")
                      for i in range(1, 6)])
    before = pool.read_pool()
    canned = {
        "url": "http://site.example/",
        "baseline": {"ok": True, "status_code": 200, "error": ""},
        "results": [
            {"proxy": "1.1.1.1:80", "protocol": "http", "ok": True,
             "status_code": 200, "latency": 0.5, "blocked": False, "reason": ""},
            {"proxy": "2.2.2.2:80", "protocol": "http", "ok": True,
             "status_code": 403, "latency": 0.1, "blocked": True, "reason": "被封"},
            {"proxy": "3.3.3.3:80", "protocol": "http", "ok": True,
             "status_code": 200, "latency": 0.2, "blocked": False, "reason": ""},
            {"proxy": "4.4.4.4:80", "protocol": "http", "ok": False,
             "status_code": None, "latency": None, "blocked": False, "reason": "超时"},
            {"proxy": "5.5.5.5:80", "protocol": "http", "ok": True,
             "status_code": 200, "latency": 0.9, "blocked": False, "reason": ""},
        ],
    }
    seen: list[list[str]] = []

    def fake_run(self, args, timeout, script=None, on_progress=None, as_dict=False):
        seen.append(list(args))
        assert as_dict is True
        return canned

    monkeypatch.setattr(ProxyPool, "_run_cli", fake_run)
    res = pool.check_url("http://site.example/", limit=10)
    assert "--url" in seen[0] and "http://site.example/" in seen[0]
    assert res["total"] == 5 and res["reachable"] == 4 and res["blocked"] == 1
    # 可用 = 未封禁且可达（1/3/5），按延迟升序：0.2(3) → 0.5(1) → 0.9(5)
    assert [u["proxy"] for u in res["usable"]] == ["3.3.3.3:80", "1.1.1.1:80", "5.5.5.5:80"]
    assert pool.read_pool() == before                    # 只读：池文件未变


def test_pool_check_url_limit_and_empty_pool(pool):
    """limit 截断；空池抛 ProxyError；空 URL 抛 ProxyError。"""
    import pytest as _pt

    from core.proxy import ProxyError
    with _pt.raises(ProxyError):
        pool.check_url("http://x/")                       # 空池
    pool.add_records([_rec("1.1.1.1:80", protocol="http")])
    with _pt.raises(ProxyError):
        pool.check_url("   ")                             # 空 URL


def test_api_check_url(client, monkeypatch):
    """端点：202 + Job 完成 result；空 url 422。"""
    from core.proxy import ProxyPool

    client.post("/api/proxy/add", json={"records": [
        {"protocol": "http", "proxy": "1.1.1.1:80", "status": "Working"}]})

    def fake_run(self, args, timeout, script=None, on_progress=None, as_dict=False):
        return {"url": "http://t/", "baseline": {"ok": True, "status_code": 200},
                "results": [{"proxy": "1.1.1.1:80", "protocol": "http", "ok": True,
                             "status_code": 200, "latency": 0.3, "blocked": False,
                             "reason": ""}]}

    monkeypatch.setattr(ProxyPool, "_run_cli", fake_run)
    r = client.post("/api/proxy/check-url", json={"url": "http://t/"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done"
    assert job["result"]["usable"][0]["proxy"] == "1.1.1.1:80"

    assert client.post("/api/proxy/check-url", json={"url": "  "}).status_code == 422


# ---------------------------------------------------------------- 验证基准站配置（2026-10-12）

def test_write_config_file_merges_and_preserves(tmp_path):
    """write_config_file：合并写入（保留既有其它键）、忽略非 ProxyConfig 键、原子落盘。"""
    from core.proxy.pool import write_config_file
    p = tmp_path / "proxy.json"
    p.write_text(json.dumps({"http_port_base": 1900, "validate_target": "http://old/"}),
                 encoding="utf-8")
    write_config_file(p, {"validate_target": "http://new/", "bogus": 1})
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["http_port_base"] == 1900          # 既有键保留
    assert data["validate_target"] == "http://new/"  # 覆盖
    assert "bogus" not in data                      # 非 ProxyConfig 键被忽略
    # 坏文件/不存在 → 按空 dict 起步，写入即建文件
    p2 = tmp_path / "sub" / "proxy.json"
    write_config_file(p2, {"validate_target": "http://x/"})
    assert json.loads(p2.read_text(encoding="utf-8"))["validate_target"] == "http://x/"


def test_api_proxy_config_get_defaults(client):
    """GET /api/proxy/config：回显当前值 + defaults（内置缺省）。"""
    body = client.get("/api/proxy/config").json()
    assert body["validate_target"] == "http://i.zut.edu.cn/new/index.html"
    assert body["defaults"]["validate_target"] == "http://i.zut.edu.cn/new/index.html"


def test_api_proxy_config_put_writes_and_hot_applies(client, tmp_path):
    """PUT：写盘 + 热更新在跑的 ProxyPool.config（下轮验证即生效）；空值 422。"""
    r = client.put("/api/proxy/config", json={
        "validate_target": "http://example.org/probe",
        "validate_target_https": "https://example.org/probe",
        "validate_target_b": "",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["validate_target"] == "http://example.org/probe"
    assert body["validate_target_https"] == "https://example.org/probe"
    assert body["validate_target_b"] is None                 # 空串 → 关闭（None）
    # 落盘到注入的临时文件（未污染仓库 config/）
    saved = json.loads((tmp_path / "proxy.json").read_text(encoding="utf-8"))
    assert saved["validate_target"] == "http://example.org/probe"
    # 热更新：在跑的池 config 已跟随
    assert client.app.state.proxy_pool.config.validate_target == "http://example.org/probe"
    # 空基准 → 422
    assert client.put("/api/proxy/config",
                      json={"validate_target": "  "}).status_code == 422
