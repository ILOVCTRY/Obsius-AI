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
      "task.deleted": { label: "🗑 删除任务", className: "text-muted-foreground", defaultOpen: true },
      "task.lease_expired": { label: "⏰ 租约过期", className: "text-(--status-approval)", defaultOpen: true },
      // A2 先规划后动手
      "task.plan_set": { label: "📐 制定计划", className: "text-sky-400", defaultOpen: true },
      "task.plan_revised": { label: "📐 修订计划", className: "text-sky-400", defaultOpen: true },
      "task.step": { label: "▦ 计划步进", className: "text-muted-foreground", defaultOpen: false },
      // ⑤ 完成对账硬拦：complete 被拒，列出未收口条目
      "task.reconcile_blocked": { label: "☑ 完成对账未收口", className: "text-amber-400", defaultOpen: true },
    }
    return map[kind] ?? { label: `task`, className: "text-muted-foreground", defaultOpen: false }
  }
  if (kind === "command") return { label: "⚡ 执行命令", className: "text-foreground/80", defaultOpen: false }
  if (kind === "command.result") return { label: "↳ 命令输出", className: "text-muted-foreground", defaultOpen: false }
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
      return { label: "🔵 发现增补", className: "text-sky-400", defaultOpen: true }
    if (payload?.kind === "basis_stale")
      return { label: "⚠ 依据撤回", className: "text-amber-400", defaultOpen: true }
    if (payload?.kind === "escalation_result")
      return { label: "🛫 升级命令已执行", className: "text-sky-400", defaultOpen: true }
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
    return { label: "🧭 策略顾问", className: "text-amber-400", defaultOpen: true }
  if (kind === "task.basis_stale_done")
    return { label: "⚠ 推翻依据下完成", className: "text-amber-400", defaultOpen: true }
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
    return { label: "🟡 预算预警", className: "text-amber-400", defaultOpen: true }
  // E8 步数预算：自助/人工增补与耗尽自动暂停
  if (kind === "step.budget_extended")
    return { label: "⏳ 步数增补", className: "text-amber-400", defaultOpen: true }
  if (kind === "session.budget_paused")
    return { label: "⏸ 步数预算用尽", className: "text-amber-400", defaultOpen: true }
  if (kind === "kb.open") return { label: "📖 打开知识库", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "kb.search") return { label: "🔍 搜索知识库", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "skill.open") return { label: "📖 打开技能正文", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "skill.routed") return { label: "🎯 技能路由", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "llm.thinking.delta")
    // 思考流式增量行（2026-09-19）：直播中默认展开看思考滚动，终稿 llm.thinking
    // 到达后该组跳过/后端清剪——本样式只活在直播窗口内
    return { label: "💭 思考中…", className: "text-primary", defaultOpen: true }
  if (kind === "llm.compact")
    // G3 上下文摘要压缩（2026-09-19）：N 条旧历史压成摘要；展开看前后规模
    return { label: "🧹 上下文压缩", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "proposal.created") return { label: "📝 变更提案", className: "text-(--status-approval)", defaultOpen: true }
  if (kind === "proposal.applied") return { label: "✅ 提案应用", className: "text-primary", defaultOpen: true }
  if (kind === "proposal.rejected") return { label: "⊘ 提案拒绝", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "session.spawned") return { label: "🟢 会话开窗", className: "text-primary", defaultOpen: true }
  if (kind === "session.work_state") return { label: "⚙ worker 状态", className: "text-muted-foreground", defaultOpen: false }
  if (kind === "session.paused") return { label: "⏸ 已暂停", className: "text-(--status-paused)", defaultOpen: true }
  if (kind === "session.resumed") return { label: "▶ 已恢复", className: "text-primary", defaultOpen: true }
  if (kind === "session.aborted") return { label: "⛔ 人工中断", className: "text-(--status-error)", defaultOpen: false }
  if (kind === "session.finished") return { label: "⚪ 会话收尾", className: "text-muted-foreground", defaultOpen: true }
  if (kind === "approval.requested") return { label: "🔔 请求审批", className: "text-(--status-approval)", defaultOpen: true }
  if (kind.startsWith("approval.")) return { label: "🔔 审批决定", className: "text-(--status-approval)", defaultOpen: true }
  if (kind === "orch.proposed") return { label: "💡 编排提案", className: "text-amber-400", defaultOpen: true }
  if (kind === "orch.tick.started")
    return { label: "⚙ 编排启动", className: "text-primary", defaultOpen: false }
  if (kind === "mission.derive")
    return { label: "🎯 自动派生判定", className: "text-primary", defaultOpen: false }
  if (kind === "mission.derive.result")
    return { label: "🎯 派生结果", className: "text-primary", defaultOpen: true }
  if (kind === "orch.chain_started") return { label: "⛓🤖 L2 自动链启动", className: "text-primary", defaultOpen: true }
  // 停止原因（含急停/预算/异常）由 eventSummary 中文行呈现
  if (kind === "orch.chain_stopped") return { label: "⛓⏹ L2 自动链停止", className: "text-amber-400", defaultOpen: true }
  if (kind === "orch.replan_priorities") return { label: "🔀 优先级重排", className: "text-primary", defaultOpen: false }
  if (kind === "project.digest") return { label: "📌 编排简报", className: "text-primary", defaultOpen: true }
  if (kind === "chain.created") return { label: "🔗 新建攻击链", className: "text-[#bc8cff]", defaultOpen: true }
  if (kind === "chain.updated") return { label: "🔗 攻击链更新", className: "text-[#bc8cff]", defaultOpen: false }
  if (kind === "chain.deleted") return { label: "🔗 删除攻击链", className: "text-muted-foreground", defaultOpen: true }
  if (kind === "chain.link_added") return { label: "🔗 链上挂节点", className: "text-[#bc8cff]", defaultOpen: true }
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

export function eventSummary(payload: Record<string, unknown>,
                              roleNames?: Record<string, string>): ReactNode {
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
  // 失败/拒绝（ok=false）整段红色 ✗。未登记工具显名原样；关键参数取该工具最有定位价值的一个字段
  if (typeof payload.name === "string" && payload.args !== undefined
      && typeof payload.duration_s === "number") {
    const a = (payload.args ?? {}) as Record<string, unknown>
    const key = TOOL_ARG_KEY[String(payload.name)] ?? ""
    let detail = typeof a[key] === "string" ? (a[key] as string) : ""
    if (!detail) {
      const first = Object.values(a).find((v) => typeof v === "string" && v)
      detail = typeof first === "string" ? first : ""
    }
    detail = detail.length > 60 ? detail.slice(0, 60) + "…" : detail
    const dur = payload.duration_s >= 0.05 ? ` · ${payload.duration_s}s` : ""
    if (payload.ok === false) {
      return (
        <span className="font-mono text-xs text-(--status-error)">
          ✗ {TOOL_LABELS[String(payload.name)] ?? String(payload.name)}
          {detail ? ` · ${detail}` : ""}
        </span>
      )
    }
    return (
      <span className="font-mono text-xs">
        {TOOL_LABELS[String(payload.name)] ?? String(payload.name)}
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
  if (typeof payload.node_type === "string" && typeof payload.node_id === "string")
    return <code className="font-mono text-xs">{payload.node_type}:{String(payload.node_id).slice(0, 14)}</code>
  const keys = Object.keys(payload)
  if (keys.length === 0) return null
  return <span className="font-mono text-xs text-muted-foreground">{keys.join(", ")}</span>
}
