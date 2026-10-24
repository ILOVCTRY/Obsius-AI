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
  tested: "text-(--viz-success)",
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

// R4 蓝图产出闭环（M2/M3，2026-10-13）：本页只「看」与「发起」——按钮建一条 chat 线程
// （主控 chat-orchestrator）+ 下发阶段 objective，随后跳「智能体工作台」（goto-agents）；
// 实际执行在工作台（主控 call_expert 扇出子专家），本页 4s 轮询反映 Agent 写回。
// M3：status=building 时露「派发重建」（阶段四/五），深析按钮在 building/built 收敛。
const ORCHESTRATOR_ID = "pentest-controller"

// 阶段一：模块划分 → 子专家 bb_blueprint_create 产骨架
function stageObjective(sampleSha: string): string {
  return [
    "【R4 蓝图 · 阶段一：模块划分】",
    `目标样本 sha256=${sampleSha}（headless 概览已在缓存，勿重跑反编译器）。`,
    "请把「模块划分」作为一个任务 call_expert 委派给 reverse-analyst（任务描述自包含）：",
    "1. 依据该样本的 headless 导出（list_symbols / strings_search / imports），",
    "   按**业务职能**聚类（网络通信/加密校验/文件持久化/许可校验/UI…），不按编译单元；",
    "   已知库函数（libc/API 包装）标 noise，不进模块。",
    `2. 用 bb_blueprint_create(name=…, goal=…, binary_sha256="${sampleSha}", ` +
    "modules=[{name,desc,func_addresses[]}]) 建骨架；每模块必附 func_addresses（hex 串）",
    "   与业务推断理由 desc；**接口 spec 先行钉死**。",
    '方法详见 skill_open("逆向重建手法") 阶段一。产出后由人类在「蓝图」页签流转 status。',
  ].join("\n")
}

// 阶段二：按模块并行深析 → 子专家 bb_blueprint_update 写回 spec/notes/status
function moduleAnalysisObjective(bp: Blueprint): string {
  const mods = bp.modules
    .map((m) => `  - ${m.name}（锚点 ${m.func_addresses.join(", ") || "无"}）`)
    .join("\n")
  return [
    "【R4 蓝图 · 阶段二：模块深析】",
    `蓝图 ${bp.id}「${bp.name}」共 ${bp.modules.length} 个模块：`,
    mods,
    "请把**每个模块**作为独立任务 call_expert 并发委派给 reverse-analyst（单轮 ≤8，超过分批）；",
    "每个任务自包含写明：模块名 + 该模块的 func_addresses 锚点清单。",
    '子专家按 skill_open("逆向重建手法") 阶段二执行：',
    '先查 func_kb 防重复 → decompile → bb_upsert_func 落结论（risk_tag 必带 "module:<模块名>"）',
    "→ bb_blueprint_update(module_name=…, notes=…) 写回；接口若发现 spec 划错，写 notes 上报、勿自行改接口。",
  ].join("\n")
}

// 阶段四/五：重建实现 + 组装（蓝图 status=building 后由人类点「派发重建」触发）
function reconstructObjective(bp: Blueprint): string {
  const mods = bp.modules
    .map((m) => `  - ${m.name} [${m.status}]（锚点 ${m.func_addresses.join(", ") || "无"}）`)
    .join("\n")
  return [
    "【R4 蓝图 · 阶段四/五：重建实现与组装】",
    `蓝图 ${bp.id}「${bp.name}」已进入重建（building），共 ${bp.modules.length} 个模块：`,
    mods,
    "请把**每个模块**作为独立任务 call_expert 并发委派给子专家（单轮 ≤8，超过分批）；",
    "任务描述自包含：模块名 + 该模块 func_addresses 锚点清单 + 蓝图 id（bb_query 自取 spec/notes）。",
    '子专家按 skill_open("逆向重建手法") 阶段四执行：',
    "1. 代码落 `<项目工作区>/artifacts/rebuild/<模块名>/`（run_cmd 写，合法路径）；",
    "2. 依 func_kb 原实现伪码构造输入/输出向量，写自测脚本；",
    "3. run_cmd 以 threat_class=untrusted + 容器 runtime（docker/sandbox）执行对拍——",
    "   **生成代码一律 untrusted，绝不 host 直跑**；",
    '4. 对拍全过 → bb_blueprint_update(module_name=…, module_status="tested")；',
    "   不过就修到过，或在 notes 记录失败原因上报，**不得虚标 tested**。",
    "全部模块 tested 后做阶段五组装：拼入口/胶水代码（接口冲突人工兜底）→ 容器整体冒烟 →",
    "产出「蓝图 vs 实现」差异表（哪个函数简化了、哪个行为未复现），经",
    'bb_add_artifact(kind="project") 落为文本记录（产物本体留在 artifacts/rebuild/，'
    + "由人类在文件树查看）。人类终验后蓝图置 built。",
  ].join("\n")
}

export function BlueprintsView({ pid, tick, sha, onLocate }: {
  pid: string
  tick: number
  /** 当前工作台样本 sha（生成蓝图默认绑定；空则禁用「生成蓝图」） */
  sha: string | null
  /** 点模块函数地址 → 工作台切回分析视图定位（顺带切样本） */
  onLocate: (sha: string, addr: string) => void
}) {
  const [items, setItems] = useState<Blueprint[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dispatching, setDispatching] = useState(false)

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

  // 建 chat 线程 + 下发阶段 objective → 跳智能体工作台（主控接手编排）
  const spawnThread = async (title: string, objective: string) => {
    setError(null)
    setDispatching(true)
    try {
      const th = await api.chatThreadCreate(pid, ORCHESTRATOR_ID, title)
      await api.chatSend(th.id, objective)
      window.dispatchEvent(new CustomEvent("goto-agents", { detail: { threadId: th.id } }))
    } catch (e) {
      setError(String(e))
    } finally {
      setDispatching(false)
    }
  }

  const next = bp ? NEXT_ACTION[bp.status] : null
  const badge = bp ? STATUS_BADGE[bp.status] : null

  return (
    <div className="flex h-full min-h-0">
      {/* 左：蓝图列表 */}
      <div className="w-72 shrink-0 overflow-auto border-r">
        <div className="border-b px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-xs font-medium">开发蓝图</h2>
            <button
              type="button"
              disabled={!sha || dispatching}
              title={sha ? "派给主控：按业务职能聚类产出蓝图骨架" : "先在样本栏选一个样本"}
              onClick={() => sha && spawnThread(`蓝图·模块划分 ${sha.slice(0, 8)}`, stageObjective(sha))}
              className="shrink-0 rounded bg-secondary px-1.5 py-0.5 text-[10px] text-primary hover:bg-accent disabled:opacity-40"
            >
              {dispatching ? "派发中…" : "+ 生成蓝图"}
            </button>
          </div>
          <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
            Agent 在会话中经 bb_blueprint_create 产出；人类侧只做 status 流转
          </p>
        </div>
        {items.length === 0 && (
          <p className="p-4 text-[11px] leading-relaxed text-muted-foreground">
            尚无蓝图。让 Agent 依 headless 导出概览按业务职能聚类产出蓝图骨架
            （网络通信/加密校验/文件持久化…），接口 spec 先行钉死；随后由人类在
            详情页流转 status（draft→reviewed→ready→building→built）。
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
              {bp.modules.length > 0 && bp.status !== "building" && bp.status !== "built" && (
                <Button size="sm" variant="outline" disabled={dispatching}
                        title="派给主控：每模块一子专家并行深析，写回 spec/notes/status"
                        onClick={() => spawnThread(`蓝图·模块深析 ${bp.name}`,
                                                  moduleAnalysisObjective(bp))}>
                  {dispatching ? "派发中…" : "按模块深析"}
                </Button>
              )}
              {bp.status === "building" && (
                <Button size="sm" variant="default" disabled={dispatching}
                        title="派给主控：每模块一子专家落码 + 容器自测对拍，全过标 tested 后汇总差异表"
                        onClick={() => spawnThread(`蓝图·重建 ${bp.name}`,
                                                  reconstructObjective(bp))}>
                  {dispatching ? "派发中…" : "派发重建"}
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
