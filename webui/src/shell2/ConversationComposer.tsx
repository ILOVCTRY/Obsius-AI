import { useEffect, useRef, useState } from "react"
import { ArrowUp, Paperclip, Square, X } from "lucide-react"
import { api } from "@/lib/api"
import type { AttachmentInfo, Task } from "@/lib/types"
import type { SessionStatus } from "@/components/StatusDot"
import { cn } from "@/lib/utils"
import { AutonomyChip, ModelChip, RuntimeChip } from "./Chips"

// 对话工作台常驻 composer（会话中心化，2026-09-25）：派活就是对话——
// 会话态单一输入：发消息给当前会话，窗 idle 立即起跑、忙则轮末注入/排队；
// 编排态：与编排对话（orch.chat 插队轮）。不再有 note/指派/task 模式之分。
// 会话控制 chips：▶继续（paused）/ ▶跑任务队列（armed/idle/finished）；
// ■ 中断（running/paused 时发送钮变位）。草稿按 sid 隔离，组件不随 sid 切换卸载。

type PendingFile = {
  key: string
  status: "uploading" | "ready" | "error"
  name: string
  size: number
  file: File
  att?: AttachmentInfo
}

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`
  if (n >= 1024) return `${Math.round(n / 1024)}KB`
  return `${n}B`
}

export interface ComposerProps {
  pid: string
  sid: string
  isOrch: boolean
  sessionName: string
  status: SessionStatus | null
  /** task.claimed 事件到达 bump（清理排队引导条） */
  claimedBump: number
  /** 当前会话绑定任务（runtime chip；null=不渲染） */
  task: Task | null
  /** runtime chip 改完任务后回调（pane 刷任务列表） */
  onTaskChanged: () => void
  onNote: (text: string, attIds: string[]) => Promise<unknown>
  onOrchChat: (text: string) => Promise<unknown>
  onAbort: () => void
  onResume: () => void
  onRunWork: () => void
}

export function ConversationComposer(p: ComposerProps) {
  const { sid, isOrch } = p
  // 草稿按 sid 隔离
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const text = drafts[sid] ?? ""

  const [files, setFiles] = useState<PendingFile[]>([])
  const [queue, setQueue] = useState<{ key: string; text: string; attCount: number }[]>([])
  const [info, setInfo] = useState<{ ok: boolean; msg: string } | null>(null)
  const [orchBusy, setOrchBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  const setText = (v: string) => setDrafts((m) => ({ ...m, [sid]: v }))

  // sid 切换：重算 textarea 高度
  useEffect(() => {
    const ta = taRef.current
    if (ta) { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight, 144)}px` }
  }, [sid])

  // 排队引导条清理：当前会话新一轮 task.claimed 到达
  useEffect(() => {
    if (p.claimedBump > 0) setQueue((qs) => qs.slice(1))
  }, [p.claimedBump]) // eslint-disable-line react-hooks/exhaustive-deps

  const uploadOne = (key: string, file: File) => {
    api.uploadAttachment(p.pid, file)
      .then((att) => setFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "ready", att } : x)))
      .catch(() => setFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "error" } : x)))
  }
  const pickFiles = (list: FileList | null) => {
    for (const f of Array.from(list ?? [])) {
      const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`
      setFiles((fs) => [...fs, { key, status: "uploading", name: f.name, size: f.size, file: f }])
      uploadOne(key, f)
    }
  }
  const removeFile = (key: string) => setFiles((fs) => fs.filter((x) => x.key !== key))

  const autoResize = () => {
    const ta = taRef.current
    if (ta) { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight, 144)}px` }
  }

  const clearSentFiles = () => {
    const sent = new Set(files.filter((f) => f.status === "ready").map((f) => f.key))
    setFiles((fs) => fs.filter((f) => !sent.has(f.key)))
  }

  const canAbort = p.status === "running" || p.status === "paused"
  const uploading = files.some((f) => f.status === "uploading")
  const hasReadyAtt = files.some((f) => f.status === "ready")
  const canSend = uploading ? false
    : isOrch ? text.trim().length > 0 && !orchBusy
    : text.trim().length > 0 || hasReadyAtt

  const send = async () => {
    const t = text.trim()
    const attIds = files.filter((f) => f.status === "ready" && f.att).map((f) => f.att!.id)
    setInfo(null)
    try {
      if (isOrch) {
        if (!t || orchBusy) return
        setText(""); setOrchBusy(true)
        try {
          await p.onOrchChat(t)
          setInfo({ ok: true, msg: "编排器思考中…（回复见上方对话流）" })
        } finally { setOrchBusy(false) }
        return
      }
      if (!t && attIds.length === 0) return
      await p.onNote(t, attIds)
      setText(""); clearSentFiles()
      if (canAbort) {
        setQueue((qs) => [...qs, {
          key: `qn-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          text: t, attCount: attIds.length,
        }])
      } else {
        setInfo({ ok: true, msg: "已发送：Agent 正在回复" })
      }
    } catch (e) {
      setInfo({ ok: false, msg: String(e) })
    }
  }

  const placeholder = isOrch
    ? "与编排器对话：问态势/问下一步/纠正方向——可让它委派委托、开窗口（同闸门）…"
    : canAbort
      ? `给「${p.sessionName}」发消息：本轮结束后注入（排队条可连续发）…`
      : `与「${p.sessionName}」对话：发消息即派活，窗空闲立即起跑；可跑命令、查黑板、登记结论…`

  return (
    <div className="relative shrink-0 p-3">
      <div className={cn(
        "rounded-xl border border-white/25 bg-background/30 px-3 py-2",
        "transition-[border-color,box-shadow] duration-200",
        "focus-within:border-white/60 focus-within:shadow-[0_0_18px_rgba(255,255,255,0.15)]")}>
        {/* 排队引导条 */}
        {queue.map((q) => (
          <div key={q.key}
               className="mb-2 flex items-center gap-2 rounded-md border border-white/10 bg-accent/40 px-2 py-1 text-xs">
            <span className="min-w-0 flex-1 truncate text-muted-foreground">
              💬 {q.text || `📎 附件 ×${q.attCount}`}（本轮结束后注入）
            </span>
            <button type="button" className="shrink-0 text-muted-foreground hover:text-foreground"
                    title="移除排队提示（消息仍在收件箱，会随下一轮注入）"
                    onClick={() => setQueue((qs) => qs.filter((x) => x.key !== q.key))}>
              <X className="size-3" />
            </button>
          </div>
        ))}
        {/* 附件 chips */}
        {files.length > 0 && (
          <div className={cn("mb-2 flex flex-wrap items-center gap-1.5", isOrch && "opacity-50")}>
            {files.map((f) => (
              <span key={f.key}
                className={cn("inline-flex max-w-64 items-center gap-1 rounded-md border px-2 py-1 text-xs",
                  f.status === "error" && "border-destructive/50 text-destructive",
                  f.status === "uploading" && "opacity-60")}
                title={f.status === "error" ? "上传失败" : `${f.name}（${fmtBytes(f.size)}）`}>
                <Paperclip className="size-3 shrink-0" />
                <span className="min-w-0 truncate">{f.name}</span>
                <span className="shrink-0 text-[10px] text-muted-foreground">{fmtBytes(f.size)}</span>
                {f.status === "uploading" && <span className="shrink-0 text-[10px]">上传中…</span>}
                {f.status === "error" && (
                  <button type="button" className="shrink-0 underline"
                          onClick={() => { setFiles((fs) => fs.map((x) => x.key === f.key ? { ...x, status: "uploading" } : x)); uploadOne(f.key, f.file) }}>
                    重试
                  </button>
                )}
                <button type="button" className="shrink-0 text-muted-foreground hover:text-foreground"
                        onClick={() => removeFile(f.key)} title="移除附件">
                  <X className="size-3" />
                </button>
              </span>
            ))}
            {isOrch && <span className="text-[10px] text-muted-foreground">与编排对话不支持附件</span>}
          </div>
        )}
        <textarea
          ref={taRef} rows={1} value={text} placeholder={placeholder}
          onChange={(e) => { setText(e.target.value); autoResize() }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault(); void send()
            }
          }}
          className="max-h-36 min-h-6 w-full resize-none bg-transparent text-sm leading-6 outline-none placeholder:text-muted-foreground"
        />
        {/* 工具行 */}
        <div className="mt-1.5 flex items-center gap-1.5 border-t border-white/10 pt-1.5">
          <button type="button" disabled={isOrch}
            title="添加附件（随消息下发）"
            onClick={() => fileRef.current?.click()}
            className={cn("flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground",
              isOrch && "cursor-not-allowed opacity-40 hover:bg-transparent hover:text-muted-foreground")}>
            <Paperclip className="size-4" />
          </button>
          {/* M3 chip 全通：自主档=编排态项目级 / runtime=任务级 / 模型=项目级 */}
          {isOrch
            ? <AutonomyChip pid={p.pid} />
            : <RuntimeChip task={p.task} onChanged={p.onTaskChanged} />}
          <ModelChip pid={p.pid} />
          {/* 会话控制 chips */}
          {!isOrch && p.status === "paused" && (
            <button type="button" onClick={p.onResume}
              title="从快照恢复"
              className="rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground">
              ▶ 继续
            </button>
          )}
          {!isOrch && (p.status === "armed" || p.status === "idle" || p.status === "finished") && (
            <button type="button" onClick={p.onRunWork}
              title="启动 worker：自动接窗内委托队列"
              className="rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground">
              ▶ 跑任务队列
            </button>
          )}
          {info && (
            <span className={cn("min-w-0 flex-1 truncate text-[11px]",
              info.ok ? "text-muted-foreground" : "text-(--status-error)")} title={info.msg}>
              {info.msg}
            </span>
          )}
          <span className="min-w-0 flex-1" />
          {canAbort ? (
            <button type="button" onClick={p.onAbort}
              title="立即中断（标记失败，快照保留可续跑）"
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground hover:opacity-90">
              <Square className="size-3.5 fill-current" />
            </button>
          ) : (
            <button type="button" onClick={send} disabled={!canSend}
              title="发送（Enter；Shift+Enter 换行）"
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-40">
              <ArrowUp className="size-4" />
            </button>
          )}
        </div>
      </div>
      <input ref={fileRef} type="file" multiple className="hidden"
             onChange={(e) => { pickFiles(e.target.files); e.target.value = "" }} />
    </div>
  )
}
