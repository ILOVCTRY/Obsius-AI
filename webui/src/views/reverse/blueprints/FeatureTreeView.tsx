import { useCallback, useEffect, useMemo, useState } from "react"
import { ReactFlow, Background, Controls, Handle, Position,
  type Edge, type Node } from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { Plus, Pencil, Trash2, Check } from "lucide-react"
import { api } from "@/lib/api"
import type { FeatureTree, FeatureTreeNode } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"

// 功能树节点尺寸与间距（LR 方向：x=层级，y=同层序号）
const NODE_W = 180
const NODE_H = 56
const GAP_X = 60
const GAP_Y = 24

interface FeatureNodeData extends Record<string, unknown> {
  node: FeatureTreeNode
  onEdit: (n: FeatureTreeNode) => void
  onAddChild: (n: FeatureTreeNode) => void
  onDelete: (n: FeatureTreeNode) => void
  onToggle: (n: FeatureTreeNode) => void
}

type FeatureFlowNode = Node<FeatureNodeData, "feature">

function FeatureNode({ data }: { data: FeatureNodeData }) {
  const { node } = data
  const passed = node.status === "passed"
  return (
    <div
      className={[
        "group relative flex h-[56px] w-[180px] flex-col justify-center rounded-md border px-2.5",
        passed
          ? "border-(--status-ok) bg-(--status-ok-bg) text-(--status-ok)"
          : "border-border bg-card text-foreground",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2" />
      <div className="flex items-center gap-1.5">
        {passed && <Check className="h-3 w-3 shrink-0" />}
        <span className="min-w-0 flex-1 truncate text-xs font-medium">{node.name}</span>
      </div>
      {node.desc && (
        <div className="mt-0.5 truncate text-[10px] text-muted-foreground">{node.desc}</div>
      )}
      <Handle type="source" position={Position.Right} className="!h-2 !w-2" />
      <div className="absolute -right-1 -top-1 hidden gap-0.5 group-hover:flex">
        <button
          type="button" title="编辑"
          onClick={(e) => { e.stopPropagation(); data.onEdit(node) }}
          className="rounded border bg-background p-0.5 hover:bg-accent"
        >
          <Pencil className="h-3 w-3" />
        </button>
        <button
          type="button" title="添加子功能"
          onClick={(e) => { e.stopPropagation(); data.onAddChild(node) }}
          className="rounded border bg-background p-0.5 hover:bg-accent"
        >
          <Plus className="h-3 w-3" />
        </button>
        <button
          type="button" title={passed ? "取消通过" : "标记通过"}
          onClick={(e) => { e.stopPropagation(); data.onToggle(node) }}
          className={[
            "rounded border bg-background p-0.5 hover:bg-accent",
            passed && "border-(--status-ok) text-(--status-ok)",
          ].join(" ")}
        >
          <Check className="h-3 w-3" />
        </button>
        <button
          type="button" title="删除（含子节点）"
          onClick={(e) => { e.stopPropagation(); data.onDelete(node) }}
          className="rounded border bg-background p-0.5 text-(--status-error) hover:bg-accent"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
    </div>
  )
}

const nodeTypes = { feature: FeatureNode }

function layoutTree(nodes: FeatureTreeNode[]): { nodes: FeatureFlowNode[]; edges: Edge[] } {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const children = new Map<string, string[]>()
  const roots: string[] = []
  for (const n of nodes) {
    const p = n.parent_id
    if (p && byId.has(p)) {
      if (!children.has(p)) children.set(p, [])
      children.get(p)!.push(n.id)
    } else {
      roots.push(n.id)
    }
  }
  const width = new Map<string, number>()
  const dfs = (id: string): number => {
    const kids = children.get(id) ?? []
    if (kids.length === 0) { width.set(id, 1); return 1 }
    let w = 0
    for (const k of kids) w += dfs(k)
    w = Math.max(1, w)
    width.set(id, w)
    return w
  }
  const yCursor = { v: 0 }
  const pos = new Map<string, { x: number; y: number }>()
  const place = (id: string, depth: number, yStart: number) => {
    const kids = children.get(id) ?? []
    const w = width.get(id) ?? 1
    const yCenter = yStart + (w * (NODE_H + GAP_Y) - GAP_Y) / 2
    pos.set(id, { x: depth * (NODE_W + GAP_X), y: yCenter })
    let y = yStart
    for (const k of kids) {
      const kw = width.get(k) ?? 1
      place(k, depth + 1, y)
      y += kw * (NODE_H + GAP_Y)
    }
  }
  for (const r of roots) {
    const rw = dfs(r)
    place(r, 0, yCursor.v)
    yCursor.v += rw * (NODE_H + GAP_Y)
  }
  const flowNodes: FeatureFlowNode[] = nodes
    .filter((n) => pos.has(n.id))
    .map((n) => {
      const p = pos.get(n.id)!
      return {
        id: n.id, type: "feature", position: { x: p.x, y: p.y },
        data: { node: n } as FeatureNodeData, draggable: false,
      }
    })
  const edges: Edge[] = nodes
    .filter((n) => n.parent_id && byId.has(n.parent_id))
    .map((n) => ({
      id: `${n.parent_id}->${n.id}`,
      source: n.parent_id!, target: n.id,
      type: "smoothstep",
      style: { stroke: "var(--viz-edge)", strokeWidth: 1.5 },
    }))
  return { nodes: flowNodes, edges }
}

interface EditState {
  mode: "add" | "edit"
  parentId: string | null
  node: FeatureTreeNode | null
  name: string
  desc: string
}

export function FeatureTreeView({ pid, sha }: {
  pid: string
  /** 当前样本 sha（用于定位/创建该样本的功能树） */
  sha: string | null
}) {
  const [tree, setTree] = useState<FeatureTree | null>(null)
  const [editing, setEditing] = useState<EditState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadTree = useCallback(async () => {
    if (!sha) { setTree(null); return }
    try {
      const list = await api.featureTrees(pid, sha)
      if (list.length > 0) {
        setTree(list[0])
      } else {
        // 该样本尚无功能树，懒创建（分析阶段产物，首次查看即建）
        const created = await api.createFeatureTree(pid, { binary_sha256: sha })
        setTree(created)
      }
    } catch (e) { setError(String(e)) }
  }, [pid, sha])

  useEffect(() => {
    loadTree()
    const t = setInterval(loadTree, 5000)
    return () => clearInterval(t)
  }, [loadTree])

  const { nodes, edges } = useMemo(
    () => layoutTree(tree?.nodes ?? []), [tree?.nodes])
  const passedCount = (tree?.nodes ?? []).filter((n) => n.status === "passed").length

  const refresh = useCallback(() => { loadTree() }, [loadTree])

  const withData = useCallback((nd: FeatureFlowNode): FeatureFlowNode => ({
    ...nd,
    data: {
      ...nd.data,
      onEdit: (n) => setEditing({ mode: "edit", parentId: n.parent_id, node: n, name: n.name, desc: n.desc }),
      onAddChild: (n) => setEditing({ mode: "add", parentId: n.id, node: null, name: "", desc: "" }),
      onDelete: async (n) => {
        if (!tree || !confirm(`删除「${n.name}」及其所有子节点？`)) return
        setBusy(true)
        try { await api.deleteFeatureNode(pid, tree.id, n.id); refresh() }
        catch (e) { setError(String(e)) }
        finally { setBusy(false) }
      },
      onToggle: async (n) => {
        if (!tree) return
        setBusy(true)
        try {
          await api.patchFeatureNode(pid, tree.id, n.id, {
            status: n.status === "passed" ? "pending" : "passed",
          })
          refresh()
        } catch (e) { setError(String(e)) }
        finally { setBusy(false) }
      },
    },
  }), [pid, tree, refresh])

  const enrichedNodes = useMemo(() => nodes.map(withData), [nodes, withData])

  const save = async () => {
    if (!editing || !tree) return
    const name = editing.name.trim()
    if (!name) { setError("名称不能为空"); return }
    setBusy(true)
    try {
      if (editing.mode === "add") {
        await api.addFeatureNode(pid, tree.id, {
          name, desc: editing.desc, parent_id: editing.parentId,
        })
      } else if (editing.node) {
        await api.patchFeatureNode(pid, tree.id, editing.node.id, {
          name, desc: editing.desc,
        })
      }
      setEditing(null)
      refresh()
    } catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }

  const addRoot = () =>
    setEditing({ mode: "add", parentId: null, node: null, name: "", desc: "" })

  return (
    <div className="h-full overflow-auto p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-xs font-medium text-muted-foreground">
          功能树（{(tree?.nodes ?? []).length} 节点，{passedCount} 通过）
        </h2>
        <Button size="sm" variant="outline" onClick={addRoot} disabled={busy || !tree}>
          <Plus className="mr-1 h-3 w-3" />添加根功能
        </Button>
        <span className="flex-1" />
        <span className="text-[10px] text-muted-foreground">
          分析阶段构建，可被蓝图引用为施工图纸；悬停节点可编辑 / 加子功能 / 标记通过（绿）
        </span>
      </div>
      {error && <p className="mb-2 text-xs text-(--status-error)">{error}</p>}
      {!sha ? (
        <div className="flex h-64 items-center justify-center text-xs text-muted-foreground">
          先在样本栏选择一个样本
        </div>
      ) : (
        <div className="h-[600px] rounded-lg border">
          {!tree || tree.nodes.length === 0 ? (
            <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
              暂无功能树节点，点「添加根功能」开始搭建程序功能流向图
            </div>
          ) : (
            <ReactFlow
              nodes={enrichedNodes}
              edges={edges}
              nodeTypes={nodeTypes}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              fitView
              minZoom={0.3}
              maxZoom={1.5}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={16} size={1} />
              <Controls showInteractive={false} />
            </ReactFlow>
          )}
        </div>
      )}

      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent>
          <DialogTitle>{editing?.mode === "add" ? "添加功能节点" : "编辑功能节点"}</DialogTitle>
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">名称</label>
              <Input
                value={editing?.name ?? ""}
                onChange={(e) => setEditing((s) => s ? { ...s, name: e.target.value } : s)}
                placeholder="如：验证登录"
                autoFocus
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">描述（可选）</label>
              <Input
                value={editing?.desc ?? ""}
                onChange={(e) => setEditing((s) => s ? { ...s, desc: e.target.value } : s)}
                placeholder="该功能的业务语义"
              />
            </div>
            {editing?.parentId && tree && (
              <p className="text-[10px] text-muted-foreground">
                父节点：{tree.nodes.find((n) => n.id === editing.parentId)?.name ?? "根"}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
              <Button onClick={save} disabled={busy}>{busy ? "保存中…" : "保存"}</Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
