import { Loader2, RotateCcw, X } from "lucide-react"
import { FILE_ICONS, FILE_TONE_BG, FILE_TONES } from "@/components/chat/FileCard"
import { fileBadge } from "@/lib/fileTargets"
import { cn } from "@/lib/utils"

// 待发附件胶囊（2026-10-20 重做）：工作台 composer 与直播间插话框**共用**。
//
// 旧版问题（Nan 反馈「不够高级、太简陋、不协调」）：手写 `<span>` 拼方角小方块
// （`rounded-md border px-2 py-1`），图标一律 paperclip、尺寸与文件名/字节挤在一行
// 同字号——而 composer 里其它控件（agent 选择、`.wb-cap` 能力钮、用量环、发送键）
// 全是**圆胶囊**，只有附件是方角，形态自成一族；且不看扩展名，任何文件长得一样。
//
// 现方案：对齐 composer 的胶囊语言（`rounded-full` + 发丝边 + 半透明底），
// 图标按**文件类型**取（复用 FileCard 的 FILE_ICONS/FILE_TONES，与文件卡同一套语义色），
// 信息分三层：类型图标（色）→ 文件名（主）→ 字节（等宽弱化）。三态共用同一骨架，
// 只换状态位，避免「上传中/失败」时胶囊宽度跳变。
//
// 刻意不依赖 `.wb-*` CSS（本目录约定，见 components/chat/CLAUDE.md）：
// 直播间与工作台都能直接复用；颜色一律走语义 token，玻璃模式下自动跟随。

export type AttachmentChipStatus = "uploading" | "ready" | "error"

export interface AttachmentChipItem {
  key: string
  name: string
  size: number
  status: AttachmentChipStatus
}

export function AttachmentChip({ item, onRemove, onRetry }: {
  item: AttachmentChipItem
  onRemove: (key: string) => void
  onRetry?: (key: string) => void
}) {
  const { badge, icon } = fileBadge(item.name)
  const Icon = FILE_ICONS[icon]
  const isError = item.status === "error"
  const isUploading = item.status === "uploading"

  return (
    <span
      className={cn(
        "group inline-flex h-8 max-w-[17.5rem] min-w-0 items-center gap-2 rounded-full border pr-1.5 pl-1",
        "text-[11px] shadow-[0_1px_2px_rgba(0,0,0,0.05)] transition-all duration-150",
        isError
          ? "border-destructive/45 bg-destructive/8 hover:border-destructive/60"
          : "border-border/70 bg-card/70 hover:-translate-y-px hover:border-(--brand-border) hover:bg-card/90 hover:shadow-[0_3px_10px_rgba(0,0,0,0.08)]",
        isUploading && "opacity-75",
      )}
      title={isError
        ? "上传失败，右侧「重试」可再传一次"
        : `${item.name}\n${badge} · ${fmtBytes(item.size)}`}
    >
      {/* 类型图标：圆角方牌 + 「类型色 / 类型色 12% 底」。
          底不能用 --accent：五套配色里 accent 与背景同色系，亮底上图标块整块消失
          （rose-light 实测：灰牌溶进白底）。用类型色自己的淡底 = 有色彩身份且两套底色都立得住。 */}
      <span className={cn(
        "grid size-6 shrink-0 place-items-center rounded-[7px]",
        isError
          ? "bg-destructive/12 text-destructive"
          : cn(FILE_TONE_BG[icon], icon === "file" || icon === "doc" ? "text-foreground/55" : FILE_TONES[icon]),
      )}>
        <Icon className="size-3.5" />
      </span>

      <span className={cn(
        "min-w-0 truncate font-medium tracking-[-0.01em]",
        isError ? "text-destructive/90" : "text-foreground/90",
      )}>
        {item.name}
      </span>

      {/* 第三层：字节数。**不用 font-mono**——等宽字体在 9.5px 下字距散（"4 . 0  K B" 观感），
          且这里是「弱提示」不是要对齐的数字列；改 sans + tabular-nums 稳列宽不散字。 */}
      {isUploading ? (
        <span className="flex shrink-0 items-center gap-1 text-[9.5px] text-muted-foreground">
          <Loader2 className="size-2.5 animate-spin" />
          上传中
        </span>
      ) : (
        <span className="shrink-0 text-[9.5px] tabular-nums text-muted-foreground/75">
          {fmtBytes(item.size)}
        </span>
      )}

      {isError && onRetry && (
        <button
          type="button"
          onClick={() => onRetry(item.key)}
          title="重新上传"
          className="grid size-5 shrink-0 place-items-center rounded-full text-destructive/80 transition-colors hover:bg-destructive/15 hover:text-destructive"
        >
          <RotateCcw className="size-2.5" />
        </button>
      )}

      {/* 移除键：常态就带一层淡底，让它成为明确可点的目标；
          悬停才抬到前景色。hover 前不给底色是因为整颗胶囊已有 hover 反馈。 */}
      <button
        type="button"
        onClick={() => onRemove(item.key)}
        title="移除附件"
        aria-label={`移除附件 ${item.name}`}
        className={cn(
          "grid size-5 shrink-0 place-items-center rounded-full text-muted-foreground/60",
          "transition-colors hover:bg-foreground/8 hover:text-foreground",
        )}
      >
        <X className="size-3" />
      </button>
    </span>
  )
}

/** 一组附件胶囊。空列表渲染 null（调用方无需自己判空）。 */
export function AttachmentChipRow({ items, onRemove, onRetry, trailing, className }: {
  items: AttachmentChipItem[]
  onRemove: (key: string) => void
  onRetry?: (key: string) => void
  /** 行尾补充说明（如直播间「与编排对话不支持附件」） */
  trailing?: React.ReactNode
  className?: string
}) {
  if (!items.length && !trailing) return null
  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      {items.map((it) => (
        <AttachmentChip key={it.key} item={it} onRemove={onRemove} onRetry={onRetry} />
      ))}
      {trailing}
    </div>
  )
}

/** 字节 → 人类可读。原先工作台与直播间各粘一份且**小数口径不同**（一个 1.5 KB、
 *  一个 15KB），现统一到本件内部，别再各自复制。 */
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
