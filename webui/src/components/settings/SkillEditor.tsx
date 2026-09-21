import { forwardRef, useEffect, useImperativeHandle, useState } from "react"
import type { SkillDetail, SkillVocab } from "@/lib/types"
import {
  parseSkill, serializeSkill, splitList,
  SKILL_LIST_FIELDS, type SkillFormState, type SkillListField,
} from "@/lib/skillfm"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ScrollArea } from "@/components/ui/scroll-area"
import { MarkdownView } from "./MarkdownView"
import { cn } from "@/lib/utils"

// 表单+源码混合编辑器：frontmatter 表单默认折叠（七字段带全仓 vocab 补全），
// 下半 Markdown 正文（编辑/预览切换，字号 13px）。
// 保存时由父组件调 ref.getRaw() 取合并后的完整 SKILL.md；未托管 frontmatter 行原样保留。

export interface SkillEditorHandle {
  getRaw: () => string
}

export const SKILL_MD_PREFIX = "skill"

const FIELD_LABELS: Record<SkillListField, string> = {
  keywords: "关键词 keywords（×2）",
  features: "行为特征 features（×3）",
  file_features: "文件特征 file_features（×3）",
  platforms: "平台标签 platforms（×3）",
  formats: "格式标签 formats（×3）",
  vuln_classes: "漏洞类 vuln_classes（×3）",
  task_types: "任务类型 task_types",
}

function emptyForm(): SkillFormState {
  return {
    description: "",
    lists: Object.fromEntries(SKILL_LIST_FIELDS.map((f) => [f, ""])) as Record<SkillListField, string>,
    body: "",
  }
}

export const SkillEditor = forwardRef<SkillEditorHandle, {
  detail: SkillDetail
  vocab: SkillVocab | null
  onDirtyChange: (dirty: boolean) => void
  /** 初始/重置后的正文模式：深链跳转（直播间路由名/doctor）传 "preview"（F12 配套） */
  startMode?: "edit" | "preview"
  /** 变化时强制重置一次（同一技能重复深链跳转时 detail 不变、effect 不触发，靠它驱动） */
  resetKey?: unknown
}>(function SkillEditor({ detail, vocab, onDirtyChange, startMode, resetKey }, ref) {
  const [form, setForm] = useState<SkillFormState>(emptyForm)
  const [passthrough, setPassthrough] = useState<string[]>([])
  const [name, setName] = useState("")
  const [formOpen, setFormOpen] = useState(false)
  const [bodyMode, setBodyMode] = useState<"edit" | "preview">("edit")

  useEffect(() => {
    const parsed = parseSkill(detail.raw)
    setForm(parsed.hasFm ? parsed.form : { ...emptyForm(), body: detail.raw })
    setPassthrough(parsed.passthrough)
    setName(parsed.hasFm ? parsed.name : detail.name)
    setFormOpen(false)
    setBodyMode(startMode ?? "edit")
    onDirtyChange(false)
    // 仅在切换技能/重载/深链重置时重置（detail.raw 在保存后也会变，正好复位 dirty）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.name, detail.raw, resetKey])

  useImperativeHandle(ref, () => ({
    getRaw: () => {
      if (!parseSkill(detail.raw).hasFm) return form.body // 无 frontmatter 的异常文件不强行包壳
      return serializeSkill(name, form, passthrough)
    },
  }), [detail.raw, form, passthrough, name])

  const setList = (key: SkillListField, v: string) => {
    setForm((f) => ({ ...f, lists: { ...f.lists, [key]: v } }))
    onDirtyChange(true)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1.5">
      {/* frontmatter 表单：默认折叠；七字段 datalist 走全仓 vocab（值后标使用计数） */}
      <div className="shrink-0 rounded border bg-card/30">
        <button className="flex w-full items-center gap-1 px-2 py-1 text-[10px] text-muted-foreground hover:text-primary"
                onClick={() => setFormOpen((v) => !v)}>
          <span className="w-3">{formOpen ? "▾" : "▸"}</span>
          frontmatter 表单（路由依据；name 不可改，其余标签字段带全仓词表补全）
        </button>
        {formOpen && (
          <div className="border-t p-2">
            <label className="text-[10px] text-muted-foreground">描述 description（×1）</label>
            <Input value={form.description}
                   onChange={(e) => { setForm((f) => ({ ...f, description: e.target.value })); onDirtyChange(true) }}
                   className="h-7 text-xs" placeholder="一句话说明何时该路由到本技能" />
            <div className="mt-1 grid grid-cols-2 gap-x-2">
              {SKILL_LIST_FIELDS.map((key) => (
                <div key={key} className="py-0.5">
                  <label className="text-[10px] text-muted-foreground">{FIELD_LABELS[key]}</label>
                  <Input value={form.lists[key]} onChange={(e) => setList(key, e.target.value)}
                         list={`vocab-${key}`}
                         className="h-7 font-mono text-[11px]" placeholder="逗号分隔，留空=不设置" />
                </div>
              ))}
            </div>
            <p className="mt-0.5 text-[10px] text-muted-foreground">
              未托管字段（enabled 等）原样保留；下拉候选来自全仓技能聚合，括号内是使用次数。
            </p>
          </div>
        )}
      </div>
      {/* 全仓 vocab datalists（七字段） */}
      {SKILL_LIST_FIELDS.map((key) => (
        <datalist key={key} id={`vocab-${key}`}>
          {(vocab?.[key] ?? []).map((v) => (
            <option key={v.value} value={v.value}>{v.count}</option>
          ))}
        </datalist>
      ))}
      <div className="flex shrink-0 items-center gap-1">
        <label className="text-[10px] text-muted-foreground">
          正文 Markdown（入口纪律与「观察→kb_open」对照；当前 {splitList(form.lists.keywords).length} 个关键词）
        </label>
        <span className="flex-1" />
        <div className="flex rounded border text-[10px]">
          <button className={cn("px-2 py-0.5", bodyMode === "edit" ? "bg-primary/10 text-primary" : "text-muted-foreground")}
                  onClick={() => setBodyMode("edit")}>编辑</button>
          <button className={cn("px-2 py-0.5", bodyMode === "preview" ? "bg-primary/10 text-primary" : "text-muted-foreground")}
                  onClick={() => setBodyMode("preview")}>预览</button>
        </div>
      </div>
      {bodyMode === "edit" ? (
        <Textarea value={form.body}
                  onChange={(e) => { setForm((f) => ({ ...f, body: e.target.value })); onDirtyChange(true) }}
                  className="min-h-0 flex-1 font-mono text-[13px] leading-relaxed" spellCheck={false} />
      ) : (
        <ScrollArea className="min-h-0 flex-1 rounded border bg-background/40 p-3">
          <MarkdownView content={form.body} prefix={SKILL_MD_PREFIX} />
        </ScrollArea>
      )}
    </div>
  )
})
