import { useState } from "react"
import {
  Braces, ChevronDown, Copy, ExternalLink, File as FileIcon, FileCode, FileImage,
  FileText, FolderOpen, Binary,
} from "lucide-react"
import { DropdownMenu } from "radix-ui"
import { api } from "@/lib/api"
import type { FileIcon as FileIconKind, FileTarget } from "@/lib/fileTargets"
import { cn } from "@/lib/utils"

// 文件卡（会话流改造，2026-10-03）：叙述/工具产出的文件渲染为卡片——图标方块 +
// 文件名 + 语言徽章 + 相对路径，右侧「打开方式」下拉（系统默认程序打开 /
// 资源管理器定位 / 复制路径 / 复制文件内容）。对齐 cc-haha AssistantOutputTargetCard。
//
// 后端 POST /api/projects/{pid}/files/open 收**相对 scope 根的路径**；本组件把
// target.relPath 原样交给它（scope 白名单=项目工作区 ∪ packs ∪ tools，越界 422）。
// pid 缺失（如审计抽屉未绑项目）时下拉降级为只读，仅展示不报错。

const ICONS: Record<FileIconKind, typeof FileIcon> = {
  md: FileText, code: FileCode, json: Braces, doc: FileText,
  image: FileImage, binary: Binary, file: FileIcon,
}
const TONES: Record<FileIconKind, string> = {
  md: "text-sky-400", code: "text-emerald-400", json: "text-amber-400",
  doc: "text-muted-foreground", image: "text-fuchsia-400",
  binary: "text-(--status-error)", file: "text-muted-foreground",
}

type Action = "open" | "reveal" | "resolve" | "content"

export function FileCard({ target, pid }: { target: FileTarget; pid?: string }) {
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const Icon = ICONS[target.icon] ?? FileIcon

  const run = async (action: Action) => {
    if (!pid || busy) return
    setBusy(true); setNote(null)
    try {
      const r = await api.openProjectFile(pid, target.relPath, action)
      if (action === "resolve") {
        await navigator.clipboard.writeText(r.abs_path)
        setNote("路径已复制")
      } else if (action === "content") {
        await navigator.clipboard.writeText(r.content ?? "")
        setNote("内容已复制")
      } else {
        setNote(action === "open" ? "已交给系统打开" : "已在资源管理器定位")
      }
    } catch (e) {
      setNote(e instanceof Error ? e.message : "操作失败")
    } finally {
      setBusy(false)
      window.setTimeout(() => setNote(null), 2400)
    }
  }

  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-border/60 bg-card/60 px-3 py-2">
      <span className={cn("grid size-8 shrink-0 place-items-center rounded-lg bg-accent/40", TONES[target.icon])}>
        <Icon className="size-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[12.5px] font-semibold text-foreground/90">{target.name}</span>
          <span className="shrink-0 rounded border border-border/60 px-1 font-mono text-[9px] tracking-wide text-muted-foreground/80">
            {target.badge}
          </span>
        </div>
        <div className="truncate font-mono text-[10px] text-muted-foreground/60" title={target.relPath}>
          {target.relPath}
        </div>
      </div>
      {note && <span className="shrink-0 text-[10px] text-muted-foreground">{note}</span>}
      {pid ? (
        <DropdownMenu.Root>
          <DropdownMenu.Trigger
            disabled={busy}
            className="flex shrink-0 items-center gap-1 rounded-md border border-border/60 px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
          >
            打开方式 <ChevronDown className="size-3" />
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content
              align="end" sideOffset={4}
              className="z-50 min-w-44 rounded-lg border bg-popover p-1 text-xs shadow-xl"
            >
              <Item icon={<ExternalLink className="size-3.5" />} onSelect={() => void run("open")}>
                用系统默认程序打开
              </Item>
              <Item icon={<FolderOpen className="size-3.5" />} onSelect={() => void run("reveal")}>
                在资源管理器显示
              </Item>
              <DropdownMenu.Separator className="my-1 h-px bg-border" />
              <Item icon={<Copy className="size-3.5" />} onSelect={() => void run("resolve")}>
                复制路径
              </Item>
              <Item icon={<Copy className="size-3.5" />} onSelect={() => void run("content")}>
                复制文件内容
              </Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      ) : null}
    </div>
  )
}

function Item({ icon, onSelect, children }: {
  icon: React.ReactNode; onSelect: () => void; children: React.ReactNode
}) {
  return (
    <DropdownMenu.Item
      onSelect={onSelect}
      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-foreground/85 outline-none data-[highlighted]:bg-accent data-[highlighted]:text-foreground"
    >
      {icon}
      {children}
    </DropdownMenu.Item>
  )
}

/** 一组文件卡（叙述下方/工具组展开内共用）。空列表渲染 null。 */
export function FileCardList({ targets, pid }: { targets: FileTarget[]; pid?: string }) {
  if (!targets.length) return null
  return (
    <div className="my-1.5 grid gap-1.5">
      {targets.map((t) => <FileCard key={t.relPath} target={t} pid={pid} />)}
    </div>
  )
}
