// 正交分类学（DESIGN.md §4.5）：能力包（多选）× 场景轨（单选）。
// 后端 /api/taxonomy 的 label 是主数据源；本表仅兜底未知/旧值的显示。

export interface PackInfo {
  name: string
  label: string
  description: string
}

export interface Taxonomy {
  capabilities: PackInfo[]
  tracks: PackInfo[]
  /** 各轨任务类型注册表：task_type → 默认噪声预算 */
  task_types: Record<string, Record<string, string>>
}

const TRACK_LABELS: Record<string, string> = {
  ctf: "CTF",
  assessment: "授权评估",
  research: "研究",
  malware: "恶意样本",
}

const CAP_LABELS: Record<string, string> = {
  web: "Web",
  binary: "二进制",
  crypto: "密码学",
  forensics: "取证",
  misc: "杂项",
}

// 旧项目 domain 单值（已迁移为 track+capabilities，仅旧数据展示兜底）
const LEGACY_DOMAIN_LABELS: Record<string, string> = {
  ctf: "CTF",
  pentest: "渗透测试(旧)",
  reverse: "逆向(旧)",
}

export const trackLabel = (t?: string | null): string =>
  t ? (TRACK_LABELS[t] ?? t) : ""

export const capLabel = (c?: string | null): string =>
  c ? (CAP_LABELS[c] ?? c) : ""

export const domainLabel = (d?: string | null): string =>
  d ? (LEGACY_DOMAIN_LABELS[d] ?? TRACK_LABELS[d] ?? d) : ""

/** 顶栏/列表徽章：「授权评估 · Web」 */
export const bindingBadge = (track?: string | null, caps?: string[] | null): string => {
  const head = trackLabel(track)
  const tail = (caps ?? []).map(capLabel).join("/")
  return tail ? `${head} · ${tail}` : head
}

/** 选轨后的推荐能力包（新建向导弹窗默认勾选；可手动改） */
export const RECOMMENDED_CAPS: Record<string, string[]> = {
  ctf: ["binary"],
  assessment: ["web"],
  research: ["binary"],
}
