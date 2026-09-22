import { useEffect, useMemo, useState, type ReactNode } from "react"
import { api } from "@/lib/api"
import type { BoardGraph, BoardGraphNode, TraceEffect } from "@/lib/types"
import { STATUS_DOT } from "../ChainToolbar"
import { CTF_LEVEL } from "../ctfLevel"
import { TYPE_ACCENT } from "./BoardNode"

// 战果主线（execution-trace-chain M2 R7 + M3，DESIGN §12）：board_graph 同页切换档。
// 过滤子图「目标资产 → verified 发现 → exploited 链」三列布局——unverified /
// false-positive 不上主线（与发现卡门禁口径一致）；底部 = 打法效果榜（R4 基于
// 物化侧轨迹链：(skill × kb) 组合 × verified finding 计数 top5）。

const SEV_ORDER: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3, info: 4 }

function Col({ title, count, accent, children }: {
  title: string; count: number; accent: string; children: ReactNode
}) {
  return (
    <section className="min-w-0">
      <p className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
        <span className="size-2 rounded-full" style={{ background: accent }} />
        {title}
        <span className="font-mono text-muted-foreground/70">{count}</span>
      </p>
      <div className="mt-1.5 space-y-1.5">{children}</div>
    </section>
  )
}

export function MainlineView({ pid, track, graph, openFinding }: {
  pid: string
  track?: string
  graph: BoardGraph | null
  openFinding: (id: string) => void
}) {
  const isCtf = track === "ctf"
  const [effect, setEffect] = useState<TraceEffect | null>(null)

  // 效果榜：跟全景图 4s 轮询同节奏重取（graph 引用随轮询换新，轻量只读聚合）
  useEffect(() => {
    let alive = true
    api.traceEffect(pid, 5).then((e) => { if (alive) setEffect(e) }).catch(() => {})
    return () => { alive = false }
  }, [pid, graph])

  const nodeById = useMemo(
    () => new Map((graph?.nodes ?? []).map((n) => [n.id, n])),
    [graph])

  const verified = useMemo(() => (graph?.nodes ?? [])
    .filter((n) => n.node_type === "finding" && n.status === "verified")
    .sort((a, b) =>
      (SEV_ORDER[a.severity ?? ""] ?? 9) - (SEV_ORDER[b.severity ?? ""] ?? 9) ||
      (a.created_at ?? "").localeCompare(b.created_at ?? "")),
    [graph])

  // 左列目标资产：verified 发现的 targets 资产（去重保持出现序）
  const targets = useMemo(() => {
    const seen = new Set<string>()
    const list: BoardGraphNode[] = []
    for (const f of verified) {
      const aid = f.target_asset_id
      if (!aid || seen.has(aid)) continue
      const a = nodeById.get(aid)
      if (a) { seen.add(aid); list.push(a) }
    }
    return list
  }, [verified, nodeById])

  // 右列 exploited 链：chain 边按 chain_id 去重；成员 verified 发现可点开详情
  const exploited = useMemo(() => {
    const byId = new Map<string, { id: string; name: string; findings: BoardGraphNode[] }>()
    for (const e of graph?.edges ?? []) {
      if (e.kind !== "chain" || e.chain_status !== "exploited" || !e.chain_id) continue
      let c = byId.get(e.chain_id)
      if (!c) { c = { id: e.chain_id, name: e.chain_name ?? "", findings: [] }; byId.set(e.chain_id, c) }
      for (const nid of [e.source, e.target]) {
        const n = nodeById.get(nid)
        if (n?.node_type === "finding" && n.status === "verified"
            && !c.findings.some((x) => x.id === nid)) c.findings.push(n)
      }
    }
    return [...byId.values()]
  }, [graph, nodeById])

  const empty = verified.length === 0 && exploited.length === 0

  return (
    <div className="absolute inset-0 overflow-auto pt-10">
      {empty && (
        <div className="flex h-1/2 items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
            主线还是空的——verified 发现与 exploited 链才登上战果主线（unverified 不上图）
          </p>
        </div>
      )}
      <div className="mx-auto grid max-w-6xl grid-cols-3 items-start gap-4 p-3">
        <Col title="目标资产" count={targets.length} accent={TYPE_ACCENT.asset}>
          {targets.map((a) => (
            <div key={a.id} className="rounded border border-[#30363d] bg-[#161b22] px-2 py-1.5">
              <p className="truncate text-[12px] font-medium">{a.label}</p>
              <p className="truncate font-mono text-[10px] text-muted-foreground">{a.value || a.sub}</p>
            </div>
          ))}
        </Col>
        <Col title="verified 发现" count={verified.length} accent="#3fb950">
          {verified.map((f) => (
            <button key={f.id} type="button"
                    className="block w-full rounded border border-[#30363d] bg-[#161b22] px-2 py-1.5 text-left transition-colors hover:border-[#3d444d] hover:bg-[#1c2128]"
                    onClick={() => openFinding(f.id)}>
              <p className="flex min-w-0 items-baseline gap-1.5">
                <span className="min-w-0 flex-1 truncate text-[12px] font-medium">
                  {f.title || f.label}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                  {isCtf ? CTF_LEVEL[f.severity ?? ""]?.label : f.severity}
                </span>
              </p>
              <p className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">
                {f.vuln_class || "rev"}
                {f.target_asset_id && nodeById.get(f.target_asset_id) && (
                  <> → {nodeById.get(f.target_asset_id)!.label}</>
                )}
              </p>
              {(f.author ?? "").startsWith("sess-") && (
                <p className="mt-0.5 text-[10px] text-sky-400">对话产出</p>
              )}
            </button>
          ))}
        </Col>
        <Col title="exploited 链" count={exploited.length} accent={STATUS_DOT.exploited}>
          {exploited.map((c) => (
            <div key={c.id} className="rounded border border-[#30363d] bg-[#161b22] px-2 py-1.5">
              <p className="flex items-baseline gap-1.5">
                <span className="size-2 shrink-0 rounded-full" style={{ background: STATUS_DOT.exploited }} />
                <span className="min-w-0 flex-1 truncate text-[12px] font-medium">{c.name}</span>
              </p>
              {c.findings.length > 0 && (
                <div className="mt-1 space-y-0.5">
                  {c.findings.map((f) => (
                    <button key={f.id} type="button"
                            className="block max-w-full truncate text-left font-mono text-[10px] text-muted-foreground hover:text-foreground"
                            onClick={() => openFinding(f.id)}>
                      📌 {f.title || f.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </Col>
      </div>

      {/* 打法效果榜（M3）：基于物化侧轨迹链的组合计数 */}
      <div className="mx-auto max-w-6xl px-3 pb-6">
        <div className="rounded border border-[#30363d] bg-[#161b22] p-2.5">
          <p className="flex items-baseline gap-2 text-[11px] font-medium text-muted-foreground">
            🏆 打法效果榜（skill × kb 组合 × verified）
            {effect && (
              <span className="font-mono text-[10px]">
                轨迹链 {effect.trace_chains} · validated {effect.validated_chains}
              </span>
            )}
          </p>
          {!effect || effect.combos.length === 0 ? (
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              暂无打法组合——任务收尾自动物化轨迹链（origin=trace），发现 verified 后计入
            </p>
          ) : (
            <ol className="mt-1.5 space-y-1">
              {effect.combos.map((c, i) => (
                <li key={`${c.skill}-${c.kb}`} className="flex items-baseline gap-2 text-[11px]">
                  <span className="w-4 shrink-0 font-mono text-muted-foreground">{i + 1}.</span>
                  <span className="shrink-0">🧠 {c.skill}</span>
                  <span className="shrink-0 text-muted-foreground">×</span>
                  <span className="min-w-0 truncate font-mono">📖 {c.kb}</span>
                  <span className="ml-auto shrink-0 font-mono text-muted-foreground">
                    {c.chains} 链 · {c.verified_findings} verified
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </div>
  )
}
