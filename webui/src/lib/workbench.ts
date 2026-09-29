import type { ProjectMeta } from "./types"

// 工作台 profile 推导（DESIGN.md §4.5.5）：config.workbench.profile 显式覆盖优先；
// 否则 research 轨 ⇒ rev-generic 逆向理解工作台。M3 起 capabilities 多选退役
// （创建恒空），旧「track=research && caps 含 binary」判据已死——M3 后新建的
// research 项目全会误落渗透模板（2026-09-29 修复）；场景档显式物化的
// board_view 非 funcs（如 code-audit 档的 findings）→ 交还渗透黑板。
// 逆向内部子类（病毒分析/破解/外挂/逆向开发）以后只加一份 profile 声明，后端不分叉。

export type WorkbenchProfile = "rev-generic" | string | null

export function deriveWorkbenchProfile(meta: ProjectMeta | null | undefined): WorkbenchProfile {
  const cfg = meta?.config as {
    workbench?: { profile?: unknown }
    board_view?: { default?: unknown }
  } | undefined
  const explicit = cfg?.workbench?.profile
  if (typeof explicit === "string" && explicit.trim()) return explicit.trim()
  if (meta?.track !== "research") return null
  const bv = cfg?.board_view?.default
  if (typeof bv === "string" && bv && bv !== "funcs") return null
  return "rev-generic"
}

// 地址一律以小写 0x hex 字符串在 UI 内流转（后端契约：JS Number 无法安全表示 64 位地址）
export function hexAddr(v: number | string): string {
  if (typeof v === "number") return `0x${v.toString(16)}`
  const s = v.trim()
  if (/^0x[0-9a-f]+$/i.test(s)) return `0x${s.slice(2).toLowerCase()}`
  if (/^[0-9]+$/.test(s)) return `0x${BigInt(s).toString(16)}`
  return s
}

// 浏览器搜索框里的地址归一化（"0x401000"/"401000" 都能命中）；非地址返回 null
export function parseAddrInput(q: string): string | null {
  const s = q.trim().toLowerCase()
  if (!s) return null
  try {
    if (s.startsWith("0x")) {
      if (!/^0x[0-9a-f]+$/.test(s)) return null
      return `0x${s.slice(2)}`
    }
    if (/^[0-9a-f]{4,16}$/.test(s)) return `0x${s}`
  } catch { /* BigInt 极端输入 */ }
  return null
}

// finding ↔ 函数相关性：evidence.address 可能是 hex 串或十进制数，归一后比对
export function sameAddr(a: unknown, b: string): boolean {
  if (a == null) return false
  try {
    return hexAddr(String(a)) === b
  } catch {
    return false
  }
}

// 逆向五类结论词汇（evidence.category ↔ 中文标签）
export const FINDING_CATEGORY_LABEL: Record<string, string> = {
  algorithm: "算法",
  protocol: "协议",
  "data-structure": "数据结构",
  mechanism: "机制",
  risk: "风险点",
}
