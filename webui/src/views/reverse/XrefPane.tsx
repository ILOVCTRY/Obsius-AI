import type { XrefData, XrefRef } from "@/lib/types"
import { cn } from "@/lib/utils"

// 右栏 tab：xref。callers/callees 点击在缓存内跳转；导入函数（无地址）不可点。

function RefRow({ ref_, onSelect }: { ref_: XrefRef; onSelect: (addr: string) => void }) {
  const clickable = ref_.address !== null
  return (
    <button
      type="button"
      disabled={!clickable}
      onClick={() => ref_.address && onSelect(ref_.address)}
      className={cn(
        "flex w-full items-center gap-2 rounded px-1.5 py-1 text-left",
        clickable ? "hover:bg-accent/40" : "cursor-default opacity-70",
      )}
      title={clickable ? "跳转到函数" : "导入函数（缓存中无地址）"}
    >
      <span className="min-w-0 flex-1 truncate font-mono text-[11px]">{ref_.name}</span>
      {clickable
        ? <span className="shrink-0 font-mono text-[10px] text-primary">{ref_.address}</span>
        : <span className="shrink-0 rounded bg-muted px-1 text-[9px] text-muted-foreground">导入</span>}
    </button>
  )
}

export function XrefPane({
  xref, loading, onSelect,
}: { xref: XrefData | null; loading: boolean; onSelect: (addr: string) => void }) {
  if (loading) return <p className="p-3 text-[11px] text-muted-foreground">加载 xref…</p>
  if (!xref) return <p className="p-3 text-[11px] text-muted-foreground">选中函数后显示调用关系</p>
  return (
    <div className="space-y-3 p-2">
      {xref.source === "mcp" && (
        <p className="px-1 text-[9px] text-primary" title="headless 缓存缺席，经 MCP func_profile 从 IDA 当前库实时取得">
          MCP 实时 · {xref.name ?? xref.address}
        </p>
      )}
      {xref.source === "cache" && (
        <p className="px-1 text-[9px] text-muted-foreground" title="按需详情缓存（自动从 IDA MCP 拉取落盘，离线可读）">
          已缓存 · {xref.name ?? xref.address}
        </p>
      )}
      <section>
        <h4 className="mb-1 px-1 text-[10px] font-medium text-muted-foreground">被谁调用 · {xref.callers.length}</h4>
        {xref.callers.length === 0
          ? <p className="px-1 font-mono text-[10px] text-muted-foreground">（无）</p>
          : xref.callers.map((r) => <RefRow key={`c-${r.address}-${r.name}`} ref_={r} onSelect={onSelect} />)}
      </section>
      <section>
        <h4 className="mb-1 px-1 text-[10px] font-medium text-muted-foreground">调用了 · {xref.callees.length}</h4>
        {xref.callees.length === 0
          ? <p className="px-1 font-mono text-[10px] text-muted-foreground">（无）</p>
          : xref.callees.map((r) => <RefRow key={`e-${r.address}-${r.name}`} ref_={r} onSelect={onSelect} />)}
      </section>
    </div>
  )
}
