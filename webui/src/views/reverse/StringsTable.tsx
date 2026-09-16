import { useEffect, useRef, useState } from "react"
import { useVirtualizer } from "@tanstack/react-virtual"
import { api } from "@/lib/api"
import type { BinaryString } from "@/lib/types"
import { cn } from "@/lib/utils"

// 左栏第三 tab：v3 字符串表。共享搜索框经 ?q 子串过滤（debounce 300ms）；
// refs 显示引用所在函数名，点击跳该函数。5000 行截断由后端控制，UI 只提示。

interface Props {
  pid: string
  sha: string
  query: string
  cached: boolean
  onSelectFunc: (addr: string) => void
}

export function StringsTable({ pid, sha, query, cached, onSelectFunc }: Props) {
  const [items, setItems] = useState<BinaryString[]>([])
  const [truncated, setTruncated] = useState(false)
  const [unavailable, setUnavailable] = useState<string | null>(null)
  const parentRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!cached) {
      setItems([]); setTruncated(false); setUnavailable(null)
      return
    }
    let alive = true
    setUnavailable(null)
    const t = setTimeout(() => {
      const q = query.trim()
      api.binaryStrings(pid, sha, q || undefined)
        .then((r) => {
          if (!alive) return
          setItems(r.items); setTruncated(r.truncated)
        })
        .catch((e) => {
          if (!alive) return
          setItems([]); setTruncated(false); setUnavailable(String(e))
        })
    }, 300)
    return () => { alive = false; clearTimeout(t) }
  }, [pid, sha, query, cached])

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 34,
    overscan: 12,
  })

  if (!cached) {
    return <p className="px-3 py-6 text-center text-[11px] text-muted-foreground">
      尚未完成 headless 分诊，字符串表暂空
    </p>
  }
  if (unavailable) {
    return <p className="px-3 py-6 text-center text-[11px] text-muted-foreground">
      字符串段不可用（{unavailable}）
    </p>
  }

  return (
    <div className="flex h-full flex-col">
      <div ref={parentRef} className="min-h-0 flex-1 overflow-auto">
        {items.length === 0 && (
          <p className="px-3 py-6 text-center text-[11px] text-muted-foreground">
            {query.trim() ? "无匹配字符串" : "无字符串"}
          </p>
        )}
        <div style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
          {virtualizer.getVirtualItems().map((vi) => {
            const s = items[vi.index]!
            return (
              <div
                key={`${s.address}-${vi.index}`}
                className="absolute left-0 flex w-full items-start gap-1.5 border-l-2 border-transparent px-2 py-1 hover:bg-accent/40"
                style={{ top: 0, height: vi.size, transform: `translateY(${vi.start}px)` }}
                title={s.string}
              >
                <span className="shrink-0 font-mono text-[9px] text-muted-foreground">{s.address}</span>
                <span className={cn(
                  "mt-0.5 shrink-0 rounded px-1 text-[8px]",
                  s.type === "unicode"
                    ? "bg-(--status-approval)/15 text-(--status-approval)"
                    : "bg-muted text-muted-foreground",
                )}>{s.type === "unicode" ? "U" : "C"}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-[10px]">{s.string}</span>
                {s.refs[0] && (
                  <button
                    type="button"
                    className="shrink-0 max-w-[8rem] truncate rounded bg-primary/10 px-1 text-[9px] text-primary hover:bg-primary/20"
                    title={`引用所在函数：${s.refs[0].func_name ?? s.refs[0].func}${s.refs.length > 1 ? `（共 ${s.refs.length} 处）` : ""}`}
                    onClick={() => onSelectFunc(s.refs[0]!.func)}
                  >
                    →{s.refs[0].func_name ?? s.refs[0].func}
                    {s.refs.length > 1 ? ` +${s.refs.length - 1}` : ""}
                  </button>
                )}
              </div>
            )
          })}
        </div>
      </div>
      {truncated && (
        <p className="shrink-0 border-t px-2 py-0.5 text-[9px] text-muted-foreground">
          结果超过 5000 行已截断，请用搜索缩小范围
        </p>
      )}
    </div>
  )
}
