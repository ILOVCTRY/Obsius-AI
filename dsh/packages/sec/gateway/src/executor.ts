/**
 * SecShellExecutor：替换 ctx.shell（单实现 seam）。
 * 继承 LocalBashExecutor 以复用 executeArgv 的 deadline/spawn/输出收集/分类；
 * 固定 sandboxMode='workspace-write'（permission-presets 启动硬校验依赖）。
 * runCommand 为安全网关富入口，按 runtime 分发：
 *   host=PowerShell（cwd=scratch，TEMP/TMP=.tmp）；
 *   wsl=wsl.exe --exec bash -lc（--cd scratch，TMPDIR=.tmp）；
 *   docker=加固容器 + 挂载 workspace；sandbox=加固容器零挂载（经 ctx.sandbox.confine）。
 */
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import type { SandboxMode } from '@deepseek-ai/dsh-sandbox'
import {
  SandboxUnavailableError,
  classifyRunnerFailure,
} from '@deepseek-ai/dsh-sandbox'
import type { RunnerFailureRule } from '@deepseek-ai/dsh-sandbox'
import { LocalBashExecutor } from '@deepseek-ai/dsh-bash-local'
import type { ShellExecRequest } from '@deepseek-ai/dsh-shell'
import { GatewayDenied, wouldDeny } from './policy'
import { buildBrief } from './brief'
import { toWslPath } from './pathguard'
import type { RunCommandInput, SecCommandResult } from './types'

export class SecShellExecutor extends LocalBashExecutor {
  static override inject = ['subprocess', 'sandbox', 'sandboxPolicy']

  /** permission-presets 只判 !== undefined；固定 workspace-write 与 L2 默认语义对齐。 */
  override get sandboxMode(): SandboxMode {
    return 'workspace-write'
  }

  /**
   * 安全网关唯一命令执行面：
   * ①wouldDeny 干跑（拒因单源，被拒只落拒绝不触命令）；
   * ②按 runtime 构造 argv/环境，绝不手拼 Windows 命令行；
   * ③执行后 runner-failure 复查 → SANDBOX_UNAVAILABLE，绝不走 host 兜底。
   */
  async runCommand(input: RunCommandInput, callerSignal?: AbortSignal): Promise<SecCommandResult> {
    const runtime = input.runtime ?? 'host'
    const threatClass = input.threatClass ?? 'trusted'
    const started = performance.now()

    // 工作区根=scratch 父目录（入口插件 cwd 指向 <ws>/scratch）。
    const scratch = this.config.cwd.get() ?? process.cwd()
    const workspaceRoot = dirname(scratch)
    const tmp = join(workspaceRoot, '.tmp')

    // 各 runtime 的路径语义：host=Windows 风味；wsl=/mnt 路径；
    // docker=容器内 /workspace；sandbox 零挂载不检查。
    const wsBound =
      runtime === 'host'
        ? { scratch, workspace: workspaceRoot, posix: false }
        : runtime === 'wsl'
          ? { scratch: toWslPath(scratch), workspace: toWslPath(workspaceRoot), posix: true }
          : runtime === 'docker'
            ? { scratch: '/workspace/scratch', workspace: '/workspace', posix: true }
            : null
    const reason = wouldDeny({
      cmd: input.cmd, runtime, threatClass,
      net: input.net ?? null, workspace: wsBound,
    })
    if (reason !== null) {
      this.ctx.logger.warn(`sec-gateway: 拒绝 runtime=${runtime} threat=${threatClass}: ${reason}`)
      throw new GatewayDenied(reason, runtime, threatClass)
    }

    mkdirSync(scratch, { recursive: true })
    mkdirSync(tmp, { recursive: true })

    // host/wsl 的固定 argv；docker/sandbox 走 prepare 回调（provider 可能拒绝）。
    let argv: readonly string[] | null = null
    const spawnEnv: Record<string, string> = {}
    if (runtime === 'host') {
      spawnEnv.TEMP = tmp
      spawnEnv.TMP = tmp
      argv = process.platform === 'win32'
        ? ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', input.cmd]
        : ['bash', '-lc', input.cmd]
    } else if (runtime === 'wsl') {
      // --exec argv 直通，引号保真（默认包装层会剥引号致 $var 被吞）。
      argv = [
        'wsl.exe', '--cd', toWslPath(scratch),
        '--exec', 'bash', '-lc',
        `export TMPDIR='${toWslPath(tmp)}'; ${input.cmd}`,
      ]
    }

    const request: ShellExecRequest = {
      command: input.cmd,
      workdir: scratch,
      ...runtime === 'host' ? { env: spawnEnv } : {},
      ...input.timeoutMs !== undefined ? { timeoutMs: input.timeoutMs } : {},
      ...callerSignal !== undefined ? { signal: callerSignal } : {},
    }
    const spec = this.resolve(request)

    let runnerRules: readonly RunnerFailureRule[] = []
    const execution =
      runtime === 'docker' || runtime === 'sandbox'
        ? await this.executeArgv(spec, async (signal) => {
            // provider 抛 SandboxUnavailableError → 直接穿透给调用方，无降级。
            const confined = await this.ctx.sandbox.confine(
              ['sh', '-c', input.cmd],
              {
                mode: runtime === 'docker' ? 'workspace-write' : 'read-only',
                workspaceRoot,
              },
              signal,
            )
            runnerRules = confined.runnerFailureRules
            return confined.argv
          })
        : await this.executeArgv(spec, argv ?? ['echo'])

    const run = await execution.result()
    const durationMs = Math.round(performance.now() - started)
    const timedOut = run.timedOut
    const interrupted = run.aborted
    const exitCode = run.exitCode
    const stdout = run.stdout.text
    const stderr = run.stderr.text

    // runner 失败复查：docker CLI 非零退出且 stderr 命中 runner 方言
    // → 命令没在容器里跑，按 SANDBOX_UNAVAILABLE 处理。
    const runnerFailure = classifyRunnerFailure(exitCode, stderr, runnerRules)
    if (runnerFailure !== undefined) {
      const mode = runtime === 'sandbox' ? 'read-only' : 'workspace-write'
      throw new SandboxUnavailableError(mode, runnerFailure.detail)
    }

    const ok = !timedOut && !interrupted && exitCode === 0
    return {
      ok, exitCode, runtime, durationMs, timedOut, interrupted, stdout, stderr,
      brief: buildBrief({ runtime, exitCode, stdout, stderr, timedOut, interrupted }),
    }
  }
}
