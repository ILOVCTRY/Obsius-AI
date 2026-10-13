import { useCallback, useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { OwnerRule, RatingRule, RuleProfiles } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ScrollArea } from "@/components/ui/scroll-area"
import { MarkdownView } from "./MarkdownView"
import { cn } from "@/lib/utils"

// 红线设置页（E1 重构，DESIGN.md §12 设置页）：左文件列表 + 右单文件编辑器。
// 2026-10-13 能力包退役：删「能力包红线」；平台规则/评级与价值口径改为全局单份（渗透独有），
// 位置 packs/kb/安全红线/{平台规则,评级与价值口径}/；轨红线 packs/kb/安全红线/轨红线/<track>.md。

type RuleFile =
  | { kind: "track-redlines"; track: string; path: string }
  | { kind: "owner"; tag: string; path: string }
  | { kind: "rating"; tag: string; path: string }

export interface RuleFocus {
  kind: "track-redlines" | "rating"; name: string; n: number; tag?: string }

const SCOPE: Record<RuleFile["kind"], string> = {
  "track-redlines": "轨红线 · 该轨全角色全量注入",
  owner: "平台规则 · 资产 meta.owner 命中才注入（渗透独有）",
  rating: "评级与价值口径 · 判级依据（带硬指令注入，渗透独有）",
}

const REDLINE_PATH = (track: string) => `kb/安全红线/轨红线/${track}.md`
const OWNER_PATH = (tag: string) => `kb/安全红线/平台规则/${tag}.md`
const RATING_PATH = (tag: string) => `kb/安全红线/评级与价值口径/${tag}.md`

const sameFile = (a: RuleFile | null, b: RuleFile) =>
  a !== null && a.kind === b.kind && a.path === b.path

function FileItem({ active, exists, path, title, onClick, onDelete }: {
  active: boolean
  exists: boolean
  path: string
  title: string
  onClick: () => void
  onDelete?: () => void
}) {
  return (
    <div className={cn("group flex items-center rounded border text-xs",
                      active && "border-primary/50 bg-primary/10")}>
      <button className="min-w-0 flex-1 truncate px-2 py-1.5 text-left font-mono hover:bg-accent/40"
              onClick={onClick} title={title}>
        <span className={cn("mr-1.5", exists ? "text-primary" : "text-muted-foreground")}>
          {exists ? "●" : "○"}
        </span>
        {path}
      </button>
      {onDelete && (
        <button className="px-1.5 text-[10px] text-(--status-error) opacity-0 transition-opacity group-hover:opacity-100"
                onClick={onDelete} title="停用（删除即停用，.history 留备份）">✕</button>
      )}
    </div>
  )
}

export function RulesPane({ track, focus, pid }: {
  track: string; focus?: RuleFocus | null; pid?: string | null }) {
  const [owners, setOwners] = useState<OwnerRule[]>([])
  const [ratings, setRatings] = useState<RatingRule[]>([])
  const [sel, setSel] = useState<RuleFile | null>(null)
  const [content, setContent] = useState("")
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)
  const [missing, setMissing] = useState(false)
  const [exists, setExists] = useState<boolean>(true)
  const [newTag, setNewTag] = useState("")
  const [newRatingTag, setNewRatingTag] = useState("")
  // 红线/规则文件都是 .md：默认预览渲染，可切编辑（对齐 SkillWorkspace/KbPane）
  const [mode, setMode] = useState<"preview" | "edit">("preview")

  const isPentest = track === "pentest"

  const reloadOwners = useCallback(() =>
    api.platformRules().then(setOwners).catch(() => {}), [])
  useEffect(() => { reloadOwners() }, [reloadOwners])
  const reloadRatings = useCallback(() =>
    api.ratings().then(setRatings).catch(() => {}), [])
  useEffect(() => { reloadRatings() }, [reloadRatings])

  // 状态点数据源：轨红线存在性预取（缺失 ○ / 存在 ●）
  useEffect(() => {
    api.trackRules(track)
      .then((r) => setExists(r.exists !== false))
      .catch(() => setExists(false))
  }, [track])

  // 选中变化 → 载入内容（文件描述符自带轨/tag，不随父级 props 漂移）
  useEffect(() => {
    if (!sel || sel.kind === "owner" || sel.kind === "rating") return
    api.trackRules(sel.track).then((r) => {
      setContent(r.content); setMissing(r.exists === false); setDirty(false)
    }).catch(() => { setContent(""); setMissing(true); setDirty(false) })
  }, [sel])

  // doctor 红线/评级跳转：自动选中对应文件
  const prevFocus = useRef(0)
  useEffect(() => {
    if (!focus?.n || focus.n === prevFocus.current) return
    prevFocus.current = focus.n
    if (focus.kind === "rating" && focus.tag) {
      setSel({ kind: "rating", tag: focus.tag, path: RATING_PATH(focus.tag) })
      api.ratings().then((rs) => {
        setRatings(rs)
        const r = rs.find((x) => x.tag === focus.tag)
        setContent(r ? r.content : ""); setMissing(!r); setDirty(false)
      }).catch(() => {})
      return
    }
    setSel({ kind: "track-redlines", track: focus.name, path: REDLINE_PATH(focus.name) })
  }, [focus?.n])  // eslint-disable-line react-hooks/exhaustive-deps

  // 切轨（父级 select）：dirty 先 confirm；取消则保留当前编辑不覆盖（修静默丢失）
  const prevTrack = useRef(track)
  useEffect(() => {
    if (prevTrack.current === track) return
    if (dirty && !window.confirm("当前文件有未保存修改，切换将丢弃，确认？")) {
      prevTrack.current = track
      return
    }
    prevTrack.current = track
    setSel({ kind: "track-redlines", track, path: REDLINE_PATH(track) })
  }, [track, dirty])

  const choose = (f: RuleFile) => {
    if (sameFile(sel, f)) return
    if (dirty && !window.confirm("当前文件有未保存修改，切换将丢弃，确认？")) return
    setSel(f)
    if (f.kind === "owner") {
      const o = owners.find((x) => x.tag === f.tag)
      setContent(o ? o.content : ""); setMissing(false); setDirty(false)
    } else if (f.kind === "rating") {
      const r = ratings.find((x) => x.tag === f.tag)
      setContent(r ? r.content : ""); setMissing(!r); setDirty(false)
    }
  }

  const save = async () => {
    if (!sel) return
    if (sel.kind === "owner") await api.updatePlatformRule(sel.tag, content)
    else if (sel.kind === "rating") await api.updateRating(sel.tag, content)
    else await api.updateTrackRules(sel.track, content)
    setSaved(true)
    setTimeout(() => setSaved(false), 1500)
    setDirty(false)
    setMissing(false)
    if (sel.kind === "track-redlines") setExists(true)
    if (sel.kind === "owner") reloadOwners()
    if (sel.kind === "rating") reloadRatings()
    window.dispatchEvent(new Event("packs-changed"))
  }

  const createOwner = async () => {
    const tag = newTag.trim()
    if (!tag) return
    await api.updatePlatformRule(tag, `# ${tag} 平台规则（标题/触发条件/测试边界…）\n`)
    setNewTag("")
    setSel({ kind: "owner", tag, path: OWNER_PATH(tag) })
    setDirty(false)
    reloadOwners()
  }

  const removeOwner = async (tag: string) => {
    await api.deletePlatformRule(tag)
    if (sel?.kind === "owner" && sel.tag === tag) { setSel(null); setContent(""); setDirty(false) }
    reloadOwners()
  }

  const createRating = async () => {
    const tag = newRatingTag.trim()
    if (!tag) return
    await api.updateRating(tag, `# ${tag} 评级与价值口径（判级条款 / 资产价值分级…）\n`)
    setNewRatingTag("")
    setSel({ kind: "rating", tag, path: RATING_PATH(tag) })
    setDirty(false)
    reloadRatings()
  }

  const removeRating = async (tag: string) => {
    await api.deleteRating(tag)
    if (sel?.kind === "rating" && sel.tag === tag) { setSel(null); setContent(""); setDirty(false) }
    reloadRatings()
  }

  // 切文件 default 回预览（编辑态仅在本次会话内显式选择）
  useEffect(() => { setMode("preview") }, [sel])

  const badge = sel ? SCOPE[sel.kind] : ""

  return (
    <div className="flex h-full min-h-0">
      <div className="w-64 shrink-0 space-y-3 overflow-y-auto border-r p-2">
        <div className="space-y-1">
          <p className="px-1 text-[10px] text-muted-foreground">轨红线 · kb/安全红线/轨红线/</p>
          <FileItem path="redlines.md" exists={exists}
                    title={REDLINE_PATH(track)}
                    active={sel?.kind === "track-redlines" && sel.track === track}
                    onClick={() => choose({ kind: "track-redlines", track, path: REDLINE_PATH(track) })} />
        </div>
        {isPentest && (
          <div className="space-y-1">
            <p className="px-1 text-[10px] text-muted-foreground">平台规则 · kb/安全红线/平台规则/</p>
            {owners.map((o) => (
              <FileItem key={o.tag} path={`${o.tag}.md`} exists title={OWNER_PATH(o.tag)}
                        active={sel?.kind === "owner" && sel.tag === o.tag}
                        onClick={() => choose({ kind: "owner", tag: o.tag, path: OWNER_PATH(o.tag) })}
                        onDelete={() => removeOwner(o.tag)} />
            ))}
            {owners.length === 0 && (
              <p className="px-1 text-[10px] text-muted-foreground">暂无平台规则</p>
            )}
            <div className="flex gap-1 pt-1">
              <Input value={newTag} onChange={(e) => setNewTag(e.target.value)} placeholder="新平台 tag，如 edusrc"
                     className="h-7 flex-1 text-xs" onKeyDown={(e) => e.key === "Enter" && createOwner()} />
              <Button size="sm" variant="outline" className="h-7 px-2" onClick={createOwner} disabled={!newTag.trim()}>＋</Button>
            </div>
          </div>
        )}
        {isPentest && (
          <div className="space-y-1">
            <p className="px-1 text-[10px] text-muted-foreground">评级与价值口径 · kb/安全红线/评级与价值口径/</p>
            {ratings.map((r) => (
              <FileItem key={r.tag} path={`${r.tag}.md`} exists title={RATING_PATH(r.tag)}
                        active={sel?.kind === "rating" && sel.tag === r.tag}
                        onClick={() => choose({ kind: "rating", tag: r.tag, path: RATING_PATH(r.tag) })}
                        onDelete={() => removeRating(r.tag)} />
            ))}
            {ratings.length === 0 && (
              <p className="px-1 text-[10px] text-muted-foreground">暂无评级规则</p>
            )}
            <div className="flex gap-1 pt-1">
              <Input value={newRatingTag} onChange={(e) => setNewRatingTag(e.target.value)}
                     placeholder="新评级 tag，如 edu-rating"
                     className="h-7 flex-1 text-xs" onKeyDown={(e) => e.key === "Enter" && createRating()} />
              <Button size="sm" variant="outline" className="h-7 px-2" onClick={createRating} disabled={!newRatingTag.trim()}>＋</Button>
            </div>
          </div>
        )}
        {pid && isPentest && <RuleProfilesEditor pid={pid} ownerTags={owners.map((o) => o.tag)} ratingTags={ratings.map((r) => r.tag)} />}
      </div>
      <div className="flex min-w-0 flex-1 flex-col p-3">
        {sel ? (
          <>
            <div className="flex shrink-0 items-center gap-2">
              <span className="truncate font-mono text-sm" title={sel.path}>{sel.path}</span>
              <Badge variant="outline" className="shrink-0 text-[10px]">{badge}</Badge>
              <span className="flex-1" />
              <div className="flex shrink-0 rounded border text-[10px]">
                <button className={cn("px-2 py-0.5", mode === "preview" ? "bg-primary/10 text-primary" : "text-muted-foreground")}
                        onClick={() => setMode("preview")}>预览</button>
                <button className={cn("px-2 py-0.5", mode === "edit" ? "bg-primary/10 text-primary" : "text-muted-foreground")}
                        onClick={() => setMode("edit")}>编辑</button>
              </div>
              <span className={cn("text-[10px] transition-opacity", saved ? "text-primary opacity-100" : "opacity-0")}>已保存 ✓</span>
              <Button size="sm" onClick={save} disabled={!dirty && !missing}>
                {missing ? "新建并保存" : "保存"}
              </Button>
            </div>
            {mode === "preview" ? (
              <ScrollArea className="mt-2 min-h-0 flex-1 rounded border bg-background/40 p-4">
                {content.trim()
                  ? <MarkdownView content={content} prefix={`rule-${sel.path.replace(/[^\w.-]+/g, "-")}`} />
                  : <p className="text-xs text-muted-foreground">{missing ? "（文件不存在，切「编辑」输入内容后保存即可创建）" : "（空文件）"}</p>}
              </ScrollArea>
            ) : (
              <Textarea value={content} onChange={(e) => { setContent(e.target.value); setDirty(true) }}
                        className="mt-2 min-h-0 flex-1 font-mono text-[11px] leading-relaxed" spellCheck={false}
                        placeholder={missing ? "（文件不存在，输入内容后点「新建并保存」）" : undefined} />
            )}
            <div className="flex shrink-0 items-center gap-2 border-t pt-1 text-[10px] text-muted-foreground">
              <span>{content.length} 字</span>
              <span className={dirty ? "text-(--status-approval)" : ""}>{dirty ? "● 未保存" : "已同步"}</span>
              <span className="flex-1" />
              <span className="text-(--status-approval)">
                红线是 Agent 的安全底线（build_rules_preamble 永久注入）；保存自动留 .history 备份可回滚。
              </span>
            </div>
          </>
        ) : (
          <p className="self-center text-center text-xs text-muted-foreground">
            选择左侧文件编辑：轨红线{isPentest ? " / 平台规则（owners）/ 评级与价值口径（rating）" : ""}<br />
            <span className="text-[10px]">● 存在　○ 缺失（新建并保存即可创建）；删除 = 停用（.history 留备份）</span>
          </p>
        )}
      </div>
    </div>
  )
}

// ---------- F11 生效档案勾选区（项目级 rule_profiles，经 PATCH /config 双写） ----------

// 纯显式（2026-10-01 去三态）：owners/rating 都是「勾哪个生效哪个」，不再有
// 自动命中/自动全注入——未勾选=不注入。保存空数组=显式不注入；全空则剥键。
function RuleProfilesEditor({ pid, ownerTags, ratingTags }: {
  pid: string; ownerTags: string[]; ratingTags: string[] }) {
  const [profiles, setProfiles] = useState<RuleProfiles>({})
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)

  const load = () => {
    api.getProject(pid).then((p) => {
      const rp = ((p.config?.rule_profiles ?? {}) as RuleProfiles)
      setProfiles({ owners: rp.owners ?? [], rating: rp.rating ?? [] })
      setDirty(false)
    }).catch(() => {})
  }

  useEffect(load, [pid])

  const toggle = (key: "owners" | "rating", tag: string) => {
    setProfiles((p) => {
      const cur = (p[key] as string[] | undefined) ?? []
      return { ...p, [key]: cur.includes(tag) ? cur.filter((x) => x !== tag) : [...cur, tag] }
    })
    setDirty(true)
  }

  const has = (key: "owners" | "rating", tag: string) =>
    ((profiles[key] as string[] | undefined) ?? []).includes(tag)

  const save = async () => {
    // 只存清单内的勾选（不在清单的残留 tag 不写入——防错位）；
    // 两个都空 → 剥键（等价不注入，盘上保持干净）
    const out: RuleProfiles = {}
    const ownersKept = (profiles.owners ?? []).filter((t) => ownerTags.includes(t))
    const ratingKept = (profiles.rating ?? []).filter((t) => ratingTags.includes(t))
    if (ownersKept.length) out.owners = ownersKept
    if (ratingKept.length) out.rating = ratingKept
    await api.patchProjectConfig(pid, { rule_profiles: Object.keys(out).length ? out : null })
    setSaved(true); setTimeout(() => setSaved(false), 1500)
    load()
  }

  const Check = ({ on, label, onClick }: { on: boolean; label: string; onClick: () => void }) => (
    <label className="flex cursor-pointer items-center gap-1 text-[11px]">
      <input type="checkbox" checked={on} onChange={onClick} className="accent-primary" />{label}
    </label>
  )

  return (
    <div className="space-y-1.5 rounded border bg-muted/30 p-2">
      <p className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground">
        生效档案 · 当前项目
        <span className="flex-1" />
        <span className={cn("text-[10px] transition-opacity", saved ? "text-primary opacity-100" : "opacity-0")}>已保存 ✓</span>
        <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" onClick={save} disabled={!dirty}>保存</Button>
      </p>
      <div>
        <p className="text-[10px] text-muted-foreground">平台规则（owners）</p>
        {ownerTags.length === 0 ? (
          <span className="ml-1 text-[10px] text-muted-foreground">暂无 owners 规则</span>
        ) : (
          <div className="ml-1 flex flex-wrap gap-x-2">
            {ownerTags.map((t) => (
              <Check key={t} on={has("owners", t)} label={t} onClick={() => toggle("owners", t)} />
            ))}
          </div>
        )}
      </div>
      <div>
        <p className="text-[10px] text-muted-foreground">评级与价值口径（rating）</p>
        {ratingTags.length === 0 ? (
          <span className="ml-1 text-[10px] text-muted-foreground">暂无评级规则</span>
        ) : (
          <div className="ml-1 flex flex-wrap gap-x-2">
            {ratingTags.map((t) => (
              <Check key={t} on={has("rating", t)} label={t} onClick={() => toggle("rating", t)} />
            ))}
          </div>
        )}
      </div>
      <p className="text-[10px] leading-relaxed text-muted-foreground">
        勾哪个生效哪个；不勾=不注入。未配置的项目默认不叠加 owners/rating（轨红线仍恒注入）。保存后下次开窗生效。
      </p>
    </div>
  )
}
