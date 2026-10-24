import { useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import { VULNERABILITY_TYPES, type Finding, type FindingPoc as FindingPocModel } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"
import { fmtDateTime, utcTitle } from "@/lib/datetime"
import { ChevronDown, ChevronUp, Copy, Image as ImageIcon, Plus, Trash2 } from "lucide-react"

// 发现详情弹窗（发现列表/黑板画布/攻击链画布三调用点共用）：收录格式三件套
// （危害描述/复现步骤/修复建议，v20）+ 复现文稿一键复制；F10 人工修订：编辑态
// （title/severity/vuln_class/impact/remediation，增量 patch）+ 删除入口（零打扰不触发撤回传播）。

const SEVERITY_COLOR: Record<string, string> = {
  critical: "text-(--status-error)",
  high: "text-(--status-error)",
  medium: "text-(--status-approval)",
  low: "text-muted-foreground",
  info: "text-muted-foreground",
}

// CTF flag 识别（2026-10-14）：扫 evidence 全部字符串值里的 `前缀{内容}` 形态
// （flag{…}/CTF{…}/DASCTF{…} 等自定义前缀），命中即返回首个。**不猜裸 hex**——
// evidence 里 sha256/md5 太多，裸串误报率高。零 schema 迁移（Nan 2026-10-14 定）。
//
// 2026-10-19 修：前缀改为**可选**。旧正则要求 flag/ctf 前至少 1 个字符，
// 导致 `ctf_submit_flag` 写入的裸 `flag{abc123}` / `FLAG{…}` / `ctf{…}` / `htb{…}`
// 在黑板 🚩 徽章与发现详情 FLAG 区块**全部不显示**（与 core/ctf/flagjudge.py
// 的候选口径不一致）。补 `htb` 系列；仍要求 `{` 前是 flag/ctf/htb 词尾、
// 内层非空且无空白，故 `we should flag{ something`、`sha256:deadbeef`、
// `%7Bflag%7D` 等不会误报。
const FLAG_RE = /(?:[A-Za-z][A-Za-z0-9_]{0,12})?(?:flag|ctf|htb)\{[^{}\s]{1,200}\}/i

export function extractFlag(evidence: Record<string, unknown> | undefined): string | null {
  if (!evidence) return null
  // 规范字段优先（`ctf_submit_flag` 落库写的就是 `evidence.flag`）：扫全部值时
  // `repro_steps[].expected` 之类也含同一个 flag，键序靠前会先命中，语义上应
  // 以显式字段为准。
  const canon = evidence.flag
  if (typeof canon === "string") {
    const hit = canon.match(FLAG_RE)
    if (hit) return hit[0]
  }
  const seen = new Set<unknown>()
  const walk = (v: unknown): string | null => {
    if (typeof v === "string") return v.match(FLAG_RE)?.[0] ?? null
    if (Array.isArray(v)) {
      for (const x of v) { const r = walk(x); if (r) return r }
      return null
    }
    if (v && typeof v === "object") {
      if (seen.has(v)) return null
      seen.add(v)
      for (const x of Object.values(v as Record<string, unknown>)) {
        const r = walk(x); if (r) return r
      }
    }
    return null
  }
  return walk(evidence)
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
export type FindingPoc = FindingPocModel
interface LegacyFindingPoc {
  name?: string; type?: string; http_raw?: string; artifact_id?: string
  target?: string; stability?: string; steps?: unknown
}

export function pocsOf(f: Finding): (FindingPoc | LegacyFindingPoc)[] {
  if (Array.isArray(f.pocs) && f.pocs.length) return f.pocs
  const ev = f.evidence as { poc?: unknown; pocs?: unknown } | undefined
  const list: LegacyFindingPoc[] = []
  if (Array.isArray(ev?.pocs)) {
    for (const p of ev.pocs) {
      if (p && typeof p === "object") list.push(p as LegacyFindingPoc)
    }
  }
  if (!list.length && ev?.poc && typeof ev.poc === "object") {
    list.push(ev.poc as LegacyFindingPoc)
  }
  return list
}

function editablePocsOf(f: Finding): FindingPocModel[] {
  return pocsOf(f).flatMap((p): FindingPocModel[] => {
    // image 型（截图 POC）：artifact_id 引用产物，无 code
    if (p.type === "image") {
      const art = typeof p.artifact_id === "string" ? p.artifact_id : ""
      return art ? [{ type: "image", code: "", artifact_id: art }] : []
    }
    const code = "code" in p && typeof p.code === "string" ? p.code
      : "http_raw" in p && typeof p.http_raw === "string" ? p.http_raw : ""
    if (!code.trim()) return []
    const type = p.type === "python" ? "python" : "http"
    return [{ type, code }]
  })
}

/** 收录格式三件套·复现步骤（v20）：结构化步骤，verified 门禁认可的证据形态 */
export interface ReproStep {
  desc: string // 步骤描述（必填）
  type?: string // http | python | cmd | image（旧值 http_raw/steps 宽容）
  code?: string // 报文/脚本/命令（image 步无 code，用 artifact_id 嵌图）
  expected?: string // 预期返回结果（复现自证锚点）
  artifact_id?: string // 引用产物（image=图片产物嵌图，其余文本产物取内容）
  stability?: string // 如 "3/3"（连续 3 次全部触发）
  target?: string
}

// 步骤代码块的围栏语言（cmd 渲染 bash 围栏；其余类型无围栏纯文本）
const FENCE: Record<string, string> = { http: "http", python: "python", cmd: "bash" }

// 旧 poc(s) → 等价步骤（expected 空；poc.steps 纯文字步骤逐条展开）。
// image 型跳过——截图 POC 在 POC 段直接嵌图渲染，进复现步骤会重复展示一份。
function legacyPocSteps(f: Finding): ReproStep[] {
  const steps: ReproStep[] = []
  for (const p of pocsOf(f)) {
    const legacyPoc = p as LegacyFindingPoc
    if (legacyPoc.type === "image") continue
    if (legacyPoc.http_raw || legacyPoc.artifact_id) {
      steps.push({
        desc: legacyPoc.name ?? "复现步骤",
        // 仅处理**存量**旧 POC 的 http_raw（这条数据形态在写入口已不再产生）。
        // 新写入的 repro_steps.type 由后端 `normalize_repro_type` 归一到
        // {http,python,cmd,image}，这里不需要也不应该再猜第二遍
        // （2026-10-23，VT-004：曾在前后端各判一次，容易漂移）。
        type: legacyPoc.type === "http_raw" ? "http" : legacyPoc.type,
        code: legacyPoc.http_raw,
        artifact_id: legacyPoc.artifact_id,
        stability: legacyPoc.stability,
        target: legacyPoc.target,
      })
    }
    const legacy = legacyPoc.steps
    if (Array.isArray(legacy)) {
      for (const s of legacy) steps.push({ desc: String(s) })
    }
  }
  return steps
}

/** 统一复现步骤视图：evidence.repro_steps 优先（一等结构），旧 POC 映射等价步骤；
 * 两者并存（并集合并可能同现）时旧 POC 步骤追加在后。 */
export function reproStepsOf(f: Finding): ReproStep[] {
  const rs = (f.evidence as { repro_steps?: unknown } | undefined)?.repro_steps
  if (Array.isArray(rs)) {
    const steps = (rs as unknown[]).filter(
      (s): s is ReproStep => !!s && typeof s === "object")
    return [...steps, ...legacyPocSteps(f)]
  }
  return legacyPocSteps(f)
}

/** 画布 POC 角标用：有没有任意复现证据（新结构步骤 / 旧 POC / 截图 POC / 产物引用） */
export function hasPoc(f: Finding): boolean {
  return reproStepsOf(f).length > 0 || !!f.poc_artifact_id
    || pocsOf(f).some((p) => p.type === "image" && !!p.artifact_id)
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

function reproText(f: Finding, steps: ReproStep[],
                   scripts: Record<string, string | undefined>): string {
  // 三节模板文稿（收录格式定稿）：危害描述/复现步骤/修复建议，一键复制即可照做交付
  const lines = [`# ${f.title}  [${f.severity}/${f.status}]`, "", "## 危害描述", "",
                 (f.impact ?? "").trim() || "（待补充）", "", "## 复现步骤"]
  if (!steps.length) lines.push("", "（待补充）")
  steps.forEach((s, i) => {
    lines.push("", `${i + 1}. ${s.desc || "（无描述）"}`)
    const fence = FENCE[s.type ?? ""]
    if (s.code?.trim()) {
      if (fence) {
        lines.push(`   \`\`\`${fence}`,
                   ...s.code.split("\n").map((l) => `   ${l}`), "   ```")
      } else {
        lines.push(...s.code.split("\n").map((l) => `   ${l}`))
      }
    }
    if (s.type === "image" && s.artifact_id) {
      lines.push(`   （截图产物: ${s.artifact_id}）`)
    } else if (s.artifact_id && scripts[s.artifact_id]) {
      lines.push(...scripts[s.artifact_id]!.split("\n").map((l) => `   ${l}`))
    }
    if ((s.expected ?? "").trim()) lines.push(`   预期返回结果：${s.expected}`)
    if (s.stability) lines.push(`   稳定性：${s.stability}`)
    if (s.target) lines.push(`   目标：${s.target}`)
  })
  if (f.poc_artifact_id) lines.push("", `主 POC 产物: ${f.poc_artifact_id}`)
  lines.push("", "## 修复建议", "", (f.remediation ?? "").trim() || "（待补充）")
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

// 复现步骤单行（收录格式 v20）：desc + 围栏代码块（image 步嵌图）+ 预期返回结果
function StepRow({ pid, step, n, scripts }:
                   { pid: string; step: ReproStep; n: number;
                     scripts: Record<string, { loading: boolean; content?: string; path?: string; error?: string }> }) {
  const isImg = step.type === "image"
  return (
    <li className="rounded border p-2">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-foreground">{n}. {step.desc || "（无描述）"}</span>
        {step.stability && <Badge variant="outline" className="text-[10px]">稳定性 {step.stability}</Badge>}
        {step.target && <span className="truncate font-mono text-[10px] text-muted-foreground">{step.target}</span>}
      </div>
      {isImg && step.artifact_id && (
        <img src={`/api/projects/${pid}/artifacts/${step.artifact_id}/download`}
             alt={step.desc} loading="lazy"
             className="max-h-64 rounded border bg-muted/20" />
      )}
      {!isImg && !!step.code?.trim() && (
        <div className="relative">
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2 pr-14 font-mono text-[10px]">
            {step.code}
          </pre>
          <div className="absolute right-1 top-1">
            <CopyButton text={step.code} />
          </div>
        </div>
      )}
      {!isImg && !step.code?.trim() && step.artifact_id && (
        <ScriptBlock ref_={step.artifact_id} scripts={scripts} />
      )}
      {!!step.expected?.trim() && (
        <p className="mt-1 text-[10px] text-muted-foreground">
          <span className="text-foreground">预期返回结果</span> · {step.expected}
        </p>
      )}
    </li>
  )
}

const selectCls =
  "h-8 rounded-md border bg-background px-1.5 text-xs"

export function FindingDetailDialog({ pid, finding, assetLabel, track, onClose, onMutated }:
                             { pid: string; finding: Finding; assetLabel?: string; track?: string; onClose: () => void; onMutated?: () => void }) {
  // 逆向轨（research）的发现是静态还原结论（端点/结构/密钥…），没有可复现的
  // 资产、POC 与修复面 → 详情只留「漏洞摘要 + 原始证据」（2026-10-13 用户要求）。
  const isReverse = track === "research"
  // CTF 轨（2026-10-14 用户定稿）：发现是**取证/解题结论**而非渗透漏洞——「受影响
  // 资产/测试环境/验证结果/风险影响评估」这些渗透口径字段 Agent 根本不填（实测全空），
  // 按轨改名 + 隐藏不适用段，只留「结论 / 解题步骤 / 解题脚本 / 备注 / 关键证据」，
  // 并自 evidence 里识别 flag 显眼展示（零 schema 迁移）。
  const isCtf = track === "ctf"
  // F10：row 本地快照（初始=prop，保存成功用 PATCH 响应覆盖）——解耦 4s 轮询且头部即时回显
  const [row, setRow] = useState<Finding>(finding)
  // flag 识别须基于 row（编辑保存后即时刷新）；CTF 轨才扫 evidence
  const flag = isCtf ? extractFlag(row.evidence) : null
  // 「已解出」= 发现本身 verified（`ctf_submit_flag` 正式路径写 verified；
  // candidate=true 写 unverified/intel），用于区分 FLAG / 候选 FLAG 两种呈现。
  const flagVerified = row.status === "verified"
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState({ title: "", severity: "", status: "unverified", vuln_class: "", rating_basis: "",
                                     impact: "", remediation: "", category: "vuln",
                                     summary: "", affected_assets: "", test_environment: "",
                                     reproduction_steps: "", verification_result: "",
                                     risk_assessment: "", pocs: [] as FindingPocModel[] })
  const [saving, setSaving] = useState(false)
  // 截图 POC 上传中标记（按 pocs 下标）
  const [pocUploading, setPocUploading] = useState<Record<number, boolean>>({})
  const [editErr, setEditErr] = useState<string | null>(null)
  const [delOpen, setDelOpen] = useState(false)
  const [delErr, setDelErr] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  // 统一复现步骤视图（v20 一等结构优先，旧 POC 映射等价步骤）
  const steps = reproStepsOf(row)
  const entries = Object.entries(row.evidence ?? {})
    .filter(([k]) => k !== "poc" && k !== "pocs" && k !== "repro_steps")

  const startEdit = () => {
    setForm({ title: row.title, severity: row.severity, status: row.status,
              vuln_class: row.vuln_class ?? "",
              rating_basis: row.rating_basis ?? "", impact: row.impact ?? "",
              remediation: row.remediation ?? "", category: row.category ?? "vuln",
              summary: row.summary ?? "", affected_assets: row.affected_assets ?? "",
              test_environment: row.test_environment ?? "",
              reproduction_steps: row.reproduction_steps ?? "",
              verification_result: row.verification_result ?? "",
              risk_assessment: row.risk_assessment ?? "",
              pocs: editablePocsOf(row) })
    setEditErr(null)
    setEditing(true)
  }
  const save = async () => {
    // 增量 patch：只发变化字段，无变化不发请求
    const changes: Record<string, unknown> = {}
    if (form.title !== row.title) changes.title = form.title
    if (form.severity !== row.severity) changes.severity = form.severity
    if (form.status !== row.status) changes.status = form.status
    if (form.vuln_class !== (row.vuln_class ?? "")) changes.vuln_class = form.vuln_class
    // F11 判级依据：空串=清空（basis 必须证成当前 severity）
    if (form.rating_basis !== (row.rating_basis ?? "")) changes.rating_basis = form.rating_basis
    // 收录格式三件套：危害描述/修复建议（空串=清空，不进门禁）
    if (form.impact !== (row.impact ?? "")) changes.impact = form.impact
    if (form.remediation !== (row.remediation ?? "")) changes.remediation = form.remediation
    // C6 分两类：vuln=漏洞 / intel=有效发现·关键发现
    if (form.category !== (row.category ?? "vuln")) changes.category = form.category
    for (const key of ["summary", "affected_assets", "test_environment",
      "reproduction_steps", "verification_result", "risk_assessment"] as const) {
      if (form[key] !== ((row[key] as string | undefined) ?? "")) changes[key] = form[key]
    }
    // POC 比对归一化：image 项 code 恒为空串、http/python 无 artifact_id——
    // 表单模型与落库形态键序/空字段不同，直接 JSON.stringify 会永远判「有变化」
    const normPoc = (p: FindingPocModel) => p.type === "image"
      ? { type: p.type, artifact_id: p.artifact_id ?? "" }
      : { type: p.type, code: p.code }
    const normPocs = (ps: FindingPocModel[]) => JSON.stringify(ps.map(normPoc))
    if (normPocs(form.pocs) !== normPocs(row.pocs ?? [])) changes.pocs = form.pocs
    if (form.status === "verified" && row.status !== "verified") {
      for (const key of ["summary", "affected_assets", "test_environment",
        "reproduction_steps", "verification_result", "risk_assessment"] as const) {
        changes[key] = form[key]
      }
      changes.pocs = form.pocs
    }
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
    if (isReverse) return []   // 逆向轨不渲染复现手册/POC，脚本内容无需预取
    const refs = steps
      .filter((s) => s.artifact_id && s.type !== "image")  // image 步走 <img> 直连下载端点
      .map((s) => s.artifact_id!)
    if (legacyRef && !refs.includes(legacyRef)) refs.push(legacyRef)
    return [...new Set(refs)]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.id, isReverse])
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

  const scriptContents: Record<string, string | undefined> = Object.fromEntries(
    Object.entries(scripts).map(([r, s]) => [r, s.content]))
  const fullText = reproText(row, steps, scriptContents)

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
          <div className="min-h-0 flex-1 space-y-2 overflow-y-auto rounded border p-2">
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">漏洞名称</label>
              <input className="h-8 w-full rounded-md border bg-background px-2 text-xs"
                     placeholder="漏洞类型 + 受影响资产 + 风险能力"
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
                <label className="mb-0.5 block text-[10px] text-muted-foreground">漏洞类型</label>
                <select className={cn(selectCls, "w-full")} value={form.vuln_class}
                        onChange={(e) => setForm((f) => ({ ...f, vuln_class: e.target.value }))}>
                  {form.vuln_class && !(VULNERABILITY_TYPES as readonly string[]).includes(form.vuln_class)
                    && <option value={form.vuln_class}>{form.vuln_class}（存量类型）</option>}
                  {VULNERABILITY_TYPES.map((v) => <option key={v} value={v}>{v}</option>)}
                </select>
              </div>
            </div>
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">状态</label>
              <select className={cn(selectCls, "w-full")} value={form.status}
                      onChange={(e) => setForm((f) => ({ ...f, status: e.target.value }))}>
                <option value="unverified">草稿 / 待验证</option>
                <option value="verified">正式漏洞</option>
                <option value="false-positive">误报</option>
              </select>
            </div>
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">记录分类</label>
              <select className={cn(selectCls, "w-full")} value={form.category}
                      onChange={(e) => setForm((f) => ({ ...f, category: e.target.value }))}>
                <option value="vuln">漏洞</option>
                <option value="intel">有效发现 / 情报</option>
              </select>
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
              <label className="mb-0.5 block text-[10px] text-muted-foreground">{isCtf ? "结论" : "漏洞摘要"}</label>
              <textarea className="min-h-16 w-full rounded-md border bg-background px-2 py-1.5 text-xs"
                        value={form.summary}
                        onChange={(e) => setForm((f) => ({ ...f, summary: e.target.value }))} />
            </div>
            {!isCtf && (
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">受影响资产说明</label>
              <textarea className="min-h-16 w-full rounded-md border bg-background px-2 py-1.5 text-xs"
                        value={form.affected_assets}
                        onChange={(e) => setForm((f) => ({ ...f, affected_assets: e.target.value }))} />
            </div>
            )}
            {(isCtf
              ? ([["reproduction_steps", "解题步骤"]] as const)
              : ([
                  ["test_environment", "测试环境"],
                  ["reproduction_steps", "操作步骤"],
                  ["verification_result", "验证结果（预期结果与实际结果）"],
                  ["risk_assessment", "风险影响评估"],
                ] as const)
            ).map(([key, label]) => (
              <div key={key}>
                <label className="mb-0.5 block text-[10px] text-muted-foreground">{label}</label>
                <textarea className="min-h-16 w-full rounded-md border bg-background px-2 py-1.5 text-xs"
                          value={form[key]}
                          onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))} />
              </div>
            ))}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-[10px] text-muted-foreground">{isCtf ? "解题脚本（Python / 命令）" : "POC（HTTP 原始报文 / Python / 图片）"}</label>
                <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-[10px]"
                        onClick={() => setForm((f) => ({ ...f, pocs: [...f.pocs, { type: "http", code: "" }] }))}>
                  <Plus className="size-3" /> 添加 POC
                </Button>
              </div>
              {form.pocs.map((poc, index) => (
                <div key={index} className="space-y-1 rounded border p-2">
                  <div className="flex items-center gap-1">
                    <select aria-label={`POC ${index + 1} 类型`} className={selectCls} value={poc.type}
                            onChange={(e) => setForm((f) => ({
                              ...f, pocs: f.pocs.map((p, i) => {
                                if (i !== index) return p
                                // 切 image 清 code、切走 image 清 artifact_id——
                                // 两形态字段互不残留，落库校验口径更干净
                                const t = e.target.value as FindingPocModel["type"]
                                return t === "image"
                                  ? { type: t, code: "", artifact_id: p.artifact_id }
                                  : { type: t, code: p.code, artifact_id: undefined }
                              }),
                            }))}>
                      <option value="http">HTTP</option><option value="python">Python</option>
                      <option value="image">图片</option>
                    </select>
                    <Button size="icon" variant="ghost" className="size-7" title="上移" aria-label="上移 POC"
                            disabled={index === 0}
                            onClick={() => setForm((f) => {
                              const next = [...f.pocs]; [next[index - 1], next[index]] = [next[index], next[index - 1]]
                              return { ...f, pocs: next }
                            })}><ChevronUp className="size-3.5" /></Button>
                    <Button size="icon" variant="ghost" className="size-7" title="下移" aria-label="下移 POC"
                            disabled={index === form.pocs.length - 1}
                            onClick={() => setForm((f) => {
                              const next = [...f.pocs]; [next[index + 1], next[index]] = [next[index], next[index + 1]]
                              return { ...f, pocs: next }
                            })}><ChevronDown className="size-3.5" /></Button>
                    <Button size="icon" variant="ghost" className="size-7 text-(--status-error)" title="删除" aria-label="删除 POC"
                            onClick={() => setForm((f) => ({ ...f, pocs: f.pocs.filter((_, i) => i !== index) }))}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                  {poc.type === "image" ? (
                    poc.artifact_id ? (
                      <div className="flex items-center gap-2">
                        <img src={`/api/projects/${pid}/artifacts/${poc.artifact_id}/download`}
                             alt="POC 截图" loading="lazy" className="max-h-24 rounded border bg-muted/20" />
                        <div className="min-w-0 flex-1 space-y-1">
                          <p className="truncate font-mono text-[10px] text-muted-foreground" title={poc.artifact_id}>{poc.artifact_id}</p>
                          <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]"
                                  onClick={() => setForm((f) => ({
                                    ...f, pocs: f.pocs.map((p, i) => i === index
                                      ? { ...p, artifact_id: undefined } : p),
                                  }))}>移除图片</Button>
                        </div>
                      </div>
                    ) : (
                      <label className={cn("flex h-28 w-full cursor-pointer flex-col items-center justify-center gap-1 rounded border border-dashed text-[10px] text-muted-foreground", pocUploading[index] && "opacity-60")}>
                        <ImageIcon className="size-4" />
                        {pocUploading[index] ? "上传中…" : "点击上传截图（≤64MB，同内容服务端去重）"}
                        <input type="file" accept="image/*" className="hidden" disabled={pocUploading[index]}
                               onChange={(e) => {
                                 const file = e.target.files?.[0]
                                 e.target.value = ""
                                 if (!file) return
                                 setPocUploading((m) => ({ ...m, [index]: true }))
                                 api.uploadAttachment(pid, file)
                                   .then((r) => setForm((f) => ({
                                     ...f, pocs: f.pocs.map((p, i) => i === index
                                       ? { ...p, artifact_id: r.id } : p),
                                   })))
                                   .catch((err) => setEditErr(String(err)))
                                   .finally(() => setPocUploading((m) => ({ ...m, [index]: false })))
                               }} />
                      </label>
                    )
                  ) : (
                    <textarea className="min-h-28 w-full rounded-md border bg-background px-2 py-1.5 font-mono text-[11px]"
                              placeholder={poc.type === "http" ? "POST /api/... HTTP/1.1\nHost: example.com" : "import requests\n..."}
                              value={poc.code}
                              onChange={(e) => setForm((f) => ({
                                ...f, pocs: f.pocs.map((p, i) => i === index ? { ...p, code: e.target.value } : p),
                              }))} />
                  )}
                </div>
              ))}
            </div>
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">{isCtf ? "备注（选填）" : "修复建议（选填）"}</label>
              <textarea className="min-h-16 w-full rounded-md border bg-background px-2 py-1.5 text-xs"
                        value={form.remediation}
                        onChange={(e) => setForm((f) => ({ ...f, remediation: e.target.value }))} />
            </div>
            <div>
              <label className="mb-0.5 block text-[10px] text-muted-foreground">危害描述（兼容旧字段）</label>
              <textarea className="min-h-16 w-full rounded-md border bg-background px-2 py-1.5 text-xs"
                        value={form.impact}
                        onChange={(e) => setForm((f) => ({ ...f, impact: e.target.value }))} />
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
              <span className="text-[10px] text-muted-foreground">
                {isCtf ? "线索类发现不强制报告字段；verified 须有证据（repro_steps / evidence）" : "正式漏洞须补齐必填报告字段和至少一个 POC"}
              </span>
            </div>
          </div>
        )}

        {/* 编辑态只渲染表单（自身即滚动容器，保存按钮可达）；非编辑态渲染只读详情 */}
        {!editing && (
        <div className="-mx-1 min-h-0 flex-1 space-y-3 overflow-y-auto px-1">
          {flag && (
            // 候选 flag（unverified，`ctf_submit_flag candidate=true`）用警示色 + 「候选」
            // 标签，别让「疑似线索」和「已解出」长得一样（2026-10-19）。
            <section className={cn("rounded px-2.5 py-1.5",
              flagVerified
                ? "border border-(--viz-success-border) bg-(--viz-success-bg)"
                : "border border-(--status-approval)/40 bg-(--status-approval)/10")}>
              <div className="flex items-center gap-2">
                <h4 className={cn("shrink-0 text-[10px] font-medium",
                  flagVerified ? "text-(--viz-success)" : "text-(--status-approval)")}>
                  {flagVerified ? "FLAG" : "候选 FLAG"}
                </h4>
                <code className="min-w-0 flex-1 break-all font-mono text-xs text-foreground">{flag}</code>
                <CopyButton text={flag} label="复制" />
              </div>
              {!flagVerified && (
                <p className="mt-0.5 text-[10px] text-muted-foreground">
                  未验证线索（可能是管理员口令/密钥，尚不构成「本题解出」）
                </p>
              )}
            </section>
          )}
          <section>
            <h4 className="mb-1 text-[11px] font-medium text-foreground">{isCtf ? "结论" : "漏洞摘要"}</h4>
            <p className="whitespace-pre-wrap text-xs text-foreground">{row.summary || "（待补充）"}</p>
          </section>
          {/* 逆向轨（research）：发现多为静态还原结论（端点/结构/密钥等），没有可
              复现的资产、POC 与修复面——受影响资产/复现手册/POC/风险评估/修复建议
              五段整体不渲染，只留摘要 + 原始证据（2026-10-13 用户要求）。
              CTF 轨（2026-10-14）：同理隐藏「受影响资产 / 测试环境 / 验证结果 / 风险
              影响评估」（渗透口径，Agent 实测全空），并把复现手册→解题步骤、
              POC→解题脚本、修复建议→备注。 */}
          {!isReverse && (
          <>
          {!isCtf && (
          <section>
            <h4 className="mb-1 text-[11px] font-medium text-foreground">受影响资产</h4>
            <p className="whitespace-pre-wrap text-xs text-foreground">
              {row.affected_assets || assetLabel || "（待补充）"}
            </p>
          </section>
          )}
          <section>
            <div className="mb-1 flex items-center justify-between">
              <h4 className="text-[11px] font-medium text-foreground">{isCtf ? "解题步骤" : "复现手册"}</h4>
              <CopyButton text={fullText} label={isCtf ? "复制解题步骤" : "复制复现文稿"} />
            </div>
            {/* CTF 轨只留「操作步骤」单段（测试环境/验证结果不适用，不渲染空壳）；
                渗透轨保留三件套。结构化步骤（evidence.repro_steps）两轨共用下方渲染。 */}
            {isCtf ? (
              row.reproduction_steps
                ? <p className="whitespace-pre-wrap text-xs text-muted-foreground">{row.reproduction_steps}</p>
                : null
            ) : (
            <div className="space-y-2 text-xs">
              <div><span className="font-medium">测试环境</span>
                <p className="whitespace-pre-wrap text-muted-foreground">{row.test_environment || "（待补充）"}</p></div>
              <div><span className="font-medium">操作步骤</span>
                <p className="whitespace-pre-wrap text-muted-foreground">{row.reproduction_steps || "（待补充）"}</p></div>
              <div><span className="font-medium">验证结果</span>
                <p className="whitespace-pre-wrap text-muted-foreground">{row.verification_result || "（待补充）"}</p></div>
            </div>
            )}
            {steps.length > 0 && (
              <ol className="mt-2 space-y-2">
                {steps.map((s, i) => <StepRow key={i} pid={pid} step={s} n={i + 1} scripts={scripts} />)}
              </ol>
            )}
            {steps.length === 0 && legacyRef && <ScriptBlock ref_={legacyRef} scripts={scripts} />}
          </section>

          {/* 结构化 POC 优先；老记录继续展示旧证据步骤 */}
          <section>
            <h4 className="mb-1 text-[11px] font-medium text-foreground">{isCtf ? "解题脚本" : "POC"}</h4>
            {pocsOf(row).some((p) => ("code" in p && typeof p.code === "string" && p.code.trim())
              || (p.type === "image" && !!p.artifact_id)) ? (
              <div className="space-y-2">
                {pocsOf(row).map((p, index) => {
                  const code = "code" in p && typeof p.code === "string" ? p.code : ""
                  const art = p.type === "image" && typeof p.artifact_id === "string" ? p.artifact_id : ""
                  if (!code.trim() && !art) return null
                  if (!code.trim() && art) {
                    return <div key={index} className="overflow-hidden rounded border">
                      <div className="border-b px-2 py-1 text-[10px] text-muted-foreground">图片 · POC {index + 1}</div>
                      <img src={`/api/projects/${pid}/artifacts/${art}/download`}
                           alt={`POC ${index + 1}`} loading="lazy"
                           className="max-h-64 w-auto bg-muted/20" />
                    </div>
                  }
                  const type = p.type === "python" ? "Python" : "HTTP"
                  return <div key={index} className="overflow-hidden rounded border">
                    <div className="flex items-center justify-between border-b px-2 py-1 text-[10px] text-muted-foreground">
                      <span>{type} · POC {index + 1}</span>
                      <Button size="sm" variant="ghost" className="h-6 gap-1 px-1.5 text-[10px]"
                              onClick={() => void navigator.clipboard.writeText(code)}>
                        <Copy className="size-3" />复制
                      </Button>
                    </div>
                    <pre className="max-h-56 overflow-auto whitespace-pre-wrap p-2 font-mono text-[10px]">{code}</pre>
                  </div>
                })}
              </div>
            ) : <p className="text-xs text-muted-foreground">（无内嵌 POC）</p>}
          </section>

          {!isCtf && (
          <section>
            <h4 className="mb-1 text-[11px] font-medium text-foreground">风险影响评估</h4>
            <p className="whitespace-pre-wrap text-xs text-foreground">{row.risk_assessment || "（待补充）"}</p>
          </section>
          )}

          {/* 修复建议 */}
          <section>
            <h4 className="mb-1 text-[11px] font-medium text-foreground">{isCtf ? "备注" : "修复建议"}</h4>
            {(row.remediation ?? "").trim() ? (
              <p className="whitespace-pre-wrap text-xs text-foreground">{row.remediation}</p>
            ) : (
              <p className="text-xs text-muted-foreground">（待补充）</p>
            )}
          </section>
          </>
          )}
          {entries.length > 0 && (
            <details className="rounded border px-2 py-1.5">
              <summary className="cursor-pointer text-[10px] text-muted-foreground">原始证据</summary>
              <div className="mt-2 space-y-1.5">
                {entries.map(([k, v]) => {
                  const text = typeof v === "string" ? v : JSON.stringify(v, null, 2)
                  return <div key={k} className="rounded border p-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[10px] text-muted-foreground">{k}</span>
                      <CopyButton text={text} />
                    </div>
                    <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[10px]">{text}</pre>
                  </div>
                })}
              </div>
            </details>
          )}
        </div>
        )}

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
