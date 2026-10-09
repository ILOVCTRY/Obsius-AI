// 活动摘要归并（会话流改造，2026-10-03）：把一轮里的连续「思考 / 命令 / 工具调用」
// 折成一行灰色摘要（`编辑了 2 个文件，执行了一条命令，思考`），对齐 cc-haha 的
// activityGroupModel。纯函数，零 React 依赖——工作台与直播间共用。
//
// 口径：按「工具族」聚合、**保持首次出现顺序**（不是按数量排序）；同族内某些工具
// 按目标去重计数（同一文件读两次算 1 次），与 cc-haha 的 Edit-by-path 同构。

export interface ActivityStep {
  /** 族键：thinking / command / 或工具名 */
  kind: "thinking" | "command" | "tool"
  /** 工具名（kind=tool 时）；kind=command 时为 "command" */
  name: string
  /** 该步的 ms 时间戳（created_at 解析值；缺省 0） */
  ts: number
  /** 后端上报的单步耗时（秒，可选） */
  durationS?: number
  /** 去重键：同族内相同 dedupKey 只计一次（如 read_file 的 path） */
  dedupKey?: string
  /** 失败标记（ok===false），用于摘要行右侧的失败数徽章 */
  failed?: boolean
}

export interface ActivitySegment {
  key: string
  label: string
  icon: string
  count: number
}

// 工具族 → 中文短语（对齐 cc-haha TOOL_VERBS 的「单数/复数」两式）
const VERBS: Record<string, { one: string; many: (n: number) => string; icon: string }> = {
  read_file: { one: "读取了 1 个文件", many: (n) => `读取了 ${n} 个文件`, icon: "📄" },
  search_files: { one: "搜索了代码", many: (n) => `搜索了 ${n} 个模式`, icon: "🔎" },
  strings_search: { one: "搜索了字符串", many: (n) => `搜索了 ${n} 个模式`, icon: "🔤" },
  command: { one: "执行了一条命令", many: (n) => `执行了 ${n} 条命令`, icon: "⌨" },
  skill_open: { one: "打开了 1 份技能手册", many: (n) => `打开了 ${n} 份文档`, icon: "📘" },
  kb_open: { one: "打开了 1 份知识库文档", many: (n) => `打开了 ${n} 份文档`, icon: "📖" },
  kb_search: { one: "检索了知识库", many: (n) => `检索了 ${n} 次`, icon: "🔍" },
  bb_query: { one: "查询了黑板", many: (n) => `查询了 ${n} 次`, icon: "🔎" },
  route_lookup: { one: "查询了路由", many: (n) => `查询了 ${n} 次`, icon: "🧭" },
  func_xrefs: { one: "查询了交叉引用", many: (n) => `查询了 ${n} 次`, icon: "🔗" },
  bb_add_finding: { one: "登记了 1 条发现", many: (n) => `登记了 ${n} 条发现`, icon: "📋" },
  bb_update_finding: { one: "更新了 1 条发现", many: (n) => `更新了 ${n} 条发现`, icon: "📋" },
  bb_add_asset: { one: "登记了 1 个资产", many: (n) => `登记了 ${n} 个资产`, icon: "🎯" },
  propose_pack_edit: { one: "提交了 1 个提案", many: (n) => `提交了 ${n} 个提案`, icon: "📝" },
  decompile: { one: "反编译", many: (n) => `反编译 ${n} 次`, icon: "⚙" },
  list_symbols: { one: "列了符号表", many: (n) => `列了 ${n} 次符号表`, icon: "⚙" },
  thinking: { one: "思考", many: (n) => `思考 ${n} 次`, icon: "✻" },
}

// 前缀归并：browser_* / mcp__* 等整族一句
const PREFIX_VERBS: [string, { one: string; many: (n: number) => string; icon: string }][] = [
  ["browser_", { one: "操作了浏览器", many: (n) => `操作了浏览器 ${n} 次`, icon: "🌐" }],
  ["mcp__", { one: "调用了 MCP 工具", many: (n) => `调用了 MCP 工具 ${n} 次`, icon: "🔌" }],
  ["bb_delete", { one: "清理了黑板条目", many: (n) => `清理了 ${n} 条黑板条目`, icon: "🗑" }],
  ["declare_intent", { one: "声明了意图", many: (n) => `声明了 ${n} 个意图`, icon: "💡" }],
]

function verbOf(name: string) {
  const exact = VERBS[name]
  if (exact) return exact
  for (const [prefix, v] of PREFIX_VERBS) if (name.startsWith(prefix)) return v
  return null
}

/**
 * 步骤序列 → 摘要段（首现顺序）。未登记的工具原样显英文名（不隐藏信息）。
 */
export function buildActivitySegments(steps: ActivityStep[]): ActivitySegment[] {
  const order: string[] = []
  const counts = new Map<string, number>()
  const seen = new Map<string, Set<string>>() // 族 → 已计过的 dedupKey
  for (const s of steps) {
    const key = s.kind === "thinking" ? "thinking" : s.name
    if (!counts.has(key)) { order.push(key); counts.set(key, 0); seen.set(key, new Set()) }
    const dedup = seen.get(key)!
    if (s.dedupKey) {
      if (dedup.has(s.dedupKey)) continue
      dedup.add(s.dedupKey)
    }
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return order.map((key) => {
    const n = counts.get(key) ?? 0
    const v = verbOf(key)
    if (v) return { key, label: n === 1 ? v.one : v.many(n), icon: v.icon, count: n }
    // 未登记工具：原样显名（单数不加后缀，复数补 (N)）——与 cc-haha 同策略
    return { key, label: n === 1 ? key : `${key} (${n})`, icon: "🛠", count: n }
  })
}

/**
 * 该组耗时（毫秒）。优先「首末步时间差」（与 cc-haha 的 transcript 口径一致，
 * 含等待审批等真实墙钟）；全部无时间戳时退化 Σ duration_s；再不行返回 undefined。
 */
export function activityDurationMs(steps: ActivityStep[]): number | undefined {
  const ts = steps.map((s) => s.ts).filter((t) => t > 0)
  if (ts.length >= 2) {
    const span = Math.max(...ts) - Math.min(...ts)
    if (span > 0) return span
  }
  const sumS = steps.reduce((a, s) => a + (s.durationS ?? 0), 0)
  return sumS > 0 ? sumS * 1000 : undefined
}

/** 耗时格式化（对齐 cc-haha formatDuration）：ms / 9.2s / 53s / 7m51s / 1h2m */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  const total = Math.round(ms / 1000)
  if (total < 10) return `${(ms / 1000).toFixed(1)}s`
  if (total < 60) return `${total}s`
  const mins = Math.floor(total / 60)
  if (mins < 60) return `${mins}m${total % 60}s`
  return `${Math.floor(mins / 60)}h${mins % 60}m`
}

/** 空闲时长（浏览器卡片「多久未操作」）：一律秒（如 3s / 180s / 3725s）。 */
export function formatIdle(ms: number): string {
  return `${Math.max(0, Math.floor(ms / 1000))}s`
}

// ---------- 思考预览（对齐 cc-haha ThinkingBlock.thinkingPreview） ----------
// 折叠行只显一行：剥掉 markdown 噪声（#/-/>/序号），流式中跟随尾部（此刻在
// 想什么最有用），稳定后回到首行——除非首行是「XXX：」这种空标题，就显其下正文。

const THINKING_PREVIEW_MAX = 160
/** 短且以冒号收尾的首行 = 后文的标题（显它等于什么都没说）。 */
const THINKING_OPENER_MAX = 24

/** 剥 markdown 噪声后逐行；空行与 `---` 丢弃。 */
export function cleanThinkingLines(content: string): string[] {
  const lines: string[] = []
  for (const raw of (content ?? "").split("\n")) {
    const line = raw.trim()
      .replace(/^#{1,6}\s+/, "")
      .replace(/^[-*+]\s+/, "")
      .replace(/^>\s*/, "")
      .replace(/^\d+\.\s+/, "")
      .trim()
    if (!line || line === "---") continue
    lines.push(line)
  }
  return lines
}

/** 折叠行的一行摘要。`streaming`=仍在下笔，跟随最后一行。 */
export function thinkingPreview(content: string, opts: { streaming?: boolean } = {}): string {
  const lines = cleanThinkingLines(content)
  if (!lines.length) return ""
  const picked = opts.streaming ? lines[lines.length - 1]! : pickSettledPreviewLine(lines)
  return picked.length > THINKING_PREVIEW_MAX ? `${picked.slice(0, THINKING_PREVIEW_MAX)}…` : picked
}

function pickSettledPreviewLine(lines: string[]): string {
  const first = lines[0]!
  const bareHeading = first.length <= THINKING_OPENER_MAX && /[:：]$/.test(first)
  return bareHeading ? (lines[1] ?? first) : first
}
