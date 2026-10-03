import { useEffect, useState } from "react"
import { BrainCircuit } from "lucide-react"
import { api } from "@/lib/api"
import { capLabel, trackLabel, type Taxonomy } from "@/lib/taxonomy"
import { SkillsPane } from "@/views/SettingsView"

/** 一级技能工作台；编辑器逻辑与设置页兼容入口共享。 */
export function SkillsView({ pid, focus }: {
  pid?: string | null
  focus?: { source: "cap" | "track"; pack: string; name: string; n: number } | null
}) {
  const [tax, setTax] = useState<Taxonomy | null>(null)
  const [track, setTrack] = useState("ctf")
  const [cap, setCap] = useState("web")

  useEffect(() => {
    api.taxonomy().then((t) => {
      setTax(t)
      if (!pid && t.tracks[0]) setTrack(t.tracks[0].name)
      if (!pid && t.capabilities[0]) setCap(t.capabilities[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!pid) return
    api.getProject(pid).then((p) => {
      if (p.track) setTrack(p.track)
      const caps = p.capabilities_bound?.length ? p.capabilities_bound : p.capabilities
      if (caps?.[0]) setCap(caps[0])
    }).catch(() => {})
  }, [pid])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b px-4 py-3">
        <BrainCircuit size={17} className="text-primary" />
        <div>
          <h1 className="text-sm font-semibold">技能库</h1>
          <p className="text-[10px] text-muted-foreground">路由技能、执行纪律与技能验证</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">场景
            <select className="rounded border bg-background px-2 py-1 text-xs [color-scheme:dark]" value={track} onChange={(e) => setTrack(e.target.value)}>
              {(tax?.tracks ?? []).map((item) => <option key={item.name} value={item.name}>{item.label || trackLabel(item.name)}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">能力包
            <select className="rounded border bg-background px-2 py-1 text-xs [color-scheme:dark]" value={cap} onChange={(e) => setCap(e.target.value)}>
              {(tax?.capabilities ?? []).map((item) => <option key={item.name} value={item.name}>{item.label || capLabel(item.name)}</option>)}
            </select>
          </label>
        </div>
      </div>
      <div className="min-h-0 flex-1"><SkillsPane track={track} cap={cap} focus={focus ?? null} /></div>
    </div>
  )
}
