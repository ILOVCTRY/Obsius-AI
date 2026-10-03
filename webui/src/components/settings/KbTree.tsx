import { useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { KbSearchHit, KbSourceTree } from "@/lib/types"
import { cn } from "@/lib/utils"
import {
  ChevronDown, ChevronRight, Compass, FileCode2, FileText, Folder,
  Pencil, Search, SlidersHorizontal, Trash2, X,
} from "lucide-react"

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

function fileLayer(path: string): { label: string; className: string } {
  const parts = path.toLowerCase().split("/")
  if (parts.includes("playbooks")) return { label: "方法论", className: "text-primary" }
  if (parts.includes("patterns")) return { label: "模式", className: "text-violet-300" }
  if (parts.includes("cases")) return { label: "案例", className: "text-amber-300" }
  return { label: "资料", className: "text-muted-foreground" }
}

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
          className="kb-dir-row flex w-full items-center gap-1 truncate rounded py-1 pr-1 text-left text-[11px] text-muted-foreground hover:bg-accent/40"
          style={{ paddingLeft: depth * 10 + 2 }}
          onClick={() => toggle(node.path)}
          title={node.path}
        >
          {isOpen ? <ChevronDown size={12} className="shrink-0" /> : <ChevronRight size={12} className="shrink-0" />}
          <Folder size={13} className="shrink-0 text-amber-300/80" />
          <span className="truncate">{node.name}</span>
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
                 className={cn("kb-file-row group flex items-center rounded text-[11px]",
                   selected === f.path && "is-selected bg-primary/10 text-primary")}>
              <button
                className="min-w-0 flex-1 truncate py-1 pr-1 text-left hover:bg-accent/40"
                style={{ paddingLeft: (depth + 1) * 10 + 2 }}
                onClick={() => onSelect(f.path)}
                title={`${f.path}（${f.size}B）`}>
                <span className="mr-1 inline-flex align-middle text-muted-foreground"><FileText size={12} /></span>{f.title || f.name}
              </button>
              <span className={cn("kb-layer-mark hidden shrink-0 text-[8px] font-medium sm:inline", fileLayer(f.path).className)}>{fileLayer(f.path).label}</span>
              <button className="kb-file-action shrink-0 rounded p-1 text-muted-foreground hover:text-primary"
                      aria-label={`改名 ${f.title || f.name}`} title="改名（全仓引用联动替换）" onClick={() => onRename(f.path)}><Pencil size={11} /></button>
              <button className="kb-file-action shrink-0 rounded p-1 text-muted-foreground hover:text-(--status-error)"
                      aria-label={`删除 ${f.title || f.name}`} title="删除（进 kb-trash，可恢复）" onClick={() => onDelete(f.path)}><Trash2 size={11} /></button>
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
  const [searchFocused, setSearchFocused] = useState(false)
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
    <div className="kb-tree flex min-h-0 flex-1 flex-col">
      <div className="kb-tree-toolbar">
        <div className={cn("kb-tree-search", searchFocused && "is-focused")}>
          <Search size={13} />
          <input
            value={q}
            onFocus={() => setSearchFocused(true)}
            onBlur={() => setSearchFocused(false)}
            onChange={(e) => setQ(e.target.value.toLowerCase())}
            onKeyDown={(e) => e.key === "Escape" && setQ("")}
            placeholder={`搜索 ${total} 份资料…`}
            aria-label="搜索知识库文件"
          />
          {q && <button className="kb-tree-clear" title="清空搜索" aria-label="清空搜索" onClick={() => setQ("")}><X size={12} /></button>}
        </div>
        <button className="kb-tree-tool" title="搜索同时匹配正文和路径" aria-label="搜索说明"><SlidersHorizontal size={13} /></button>
      </div>
      <div className="kb-tree-summary">
        <span><Compass size={11} />知识导航</span>
        <span className="font-mono">{q ? `${hits.length} 命中` : `${total} 文件`}</span>
      </div>
      {hits.length > 0 && (
        <div className="mb-1 max-h-40 shrink-0 overflow-y-auto rounded border bg-card/60 px-1 py-0.5">
            <p className="px-1 py-1 text-[10px] font-medium text-muted-foreground">正文命中 {hits.length} 篇</p>
          {hits.map((h) => (
            <button key={`${h.source}/${h.path}`}
                    className="block w-full truncate rounded px-1 py-0.5 text-left text-[11px] hover:bg-accent/40"
                    title={`${h.path}（命中 ${h.matches} 次）\n${h.snippet}`}
                    onClick={() => onSelect(h.path)}>
              <span className="inline-flex items-center gap-1 text-primary"><FileCode2 size={11} />{h.title || h.path}</span>
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
