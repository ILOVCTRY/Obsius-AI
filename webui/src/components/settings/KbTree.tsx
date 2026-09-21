import { useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { KbSearchHit, KbSourceTree } from "@/lib/types"
import { cn } from "@/lib/utils"

// 知识库折叠树：源 → 多级目录（中文目录名允许）→ 文件（K5 白名单 .md/.py/.txt/.json）。
// 文件行显 kbindex 同口径标题（frontmatter title > # H1 > stem，F15），悬停 title 出路径；
// 排序用自然序（数字段按数值，残存编号不乱序）。
// 默认只展开源根；过滤时自动展开并只显命中路径。文件悬停出改名/删除。
// 双通道搜索：输入 ≥2 字符防抖调后端正文搜索，命中列表置顶（点击即打开该文件），
// 文件名过滤照常作用于树，两不干扰。

interface TreeNode {
  name: string
  path: string          // 完整源内路径（目录无尾斜杠）
  dirs: Map<string, TreeNode>
  files: TreeNodeFile[]
}
interface TreeNodeFile { name: string; path: string; size: number; title: string }

// 自然排序：数字段按数值比较（`-2` 紧跟主体、`11-` 排 `2-` 后），
// 过渡期残存编号文件不再按字典序乱序
function natCmp(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" })
}

function buildTree(files: { path: string; size: number; title: string }[]): TreeNode {
  const root: TreeNode = { name: "", path: "", dirs: new Map(), files: [] }
  for (const f of files) {
    const parts = f.path.split("/")
    let node = root
    parts.slice(0, -1).forEach((seg, i) => {
      let child = node.dirs.get(seg)
      if (!child) {
        child = { name: seg, path: parts.slice(0, i + 1).join("/"), dirs: new Map(), files: [] }
        node.dirs.set(seg, child)
      }
      node = child
    })
    node.files.push({ name: parts[parts.length - 1], path: f.path, size: f.size, title: f.title })
  }
  return root
}

function matchPath(path: string, q: string): boolean {
  return !q || path.toLowerCase().includes(q)
}

function DirNode({ node, depth, q, selected, onSelect, onRename, onDelete, collapsed, toggle }: {
  node: TreeNode
  depth: number
  q: string
  selected: string | null
  onSelect: (path: string) => void
  onRename: (path: string) => void
  onDelete: (path: string) => void
  collapsed: Set<string>
  toggle: (path: string) => void
}) {
  const isOpen = q ? true : !collapsed.has(node.path)
  const dirs = [...node.dirs.values()].sort((a, b) => natCmp(a.name, b.name))
  const files = node.files
    .filter((f) => matchPath(f.path, q))
    .sort((a, b) => natCmp(a.name, b.name))
  // 过滤模式下隐藏整棵无子树的目录
  const hasVisible = q
    ? files.length > 0 || dirs.some((d) => dirHasMatch(d, q))
    : true
  if (!hasVisible) return null
  return (
    <div>
      {node.name && (
        <button
          className="flex w-full items-center gap-0.5 truncate rounded py-0.5 pr-1 text-left text-[11px] text-muted-foreground hover:bg-accent/40"
          style={{ paddingLeft: depth * 10 + 2 }}
          onClick={() => toggle(node.path)}
          title={node.path}
        >
          <span className="w-3 shrink-0 text-[9px]">{isOpen ? "▾" : "▸"}</span>
          <span className="truncate">📁 {node.name}</span>
        </button>
      )}
      {isOpen && (
        <div>
          {dirs.map((d) => (
            <DirNode key={d.path} node={d} depth={depth + 1} q={q} selected={selected}
                     onSelect={onSelect} onRename={onRename} onDelete={onDelete}
                     collapsed={collapsed} toggle={toggle} />
          ))}
          {files.map((f) => (
            <div key={f.path}
                 className={cn("group flex items-center rounded text-[11px]",
                   selected === f.path && "bg-primary/10 text-primary")}>
              <button
                className="min-w-0 flex-1 truncate py-0.5 pr-1 text-left hover:bg-accent/40"
                style={{ paddingLeft: (depth + 1) * 10 + 2 }}
                onClick={() => onSelect(f.path)}
                title={`${f.path}（${f.size}B）`}>
                📄 {f.title || f.name}
              </button>
              <button className="shrink-0 px-1 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:text-primary"
                      title="改名（全仓引用联动替换）" onClick={() => onRename(f.path)}>✎</button>
              <button className="shrink-0 px-1 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:text-(--status-error)"
                      title="删除（进 kb-trash，可恢复）" onClick={() => onDelete(f.path)}>✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function dirHasMatch(node: TreeNode, q: string): boolean {
  if (node.files.some((f) => matchPath(f.path, q))) return true
  return [...node.dirs.values()].some((d) => dirHasMatch(d, q))
}

export function KbTree({ cap, sources, selected, onSelect, onRename, onDelete }: {
  cap: string            // 正文搜索目标能力包（与树同源，即 SkillsPane 的 kbCap）
  sources: KbSourceTree[]
  selected: string | null
  onSelect: (path: string) => void
  onRename: (path: string) => void
  onDelete: (path: string) => void
}) {
  const [q, setQ] = useState("")
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [hits, setHits] = useState<KbSearchHit[]>([])
  const [routes, setRoutes] = useState<[string, string[]][]>([])
  const [routesOpen, setRoutesOpen] = useState(false)
  const seqRef = useRef(0)
  const trees = useMemo(
    () => sources.map((s) => ({ src: s, tree: buildTree(s.files) })), [sources])
  const total = sources.reduce((n, s) => n + s.files.length, 0)
  const toggle = (path: string) =>
    setCollapsed((cur) => {
      const next = new Set(cur)
      if (next.has(path)) next.delete(path); else next.add(path)
      return next
    })

  // 正文搜索：≥2 字符 300ms 防抖；请求序号守卫防乱序回填，切包即清
  useEffect(() => {
    const term = q.trim()
    const seq = ++seqRef.current
    const t = setTimeout(() => {
      if (term.length < 2) {
        setHits([])
        return
      }
      api.kbSearch(cap, term, 30).then((r) => {
        if (seq === seqRef.current) setHits(r.results)
      }).catch(() => {
        if (seq === seqRef.current) setHits([])
      })
    }, 300)
    return () => clearTimeout(t)
  }, [q, cap])

  // 任务导航路由（2026-09-19 kb/route.json）：点击分组=按前缀过滤树
  // （前缀含目录斜杠，走 matchPath 子串匹配，天然兼容既有过滤框）
  useEffect(() => {
    let dead = false
    api.kbRoutes(cap).then((r) => {
      if (!dead) setRoutes(Object.entries(r.routes))
    }).catch(() => { if (!dead) setRoutes([]) })
    return () => { dead = true }
  }, [cap])

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <input
        value={q}
        onChange={(e) => setQ(e.target.value.toLowerCase())}
        placeholder={`过滤/正文搜索 ${total} 篇…`}
        className="mx-1 mb-1 h-6 shrink-0 rounded border bg-background px-1.5 text-[11px] outline-none focus:border-primary/50"
      />
      {hits.length > 0 && (
        <div className="mb-1 max-h-40 shrink-0 overflow-y-auto rounded border bg-card/60 px-1 py-0.5">
          <p className="px-1 py-0.5 text-[10px] text-muted-foreground">正文命中 {hits.length} 篇</p>
          {hits.map((h) => (
            <button key={`${h.source}/${h.path}`}
                    className="block w-full truncate rounded px-1 py-0.5 text-left text-[11px] hover:bg-accent/40"
                    title={`${h.path}（命中 ${h.matches} 次）\n${h.snippet}`}
                    onClick={() => onSelect(h.path)}>
              <span className="text-primary">📄 {h.path}</span>
              <span className="ml-1 text-[10px] text-muted-foreground">×{h.matches} · {h.snippet}</span>
            </button>
          ))}
        </div>
      )}
      {routes.length > 0 && (
        <div className="mb-1 shrink-0 rounded border bg-card/60 px-1 py-0.5">
          <button className="flex w-full items-center rounded px-1 py-0.5 text-left text-[10px] text-muted-foreground hover:bg-accent/40"
                  onClick={() => setRoutesOpen((v) => !v)}>
            <span className="mr-1">{routesOpen ? "▾" : "▸"}</span>
            🧭 任务导航（{routes.length} 类，点击过滤树）
          </button>
          {routesOpen && (
            <div className="max-h-40 overflow-y-auto">
              {routes.map(([key, prefixes]) => (
                <div key={key} className="flex items-baseline gap-1 px-1 py-0.5">
                  <span className="shrink-0 text-[10px] text-amber-400">{key.split("|").join(" / ")}</span>
                  <span className="flex min-w-0 flex-wrap gap-1">
                    {prefixes.map((p) => (
                      <button key={p}
                              className="truncate rounded bg-primary/10 px-1 font-mono text-[10px] text-primary hover:bg-primary/20"
                              title={`按 ${p} 过滤树`} onClick={() => setQ(p.toLowerCase())}>
                        {p}
                      </button>
                    ))}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-1">
        {total === 0 && <p className="p-1 text-[10px] text-muted-foreground">知识库为空</p>}
        {trees.map(({ src, tree }) => (
          <div key={src.id} className="mb-0.5">
            {sources.length > 1 && (
              <p className="px-1 py-0.5 font-mono text-[10px] text-muted-foreground">
                源 {src.id}（{src.root}）
              </p>
            )}
            <DirNode node={tree} depth={0} q={q.trim()} selected={selected}
                     onSelect={onSelect} onRename={onRename} onDelete={onDelete}
                     collapsed={collapsed} toggle={toggle} />
          </div>
        ))}
      </div>
    </div>
  )
}
