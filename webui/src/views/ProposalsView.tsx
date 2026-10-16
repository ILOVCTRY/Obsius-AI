import { useCallback, useEffect, useState } from "react"
import { Sparkles } from "lucide-react"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import { ProposalsPane } from "@/components/settings/ProposalsPane"

// 变更提案（工作区顶级入口，2026-10-14）：packs 级提案审批——知识库文档与技能的
// 新建/改写/删除（含技能删除进回收站），应用前自动备份 + 引用联动。提案全局共享；
// 顶部「知识库迭代」开关为**项目级**（2026-10-15 起为 Agent 一切提案的总闸：
// 开启才允许轮末按信号自动提案与 propose_pack_edit 主动沉淀，关闭一律不提），
// 仅在有项目上下文（pid）时显示。

export function ProposalsView({ pid }: { pid?: string }) {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(() => {
    if (!pid) { setEnabled(null); return }
    api.getProject(pid).then((p) => {
      const cfg = (p.config?.kb_iteration ?? {}) as { enabled?: boolean }
      setEnabled(!!cfg.enabled)
    }).catch(() => setEnabled(false))
  }, [pid])
  useEffect(load, [load])

  const toggle = async (next: boolean) => {
    if (!pid) return
    setBusy(true)
    setErr(null)
    try {
      await api.patchProjectConfig(pid, { kb_iteration: { enabled: next } })
      setEnabled(next)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b px-3 py-2">
        <span className="text-xs font-medium">变更提案</span>
        <span className="text-[11px] text-muted-foreground">
          Agent 提出的知识库/技能变更在此审批；应用前自动备份、删除进回收站
        </span>
        <span className="min-w-0 flex-1" />
        {pid && enabled !== null && (
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            disabled={busy}
            onClick={() => void toggle(!enabled)}
            title="Agent 提案总闸：开启后主控轮末按信号（试错多 / 文档有误 / 新 Web 漏洞）自动提案、专家也可主动沉淀；关闭时 Agent 不提任何提案。提案仍全部人审"
            className={cn("kbiter-toggle", enabled && "is-on")}
          >
            <Sparkles />
            <span>知识库迭代</span>
            <span className="kbiter-tag">本项目</span>
            <span className="kbiter-switch" aria-hidden><i /></span>
          </button>
        )}
      </header>
      {err && <p className="shrink-0 px-3 py-1 text-[11px] text-(--status-error)">{err}</p>}
      <div className="min-h-0 flex-1">
        <ProposalsPane />
      </div>
    </div>
  )
}
