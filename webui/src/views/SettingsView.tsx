import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/lib/api"
import type {
  AgentTool, Expert, LlmProvider, McpServer, SkillDef,
} from "@/lib/types"
import { capLabel, trackLabel, type Taxonomy } from "@/lib/taxonomy"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { GatewayPane } from "@/components/settings/GatewayPane"
import { AdvisorPane } from "@/components/settings/AdvisorPane"
import { Textarea } from "@/components/ui/textarea"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { DoctorBar } from "@/components/settings/DoctorBar"
import { HistoryButton } from "@/components/settings/HistoryDialog"
import { MatrixPane } from "@/components/settings/MatrixPane"
import {
  ExpertCreateDialog,
} from "@/components/settings/CreateDialogs"
import { KbView, type KbFocus } from "@/components/settings/KbView"
import { SkillWorkspace } from "@/components/settings/SkillWorkspace"
import { ProposalsPane } from "@/components/settings/ProposalsPane"
import { IntelSourcePane } from "@/components/settings/IntelSourcePane"
import { RulesPane, type RuleFocus } from "@/components/settings/RulesPane"
import { AgentToolsPane } from "@/components/settings/AgentToolsPane"
import { AppearancePane } from "@/components/settings/AppearancePane"
import { FeishuPane } from "@/components/settings/FeishuPane"
import { ProviderDialog } from "@/components/settings/ProviderDialog"
import { FORMAT_LABEL } from "@/lib/providerPresets"
import { McpPane } from "@/components/McpPane"
import { cn } from "@/lib/utils"
import {
  Bot, Cable, ChevronDown, ChevronRight, CircleGauge,
  Gauge, GitPullRequest, Network, Radio, ShieldCheck,
  Palette, Sparkles, Wrench, BellRing,
} from "lucide-react"

/** packs 文件写操作后通知 doctor 条刷新（30s 轮询之外的即时通道） */
function packsChanged() {
  window.dispatchEvent(new Event("packs-changed"))
}

// doctor 跳转指令（跨 tab/轨选中具体专家或技能）
interface ExpertFocus { id: string; n: number }
interface SkillFocus { name: string; n: number }

// 设置页（DESIGN.md §12 页面 6-9）：专家池（packs/experts/ 单文件池）/ 技能库（packs/kb/skills 扁平）/
// 安全红线（轨红线/平台规则/评级）/ 模型 / MCP。
// 修改经 core API 写 packs 文件（写入前留 .history）；改动在下次开窗生效，在跑会话不受影响。

function Saved({ saved }: { saved: boolean }) {
  return <span className={cn("text-[10px] transition-opacity", saved ? "text-primary opacity-100" : "opacity-0")}>已保存 ✓</span>
}

// 勾选清单三态：null = 全量（不限制，后端不落键）；[] = 一个都不启用；非空 = 勾选清单。
// null 必须保留——兜底/主控专家靠它表达「全量」，读成 [] 再保存会把能力清空。
// 按 group 分组展示（技能=后端推断的展示分组、工具=TOOL_GROUPS、MCP=domains），
// 组头可折叠、可整组全选/清空；分组只是排版，勾选结果仍是一条扁平 id 清单。
type CheckItem = { id: string; label: string; detail?: string; group?: string }

/** 已选但不在目录里的项单独成组（技能被删/目录接口失败时不能凭空消失） */
const ORPHAN_GROUP = "清单外（保留原值）"

function CheckList({ title, items, selected, onChange, groupOrder }: {
  title: string
  items: CheckItem[]
  selected: string[] | null
  onChange: (next: string[] | null) => void
  groupOrder?: readonly string[]
}) {
  const [closed, setClosed] = useState<Record<string, boolean>>({})
  const allIds = useMemo(() => items.map((i) => i.id), [items])
  const isAll = selected === null
  const picked = selected ?? allIds
  const pickedSet = new Set(picked)
  const rows = useMemo<CheckItem[]>(() => [
    ...items,
    ...picked.filter((id) => !allIds.includes(id))
      .map((id) => ({ id, label: id, detail: "不在当前清单中（保留原值，可取消勾选）", group: ORPHAN_GROUP })),
  ], [items, picked, allIds])
  // 分组顺序：先按调用方给的类别顺序（工具=TOOL_GROUPS、技能=SKILL_GROUP_ORDER），
  // 未列出的组按首次出现顺序追加，保证新增类别不会消失。
  const groups = useMemo(() => {
    const map = new Map<string, CheckItem[]>()
    for (const it of rows) {
      const g = it.group || "其他"
      const bucket = map.get(g)
      if (bucket) bucket.push(it)
      else map.set(g, [it])
    }
    const head = (groupOrder ?? []).filter((g) => map.has(g))
    const tail = [...map.keys()].filter((g) => !head.includes(g))
    return [...head, ...tail].map((g) => [g, map.get(g) ?? []] as const)
  }, [rows, groupOrder])

  const toggle = (id: string) =>
    onChange(pickedSet.has(id) ? picked.filter((x) => x !== id) : [...picked, id])

  return <div className="rounded border bg-card/30 p-2">
    <div className="mb-1 flex items-center gap-2">
      <label className="text-[10px] text-muted-foreground">{title}</label>
      <span className="text-[10px] text-primary">{isAll ? "全部（未限制）" : `已选 ${picked.length}`}</span>
      <span className="flex-1" />
      {isAll
        ? <button className="text-[10px] text-muted-foreground hover:text-primary" onClick={() => onChange([])}>改为按勾选</button>
        : <button className="text-[10px] text-muted-foreground hover:text-primary" onClick={() => onChange(null)}>恢复全部</button>}
    </div>
    {/* 不再内层限高：设置页本身可滚动，嵌套滚动会让「任务类型/工具清单」难到达；
        清单太长时折叠不关心的组即可。 */}
    <div className="space-y-1">
      {groups.map(([g, list]) => {
        const ids = list.map((i) => i.id)
        const on = ids.filter((id) => pickedSet.has(id)).length
        const open = !closed[g]
        return <div key={g} className="rounded border border-border/60 bg-background/40">
          <div className="flex items-center gap-1 px-1.5 py-1">
            <button type="button" className="flex min-w-0 flex-1 items-center gap-1 text-left"
                    title={open ? "折叠该组" : "展开该组"}
                    onClick={() => setClosed((c) => ({ ...c, [g]: open }))}>
              {open ? <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
                    : <ChevronRight className="size-3 shrink-0 text-muted-foreground" />}
              <span className="truncate text-[11px] font-medium">{g}</span>
              <span className={cn("shrink-0 text-[10px]", on ? "text-primary" : "text-muted-foreground")}>{on}/{ids.length}</span>
            </button>
            {g !== ORPHAN_GROUP && <>
              <button type="button" className="shrink-0 text-[10px] text-muted-foreground hover:text-primary"
                      onClick={() => onChange(Array.from(new Set([...picked, ...ids])))}>全选</button>
              <button type="button" className="shrink-0 text-[10px] text-muted-foreground hover:text-primary"
                      onClick={() => onChange(picked.filter((x) => !ids.includes(x)))}>清空</button>
            </>}
          </div>
          {open && <div className={cn("grid grid-cols-1 gap-0.5 border-t border-border/40 px-1.5 py-1",
                                     list.length > 1 && "md:grid-cols-2")}>
            {list.map((item) => <label key={item.id} title={item.detail ? `${item.label} — ${item.detail}` : item.label}
                                       className="flex min-w-0 cursor-pointer items-start gap-1.5 rounded px-1.5 py-1 hover:bg-accent/40">
              <input type="checkbox" checked={pickedSet.has(item.id)} onChange={() => toggle(item.id)} className="mt-0.5" />
              <span className="min-w-0"><code className="block truncate text-[11px]">{item.label}</code>{item.detail && <span className="block truncate text-[10px] text-muted-foreground">{item.detail}</span>}</span>
            </label>)}
          </div>}
        </div>
      })}
      {groups.length === 0 && <span className="text-[10px] text-muted-foreground">暂无可选项</span>}
    </div>
  </div>
}

/** 技能展示分组顺序（与 core/skills/taxonomy.SKILL_GROUP_ORDER 同口径） */
const SKILL_GROUP_ORDER = ["分诊", "Web 攻击", "二进制与逆向", "红队后渗透",
                           "云与容器", "侦察", "蓝图与建模", "其他"]
/** MCP server 的 domain（config/mcp.json 的 domains）→ 中文类别 */
const MCP_DOMAIN_LABEL: Record<string, string> = {
  pentest: "渗透测试", reverse: "逆向", binary: "二进制",
  web: "Web", forensics: "取证", crypto: "密码", misc: "杂项",
}
const MCP_DOMAIN_ORDER = ["渗透测试", "逆向", "二进制", "Web", "取证", "密码", "杂项", "其他"]

export function SettingsView({ nav, pid }: {
  nav?: { tab: string; n: number; skill?: { name: string } } | null
  pid?: string | null
} = {}) {
  const [tax, setTax] = useState<Taxonomy | null>(null)
  const [track, setTrack] = useState("ctf")
  const [cap, setCap] = useState("web")
  const [tab, setTab] = useState("experts")
  const [expertFocus, setExpertFocus] = useState<ExpertFocus | null>(null)
  const [skillFocus, setSkillFocus] = useState<SkillFocus | null>(null)
  const [kbFocus, setKbFocus] = useState<KbFocus | null>(null)
  const [ruleFocus, setRuleFocus] = useState<RuleFocus | null>(null)
  const [pendingN, setPendingN] = useState(0)
  // 深链已指定轨/包时，taxonomy 异步回填的缺省值不得覆盖（晚于 nav effect 落地会冲掉深链）
  const navAppliedRef = useRef(false)
  // 深链只钉一个维度（track 或 cap），另一维仍由项目缺省补
  const navPinnedRef = useRef<{ track?: boolean; cap?: boolean }>({})
  // 项目上下文缺省：从项目内打开设置 → 轨/能力包跟随当前项目（优先级低于深链）
  const projectAppliedRef = useRef(false)

  useEffect(() => {
    api.taxonomy().then((t) => {
      setTax(t)
      if (!navAppliedRef.current && !projectAppliedRef.current) {
        if (t.tracks[0]) setTrack(t.tracks[0].name)
        if (t.directions[0]) setCap(t.directions[0])
      }
    }).catch(() => {})
  }, [])

  // 从项目内打开（pid 存在）：轨/kb 方向缺省跟随当前项目——不再每次都显示 CTF
  useEffect(() => {
    if (!pid || navAppliedRef.current) return
    projectAppliedRef.current = true
    api.getProject(pid).then((p) => {
      // 守卫须在异步回调内按维度复查：请求在飞期间深链可能已落地（nav effect 晚于入口检查）；
      // 深链只钉一维，另一维仍按项目补（cap 深链时 track 仍应=pentest）
      if (p.track && !navPinnedRef.current.track) setTrack(p.track)
      // 用盘上绑定方向：effective 在 _generalist 项目=全部方向，[0] 会错落到 binary
      const dirs = p.directions_bound?.length ? p.directions_bound : p.directions
      if (dirs?.length && !navPinnedRef.current.cap) setCap(dirs[0])
    }).catch(() => {})
  }, [pid])

  // 提案角标数：挂载即拉 + 提案变化（审批/复盘 Job 落地）时刷新
  useEffect(() => {
    const refresh = () => api.proposals("pending")
      .then((ps) => setPendingN(ps.length)).catch(() => {})
    refresh()
    window.addEventListener("proposals-changed", refresh)
    return () => window.removeEventListener("proposals-changed", refresh)
  }, [])

  // 跨视图跳指定 tab；带 skill 时（直播间 skill.routed 双击）深链选中该技能（技能已扁平，无轨/包维度）
  useEffect(() => {
    if (!nav?.n) return
    setTab(nav.tab)
    const s = nav.skill
    if (s) {
      navAppliedRef.current = true
      setSkillFocus({ name: s.name, n: nav.n })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav?.n])

  // doctor 详情跳转：按 target 路径切 tab/轨并选中具体条目（2026-10-13 新布局：技能扁平、红线收在 kb/安全红线/）
  const navigateTarget = (target: string): boolean => {
    let m: RegExpMatchArray | null
    if ((m = target.match(/^experts\/([\w.-]+)\.yaml$/))) {
      setTab("experts")
      setExpertFocus({ id: m[1], n: Date.now() })
      return true
    }
    // 技能（扁平：packs/kb/skills/<name>/SKILL.md）
    if ((m = target.match(/^kb\/skills\/([\w.-]+)\/SKILL\.md$/))) {
      setTab("skills")
      setSkillFocus({ name: m[1], n: Date.now() })
      return true
    }
    // 轨红线：packs/kb/安全红线/轨红线/<track>.md
    if ((m = target.match(/^kb\/安全红线\/轨红线\/([\w.-]+)\.md$/))) {
      setTrack(m[1]); setTab("rules")
      setRuleFocus({ kind: "track-redlines", name: m[1], n: Date.now() })
      return true
    }
    // 评级与价值口径：packs/kb/安全红线/评级与价值口径/<tag>.md（渗透独有）
    if ((m = target.match(/^kb\/安全红线\/评级与价值口径\/([\w.-]+)\.md$/))) {
      setTab("rules")
      setRuleFocus({ kind: "rating", name: "pentest", tag: m[1], n: Date.now() })
      return true
    }
    // kb 知识模块：packs/kb/<domain>/<path>
    if ((m = target.match(/^kb\/([\w.-]+)\/(.+\.(?:md|py|txt|json))$/))) {
      setCap(m[1]); setTab("kb")
      setKbFocus({ path: m[2], n: Date.now() })
      return true
    }
    return false
  }

  const navGroups: { label: string; items: [string, string, typeof Bot][] }[] = [
    { label: "智能系统", items: [["experts", "专家池", Bot], ["matrix", "能力矩阵", CircleGauge], ["advisor", "策略顾问", Sparkles]] },
    { label: "安全控制", items: [["rules", "安全红线", ShieldCheck], ["gateway", "访问网关", Network], ["tools", "执行工具", Wrench]] },
    { label: "基础设施", items: [["llm", "模型供应商", Gauge], ["mcp", "MCP 连接", Cable], ["intel", "情报来源", Radio], ["feishu", "飞书推送", BellRing]] },
    { label: "工作流", items: [["proposals", "变更提案", GitPullRequest], ["appearance", "外观主题", Palette]] },
  ]
  const activeLabel = navGroups.flatMap((g) => g.items).find(([key]) => key === tab)?.[1] ?? "设置"

  // pt-9=首页无顶栏分支的 36px 拖动区让位；项目内有 .topbar(h-16)，再加就是重复死区
  return (
    <div className={cn("settings-shell", !pid && "pt-9")}>
      <aside className="settings-sidebar">
        {/* 精简侧栏（2026-10-04）：去掉「控制中心」品牌块（左侧主导航已有「设置」入口），
            只留一行轻量上下文，与下方导航自然过渡。 */}
        <div className="settings-context"><span className="settings-live-dot" /><span>{trackLabel(track)}</span><span className="settings-slash">/</span><span>{capLabel(cap)}</span></div>
        <div className="settings-nav">
          {navGroups.map((group) => <div className="settings-nav-group" key={group.label}><span className="settings-nav-label">{group.label}</span>{group.items.map(([key, label, Icon]) => <button key={key} aria-label={label} className={cn("settings-nav-item", tab === key && "is-active")} onClick={() => setTab(key)}><Icon size={14} /><span>{label}</span>{key === "proposals" && pendingN > 0 && <b>{pendingN}</b>}<ChevronRight size={13} className="settings-nav-arrow" /></button>)}</div>)}
        </div>
        <div className="settings-sidebar-footer"><span className="settings-live-dot" />配置服务正常<span className="settings-version">v0.8</span></div>
      </aside>
      <main className="settings-main">
        {/* 只留「健康度」（2026-10-04）：控制中心表头与同步提示项目内外都不需要；
            健康度是全局信息，仅首页（无项目上下文）显示。 */}
        {!pid && <DoctorBar onNavigate={navigateTarget} />}
        <Tabs value={tab} onValueChange={setTab} className="settings-content min-h-0 flex-1 flex-col gap-0">
          <TabsList className="hidden"><TabsTrigger value={tab}>{activeLabel}</TabsTrigger></TabsList>
          <TabsContent value="experts" className="min-h-0 flex-1"><ExpertsPane tax={tax} focus={expertFocus} /></TabsContent>
          <TabsContent value="skills" className="min-h-0 flex-1"><SkillWorkspace pid={pid} focus={skillFocus} onExpert={(id) => { setTab("experts"); setExpertFocus({ id, n: Date.now() }) }} /></TabsContent>
          <TabsContent value="kb" className="min-h-0 flex-1"><KbView cap={cap} focus={kbFocus} /></TabsContent>
          <TabsContent value="matrix" className="min-h-0 flex-1"><MatrixPane tax={tax} track={track} onFocusSkill={(name) => { setTab("skills"); setSkillFocus({ name, n: Date.now() }) }} /></TabsContent>
          <TabsContent value="rules" className="min-h-0 flex-1"><RulesPane track={track} focus={ruleFocus} pid={pid} /></TabsContent>
          <TabsContent value="advisor" className="min-h-0 flex-1"><AdvisorPane pid={pid} /></TabsContent>
          <TabsContent value="llm" className="min-h-0 flex-1"><LlmPane /></TabsContent><TabsContent value="mcp" className="min-h-0 flex-1"><McpPane /></TabsContent><TabsContent value="gateway" className="min-h-0 flex-1"><GatewayPane pid={pid} /></TabsContent><TabsContent value="tools" className="min-h-0 flex-1"><AgentToolsPane /></TabsContent><TabsContent value="intel" className="min-h-0 flex-1"><IntelSourcePane /></TabsContent><TabsContent value="proposals" className="min-h-0 flex-1"><ProposalsPane onPendingChange={setPendingN} /></TabsContent><TabsContent value="appearance" className="min-h-0 flex-1"><AppearancePane /></TabsContent><TabsContent value="feishu" className="min-h-0 flex-1"><FeishuPane /></TabsContent>
        </Tabs>
      </main>
    </div>
  )
}

// ---------- 专家池（expert-pool M3）：packs/experts/ 单文件池，全池列表 + 全字段覆写表单 ----------

function ExpertsPane({ tax, focus }: { tax: Taxonomy | null; focus: ExpertFocus | null }) {
  const [experts, setExperts] = useState<Expert[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [displayName, setDisplayName] = useState("")   // 中文显示名；留空 = 用专家 id
  const [description, setDescription] = useState("")
  const [persona, setPersona] = useState("")
  const [trackSel, setTrackSel] = useState<string[]>([]) // 空 = null = 服务全轨
  const [skills, setSkills] = useState<string[] | null>(null)
  const [skillCatalog, setSkillCatalog] = useState<SkillDef[]>([])
  const [taskTypes, setTaskTypes] = useState("")
  const [tools, setTools] = useState<string[] | null>(null)
  const [toolCatalog, setToolCatalog] = useState<AgentTool[]>([])
  const [toolGroups, setToolGroups] = useState<string[]>([])
  const [mcpServers, setMcpServers] = useState<string[] | null>(null)
  const [mcpCatalog, setMcpCatalog] = useState<McpServer[]>([])
  const [steps, setSteps] = useState("")
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [confirmDel, setConfirmDel] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(() => {
    let alive = true
    // M3 virtual（编排器单例，不入 experts/*.yaml）不进专家池管理
    api.listExperts().then((es) => {
      if (!alive) return
      const pool = es.filter((e) => e.kind !== "virtual")
      setExperts(pool)
      setSelected((cur) => cur && pool.some((e) => e.id === cur) ? cur : (pool[0]?.id ?? null))
    }).catch(() => {})
    return () => { alive = false }
  }, [])
  useEffect(() => reload(), [reload])
  useEffect(() => {
    api.skills().then(setSkillCatalog).catch(() => {})
    api.agentTools().then((r) => { setToolCatalog(r.tools); setToolGroups(r.groups ?? []) }).catch(() => {})
    api.mcpServers().then((r) => setMcpCatalog(r.servers)).catch(() => {})
  }, [])

  // doctor 跳转：选中指定专家
  useEffect(() => {
    if (focus) setSelected(focus.id)
  }, [focus])

  const cur = experts.find((e) => e.id === selected)

  /** 控制器专属工具（不在 AGENT_TOOLS 目录里，后端随 tools_runtime 下发）。
   *  它们是走专用 dispatch 分支的工具：`todo_write`/`call_expert` 走 `_dispatch`
   *  早拦截，`artex_*` 走 artex_steering_dispatch，`ctf_dispatch/review/inspect`
   *  走 CTF 主控专用分支。没有条目就只能显示裸名字，补一份中文说明便于勾选。 */
  const CONTROLLER_ONLY_TOOL_DETAIL: Record<string, string> = {
    todo_write: "维护线程待办清单（整体覆写；工作记忆外化，人类实时可见）",
    call_expert: "委派专家池专家执行一项任务（spawn 隔离子线程，返回摘要）",
    artex_plan: "让 ARTEX Planner 生成/扩展 Intent（ARTEX 主控的默认派发通道）",
    artex_status: "查看 ARTEX 任务 runtime 状态、目标达成、Intent 计数与探索图",
    artex_hint: "给运行中的 Worker 下发方向调整 / 留给后续规划的 Hint",
    artex_control: "暂停 / 取消 / 终止 ARTEX 任务",
    artex_goal: "登记与核验目标达成（prove / goal_met）",
    artex_fact: "写回探索图事实（主控视角的结论沉淀）",
    ctf_dispatch: "CTF 唯一派发入口：方向提交代码调度器，拿回准派/拦下计划",
    ctf_review: "查看代码侧 verdict 表（轮次/已得/放弃判据）",
    ctf_inspect: "只读核对通道：摊台账证据链原文与产物 provenance（不执行）",
  }
  /** 工具勾选清单。
   *
   *  - **控制器**：**只用后端下发的 `tools_runtime`**（`controller_tool_candidates`）。
   *    不能用「AGENT_TOOLS ∪ tools_runtime」——那样后端已扣掉的结构性禁令
   *    （ARTEX 主控的 `declare_intent` 等意图链、`finish` 等控制原语）会从
   *    AGENT_TOOLS 里漏回来，用户勾了不生效，又是一次「界面在骗人」。
   *  - **子专家**：用 AGENT_TOOLS 通用目录（后端不下发候选）。
   *
   *  目录里查不到的（`artex_*` / `ctf_dispatch` 等专属工具）补一份中文说明，
   *  否则勾选行只有裸名字。 */
  const toolItems = useMemo<CheckItem[]>(() => {
    const byName = new Map(toolCatalog.map((t) => [t.name, t]))
    const controllerNames = cur?.tools_runtime
    if (controllerNames && controllerNames.length) {
      return controllerNames.map((n) => {
        const hit = byName.get(n)
        if (hit) return { id: hit.name, label: hit.name, detail: hit.description, group: hit.group }
        // MCP server 通配（`mcp__<server>__*`，controller-mcp-face）：一个 server
        // 可能有 200+ 工具（rea 215 个 / 549K 字符 schema），逐个列既没法勾也
        // 没人看。勾选粒度就是「要不要让这个主控用这个 server」。
        if (n.startsWith("mcp__") && n.endsWith("__*")) {
          return { id: n, label: n,
                   detail: "MCP server 整包：勾选后该 server 的全部工具下发给本控制器（未勾选则一个都不下发）",
                   group: "MCP server" }
        }
        return { id: n, label: n,
                 detail: CONTROLLER_ONLY_TOOL_DETAIL[n] ?? "控制器专属工具（无通用目录条目）",
                 group: "控制器专属" }
      })
    }
    return toolCatalog.map((t) => ({ id: t.name, label: t.name, detail: t.description, group: t.group }))
  }, [toolCatalog, cur?.tools_runtime])

  useEffect(() => {
    if (!cur) return
    setDisplayName(cur.name && cur.name !== cur.id ? cur.name : "")
    setDescription(cur.description ?? "")
    setPersona(cur.persona ?? "")
    setTrackSel(cur.tracks ?? [])
    // null 必须原样保留：null = 全量（后端不落键），[] = 一个都不启用。
    // 读成 [] 再保存会把「未限制」写成「全部禁用」。
    setSkills(cur.skills ?? null)
    setTaskTypes((cur.task_types ?? []).join(", "))
    setTools(cur.tools ?? null)
    setMcpServers(cur.mcp_servers ?? null)
    setSteps(cur.max_steps != null ? String(cur.max_steps) : "")
    setSaved(false)
    setErr(null)
  }, [cur])

  const toggleTrack = (t: string) =>
    setTrackSel((ts) => ts.includes(t) ? ts.filter((x) => x !== t) : [...ts, t])

  const save = async () => {
    if (!cur) return
    const toList = (s: string) => s.split(/[,，]/).map((x) => x.trim()).filter(Boolean)
    const stepsTrim = steps.trim()
    try {
      // 全字段覆写（表单即最终态）：空值=null 不落键。
      // 能力白名单三件套按 state 原样提交：null = 不限制（后端不落键）；
      // [] = 明确一个都不启用（后端落 `key: []`）；非空 = 勾选清单。
      await api.updateExpert(cur.id, {
        name: displayName.trim() || null,
        description: description.trim() || null,
        persona: persona.trim() || null,
        tracks: trackSel.length ? trackSel : null,
        skills,
        // kb 方向两态（null/[] 都=不限），表单不编辑但必须原样回传，否则覆写会清空。
        knowledge: cur.knowledge ?? null,
        task_types: taskTypes.trim() ? toList(taskTypes) : null,
        // 工具白名单：勾选即开关（controller-tool-select，2026-10-20）。
        // 控制器与子专家同口径——null = 不落键（控制器此时回落代码默认面），
        // [] = 明确一个都不开，非空 = 勾选清单。
        tools,
        mcp_servers: mcpServers,
        max_steps: stepsTrim ? Number(stepsTrim) : null,
        // 轨变体只读展示，但必须原样回传——否则全字段覆写会把 variant_<轨>_<字段> 抹掉。
        variants: Object.keys(cur.variants ?? {}).length ? cur.variants : null,
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
      packsChanged()
      reload()
    } catch (e) {
      setErr(String(e))
    }
  }

  const doDelete = async () => {
    if (!confirmDel) return
    setBusy(true)
    setErr(null)
    try {
      await api.deleteExpert(confirmDel)
      setSelected((c) => c === confirmDel ? null : c)
      packsChanged()
      reload()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
      setConfirmDel(null)
    }
  }

  return (
    <div className="flex h-full">
      <ScrollArea className="w-52 shrink-0 border-r">
        <div className="p-2">
          <Button size="sm" variant="outline" className="mb-1 h-7 w-full text-[11px]"
                  onClick={() => setCreateOpen(true)}>＋ 新建专家</Button>
          {experts.map((e) => (
            <div key={e.id}
                 className={cn("group flex items-center rounded text-xs",
                   selected === e.id && "bg-primary/10 text-primary")}>
              <button
                className="min-w-0 flex-1 truncate rounded px-2 py-1.5 text-left font-mono hover:bg-accent/40"
                onClick={() => setSelected(e.id)}
                title={`${e.id}${e.description ? ` · ${e.description}` : ""}`}>
                {e.name || e.id}
                <span className="ml-1 text-[9px] text-muted-foreground">
                  {!e.tracks?.length ? "全轨" : e.tracks.join("/")}
                </span>
              </button>
              {!e.protected && (
                <button className="px-1.5 text-[10px] text-(--status-error) opacity-0 transition-opacity group-hover:opacity-100"
                        title="删除（移入 experts/.history/trash，可恢复）"
                        onClick={() => setConfirmDel(e.id)}>✕</button>
              )}
            </div>
          ))}
          {experts.length === 0 && <p className="p-2 text-xs text-muted-foreground">专家池为空</p>}
        </div>
      </ScrollArea>
      <div className="flex min-w-0 flex-1 flex-col gap-2 overflow-y-auto p-3">
        {cur ? (
          <>
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm">{cur.file}</span>
              {cur.protected && <span className="text-[10px] text-muted-foreground">受保护（拒删，可编辑）</span>}
              <span className="flex-1" />
              {err && <span className="text-[10px] text-(--status-error)">{err}</span>}
              <Saved saved={saved} />
              {cur.file && <HistoryButton file={cur.file} onRolledBack={reload} />}
              <Button size="sm" onClick={save}>保存</Button>
            </div>
            <label className="text-[10px] text-muted-foreground">显示名（可中文；留空 = 用专家 id {cur.id}；界面各处展示用）</label>
            <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} className="text-xs"
                   placeholder={`如：${cur.id.replace(/^_/, "")}`} />
            <label className="text-[10px] text-muted-foreground">职责 description（编排开窗目录与组队列表的一句话说明）</label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} className="text-xs"
                   placeholder="如：外网打点与入口利用" />
            <label className="text-[10px] text-muted-foreground">人设 persona（注入系统提示；轨变体可按轨覆写）</label>
            {/* shrink-0：本列是 flex-col，textarea 默认 flex-shrink:1 会被压回 min-h-16，
                表现为「拖拽句柄拖大了又弹回去」。resize-y 显式声明可纵向拖拽。 */}
            <Textarea value={persona} onChange={(e) => setPersona(e.target.value)}
                      className="min-h-16 shrink-0 resize-y text-xs" />
            <div>
              <label className="text-[10px] text-muted-foreground">服务轨 tracks（不选 = 全轨通用）</label>
              <div className="mt-1 flex flex-wrap gap-1">
                {(tax?.tracks ?? []).map((t) => (
                  <button key={t.name}
                          onClick={() => toggleTrack(t.name)}
                          className={cn("rounded border px-2 py-0.5 text-[11px]",
                            trackSel.includes(t.name)
                              ? "border-primary/50 bg-primary/10 text-primary"
                              : "text-muted-foreground hover:bg-accent/50")}>
                    {t.label || trackLabel(t.name)}
                  </button>
                ))}
              </div>
            </div>
            <CheckList title="技能白名单（勾选启动）"
                       items={skillCatalog.map((s) => ({ id: s.name, label: s.name, detail: s.description, group: s.group }))}
                       selected={skills} onChange={setSkills} groupOrder={SKILL_GROUP_ORDER} />
            <p className="text-[10px] text-muted-foreground">
              kb 方向 knowledge（两态：空 = 不限方向；保存时原样保留，改方向请直接编辑 yaml）
              ：<code className="font-mono">{cur.knowledge?.length ? cur.knowledge.join(", ") : "不限"}</code>
            </p>
            <label className="text-[10px] text-muted-foreground">任务类型 task_types（Worker 认领过滤器；按各轨注册表并集体检；留空 = 不限）</label>
            <Input value={taskTypes} onChange={(e) => setTaskTypes(e.target.value)} className="font-mono text-xs" placeholder="recon, exploit" />
            {/* 工具白名单（controller-tool-select，2026-10-20 定稿）：
                控制器类智能体（chat-orchestrator / aggressive / artex / ctf-controller）
                的 yaml `tools` 现在是**真实开关**——勾了就下发、没勾就既不下发 schema
                也会在执行侧被拒。候选清单额外并入后端下发的 `tools_runtime`
                （`artex_*` / `ctf_*` / `todo_write` / `call_expert` 不在通用目录里），
                否则这些工具永远勾不到，勾选又退化成固定清单。 */}
            <CheckList title="工具白名单（勾选启动）"
                       items={toolItems}
                       selected={tools} onChange={setTools} groupOrder={toolGroups} />
            {cur.tools_controller && (
              <p className="text-[10px] leading-relaxed text-muted-foreground">
                这是控制器类智能体：上方清单已并入后端下发的专属工具
                （<code className="font-mono">artex_*</code> /
                <code className="font-mono">ctf_dispatch/review/inspect</code> /
                <code className="font-mono">todo_write</code> /
                <code className="font-mono">call_expert</code>）。
                勾选后保存即生效；<code className="font-mono">declare_intent</code> 等
                计划/控制原语恒放行、不受勾选影响。
                <code className="font-mono">propose_pack_edit</code> 另受项目级
                kb 迭代开关门禁——勾了但项目开关关闭时仍不会下发。
              </p>
            )}
            <CheckList title="MCP server（勾选启动）"
                       items={mcpCatalog.map((s) => ({
                         id: s.name,
                         label: s.name,
                         detail: `${s.transport}${s.enabled ? " · 全局已启用" : " · 全局未启用"}`
                           + (s.domains?.length > 1 ? ` · ${s.domains.join("/")}` : ""),
                         group: s.domains?.length
                           ? (MCP_DOMAIN_LABEL[s.domains[0]] ?? s.domains[0])
                           : "其他",
                       }))}
                       selected={mcpServers} onChange={setMcpServers} groupOrder={MCP_DOMAIN_ORDER} />
            <label className="text-[10px] text-muted-foreground">最大步数 max_steps（正整数；留空 = 不设）</label>
            <Input value={steps} onChange={(e) => setSteps(e.target.value)} className="w-48 font-mono text-xs" inputMode="numeric" placeholder="200" />
            {Object.keys(cur.variants ?? {}).length > 0 && (
              <div className="rounded border bg-card/40 p-2">
                <p className="mb-1 text-[10px] text-muted-foreground">轨变体 variants（只读——yaml 中以 variant_&lt;轨&gt;_&lt;字段&gt; 平铺键维护）</p>
                {Object.entries(cur.variants!).map(([tr, fields]) => (
                  <p key={tr} className="font-mono text-[10px] text-muted-foreground">
                    {tr}: {Object.entries(fields).map(([k, v]) => `${k}=${String(v)}`).join("、")}
                  </p>
                ))}
              </div>
            )}
          </>
        ) : <p className="text-xs text-muted-foreground">选择左侧专家</p>}
      </div>
      <ExpertCreateDialog open={createOpen} onOpenChange={setCreateOpen}
                          onCreated={(id) => { setSelected(id); reload() }} />
      <AlertDialog open={confirmDel !== null} onOpenChange={(v) => !v && setConfirmDel(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除专家 {confirmDel}？</AlertDialogTitle>
            <AlertDialogDescription>
              yaml 将移入 experts/.history/trash/（带时间戳，可手工恢复）。
              正在运行的会话不受影响；下次开窗生效。受保护专家（_generalist）不可删除。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction disabled={busy}
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={doDelete}>删除</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

// ---------- LLM 供应商：增删改 + 获取模型 + 勾选保存（首个=默认） ----------

function LlmPane() {
  const [providers, setProviders] = useState<LlmProvider[]>([])
  const [def, setDef] = useState<{ provider: string; model: string } | null>(null)
  // 可选默认供应商（全局默认 = 它的第一个模型；未设置 = 第一个启用供应商）
  const [defaultProvider, setDefaultProvider] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [helpOpen, setHelpOpen] = useState(false)
  // 弹窗目标（2026-10-04 编辑改弹窗）：null=关闭 / -1=新增 / >=0=编辑该索引的供应商
  const [editIdx, setEditIdx] = useState<number | null>(null)
  // 行内「测试」结果（按供应商索引）：测该供应商首个模型是否可用
  const [rowTest, setRowTest] = useState<Record<number, string>>({})

  useEffect(() => {
    api.llmProviders().then((r) => {
      setProviders(r.providers)
      setDef(r.default)
      setDefaultProvider(r.default_provider ?? null)
    }).catch(() => {})
  }, [])

  const patch = (i: number, p: Partial<LlmProvider>) =>
    setProviders((ps) => ps.map((s, j) => j === i ? { ...s, ...p } : s))

  const move = (i: number, delta: number) =>
    setProviders((ps) => {
      const j = i + delta
      if (j < 0 || j >= ps.length) return ps
      const next = [...ps]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })

  // 弹窗保存：editIdx<0 或 null 时新增（追加末尾），否则覆写该索引
  const saveProvider = (p: LlmProvider) =>
    setProviders((ps) => (editIdx != null && editIdx >= 0
      ? ps.map((x, j) => (j === editIdx ? p : x))
      : [...ps, p]))

  // 行内「测试」：测该供应商首个模型是否可用（未加模型则提示）
  const testProvider = async (i: number) => {
    const p = providers[i]
    if (!p.models.length) { setRowTest((t) => ({ ...t, [i]: "未添加模型" })); return }
    setRowTest((t) => ({ ...t, [i]: "测试中…" }))
    const key = p.has_key && !(p.api_key ?? "").trim()
      ? { name: p.name }
      : { base_url: p.base_url, api_key: p.api_key ?? "" }
    try {
      const r = await api.testLlmModel({
        ...key, format: p.format, model: p.models[0], proxy: p.proxy ?? null })
      setRowTest((t) => ({ ...t, [i]: r.ok ? "✓ 可用" : `✗ ${r.error ?? "不可用"}` }))
    } catch (e) {
      setRowTest((t) => ({ ...t, [i]: `✗ ${String(e)}` }))
    }
  }

  const save = async () => {
    setError(null)
    try {
      const r = await api.saveLlmProviders(
        providers.map(({ has_key: _h, ...rest }) => rest),
        defaultProvider ?? undefined)
      setProviders(r.providers)
      setDef(r.default)
      setDefaultProvider(r.default_provider ?? null)
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
    } catch (e) {
      setError(String(e))
    }
  }

  const defaultModel = def ? `${def.provider} / ${def.model}` : "未设置"
  return (
    <div className="llm-page">
      <div className="llm-overview">
        <div><span className="eyebrow">MODEL ROUTING / PROVIDERS</span><h2>模型供应商</h2><p>管理连接、模型优先级和会话默认路由。</p></div>
        <div className="llm-overview-actions"><div className="llm-file-label">config/providers.json</div><Saved saved={saved} /><Button size="sm" onClick={save}>保存配置</Button></div>
      </div>
      <div className="llm-default-card"><div className="llm-default-mark">◎</div><div><span className="llm-kicker">全局默认模型</span><strong>{defaultModel}</strong><small>用于新建会话、编排器和分类器</small></div><label className="llm-default-select"><span>默认供应商</span><select value={defaultProvider ?? ""} onChange={(e) => setDefaultProvider(e.target.value || null)}><option value="">自动选择</option>{providers.filter((p) => p.enabled).map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}</select></label></div>
      {error && <div className="llm-error">{error}</div>}
      <ScrollArea className="llm-scroll"><div className="llm-provider-list">
        {providers.map((p, i) => <div key={i} className={cn("llm-provider-card", !p.enabled && "is-disabled", p.name === defaultProvider && "is-default")}>
          <div className="llm-provider-header">
            <div className="llm-order"><button onClick={() => move(i, -1)} aria-label="上移">↑</button><button onClick={() => move(i, 1)} aria-label="下移">↓</button></div>
            <span className={cn("llm-provider-dot", p.enabled && "is-on")} />
            <div className="llm-provider-title">
              <span className="llm-provider-name">{p.name || "未命名供应商"}</span>
              <div className="llm-provider-sub">
                <span className="truncate font-mono">{p.base_url || "未填接口地址"}</span>
                <span>·</span>
                <span className="truncate font-mono">{p.models[0] ?? "未添加模型"}{p.models.length > 1 ? ` +${p.models.length - 1}` : ""}</span>
              </div>
            </div>
            <Badge variant="outline" className="shrink-0 text-[9px]">{FORMAT_LABEL[p.format]}</Badge>
            {p.name === defaultProvider && <Badge className="shrink-0 text-[9px]">默认</Badge>}
            {/* 视觉能力（vision 通道）：勾了「图片输入」才有此标——让用户一眼看出
                为什么模型不调 view_image（未声明的供应商按「不支持」处理）。 */}
            {p.vision && <Badge variant="outline" className="shrink-0 text-[9px]">视觉</Badge>}
            {!p.enabled && <span className="llm-status shrink-0">已停用</span>}
            {rowTest[i] && (
              <span className={cn("llm-test-state shrink-0", rowTest[i].startsWith("✓") && "is-ok",
                rowTest[i].startsWith("✗") && "is-fail")}>{rowTest[i]}</span>
            )}
            <button className="llm-row-act" onClick={() => void testProvider(i)}>测试</button>
            <button className="llm-row-act" onClick={() => setEditIdx(i)}>编辑</button>
            <label className="llm-switch"><input type="checkbox" checked={p.enabled} onChange={(e) => patch(i, { enabled: e.target.checked })} /><span /></label>
            <button className="llm-delete" onClick={() => setProviders((ps) => ps.filter((_, j) => j !== i))}>删除</button>
          </div>
        </div>)}
        <Button size="sm" variant="outline" className="llm-add-provider" onClick={() => setEditIdx(-1)}>+ 添加供应商</Button>
      </div></ScrollArea>
      <button className="llm-help-toggle" onClick={() => setHelpOpen((open) => !open)}>配置说明 {helpOpen ? "⌃" : "⌄"}</button>{helpOpen && <div className="llm-help"><p>第一个启用供应商的第一个模型会作为自动默认模型。</p><p>API 密钥只显示配置状态，不会回显真实值。</p><p>上下文 K 用于控制会话历史预算和摘要压缩阈值，留空使用默认值。</p></div>}
      <ProviderDialog
        open={editIdx !== null}
        onOpenChange={(v) => { if (!v) setEditIdx(null) }}
        initial={editIdx != null && editIdx >= 0 ? (providers[editIdx] ?? null) : null}
        onSave={saveProvider} />
    </div>
  )
}
