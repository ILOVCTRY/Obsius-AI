import { useEffect, useState } from "react"
import { api, ApiError } from "@/lib/api"
import type { TrackProfile } from "@/lib/types"
import { cn } from "@/lib/utils"
import { CtfWizard, PentestWizard, SampleWizard } from "./TemplateWizards"

// M5 模板中心：三类录入向导 + 四轨 8 场景档卡（卡内名称输入即建项）。
// 全部前端收纳，建项物化走后端既有 profile 机制（M4a），零后端改动。

const TRACK_GLYPH: Record<string, string> = {
  ctf: "🚩", pentest: "🎯", redteam: "⚔", research: "🧬",
}

type WizardKind = "pentest" | "sample" | "ctf" | null

export function TemplatesView({ onCreated }: { onCreated: (pid: string) => void }) {
  const [tracks, setTracks] = useState<{ name: string; label: string }[]>([])
  const [profilesMap, setProfilesMap] = useState<Record<string, TrackProfile[]>>({})
  const [names, setNames] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [wizard, setWizard] = useState<WizardKind>(null)

  useEffect(() => {
    let alive = true
    api.taxonomy().then(async (tx) => {
      const ts = tx.tracks.map((t) => ({ name: t.name, label: t.label }))
      if (!alive) return
      setTracks(ts)
      const entries = await Promise.all(
        ts.map(async (t) => [t.name, await api.trackProfiles(t.name).catch(() => [])] as const))
      if (!alive) return
      setProfilesMap(Object.fromEntries(entries))
    }).catch(() => {})
    return () => { alive = false }
  }, [])

  const createFromCard = async (track: string, p: TrackProfile) => {
    const nm = names[p.id]?.trim()
    if (!nm || busyId) return
    setBusyId(p.id)
    setErr(null)
    try {
      const meta = await api.createProject(nm, track, [], { profile: p.id })
      onCreated(meta.id)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
      setBusyId(null)
    }
  }

  return (
    <div className="h-full overflow-auto bg-[#0d1117]">
      <div className="mx-auto max-w-4xl p-5">
        <h1 className="mb-1 text-lg font-semibold">🧩 场景模板中心</h1>
        <p className="mb-4 text-xs text-muted-foreground">
          选录入向导快速开场，或从场景档直接组队建项——模板在建项时物化，之后与模板不再有关联。
        </p>

        {/* 录入向导 */}
        <div className="mb-6 grid gap-2 sm:grid-cols-3">
          <WizardCard glyph="🎯" title="渗透目标录入"
                      desc="给目标 URL/IP，建项并登记资产"
                      onClick={() => setWizard("pentest")} />
          <WizardCard glyph="🧬" title="样本 triage"
                      desc="上传样本，建项并自动跑 headless triage"
                      onClick={() => setWizard("sample")} />
          <WizardCard glyph="🚩" title="CTF 向导"
                      desc="给比赛名与题面，建项并告知编排器"
                      onClick={() => setWizard("ctf")} />
        </div>

        {/* 各轨场景档 */}
        {tracks.map((t) => {
          const ps = profilesMap[t.name] ?? []
          if (ps.length === 0) return null
          return (
            <section key={t.name} className="mb-6">
              <h2 className="mb-2 text-sm font-medium">
                {TRACK_GLYPH[t.name]} {t.label}
              </h2>
              <div className="grid gap-2 sm:grid-cols-2">
                {ps.map((p) => (
                  <div key={p.id} className="rounded-lg border p-3">
                    <div className="mb-1 text-sm font-medium">{p.name || p.id}</div>
                    <p className="mb-2 line-clamp-2 text-[11px] text-muted-foreground">
                      {p.description}
                    </p>
                    {p.playbook && (
                      <p className="mb-2 line-clamp-2 text-[10px] text-muted-foreground/80">
                        📖 {p.playbook}
                      </p>
                    )}
                    <div className="mb-2 flex flex-wrap gap-1">
                      {(p.artifacts ?? []).map((a) => (
                        <span key={a} className="rounded bg-secondary px-1 text-[9px]">📦 {a}</span>
                      ))}
                      {(p.experts ?? []).map((e) => (
                        <span key={e} className="rounded bg-secondary px-1 text-[9px]">{e}</span>
                      ))}
                    </div>
                    <div className="flex items-center gap-1">
                      <input
                        value={names[p.id] ?? ""}
                        onChange={(e) => setNames((m) => ({ ...m, [p.id]: e.target.value }))}
                        onKeyDown={(e) => e.key === "Enter" && createFromCard(t.name, p)}
                        placeholder="项目名称…"
                        className="h-7 flex-1 rounded border bg-transparent px-2 text-[11px] focus:outline-none"
                      />
                      <button
                        onClick={() => createFromCard(t.name, p)}
                        disabled={!names[p.id]?.trim() || busyId !== null}
                        className={cn(
                          "h-7 rounded bg-primary px-2 text-[11px] text-primary-foreground disabled:opacity-40")}
                      >
                        {busyId === p.id ? "…" : "创建"}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )
        })}

        {err && <p className="text-xs text-red-400">{err}</p>}
      </div>

      {wizard === "pentest" && (
        <PentestWizard onClose={() => setWizard(null)} onCreated={onCreated} />
      )}
      {wizard === "sample" && (
        <SampleWizard onClose={() => setWizard(null)} onCreated={onCreated} />
      )}
      {wizard === "ctf" && (
        <CtfWizard onClose={() => setWizard(null)} onCreated={onCreated} />
      )}
    </div>
  )
}

function WizardCard({ glyph, title, desc, onClick }: {
  glyph: string
  title: string
  desc: string
  onClick: () => void
}) {
  return (
    <button onClick={onClick}
            className="rounded-lg border p-3 text-left transition-colors hover:border-primary/50">
      <div className="mb-1 flex items-center gap-1 text-sm font-medium">
        <span>{glyph}</span>{title}
      </div>
      <p className="text-[11px] text-muted-foreground">{desc}</p>
    </button>
  )
}
