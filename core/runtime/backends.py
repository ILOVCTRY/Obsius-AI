"""执行后端：host / wsl / docker / sandbox 的命令执行实现。

统一约定：execute() 返回 (exit_code, stdout, stderr, timed_out)。
超时必须 kill（挂死的样本进程不允许遗留）。
■ 即点即停（2026-09-19）：execute 支持 abort_event（threading.Event）——
Popen 轮询等待，置位即杀进程树返回 interrupted，不等命令自然结束。
排水线程（2026-09-23）：stdout/stderr=PIPE 时由 `_execute_piped` 的 daemon
线程持续消费，防 64KB 管道缓冲写阻塞死锁（见 docs/plans/exec-gateway-pipe-deadlock.md）。
"""

import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: 中断/超时轮询间隔（秒）——即 ■ 中断的最大响应延迟
POLL_INTERVAL = 0.2

# Windows 子进程统一隐藏窗口（2026-09-23）：窗口模式（pythonw/打包 exe）下父进程
# 无控制台可继承，不带标志则每条命令弹 PowerShell 窗；POSIX 传 0 合法无副作用。
# 实测定论：CREATE_NO_WINDOW 对孙进程链生效，只需覆盖平台自身创建点。
NO_WINDOW_FLAGS = (
    subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
)


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀进程树：Windows 用 taskkill /T /F（PowerShell/bash 会拉起子进程，
    单杀 proc 会留尸）；POSIX 直接 SIGKILL。失败兜底再 proc.kill()。"""
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=NO_WINDOW_FLAGS)
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


def _execute_piped(proc: subprocess.Popen, timeout: float,
                   abort_event: threading.Event | None) -> ExecOutcome:
    """Popen 全生命周期（2026-09-23 管道死锁修复）：排水 daemon 线程持续消费
    stdout/stderr——stdout=PIPE + poll() 轮询不读管道时，输出超 Windows 管道
    缓冲（64KB）子进程写阻塞、父进程等退出 = 双方死锁到超时杀树（实验复现，
    见 docs/plans/exec-gateway-pipe-deadlock.md）。排水后轮询语义不变：
    正常结束 join 排水线程取输出；abort → interrupted；超时 → 杀树 timed_out。"""
    def _drain(pipe, sink: list) -> None:
        try:
            for chunk in pipe:
                sink.append(chunk)
        except Exception:  # noqa: BLE001 —— 排水绝不反向影响执行
            pass

    out_buf: list[str] = []
    err_buf: list[str] = []
    threads: list[threading.Thread] = []
    for pipe, sink in ((getattr(proc, "stdout", None), out_buf),
                       (getattr(proc, "stderr", None), err_buf)):
        if pipe is not None:
            t = threading.Thread(target=_drain, args=(pipe, sink), daemon=True)
            t.start()
            threads.append(t)

    interrupted = False
    timed_out = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if abort_event is not None and abort_event.is_set():
            interrupted = True
            _kill_tree(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_tree(proc)
            break
        time.sleep(POLL_INTERVAL)
    for t in threads:
        t.join(timeout=5)
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    rc = proc.returncode if proc.returncode is not None else -1
    meta: dict[str, Any] = {}
    if interrupted:
        meta["interrupted"] = True
    if timed_out:
        meta["killed"] = True
    return ExecOutcome(exit_code=-1 if timed_out else rc,
                       stdout="".join(out_buf), stderr="".join(err_buf),
                       timed_out=timed_out, interrupted=interrupted,
                       meta=meta)


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
                creationflags=NO_WINDOW_FLAGS,
            )
        except FileNotFoundError as e:
            raise BackendError(f"后端解释器不可用: {e}") from e
        return _execute_piped(proc, timeout, abort_event)


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
        # --exec argv 直通（2026-09-23）：默认包装层重新引用命令行时会剥引号，
        # bash 拿到裸命令把 $var/awk $1/$$ 按 Linux 环境展开吞掉（实验实锤，
        # 见 docs/plans/wsl-dollar-swallow.md）；--exec 后参数不经包装层，引号保真。
        argv += ["--exec", "bash", "-lc", cmd]
        return self._run(argv, timeout, None, None, abort_event)


#: L2 默认容器镜像（pentest-tools-container-m0）：Debian slim + 渗透工具箱，
#: 构建脚本 scripts/build_pentest_box.py（tools/pentest-box/Dockerfile 三层：基础/web/python）
DEFAULT_PENTEST_IMAGE = "cyberstrike/pentest-box:0.1"


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
                                errors="replace", creationflags=NO_WINDOW_FLAGS)
        return _execute_piped(proc, timeout, abort_event)

    def run_once(self, image: str, cmd: str, *, net: str = "none", sandbox: bool = False,
                 timeout: float = 300.0, abort_event: threading.Event | None = None,
                 mounts: list[tuple[str, str]] | None = None,
                 cwd: str | None = None) -> ExecOutcome:
        """一次性容器执行。sandbox=True 时强制 L3 加固参数（忽略 mounts——L3 零挂载）。
        mounts=[(宿主路径, 容器路径), ...]（L2 workspace 卷挂载，pentest-tools-container-m0）；
        cwd=容器内工作目录（网关固定 /workspace/scratch，对齐 host/wsl 相对路径习惯）。
        中断杀 docker CLI 客户端；--rm 容器侧残留由容器生命周期兜底（已知局限）。"""
        argv = ["run"]
        if sandbox:
            argv += sandbox_docker_args(net)
        else:
            argv += ["--network", net if net in {"none", "bridge"} else "none"]
            for host_path, cpath in (mounts or []):
                # 宿主路径转正斜杠：docker -v 在 Windows 上两种斜杠都收，正斜杠免转义
                argv += ["-v", f"{str(host_path).replace(chr(92), '/')}:{cpath}"]
            if cwd:
                argv += ["-w", cwd]
        argv += [image, "sh", "-c", cmd]
        return self._docker(argv, timeout, abort_event)

    def exec_in(self, container: str, cmd: str, timeout: float = 120.0,
                abort_event: threading.Event | None = None) -> ExecOutcome:
        """在已运行容器内执行（pwn 题目交互等场景）。"""
        return self._docker(["exec", container, "sh", "-c", cmd], timeout, abort_event)
