import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { KbRenameResult } from "@/lib/types"
import { Textarea } from "@/components/ui/textarea"

/** 技能归属来源：能力包 / 场景轨 */
export type SkillSource = "cap" | "track"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"

// 新建专家（expert-pool M3）与新建技能（薄路由向导）对话框。专家 slug 只允许小写
// 字母数字与 -（ASCII 文件名/命令引用面），服务端同口径 422 + 重名 409，错误直接展示 detail。

const SLUG_HINT = "英文 slug：字母/数字/_-/.，1-64 字符（如 web-recon）"
const EXPERT_ID_HINT = "小写字母/数字开头，仅小写字母数字与 -，≤48 字符（如 web-solver）"

function ErrorLine({ err }: { err: string | null }) {
  if (!err) return null
  return <p className="break-all text-[11px] text-(--status-error)">{err}</p>
}

export function ExpertCreateDialog({ open, onOpenChange, onCreated }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onCreated: (id: string) => void
}) {
  const [id, setId] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (open) { setId(""); setDisplayName(""); setErr(null); setBusy(false) }
  }, [open])

  const submit = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.createExpert(id.trim(), { name: displayName.trim() || null })
      onCreated(id.trim())
      onOpenChange(false)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>新建专家</DialogTitle>
        <DialogDescription>packs/experts/ 下新增一个专家 yaml；其余字段建后在校对表单补齐，删除进回收站可恢复。</DialogDescription>
        <Input value={id} onChange={(e) => setId(e.target.value)} placeholder="专家 id，如 web-solver"
               className="font-mono text-xs" autoFocus
               onKeyDown={(e) => e.key === "Enter" && id.trim() && submit()} />
        <p className="-mt-1 text-[10px] text-muted-foreground">{EXPERT_ID_HINT}</p>
        <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)}
               placeholder="显示名（可中文，留空 = 用 id）" className="text-xs"
               onKeyDown={(e) => e.key === "Enter" && id.trim() && submit()} />
        <ErrorLine err={err} />
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={busy || !id.trim()} onClick={submit}>
            {busy ? "创建中…" : "创建"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export function SkillCreateDialog({ open, onOpenChange, source, packName, onCreated }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  source: SkillSource
  packName: string
  onCreated: (name: string) => void
}) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [keywords, setKeywords] = useState("")
  const [fileFeatures, setFileFeatures] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setName(""); setDescription(""); setKeywords(""); setFileFeatures("")
      setErr(null); setBusy(false)
    }
  }, [open])

  const toList = (s: string) => s.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean)

  const submit = async () => {
    setBusy(true)
    setErr(null)
    const body = {
      name: name.trim(),
      description: description.trim(),
      keywords: toList(keywords),
      file_features: toList(fileFeatures),
    }
    try {
      const create = source === "cap" ? api.createCapSkill : api.createTrackSkill
      await create(packName, body)
      onCreated(name.trim())
      onOpenChange(false)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>新建中文薄路由技能</DialogTitle>
        <DialogDescription>
          {source === "cap" ? `capabilities/${packName}/skills/` : `tracks/${packName}/skills/`}
          下生成 SKILL.md 模板（特征→kb_open 对照表骨架）。
        </DialogDescription>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="技能 slug，如 web-strike-entry"
               className="font-mono text-xs" autoFocus />
        <p className="-mt-1 text-[10px] text-muted-foreground">{SLUG_HINT}</p>
        <Input value={description} onChange={(e) => setDescription(e.target.value)}
               placeholder="一句话描述（路由 description×1 加权）" className="text-xs" />
        <Input value={keywords} onChange={(e) => setKeywords(e.target.value)}
               placeholder="关键词 keywords，逗号/空格分隔（×2 加权）" className="font-mono text-xs" />
        <Input value={fileFeatures} onChange={(e) => setFileFeatures(e.target.value)}
               placeholder="文件特征 file_features，如 NX, Canary, PIE（×3 加权）"
               className="font-mono text-xs"
               onKeyDown={(e) => e.key === "Enter" && name.trim() && submit()} />
        <ErrorLine err={err} />
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={busy || !name.trim()} onClick={submit}>
            {busy ? "创建中…" : "创建"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// ---------- kb 本地基线：新建 md / 改名联动（C2/C3） ----------

const KB_PATH_HINT = "源内相对路径，仅 .md；允许中文多级目录，如 ctf-web/新手法/绕过.md（1 MiB 内）"

export function KbCreateDialog({ open, onOpenChange, cap, onCreated }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  cap: string
  onCreated: (path: string) => void
}) {
  const [path, setPath] = useState("")
  const [content, setContent] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setPath(""); setContent(""); setErr(null); setBusy(false)
    }
  }, [open])

  const submit = async () => {
    const p = path.trim()
    const body = content.trim() || `# ${p.split("/").pop()?.replace(/\.md$/, "") ?? "新文档"}\n\n`
    setBusy(true)
    setErr(null)
    try {
      await api.kbCreate(cap, p, body)
      onCreated(p)
      onOpenChange(false)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>新建知识库文档</DialogTitle>
        <DialogDescription>
          capabilities/{cap}/kb/ 下新增 .md。英文上游快照原文不翻译不覆盖——新经验写新文件。
        </DialogDescription>
        <Input value={path} onChange={(e) => setPath(e.target.value)}
               placeholder="如 ctf-web/sqli/新绕过手法.md"
               className="font-mono text-xs" autoFocus
               onKeyDown={(e) => e.key === "Enter" && path.trim() && submit()} />
        <p className="-mt-1 text-[10px] text-muted-foreground">{KB_PATH_HINT}</p>
        <Textarea value={content} onChange={(e) => setContent(e.target.value)}
                  className="min-h-40 font-mono text-[11px]" spellCheck={false}
                  placeholder="初始内容（留空=标题骨架；新建后在中栏继续编辑）" />
        <ErrorLine err={err} />
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={busy || !path.trim()} onClick={submit}>
            {busy ? "创建中…" : "创建"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export function KbRenameDialog({ open, onOpenChange, cap, path, onRenamed }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  cap: string
  path: string | null
  onRenamed: (r: KbRenameResult) => void
}) {
  const [newPath, setNewPath] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [result, setResult] = useState<KbRenameResult | null>(null)

  useEffect(() => {
    if (open) { setNewPath(path ?? ""); setErr(null); setBusy(false); setResult(null) }
  }, [open, path])

  const submit = async () => {
    if (!path) return
    setBusy(true)
    setErr(null)
    try {
      const r = await api.kbRename(cap, path, newPath.trim())
      setResult(r)
      onRenamed(r)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>知识库文件改名 / 移动</DialogTitle>
        <DialogDescription className="font-mono text-[10px]">
          {path} → 同源内新路径；全仓引用完整路径自动替换（各文件先备份）。
        </DialogDescription>
        <Input value={newPath} onChange={(e) => setNewPath(e.target.value)}
               className="font-mono text-xs" autoFocus
               onKeyDown={(e) => e.key === "Enter" && newPath.trim() && newPath.trim() !== path && submit()} />
        <p className="-mt-1 text-[10px] text-muted-foreground">{KB_PATH_HINT}；跨知识库源不支持。</p>
        {result && (
          <div className="rounded border bg-card/40 p-2 text-[11px]">
            <p className="text-primary">已移动并联动 {result.updated.length} 个文件：</p>
            {result.updated.map((u) => (
              <p key={u.file} className="truncate font-mono text-[10px] text-muted-foreground" title={u.file}>
                {u.file}（替换 {u.replacements} 处）
              </p>
            ))}
            {result.skipped_relative.length > 0 && (
              <p className="mt-1 text-[10px] text-(--status-approval)">
                {result.skipped_relative.length} 个文件只用相对链接引用，无法安全自动替换，请手工检查：
                {result.skipped_relative.slice(0, 5).join("、")}
                {result.skipped_relative.length > 5 ? " …" : ""}
              </p>
            )}
          </div>
        )}
        <ErrorLine err={err} />
        <div className="flex justify-end gap-2">
          {result
            ? <Button size="sm" onClick={() => onOpenChange(false)}>完成</Button>
            : <>
              <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
              <Button size="sm" disabled={busy || !newPath.trim() || newPath.trim() === path}
                      onClick={submit}>{busy ? "改名中…" : "改名并联动"}</Button>
            </>}
        </div>
      </DialogContent>
    </Dialog>
  )
}
