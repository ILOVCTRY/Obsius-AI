import { useCallback, useEffect, useMemo, useState } from "react"
import { Download, FileText, RefreshCw } from "lucide-react"
import { api } from "@/lib/api"
import type { Artifact } from "@/lib/types"
import { fmtDateTimeMin } from "@/lib/datetime"
import { cn } from "@/lib/utils"

// 产物文件浏览（webui-trae-shell M3，2026-09-25）：侧栏「📁 文件」资源入口。
// 只读——黑板 artifacts 注册表为据：按 kind 过滤 + 名称搜索；文本类走 content 口
// 预览，二进制/附件走 download 口下载（原始文件名）。

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`
  if (n >= 1024) return `${Math.round(n / 1024)}KB`
  return `${n}B`
}

function baseName(p: string): string {
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"))
  return i >= 0 ? p.slice(i + 1) : p
}

const KIND_LABEL: Record<string, string> = {
  attachment: "附件",
  "debug-log": "调试日志",
}

export function FilesView({ pid }: { pid: string | null | undefined }) {
  const [arts, setArts] = useState<Artifact[]>([])
  const [kind, setKind] = useState<string>("all")
  const [q, setQ] = useState("")
  const [selId, setSelId] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ text?: string; error?: string } | null>(null)

  const refresh = useCallback(() => {
    if (!pid) { setArts([]); return }
    api.artifacts(pid).then(setArts).catch(() => {})
  }, [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 10_000)
    return () => clearInterval(t)
  }, [refresh])

  const kinds = useMemo(
    () => ["all", ...Array.from(new Set(arts.map((a) => a.kind)))], [arts])

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return arts
      .filter((a) => kind === "all" || a.kind === kind)
      .filter((a) => !needle
        || a.path.toLowerCase().includes(needle)
        || (a.description || "").toLowerCase().includes(needle))
      .sort((x, y) => (x.created_at < y.created_at ? 1 : -1))
  }, [arts, kind, q])

  const selected = useMemo(
    () => arts.find((a) => a.id === selId) ?? null, [arts, selId])

  // 选中即拉文本预览（失败=二进制等，转下载引导）
  useEffect(() => {
    if (!pid || !selected) { setPreview(null); return }
    let alive = true
    setPreview(null)
    api.artifactContent(pid, selected.path)
      .then((r) => { if (alive) setPreview({ text: r.content }) })
      .catch((e) => { if (alive) setPreview({ error: String(e) }) })
    return () => { alive = false }
  }, [pid, selected])

  if (!pid) {
    return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      未选择项目
    </div>
  }

  const nameOf = (a: Artifact) =>
    a.meta?.original_name || a.description || baseName(a.path)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
        <span className="text-sm font-medium">📁 产物文件</span>
        <span className="text-[11px] text-muted-foreground">{arts.length} 个</span>
        <span className="flex-1" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索文件名…"
          className="h-7 w-44 rounded-md border bg-transparent px-2 text-xs focus:outline-none"
        />
        <button type="button" onClick={refresh} title="刷新"
          className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground">
          <RefreshCw className="size-3.5" />
        </button>
      </div>

      <div className="flex shrink-0 flex-wrap gap-1 border-b px-3 py-1.5">
        {kinds.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setKind(k)}
            className={cn(
              "rounded-full border px-2 py-0.5 text-[11px]",
              kind === k
                ? "bg-secondary text-primary"
                : "text-muted-foreground hover:bg-accent")}
          >
            {k === "all" ? "全部" : KIND_LABEL[k] ?? k}
          </button>
        ))}
      </div>

      <div className="flex min-h-0 flex-1">
        {/* 文件清单 */}
        <div className="w-72 shrink-0 overflow-auto border-r">
          {shown.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              无产物
            </div>
          )}
          {shown.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => setSelId(a.id)}
              className={cn(
                "flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-accent",
                selected?.id === a.id && "bg-accent/70")}
            >
              <FileText className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs">{nameOf(a)}</span>
                <span className="block truncate text-[10px] text-muted-foreground">
                  {KIND_LABEL[a.kind] ?? a.kind}
                  {a.meta?.size ? ` · ${fmtBytes(a.meta.size)}` : ""}
                  {" · "}{fmtDateTimeMin(a.created_at)}
                </span>
              </span>
            </button>
          ))}
        </div>

        {/* 预览 */}
        <div className="min-w-0 flex-1 overflow-hidden">
          {!selected ? (
            <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
              选择文件查看内容
            </div>
          ) : (
            <div className="flex h-full min-h-0 flex-col">
              <div className="flex shrink-0 items-center gap-2 border-b px-3 py-1.5">
                <span className="min-w-0 flex-1 truncate text-xs">{nameOf(selected)}</span>
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                  {selected.sha256.slice(0, 12)}
                </span>
                <button
                  type="button"
                  onClick={() => api.downloadArtifact(pid, selected.id).catch(() => {})}
                  title="下载原文件"
                  className="flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground"
                >
                  <Download className="size-3" /> 下载
                </button>
              </div>
              <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-all p-3 text-[11px] leading-5">
                {preview?.error
                  ? `无法文本预览（${preview.error}），请点「下载」查看原文件`
                  : preview?.text ?? "加载中…"}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
