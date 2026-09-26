/**
 * 工作区路径守卫（移植 core/runtime/pathguard.py）。
 * 静态提取命令文本中的写目标并判定是否逃逸工作区：
 * - 只拦「写」，读不拦（核心诉求=产物归置）；
 * - grep 族 -o*（only-matching）是输出开关不写文件，按命令词跟踪豁免；
 *   nmap 风格 -oG/-oN 写文件仍拦；
 * - 强护栏不是沙箱：变量间接/编码/别名无法静态穷尽，真边界靠容器承载。
 * 纯函数无 IO。
 */
import path from 'node:path'

/** PowerShell 写文件 cmdlet。 */
const PS_WRITE_CMDS = new Set([
  'out-file', 'set-content', 'add-content', 'tee-object', 'new-item',
  'export-csv', 'export-clixml', 'export-pfxcertificate', 'set-variable',
])

/** grep 族（-o 仅匹配不写文件）。 */
const GREP_CMDS = new Set(['grep', 'egrep', 'fgrep', 'zgrep', 'rg', 'ripgrep'])

const ALLOWED_SPECIAL_TARGETS = new Set(['&1', '&2', '/dev/null', 'nul', 'nul.', 'con', '$null'])

/** 按空白切 token，尊重单/双引号（引号保留在 token 里，判定时再剥）。 */
export function splitTokens(cmd: string): string[] {
  const toks: string[] = []
  let cur = ''
  let quote: string | null = null
  for (const ch of cmd) {
    if (quote !== null) {
      cur += ch
      if (ch === quote) quote = null
    } else if (ch === '"' || ch === "'") {
      quote = ch
      cur += ch
    } else if (/\s/.test(ch)) {
      if (cur) {
        toks.push(cur)
        cur = ''
      }
    } else {
      cur += ch
    }
  }
  if (cur) toks.push(cur)
  return toks
}

/** 去空白与引号。 */
export function clean(tok: string): string {
  return tok.trim().replace(/^["']+|["']+$/g, '').trim()
}

/**
 * 重定向目标在引号外首个 shell 分隔符（; | &）处结束。
 * 2>/dev/null; 这类无空格粘连整串是一个 token，不切掉会把 /dev/null; 误判逃逸。
 */
export function trimShellSeparator(target: string): string {
  let quote: string | null = null
  for (let i = 0; i < target.length; i += 1) {
    const ch = target[i]!
    if (quote !== null) {
      if (ch === quote) quote = null
    } else if (ch === '"' || ch === "'") {
      quote = ch
    } else if (ch === ';' || ch === '|' || ch === '&') {
      return target.slice(0, i)
    }
  }
  return target
}

/** POSIX 下 /path 是路径不是 flag；Windows 下 /foo 可能是参数。 */
export function isFlag(tok: string, posix = false): boolean {
  if (posix) return tok.startsWith('-')
  return tok.startsWith('-') || tok.startsWith('/')
}

/** 命令词归一：剥路径段与 .exe 后缀。 */
function cmdWord(low: string): string {
  const w = low.split(/[/\\]/).at(-1) ?? low
  return w.endsWith('.exe') ? w.slice(0, -4) : w
}

/**
 * 提取命令中的写目标 token（原始串，含引号）。启发式：宁可多报不漏报。
 * 管道/分号/逻辑符重置命令边界，grep 族当前段豁免 -o 写判定。
 */
export function scanWriteTargets(cmd: string, posix = false): string[] {
  const targets: string[] = []
  const toks = splitTokens(cmd)
  let curCmd: string | null = null
  let i = 0
  while (i < toks.length) {
    const raw = toks[i]!
    const low = clean(raw).toLowerCase()

    // 命令边界跟踪
    if (low === '|' || low === '||' || low === '&&' || low === ';') {
      curCmd = null
    } else if (raw.includes('|') || raw.includes(';') || raw.includes('&')) {
      const seg = clean(raw.split(/[|;&]/).at(-1) ?? '').toLowerCase()
      curCmd = seg && !isFlag(seg, posix) ? cmdWord(seg) : null
    } else if (curCmd === null && !isFlag(clean(raw), posix)) {
      curCmd = cmdWord(low)
    }

    // ① 重定向 > >> 1> 2>（独立 token 或 ">out.txt" 连写）
    const stripped = raw.replace(/^\d+/, '')
    if (stripped.startsWith('>')) {
      // 与 Python 对齐：任意数量 > 一次剥净
      let rest = trimShellSeparator(stripped.slice(1).replace(/^>+/, ''))
      if (rest.replace(/["']/g, '')) {
        targets.push(rest)
      } else if (i + 1 < toks.length) {
        targets.push(toks[i + 1]!)
      }
      i += 1
      continue
    }

    // ② PowerShell cmdlet / -o / --output / -oG（nmap）；目标在下一非 flag token
    if (PS_WRITE_CMDS.has(low) || low.replace(/^-+/, '').startsWith('o')) {
      // grep 族 -o 豁免；nmap -oG 仍拦
      if (!(GREP_CMDS.has(curCmd ?? '') && low.startsWith('-'))) {
        if (low.includes('=')) {
          const val = clean(raw).split('=').slice(1).join('=')
          if (val) targets.push(val)
        } else if (i + 1 < toks.length) {
          const nxt = clean(toks[i + 1]!)
          if (!isFlag(nxt, posix)) targets.push(toks[i + 1]!)
        }
      }
      i += 1
      continue
    }

    // ③ tee / tee -a
    if (low === 'tee' || low.startsWith('tee ')) {
      let j = i + 1
      while (j < toks.length && isFlag(clean(toks[j]!), posix)) j += 1
      if (j < toks.length) targets.push(toks[j]!)
      i += 1
      continue
    }

    i += 1
  }
  return targets
}

function normcase(p: string, posix: boolean): string {
  if (posix) return path.posix.normalize(p)
  return path.win32.normalize(p).toLowerCase()
}

function inside(child: string, parent: string, posix: boolean): boolean {
  const childN = normcase(child, posix)
  const parentN = normcase(parent, posix)
  if (posix) {
    return childN === parentN || childN.startsWith(parentN.replace(/\/+$/, '') + '/')
  }
  return childN === parentN || childN.startsWith(parentN.replace(/\\+$/, '') + path.win32.sep)
}

/** workspaceEscapes 入参。 */
export interface WorkspaceBound {
  /** cwd（scratch）绝对路径。 */
  scratch: string
  /** 工作区根绝对路径。 */
  workspace: string
  posix?: boolean
}

/**
 * 返回命令中会写到工作区外的目标；空数组=放行。
 * - 绝对路径不在 workspace 内 → 逃逸；
 * - 相对路径按 cwd=scratch 解析，上穿 scratch → 逃逸；
 * - 变量目标（$ / %）无法静态解析 → 放行（TEMP 已被网关重定向）；
 * - 特殊目标（&1 /dev/null NUL $null）放行；~ 一律逃逸。
 */
export function workspaceEscapes(cmd: string, bound: WorkspaceBound): string[] {
  const posix = bound.posix ?? false
  const escapes: string[] = []
  for (const raw of scanWriteTargets(cmd, posix)) {
    const t = clean(raw)
    if (!t || ALLOWED_SPECIAL_TARGETS.has(t)) continue
    if (t.startsWith('-')) continue
    if (t.includes('://')) continue
    if (t.includes('$')) continue
    if (t.includes('%') && !posix) continue
    if (t.startsWith('~')) {
      escapes.push(t)
      continue
    }
    let isAbs = false
    if (/^[A-Za-z]:[\\/]/.test(t)) {
      isAbs = true
    } else if (t.startsWith('\\\\')) {
      isAbs = true
    } else if (t.startsWith('/') && !posix) {
      // Windows PowerShell 下 /foo 解析到当前驱动器根
      escapes.push(t)
      continue
    }
    if (posix && t.startsWith('/')) isAbs = true
    if (isAbs) {
      if (!inside(t, bound.workspace, posix)) escapes.push(t)
      continue
    }
    // 相对路径：按 scratch 解析
    const resolved = posix
      ? path.posix.normalize(path.posix.join(bound.scratch, t))
      : path.win32.normalize(path.win32.join(bound.scratch, t)).toLowerCase()
    if (!inside(resolved, bound.scratch, posix)) escapes.push(t)
  }
  return escapes
}

/**
 * Windows 路径 → WSL 路径（WSL 默认挂载约定）：
 * E:\workspaces\a\scratch → /mnt/e/workspaces/a/scratch
 */
export function toWslPath(p: string): string {
  const s = p.replace(/\\/g, '/')
  return s.length >= 2 && s[1] === ':'
    ? `/mnt/${s[0]!.toLowerCase()}${s.slice(2)}`
    : s
}
