import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { FeishuConfig, FeishuPushRecord } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { fmtDateTimeMin } from "@/lib/datetime"
import { cn } from "@/lib/utils"

// 设置页「飞书推送」tab（feishu-push v1，docs/plans/feishu-connector.md）：
// 确认的中危及以上漏洞（vuln + verified + severity≥min）边沿触发 → 自定义机器人
// webhook。密钥只落服务端 config/feishu.json（.gitignore），前端只见脱敏回显。

const SEV_LABELS: Record<string, string> = {
  critical: "严重", high: "高危", medium: "中危", low: "低危",
}

const TRIGGER_LABELS: Record<string, string> = {
  new: "首报", verified: "确认", escalation: "升档",
}

export function FeishuPane() {
  const [cfg, setCfg] = useState<FeishuConfig | null>(null)
  // 表单编辑值（加载后由 cfg 回填；webhook/secret 空 = 不修改）
  const [enabled, setEnabled] = useState(false)
  const [webhook, setWebhook] = useState("")
  const [secret, setSecret] = useState("")
  const [clearSecret, setClearSecret] = useState(false)
  const [minSeverity, setMinSeverity] = useState("medium")
  const [projects, setProjects] = useState("")
  const [escalation, setEscalation] = useState(false)
  const [includePoc, setIncludePoc] = useState(false)
  const [baseUrl, setBaseUrl] = useState("")
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [testMsg, setTestMsg] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)

  const reload = useCallback(() => {
    api.feishuConfig().then((r) => {
      setCfg(r)
      setEnabled(r.enabled)
      setWebhook(r.webhook_url)
      setSecret("")
      setClearSecret(false)
      setMinSeverity(r.min_severity)
      setProjects(r.projects.join(", "))
      setEscalation(r.notify_on_escalation)
      setIncludePoc(r.include_poc)
      setBaseUrl(r.base_url)
    }).catch((e) => setErr(String(e)))
  }, [])

  useEffect(() => { reload() }, [reload])

  // stats 轻轮询（推送记录/熔断状态）：15s 拉一次配置（含 stats）
  useEffect(() => {
    const t = window.setInterval(() => {
      api.feishuConfig().then(setCfg).catch(() => {})
    }, 15000)
    return () => window.clearInterval(t)
  }, [])

  const save = async () => {
    setErr(null)
    const body: Record<string, unknown> = {
      enabled,
      min_severity: minSeverity,
      projects: projects.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      notify_on_escalation: escalation,
      include_poc: includePoc,
      base_url: baseUrl.trim(),
    }
    if (webhook.trim() && !webhook.includes("*")) body.webhook_url = webhook.trim()
    if (clearSecret) body.clear_secret = true
    else if (secret.trim()) body.secret = secret.trim()
    try {
      const r = await api.feishuSaveConfig(body)
      setCfg(r)
      setWebhook(r.webhook_url)
      setSecret("")
      setClearSecret(false)
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
    } catch (e) {
      setErr(String(e))
    }
  }

  const test = async () => {
    setTesting(true)
    setTestMsg(null)
    try {
      const r = await api.feishuTest()
      setTestMsg(r.detail)
      if (r.ok) api.feishuConfig().then(setCfg).catch(() => {})
    } catch (e) {
      setTestMsg(String(e))
    } finally {
      setTesting(false)
    }
  }

  const stats = cfg?.stats
  const recent: FeishuPushRecord[] = stats?.worker?.recent ?? []

  return (
    <ScrollArea className="h-full">
      <div className="mx-auto max-w-3xl space-y-4 p-4">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-medium">飞书推送</h2>
          <span className="text-[10px] text-muted-foreground">确认的中危及以上漏洞自动推送到飞书群</span>
          <span className="flex-1" />
          {saved && <span className="text-[10px] text-primary">已保存 ✓</span>}
          <Button size="sm" variant="outline" className="h-7 text-[11px]" disabled={testing}
                  onClick={() => void test()}>测试连接</Button>
          <Button size="sm" className="h-7 text-[11px]" onClick={() => void save()}>保存</Button>
        </div>
        {err && <p className="text-[11px] text-(--status-error)">{err}</p>}
        {testMsg && <p className="text-[11px] text-muted-foreground">测试：{testMsg}</p>}

        <div className="rounded border bg-card/40 p-3 space-y-3">
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={enabled}
                   onChange={(e) => setEnabled(e.target.checked)} />
            启用推送（关闭后只记录不发送；默认关）
          </label>

          <div className="space-y-1">
            <label className="text-[10px] text-muted-foreground">
              自定义机器人 Webhook 地址（群设置 → 群机器人 → 自定义机器人；仅 https）
            </label>
            <Input value={webhook} onChange={(e) => setWebhook(e.target.value)}
                   className="font-mono text-xs"
                   placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." />
            {cfg && cfg.webhook_set && !webhook.includes("*") && (
              <p className="text-[10px] text-muted-foreground">已保存新地址，点「保存」生效</p>
            )}
          </div>

          <div className="space-y-1">
            <label className="text-[10px] text-muted-foreground">
              签名密钥 Secret（自定义机器人「签名校验」；留空 = 不修改，已设置：
              {cfg?.secret_set ? "是" : "否"}）
            </label>
            <div className="flex items-center gap-2">
              <Input type="password" value={secret} onChange={(e) => { setSecret(e.target.value); setClearSecret(false) }}
                     className="text-xs" placeholder={cfg?.secret_set ? "••••（不修改）" : "未设置"} />
              {cfg?.secret_set && (
                <Button size="sm" variant="outline" className="h-7 text-[11px]"
                        onClick={() => { setClearSecret(true); setSecret("") }}>清除</Button>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-xs">
              最低严重度
              <select value={minSeverity} onChange={(e) => setMinSeverity(e.target.value)}
                      className="rounded border bg-background px-2 py-1 text-xs">
                {(cfg?.min_severity_choices ?? ["low", "medium", "high", "critical"]).map((s) => (
                  <option key={s} value={s}>{SEV_LABELS[s] ?? s}（{s}）及以上</option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={escalation}
                     onChange={(e) => setEscalation(e.target.checked)} />
              已推漏洞严重度升档时再推一次
            </label>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={includePoc}
                     onChange={(e) => setIncludePoc(e.target.checked)} />
              卡片附带 POC 原文（默认不带，防利用细节外泄）
            </label>
          </div>

          <div className="space-y-1">
            <label className="text-[10px] text-muted-foreground">
              项目白名单（逗号分隔项目 id 或 slug；留空 = 全部项目）
            </label>
            <Input value={projects} onChange={(e) => setProjects(e.target.value)}
                   className="font-mono text-xs" placeholder="留空 = 全部项目" />
          </div>

          <div className="space-y-1">
            <label className="text-[10px] text-muted-foreground">
              平台地址 base_url（卡片「打开平台」按钮深链用）
            </label>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                   className="font-mono text-xs" placeholder="http://127.0.0.1:8420" />
          </div>
        </div>

        {/* 运行状态 */}
        <div className="rounded border bg-card/40 p-3 space-y-2">
          <div className="flex items-center gap-3 text-[11px]">
            <span className="font-medium">运行状态</span>
            {stats?.worker && (
              <span className={cn("flex items-center gap-1",
                stats.worker.degraded ? "text-(--status-error)" : "text-muted-foreground")}>
                <span className={cn("inline-block h-1.5 w-1.5 rounded-full",
                  stats.worker.degraded ? "bg-(--status-error)" :
                  stats.worker.running ? "bg-(--status-ok)" : "bg-muted-foreground")} />
                {stats.worker.degraded ? "已熔断（连续失败，修复后点测试连接复位）"
                  : stats.worker.running ? "运行中" : "未启动"}
              </span>
            )}
            <span className="flex-1" />
            <Button size="sm" variant="ghost" className="h-6 px-2 text-[10px]"
                    onClick={() => void reload()}>刷新</Button>
          </div>
          <div className="flex flex-wrap gap-4 text-[10px] text-muted-foreground">
            <span>已订阅项目 {stats?.attached.length ?? 0}</span>
            <span>累计判定推送 {stats?.pushed_total ?? 0}</span>
            <span>已发送 {stats?.worker?.sent ?? 0}</span>
            <span>失败 {stats?.worker?.failed ?? 0}</span>
            <span>丢弃 {stats?.worker?.dropped ?? 0}</span>
            <span>队列 {stats?.worker?.queue_depth ?? 0}/{stats?.worker?.queue_cap ?? 0}</span>
          </div>
          {recent.length > 0 && (
            <table className="w-full text-[10px]">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="py-1 pr-2 font-normal">时间</th>
                  <th className="py-1 pr-2 font-normal">结果</th>
                  <th className="py-1 pr-2 font-normal">项目</th>
                  <th className="py-1 pr-2 font-normal">漏洞</th>
                  <th className="py-1 pr-2 font-normal">触发</th>
                  <th className="py-1 font-normal">备注</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((r, i) => (
                  <tr key={`${r.ts}-${i}`} className="border-t border-border/40">
                    <td className="py-1 pr-2 whitespace-nowrap text-muted-foreground">{fmtDateTimeMin(r.ts)}</td>
                    <td className={cn("py-1 pr-2", r.ok ? "text-(--status-ok)" : "text-(--status-error)")}>
                      {r.ok ? "✓" : "✗"}
                    </td>
                    <td className="py-1 pr-2 max-w-24 truncate">{r.project || r.finding_id || "—"}</td>
                    <td className="py-1 pr-2 max-w-40 truncate" title={r.title}>
                      {r.title || "—"}
                      {r.severity && <span className="ml-1 text-muted-foreground">({r.severity})</span>}
                    </td>
                    <td className="py-1 pr-2">{r.trigger ? TRIGGER_LABELS[r.trigger] ?? r.trigger : "—"}</td>
                    <td className="py-1 max-w-40 truncate text-muted-foreground" title={r.detail}>{r.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {recent.length === 0 && (
            <p className="text-[10px] text-muted-foreground">暂无推送记录</p>
          )}
        </div>

        <p className="text-[10px] leading-relaxed text-muted-foreground">
          触发口径：漏洞（category=vuln）经门禁验证为已确认（status=verified，必须带复现证据）
          且严重度 ≥ 所选最低档时推送；同一漏洞只推一次（升档再推可另开）。
          推送失败只记日志，绝不影响平台写操作。配置仅存本机 config/feishu.json，不随仓库分发。
        </p>
      </div>
    </ScrollArea>
  )
}
