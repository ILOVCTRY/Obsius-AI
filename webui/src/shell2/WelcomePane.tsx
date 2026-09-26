import { useEffect, useState } from "react"
import { api, ApiError } from "@/lib/api"
import type { TrackProfile } from "@/lib/types"
import { cn } from "@/lib/utils"

// M5 欢迎态（拍板：名称输入+模板卡）：名称 + 场景轨 + 该轨场景档卡片。
// 选档=建项时物化组队/五件套（后端 expert-pool M4a 已支持，前端零后端改动）；
// 直接回车=不选档，按轨全池直通。

const TRACK_GLYPH: Record<string, string> = {
  ctf: "🚩", pentest: "🎯", redteam: "⚔", research: "🧬",
}

export function WelcomePane({ onCreated }: { onCreated: (pid: string) => void }) {
  const [name, setName] = useState("")
  const [track, setTrack] = useState("pentest")
  const [tracks, setTracks] = useState<{ name: string; label: string }[]>([])
  const [profiles, setProfiles] = useState<TrackProfile[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.taxonomy()
      .then((tx) => setTracks(tx.tracks.map((t) => ({ name: t.name, label: t.label }))))
      .catch(() => {})
  }, [])

  useEffect(() => {
    let alive = true
    api.trackProfiles(track)
      .then((ps) => { if (alive) setProfiles(ps) })
      .catch(() => { if (alive) setProfiles([]) })
    return () => { alive = false }
  }, [track])

  const create = async (profile?: string) => {
    if (!name.trim() || busy) return
    setBusy(true)
    setErr(null)
    try {
      const meta = await api.createProject(
        name.trim(), track, [], profile ? { profile } : undefined)
      onCreated(meta.id)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center overflow-auto p-6">
      <div className="w-full max-w-2xl">
        <div className="mb-2 text-center text-3xl">🛡</div>
        <h1 className="mb-1 text-center text-xl font-semibold">Work with CyberStrike</h1>
        <p className="mb-5 text-center text-sm text-muted-foreground">
          给项目起个名，选场景轨与场景档——创建后直接与编排器对话定下目标。
        </p>

        <div className="mb-3 flex items-center gap-2 rounded-lg border bg-background px-3 py-2">
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
            placeholder="项目名称 / 目标"
            className="h-7 flex-1 bg-transparent text-sm focus:outline-none"
          />
          <button
            onClick={() => create()}
            disabled={!name.trim() || busy}
            title="不选场景档：按轨全池直通"
            className="flex h-7 w-7 items-center justify-center rounded-full bg-primary text-primary-foreground disabled:opacity-40"
          >
            ↑
          </button>
        </div>

        <div className="mb-4 flex flex-wrap justify-center gap-1">
          {tracks.map((t) => (
            <button
              key={t.name}
              onClick={() => setTrack(t.name)}
              className={cn(
                "rounded-full border px-3 py-0.5 text-xs",
                track === t.name ? "bg-secondary text-primary" : "text-muted-foreground",
              )}
            >
              {TRACK_GLYPH[t.name]} {t.label}
            </button>
          ))}
        </div>

        <div className="grid gap-2 sm:grid-cols-2">
          {profiles.map((p) => (
            <button
              key={p.id}
              onClick={() => create(p.id)}
              disabled={!name.trim() || busy}
              className="rounded-lg border p-3 text-left transition-colors hover:border-primary/50 disabled:opacity-40"
            >
              <div className="mb-1 flex items-center gap-1 text-sm font-medium">
                <span>{TRACK_GLYPH[track]}</span>{p.name || p.id}
              </div>
              <p className="mb-2 line-clamp-2 text-[11px] text-muted-foreground">
                {p.description}
              </p>
              {p.playbook && (
                <p className="line-clamp-2 text-[10px] text-muted-foreground/80">
                  📖 {p.playbook}
                </p>
              )}
              <div className="mt-1.5 flex flex-wrap gap-1">
                {(p.experts ?? []).map((e) => (
                  <span key={e} className="rounded bg-secondary px-1 text-[9px]">{e}</span>
                ))}
              </div>
            </button>
          ))}
        </div>

        {err && <p className="mt-3 text-center text-xs text-red-400">{err}</p>}
        {!name.trim() && (
          <p className="mt-3 text-center text-[10px] text-muted-foreground">
            先输入项目名称，再选场景档（或直接回车全池直通）
          </p>
        )}
      </div>
    </div>
  )
}
