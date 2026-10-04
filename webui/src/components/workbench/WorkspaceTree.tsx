import { useState } from "react"
import { ChevronDown, ChevronRight, File, Folder } from "lucide-react"
import type { WorkspaceTreeNode } from "@/lib/types"
import { api } from "@/lib/api"

export function WorkspaceTree({ pid, nodes, truncated }: {
  pid: string
  nodes: WorkspaceTreeNode[]
  truncated: boolean
}) {
  return (
    <div className="wb-context-tree">
      {nodes.map((node) => <TreeNode key={node.path} pid={pid} node={node} />)}
      {truncated && <p className="wb-context-muted">文件树已截断，仅显示部分内容</p>}
      {!nodes.length && <p className="wb-context-muted">工作区暂无可显示文件</p>}
    </div>
  )
}

function TreeNode({ pid, node }: { pid: string; node: WorkspaceTreeNode }) {
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const isDir = node.kind === "dir"
  const openFile = async (action: "open" | "reveal" | "content") => {
    setBusy(action)
    try {
      const result = await api.openProjectFile(pid, node.path, action)
      if (action === "content" && result.content !== undefined) {
        await navigator.clipboard.writeText(result.content)
      }
    } finally { setBusy(null) }
  }
  return (
    <div className="wb-context-tree-node">
      <button type="button" className="wb-context-tree-row" onClick={() => isDir ? setExpanded((v) => !v) : openFile("open")}
              title={node.path}>
        {isDir ? (expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />) : <File size={12} />}
        {isDir ? <Folder size={13} /> : null}
        <span>{node.name}</span>
      </button>
      {isDir && expanded && node.children?.map((child) => (
        <div className="wb-context-tree-child" key={child.path}>
          <TreeNode pid={pid} node={child} />
        </div>
      ))}
      {!isDir && busy && <span className="wb-context-tree-busy">处理中…</span>}
    </div>
  )
}
