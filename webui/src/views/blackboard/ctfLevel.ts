// CTF 线索级别（C2 换词表）：severity 字段在 ctf 轨的语义映射与展示色。
// 从 Blackboard.tsx 提出共享导出（2026-09-20）：列表视图与黑板链路图节点卡共用。

export const CTF_LEVEL: Record<string, { label: string; cls: string }> = {
  critical: { label: "关键突破", cls: "text-(--status-error)" },
  high: { label: "有效线索", cls: "text-(--status-approval)" },
  medium: { label: "背景信息", cls: "text-muted-foreground" },
  low: { label: "背景信息", cls: "text-muted-foreground" },
  info: { label: "背景信息", cls: "text-muted-foreground" },
}
export const CTF_LEVEL_ORDER = ["critical", "high", "medium", "low", "info"]
