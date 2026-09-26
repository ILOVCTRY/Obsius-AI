import { useState } from "react"
import { Repeat } from "lucide-react"
import { api, ApiError } from "@/lib/api"
import { cn } from "@/lib/utils"

// 会话中心化 §4.4：会话顶栏「🔄 换智能体」——选专家 → 会话级换人
// （热换装、下个步边界生效），对话历史与黑板全保留、跨委托持续。

export function RoleSwitchButton({
  sid, roles, currentRole, onChanged,
}: {
  sid: string
  roles: { role: string; name: string }[]
  currentRole: string
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const pick = async (role: string) => {
    if (busy) return
    if (role === currentRole) { setOpen(false); return }
    setBusy(role); setErr(null)
    try {
      await api.switchSessionRole(sid, role)
      setOpen(false)
      onChanged()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally { setBusy(null) }
  }

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        title="换智能体：保留对话历史与黑板，下个步边界切换身份"
        onClick={() => { setOpen((v) => !v); setErr(null) }}
        className="flex items-center gap-0.5 rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground"
      >
        <Repeat className="size-3" />
        换智能体
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full right-0 z-40 mb-1 w-56 rounded-lg border bg-popover p-1 shadow-xl">
            <div className="px-1.5 py-1 text-[10px] text-muted-foreground">
              选择专家接手此会话（历史全保留）
            </div>
            <div className="max-h-64 overflow-auto">
              {roles.map((r) => (
                <button
                  key={r.role}
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void pick(r.role)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-accent disabled:opacity-50",
                    r.role === currentRole && "text-primary")}
                >
                  <span className="min-w-0 flex-1 truncate">{r.name}</span>
                  {busy === r.role ? "切换中…"
                    : r.role === currentRole ? "✓"
                    : <span className="font-mono text-[9px] text-muted-foreground">{r.role}</span>}
                </button>
              ))}
            </div>
            {err && <p className="px-1.5 py-1 text-[10px] text-(--status-error)">{err}</p>}
          </div>
        </>
      )}
    </div>
  )
}
