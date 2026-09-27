import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import type { OrchPersona, PhaseGoal } from "@/lib/types"

// 编排器弹层组件（2026-09-25 从 LiveRoom 抽出，webui-trae-shell M2）：
// LiveRoom 专用（原为与已移除的 shell2/ConversationPane 共用件，2026-09-26 新壳移除后归一）。

/** 阶段目标弹层：确认口径注入编排 tick 与对话轮；变更落 goal.confirm/clear 事件 */
export function GoalEditor({ initial, onClose, onSave, onClear }: {
  initial: PhaseGoal | null
  onClose: () => void
  onSave: (body: { text: string; criteria?: string[]; phase?: string | null }) => Promise<void>
  onClear: () => Promise<void>
}) {
  const [text, setText] = useState(initial?.text ?? "")
  const [criteria, setCriteria] = useState((initial?.criteria ?? []).join("\n"))
  const [phase, setPhase] = useState(initial?.phase ?? "")
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [templates, setTemplates] = useState<{ builtin: Record<string, string>; user: Record<string, string> }>({ builtin: {}, user: {} })

  useEffect(() => {
    api.judgmentTemplates().then(setTemplates).catch(() => {})
  }, [])

  const save = async () => {
    setSaving(true); setErr(null)
    try {
      await onSave({
        text: text.trim(),
        criteria: criteria.split("\n").map((s) => s.trim()).filter(Boolean),
        phase: phase.trim() || null,
      })
      onClose()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="w-96 rounded-lg border bg-popover p-4 text-xs shadow-lg">
      <p className="mb-1">🎯 阶段目标（人类确认口径）</p>
      <p className="mb-2 text-[10px] leading-relaxed text-muted-foreground">
        确认后注入编排 tick 与对话轮系统提示——编排器朝它推进，判据达成或资产穷尽才收工。
        变更历史在事件流（goal.confirm / goal.clear）。
      </p>
      <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)}
        placeholder="一句话目标（如：本周打穿靶场 3 台主机）"
        className="mb-2 w-full resize-y rounded-md border bg-background px-2 py-1 leading-relaxed" />
      <div className="mb-1 flex items-center justify-between gap-2">
        <label className="text-muted-foreground">验收判据（一行一条；判据第一优先源，自动派生朝它推进）</label>
        <select
          className="h-6 max-w-36 shrink-0 rounded border bg-background px-1 text-[10px]"
          value=""
          onChange={(e) => {
            const name = e.target.value
            if (!name) return
            setCriteria(templates.user[name] ?? templates.builtin[name] ?? "")
          }}>
          <option value="">应用模板…</option>
          {Object.keys(templates.builtin).length > 0 && (
            <optgroup label="内置">
              {Object.keys(templates.builtin).map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </optgroup>
          )}
          {Object.keys(templates.user).length > 0 && (
            <optgroup label="我的模板">
              {Object.keys(templates.user).map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </optgroup>
          )}
        </select>
      </div>
      <textarea rows={5} value={criteria} onChange={(e) => setCriteria(e.target.value)}
        placeholder={"拿到 3 台主机的 flag\n输出复现报告"}
        className="mb-2 w-full resize-y rounded-md border bg-background px-2 py-1 leading-relaxed" />
      <label className="mb-1 block text-muted-foreground">阶段标记（可空，如 initial-access）</label>
      <input value={phase} onChange={(e) => setPhase(e.target.value)}
        className="mb-2 h-7 w-full rounded-md border bg-background px-2" />
      {err && <p className="mb-1 text-(--status-error)">{err}</p>}
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" onClick={onClose}>取消</Button>
        <Button size="sm" variant="outline" disabled={saving || !initial}
          title={initial ? "清空目标（goal.clear 留痕，编排器回到无目标态）" : "当前无目标"}
          onClick={async () => {
            setSaving(true); setErr(null)
            try { await onClear(); onClose() } catch (e) { setErr(String(e)) } finally { setSaving(false) }
          }}>
          清空
        </Button>
        <span className="flex-1" />
        <Button size="sm" disabled={saving || !text.trim()} onClick={save}>
          {saving ? "保存中…" : "确认目标"}
        </Button>
      </div>
    </div>
  )
}

/** 编排器拟人身份弹层：display_name 贯穿页签/气泡，persona 只注入对话轮（tick 不受影响）。
 *  清空 persona 文本=剥键回退缺省。 */
export function PersonaEditor({ initial, onClose, onSave }: {
  initial: OrchPersona | null
  onClose: () => void
  onSave: (body: { display_name: string; persona: string }) => Promise<void>
}) {
  const [name, setName] = useState(initial?.display_name ?? "")
  const [persona, setPersona] = useState(initial?.persona ?? "")
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const save = async () => {
    setSaving(true); setErr(null)
    try {
      await onSave({ display_name: name.trim() || "编排器", persona: persona.trim() })
      onClose()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="w-96 rounded-lg border bg-popover p-4 text-xs shadow-lg">
      <p className="mb-1">🎭 编排器身份（拟人）</p>
      <p className="mb-2 text-[10px] leading-relaxed text-muted-foreground">
        显示名贯穿页签与对话气泡；人设只注入对话轮（「与编排对话」），编排 tick 的决策语气不受影响。
      </p>
      <label className="mb-1 block text-muted-foreground">显示名</label>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="编排器"
        className="mb-2 h-7 w-full rounded-md border bg-background px-2" />
      <label className="mb-1 block text-muted-foreground">人设（persona，可空）</label>
      <textarea rows={4} value={persona} onChange={(e) => setPersona(e.target.value)}
        placeholder="如：老编——先给结论再给依据，不打官腔，拿不准就直说"
        className="mb-2 w-full resize-y rounded-md border bg-background px-2 py-1 leading-relaxed" />
      {err && <p className="mb-1 text-(--status-error)">{err}</p>}
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" onClick={onClose}>取消</Button>
        <span className="flex-1" />
        <Button size="sm" disabled={saving} onClick={save}>{saving ? "保存中…" : "保存"}</Button>
      </div>
    </div>
  )
}
