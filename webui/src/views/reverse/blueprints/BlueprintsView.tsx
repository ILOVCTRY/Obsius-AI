import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { Blueprint, BlueprintModule, BlueprintStatus } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { fmtDateTime, utcTitle } from "@/lib/datetime"
import { MarkdownView } from "@/components/settings/MarkdownView"

// 蓝图视图（R4 逆向开发管线，DESIGN.md §9 R4）：rev-generic 工作台第三个子 tab。
// 模块卡片可点函数地址回跳分析视图；蓝图整体 status 是人类职责（Agent 无入口）。

const STATUS_BADGE: Record<BlueprintStatus, { text: string; cls: string }> = {
  draft: { text: "草稿", cls: "text-muted-foreground" },
  reviewed: { text: "已评审", cls: "text-muted-foreground" },
  ready: { text: "已批准·待重建", cls: "text-(--status-approval)" },
  building: { text: "重建中", cls: "text-primary" },
  built: { text: "已建成", cls: "text-primary" },
}

// 人类侧流转按钮（白名单只进不退，服务端强校验）
const NEXT_ACTION: Record<BlueprintStatus, { to: BlueprintStatus; label: string } | null> = {
  draft: { to: "reviewed", label: "标记已评审" },
  reviewed: { to: "ready", label: "批准进入重建（ready）" },
  ready: { to: "building", label: "开始重建（building）" },
  building: { to: "built", label: "终验通过（built）" },
  built: null,
}

const MODULE_STATUS_TEXT: Record<string, string> = {
  pending: "待分析",
  analyzed: "已分析",
  specd: "接口已定",
  tested: "自测通过",
}

const MODULE_STATUS_CLS: Record<string, string> = {
  pending: "text-muted-foreground",
  analyzed: "text-primary",
  specd: "text-primary",
  tested: "text-[#4ade80]",
}

function ModuleCard({ m, onLocate, bpSha }: {
  m: BlueprintModule
  bpSha: string
  onLocate: (sha: string, addr: string) => void
}) {
  return (
    <div className="rounded-lg border p-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">{m.name}</span>
        <Badge variant="outline" className={cn("font-mono text-[10px]", MODULE_STATUS_CLS[m.status])}>
          {MODULE_STATUS_TEXT[m.status] ?? m.status}
        </Badge>
      </div>
      {m.desc && <p className="mt-1 text-xs text-muted-foreground">{m.desc}</p>}
      {m.spec && (
        <div className="mt-2">
          <p className="text-[10px] text-muted-foreground">接口约定（spec）</p>
          <pre className="mt-1 max-h-32 overflow-auto rounded bg-card p-2 font-mono text-[10px] whitespace-pre-wrap">{m.spec}</pre>
        </div>
      )}
      {m.notes && (
        <div className="mt-2">
          <p className="text-[10px] text-muted-foreground">实现要点</p>
          <pre className="mt-1 max-h-32 overflow-auto rounded bg-card p-2 font-mono text-[10px] whitespace-pre-wrap">{m.notes}</pre>
        </div>
      )}
      {m.func_addresses.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {m.func_addresses.map((a) => (
            <button
              key={a} type="button"
              onClick={() => onLocate(bpSha, a)}
              className="rounded bg-accent px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-primary"
              title="回逆向分析视图定位该函数"
            >
              {a}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function BlueprintsView({ pid, tick, onLocate }: {
  pid: string
  tick: number
  /** 点模块函数地址 → 工作台切回分析视图定位（顺带切样本） */
  onLocate: (sha: string, addr: string) => void
}) {
  const [items, setItems] = useState<Blueprint[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.blueprints(pid).then((rs) => {
      setItems(rs)
      setSelected((cur) => (cur && rs.some((r) => r.id === cur) ? cur : rs[0]?.id ?? null))
    }).catch((e) => setError(String(e)))
  }, [pid])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh, tick])

  const bp = items.find((b) => b.id === selected) ?? null

  const advance = async (to: BlueprintStatus) => {
    if (!bp) return
    setError(null)
    try {
      await api.updateBlueprint(pid, bp.id, { status: to })
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const next = bp ? NEXT_ACTION[bp.status] : null
  const badge = bp ? STATUS_BADGE[bp.status] : null

  return (
    <div className="flex h-full min-h-0">
      {/* 左：蓝图列表 */}
      <div className="w-72 shrink-0 overflow-auto border-r">
        <div className="border-b px-3 py-2">
          <h2 className="text-xs font-medium">开发蓝图</h2>
          <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
            模块划分任务（task_type=blueprint）经 bb_blueprint_create 产出
          </p>
        </div>
        {items.length === 0 && (
          <p className="p-4 text-[11px] leading-relaxed text-muted-foreground">
            尚无蓝图。发布一个模块划分任务：Agent 依 headless 导出概览按业务职能
            聚类产出蓝图骨架（网络通信/加密校验/文件持久化…），接口 spec 先行钉死。
          </p>
        )}
        {items.map((b) => (
          <button
            key={b.id} type="button" onClick={() => setSelected(b.id)}
            className={cn("block w-full border-b px-3 py-2 text-left hover:bg-accent",
              b.id === selected && "bg-accent")}
          >
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-xs font-medium">{b.name}</span>
              <Badge variant="outline" className={cn("shrink-0 font-mono text-[9px]", STATUS_BADGE[b.status].cls)}>
                {STATUS_BADGE[b.status].text}
              </Badge>
            </div>
            <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
              <span>{b.modules.length} 模块</span>
              <span title={utcTitle(b.updated_at)}>{fmtDateTime(b.updated_at)}</span>
            </div>
          </button>
        ))}
      </div>

      {/* 右：蓝图详情 */}
      <div className="min-w-0 flex-1 overflow-auto">
        {error && <p className="p-3 text-xs text-(--status-error)">{error}</p>}
        {!bp ? (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            从左侧选择蓝图
          </div>
        ) : (
          <div className="space-y-4 p-4">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-base font-semibold">{bp.name}</h1>
              {badge && <Badge variant="outline" className={cn("font-mono text-[10px]", badge.cls)}>{badge.text}</Badge>}
              {next && (
                <Button size="sm" variant={bp.status === "reviewed" ? "default" : "outline"}
                        onClick={() => advance(next.to)}>
                  {next.label}
                </Button>
              )}
              <span className="flex-1" />
              {bp.binary_sha256 && (
                <span className="font-mono text-[10px] text-muted-foreground" title="目标样本 sha256">
                  sha256 {bp.binary_sha256.slice(0, 12)}…
                </span>
              )}
            </div>
            {bp.goal && <p className="text-sm text-muted-foreground">{bp.goal}</p>}

            <div>
              <h2 className="mb-2 text-xs font-medium text-muted-foreground">
                业务模块（{bp.modules.length}）
              </h2>
              <div className="grid gap-2 md:grid-cols-2">
                {bp.modules.map((m) => (
                  <ModuleCard key={m.name} m={m} bpSha={bp.binary_sha256} onLocate={onLocate} />
                ))}
              </div>
            </div>

            {bp.content_md && (
              <div>
                <h2 className="mb-1 text-xs font-medium text-muted-foreground">蓝图正文</h2>
                <div className="rounded-lg border p-4">
                  <MarkdownView content={bp.content_md} prefix={`bp-${bp.id}`} />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
