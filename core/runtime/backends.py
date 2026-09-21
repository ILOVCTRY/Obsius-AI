"""执行后端：host / wsl / docker / sandbox 的命令执行实现。

统一约定：execute() 返回 (exit_code, stdout, stderr, timed_out)。
超时必须 kill（挂死的样本进程不允许遗留）。
■ 即点即停（2026-09-19）：execute 支持 abort_event（threading.Event）——
Popen 轮询等待，置位即杀进程树返回 interrupted，不等命令自然结束。
"""

import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: 中断/超时轮询间隔（秒）——即 ■ 中断的最大响应延迟
POLL_INTERVAL = 0.2


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀进程树：Windows 用 taskkill /T /F（PowerShell/bash 会拉起子进程，
    单杀 proc 会留尸）；POSIX 直接 SIGKILL。失败兜底再 proc.kill()。"""
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@dataclass
class ExecOutcome:
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    interrupted: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def _wait_or_kill(proc: subprocess.Popen, timeout: float,
                  abort_event: threading.Event | None) -> ExecOutcome | None:
    """Popen 轮询等待：正常退出返回 None（调方再 communicate 收尸）；超时或
    abort 置位杀进程树并返回带标记的 ExecOutcome（timeout → timed_out，
    abort → interrupted）。返回 None 表示跑完了。"""
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if abort_event is not None and abort_event.is_set():
            _kill_tree(proc)
            out, err = proc.communicate()
            return ExecOutcome(exit_code=proc.returncode, stdout=out or "",
                               stderr=err or "", interrupted=True,
                               meta={"interrupted": True})
        if time.monotonic() >= deadline:
            _kill_tree(proc)
            out, err = proc.communicate()
            return ExecOutcome(timed_out=True, stdout=out or "", stderr=err or "",
                               meta={"killed": True})
        time.sleep(POLL_INTERVAL)
    return None


class BackendError(RuntimeError):
    pass


class NativeBackend:
    """L0 宿主执行。Windows 走 PowerShell，POSIX 走 bash。"""

    name = "host"

    def execute(self, cmd: str, timeout: float = 120.0, cwd: str | None = None,
                env: dict[str, str] | None = None,
                abort_event: threading.Event | None = None) -> ExecOutcome:
        if platform.system() == "Windows":
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd]
        else:
            argv = ["/bin/bash", "-c", cmd]
        return self._run(argv, timeout, cwd, env, abort_event)

    def _run(self, argv: list[str], timeout: float, cwd: str | None,
             env: dict[str, str] | None,
             abort_event: threading.Event | None = None) -> ExecOutcome:
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=cwd, encoding="utf-8", errors="replace",
                env={**env} if env else None,
            )
        except FileNotFoundError as e:
            raise BackendError(f"后端解释器不可用: {e}") from e
        early = _wait_or_kill(proc, timeout, abort_event)
        if early is not None:
            return early
        out, err = proc.communicate()
        return ExecOutcome(exit_code=proc.returncode, stdout=out or "", stderr=err or "")


class WSLBackend(NativeBackend):
    """L1 WSL 执行：wsl bash -lc（与宿主同级信任，不跑不可信代码）。

    cwd 为 WSL 侧路径（网关经 windows_to_wsl_path 换算）→ wsl --cd 落点；
    env 不透传 Windows 进程环境（无意义），TMPDIR 重定向由网关前缀进 cmd。
    中断语义（2026-09-19）：杀 wsl.exe 客户端进程——WSL 侧残留进程由
    distro 生命周期兜底（已知局限，宁误杀勿悬挂）。
    """

    name = "wsl"

    def __init__(self, distro: str | None = None):
        self.distro = distro

    def execute(self, cmd: str, timeout: float = 120.0, cwd: str | None = None,
                env: dict[str, str] | None = None,
                abort_event: threading.Event | None = None) -> ExecOutcome:
        argv = ["wsl.exe"]
        if self.distro:
            argv += ["-d", self.distro]
        if cwd:
            argv += ["--cd", cwd]
        argv += ["bash", "-lc", cmd]
        return self._run(argv, timeout, None, None, abort_event)


def sandbox_docker_args(net: str) -> list[str]:
    """L3 加固参数（DESIGN.md §7）：无挂载、限额、降权。fakenet 下一里程碑实现。"""
    if net not in {"none", "real"}:
        raise NotImplementedError(f"网络模式 {net} 尚未实现（fakenet=INetSim sidecar，下一里程碑）")
    docker_net = {"none": "none", "real": "bridge"}[net]  # real=宿主网络栈，须审批
    args = [
        "--rm",                       # 一次性生命周期：跑完即焚
        "--network", docker_net,
        "--memory", "512m",
        "--pids-limit", "64",
        "--cpus", "1.0",
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
    ]
    return args


class DockerBackend:
    """L2/L3 Docker 执行。sandbox 模式自动附加加固参数并禁止挂载。"""

    name = "docker"

    def __init__(self, docker_cmd: str = "docker"):
        self.docker_cmd = docker_cmd

    def _docker(self, argv: list[str], timeout: float,
                abort_event: threading.Event | None = None) -> ExecOutcome:
        proc = subprocess.Popen([self.docker_cmd, *argv], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace")
        early = _wait_or_kill(proc, timeout, abort_event)
        if early is not None:
            return early
        out, err = proc.communicate()
        return ExecOutcome(exit_code=proc.returncode, stdout=out or "", stderr=err or "")

    def run_once(self, image: str, cmd: str, *, net: str = "none", sandbox: bool = False,
                 timeout: float = 300.0,
                 abort_event: threading.Event | None = None) -> ExecOutcome:
        """一次性容器执行。sandbox=True 时强制 L3 加固参数（无宿主挂载）。
        中断杀 docker CLI 客户端；--rm 容器侧残留由容器生命周期兜底（已知局限）。"""
        argv = ["run"]
        if sandbox:
            argv += sandbox_docker_args(net)
        else:
            argv += ["--network", net if net in {"none", "bridge"} else "none"]
        argv += [image, "sh", "-c", cmd]
        return self._docker(argv, timeout, abort_event)

    def exec_in(self, container: str, cmd: str, timeout: float = 120.0,
                abort_event: threading.Event | None = None) -> ExecOutcome:
        """在已运行容器内执行（pwn 题目交互等场景）。"""
        return self._docker(["exec", container, "sh", "-c", cmd], timeout, abort_event)
