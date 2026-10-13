import { useCallback, useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { PackRole, SkillDef } from "@/lib/types"
import type { Taxonomy } from "@/lib/taxonomy"
import { roleLabel } from "@/lib/roles"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

// 角色 × 技能/任务类型 只读矩阵：白名单命中 ✓；悬空引用/未注册类型标红；禁用技能标黄。
// 2026-10-13 能力包退役：技能扁平（packs/kb/skills），不再按包/轨分组。

export function MatrixPane({ tax, track, onFocusSkill }: {
  tax: Taxonomy | null
  track: string
  onFocusSkill?: (name: string) => void
}) {
  const [roles, setRoles] = useState<PackRole[]>([])
  const [cols, setCols] = useState<SkillDef[]>([])
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(() => {
    if (!tax) return
    setErr(null)
    // 竞态守卫：切轨后旧轨聚合响应晚到不得覆盖新轨矩阵（同 RolesPane）
    let alive = true
    Promise.all([
      api.trackRoles(track).catch(() => [] as PackRole[]),
      api.skills().catch(() => [] as SkillDef[]),
    ]).then(([rs, ss]) => {
      if (!alive) return
      setRoles(rs)
      setCols(ss)
    }).catch((e) => { if (alive) setErr(String(e)) })
    return () => { alive = false }
  }, [tax, track])
  useEffect(() => reload(), [reload])

  const knownNames = useMemo(() => new Set(cols.map((c) => c.name)), [cols])
  const validTypes = new Set(Object.keys(tax?.task_types?.[track] ?? { generic: "passive" }))

  // 转置后悬空/禁用徽章挂角色列头（原为行尾附加列）
  const roleIssues = useMemo(() => {
    const m = new Map<string, { dangling: string[]; disabled: string[] }>()
    for (const r of roles) {
      const dangling = (r.skills ?? []).filter((s) => !knownNames.has(s))
      const disabled = (r.skills ?? []).filter((s) =>
        knownNames.has(s) && !cols.find((c) => c.name === s)!.enabled)
      m.set(r.file, { dangling, disabled })
    }
    return m
  }, [roles, cols, knownNames])

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
          行=全部技能（packs/kb/skills）；列=「{track}」轨角色。✓=在该角色技能白名单内，
          ·=skills 为 null（全放行），空白=不挂。红=悬空引用或未注册 task_type，黄=引用了已禁用技能。
        </p>
        {err && <p className="text-xs text-(--status-error)">{err}</p>}

        {/* 技能矩阵（转置：技能为行、角色为列；表窄居中、表宽横滚，边框贴住表格） */}
        <div className="flex justify-center">
          <div className="max-w-full overflow-x-auto rounded border">
            <table className="border-collapse bg-card text-xs">
              <thead>
                <tr className="border-b">
                  <th className="sticky left-0 z-10 min-w-[220px] bg-card p-2 text-left font-mono text-[10px] font-normal text-muted-foreground">技能 \ 角色</th>
                  {roles.map((r) => {
                    const iss = roleIssues.get(r.file) ?? { dangling: [], disabled: [] }
                    return (
                      <th key={r.file} className="border-l p-2 text-left align-top font-normal">
                        <div className="whitespace-nowrap font-mono">
                          {roleLabel(r)}
                        </div>
                        {iss.dangling.map((d) => (
                          <Badge key={d} variant="outline" className="mt-1 mr-1 border-red-500/50 text-[9px] text-red-300"
                                 title="该技能在技能库中不存在">
                            悬空:{d}
                          </Badge>
                        ))}
                        {iss.disabled.map((d) => (
                          <Badge key={d} variant="outline" className="mt-1 mr-1 text-[9px] text-amber-300"
                                 title="引用了 enabled:false 的技能，开窗时被静默过滤">禁用:{d}</Badge>
                        ))}
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <tbody>
                {cols.map((c) => (
                  <tr key={c.name} className="border-b last:border-0 hover:bg-accent/20">
                    <td className="sticky left-0 z-10 bg-card p-2 font-mono whitespace-nowrap">
                      <button className="text-primary hover:underline"
                              title={c.description}
                              onClick={() => onFocusSkill?.(c.name)}>
                        {c.name}
                      </button>
                      {!c.enabled && <span className="ml-1.5 text-[9px] text-amber-300">禁用</span>}
                    </td>
                    {roles.map((r) => (
                      <td key={`${r.file}:${c.name}`} className={cn("border-l p-2 text-center",
                        !c.enabled && r.skills?.includes(c.name) && "bg-amber-500/10")}>
                        <Cell role={r} name={c.name} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* task_types 区 */}
        <div className="rounded border p-2">
          <p className="mb-1 text-[11px]">任务类型白名单（注册表：{[...validTypes].join("、")}）</p>
          <div className="space-y-1">
            {roles.map((r) => {
              const tts = r.task_types
              return (
                <div key={r.file} className="flex items-center gap-2 text-[11px]">
                  <span className="w-32 shrink-0 truncate font-mono" title={r.file}>{roleLabel(r)}</span>
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
