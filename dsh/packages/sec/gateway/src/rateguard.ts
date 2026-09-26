/**
 * 扫描限速纪律（移植 core/runtime/rateguard.py，借鉴 dsh rateDiscipline）。
 * 拦的不是「能不能扫」，而是「不许不限速地扫」：拒因文案自带放行参数，
 * Agent 补参数即可重试。纯静态文本检查，不是资源隔离。
 */
import { clean, splitTokens } from './pathguard'

/** nmap 全端口取值。 */
const FULL_RANGE = new Set(['-', '1-65535', '0-65535', '-65535'])

/** 规则表快照（校验与展示单一事实源）。 */
export interface RateRule {
  requirement: string
  params: string[]
  hint: string
  threshold?: number
}

export const RATE_RULES: Record<string, RateRule> = {
  nmap: {
    requirement: '全端口扫描未带限速参数（一跑就是 65535 端口全速发包）',
    params: ['-T0..3', '--max-rate', '--max-parallelism', '--scan-delay'],
    hint: '补任一即可放行：-T3 --max-rate 200（推荐）/ -T0..2 / --max-parallelism / --scan-delay',
  },
  masscan: {
    requirement: '--rate 超过阈值 1000（p/s）',
    params: ['--rate'],
    hint: '降到 --rate 1000 及以下（保守默认 500）再执行',
    threshold: 1000,
  },
  ffuf: {
    requirement: '未带速率限制（默认不限速全速跑）',
    params: ['-rate', '-rl', '-t'],
    // pentest-box 自带 ffuf 1.1 无任何速率旗标，只能 -t 限并发
    hint: '补任一：-rl 50 / -rate 50（新版按秒限速），旧版（如镜像内 ffuf 1.1）用 -t 5 限并发',
  },
  hydra: {
    requirement: '未带并发任务数 -t（默认 16 并发易触发账户锁定/封禁）',
    params: ['-t'],
    hint: '补 -t 8（推荐）及以下再执行',
  },
}

/** 按表组装拒因。 */
function reject(tool: string): string {
  const r = RATE_RULES[tool]!
  return `限速纪律：${tool} ${r.requirement}。${r.hint}`
}

function segments(cmd: string): string[][] {
  // ; && || | 分段（引号感知在段内再切）。
  return cmd
    .split(/&&|\|\||;|\|/)
    .map(seg => splitTokens(seg))
    .filter(toks => toks.length > 0)
}

/** 段内工具 basename（容忍 /usr/bin/nmap、.\nmap.exe）。 */
function binname(toks: string[]): string {
  const t = clean(toks[0]!).replace(/\\/g, '/').toLowerCase()
  const base = t.split('/').at(-1) ?? t
  return base.endsWith('.exe') ? base.slice(0, -4) : base
}

function flagValue(low: string[], i: number): string {
  const t = low[i]!
  if (t.includes('=')) return t.split('=').slice(1).join('=')
  return low[i + 1] ?? ''
}

function hasFlag(low: string[], names: readonly string[]): boolean {
  return low.some(t => names.includes(t.split('=')[0]!))
}

function nmapReason(low: string[]): string | null {
  let full = false
  for (let i = 0; i < low.length; i += 1) {
    const t = low[i]!
    if (t === '-p-') {
      full = true
    } else if (t === '-p' || t === '--ports') {
      if (FULL_RANGE.has(flagValue(low, i))) full = true
    } else if (t.startsWith('-p') && t !== '-p' && FULL_RANGE.has(t.slice(2))) {
      full = true
    } else if (t.startsWith('--ports=') && FULL_RANGE.has(t.split('=').slice(1).join('='))) {
      full = true
    }
    if (full) break
  }
  if (!full) return null
  const throttled =
    low.some(t => /^-t[0-3]$/.test(t))
    || low.some((t, i) => t === '-t' && ['0', '1', '2', '3'].includes(low[i + 1] ?? ''))
    || hasFlag(low, RATE_RULES.nmap!.params.slice(1))
  return throttled ? null : reject('nmap')
}

function masscanReason(low: string[]): string | null {
  const threshold = RATE_RULES.masscan!.threshold!
  for (let i = 0; i < low.length; i += 1) {
    const t = low[i]!
    if (t.split('=')[0] === '--rate') {
      const v = flagValue(low, i)
      if (/^\d+$/.test(v) && Number(v) > threshold) {
        return (
          `限速纪律：masscan --rate ${v} 超过阈值 ${threshold}（p/s）。`
          + RATE_RULES.masscan!.hint
        )
      }
    }
  }
  return null
}

function ffufReason(low: string[]): string | null {
  const throttled =
    hasFlag(low, ['-rate', '-rl'])
    || low.some(t => /^-t\d+$/.test(t))
    || low.some((t, i) => t === '-t' && /^\d+$/.test(low[i + 1] ?? ''))
  return throttled ? null : reject('ffuf')
}

function hydraReason(low: string[]): string | null {
  const hasT =
    low.some(t => /^-t\d+$/.test(t))
    || low.some((t, i) => t === '-t' && /^\d+$/.test(low[i + 1] ?? ''))
  return hasT ? null : reject('hydra')
}

const RULES: Record<string, (low: string[]) => string | null> = {
  nmap: nmapReason,
  masscan: masscanReason,
  ffuf: ffufReason,
  hydra: hydraReason,
}

/**
 * 检查命令中的扫描工具是否带限速参数。返回拒因文案；null=放行。
 */
export function checkRate(cmd: string): string | null {
  for (const toks of segments(cmd)) {
    const rule = RULES[binname(toks)]
    if (rule === undefined) continue
    const low = toks.map(t => clean(t).toLowerCase())
    const reason = rule(low)
    if (reason) return reason
  }
  return null
}
