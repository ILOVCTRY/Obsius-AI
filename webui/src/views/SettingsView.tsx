import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/lib/api"
import type {
  AgentTool, Expert, ExpertBody, LlmProvider, McpServer, SkillDef,
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

/** 自动保存路径专用：packs-changed 会让 DoctorBar 重跑整棵 packs 的 doctor 体检
 *  （磁盘扫描级开销）。改动即存下一次输入会触发多次，必须并流到「停手 2s 后一次」。 */
let packsChangedTimer: number | null = null
function packsChangedSoon() {
  if (packsChangedTimer != null) window.clearTimeout(packsChangedTimer)
  packsChangedTimer = window.setTimeout(() => { packsChangedTimer = null; packsChanged() }, 2000)
}

// doctor 跳转指令（跨 tab/轨选中具体专家或技能）
interface ExpertFocus { id: string; n: number }
interface SkillFocus { name: string; n: number }

// 设置页（DESIGN.md §12 页面 6-9）：专家池（packs/experts/ 单文件池）/ 技能库（packs/kb/skills 扁平）/
// 安全红线（轨红线/平台规则/评级）/ 模型 / MCP。
// 修改经 core API 写 packs 文件（写入前留 .history）；改动在下次开窗生效，在跑会话不受影响。

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

/** 专家表单草稿：专家页所有可编辑字段的本地副本（自动保存的提交源）。
 *
 *  单独建模而不是一堆 useState，是因为自动保存必须**原子地**算出「这一笔改动的
 *  完整快照」——全字段覆写要求每次提交都带上所有字段，分散的 state 在连打时会
 *  各自落后一拍，拼出来的 yaml 就是几个不同时刻的混合体。 */
interface ExpertDraft {
  name: string
  description: string
  persona: string
  tracks: string[]
  skills: string[] | null
  tools: string[] | null
  mcpServers: string[] | null
  steps: string
}
const EMPTY_DRAFT: ExpertDraft = {
  name: "", description: "", persona: "", tracks: [],
  skills: null, tools: null, mcpServers: null, steps: "",
}

function draftFrom(e: Expert): ExpertDraft {
  return {
    // 显示名等于 id 时视为「未命名」，表单留空（否则用户会看到一个没改过的 id 文本）
    name: e.name && e.name !== e.id ? e.name : "",
    description: e.description ?? "",
    persona: e.persona ?? "",
    tracks: e.tracks ?? [],
    // null 必须原样保留：null = 全量（后端不落键），[] = 一个都不启用。
    // 读成 [] 再保存会把「未限制」写成「全部禁用」。
    skills: e.skills ?? null,
    tools: e.tools ?? null,
    mcpServers: e.mcp_servers ?? null,
    steps: e.max_steps != null ? String(e.max_steps) : "",
  }
}

/** 草稿 → 请求体（全字段覆写：空值 = null 不落键）。 */
function bodyFrom(d: ExpertDraft, e: Expert): ExpertBody {
  const stepsTrim = d.steps.trim()
  return {
    name: d.name.trim() || null,
    description: d.description.trim() || null,
    persona: d.persona.trim() || null,
    tracks: d.tracks.length ? d.tracks : null,
    // 能力白名单三件套按草稿原样提交：null = 不限制（后端不落键）；
    // [] = 明确一个都不启用（后端落 `key: []`）；非空 = 勾选清单。
    skills: d.skills,
    // kb 方向两态（null/[] 都=不限），表单不编辑但必须原样回传，否则覆写会清空。
    knowledge: e.knowledge ?? null,
    // task_types 已退役（2026-10-21）：不提交——全字段覆写时后端不再序列化该键，
    // 存量 yaml 里的旧值随之被清掉（这是刻意行为）。
    // 工具白名单：勾选即开关（controller-tool-select，2026-10-20）。控制器与子专家
    // 同口径——null = 不落键（控制器此时回落代码默认面），[] = 明确一个都不开。
    tools: d.tools,
    mcp_servers: d.mcpServers,
    max_steps: stepsTrim ? Number(stepsTrim) : null,
    // 轨变体只读展示，但必须原样回传——否则全字段覆写会把 variant_<轨>_<字段> 抹掉。
    variants: Object.keys(e.variants ?? {}).length ? e.variants : null,
  }
}

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
  const [draft, setDraft] = useState<ExpertDraft>(EMPTY_DRAFT)
  // 表单从磁盘重灌的触发器：只在「选中项变了」或「显式重新加载」时递增。
  // **不能拿 `cur` 当依赖**——自动保存会更新左列表（对象换了引用），依赖 cur 的
  // effect 会在打字过程中把表单重置回盘上内容，表现为「输入框自己跳字」。
  const [resync, setResync] = useState(0)
  const [skillCatalog, setSkillCatalog] = useState<SkillDef[]>([])
  const [toolCatalog, setToolCatalog] = useState<AgentTool[]>([])
  const [toolGroups, setToolGroups] = useState<string[]>([])
  const [mcpCatalog, setMcpCatalog] = useState<McpServer[]>([])
  const [saveState, setSaveState] = useState<SaveState>("idle")
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
      // 列表换了 → 表单要按盘上最新内容重灌（保存/回滚/新建/删除后都必须）
      setResync((n) => n + 1)
    }).catch(() => {})
    return () => { alive = false }
  }, [])
  useEffect(() => reload(), [reload])
  useEffect(() => {
    api.skills().then(setSkillCatalog).catch(() => {})
    api.agentTools().then((r) => { setToolCatalog(r.tools); setToolGroups(r.groups ?? []) }).catch(() => {})
    api.mcpServers().then((r) => setMcpCatalog(r.servers)).catch(() => {})
  }, [])

  const cur = experts.find((e) => e.id === selected)
  const curRef = useRef<Expert | undefined>(cur)
  curRef.current = cur
  const selectedRef = useRef<string | null>(selected)
  selectedRef.current = selected
  // 切专家的统一入口（会先冲出待写改动）；handler 在下面定义，这里经 ref 调用
  const selectRef = useRef<(id: string) => void>(() => {})

  // doctor 跳转：选中指定专家（走 select 以便顺手落盘上一家的待写改动）
  useEffect(() => {
    if (focus && focus.id !== selectedRef.current) selectRef.current(focus.id)
  }, [focus])

  // ---- 自动保存（2026-10-21 用户要求去掉「保存」按钮：改动即存）----
  //
  // 与 `LlmPane` 同一套纪律（串行链 + 本地状态是唯一真相 + 不回写表单），另加三条
  // 专家页特有的约束：
  //  1) **草稿在 ref 里同步推进**：`setDraft` 是异步的，输入连打时读 state 会拿到
  //     上一个字符的快照，落盘内容永远慢一拍。所有改动都先算进 `draftRef` 再 enqueue。
  //  2) **切换专家 / 卸载前必须 flush**：否则「敲完最后一个字立刻点另一个专家」或
  //     「直接关掉设置页」会丢掉最后 <400ms 的输入。
  //  3) **非法输入整次不落盘**：全字段覆写没有「只跳过这个字段」的余地，`max_steps`
  //     填了非正整数就别提交，明确说"未保存"而不是偷偷写个别的值。
  const draftRef = useRef<ExpertDraft>(EMPTY_DRAFT)
  const chainRef = useRef<Promise<void>>(Promise.resolve())
  const timerRef = useRef<number | null>(null)
  const savedTimer = useRef<number | null>(null)
  const scheduled = useRef<{ id: string; body: ExpertBody; seq: number } | null>(null)
  // 改动序号：用于判断「这次落盘之后用户又改了吗」。不判的话，请求在飞期间继续打字，
  // 响应回来会把「已保存 ✓」盖在**还有待写改动**的界面上——用户以为存了其实没有。
  const seqRef = useRef(0)
  // 待写草稿（专家 id → 最新草稿 + 它的改动序号）。
  // 快速 A→B→A 切换时，A 的 PUT 可能还在路上，此刻「从盘上重灌」读到的是**旧值**，
  // 界面会显示上一版内容、再被下一个请求覆盖——所以「有更新的草稿还没落盘」时，
  // 表单要读草稿而不是读盘。带 seq 是为了只清理**自己那一笔**：A 在飞期间用户又改了
  // A，响应回来不能把新草稿一起删掉。
  const pendingDraftRef = useRef<Map<string, { seq: number; draft: ExpertDraft }>>(new Map())

  const send = useCallback(async (id: string, body: ExpertBody, seq: number) => {
    // 只有「当前编辑对象」的状态变化才值得显示在顶部；切到别家之后，A 的响应回来
    // 不该改 B 的徽标。
    const isCurrent = () => selectedRef.current === id
    if (isCurrent()) setSaveState("saving")
    try {
      await api.updateExpert(id, body, true)
      // 这一笔已经落盘 → 若草稿没再被更新过（seq 仍是这一笔），就不再算「待写」，
      // 之后重灌读盘即可；被更新过则保留，等下一笔落盘再清。
      const pend = pendingDraftRef.current.get(id)
      const superseded = pend != null && pend.seq !== seq
      if (!superseded) pendingDraftRef.current.delete(id)
      // 落盘期间又改了 → 保持 pending，别盖「已保存」
      if (!superseded && isCurrent()) {
        setSaveState("saved")
        if (savedTimer.current != null) window.clearTimeout(savedTimer.current)
        savedTimer.current = window.setTimeout(() => setSaveState("idle"), 1600)
      }
      // 左列表的显示名/轨标签要跟着变，但**不回写表单**（回写会打断正在打的字），
      // 也不整表重拉（重拉会让 cur 换引用，触发无谓重渲染）。
      setExperts((es) => es.map((e) => (e.id === id
        ? { ...e, name: body.name, description: body.description, persona: body.persona,
            tracks: body.tracks ?? [], max_steps: body.max_steps }
        : e)))
      packsChangedSoon()
    } catch (e) {
      // 校验失败（422：未知技能 / 未知方向 / 步数）要显式暴露，不能静默。
      // 失败时保留待写草稿：用户切回来还能看到并改掉那个非法值。
      if (isCurrent()) {
        setErr(String(e))
        setSaveState("idle")
      }
    }
  }, [])

  const queue = useCallback((id: string, body: ExpertBody, seq: number) => {
    chainRef.current = chainRef.current.then(() => send(id, body, seq))
  }, [send])

  /** 把攒着的改动立刻发出去（停止防抖计时）。 */
  const flush = useCallback(() => {
    if (timerRef.current != null) { window.clearTimeout(timerRef.current); timerRef.current = null }
    const p = scheduled.current
    scheduled.current = null
    if (p) queue(p.id, p.body, p.seq)
  }, [queue])
  const flushRef = useRef(flush)
  flushRef.current = flush

  /** 改动 → 记入草稿 → 防抖落盘。`immediate` 用于切换前/卸载前的强制冲出。 */
  const commit = useCallback((next: ExpertDraft, immediate = false) => {
    const e = curRef.current
    if (!e) return
    draftRef.current = next
    setDraft(next)
    const stepsTrim = next.steps.trim()
    if (stepsTrim && !/^\d+$/.test(stepsTrim)) {
      // 非法步数：**整次不落盘**（全字段覆写必须带一个合法的 max_steps，不能只跳过它）
      if (timerRef.current != null) { window.clearTimeout(timerRef.current); timerRef.current = null }
      scheduled.current = null
      setSaveState("idle")
      setErr("最大步数必须是正整数——本次改动未保存")
      return
    }
    setErr(null)
    const seq = ++seqRef.current
    pendingDraftRef.current.set(e.id, { seq, draft: next })
    scheduled.current = { id: e.id, body: bodyFrom(next, e), seq }
    if (timerRef.current != null) window.clearTimeout(timerRef.current)
    if (immediate) { flush(); return }
    setSaveState("pending")
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      const p = scheduled.current
      scheduled.current = null
      if (p) queue(p.id, p.body, p.seq)
    }, 400)
  }, [flush, queue])

  const edit = useCallback((patch: Partial<ExpertDraft>) => {
    commit({ ...draftRef.current, ...patch })
  }, [commit])

  /** 切换选中项：先把上一家待写的改动冲出去，再切。 */
  const select = useCallback((id: string) => {
    if (id === selectedRef.current) return
    flush()
    setSelected(id)
  }, [flush])
  selectRef.current = select

  // 卸载（关掉设置页 / 切 tab）时冲出待写改动，别把最后几个字留在内存里
  useEffect(() => () => flushRef.current(), [])

  // 表单重灌：选中项变了，或显式 reload（保存/回滚/新建/删除之后）。
  // 有未落盘草稿时读草稿——盘上还是旧的，读盘会让界面回退到上一版。
  useEffect(() => {
    const e = experts.find((x) => x.id === selected)
    if (!e) return
    const d = pendingDraftRef.current.get(e.id)?.draft ?? draftFrom(e)
    draftRef.current = d
    setDraft(d)
    setErr(null)
    setSaveState("idle")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, resync])


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

  const toggleTrack = (t: string) =>
    edit({ tracks: draftRef.current.tracks.includes(t)
      ? draftRef.current.tracks.filter((x) => x !== t)
      : [...draftRef.current.tracks, t] })

  const doDelete = async () => {
    if (!confirmDel) return
    setBusy(true)
    setErr(null)
    // 删的就是当前这家：待写的自动保存要**丢弃**而不是冲出——冲出会与 DELETE 抢同一
    // 个文件（PUT 落在 DELETE 之后就是 404，落在之前则白写一次再被删）。
    if (confirmDel === selectedRef.current) {
      if (timerRef.current != null) { window.clearTimeout(timerRef.current); timerRef.current = null }
      scheduled.current = null
    }
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
                onClick={() => select(e.id)}
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
              {/* 没有「保存」按钮了（2026-10-21 用户要求改动即存）：这行小字是唯一的
                  落盘反馈。三态覆盖「正在敲但还没发」「请求在飞」「已落盘」。 */}
              <span className={cn("text-[10px] transition-opacity",
                saveState === "pending" && "text-muted-foreground",
                saveState === "saving" && "text-muted-foreground",
                saveState === "saved" && "text-primary")}>
                {saveState === "pending" ? "待保存…"
                  : saveState === "saving" ? "保存中…"
                  : saveState === "saved" ? "已保存 ✓" : "改动即存"}
              </span>
              {cur.file && <HistoryButton file={cur.file} onRolledBack={reload} />}
            </div>
            <label className="text-[10px] text-muted-foreground">显示名（可中文；留空 = 用专家 id {cur.id}；界面各处展示用）</label>
            <Input value={draft.name} onChange={(e) => edit({ name: e.target.value })} className="text-xs"
                   placeholder={`如：${cur.id.replace(/^_/, "")}`} />
            <label className="text-[10px] text-muted-foreground">职责 description（编排开窗目录与组队列表的一句话说明）</label>
            <Input value={draft.description} onChange={(e) => edit({ description: e.target.value })} className="text-xs"
                   placeholder="如：外网打点与入口利用" />
            <label className="text-[10px] text-muted-foreground">人设 persona（注入系统提示；轨变体可按轨覆写）</label>
            {/* shrink-0：本列是 flex-col，textarea 默认 flex-shrink:1 会被压回 min-h-16，
                表现为「拖拽句柄拖大了又弹回去」。resize-y 显式声明可纵向拖拽。 */}
            <Textarea value={draft.persona} onChange={(e) => edit({ persona: e.target.value })}
                      className="min-h-16 shrink-0 resize-y text-xs" />
            <div>
              <label className="text-[10px] text-muted-foreground">服务轨 tracks（不选 = 全轨通用）</label>
              <div className="mt-1 flex flex-wrap gap-1">
                {(tax?.tracks ?? []).map((t) => (
                  <button key={t.name}
                          onClick={() => toggleTrack(t.name)}
                          className={cn("rounded border px-2 py-0.5 text-[11px]",
                            draft.tracks.includes(t.name)
                              ? "border-primary/50 bg-primary/10 text-primary"
                              : "text-muted-foreground hover:bg-accent/50")}>
                    {t.label || trackLabel(t.name)}
                  </button>
                ))}
              </div>
            </div>
            <CheckList title="技能白名单（勾选启动）"
                       items={skillCatalog.map((s) => ({ id: s.name, label: s.name, detail: s.description, group: s.group }))}
                       selected={draft.skills} onChange={(v) => edit({ skills: v })} groupOrder={SKILL_GROUP_ORDER} />
            <p className="text-[10px] text-muted-foreground">
              kb 方向 knowledge（两态：空 = 不限方向；保存时原样保留，改方向请直接编辑 yaml）
              ：<code className="font-mono">{cur.knowledge?.length ? cur.knowledge.join(", ") : "不限"}</code>
            </p>
            {/* task_types 输入框已移除（2026-10-21）：任务认领机制 2026-10-06
                退役后该字段零运行时消费，留着只会让人对着无效字段做决策。 */}
            {/* 工具白名单（controller-tool-select，2026-10-20 定稿）：
                控制器类智能体（chat-orchestrator / aggressive / artex / ctf-controller）
                的 yaml `tools` 现在是**真实开关**——勾了就下发、没勾就既不下发 schema
                也会在执行侧被拒。候选清单额外并入后端下发的 `tools_runtime`
                （`artex_*` / `ctf_*` / `todo_write` / `call_expert` 不在通用目录里），
                否则这些工具永远勾不到，勾选又退化成固定清单。 */}
            <CheckList title="工具白名单（勾选启动）"
                       items={toolItems}
                       selected={draft.tools} onChange={(v) => edit({ tools: v })} groupOrder={toolGroups} />
            {cur.tools_controller && (
              <p className="text-[10px] leading-relaxed text-muted-foreground">
                这是控制器类智能体：上方清单已并入后端下发的专属工具
                （<code className="font-mono">artex_*</code> /
                <code className="font-mono">ctf_dispatch/review/inspect</code> /
                <code className="font-mono">todo_write</code> /
                <code className="font-mono">call_expert</code>）。
                勾选即时生效；<code className="font-mono">declare_intent</code> 等
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
                       selected={draft.mcpServers} onChange={(v) => edit({ mcpServers: v })} groupOrder={MCP_DOMAIN_ORDER} />
            <label className="text-[10px] text-muted-foreground">最大步数 max_steps（正整数；留空 = 不设）</label>
            <Input value={draft.steps} onChange={(e) => edit({ steps: e.target.value })} className="w-48 font-mono text-xs" inputMode="numeric" placeholder="200" />
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
                          onCreated={(id) => { flush(); setSelected(id); reload() }} />
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

// ---------- LLM 供应商：增删改 + 获取模型（改动即存，首个启用=默认） ----------
//
// 2026-10-21 Nan 要求：**去掉「保存配置」按钮，每次操作直接落盘**。自动保存要守住
// 三条语义，否则比手点保存更容易坏：
//  1) **串行队列 + 只落最后一份快照**：连点两次启停会发两个 PUT，乱序返回会把新值
//     写成旧值；用 ref 里的 promise 链保证顺序，被更新改动取代的中间快照直接跳过
//     （最新快照已包含全部累积改动）。
//  2) **本地状态是唯一真相，成功后不回写整表**：回写 `r.providers` 会带来两个坑——
//     在飞的改动被上一笔响应覆盖（开关回弹）；以及 `ProviderDialog` 的 `initial`
//     换了引用会触发它的重置 effect，把用户正在填的表单冲掉。
//  3) **保住后端的硬约束**：`save()` 拒绝「一个启用供应商都没有」和「默认供应商
//     已停用/不存在」，这两条在 UI 层就先拦/先顺延，否则会出现「界面变了、盘上没变」。
/** 落盘状态三态 + pending：
 *  - `pending`：改动已进草稿、还在防抖计时里（用户可能继续打字）
 *  - `saving` ：请求在飞
 *  - `saved`  ：已落盘（1.6s 后回 idle）
 *  - `idle`   ：无待写改动（徽标文案回落「改动即存」） */
type SaveState = "idle" | "pending" | "saving" | "saved"

function LlmPane() {
  const [providers, setProviders] = useState<LlmProvider[]>([])
  const [def, setDef] = useState<{ provider: string; model: string } | null>(null)
  // 可选默认供应商（全局默认 = 它的第一个模型；未设置 = 第一个启用供应商）
  const [defaultProvider, setDefaultProvider] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>("idle")
  const [error, setError] = useState<string | null>(null)
  const [helpOpen, setHelpOpen] = useState(false)
  // 弹窗目标（2026-10-04 编辑改弹窗）：null=关闭 / -1=新增 / >=0=编辑该索引的供应商
  const [editIdx, setEditIdx] = useState<number | null>(null)
  // 行内「测试」结果（按供应商索引）：测该供应商首个模型是否可用
  const [rowTest, setRowTest] = useState<Record<number, string>>({})

  const saveChain = useRef<Promise<void>>(Promise.resolve())
  const saveSeq = useRef(0)
  const savedTimer = useRef<number | null>(null)

  useEffect(() => {
    api.llmProviders().then((r) => {
      setProviders(r.providers)
      setDef(r.default)
      setDefaultProvider(r.default_provider ?? null)
    }).catch(() => {})
    return () => { if (savedTimer.current != null) window.clearTimeout(savedTimer.current) }
  }, [])

  /** 排队落盘：`next` 显式传入（setState 异步，别在保存里读 state）。 */
  const persist = useCallback((next: LlmProvider[], defProvider: string | null) => {
    const seq = ++saveSeq.current
    setSaveState("saving")
    setError(null)
    saveChain.current = saveChain.current.then(async () => {
      if (seq !== saveSeq.current) return  // 已被更新的改动取代，只落最后一份快照
      try {
        const r = await api.saveLlmProviders(
          next.map(({ has_key: _h, ...rest }) => rest),
          defProvider ?? undefined)
        if (seq !== saveSeq.current) return
        setDef(r.default)
        setSaveState("saved")
        if (savedTimer.current != null) window.clearTimeout(savedTimer.current)
        savedTimer.current = window.setTimeout(() => setSaveState("idle"), 1600)
      } catch (e) {
        if (seq !== saveSeq.current) return
        setError(String(e))
        setSaveState("idle")
      }
    })
  }, [])

  /** 先改 UI 再排队写盘——所有改动都必须走这里，别单独 setProviders。 */
  const commit = (next: LlmProvider[], defProvider: string | null = defaultProvider) => {
    setProviders(next)
    persist(next, defProvider)
  }

  /** 默认供应商顺延：原默认被停用/删除/改名时，落到剩下的第一个启用供应商。 */
  const nextDefault = (list: LlmProvider[]) => list.find((p) => p.enabled)?.name ?? null

  const toggleEnabled = (i: number, enabled: boolean) => {
    const p = providers[i]
    if (!enabled && p.enabled && providers.filter((x) => x.enabled).length <= 1) {
      setError("至少保留一个启用供应商，否则 Agent 无模型可用")
      return
    }
    const next = providers.map((s, j) => (j === i ? { ...s, enabled } : s))
    const dp = !enabled && p.name === defaultProvider
      ? nextDefault(next) : defaultProvider
    setDefaultProvider(dp)
    commit(next, dp)
  }

  const move = (i: number, delta: number) => {
    const j = i + delta
    if (j < 0 || j >= providers.length) return
    const next = [...providers]
    ;[next[i], next[j]] = [next[j], next[i]]
    commit(next)
  }

  const removeProvider = (i: number) => {
    if (providers.length <= 1) {
      setError("至少保留一个启用供应商，否则 Agent 无模型可用")
      return
    }
    const next = providers.filter((_, j) => j !== i)
    const dp = providers[i].name === defaultProvider ? nextDefault(next) : defaultProvider
    setDefaultProvider(dp)
    commit(next, dp)
  }

  // 弹窗保存：editIdx<0 或 null 时新增（追加末尾），否则覆写该索引
  const saveProvider = (p: LlmProvider) => {
    const idx = editIdx != null && editIdx >= 0 ? editIdx : null
    // 本次填了 key 就标记「已配置」，否则行内「测试」会拿空 key 走 base_url 分支
    const saved: LlmProvider = { ...p, has_key: p.has_key || !!p.api_key?.trim() }
    const next = idx == null
      ? [...providers, saved]
      : providers.map((x, j) => (j === idx ? saved : x))
    // 改名会打断 default_provider 的名字绑定（后端按名命中）→ 跟着改，否则保存被拒
    const dp = idx != null && providers[idx].name === defaultProvider ? saved.name : defaultProvider
    setDefaultProvider(dp)
    commit(next, dp)
  }

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

  const defaultModel = def ? `${def.provider} / ${def.model}` : "未设置"
  return (
    <div className="llm-page">
      <div className="llm-overview">
        <div><span className="eyebrow">MODEL ROUTING / PROVIDERS</span><h2>模型供应商</h2><p>管理连接、模型优先级和会话默认路由。</p></div>
        {/* 没有「保存配置」按钮了：这行小字是唯一的落盘反馈（改动即存） */}
        <div className="llm-overview-actions">
          <div className="llm-file-label">config/providers.json</div>
          <span className={cn("llm-save-state", saveState === "saving" && "is-saving",
            saveState === "saved" && "is-saved")}>
            {saveState === "saving" ? "保存中…" : saveState === "saved" ? "已保存 ✓" : "改动即存"}
          </span>
        </div>
      </div>
      <div className="llm-default-card"><div className="llm-default-mark">◎</div><div><span className="llm-kicker">全局默认模型</span><strong>{defaultModel}</strong><small>用于新建会话、编排器和分类器</small></div><label className="llm-default-select"><span>默认供应商</span><select value={defaultProvider ?? ""} onChange={(e) => { const v = e.target.value || null; setDefaultProvider(v); persist(providers, v) }}><option value="">自动选择</option>{providers.filter((p) => p.enabled).map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}</select></label></div>
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
            <label className="llm-switch"><input type="checkbox" checked={p.enabled} onChange={(e) => toggleEnabled(i, e.target.checked)} /><span /></label>
            <button className="llm-delete" onClick={() => removeProvider(i)}>删除</button>
          </div>
        </div>)}
        <Button size="sm" variant="outline" className="llm-add-provider" onClick={() => setEditIdx(-1)}>+ 添加供应商</Button>
      </div></ScrollArea>
      <button className="llm-help-toggle" onClick={() => setHelpOpen((open) => !open)}>配置说明 {helpOpen ? "⌃" : "⌄"}</button>{helpOpen && <div className="llm-help"><p>所有改动（启停 / 排序 / 增删 / 编辑 / 默认供应商）立即写入 config/providers.json，无需手动保存。</p><p>第一个启用供应商的第一个模型会作为自动默认模型。</p><p>API 密钥只显示配置状态，不会回显真实值。</p><p>上下文 K 用于控制会话历史预算和摘要压缩阈值，留空使用默认值。</p></div>}
      <ProviderDialog
        open={editIdx !== null}
        onOpenChange={(v) => { if (!v) setEditIdx(null) }}
        initial={editIdx != null && editIdx >= 0 ? (providers[editIdx] ?? null) : null}
        onSave={saveProvider} />
    </div>
  )
}
