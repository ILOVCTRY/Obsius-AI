import { useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { Approval } from "@/lib/types"

/** 待审批数据源（审批模块下线后，对话内联「审批问答卡」的唯一数据源）。
 *  - 3s 轮询 `api.approvals(pid, "pending")`；
 *  - `wakeKey` 变化即立即重拉——对话页监听到新的 `approval.requested` 事件时传事件 id，
 *    让卡片「即弹」而不等下一拍轮询。
 *  事件载荷本身只有 `{approval_id, op, summary(截断), risk}`，卡片要的完整 `action`/`boundary`
 *  只有这个列表端点给得到，故一律以本 hook 为准（后端零改动）。 */
export function usePendingApprovals(pid: string | null, wakeKey: string | number = 0) {
  const [items, setItems] = useState<Approval[]>([])

  useEffect(() => {
    if (!pid) { setItems([]); return }
    let alive = true
    const tick = () => {
      api.approvals(pid, "pending")
        .then((rows) => { if (alive) setItems(rows) })
        .catch(() => { /* 轮询失败静默，下一拍重试 */ })
    }
    tick()
    const t = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, wakeKey])

  const byId = useMemo(() => new Map(items.map((a) => [a.id, a])), [items])
  return { items, byId }
}
