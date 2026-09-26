/**
 * Docker SandboxProvider：替换 ctx.sandbox（单实现 seam）。
 * confine 先经 detector 三态探测（CLI / daemon / 镜像，结果按 TTL 缓存），
 * 再拼装加固 docker run argv；不可用时抛 SandboxUnavailableError，
 * fail-closed，绝不直通宿主执行。
 */
import { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import {
  SandboxProvider,
  SandboxUnavailableError,
} from '@deepseek-ai/dsh-sandbox'
import type {
  ConfinedArgv,
  RunnerFailureRule,
  SandboxPolicy,
} from '@deepseek-ai/dsh-sandbox'

export interface ProviderConfig {
  dockerImage: string
  sandboxImage: string
  probeTtlMs: number
}

/** docker 自身（runner）失败的 stderr 方言：命令根本没在容器里跑起来。 */
const RUNNER_FAILURE_RULES: readonly RunnerFailureRule[] = [
  {
    fatalSignatures: [
      'cannot connect to the docker api',
      'error during connect',
      'unable to find image',
      'docker: command not found',
    ],
  },
]

/** 文件效果被拒的方言（L3 容器内只读场景的防御性识别）。 */
const DENIAL_SIGNATURES = ['permission denied', 'read-only file system'] as const

interface ProbeResult {
  exitCode: number | null
  stderr: string
}

interface CachedProbe {
  at: number
  error: string | null
}

export class DockerSandboxProvider extends SandboxProvider {
  static inject = ['subprocess']

  static Config = z.object({
    dockerImage: z.string().default('cyberstrike/pentest-box:0.1'),
    sandboxImage: z.string().default('python:3.11-alpine'),
    probeTtlMs: z.number().default(60_000),
  })

  private daemonCache: CachedProbe | null = null
  private imageCache = new Map<string, CachedProbe>()

  constructor(ctx: Context, readonly config: ProviderConfig) {
    super(ctx)
  }

  async confine(
    argv: readonly string[],
    policy: SandboxPolicy,
    signal?: AbortSignal,
  ): Promise<ConfinedArgv> {
    const mode = policy.mode
    const image = mode === 'read-only' ? this.config.sandboxImage : this.config.dockerImage

    const daemon = await this.daemonError(signal)
    if (daemon !== null) throw new SandboxUnavailableError(mode, daemon)
    const missing = await this.imageError(image, signal)
    if (missing !== null) throw new SandboxUnavailableError(mode, missing)

    return {
      argv: this.buildArgv(argv, policy, image),
      enforcement: 'full',
      denialSignatures: DENIAL_SIGNATURES,
      runnerFailureRules: RUNNER_FAILURE_RULES,
    }
  }

  /** 跑一条探测 argv（10s 内），收 stderr；spawn 失败按 CLI 缺失归类。 */
  private async runProbe(probeArgv: readonly string[], signal?: AbortSignal): Promise<ProbeResult> {
    let handle
    try {
      handle = this.ctx.subprocess.spawn({
        argv: [...probeArgv],
        cwd: process.cwd(),
        stdio: {
          stdin: 'ignore',
          stdout: { maxBytes: 4_000 },
          stderr: { maxBytes: 4_000 },
        },
        graceMs: 2_000,
        ...signal !== undefined ? { signal } : {},
      })
    } catch (error) {
      return {
        exitCode: -1,
        stderr: error instanceof Error ? error.message : 'spawn failed',
      }
    }
    const outcome = await handle.done
    return {
      exitCode: outcome.exitCode,
      stderr: handle.collected.stderr?.readFrom(0).text ?? '',
    }
  }

  /** daemon 探测：返回可操作拒因，null=就绪。结果缓存 probeTtlMs。 */
  private async daemonError(signal?: AbortSignal): Promise<string | null> {
    const cached = this.cached(this.daemonCache ?? undefined)
    if (cached !== undefined) return cached
    const r = await this.runProbe(['docker', 'version', '--format', '{{.Server.Version}}'], signal)
    let error: string | null = null
    if (r.exitCode !== 0) {
      const lower = r.stderr.toLowerCase()
      if (lower.includes('cannot connect') || lower.includes('error during connect')) {
        error = 'Docker daemon 未启动：请启动 Docker Desktop，等待其完全就绪后重试'
      } else if (r.exitCode === -1 || lower.includes('not recognized') || lower.includes('enoent')) {
        error = 'docker CLI 不可用：请安装 Docker Desktop 并将 docker 加入 PATH'
      } else {
        error = `docker 探测失败：${r.stderr.slice(0, 300)}`
      }
    }
    this.daemonCache = { at: performance.now(), error }
    return error
  }

  /** 镜像探测：返回可操作拒因，null=本地存在。按镜像缓存。 */
  private async imageError(image: string, signal?: AbortSignal): Promise<string | null> {
    const cached = this.cached(this.imageCache.get(image))
    if (cached !== undefined) return cached
    const r = await this.runProbe(['docker', 'image', 'inspect', image, '--format', 'ok'], signal)
    let error: string | null = null
    if (r.exitCode !== 0) {
      const fix = image === this.config.dockerImage
        ? 'pentest-box 构建：python scripts/build_pentest_box.py'
        : `拉取：docker pull ${image}`
      error = `镜像缺失或不可用：${image}（${fix}）`
    }
    const record = { at: performance.now(), error }
    this.imageCache.set(image, record)
    return error
  }

  /** 读缓存：未过期返回缓存拒因（可能 null=放行），过期/无缓存返回 undefined。 */
  private cached(record: CachedProbe | undefined): string | null | undefined {
    if (record === undefined) return undefined
    if (performance.now() - record.at >= this.config.probeTtlMs) return undefined
    return record.error
  }

  /** 拼装加固容器 argv：公共限额/降权 + L2 挂载工作目录 + 调用方 argv。 */
  private buildArgv(argv: readonly string[], policy: SandboxPolicy, image: string): string[] {
    const out = [
      'docker', 'run', '--rm', '--network', 'none',
      '--memory', '512m', '--pids-limit', '64', '--cpus', '1.0',
      '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL',
    ]
    if (policy.mode === 'workspace-write') {
      // 宿主路径转正斜杠：Docker Desktop 收正斜杠路径，免转义。
      const hostRoot = policy.workspaceRoot.replace(/\\/g, '/')
      out.push('-v', `${hostRoot}:/workspace`, '-w', '/workspace/scratch')
    }
    out.push(image, ...argv)
    return out
  }
}
