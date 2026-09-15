import { useEffect, useMemo, useRef, useState } from "react"
import { FileUp, Loader2 } from "lucide-react"
import { api } from "@/lib/api"
import type { Finding } from "@/lib/types"
import { FINDING_CATEGORY_LABEL, sameAddr } from "@/lib/workbench"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { AddToChainButton } from "./chains/AddToChainDialog"

// 右栏 tab：与当前样本/函数相关的发现 + 行内调试日志回流（动态验证 → verified）。

interface Props {
  pid: string
  findings: Finding[]
  assetId: string | null
  addr: string | null
  kbId: string | null
  onChanged: () => void
  /** 从攻击链节点跳来时高亮并滚动到该发现 */
  focusId?: string | null
}

export function RelatedFindings({ pid, findings, assetId, addr, kbId, onChanged, focusId }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const fileRefs = useRef<Map<string, HTMLInputElement>>(new Map())

  const { related, hitsAddr } = useMemo(() => {
    const hitsAddr = new Set<string>()
    const related = findings.filter((f) => {
      if (assetId && f.target_asset_id === assetId) return true
      const ev = f.evidence ?? {}
      if (kbId && ev.func_id === kbId) {
        if (addr) hitsAddr.add(f.id)
        return true
      }
      if (addr && sameAddr(ev.address, addr)) {
        hitsAddr.add(f.id)
        return true
      }
      return false
    })
    return { related, hitsAddr }
  }, [findings, assetId, addr, kbId])

  const confirmWithLog = async (f: Finding, file: File) => {
    setBusy(f.id)
    setErr(null)
    try {
      const art = await api.uploadDebugLog(pid, file)
      await api.patchFinding(pid, f.id, {
        status: "verified",
        evidence: {
          confirm_by: "dynamic",
          debug_log_artifact_id: art.id,
          confirmed_at: new Date().toISOString(),
        },
      })
      onChanged()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(null)
    }
  }

  // 从攻击链定位来：高亮 + 滚动（focusId 不在当前过滤集时静默不滚）
  const focusRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (focusId && related.some((f) => f.id === focusId)) {
      focusRef.current?.scrollIntoView({ block: "center", behavior: "smooth" })
    }
  }, [focusId, related])

  if (related.length === 0) {
    return (
      <p className="p-3 text-[11px] text-muted-foreground">
        还没有相关发现（AI triage 或人工添加后出现在这里）
        {focusId && "——该发现可能挂在别的样本上"}
      </p>
    )
  }

  return (
    <div className="space-y-1.5 p-2">
      {err && <p className="rounded bg-[--status-error]/10 p-1.5 text-[10px] text-[--status-error]">{err}</p>}
      {related.map((f) => {
        const cat = typeof f.evidence?.category === "string"
          ? FINDING_CATEGORY_LABEL[f.evidence.category as string] ?? f.evidence.category
          : null
        const pinned = hitsAddr.has(f.id)
        return (
          <div
            key={f.id}
            ref={focusId === f.id ? focusRef : undefined}
            className={cn(
              "rounded border p-1.5",
              pinned ? "border-primary/50 bg-primary/5" : "hover:bg-accent/30",
              focusId === f.id && "ring-2 ring-primary/60",
            )}
          >
            <div className="flex items-center gap-1.5">
              <span className="min-w-0 flex-1 truncate text-[11px]">{f.title}</span>
              {f.status === "verified"
                ? <Badge variant="outline" className="shrink-0 text-[9px] text-primary">verified</Badge>
                : <Badge variant="outline" className="shrink-0 text-[9px]">unverified</Badge>}
            </div>
            <div className="mt-0.5 flex items-center gap-1">
              {cat && <span className="rounded bg-muted px-1 text-[9px] text-muted-foreground">{cat}</span>}
              <span className="font-mono text-[9px] uppercase text-muted-foreground">{f.severity}</span>
              <span className="flex-1" />
              <AddToChainButton pid={pid} nodeType="finding" nodeId={f.id} />
              {f.status !== "verified" && (
                <>
                  <input
                    ref={(el) => { if (el) fileRefs.current.set(f.id, el) }}
                    type="file"
                    className="hidden"
                    onChange={(e) => {
                      const file = e.target.files?.[0]
                      if (file) confirmWithLog(f, file)
                      e.target.value = ""
                    }}
                  />
                  <button
                    type="button"
                    disabled={busy === f.id}
                    onClick={() => fileRefs.current.get(f.id)?.click()}
                    title="上传 x64dbg 调试日志作为动态验证凭据（confirm_by=dynamic → verified）"
                    className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-primary hover:bg-primary/10 disabled:opacity-50"
                  >
                    {busy === f.id ? <Loader2 className="size-3 animate-spin" /> : <FileUp className="size-3" />}
                    传日志确认
                  </button>
                </>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
