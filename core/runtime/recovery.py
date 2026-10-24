"""受控执行环境恢复（environment-recovery）。

本模块只处理两类可证明、可审计的环境缺失：

* ``tools/venv`` 中由 ``tools/registry.json`` 明确声明的 Python 工具依赖；
* 项目自带且镜像名固定的 ``pentest-box`` Docker 镜像。

它不是通用安装器，也不接受模型传入的任意 shell、URL、包管理器或 Docker
参数。恢复动作由调用方注入命令执行器（通常是 ExecutionGateway.run），因此
恢复仍受 runtime/threat_class/workspace/scope/net/approval 约束。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from core.runtime.backends import DEFAULT_PENTEST_IMAGE
from core.runtime.pathguard import windows_to_wsl_path
from core.toolchain import load_registry, resolve_tool


class EnvironmentErrorCategory(str, Enum):
    """执行失败中可识别的环境类原因。"""

    TOOL_MISSING = "tool_missing"
    PYTHON_DEPENDENCY_MISSING = "python_dependency_missing"
    RUNTIME_MISSING = "runtime_missing"
    DOCKER_CLI_MISSING = "docker_cli_missing"
    DOCKER_DAEMON_UNAVAILABLE = "docker_daemon_unavailable"
    DOCKER_IMAGE_MISSING = "docker_image_missing"
    PERMISSION_DENIED = "permission_denied"
    POLICY_DENIED = "policy_denied"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


# 兼容调用方使用 Category / Kind 的命名习惯；值保持同一枚举。
EnvironmentErrorKind = EnvironmentErrorCategory


@dataclass(frozen=True)
class EnvironmentIssue:
    """一次命令失败的结构化环境诊断。"""

    category: EnvironmentErrorCategory | str
    message: str
    command: str = ""
    runtime: str = ""
    tool: str | None = None
    image: str | None = None
    recoverable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def code(self) -> str:
        return (self.category.value if isinstance(self.category, EnvironmentErrorCategory)
                else str(self.category))

    @property
    def kind(self) -> str:
        """``kind`` 是给旧调用方的别名。"""
        return self.code

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.code,
            "message": self.message,
            "command": self.command,
            "runtime": self.runtime,
            "tool": self.tool,
            "image": self.image,
            "recoverable": self.recoverable,
            "details": dict(self.details),
        }


class EnvironmentUnavailable(RuntimeError):
    """后端本身不可用，不属于策略拒绝。"""

    def __init__(self, issue: EnvironmentIssue):
        self.issue = issue
        super().__init__(issue.message)


@dataclass
class RecoveryResult:
    """受控恢复的最终状态。"""

    status: str
    category: str
    message: str
    action: str | None = None
    attempts: int = 0
    verified: bool = False
    tool: str | None = None
    image: str | None = None
    probe: Any = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verified and self.status in {"recovered", "already_ready"}

    @property
    def success(self) -> bool:
        return self.ok

    @property
    def ready(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "category": self.category,
            "message": self.message,
            "action": self.action,
            "attempts": self.attempts,
            "verified": self.verified,
            "tool": self.tool,
            "image": self.image,
            "probe": self.probe,
            "details": dict(self.details),
        }

    def brief(self) -> str:
        prefix = "[环境恢复完成]" if self.ok else "[环境恢复未完成]"
        action = f" action={self.action}" if self.action else ""
        return f"{prefix} category={self.category} status={self.status}{action}: {self.message}"


@dataclass(frozen=True)
class RecoveryRequest:
    """恢复动作执行所需的策略上下文。"""

    runtime: str
    threat_class: str
    project_id: str | None = None
    session_id: str | None = None
    author: str = "system"
    environment_id: str | None = None
    net: str | None = None
    approval_id: str | None = None
    timeout: float | None = None
    abort_event: Any = None
    step: int | None = None


# registry 中的包名是代码库声明，不允许模型自行拼接包名/URL。
_SAFE_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]*(?:\[[A-Za-z0-9_,.\-]+\])?(?:[<>=!~]=?[A-Za-z0-9.*+!~<>=\-]+)?$")
_SAFE_TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]*$")


def _category(value: EnvironmentErrorCategory | str) -> str:
    return value.value if isinstance(value, EnvironmentErrorCategory) else str(value)


def _first_command_word(command: str) -> str | None:
    """取命令的首个普通词，仅用于提示/工具名，不参与执行。"""
    try:
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        words = command.strip().split()
    if not words:
        return None
    word = Path(words[0].strip('"\'')).name
    return re.sub(r"\.exe$", "", word, flags=re.I) or None


def _python_module_name(stderr: str) -> str | None:
    patterns = (
        r"No module named ['\"]([^'\"]+)['\"]",
        r"ModuleNotFoundError: No module named ([^\s]+)",
        r"ImportError: No module named ['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        hit = re.search(pattern, stderr, flags=re.I)
        if hit:
            return hit.group(1).split(".", 1)[0]
    return None


def classify_backend_error(exc: BaseException, *, command: str = "",
                            runtime: str = "") -> EnvironmentIssue:
    """把 BackendError/解释器错误分成环境类原因。"""
    msg = str(exc) or type(exc).__name__
    low = msg.lower()
    if any(x in low for x in (
            "docker cli 不可用", "docker cli unavailable", "no such file or directory",
            "cannot find the file", "[winerror 2]")) and runtime == "docker":
        cat = EnvironmentErrorCategory.DOCKER_CLI_MISSING
    elif any(x in low for x in (
            "docker daemon 不可用", "docker daemon unavailable",
            "failed to connect to the docker api", "is the docker daemon running",
            "dockerdesktoplinuxengine", "cannot connect to the docker daemon")):
        cat = EnvironmentErrorCategory.DOCKER_DAEMON_UNAVAILABLE
    elif any(x in low for x in ("后端解释器不可用", "interpreter unavailable",
                                "runtime missing", "无对应后端")):
        cat = EnvironmentErrorCategory.RUNTIME_MISSING
    elif isinstance(exc, PermissionError) or "permission denied" in low or "access is denied" in low:
        cat = EnvironmentErrorCategory.PERMISSION_DENIED
    else:
        cat = EnvironmentErrorCategory.UNKNOWN
    return EnvironmentIssue(
        category=cat,
        message=msg[:600],
        command=command,
        runtime=runtime,
        recoverable=False,
    )


def environment_issue_from_dict(data: Mapping[str, Any] | None, *,
                                command: str = "", runtime: str = "") -> EnvironmentIssue | None:
    """把 ``ExecutionResult.environment_error`` 还原为结构化问题。

    结果会跨工具层以 JSON/dict 传递，不能假设 category 一定是枚举实例；未知
    category 仍保留原值，由恢复器按不可恢复分支处理。
    """
    if not isinstance(data, Mapping):
        return None
    raw_category = data.get("category") or data.get("code") or data.get("kind")
    if not raw_category:
        return None
    try:
        category: EnvironmentErrorCategory | str = EnvironmentErrorCategory(str(raw_category))
    except ValueError:
        category = str(raw_category)
    details = data.get("details")
    return EnvironmentIssue(
        category=category,
        message=str(data.get("message") or "环境不可用")[:600],
        command=str(data.get("command") or command),
        runtime=str(data.get("runtime") or runtime),
        tool=(str(data["tool"]) if data.get("tool") is not None else None),
        image=(str(data["image"]) if data.get("image") is not None else None),
        recoverable=bool(data.get("recoverable", False)),
        details=dict(details) if isinstance(details, Mapping) else {},
    )


def classify_execution_failure(*, command: str, runtime: str,
                               exit_code: int, stdout: str = "", stderr: str = "",
                               image: str | None = None) -> EnvironmentIssue | None:
    """识别非零执行结果中的环境故障；普通业务失败返回 ``None``。

    识别只依赖明确的解释器/工具/Docker 错误特征，不能因为任意非零退出就
    触发安装或构建。

    **成因为什么必须判 ``exit_code != 0``**（2026-10-23，R2 #3）：
    ``gateway.py`` 对**所有非超时结果**都调本函数（不只非零退出）。只看输出
    子串时，一条 ``exit_code=0``、已经成功的命令，只要输出里带
    ``cannot create /dev/null: Permission denied``（CTF 里 `2>/dev/null` 在
    受限容器中的常见噪音）或 ``try again`` 之类字样，就会被判成环境错误，
    进而触发装包/换镜像等恢复动作——**对着一条成功的命令做环境修复**。
    故下面两个「纯子串」分支（permission_denied / transient）必须要求非零退出。

    其余分支本就隐含非零语义（127 / 126 / docker 拉取失败 / 模块缺失），
    不额外加条件：``ModuleNotFoundError`` 出现在 exit 0 的输出里也不该被当成
    失败，但那种情况本函数返回 None 也无害（调用方只对非零结果做恢复）。
    """
    text = "\n".join(x for x in (stderr, stdout) if x)
    low = text.lower()
    if not text and exit_code == 127:
        # 没有 stderr 的 127 可能是 shell 兼容问题，不能安全推断工具缺失。
        return None
    # permission_denied：纯子串判据 → 必须非零退出才成立（见 docstring）
    if exit_code != 0 and any(
            x in low for x in ("permission denied", "access is denied", "拒绝访问")):
        return EnvironmentIssue(
            EnvironmentErrorCategory.PERMISSION_DENIED, text[:600], command, runtime,
            recoverable=False)
    module = _python_module_name(text)
    if module and re.search(r"(?:python(?:\.exe)?|python3|py)(?:\s|$)", command, re.I):
        return EnvironmentIssue(
            EnvironmentErrorCategory.PYTHON_DEPENDENCY_MISSING,
            f"Python 依赖缺失: {module}", command, runtime, tool=module,
            recoverable=True, details={"module": module})
    if runtime in {"docker", "sandbox"} and any(x in low for x in (
            "unable to find image", "no such image", "manifest unknown",
            "pull access denied", "repository does not exist")):
        return EnvironmentIssue(
            EnvironmentErrorCategory.DOCKER_IMAGE_MISSING,
            f"Docker 镜像不可用: {image or DEFAULT_PENTEST_IMAGE}", command, runtime,
            image=image or DEFAULT_PENTEST_IMAGE, recoverable=True)
    not_found = (
        "command not found", "is not recognized as the name of a cmdlet",
        "the term '" , "the term \"", "不是内部或外部命令", "找不到指定的命令",
    )
    if any(x in low for x in not_found) or exit_code == 127:
        return EnvironmentIssue(
            EnvironmentErrorCategory.TOOL_MISSING,
            text[:600] or f"命令不可用（exit={exit_code}）", command, runtime,
            tool=_first_command_word(command), recoverable=True)
    if exit_code in {126, 127}:
        return EnvironmentIssue(
            EnvironmentErrorCategory.RUNTIME_MISSING,
            text[:600] or f"运行时不可用（exit={exit_code}）", command, runtime,
            recoverable=False)
    # transient：同为纯子串判据 → 同样要求非零退出（见 docstring）
    if exit_code != 0 and any(
            x in low for x in ("temporarily unavailable", "resource busy", "try again",
                               "timed out", "timeout", "connection reset")):
        return EnvironmentIssue(
            EnvironmentErrorCategory.TRANSIENT, text[:600], command, runtime,
            recoverable=False)
    return None


class EnvironmentRecovery:
    """只执行白名单恢复动作，并在动作后做定向探测。"""

    MAX_ATTEMPTS = 1

    def __init__(self, workspace: str | Path, *, tools_root: str | Path | None = None,
                 command_runner: Callable[..., Any] | None = None,
                 image_probe: Callable[[str], bool] | None = None,
                 bb: Any = None, max_attempts: int = MAX_ATTEMPTS,
                 base_python: str | Path | None = None,
                 on_recovered: Callable[[RecoveryResult], None] | None = None):
        self.workspace = Path(workspace).resolve()
        # tools_root 是 registry 来源；安装目标永远是当前项目工作区下的 tools/。
        # 这样 registry 可以来自平台仓库，但任何写入仍被限制在项目工作区。
        self.registry_root = Path(tools_root or self.workspace / "tools").resolve()
        self.tools_root = (self.workspace / "tools").resolve()
        self.command_runner = command_runner
        self.image_probe = image_probe
        # 恢复成功后的上层通知（通常是「重探宿主能力清单」）：由调用方注入，
        # 恢复器本身不 import API 层，也不假设存在 UI。
        self.on_recovered = on_recovered
        self.bb = bb
        self.max_attempts = max(1, int(max_attempts))
        self.base_python = Path(base_python).resolve() if base_python else Path(sys.executable).resolve()
        self._attempts: dict[tuple[str, str], int] = {}
        # 恢复器是项目级共享实例（多线程/子专家并发跑），去重计数必须原子：
        # 否则两个 worker 会同时判定「还没装过」并各装一遍。
        self._attempts_lock = threading.Lock()

    def _audit(self, request: RecoveryRequest, issue: EnvironmentIssue,
               result: RecoveryResult) -> None:
        if self.bb is None or not request.project_id:
            return
        try:
            self.bb.append_event(
                request.project_id, "environment.recovery",
                {"category": issue.code, "status": result.status,
                 "action": result.action, "attempts": result.attempts,
                 "verified": result.verified, "tool": issue.tool,
                 "image": issue.image, "message": result.message[:600]},
                session_id=request.session_id, author=request.author,
            )
        except Exception:  # noqa: BLE001 - 审计不阻断恢复/原命令
            pass

    def _result(self, request: RecoveryRequest, issue: EnvironmentIssue, **kwargs) -> RecoveryResult:
        result = RecoveryResult(category=issue.code, **kwargs)
        self._audit(request, issue, result)
        return result

    def _release(self, key: tuple[str, str], claimed: int) -> None:
        """撤销一次未真正执行的占位（denied = 前置校验没过，一个动作都没跑）。

        只在计数仍等于自己占的那个值时才回滚：若并发线程已把计数推高，说明
        另有恢复在跑，此时不能把它的名额抹掉。
        """
        with self._attempts_lock:
            if self._attempts.get(key) == claimed:
                if claimed <= 1:
                    self._attempts.pop(key, None)
                else:
                    self._attempts[key] = claimed - 1

    def recover(self, issue: EnvironmentIssue, request: RecoveryRequest) -> RecoveryResult:
        """按故障类别执行至多一次白名单恢复。"""
        key = (issue.code, issue.tool or issue.image or "")
        # 计数只占坑不占锁：真正的安装/构建在锁外执行，避免一条慢恢复串行化
        # 其他无关故障的恢复；同一 key 的并发第二次会直接判 exhausted。
        with self._attempts_lock:
            used = self._attempts.get(key, 0)
            exhausted = used >= self.max_attempts
            claimed = used + 1
            if not exhausted:
                self._attempts[key] = claimed
        if exhausted:
            return self._result(
                request, issue, status="exhausted", action=None, attempts=used,
                verified=False, tool=issue.tool, image=issue.image,
                message="同一环境故障的恢复次数已耗尽，不再重复安装或构建。")
        if not issue.recoverable:
            self._release(key, claimed)
            return self._result(
                request, issue, status="denied", action=None, attempts=used,
                message=f"{issue.code} 不允许自动恢复：{issue.message}",
                tool=issue.tool, image=issue.image)
        if issue.code == EnvironmentErrorCategory.PYTHON_DEPENDENCY_MISSING.value:
            result = self._recover_python(issue, request, claimed)
        elif issue.code == EnvironmentErrorCategory.DOCKER_IMAGE_MISSING.value:
            result = self._recover_docker(issue, request, claimed)
        elif issue.code == EnvironmentErrorCategory.TOOL_MISSING.value:
            result = self._recover_tool(issue, request, claimed)
        else:
            result = self._result(
                request, issue, status="denied", attempts=claimed,
                message=f"{issue.code} 没有受控恢复动作。")
        if result.status == "denied":
            # 白名单/审批/固定脚本等前置条件没过 → 没有发生任何安装或构建，
            # 不占用名额：用户补上审批后重试仍应能真正执行一次恢复。
            self._release(key, claimed)
        elif result.ok:
            self._notify_recovered(result)
        return result

    def _notify_recovered(self, result: RecoveryResult) -> None:
        """恢复成功后的 best-effort 通知（能力清单刷新等）。

        通知失败绝不能影响「恢复已成功」这个结论——上层刷新是可见性优化，
        不是恢复正确性的一部分。
        """
        if self.on_recovered is None:
            return
        try:
            self.on_recovered(result)
        except Exception:  # noqa: BLE001 - 通知失败不影响恢复结论
            pass

    def _validate_inside_workspace(self, path: Path, label: str) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as e:
            raise ValueError(f"{label} 必须位于项目工作区内: {resolved}") from e
        return resolved

    def _registry_entry(self, name: str) -> tuple[dict[str, Any] | None, str | None]:
        if not _SAFE_TOOL_RE.fullmatch(name or ""):
            return None, "工具名不符合 registry 标识格式"
        try:
            registry = load_registry(self.registry_root)
        except Exception as e:  # noqa: BLE001
            return None, f"registry 读取失败: {e}"
        entry = registry.get(name)
        if not isinstance(entry, dict):
            return None, f"工具 {name} 不在 tools/registry.json 白名单中"
        return entry, None

    def _run(self, command: str, request: RecoveryRequest, *, runtime: str,
             net: str | None = None, approval_id: str | None = None,
             workspace: Path | None = None, consume_approval: bool = True) -> Any:
        if self.command_runner is None:
            raise RuntimeError("未装配受控命令执行器，拒绝直接运行恢复命令")
        kwargs = {
            "runtime": runtime,
            "threat_class": request.threat_class,
            "project_id": request.project_id,
            "session_id": request.session_id,
            "author": request.author,
            "environment_id": request.environment_id,
            "net": net,
            "approval_id": approval_id,
            "timeout": request.timeout,
            "abort_event": request.abort_event,
            "workspace": workspace or self.workspace,
            "step": request.step,
            "consume_approval": consume_approval,
        }
        try:
            return self.command_runner(command, **kwargs)
        except TypeError as e:
            # 兼容测试替身/旧 runner 未声明该可选参数；真实 gateway 支持它。
            if "consume_approval" not in str(e):
                raise
            kwargs.pop("consume_approval", None)
            return self.command_runner(command, **kwargs)

    @staticmethod
    def _ps_quote(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _sh_quote(value: str | Path) -> str:
        return shlex.quote(str(value))

    def _python_packages(self, tool: str) -> tuple[list[str], str | None]:
        entry, error = self._registry_entry(tool)
        if error:
            return [], error
        if entry.get("kind") != "python-tool":
            return [], f"工具 {tool} 不是 python-tool，拒绝走 pip 恢复"
        packages = entry.get("pip") or []
        if not packages or not isinstance(packages, list):
            return [], f"工具 {tool} 未声明可安装的 pip 白名单"
        safe: list[str] = []
        for package in packages:
            if not isinstance(package, str) or not _SAFE_PACKAGE_RE.fullmatch(package):
                return [], f"registry 中存在不安全的 pip 声明: {package!r}"
            safe.append(package)
        return safe, None

    def _recover_python(self, issue: EnvironmentIssue, request: RecoveryRequest,
                        attempts: int) -> RecoveryResult:
        tool = issue.tool or str(issue.details.get("tool") or "")
        packages, error = self._python_packages(tool)
        if error:
            return self._result(
                request, issue, status="denied", action="python_venv_install",
                attempts=attempts, message=error, tool=tool)
        if request.runtime not in {"host", "wsl"}:
            return self._result(
                request, issue, status="denied", action="python_venv_install",
                attempts=attempts, message="Python 依赖恢复只允许在 host/wsl 项目 venv 中进行；"
                "不会把宿主安装动作藏进 docker/sandbox。", tool=tool)
        if request.net != "real" or not request.approval_id:
            return self._result(
                request, issue, status="denied", action="python_venv_install",
                attempts=attempts, message="从包源恢复依赖需要 net=real 和当前项目的一次性 approval_id；"
                "未获得审批，不执行安装。", tool=tool)
        venv = self._validate_inside_workspace(self.tools_root / "venv", "tools/venv")
        self._validate_inside_workspace(self.tools_root, "tools_root")
        if request.runtime == "wsl":
            # gateway 的 WSL cwd/命令运行在 Linux 侧；workspace 是 Windows Path，
            # 必须先换算为 /mnt/<drive>/...，避免把 ``E:\\...`` 当成 WSL 相对路径。
            wsl_venv = windows_to_wsl_path(venv)
            wsl_venv_py = windows_to_wsl_path(venv / "bin" / "python")
            venv_py = venv / "bin" / "python"
            base = "python3"
            venv_arg = self._sh_quote(wsl_venv)
            py_arg = self._sh_quote(wsl_venv_py)
            pkg_args = " ".join(self._sh_quote(p) for p in packages)
            command = (
                f"{base} -m venv {venv_arg} && "
                f"{py_arg} -m pip install --isolated --require-virtualenv "
                f"--disable-pip-version-check --no-input --no-cache-dir "
                f"--only-binary=:all: {pkg_args}"
            )
        else:
            venv_py = venv / "Scripts" / "python.exe"
            base = self._ps_quote(self.base_python)
            venv_arg = self._ps_quote(venv)
            py_arg = self._ps_quote(venv_py)
            pkg_args = " ".join(self._ps_quote(p) for p in packages)
            command = (
                f"& {base} -m venv {venv_arg}; "
                f"if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}; "
                f"& {py_arg} -m pip install --isolated --require-virtualenv "
                f"--disable-pip-version-check --no-input --no-cache-dir "
                f"--only-binary=:all: {pkg_args}"
            )
        try:
            outcome = self._run(command, request, runtime=request.runtime,
                                net="real", approval_id=request.approval_id,
                                workspace=self.workspace,
                                consume_approval=False)
        except EnvironmentUnavailable as e:
            return self._result(
                request, issue, status="failed", action="python_venv_install",
                attempts=attempts, message=f"受控安装被后端拒绝: {e}", tool=tool)
        except Exception as e:  # noqa: BLE001
            return self._result(
                request, issue, status="failed", action="python_venv_install",
                attempts=attempts, message=f"受控安装异常: {e}", tool=tool)
        if not getattr(outcome, "ok", False):
            text = (getattr(outcome, "stderr", "") or getattr(outcome, "stdout", "") or "")[:600]
            return self._result(
                request, issue, status="failed", action="python_venv_install",
                attempts=attempts, message=f"依赖安装失败: {text or '命令返回非零'}",
                tool=tool, details={"exit_code": getattr(outcome, "exit_code", None)})
        verified, probe = self._verify_tool(tool, venv, runtime=request.runtime)
        return self._result(
            request, issue, status="recovered" if verified else "failed",
            action="python_venv_install", attempts=attempts, verified=verified,
            message=(f"{tool} 已安装到项目 tools/venv，并完成重新探测"
                     if verified else f"{tool} 安装命令成功，但重新探测仍未确认就绪"),
            tool=tool, probe=probe)

    def _verify_tool(self, tool: str, venv: Path, *, runtime: str) -> tuple[bool, Any]:
        try:
            registry = load_registry(self.registry_root)
            entry = registry.get(tool)
            if not isinstance(entry, dict):
                return False, {"error": f"工具 {tool} 不在 registry 中"}
            # resolve_tool 按当前宿主平台探测；WSL 在 Windows 宿主上必须显式检查
            # Linux venv/bin/python，不能误用 Windows Scripts 目录。
            py = venv / ("bin" if runtime == "wsl" else "Scripts") / (
                "python" if runtime == "wsl" else "python.exe")
            if not py.is_file():
                return False, {"name": tool, "kind": entry.get("kind"),
                               "status": "missing", "path": str(py),
                               "detail": "项目 venv 解释器缺失"}
            row = {"name": tool, "kind": entry.get("kind"), "status": "ready",
                   "source": "bundled", "path": str(py), "detail": "tools/venv"}
        except Exception as e:  # noqa: BLE001
            return False, {"error": str(e)}
        # python-tool 的探测本身验证 venv 解释器落位；安装命令已由 pip 返回成功。
        return True, row

    def _recover_tool(self, issue: EnvironmentIssue, request: RecoveryRequest,
                      attempts: int) -> RecoveryResult:
        tool = issue.tool or ""
        entry, error = self._registry_entry(tool)
        if error:
            return self._result(
                request, issue, status="denied", action="tool_install",
                attempts=attempts, message=error, tool=tool)
        if entry.get("kind") == "python-tool" and entry.get("pip"):
            issue2 = EnvironmentIssue(
                EnvironmentErrorCategory.PYTHON_DEPENDENCY_MISSING,
                issue.message, issue.command, issue.runtime, tool=tool,
                recoverable=True, details=dict(issue.details))
            return self._recover_python(issue2, request, attempts)
        return self._result(
            request, issue, status="denied", action="tool_install", attempts=attempts,
            message="普通二进制工具没有项目内、来源固定的安装器；不会自动下载未知二进制。",
            tool=tool)

    def _recover_docker(self, issue: EnvironmentIssue, request: RecoveryRequest,
                        attempts: int) -> RecoveryResult:
        image = issue.image or DEFAULT_PENTEST_IMAGE
        if image != DEFAULT_PENTEST_IMAGE:
            return self._result(
                request, issue, status="denied", action="docker_build",
                attempts=attempts, message="只允许构建项目固定的 pentest-box 镜像，拒绝模型指定镜像。",
                image=image)
        if request.runtime != "docker":
            return self._result(
                request, issue, status="denied", action="docker_build", attempts=attempts,
                message="Docker 镜像恢复只能由 docker runtime 的已知镜像缺失触发。",
                image=image)
        script = self._validate_inside_workspace(
            self.workspace / "scripts" / "build_pentest_box.py", "镜像构建脚本")
        dockerfile = self._validate_inside_workspace(
            self.workspace / "tools" / "pentest-box" / "Dockerfile", "Dockerfile")
        if not script.is_file() or not dockerfile.is_file():
            return self._result(
                request, issue, status="denied", action="docker_build", attempts=attempts,
                message="项目固定的 scripts/build_pentest_box.py 或 tools/pentest-box/Dockerfile 不存在。",
                image=image)
        if request.net != "real" or not request.approval_id:
            return self._result(
                request, issue, status="denied", action="docker_build", attempts=attempts,
                message="镜像构建可能访问外部源，需要 net=real 和一次性 approval_id；未获得审批，不构建。",
                image=image)
        # Docker CLI/daemon 是宿主控制面。只有原 threat_class 也允许 host 时才可调用
        # 项目固定脚本；untrusted/malware_live 不会借恢复动作升级到宿主。
        try:
            command = f"& {self._ps_quote(self.base_python)} {self._ps_quote(script)}"
            if os.name != "nt":
                command = f"{self._sh_quote(self.base_python)} {self._sh_quote(script)}"
            # 构建动作同样不能提前消费原命令的一次性 approval：镜像构建成功后
            # 还要重试原命令，approval 必须留到那次真正成功的执行再消费。
            outcome = self._run(command, request, runtime="host", net="real",
                                approval_id=request.approval_id,
                                workspace=self.workspace,
                                consume_approval=False)
        except Exception as e:  # noqa: BLE001
            return self._result(
                request, issue, status="failed", action="docker_build", attempts=attempts,
                message=f"固定镜像构建失败: {e}", image=image)
        if not getattr(outcome, "ok", False):
            text = (getattr(outcome, "stderr", "") or getattr(outcome, "stdout", "") or "")[:600]
            return self._result(
                request, issue, status="failed", action="docker_build", attempts=attempts,
                message=f"固定镜像构建失败: {text or '命令返回非零'}", image=image,
                details={"exit_code": getattr(outcome, "exit_code", None)})
        verified = self._probe_image(image, request)
        return self._result(
            request, issue, status="recovered" if verified else "failed",
            action="docker_build", attempts=attempts, verified=verified,
            message=(f"{image} 已构建并重新探测确认就绪"
                     if verified else f"构建脚本成功，但重新探测未确认镜像 {image} 就绪"),
            image=image)

    def _probe_image(self, image: str, request: RecoveryRequest) -> bool:
        if self.image_probe is not None:
            try:
                return bool(self.image_probe(image))
            except Exception:  # noqa: BLE001 - 探测异常按未就绪处理
                return False
        # 未注入探测函数时，用固定只读命令经 gateway 重新确认镜像存在。
        # 只接受项目固定镜像名（常量，无模型可控输入），避免把任意字符串拼进命令。
        if self.command_runner is None or image != DEFAULT_PENTEST_IMAGE:
            return False
        try:
            outcome = self._run(f"docker image inspect {image}", request,
                                runtime="host", workspace=self.workspace,
                                consume_approval=False)
        except Exception:  # noqa: BLE001 - 策略拒绝/后端不可用都算未就绪
            return False
        return bool(getattr(outcome, "ok", False))


# 更短的兼容名，便于上层按 Manager 习惯导入。
EnvironmentRecoveryManager = EnvironmentRecovery


__all__ = [
    "EnvironmentErrorCategory", "EnvironmentErrorKind", "EnvironmentIssue",
    "EnvironmentUnavailable", "RecoveryRequest", "RecoveryResult",
    "EnvironmentRecovery", "EnvironmentRecoveryManager",
    "classify_backend_error", "classify_execution_failure",
    "environment_issue_from_dict",
]
