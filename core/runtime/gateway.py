"""执行网关（DESIGN.md §7 四层纵深的核心层）。

Agent 无裸 shell：一切命令经 run(cmd, runtime)。
- runtime 由 Agent 按场景选，服务端按 policy 强制校验（越权 → 拒绝 + 审计）；
- net="real" 须引用一条已批准的 approval（§6 approvals 表）；
- 每次执行（含拒绝）落黑板事件流，带 runtime 标签（§7 审计层）。
"""

import os
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blackboard.store import Blackboard
from core.runtime import pathguard, rateguard
from core.runtime.backends import BackendError, DockerBackend, ExecOutcome, NativeBackend, WSLBackend
from core.runtime.pathguard import windows_to_wsl_path
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
    interrupted: bool = False

    def brief(self, limit: int = 2000) -> str:
        """喂给 Agent 上下文的截断版（防止工具输出淹没上下文）。

        截断时尾部追加可见标记（2026-09-20）：Agent 必须能察觉被切，
        否则靠语义发现浪费 LLM 轮次（见 DESIGN.md §7 截断可见化）。
        """
        out = self.stdout[:limit]
        err = self.stderr[:limit]
        s = f"[{self.runtime}] exit={self.exit_code}"
        if self.interrupted:
            s += " (被人手中断)"
        elif self.timed_out:
            s += " (超时被杀)"
        if out:
            if len(self.stdout) > limit:
                out += f"\n…[stdout 已截断：{limit}/{len(self.stdout)} 字符，请缩小窗口分段读]"
            s += f"\n{out}"
        if err:
            if len(self.stderr) > limit:
                err += f"\n…[stderr 已截断：{limit}/{len(self.stderr)} 字符，请缩小窗口分段读]"
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
        workspace: str | Path | None = None,
        abort_event: threading.Event | None = None,
    ) -> ExecutionResult:
        """执行一条命令。第②层防线（服务端校验）在此实现：
        runtime 不在 threat_class 允许集 → GatewayDenied + audit.deny 事件。

        workspace（工作区隔离，§7 2026-09-17）：host/wsl 执行时传项目 workspace 根——
        ①写目标逃逸工作区 → 网关硬拒（pathguard，只拦写不拦读）；
        ②cwd 强制 = <workspace>/scratch（临时中间文件统一去处）；
        ③TEMP/TMP 重定向 = <workspace>/.tmp（host）；WSL 用 TMPDIR 前缀等价处理。
        docker/sandbox 零挂载本不落盘，workspace 不生效。
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

        # ①②③'③'' 策略校验统一走 would_deny（H3：干跑判定与真实执行单一出处，
        # 防两处口径漂移；net=real 审批校验在下方单独做——它依赖 approval_id）
        deny = self.would_deny(cmd, runtime, threat_class=threat_class,
                               net=net, workspace=workspace)
        if deny:
            raise _audit_deny(deny)

        # ③ 网络模式校验：real 需已批准的 approval
        if net is None:
            net = DEFAULT_NET_MODE if runtime == "sandbox" else "bridge"
        if net == "real":
            if not (approval_id and self._approval_approved(approval_id)):
                raise _audit_deny(
                    f"net=real 须人工审批：approval_id={approval_id} 缺失或未批准"
                )

        # ③' 工作区隔离（§7 2026-09-17）：仅 host/wsl 有宿主文件系统边界问题
        exec_cmd = cmd
        cwd: str | None = None
        env: dict[str, str] | None = None
        scratch_str = ""
        if workspace is not None and runtime in ("host", "wsl"):
            ws = Path(workspace)
            scratch = ws / "scratch"
            tmp = ws / ".tmp"
            scratch.mkdir(parents=True, exist_ok=True)
            tmp.mkdir(parents=True, exist_ok=True)
            posix = runtime == "wsl"
            scratch_str = windows_to_wsl_path(scratch) if posix else str(scratch)
            if runtime == "host":
                cwd = str(scratch)
                env = {**os.environ, "TEMP": str(tmp), "TMP": str(tmp)}
            else:  # wsl：cwd 走 --cd（WSL 侧路径）；TMPDIR 前缀重定向
                cwd = scratch_str
                wsl_tmp = windows_to_wsl_path(tmp)
                exec_cmd = f"export TMPDIR={shlex.quote(wsl_tmp)}; {cmd}"

        # ③'' 扫描限速纪律（借鉴 dsh-scanner-tools rateDiscipline，2026-09-19）：
        # 裸奔扫描一跑就是数万包——补限速参数即可重试，拒因文案自带放行配方
        rate_reason = rateguard.check_rate(cmd)
        if rate_reason:
            raise _audit_deny(rate_reason)

        # ④ 执行（审计起始事件在此前落——前端直播可见「运行中」状态；
        # call_id 供前端与 command.result 配对。被拒命令不走到这里，不落 command 事件）
        call_id = "cmd-" + uuid.uuid4().hex[:12]
        if self.bb and project_id:
            self.bb.append_event(
                project_id, "command",
                {"call_id": call_id, "cmd": cmd, "runtime": runtime,
                 "threat_class": threat_class, "net": net,
                 **({"cwd": cwd} if cwd else {})},
                session_id=session_id, author=author,
            )
        try:
            # abort_event（■ 即点即停 2026-09-19）：命令执行中置位 → 后端杀进程树
            # 立即返回 interrupted，不等命令自然结束；结果 ok=False 进 command.result
            outcome = self._dispatch(exec_cmd, runtime, timeout, net, image,
                                     cwd=cwd, env=env, abort_event=abort_event)
        except BackendError as e:
            raise _audit_deny(f"后端不可用: {e}") from e
        except NotImplementedError as e:
            raise _audit_deny(str(e)) from e

        result = ExecutionResult(
            ok=(not outcome.timed_out and not outcome.interrupted
                and outcome.exit_code == 0),
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            runtime=runtime,
            duration_s=round(time.monotonic() - started, 3),
            timed_out=outcome.timed_out,
            interrupted=outcome.interrupted,
        )
        # ⑤ 审计：结果带 runtime 标签 + call_id/cmd 冗余落事件流（cwd=工作区隔离落点，W4；
        # stdout/stderr 只存头部 2000 字符，前端展开块消费）
        if self.bb and project_id:
            self.bb.append_event(
                project_id, "command.result",
                {"call_id": call_id, "cmd": cmd, "runtime": runtime,
                 "exit_code": result.exit_code,
                 "duration_s": result.duration_s, "timed_out": result.timed_out,
                 **({"interrupted": True} if result.interrupted else {}),
                 "stdout_head": result.stdout[:2000], "stderr_head": result.stderr[:2000]},
                session_id=session_id, author=author,
            )
        # H3 一次性审批（借鉴 dsh approval allowed-once）：approval 只批准「一次
        # 执行」——本次命令真跑完后立即标记 consumed，同 approval_id 二次使用会被
        # _approval_approved 拒绝（net=real 长期通行证在此封死）。失败不影响执行结果。
        if approval_id:
            self._consume_approval(approval_id)
        return result

    # ---------- 内部 ----------

    def would_deny(
        self, cmd: str, runtime: str, *, threat_class: str = "trusted",
        net: str | None = None, workspace: str | Path | None = None,
    ) -> str | None:
        """干跑策略校验（不执行、不审计）：返回拒因文本，None=当前策略允许放行。

        H3（2026-09-19）：deny-driven 升级的前置判定 + run() 内部复用——策略
        口径单一出处。覆盖 ①runtime 合法 ②隔离等级 ③非法网络模式 ③'工作区逃逸
        ③''限速纪律；**不含** net=real 审批校验（依赖 approval_id，由 run() 自查）
        与后端可用性（执行期才知道）。
        """
        if runtime not in RUNTIME_LEVELS:
            return f"未知 runtime: {runtime}"
        if Level(RUNTIME_LEVELS[runtime]) not in allowed_levels(threat_class):
            return (
                f"策略拒绝：threat_class={threat_class} 不允许 runtime={runtime}"
                f"（允许集: {sorted(allowed_runtimes_names(threat_class))}）"
            )
        if net is not None and net not in NET_MODES and net != "bridge":
            return f"非法网络模式: {net}"
        if workspace is not None and runtime in ("host", "wsl"):
            ws = Path(workspace)
            posix = runtime == "wsl"
            scratch_str = (windows_to_wsl_path(ws / "scratch") if posix
                           else str(ws / "scratch"))
            escapes = pathguard.workspace_escapes(
                cmd, scratch=scratch_str,
                workspace=windows_to_wsl_path(ws) if posix else str(ws),
                posix=posix,
            )
            if escapes:
                return (
                    "工作区隔离：命令试图写入工作区外（"
                    + ", ".join(escapes[:5])
                    + "）。改用相对路径写当前工作目录（scratch，可随时清理），"
                    "正式产物走 bb_add_artifact。"
                )
        return rateguard.check_rate(cmd)

    def _dispatch(self, cmd: str, runtime: str, timeout: float, net: str,
                  image: str | None, cwd: str | None = None,
                  env: dict[str, str] | None = None,
                  abort_event: threading.Event | None = None) -> ExecOutcome:
        backend = self.backends.get(runtime)
        if backend is None:
            raise BackendError(f"runtime {runtime} 无对应后端")
        if runtime == "sandbox":
            return backend.run_once(
                image or self.sandbox_image, cmd, net=net, sandbox=True, timeout=timeout,
                abort_event=abort_event
            )
        if runtime == "docker":
            return backend.run_once(image or "alpine", cmd, net=net, sandbox=False,
                                    timeout=timeout, abort_event=abort_event)
        return backend.execute(cmd, timeout=timeout, cwd=cwd, env=env,
                               abort_event=abort_event)

    def _approval_approved(self, approval_id: str) -> bool:
        if not self.bb:
            return False
        row = self.bb.conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        return row is not None and row["status"] == "approved"

    def _consume_approval(self, approval_id: str) -> None:
        """审批一次性消费：approved → consumed（H3）。幂等且尽力而为：
        无 bb / 行不存在 / 已 consumed 均静默跳过。"""
        if not self.bb:
            return
        try:
            with self.bb._tx():
                self.bb.conn.execute(
                    "UPDATE approvals SET status='consumed' WHERE id=? AND status='approved'",
                    (approval_id,))
        except Exception:  # noqa: BLE001 —— 消费失败不影响执行结果
            pass


def allowed_runtimes_names(threat_class: str) -> list[str]:
    return sorted(
        name for name, lv in RUNTIME_LEVELS.items()
        if lv in allowed_levels(threat_class)
    )
