import { useCallback, useEffect, useState } from "react"
import { api, pollJob } from "@/lib/api"
import type { IntelFeed, IntelProfile } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// 设置页「情报源」tab（E9，DESIGN.md §16.4）：源清单 CRUD + 兴趣画像（七方向权重、
// 学习阶段声明）+ 手动抓取。仿 McpPane 结构：读取即加载，保存整单 PUT（.history 备份）。

const DIRECTION_LABELS: Record<string, string> = {
  web: "Web", ai: "AI 安全", vehicle: "车联网", reverse: "逆向",
  android: "Android", pwn: "PWN", forensics: "取证",
}

export function IntelSourcePane() {
  const [feeds, setFeeds] = useState<IntelFeed[]>([])
  const [profile, setProfile] = useState<IntelProfile | null>(null)
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const flashSaved = () => {
    setSaved(true)
    setTimeout(() => setSaved(false), 1500)
  }

  const reload = useCallback(() => {
    api.intelFeeds().then((r) => setFeeds(r.feeds)).catch(() => {})
    api.intelProfile().then(setProfile).catch(() => {})
  }, [])
  useEffect(() => { reload() }, [reload])

  const saveFeeds = async (list: IntelFeed[]) => {
    try {
      const r = await api.intelUpdateFeeds(
        list.map((f) => ({ name: f.name, url: f.url })))
      setFeeds(r.feeds)
      flashSaved()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const saveProfile = async (p: IntelProfile) => {
    try {
      const r = await api.intelUpdateProfile(p)
      setProfile(r.profile)
      flashSaved()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const fetchNow = async () => {
    if (busy) return
    setBusy(true)
    setMsg(null)
    try {
      const { job_id } = await api.intelFetch()
      const job = await pollJob(job_id, () => {})
      const res = job.result as { new?: number; scored?: number } | null
      setMsg(job.status === "done"
        ? `抓取完成：新增 ${res?.new ?? 0} 条，评分 ${res?.scored ?? 0} 条`
        : `抓取失败：${job.error ?? "未知错误"}`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const setWeight = (d: string, v: string) => {
    if (!profile) return
    const num = parseFloat(v)
    setProfile({ ...profile, directions: { ...profile.directions, [d]: isNaN(num) ? 0 : num } })
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm">config/intel/feeds.json</span>
        <span className="flex-1" />
        <span className={cn("text-[10px] transition-opacity", saved ? "text-primary opacity-100" : "opacity-0")}>已保存 ✓</span>
        <Button size="sm" onClick={fetchNow} disabled={busy}>{busy ? "抓取中…" : "手动抓取"}</Button>
      </div>
      {msg && <p className="text-[10px] text-muted-foreground">{msg}</p>}
      <div className="space-y-1.5">
        {feeds.map((f, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Input value={f.name} onChange={(e) => setFeeds((fs) => fs.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}
                   placeholder="源名" className="h-7 w-36 text-xs" />
            <Input value={f.url} onChange={(e) => setFeeds((fs) => fs.map((x, j) => j === i ? { ...x, url: e.target.value } : x))}
                   placeholder="RSS URL" className="h-7 flex-1 font-mono text-xs" />
            <Button size="sm" variant="ghost" className="h-7 px-2 text-[10px] text-[--status-error]"
                    onClick={() => saveFeeds(feeds.filter((_, j) => j !== i))}>✕</Button>
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                onClick={() => setFeeds((fs) => [...fs, { name: "", url: "", kind: "rss" }])}>＋ 新增源</Button>
        <Button size="sm" className="h-7 px-2 text-xs" disabled={!feeds.some((f) => f.url.trim())}
                onClick={() => saveFeeds(feeds)}>保存源清单</Button>
      </div>

      <div className="mt-2 border-t pt-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm">profile.json · 兴趣画像</span>
          <span className="flex-1" />
        </div>
        {profile && (
          <div className="mt-2 space-y-2">
            <div className="flex flex-wrap items-center gap-3">
              {Object.entries(profile.directions).map(([d, w]) => (
                <label key={d} className="flex items-center gap-1 text-xs">
                  {DIRECTION_LABELS[d] ?? d}
                  <Input type="number" step="0.5" min="0" max="5" value={w}
                         onChange={(e) => setWeight(d, e.target.value)}
                         className="h-7 w-16 text-xs" />
                </label>
              ))}
            </div>
            <label className="flex items-center gap-2 text-xs">
              学习阶段声明
              <Input value={profile.stage} onChange={(e) => setProfile({ ...profile, stage: e.target.value })}
                     placeholder="如：Web 入门 / 逆向进阶" className="h-7 w-56 text-xs" />
            </label>
            <Button size="sm" className="h-7 px-2 text-xs" onClick={() => saveProfile(profile)}>保存画像</Button>
          </div>
        )}
        <p className="mt-2 text-[10px] text-[--status-approval]">
          抓取出站为平台自身可信请求（不经执行网关）；classifier 小模型缺席时自动降级规则打分，情报功能始终可用。
        </p>
      </div>
    </div>
  )
}
