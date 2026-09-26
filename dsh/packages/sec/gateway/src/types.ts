/**
 * 网关公共类型。
 */

/** 入口插件配置（profile patch config 同形）。 */
export interface GatewayConfig {
  /** 项目工作区根：scratch/ 与 .tmp/ 的父目录，也是 docker 卷挂载源。 */
  workspaceRoot: string
  /** L2 docker runtime 默认镜像。 */
  dockerImage: string
  /** L3 sandbox runtime 默认镜像。 */
  sandboxImage: string
  /** detector 探测结果缓存 TTL（毫秒）。 */
  probeTtlMs: number
}

/** runCommand 入参（run_cmd 工具 execute 组装）。 */
export interface RunCommandInput {
  /** 命令文本（host=PowerShell 语法；docker/sandbox=sh 语法）。 */
  cmd: string
  /** 隔离等级 host/wsl/docker/sandbox，缺省 host。 */
  runtime?: string
  /** 威胁分类 trusted/untrusted/malware_live，缺省 trusted；未知值归 malware_live。 */
  threatClass?: string
  /** 网络模式 none/fakenet/real；M1 仅接受缺省/none。 */
  net?: string | null
  /** net=real 的人工审批 id（M2 approvals 落地后生效）。 */
  approvalId?: string
  /** 超时（毫秒），缺省走 executor 配置 120s。 */
  timeoutMs?: number
}

/** runCommand 富结果；brief 为模型可读摘要（带截断标注）。 */
export interface SecCommandResult {
  /** 非超时非中断且 exitCode===0。 */
  ok: boolean
  exitCode: number | null
  runtime: string
  durationMs: number
  timedOut: boolean
  interrupted: boolean
  stdout: string
  stderr: string
  brief: string
}
