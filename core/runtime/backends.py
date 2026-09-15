"""执行后端：host / wsl / docker / sandbox 的命令执行实现。

统一约定：execute() 返回 (exit_code, stdout, stderr, timed_out)。
超时必须 kill（挂死的样本进程不允许遗留）。
"""

import platform
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecOutcome:
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class BackendError(RuntimeError):
    pass


class NativeBackend:
    """L0 宿主执行。Windows 走 PowerShell，POSIX 走 bash。"""

    name = "host"

    def execute(self, cmd: str, timeout: float = 120.0, cwd: str | None = None,
                env: dict[str, str] | None = None) -> ExecOutcome:
        if platform.system() == "Windows":
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd]
        else:
            argv = ["/bin/bash", "-c", cmd]
        return self._run(argv, timeout, cwd, env)

    def _run(self, argv: list[str], timeout: float, cwd: str | None,
             env: dict[str, str] | None) -> ExecOutcome:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout,
                cwd=cwd, encoding="utf-8", errors="replace",
                env={**env} if env else None,
            )
            return ExecOutcome(exit_code=proc.returncode, stdout=proc.stdout,
                               stderr=proc.stderr)
        except subprocess.TimeoutExpired as e:
            return ExecOutcome(timed_out=True, stdout=(e.stdout or b"").decode(errors="replace")
                               if isinstance(e.stdout, bytes) else (e.stdout or ""),
                               stderr=(e.stderr or b"").decode(errors="replace")
                               if isinstance(e.stderr, bytes) else (e.stderr or ""),
                               meta={"killed": True})
        except FileNotFoundError as e:
            raise BackendError(f"后端解释器不可用: {e}") from e


class WSLBackend(NativeBackend):
    """L1 WSL 执行：wsl bash -lc（与宿主同级信任，不跑不可信代码）。"""

    name = "wsl"

    def __init__(self, distro: str | None = None):
        self.distro = distro

    def execute(self, cmd: str, timeout: float = 120.0, cwd: str | None = None,
                env: dict[str, str] | None = None) -> ExecOutcome:
        argv = ["wsl.exe"]
        if self.distro:
            argv += ["-d", self.distro]
        argv += ["bash", "-lc", cmd]
        return self._run(argv, timeout, None, None)


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

    def _docker(self, argv: list[str], timeout: float) -> ExecOutcome:
        proc = subprocess.run([self.docker_cmd, *argv], capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
        return ExecOutcome(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def run_once(self, image: str, cmd: str, *, net: str = "none", sandbox: bool = False,
                 timeout: float = 300.0) -> ExecOutcome:
        """一次性容器执行。sandbox=True 时强制 L3 加固参数（无宿主挂载）。"""
        argv = ["run"]
        if sandbox:
            argv += sandbox_docker_args(net)
        else:
            argv += ["--network", net if net in {"none", "bridge"} else "none"]
        argv += [image, "sh", "-c", cmd]
        return self._docker(argv, timeout)

    def exec_in(self, container: str, cmd: str, timeout: float = 120.0) -> ExecOutcome:
        """在已运行容器内执行（pwn 题目交互等场景）。"""
        return self._docker(["exec", container, "sh", "-c", cmd], timeout)
