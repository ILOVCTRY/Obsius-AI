"""F6 Agent 工具面单测（不依赖 playwright）：
no-tool 降级回填 / 计划闸放行集 / BrowserError→[拒绝] / 轨门控装配语义。"""

import socket

import pytest

from core.agent.tools import AGENT_TOOLS, ToolDispatcher, _PLAN_PRE_ALLOWED
from core.blackboard import Blackboard
from core.runtime.gateway import ExecutionGateway

from test_agent import FakeDockerBackend, NativeBackend


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("断网")))
    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("浏览器测试", "pentest", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend(),
                                           "sandbox": FakeDockerBackend()})
    yield bb, project, gw, tmp_path
    bb.close()


def _disp(env, **kw):
    bb, project, gw, _ = env
    sid = "sess-" + "b" * 12
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid, **kw)
    d._intent_lead_passed = True  # 本组用例主题=浏览器工具面，跳过意图先行闸
    return d


# ---------- schema 与计划闸 ----------

def test_browser_tools_in_schema():
    names = {t["name"] for t in AGENT_TOOLS}
    assert {"browser_navigate", "browser_click", "browser_type",
            "browser_screenshot", "browser_content", "browser_back"} <= names
    # 只读侦察先行；click/type/back 受计划闸
    assert "browser_navigate" in _PLAN_PRE_ALLOWED
    assert "browser_screenshot" in _PLAN_PRE_ALLOWED
    assert "browser_content" in _PLAN_PRE_ALLOWED
    assert "browser_click" not in _PLAN_PRE_ALLOWED
    assert "browser_type" not in _PLAN_PRE_ALLOWED


def test_no_tool_fallback_when_not_injected(env):
    """未接入 BrowserPool（轨外/测试）：六工具统一 no-tool 回填，不 500。"""
    d = _disp(env)
    for name, args in [("browser_navigate", {"url": "http://1.2.3.4"}),
                       ("browser_click", {"selector": "#x"}),
                       ("browser_type", {"selector": "#u", "text": "a"}),
                       ("browser_screenshot", {}),
                       ("browser_content", {}),
                       ("browser_back", {})]:
        r = d.dispatch(name, args)
        assert r.startswith("[no-tool]"), (name, r)


def test_no_tool_fallback_when_dependency_missing(env, monkeypatch):
    """接入了池但 playwright 未装：同样 no-tool 安装指引（不 500）。"""
    import core.browser.pool as pool_mod
    monkeypatch.setattr(pool_mod, "browser_available", lambda: False)
    d = _disp(env, browser=object())
    r = d.dispatch("browser_navigate", {"url": "http://1.2.3.4"})
    assert r.startswith("[no-tool]") and "playwright install" in r


# ---------- 接入假池后的行为 ----------

class _FakeInstance:
    """最小 BrowserInstance 假身：记录调用、可注入 BrowserError。"""

    def __init__(self, bb, pid):
        self.bb, self.project_id = bb, pid
        self.calls = []
        self.error = None

    def _raise(self):
        from core.browser.pool import BrowserError
        if self.error:
            raise BrowserError(self.error)

    def get_instance(self, _pid):
        return self

    def open_session(self, sid, owner):
        self.calls.append(("open", sid))

    def set_task_id(self, sid, tid):
        self.calls.append(("task", tid))

    def navigate(self, sid, url):
        self._raise()
        self.calls.append(("navigate", url))
        return {"final_url": url, "title": "T", "status": 200,
                "target_host": "1.2.3.4", "duration_ms": 5}

    def act(self, sid, action, **kw):
        self._raise()
        self.calls.append((action, kw))
        if action == "content":
            return {"content": "hello", "truncated": False}
        return {"final_url": "http://1.2.3.4/", "title": "T"}

    def screenshot(self, sid):
        self._raise()
        self.calls.append(("screenshot",))
        return b"\x89PNG fake"

    def close_session(self, sid):
        self.calls.append(("close", sid))


def _fake(env):
    bb, project, gw, _ = env

    class _Pool:
        def __init__(self):
            self.inst = _FakeInstance(bb, project["id"])

        def get_instance(self, pid):
            return self.inst

    return _Pool()


def test_navigate_and_content_via_fake_pool(env):
    pool = _fake(env)
    d = _disp(env, browser=pool, artifacts_dir=None)
    r = d.dispatch("browser_navigate", {"url": "http://1.2.3.4"})
    assert "已导航" in r and "1.2.3.4" in r
    r = d.dispatch("browser_content", {})
    assert "hello" in r
    assert any(c[0] == "navigate" for c in pool.inst.calls)
    # 审计（browser.action）由真实 BrowserInstance 落库，见 tests/test_browser_e2e.py


def test_navigate_denied_maps_to_ju_jue(env):
    pool = _fake(env)
    pool.inst.error = "目标 9.9.9.9 未登记"
    d = _disp(env, browser=pool)
    r = d.dispatch("browser_navigate", {"url": "http://9.9.9.9"})
    assert r.startswith("[拒绝]") and "未登记" in r


def test_screenshot_writes_artifact(env):
    pool = _fake(env)
    bb, project, gw, tmp_path = env
    art = tmp_path / "artifacts"
    art.mkdir()
    d = _disp(env, browser=pool, artifacts_dir=str(art))
    r = d.dispatch("browser_screenshot", {})
    assert "browser-shots/" in r
    files = list((art / "browser-shots").glob("*.png"))
    assert len(files) == 1 and files[0].read_bytes().startswith(b"\x89PNG")
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "artifact.new" in kinds


def test_close_browser_session_lifecycle(env):
    """F6-v3：会话收尾自动清除本会话浏览器 Page——任务机制退役后仅 finish
    收尾触发（_tool_finish 调 _close_browser_session）。"""
    pool = _fake(env)
    d = _disp(env, browser=pool, artifacts_dir=None)
    d.dispatch("finish", {"summary": "s"})
    assert ("close", d.session_id) in pool.inst.calls
