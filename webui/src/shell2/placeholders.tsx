import type { ResourceKey } from "./types"

// WelcomePane 已 M5 升级为独立文件（./WelcomePane.tsx：名称输入+模板卡）。

const RESOURCE_META: Record<ResourceKey, { title: string; milestone: string; hint: string }> = {
  templates: { title: "模板库", milestone: "M5", hint: "已可用：场景模板中心（CTF 向导 / 渗透目标录入 / 样本 triage）" },
  plugins: { title: "插件 · MCP", milestone: "M4", hint: "已迁入工具姿态「插件·MCP」tab" },
  intel: { title: "情报", milestone: "M4", hint: "情报页现已可直接使用（本页即为内嵌）" },
  files: { title: "产物文件", milestone: "M3", hint: "本项目 artifacts 浏览与下载" },
}

/** 资源入口占位（情报除外：情报直接内嵌 IntelView） */
export function ResourcePlaceholder({ resource }: { resource: Exclude<ResourceKey, "intel"> }) {
  const m = RESOURCE_META[resource]
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="w-full max-w-md rounded-lg border p-5">
        <h2 className="mb-1 text-sm font-semibold">{m.title} · {m.milestone}</h2>
        <p className="text-xs text-muted-foreground">{m.hint}</p>
      </div>
    </div>
  )
}
