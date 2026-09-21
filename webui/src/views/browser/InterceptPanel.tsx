import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import { api, ApiError } from "@/lib/api"
import type { AssetMissingDetail, InterceptPending, InterceptState } from "@/lib/types"

// F6-v3 拦截面板：仅人工浏览流量可挂起裁决——两个独立开关（请求/响应）
// + 挂起包列表 2s 轮询 + 行内弹窗看/改原始报文（解析在后端）。
// 超时（默认 120s）/关开关/注册满 → 后端自动放行原文，行自然消失。
export function InterceptPanel({ pid }: { pid: string }) {
  const [state, setState] = useState<InterceptState | null>(null)
  const [selected, setSelected] = useState<InterceptPending | null>(null)
  const [rawDraft, setRawDraft] = useState("")
  const [err, setErr] = useState<string | null>(null)
  const [missing, setMissing] = useState<AssetMissingDetail | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    api.browserIntercept(pid).then((s) => {
      setState(s)
      // 404（已裁决/超时）的行自然消失：弹窗还开着就关掉
      setSelected((cur) => (cur && !s.pending.some((p) => p.hold_id === cur.hold_id) ? null : cur))
    }).catch(() => { /* 后端重启等，下一轮再试 */ })
  }, [pid])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 2000)
    return () => clearInterval(t)
  }, [refresh])

  // 弹窗打开时读最新 raw（每次选中/刷新同步，避免陈旧）
  useEffect(() => {
    if (!selected) return
    const fresh = state?.pending.find((p) => p.hold_id === selected.hold_id)
    if (fresh) setRawDraft(fresh.raw)
  }, [selected, state])

  const toggle = async (direction: "request" | "response", enabled: boolean) => {
    try {
      setState(await api.browserInterceptToggle(pid, direction, enabled))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  const decide = async (p: InterceptPending, action: "forward" | "drop", withRaw: boolean) => {
    setBusy(true); setErr(null); setMissing(null)
    try {
      await api.browserInterceptDecide(pid, p.hold_id,
        { action, raw: withRaw ? rawDraft : undefined })
      setSelected(null)
      refresh()
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const d = e.data as AssetMissingDetail
        if (d?.asset_missing) setMissing(d)
        else setErr(typeof d === "string" ? d : d?.reason ?? String(e))
      } else setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  const registerAndRetry = async () => {
    if (!missing) return
    try {
      await api.addAsset(pid, "auto", missing.host)
      setMissing(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col text-xs">
      <div className="flex items-center gap-2 border-b px-2 py-1.5">
        <Button size="sm" className="h-6 text-[10px]"
                variant={state?.request_enabled ? "default" : "outline"}
                onClick={() => void toggle("request", !state?.request_enabled)}>
          {state?.request_enabled ? "● 拦截请求：开" : "○ 拦截请求：关"}
        </Button>
        <Button size="sm" className="h-6 text-[10px]"
                variant={state?.response_enabled ? "default" : "outline"}
                onClick={() => void toggle("response", !state?.response_enabled)}>
          {state?.response_enabled ? "● 拦截响应：开" : "○ 拦截响应：关"}
        </Button>
        <span className="ml-auto text-[10px] text-muted-foreground">
          仅人工浏览流量 · 120s 未裁决自动放行
        </span>
      </div>
      {err && (
        <div className="flex items-center gap-2 border-b px-2 py-1 text-red-400">
          <span className="flex-1">{err}</span>
          <button className="text-[10px] underline" onClick={() => setErr(null)}>关闭</button>
        </div>
      )}
      {missing && (
        <div className="flex items-center gap-2 border-b bg-(--status-paused)/10 px-2 py-1">
          <Badge variant="outline" className="font-mono">{missing.host}</Badge>
          <span className="flex-1 truncate text-muted-foreground">{missing.reason}</span>
          <Button size="sm" variant="outline" onClick={() => void registerAndRetry()}>一键登记资产</Button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        {(state?.pending.length ?? 0) === 0 ? (
          <div className="p-3 text-[11px] text-muted-foreground">
            暂无挂起包——打开开关后浏览，命中流量会停在这里等你裁决
          </div>
        ) : (
          <table className="w-full text-left font-mono text-[10px]">
            <tbody>
              {state!.pending.map((p) => (
                <tr key={p.hold_id} className="cursor-pointer hover:bg-accent"
                    onClick={() => setSelected(p)}>
                  <td className="w-16 px-1 py-0.5">
                    <Badge variant={p.direction === "request" ? "default" : "outline"}
                           className="px-1 text-[9px]">
                      {p.direction === "request" ? "请求" : "响应"}
                    </Badge>
                  </td>
                  <td className="w-12 px-1 py-0.5">{p.method}</td>
                  <td className="w-10 px-1 py-0.5 text-right">{p.status ?? "—"}</td>
                  <td className="px-1 py-0.5">
                    <span className="block truncate" title={p.url}>{p.url}</span>
                  </td>
                  <td className="w-14 px-1 py-0.5 text-right text-(--status-paused)">
                    {Math.ceil(p.expires_in_ms / 1000)}s
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Dialog open={!!selected} onOpenChange={(o) => !o && setSelected(null)}>
        <DialogContent className="max-w-2xl">
          <DialogTitle className="font-mono text-xs">
            {selected?.method} {selected?.url}{selected?.status != null ? ` → ${selected.status}` : ""}
          </DialogTitle>
          {selected && (
            <div className="space-y-2 text-xs">
              {selected.editable ? (
                <textarea
                  className="h-72 w-full resize-y rounded-md border bg-background p-1 font-mono text-[10px]"
                  value={rawDraft}
                  onChange={(e) => setRawDraft(e.target.value)}
                  spellCheck={false}
                />
              ) : (
                <pre className="h-72 overflow-auto whitespace-pre-wrap break-all rounded-md border p-1 font-mono text-[10px] text-muted-foreground">
                  {selected.raw}
                  {"\n\n（二进制 body 不可编辑——只能放行原文或丢弃）"}
                </pre>
              )}
              <div className="flex items-center gap-2">
                {selected.editable && (
                  <Button size="sm" disabled={busy} onClick={() => void decide(selected, "forward", true)}>
                    {busy ? "…" : "放行（改后）"}
                  </Button>
                )}
                <Button size="sm" variant="outline" disabled={busy}
                        onClick={() => void decide(selected, "forward", false)}>
                  放行原文
                </Button>
                <Button size="sm" variant="outline" disabled={busy}
                        onClick={() => void decide(selected, "drop", false)}>
                  丢弃
                </Button>
                <span className="ml-auto text-[10px] text-muted-foreground">
                  {Math.ceil(selected.expires_in_ms / 1000)}s 后自动放行
                </span>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
