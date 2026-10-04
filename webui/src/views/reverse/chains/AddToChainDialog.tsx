import { useCallback, useEffect, useState } from "react"
import { Link2 } from "lucide-react"
import { api } from "@/lib/api"
import type { ChainNodeType, ChainSummary } from "@/lib/types"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"

// 「⛓ 加入攻击链」：挂到已有链尾，或新建链并挂首节点（P2 只做人工建链）

interface Props {
  pid: string
  nodeType: ChainNodeType
  nodeId: string
  disabled?: boolean
  title?: string
}

const STATUS_DOT: Record<string, string> = {
  hypothesis: "var(--viz-sev-medium)",
  validated: "var(--viz-success)",
  exploited: "var(--viz-chain)",
}

export function AddToChainButton({ pid, nodeType, nodeId, disabled, title }: Props) {
  const [open, setOpen] = useState(false)
  const [chains, setChains] = useState<ChainSummary[]>([])
  // 已含本实体的链 id（详情拉取，失败按「不含」处理，后端另有校验）
  const [memberIn, setMemberIn] = useState<Set<string>>(new Set())
  const [newName, setNewName] = useState("")
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    const list = await api.chains(pid)
    setChains(list)
    const details = await Promise.allSettled(list.map((c) => api.chain(pid, c.id)))
    const inSet = new Set<string>()
    details.forEach((r, i) => {
      if (r.status === "fulfilled"
        && r.value.links?.some((l) => l.node_type === nodeType && l.node_id === nodeId)) {
        inSet.add(list[i].id)
      }
    })
    setMemberIn(inSet)
  }, [pid, nodeType, nodeId])

  useEffect(() => {
    if (!open) return
    setMsg(null); setErr(null); setMemberIn(new Set())
    refresh().catch(() => {})
  }, [open, refresh])

  const join = async (cid: string, name: string) => {
    setBusy(true); setErr(null)
    try {
      await api.addChainLink(pid, cid, { node_type: nodeType, node_id: nodeId })
      setMsg(`已加入链「${name}」（攻击链视图可见）`)
      await refresh()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const createAndJoin = async () => {
    const name = newName.trim()
    if (!name) return
    setBusy(true); setErr(null)
    try {
      const c = await api.createChain(pid, { name })
      await api.addChainLink(pid, c.id, { node_type: nodeType, node_id: nodeId })
      setNewName("")
      setMsg(`已新建链「${name}」并挂为首节点`)
      await refresh()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button
        type="button" disabled={disabled} onClick={() => setOpen(true)}
        title={title ?? "加入攻击链"}
        className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-primary hover:bg-primary/10 disabled:opacity-40"
      >
        <Link2 className="size-3" />入链
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>加入攻击链</DialogTitle>
          <DialogDescription>把该节点挂到一条已有链的链尾，或新建一条链。</DialogDescription>
          {err && <p className="rounded bg-(--status-error)/10 p-1.5 text-[10px] text-(--status-error)">{err}</p>}
          {msg && <p className="rounded bg-primary/10 p-1.5 text-[10px] text-primary">{msg}</p>}
          <div className="flex gap-1">
            <input
              value={newName} onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") createAndJoin() }}
              placeholder="新链名称，如「破解校验链」"
              className="h-8 min-w-0 flex-1 rounded-md border bg-background px-2 text-xs outline-none focus:border-primary"
            />
            <button
              type="button" disabled={busy || !newName.trim()} onClick={createAndJoin}
              className="shrink-0 rounded-md bg-primary px-2.5 text-xs text-primary-foreground disabled:opacity-40"
            >
              新建并加入
            </button>
          </div>
          <div className="max-h-[40vh] space-y-0.5 overflow-auto">
            {chains.length === 0 && <p className="p-2 text-[10px] text-muted-foreground">还没有链，先在上方新建。</p>}
            {chains.map((c) => {
              const already = memberIn.has(c.id)
              return (
                <button
                  key={c.id} type="button" disabled={busy || already}
                  onClick={() => join(c.id, c.name)}
                  className="flex w-full items-center gap-2 rounded border border-transparent px-2 py-1.5 text-left text-[11px] hover:border-primary/40 hover:bg-accent/40 disabled:opacity-40"
                >
                  <span className="size-1.5 shrink-0 rounded-full" style={{ background: STATUS_DOT[c.status] }} />
                  <span className="min-w-0 flex-1 truncate">{c.name}</span>
                  <span className="shrink-0 text-[9px] text-muted-foreground">
                    {already ? "已在链上" : `${c.link_count} 节点`}
                  </span>
                </button>
              )
            })}
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
