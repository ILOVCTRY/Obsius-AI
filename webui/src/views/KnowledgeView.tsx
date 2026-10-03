import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import { capLabel, type PackInfo, type Taxonomy } from "@/lib/taxonomy"
import { KbView } from "@/components/settings/KbView"

/** 一级知识库工作台；设置页只保留配置相关内容。 */
export function KnowledgeView({ pid }: { pid?: string | null }) {
  const [tax, setTax] = useState<Taxonomy | null>(null)
  const [cap, setCap] = useState("web")
  const [capabilities, setCapabilities] = useState<PackInfo[]>([])

  useEffect(() => {
    api.taxonomy().then((t) => {
      setTax(t)
      if (!pid && t.capabilities[0]) setCap(t.capabilities[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!pid) {
      const options = tax?.capabilities ?? []
      setCapabilities(options)
      setCap((current) => options.some((item) => item.name === current) ? current : (options[0]?.name ?? current))
      return
    }
    api.getProject(pid).then((p) => {
      const caps = p.capabilities_bound?.length ? p.capabilities_bound : p.capabilities
      const options = (caps ?? []).map((name) => ({
        name,
        label: tax?.capabilities.find((item) => item.name === name)?.label || capLabel(name),
        description: tax?.capabilities.find((item) => item.name === name)?.description || "",
      }))
      setCapabilities(options)
      setCap((current) => options.some((item) => item.name === current) ? current : (options[0]?.name ?? current))
    }).catch(() => {})
  }, [pid, tax])

  return (
    <div className="h-full min-h-0"><KbView cap={cap} capabilities={capabilities} onCapChange={setCap} /></div>
  )
}
