import { useEffect, useState } from "react"
import { Eye, EyeOff, ExternalLink, RefreshCw } from "lucide-react"
import { api } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import type { LlmProvider, LlmProviderFormat } from "@/lib/types"
import { FORMAT_LABEL, PROVIDER_PRESETS, type ProviderPreset } from "@/lib/providerPresets"
import { cn } from "@/lib/utils"

// 供应商弹窗（2026-10-04，对齐 cc-haha 的「添加/编辑模型」）：新增与编辑共用一套表单。
// - 新增（initial=null）：顶部预设 chips 一点即填名称/根地址/兼容格式，模型留空稍后拉。
// - 编辑（initial 非空）：预填全部字段，密钥留空=沿用已存密钥（后端口径）。
// - 卡内可直接「获取模型」（上游 /v1/models，失败降级探活候选）与「测试连接」。
//
// 根地址口径：填**根**，后端自动追加 `/v1/{chat/completions|responses|messages}`
// （core/llm/openai_compat.py、anthropic_compat.py）。

type DiscState = { ids: string[]; checked: string[]; listed: boolean; msg?: string }

const SELECT_CLS =
  "h-9 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground outline-none focus:border-ring"

export function ProviderDialog({ open, onOpenChange, initial, onSave }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  /** null=新增（显示预设）；非 null=编辑该供应商 */
  initial: LlmProvider | null
  onSave: (p: LlmProvider) => void
}) {
  const isEdit = initial !== null
  const [presetId, setPresetId] = useState(PROVIDER_PRESETS[0].id)
  const [name, setName] = useState("")
  const [baseUrl, setBaseUrl] = useState("")
  const [format, setFormat] = useState<LlmProviderFormat>("openai-chat-completions")
  const [apiKey, setApiKey] = useState("")
  const [proxy, setProxy] = useState("")
  const [thinking, setThinking] = useState(false)
  const [models, setModels] = useState<string[]>([])
  const [ctx, setCtx] = useState<Record<string, number>>({})
  const [hasKey, setHasKey] = useState(false)
  const [newModel, setNewModel] = useState("")
  const [showKey, setShowKey] = useState(false)
  const [busy, setBusy] = useState(false)
  const [disc, setDisc] = useState<DiscState | null>(null)
  const [testRes, setTestRes] = useState<Record<string, string>>({})
  const [connTest, setConnTest] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const preset = PROVIDER_PRESETS.find((p) => p.id === presetId) ?? PROVIDER_PRESETS[0]

  const pick = (p: ProviderPreset) => {
    setPresetId(p.id)
    setName(p.id === "custom" ? "" : p.name)
    setBaseUrl(p.baseUrl)
    setFormat(p.format)
    setErr(null)
  }

  useEffect(() => {
    if (!open) return
    if (initial) {
      setPresetId("custom")
      setName(initial.name); setBaseUrl(initial.base_url); setFormat(initial.format)
      setApiKey(""); setProxy(initial.proxy ?? "")
      setThinking(initial.thinking === true)
      setModels(initial.models); setCtx(initial.model_context ?? {})
      setHasKey(!!initial.has_key)
    } else {
      const p = PROVIDER_PRESETS[0]
      setPresetId(p.id); setName(""); setBaseUrl(p.baseUrl); setFormat(p.format)
      setApiKey(""); setProxy(""); setThinking(false); setModels([]); setCtx({}); setHasKey(false)
    }
    setNewModel(""); setShowKey(false); setBusy(false)
    setDisc(null); setTestRes({}); setConnTest(null); setErr(null)
  }, [open, initial])

  // 已存密钥且密钥框为空 → 用 name 让服务端取存的 key；否则用地址+key
  const creds = () => (isEdit && hasKey && !apiKey.trim())
    ? { name: name.trim() }
    : { base_url: baseUrl.trim(), api_key: apiKey.trim() }

  const fetchModels = async () => {
    setBusy(true); setErr(null)
    try {
      const r = await api.discoverLlm({ ...creds(), format, proxy: proxy.trim() || null })
      const ids = r.models.map((m) => m.id)
      setDisc({
        ids, checked: ids.filter((id) => models.includes(id)), listed: r.listed,
        msg: r.listed ? undefined
          : `该端点不支持模型列表，已探活候选 ${ids.length} 个可用`,
      })
    } catch (e) {
      setDisc({ ids: [], checked: [], listed: false, msg: String(e) })
    } finally { setBusy(false) }
  }

  const applyDisc = () => {
    if (disc) setModels(disc.ids.filter((id) => disc.checked.includes(id)))
    setDisc(null)
  }

  const testModel = async (m: string) => {
    setTestRes((t) => ({ ...t, [m]: "…" }))
    try {
      const r = await api.testLlmModel({ ...creds(), format, model: m, proxy: proxy.trim() || null })
      setTestRes((t) => ({ ...t, [m]: r.ok ? "✓ 可用" : `✗ ${r.error ?? "不可用"}` }))
    } catch (e) {
      setTestRes((t) => ({ ...t, [m]: `✗ ${String(e)}` }))
    }
  }

  const testConnection = async () => {
    const m = models[0]
    if (!m) { setConnTest("先添加模型"); return }
    setConnTest("测试中…")
    try {
      const r = await api.testLlmModel({ ...creds(), format, model: m, proxy: proxy.trim() || null })
      setConnTest(r.ok ? `✓ 可用（${m}）` : `✗ ${r.error ?? "不可用"}`)
    } catch (e) { setConnTest(`✗ ${String(e)}`) }
  }

  const addModel = () => {
    const m = newModel.trim()
    if (m && !models.includes(m)) setModels((ms) => [...ms, m])
    setNewModel("")
  }
  const removeModel = (m: string) => {
    setModels((ms) => ms.filter((x) => x !== m))
    setCtx((c) => { const n = { ...c }; delete n[m]; return n })
  }
  const moveModel = (mi: number, delta: number) =>
    setModels((ms) => {
      const j = mi + delta
      if (j < 0 || j >= ms.length) return ms
      const next = [...ms];[next[mi], next[j]] = [next[j], next[mi]]
      return next
    })

  const save = () => {
    const n = name.trim()
    if (!n) { setErr("请填配置名称"); return }
    if (!/^https?:\/\//i.test(baseUrl.trim())) { setErr("接口地址须为 http(s) 地址"); return }
    if (!isEdit && preset.needsKey && !apiKey.trim()) { setErr(`${preset.name} 需要 API 密钥`); return }
    onSave({
      name: n,
      base_url: baseUrl.trim().replace(/\/+$/, ""),
      format,
      api_key: apiKey.trim(),
      models,
      enabled: initial?.enabled ?? true,
      model_context: Object.keys(ctx).length ? ctx : undefined,
      thinking: thinking || null,
      proxy: proxy.trim() || null,
    })
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogTitle>{isEdit ? "编辑供应商" : "添加供应商"}</DialogTitle>
        <DialogDescription>
          {isEdit
            ? "改完点保存；密钥留空表示沿用已存密钥。"
            : "选一个预设自动填好地址与兼容格式；模型可留空，建好后点「获取模型」拉取。"}
        </DialogDescription>

        <div className="min-h-0 flex-1 overflow-y-auto pr-0.5">
          {!isEdit && (
            <div className="mb-3">
              <div className="mb-2 text-[11px] text-muted-foreground">预设</div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {PROVIDER_PRESETS.map((p) => (
                  <button key={p.id} type="button" onClick={() => pick(p)} title={p.note}
                    className={cn(
                      "truncate rounded-md border px-2.5 py-1.5 text-[11.5px] transition-colors",
                      p.id === presetId
                        ? "border-primary bg-primary/10 text-foreground"
                        : "border-border text-muted-foreground hover:border-primary/50 hover:text-foreground",
                    )}>
                    {p.name}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="llm-fields">
            <label><span>配置名称 <em className="not-italic text-(--status-error)">*</em></span>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如 DeepSeek" /></label>
            <label><span>接口地址 <em className="not-italic text-(--status-error)">*</em>
              <i className="ml-1 not-italic text-muted-foreground/70">填根地址，自动追加 /v1/…</i></span>
              <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                     className="font-mono" placeholder="https://api.example.com" /></label>
            <label><span>兼容格式</span>
              <select value={format} className={SELECT_CLS}
                      onChange={(e) => setFormat(e.target.value as LlmProviderFormat)}>
                {(Object.keys(FORMAT_LABEL) as LlmProviderFormat[]).map((f) => (
                  <option key={f} value={f}>{FORMAT_LABEL[f]}</option>
                ))}
              </select></label>
            <label><span>API 密钥 {!isEdit && preset.needsKey && <em className="not-italic text-(--status-error)">*</em>}</span>
              <div className="relative">
                <Input type={showKey ? "text" : "password"} value={apiKey}
                       onChange={(e) => setApiKey(e.target.value)} className="pr-8 font-mono"
                       placeholder={isEdit && hasKey ? "已配置 · 留空表示保持不变" : "sk-…"} />
                <button type="button" title={showKey ? "隐藏" : "显示"}
                        onClick={() => setShowKey((v) => !v)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
                  {showKey ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
                </button>
              </div></label>
            <label><span>供应商代理</span>
              <Input value={proxy} onChange={(e) => setProxy(e.target.value)} className="font-mono"
                     placeholder="留空跟随系统代理，例如 http://127.0.0.1:7890" /></label>
            <label className="llm-thinking" title="勾选=按兼容格式发送 reasoning/thinking（不支持时自动降级）；不勾=普通请求">
              <input type="checkbox" checked={thinking} onChange={(e) => setThinking(e.target.checked)} />
              <span>思考链</span></label>
          </div>

          {!isEdit && preset.keyUrl && (
            <a href={preset.keyUrl} target="_blank" rel="noreferrer"
               className="mt-2 inline-flex items-center gap-1 text-[11px] text-primary hover:underline">
              <ExternalLink className="size-3" /> 获取 {preset.name} 的 API Key
            </a>
          )}

          <div className="llm-section">
            <div className="llm-section-title">
              <span>模型清单</span><small>第一个模型作为供应商默认</small>
              <div className="llm-model-actions">
                <Button size="sm" variant="outline" disabled={busy || !baseUrl.trim()} onClick={fetchModels}>
                  <RefreshCw className={cn("size-3", busy && "animate-spin")} /> {busy ? "获取中…" : "获取模型"}
                </Button>
                <Button size="sm" variant="outline" onClick={testConnection}>测试连接</Button>
              </div>
            </div>
            {connTest && <div className={cn("llm-test-line",
              connTest.startsWith("✓") && "is-ok", connTest.startsWith("✗") && "is-fail")}>{connTest}</div>}
            <div className="llm-model-list">
              {models.map((m, mi) => (
                <div className={cn("llm-model-row", mi === 0 && "is-primary")} key={m}>
                  <div className="llm-model-main">
                    <span className="llm-model-dot" /><span className="font-mono">{m}</span>
                    {mi === 0 && <Badge>默认模型</Badge>}
                  </div>
                  <label className="llm-context"><span>上下文</span>
                    <Input type="number" min={0.1} step={1} placeholder="默认"
                           value={ctx[m] ? String(ctx[m] / 1000) : ""}
                           onChange={(e) => {
                             const raw = e.target.value
                             setCtx((c) => {
                               const n = { ...c }
                               if (raw === "") delete n[m]
                               else n[m] = Math.round(Number(raw) * 1000)
                               return n
                             })
                           }} /><em>K</em></label>
                  <span className={cn("llm-test-state",
                    testRes[m]?.startsWith("✓") && "is-ok", testRes[m]?.startsWith("✗") && "is-fail")}>
                    {testRes[m] ?? "未测试"}</span>
                  <Button size="sm" variant="ghost" onClick={() => testModel(m)}>测试</Button>
                  <div className="llm-row-arrows">
                    <button onClick={() => moveModel(mi, -1)} aria-label="上移">↑</button>
                    <button onClick={() => moveModel(mi, 1)} aria-label="下移">↓</button>
                    <button className="is-danger" onClick={() => removeModel(m)} aria-label="移除">×</button>
                  </div>
                </div>
              ))}
            </div>
            <div className="llm-add-model">
              <Input value={newModel} onChange={(e) => setNewModel(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && addModel()}
                     placeholder="输入模型 ID，例如 gpt-4o-mini" />
              <Button size="sm" onClick={addModel}>添加</Button>
            </div>
            {disc && (
              <div className="llm-discovery">
                <div className="llm-discovery-title">
                  <span>模型发现</span><small>{disc.ids.length} 个候选模型</small>
                </div>
                {disc.msg && <p>{disc.msg}</p>}
                <div className="llm-discovery-list">
                  {disc.ids.map((id) => (
                    <label key={id}>
                      <input type="checkbox" checked={disc.checked.includes(id)}
                             onChange={(e) => setDisc((d) => d && {
                               ...d,
                               checked: e.target.checked
                                 ? [...d.checked, id] : d.checked.filter((x) => x !== id),
                             })} />
                      {id}
                    </label>
                  ))}
                </div>
                <div>
                  <Button size="sm" onClick={applyDisc}>应用已选模型</Button>
                  <Button size="sm" variant="ghost" onClick={() => setDisc(null)}>取消</Button>
                </div>
              </div>
            )}
          </div>
        </div>

        {err && <p className="text-[11px] text-(--status-error)">{err}</p>}

        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" onClick={save}>{isEdit ? "保存" : "添加"}</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
