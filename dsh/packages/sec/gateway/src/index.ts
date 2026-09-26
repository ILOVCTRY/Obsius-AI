/**
 * sec-gateway 入口插件：一个入口装配两个 Service + 一道前置守卫。
 * ctx.sandbox → DockerSandboxProvider；ctx.shell → SecShellExecutor；
 * tools/pre-execute 对 run_cmd 做 wouldDeny 干跑（威胁矩阵/net/pathguard/rateguard），
 * executor.runCommand 内再做一次同函数兜底，拒因单源不漂移。
 * @module @sec/gateway
 */
import { Context } from '@deepseek-ai/cordis'
import type { PreToolDecision, ToolExecution } from '@deepseek-ai/dsh-tools'
import z from '@deepseek-ai/schemastery'
import type { GatewayConfig } from './types'
import { DockerSandboxProvider } from './docker-provider'
import { SecShellExecutor } from './executor'
import { wouldDeny } from './policy'
import { toWslPath } from './pathguard'

export { SecShellExecutor } from './executor'
export { DockerSandboxProvider } from './docker-provider'
export { GatewayDenied, wouldDeny, allowedLevels, allowedRuntimes } from './policy'
export { buildBrief } from './brief'
export { toWslPath } from './pathguard'
export type { GatewayConfig, RunCommandInput, SecCommandResult } from './types'

export const name = 'sec-gateway'
export const inject = ['tools']

export const Config = z.object({
  workspaceRoot: z.string(),
  dockerImage: z.string().default('cyberstrike/pentest-box:0.1'),
  sandboxImage: z.string().default('python:3.11-alpine'),
  probeTtlMs: z.number().default(60_000),
})

/** run_cmd 入参形状（守卫只读，工具体内自有 schema 校验）。 */
interface RunCmdArguments {
  cmd?: string
  runtime?: string
  threat_class?: string
  net?: string | null
}

export function apply(ctx: Context, config: GatewayConfig): void {
  ctx.plugin(DockerSandboxProvider, {
    dockerImage: config.dockerImage,
    sandboxImage: config.sandboxImage,
    probeTtlMs: config.probeTtlMs,
  })
  // 与 executor 内 wsBound 逐字一致：cwd 配置即 `${workspaceRoot}/scratch`。
  const scratch = `${config.workspaceRoot}/scratch`
  ctx.plugin(SecShellExecutor, { cwd: scratch })

  ctx.on('tools/pre-execute', async (exec, next): Promise<PreToolDecision> => {
    if (exec.name !== 'run_cmd') return next()
    const args = (exec.arguments ?? {}) as RunCmdArguments
    const runtime = args.runtime ?? 'host'
    const bound =
      runtime === 'host'
        ? { scratch, workspace: config.workspaceRoot, posix: false }
        : runtime === 'wsl'
          ? { scratch: toWslPath(scratch), workspace: toWslPath(config.workspaceRoot), posix: true }
          : runtime === 'docker'
            ? { scratch: '/workspace/scratch', workspace: '/workspace', posix: true }
            : null
    const reason = wouldDeny({
      cmd: args.cmd ?? '',
      runtime,
      threatClass: args.threat_class ?? 'trusted',
      net: args.net ?? null,
      workspace: bound,
    })
    return reason !== null ? { kind: 'deny', reason } : next()
  })
}
