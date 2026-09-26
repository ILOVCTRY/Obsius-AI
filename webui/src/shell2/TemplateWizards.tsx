import { useEffect, useState, type ReactNode } from "react"
import { api, ApiError } from "@/lib/api"
import type { TrackProfile } from "@/lib/types"

// M5 三类录入向导（拍板：场景档卡+向导）。全部走现有端点、后端零改动：
// 渗透目标录入=建项+逐行登记资产；样本 triage=建项+传样本；CTF 向导=建项+题面发编排器。

function WizardShell({ title, onClose, onSubmit, submitLabel, canSubmit, busy, children }: {
  title: string
  onClose: () => void
  onSubmit: () => void
  submitLabel: string
  canSubmit: boolean
  busy: boolean
  children: ReactNode
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
         onClick={busy ? undefined : onClose}>
      <div className="w-96 max-w-[92vw] rounded-lg border bg-background p-4 shadow-lg"
           onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-3 text-sm font-semibold">{title}</h2>
        {children}
        <div className="mt-4 flex justify-end gap-2">
          <button onClick={onClose} disabled={busy}
                  className="rounded-md px-3 py-1 text-xs hover:bg-accent disabled:opacity-40">
            取消
          </button>
          <button onClick={onSubmit} disabled={!canSubmit || busy}
                  className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground disabled:opacity-40">
            {busy ? "处理中…" : submitLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

const labelCls = "mb-1 block text-[10px] text-muted-foreground"
const inputCls = "w-full rounded-md border bg-transparent px-2 py-1.5 text-sm focus:outline-none"

function useProfiles(track: string) {
  const [profiles, setProfiles] = useState<TrackProfile[]>([])
  useEffect(() => {
    let alive = true
    api.trackProfiles(track)
      .then((ps) => { if (alive) setProfiles(ps) })
      .catch(() => {})
    return () => { alive = false }
  }, [track])
  return profiles
}

// ---------- 渗透目标录入 ----------

export function PentestWizard({ onClose, onCreated }: {
  onClose: () => void
  onCreated: (pid: string) => void
}) {
  const profiles = useProfiles("pentest")
  const [name, setName] = useState("")
  const [targets, setTargets] = useState("")
  const [profile, setProfile] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!profile && profiles.some((p) => p.id === "recon-first")) setProfile("recon-first")
  }, [profiles, profile])

  const lines = targets.split("\n").map((s) => s.trim()).filter(Boolean)

  const submit = async () => {
    if (busy || !name.trim() || lines.length === 0) return
    setBusy(true)
    setErr(null)
    try {
      const meta = await api.createProject(
        name.trim(), "pentest", [], profile ? { profile } : undefined)
      const failed: string[] = []
      for (const v of lines) {
        try { await api.addAsset(meta.id, "auto", v) }
        catch { failed.push(v) }
      }
      if (failed.length) setErr(`项目已创建，但 ${failed.length} 个目标登记失败：${failed.join("、")}`)
      onCreated(meta.id)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <WizardShell title="🎯 渗透目标录入" onClose={onClose} onSubmit={submit}
                 submitLabel="创建并登记目标" busy={busy}
                 canSubmit={!!name.trim() && lines.length > 0}>
      <label className={labelCls}>项目名称</label>
      <input value={name} onChange={(e) => setName(e.target.value)}
             placeholder="如：某 SRC 目标 / 授权评估" className={`${inputCls} mb-3`} />
      <label className={labelCls}>目标（每行一个 URL / IP / 域名）</label>
      <textarea value={targets} onChange={(e) => setTargets(e.target.value)} rows={4}
                placeholder={"https://example.com\n10.10.10.10"}
                className={`${inputCls} mb-3 resize-none`} />
      <label className={labelCls}>场景档</label>
      <select value={profile} onChange={(e) => setProfile(e.target.value)}
              className={`${inputCls} [&>option]:bg-popover`}>
        {profiles.map((p) => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}
      </select>
      {err && <p className="mt-2 text-xs text-red-400">{err}</p>}
    </WizardShell>
  )
}

// ---------- 样本 triage ----------

export function SampleWizard({ onClose, onCreated }: {
  onClose: () => void
  onCreated: (pid: string) => void
}) {
  const [name, setName] = useState("")
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    if (busy || !name.trim() || !file) return
    setBusy(true)
    setErr(null)
    try {
      const meta = await api.createProject(
        name.trim(), "research", [], { profile: "rev-workbench" })
      await api.uploadSample(meta.id, file)
      onCreated(meta.id)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <WizardShell title="🧬 样本 triage" onClose={onClose} onSubmit={submit}
                 submitLabel="创建并上传样本" busy={busy}
                 canSubmit={!!name.trim() && !!file}>
      <label className={labelCls}>项目名称</label>
      <input value={name} onChange={(e) => setName(e.target.value)}
             placeholder="如：某样本分析" className={`${inputCls} mb-3`} />
      <label className={labelCls}>样本文件（自动跑 headless triage）</label>
      <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)}
             className="text-xs text-muted-foreground" />
      <p className="mt-2 text-[10px] text-muted-foreground">
        默认逆向工作台队（reverse-analyst + rebuilder），工作台随后可看 AI triage 结果。
      </p>
      {err && <p className="mt-2 text-xs text-red-400">{err}</p>}
    </WizardShell>
  )
}

// ---------- CTF 向导 ----------

export function CtfWizard({ onClose, onCreated }: {
  onClose: () => void
  onCreated: (pid: string) => void
}) {
  const profiles = useProfiles("ctf")
  const [name, setName] = useState("")
  const [brief, setBrief] = useState("")
  const [profile, setProfile] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!profile && profiles.some((p) => p.id === "solo-generalist")) {
      setProfile("solo-generalist")
    }
  }, [profiles, profile])

  const submit = async () => {
    if (busy || !name.trim()) return
    setBusy(true)
    setErr(null)
    try {
      const meta = await api.createProject(
        name.trim(), "ctf", [], profile ? { profile } : undefined)
      if (brief.trim()) {
        // 题面作为首条消息发给编排器（失败不阻断进项目）
        api.orchChat(meta.id, brief.trim()).catch(() => {})
      }
      onCreated(meta.id)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <WizardShell title="🚩 CTF 向导" onClose={onClose} onSubmit={submit}
                 submitLabel="创建作战" busy={busy} canSubmit={!!name.trim()}>
      <label className={labelCls}>比赛 / 项目名称</label>
      <input value={name} onChange={(e) => setName(e.target.value)}
             placeholder="如：某杯 2026" className={`${inputCls} mb-3`} />
      <label className={labelCls}>题面（可选，创建后自动发给编排器）</label>
      <textarea value={brief} onChange={(e) => setBrief(e.target.value)} rows={4}
                placeholder="粘贴题目描述/附件链接……"
                className={`${inputCls} mb-3 resize-none`} />
      <label className={labelCls}>场景档</label>
      <select value={profile} onChange={(e) => setProfile(e.target.value)}
              className={`${inputCls} [&>option]:bg-popover`}>
        {profiles.map((p) => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}
      </select>
      {err && <p className="mt-2 text-xs text-red-400">{err}</p>}
    </WizardShell>
  )
}
