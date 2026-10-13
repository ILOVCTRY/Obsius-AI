// 正交分类学（DESIGN.md §4.5）：kb 知识方向 × 场景轨（单选）。
// 2026-10-13 能力包退役：原「能力包」降级为 kb 知识方向（directions），项目不再绑定；
// 后端 /api/taxonomy 的 label 是主数据源；本表仅兜底未知/旧值的显示。

export interface PackInfo {
  name: string
  label: string
  description: string
}

export interface Taxonomy {
  /** kb 知识方向（原能力包名：web/binary/crypto/forensics/misc/cloud） */
  directions: string[]
  tracks: PackInfo[]
  /** 各轨任务类型注册表：task_type → 默认噪声预算 */
  task_types: Record<string, Record<string, string>>
}

const TRACK_LABELS: Record<string, string> = {
  ctf: "CTF",
  pentest: "渗透测试",
  redteam: "红队行动",
  assessment: "授权评估(旧)", // R1 退役键：服务端已映射 pentest，仅兜底未知来源
  research: "逆向分析",
  malware: "恶意样本",
}

const DIR_LABELS: Record<string, string> = {
  web: "Web",
  binary: "二进制",
  crypto: "密码学",
  forensics: "取证",
  misc: "杂项",
  cloud: "云安全",
}

// 旧项目 domain 单值（仅旧数据展示兜底）
const LEGACY_DOMAIN_LABELS: Record<string, string> = {
  ctf: "CTF",
  pentest: "渗透测试(旧)",
  reverse: "逆向(旧)",
}

export const trackLabel = (t?: string | null): string =>
  t ? (TRACK_LABELS[t] ?? t) : ""

/** kb 知识方向显示名（原能力包名） */
export const dirLabel = (d?: string | null): string =>
  d ? (DIR_LABELS[d] ?? d) : ""

/** 兼容别名：能力包已退役，方向名即原包名，旧调用点零改动 */
export const capLabel = dirLabel

export const domainLabel = (d?: string | null): string =>
  d ? (LEGACY_DOMAIN_LABELS[d] ?? TRACK_LABELS[d] ?? d) : ""

/** 顶栏/列表徽章（expert-pool M3）：「渗透测试 · 专家×4」；无绑定=存量直通，只显轨名 */
export const bindingBadge = (track?: string | null, experts?: string[] | null): string => {
  const head = trackLabel(track)
  const n = experts?.length ?? 0
  return n > 0 ? `${head} · 专家×${n}` : head
}
