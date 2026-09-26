import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { api } from "@/lib/api"
import type {
  Autonomy, ExecutorLlmView, HostCapability, ModelInfo, ProjectDetail, Task,
} from "@/lib/types"
import { cn } from "@/lib/utils"

// chip 全通（webui-trae-shell M3，2026-09-25）：
// RuntimeChip 写任务 preferred_runtime（open/failed 可改；''=重置回跟随缺省）；
// ModelChip 写项目 executor 覆写（持久 + 在内存会话即时换装；重置回路由缺省）。
// 无可复用 popover 组件，弹层为轻量自制（外点/Esc 关闭，绝对定位不抢滚动）。

// ── 通用 chip 弹层骨架 ─────────────────────────────────────────

function ChipShell({ label, title, active, disabled, children, onOpen }: {
  label: ReactNode
  title: string
  active: boolean
  disabled?: boolean
  onOpen?: () => void
  children: (close: () => void) => ReactNode
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false)
    document.addEventListener("mousedown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDoc)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        title={title}
        disabled={disabled}
        onClick={() => {
          if (disabled) return
          setOpen((v) => !v)
          if (!open) onOpen?.()
        }}
        className={cn(
          "max-w-52 truncate rounded-full border px-2.5 py-0.5 text-xs",
          disabled && "cursor-not-allowed opacity-50",
          active
            ? "border-primary/60 text-primary hover:bg-primary/10"
            : "text-muted-foreground hover:bg-accent hover:text-foreground")}
      >
        {label}
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-30 mb-1.5 w-64 rounded-lg border bg-popover p-1.5 text-popover-foreground shadow-xl">
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  )
}

// ── Runtime chip（任务级）──────────────────────────────────────

const RUNTIMES: { key: string; icon: string; label: string; hint: string }[] = [
  { key: "host", icon: "🪟", label: "Windows 本机", hint: "PowerShell，仅 Windows 目标/命令" },
  { key: "wsl", icon: "🐧", label: "WSL2", hint: "bash 兜底" },
  { key: "docker", icon: "🐳", label: "Docker 工具箱", hint: "nmap/sqlmap/ffuf 全套" },
  { key: "sandbox", icon: "🔒", label: "恶意样本沙箱", hint: "恶意样本唯一选择" },
]

export function RuntimeChip({ task, onChanged }: {
  task: Task | null
  onChanged?: () => void
}) {
  const [cap, setCap] = useState<HostCapability | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const pid = task?.project_id
  useEffect(() => {
    if (!pid) { setCap(null); return }
    let alive = true
    const load = () =>
      api.getProject(pid)
        .then((m) => { if (alive) setCap(m.capability ?? null) })
        .catch(() => {})
    load()
    const t = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  // 注意：早返回到 hooks 全部声明之后（task=null 也须保 hook 数恒定）
  const choose = useCallback(async (rt: string, close: () => void) => {
    if (busy || !task) return
    setBusy(true)
    setErr(null)
    try {
      await api.updateTask(task.id, { preferred_runtime: rt })
      onChanged?.()
      close()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }, [busy, task, onChanged])

  if (!task) return null
  const value = task.preferred_runtime || ""
  const editable = task.status === "open" || task.status === "failed"
  const cur = RUNTIMES.find((r) => r.key === value)

  const unavailable = (key: string): string | null => {
    if (!cap) return null
    if (key === "docker") return cap.docker.available ? null : cap.docker.detail || "Docker 不可用"
    if (key === "wsl") return cap.wsl.available ? null : cap.wsl.detail || "WSL2 不可用"
    return null
  }

  return (
    <ChipShell
      active={!!value}
      disabled={!editable || busy}
      title={!editable
        ? `任务${task.status === "claimed" ? "执行中" : "已完成"}：默认运行时不可改（当前 ${value || "未设"}）`
        : "任务默认运行时：run_cmd 省略 runtime 即按此执行；单条命令显式传仍可临时覆盖"}
      label={<span className="flex items-center gap-1">
        <span>⏳</span>
        {value ? `${cur?.icon ?? ""} ${value}` : "运行时·默认"}
      </span>}
    >
      {(close) => (
        <div className="max-h-72 overflow-auto">
          <div className="px-1 pb-1 text-[10px] text-muted-foreground">
            任务默认运行时{busy ? "（设置中…）" : ""}
          </div>
          <button
            type="button"
            onClick={() => void choose("", close)}
            className={cn(
              "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent",
              !value && "bg-accent/60")}
          >
            <span>↩️</span>
            <span className="min-w-0">
              <div>跟随缺省（不设默认）</div>
              <div className="text-[10px] text-muted-foreground">
                run_cmd 逐命令按能力清单显式选 runtime
              </div>
            </span>
          </button>
          {RUNTIMES.map((r) => {
            const why = unavailable(r.key)
            const selected = value === r.key
            return (
              <button
                key={r.key}
                type="button"
                disabled={!!why}
                title={why ?? r.hint}
                onClick={() => void choose(r.key, close)}
                className={cn(
                  "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent",
                  selected && "bg-accent/60", why && "cursor-not-allowed opacity-40")}
              >
                <span>{r.icon}</span>
                <span className="min-w-0 flex-1">
                  <div>{r.label}{selected ? " ✓" : ""}</div>
                  <div className="text-[10px] text-muted-foreground">
                    {why ?? r.hint}
                  </div>
                </span>
              </button>
            )
          })}
          {err && <div className="px-2 pt-1 text-[10px] text-destructive">{err}</div>}
        </div>
      )}
    </ChipShell>
  )
}

// ── Model chip（项目级 executor 覆写）─────────────────────────

export function ModelChip({ pid }: { pid: string }) {
  const [view, setView] = useState<ExecutorLlmView | null>(null)
  const [models, setModels] = useState<ModelInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.executorLlm(pid).then(setView).catch(() => {})
    api.models().then(setModels).catch(() => {})
  }, [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30_000)
    return () => clearInterval(t)
  }, [refresh])

  const ov = view?.override ?? null
  const eff = view?.effective ?? view?.default ?? null

  const apply = useCallback(async (fn: () => Promise<unknown>, close: () => void) => {
    if (busy) return
    setBusy(true)
    setErr(null)
    try {
      await fn()
      refresh()
      close()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }, [busy, refresh])

  const short = eff
    ? `${eff.provider}/${eff.model ? eff.model.split("/").pop() : "默认模型"}`
    : "未配置模型"

  return (
    <ChipShell
      active={!!ov}
      disabled={busy}
      title="项目 executor 模型覆写：本项目全部会话持久生效（重置回跟随路由缺省）"
      label={<span className="flex items-center gap-1">
        <span>🧠</span>
        <span className="max-w-36 truncate">{ov ? short : `跟随·${short}`}</span>
      </span>}
      onOpen={refresh}
    >
      {(close) => (
        <div className="max-h-72 overflow-auto">
          <div className="px-1 pb-1 text-[10px] text-muted-foreground">
            executor 模型{busy ? "（切换中…）" : ""}
          </div>
          <button
            type="button"
            onClick={() => void apply(() => api.resetExecutorLlm(pid), close)}
            className={cn(
              "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent",
              !ov && "bg-accent/60")}
          >
            <span>↩️</span>
            <span className="min-w-0">
              <div>跟随缺省{!ov ? " ✓" : ""}</div>
              <div className="truncate text-[10px] text-muted-foreground">
                {view?.default
                  ? `${view.default.provider}/${view.default.model.split("/").pop()}`
                  : "路由/供应商默认"}
              </div>
            </span>
          </button>
          {models?.providers.map((p) => (
            <div key={p.name} className="border-t border-white/10 pt-1">
              <button
                type="button"
                title={`覆写为 ${p.name} 默认模型`}
                onClick={() => void apply(
                  () => api.setExecutorLlm(pid, p.name), close)}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs hover:bg-accent"
              >
                <span className="flex-1 truncate font-medium">{p.name}</span>
                <span className="text-[10px] text-muted-foreground">默认模型</span>
              </button>
              {p.models.map((m) => {
                const selected = ov?.provider === p.name && !!ov.model
                  && (ov.model === m || ov.model.endsWith(`/${m}`))
                return (
                  <button
                    key={m}
                    type="button"
                    onClick={() => void apply(
                      () => api.setExecutorLlm(pid, p.name, m), close)}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-md py-1 pl-6 pr-2 text-left text-[11px] hover:bg-accent",
                      selected && "bg-accent/60 text-primary")}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {m.split("/").pop()}{selected ? " ✓" : ""}
                    </span>
                  </button>
                )
              })}
            </div>
          ))}
          {err && <div className="px-2 pt-1 text-[10px] text-destructive">{err}</div>}
        </div>
      )}
    </ChipShell>
  )
}

// ── Autonomy chip（项目级自主档，仅编排态）──────────────────────

// 轨默认档（须与后端 core/autonomy.py TRACK_DEFAULT_LEVEL 对齐）
const TRACK_DEFAULT_LEVEL: Record<string, string> = {
  ctf: "L0", pentest: "L1", redteam: "L0", research: "L1",
  malware: "L0", assessment: "L1",
}
const AUTONOMY_LEVELS: { key: Autonomy["level"]; label: string; hint: string }[] = [
  { key: "L0", label: "L0 全手动", hint: "开窗 / 起链全部人工确认" },
  { key: "L1", label: "L1 任务自动·开窗审批", hint: "任务自动认领续派，开窗仍需审批" },
  { key: "L2", label: "L2 全自动链", hint: "自动链按 max_chain_ticks 推进；安全闸不放松" },
]

export function AutonomyChip({ pid }: { pid: string }) {
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.getProject(pid).then(setDetail).catch(() => {})
  }, [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 30_000)
    return () => clearInterval(t)
  }, [refresh])

  const usage = detail?.usage ?? null
  const trackDefault = TRACK_DEFAULT_LEVEL[detail?.track ?? ""] ?? "L0"
  const level = usage?.level ?? null
  // 非默认档 / 暂停 / 显式开了自动派生 → chip 高亮（有覆写态）
  const changed = !!usage && (level !== trackDefault || usage.paused || !!usage.auto_derive)

  const patch = useCallback(async (body: Partial<Autonomy>, close: () => void) => {
    if (!usage || busy) return
    setBusy(true)
    setErr(null)
    try {
      // 只挑服务端认识的 autonomy 字段（usage 另带实时计数，剥干净）
      const cur: Autonomy = {
        level: usage.level, paused: usage.paused,
        sessions_cap: usage.sessions_cap, max_chain_ticks: usage.max_chain_ticks,
        token_budget: usage.token_budget, task_budget: usage.task_budget,
        auto_derive: usage.auto_derive ?? false,
      }
      await api.patchProjectConfig(pid, { autonomy: { ...cur, ...body } })
      refresh()
      close()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }, [usage, busy, pid, refresh])

  return (
    <ChipShell
      active={changed}
      disabled={busy || !usage}
      title="项目自主档（DESIGN §6.8）：级别 / 暂停 / 自动派生；预算编辑 M4 前在旧壳设置"
      label={<span className="flex items-center gap-1">
        <span>{usage?.paused ? "⏸" : "🧠"}</span>
        <span className="max-w-36 truncate">
          {level ?? "自主档"}{level && level !== trackDefault ? `·非默认(${trackDefault})` : ""}
        </span>
      </span>}
      onOpen={refresh}
    >
      {(close) => (
        <div className="max-h-80 overflow-auto">
          <div className="px-1 pb-1 text-[10px] text-muted-foreground">
            项目自主档{busy ? "（设置中…）" : ""}
          </div>
          {AUTONOMY_LEVELS.map((lv) => {
            const selected = level === lv.key
            return (
              <button
                key={lv.key}
                type="button"
                title={lv.hint}
                onClick={() => void patch({ level: lv.key }, close)}
                className={cn(
                  "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent",
                  selected && "bg-accent/60 text-primary")}
              >
                <span className="min-w-0 flex-1">
                  <div>{lv.label}{selected ? " ✓" : ""}</div>
                  <div className="text-[10px] text-muted-foreground">{lv.hint}</div>
                </span>
              </button>
            )
          })}
          <div className="my-1 border-t border-white/10" />
          <ToggleRow
            icon="⏸" label="暂停全部自主动作" on={!!usage?.paused}
            onClick={() => void patch({ paused: !usage?.paused }, close)}
          />
          <ToggleRow
            icon="🫧" label="任务空时自动派生下一批" on={!!usage?.auto_derive}
            hint="C2：mission 自动派生（§6.9）"
            onClick={() => void patch({ auto_derive: !usage?.auto_derive }, close)}
          />
          <div className="my-1 border-t border-white/10" />
          <button
            type="button"
            onClick={() => void patch({ level: trackDefault as Autonomy["level"] }, close)}
            className={cn(
              "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent",
              level === trackDefault && "bg-accent/60")}
          >
            <span>↩️</span>
            <span className="min-w-0 flex-1">
              <div>跟随缺省（轨默认 {trackDefault}）</div>
              <div className="text-[10px] text-muted-foreground">
                级别回 {trackDefault}；暂停/自动派生不回（逐项再点）
              </div>
            </span>
          </button>
          {err && <div className="px-2 pt-1 text-[10px] text-destructive">{err}</div>}
        </div>
      )}
    </ChipShell>
  )
}

function ToggleRow({ icon, label, hint, on, onClick }: {
  icon: string
  label: string
  hint?: string
  on: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      title={hint ?? label}
      onClick={onClick}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent"
    >
      <span>{icon}</span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className={cn(
        "inline-flex h-4 w-7 items-center rounded-full px-0.5 text-[10px]",
        on ? "justify-end bg-primary text-primary-foreground" : "justify-start bg-muted text-muted-foreground")}>
        {on ? "开" : "关"}
      </span>
    </button>
  )
}
