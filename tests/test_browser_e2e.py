"""F6 浏览器端到端（需 playwright + chromium，缺任一自动跳过）。

覆盖：导航→白名单拒绝→登记后重试→抓包入库→截图→click/type→自签 HTTPS 放行。
本地 http.server 起回环靶站，资产登记 127.0.0.1（DNS 断网放行回环）。
"""

import base64
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.blackboard import Blackboard
from core.blackboard.assets import register_asset
from core.browser import BrowserConfig, BrowserPool
from core.browser.pool import browser_available

pytestmark = pytest.mark.skipif(
    not browser_available(), reason="playwright 未安装")


@pytest.fixture()
def server():
    html = b"<html><body><h1>IT WORKS</h1>" \
           b"<input id='u'/><button id='go'>go</button></body></html>"
    # 全屏可点击 div（接管输入用：点任意坐标都命中，坐标断言无需猜布局）
    btn_html = (b"<html><body style='margin:0'>"
                b"<div id='b' onclick=\"document.body.textContent='CLICKED'\" "
                b"style='width:100vw;height:100vh;background:#fff'>wait</div>"
                b"</body></html>")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = btn_html if self.path.startswith("/btn") else html
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def https_server():
    """回环 HTTPS 靶站：自签证书（fixtures/selfsigned-127.0.0.1.pem）。"""
    html = b"<html><body><h1>HTTPS WORKS</h1></body></html>"

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        str(Path(__file__).parent / "fixtures" / "selfsigned-127.0.0.1.pem"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def pool(tmp_path, monkeypatch):
    _real = socket.getaddrinfo

    def loopback(host, *a, **k):
        if host in ("127.0.0.1", "localhost", "::1"):
            return _real(host, *a, **k)
        raise OSError("断网")

    monkeypatch.setattr(socket, "getaddrinfo", loopback)
    bb = Blackboard(str(tmp_path / "bb.db"))
    p = bb.create_project("浏览器E2E", "pentest", ["web"])
    register_asset(bb, p["id"], "127.0.0.1", author="test")
    # chromium 未下载时跳过（启发式探测）
    from core.browser.pool import chromium_available
    if not chromium_available():
        pytest.skip("chromium 未安装（playwright install chromium）")
    pool = BrowserPool(tmp_path, bb_getter=lambda pid: bb,
                       config=BrowserConfig(max_sessions_per_project=2,
                                            intercept_timeout_s=2.0))
    yield pool, bb, p["id"]
    pool.close_all()
    bb.close()


def test_navigate_capture_screenshot_unregistered_localhost_allowed(pool, server):
    from core.browser.pool import BrowserError
    bpool, bb, pid = pool
    inst = bpool.get_instance(pid)
    # 当前产品允许任意目标导航；localhost 未登记也应访问 fixture 本地服务。
    unregistered_sid = "sess-" + "d" * 12
    inst.open_session(unregistered_sid, unregistered_sid)
    r = inst.navigate(unregistered_sid, f"http://localhost:{server.rsplit(':', 1)[1]}/")
    assert r["status"] == 200 and r["target_host"] == "localhost"
    inst.close_session(unregistered_sid)
    # 回环已登记：导航成功 + browser.action 审计
    sid = "sess-" + "e" * 12
    info = inst.open_session(sid, sid)
    assert info["sid"] == sid
    r = inst.navigate(sid, f"{server}/")
    assert r["status"] == 200 and r["target_host"] == "127.0.0.1"
    # ③ 抓包入库（route 拦截；navigate 返回早于 _store 落库，轮询等一拍）
    rows = []
    for _ in range(20):
        rows = bb.list_http_history(pid, source="browser")
        if rows:
            break
        time.sleep(0.3)
    assert len(rows) >= 1 and rows[-1]["url"].startswith(server)
    # ④ 截图
    png = inst.screenshot(sid)
    assert png[:4] == b"\x89PNG"
    # ⑤ content
    c = inst.act(sid, "content")
    assert "IT WORKS" in c["content"]
    # ⑥ 会话上限 2 只数 AI 页（F6-v4：human-main 豁免不计）
    inst.ensure_human_session()
    inst.open_session("sess-" + "f" * 12, "human")  # 第 2 个 AI 页（sid 是第 1 个）
    with pytest.raises(BrowserError):
        inst.open_session("sess-" + "0" * 12, "human")
    # ⑦ browser.action 审计齐了（navigate/screenshot/content，author=owner）
    events = [e for e in bb.recent_events(pid) if e["kind"] == "browser.action"]
    actions = {e["payload"]["action"] for e in events}
    assert {"navigate", "screenshot", "content"} <= actions
    assert {e["author"] for e in events} <= {sid, unregistered_sid}


def test_navigate_self_signed_https_ignored(pool, https_server, tmp_path):
    """证书过期/自签默认放行：自签 HTTPS 目标导航 200；ignore_https_errors=False
    的独立严格实例对同一目标必须报证书错误。"""
    from core.browser.pool import BrowserError
    bpool, bb, pid = pool
    inst = bpool.get_instance(pid)
    sid = "sess-" + "h" * 12
    inst.open_session(sid, sid)
    r = inst.navigate(sid, f"{https_server}/")
    assert r["status"] == 200 and r["target_host"] == "127.0.0.1"
    c = inst.act(sid, "content")
    assert "HTTPS WORKS" in c["content"]
    inst.close_session(sid)

    strict = BrowserPool(tmp_path / "strict", bb_getter=lambda pid: bb,
                         config=BrowserConfig(ignore_https_errors=False,
                                             intercept_timeout_s=2.0))
    try:
        sinst = strict.get_instance(pid)
        ssid = "sess-" + "g" * 12
        sinst.open_session(ssid, ssid)
        with pytest.raises(BrowserError, match="ERR_CERT"):
            sinst.navigate(ssid, f"{https_server}/")
    finally:
        strict.close_all()


def test_persistent_profile_dir_created(pool):
    bpool, bb, pid = pool
    inst = bpool.get_instance(pid)
    sid = "sess-" + "1" * 12
    inst.open_session(sid, sid)  # open_session 即懒启动 Chromium → 建持久 profile
    assert inst.profile_dir.exists()


def test_screencast_frames(pool, server):
    """F6-v2：attach_screencast 收 CDP JPEG 帧流；detach 后不再来帧。"""
    import queue as _queue

    bpool, bb, pid = pool
    inst = bpool.get_instance(pid)
    sid = "human-" + "s" * 12
    inst.open_session(sid, "human")
    inst.navigate(sid, f"{server}/")
    q: "_queue.Queue[dict]" = _queue.Queue()
    inst.attach_screencast(sid, q.put)
    try:
        frame = q.get(timeout=20)  # 页面静止时 Chromium 也至少推首帧
        assert frame["data"].startswith("/9j/")  # base64 JPEG 魔数
        assert isinstance(frame["metadata"], dict) and frame["ts"] > 0
    finally:
        inst.detach_screencast(sid, q.put)
    # detach 后再触发导航（必出新帧），不应再收到任何帧
    inst.navigate(sid, f"{server}/?again=1")
    time.sleep(1.5)
    assert q.empty()


def test_human_input_takeover(pool, server):
    """F6-v2：human-* 会话接管点击生效；AI 会话调用被红线拒绝；审计 origin=human。"""
    from core.browser.pool import BrowserError

    bpool, bb, pid = pool
    inst = bpool.get_instance(pid)
    human_sid = "human-" + "h" * 12
    inst.open_session(human_sid, "human")
    inst.navigate(human_sid, f"{server}/btn")
    # AI 会话红线：human_input 直接 BrowserError
    ai_sid = "sess-" + "a" * 12
    inst.open_session(ai_sid, ai_sid)
    with pytest.raises(BrowserError):
        inst.human_input(ai_sid, "click", x=10, y=10)
    # 人工点击（页面全屏 div，任意坐标命中）→ 效果落上
    inst.human_input(human_sid, "click", x=100, y=100)
    for _ in range(20):
        if "CLICKED" in inst.act(human_sid, "content")["content"]:
            break
        time.sleep(0.3)
    assert "CLICKED" in inst.act(human_sid, "content")["content"]
    # 审计：browser.action action=human-input 且 origin=human（click 属审计口径）
    evs = [e for e in bb.recent_events(pid) if e["kind"] == "browser.action"
           and e["payload"].get("action") == "human-input"]
    assert evs and all(e["payload"].get("origin") == "human" for e in evs)
    # author=会话 owner（human 会话 open_session 时传 "human"）
    assert any(e["author"] == "human" and e["session_id"] == human_sid for e in evs)


# ---------- F6-v4 拦截 / 去会话化 ----------

def _wait_pending(hub, n=1, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = hub.snapshot()
        if len(snap["pending"]) >= n:
            return snap["pending"]
        time.sleep(0.05)
    raise AssertionError("挂起包未出现")


def _decide_raw(hub, hold, new_raw):
    """e2e 版 decide（解析逻辑同 API 层：parse_raw_* → mods）。"""
    from core.browser.httpmsg import parse_raw_request, parse_raw_response
    if hold["direction"] == "request":
        p = parse_raw_request(new_raw, base_url=hold["url"])
        mods = {"method": p["method"], "url": p["url"],
                "headers": p["headers"], "body": p["body"]}
    else:
        p = parse_raw_response(new_raw)
        mods = {"status": p["status"], "headers": p["headers"], "body": p["body"]}
    hub.decide(hold["hold_id"], "forward", mods)


def test_intercept_e2e(pool, server):
    """请求向改包路径生效+记改后值；drop 不入库；超时放行原文；响应向改 body。"""
    from core.browser.pool import HUMAN_MAIN_SID

    bpool, bb, pid = pool
    inst = bpool.get_instance(pid)
    sid = inst.ensure_human_session()["sid"]
    assert sid == HUMAN_MAIN_SID
    hub = inst._intercept

    # ① 请求向：改 path 放行 → 靶站按改后路径响应，history 记改后 url
    hub.toggle("request", True)
    errs: list[Exception] = []

    def _nav(url):
        try:
            inst.navigate(sid, url)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    t = threading.Thread(target=_nav, args=(f"{server}/btn",))
    t.start()
    hold = _wait_pending(hub)[0]
    _decide_raw(hub, hold, f"GET {server}/btn?mod=1 HTTP/1.1\nHost: x\n\n")
    t.join(30)
    assert not errs, errs
    rows = []
    for _ in range(20):
        rows = [r for r in bb.list_http_history(pid, source="browser")
                if r["url"].endswith("/btn?mod=1")]
        if rows:
            break
        time.sleep(0.3)
    assert rows and rows[0]["meta"]["intercept"]["request_modified"] is True

    # ② drop：导航请求被丢弃（goto 报 net 错误也行），但不入库只落事件
    n_before = len(bb.list_http_history(pid, source="browser"))
    t = threading.Thread(target=_nav, args=(f"{server}/?drop=1",))
    t.start()
    hold = _wait_pending(hub)[0]
    hub.decide(hold["hold_id"], "drop")
    t.join(30)
    time.sleep(0.5)
    rows = bb.list_http_history(pid, source="browser")
    assert len(rows) == n_before  # 无新行
    drops = [e for e in bb.recent_events(pid) if e["kind"] == "browser.intercept"
             and e["payload"].get("action") == "drop"]
    assert drops

    # ③ 关开关：pending 自动放行原文
    hub.toggle("request", False)
    time.sleep(0.5)
    assert hub.snapshot()["pending"] == []
    assert all("net::" in str(e) or "ERR_" in str(e) for e in errs)  # ②的 goto 失败
    errs.clear()

    # ④ 超时（2s）放行原文：不动裁决，导航照常完成
    hub.toggle("response", True)
    t = threading.Thread(target=_nav, args=(f"{server}/",))
    t.start()
    hold = _wait_pending(hub)[0]
    time.sleep(2.5)  # 越过 intercept_timeout_s=2.0
    t.join(30)
    assert not errs, errs
    assert hold["hold_id"] not in {h["hold_id"] for h in hub.snapshot()["pending"]}

    # ⑤ 响应向改 body：页面渲染改后内容，history 记改后响应
    t = threading.Thread(target=_nav, args=(f"{server}/",))
    t.start()
    hold = _wait_pending(hub)[0]
    _decide_raw(hub, hold,
                "HTTP/1.1 200 OK\nContent-Type: text/html\n\n<html><body>MODIFIED-BODY</body></html>")
    t.join(30)
    assert not errs, errs
    for _ in range(20):
        if "MODIFIED-BODY" in inst.act(sid, "content")["content"]:
            break
        time.sleep(0.3)
    assert "MODIFIED-BODY" in inst.act(sid, "content")["content"]
    rows = []
    for _ in range(20):  # _store 在 route 协程尾部落库，可能晚于 goto 返回
        rows = [r for r in bb.list_http_history(pid, source="browser")
                if r["resp_body"] and "MODIFIED-BODY" in r["resp_body"]]
        if rows:
            break
        time.sleep(0.3)
    assert rows and rows[0]["meta"]["intercept"]["response_modified"] is True
    hub.toggle("response", False)


def test_ai_session_auto_close(pool, server):
    """F6-v4：AI 会话可开可关；human-main 豁免不计上限、不受 AI 关闭影响。"""
    bpool, bb, pid = pool
    inst = bpool.get_instance(pid)
    ai_sid = "sess-" + "a" * 12
    inst.open_session(ai_sid, ai_sid)
    assert any(s["sid"] == ai_sid for s in inst.status()["sessions"])
    inst.close_session(ai_sid)
    assert not any(s["sid"] == ai_sid for s in inst.status()["sessions"])
    # 隐式人工会话懒创建 + 幂等
    s1 = inst.ensure_human_session()
    s2 = inst.ensure_human_session()
    assert s1["sid"] == s2["sid"] == "human-main"
    inst.navigate("human-main", f"{server}/")
    rows = [r for r in bb.list_http_history(pid, source="browser")
            if r["session_id"] == "human-main"]
    assert rows
