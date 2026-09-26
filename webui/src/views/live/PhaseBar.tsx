import { useCallback, useEffect, useState } from "react"
import { api, ApiError } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { PhaseInfo } from "@/lib/types"

/**
 * 分阶段工作流阶段条（pentest-phased-workflow M4，方案 §4.7 / 打磨定稿 #9）：
 * 三阶段步骤序（当前高亮/到访过亮字/未到灰）+ 当前阶段门进度（达标绿、未达标
 * 琥珀明细）+ 前向流转入口（人工最终——不强制门，422 带原因行内显）。
 * 数据源 GET /projects/{pid}/phase，5s 轮询（对齐 sessions 轮询节奏）；
 * 轨无阶段剧本（enabled=false）渲染 null，由 LiveRoom 无感挂载。
 */

export function PhaseBar({ pid }: { pid: string }) {
  const [info, setInfo] = useState<PhaseInfo | null>(null)
  const [err, setErr] = useState("")
  const [busy, setBusy] = useState(false)

  const reload = useCallback(() => {
    api.projectPhase(pid).then((d) => {
      setInfo(d)
      setErr("")
    }).catch(() => setInfo(null)) // 404（项目删除中）静默退场，轮询自然停于卸载
  }, [pid])

  useEffect(() => {
    reload()
    const t = setInterval(reload, 5000)
    return () => clearInterval(t)
  }, [reload])

  if (!info?.enabled || !info.spec || !info.gate) return null
  const { spec, gate } = info
  const visited = new Set(info.history?.map((h) => h.phase))
  const nameOf = (id: string) => info.phases?.find((p) => p.id === id)?.name ?? id
  const hasGate = spec.gate && Object.keys(spec.gate).length > 0

  const advance = async (to: string) => {
    setBusy(true)
    setErr("")
    try {
      await api.transitionPhase(pid, to)
      reload()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-9 shrink-0 items-center gap-2 border-b px-2 text-[11px]">
      <span className="shrink-0 text-muted-foreground">阶段</span>
      <div className="flex min-w-0 items-center gap-1 overflow-x-auto">
        {info.phases?.map((p, i) => (
          <span key={p.id} className="flex shrink-0 items-center gap-1">
            {i > 0 && <span className="text-muted-foreground/50">→</span>}
            <span
              className={cn(
                "rounded px-1.5 py-0.5",
                p.id === spec.id
                  ? "bg-secondary font-medium text-primary"
                  : visited.has(p.id)
                    ? "text-foreground/80"
                    : "text-muted-foreground",
              )}
              title={p.id === spec.id ? spec.goal || "" : undefined}
            >
              {p.name}
            </span>
          </span>
        ))}
      </div>
      {hasGate && (
        <span className="flex shrink-0 items-center gap-1.5" title="入场门：当前阶段出口门指标（唯一硬约束，exploit 类任务被拦直至过门）">
          <span className="text-muted-foreground">门</span>
          {gate.met ? (
            <span className="text-(--status-ok)">已达标</span>
          ) : (
            <span className="truncate text-amber-400" title={gate.unmet.join("；")}>
              {gate.unmet.join(" · ")}
            </span>
          )}
        </span>
      )}
      <span className="flex-1" />
      {gate.forward.map((to) => (
        <button
          key={to}
          type="button"
          disabled={busy}
          onClick={() => void advance(to)}
          title={`流转到「${nameOf(to)}」（人工流转不强制门；到访过的阶段回退重进，剧本已发任务不重发）`}
          className="shrink-0 rounded border bg-card px-2 py-0.5 text-[11px] whitespace-nowrap text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
        >
          → {nameOf(to)}
        </button>
      ))}
      {err && (
        <span className="max-w-80 shrink-0 truncate text-(--status-error)" title={err}>
          {err}
        </span>
      )}
    </div>
  )
}
