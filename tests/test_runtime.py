"""runtime 层测试：策略映射、网关四层防线、真实本机执行、审计、detector。

真实执行仅限 host 上无害命令（echo / exit），docker/wsl 交互用 fake backend。
"""

import platform

import pytest

from core.blackboard import Blackboard
from core.runtime import (
    DockerBackend,
    ExecutionGateway,
    GatewayDenied,
    HostDetector,
    NativeBackend,
    allowed_runtimes,
)
from core.runtime.backends import ExecOutcome
from core.runtime.policy import Level, RUNTIME_LEVELS, allowed_levels


# ---------- 策略映射（§7 隔离等级） ----------

def test_policy_threat_class_mapping():
    assert allowed_runtimes("malware_live") == {"sandbox"}
    assert allowed_runtimes("untrusted") == {"docker", "sandbox"}
    assert allowed_runtimes("trusted") == {"host", "wsl", "docker", "sandbox"}
    # 未知 → 按恶意处理（安全默认值）
    assert allowed_runtimes("unknown") == {"sandbox"}
    assert allowed_levels("whatever-else") == {Level.L3_SANDBOX}
    assert RUNTIME_LEVELS["wsl"] == Level.L1_WSL  # WSL 与宿主同级


# ---------- 网关：拒绝路径（第②层防线） ----------

@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "rt.db"))
    yield board
    board.close()


def test_gateway_denies_malware_on_host(bb):
    pid = bb.create_project("t", "reverse")["id"]
    gw = ExecutionGateway(bb=bb)
    with pytest.raises(GatewayDenied, match="策略拒绝"):
        gw.run("echo hi", runtime="host", threat_class="malware_live", project_id=pid)
    # 拒绝必须落审计（audit.deny 事件，§7 第④层）
    deny = [e for e in bb.recent_events(pid) if e["kind"] == "audit.deny"]
    assert len(deny) == 1
    assert deny[0]["payload"]["runtime"] == "host"


def test_gateway_denies_unknown_threat_on_host(bb):
    pid = bb.create_project("t2", "pentest")["id"]
    gw = ExecutionGateway(bb=bb)
    with pytest.raises(GatewayDenied):
        gw.run("echo hi", runtime="host", threat_class="unknown", project_id=pid)


def test_gateway_allows_real_net_without_approval(bb):
    """net=real 不再人工审批（2026-10-01）：sandbox 直接指定 net=real 即执行，
    不产生 approval.* 事件；隔离等级/threat_class 约束不变（unknown 仍拒）。"""
    pid = bb.create_project("t3", "reverse")["id"]
    seen = {}

    class FakeSandbox:
        def run_once(self, image, cmd, *, net, sandbox, timeout, abort_event=None):
            seen["net"] = net
            return ExecOutcome(exit_code=0, stdout="done")

    gw = ExecutionGateway(bb=bb, backends={"sandbox": FakeSandbox()})
    r = gw.run("wget http://c2.example", runtime="sandbox", threat_class="malware_live",
               project_id=pid, net="real")
    assert r.ok and seen["net"] == "real"
    kinds = {e["kind"] for e in bb.recent_events(pid)}
    assert not any(k.startswith("approval.") for k in kinds)
    # 隔离等级红线不受影响：unknown（按 malware_live 处理）不允许 host
    with pytest.raises(GatewayDenied):
        gw.run("echo hi", runtime="host", threat_class="unknown",
               project_id=pid, net="real")


# ---------- 网关：真实执行（host，无害命令） ----------

@pytest.mark.skipif(platform.system() != "Windows", reason="Windows 路径")
def test_gateway_native_execution_and_audit(bb):
    pid = bb.create_project("t4", "reverse")["id"]
    gw = ExecutionGateway(bb=bb)
    r = gw.run("Write-Output runtime-ok", runtime="host", threat_class="trusted",
               project_id=pid, timeout=30)
    assert r.ok and r.runtime == "host"
    assert "runtime-ok" in r.stdout
    kinds = [e["kind"] for e in bb.recent_events(pid)]
    assert "command" in kinds and "command.result" in kinds
    cmd_ev = [e for e in bb.recent_events(pid) if e["kind"] == "command"][0]
    assert cmd_ev["payload"]["runtime"] == "host"  # 审计带 runtime 标签


def test_gateway_timeout_kills(bb):
    pid = bb.create_project("t5", "ctf")["id"]
    gw = ExecutionGateway(bb=bb)
    r = gw.run("Start-Sleep -Seconds 30", runtime="host", threat_class="trusted",
               project_id=pid, timeout=3)
    assert r.timed_out and not r.ok


def test_gateway_abort_kills_running_command(bb):
    """■ 即点即停（2026-09-19）：abort_event 置位 → 运行中命令立刻被杀
    （不等超时/自然结束），结果 interrupted、ok=False；command.result 事件
    带中断标记。"""
    import threading
    pid = bb.create_project("t5b", "ctf")["id"]
    gw = ExecutionGateway(bb=bb)
    abort = threading.Event()
    timer = threading.Timer(0.5, abort.set)
    timer.start()
    r = gw.run("Start-Sleep -Seconds 20", runtime="host", threat_class="trusted",
               project_id=pid, timeout=30, abort_event=abort)
    timer.join()
    assert r.interrupted and not r.ok
    assert "被人手中断" in r.brief()
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "command.result"][-1]
    assert ev["payload"].get("interrupted") is True


def test_native_backend_large_output_drained():
    """管道死锁回归（2026-09-23）：stdout 超 Windows 管道缓冲（64KB）时排水
    线程必须持续消费、进程正常退出；旧实现 poll 轮询不读管道 → 双方死锁到
    timeout 杀树（实战 wsl grep 单行 120KB spill JSON 两条命令各挂 120.8s）。"""
    if platform.system() != "Windows":
        pytest.skip("host 后端 PowerShell 大输出")
    import time as _t
    from core.runtime.backends import NativeBackend
    b = NativeBackend()
    t0 = _t.monotonic()
    r = b.execute("$s='A'*200000; Write-Output $s", timeout=30.0)
    dt = _t.monotonic() - t0
    assert r.exit_code == 0 and not r.timed_out and not r.interrupted
    assert len(r.stdout) >= 200_000
    assert dt < 15  # 旧实现挂满 30s（timed_out）；排水后秒级返回


def test_brief_truncation_marker():
    """截断可见化（2026-09-20）：stdout/stderr 被切时尾部带显式标注（显示/总字符数），
    Agent 不必靠语义猜输出不完整。"""
    from core.runtime.gateway import ExecutionResult
    r = ExecutionResult(ok=True, exit_code=0, stdout="x" * 3000, stderr="",
                        runtime="host", duration_s=0.1)
    b = r.brief(limit=2000)
    assert "stdout 已截断" in b and "2000/3000 字符" in b and b.count("x" * 10) >= 1
    r2 = ExecutionResult(ok=True, exit_code=0, stdout="short", stderr="e" * 2500,
                         runtime="host", duration_s=0.1)
    b2 = r2.brief(limit=2000)
    assert "stderr 已截断" in b2 and "2000/2500 字符" in b2
    r3 = ExecutionResult(ok=True, exit_code=0, stdout="short", stderr="",
                         runtime="host", duration_s=0.1)
    assert "已截断" not in r3.brief()


def test_brief_timeout_guide():
    """超时引导（2026-10-01）：超时回执带「已跑约 N 秒」+ 分段/小 timeout 引导；
    非超时回执不带该引导。"""
    from core.runtime.gateway import ExecutionResult
    r = ExecutionResult(ok=False, exit_code=-1, stdout="partial", stderr="",
                        runtime="host", duration_s=12.4, timed_out=True)
    b = r.brief()
    assert "超时被杀" in b and "已跑约 12s" in b
    assert "[超时引导]" in b and "拆成多段" in b and "max-time" in b
    ok = ExecutionResult(ok=True, exit_code=0, stdout="x", stderr="",
                         runtime="host", duration_s=0.1)
    assert "[超时引导]" not in ok.brief()


# ---------- docker 后端：参数构造（fake subprocess） ----------

def test_sandbox_docker_args(monkeypatch):
    from core.runtime.backends import sandbox_docker_args
    args = sandbox_docker_args("none")
    assert "--rm" in args and args[args.index("--network") + 1] == "none"
    assert "--cap-drop" in args  # 降权
    with pytest.raises(NotImplementedError, match="fakenet"):
        sandbox_docker_args("fakenet")  # 下一里程碑，未实现必须显式失败


def test_docker_run_once_sandbox_invocation(monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        class P:
            returncode = 0

            def poll(self):
                return 0

            def communicate(self):
                return ("ok", "")
        return P()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    d = DockerBackend()
    d.run_once("anal-image", "id", net="none", sandbox=True)
    argv = calls[0]
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--rm" in argv                      # 一次性
    assert "anal-image" in argv and "sh" in argv
    assert "-c" in argv and "id" in argv
    # 禁止宿主挂载：sandbox 参数里不允许出现 -v/--volume
    assert "-v" not in argv and "--volume" not in argv


def test_docker_run_once_mounts_and_cwd(monkeypatch):
    """pentest-tools-container-m0：L2 workspace 卷挂载 + 容器 cwd 落 argv
    （宿主路径反斜杠转正斜杠）；sandbox 忽略 mounts（L3 零挂载铁律）。"""
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        class P:
            returncode = 0

            def poll(self):
                return 0
        return P()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    d = DockerBackend()
    d.run_once("img", "id", net="bridge", mounts=[(r"E:\proj\ws", "/workspace")],
               cwd="/workspace/scratch")
    argv = calls[0]
    assert "--rm" in argv                        # P0：L2 一次性生命周期（曾漏致残留堆积）
    assert argv[argv.index("-v") + 1] == "E:/proj/ws:/workspace"
    assert argv[argv.index("-w") + 1] == "/workspace/scratch"
    d.run_once("img", "id", net="none", sandbox=True,
               mounts=[(r"E:\proj\ws", "/workspace")], cwd="/workspace/scratch")
    argv2 = calls[1]
    assert "-v" not in argv2 and "-w" not in argv2  # L3 零挂载且 cwd 参数被忽略


def test_gateway_docker_workspace_mount_and_deny(bb, tmp_path):
    """docker runtime workspace（容器化 M0）：默认 pentest-box 镜像 + 整卷挂载 +
    cwd=/workspace/scratch；pathguard 校验切容器内路径语义，逃逸仍拦。"""
    from core.runtime.backends import DEFAULT_PENTEST_IMAGE

    pid = bb.create_project("t-dk", "pentest")["id"]

    class FakeDocker:
        def __init__(self):
            self.calls = []

        def run_once(self, image, cmd, *, net, sandbox, timeout,
                     abort_event=None, mounts=None, cwd=None):
            self.calls.append({"image": image, "cmd": cmd, "net": net,
                               "sandbox": sandbox, "mounts": mounts, "cwd": cwd})
            return ExecOutcome(exit_code=0, stdout="ok")

    fd = FakeDocker()
    gw = ExecutionGateway(bb=bb, backends={"docker": fd})
    ws = tmp_path / "ws"
    r = gw.run("nmap -T3 -p 80 target", runtime="docker", threat_class="trusted",
               project_id=pid, workspace=ws)
    assert r.exit_code == 0
    call = fd.calls[-1]
    assert call["image"] == DEFAULT_PENTEST_IMAGE
    assert call["mounts"] == [(str(ws), "/workspace")]
    assert call["cwd"] == "/workspace/scratch"
    assert call["net"] == "bridge" and call["sandbox"] is False
    # 相对路径写 scratch 放行（posix 语义）
    gw.run("echo hi > out.txt", runtime="docker", threat_class="trusted",
           project_id=pid, workspace=ws)
    # 容器内绝对路径写 /workspace 外仍拦
    with pytest.raises(GatewayDenied, match="工作区隔离"):
        gw.run("curl -o /etc/evil x", runtime="docker", threat_class="trusted",
               project_id=pid, workspace=ws)


# ---------- 网关：会话级常驻容器路由（P3，2026-10-07） ----------

_UNSET = object()


class _FakeManager:
    """假 SessionContainerManager：记录 exec 调用，供路由断言。"""

    def __init__(self, enabled=True, result=_UNSET):
        self.enabled = enabled
        self.execs = []
        self._result = (ExecOutcome(exit_code=0, stdout="via-exec")
                        if result is _UNSET else result)

    def exec(self, pid, sid, cmd, *, timeout, abort_event, image, net, mounts, cwd):
        self.execs.append({"pid": pid, "sid": sid, "cmd": cmd, "net": net,
                           "mounts": mounts, "cwd": cwd})
        return self._result


class _RecDocker:
    def __init__(self):
        self.calls = []

    def run_once(self, image, cmd, *, net, sandbox, timeout,
                 abort_event=None, mounts=None, cwd=None):
        self.calls.append({"image": image, "cmd": cmd, "net": net,
                           "sandbox": sandbox, "mounts": mounts, "cwd": cwd})
        return ExecOutcome(exit_code=0, stdout="via-runonce")


def test_gateway_docker_sess_routes_to_manager(bb, tmp_path):
    """sess-* 会话 + 管理器可用 → 走 exec 复用，不回落 run_once。"""
    pid = bb.create_project("t-sc", "pentest")["id"]
    fd, fm = _RecDocker(), _FakeManager()
    gw = ExecutionGateway(bb=bb, backends={"docker": fd}, containers=fm)
    ws = tmp_path / "ws"
    r = gw.run("id", runtime="docker", threat_class="trusted", project_id=pid,
               session_id="sess-001122334455", workspace=ws)
    assert r.stdout == "via-exec"
    assert fd.calls == []                              # 未回落 per-call
    assert fm.execs[0]["mounts"] == [(str(ws), "/workspace")]
    assert fm.execs[0]["cwd"] == "/workspace/scratch"
    assert fm.execs[0]["net"] == "bridge"


def test_gateway_docker_falls_back_without_session(bb, tmp_path):
    """无会话 / 非 sess- 前缀 / 管理器禁用 → 一律回落 run_once（绝不失败）。"""
    pid = bb.create_project("t-fb", "pentest")["id"]
    fd, fm = _RecDocker(), _FakeManager()
    gw = ExecutionGateway(bb=bb, backends={"docker": fd}, containers=fm)
    ws = tmp_path / "ws"
    for sid in (None, "chat-abc123", "rev-workbench"):
        gw.run("id", runtime="docker", threat_class="trusted", project_id=pid,
               session_id=sid, workspace=ws)
    assert len(fd.calls) == 3 and fm.execs == []

    fm.enabled = False                                  # kill switch
    gw.run("id", runtime="docker", threat_class="trusted", project_id=pid,
           session_id="sess-001122334455", workspace=ws)
    assert len(fd.calls) == 4 and fm.execs == []


def test_gateway_docker_falls_back_when_manager_returns_none(bb, tmp_path):
    """管理器建容器失败（exec 返回 None）→ 回落 run_once，命令仍执行。"""
    pid = bb.create_project("t-none", "pentest")["id"]
    fd, fm = _RecDocker(), _FakeManager(result=None)
    gw = ExecutionGateway(bb=bb, backends={"docker": fd}, containers=fm)
    r = gw.run("id", runtime="docker", threat_class="trusted", project_id=pid,
               session_id="sess-001122334455", workspace=tmp_path / "ws")
    assert r.stdout == "via-runonce" and len(fd.calls) == 1


def test_gateway_sandbox_never_uses_manager(bb):
    """L3 铁律：sandbox 恒走 per-execution run_once，即使有 sess- 会话也不经管理器。"""
    pid = bb.create_project("t-sb", "pentest")["id"]

    class _SandboxDocker:
        def __init__(self):
            self.calls = 0

        def run_once(self, image, cmd, *, net, sandbox, timeout,
                     abort_event=None, mounts=None, cwd=None):
            self.calls += 1
            assert sandbox is True
            return ExecOutcome(exit_code=0, stdout="sandbox")

    fd, fm = _SandboxDocker(), _FakeManager()
    gw = ExecutionGateway(bb=bb, backends={"sandbox": fd}, containers=fm)
    r = gw.run("id", runtime="sandbox", threat_class="untrusted", project_id=pid,
               session_id="sess-001122334455")
    assert r.stdout == "sandbox" and fd.calls == 1 and fm.execs == []


# ---------- detector ----------

def test_detector_inventory_prompt(tmp_path, monkeypatch):
    """探测结果 → 能力清单文本（注入系统提示用）。"""
    def fake_probe(cmd, timeout=10.0):
        if cmd[0] == "docker" and cmd[1] == "image":
            return True, "sha256:abc"  # pentest-box 镜像在
        if cmd[0] == "docker":
            return True, "29.6.1"
        if cmd[0] == "wsl.exe":
            return True, "默认发行版"
        return False, "not found"
    monkeypatch.setattr("core.runtime.detector._run_probe", fake_probe)
    inv = HostDetector().probe()
    text = inv.to_prompt()
    assert "Docker: 可用" in text and "29.6.1" in text
    assert "pentest-box 镜像就绪" in inv.docker.detail
    assert "WSL2: 可用" in text
    assert "工具清单: 暂无注册" in text


def test_detector_pentest_box_image_missing_guides_build(monkeypatch):
    """docker 热而 pentest-box 镜像缺失：detail 带构建指引（容器化 M0）。"""
    def fake_probe(cmd, timeout=10.0):
        if cmd[0] == "docker" and cmd[1] == "image":
            return False, "No such image"
        if cmd[0] == "docker":
            return True, "29.6.1"
        if cmd[0] == "wsl.exe":
            return False, "skip"
        return False, "not found"
    monkeypatch.setattr("core.runtime.detector._run_probe", fake_probe)
    inv = HostDetector().probe()
    assert inv.docker.available
    assert "pentest-box 镜像缺失" in inv.docker.detail
    assert "build_pentest_box" in inv.to_prompt()


def test_detector_tools_registry_probe(tmp_path, monkeypatch):
    """工具链注册表驱动探测（toolchain-registry M1，2026-09-23）：
    registry.json 条目逐一四来源检测，缺失工具入提示引导安装。"""
    import json
    tdir = tmp_path / "tools"
    tdir.mkdir()
    (tdir / "registry.json").write_text(json.dumps({
        "zap-xyz": {"kind": "binary", "search": ["zap-xyz.exe"], "guide": "装一下"},
    }), encoding="utf-8")
    monkeypatch.setattr("core.toolchain.shutil.which", lambda name, path=None: None)
    inv = HostDetector().probe(tools_root=tdir)
    assert inv.tools[0].name == "zap-xyz" and not inv.tools[0].available
    assert "装一下" in inv.tools[0].detail
    assert "zap-xyz" in inv.to_prompt()


def test_detector_bad_registry_degrades(tmp_path):
    """registry 坏结构降级为单条 issue 行，不阻断 docker/wsl 主探测。"""
    tdir = tmp_path / "tools"
    tdir.mkdir()
    (tdir / "registry.json").write_text("{ bad json", encoding="utf-8")
    inv = HostDetector().probe(tools_root=tdir)
    assert inv.tools[0].name == "registry" and not inv.tools[0].available
    assert "registry 读取失败" in inv.tools[0].detail


# ---------- H3：would_deny 干跑 + 审批一次性消费（2026-09-19） ----------

def test_gateway_would_deny_pure_function(bb):
    """would_deny 不执行不审计，只返回拒因；与 run() 同口径。"""
    pid = bb.create_project("t-wd", "pentest")["id"]
    gw = ExecutionGateway(bb=bb)
    before = len(bb.recent_events(pid))
    # 隔离等级拒绝
    assert "策略拒绝" in gw.would_deny("echo hi", "host", threat_class="untrusted")
    # 未知 runtime
    assert "未知 runtime" in gw.would_deny("echo hi", "quantum")
    # 限速纪律（nmap 全端口无限速参数）
    assert "限速纪律" in gw.would_deny("nmap -p- 1.2.3.4", "host")
    # 限速参数补上 → 放行
    assert gw.would_deny("nmap -p- -T3 1.2.3.4", "host") is None
    # 普通命令放行
    assert gw.would_deny("echo hi", "host") is None
    assert len(bb.recent_events(pid)) == before  # 干跑不落任何事件


def test_gateway_consumes_approval_once(bb):
    """H3 一次性审批：带 approval_id 的命令跑完即 consumed；net=real 自 2026-10-01
    起不再依赖审批，但已批准单在升级场景仍一次性消费。"""
    pid = bb.create_project("t-consume", "pentest")["id"]
    approval_id = "appr-once"

    class FakeSandbox:
        def run_once(self, image, cmd, *, net, sandbox, timeout, abort_event=None):
            return ExecOutcome(exit_code=0, stdout="ok")

    gw = ExecutionGateway(bb=bb, backends={"sandbox": FakeSandbox()})
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO approvals(id,project_id,session_id,action,risk,status,requested_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (approval_id, pid, None, '{"op":"authorization"}', "high", "approved",
             "sess-x", "2026-09-19T00:00:00+00:00"),
        )
    r = gw.run("echo hi", runtime="sandbox", threat_class="trusted",
               project_id=pid, approval_id=approval_id)
    assert r.ok
    row = bb.conn.execute("SELECT status FROM approvals WHERE id=?",
                          (approval_id,)).fetchone()
    assert row["status"] == "consumed"
