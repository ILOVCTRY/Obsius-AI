import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { AdvisorConfig, ModelInfo } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// 设置页「顾问」tab（advisor-settings-ui D10，2026-09-24）：卡死/顾问机制四个
// 参数项目级可调。经通用 PATCH config 的 advisor 段整段写入，下次新建会话窗生效。

const DEFAULTS = { stuck_after: 12, stuck_max_extensions: 2, closing_max_rounds: 2 }
const RANGES: { key: keyof typeof DEFAULTS; label: string; lo: number; hi: number; hint: string }[] = [
  { key: "stuck_after", label: "观察窗步数", lo: 6, hi: 30,
    hint: "连续 N 步无黑板写入进展 → 召唤顾问" },
  { key: "stuck_max_extensions", label: "静默延长上限", lo: 0, hi: 4,
    hint: "活跃探索时静默延长观察窗的次数；0 = 关闭静默延长" },
  { key: "closing_max_rounds", label: "收尾确认轮", lo: 0, hi: 3,
    hint: "complete 申报的确认轮上限；0 = 首次申报即放行" },
]

const selectCls =
  "rounded border bg-background px-1.5 py-1 text-xs [color-scheme:dark] [&>option]:bg-popover [&>option]:text-popover-foreground"

export function AdvisorPane({ pid }: { pid?: string | null }) {
  const [values, setValues] = useState(DEFAULTS)
  const [provider, setProvider] = useState("")
  const [model, setModel] = useState("")
  const [models, setModels] = useState<ModelInfo | null>(null)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(() => {
    if (!pid) return
    let alive = true
    setLoading(true)
    Promise.all([api.getProject(pid), api.models()])
      .then(([proj, minfo]) => {
        if (!alive) return
        const adv = (proj.config?.advisor ?? null) as Partial<AdvisorConfig> | null
        setValues({
          stuck_after: adv?.stuck_after ?? DEFAULTS.stuck_after,
          stuck_max_extensions: adv?.stuck_max_extensions ?? DEFAULTS.stuck_max_extensions,
          closing_max_rounds: adv?.closing_max_rounds ?? DEFAULTS.closing_max_rounds,
        })
        setProvider(adv?.provider ?? "")
        setModel(adv?.model ?? "")
        setModels(minfo)
      })
      .catch((e) => { if (alive) setErr(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [pid])

  useEffect(() => { reload() }, [reload])

  if (!pid) {
    return (
      <div className="p-4 text-xs text-muted-foreground">
        顾问配置按项目保存，请从具体项目内打开。
      </div>
    )
  }
  if (loading) {
    return <div className="p-4 text-xs text-muted-foreground">{err ?? "加载中…"}</div>
  }

  const invalid = RANGES.some(
    (r) => !Number.isInteger(values[r.key]) || values[r.key] < r.lo || values[r.key] > r.hi)
  const isDefault =
    RANGES.every((r) => values[r.key] === DEFAULTS[r.key]) && !provider && !model

  const providerModels =
    models?.providers.find((p) => p.name === provider)?.models ?? []

  const save = async () => {
    setSaved(false)
    setErr(null)
    // 整段替换语义：全缺省且无覆写 → 显式发 null 剥键恢复默认
    const advisor = isDefault
      ? null
      : {
          ...values,
          ...(provider ? { provider, ...(model ? { model } : {}) } : {}),
        }
    try {
      await api.patchProjectConfig(pid, { advisor })
      setSaved(true)
      reload() // 保存后重拉确认（服务端归一化口径）
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4 text-xs">
      <div className="mb-3 rounded border border-(--status-warning)/40 bg-(--status-warning)/5 p-2.5 text-(--status-warning)">
        ⚠ 配置在工厂/会话构造期读取：<b>在跑会话不生效，下次新建会话窗生效</b>。
      </div>

      {/* 三个数字参数 */}
      <div className="mb-4 overflow-hidden rounded border">
        <div className="border-b bg-muted/30 px-3 py-1.5 font-medium">卡死/顾问参数</div>
        {RANGES.map((r) => {
          const v = values[r.key]
          const bad = !Number.isInteger(v) || v < r.lo || v > r.hi
          return (
            <div key={r.key} className="flex items-center gap-3 border-b px-3 py-2 last:border-b-0">
              <span className="w-24 shrink-0 font-medium">{r.label}</span>
              <Input
                type="number"
                className={cn("h-7 w-20 text-xs", bad && "border-(--status-error)")}
                value={v}
                min={r.lo}
                max={r.hi}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  setValues((prev) => ({ ...prev, [r.key]: n }))
                }}
              />
              <span className="shrink-0 text-[10px] text-muted-foreground">
                允许 {r.lo}-{r.hi}
              </span>
              <span className="min-w-0 flex-1 truncate text-muted-foreground" title={r.hint}>
                {r.hint}
              </span>
            </div>
          )
        })}
      </div>

      {/* 顾问/复盘模型覆写 */}
      <div className="mb-4 overflow-hidden rounded border">
        <div className="border-b bg-muted/30 px-3 py-1.5 font-medium">顾问/复盘模型（项目级覆写）</div>
        <div className="flex flex-wrap items-center gap-3 px-3 py-2.5">
          <span className="text-muted-foreground">供应商</span>
          <select
            className={selectCls}
            value={provider}
            onChange={(e) => { setProvider(e.target.value); setModel("") }}
          >
            <option value="">跟随全局 planner</option>
            {(models?.providers ?? [])
              .filter((p) => p.enabled)
              .map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          </select>
          <span className="text-muted-foreground">模型</span>
          <select
            className={cn(selectCls, !provider && "opacity-50")}
            value={model}
            disabled={!provider}
            onChange={(e) => setModel(e.target.value)}
          >
            <option value="">供应商默认模型</option>
            {providerModels.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
        <div className="border-t px-3 py-2 text-[10px] leading-5 text-muted-foreground">
          · 覆写同时作用于<b>顾问建议、顾问裁决、任务收尾复盘</b>三处；Orchestrator 编排器自身不受影响。<br />
          · 供应商后被删除/停用导致构建失败时，自动静默回退全局 planner，不开窗失败。
        </div>
      </div>

      {/* 保存行 */}
      <div className="flex items-center gap-3">
        <Button size="sm" className="h-7 text-xs" disabled={invalid} onClick={save}>
          保存
        </Button>
        {isDefault && <span className="text-[10px] text-muted-foreground">当前为全部缺省值（保存将剥键）</span>}
        <span className={cn("text-[10px] transition-opacity", saved ? "text-primary opacity-100" : "opacity-0")}>
          已保存 ✓
        </span>
        {err && <span className="text-[10px] text-(--status-error)">{err}</span>}
      </div>

      <div className="mt-4 rounded border border-(--status-approval)/40 bg-(--status-approval)/10 p-2.5 text-[10px] leading-5 text-(--status-approval)">
        第 3 轮机械硬闸（顾问裁决「继续」后再干满一个观察窗仍无进展 → 机械终止）是最终安全网，不可关闭、不可调。
      </div>
    </div>
  )
}
