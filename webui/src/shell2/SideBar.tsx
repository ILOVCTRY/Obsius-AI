import { useEffect, useState } from "react"
import { api, ApiError } from "@/lib/api"
import type { ProjectMeta, Session, Task } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ProjectTree } from "./ProjectTree"
import type { Posture, ResourceKey, Screen, Target } from "./types"

const POSTURES: { key: Posture; label: string }[] = [
  { key: "work", label: "工作台" },
  { key: "canvas", label: "画布" },
  { key: "tools", label: "工具" },
]

const RESOURCES: { key: ResourceKey; label: string; icon: string }[] = [
  { key: "templates", label: "模板", icon: "🧩" },
  { key: "plugins", label: "插件", icon: "🔌" },
  { key: "intel", label: "情报", icon: "📡" },
  { key: "files", label: "文件", icon: "📁" },
]

/** 轻量建项弹窗（M5 加场景档位）：名称 + 场景轨 + 可选场景档，专家留缺省 */
function CreateDialog({ onClose, onCreated }: {
  onClose: () => void
  onCreated: (pid: string) => void
}) {
  const [name, setName] = useState("")
  const [track, setTrack] = useState("pentest")
  const [tracks, setTracks] = useState<{ name: string; label: string }[]>([])
  const [profiles, setProfiles] = useState<{ id: string; name?: string | null }[]>([])
  const [profile, setProfile] = useState("") // ""=不选档，全池直通
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api.taxonomy()
      .then((tx) => setTracks(tx.tracks.map((t) => ({ name: t.name, label: t.label }))))
      .catch(() => {})
  }, [])

  useEffect(() => {
    let alive = true
    setProfile("")
    api.trackProfiles(track)
      .then((ps) => { if (alive) setProfiles(ps) })
      .catch(() => { if (alive) setProfiles([]) })
    return () => { alive = false }
  }, [track])

  const submit = async () => {
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
         onClick={onClose}>
      <div className="w-80 rounded-lg border bg-background p-4 shadow-lg"
           onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-3 text-sm font-semibold">新建作战</h2>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="项目名称 / 目标"
          className="mb-3 flex h-9 w-full rounded-md border bg-transparent px-3 text-sm focus:outline-none"
        />
        <div className="mb-1 text-[10px] text-muted-foreground">场景轨</div>
        <div className="mb-4 flex flex-wrap gap-1">
          {tracks.map((t) => (
            <button
              key={t.name}
              onClick={() => setTrack(t.name)}
              className={cn(
                "rounded-full border px-2.5 py-0.5 text-xs",
                track === t.name ? "bg-secondary text-primary" : "text-muted-foreground",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        {profiles.length > 0 && (
          <>
            <div className="mb-1 text-[10px] text-muted-foreground">
              场景档<span className="ml-1">（不选=按轨全池直通）</span>
            </div>
            <div className="mb-4 flex flex-wrap gap-1">
              {profiles.map((p) => (
                <button
                  key={p.id}
                  onClick={() => setProfile(profile === p.id ? "" : p.id)}
                  className={cn(
                    "rounded-full border px-2 py-0.5 text-[10px]",
                    profile === p.id ? "bg-secondary text-primary" : "text-muted-foreground",
                  )}
                >
                  {p.name || p.id}
                </button>
              ))}
            </div>
          </>
        )}
        {err && <p className="mb-2 text-xs text-red-400">{err}</p>}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md px-3 py-1 text-xs hover:bg-accent">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!name.trim() || busy}
            className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground disabled:opacity-40"
          >
            {busy ? "创建中…" : "创建并对话"}
          </button>
        </div>
      </div>
    </div>
  )
}

export function SideBar({
  projects, posture, screen, target, expanded, tasksMap, sessionsMap, rolesMap, pending,
  onSelectPosture, onSelectTarget, onToggleExpanded,
  onOpenResource, onOpenSettings, onOpenApprovals, onCreated,
}: {
  projects: ProjectMeta[]
  posture: Posture
  screen: Screen
  target: Target | null
  expanded: string[]
  tasksMap: Record<string, Task[]>
  sessionsMap: Record<string, Session[]>
  rolesMap: Record<string, Record<string, string>>
  pending: number
  onSelectPosture: (p: Posture) => void
  onSelectTarget: (pid: string, sid: string) => void
  onToggleExpanded: (pid: string) => void
  onOpenResource: (r: ResourceKey) => void
  onOpenSettings: () => void
  onOpenApprovals: () => void
  onCreated: (pid: string) => void
}) {
  const [createOpen, setCreateOpen] = useState(false)

  const backToOld = () => {
    try { localStorage.setItem("ui.shell", "1") } catch { /* 隐私模式等 */ }
    const u = new URL(location.href)
    u.searchParams.delete("shell")
    history.replaceState(null, "", u.toString())
    location.reload()
  }

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r">
      {/* 姿态切换 */}
      <div className="grid grid-cols-3 gap-1 p-2">
        {POSTURES.map((p) => (
          <button
            key={p.key}
            onClick={() => onSelectPosture(p.key)}
            className={cn(
              "rounded-md py-1.5 text-xs",
              screen.kind === "posture" && posture === p.key
                ? "bg-secondary text-primary"
                : "text-muted-foreground hover:bg-accent",
            )}
          >
            {p.label}
          </button>
        ))}
      </div>

      <div className="px-2 pb-2">
        <button
          onClick={() => setCreateOpen(true)}
          className="w-full rounded-md border border-dashed py-1.5 text-xs text-muted-foreground hover:bg-accent"
        >
          ＋ 新建作战
        </button>
      </div>

      <div className="px-3 pb-1 text-[10px] text-muted-foreground">资源</div>
      <div className="grid grid-cols-2 gap-1 px-2 pb-2">
        {RESOURCES.map((r) => (
          <button
            key={r.key}
            onClick={() => onOpenResource(r.key)}
            className={cn(
              "flex items-center gap-1 rounded-md px-2 py-1 text-xs",
              screen.kind === "resource" && screen.resource === r.key
                ? "bg-secondary text-primary"
                : "text-muted-foreground hover:bg-accent",
            )}
          >
            <span>{r.icon}</span>{r.label}
          </button>
        ))}
      </div>

      <div className="px-3 pb-1 text-[10px] text-muted-foreground">作战项目</div>
      <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        <ProjectTree
          projects={projects}
          expanded={expanded}
          tasksMap={tasksMap}
          sessionsMap={sessionsMap}
          rolesMap={rolesMap}
          target={target}
          onToggleExpanded={onToggleExpanded}
          onSelectTarget={onSelectTarget}
        />
      </div>

      <div className="flex items-center gap-1 border-t p-2">
        <button
          onClick={onOpenApprovals}
          title="审批"
          className={cn(
            "relative flex items-center rounded-md px-2 py-1 text-xs",
            screen.kind === "approvals" ? "bg-secondary" : "hover:bg-accent",
          )}
        >
          🔔
          {pending > 0 && (
            <span className="ml-0.5 rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
              {pending}
            </span>
          )}
        </button>
        <button
          onClick={onOpenSettings}
          title="设置"
          className={cn(
            "rounded-md px-2 py-1 text-xs",
            screen.kind === "settings" ? "bg-secondary" : "hover:bg-accent",
          )}
        >
          ⚙ 设置
        </button>
        <span className="flex-1" />
        <button
          onClick={backToOld}
          title="切回旧壳（并存期）"
          className="rounded-md px-2 py-1 text-[10px] text-muted-foreground hover:bg-accent"
        >
          旧壳
        </button>
      </div>

      {createOpen && (
        <CreateDialog
          onClose={() => setCreateOpen(false)}
          onCreated={(pid) => {
            setCreateOpen(false)
            onCreated(pid)
          }}
        />
      )}
    </aside>
  )
}
