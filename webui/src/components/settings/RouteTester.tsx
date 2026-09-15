import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { PackRole, RouteHit } from "@/lib/types"
import type { Taxonomy } from "@/lib/taxonomy"
import { trackLabel } from "@/lib/taxonomy"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// 路由试算器（右栏）：真评分，候选=全部能力包 ∪ 当前轨；labels 第三框（×3）。
// 命中行可展开看 breakdown：每类（features/file_features/labels/keywords/description）
// 的命中词与加权得分。

export function RouteTester({ tax, track }: { tax: Taxonomy | null; track: string }) {
  const [query, setQuery] = useState("")
  const [role, setRole] = useState("")
  const [roles, setRoles] = useState<PackRole[]>([])
  const [features, setFeatures] = useState("")
  const [fileFeatures, setFileFeatures] = useState("")
  const [labels, setLabels] = useState("")
  const [hits, setHits] = useState<RouteHit[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => { api.trackRoles(track).then(setRoles).catch(() => {}) }, [track])

  const preview = async () => {
    if (!query.trim() && !features.trim() && !fileFeatures.trim() && !labels.trim()) return
    const toList = (s: string) => s.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean)
    try {
      const hs = await api.routePreview({
        query, track,
        capabilities: (tax?.capabilities ?? []).map((c) => c.name),
        role: role || undefined,
        features: toList(features),
        file_features: toList(fileFeatures),
        labels: toList(labels),
      })
      setHits(hs)
      setExpanded(hs[0] ? `${hs[0].pack}/${hs[0].name}` : null)
    } catch {
      setHits([])
    }
  }

  return (
    <div className="flex shrink-0 flex-col gap-1 border-b p-2">
      <p className="text-[10px] font-semibold text-muted-foreground">路由试算</p>
      <div className="flex items-center gap-1">
        <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="任务描述 / 关键词…"
               className="h-7 flex-1 text-xs" onKeyDown={(e) => e.key === "Enter" && preview()} />
        <select value={role} onChange={(e) => setRole(e.target.value)} title="按角色白名单窄化（可选）"
                className={cn("h-7 rounded border bg-background px-1 text-[11px] [color-scheme:dark]")}>
          <option value="">不限角色</option>
          {roles.map((r) => <option key={r.file} value={r.file}>{r.file}</option>)}
        </select>
      </div>
      <Input value={features} onChange={(e) => setFeatures(e.target.value)} className="h-7 font-mono text-[11px]"
             placeholder="行为特征 features（×3）" onKeyDown={(e) => e.key === "Enter" && preview()} />
      <Input value={fileFeatures} onChange={(e) => setFileFeatures(e.target.value)} className="h-7 font-mono text-[11px]"
             placeholder="文件特征 file_features，如 NX, Canary（×3）"
             onKeyDown={(e) => e.key === "Enter" && preview()} />
      <Input value={labels} onChange={(e) => setLabels(e.target.value)} className="h-7 font-mono text-[11px]"
             placeholder="标签 labels：平台/格式/漏洞类（×3）"
             onKeyDown={(e) => e.key === "Enter" && preview()} />
      <div className="flex items-center gap-1">
        <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={preview}>试算</Button>
        <span className="flex-1" />
        <span className="text-[10px] text-muted-foreground">
          全包 ∪ {trackLabel(track)} 轨{role ? ` · 角色 ${role}` : ""}
        </span>
      </div>
      {hits.length > 0 && (
        <div className="mt-1 space-y-0.5 overflow-y-auto">
          {hits.map((h) => {
            const key = `${h.pack}/${h.name}`
            const open = expanded === key
            return (
              <div key={key} className="rounded border bg-card/30 text-[11px]">
                <button className="flex w-full items-center gap-1.5 px-1.5 py-1 text-left"
                        onClick={() => setExpanded(open ? null : key)}>
                  <span className="w-3 text-[9px] text-muted-foreground">{open ? "▾" : "▸"}</span>
                  <span className="font-mono text-primary">{h.name}</span>
                  <span className="font-mono text-[10px] text-muted-foreground">{h.kind}/{h.pack}</span>
                  <span className="font-mono text-[10px] text-muted-foreground">{h.score}</span>
                  {!h.enabled && <span className="text-[10px] text-[--status-approval]">已禁用</span>}
                  <span className="flex-1" />
                  <span className="max-w-[45%] truncate text-[10px] text-muted-foreground"
                        title={h.matched.join(", ")}>{h.matched.join(", ")}</span>
                </button>
                {open && h.breakdown && (
                  <div className="space-y-0.5 border-t px-2 py-1 text-[10px]">
                    {h.breakdown.map((b) => (
                      <div key={b.category} className={cn("flex gap-1", b.hits.length === 0 && "opacity-40")}>
                        <span className="w-28 shrink-0 text-muted-foreground"
                              title={`权重 ×${b.weight}`}>{b.label} ×{b.weight}</span>
                        <span className="min-w-0 flex-1 truncate text-foreground/80"
                              title={b.hits.join(", ")}>{b.hits.join(", ") || "—"}</span>
                        <span className="shrink-0 font-mono">{b.score}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
