import { useCallback, useEffect, useRef, useState } from "react"
import { Group, Panel, Separator } from "react-resizable-panels"
import { api } from "@/lib/api"
import type {
  Expert, LlmProvider,
  SkillDef, SkillDetail, SkillVocab,
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
  ExpertCreateDialog, SkillCreateDialog, type SkillSource,
} from "@/components/settings/CreateDialogs"
import { SkillEditor, SKILL_MD_PREFIX, type SkillEditorHandle } from "@/components/settings/SkillEditor"
import { KbView, type KbFocus } from "@/components/settings/KbView"
import { MarkdownOutline } from "@/components/settings/MarkdownOutline"
import { ProposalsPane } from "@/components/settings/ProposalsPane"
import { IntelSourcePane } from "@/components/settings/IntelSourcePane"
import { RulesPane, type RuleFocus } from "@/components/settings/RulesPane"
import { AgentToolsPane } from "@/components/settings/AgentToolsPane"
import { McpPane } from "@/components/McpPane"
import { parseSkill } from "@/lib/skillfm"
import { cn } from "@/lib/utils"
import {
  Bot, Cable, ChevronRight, CircleGauge,
  Gauge, GitPullRequest, Network, Radio, Search, Settings2, ShieldCheck,
  Sparkles, Wrench,
} from "lucide-react"

/** packs 文件写操作后通知 doctor 条刷新（30s 轮询之外的即时通道） */
function packsChanged() {
  window.dispatchEvent(new Event("packs-changed"))
}

// doctor 跳转指令（跨 tab/轨/包选中具体专家或技能）
interface ExpertFocus { id: string; n: number }
interface SkillFocus { source: SkillSource; pack: string; name: string; n: number }

// 设置页（DESIGN.md §12 页面 6-9）：专家池（packs/experts/ 单文件池）/ Skill（轨+能力包，含路由试算）/
// 红线（轨+能力包）/ owners（轨）/ 模型 / MCP。
// 修改经 core API 写 packs 文件（写入前留 .history）；改动在下次开窗生效，在跑会话不受影响。

function Saved({ saved }: { saved: boolean }) {
  return <span className={cn("text-[10px] transition-opacity", saved ? "text-primary opacity-100" : "opacity-0")}>已保存 ✓</span>
}

function DangerNote({ children }: { children: React.ReactNode }) {
  return <p className="text-[10px] text-(--status-approval)">{children}</p>
}

const selectCls = "rounded border bg-background px-1.5 py-0.5 text-xs [color-scheme:dark] [&>option]:bg-popover [&>option]:text-popover-foreground"

export function SettingsView({ nav, pid }: {
  nav?: { tab: string; n: number; skill?: { source: SkillSource; pack: string; name: string } } | null
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
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState("")
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
        if (t.capabilities[0]) setCap(t.capabilities[0].name)
      }
    }).catch(() => {})
  }, [])

  // 从项目内打开（pid 存在）：轨/能力包缺省跟随当前项目——不再每次都显示 CTF
  useEffect(() => {
    if (!pid || navAppliedRef.current) return
    projectAppliedRef.current = true
    api.getProject(pid).then((p) => {
      // 守卫须在异步回调内按维度复查：请求在飞期间深链可能已落地（nav effect 晚于入口检查）；
      // 深链只钉一维，另一维仍按项目补（cap 深链时 track 仍应=pentest）
      if (p.track && !navPinnedRef.current.track) setTrack(p.track)
      // 用盘上绑定包：effective 在 _generalist 项目=全部包，[0] 会错落到 binary
      const caps = p.capabilities_bound?.length ? p.capabilities_bound : p.capabilities
      if (caps?.length && !navPinnedRef.current.cap) setCap(caps[0])
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

  // 跨视图跳指定 tab；带 skill 时（直播间 skill.routed 双击）同步轨/包并深链选中该技能
  useEffect(() => {
    if (!nav?.n) return
    setTab(nav.tab)
    const s = nav.skill
    if (s) {
      navAppliedRef.current = true
      navPinnedRef.current = s.source === "track" ? { track: true } : { cap: true }
      if (s.source === "track") setTrack(s.pack); else setCap(s.pack)
      setSkillFocus({ source: s.source, pack: s.pack, name: s.name, n: nav.n })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav?.n])

  // doctor 详情跳转：按 target 路径切 tab/轨/包并选中具体条目
  const navigateTarget = (target: string): boolean => {
    let m: RegExpMatchArray | null
    if ((m = target.match(/^experts\/([\w.-]+)\.yaml$/))) {
      setTab("experts")
      setExpertFocus({ id: m[1], n: Date.now() })
      return true
    }
    if ((m = target.match(/^tracks\/([\w.-]+)\/skills\/([\w.-]+)\/SKILL\.md$/))) {
      setTrack(m[1]); setTab("skills")
      setSkillFocus({ source: "track", pack: m[1], name: m[2], n: Date.now() })
      return true
    }
    if ((m = target.match(/^capabilities\/([\w.-]+)\/skills\/([\w.-]+)\/SKILL\.md$/))) {
      setCap(m[1]); setTab("skills")
      setSkillFocus({ source: "cap", pack: m[1], name: m[2], n: Date.now() })
      return true
    }
    if ((m = target.match(/^kb\/([\w.-]+)\/(.+\.(?:md|py|txt|json))$/))) {
      setCap(m[1]); setTab("kb")
      setKbFocus({ path: m[2], n: Date.now() })
      return true
    }
    if ((m = target.match(/^(tracks|capabilities)\/([\w.-]+)\/rules\/redlines\.md$/))) {
      if (m[1] === "tracks") setTrack(m[2]); else setCap(m[2])
      setTab("rules")
      setRuleFocus({ kind: m[1] === "tracks" ? "track-redlines" : "cap-redlines", name: m[2], n: Date.now() })
      return true
    }
    if ((m = target.match(/^tracks\/([\w.-]+)\/rules\/rating\/([\w.-]+)\.md$/))) {
      setTrack(m[1]); setTab("rules")
      setRuleFocus({ kind: "rating", name: m[1], tag: m[2], n: Date.now() })
      return true
    }
    return false
  }

  const navGroups: { label: string; items: [string, string, typeof Bot][] }[] = [
    { label: "智能系统", items: [["experts", "专家池", Bot], ["matrix", "能力矩阵", CircleGauge], ["advisor", "策略顾问", Sparkles]] },
    { label: "安全控制", items: [["rules", "安全红线", ShieldCheck], ["gateway", "访问网关", Network], ["tools", "执行工具", Wrench]] },
    { label: "基础设施", items: [["llm", "模型供应商", Gauge], ["mcp", "MCP 连接", Cable], ["intel", "情报来源", Radio]] },
    { label: "工作流", items: [["proposals", "变更提案", GitPullRequest]] },
  ]
  const activeLabel = navGroups.flatMap((g) => g.items).find(([key]) => key === tab)?.[1] ?? "设置"
  const searchResults = navGroups.flatMap((group) => group.items.map(([key, label]) => ({ key, label, group: group.label }))).filter((item) => {
    const query = searchTerm.trim().toLowerCase()
    return query && `${item.label} ${item.key} ${item.group}`.toLowerCase().includes(query)
  })

  return (
    <div className="settings-shell">
      <aside className="settings-sidebar">
        <div className="settings-brand"><div className="settings-brand-mark"><Settings2 size={15} /></div><div><strong>控制中心</strong><span>OBSIUS / SETTINGS</span></div></div>
        <div className="settings-context"><span className="eyebrow">当前上下文</span><div className="settings-context-row"><span className="settings-live-dot" /> <span>{trackLabel(track)}</span><span className="settings-slash">/</span><span>{capLabel(cap)}</span></div></div>
        <div className="settings-nav">
          {navGroups.map((group) => <div className="settings-nav-group" key={group.label}><span className="settings-nav-label">{group.label}</span>{group.items.map(([key, label, Icon]) => <button key={key} aria-label={label} className={cn("settings-nav-item", tab === key && "is-active")} onClick={() => setTab(key)}><Icon size={14} /><span>{label}</span>{key === "proposals" && pendingN > 0 && <b>{pendingN}</b>}<ChevronRight size={13} className="settings-nav-arrow" /></button>)}</div>)}
        </div>
        <div className="settings-sidebar-footer"><span className="settings-live-dot" />配置服务正常<span className="settings-version">v0.8</span></div>
      </aside>
      <main className="settings-main">
        <header className="settings-header"><div><span className="eyebrow">OBSIUS / CONTROL CENTER</span><h1>{activeLabel}</h1></div><div className="settings-header-actions"><label className="settings-select"><span>场景</span><select value={track} onChange={(e) => setTrack(e.target.value)}>{(tax?.tracks ?? []).map((t) => <option key={t.name} value={t.name}>{t.label || trackLabel(t.name)}</option>)}</select></label><label className="settings-select"><span>能力包</span><select value={cap} onChange={(e) => setCap(e.target.value)}>{(tax?.capabilities ?? []).map((c) => <option key={c.name} value={c.name}>{c.label || capLabel(c.name)}</option>)}</select></label><div className={cn("settings-search", searchOpen && "is-open")}><Search size={15} /><input aria-label="搜索设置" placeholder="搜索设置" value={searchTerm} onChange={(e) => setSearchTerm(e.target.value)} onFocus={() => setSearchOpen(true)} onKeyDown={(e) => { if (e.key === "Escape") { setSearchTerm(""); setSearchOpen(false) } }} /><button className="settings-search-toggle" aria-label="打开搜索" onClick={() => { setSearchOpen((open) => !open); if (searchOpen) setSearchTerm("") }}><Search size={15} /></button>{searchOpen && searchTerm.trim() && <div className="settings-search-results">{searchResults.length ? searchResults.map((item) => <button key={item.key} onClick={() => { setTab(item.key); setSearchOpen(false) }}><span>{item.label}</span><small>{item.group}</small></button>) : <span className="settings-search-empty">没有匹配的设置</span>}</div>}</div></div></header>
        <div className="settings-notice"><ShieldCheck size={14} /><span>所有配置修改将写入历史版本，并在下次打开会话时生效。</span><span className="settings-notice-meta">SYNCED · JUST NOW</span></div>
        <DoctorBar onNavigate={navigateTarget} />
        <Tabs value={tab} onValueChange={setTab} className="settings-content min-h-0 flex-1 flex-col gap-0">
          <TabsList className="hidden"><TabsTrigger value={tab}>{activeLabel}</TabsTrigger></TabsList>
          <TabsContent value="experts" className="min-h-0 flex-1"><ExpertsPane tax={tax} focus={expertFocus} /></TabsContent>
          <TabsContent value="skills" className="min-h-0 flex-1"><SkillsPane track={track} cap={cap} focus={skillFocus} /></TabsContent>
          <TabsContent value="kb" className="min-h-0 flex-1"><KbView cap={cap} focus={kbFocus} /></TabsContent>
          <TabsContent value="matrix" className="min-h-0 flex-1"><MatrixPane tax={tax} track={track} onFocusSkill={(source, pack, name) => { if (source === "track") setTrack(pack); else setCap(pack); setTab("skills"); setSkillFocus({ source, pack, name, n: Date.now() }) }} /></TabsContent>
          <TabsContent value="rules" className="min-h-0 flex-1"><RulesPane track={track} cap={cap} focus={ruleFocus} pid={pid} /></TabsContent>
          <TabsContent value="advisor" className="min-h-0 flex-1"><AdvisorPane pid={pid} /></TabsContent>
          <TabsContent value="llm" className="min-h-0 flex-1"><LlmPane /></TabsContent><TabsContent value="mcp" className="min-h-0 flex-1"><McpPane /></TabsContent><TabsContent value="gateway" className="min-h-0 flex-1"><GatewayPane pid={pid} /></TabsContent><TabsContent value="tools" className="min-h-0 flex-1"><AgentToolsPane /></TabsContent><TabsContent value="intel" className="min-h-0 flex-1"><IntelSourcePane /></TabsContent><TabsContent value="proposals" className="min-h-0 flex-1"><ProposalsPane onPendingChange={setPendingN} /></TabsContent>
        </Tabs>
      </main>
    </div>
  )
}

// ---------- 专家池（expert-pool M3）：packs/experts/ 单文件池，全池列表 + 全字段覆写表单 ----------

const RUNTIME_LEVELS = ["", "host", "wsl", "docker", "sandbox"]
const NOISE_LEVELS = ["", "passive", "low", "medium", "high"]

function ExpertsPane({ tax, focus }: { tax: Taxonomy | null; focus: ExpertFocus | null }) {
  const [experts, setExperts] = useState<Expert[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [displayName, setDisplayName] = useState("")   // 中文显示名；留空 = 用专家 id
  const [description, setDescription] = useState("")
  const [persona, setPersona] = useState("")
  const [trackSel, setTrackSel] = useState<string[]>([]) // 空 = null = 服务全轨
  const [skills, setSkills] = useState("")       // 逗号分隔；空 = 不落键（全量专家语义）
  const [taskTypes, setTaskTypes] = useState("")
  const [tools, setTools] = useState("")
  const [noise, setNoise] = useState("")
  const [runtime, setRuntime] = useState("")
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

  // doctor 跳转：选中指定专家
  useEffect(() => {
    if (focus) setSelected(focus.id)
  }, [focus])

  const cur = experts.find((e) => e.id === selected)

  useEffect(() => {
    if (!cur) return
    setDisplayName(cur.name && cur.name !== cur.id ? cur.name : "")
    setDescription(cur.description ?? "")
    setPersona(cur.persona ?? "")
    setTrackSel(cur.tracks ?? [])
    setSkills((cur.skills ?? []).join(", "))
    setTaskTypes((cur.task_types ?? []).join(", "))
    setTools((cur.tools ?? []).join(", "))
    setNoise(cur.default_noise ?? "")
    setRuntime(cur.max_runtime ?? "")
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
      // 全字段覆写（表单即最终态）：空值=null 不落键（skills 缺键 = 全量专家语义）
      await api.updateExpert(cur.id, {
        name: displayName.trim() || null,
        description: description.trim() || null,
        persona: persona.trim() || null,
        tracks: trackSel.length ? trackSel : null,
        skills: skills.trim() ? toList(skills) : null,
        task_types: taskTypes.trim() ? toList(taskTypes) : null,
        tools: tools.trim() ? toList(tools) : null,
        default_noise: noise.trim() || null,
        max_runtime: runtime || null,
        max_steps: stepsTrim ? Number(stepsTrim) : null,
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
            <Textarea value={persona} onChange={(e) => setPersona(e.target.value)} className="min-h-16 text-xs" />
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
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-[10px] text-muted-foreground">默认噪声预算 default_noise</label>
                <select value={noise} onChange={(e) => setNoise(e.target.value)} className={cn(selectCls, "w-full")}>
                  {NOISE_LEVELS.map((n) => <option key={n} value={n}>{n || "（不设）"}</option>)}
                </select>
              </div>
              <div>
                <label className="text-[10px] text-muted-foreground">运行时软上限 max_runtime（只能比网关更严）</label>
                <select value={runtime} onChange={(e) => setRuntime(e.target.value)} className={cn(selectCls, "w-full")}>
                  {RUNTIME_LEVELS.map((n) => <option key={n} value={n}>{n || "（不设）"}</option>)}
                </select>
              </div>
            </div>
            <label className="text-[10px] text-muted-foreground">技能白名单 skills（逗号分隔；留空 = 不限定（全量专家，能力面=全部能力包））</label>
            <Input value={skills} onChange={(e) => setSkills(e.target.value)} className="font-mono text-xs" placeholder="recon-asset-enum, web-strike-entry" />
            <label className="text-[10px] text-muted-foreground">任务类型 task_types（Worker 认领过滤器；按各轨注册表并集体检；留空 = 不限）</label>
            <Input value={taskTypes} onChange={(e) => setTaskTypes(e.target.value)} className="font-mono text-xs" placeholder="recon, exploit" />
            <label className="text-[10px] text-muted-foreground">工具白名单 tools（逗号分隔；留空 = 不限制；complete/fail/finish 永远放行）</label>
            <Input value={tools} onChange={(e) => setTools(e.target.value)} className="font-mono text-xs" placeholder="bb_query, run_cmd" />
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

// ---------- Skill + 知识库（三栏：列表/树 · 编辑 · 试算+大纲） ----------

const HANDLE_CLS = "z-10 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary"

function HHandle() {
  return <Separator className={cn("h-full w-px shrink-0", HANDLE_CLS)} />
}
export function SkillsPane({ track, cap, focus }: {
  track: string; cap: string; focus: SkillFocus | null
}) {
  const [source, setSource] = useState<SkillSource>("cap")
  const packName = source === "cap" ? cap : track
  const [skills, setSkills] = useState<SkillDef[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [confirmDel, setConfirmDel] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [vocab, setVocab] = useState<SkillVocab | null>(null)
  const editorRef = useRef<SkillEditorHandle>(null)
  // 切包/切来源时在 render 期同步清空选中：避免详情 effect 拿旧选中对新包发一次 404
  const [prevPack, setPrevPack] = useState(packName)
  if (prevPack !== packName) {
    setPrevPack(packName)
    setSelected(null)
  }

  const listApi = source === "cap" ? api.capSkills : api.trackSkills
  const detailApi = source === "cap" ? api.capSkillDetail : api.trackSkillDetail
  const updateApi = source === "cap" ? api.updateCapSkill : api.updateTrackSkill

  const reload = useCallback(() => {
    // 竞态守卫：切包/切轨后旧响应晚到不得覆盖新列表（同 RolesPane）
    let alive = true
    listApi(packName).then((ss) => {
      if (!alive) return
      setSkills(ss)
      setSelected((cur) => cur && ss.some((s) => s.name === cur) ? cur : null)
    }).catch(() => { if (alive) setSkills([]) })
    return () => { alive = false }
  }, [listApi, packName])
  useEffect(() => reload(), [reload])
  useEffect(() => { api.skillsVocab().then(setVocab).catch(() => {}) }, [])

  // doctor/矩阵/直播间深链跳转：切到指定来源并选中技能
  // （守卫按 focus.source 对应的包比较——packName 依赖本 pane 的 source state，
  //   轨技能深链时 source 仍是 "cap"，用 packName 会永远不命中）
  // 技能打开默认预览；新建技能使用编辑模式，保存/启用刷新保留当前模式
  const [openMode, setOpenMode] = useState<"edit" | "preview">("preview")
  useEffect(() => {
    if (!focus) return
    const focusPack = focus.source === "cap" ? cap : track
    if (focus.pack === focusPack) {
      setSource(focus.source)
      setSelected(focus.name)
      setOpenMode("preview")
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.n])

  useEffect(() => {
    if (!selected) { setDetail(null); return }
    detailApi(packName, selected).then(setDetail).catch(() => setDetail(null))
  }, [detailApi, packName, selected])

  const switchSource = (s: SkillSource) => {
    if (s === source) return
    if (dirty && !window.confirm("技能有未保存修改，放弃并切换？")) return
    setSource(s)
    setSelected(null)
    setOpenMode("preview")
  }

  const save = async () => {
    if (!selected || !editorRef.current) return
    setErr(null)
    try {
      await updateApi(packName, selected, editorRef.current.getRaw())
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
      packsChanged()
      reload()
      detailApi(packName, selected).then(setDetail).catch(() => {})
    } catch (e) {
      setErr(String(e))
    }
  }

  const toggleEnabled = async (enabled: boolean) => {
    if (!selected) return
    setErr(null)
    try {
      const toggle = source === "cap" ? api.setCapSkillEnabled : api.setTrackSkillEnabled
      await toggle(packName, selected, enabled)
      packsChanged()
      reload()
      detailApi(packName, selected).then(setDetail).catch(() => {})
    } catch (e) {
      setErr(String(e))
    }
  }

  const doDelete = async () => {
    if (!confirmDel) return
    setBusy(true)
    setErr(null)
    try {
      const del = source === "cap" ? api.deleteCapSkill : api.deleteTrackSkill
      await del(packName, confirmDel)
      setSelected((cur) => cur === confirmDel ? null : cur)
      packsChanged()
      reload()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
      setConfirmDel(null)
    }
  }

  const sk = detail?.skill
  // 右栏大纲：技能取已保存正文（parseSkill）
  const outlineMd = detail ? (parseSkill(detail.raw).hasFm ? parseSkill(detail.raw).form.body : detail.raw) : ""
  const outlinePrefix = SKILL_MD_PREFIX

  return (
    <div className="h-full min-h-0">
      <Group orientation="horizontal" className="h-full">
        {/* 左栏：技能列表 + 知识库树（默认 240px） */}
        <Panel defaultSize={240} minSize="14%">
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex shrink-0 items-center gap-1 border-b px-1.5 py-1">
              {(["cap", "track"] as SkillSource[]).map((s) => (
                <button key={s}
                  className={cn("rounded px-1.5 py-0.5 text-[10px]",
                    source === s ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")}
                  onClick={() => switchSource(s)}>
                  {s === "cap" ? `包：${capLabel(cap)}` : `轨：${trackLabel(track)}`}
                </button>
              ))}
              <span className="flex-1" />
              <button className="rounded px-1.5 py-0.5 text-[10px] text-primary hover:bg-accent/40"
                      title="新建中文薄路由技能" onClick={() => setCreateOpen(true)}>＋新建</button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
              {skills.map((s) => (
                <div key={s.name}
                     className={cn("group flex items-center rounded text-xs",
                       selected === s.name && "bg-primary/10 text-primary",
                       selected !== s.name && !s.enabled && "opacity-50")}>
                  <button
                    className="min-w-0 flex-1 truncate rounded px-2 py-1.5 text-left font-mono hover:bg-accent/40"
                    onClick={() => { setSelected(s.name); setOpenMode("preview") }}>
                    {s.name}
                  </button>
                  <button className="px-1.5 text-[10px] text-(--status-error) opacity-0 transition-opacity group-hover:opacity-100"
                          title="删除（整目录移入 .history/trash，可恢复）"
                          onClick={() => setConfirmDel(s.name)}>✕</button>
                </div>
              ))}
              {skills.length === 0 && (
                <p className="p-2 text-[11px] leading-relaxed text-muted-foreground">
                  该{source === "cap" ? "能力包" : "场景轨"}暂无中文薄路由技能
                </p>
              )}
            </div>
          </div>
        </Panel>
        <HHandle />

        {/* 中栏：技能编辑器（弹性宽） */}
        <Panel minSize="30%">
          {detail ? (
            <div className="flex h-full min-h-0 flex-col gap-1.5 p-3">
              <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                <span className="font-mono text-sm">{detail.name}</span>
                <Badge variant="outline" className="text-[10px]">{sk?.kind}/{sk?.pack}</Badge>
                {sk && !sk.enabled && <Badge variant="outline" className="text-[10px] text-(--status-approval)">enabled:false</Badge>}
                {err && <span className="break-all text-[10px] text-(--status-error)">{err}</span>}
                <span className="flex-1" />
                <label className="flex items-center gap-1 text-[10px] text-muted-foreground" title="只改 frontmatter enabled 行；禁用后不参与路由，正文保留">
                  <input type="checkbox" checked={sk?.enabled ?? true}
                         onChange={(e) => toggleEnabled(e.target.checked)} />
                  启用
                </label>
                <HistoryButton file={
                  source === "cap"
                    ? `capabilities/${packName}/skills/${selected}/SKILL.md`
                    : `tracks/${packName}/skills/${selected}/SKILL.md`} />
                <Button size="sm" variant="outline" className="text-[10px] text-(--status-error)"
                        onClick={() => setConfirmDel(detail.name)}>删除</Button>
                <Saved saved={saved} />
                <Button size="sm" onClick={save} disabled={!dirty}>保存</Button>
              </div>
              <p className="shrink-0 text-[11px] text-muted-foreground">
                {sk?.description ?? detail.meta.description as string}
              </p>
              <SkillEditor key={detail.name} ref={editorRef} detail={detail} vocab={vocab}
                           onDirtyChange={setDirty}
                           startMode={openMode} resetKey={focus?.n} />
              <DangerNote>表单保存时合并回写 frontmatter（name 锁定与目录一致，改名请新建+删除）；正文是 Agent 入口纪律，写坏会导致路由失效。</DangerNote>
            </div>
          ) : (
            <p className="p-4 text-xs text-muted-foreground">选择左侧技能，或点「＋新建」创建薄路由模板</p>
          )}
        </Panel>
        <HHandle />

        {/* 右栏：正文大纲（默认 300px） */}
        <Panel defaultSize={300} minSize="17%">
          <div className="flex h-full min-h-0 flex-col">
            <p className="shrink-0 border-b px-2 py-1 text-[10px] font-semibold text-muted-foreground">
              正文大纲（h1–h3，点击滚动）
            </p>
            <MarkdownOutline markdown={outlineMd} prefix={outlinePrefix} />
          </div>
        </Panel>
      </Group>

      <SkillCreateDialog open={createOpen} onOpenChange={setCreateOpen}
                        source={source} packName={packName}
                        onCreated={(name) => { setSelected(name); setOpenMode("edit"); reload() }} />
      <AlertDialog open={confirmDel !== null} onOpenChange={(v) => !v && setConfirmDel(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除技能 {confirmDel}？</AlertDialogTitle>
            <AlertDialogDescription>
              整个技能目录将移入 {source === "cap"
                ? `capabilities/${packName}/skills` : `tracks/${packName}/skills`}/.history/trash/
              （带时间戳，可手工恢复）。若有角色白名单引用它，将变成悬空引用（doctor 报 error）。
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

type DiscState = { ids: string[]; checked: string[]; listed: boolean; msg?: string }

function LlmPane() {
  const [providers, setProviders] = useState<LlmProvider[]>([])
  const [def, setDef] = useState<{ provider: string; model: string } | null>(null)
  // 可选默认供应商（全局默认 = 它的第一个模型；未设置 = 第一个启用供应商）
  const [defaultProvider, setDefaultProvider] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyIdx, setBusyIdx] = useState<number | null>(null)
  const [disc, setDisc] = useState<Record<number, DiscState>>({})
  const [testRes, setTestRes] = useState<Record<string, string>>({})
  const [newModel, setNewModel] = useState<Record<number, string>>({})
  const [helpOpen, setHelpOpen] = useState(false)

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

  const moveModel = (pi: number, mi: number, delta: number) =>
    setProviders((ps) => ps.map((p, j) => {
      if (j !== pi) return p
      const mj = mi + delta
      if (mj < 0 || mj >= p.models.length) return p
      const models = [...p.models]
      ;[models[mi], models[mj]] = [models[mj], models[mi]]
      return { ...p, models }
    }))

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

  // 已保存供应商且 key 框为空 → 用 name（服务端取存的 key）；否则用地址+key
  const creds = (p: LlmProvider) =>
    p.has_key && !(p.api_key ?? "").trim()
      ? { name: p.name }
      : { base_url: p.base_url, api_key: p.api_key ?? "" }

  const discover = async (i: number) => {
    setBusyIdx(i)
    setError(null)
    try {
      const r = await api.discoverLlm({ ...creds(providers[i]), format: providers[i].format, proxy: providers[i].proxy ?? null })
      const ids = r.models.map((m) => m.id)
      const checked = ids.filter((id) => providers[i].models.includes(id))
      setDisc((d) => ({ ...d, [i]: {
        ids, checked, listed: r.listed,
        msg: r.listed ? undefined : `该端点不支持模型列表，已探活候选 ${ids.length} 个可用`,
      } }))
    } catch (e) {
      setDisc((d) => ({ ...d, [i]: { ids: [], checked: [], listed: false, msg: String(e) } }))
    } finally {
      setBusyIdx(null)
    }
  }

  const applyDisc = (i: number) => {
    const d = disc[i]
    if (d) patch(i, { models: d.ids.filter((id) => d.checked.includes(id)) })
    setDisc(({ [i]: _drop, ...rest }) => rest)
  }

  const testModel = async (i: number, model: string) => {
    const key = `${i}/${model}`
    setTestRes((t) => ({ ...t, [key]: "…" }))
    const r = await api.testLlmModel({ ...creds(providers[i]), format: providers[i].format, model, proxy: providers[i].proxy ?? null })
    setTestRes((t) => ({ ...t, [key]: r.ok ? "✓ 可用" : `✗ ${r.error ?? "不可用"}` }))
  }

  const addModel = (i: number) => {
    const m = (newModel[i] ?? "").trim()
    if (!m) return
    if (!providers[i].models.includes(m)) patch(i, { models: [...providers[i].models, m] })
    setNewModel((n) => ({ ...n, [i]: "" }))
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
          <div className="llm-provider-header"><div className="llm-order"><button onClick={() => move(i, -1)} aria-label="上移">↑</button><button onClick={() => move(i, 1)} aria-label="下移">↓</button></div><div className="llm-provider-title"><Input value={p.name} onChange={(e) => patch(i, { name: e.target.value })} placeholder="供应商名" /><div><span>{p.models.length} 个模型</span><span className={p.enabled ? "llm-status is-on" : "llm-status"}>{p.enabled ? "已启用" : "已停用"}</span></div></div><label className="llm-switch"><input type="checkbox" checked={p.enabled} onChange={(e) => patch(i, { enabled: e.target.checked })} /><span /></label><button className="llm-delete" onClick={() => setProviders((ps) => ps.filter((_, j) => j !== i))}>删除</button></div>
           <div className="llm-section"><div className="llm-section-title"><span>连接配置</span><small>地址填写 API 根地址，系统自动追加协议端点</small></div><div className="llm-fields"><label><span>兼容格式</span><select value={p.format} onChange={(e) => patch(i, { format: e.target.value as LlmProvider["format"] })}><option value="openai-chat-completions">OpenAI Chat Completions</option><option value="openai-responses">OpenAI Responses API</option><option value="anthropic-messages">Anthropic Messages</option></select></label><label><span>API 地址</span><Input value={p.base_url} onChange={(e) => patch(i, { base_url: e.target.value })} className="font-mono" placeholder="https://api.example.com" /></label><label><span>API 密钥</span><Input type="password" value={p.api_key ?? ""} onChange={(e) => patch(i, { api_key: e.target.value })} className="font-mono" placeholder={p.has_key ? "已配置 · 留空表示保持不变" : "输入 API 密钥"} /></label><label><span>供应商代理</span><Input value={p.proxy ?? ""} onChange={(e) => patch(i, { proxy: e.target.value || null })} className="font-mono" placeholder="留空跟随系统代理，例如 http://127.0.0.1:7890" /></label><label className="llm-thinking" title="勾选=按兼容格式发送 reasoning/thinking（不支持时自动降级）；不勾=普通请求"><input type="checkbox" checked={p.thinking === true} onChange={(e) => patch(i, { thinking: e.target.checked || null })} /><span>思考链</span></label></div></div>
          <div className="llm-section"><div className="llm-section-title"><span>模型清单</span><small>第一个模型作为供应商默认</small><div className="llm-model-actions"><Button size="sm" variant="outline" onClick={() => addModel(i)}>添加模型</Button><Button size="sm" variant="outline" disabled={busyIdx === i || !p.base_url} onClick={() => discover(i)}>{busyIdx === i ? "获取中…" : "发现模型"}</Button></div></div><div className="llm-model-list">{p.models.map((m, mi) => <div className={cn("llm-model-row", mi === 0 && "is-primary")} key={m}><div className="llm-model-main"><span className="llm-model-dot" /><span className="font-mono">{m}</span>{mi === 0 && <Badge>默认模型</Badge>}</div><label className="llm-context"><span>上下文</span><Input type="number" min={0.1} step={1} placeholder="默认" value={p.model_context?.[m] ? String(p.model_context[m] / 1000) : ""} onChange={(e) => { const raw = e.target.value; const ctx = { ...(p.model_context ?? {}) }; if (raw === "") delete ctx[m]; else ctx[m] = Math.round(Number(raw) * 1000); patch(i, { model_context: ctx }) }} /><em>K</em></label><span className={cn("llm-test-state", testRes[`${i}/${m}`]?.startsWith("✓") && "is-ok", testRes[`${i}/${m}`]?.startsWith("✗") && "is-fail")}>{testRes[`${i}/${m}`] ?? "未测试"}</span><Button size="sm" variant="ghost" onClick={() => testModel(i, m)}>测试</Button><div className="llm-row-arrows"><button onClick={() => moveModel(i, mi, -1)}>↑</button><button onClick={() => moveModel(i, mi, 1)}>↓</button><button className="is-danger" onClick={() => patch(i, { models: p.models.filter((x) => x !== m) })}>×</button></div></div>)}</div><div className="llm-add-model"><Input value={newModel[i] ?? ""} onChange={(e) => setNewModel((n) => ({ ...n, [i]: e.target.value }))} onKeyDown={(e) => e.key === "Enter" && addModel(i)} placeholder="输入模型 ID，例如 gpt-4o-mini" /><Button size="sm" onClick={() => addModel(i)}>添加</Button></div>{disc[i] && <div className="llm-discovery"><div className="llm-discovery-title"><span>模型发现</span><small>{disc[i].ids.length} 个候选模型</small></div>{disc[i].msg && <p>{disc[i].msg}</p>}<div className="llm-discovery-list">{disc[i].ids.map((id) => <label key={id}><input type="checkbox" checked={disc[i].checked.includes(id)} onChange={(e) => setDisc((d) => { const cur = d[i]; const checked = e.target.checked ? [...cur.checked, id] : cur.checked.filter((x) => x !== id); return { ...d, [i]: { ...cur, checked } } })} />{id}</label>)}</div><div><Button size="sm" onClick={() => applyDisc(i)}>应用已选模型</Button><Button size="sm" variant="ghost" onClick={() => { const { [i]: _drop, ...rest } = disc; setDisc(rest) }}>取消</Button></div></div>}</div>
        </div>)}
        <Button size="sm" variant="outline" className="llm-add-provider" onClick={() => setProviders((ps) => [...ps, { name: "", base_url: "", format: "openai-chat-completions", api_key: "", models: [], enabled: true }])}>+ 添加供应商</Button>
      </div></ScrollArea>
      <button className="llm-help-toggle" onClick={() => setHelpOpen((open) => !open)}>配置说明 {helpOpen ? "⌃" : "⌄"}</button>{helpOpen && <div className="llm-help"><p>第一个启用供应商的第一个模型会作为自动默认模型。</p><p>API 密钥只显示配置状态，不会回显真实值。</p><p>上下文 K 用于控制会话历史预算和摘要压缩阈值，留空使用默认值。</p></div>}
    </div>
  )
}
