import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import { dirLabel, type PackInfo, type Taxonomy } from "@/lib/taxonomy"
import { cn } from "@/lib/utils"
import { KbView } from "@/components/settings/KbView"

/** 一级知识库工作台；设置页只保留配置相关内容。
 *  2026-10-13 能力包退役：选择器改为 kb 知识方向（directions）。 */
export function KnowledgeView({ pid }: { pid?: string | null }) {
  const [tax, setTax] = useState<Taxonomy | null>(null)
  const [cap, setCap] = useState("web")
  const [directions, setDirections] = useState<PackInfo[]>([])

  const optionsOf = (names: string[]): PackInfo[] =>
    names.map((name) => ({ name, label: dirLabel(name), description: "" }))

  useEffect(() => {
    api.taxonomy().then((t) => {
      setTax(t)
      if (!pid && t.directions[0]) setCap(t.directions[0])
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!pid) {
      const options = optionsOf(tax?.directions ?? [])
      setDirections(options)
      setCap((current) => options.some((item) => item.name === current) ? current : (options[0]?.name ?? current))
      return
    }
    api.getProject(pid).then((p) => {
      const names = p.directions_bound?.length ? p.directions_bound : p.directions
      const options = optionsOf(names ?? [])
      setDirections(options)
      setCap((current) => options.some((item) => item.name === current) ? current : (options[0]?.name ?? current))
    }).catch(() => {})
  }, [pid, tax])

  return (
    // pt-9=首页无顶栏分支的 36px 拖动区让位；项目内有 .topbar(h-16)，再加就是重复死区
    <div className={cn("h-full min-h-0", !pid && "pt-9")}>
      <KbView cap={cap} capabilities={directions} onCapChange={setCap} />
    </div>
  )
}
