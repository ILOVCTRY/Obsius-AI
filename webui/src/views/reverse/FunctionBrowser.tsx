import { useMemo, useRef, useState } from "react"
import { useVirtualizer } from "@tanstack/react-virtual"
import type { CachedFuncRow, FuncEntry } from "@/lib/types"
import { hexAddr, parseAddrInput } from "@/lib/workbench"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { ImportsTable } from "./ImportsTable"
import { StringsTable } from "./StringsTable"

// 左栏：函数浏览器（风险置顶三段）+ 字符串表（v3）+ 导入表。cache 全量客观行 join func_kb。

type Item =
  | { kind: "header"; key: string; label: string }
  | { kind: "row"; key: string; row: CachedFuncRow; kb?: FuncEntry }

interface Props {
  pid: string
  sha: string
  rows: CachedFuncRow[]
  funcs: FuncEntry[]
  selected: string | null
  onSelect: (addr: string) => void
  imports: Record<string, string[]> | null
  cached: boolean
  stringsCount: number
}

export function FunctionBrowser(
  { pid, sha, rows, funcs, selected, onSelect, imports, cached, stringsCount }: Props,
) {
  const [query, setQuery] = useState("")
  const kbByAddr = useMemo(() => {
    const m = new Map<string, FuncEntry>()
    for (const f of funcs) m.set(hexAddr(f.address), f)
    return m
  }, [funcs])

  const items = useMemo<Item[]>(() => {
    const q = query.trim().toLowerCase()
    const addrQ = parseAddrInput(query)
    const match = (addr: string, name: string | null) => {
      if (!q) return true
      if (addrQ) return addr === addrQ || addr.startsWith(addrQ)
      return (name ?? "").toLowerCase().includes(q)
    }
    const risk: Item[] = []
    const analyzed: Item[] = []
    const plain: Item[] = []
    const seen = new Set<string>()
    const pushRow = (row: CachedFuncRow, kb: FuncEntry | undefined, keyPrefix: string) => {
      if (!match(row.address, row.name)) return
      const display = kb?.name || row.name || row.address
      const enriched = { ...row, name: display }
      if (kb?.risk_tags?.length) risk.push({ kind: "row", key: `${keyPrefix}-${row.address}`, row: enriched, kb })
      else if (kb) analyzed.push({ kind: "row", key: `${keyPrefix}-${row.address}`, row: enriched, kb })
      else plain.push({ kind: "row", key: `${keyPrefix}-${row.address}`, row: enriched })
    }
    for (const row of rows) {
      seen.add(hexAddr(row.address))
      pushRow(row, kbByAddr.get(hexAddr(row.address)), "p")
    }
    // 缓存缺席（未分诊/缓存被移走）时，func_kb 中分析过的函数仍列出：
    // MCP 在线可实时取伪码，离线则在详情侧看到启动提示
    for (const [addr, kb] of kbByAddr) {
      if (seen.has(addr)) continue
      pushRow({ address: addr, name: kb.name, size: 0, has_pseudo: false, n_calls: 0 }, kb, "k")
    }
    const out: Item[] = []
    if (risk.length) out.push({ kind: "header", key: "h-risk", label: `风险函数 ${risk.length}` }, ...risk)
    if (analyzed.length) out.push({ kind: "header", key: "h-analyzed", label: `已分析 ${analyzed.length}` }, ...analyzed)
    if (plain.length) out.push({ kind: "header", key: "h-all", label: `全部 ${plain.length}` }, ...plain)
    return out
  }, [rows, kbByAddr, query])

  const parentRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: (i) => (items[i]?.kind === "header" ? 24 : 30),
    overscan: 12,
  })

  return (
    <Tabs defaultValue="funcs" className="flex h-full min-h-0 flex-col gap-0">
      <div className="shrink-0 border-b p-1.5">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索函数名 / hex 地址…"
          className="h-7 text-[11px]"
        />
      </div>
      <TabsList className="w-full justify-start rounded-none border-b bg-transparent p-0">
        <TabsTrigger value="funcs" className="rounded-none border-b-2 px-3 py-1 text-[11px]">函数 {rows.length}</TabsTrigger>
        <TabsTrigger value="strings" className="rounded-none border-b-2 px-3 py-1 text-[11px]">
          字符串{stringsCount ? ` ${stringsCount}` : ""}
        </TabsTrigger>
        <TabsTrigger value="imports" className="rounded-none border-b-2 px-3 py-1 text-[11px]">导入表</TabsTrigger>
      </TabsList>
      <TabsContent value="funcs" className="min-h-0 flex-1">
        <div ref={parentRef} className="h-full overflow-auto">
          {items.length === 0 && (
            <p className="px-3 py-6 text-center text-[11px] text-muted-foreground">
              {!cached ? "尚未完成 headless 分诊，函数列表暂空" : "无匹配函数"}
            </p>
          )}
          <div style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
            {virtualizer.getVirtualItems().map((vi) => {
              const it = items[vi.index]!
              if (it.kind === "header") {
                return (
                  <div
                    key={it.key}
                    className="flex items-center bg-muted/40 px-2 text-[10px] font-medium text-muted-foreground"
                    style={{ position: "absolute", top: 0, left: 0, width: "100%", height: vi.size, transform: `translateY(${vi.start}px)` }}
                  >
                    {it.label}
                  </div>
                )
              }
              const active = selected === it.row.address
              return (
                <button
                  key={it.key}
                  type="button"
                  onClick={() => onSelect(it.row.address)}
                  className={cn(
                    "flex w-full items-center gap-1.5 border-l-2 px-2 text-left",
                    active ? "border-primary bg-primary/10" : "border-transparent hover:bg-accent/40",
                  )}
                  style={{ position: "absolute", top: 0, left: 0, width: "100%", height: vi.size, transform: `translateY(${vi.start}px)` }}
                >
                  <span className={cn("min-w-0 flex-1 truncate font-mono text-[11px]", it.kb?.risk_tags.length && "text-[--status-error]")}>
                    {it.row.name}
                  </span>
                  {it.kb?.risk_tags[0] && (
                    <span className="shrink-0 rounded bg-[--status-error]/15 px-1 text-[9px] text-[--status-error]">
                      {it.kb.risk_tags[0]}
                    </span>
                  )}
                  {it.kb && <span className="shrink-0 text-[10px] text-primary" title="已入 func_kb">✓</span>}
                  {!it.row.has_pseudo && <span className="shrink-0 text-[9px] text-muted-foreground" title="无伪码">asm</span>}
                  <span className="shrink-0 font-mono text-[9px] text-muted-foreground">{it.row.address}</span>
                </button>
              )
            })}
          </div>
        </div>
      </TabsContent>
      <TabsContent value="strings" className="min-h-0 flex-1">
        <StringsTable pid={pid} sha={sha} query={query} cached={cached} onSelectFunc={onSelect} />
      </TabsContent>
      <TabsContent value="imports" className="min-h-0 flex-1 overflow-auto">
        <ImportsTable imports={imports} />
      </TabsContent>
    </Tabs>
  )
}
