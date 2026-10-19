"""执行网关（DESIGN.md §7 四层纵深的核心层）。

Agent 无裸 shell：一切命令经 run(cmd, runtime)。
- runtime 由 Agent 按场景选，服务端按 policy 强制校验（越权 → 拒绝 + 审计）；
- net="real" 须在项目上下文中引用一条已批准的一次性 approval（§6 approvals 表）；
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
from core.runtime.backends import (
    DEFAULT_PENTEST_IMAGE,
    BackendError,
    DockerBackend,
    ExecOutcome,
    NativeBackend,
    WSLBackend,
)
from core.runtime.pathguard import windows_to_wsl_path
from core.runtime.policy import DEFAULT_NET_MODE, NET_MODES, RUNTIME_LEVELS, Level, allowed_levels
from core.scope_policy import check_command_scope
from core.runtime.sessioncontainers import SessionContainerManager
from core.runtime.recovery import (
    EnvironmentUnavailable,
    classify_backend_error,
    classify_execution_failure,
)

#: docker runtime 的 workspace 挂载约定（pentest-tools-container-m0）：
#: 宿主 <workspace> 挂容器 /workspace 读写，cwd 固定 /workspace/scratch
_DOCKER_WS_MOUNT = "/workspace"
_DOCKER_WS_SCRATCH = "/workspace/scratch"


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
    # 环境失败的结构化分类（tool_missing / docker_image_missing 等）。
    # 与 GatewayDenied 分离，避免环境故障被拒绝熔断误判。
    environment_error: dict[str, Any] | None = None

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
            s += f" (超时被杀，已跑约 {self.duration_s:.0f}s)"
        if out:
            if len(self.stdout) > limit:
                out += f"\n…[stdout 已截断：{limit}/{len(self.stdout)} 字符，请缩小窗口分段读]"
            s += f"\n{out}"
        if err:
            if len(self.stderr) > limit:
                err += f"\n…[stderr 已截断：{limit}/{len(self.stderr)} 字符，请缩小窗口分段读]"
            s += f"\n[stderr]\n{err}"
        if self.timed_out:
            # 超时引导（2026-10-01）：整条命令被杀会丢掉全部输出，拆段是唯一可靠解。
            # 网关按整条命令超时，长驻请求（如目标 hang 住）会连带拖死同圈其他步骤。
            s += ("\n[超时引导] 本条命令超时被杀，输出可能不完整。建议："
                  "① 拆成多段分别跑，每段传更小的 timeout（如 15~30s）；"
                  "② 避免把抓取头/路径遍历变体等易挂起请求与常规步骤放在同一条命令；"
                  "③ 对每次请求单独加短超时（curl -m 10 / --max-time 10），"
                  "别让被测端 hang 死整条命令。")
        if self.environment_error:
            s += "\n[环境错误] " + str(self.environment_error.get("category", "unknown"))
            if self.environment_error.get("message"):
                s += ": " + str(self.environment_error["message"])
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
        containers: SessionContainerManager | None = None,
    ):
        self.bb = bb
        self.default_timeout = default_timeout
        self.sandbox_image = sandbox_image
        # 会话级常驻容器管理器（P3，2026-10-07）：None=关闭（默认），恒走 per-call run_once。
        # 仅 sess-* 会话的 docker runtime 会经它复用容器；sandbox/其它 runtime 不受影响。
        self.containers = containers
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
        environment_id: str | None = None,
        timeout: float | None = None,
        net: str | None = None,
        image: str | None = None,
        approval_id: str | None = None,
        workspace: str | Path | None = None,
        abort_event: threading.Event | None = None,
        step: int | None = None,
        consume_approval: bool = True,
    ) -> ExecutionResult:
        """执行一条命令。第②层防线（服务端校验）在此实现：
        runtime 不在 threat_class 允许集 → GatewayDenied + audit.deny 事件。

        workspace（工作区隔离，§7 2026-09-17）：host/wsl/docker 执行时传项目
        workspace 根——
        ①写目标逃逸工作区 → 网关硬拒（pathguard，只拦写不拦读）；
        ②cwd 强制 = <workspace>/scratch（临时中间文件统一去处）；
        ③TEMP/TMP 重定向 = <workspace>/.tmp（host）；WSL 用 TMPDIR 前缀等价处理；
        docker（2026-09-23 容器化 M0）挂载 <workspace>:/workspace 读写、
        cwd=/workspace/scratch，bash+工具链全 Linux 语义；sandbox 零挂载不落盘。
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
        # 防两处口径漂移）
        deny = self.would_deny(cmd, runtime, threat_class=threat_class,
                               net=net, workspace=workspace,
                               environment_id=environment_id or session_id)
        if deny:
            raise _audit_deny(deny)

        # 项目级 scope 门禁：命令中出现明确网络目标时，先于执行检查授权范围。
        # 动态目标无法静态提取时仍由 net/approval 与后端策略保护。
        if project_id and self.bb:
            scope_verdict = check_command_scope(self.bb, project_id, cmd)
            if not scope_verdict.allowed:
                raise _audit_deny("策略拒绝：" + scope_verdict.reason)

        # ③ 网络模式缺省：sandbox 默认 none，其余 runtime 默认 bridge。
        #    real 网络属于高风险出网，必须携带当前项目中已批准的单次审批；
        #    校验放在实际执行前，避免工具层或调用方绕过审批直接出网。
        if net is None:
            net = DEFAULT_NET_MODE if runtime == "sandbox" else "bridge"
        if net == "real":
            if not project_id:
                raise _audit_deny("策略拒绝：net=real 必须绑定项目上下文")
            if not approval_id:
                raise _audit_deny("策略拒绝：net=real 必须提供已批准的 approval_id")
            if not self._approval_approved(approval_id, project_id=project_id):
                raise _audit_deny(
                    "策略拒绝：approval_id 不存在、未批准、已消费或不属于当前项目")

        # ③' 工作区隔离（§7 2026-09-17）：host/wsl/docker 有文件系统边界问题；
        #     docker 容器化 M0（2026-09-23）挂 workspace 卷并对齐 scratch 相对路径习惯
        exec_cmd = cmd
        cwd: str | None = None
        env: dict[str, str] | None = None
        mounts: list[tuple[str, str]] | None = None
        scratch_str = ""
        if workspace is not None and runtime in ("host", "wsl", "docker"):
            ws = Path(workspace)
            scratch = ws / "scratch"
            tmp = ws / ".tmp"
            scratch.mkdir(parents=True, exist_ok=True)
            tmp.mkdir(parents=True, exist_ok=True)
            posix = runtime in ("wsl", "docker")
            scratch_str = windows_to_wsl_path(scratch) if runtime == "wsl" else str(scratch)
            if runtime == "host":
                cwd = str(scratch)
                env = {**os.environ, "TEMP": str(tmp), "TMP": str(tmp)}
            elif runtime == "wsl":  # wsl：cwd 走 --cd（WSL 侧路径）；TMPDIR 前缀重定向
                cwd = scratch_str
                wsl_tmp = windows_to_wsl_path(tmp)
                exec_cmd = f"export TMPDIR={shlex.quote(wsl_tmp)}; {cmd}"
            else:  # docker：整卷挂载（容器内 bash/Linux 语义，路径以 /workspace 为根）
                cwd = _DOCKER_WS_SCRATCH
                mounts = [(str(ws), _DOCKER_WS_MOUNT)]
                if str(environment_id or session_id or "").startswith("ctf-"):
                    # CTF Docker 的传统 /tmp 映射到项目 .tmp，跨命令、主控
                    # 与 worker 共享；普通 Docker 任务保持原挂载契约。
                    mounts.append((str(tmp), "/tmp"))

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
                 # step（D9 卡死预检的区间切分用）：仅执行器链路透传，其余调用方无
                 **({"cwd": cwd} if cwd else {}),
                 **({"step": step} if step is not None else {})},
                session_id=session_id, author=author,
            )
        try:
            # abort_event（■ 即点即停 2026-09-19）：命令执行中置位 → 后端杀进程树
            # 立即返回 interrupted，不等命令自然结束；结果 ok=False 进 command.result
            outcome = self._dispatch(exec_cmd, runtime, timeout, net, image,
                                     cwd=cwd, env=env, abort_event=abort_event,
                                     mounts=mounts, project_id=project_id,
                                     session_id=environment_id or session_id)
        except BackendError as e:
            # 后端环境故障不是策略拒绝：保留结构化类别，避免误触发 Agent 的
            # 拒绝熔断；恢复器/上层可据此决定是否执行白名单修复。
            issue = classify_backend_error(e, command=cmd, runtime=runtime)
            if self.bb and project_id:
                self.bb.append_event(
                    project_id, "command.result",
                    {"call_id": call_id, "cmd": cmd, "runtime": runtime,
                     "exit_code": -1, "duration_s": round(time.monotonic() - started, 3),
                     "environment_error": issue.to_dict()},
                    session_id=session_id, author=author,
                )
            raise EnvironmentUnavailable(issue) from e
        except NotImplementedError as e:
            raise _audit_deny(str(e)) from e

        issue = None if outcome.timed_out or outcome.interrupted else classify_execution_failure(
            command=cmd, runtime=runtime, exit_code=outcome.exit_code,
            stdout=outcome.stdout, stderr=outcome.stderr,
            image=(image or (DEFAULT_SANDBOX_IMAGE if runtime == "sandbox"
                             else DEFAULT_PENTEST_IMAGE if runtime == "docker" else None)),
        )
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
            environment_error=issue.to_dict() if issue else None,
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
                 **({"environment_error": result.environment_error}
                    if result.environment_error else {}),
                 "stdout_head": result.stdout[:2000], "stderr_head": result.stderr[:2000]},
                session_id=session_id, author=author,
            )
        # H3 一次性审批（借鉴 dsh approval allowed-once）：approval 只批准「一次
        # 执行」——本次命令真跑完后立即标记 consumed，同 approval_id 二次使用会被
        # _approval_approved 拒绝。审批消费失败不影响已经完成的执行结果，但下一次
        # 执行仍会因状态不是 approved 而被拒绝。
        # 环境故障不是一次完整的获准动作：保留 approval 供受控恢复链使用，
        # 由恢复后的最终重试（或调用方明确指定 consume_approval=True 的成功动作）
        # 消费一次性审批。策略拒绝仍在执行前直接拒绝，不会走到这里。
        if approval_id and consume_approval and not result.environment_error:
            self._consume_approval(approval_id)
        return result

    # ---------- 内部 ----------

    def would_deny(
        self, cmd: str, runtime: str, *, threat_class: str = "trusted",
        net: str | None = None,         workspace: str | Path | None = None,
        environment_id: str | None = None,
    ) -> str | None:
        """干跑策略校验（不执行、不审计）：返回拒因文本，None=当前策略允许放行。

        H3（2026-09-19）：deny-driven 升级的前置判定 + run() 内部复用——策略
        口径单一出处。覆盖 ①runtime 合法 ②隔离等级 ③非法网络模式 ③'工作区逃逸
        ③''限速纪律；**不含**后端可用性（执行期才知道）。net=real 的项目上下文、
        approval_id 和审批状态在 run() 的实际执行前校验，避免干跑接口承担审批消费。
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
        if workspace is not None and runtime in ("host", "wsl", "docker"):
            ws = Path(workspace)
            posix = runtime in ("wsl", "docker")
            # docker 容器侧路径以 /workspace 为根（挂载约定，pentest-tools-container-m0）
            scratch_str = (windows_to_wsl_path(ws / "scratch") if runtime == "wsl"
                           else str(ws / "scratch") if runtime == "host"
                           else _DOCKER_WS_SCRATCH)
            ws_str = (windows_to_wsl_path(ws) if runtime == "wsl"
                      else str(ws) if runtime == "host" else _DOCKER_WS_MOUNT)
            escapes = pathguard.workspace_escapes(
                cmd, scratch=scratch_str, workspace=ws_str, posix=posix,
                # CTF Docker 的 /tmp 映射到项目 .tmp，允许题目工具使用
                # 传统临时路径，同时保证跨命令/同项目容器可复用。
                allow_posix_tmp=(runtime == "docker"
                                  and str(environment_id or "").startswith("ctf-")),
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
                  abort_event: threading.Event | None = None,
                  mounts: list[tuple[str, str]] | None = None,
                  project_id: str | None = None,
                  session_id: str | None = None) -> ExecOutcome:
        backend = self.backends.get(runtime)
        if backend is None:
            raise BackendError(f"runtime {runtime} 无对应后端")
        if runtime == "sandbox":
            # L3 铁律（per-execution 一次性 + 零挂载 + 断网）：**永不**经常驻容器管理器
            return backend.run_once(
                image or self.sandbox_image, cmd, net=net, sandbox=True, timeout=timeout,
                abort_event=abort_event
            )
        if runtime == "docker":
            # 常驻容器：任务链仍使用 sess-*；CTF 对话使用 ctf-* 项目环境 key。
            # 无环境上下文或建容器失败 → 回落 per-call run_once（绝不失败）。
            mgr = self.containers
            if (mgr is not None and mgr.enabled and session_id
                    and (session_id.startswith("sess-") or session_id.startswith("ctf-"))):
                out = mgr.exec(project_id or "", session_id, cmd, timeout=timeout,
                               abort_event=abort_event, image=image, net=net,
                               mounts=mounts, cwd=cwd)
                if out is not None:
                    return out
            return backend.run_once(image or DEFAULT_PENTEST_IMAGE, cmd, net=net,
                                    sandbox=False, timeout=timeout,
                                    abort_event=abort_event,
                                    mounts=mounts, cwd=cwd)
        return backend.execute(cmd, timeout=timeout, cwd=cwd, env=env,
                               abort_event=abort_event)

    def _approval_approved(self, approval_id: str,
                           *, project_id: str | None = None) -> bool:
        if not self.bb:
            return False
        row = self.bb.conn.execute(
            "SELECT status, project_id FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if row is None or row["status"] != "approved":
            return False
        return project_id is None or row["project_id"] == project_id

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
