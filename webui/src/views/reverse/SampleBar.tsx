import { useRef, useState } from "react"
import { ChevronDown, Upload, RefreshCw, BrainCircuit, Download, Loader2, ListTree, PenLine, Cpu, Trash2 } from "lucide-react"
import type { Asset, BinaryOverview, ToolState } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

// 样本条（DESIGN.md §12 逆向工作台）：多样本下拉 + 客观指标 + 工具三态灯 + 动作按钮。
// 引擎双模式（2026-10-01）：引擎下拉（IDA / Ghidra）紧邻状态文案；Ghidra 模式隐藏
// IDA 专属按钮与 MCP 灯（那些功能只有 IDA MCP 通道）。

export type Engine = "ida" | "ghidra"

const ENGINE_OPTIONS: { key: Engine; label: string; hint: string }[] = [
  { key: "ida", label: "IDA", hint: "IDA headless + MCP 实时桥" },
  { key: "ghidra", label: "Ghidra", hint: "Ghidra headless 反编译/反汇编" },
]

export function ToolLamp({ state, label, hint }: { state: ToolState; label: string; hint?: string }) {
  // installed=青（后端可用）；cached=琥珀（仅缓存可读）；off=灰
  const color =
    state === "installed" ? "bg-primary" : state === "cached" ? "bg-(--status-approval)" : "bg-muted-foreground/30"
  return (
    <span className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground" title={hint ?? `${label}: ${state}`}>
      <span className={cn("size-1.5 rounded-full", color)} />
      {label}
    </span>
  )
}

/** MCP 灯悬停提示：在线讲能力，离线讲怎么开 */
export function mcpLampHint(state: ToolState): string {
  return state === "installed"
    ? "MCP 已连接 IDA：写回实时写入当前库，缓存缺席也可实时读伪码/xref"
    : "MCP 离线：在 IDA 中打开库后按 Ctrl-Alt-M 启动（仅连本机 127.0.0.1:13337）"
}

/** 拉取进度环（2026-09-30）：total 未知时只有轨道不画进度（无分母退化态） */
function PullRing({ pulled, total }: { pulled: number; total: number | null }) {
  const R = 7
  const C = 2 * Math.PI * R
  const pct = total && total > 0 ? Math.min(100, (pulled / total) * 100) : null
  return (
    <svg viewBox="0 0 18 18" className="size-3.5 -rotate-90 shrink-0">
      <circle cx="9" cy="9" r={R} className="fill-none stroke-muted-foreground/30" strokeWidth="2.5" />
      {pct !== null && (
        <circle cx="9" cy="9" r={R} className="fill-none stroke-primary"
                strokeWidth="2.5" strokeLinecap="round"
                strokeDasharray={C} strokeDashoffset={C * (1 - pct / 100)} />
      )}
    </svg>
  )
}

function sampleName(a: Asset): string {
  const fn = a.meta?.filename
  return typeof fn === "string" && fn ? fn : `${a.value.slice(0, 12)}…`
}

interface Props {
  samples: Asset[]
  sha: string | null
  onSelect: (sha: string) => void
  /** 从项目移除样本（父组件负责确认框 + 刷新；2026-10-01 样本删除） */
  onDeleteSample?: (sha: string) => void
  overview: BinaryOverview | null
  /** 样本配置的引擎模式（ida / ghidra；2026-10-01 双模式） */
  engine: Engine
  /** 切换引擎（落库到样本 meta，父组件负责刷新 overview） */
  onSelectEngine?: (engine: Engine) => Promise<void>
  triaging: boolean
  uploading: boolean
  busyAi: boolean
  onUpload: (file: File) => void
  onRetry: () => void
  onAiTriage: () => void
  /** IDA GUI 手改名 → func_kb（父组件负责 Job 轮询，返回给用户看的结果文案） */
  onPullNames: () => Promise<string>
  /** GUI IDA MCP 拉全量函数清单进轻量缓存 + 手改名 diff 回拉（需 MCP 灯亮） */
  onPullIdaFunctions: () => Promise<string>
  /** 反向：func_kb 有效命名批量写回 GUI IDA 当前库（自动名不推） */
  onPushNames: () => Promise<string>
  /** 拉取进度（非 null=拉取中，按钮变「停止」+进度环） */
  pullProgress: { pulled: number; total: number | null } | null
  onStopPull: () => void
  /** headless 导出进度（非 null=导出中；含阶段与可停性；2026-10-01 独立进度条） */
  triageProgress: { phase: string; done: number; total: number; stoppable: boolean } | null
  onStopTriage: () => void
  triageMsg: string | null
}

export function SampleBar({
  samples, sha, onSelect, onDeleteSample, overview, engine, onSelectEngine, triaging, uploading, busyAi,
  onUpload, onRetry, onAiTriage,
  onPullNames, onPullIdaFunctions, onPushNames, pullProgress, onStopPull,
  triageProgress, onStopTriage, triageMsg,
}: Props) {
  const [open, setOpen] = useState(false)
  const [pulling, setPulling] = useState(false)
  const [pullMsg, setPullMsg] = useState<string | null>(null)
  const [idaBusy, setIdaBusy] = useState<null | "pull-funcs" | "push-names">(null)
  const [idaMsg, setIdaMsg] = useState<string | null>(null)
  const [engOpen, setEngOpen] = useState(false)
  const [engBusy, setEngBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const ghidra = engine === "ghidra"
  // 缓存产出引擎（旧缓存无此字段 → undefined，不提示）
  const cacheEngine = overview?.meta?.engine
  const engineMismatch = !!cacheEngine && cacheEngine !== engine

  const pull = async () => {
    if (!sha) return
    setPulling(true)
    setPullMsg(null)
    try {
      setPullMsg(await onPullNames())
    } catch (e) {
      setPullMsg(String(e))
    } finally {
      setPulling(false)
    }
  }

  // GUI IDA MCP 动作共用一把忙锁（拉取/反向同步互斥；结果文案同槽展示）
  const runIda = async (slot: "pull-funcs" | "push-names", fn: () => Promise<string>) => {
    if (!sha || idaBusy) return
    setIdaBusy(slot)
    setIdaMsg(null)
    try {
      setIdaMsg(await fn())
    } catch (e) {
      setIdaMsg(String(e))
    } finally {
      setIdaBusy(null)
    }
  }
  const mcpReady = overview?.tools.mcp.state === "installed"
  // 引擎切换：落库到样本 meta（父组件刷新 overview 使缓存一致性提示生效）
  const pickEngine = async (next: Engine) => {
    if (!sha || engBusy || !onSelectEngine || next === engine) return
    setEngBusy(true)
    try {
      await onSelectEngine(next)
    } finally {
      setEngBusy(false)
    }
  }
  const current = samples.find((a) => a.value === sha)
  const triage = (overview?.asset_meta?.triage ?? null) as
    | { packer_suspect?: boolean; backend?: string } | null
  const coverage = overview && overview.function_count > 0
    ? Math.round((overview.analyzed_count / overview.function_count) * 100)
    : null

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-1.5">
      {/* 多样本自绘下拉 */}
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex h-7 min-w-44 max-w-64 items-center justify-between gap-2 rounded border px-2 text-xs"
        >
          <span className="truncate font-mono">{current ? sampleName(current) : "选择样本"}</span>
          <ChevronDown className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")} />
        </button>
        {open && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <div className="absolute left-0 top-full z-50 mt-1 max-h-72 w-80 overflow-auto rounded-md border bg-popover p-1 text-popover-foreground shadow-md">
              {samples.length === 0 && (
                <p className="px-2 py-1.5 text-[11px] text-muted-foreground">还没有样本，先上传一个</p>
              )}
              {samples.map((a) => (
                <div
                  key={a.id}
                  className={cn(
                    "group flex items-center rounded hover:bg-accent/40",
                    a.value === sha && "bg-primary/10 text-primary",
                  )}
                >
                  <button
                    type="button"
                    onClick={() => { onSelect(a.value); setOpen(false) }}
                    className="min-w-0 flex-1 truncate px-2 py-1 text-left font-mono text-[11px]"
                    title={a.value}
                  >
                    {sampleName(a)} <span className="text-muted-foreground">{a.value.slice(0, 12)}…</span>
                  </button>
                  {onDeleteSample && (
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); onDeleteSample(a.value) }}
                      className="mr-1 shrink-0 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-(--status-error)/15 hover:text-(--status-error) group-hover:opacity-100"
                      title="从项目中移除该样本"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* 当前样本·从项目移除（父组件弹确认框；连带函数条目/发现/逻辑块/缓存） */}
      {sha && onDeleteSample && (
        <button
          type="button"
          onClick={() => onDeleteSample(sha)}
          className="flex h-7 shrink-0 items-center gap-1 rounded border px-2 text-[11px] text-muted-foreground transition-colors hover:border-(--status-error)/40 hover:text-(--status-error)"
          title="从项目中移除当前样本（连带其函数条目 / 发现 / 逻辑块 / 缓存文件，不可逆）"
        >
          <Trash2 className="size-3" />移除
        </button>
      )}

      {sha && (
        <span className="font-mono text-[10px] text-muted-foreground" title={sha}>{sha.slice(0, 16)}…</span>
      )}
      {overview?.meta && (
        <span className="font-mono text-[10px] text-muted-foreground">
          {[overview.meta.arch, overview.meta.bits ? `${overview.meta.bits}bit` : null, overview.meta.endian]
            .filter(Boolean).join("/")}
        </span>
      )}
      {triage?.packer_suspect && (
        <span className="rounded bg-(--status-error)/15 px-1.5 py-0.5 text-[10px] text-(--status-error)">壳嫌疑</span>
      )}
      {overview && (
        <span className="font-mono text-[10px] text-muted-foreground">
          {overview.function_count} 函数{coverage !== null && ` · 覆盖 ${coverage}%`} · {overview.risk_count} 风险
        </span>
      )}
      {!overview?.cached && sha && (
        <span className="text-[10px] text-(--status-approval)">
          {triaging ? "headless 分诊中…" : "未分诊"}
        </span>
      )}

      {/* 引擎下拉（IDA / Ghidra）：紧邻状态文案，落库到样本 meta */}
      {sha && (
        <div className="relative">
          <button
            type="button"
            onClick={() => setEngOpen((o) => !o)}
            disabled={engBusy || !onSelectEngine}
            title={ghidra
              ? "当前引擎：Ghidra（headless 反编译；反汇编小样本导出内嵌、大样本按需）"
              : "当前引擎：IDA（IDA headless + IDA MCP 实时桥）"}
            className="flex h-7 items-center gap-1 rounded border px-2 text-[11px] disabled:opacity-60"
          >
            {engBusy ? <Loader2 className="size-3 animate-spin" /> : <Cpu className="size-3" />}
            <span className="font-mono">{ghidra ? "Ghidra" : "IDA"}</span>
            <ChevronDown className={cn("size-3.5 text-muted-foreground transition-transform", engOpen && "rotate-180")} />
          </button>
          {engOpen && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setEngOpen(false)} />
              <div className="absolute left-0 top-full z-50 mt-1 w-52 overflow-hidden rounded-md border bg-popover p-1 text-popover-foreground shadow-md">
                {ENGINE_OPTIONS.map((o) => (
                  <button
                    key={o.key}
                    type="button"
                    onClick={() => { setEngOpen(false); void pickEngine(o.key) }}
                    className={cn(
                      "flex w-full flex-col items-start gap-0.5 rounded px-2 py-1.5 text-left hover:bg-accent/40",
                      o.key === engine && "bg-primary/10 text-primary",
                    )}
                  >
                    <span className="font-mono text-[11px]">{o.label}</span>
                    <span className="text-[10px] text-muted-foreground">{o.hint}</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {engineMismatch && (
        <span
          className="text-[10px] text-(--status-approval)"
          title={`当前缓存由 ${String(cacheEngine).toUpperCase()} 产出，与所选 ${engine.toUpperCase()} 不一致——点「开始分析」重新分析覆盖`}
        >
          缓存来自 {String(cacheEngine).toUpperCase()}，需重跑
        </span>
      )}

      <span className="flex-1" />

      {overview && (
        <span className="flex items-center gap-2">
          <ToolLamp state={overview.tools.ida.state} label="IDA" />
          <ToolLamp state={overview.tools.ghidra.state} label="Ghidra" />
          {/* MCP 灯仅 IDA 模式（MCP 桥为 IDA 专属；Ghidra 模式无此通道） */}
          {!ghidra && (
            <ToolLamp state={overview.tools.mcp.state} label="MCP" hint={mcpLampHint(overview.tools.mcp.state)} />
          )}
        </span>
      )}

      <input
        ref={fileRef}
        type="file"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) onUpload(f)
          e.target.value = ""
        }}
      />
      <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" onClick={() => fileRef.current?.click()}
              disabled={uploading}>
        <Upload className="size-3" />{uploading ? "上传中…" : "上传样本"}
      </Button>
      <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" onClick={onRetry}
              disabled={!sha || triaging}
              title={ghidra
                ? "headless 全量导出（Ghidra：小样本内嵌反汇编、大样本按需反汇编；>20MB 会二次确认）"
                : "headless 全量导出（>20MB 大样本会二次确认；也可改走 IDA 拉取）"}>
        <RefreshCw className={cn("size-3", triaging && "animate-spin")} />{triaging ? "分析中…" : "开始分析"}
      </Button>
      {triageProgress && (() => {
        // 阶段：分析中（不确定态）→ 反编译 X/Y（确定态）
        const analyzing = triageProgress.phase === "analyzing"
          || triageProgress.phase === "starting" || triageProgress.total <= 0
        const pct = analyzing ? 0
          : Math.min(100, Math.round((triageProgress.done / triageProgress.total) * 100))
        return (
          <span className="flex items-center gap-2">
            <span className="flex items-center gap-1.5">
              <span className="block h-1.5 w-28 overflow-hidden rounded-full bg-muted">
                {analyzing
                  ? <span className="block h-full w-1/3 animate-pulse rounded-full bg-primary" />
                  : <span className="block h-full rounded-full bg-primary transition-all"
                          style={{ width: `${pct}%` }} />}
              </span>
              <span className="font-mono text-[10px] text-muted-foreground">
                {analyzing ? "分析中…" : `反编译 ${triageProgress.done}/${triageProgress.total}`}
              </span>
            </span>
            <Button size="sm" variant="outline" className="h-7 gap-1.5 text-[11px]"
                    onClick={onStopTriage}
                    disabled={!triageProgress.stoppable}
                    title={triageProgress.stoppable
                      ? "协作式停止：分析阶段立即停、反编译阶段当前函数完成后停（已导出部分保留）"
                      : "当前引擎（IDA）不支持中断——请等其完成"}>
              <span className={cn("inline-block size-2 rounded-[2px] bg-(--status-error)",
                                  !triageProgress.stoppable && "bg-muted-foreground/40")} />
              <span className={cn(!triageProgress.stoppable && "text-muted-foreground")}>停止</span>
            </Button>
          </span>
        )
      })()}
      {triageMsg && (
        <span className="max-w-80 truncate font-mono text-[10px] text-muted-foreground" title={triageMsg}>
          {triageMsg}
        </span>
      )}
      {/* IDA 专属动作（拉取函数 / 同步改名 / 写回命名）：Ghidra 模式隐藏
          （这些能力只有 IDA MCP / IDA 库通道，Ghidra headless 无对等写回） */}
      {!ghidra && (
        <>
          {pullProgress ? (
            <Button size="sm" variant="outline" className="h-7 gap-1.5 text-[11px]" onClick={onStopPull}
                    title={pullProgress.total
                      ? `已拉 ${pullProgress.pulled}/${pullProgress.total} 个函数，点击停止（已拉部分保持生效，可断点续拉）`
                      : `已拉 ${pullProgress.pulled} 个函数，点击停止（已拉部分保持生效）`}>
              <PullRing pulled={pullProgress.pulled} total={pullProgress.total} />
              {pullProgress.total && pullProgress.total > 0
                ? `${Math.min(100, Math.round((pullProgress.pulled / pullProgress.total) * 100))}%`
                : `${pullProgress.pulled} 个`}
              <span className="text-muted-foreground">停止</span>
            </Button>
          ) : (
            <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]"
                    onClick={() => runIda("pull-funcs", onPullIdaFunctions)}
                    disabled={!sha || idaBusy !== null || !mcpReady}
                    title={overview?.meta?.partial
                      ? `继续上次拉取（已拉 ${overview.function_count}${overview.meta.total_functions ? `/${overview.meta.total_functions}` : ""}，从断点续传）`
                      : "连 GUI IDA（MCP 灯亮，Ctrl-Alt-M 启动）拉全量函数清单进缓存；IDA 里手改的名字一并回拉 func_kb"}>
              {idaBusy === "pull-funcs" ? <Loader2 className="size-3 animate-spin" /> : <ListTree className="size-3" />}
              {overview?.meta?.partial ? "继续拉取" : "从 IDA 拉取函数"}
            </Button>
          )}
          <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" onClick={pull}
                  disabled={!sha || pulling || !overview?.db_path}
                  title="把 IDA 里手改的函数名拉回 func_kb（需已有 IDA 库）">
            {pulling ? <Loader2 className="size-3 animate-spin" /> : <Download className="size-3" />}
            ⬇ 从 IDA 同步改名
          </Button>
          <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]"
                  onClick={() => runIda("push-names", onPushNames)}
                  disabled={!sha || idaBusy !== null || !mcpReady}
                  title="把平台侧有效命名（AI/人工）批量写回 IDA 当前库；自动名不推，写完请在 IDA 里保存落盘">
            {idaBusy === "push-names" ? <Loader2 className="size-3 animate-spin" /> : <PenLine className="size-3" />}
            同步命名到 IDA
          </Button>
        </>
      )}
      {pullMsg && (
        <span className="max-w-72 truncate font-mono text-[10px] text-muted-foreground" title={pullMsg}>
          {pullMsg}
        </span>
      )}
      {idaMsg && (
        <span className="max-w-80 truncate font-mono text-[10px] text-muted-foreground" title={idaMsg}>
          {idaMsg}
        </span>
      )}
      <Button size="sm" className="h-7 gap-1 text-[11px]" onClick={onAiTriage}
              disabled={!sha || busyAi || !overview?.cached}
              title="只发被动任务给 AI：客观全量已在缓存，AI 专注理解与结论">
        <BrainCircuit className="size-3" />{busyAi ? "已发布" : "AI triage"}
      </Button>
    </div>
  )
}
