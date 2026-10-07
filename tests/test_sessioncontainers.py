"""会话级常驻容器管理器测试（P2/P3，2026-10-07）。

两层：
- argv 层：真 DockerBackend + 假 subprocess.Popen，断言 docker 命令行形状；
- 管理器层：假 backend 记录调用，验证 ensure 幂等/并发、exec 自愈、回收与对账。
"""

import threading

import pytest

from core.runtime.backends import DockerBackend, ExecOutcome
from core.runtime.sessioncontainers import (
    SessionContainerManager,
    container_name,
    parse_container_name,
)


# ---------- 命名与解析 ----------

def test_container_name_roundtrip():
    name = container_name("proj-abc123def456", "sess-001122334455")
    assert name == "csp_proj-abc123def456_sess-001122334455"
    assert parse_container_name(name) == ("proj-abc123def456", "sess-001122334455")
    assert parse_container_name("dazzling_bhaskara") is None   # 非本管理器命名


# ---------- argv 层：DockerBackend 新原语 ----------

def _fake_popen(monkeypatch, calls):
    def fake(argv, **kwargs):
        calls.append(argv)

        class P:
            returncode = 0

            def poll(self):
                return 0
        return P()

    monkeypatch.setattr("subprocess.Popen", fake)


def test_run_detached_argv(monkeypatch):
    calls = []
    _fake_popen(monkeypatch, calls)
    DockerBackend().run_detached(
        "img", "sleep infinity", name="csp_p_s",
        labels={"csp.managed": "1", "csp.project": "p", "csp.session": "s"},
        net="bridge", mounts=[(r"E:\proj\ws", "/workspace")], cwd="/workspace/scratch")
    argv = calls[0]
    assert argv[:3] == ["docker", "run", "-d"]
    assert "--name" in argv and argv[argv.index("--name") + 1] == "csp_p_s"
    assert "--rm" not in argv                                  # 常驻容器绝不带 --rm
    assert argv.count("--label") == 3
    assert argv[argv.index("--network") + 1] == "bridge"
    assert argv[argv.index("-v") + 1] == "E:/proj/ws:/workspace"
    assert argv[argv.index("-w") + 1] == "/workspace/scratch"
    assert argv[-4:] == ["img", "sh", "-c", "sleep infinity"]


def test_exec_in_argv_with_cwd(monkeypatch):
    calls = []
    _fake_popen(monkeypatch, calls)
    DockerBackend().exec_in("csp_p_s", "id", cwd="/workspace/scratch")
    assert calls[0] == ["docker", "exec", "-w", "/workspace/scratch",
                        "csp_p_s", "sh", "-c", "id"]


def test_rm_and_ps_by_label_argv(monkeypatch):
    calls = []
    _fake_popen(monkeypatch, calls)
    d = DockerBackend()
    d.rm("csp_p_s")
    assert calls[0] == ["docker", "rm", "-f", "csp_p_s"]
    d.ps_by_label("csp.managed=1")
    argv = calls[1]
    assert argv[:3] == ["docker", "ps", "-a"]
    assert argv[argv.index("--filter") + 1] == "label=csp.managed=1"
    assert argv[argv.index("--format") + 1] == "{{.Names}}"


# ---------- 管理器层：假 backend ----------

class _FakeBackend:
    def __init__(self):
        self.calls = []
        self.running = {}
        self.listing = []
        self.exec_result = ExecOutcome(exit_code=0, stdout="out")

    def run_detached(self, image, cmd, *, name, labels, net, mounts, cwd, timeout):
        self.calls.append(("run_detached", name, labels, net, mounts, cwd))
        self.running[name] = True
        return ExecOutcome(exit_code=0, stdout=name)

    def exec_in(self, container, cmd, timeout=120.0, abort_event=None, cwd=None):
        self.calls.append(("exec_in", container, cmd, cwd))
        return self.exec_result

    def inspect_running(self, container, timeout=15.0):
        return self.running.get(container, False)

    def restart(self, container, timeout=30.0):
        self.calls.append(("restart", container))
        return ExecOutcome(exit_code=0)

    def rm(self, container, timeout=30.0):
        self.calls.append(("rm", container))
        self.running.pop(container, None)
        return ExecOutcome(exit_code=0)

    def ps_by_label(self, selector, timeout=15.0):
        self.calls.append(("ps", selector))
        return list(self.listing)

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


@pytest.fixture()
def mgr(monkeypatch):
    """禁用 reaper 线程（避免异步 reconcile 污染调用记录）；各用例直调同步方法。"""
    monkeypatch.setattr(SessionContainerManager, "_start_reaper",
                        lambda self, **kw: None)
    backend = _FakeBackend()
    m = SessionContainerManager(backend, enabled=True, idle_timeout=1800,
                                reaper_interval=60)
    return m, backend


def test_ensure_creates_then_reuses(mgr):
    m, b = mgr
    name = m.ensure("proj-1", "sess-a", image="img")
    assert name == "csp_proj-1_sess-a"
    assert b.count("run_detached") == 1
    assert m.ensure("proj-1", "sess-a") == name            # 二次复用
    assert b.count("run_detached") == 1
    # labels 含 managed/project/session
    _kind, _n, labels, _net, _mnt, _cwd = b.calls[0]
    assert labels["csp.managed"] == "1" and labels["csp.project"] == "proj-1"


def test_ensure_concurrent_only_one_run(mgr):
    m, b = mgr
    results = []

    def worker():
        results.append(m.ensure("proj-1", "sess-c", image="img"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.count("run_detached") == 1                    # 并发只建一次
    assert set(results) == {"csp_proj-1_sess-c"}


def test_ensure_rebuilds_when_container_gone(mgr):
    m, b = mgr
    name = m.ensure("proj-1", "sess-d", image="img")
    b.running[name] = False                                # 容器被外部删/退出
    assert m.ensure("proj-1", "sess-d") == name
    assert b.count("run_detached") == 2                    # 重建


def test_exec_uses_exec_in_with_cwd(mgr):
    m, b = mgr
    out = m.exec("proj-1", "sess-e", "id", timeout=30, cwd="/workspace/scratch")
    assert out.stdout == "out"
    assert ("exec_in", "csp_proj-1_sess-e", "id", "/workspace/scratch") in b.calls


def test_exec_self_heals_once(mgr):
    m, b = mgr
    b.exec_result = ExecOutcome(exit_code=1, stderr="Error: container x is not running")
    out = m.exec("proj-1", "sess-f", "id", timeout=30)
    assert b.count("exec_in") == 2                         # 首失 → 重建 → 重试
    assert b.count("run_detached") == 2                    # 首次建 + 自愈重建
    assert out.exit_code == 1


def test_exec_marks_dirty_then_restart_on_next_ensure(mgr):
    m, b = mgr
    b.exec_result = ExecOutcome(exit_code=-1, interrupted=True)
    m.exec("proj-1", "sess-g", "nmap x", timeout=30)
    m.ensure("proj-1", "sess-g")                           # dirty → restart 清场
    assert b.count("restart") == 1


def test_exec_returns_none_when_disabled():
    m = SessionContainerManager(_FakeBackend(), enabled=False)
    assert m.exec("p", "sess-x", "id") is None
    assert m.ensure("p", "sess-x") is None


def test_remove_calls_rm(mgr):
    m, b = mgr
    m.ensure("proj-1", "sess-h", image="img")
    m.remove("proj-1", "sess-h")
    assert ("rm", "csp_proj-1_sess-h") in b.calls


def test_remove_project_by_label(mgr):
    m, b = mgr
    b.listing = ["csp_proj-9_sess-1", "csp_proj-9_sess-2"]
    m.remove_project("proj-9")
    assert ("ps", "csp.project=proj-9") in b.calls
    assert b.count("rm") == 2


def test_reconcile_removes_all_managed(mgr):
    m, b = mgr
    b.listing = ["csp_p_s1", "csp_p_s2"]
    m.reconcile()
    assert ("ps", "csp.managed=1") in b.calls
    assert b.count("rm") == 2


def test_reap_orphans_idle_and_unowned_stopped(mgr):
    m, b = mgr
    m.idle_timeout = -1.0                                  # 任何已注册容器立即算空闲
    m.ensure("proj-1", "sess-i", image="img")
    b.listing = ["csp_proj-1_sess-i", "csp-orphan-stopped", "csp-orphan-running"]
    b.running["csp-orphan-running"] = True                 # 运行中的无主容器不误删
    m.reap_orphans()
    rm_names = [c[1] for c in b.calls if c[0] == "rm"]
    assert "csp_proj-1_sess-i" in rm_names                 # 注册表内空闲 → 删
    assert "csp-orphan-stopped" in rm_names                # 无主且已停 → 删
    assert "csp-orphan-running" not in rm_names            # 无主但运行中 → 保留


def test_shutdown_all_clears(mgr):
    m, b = mgr
    m.ensure("proj-1", "sess-j", image="img")
    b.listing = ["csp_proj-1_sess-j"]
    m.shutdown_all()
    assert b.count("rm") == 1
    assert m.ensure("proj-1", "sess-k") is None            # 关后拒绝新建
