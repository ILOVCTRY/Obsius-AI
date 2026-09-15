import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { Artifact, Finding, FuncEntry } from "@/lib/types"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { FINDING_CATEGORY_LABEL } from "@/lib/workbench"

// 末节点「+」：从本项目既有实体选一个挂到链尾（节点必须真实且属本项目，store 双校验）

interface Props {
  pid: string
  chainId: string
  /** 已在链上的 `${node_type}:${node_id}`，禁重复挂 */
  existing: Set<string>
  open: boolean
  onOpenChange: (v: boolean) => void
  onAdded: () => void
}

export function AddNodeDialog({ pid, chainId, existing, open, onOpenChange, onAdded }: Props) {
  const [q, setQ] = useState("")
  const [funcs, setFuncs] = useState<FuncEntry[]>([])
  const [findings, setFindings] = useState<Finding[]>([])
  const [arts, setArts] = useState<Artifact[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setQ(""); setErr(null)
    Promise.allSettled([
      api.funcs(pid), api.findings(pid), api.artifacts(pid),
    ]).then(([f, d, a]) => {
      if (f.status === "fulfilled") setFuncs(f.value)
      if (d.status === "fulfilled") setFindings(d.value)
      if (a.status === "fulfilled") setArts(a.value)
    })
  }, [open, pid])

  const kw = q.trim().toLowerCase()
  const match = (s: string) => !kw || s.toLowerCase().includes(kw)
  const add = async (nodeType: "func_kb" | "finding" | "artifact", nodeId: string) => {
    setBusy(`${nodeType}:${nodeId}`); setErr(null)
    try {
      await api.addChainLink(pid, chainId, { node_type: nodeType, node_id: nodeId })
      onAdded()
      onOpenChange(false)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(null)
    }
  }

  const Row = ({ disabled, onPick, children }: {
    disabled: boolean; onPick: () => void; children: React.ReactNode
  }) => (
    <button
      type="button" disabled={disabled || busy !== null}
      onClick={onPick}
      className="flex w-full items-center gap-2 rounded border border-transparent px-2 py-1 text-left text-[11px] hover:border-primary/40 hover:bg-accent/40 disabled:opacity-35"
    >
      {children}
    </button>
  )

  const listCls = "max-h-[46vh] space-y-0.5 overflow-auto pr-1"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>挂节点到链尾</DialogTitle>
        <DialogDescription>只能引用本项目已落库的实体（函数知识 / 发现 / 产物）。</DialogDescription>
        <input
          value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索名称 / 地址 / 标题…"
          className="h-8 rounded-md border bg-background px-2 text-xs outline-none focus:border-primary"
        />
        {err && <p className="rounded bg-[--status-error]/10 p-1.5 text-[10px] text-[--status-error]">{err}</p>}
        <Tabs defaultValue="func_kb">
          <TabsList>
            <TabsTrigger value="func_kb">函数知识</TabsTrigger>
            <TabsTrigger value="finding">发现</TabsTrigger>
            <TabsTrigger value="artifact">产物</TabsTrigger>
          </TabsList>
          <TabsContent value="func_kb">
            <div className={listCls}>
              {funcs.filter((f) => match(f.name) || match(f.address)).map((f) => (
                <Row key={f.id}
                  disabled={existing.has(`func_kb:${f.id}`)}
                  onPick={() => add("func_kb", f.id)}>
                  <span className="min-w-0 flex-1 truncate text-primary">ƒ {f.name}</span>
                  <span className="font-mono text-[9px] text-muted-foreground">{f.address}</span>
                </Row>
              ))}
              {funcs.length === 0 && <p className="p-2 text-[10px] text-muted-foreground">还没有 func_kb 条目</p>}
            </div>
          </TabsContent>
          <TabsContent value="finding">
            <div className={listCls}>
              {findings.filter((f) => match(f.title)).map((f) => (
                <Row key={f.id}
                  disabled={existing.has(`finding:${f.id}`)}
                  onPick={() => add("finding", f.id)}>
                  <span className="min-w-0 flex-1 truncate">🔍 {f.title}</span>
                  {typeof f.evidence?.category === "string" && (
                    <span className="rounded bg-muted px-1 text-[9px] text-muted-foreground">
                      {FINDING_CATEGORY_LABEL[f.evidence.category as string] ?? f.evidence.category}
                    </span>
                  )}
                  <span className="font-mono text-[9px] uppercase text-muted-foreground">{f.severity}</span>
                </Row>
              ))}
            </div>
          </TabsContent>
          <TabsContent value="artifact">
            <div className={listCls}>
              {arts.filter((a) => match(a.description) || match(a.path) || match(a.kind)).map((a) => (
                <Row key={a.id}
                  disabled={existing.has(`artifact:${a.id}`)}
                  onPick={() => add("artifact", a.id)}>
                  <span className="rounded bg-muted px-1 text-[9px] text-muted-foreground">{a.kind}</span>
                  <span className="min-w-0 flex-1 truncate">{a.description || a.path}</span>
                </Row>
              ))}
              {arts.length === 0 && <p className="p-2 text-[10px] text-muted-foreground">还没有产物</p>}
            </div>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}
