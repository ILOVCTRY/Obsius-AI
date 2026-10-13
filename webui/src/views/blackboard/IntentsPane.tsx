import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { Asset, IntentInfo } from "@/lib/types"
import { fmtDateTime } from "@/lib/datetime"
import { cn } from "@/lib/utils"

// 意图清单（黑板「意图」tab，2026-10-13）：把 Agent `declare_intent` 的规划产物
// 摊平成人类可核对的列表。渗透轨本有「发现 → 链路」图可看意图，其余轨（如逆向）
// 此前无任何入口——只能从对话流的工具调用里看到「声明了意图」，看不到落在哪。
// 数据源 GET /api/projects/{pid}/intents（open/closed 全量），已收尾可重开
// （POST …/intents/{id}/reopen，人类否决收尾）。

const OUTCOME: Record<string, { label: string; cls: string }> = {
  vuln: { label: "漏洞", cls: "text-(--status-error)" },
  finding: { label: "有效发现", cls: "text-(--status-ok)" },
  dead_end: { label: "死路", cls: "text-muted-foreground" },
}

export function IntentsPane({ pid }: { pid: string }) {
  const [items, setItems] = useState<IntentInfo[]>([])
  const [assets, setAssets] = useState<Asset[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState("")

  useEffect(() => {
    let alive = true
    const load = () => {
      api.intents(pid).then((r) => { if (alive) setItems(r) }).catch(() => {})
      api.assets(pid).then((r) => { if (alive) setAssets(r) }).catch(() => {})
    }
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  const assetById = new Map(assets.map((a) => [a.id, a]))
  const open = items.filter((i) => i.status === "open")
  const closed = items.filter((i) => i.status === "closed")
    .sort((a, b) => (b.closed_at ?? "").localeCompare(a.closed_at ?? ""))

  const reopen = async (id: string) => {
    setBusy(id); setErr("")
    try {
      await api.reopenIntent(pid, id, "黑板意图列表重开")
      setItems(await api.intents(pid))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const row = (it: IntentInfo) => {
    const target = it.target_asset_id ? assetById.get(it.target_asset_id) : null
    const outcome = it.status === "closed" ? OUTCOME[it.outcome_type] : null
    return (
      <div key={it.id} className="rounded border p-2">
        <div className="flex items-start gap-2">
          <span className={cn("mt-1 size-1.5 shrink-0 rounded-full",
            it.status === "open" ? "animate-pulse bg-(--status-warning)" : "bg-(--status-ok)")} />
          <p className="min-w-0 flex-1 text-xs leading-snug">{it.statement}</p>
          {outcome && (
            <span className={cn("shrink-0 text-[10px] font-medium", outcome.cls)}>
              {outcome.label}{it.outcome_refs?.length ? `×${it.outcome_refs.length}` : ""}
            </span>
          )}
          {it.status === "closed" && (
            <button type="button" disabled={busy === it.id}
              onClick={() => void reopen(it.id)}
              className="shrink-0 rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50">
              {busy === it.id ? "…" : "重开"}
            </button>
          )}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 pl-3.5 text-[10px] text-muted-foreground">
          {target
            ? <span className="font-mono">🎯 {target.value}
                <span className="ml-1 opacity-60">{target.type}</span></span>
            : it.target_asset_id
              ? <span className="font-mono opacity-60">🎯 {it.target_asset_id}</span>
              : <span className="opacity-60">无资产锚点</span>}
          {it.dimension && <span>维度 {it.dimension}</span>}
          <span className="font-mono opacity-70">{it.author}</span>
          <span>{fmtDateTime(it.created_at)}</span>
          {it.status === "closed" && it.closed_at && <span>收尾 {fmtDateTime(it.closed_at)}</span>}
        </div>
        {!!it.basis_refs?.length && (
          <p className="mt-0.5 truncate pl-3.5 font-mono text-[10px] text-muted-foreground/70"
             title={it.basis_refs.join("\n")}>
            依据 {it.basis_refs.join("、")}
          </p>
        )}
        {it.status === "closed" && it.dead_reason && (
          <p className="mt-0.5 pl-3.5 text-[10px] text-muted-foreground">死因 {it.dead_reason}</p>
        )}
      </div>
    )
  }

  const section = (title: string, rows: IntentInfo[]) => (
    <div className="space-y-1.5">
      <div className="text-[11px] font-medium text-muted-foreground">{title}（{rows.length}）</div>
      {rows.length
        ? rows.map(row)
        : <p className="px-1 text-[11px] text-muted-foreground/70">无</p>}
    </div>
  )

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b px-3 py-1.5 text-[11px]">
        <span>意图 {items.length}</span>
        <span className="text-muted-foreground">待收尾 {open.length} · 已收尾 {closed.length}</span>
        {err && <span className="ml-auto truncate text-(--status-error)" title={err}>{err}</span>}
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-auto p-3">
        {items.length ? (
          <>
            {section("待收尾", open)}
            {section("已收尾", closed)}
          </>
        ) : (
          <p className="pt-6 text-center text-[11px] text-muted-foreground">
            尚无意图。Agent 用 <span className="font-mono">declare_intent</span> 声明后出现在此；
            每条意图必须收尾（漏洞 / 有效发现 / 死路）。
          </p>
        )}
      </div>
    </div>
  )
}
