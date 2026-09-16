import { Handle, Position, type NodeProps } from "@xyflow/react"
import { X } from "lucide-react"
import type { EntityFlowNode } from "./chainNodes"
import { FINDING_CATEGORY_LABEL } from "@/lib/workbench"

// React Flow 自定义节点：ƒ函数（青边+hex 地址）/🔍发现（severity 色条+category）/📎产物。
// 实体已删=孤儿节点，灰显 [已删除] 但保留在链上（数据不级联删除，DESIGN §5.2）。

const SEV: Record<string, string> = {
  critical: "#f85149",
  high: "#ff7b72",
  medium: "#d29922",
  low: "#58a6ff",
  info: "#8b949e",
}

export function EntityNode({ data }: NodeProps<EntityFlowNode>) {
  const { link, onRemove } = data
  const e = link.entity

  return (
    <div
      className={`group relative w-[200px] rounded-md border bg-popover px-2.5 py-2 shadow-sm ${
        link.deleted ? "border-dashed border-muted-foreground/40 opacity-60"
        : link.node_type === "func_kb" ? "border-primary/50" : "border-[#39424e]"
      }`}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-muted-foreground" />
      {link.node_type === "finding" && !link.deleted && (
        <span
          className="absolute inset-y-0 left-0 w-0.5 rounded-l-md"
          style={{ background: SEV[e?.severity ?? "info"] ?? SEV.info }}
        />
      )}
      <button
        type="button"
        title="移出攻击链（不删实体）"
        onClick={(ev) => { ev.stopPropagation(); onRemove(link) }}
        className="absolute right-1 top-1 hidden rounded text-muted-foreground hover:text-(--status-error) group-hover:block"
      >
        <X className="size-3" />
      </button>

      {link.deleted ? (
        <div className="pr-4">
          <p className="text-[10px] text-muted-foreground">[已删除]</p>
          <p className="mt-0.5 font-mono text-[9px] text-muted-foreground">
            {link.node_type}:{link.node_id.slice(0, 14)}
          </p>
        </div>
      ) : link.node_type === "func_kb" ? (
        <div className="pr-4">
          <p className="text-[9px] text-muted-foreground">ƒ 函数</p>
          <p className="truncate text-xs text-primary">{e?.name}</p>
          <p className="font-mono text-[9px] text-muted-foreground">{e?.address}</p>
          {e?.risk_tags?.length ? (
            <p className="mt-0.5 truncate text-[9px] text-(--status-error)">{e.risk_tags.join(" · ")}</p>
          ) : null}
        </div>
      ) : link.node_type === "finding" ? (
        <div className="pr-4">
          <p className="text-[9px] text-muted-foreground">🔍 发现</p>
          <p className="line-clamp-2 text-[11px] leading-tight">{e?.title}</p>
          <div className="mt-1 flex items-center gap-1">
            {e?.category && (
              <span className="rounded bg-muted px-1 text-[9px] text-muted-foreground">
                {FINDING_CATEGORY_LABEL[e.category] ?? e.category}
              </span>
            )}
            <span
              className="font-mono text-[9px] uppercase"
              style={{ color: SEV[e?.severity ?? "info"] ?? SEV.info }}
            >
              {e?.severity}
            </span>
            {e?.status === "verified" && <span className="text-[9px] text-primary">✓</span>}
          </div>
        </div>
      ) : (
        <div className="pr-4">
          <p className="text-[9px] text-muted-foreground">📎 产物</p>
          <span className="rounded bg-muted px-1 text-[9px] text-muted-foreground">{e?.kind}</span>
          <p className="mt-0.5 truncate text-[10px]" title={e?.path}>{e?.description || e?.path}</p>
        </div>
      )}
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-muted-foreground" />
    </div>
  )
}
