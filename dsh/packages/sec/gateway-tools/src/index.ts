/**
 * sec-gateway-tools：模型命令工具组——run_cmd 唯一命令口。
 * Agent 无裸 shell：一切命令经 run_cmd，服务端按威胁矩阵强制校验。
 * @module @sec/gateway-tools
 */
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { SecShellExecutor } from '@sec/gateway'

export const name = 'sec-gateway-tools'
export const inject = ['shell', 'tools']

const RUNTIMES = ['host', 'wsl', 'docker', 'sandbox'] as const
const THREATS = ['trusted', 'untrusted', 'malware_live', 'unknown'] as const
const NETS = ['none', 'fakenet', 'real'] as const

export function apply(ctx: Context): void {
  const runCmd = defineTool({
    name: 'run_cmd',
    description: [
      '安全网关唯一命令口：在指定隔离等级执行一条命令，返回 exit 码与输出摘要。',
      'runtime：host=宿主 PowerShell（仅 trusted）；wsl=WSL2（信任级=宿主，M1.2）；',
      'docker=普通容器（不可信代码默认，M1.2）；sandbox=加固沙箱（活体恶意样本，M1.2）。',
      'threat_class 按命令来源如实声明；不确定时用 unknown（按最严处理）。',
      'M1 阶段 net 仅支持 none，net=real 须 M2 人工审批。',
    ].join('\n'),
    parameters: {
      cmd: {
        type: 'string',
        description: '要执行的命令（host=PowerShell 语法；docker/sandbox=sh 语法）。',
        required: true,
      },
      runtime: {
        type: 'string',
        enum: RUNTIMES,
        description: '隔离等级，缺省 host。',
      },
      threat_class: {
        type: 'string',
        enum: THREATS,
        description: '威胁分类，缺省 trusted；unknown/非法值按 malware_live 处理。',
      },
      net: {
        type: 'string',
        enum: NETS,
        description: '网络模式，M1 仅 none。',
      },
      approval_id: {
        type: 'string',
        description: 'net=real 已批准的审批 id（M2，一次性消费）。',
      },
      timeout: {
        type: 'number',
        description: '超时（秒），缺省 120；上限 600。',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          exit_code: {
            oneOf: [{ type: 'number' }, { type: 'null' }],
            required: true,
          },
          runtime: { type: 'string', required: true },
          duration_ms: { type: 'number', required: true },
          timed_out: { type: 'boolean', required: true },
          interrupted: { type: 'boolean', required: true },
          brief: {
            type: 'string',
            description: '模型可读结果摘要（stdout/stderr 各保留 2000 字符，截断有标注）。',
            required: true,
          },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.brief }],
      presentationMeta: (_args, value) => ({
        runtime: value.runtime,
        exit_code: value.exit_code,
        ok: value.ok,
      }),
    },
    async execute(args, exec) {
      // profile 保证 sec-gateway 已把 ctx.shell 替换为 SecShellExecutor。
      const shell = ctx.shell as SecShellExecutor
      const result = await shell.runCommand(
        {
          cmd: args.cmd,
          runtime: args.runtime ?? 'host',
          threatClass: args.threat_class ?? 'trusted',
          net: args.net ?? null,
          ...args.approval_id !== undefined ? { approvalId: args.approval_id } : {},
          ...args.timeout !== undefined ? { timeoutMs: args.timeout * 1000 } : {},
        },
        exec.signal,
      )
      return {
        ok: result.ok,
        exit_code: result.exitCode,
        runtime: result.runtime,
        duration_ms: result.durationMs,
        timed_out: result.timedOut,
        interrupted: result.interrupted,
        brief: result.brief,
      }
    },
  })
  ctx.tools.register(runCmd)
}
