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
  pentest: "渗透测试",
  redteam: "红队行动",
  assessment: "授权评估(旧)", // R1 退役键：服务端已映射 pentest，仅兜底未知来源
  research: "逆向分析",
  malware: "恶意样本",
}

const CAP_LABELS: Record<string, string> = {
  web: "Web",
  binary: "二进制",
  crypto: "密码学",
  forensics: "取证",
  misc: "杂项",
  "shell-c2": "Shell 管理",
  social: "社工",
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

/** 顶栏/列表徽章（expert-pool M3）：「渗透测试 · 专家×4」；无绑定=存量直通，只显轨名 */
export const bindingBadge = (track?: string | null, experts?: string[] | null): string => {
  const head = trackLabel(track)
  const n = experts?.length ?? 0
  return n > 0 ? `${head} · 专家×${n}` : head
}

/** 选轨后的推荐能力包（建项默认绑定；专家池 M3 后仅作存量展示与 captcha 门控兜底） */
export const RECOMMENDED_CAPS: Record<string, string[]> = {
  ctf: ["binary"],
  pentest: ["web"],
  redteam: ["web"],
  research: ["binary"],
}
