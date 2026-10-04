import { useCallback, useEffect, useMemo, useState } from "react"
import { ReactFlow, Background } from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { Plus, Pencil, Trash2 } from "lucide-react"
import { api } from "@/lib/api"
import type { Chain, ChainLink, ChainNodeType, ChainStatus, ChainSummary } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { layout, type EntityFlowNode } from "./chainNodes"
import { EntityNode } from "./EntityNode"
import { ChainEdge } from "./ChainEdge"
import { AddNodeDialog } from "./AddNodeDialog"

// 攻击链视图（DESIGN §12）：左 260 链列表 + 右 React Flow 横排线性链。
// 本期只做人工建链；节点点击回跳分析视图定位（回调由 ReverseWorkbench 提供）。

const STATUS_DOT: Record<ChainStatus, string> = {
  hypothesis: "var(--viz-sev-medium)",
  validated: "var(--viz-success)",
  exploited: "var(--viz-chain)",
}
const STATUSES: ChainStatus[] = ["hypothesis", "validated", "exploited"]
const STATUS_LABEL: Record<ChainStatus, string> = {
  hypothesis: "假说", validated: "已验证", exploited: "已利用",
}
const nodeTypes = { entity: EntityNode }
const edgeTypes = { "entity-edge": ChainEdge }

interface Props {
  pid: string
  /** 父级 WS/轮询 bump token */
  tick: number
  /** 节点点击：切回分析视图并定位（函数→中栏，发现→右栏） */
  onLocate: (nodeType: ChainNodeType, nodeId: string, addr?: string) => void
}

export function ChainView({ pid, tick, onLocate }: Props) {
  const [summaries, setSummaries] = useState<ChainSummary[]>([])
  const [cid, setCid] = useState<string | null>(null)
  const [detail, setDetail] = useState<Chain | null>(null)
  const [newName, setNewName] = useState("")
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<ChainSummary | null>(null)
  const [editName, setEditName] = useState("")
  const [editGoal, setEditGoal] = useState("")
  const [deleting, setDeleting] = useState<ChainSummary | null>(null)
  const [edgeLink, setEdgeLink] = useState<ChainLink | null>(null)
  const [noteText, setNoteText] = useState("")

  const reloadList = useCallback(
    () => api.chains(pid).then(setSummaries).catch(() => {}), [pid])
  const reloadDetail = useCallback(() => {
    if (!cid) { setDetail(null); return }
    api.chain(pid, cid).then(setDetail).catch(() => {})
  }, [pid, cid])

  useEffect(() => {
    reloadList()
    const t = setInterval(reloadList, 4000)
    return () => clearInterval(t)
  }, [reloadList, tick])
  useEffect(() => { reloadDetail() }, [reloadDetail, tick])

  // 首次有数据默认选第一条；当前选中链被删后回退
  useEffect(() => {
    if (cid && !summaries.some((c) => c.id === cid)) setCid(summaries[0]?.id ?? null)
    if (!cid && summaries.length) setCid(summaries[0].id)
  }, [summaries, cid])

  const links = detail?.links ?? []
  const existing = useMemo(
    () => new Set(links.map((l) => `${l.node_type}:${l.node_id}`)), [links])

  const createChain = async () => {
    const name = newName.trim()
    if (!name) return
    const c = await api.createChain(pid, { name })
    setNewName("")
    setCid(c.id)
    await reloadList()
  }

  const changeStatus = async (c: ChainSummary, status: ChainStatus) => {
    await api.updateChain(pid, c.id, { status })
    reloadList(); if (c.id === cid) reloadDetail()
  }

  const saveEdit = async () => {
    if (!editing) return
    await api.updateChain(pid, editing.id, { name: editName, goal: editGoal })
    setEditing(null); reloadList(); reloadDetail()
  }

  const [delErr, setDelErr] = useState<string | null>(null)
  const confirmDelete = async () => {
    if (!deleting) return
    try {
      await api.deleteChain(pid, deleting.id)
      setDeleting(null); reloadList()
    } catch (e) {
      // 失败保持弹窗显示原因（同 ProjectsView 删除弹窗的坑：Action 默认点击即关弹）
      setDelErr(String(e))
    }
  }

  const removeLink = async (link: ChainLink) => {
    await api.deleteChainLink(pid, link.id)
    reloadList(); reloadDetail()
  }

  const openNote = (link: ChainLink) => {
    setEdgeLink(link); setNoteText(link.edge_note)
  }
  const saveNote = async () => {
    if (!edgeLink) return
    await api.updateLinkNote(pid, edgeLink.id, noteText)
    setEdgeLink(null); reloadDetail()
  }

  const { nodes, edges } = useMemo(
    () => layout(links, removeLink),
    // removeLink 每次渲染新建；按 links 重算即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [links])

  return (
    <div className="flex min-h-0 flex-1">
      {/* 左：链列表 */}
      <div className="flex w-[260px] shrink-0 flex-col border-r">
        <div className="space-y-2 border-b p-2">
          <div className="flex gap-1">
            <Input
              value={newName} onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") createChain() }}
              placeholder="新链名称…" className="h-8 text-xs"
            />
            <Button size="sm" className="h-8 px-2" onClick={createChain}>
              <Plus className="size-3.5" />
            </Button>
          </div>
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-auto p-2">
          {summaries.map((c) => (
            <div
              key={c.id}
              onClick={() => setCid(c.id)}
              className={`cursor-pointer rounded-md border p-2 ${
                c.id === cid ? "border-primary/60 bg-primary/5" : "border-transparent hover:bg-accent/40"
              }`}
            >
              <div className="flex items-center gap-1.5">
                <span className="size-2 shrink-0 rounded-full" style={{ background: STATUS_DOT[c.status] }} />
                <span className="min-w-0 flex-1 truncate text-xs font-medium">{c.name}</span>
                <button
                  type="button" title="改名/目标"
                  onClick={(e) => {
                    e.stopPropagation(); setEditing(c); setEditName(c.name); setEditGoal(c.goal)
                  }}
                  className="text-muted-foreground hover:text-primary"
                ><Pencil className="size-3" /></button>
                <button
                  type="button" title="删除链"
                  onClick={(e) => { e.stopPropagation(); setDeleting(c) }}
                  className="text-muted-foreground hover:text-(--status-error)"
                ><Trash2 className="size-3" /></button>
              </div>
              {c.goal && <p className="mt-0.5 truncate text-[10px] text-muted-foreground" title={c.goal}>{c.goal}</p>}
              <div className="mt-1 flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                <select
                  value={c.status}
                  onChange={(e) => changeStatus(c, e.target.value as ChainStatus)}
                  className="h-6 rounded border bg-background px-1 text-[10px] outline-none"
                >
                  {STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
                </select>
                <span className="flex-1" />
                <span className="text-[9px] text-muted-foreground">{c.link_count} 节点</span>
              </div>
            </div>
          ))}
          {summaries.length === 0 && (
            <p className="p-2 text-[11px] text-muted-foreground">还没有攻击链。人工建链：起个名字，把函数/发现/产物按利用顺序串起来。</p>
          )}
        </div>
      </div>

      {/* 右：React Flow 画布 */}
      <div className="relative min-w-0 flex-1 bg-muted/20">
        {detail ? (
          <>
            <ReactFlow
              key={`${detail.id}-${links.map((l) => l.id).join(",")}`}
              nodes={nodes as EntityFlowNode[]}
              edges={edges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              nodesDraggable={false}
              fitView
              fitViewOptions={{ maxZoom: 1, padding: 0.15 }}
              proOptions={{ hideAttribution: true }}
              minZoom={0.2}
              onNodeClick={(_, node) => {
                const link = (node as EntityFlowNode).data.link
                if (!link.deleted) onLocate(link.node_type, link.node_id, link.entity?.address)
              }}
              onEdgeDoubleClick={(_, edge) => {
                const target = links.find((l) => l.id === edge.target)
                if (target) openNote(target)
              }}
            >
              <Background color="var(--viz-edge)" gap={20} size={1} />
            </ReactFlow>
            <button
              type="button" onClick={() => setAddOpen(true)}
              className="absolute right-3 top-3 inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1.5 text-xs text-primary-foreground"
            >
              <Plus className="size-3.5" />挂节点
            </button>
            {links.length === 0 && (
              <p className="pointer-events-none absolute inset-x-0 top-1/2 text-center text-xs text-muted-foreground">
                空链——右上「挂节点」选第一个实体（通常是入口函数）
              </p>
            )}
            <p className="pointer-events-none absolute bottom-2 left-3 text-[10px] text-muted-foreground">
              点节点回跳分析视图 · 双击边编辑「这条边为什么成立」 · 节点悬停 × 移出链
            </p>
          </>
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            {summaries.length === 0 ? "新建一条链开始" : "选择左侧链"}
          </div>
        )}
      </div>

      <AddNodeDialog
        pid={pid} chainId={cid ?? ""} existing={existing}
        open={addOpen && !!cid} onOpenChange={setAddOpen}
        onAdded={() => { reloadList(); reloadDetail() }}
      />

      {/* 改名/目标 */}
      <Dialog open={!!editing} onOpenChange={(v) => !v && setEditing(null)}>
        <DialogContent>
          <DialogTitle>链设置</DialogTitle>
          <label className="text-[11px] text-muted-foreground">名称
            <Input value={editName} onChange={(e) => setEditName(e.target.value)} className="mt-1 h-8 text-xs" />
          </label>
          <label className="text-[11px] text-muted-foreground">目标（这条链打穿后得到什么）
            <textarea
              value={editGoal} onChange={(e) => setEditGoal(e.target.value)} rows={3}
              className="mt-1 w-full rounded-md border bg-background p-2 text-xs outline-none focus:border-primary"
            />
          </label>
          <div className="flex justify-end">
            <Button size="sm" onClick={saveEdit}>保存</Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 边注编辑 */}
      <Dialog open={!!edgeLink} onOpenChange={(v) => !v && setEdgeLink(null)}>
        <DialogContent>
          <DialogTitle>边注 — 为什么走到这一步</DialogTitle>
          <textarea
            autoFocus value={noteText} onChange={(e) => setNoteText(e.target.value)} rows={3}
            placeholder='如「该函数可溢出覆盖返回地址」'
            className="w-full rounded-md border bg-background p-2 text-xs outline-none focus:border-primary"
          />
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="outline" onClick={() => setEdgeLink(null)}>取消</Button>
            <Button size="sm" onClick={saveNote}>保存</Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 删链二次确认 */}
      <AlertDialog open={!!deleting} onOpenChange={(v) => !v && setDeleting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除攻击链「{deleting?.name}」？</AlertDialogTitle>
            <AlertDialogDescription>
              只删链与连线，链上的函数知识/发现/产物实体不受影响。
            </AlertDialogDescription>
            {delErr && <p className="text-sm text-(--status-error)">{delErr}</p>}
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            {/* preventDefault 拦下 Radix 点击即关弹：失败原因要留在弹窗里（同 ProjectsView） */}
            <AlertDialogAction
              onClick={(e) => { e.preventDefault(); setDelErr(null); confirmDelete() }}
              className="bg-(--status-error) text-(--background) hover:opacity-90"
            >
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
