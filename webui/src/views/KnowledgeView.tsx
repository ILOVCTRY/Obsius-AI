import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import { capOptionLabel, KB_ALL, type PackInfo } from "@/lib/taxonomy"
import { cn } from "@/lib/utils"
import { KbView } from "@/components/settings/KbView"

/** 一级知识库工作台；设置页只保留配置相关内容。
 *
 *  2026-10-13 能力包退役：选择器改为 kb 知识方向（directions）。
 *  2026-10-23（Nan 报 bug）：「进入 CTF 轨后打开只显示二进制」——根因是这里
 *  用**项目盘上绑定**（`directions_bound`，CTF 项目存的是 `["binary"]`）当选项，
 *  于是整个知识库被裁到一个方向。改为：选项恒为「全部方向 + 盘上全部 kb 方向」，
 *  默认选「全部方向」（聚合树，路径带方向前缀）。
 *
 *  项目绑定**不再参与**这里的选择器：知识库是给人看的资料库，方向只是树的组织
 *  维度；「哪个方向更该被先看到」交给检索侧的轨道加权
 *  （`core/skills/taxonomy.TRACK_KB_DIRECTION_WEIGHTS`），不靠裁剪可见范围。 */
export function KnowledgeView({ pid }: { pid?: string | null }) {
  const [cap, setCap] = useState(KB_ALL)
  const [directions, setDirections] = useState<PackInfo[]>([])

  useEffect(() => {
    api.taxonomy().then((t) => {
      const options: PackInfo[] = [
        { name: KB_ALL, label: capOptionLabel(KB_ALL), description: "" },
        ...t.directions.map((name) => ({ name, label: capOptionLabel(name), description: "" })),
      ]
      setDirections(options)
      setCap((current) => options.some((item) => item.name === current) ? current : KB_ALL)
    }).catch(() => {})
  }, [])

  return (
    // pt-9=首页无顶栏分支的 36px 拖动区让位；项目内有 .topbar(h-16)，再加就是重复死区
    <div className={cn("h-full min-h-0", !pid && "pt-9")}>
      <KbView cap={cap} capabilities={directions} onCapChange={setCap} />
    </div>
  )
}
