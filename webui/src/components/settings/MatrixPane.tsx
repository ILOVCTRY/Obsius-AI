import { useCallback, useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { PackRole, SkillDef } from "@/lib/types"
import type { Taxonomy } from "@/lib/taxonomy"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

// 角色 × 技能/任务类型 只读矩阵：白名单命中 ✓；悬空引用/未注册类型标红；禁用技能标黄。
// 数据源与真实开窗一致：全局技能（全部 cap ∪ 当前轨）+ 轨角色 + 轨 task_types 注册表。

interface SkillCol extends SkillDef {
  source: "cap" | "track"
}

export function MatrixPane({ tax, track, onFocusSkill }: {
  tax: Taxonomy | null
  track: string
  onFocusSkill?: (source: "cap" | "track", pack: string, name: string) => void
}) {
  const [roles, setRoles] = useState<PackRole[]>([])
  const [cols, setCols] = useState<SkillCol[]>([])
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(() => {
    if (!tax) return
    setErr(null)
    const roleP: Promise<PackRole[]> = api.trackRoles(track).catch(() => [] as PackRole[])
    const skillPs: Promise<SkillCol[]>[] = [
      ...tax.capabilities.map((c) =>
        api.capSkills(c.name)
          .then((ss) => ss.map((s): SkillCol => ({ ...s, source: "cap" })))
          .catch(() => [] as SkillCol[])),
      api.trackSkills(track)
        .then((ss) => ss.map((s): SkillCol => ({ ...s, source: "track" })))
        .catch(() => [] as SkillCol[]),
    ]
    Promise.all([roleP, ...skillPs]).then(([rs, ...skillGroups]) => {
      setRoles(rs)
      // 同名以后加载的轨技能覆盖（与 registry 语义一致）
      const map = new Map<string, SkillCol>()
      for (const group of skillGroups) for (const s of group) map.set(s.name, s)
      setCols([...map.values()])
    }).catch((e) => setErr(String(e)))
  }, [tax, track])
  useEffect(() => { reload() }, [reload])

  const knownNames = useMemo(() => new Set(cols.map((c) => c.name)), [cols])
  const validTypes = new Set(Object.keys(tax?.task_types?.[track] ?? { generic: "passive" }))

  const grouped = useMemo(() => {
    const groups: { title: string; cols: SkillCol[] }[] = []
    const trackCols = cols.filter((c) => c.source === "track")
    if (trackCols.length) groups.push({ title: `轨 ${track}`, cols: trackCols })
    for (const c of tax?.capabilities ?? []) {
      const cs = cols.filter((s) => s.source === "cap" && s.pack === c.name)
      if (cs.length) groups.push({ title: c.label || c.name, cols: cs })
    }
    return groups
  }, [cols, tax, track])

  const Cell = ({ role, name }: { role: PackRole; name: string }) => {
    if (role.skills == null) return <span className="text-muted-foreground/40" title="skills: null 全放行">·</span>
    return role.skills.includes(name)
      ? <span className="text-primary">✓</span>
      : null
  }

  return (
    <ScrollArea className="h-full">
      <div className="space-y-3 p-3">
        <p className="text-[10px] text-muted-foreground">
          行=「{track}」轨角色；✓=在该角色技能白名单内，·=skills 为 null（全放行），空白=不挂。
          列=全部能力包 ∪ 本轨技能。红=悬空引用或未注册 task_type，黄=引用了已禁用技能。
        </p>
        {err && <p className="text-xs text-[--status-error]">{err}</p>}

        {/* 技能矩阵 */}
        <div className="overflow-x-auto rounded border">
          <table className="border-collapse text-[11px]">
            <thead>
              <tr className="border-b">
                <th className="sticky left-0 z-10 bg-popover p-1.5 text-left font-mono font-normal text-muted-foreground">角色 \ 技能</th>
                {grouped.map((g) =>
                  g.cols.map((c) => (
                    <th key={`${c.source}:${c.pack}:${c.name}`}
                        className="border-l p-1 text-center align-bottom font-normal">
                      <button className="font-mono text-[10px] leading-tight text-primary hover:underline"
                              style={{ writingMode: "vertical-rl" }}
                              title={`${c.kind}/${c.pack} · ${c.description}`}
                              onClick={() => onFocusSkill?.(c.source, c.pack, c.name)}>
                        {c.name}
                      </button>
                      {!c.enabled && <div className="text-[9px] text-amber-300">禁用</div>}
                    </th>
                  )))}
              </tr>
              <tr className="border-b bg-card/40 text-[9px] text-muted-foreground">
                <th className="sticky left-0 z-10 bg-card p-1 text-left font-normal" />
                {grouped.map((g) =>
                  g.cols.map((c) => (
                    <th key={`g:${c.source}:${c.pack}:${c.name}`}
                        className="border-l px-1 text-center font-normal">{g.title}</th>
                  )))}
              </tr>
            </thead>
            <tbody>
              {roles.map((r) => {
                const dangling = (r.skills ?? []).filter((s) => !knownNames.has(s))
                const disabled = (r.skills ?? []).filter((s) =>
                  knownNames.has(s) && !cols.find((c) => c.name === s)!.enabled)
                return (
                  <tr key={r.file} className="border-b last:border-0 hover:bg-accent/20">
                    <td className="sticky left-0 z-10 bg-popover p-1.5 font-mono whitespace-nowrap">
                      {r.file === "_generalist" ? "通用 _generalist" : r.file}
                    </td>
                    {grouped.map((g) => g.cols.map((c) => (
                      <td key={`${r.file}:${c.name}`} className={cn("border-l p-1 text-center",
                        !c.enabled && r.skills?.includes(c.name) && "bg-amber-500/10")}>
                        <Cell role={r} name={c.name} />
                      </td>
                    )))}
                    {dangling.length > 0 && (
                      <td className="border-l bg-red-500/10 p-1">
                        {dangling.map((d) => (
                          <Badge key={d} variant="outline"
                                 className="mr-1 border-red-500/50 text-[9px] text-red-300"
                                 title="该技能在任何包/轨中都不存在">
                            悬空:{d}
                          </Badge>
                        ))}
                      </td>
                    )}
                    {disabled.length > 0 && dangling.length === 0 && (
                      <td className="border-l bg-amber-500/10 p-1">
                        {disabled.map((d) => (
                          <Badge key={d} variant="outline" className="mr-1 text-[9px] text-amber-300"
                                 title="引用了 enabled:false 的技能，开窗时被静默过滤">禁用:{d}</Badge>
                        ))}
                      </td>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {/* task_types 区 */}
        <div className="rounded border p-2">
          <p className="mb-1 text-[11px]">任务类型白名单（注册表：{[...validTypes].join("、")}）</p>
          <div className="space-y-1">
            {roles.map((r) => {
              const tts = r.task_types
              return (
                <div key={r.file} className="flex items-center gap-2 text-[11px]">
                  <span className="w-32 shrink-0 truncate font-mono">{r.file}</span>
                  {tts == null
                    ? <span className="text-muted-foreground">全部（task_types: null）</span>
                    : tts.length === 0
                      ? <span className="text-amber-300">空列表（白名单关闭：无类型可领）</span>
                      : tts.map((t) => (
                        <Badge key={t} variant="outline"
                               className={cn("text-[10px]", !validTypes.has(t) && "border-red-500/50 text-red-300")}
                               title={validTypes.has(t) ? undefined : "未在 task_types.yaml 注册，publish 会被拒"}>
                          {t}{!validTypes.has(t) && " ✗未注册"}
                        </Badge>
                      ))}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </ScrollArea>
  )
}
