import { useCallback, useEffect, useRef, useState } from "react"
import { Group, Panel, Separator } from "react-resizable-panels"
import { api } from "@/lib/api"
import type {
  Expert, KbRefHit, KbSourceTree, LlmProvider, McpServer,
  SkillDef, SkillDetail, SkillVocab,
} from "@/lib/types"
import { capLabel, trackLabel, type Taxonomy } from "@/lib/taxonomy"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { DoctorBar } from "@/components/settings/DoctorBar"
import { HistoryButton } from "@/components/settings/HistoryDialog"
import { MatrixPane } from "@/components/settings/MatrixPane"
import {
  ExpertCreateDialog, KbCreateDialog, KbRenameDialog, SkillCreateDialog, type SkillSource,
} from "@/components/settings/CreateDialogs"
import { SkillEditor, SKILL_MD_PREFIX, type SkillEditorHandle } from "@/components/settings/SkillEditor"
import { KbPane, refsFromError } from "@/components/settings/KbPane"
import { KbTree } from "@/components/settings/KbTree"
import { RouteTester } from "@/components/settings/RouteTester"
import { MarkdownOutline } from "@/components/settings/MarkdownOutline"
import { ProposalsPane } from "@/components/settings/ProposalsPane"
import { IntelSourcePane } from "@/components/settings/IntelSourcePane"
import { RulesPane, type RuleFocus } from "@/components/settings/RulesPane"
import { parseSkill } from "@/lib/skillfm"
import { cn } from "@/lib/utils"

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
  const [ruleFocus, setRuleFocus] = useState<RuleFocus | null>(null)
  const [pendingN, setPendingN] = useState(0)
  // 深链已指定轨/包时，taxonomy 异步回填的缺省值不得覆盖（晚于 nav effect 落地会冲掉深链）
  const navAppliedRef = useRef(false)
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
      if (p.track) setTrack(p.track)
      if (p.capabilities?.length) setCap(p.capabilities[0])
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

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-2">
        <span className="text-[10px] text-muted-foreground">场景轨</span>
        <select value={track} onChange={(e) => setTrack(e.target.value)} className={selectCls}>
          {(tax?.tracks ?? []).map((t) => <option key={t.name} value={t.name}>{t.label || trackLabel(t.name)}</option>)}
        </select>
        <span className="text-[10px] text-muted-foreground">能力包</span>
        <select value={cap} onChange={(e) => setCap(e.target.value)} className={selectCls}>
          {(tax?.capabilities ?? []).map((c) => <option key={c.name} value={c.name}>{c.label || capLabel(c.name)}</option>)}
        </select>
        <span className="text-[10px] text-muted-foreground">角色/owners 属轨，Skill/红线轨与包各一份；写入留 .history，下次开窗生效</span>
      </div>
      <DoctorBar onNavigate={navigateTarget} />
      <Tabs value={tab} onValueChange={setTab} className="min-h-0 flex-1 flex-col gap-0">
        <TabsList className="w-full shrink-0 justify-start rounded-none border-b bg-transparent p-0">
          {[
            ["experts", "专家"], ["skills", "Skill"], ["matrix", "矩阵"], ["rules", "红线"],
            ["llm", "模型"], ["mcp", "MCP"], ["intel", "情报源"], ["proposals", "提案"],
          ].map(([k, label]) => (
            <TabsTrigger key={k} value={k} className="rounded-none border-b-2 px-3 py-1.5 text-xs">
              {label}
              {k === "proposals" && pendingN > 0 && (
                <span className="ml-1 rounded-full bg-(--status-error) px-1.5 text-[9px] leading-4 text-white">{pendingN}</span>
              )}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="experts" className="min-h-0 flex-1"><ExpertsPane tax={tax} focus={expertFocus} /></TabsContent>
        <TabsContent value="skills" className="min-h-0 flex-1"><SkillsPane tax={tax} track={track} cap={cap} focus={skillFocus} /></TabsContent>
        <TabsContent value="matrix" className="min-h-0 flex-1">
          <MatrixPane tax={tax} track={track}
                      onFocusSkill={(source, pack, name) => {
                        if (source === "track") setTrack(pack); else setCap(pack)
                        setTab("skills")
                        setSkillFocus({ source, pack, name, n: Date.now() })
                      }} />
        </TabsContent>
        <TabsContent value="rules" className="min-h-0 flex-1"><RulesPane track={track} cap={cap} focus={ruleFocus} pid={pid} /></TabsContent>
        <TabsContent value="llm" className="min-h-0 flex-1"><LlmPane /></TabsContent>
        <TabsContent value="mcp" className="min-h-0 flex-1"><McpPane /></TabsContent>
        <TabsContent value="intel" className="min-h-0 flex-1"><IntelSourcePane /></TabsContent>
        <TabsContent value="proposals" className="min-h-0 flex-1">
          <ProposalsPane onPendingChange={setPendingN} />
        </TabsContent>
      </Tabs>
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
function VHandle() {
  return <Separator className={cn("h-px w-full shrink-0", HANDLE_CLS)} />
}

function SkillsPane({ tax, track, cap, focus }: {
  tax: Taxonomy | null; track: string; cap: string; focus: SkillFocus | null
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

  // 互斥选中：技能列表与知识库树同一时刻只有一个中栏文档
  const [mode, setMode] = useState<"skill" | "kb">("skill")
  // kb 固定挂在能力包上；切 cap 时若有未保存修改先拦截，kbCap 冻结到确认后
  const [kbCap, setKbCap] = useState(cap)
  const [kbSources, setKbSources] = useState<KbSourceTree[]>([])
  const [kbPath, setKbPath] = useState<string | null>(null)
  const [kbDirty, setKbDirty] = useState(false)
  const [kbContent, setKbContent] = useState("")
  const [kbTreeOpen, setKbTreeOpen] = useState(true)
  const [kbCreateOpen, setKbCreateOpen] = useState(false)
  const [kbRenamePath, setKbRenamePath] = useState<string | null>(null)
  const [kbDel, setKbDel] = useState<{ path: string; refs: KbRefHit[] } | null>(null)
  const [kbBusy, setKbBusy] = useState(false)

  const listApi = source === "cap" ? api.capSkills : api.trackSkills
  const detailApi = source === "cap" ? api.capSkillDetail : api.trackSkillDetail
  const updateApi = source === "cap" ? api.updateCapSkill : api.updateTrackSkill

  const reload = useCallback(() => {
    // 竞态守卫：切包/切轨后旧响应晚到不得覆盖新列表（同 RolesPane）
    let alive = true
    listApi(packName).then((ss) => {
      if (!alive) return
      setSkills(ss)
      setSelected((cur) => cur && ss.some((s) => s.name === cur) ? cur : (ss[0]?.name ?? null))
    }).catch(() => { if (alive) setSkills([]) })
    return () => { alive = false }
  }, [listApi, packName])
  useEffect(() => reload(), [reload])
  useEffect(() => { api.skillsVocab().then(setVocab).catch(() => {}) }, [])

  const reloadKb = useCallback(() => {
    api.kbList(kbCap).then((r) => setKbSources(r.sources)).catch(() => setKbSources([]))
  }, [kbCap])
  useEffect(reloadKb, [reloadKb])

  // 顶部能力包切换：kb 有未保存修改时拦截（kbCap 不前进，树仍停在旧包）
  useEffect(() => {
    if (cap === kbCap) return
    if (kbDirty && !window.confirm(
      `知识库文档 ${kbPath} 有未保存修改，切到能力包 ${cap} 将放弃修改，继续？`)) return
    setKbCap(cap)
    setKbPath(null)
    setKbDirty(false)
  }, [cap, kbCap, kbDirty, kbPath])

  // doctor/矩阵/直播间深链跳转：切到指定来源并选中技能
  // （守卫按 focus.source 对应的包比较——packName 依赖本 pane 的 source state，
  //   轨技能深链时 source 仍是 "cap"，用 packName 会永远不命中）
  // viaFocus：深链选中的技能以预览模式打开（F12 配套）；手动点选/保存后回编辑默认
  const [viaFocus, setViaFocus] = useState(false)
  useEffect(() => {
    if (!focus) return
    const focusPack = focus.source === "cap" ? cap : track
    if (focus.pack === focusPack) {
      setSource(focus.source)
      setMode("skill")
      setSelected(focus.name)
      setViaFocus(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.n])

  useEffect(() => {
    if (mode !== "skill" || !selected) { setDetail(null); return }
    detailApi(packName, selected).then(setDetail).catch(() => setDetail(null))
  }, [detailApi, packName, selected, mode])

  // —— dirty 拦截：技能 ↔ kb 互斥切换 ——
  const guardSkill = () => !dirty || window.confirm("技能有未保存修改，放弃并切换？")
  const guardKb = () => !kbDirty || window.confirm("知识库文档有未保存修改，放弃并切换？")
  const pickSkill = (n: string) => {
    if (mode === "kb" && !guardKb()) return
    setMode("skill")
    setSelected(n)
    setViaFocus(false)
  }
  const pickKb = (p: string) => {
    if (mode === "skill" && !guardSkill()) return
    setMode("kb")
    setKbPath(p)
  }
  const switchSource = (s: SkillSource) => {
    if (s === source) return
    if (dirty && !guardSkill()) return
    setSource(s)
  }

  const save = async () => {
    if (!selected || !editorRef.current) return
    setErr(null)
    try {
      await updateApi(packName, selected, editorRef.current.getRaw())
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
      packsChanged()
      setViaFocus(false) // 保存后重载 detail 会触发编辑器重置，回到编辑模式
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

  // kb 删除：先无引用尝试；409 带 refs → 弹引用面，显式「强制删除」才带 force
  const doDeleteKb = async (force: boolean) => {
    if (!kbDel) return
    setKbBusy(true)
    setErr(null)
    try {
      await api.kbDelete(kbCap, kbDel.path, force)
      if (kbPath === kbDel.path) { setKbPath(null); setKbDirty(false) }
      setKbDel(null)
      packsChanged()
      reloadKb()
    } catch (e) {
      const refs = refsFromError(e)
      if (refs.length > 0) setKbDel({ path: kbDel.path, refs })
      else { setErr(String(e)); setKbDel(null) }
    } finally {
      setKbBusy(false)
    }
  }

  const sk = detail?.skill
  const kbTotal = kbSources.reduce((n, s) => n + s.files.length, 0)
  // 右栏大纲：技能取已保存正文（parseSkill），kb 取当前编辑内容
  const outlineMd = mode === "kb"
    ? kbContent
    : detail ? (parseSkill(detail.raw).hasFm ? parseSkill(detail.raw).form.body : detail.raw) : ""
  const outlinePrefix = mode === "kb" ? `kb-${kbCap}` : SKILL_MD_PREFIX

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
                       mode === "skill" && selected === s.name && "bg-primary/10 text-primary",
                       !(mode === "skill" && selected === s.name) && !s.enabled && "opacity-50")}>
                  <button
                    className="min-w-0 flex-1 truncate rounded px-2 py-1.5 text-left font-mono hover:bg-accent/40"
                    onClick={() => pickSkill(s.name)}>
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
            {/* 📚 知识库本地基线树（挂当前能力包） */}
            <div className="flex shrink-0 flex-col border-t">
              <div className="flex shrink-0 items-center gap-1 py-1 pl-1.5 pr-1">
                <button className="flex min-w-0 flex-1 items-center gap-1 text-[11px] text-muted-foreground hover:text-primary"
                        onClick={() => setKbTreeOpen((v) => !v)}
                        title="知识库本地基线（英文快照不翻译，新经验写新 md）">
                  <span className="w-3 text-[9px]">{kbTreeOpen ? "▾" : "▸"}</span>
                  <span className="truncate">📚 {capLabel(kbCap)} 知识库（{kbTotal}）</span>
                </button>
                <button className="shrink-0 rounded px-1 text-[11px] text-primary hover:bg-accent/40"
                        title="新建知识库 md"
                        onClick={() => setKbCreateOpen(true)}>＋</button>
              </div>
              {kbTreeOpen && (
                <div className="h-[42%] min-h-[120px] border-t">
                  <KbTree cap={kbCap} sources={kbSources}
                          selected={mode === "kb" ? kbPath : null}
                          onSelect={pickKb}
                          onRename={setKbRenamePath}
                          onDelete={(p) => setKbDel({ path: p, refs: [] })} />
                </div>
              )}
            </div>
          </div>
        </Panel>
        <HHandle />

        {/* 中栏：技能或 kb 文档（互斥，弹性宽） */}
        <Panel minSize="30%">
          {mode === "kb" && kbPath ? (
            <KbPane key={`${kbCap}/${kbPath}`}
                    cap={kbCap} path={kbPath} reloadKey={0}
                    onDirtyChange={setKbDirty}
                    onContent={setKbContent}
                    onSaved={() => { packsChanged(); reloadKb() }}
                    onRename={setKbRenamePath}
                    onDelete={(p) => setKbDel({ path: p, refs: [] })}
                    onOpenKb={pickKb} />
          ) : mode === "kb" ? (
            <p className="p-4 text-xs text-muted-foreground">
              从左下知识库树选择文档；英文上游快照只读纪律：不翻译、不覆盖，新经验点「＋」写新 md。
            </p>
          ) : detail ? (
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
                           startMode={viaFocus ? "preview" : "edit"} resetKey={focus?.n} />
              <DangerNote>表单保存时合并回写 frontmatter（name 锁定与目录一致，改名请新建+删除）；正文是 Agent 入口纪律，写坏会导致路由失效。</DangerNote>
            </div>
          ) : (
            <p className="p-4 text-xs text-muted-foreground">选择左侧技能，或点「＋新建」创建薄路由模板</p>
          )}
        </Panel>
        <HHandle />

        {/* 右栏：路由试算 + md 大纲（默认 300px） */}
        <Panel defaultSize={300} minSize="17%">
          <Group orientation="vertical">
            <Panel defaultSize="58%" minSize="20%" className="min-h-0 overflow-y-auto">
              <RouteTester tax={tax} track={track} />
            </Panel>
            <VHandle />
            <Panel defaultSize="42%" minSize="12%">
              <div className="flex h-full min-h-0 flex-col">
                <p className="shrink-0 border-b px-2 py-1 text-[10px] font-semibold text-muted-foreground">
                  {mode === "kb" ? "文档大纲" : "正文大纲"}（h1–h3，点击滚动）
                </p>
                <MarkdownOutline markdown={outlineMd} prefix={outlinePrefix} />
              </div>
            </Panel>
          </Group>
        </Panel>
      </Group>

      <SkillCreateDialog open={createOpen} onOpenChange={setCreateOpen}
                        source={source} packName={packName}
                        onCreated={(name) => { setMode("skill"); setSelected(name); reload() }} />
      <KbCreateDialog open={kbCreateOpen} onOpenChange={setKbCreateOpen} cap={kbCap}
                      onCreated={(p) => {
                        if (dirty && !guardSkill()) return
                        reloadKb()
                        setMode("kb")
                        setKbPath(p)
                      }} />
      <KbRenameDialog open={kbRenamePath !== null} onOpenChange={(v) => !v && setKbRenamePath(null)}
                      cap={kbCap} path={kbRenamePath}
                      onRenamed={(r) => {
                        setKbPath(r.new_path)
                        setMode("kb")
                        reloadKb()
                        packsChanged()
                      }} />
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
      <AlertDialog open={kbDel !== null} onOpenChange={(v) => !v && setKbDel(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除知识库文档{kbDel && kbDel.refs.length > 0 ? "（仍被引用）" : ""} {kbDel?.path}？</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div>
                <p>文件将移入 {kbCap}/kb/.history/kb-trash/（带时间戳，可手工恢复）。</p>
                {kbDel && kbDel.refs.length > 0 && (
                  <div className="mt-2 max-h-40 overflow-y-auto rounded border p-1.5">
                    <p className="text-(--status-approval)">以下 {kbDel.refs.length} 处完整路径引用将变成悬空引用（doctor 报 error）：</p>
                    {kbDel.refs.map((r, i) => (
                      <p key={i} className="truncate font-mono text-[10px]" title={r.forms.join(" / ")}>
                        {r.file}:{r.line}（{r.kind}）
                      </p>
                    ))}
                    <p className="mt-1">相对 md 链接无法静态扫描，删除前请自行确认。</p>
                  </div>
                )}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction disabled={kbBusy}
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={() => doDeleteKb(kbDel?.refs.length === 0 ? false : true)}>
              {kbBusy ? "删除中…" : kbDel && kbDel.refs.length > 0
                ? `强制删除（${kbDel.refs.length} 处引用悬空）` : "删除"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

// ---------- MCP 配置层 ----------

function McpPane() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.mcpServers().then((r) => setServers(r.servers)).catch(() => {})
  }, [])

  const patch = (i: number, p: Partial<McpServer>) =>
    setServers((ss) => ss.map((s, j) => j === i ? { ...s, ...p } : s))

  const save = async () => {
    // http server 需 url；stdio 需 command
    const valid = servers.filter((s) => s.name.trim() &&
      (s.transport === "stdio" ? (s.command ?? "").trim() : s.url.trim()))
    await api.updateMcpServers(valid)
    setSaved(true)
    setTimeout(() => setSaved(false), 1500)
    setServers(valid)
  }

  return (
    <div className="flex h-full flex-col gap-2 p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm">config/mcp.json</span>
        <span className="flex-1" />
        <Saved saved={saved} />
        <Button size="sm" onClick={save}>保存</Button>
      </div>
      <div className="space-y-1.5">
        {servers.map((s, i) => (
          <div key={i} className="rounded border p-2">
            <div className="flex items-center gap-2">
              <Input value={s.name} onChange={(e) => patch(i, { name: e.target.value })}
                     className="w-36 font-mono text-xs" placeholder="名称" />
              <select value={s.transport} onChange={(e) => patch(i, { transport: e.target.value })}
                      className={cn(selectCls, "h-7")}>
                <option value="streamable-http">http</option>
                <option value="stdio">stdio</option>
              </select>
              {s.transport === "stdio" ? (
                <>
                  <Input value={s.command ?? ""} onChange={(e) => patch(i, { command: e.target.value })}
                         className="w-28 font-mono text-xs" placeholder="命令，如 uv" />
                  <Input value={(s.args ?? []).join(" ")} onChange={(e) => patch(i, { args: e.target.value.split(" ").filter(Boolean) })}
                         className="flex-1 font-mono text-xs" placeholder="参数（空格分隔）" />
                </>
              ) : (
                <Input value={s.url} onChange={(e) => patch(i, { url: e.target.value })}
                       className="flex-1 font-mono text-xs" placeholder="http://127.0.0.1:8081/mcp" />
              )}
              <Input value={s.domains.join(",")} onChange={(e) => patch(i, { domains: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })}
                     className="w-32 font-mono text-xs" placeholder="适用范围(空=全部)" />
              <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
                <input type="checkbox" checked={s.enabled} onChange={(e) => patch(i, { enabled: e.target.checked })} />
                启用
              </label>
              <Button size="sm" variant="ghost" className="text-[10px] text-(--status-error)"
                      onClick={() => setServers((ss) => ss.filter((_, j) => j !== i))}>删除</Button>
            </div>
          </div>
        ))}
      </div>
      <Button size="sm" variant="outline" className="w-fit"
              onClick={() => setServers((ss) => [...ss, { name: "", url: "", transport: "stdio", enabled: true, domains: [], command: "", args: [] }])}>
        + 添加 server
      </Button>
      <DangerNote>MCP 配置层（stdio 填命令+参数，http 填 URL）：记录端点与启动方式。Agent 运行时工具桥（把 MCP 工具注入会话工具集）是后续批次——现在改配置不会改变 Agent 可用工具。</DangerNote>
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
      const r = await api.discoverLlm(creds(providers[i]))
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
    const r = await api.testLlmModel({ ...creds(providers[i]), model })
    setTestRes((t) => ({ ...t, [key]: r.ok ? "✓ 可用" : `✗ ${r.error ?? "不可用"}` }))
  }

  const addModel = (i: number) => {
    const m = (newModel[i] ?? "").trim()
    if (!m) return
    if (!providers[i].models.includes(m)) patch(i, { models: [...providers[i].models, m] })
    setNewModel((n) => ({ ...n, [i]: "" }))
  }

  return (
    <div className="flex h-full flex-col gap-2 p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm">config/providers.json</span>
        <span className="flex-1" />
        <span className="text-[10px] text-muted-foreground">默认供应商</span>
        <select
          className="h-8 rounded-md border bg-background px-2 text-xs"
          value={defaultProvider ?? ""}
          onChange={(e) => setDefaultProvider(e.target.value || null)}
          title="全局默认供应商（开窗/编排/分类器缺省用它；未设置=第一个启用供应商）"
        >
          <option value="">（自动：第一个启用供应商）</option>
          {providers.filter((p) => p.enabled).map((p) => (
            <option key={p.name} value={p.name}>{p.name}</option>
          ))}
        </select>
        <span className="text-[10px] text-muted-foreground">
          全局默认：{def ? `${def.provider} / ${def.model}` : "无（至少启用一个供应商）"}
        </span>
        <Saved saved={saved} />
        <Button size="sm" onClick={save}>保存</Button>
      </div>
      {error && <p className="text-xs text-(--status-error)">{error}</p>}
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-2 pr-2">
          {providers.map((p, i) => (
            <div key={i} className={cn("rounded border p-2", !p.enabled && "opacity-50")}>
              <div className="flex items-center gap-2">
                <Button size="sm" variant="ghost" className="h-6 px-1 text-[10px]"
                        onClick={() => move(i, -1)}>↑</Button>
                <Button size="sm" variant="ghost" className="h-6 px-1 text-[10px]"
                        onClick={() => move(i, 1)}>↓</Button>
                <Input value={p.name} onChange={(e) => patch(i, { name: e.target.value })}
                       className="w-40 font-mono text-xs" placeholder="供应商名（唯一）" />
                <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
                  <input type="checkbox" checked={p.enabled}
                         onChange={(e) => patch(i, { enabled: e.target.checked })} />
                  启用
                </label>
                <span className="flex-1" />
                <Button size="sm" variant="ghost" className="text-[10px] text-(--status-error)"
                        onClick={() => setProviders((ps) => ps.filter((_, j) => j !== i))}>删除</Button>
              </div>
              <div className="mt-1.5 flex flex-col gap-1.5">
                <Input value={p.base_url} onChange={(e) => patch(i, { base_url: e.target.value })}
                       className="font-mono text-xs" placeholder="https://…/api/xxx（Anthropic /v1/messages 兼容）" />
                <Input type="password" value={p.api_key ?? ""}
                       onChange={(e) => patch(i, { api_key: e.target.value })}
                       className="font-mono text-xs"
                       placeholder={p.has_key ? "已配置（留空保存=不修改）" : "api_key"} />
              </div>
              {/* 模型有序清单：第一个=该供应商默认模型；上下文=该模型最大窗口（token，
                  可选，驱动 Agent 上下文预算；留空=默认） */}
              <div className="mt-1.5 rounded bg-card/40 p-1.5">
                {p.models.map((m, mi) => (
                  <div key={m} className="flex items-center gap-1 py-0.5 text-xs">
                    {mi === 0 && <Badge className="text-[9px]">默认</Badge>}
                    <span className="font-mono">{m}</span>
                    <Input type="number" min={0.1} step={1} className="ml-1 h-6 w-28 px-1 font-mono text-[10px]"
                           placeholder="上下文 K"
                           value={p.model_context?.[m] ? String(p.model_context[m] / 1000) : ""}
                           title="该模型最大上下文，单位 K token（填 128 = 128k token）。驱动会话历史预算（≈K×2000 字符）与摘要压缩阈值；留空=默认 256K"
                           onChange={(e) => {
                             const raw = e.target.value
                             const ctx = { ...(p.model_context ?? {}) }
                             if (raw === "") delete ctx[m]
                             else ctx[m] = Math.round(Number(raw) * 1000)
                             patch(i, { model_context: ctx })
                           }} />
                    <span className="flex-1" />
                    <span className="w-16 text-[10px]" title="最小调用测活">
                      {testRes[`${i}/${m}`] ?? ""}
                    </span>
                    <Button size="sm" variant="ghost" className="h-5 px-1 text-[10px]"
                            onClick={() => testModel(i, m)}>测活</Button>
                    <Button size="sm" variant="ghost" className="h-5 px-1 text-[10px]"
                            onClick={() => moveModel(i, mi, -1)}>↑</Button>
                    <Button size="sm" variant="ghost" className="h-5 px-1 text-[10px]"
                            onClick={() => moveModel(i, mi, 1)}>↓</Button>
                    <Button size="sm" variant="ghost" className="h-5 px-1 text-[10px] text-(--status-error)"
                            onClick={() => patch(i, { models: p.models.filter((x) => x !== m) })}>✕</Button>
                  </div>
                ))}
                <div className="mt-1 flex items-center gap-1.5">
                  <Input className="h-7 flex-1 font-mono text-xs" placeholder="手动添加模型名"
                         value={newModel[i] ?? ""}
                         onChange={(e) => setNewModel((n) => ({ ...n, [i]: e.target.value }))}
                         onKeyDown={(e) => e.key === "Enter" && addModel(i)} />
                  <Button size="sm" variant="outline" className="h-7 text-[10px]" onClick={() => addModel(i)}>添加</Button>
                  <Button size="sm" variant="outline" className="h-7 text-[10px]"
                          disabled={busyIdx === i || !p.base_url} onClick={() => discover(i)}>
                    {busyIdx === i ? "获取中…" : "🔌 获取支持的模型"}
                  </Button>
                </div>
                {disc[i] && (
                  <div className="mt-1.5 rounded border p-1.5">
                    {disc[i].msg && <p className="mb-1 text-[10px] text-(--status-approval)">{disc[i].msg}</p>}
                    <div className="max-h-36 overflow-auto">
                      {disc[i].ids.length === 0 && <p className="text-[10px] text-muted-foreground">无可用模型</p>}
                      {disc[i].ids.map((id) => (
                        <label key={id} className="flex items-center gap-1.5 py-0.5 text-xs font-mono">
                          <input type="checkbox" checked={disc[i].checked.includes(id)}
                                 onChange={(e) => setDisc((d) => {
                                   const cur = d[i]
                                   const checked = e.target.checked
                                     ? [...cur.checked, id]
                                     : cur.checked.filter((x) => x !== id)
                                   return { ...d, [i]: { ...cur, checked } }
                                 })} />
                          {id}
                        </label>
                      ))}
                    </div>
                    <div className="mt-1 flex gap-1.5">
                      <Button size="sm" className="h-6 text-[10px]" onClick={() => applyDisc(i)}>应用勾选（首个=默认）</Button>
                      <Button size="sm" variant="ghost" className="h-6 text-[10px]"
                              onClick={() => {
                                const { [i]: _drop, ...rest } = disc
                                setDisc(rest)
                              }}>取消</Button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>
      <Button size="sm" variant="outline" className="w-fit"
              onClick={() => setProviders((ps) => [...ps,
                { name: "", base_url: "", api_key: "", models: [], enabled: true }])}>
        + 添加供应商
      </Button>
      <DangerNote>
        勾选保存的模型按顺序可用，第一个是该供应商默认；第一个启用供应商的默认模型=全局默认（新开窗使用）。
        在跑会话可在会话页「切换模型」即时生效。密钥存 config/providers.json（已 gitignore），读出只显示是否已配置。
        「上下文 K」为该模型最大窗口（单位 K token，填 128 = 128k），驱动会话历史预算与摘要压缩时机；
        不填默认 256K。已在跑会话切换模型时随之生效。
      </DangerNote>
    </div>
  )
}
