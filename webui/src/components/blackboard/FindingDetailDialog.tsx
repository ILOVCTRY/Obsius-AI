import { useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { Finding } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"
import { fmtDateTime, utcTitle } from "@/lib/datetime"

// 发现详情弹窗（发现列表/黑板画布/攻击链画布三调用点共用）：复现步骤 + POC 一键复制；
// F10 人工修订：编辑态（title/severity/vuln_class，增量 patch）+ 删除入口（零打扰不触发撤回传播）。

const SEVERITY_COLOR: Record<string, string> = {
  critical: "text-(--status-error)",
  high: "text-(--status-error)",
  medium: "text-(--status-approval)",
  low: "text-muted-foreground",
  info: "text-muted-foreground",
}

// severity 五档白名单（与 store FINDING_SEVERITIES 一致）+ 中文辅助文案
const SEVERITIES: { value: string; label: string }[] = [
  { value: "critical", label: "critical（严重 / 关键突破）" },
  { value: "high", label: "high（高 / 有效线索）" },
  { value: "medium", label: "medium（中）" },
  { value: "low", label: "low（低）" },
  { value: "info", label: "info（信息）" },
]

// evidence.poc(s) 约定（DESIGN.md §5.2）：稳定复现的证据结构；
// 同一发现可有**多条 POC**（不同触发路径/报文位置），pocs 数组逐条独立；
// 旧数据单条 evidence.poc 与 findings.poc_artifact_id 保留兼容（视为主 POC）。
export interface FindingPoc {
  name?: string // POC 名称（如 "GET 探测" / "POST 利用"）
  type?: string // http_raw | python | steps
  http_raw?: string // HTTP 报文原文（可复现的最小报文，稳定触发时入黑板）
  artifact_id?: string // 不稳定时引 artifacts 里的 Python POC
  target?: string
  stability?: string // 如 "3/3"（连续 3 次全部触发）
}

export function pocsOf(f: Finding): FindingPoc[] {
  const ev = f.evidence as { poc?: unknown; pocs?: unknown } | undefined
  const list: FindingPoc[] = []
  if (Array.isArray(ev?.pocs)) {
    for (const p of ev.pocs) {
      if (p && typeof p === "object") list.push(p as FindingPoc)
    }
  }
  if (!list.length && ev?.poc && typeof ev.poc === "object") {
    list.push(ev.poc as FindingPoc)
  }
  return list
}

/** 画布 POC 角标用：有没有任意 POC（含旧数据产物引用） */
export function hasPoc(f: Finding): boolean {
  return pocsOf(f).length > 0 || !!f.poc_artifact_id
}

function CopyButton({ text, label = "复制" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      size="sm"
      variant="outline"
      className="h-6 shrink-0 px-2 text-[10px]"
      onClick={() => {
        navigator.clipboard.writeText(text).catch(() => {})
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      }}
    >
      {copied ? "已复制 ✓" : label}
    </Button>
  )
}

function reproText(f: Finding, pocs: FindingPoc[], steps: string[],
                   scripts: Record<string, string | undefined>): string {
  // 组装成可直接照做的复现文稿（一键复制全部，含全部 POC）
  const lines = [`${f.title}  [${f.severity}/${f.status}]`]
  if (steps.length) {
    lines.push("", "复现步骤:")
    steps.forEach((s, i) => lines.push(`${i + 1}. ${s}`))
  }
  pocs.forEach((poc, i) => {
    const label = pocs.length > 1 ? `POC ${i + 1}${poc.name ? `（${poc.name}）` : ""}` : "POC"
    if (poc.target || poc.stability) {
      lines.push("", `${label}${poc.stability ? ` [稳定性 ${poc.stability}]` : ""}` +
        (poc.target ? ` 目标: ${poc.target}` : ""))
    } else {
      lines.push("", label)
    }
    if (poc.http_raw) lines.push(poc.http_raw)
    if (poc.artifact_id && scripts[poc.artifact_id]) {
      lines.push(scripts[poc.artifact_id]!)
    }
  })
  if (f.poc_artifact_id) lines.push("", `主 POC 产物: ${f.poc_artifact_id}`)
  return lines.join("\n")
}

// 弹窗内的 POC 脚本内容块（内容由父级统一预取；加载/出错/可读三态）
function ScriptBlock({ ref_, scripts }:
                     { ref_: string; scripts: Record<string, { loading: boolean; content?: string; path?: string; error?: string }> }) {
  const s = scripts[ref_]
  if (!s || s.loading) return <p className="text-xs text-muted-foreground">加载脚本内容…</p>
  if (s.error) {
    return <p className="font-mono text-[10px] text-muted-foreground">脚本内容不可读（{s.error}）· 引用: {ref_}</p>
  }
  if (!s.content) return null
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[10px] text-muted-foreground">{s.path}</span>
        <CopyButton text={s.content} label="复制脚本" />
      </div>
      <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2 font-mono text-[10px]">
        {s.content}
      </pre>
    </div>
  )
}

const selectCls =
  "h-8 rounded-md border bg-background px-1.5 text-xs"

export function FindingDetailDialog({ pid, finding, assetLabel, track, onClose, onMutated }:
                             { pid: string; finding: Finding; assetLabel?: string; track?: string; onClose: () => void; onMutated?: () => void }) {
  // F10：row 本地快照（初始=prop，保存成功用 PATCH 响应覆盖）——解耦 4s 轮询且头部即时回显
  const [row, setRow] = useState<Finding>(finding)
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ title: "", severity: "", vuln_class: "", rating_basis: "", category: "vuln" })
  const [saving, setSaving] = useState(false)
  const [editErr, setEditErr] = useState<string | null>(null)
  const [delOpen, setDelOpen] = useState(false)
  const [delErr, setDelErr] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  const pocs = pocsOf(row)
  const rawSteps = (row.evidence as { poc?: { steps?: unknown } })?.poc?.steps
  const steps = Array.isArray(rawSteps)
    ? rawSteps.map(String)
    : []  // 旧数据无结构化 steps → 用 evidence 各条目当步骤
  const entries = Object.entries(row.evidence ?? {})
    .filter(([k]) => k !== "poc" && k !== "pocs")

  const startEdit = () => {
    setForm({ title: row.title, severity: row.severity, vuln_class: row.vuln_class ?? "",
              rating_basis: row.rating_basis ?? "", category: row.category ?? "vuln" })
    setEditErr(null)
    setEditing(true)
  }
  const save = async () => {
    // 增量 patch：只发变化字段，无变化不发请求
    const changes: Record<string, unknown> = {}
    if (form.title !== row.title) changes.title = form.title
    if (form.severity !== row.severity) changes.severity = form.severity
    if (form.vuln_class !== (row.vuln_class ?? "")) changes.vuln_class = form.vuln_class
    // F11 判级依据：空串=清空（basis 必须证成当前 severity）
    if (form.rating_basis !== (row.rating_basis ?? "")) changes.rating_basis = form.rating_basis
    // C6 分两类：vuln=漏洞 / intel=有效发现·关键发现
    if (form.category !== (row.category ?? "vuln")) changes.category = form.category
    if (!Object.keys(changes).length) {
      setEditing(false)
      return
    }
    setSaving(true)
    setEditErr(null)
    try {
      const updated = await api.patchFinding(pid, row.id, changes)
      setRow(updated)
      setEditing(false)
      onMutated?.()
    } catch (e) {
      setEditErr(String(e))
    } finally {
      setSaving(false)
    }
  }
  const doDelete = async () => {
    setDeleting(true)
    setDelErr(null)
    try {
      await api.deleteFinding(pid, row.id)
      setDelOpen(false)
      onMutated?.()
      onClose()
    } catch (e) {
      setDelErr(String(e)) // 失败保持弹窗打开（TaskBoard 先例）
    } finally {
      setDeleting(false)
    }
  }

  // 全部 POC 的脚本引用（多 POC 逐条拉内容）+ 旧数据兜底：
  // findings.poc_artifact_id / evidence.poc_artifact 裸路径
  const evRecord = row.evidence as { poc_artifact?: unknown } | undefined
  const legacyRef = row.poc_artifact_id
    ?? (typeof evRecord?.poc_artifact === "string" ? evRecord.poc_artifact : null)
  const scriptRefs = useMemo(() => {
    const refs = pocs.map((p) => p.artifact_id).filter((r): r is string => !!r)
    if (legacyRef && !refs.includes(legacyRef)) refs.push(legacyRef)
    return [...new Set(refs)]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.id])
  const [scripts, setScripts] = useState<Record<string, { loading: boolean; content?: string; path?: string; error?: string }>>({})
  useEffect(() => {
    if (!scriptRefs.length) return
    setScripts(Object.fromEntries(scriptRefs.map((r) => [r, { loading: true }])))
    let alive = true
    for (const ref of scriptRefs) {
      api.artifactContent(pid, ref)
        .then((r) => { if (alive) setScripts((s) => ({ ...s, [ref]: { loading: false, content: r.content, path: r.path } })) })
        .catch((e) => { if (alive) setScripts((s) => ({ ...s, [ref]: { loading: false, error: String(e) } })) })
    }
    return () => { alive = false }
  }, [pid, scriptRefs])

  const hasPocAny = pocs.length > 0 || !!legacyRef
  const scriptContents: Record<string, string | undefined> = Object.fromEntries(
    Object.entries(scripts).map(([r, s]) => [r, s.content]))
  const fullText = reproText(row, pocs, steps, scriptContents)

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogTitle className="flex items-center gap-2 pr-6">
          <span className={cn("text-xs font-medium uppercase", SEVERITY_COLOR[row.severity])}>
            {row.severity}
          </span>
          <span>{row.title}</span>
          {row.status === "verified" && <Badge variant="outline" className="text-[10px]">verified</Badge>}
          <span className="flex-1" />
          {/* F10 人工修订：编辑态切换 + 删除入口 */}
          {!editing && (
            <Button size="sm" variant="outline" className="h-6 shrink-0 px-2 text-[10px]"
                    onClick={startEdit}>
              ✎ 编辑
            </Button>
          )}
          <Button size="sm" variant="outline"
                  className="h-6 shrink-0 px-2 text-[10px] text-(--status-error) hover:text-(--status-error)"
                  onClick={() => { setDelErr(null); setDelOpen(true) }}>
            🗑 删除
          </Button>
        </DialogTitle>
        <DialogDescription className="font-mono">
          {row.vuln_class || "（未分类）"} · <span title={utcTitle(row.created_at)}>{fmtDateTime(row.created_at)}</span> · {row.author}
          {assetLabel && <> · {assetLabel}</>}
        </DialogDescription>

        {/* F11 判级依据：注入评级口径判级时由 Agent 填写，人工可改 */}
        {(row.rating_basis ?? "").trim() && (
          <p className="rounded border bg-muted/30 px-2 py-1 font-mono text-[10px] text-muted-foreground"
             title={`判级依据：${row.rating_basis}`}>
            <span className="text-foreground">判级依据</span> · {row.rating_basis}
          </p>
        )}

        {editing && (
          <div className="space-y-2 rounded border p-2">
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">标题</label>
              <input className="h-8 w-full rounded-md border bg-background px-2 text-xs"
                     value={form.title}
                     onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))} />
            </div>
            <div className="flex gap-2">
              <div className="flex-1">
                <label className="mb-0.5 block text-[10px] text-muted-foreground">严重度</label>
                <select className={cn(selectCls, "w-full")}
                        value={form.severity}
                        onChange={(e) => setForm((f) => ({ ...f, severity: e.target.value }))}>
                  {SEVERITIES
                    // 渗透/红队轨 info 停收（2026-09-18）：编辑选项不含 info；
                    // 存量 info 行（清洗后应不存在）保留当前值显示防误改
                    .filter((s) => !(track === "pentest" || track === "redteam")
                                   || s.value !== "info" || s.value === form.severity)
                    .map((s) => (
                      <option key={s.value} value={s.value}>{s.label}</option>
                    ))}
                </select>
              </div>
              <div className="flex-1">
                <label className="mb-0.5 block text-[10px] text-muted-foreground">
                  类别（可空；CTF 轨填线索类别）
                </label>
                <input className="h-8 w-full rounded-md border bg-background px-2 text-xs"
                       value={form.vuln_class}
                       onChange={(e) => setForm((f) => ({ ...f, vuln_class: e.target.value }))} />
              </div>
            </div>
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">
                判级依据（可空；如 rating:edu-rating 高危#2 任意文件覆盖写）
              </label>
              <input className="h-8 w-full rounded-md border bg-background px-2 text-xs"
                     value={form.rating_basis}
                     onChange={(e) => setForm((f) => ({ ...f, rating_basis: e.target.value }))} />
            </div>
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">分类</label>
              <select className={cn(selectCls, "w-full")}
                      value={form.category}
                      onChange={(e) => setForm((f) => ({ ...f, category: e.target.value }))}>
                <option value="vuln">漏洞（可验证的安全问题）</option>
                <option value="intel">有效发现 / 关键发现（信息点/提示/合规等非漏洞结论）</option>
              </select>
            </div>
            {editErr && <p className="text-[10px] text-(--status-error)">{editErr}</p>}
            <div className="flex items-center gap-2">
              <Button size="sm" className="h-7 px-3 text-xs" disabled={saving} onClick={() => void save()}>
                {saving ? "保存中…" : "保存"}
              </Button>
              <Button size="sm" variant="outline" className="h-7 px-3 text-xs"
                      onClick={() => { setEditing(false); setEditErr(null) }}>
                取消
              </Button>
              <span className="text-[10px] text-muted-foreground">只提交变化字段；状态切换请走误报/验证通道</span>
            </div>
          </div>
        )}

        <div className="-mx-1 min-h-0 flex-1 space-y-3 overflow-y-auto px-1">
          {/* POC（置顶，打开即见）：同一发现可有多条，逐条展示报文 / Python 脚本内容 */}
          {hasPocAny && (
            <section>
              <div className="mb-1 flex items-center justify-between">
                <h4 className="text-[11px] font-medium text-foreground">
                  POC{pocs.length > 1 && <span className="text-[10px] text-muted-foreground"> · {pocs.length} 条</span>}
                </h4>
                <CopyButton text={fullText} label="一键复制全部" />
              </div>
              <div className="space-y-2">
                {pocs.length === 0 && legacyRef && (
                  // 旧数据：只有产物引用（findings.poc_artifact_id / evidence 裸路径）
                  <ScriptBlock ref_={legacyRef} scripts={scripts} />
                )}
                {pocs.map((poc, i) => (
                  <div key={i} className="rounded border p-2">
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <span className="text-[11px] font-medium text-foreground">
                        {poc.name ?? (pocs.length > 1 ? `POC ${i + 1}` : poc.type ?? "POC")}
                      </span>
                      {poc.stability && <Badge variant="outline" className="text-[10px]">稳定性 {poc.stability}</Badge>}
                      {poc.target && <span className="truncate font-mono text-[10px] text-muted-foreground">{poc.target}</span>}
                    </div>
                    {poc.http_raw && (
                      <div className="relative">
                        <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2 pr-14 font-mono text-[10px]">
                          {poc.http_raw}
                        </pre>
                        <div className="absolute right-1 top-1">
                          <CopyButton text={poc.http_raw} />
                        </div>
                      </div>
                    )}
                    {poc.artifact_id && (
                      <ScriptBlock ref_={poc.artifact_id} scripts={scripts} />
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* 复现步骤 */}
          <section>
            <div className="mb-1 flex items-center justify-between">
              <h4 className="text-[11px] font-medium text-foreground">复现步骤</h4>
              {!hasPocAny && <CopyButton text={fullText} label="一键复制全部" />}
            </div>
            {steps.length > 0 ? (
              <ol className="list-inside list-decimal space-y-1 text-xs text-foreground">
                {steps.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
            ) : entries.length > 0 ? (
              <div className="space-y-1.5">
                {/* 旧数据无结构化 steps：evidence 各条目按「字段名 + 内容 + 单独复制」呈现 */}
                {entries.map(([k, v]) => {
                  const text = typeof v === "string" ? v : JSON.stringify(v, null, 2)
                  return (
                    <div key={k} className="rounded border p-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[10px] text-muted-foreground">{k}</span>
                        <CopyButton text={text} />
                      </div>
                      <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-foreground">
                        {text}
                      </pre>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">（无复现步骤记录）</p>
            )}
          </section>
        </div>

        {/* F10 删除确认（失败保持打开） */}
        <AlertDialog open={delOpen} onOpenChange={(o) => !o && setDelOpen(false)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>物理删除发现「{row.title.slice(0, 40)}」？</AlertDialogTitle>
              <AlertDialogDescription>
                误报请用「编辑」外的状态通道（PATCH false-positive，触发撤回传播）；
                物理删除用于垃圾/走查数据，不可恢复（POC 产物保留）。
              </AlertDialogDescription>
            </AlertDialogHeader>
            {delErr && <p className="text-xs text-(--status-error)">{delErr}</p>}
            <AlertDialogFooter>
              <AlertDialogCancel asChild>
                <Button size="sm" variant="outline">取消</Button>
              </AlertDialogCancel>
              <Button size="sm" variant="destructive" disabled={deleting}
                      onClick={() => void doDelete()}>
                {deleting ? "删除中…" : "确认删除"}
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </DialogContent>
    </Dialog>
  )
}
