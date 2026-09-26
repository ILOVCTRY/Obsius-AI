/**
 * 威胁矩阵与策略干跑（移植 core/runtime/policy.py + gateway.would_deny）。
 * 核心规则：WSL 信任级 = 宿主机；不可信代码只允许容器；
 * 未知/非法威胁分类一律按恶意样本处理（安全默认值，宁严勿松）。
 */
import { workspaceEscapes } from './pathguard'
import type { WorkspaceBound } from './pathguard'
import { checkRate } from './rateguard'

/** 隔离等级（数值越大隔离越强）。 */
export const Level = {
  L0_HOST: 0,
  L1_WSL: 1,
  L2_DOCKER: 2,
  L3_SANDBOX: 3,
} as const

/** runtime 名称 → 隔离等级。 */
export const RUNTIME_LEVELS = {
  host: Level.L0_HOST,
  wsl: Level.L1_WSL,
  docker: Level.L2_DOCKER,
  sandbox: Level.L3_SANDBOX,
} as const
export type RuntimeName = keyof typeof RUNTIME_LEVELS

/** 合法威胁分类（unknown 为显式入参，运行时归一到 malware_live）。 */
export type ThreatClass = 'trusted' | 'untrusted' | 'malware_live'

/** threat_class → 允许的等级集合。 */
export const THREAT_ALLOWED = {
  trusted: [Level.L0_HOST, Level.L1_WSL, Level.L2_DOCKER, Level.L3_SANDBOX],
  untrusted: [Level.L2_DOCKER, Level.L3_SANDBOX],
  malware_live: [Level.L3_SANDBOX],
} satisfies Record<ThreatClass, readonly number[]>

/** L3 网络模式：real 永不默认，需人工审批。 */
export const NET_MODES = ['none', 'fakenet', 'real'] as const

/**
 * 未知（unknown）/非法 threat_class → 按恶意样本处理（§7：安全默认值）。
 */
export function allowedLevels(threatClass: string): readonly number[] {
  return Object.hasOwn(THREAT_ALLOWED, threatClass)
    ? THREAT_ALLOWED[threatClass as ThreatClass]
    : THREAT_ALLOWED.malware_live
}

/** 威胁分类允许的 runtime 名称（升序）。 */
export function allowedRuntimes(threatClass: string): RuntimeName[] {
  const levels = allowedLevels(threatClass)
  return (Object.keys(RUNTIME_LEVELS) as RuntimeName[])
    .filter(name => levels.includes(RUNTIME_LEVELS[name]))
    .sort()
}

/** 策略拒绝错误：调用方（Agent）必须放弃该动作并改道。 */
export class GatewayDenied extends Error {
  readonly code = 'GATEWAY_DENIED'
  constructor(
    readonly reason: string,
    readonly runtime: string,
    readonly threatClass: string,
  ) {
    super(reason)
    this.name = 'GatewayDenied'
  }
}

/** wouldDeny 入参。 */
export interface WouldDenyInput {
  cmd: string
  runtime: string
  threatClass?: string
  net?: string | null
  /** 工作区边界（host/wsl/docker）；sandbox/缺省不检查。 */
  workspace?: WorkspaceBound | null
}

/**
 * 干跑策略校验（不执行、不审计）：返回拒因文本，null=当前策略允许放行。
 * 拒因单源——runCommand 与 pre-execute 守卫（M1.3）共用本函数，防口径漂移。
 * 覆盖：①runtime 合法 ②隔离等级 ③网络模式（M1 仅 none）。
 * 工作区逃逸/限速纪律已在 M1.3 接入；不含 net=real 审批与后端可用性。
 */
export function wouldDeny(input: WouldDenyInput): string | null {
  const runtime = input.runtime
  if (!Object.hasOwn(RUNTIME_LEVELS, runtime)) {
    return `未知 runtime: ${runtime}`
  }
  const threatClass = input.threatClass ?? 'trusted'
  const level = RUNTIME_LEVELS[runtime as RuntimeName]
  if (!allowedLevels(threatClass).includes(level)) {
    return (
      `策略拒绝：threat_class=${threatClass} 不允许 runtime=${runtime}`
      + `（允许集: ${allowedRuntimes(threatClass).join(', ')}）`
    )
  }
  const net = input.net ?? null
  if (net !== null) {
    if (!(NET_MODES as readonly string[]).includes(net)) {
      return `非法网络模式: ${net}`
    }
    if (net !== 'none') {
      return net === 'real'
        ? 'net=real 须人工审批：M2 approvals 落地前一律拒绝'
        : `M1 仅支持 net=none：net=${net} 将在 M2 后开放`
    }
  }
  // ③' 工作区逃逸（只拦写）
  if (input.workspace) {
    const escapes = workspaceEscapes(input.cmd, input.workspace)
    if (escapes.length > 0) {
      return (
        '工作区隔离：命令试图写入工作区外（'
        + escapes.slice(0, 5).join(', ')
        + '）。改用相对路径写当前工作目录（scratch，可随时清理），'
        + '正式产物走 bb_add_artifact。'
      )
    }
  }
  // ③'' 限速纪律
  return checkRate(input.cmd)
}
