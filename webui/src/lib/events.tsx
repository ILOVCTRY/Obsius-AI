import type { ReactNode } from "react"

// 事件分层：工具输出默认折叠，思考/决策默认展开（DESIGN.md §12 直播间实现要求）

export interface EventStyle {
  label: string
  className: string
  defaultOpen: boolean
}

export function eventStyle(kind: string, payload?: Record<string, unknown>): EventStyle {
  if (kind.startsWith("task.")) {
    const map: Record<string, EventStyle> = {
      "task.published": { label: "📋 发布任务", className: "text-primary", defaultOpen: true },
      "task.claimed": { label: "🔧 认领任务", className: "text-muted-foreground", defaultOpen: false },
      "task.done": { label: "✅ 任务完成", className: "text-primary", defaultOpen: true },
      "task.failed": { label: "❌ 任务失败", className: "text-(--status-error)", defaultOpen: false },
      "task.updated": { label: "✏️ 编辑任务", className: "text-muted-foreground", defaultOpen: true },
      "task.reopened": { label: "♻️ 放回待认领", className: "text-primary", defaultOpen: true },
      "task.cancelled": { label: "🚫 任务已取消", className: "text-muted-foreground", defaultOpen: true },
      "task.deleted": { label: "🗑 删除任务", className: "text-muted-foreground", defaultOpen: true },
      "task.lease_expired": { label: "⏰ 租约过期", className: "text-(--status-approval)", defaultOpen: true },
      // A2 先规划后动手
      "task.plan_set": { label: "📐 制定计划", className: "text-(--viz-sev-low)", defaultOpen: true },
      "task.plan_revised": { label: "📐 修订计划", className: "text-(--viz-sev-low)", defaultOpen: true },
      "task.step": { label: "▦ 计划步进", className: "text-muted-foreground", defaultOpen: false },
      // ⑤ 完成对账硬拦：complete 被拒，列出未收口条目
      "task.reconcile_blocked": { label: "☑ 完成对账未收口", className: "text-(--viz-sev-medium)", defaultOpen: true },
    }
    return map[kind] ?? { label: `task`, className: "text-muted-foreground", defaultOpen: false }
  }
  if (kind === "command") return { label: "⚡ 执行命令", className: "text-foreground/80", defaultOpen: false }
  if (kind === "command.result") return { label: "↳ 命令输出", className: "text-muted-foreground", defaultOpen: false }
  // 独立验证 M1：验证器对 verify 验收条目的判定回执（未过默认展开，脱敏摘要）
  if (kind === "verify.result")
    return payload?.passed
      ? { label: "🔬 独立验证通过", className: "text-(--viz-success)", defaultOpen: false }
      : { label: "🔬 独立验证未过", className: "text-(--status-error)", defaultOpen: true }
  if (kind === "tool.call") return { label: "🛠 工具调用", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "audit.deny") return { label: "🛡 网关拒绝", className: "text-(--status-approval)", defaultOpen: true }
  if (kind === "finding.new" || kind === "finding.merged")
    return { label: "🔍 新发现", className: "text-primary", defaultOpen: true }
  if (kind === "finding.updated")
    return { label: "🔍 发现更新", className: "text-primary", defaultOpen: true }
  if (kind === "finding.retracted")
    return { label: "🚫 发现撤回", className: "text-(--status-error)", defaultOpen: true }
  if (kind === "message.inbox") {
    // A4：信息式增补（🔵）与撤回强提醒（⚠ 三选一）严格分样式；E8 人类引导直达
    if (payload?.kind === "human_note")
      return { label: "💬 人类引导", className: "text-primary", defaultOpen: true }
    if (payload?.kind === "finding_update")
      return { label: "🔵 发现增补", className: "text-(--viz-sev-low)", defaultOpen: true }
    if (payload?.kind === "basis_stale")
      return { label: "⚠ 依据撤回", className: "text-(--viz-sev-medium)", defaultOpen: true }
    if (payload?.kind === "authorization_result")
      return { label: "✅ 授权申请已批准", className: "text-(--viz-success)", defaultOpen: true }
    if (payload?.kind === "approval_rejected")
      return { label: "❌ 申请被拒绝", className: "text-(--status-error)", defaultOpen: true }
    return { label: "🔔 会话私信", className: "text-(--status-approval)", defaultOpen: true }
  }
  if (kind === "agent.chat")
    // Agent 回复（2026-09-19）：空闲对话轮回复 + 任务循环叙述行（工具调用间「做了什么」），
    // 与 💭 思考同居「思考」tab；渲染走 EventRow 专属 prose 行（正文即行，本表 label 仅供 tab/筛选）
    return { label: "🤖 Agent 回复", className: "text-primary", defaultOpen: true }
  if (kind === "agent.chat.delta")
    // 回复流式增量行（2026-09-20 对话化）：直播中在对话轮气泡内滚动，终稿 agent.chat
    // 到达后该组跳过/后端清剪——同 llm.thinking.delta 先例；本样式仅供孤儿 delta 兜底行
    return { label: "🤖 回复中…", className: "text-primary", defaultOpen: true }
  if (kind === "advisor.intervention")
    // 策略顾问发言（2026-09-20）：卡壳干预的正文落事件流，人工可判断顾问说了什么、
    // 建议是否合理；渲染同样走 prose 行（正文即行），同居「决策」tab
    return { label: "🧭 策略顾问", className: "text-(--viz-sev-medium)", defaultOpen: true }
  // D9 活跃探索静默延长（2026-09-24）：未叫顾问、仅重置观察窗——默认展开让人工
  // 看得见为什么没打断；file.read 是其取材源，默认折叠
  if (kind === "agent.stuck_extend")
    return { label: "⏱ 活跃探索，观察窗延长", className: "text-(--viz-success)", defaultOpen: true }
  if (kind === "file.read")
    return { label: "📄 读取文件", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "file.search")
    return { label: "🔎 内容检索", className: "text-muted-foreground", defaultOpen: false }
  // 计划闸教练链（2026-09-24）：强提示默认展开、挂起/熔断红色默认展开
  if (kind === "agent.plan_nudge")
    return { label: "📋 请先写计划", className: "text-(--viz-sev-medium)", defaultOpen: true }
  if (kind === "agent.plan_gate_block")
    return { label: "🚫 拒写计划，挂起", className: "text-(--status-error)", defaultOpen: true }
  if (kind === "agent.reject_breaker")
    return { label: "🚫 拒绝熔断挂起", className: "text-(--status-error)", defaultOpen: true }
  if (kind === "task.basis_stale_done")
    return { label: "⚠ 推翻依据下完成", className: "text-(--viz-sev-medium)", defaultOpen: true }
  if (kind === "binary.triaged")
    return { label: "🧊 样本分诊完成", className: "text-primary", defaultOpen: true }
  if (kind === "binary.triage_failed")
    return { label: "⚠ 样本分诊失败", className: "text-(--status-error)", defaultOpen: true }
  if (kind === "binary.annotated")
    return { label: "⬆ 写回 IDA", className: "text-primary", defaultOpen: false }
  if (kind === "binary.names_pulled")
    return { label: "⬇ IDA 改名同步", className: "text-primary", defaultOpen: false }
  if (kind === "func.updated")
    return { label: "ƒ 函数更新", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "asset.new") return { label: "📦 资产", className: "text-muted-foreground", defaultOpen: false }
  // 资产批量导入汇总（cyberspace-mapping M1）：整批单条，摘要行即全量信息
  if (kind === "asset.imported")
    return { label: "📥 资产导入", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "asset.status_changed") return { label: "🔄 资产状态", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "func.upsert") return { label: "ƒ 函数分析", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "llm.thinking")
    return { label: "💭 思考", className: "text-muted-foreground italic", defaultOpen: false }
  if (kind === "llm.switched") return { label: "🔀 切换模型", className: "text-primary", defaultOpen: true }
  if (kind === "llm.usage")
    return { label: "∑ token", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "llm.error")
    return { label: "🚨 LLM 调用失败", className: "text-(--status-error)", defaultOpen: true }
  if (kind === "budget.soft_warning")
    return { label: "🟡 预算预警", className: "text-(--viz-sev-medium)", defaultOpen: true }
  // E8 步数预算：自助/人工增补与耗尽自动暂停
  if (kind === "step.budget_extended")
    return { label: "⏳ 步数增补", className: "text-(--viz-sev-medium)", defaultOpen: true }
  if (kind === "session.budget_paused")
    return { label: "⏸ 步数预算用尽", className: "text-(--viz-sev-medium)", defaultOpen: true }
  if (kind === "kb.open") return { label: "📖 打开知识库", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "kb.search") return { label: "🔍 搜索知识库", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "skill.open") return { label: "📖 打开技能正文", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "skill.routed") return { label: "🎯 技能路由", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "llm.thinking.delta")
    // 思考流式增量行（2026-09-19；E3 折叠 2026-09-22）：原直播中默认展开全文——
    // 长思考逐帧刷屏淹没工具结果（视觉权重倒挂）。改默认折叠单行（点开可看全文），
    // 终稿 llm.thinking 到达后该组跳过/后端清剪——本样式只活在直播窗口内
    return { label: "💭 思考中…", className: "text-muted-foreground italic", defaultOpen: false }
  if (kind === "llm.compact")
    // G3 上下文摘要压缩（2026-09-19）：N 条旧历史压成摘要；展开看前后规模
    return { label: "🧹 上下文压缩", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "proposal.created") return { label: "📝 变更提案", className: "text-(--status-approval)", defaultOpen: true }
  if (kind === "proposal.applied") return { label: "✅ 提案应用", className: "text-primary", defaultOpen: true }
  if (kind === "proposal.rejected") return { label: "⊘ 提案拒绝", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "session.spawned") return { label: "🟢 会话开窗", className: "text-primary", defaultOpen: true }
  // 会话中心化（2026-09-25）：编排器委派 + 中途换人留痕
  if (kind === "delegation.posted")
    return { label: "🧭 编排器委派", className: "text-primary", defaultOpen: true }
  if (kind === "session.persona_switched")
    return { label: "🔄 切换智能体", className: "text-primary", defaultOpen: true }
  if (kind === "session.work_state") return { label: "⚙ worker 状态", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "session.paused") return { label: "⏸ 已暂停", className: "text-(--status-paused)", defaultOpen: true }
  if (kind === "session.resumed") return { label: "▶ 已恢复", className: "text-primary", defaultOpen: true }
  if (kind === "session.aborted") return { label: "⛔ 人工中断", className: "text-(--status-error)", defaultOpen: false }
  if (kind === "session.finished") return { label: "⚪ 会话收尾", className: "text-muted-foreground", defaultOpen: true }
  if (kind === "approval.requested") return { label: "🔔 请求审批", className: "text-(--status-approval)", defaultOpen: true }
  if (kind.startsWith("approval.")) return { label: "🔔 审批决定", className: "text-(--status-approval)", defaultOpen: true }
  if (kind === "orch.proposed") return { label: "💡 编排提案", className: "text-(--viz-sev-medium)", defaultOpen: true }
  // auto-attack（2026-09-28）：研判完成（主渲染走 OrchChatPane 研判卡，本样式供审计兜底）
  if (kind === "orch.auto_attack.analyzed")
    return { label: "🚀 自动渗透研判", className: "text-primary", defaultOpen: true }
  // 对话化编排器（M1/M2，§6.4）：对话流主渲染走 OrchChatPane 气泡（本样式供
  // 审计抽屉平铺兜底）；goal 确认/清空是人类决策留痕
  if (kind === "orch.chat") return { label: "💬 编排对话", className: "text-primary", defaultOpen: false }
  if (kind === "goal.confirm") return { label: "🎯 阶段目标确认", className: "text-(--viz-sev-medium)", defaultOpen: true }
  if (kind === "goal.clear") return { label: "🎯 阶段目标清空", className: "text-muted-foreground", defaultOpen: false }
  // 分阶段工作流（pentest M4）：阶段流转留痕与过门开门事件（门开启摘要走 payload.summary 通用分支）
  if (kind === "phase.changed") return { label: "🔄 阶段流转", className: "text-primary", defaultOpen: true }
  if (kind === "phase.gate_open") return { label: "🚪 渗透门开启", className: "text-(--viz-sev-medium)", defaultOpen: true }
  if (kind === "orch.tick.started")
    return { label: "⚙ 编排启动", className: "text-primary", defaultOpen: false }
  if (kind === "mission.derive")
    return { label: "🎯 自动派生判定", className: "text-primary", defaultOpen: false }
  if (kind === "mission.derive.result")
    return { label: "🎯 派生结果", className: "text-primary", defaultOpen: true }
  if (kind === "orch.chain_started") return { label: "⛓🤖 L2 自动链启动", className: "text-primary", defaultOpen: true }
  // 停止原因（含急停/预算/异常）由 eventSummary 中文行呈现
  if (kind === "orch.chain_stopped") return { label: "⛓⏹ L2 自动链停止", className: "text-(--viz-sev-medium)", defaultOpen: true }
  if (kind === "orch.replan_priorities") return { label: "🔀 优先级重排", className: "text-primary", defaultOpen: false }
  if (kind === "project.digest") return { label: "📌 编排简报", className: "text-primary", defaultOpen: true }
  if (kind === "chain.created") return { label: "🔗 新建攻击链", className: "text-(--viz-chain)", defaultOpen: true }
  if (kind === "chain.updated") return { label: "🔗 攻击链更新", className: "text-(--viz-chain)", defaultOpen: false }
  if (kind === "chain.deleted") return { label: "🔗 删除攻击链", className: "text-muted-foreground", defaultOpen: true }
  if (kind === "chain.link_added") return { label: "🔗 链上挂节点", className: "text-(--viz-chain)", defaultOpen: true }
  if (kind === "chain.link_removed") return { label: "🔗 链上移节点", className: "text-muted-foreground", defaultOpen: false }
  // 蓝图（R4 逆向开发管线）
  if (kind === "blueprint.created") return { label: "📐 新建蓝图", className: "text-primary", defaultOpen: true }
  if (kind === "blueprint.updated") return { label: "📐 蓝图更新", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "blueprint.status_changed") return { label: "📐 蓝图状态流转", className: "text-primary", defaultOpen: true }
  return { label: kind, className: "text-muted-foreground", defaultOpen: false }
}

// payload 摘要行（每个 kind 取最有信息量的字段）
// L2 链停止原因中文映射（批 5，§6.8）
const CHAIN_STOP_REASON: Record<string, string> = {
  converged: "本轮收敛完成",
  no_sessions: "无可用执行窗",
  max_chain_ticks: "链轮数预算到顶",
  paused: "项目已暂停",
  level_changed: "自主档位被调整",
  budget_blocked: "预算硬闸拦截",
  restart: "服务重启急停（需手动恢复）",
  error: "自动编排异常",
}

// tool.call 摘要用：工具名 → 中文图标+名（未登记的原样显工具名）；
// TOOL_ARG_KEY 取该工具最有定位价值的一个参数字段（缺省取 args 第一个字符串值）
const TOOL_LABELS: Record<string, string> = {
  kb_open: "📖 知识库模块", kb_search: "🔍 知识库检索",
  bb_add_asset: "🎯 登记资产", bb_asset_status: "🎯 资产状态",
  bb_add_finding: "📋 新增发现", bb_update_finding: "📋 更新发现",
  bb_delete_finding: "📋 删除发现", bb_add_artifact: "📦 落产物",
  bb_query: "🔎 查黑板", bb_upsert_func: "⚙ 函数知识",
  propose_pack_edit: "📝 变更提案", decompile: "⚙ 反编译",
  list_symbols: "⚙ 符号表", task_plan: "📐 写计划",
  task_step: "▦ 计划步进", publish_task: "📨 派发任务",
  complete_task: "✅ 完成任务", fail_task: "✗ 任务失败",
  finish: "🏁 收尾会话", request_steps: "⏳ 申请增补步数",
  browser_navigate: "🌐 打开网页", browser_click: "🖱 点击",
  browser_type: "⌨ 输入", browser_screenshot: "📸 截图",
  browser_content: "📄 取页面内容", browser_back: "↩ 后退",
  // 2026-09-28 补登记（此前未注册的工具降级成裸英文名、参数兜底空白）：
  read_file: "📄 读文件", search_files: "🔍 文件检索",
  strings_search: "🔤 字符串检索", func_xrefs: "🔗 交叉引用",
  skill_open: "📘 打开技能", route_lookup: "🧭 路由查询",
  declare_intent: "💡 声明意图", close_intent: "🏁 收尾意图",
  reopen_intent: "↩ 重开意图", task_reconcile: "✓ 任务对账",
  request_authorization: "🙏 申请授权",
  run_cmd: "⌨ 命令",  // 仅计划闸/越界拒绝时落审计（正常执行走 command 配对事件）
}
const TOOL_ARG_KEY: Record<string, string> = {
  kb_open: "module", kb_search: "query", browser_navigate: "url",
  bb_add_finding: "title", bb_update_finding: "finding_id",
  bb_delete_finding: "finding_id", bb_asset_status: "asset_id",
  bb_add_artifact: "filename", publish_task: "objective",
  decompile: "binary", list_symbols: "binary", browser_click: "selector",
  browser_type: "selector", bb_upsert_func: "func_id",
  propose_pack_edit: "target", task_step: "step_id",
}

// severity 徽章共用（live-stream-ux C2，2026-09-23）：色值从 Blackboard.tsx 提升，
// 发现页签与事件流同源；中文标签事件流专用
export const SEVERITY_COLOR: Record<string, string> = {
  critical: "text-(--viz-sev-critical)",
  high: "text-(--viz-sev-high)",
  medium: "text-(--viz-sev-medium)",
  low: "text-(--viz-sev-low)",
  info: "text-(--viz-sev-info)",
}
const SEVERITY_LABEL: Record<string, string> = {
  critical: "严重", high: "高危", medium: "中危", low: "低危", info: "信息",
}

// 摘要长度分级（A2）：默认 60，长文案字段（note/result_note/summary）200——
// 与后端 _truncate_args 上限对齐，展开体可看全
const LEN_SHORT = 60
const LEN_LONG = 200
const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n) + "…" : s)

// 摘要上下文（A3 资产反查）：LiveRoom 自建 assets 映射经 EventRow 传入；
// 反查不到显 id 尾 6 位，不阻塞渲染
export interface SummaryCtx {
  assetName?: (id: string) => string | undefined
}
const assetLabel = (id: unknown, ctx?: SummaryCtx): string => {
  if (typeof id !== "string" || !id) return ""
  return ctx?.assetName?.(id) ?? `…${id.slice(-6)}`
}

// per-tool 语义摘要（A1，live-stream-ux 2026-09-23；2026-09-28 补查询类工具）：
// 查询类摘要必须带出「查了什么」——检索词/目标值/过滤条件，否则行只剩工具图标无从判断。
// 入参 a=截断后的 args（_truncate_args 只截长字符串值，结构不变）
const TOOL_SUMMARIZERS: Record<string, (a: Record<string, unknown>, ctx?: SummaryCtx) => ReactNode> = {
  bb_asset_status: (a, ctx) => {
    const label = assetLabel(a.asset_id, ctx) || String(a.asset_id ?? "")
    const note = typeof a.note === "string" && a.note ? `（${clip(a.note, LEN_SHORT)}）` : ""
    return `资产状态 · ${label} → ${String(a.status ?? "?")}${note}`
  },
  bb_query: (a, ctx) => {
    const filters: string[] = []
    // site 查询的「查哪个站」在 asset（值或 id）；events 在 kinds/session_id；
    // func 在 binary_sha256/address——全部带出，否则「查 site」不知道查的是谁
    const target = assetLabel(a.target_asset_id, ctx)
    if (target) filters.push(`目标 ${target}`)
    const site = typeof a.asset === "string" && a.asset ? a.asset : ""
    if (site) filters.push(`站点 ${site}`)
    if (Array.isArray(a.kinds) && a.kinds.length) filters.push(`类型 ${(a.kinds as unknown[]).slice(0, 3).join(",")}`)
    if (typeof a.session_id === "string" && a.session_id) filters.push(`会话 …${a.session_id.slice(-6)}`)
    if (typeof a.binary_sha256 === "string" && a.binary_sha256) filters.push(`…${a.binary_sha256.slice(-8)}`)
    if (a.address !== undefined && a.address !== null) filters.push(`@ ${String(a.address)}`)
    for (const k of ["type", "status", "risk_tag", "category", "tag", "min_severity"] as const) {
      if (typeof a[k] === "string" && a[k]) filters.push(`${k} ${a[k]}`)
    }
    return `查 ${String(a.what ?? "?")}${filters.length ? `（${filters.join(" · ")}）` : "（无过滤）"}`
  },
  search_files: (a) => {
    const pat = typeof a.pattern === "string" ? clip(a.pattern, LEN_SHORT) : ""
    const path = typeof a.path === "string" && a.path ? ` @ ${a.path}` : ""
    return `搜 ${pat ? `“${pat}”` : "（无关键词）"}${path}`
  },
  strings_search: (a) => {
    const pat = typeof a.pattern === "string" ? clip(a.pattern, LEN_SHORT) : ""
    const bin = typeof a.binary === "string" ? ` @ …${a.binary.split("/").pop()}` : ""
    return `搜 ${pat ? `“${pat}”` : "全量"}${bin}`
  },
  func_xrefs: (a) => {
    const bin = typeof a.binary === "string" ? `…${a.binary.split("/").pop()}` : ""
    const who = typeof a.name === "string" && a.name ? a.name
      : a.address !== undefined && a.address !== null ? `@ ${String(a.address)}` : ""
    return `${bin}${who ? ` · ${who}` : ""}`
  },
  read_file: (a) => {
    const p = typeof a.path === "string" ? a.path : ""
    const off = typeof a.offset === "number" && a.offset ? ` (offset ${a.offset})` : ""
    return `${p}${off}`
  },
  skill_open: (a) => String(a.name ?? ""),
  route_lookup: (a) => {
    const q = typeof a.query === "string" ? clip(a.query, LEN_SHORT) : ""
    return q ? `查 “${q}”` : ""
  },
  declare_intent: (a) => clip(String(a.statement ?? ""), LEN_LONG),
  close_intent: (a) => {
    const id = typeof a.intent_id === "string" ? `…${a.intent_id.slice(-6)}` : ""
    const outcome = typeof a.outcome === "string" ? a.outcome : ""
    return `${outcome}${id ? ` · ${id}` : ""}`
  },
  reopen_intent: (a) => {
    const id = typeof a.intent_id === "string" ? `…${a.intent_id.slice(-6)}` : ""
    const note = typeof a.note === "string" && a.note ? `（${clip(a.note, LEN_SHORT)}）` : ""
    return `${id}${note}`
  },
  task_reconcile: (a) => {
    const item = typeof a.item_id === "number" ? `#${a.item_id}` : ""
    const state = typeof a.state === "string" ? a.state : ""
    const note = typeof a.note === "string" && a.note ? ` ${clip(a.note, LEN_SHORT)}` : ""
    return `${item}${state ? ` → ${state}` : ""}${note}`
  },
  task_plan: (a) => {
    const n = Array.isArray(a.steps) ? a.steps.length : 0
    const rev = typeof a.rev_reason === "string" && a.rev_reason ? `（修订：${clip(a.rev_reason, LEN_SHORT)}）` : ""
    return `${n} 步${rev}`
  },
  request_authorization: (a) => {
    const kind = String(a.kind ?? "")
    const why = typeof a.reason === "string" && a.reason ? ` ${clip(a.reason, LEN_SHORT)}` : ""
    return `${kind}${why}`
  },
  run_cmd: (a) => clip(String(a.cmd ?? ""), LEN_SHORT),  // gated 拒绝审计行
  bb_add_asset: (a) =>
    `${String(a.value ?? "")}（${String(a.type ?? "auto")}）`,
  complete_task: (a) => clip(String(a.result_note ?? ""), LEN_LONG),
  fail_task: (a) => {
    const why = a.blocked_reason === "awaiting_human" ? "（待人类处理）" : ""
    return `${clip(String(a.result_note ?? ""), LEN_LONG)}${why}`
  },
  finish: (a) => clip(String(a.summary ?? ""), LEN_LONG),
  bb_add_finding: (a) => {
    const sev = String(a.severity ?? "")
    const badge = sev ? (
      <span className={SEVERITY_COLOR[sev] ?? ""}>[{SEVERITY_LABEL[sev] ?? sev}]</span>
    ) : null
    return (
      <>
        {badge}
        {typeof a.title === "string" && a.title ? ` ${clip(a.title, LEN_SHORT)}` : ""}
        {typeof a.vuln_class === "string" && a.vuln_class ? `（${a.vuln_class}）` : ""}
      </>
    )
  },
}

// B3（live-stream-ux）：task.done/task.failed 事件行专属摘要——「动作」与「结果」
// 分行一眼可辨；blocked_reason=awaiting_human 标注「待人类处理」。
// M2（orchestrator-coordination-fusion）：委派回执富化——payload.receipt 存在时
// 追加产出计数（🏷N发现 · 🔧N产物），人类在事件流一眼看清委托成果。
function receiptSuffix(payload: Record<string, unknown>): string {
  const r = payload.receipt
  if (!r || typeof r !== "object") return ""
  const rec = r as Record<string, unknown>
  const nf = Array.isArray(rec.findings) ? rec.findings.length : 0
  const na = Array.isArray(rec.artifacts) ? rec.artifacts.length : 0
  const parts: string[] = []
  if (nf) parts.push(`🏷${nf}发现`)
  if (na) parts.push(`🔧${na}产物`)
  return parts.length ? ` · ${parts.join(" · ")}` : ""
}
function taskDoneSummary(payload: Record<string, unknown>): ReactNode {
  const note = typeof payload.note === "string" ? payload.note : ""
  const s = `${clip(note, LEN_LONG)}${receiptSuffix(payload)}`
  return s || undefined
}
function taskFailedSummary(payload: Record<string, unknown>): ReactNode {
  const note = typeof payload.note === "string" ? clip(payload.note, LEN_LONG) : ""
  const why = payload.blocked_reason === "awaiting_human" ? "（待人类处理）" : ""
  const s = `${note}${why}${receiptSuffix(payload)}`
  return s || undefined
}

export function eventSummary(payload: Record<string, unknown>,
                              roleNames?: Record<string, string>,
                              ctx?: SummaryCtx,
                              kind?: string): ReactNode {
  // B3（live-stream-ux）：task.done/task.failed 专属摘要——与收尾 tool.call 行
  // 图标呼应，一眼分清「动作」与「结果」；note 空（纯 awaiting_human）也有标注
  if (kind === "task.done") {
    const s = taskDoneSummary(payload)
    if (s !== undefined) return s
  }
  if (kind === "task.failed") {
    const s = taskFailedSummary(payload)
    if (s !== undefined) return s
  }
  // 资产批量导入（cyberspace-mapping M1）：N 行 → 新建/合并/跳过/失败 单行摘要
  if (kind === "asset.imported") {
    const skipped = typeof payload.skipped === "number" && payload.skipped > 0
      ? ` · ${payload.skipped} 跳过` : ""
    const failed = typeof payload.failed_count === "number" && payload.failed_count > 0
      ? ` · ${payload.failed_count} 失败` : ""
    return (
      <span className="font-mono text-xs">
        {String(payload.source ?? "unknown")} 导入 {String(payload.total ?? 0)} 行 ·{" "}
        {String(payload.created ?? 0)} 新建 / {String(payload.merged ?? 0)} 合并{skipped}{failed}
      </span>
    )
  }
  // 独立验证 M1（independent-verification-audit）：验证器判定回执摘要
  // 会话中心化（2026-09-25）：委派卡摘要 + 换人摘要
  if (kind === "delegation.posted") {
    const role = typeof payload.role === "string" && payload.role ? `/${payload.role}` : ""
    return (
      <span className="font-mono text-xs">
        🧭 {String(payload.objective ?? "")}（{String(payload.task_type ?? "generic")}
        {role}，{payload.new_window ? "新窗" : "复用窗"}）
      </span>
    )
  }
  const PERSONA_REASON: Record<string, string> = {
    "manual-switch": "人工换人", "role-change": "委托改角色",
  }
  if (kind === "session.persona_switched") {
    const why = typeof payload.reason === "string"
      ? ` · ${PERSONA_REASON[payload.reason] ?? payload.reason}` : ""
    return (
      <span className="font-mono text-xs">
        {String(payload.from ?? "?")} → {String(payload.to ?? "?")}{why}
      </span>
    )
  }
  if (kind === "verify.result") {
    return (
      <span className="font-mono text-xs">
        [{String(payload.strategy ?? "?")}] 条目#{String(payload.item_id ?? "?")}{" "}
        {payload.passed ? "✅ 通过" : "❌ 未过"}
        {typeof payload.evidence_head === "string" ? ` · ${payload.evidence_head}` : ""}
      </span>
    )
  }
  // D9 活跃探索延长（2026-09-24）：摘要呈现第几次延长 + 命中信号
  if (kind === "agent.stuck_extend") {
    const sig = (payload.signals ?? {}) as Record<string, unknown>
    const parts = [`第 ${String(payload.extension ?? "?")} 次`]
    if (Array.isArray(sig.new_files) && sig.new_files.length)
      parts.push(`新文件 ×${sig.new_files.length}`)
    if (typeof sig.unique_commands === "number")
      parts.push(`命令 ${String(sig.command_count)}（${sig.unique_commands} 种）`)
    return <span className="font-mono text-xs">{parts.join(" · ")}</span>
  }
  if (kind === "file.read")
    return <span className="font-mono text-xs">{String(payload.path ?? "")}</span>
  // roleNames：role id → 中文显示名映射（LiveRoom 由 GET /api/projects/{pid}/roles 建）；
  // 缺省兜底显 role id。
  // 批 6 L0 提案：{op, args}，按动作给一行人话摘要（行内「采纳」按钮在 LiveRoom 挂）
  if (payload.op === "publish_task" && payload.args && typeof payload.args === "object") {
    const a = payload.args as Record<string, unknown>
    if (typeof a.objective === "string") {
      return (
        <span className="font-mono text-xs">
          提议发任务 · {a.objective}
          {typeof a.task_type === "string" ? `（${a.task_type}/${String(a.noise_budget ?? "")}）` : ""}
        </span>
      )
    }
  }
  if (payload.op === "spawn_session" && payload.args && typeof payload.args === "object") {
    const a = payload.args as Record<string, unknown>
    if (typeof a.role === "string") {
      return (
        <span className="font-mono text-xs">
          提议开窗 · {roleNames?.[a.role] ?? a.role}
          {typeof a.reason === "string" && a.reason ? `（${a.reason}）` : ""}
        </span>
      )
    }
  }
  // orch.chain_stopped：reason 为八值枚举之一 + chain_ticks
  if (typeof payload.reason === "string" && payload.reason in CHAIN_STOP_REASON
      && typeof payload.chain_ticks === "number") {
    return (
      <span className="font-mono text-xs">
        {CHAIN_STOP_REASON[payload.reason]} · 自动 {String(payload.chain_ticks)} 轮
      </span>
    )
  }
  // orch.chain_started
  if (payload.reason === "manual_tick") return "人手编排一轮，自动链启动"
  if (payload.reason === "manual_recovery") return "急停后手动恢复，链预算清零重算"
  // orch.replan_priorities：updated=[{task_id,old,new}] + skipped_n
  if (Array.isArray(payload.updated) && typeof payload.skipped_n === "number") {
    const updated = payload.updated as { task_id: string; old: number; new: number }[]
    const detail = updated
      .map((u) => `${u.task_id.slice(-6)} P${u.old}→P${u.new}`).join("，")
    return (
      <span className="font-mono text-xs">
        {updated.length} 条改级{detail ? ` · ${detail}` : ""} · 跳过 {payload.skipped_n}
      </span>
    )
  }
  if (typeof payload.thinking === "string") {
    // 思考行：折叠时显示首行摘要（Claude Code 式），展开看全文
    const line = (payload.thinking.split("\n").find((l) => l.trim()) ?? "").trim()
    return line.length > 80 ? line.slice(0, 80) + "…" : line || undefined
  }
  // agent.chat（空闲对话轮回复，2026-09-19）：正常渲染走 EventRow 专属 prose 行不经过这里；
  // 本摘要仅作无 text 载荷回退通用 JSON 行时的标题
  if (typeof payload.text === "string" && typeof payload.session_id === "string"
      && payload.thinking === undefined && payload.step_id === undefined) {
    const line = (payload.text.split("\n").find((l) => l.trim()) ?? "").trim()
    return line.length > 80 ? line.slice(0, 80) + "…" : line || undefined
  }
  // kb 读取摘要（kb.open={source,module,path} / kb.search={query,hits}，与 skill.routed
  // 同居「路由」tab——2026-09-19 定稿）
  if (typeof payload.module === "string" && typeof payload.source === "string") {
    return (
      <span className="font-mono text-xs">
        源 {payload.source} · {payload.module}
      </span>
    )
  }
  if (typeof payload.query === "string" && typeof payload.hits === "number") {
    return (
      <span className="font-mono text-xs">
        检索 “{payload.query}” · 命中 {payload.hits} 篇
      </span>
    )
  }
  // tool.call（2026-09-19「工具」tab）：Claude Code 式一行摘要——工具图标+关键参数+耗时，
  // 失败/拒绝（ok=false）整段红色 ✗。per-tool 语义摘要（A1）优先，未登记工具走
  // TOOL_ARG_KEY 单字段兜底（再退首字符串值）
  if (typeof payload.name === "string" && payload.args !== undefined
      && typeof payload.duration_s === "number") {
    const a = (payload.args ?? {}) as Record<string, unknown>
    const tool = String(payload.name)
    const dur = payload.duration_s >= 0.05 ? ` · ${payload.duration_s}s` : ""
    const summarizer = TOOL_SUMMARIZERS[tool]
    if (summarizer) {
      const custom = summarizer(a, ctx)
      if (payload.ok === false) {
        return (
          <span className="font-mono text-xs text-(--status-error)">
            ✗ {TOOL_LABELS[tool] ?? tool} · {custom}
          </span>
        )
      }
      return (
        <span className="font-mono text-xs">
          {TOOL_LABELS[tool] ?? tool} · {custom}{dur}
        </span>
      )
    }
    const key = TOOL_ARG_KEY[tool] ?? ""
    let detail = typeof a[key] === "string" ? (a[key] as string) : ""
    if (!detail) {
      // 三级兜底（2026-09-28 问题1/2）：登记 arg key → 首个字符串值 → k=v 对拼接
      // （此前只取首字符串，args 首字段非字符串或全嵌套时摘要空白「看不出做了什么」）
      const pairs = Object.entries(a)
        .filter(([, v]) => (typeof v === "string" && v) || typeof v === "number")
        .slice(0, 2)
        .map(([k, v]) => `${k}=${clip(String(v), 40)}`)
      if (pairs.length) {
        detail = pairs.join(" ")
      } else if (Object.keys(a).length > 0) {
        detail = JSON.stringify(a)
      }
    }
    detail = clip(detail, LEN_SHORT)
    if (payload.ok === false) {
      return (
        <span className="font-mono text-xs text-(--status-error)">
          ✗ {TOOL_LABELS[tool] ?? tool}
          {detail ? ` · ${detail}` : ""}
        </span>
      )
    }
    return (
      <span className="font-mono text-xs">
        {TOOL_LABELS[tool] ?? tool}
        {detail ? ` · ${detail}` : ""}{dur}
      </span>
    )
  }
  // 技能路由审计：命中=技能名+分数+命中词；未命中 name=null，显查询首段
  if (typeof payload.score === "number" && Array.isArray(payload.matched) && typeof payload.query === "string") {
    if (typeof payload.name === "string") {
      const matched = (payload.matched as unknown[]).slice(0, 4).join(", ")
      return (
        <span className="font-mono text-xs">
          {payload.name} · {payload.score} 分{matched ? ` · ${matched}` : ""}
        </span>
      )
    }
    const q = payload.query.trim()
    return <span className="font-mono text-xs text-muted-foreground">未命中 · {q.slice(0, 50)}</span>
  }
  // A2 计划事件
  if (typeof payload.step_id === "string" && typeof payload.status === "string") {
    const demoted = Array.isArray(payload.auto_demoted) && payload.auto_demoted.length > 0
      ? `（${payload.auto_demoted.join("、")} 自动回 todo）`
      : ""
    return <span className="font-mono text-xs">{payload.step_id} → {payload.status}{demoted}</span>
  }
  if (Array.isArray(payload.plan) && typeof payload.session_id === "string") {
    const done = payload.plan.filter((s) => (s as { status?: string })?.status === "done").length
    const why = typeof payload.rev_reason === "string" && payload.rev_reason ? `（${payload.rev_reason}）` : ""
    return <span className="font-mono text-xs">{payload.plan.length} 步 · 完成 {done}/{payload.plan.length}{why}</span>
  }
  // E8 人类引导（2026-09-20 对话化）：事件 payload 现带全文（title 仍截 80 向后
  // 兼容），摘要读全文——对话气泡行直读 text 不经过这里，此为筛选视图/兜底通用行
  if (payload.kind === "human_note" && typeof payload.text === "string") return payload.text
  if (payload.kind === "finding_update" && typeof payload.title === "string") {
    const changes = Array.isArray(payload.changes) ? `（${payload.changes.join("、")}）` : ""
    return `${payload.title}${changes}`
  }
  // E7 资产状态流转摘要：open → visited（note）
  if (typeof payload.old === "string" && typeof payload.new === "string"
      && typeof payload.asset_id === "string") {
    const note = typeof payload.note === "string" && payload.note ? `（${payload.note}）` : ""
    return (
      <span className="font-mono text-xs">
        {payload.old} → {payload.new}{note}
      </span>
    )
  }
  // E8 步数预算事件摘要
  if (typeof payload.old_max === "number" && typeof payload.new_max === "number") {
    const who = payload.by === "human" ? "人类" : "Agent"
    const why = typeof payload.reason === "string" && payload.reason ? `（${payload.reason}）` : ""
    return (
      <span className="font-mono text-xs">
        {who}增补 · {payload.old_max} → {payload.new_max} 步{why}
      </span>
    )
  }
  if (typeof payload.max_steps === "number" && payload.session_id !== undefined
      && payload.old_max === undefined) {
    return (
      <span className="font-mono text-xs">
        预算 {payload.max_steps} 步已用尽 · 任务保持，等待人类「继续」（可附引导/增补步数）
      </span>
    )
  }
  // 分阶段工作流（pentest M4）：phase.changed {from,to,by,auto,published,reason?}——
  // auto=true 是过门自动流转，false 看生产方（human=人工 / approval=L1 审批）
  if (typeof payload.to === "string" && typeof payload.from === "string"
      && typeof payload.auto === "boolean") {
    const published = Array.isArray(payload.published) ? payload.published.length : 0
    return (
      <span className="font-mono text-xs">
        {String(payload.from)} → {String(payload.to)}
        {" · "}{payload.auto ? "门达标自动流转" : `by ${String(payload.by ?? "?")}`}
        {published > 0 ? ` · 首发 ${published} 剧本任务` : ""}
        {typeof payload.reason === "string" && payload.reason ? ` · ${payload.reason}` : ""}
      </span>
    )
  }
  // 对话化编排器（M2）：goal.confirm/clear 摘要 = 目标文本（payload={goal:{text,…}}）
  if (payload.goal && typeof (payload.goal as { text?: unknown }).text === "string")
    return (payload.goal as { text: string }).text
  if (typeof payload.summary === "string") return payload.summary
  // 自由文本正文（advisor.intervention 等；message.inbox 的 payload 自带 kind 先被上面分支接住）
  if (typeof payload.text === "string" && payload.kind === undefined) return payload.text
  if (typeof payload.objective === "string") return payload.objective
  if (typeof payload.cmd === "string")
    return <code className="font-mono text-xs">{payload.cmd}</code>
  if (typeof payload.title === "string") return payload.title
  if (typeof payload.note === "string") return payload.note
  if (typeof payload.name === "string") return payload.name
  if (typeof payload.total_tokens === "number") {
    // llm.usage：模型 · ↑输入 ↓输出（缓存读/建在展开 JSON 里看）
    const who = payload.source === "orchestrator" ? "编排"
      : payload.source === "planner" ? "顾问" : "Agent"
    return (
      <span className="font-mono text-xs text-muted-foreground">
        {who} · {String(payload.model || "?")} · ↑{String(payload.input_tokens)} ↓{String(payload.output_tokens)}
        {" "}· ∑{payload.total_tokens}
      </span>
    )
  }
  // llm.error：来源 + 截断错误文案；配额类（kind_hint=quota）加提示前缀
  if (typeof payload.source === "string" && typeof payload.error === "string") {
    const who = payload.source === "orchestrator" ? "编排"
      : payload.source === "planner" ? "顾问"
      : payload.source === "worker" ? "Agent" : payload.source
    return (
      <span className="font-mono text-xs">
        {payload.kind_hint === "quota" ? "⚠ 配额不足或限流 · " : ""}
        {who} · {payload.error.slice(0, 120)}
      </span>
    )
  }
  // mission.derive.result：发布 n 任务 · 开 m 窗
  if (typeof payload.published === "number") {
    return (
      <span className="font-mono text-xs">
        发布 {payload.published} 任务
        {payload.spawned ? ` · 开 ${payload.spawned} 窗` : ""}
      </span>
    )
  }
  if (typeof payload.error === "string") return payload.error
  if (typeof payload.provider === "string" && typeof payload.model === "string")
    return <code className="font-mono text-xs">{payload.provider}/{payload.model}</code>
  if (typeof payload.sha === "string") {
    const tail =
      typeof payload.function_count === "number"
        ? `${payload.function_count} 函数`
        : typeof payload.reason === "string"
          ? payload.reason
          : ""
    return (
      <code className="font-mono text-xs">
        {payload.sha.slice(0, 12)}
        {tail ? ` · ${tail}` : ""}
      </code>
    )
  }
  // C2（live-stream-ux）：finding.new/merged 人话渲染——[高危] title（vuln_class）· 目标 x。
  // finding.updated（payload={finding_id,changed,status} 无 vuln_class）不经此分支；
  // C1 前的旧事件无 title 回退显 vuln_class，target 反查不到显 id 尾 6 位
  if (typeof payload.finding_id === "string" && typeof payload.vuln_class === "string") {
    const sev = String(payload.severity ?? "")
    const label = assetLabel(payload.target_asset_id, ctx)
    return (
      <span className="font-mono text-xs">
        <span className={SEVERITY_COLOR[sev] ?? ""}>[{SEVERITY_LABEL[sev] ?? (sev || "?")}]</span>
        {" "}{typeof payload.title === "string" && payload.title ? clip(payload.title, LEN_SHORT) : payload.vuln_class}
        {`（${payload.vuln_class}）`}
        {label ? <> · 目标 {label}</> : null}
      </span>
    )
  }
  const keys = Object.keys(payload)
  if (keys.length === 0) return null
  // C3（live-stream-ux）：终兜底从「键名罗列」改「首个字符串值截 60」——任何事件行
  // 至少给一条可读信息；纯结构化对象才显键名
  const firstStr = Object.values(payload).find((v) => typeof v === "string" && v)
  if (typeof firstStr === "string")
    return <span className="font-mono text-xs">{clip(firstStr, LEN_SHORT)}</span>
  return <span className="font-mono text-xs text-muted-foreground">{keys.join(", ")}</span>
}
