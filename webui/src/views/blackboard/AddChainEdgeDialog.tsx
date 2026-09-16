import { useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { Chain, ChainStatus, ChainSummary, Finding } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// 评估画布「手拖连线 / 强边确认入链」对话框（E1）：
// 选已有链或新建（名+goal），edge_note 必填；目标 finding 追加到链尾。

const STATUS_DOT: Record<ChainStatus, string> = {
  hypothesis: "#d29922", // 黄
  validated: "#58a6ff",  // 蓝
  exploited: "#f85149",  // 红
}

export interface ChainEdgeRequest {
  source: Finding | null // 手拖有来源；卡片「加入链」可只有目标
  target: Finding
  presetNote?: string    // 从 relates_to 强边带入的关联理由
}

export function AddChainEdgeDialog({ pid, req, chains, onDone, onClose }: {
  pid: string
  req: ChainEdgeRequest
  chains: ChainSummary[]
  onDone: () => void
  onClose: () => void
}) {
  const [details, setDetails] = useState<Map<string, Chain>>(new Map())
  const [mode, setMode] = useState<"existing" | "new">("existing")
  const [cid, setCid] = useState("")
  const [name, setName] = useState("")
  const [goal, setGoal] = useState("")
  const [note, setNote] = useState(req.presetNote ?? "")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState("")

  // 拉各链详情判断成员归属（与逆向 AddToChainDialog 同一交互语言）
  useEffect(() => {
    let alive = true
    Promise.allSettled(chains.map((c) => api.chain(pid, c.id)))
      .then((rs) => {
        if (!alive) return
        const m = new Map<string, Chain>()
        rs.forEach((r, i) => { if (r.status === "fulfilled") m.set(chains[i].id, r.value) })
        setDetails(m)
        // 默认选第一个目标不在其中的链
        const firstFree = chains.find((c) =>
          !memberIds(m.get(c.id)).has(req.target.id))
        if (firstFree) setCid(firstFree.id)
      })
    return () => { alive = false }
  }, [pid, chains, req.target.id])

  const memberOf = useMemo(() => {
    const m = new Map<string, Set<string>>()
    for (const [id, ch] of details) m.set(id, memberIds(ch))
    return m
  }, [details])

  const submit = async () => {
    setErr("")
    if (!note.trim()) {
      setErr("必须填写边理由（edge_note）：为什么这条升级/利用关系成立")
      return
    }
    try {
      setBusy(true)
      let chainId = cid
      if (mode === "new") {
        if (!name.trim()) { setErr("请填写攻击链名称"); setBusy(false); return }
        const ch = await api.createChain(pid, { name: name.trim(), goal: goal.trim() || undefined })
        chainId = ch.id
      }
      if (!chainId) { setErr("请选择一条攻击链"); setBusy(false); return }
      const members = memberOf.get(chainId) ?? new Set<string>()
      // 来源 finding 不在链里先补挂（链首节点无前置边，edge_note 留空）；
      // 目标追加到链尾并带必填边理由——链视图按 seq 相邻成边。
      if (req.source && !members.has(req.source.id)) {
        await api.addChainLink(pid, chainId, { node_type: "finding", node_id: req.source.id })
      }
      if (!members.has(req.target.id)) {
        await api.addChainLink(pid, chainId, {
          node_type: "finding", node_id: req.target.id, edge_note: note.trim(),
        })
      }
      onDone()
      onClose()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const targetBusy = mode === "existing" && cid
    ? memberOf.get(cid)?.has(req.target.id) ?? false
    : false

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogTitle>加入攻击链</DialogTitle>
        <DialogDescription className="space-y-0.5">
          <span className="block truncate">
            目标发现：<span className="text-foreground">{req.target.title}</span>
          </span>
          {req.source && (
            <span className="block truncate text-[11px]">
              连线来源：{req.source.title}
            </span>
          )}
          <span className="block text-[11px]">目标将追加到链尾，按链内顺序与上一节点自动成边。</span>
        </DialogDescription>

        <div className="space-y-2">
          <div className="flex gap-1 text-[11px]">
            <button
              className={cn("rounded px-2 py-0.5 ring-1 ring-inset",
                mode === "existing" ? "bg-primary/10 text-primary ring-primary/40" : "ring-border text-muted-foreground")}
              onClick={() => setMode("existing")}
            >
              选已有链（{chains.length}）
            </button>
            <button
              className={cn("rounded px-2 py-0.5 ring-1 ring-inset",
                mode === "new" ? "bg-primary/10 text-primary ring-primary/40" : "ring-border text-muted-foreground")}
              onClick={() => setMode("new")}
            >
              新建攻击链
            </button>
          </div>

          {mode === "existing" ? (
            <div className="max-h-48 space-y-1 overflow-auto rounded border p-1">
              {chains.length === 0 && (
                <p className="px-2 py-2 text-[11px] text-muted-foreground">
                  暂无攻击链，请切到「新建攻击链」。
                </p>
              )}
              {chains.map((c) => {
                const busy2 = memberOf.get(c.id)?.has(req.target.id)
                return (
                  <button
                    key={c.id}
                    type="button"
                    disabled={busy2}
                    onClick={() => setCid(c.id)}
                    className={cn("flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[11px]",
                      cid === c.id ? "bg-primary/10 ring-1 ring-inset ring-primary/40" : "hover:bg-accent/40",
                      busy2 && "cursor-not-allowed opacity-50")}
                  >
                    <span className="size-2 shrink-0 rounded-full" style={{ background: STATUS_DOT[c.status] }} />
                    <span className="shrink-0 font-medium">{c.name}</span>
                    {c.goal && <span className="min-w-0 flex-1 truncate text-muted-foreground">{c.goal}</span>}
                    {busy2 && <span className="shrink-0 text-[10px] text-muted-foreground">目标已在链</span>}
                  </button>
                )
              })}
            </div>
          ) : (
            <div className="space-y-2">
              <Input className="h-8 text-xs" placeholder="攻击链名称（如：外网打点到 DBA）"
                     value={name} onChange={(e) => setName(e.target.value)} autoFocus />
              <Input className="h-8 text-xs" placeholder="目标（goal，可选）"
                     value={goal} onChange={(e) => setGoal(e.target.value)} />
            </div>
          )}

          <div className="space-y-1">
            <label className="text-[11px] font-medium text-foreground">
              边理由 edge_note <span className="text-(--status-error)">*</span>
            </label>
            <textarea
              className="min-h-16 w-full rounded-md border bg-transparent p-2 text-xs outline-none focus:ring-1 focus:ring-primary/40"
              placeholder="为什么能从上一节点打到本发现？（如：同一 id 参数，UNION 注入升级到 DBA）"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>

          {targetBusy && (
            <p className="text-[11px] text-(--status-approval)">
              目标发现已在所选链中（链按 seq 相邻自动成边，不能重复挂接），请选其它链。
            </p>
          )}
          {err && <p className="text-[11px] text-(--status-error)">{err}</p>}

          <div className="flex justify-end gap-2">
            <Button size="sm" variant="outline" onClick={onClose}>取消</Button>
            <Button size="sm" onClick={submit}
                    disabled={busy || (mode === "existing" && targetBusy)}>
              {busy ? "提交中…" : "确认入链"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function memberIds(ch?: Chain): Set<string> {
  return new Set((ch?.links ?? [])
    .filter((l) => !l.deleted && l.node_type === "finding")
    .map((l) => l.node_id))
}
