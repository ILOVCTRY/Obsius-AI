import { useCallback, useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { OwnerRule, RatingRule, RuleProfiles } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"

// 红线设置页（E1 重构，DESIGN.md §12 设置页）：左文件列表 + 右单文件编辑器。
// 旧实现三块大文本框纵向堆叠同屏抢高度、切轨/包未保存内容被静默覆盖——
// 现在单文件编辑 + dirty confirm 守卫（对齐 Skill 页 guard）；后端 API 与 .history 不动。
// F11：第四组「评级与价值口径」（rating/）+ 项目级生效档案勾选区（需 pid 上下文）。

type RuleFile =
  | { kind: "track-redlines"; track: string; path: string }
  | { kind: "cap-redlines"; cap: string; path: string }
  | { kind: "owner"; track: string; tag: string; path: string }
  | { kind: "rating"; track: string; tag: string; path: string }

export interface RuleFocus {
  kind: "track-redlines" | "cap-redlines" | "rating"; name: string; n: number; tag?: string }

const SCOPE: Record<RuleFile["kind"], string> = {
  "track-redlines": "轨红线 · 该轨全角色全量注入",
  "cap-redlines": "能力包红线 · 启用该包的项目注入",
  owner: "平台规则 · 资产 meta.owner 命中才注入",
  rating: "评级与价值口径 · 判级依据（带硬指令注入）",
}

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

export function RulesPane({ track, cap, focus, pid }: {
  track: string; cap: string; focus?: RuleFocus | null; pid?: string | null }) {
  const [owners, setOwners] = useState<OwnerRule[]>([])
  const [ratings, setRatings] = useState<RatingRule[]>([])
  const [sel, setSel] = useState<RuleFile | null>(null)
  const [content, setContent] = useState("")
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)
  const [missing, setMissing] = useState(false)
  const [exists, setExists] = useState<{ track: boolean; cap: boolean }>({ track: true, cap: true })
  const [newTag, setNewTag] = useState("")
  const [newRatingTag, setNewRatingTag] = useState("")

  const reloadOwners = useCallback(() =>
    api.trackOwners(track).then(setOwners).catch(() => {}), [track])
  useEffect(() => { reloadOwners() }, [reloadOwners])
  const reloadRatings = useCallback(() =>
    api.trackRatings(track).then(setRatings).catch(() => {}), [track])
  useEffect(() => { reloadRatings() }, [reloadRatings])

  // 状态点数据源：两份 redlines 存在性预取（缺失 ○ / 存在 ●）
  useEffect(() => {
    api.trackRules(track)
      .then((r) => setExists((m) => ({ ...m, track: r.exists !== false })))
      .catch(() => setExists((m) => ({ ...m, track: false })))
    api.capRules(cap)
      .then((r) => setExists((m) => ({ ...m, cap: r.exists !== false })))
      .catch(() => setExists((m) => ({ ...m, cap: false })))
  }, [track, cap])

  // 选中变化 → 载入内容（文件描述符自带轨/包，不随父级 props 漂移）
  useEffect(() => {
    if (!sel || sel.kind === "owner" || sel.kind === "rating") return
    const load = sel.kind === "track-redlines"
      ? api.trackRules(sel.track) : api.capRules(sel.cap)
    load.then((r) => {
      setContent(r.content); setMissing(r.exists === false); setDirty(false)
    }).catch(() => { setContent(""); setMissing(true); setDirty(false) })
  }, [sel])

  // owners/ratings 内容不走 effect 回填：列表自带 content，选中时（choose/create/doctor 跳转）直接同步

  // doctor 红线/评级跳转：自动选中对应文件
  const prevFocus = useRef(0)
  useEffect(() => {
    if (!focus?.n || focus.n === prevFocus.current) return
    prevFocus.current = focus.n
    if (focus.kind === "rating" && focus.tag) {
      setSel({ kind: "rating", track: focus.name, tag: focus.tag,
               path: `tracks/${focus.name}/rules/rating/${focus.tag}.md` })
      api.trackRatings(focus.name).then((rs) => {
        setRatings(rs)
        const r = rs.find((x) => x.tag === focus.tag)
        setContent(r ? r.content : ""); setMissing(!r); setDirty(false)
      }).catch(() => {})
      return
    }
    setSel(focus.kind === "track-redlines"
      ? { kind: "track-redlines", track: focus.name, path: `tracks/${focus.name}/rules/redlines.md` }
      : { kind: "cap-redlines", cap: focus.name, path: `capabilities/${focus.name}/rules/redlines.md` })
  }, [focus?.n])  // eslint-disable-line react-hooks/exhaustive-deps

  // 切轨/包（父级 select）：dirty 先 confirm；取消则保留当前编辑不覆盖（修静默丢失）
  const prevTC = useRef({ track, cap })
  useEffect(() => {
    if (prevTC.current.track === track && prevTC.current.cap === cap) return
    if (dirty && !window.confirm("当前文件有未保存修改，切换将丢弃，确认？")) {
      prevTC.current = { track, cap }   // 父级选择无法回退，但未保存内容保留在编辑器
      return
    }
    prevTC.current = { track, cap }
    setSel({ kind: "track-redlines", track, path: `tracks/${track}/rules/redlines.md` })
  }, [track, cap, dirty])

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
    if (sel.kind === "owner") await api.updateTrackOwner(sel.track, sel.tag, content)
    else if (sel.kind === "rating") await api.updateTrackRating(sel.track, sel.tag, content)
    else if (sel.kind === "track-redlines") await api.updateTrackRules(sel.track, content)
    else await api.updateCapRules(sel.cap, content)
    setSaved(true)
    setTimeout(() => setSaved(false), 1500)
    setDirty(false)
    setMissing(false)
    setExists((m) => ({ ...m, track: sel.kind === "track-redlines" ? true : m.track,
                        cap: sel.kind === "cap-redlines" ? true : m.cap }))
    if (sel.kind === "owner") reloadOwners()
    if (sel.kind === "rating") reloadRatings()
    window.dispatchEvent(new Event("packs-changed"))
  }

  const createOwner = async () => {
    const tag = newTag.trim()
    if (!tag) return
    await api.updateTrackOwner(track, tag, `# ${tag} 平台规则（标题/触发条件/测试边界…）\n`)
    setNewTag("")
    setSel({ kind: "owner", track, tag, path: `tracks/${track}/rules/owners/${tag}.md` })
    setDirty(false)
    reloadOwners()
  }

  const removeOwner = async (tag: string) => {
    await api.deleteTrackOwner(track, tag)
    if (sel?.kind === "owner" && sel.tag === tag) { setSel(null); setContent(""); setDirty(false) }
    reloadOwners()
  }

  const createRating = async () => {
    const tag = newRatingTag.trim()
    if (!tag) return
    await api.updateTrackRating(track, tag,
      `# ${tag} 评级与价值口径（判级条款 / 资产价值分级…）\n`)
    setNewRatingTag("")
    setSel({ kind: "rating", track, tag, path: `tracks/${track}/rules/rating/${tag}.md` })
    setDirty(false)
    reloadRatings()
  }

  const removeRating = async (tag: string) => {
    await api.deleteTrackRating(track, tag)
    if (sel?.kind === "rating" && sel.tag === tag) { setSel(null); setContent(""); setDirty(false) }
    reloadRatings()
  }

  const badge = sel ? SCOPE[sel.kind] : ""

  return (
    <div className="flex h-full min-h-0">
      <div className="w-64 shrink-0 space-y-3 overflow-y-auto border-r p-2">
        <div className="space-y-1">
          <p className="px-1 text-[10px] text-muted-foreground">轨红线 · tracks/{track}/rules/</p>
          <FileItem path="redlines.md" exists={exists.track}
                    title={`tracks/${track}/rules/redlines.md`}
                    active={sel?.kind === "track-redlines" && sel.track === track}
                    onClick={() => choose({ kind: "track-redlines", track, path: `tracks/${track}/rules/redlines.md` })} />
        </div>
        <div className="space-y-1">
          <p className="px-1 text-[10px] text-muted-foreground">能力包红线 · capabilities/{cap}/rules/</p>
          <FileItem path="redlines.md" exists={exists.cap}
                    title={`capabilities/${cap}/rules/redlines.md`}
                    active={sel?.kind === "cap-redlines" && sel.cap === cap}
                    onClick={() => choose({ kind: "cap-redlines", cap, path: `capabilities/${cap}/rules/redlines.md` })} />
        </div>
        <div className="space-y-1">
          <p className="px-1 text-[10px] text-muted-foreground">平台规则 · tracks/{track}/rules/owners/</p>
          {owners.map((o) => (
            <FileItem key={o.tag} path={`${o.tag}.md`} exists title={`tracks/${track}/rules/owners/${o.tag}.md`}
                      active={sel?.kind === "owner" && sel.tag === o.tag}
                      onClick={() => choose({ kind: "owner", track, tag: o.tag, path: `tracks/${track}/rules/owners/${o.tag}.md` })}
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
        <div className="space-y-1">
          <p className="px-1 text-[10px] text-muted-foreground">评级与价值口径 · tracks/{track}/rules/rating/</p>
          {ratings.map((r) => (
            <FileItem key={r.tag} path={`${r.tag}.md`} exists title={`tracks/${track}/rules/rating/${r.tag}.md`}
                      active={sel?.kind === "rating" && sel.tag === r.tag}
                      onClick={() => choose({ kind: "rating", track, tag: r.tag, path: `tracks/${track}/rules/rating/${r.tag}.md` })}
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
        {pid && <RuleProfilesEditor pid={pid} ownerTags={owners.map((o) => o.tag)} ratingTags={ratings.map((r) => r.tag)} />}
      </div>
      <div className="flex min-w-0 flex-1 flex-col p-3">
        {sel ? (
          <>
            <div className="flex shrink-0 items-center gap-2">
              <span className="truncate font-mono text-sm" title={sel.path}>{sel.path}</span>
              <Badge variant="outline" className="shrink-0 text-[10px]">{badge}</Badge>
              <span className="flex-1" />
              <span className={cn("text-[10px] transition-opacity", saved ? "text-primary opacity-100" : "opacity-0")}>已保存 ✓</span>
              <Button size="sm" onClick={save} disabled={!dirty && !missing}>
                {missing ? "新建并保存" : "保存"}
              </Button>
            </div>
            <Textarea value={content} onChange={(e) => { setContent(e.target.value); setDirty(true) }}
                      className="mt-2 min-h-0 flex-1 font-mono text-[11px] leading-relaxed" spellCheck={false}
                      placeholder={missing ? "（文件不存在，输入内容后点「新建并保存」）" : undefined} />
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
            选择左侧文件编辑：轨红线 / 能力包红线 / 平台规则（owners）/ 评级与价值口径（rating）<br />
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
    // 只存本轨清单内的勾选（切轨后不在新轨清单的残留 tag 不写入——防错位）；
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
          <span className="ml-1 text-[10px] text-muted-foreground">本轨暂无 owners 规则</span>
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
          <span className="ml-1 text-[10px] text-muted-foreground">本轨暂无评级规则</span>
        ) : (
          <div className="ml-1 flex flex-wrap gap-x-2">
            {ratingTags.map((t) => (
              <Check key={t} on={has("rating", t)} label={t} onClick={() => toggle("rating", t)} />
            ))}
          </div>
        )}
      </div>
      <p className="text-[10px] leading-relaxed text-muted-foreground">
        勾哪个生效哪个；不勾=不注入。未配置的项目默认不叠加 owners/rating（能力包红线/轨红线仍恒注入）。保存后下次开窗生效。
      </p>
    </div>
  )
}
