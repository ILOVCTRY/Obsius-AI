import { useCallback, useEffect, useState } from "react"
import { api, pollJob } from "@/lib/api"
import type { IntelFeed, IntelProfile, IntelVaultInfo } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { fmtDateTimeMin } from "@/lib/datetime"
import { cn } from "@/lib/utils"

// 设置页「情报源」tab（E9/E10，DESIGN.md §16.4/§16.3）：源清单 CRUD + 兴趣画像 +
// Obsidian vault 只读接入（保存即自动索引）+ 手动抓取。仿 McpPane 结构：读取即加载，
// 保存整单 PUT（.history 备份）。

const DIRECTION_LABELS: Record<string, string> = {
  web: "Web", ai: "AI 安全", vehicle: "车联网", reverse: "逆向",
  android: "Android", pwn: "PWN", forensics: "取证",
}

type SearxngStatus = {
  ok?: boolean
  docker?: boolean
  image?: boolean
  container?: boolean
  running?: boolean
  http?: boolean
  proxy?: boolean | null
  proxy_configured?: boolean
  count?: number
  engines?: string[]
  detail?: string
}

type SearxngNotice = { text: string; tone: "success" | "error" | "pending" }
type SearxngAction = "connection" | "validate" | "start" | "stop"

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export function IntelSourcePane() {
  const [feeds, setFeeds] = useState<IntelFeed[]>([])
  const [profile, setProfile] = useState<IntelProfile | null>(null)
  // E10：vault 配置（vaultInfo=服务端现状，vaultPath=输入框编辑值）
  const [vaultInfo, setVaultInfo] = useState<IntelVaultInfo | null>(null)
  const [vaultPath, setVaultPath] = useState("")
  const [vaultBusy, setVaultBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [keywordInput, setKeywordInput] = useState("")
  const [searxStatus, setSearxStatus] = useState<SearxngStatus | null>(null)
  const [searxBusy, setSearxBusy] = useState(false)
  const [searxNotice, setSearxNotice] = useState<SearxngNotice | null>(null)
  const [composing, setComposing] = useState(false)

  const flashSaved = () => {
    setSaved(true)
    setTimeout(() => setSaved(false), 1500)
  }

  const reload = useCallback(() => {
    api.intelFeeds().then((r) => setFeeds(r.feeds)).catch(() => {})
    api.intelProfile().then(setProfile).catch(() => {})
    api.intelVault().then((v) => { setVaultInfo(v); setVaultPath(v.path) }).catch(() => {})
  }, [])
  useEffect(() => { reload(); api.intelSearxngStatus().then(setSearxStatus).catch(() => {}) }, [reload])

  const addKeyword = () => {
    if (!profile) return
    const text = keywordInput.normalize("NFKC").trim().replace(/\s+/gu, " ").slice(0, 60)
    if (!text) return
    const keywords = profile.keywords ?? []
    if (keywords.length >= 20) { setMsg("关注词最多 20 条"); return }
    if (keywords.some((k) => k.text.normalize("NFKC").toLocaleLowerCase() === text.normalize("NFKC").toLocaleLowerCase())) {
      setMsg("这个关注词已经录入")
      return
    }
    setProfile({ ...profile, keywords: [...keywords, { text, added_at: new Date().toISOString() }] })
    setKeywordInput("")
    setMsg(null)
  }

  const searxAction = async (action: SearxngAction) => {
    if (searxBusy) return
    setSearxBusy(true)
    setSearxNotice(action === "start"
      ? { text: "正在启动 SearXNG…", tone: "pending" }
      : action === "stop" ? { text: "正在停止 SearXNG…", tone: "pending" } : null)
    try {
      if (action === "start") {
        const result = await api.intelSearxngStart()
        if (result.status === "error") {
          const detail = typeof result.detail === "string" ? result.detail : "启动请求失败"
          const status = await api.intelSearxngStatus().catch(() => null)
          if (status) setSearxStatus(status as SearxngStatus)
          setSearxNotice({ text: `启动失败：${detail}`, tone: "error" })
          return
        }
        let status: SearxngStatus | null = null
        for (let attempt = 0; attempt < 20; attempt += 1) {
          status = await api.intelSearxngStatus() as SearxngStatus
          setSearxStatus(status)
          if (status.running && status.http) break
          await sleep(500)
        }
        if (status?.running && status.http) {
          setSearxNotice({
            text: result.reused ? "SearXNG 已运行（复用了现有容器）" : "SearXNG 已启动并通过 HTTP 探测",
            tone: "success",
          })
        } else {
          setSearxNotice({ text: "启动命令已执行，但服务未通过 HTTP 探测；请检查容器状态", tone: "error" })
        }
        return
      }
      if (action === "stop") {
        const result = await api.intelSearxngStop()
        const status = await api.intelSearxngStatus() as SearxngStatus
        setSearxStatus(status)
        setSearxNotice(result.status === "error" || status.running
          ? { text: "停止失败：容器仍在运行或 Docker 返回错误", tone: "error" }
          : { text: "SearXNG 已停止", tone: "success" })
        return
      }
      if (action === "connection") {
        const result = await api.intelSearxngConnectionTest()
        setSearxStatus(result as SearxngStatus)
        const issues = [
          !result.docker && "Docker 不可用",
          !result.image && "镜像不存在（不会自动拉取）",
          !result.container && "容器不存在",
          result.container && !result.running && "容器未运行",
          result.running && !result.http && "HTTP 端口未响应",
          result.proxy_configured && result.proxy === false && "代理地址不可达",
        ].filter((item): item is string => typeof item === "string")
        setSearxNotice(issues.length
          ? { text: `连接检查未通过：${issues.join("；")}`, tone: "error" }
          : { text: "连接检查通过；未发起真实搜索", tone: "success" })
        return
      }
      const result = await api.intelSearxngValidateSearch()
      const count = typeof result.count === "number" ? result.count : 0
      const engines = Array.isArray(result.engines) ? result.engines.join("、") : "未知"
      const detail = typeof result.detail === "string" ? `：${result.detail}` : ""
      setSearxNotice(result.ok
        ? { text: `检索验证通过：${count} 条，搜索引擎 ${engines}`, tone: "success" }
        : { text: `检索验证失败：${count} 条，搜索引擎 ${engines}${detail}`, tone: "error" })
      const status = await api.intelSearxngStatus().catch(() => null)
      if (status) setSearxStatus(status as SearxngStatus)
    } catch (e) {
      setSearxNotice({ text: e instanceof Error ? e.message : String(e), tone: "error" })
      const status = await api.intelSearxngStatus().catch(() => null)
      if (status) setSearxStatus(status as SearxngStatus)
    } finally {
      setSearxBusy(false)
    }
  }

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

  // E10：保存 vault 配置（写 profile.json 经 .history 备份）；配置了路径即自动触发索引 Job
  const saveVault = async () => {
    if (vaultBusy) return
    setVaultBusy(true)
    setMsg(null)
    try {
      const r = await api.intelUpdateVault({ path: vaultPath.trim(), enabled: !!vaultPath.trim() })
      if (r.index_job_id) {
        setMsg("vault 配置已保存，正在索引…")
        const job = await pollJob(r.index_job_id, () => {})
        const res = job.result as { indexed?: number } | null
        setMsg(job.status === "done"
          ? `索引完成：${res?.indexed ?? 0} 篇笔记`
          : `索引失败：${job.error ?? "未知错误"}`)
      } else {
        setMsg("vault 配置已保存（未配置路径，未索引）")
      }
      const v = await api.intelVault()
      setVaultInfo(v)
      setVaultPath(v.path)
      flashSaved()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setVaultBusy(false)
    }
  }

  // 手动全量重建索引（vault 编辑后同步用；v1 无自动监听，接受 staleness）
  const rebuildVault = async () => {
    if (vaultBusy) return
    setVaultBusy(true)
    setMsg(null)
    try {
      const { job_id } = await api.intelVaultIndex()
      const job = await pollJob(job_id, () => {})
      const res = job.result as { indexed?: number } | null
      setMsg(job.status === "done"
        ? `重建完成：${res?.indexed ?? 0} 篇笔记`
        : `重建失败：${job.error ?? "未知错误"}`)
      api.intelVault().then(setVaultInfo).catch(() => {})
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setVaultBusy(false)
    }
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
            <Button size="sm" variant="ghost" className="h-7 px-2 text-[10px] text-(--status-error)"
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
            <div className="space-y-1">
              <p className="text-xs">关注词</p>
              <div className="flex flex-wrap items-center gap-1">
                {(profile.keywords ?? []).map((keyword, index) => (
                  <button key={`${keyword.text}-${index}`} type="button" className="rounded border px-2 py-1 text-[10px]"
                    title="点击删除" onClick={() => setProfile({ ...profile, keywords: (profile.keywords ?? []).filter((_, i) => i !== index) })}>{keyword.text} ×</button>
                ))}
                <Input value={keywordInput} onChange={(e) => setKeywordInput(e.target.value)} placeholder="输入关注词后回车" className="h-7 w-48 text-xs"
                  onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !composing && !e.nativeEvent.isComposing) { e.preventDefault(); addKeyword() } else if (e.key === "Backspace" && !keywordInput && !composing && (profile.keywords ?? []).length) setProfile({ ...profile, keywords: (profile.keywords ?? []).slice(0, -1) }) }} />
              </div>
            </div>
            <Button size="sm" className="h-7 px-2 text-xs" onClick={() => saveProfile(profile)}>保存画像</Button>
          </div>
        )}
        <p className="mt-2 text-[10px] text-(--status-approval)">
          抓取出站为平台自身可信请求（不经执行网关）；classifier 小模型缺席时自动降级规则打分，情报功能始终可用。
        </p>
      </div>

      <div className="mt-2 border-t pt-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm">高级检索 · SearXNG</span><span className="flex-1" />
          <span aria-live="polite" className={cn(
            "text-[10px]",
            searxStatus?.running && searxStatus.http ? "text-(--status-running)"
              : searxStatus ? "text-(--status-error)" : "text-muted-foreground",
          )}>
            {searxBusy ? "操作中…" : searxStatus?.running && searxStatus.http ? "运行中 · HTTP 正常"
              : searxStatus?.running ? "容器运行中 · HTTP 未响应"
                : searxStatus ? "未运行" : "状态未知"}
          </span>
        </div>
        <div className="mt-1 text-[10px] text-muted-foreground" aria-live="polite">
          {searxStatus ? <>
            Docker {searxStatus.docker ? "可用" : "不可用"} · 镜像 {searxStatus.image ? "已缓存" : "不存在"} ·
            容器 {searxStatus.container ? (searxStatus.running ? "运行中" : "已停止") : "不存在"} ·
            HTTP {searxStatus.http ? "正常" : "无响应"} · 代理 {searxStatus.proxy_configured
              ? searxStatus.proxy ? "可达" : "不可达" : "未配置"}
          </> : "正在读取容器状态…"}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={Boolean(((profile?.search as Record<string, unknown> | undefined)?.searxng as Record<string, unknown> | undefined)?.enabled)} onChange={(e) => profile && setProfile({ ...profile, search: { ...(profile.search ?? {}), searxng: { ...(((profile.search as Record<string, unknown> | undefined)?.searxng as Record<string, unknown> | undefined) ?? {}), enabled: e.target.checked } } })} />启用</label>
          <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={Boolean(((profile?.search as Record<string, unknown> | undefined)?.searxng as Record<string, unknown> | undefined)?.auto_start)} onChange={(e) => profile && setProfile({ ...profile, search: { ...(profile.search ?? {}), searxng: { ...(((profile.search as Record<string, unknown> | undefined)?.searxng as Record<string, unknown> | undefined) ?? {}), auto_start: e.target.checked } } })} />随下次平台启动</label>
          <Button size="sm" className="h-7 px-2 text-xs" onClick={() => profile && saveProfile(profile)} disabled={!profile || searxBusy}>保存配置</Button>
          <Button size="sm" variant="outline" onClick={() => void searxAction("connection")} disabled={searxBusy}>测试连接</Button>
          <Button size="sm" variant="outline" onClick={() => void searxAction("validate")} disabled={searxBusy}>验证检索</Button>
          <Button size="sm" onClick={() => void searxAction("start")} disabled={searxBusy || Boolean(searxStatus?.running && searxStatus.http)}>{searxBusy ? "处理中…" : "立即启动"}</Button>
          <Button size="sm" variant="outline" onClick={() => void searxAction("stop")} disabled={searxBusy || !searxStatus?.running}>停止</Button>
        </div>
        {searxNotice && <p role="status" className={cn(
          "mt-1 text-[10px]",
          searxNotice.tone === "success" ? "text-(--status-running)"
            : searxNotice.tone === "error" ? "text-(--status-error)" : "text-muted-foreground",
        )}>{searxNotice.text}</p>}
        <p className="mt-1 text-[10px] text-muted-foreground">未启用时检索走内置 DDG 通道。</p>
      </div>

      <div className="mt-2 border-t pt-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm">Obsidian vault · 只读接入</span>
          <span className="flex-1" />
        </div>
        <div className="mt-2 space-y-2">
          <div className="flex items-center gap-2">
            <Input value={vaultPath} onChange={(e) => setVaultPath(e.target.value)}
                   placeholder="vault 根目录绝对路径，如 D:/Notes/MyVault" className="h-7 flex-1 font-mono text-xs" />
            <Button size="sm" className="h-7 px-2 text-xs" onClick={saveVault} disabled={vaultBusy}>
              {vaultBusy ? "处理中…" : "保存并索引"}
            </Button>
            <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                    onClick={rebuildVault} disabled={vaultBusy || !vaultInfo?.configured}>重建索引</Button>
          </div>
          <p className="font-mono text-[10px] text-muted-foreground">
            {vaultInfo?.configured
              ? `已索引 ${vaultInfo.notes} 篇 · 上次索引 ${vaultInfo.last_indexed ? fmtDateTimeMin(vaultInfo.last_indexed) : "—"}`
              : "未配置——vault 只读，平台绝不写回；笔记正文仅本地搜索用，LLM 只看元数据。"}
          </p>
        </div>
        <p className="mt-2 text-[10px] text-(--status-approval)">
          索引为全量重建，编辑笔记后手动点「重建索引」同步（v1 无自动监听）；树/搜索按上次索引快照展示。
        </p>
      </div>
    </div>
  )
}
