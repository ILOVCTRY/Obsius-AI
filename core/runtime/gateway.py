"""执行网关（DESIGN.md §7 四层纵深的核心层）。

Agent 无裸 shell：一切命令经 run(cmd, runtime)。
- runtime 由 Agent 按场景选，服务端按 policy 强制校验（越权 → 拒绝 + 审计）；
- net="real" 须引用一条已批准的 approval（§6 approvals 表）；
- 每次执行（含拒绝）落黑板事件流，带 runtime 标签（§7 审计层）。
"""

import time
from dataclasses import dataclass
from typing import Any

from core.blackboard.store import Blackboard
from core.runtime.backends import BackendError, DockerBackend, ExecOutcome, NativeBackend, WSLBackend
from core.runtime.policy import DEFAULT_NET_MODE, NET_MODES, RUNTIME_LEVELS, Level, allowed_levels


class GatewayDenied(PermissionError):
    """执行被策略拒绝。Agent 收到此异常必须放弃该动作并改道。"""

    def __init__(self, message: str, *, runtime: str = "", threat_class: str = ""):
        super().__init__(message)
        self.runtime = runtime
        self.threat_class = threat_class


@dataclass
class ExecutionResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    runtime: str
    duration_s: float
    timed_out: bool = False

    def brief(self, limit: int = 2000) -> str:
        """喂给 Agent 上下文的截断版（防止工具输出淹没上下文）。"""
        out = self.stdout[:limit]
        err = self.stderr[:limit]
        s = f"[{self.runtime}] exit={self.exit_code}"
        if self.timed_out:
            s += " (超时被杀)"
        if out:
            s += f"\n{out}"
        if err:
            s += f"\n[stderr]\n{err}"
        return s


DEFAULT_SANDBOX_IMAGE = "python:3.11-alpine"  # MVP 占位；专用分析镜像后续经 tools/ 管理


class ExecutionGateway:
    def __init__(
        self,
        bb: Blackboard | None = None,
        backends: dict[str, Any] | None = None,
        *,
        default_timeout: float = 120.0,
        sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
    ):
        self.bb = bb
        self.default_timeout = default_timeout
        self.sandbox_image = sandbox_image
        self.backends: dict[str, Any] = backends or {
            "host": NativeBackend(),
            "wsl": WSLBackend(),
            "docker": DockerBackend(),
            "sandbox": DockerBackend(),
        }

    # ---------- 主入口 ----------

    def run(
        self,
        cmd: str,
        runtime: str = "host",
        *,
        threat_class: str = "trusted",
        project_id: str | None = None,
        session_id: str | None = None,
        author: str = "system",
        timeout: float | None = None,
        net: str | None = None,
        image: str | None = None,
        approval_id: str | None = None,
    ) -> ExecutionResult:
        """执行一条命令。第②层防线（服务端校验）在此实现：
        runtime 不在 threat_class 允许集 → GatewayDenied + audit.deny 事件。
        """
        timeout = timeout or self.default_timeout
        started = time.monotonic()

        def _audit_deny(reason: str) -> GatewayDenied:
            denied = GatewayDenied(reason, runtime=runtime, threat_class=threat_class)
            if self.bb and project_id:
                self.bb.append_event(
                    project_id, "audit.deny",
                    {"cmd": cmd, "runtime": runtime, "threat_class": threat_class,
                     "reason": reason},
                    session_id=session_id, author=author,
                )
            return denied

        # ① runtime 名称合法
        if runtime not in RUNTIME_LEVELS:
            raise _audit_deny(f"未知 runtime: {runtime}")

        # ② 隔离等级校验（防越权核心）
        if Level(RUNTIME_LEVELS[runtime]) not in allowed_levels(threat_class):
            raise _audit_deny(
                f"策略拒绝：threat_class={threat_class} 不允许 runtime={runtime}"
                f"（允许集: {sorted(allowed_runtimes_names(threat_class))}）"
            )

        # ③ 网络模式校验：real 需已批准的 approval
        if net is None:
            net = DEFAULT_NET_MODE if runtime == "sandbox" else "bridge"
        if net not in NET_MODES and net != "bridge":
            raise _audit_deny(f"非法网络模式: {net}")
        if net == "real":
            if not (approval_id and self._approval_approved(approval_id)):
                raise _audit_deny(
                    f"net=real 须人工审批：approval_id={approval_id} 缺失或未批准"
                )

        # ④ 执行
        try:
            outcome = self._dispatch(cmd, runtime, timeout, net, image)
        except BackendError as e:
            raise _audit_deny(f"后端不可用: {e}") from e
        except NotImplementedError as e:
            raise _audit_deny(str(e)) from e

        result = ExecutionResult(
            ok=(not outcome.timed_out and outcome.exit_code == 0),
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            runtime=runtime,
            duration_s=round(time.monotonic() - started, 3),
            timed_out=outcome.timed_out,
        )
        # ⑤ 审计：命令与结果都带 runtime 标签落事件流
        if self.bb and project_id:
            self.bb.append_event(
                project_id, "command",
                {"cmd": cmd, "runtime": runtime, "threat_class": threat_class, "net": net},
                session_id=session_id, author=author,
            )
            self.bb.append_event(
                project_id, "command.result",
                {"runtime": runtime, "exit_code": result.exit_code,
                 "duration_s": result.duration_s, "timed_out": result.timed_out,
                 "stdout_head": result.stdout[:500], "stderr_head": result.stderr[:500]},
                session_id=session_id, author=author,
            )
        return result

    # ---------- 内部 ----------

    def _dispatch(self, cmd: str, runtime: str, timeout: float, net: str,
                  image: str | None) -> ExecOutcome:
        backend = self.backends.get(runtime)
        if backend is None:
            raise BackendError(f"runtime {runtime} 无对应后端")
        if runtime == "sandbox":
            return backend.run_once(
                image or self.sandbox_image, cmd, net=net, sandbox=True, timeout=timeout
            )
        if runtime == "docker":
            return backend.run_once(image or "alpine", cmd, net=net, sandbox=False,
                                    timeout=timeout)
        return backend.execute(cmd, timeout=timeout)

    def _approval_approved(self, approval_id: str) -> bool:
        if not self.bb:
            return False
        row = self.bb.conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        return row is not None and row["status"] == "approved"


def allowed_runtimes_names(threat_class: str) -> list[str]:
    return sorted(
        name for name, lv in RUNTIME_LEVELS.items()
        if lv in allowed_levels(threat_class)
    )
