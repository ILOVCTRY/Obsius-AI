import { cn } from "@/lib/utils"
import { SkillWorkspace } from "@/components/settings/SkillWorkspace"

/** 一级技能工作台；编辑器逻辑与设置页兼容入口共享。
 *  顶部标题栏由 SkillWorkspace 自带，此处不再重复套壳。 */
export function SkillsView({ pid, focus }: {
  pid?: string | null
  focus?: { source: "cap" | "track"; pack: string; name: string; n: number } | null
}) {
  return (
    <div className={cn("flex h-full min-h-0 flex-col", !pid && "pt-9")}>
      <div className="min-h-0 flex-1"><SkillWorkspace pid={pid} focus={focus ?? null} /></div>
    </div>
  )
}
