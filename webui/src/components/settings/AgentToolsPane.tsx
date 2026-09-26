import { useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { AgentTool, AgentToolParam } from "@/lib/types"

// 设置页「工具」tab（agent-tools-view M1，2026-09-24）：Agent 工具静态只读目录。
// 单一事实源 = 后端 AGENT_TOOLS（GET /api/agent-tools），页面只渲染不改写；
// 不标注角色白名单/环境依赖等动态可用性（后置观察）。

function typeText(p: AgentToolParam): string {
  const t = Array.isArray(p.type) ? p.type.join(" | ") : (p.type ?? "any")
  if (t === "array") {
    const inner = p.items?.type ?? "any"
    return `${inner}[]`
  }
  return t
}

function EnumChips({ values }: { values: string[] }) {
  return (
    <span className="ml-1 inline-flex flex-wrap gap-1 align-middle">
      {values.map((v) => (
        <code key={v} className="rounded bg-muted px-1 text-[10px] text-muted-foreground">
          {v}
        </code>
      ))}
    </span>
  )
}

function ToolCard({ tool }: { tool: AgentTool }) {
  const props = Object.entries(tool.input_schema.properties ?? {})
  const required = new Set(tool.input_schema.required ?? [])
  return (
    <div className="rounded border bg-card p-2.5">
      <code className="text-xs font-semibold text-foreground">{tool.name}</code>
      <p className="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-muted-foreground">
        {tool.description}
      </p>
      {props.length > 0 ? (
        <table className="mt-2 w-full text-[10px]">
          <tbody>
            {props.map(([pn, p]) => {
              const enums = p.enum ?? p.items?.enum
              return (
                <tr key={pn} className="align-top">
                  <td className="w-28 py-0.5 pr-2">
                    <code className="text-foreground">{pn}</code>
                    {required.has(pn) && <span className="ml-0.5 text-(--status-error)">*</span>}
                  </td>
                  <td className="py-0.5 pr-2 text-muted-foreground">{typeText(p)}</td>
                  <td className="py-0.5 text-muted-foreground">
                    {p.description}
                    {enums && enums.length > 0 && <EnumChips values={enums} />}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : (
        <p className="mt-1.5 text-[10px] text-muted-foreground">（无参数）</p>
      )}
    </div>
  )
}

export function AgentToolsPane() {
  const [tools, setTools] = useState<AgentTool[]>([])
  const [groups, setGroups] = useState<string[]>([])
  const [query, setQuery] = useState("")
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .agentTools()
      .then((r) => {
        if (!alive) return
        setTools(r.tools)
        setGroups(r.groups)
      })
      .catch((e) => { if (alive) setErr(String(e)) })
    return () => { alive = false }
  }, [])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return tools
    return tools.filter(
      (t) => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q))
  }, [tools, query])

  if (err) {
    return <div className="p-4 text-xs text-(--status-error)">工具目录加载失败：{err}</div>
  }

  return (
    <div className="flex h-full flex-col gap-2 p-3">
      <div className="flex shrink-0 items-center gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索工具名称或描述…"
          className="h-7 w-64 rounded border bg-background px-2 text-xs"
        />
        <span className="text-[10px] text-muted-foreground">
          共 {tools.length} 个工具{query && ` · 命中 ${filtered.length}`}；* 为必填参数
        </span>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-auto pr-1">
        {groups.map((g) => {
          const items = filtered.filter((t) => t.group === g)
          if (items.length === 0) return null
          return (
            <section key={g}>
              <h3 className="sticky top-0 z-10 bg-background py-1 text-[11px] font-semibold text-muted-foreground">
                {g}
                <span className="ml-1.5 font-normal text-muted-foreground/70">{items.length}</span>
              </h3>
              <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
                {items.map((t) => <ToolCard key={t.name} tool={t} />)}
              </div>
            </section>
          )
        })}
        {filtered.length === 0 && (
          <p className="py-8 text-center text-xs text-muted-foreground">没有匹配的工具</p>
        )}
      </div>
    </div>
  )
}
