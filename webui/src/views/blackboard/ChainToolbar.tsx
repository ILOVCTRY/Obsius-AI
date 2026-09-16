import { useState } from "react"
import { ChevronDown } from "lucide-react"
import type { ChainStatus, ChainSummary } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// 画布顶链工具条（E1）：链下拉（名+goal+状态灯）、新建链、状态单向流转
// hypothesis → validated → exploited（只许前进不许降级，安全默认宁严勿松）。

export const STATUS_DOT: Record<ChainStatus, string> = {
  hypothesis: "#d29922",
  validated: "#58a6ff",
  exploited: "#f85149",
}

const NEXT: Record<ChainStatus, ChainStatus | null> = {
  hypothesis: "validated",
  validated: "exploited",
  exploited: null,
}

const NEXT_LABEL: Record<ChainStatus, string> = {
  hypothesis: "证实 → validated",
  validated: "确认利用 → exploited",
  exploited: "已到终态",
}

export function ChainToolbar({ chains, selectedId, onSelect, onCreate, onAdvance, mutating }: {
  chains: ChainSummary[]
  selectedId: string
  onSelect: (id: string) => void
  onCreate: (name: string, goal: string) => Promise<void>
  onAdvance: (next: ChainStatus) => Promise<void>
  mutating: boolean
}) {
  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState("")
  const [goal, setGoal] = useState("")
  const [err, setErr] = useState("")

  const sel = chains.find((c) => c.id === selectedId)
  const next = sel ? NEXT[sel.status] : null

  const submitCreate = async () => {
    setErr("")
    if (!name.trim()) { setErr("请填写链名称"); return }
    try {
      await onCreate(name.trim(), goal.trim())
      setName(""); setGoal(""); setCreating(false)
    } catch (e) {
      setErr(String(e))
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* 链下拉 */}
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex h-7 min-w-52 max-w-80 items-center gap-1.5 rounded border px-2 text-[11px]"
        >
          {sel ? (
            <>
              <span className="size-2 shrink-0 rounded-full" style={{ background: STATUS_DOT[sel.status] }} />
              <span className="shrink-0 font-medium">{sel.name}</span>
              {sel.goal && <span className="min-w-0 flex-1 truncate text-muted-foreground">{sel.goal}</span>}
            </>
          ) : (
            <span className="text-muted-foreground">{chains.length ? "选择攻击链" : "尚无攻击链"}</span>
          )}
          <ChevronDown className={cn("size-3 shrink-0 text-muted-foreground", open && "rotate-180")} />
        </button>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <div className="absolute left-0 top-full z-50 mt-1 w-80 rounded-md border bg-popover p-1 shadow-md">
              {chains.length === 0 && (
                <p className="px-2 py-2 text-[11px] text-muted-foreground">暂无链，新建一条。</p>
              )}
              {chains.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => { onSelect(c.id); setOpen(false) }}
                  className={cn("flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[11px] hover:bg-accent/40",
                    c.id === selectedId && "bg-primary/10")}
                >
                  <span className="size-2 shrink-0 rounded-full" style={{ background: STATUS_DOT[c.status] }} />
                  <span className="shrink-0">{c.name}</span>
                  {c.goal && <span className="min-w-0 flex-1 truncate text-muted-foreground">{c.goal}</span>}
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{c.link_count}</span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      {sel && (
        <Button
          size="sm" variant="outline" className="h-7 text-[11px]"
          disabled={!next || mutating}
          title={next ? "链状态单向流转" : "终态不可再流转"}
          onClick={() => next && onAdvance(next)}
        >
          {NEXT_LABEL[sel.status]}
        </Button>
      )}

      {!creating ? (
        <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={() => setCreating(true)}>
          ＋新建链
        </Button>
      ) : (
        <div className="flex items-center gap-1 rounded border p-1">
          <Input className="h-6 w-40 text-[11px]" placeholder="链名称 *"
                 value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          <Input className="h-6 w-48 text-[11px]" placeholder="目标 goal（可选）"
                 value={goal} onChange={(e) => setGoal(e.target.value)} />
          <Button size="sm" className="h-6 text-[11px]" disabled={mutating} onClick={submitCreate}>建链</Button>
          <Button size="sm" variant="ghost" className="h-6 text-[11px]"
                  onClick={() => { setCreating(false); setErr("") }}>取消</Button>
        </div>
      )}
      {err && <span className="text-[11px] text-(--status-error)">{err}</span>}
    </div>
  )
}
