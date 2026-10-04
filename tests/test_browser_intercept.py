"""F6-v4 拦截单测（core/browser/intercept.py + capture.py 的 hold/裁决流程）。

零 playwright：FakeRoute/FakeRequest 复刻 route.fetch/fulfill/abort/continue_。
人工与 AI 页面均可挂起裁决；测试覆盖跨线程 hold→裁决→放行。线程模型与真机一致——
route 协程跑在独立 asyncio loop 线程（FakeInstance._loop），裁决（toggle/decide）
从测试主线程（≈API 线程）经 call_soon_threadsafe 回环。
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from core.blackboard import Blackboard
from core.browser.capture import CaptureTap
from core.browser.intercept import InterceptHub
from core.browser.pool import HUMAN_MAIN_SID

_TIMEOUT = 5.0


class _Page:
    """占位 page 对象——_sid_of 只做身份比较。"""


class _Entry:
    def __init__(self, page, task_id=None):
        self.page = page
        self.info = type("Info", (), {"task_id": task_id})()


class _Request:
    def __init__(self, method, url, headers=None, body=None, page=None):
        self.method = method
        self.url = url
        self.headers = headers or {"host": "127.0.0.1"}
        self.post_data_buffer = body
        self.frame = type("Frame", (), {"page": page})()


class _Response:
    def __init__(self, status=200, headers=None, body=b"ok"):
        self.status = status
        self.headers = headers or {"content-type": "application/json"}
        self._body = body

    async def body(self):
        return self._body


class _Route:
    def __init__(self, request, resp=None):
        self.request = request
        self._resp = resp or _Response()
        self.fetch_calls: list[dict] = []
        self.fulfill_calls: list[dict] = []
        self.aborted = False
        self.continued = False

    async def fetch(self, **kw):
        self.fetch_calls.append(kw)
        return self._resp

    async def fulfill(self, **kw):
        self.fulfill_calls.append(kw)

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class _LoopRunner:
    """独立 asyncio loop 线程（≈BrowserInstance 的 loop 线程）。"""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(_TIMEOUT)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class FakeInstance:
    def __init__(self, bb, pid, *, timeout_s=120.0):
        self.project_id = pid
        self.bb = bb
        self.config = type("Cfg", (), {"body_max_bytes": 65536})()
        self._pages = {HUMAN_MAIN_SID: _Entry(_Page()),
                       "agent-1": _Entry(_Page(), task_id="t-1")}
        self._intercept = InterceptHub(self, timeout_s=timeout_s)
        self._loop = None  # 由 fixture 注入 LoopRunner.loop


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def runner():
    r = _LoopRunner()
    yield r
    r.close()


@pytest.fixture()
def inst(bb, runner):
    pid = bb.create_project("拦截测试", "pentest", ["web"])["id"]
    fake = FakeInstance(bb, pid)
    fake._loop = runner.loop
    return fake


def _route(inst, sid=HUMAN_MAIN_SID, *, method="GET", url="http://127.0.0.1:9/a",
           body=None, resp=None):
    page = inst._pages[sid].page
    req = _Request(method, url, body=body, page=page)
    return _Route(req, resp)


def _wait_pending(hub, n=1):
    import time
    deadline = time.time() + _TIMEOUT
    while time.time() < deadline:
        if len(hub.snapshot()["pending"]) >= n:
            return hub.snapshot()["pending"]
        time.sleep(0.02)
    raise AssertionError("挂起包未出现")


# ---------- 请求向 ----------

def test_request_hold_forward_modified(inst, runner):
    inst._intercept.toggle("request", True)
    route = _route(inst)
    t = threading.Thread(target=runner.run,
                         args=(CaptureTap(inst)._on_route(route),))
    t.start()
    snap = _wait_pending(inst._intercept)
    assert snap[0]["direction"] == "request" and snap[0]["editable"] is True
    assert snap[0]["method"] == "GET" and snap[0]["url"] == route.request.url
    raw = "POST http://127.0.0.1:9/b HTTP/1.1\nHost: 127.0.0.1:9\nX-Tag: mod\n\nk=v"
    # decide 从主线程（≈API 线程）发起，经 call_soon_threadsafe 回 loop 线程 resolve
    res = inst._intercept.decide(snap[0]["hold_id"], "forward",
                                 {"method": "POST", "url": "http://127.0.0.1:9/b",
                                  "headers": {"host": "127.0.0.1:9",
                                              "content-length": "3",
                                              "x-tag": "mod"},
                                  "body": "k=v"})
    assert res is not None
    t.join(_TIMEOUT)
    # 改包走 fetch 覆写（content-length/host 被剥，其余头保留），fulfill 原样响应
    assert route.fetch_calls == [{"method": "POST", "url": "http://127.0.0.1:9/b",
                                  "headers": {"x-tag": "mod"},
                                  "post_data": b"k=v"}]
    assert route.fulfill_calls and "response" in route.fulfill_calls[0]
    # 记录按改后值入库 + meta.intercept 留痕
    rows = inst.bb.list_http_history(inst.project_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["method"] == "POST" and row["url"] == "http://127.0.0.1:9/b"
    assert row["meta"]["intercept"]["request_modified"] is True


def test_request_drop_no_history(inst, runner):
    inst._intercept.toggle("request", True)
    route = _route(inst)
    t = threading.Thread(target=runner.run,
                         args=(CaptureTap(inst)._on_route(route),))
    t.start()
    snap = _wait_pending(inst._intercept)
    inst._intercept.decide(snap[0]["hold_id"], "drop")
    t.join(_TIMEOUT)
    assert route.aborted and route.fetch_calls == [] and route.fulfill_calls == []
    assert inst.bb.list_http_history(inst.project_id) == []
    kinds = [e["kind"] for e in inst.bb.recent_events(inst.project_id)]
    assert "browser.intercept" in kinds
    drop = [e for e in inst.bb.recent_events(inst.project_id)
            if e["kind"] == "browser.intercept"][-1]
    payload = drop["payload"]
    assert payload["action"] == "drop" and payload["direction"] == "request"


def test_request_timeout_forwards_original(inst, runner):
    fake = FakeInstance(inst.bb, inst.project_id, timeout_s=0.2)
    fake._loop = runner.loop
    fake._pages = inst._pages
    fake._intercept.toggle("request", True)
    route = _route(fake)
    runner.run(CaptureTap(fake)._on_route(route))
    assert route.fetch_calls == [{}]  # 无覆写
    assert route.fulfill_calls and "response" in route.fulfill_calls[0]
    assert inst._intercept.snapshot()["pending"] == []


def test_toggle_off_releases_pending(inst, runner):
    inst._intercept.toggle("request", True)
    route = _route(inst)
    t = threading.Thread(target=runner.run,
                         args=(CaptureTap(inst)._on_route(route),))
    t.start()
    _wait_pending(inst._intercept)
    inst._intercept.toggle("request", False)
    assert inst._intercept.snapshot()["request_enabled"] is False
    t.join(_TIMEOUT)
    assert route.fetch_calls == [{}]  # 放行原文
    assert inst._intercept.snapshot()["pending"] == []


# ---------- 响应向 ----------

def test_response_hold_forward_modified_body(inst, runner):
    inst._intercept.toggle("response", True)
    route = _route(inst, resp=_Response(200, {"content-type": "application/json"},
                                        b'{"a":1}'))
    t = threading.Thread(target=runner.run,
                         args=(CaptureTap(inst)._on_route(route),))
    t.start()
    snap = _wait_pending(inst._intercept)
    assert snap[0]["direction"] == "response" and snap[0]["status"] == 200
    raw = "HTTP/1.1 403 Forbidden\nContent-Type: application/json\n\n{\"deny\":true}"
    inst._intercept.decide(snap[0]["hold_id"], "forward",
                           {"status": 403,
                            "headers": {"content-type": "application/json"},
                            "body": '{"deny":true}'})
    t.join(_TIMEOUT)
    assert route.fetch_calls == [{}]
    call = route.fulfill_calls[0]
    assert call["status"] == 403 and b'"deny"' in call["body"]
    assert "content-encoding" not in {k.lower() for k in call["headers"]}
    row = inst.bb.list_http_history(inst.project_id)[0]
    assert row["status"] == 403
    assert row["meta"]["intercept"]["response_modified"] is True


# ---------- 范围与上限 ----------

def test_ai_session_hold_and_forward(inst, runner):
    """F6-v4：AI 页面同样进入拦截队列，逐个裁决后正常放行并入库。"""
    inst._intercept.toggle("request", True)
    inst._intercept.toggle("response", True)
    route = _route(inst, sid="agent-1")
    t = threading.Thread(target=runner.run,
                         args=(CaptureTap(inst)._on_route(route),))
    t.start()
    req = _wait_pending(inst._intercept)
    assert req[0]["direction"] == "request"
    inst._intercept.decide(req[0]["hold_id"], "forward")
    resp = _wait_pending(inst._intercept)
    assert resp[0]["direction"] == "response"
    inst._intercept.decide(resp[0]["hold_id"], "forward")
    t.join(_TIMEOUT)
    assert not t.is_alive()
    assert route.fetch_calls == [{}] and route.fulfill_calls
    assert inst._intercept.snapshot()["pending"] == []
    assert len(inst.bb.list_http_history(inst.project_id)) == 1


def test_pending_overflow_forwards(inst, runner):
    inst._intercept.toggle("request", True)

    async def _fill(hub):
        for _ in range(hub.MAX_PENDING):
            h = hub.new_hold(direction="request", method="GET",
                             url="http://127.0.0.1:9/x", status=None)
            assert hub.try_register(h)

    runner.run(_fill(inst._intercept))
    route = _route(inst)
    runner.run(CaptureTap(inst)._on_route(route))  # 注册失败 → 原文放行
    assert route.fetch_calls == [{}] and route.fulfill_calls
