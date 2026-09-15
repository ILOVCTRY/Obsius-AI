// SKILL.md frontmatter 解析/合并（与后端极简约定对齐：平铺 key: value、逗号列表）。
// 混合编辑器只托管常用字段；未托管行（enabled/required_tools/自定义键/注释）原样保留。

export const SKILL_LIST_FIELDS = [
  "keywords", "features", "file_features", "platforms",
  "formats", "vuln_classes", "task_types",
] as const
export type SkillListField = (typeof SKILL_LIST_FIELDS)[number]

export interface SkillFormState {
  description: string
  lists: Record<SkillListField, string>  // 逗号分隔的原文
  body: string
}

export interface ParsedSkill {
  hasFm: boolean
  name: string
  form: SkillFormState
  /** 未托管的 frontmatter 原始行（含 enabled/required_tools/注释），保存时原样插回 */
  passthrough: string[]
}

const LIST_SET = new Set<string>(SKILL_LIST_FIELDS)

export function splitList(v: string): string[] {
  return v.split(/[,，]/).map((x) => x.trim()).filter(Boolean)
}

export function parseSkill(raw: string): ParsedSkill {
  const emptyLists = Object.fromEntries(
    SKILL_LIST_FIELDS.map((f) => [f, ""])) as Record<SkillListField, string>
  const form: SkillFormState = { description: "", lists: emptyLists, body: raw }
  // --- 必须独占一行，避免正文/值中的 --- 子串误判
  const m = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n([\s\S]*))?$/.exec(raw.trimEnd())
  if (!m) {
    return { hasFm: false, name: "", form, passthrough: [] }
  }
  const head = m[1]
  const body = m[2] ?? ""
  const passthrough: string[] = []
  let name = ""
  for (const line of head.split(/\r?\n/)) {
    if (!line.trim()) continue
    if (/^\s*#/.test(line)) { passthrough.push(line); continue }
    const m = /^([A-Za-z_][\w-]*)\s*:\s*(.*)$/.exec(line)
    if (!m) { passthrough.push(line); continue }
    const [, key, val] = m
    const v = val.trim().replace(/^["']|["']$/g, "")
    if (key === "name") name = v
    else if (key === "description") form.description = v
    else if (LIST_SET.has(key)) form.lists[key as SkillListField] = v
    else passthrough.push(line)
  }
  form.body = body.replace(/\s*$/, "")
  return { hasFm: true, name, form, passthrough }
}

/** 表单 + 正文 + 未托管行合并回完整 SKILL.md（name 不允许在表单改，恒用原 name）。 */
export function serializeSkill(name: string, form: SkillFormState, passthrough: string[]): string {
  const lines: string[] = ["---", `name: ${name}`]
  if (form.description.trim()) lines.push(`description: ${form.description.trim()}`)
  // 未托管行里若混入托管键（理论不会，防御性过滤），避免重复键
  const keep = passthrough.filter((line) => {
    const m = /^([A-Za-z_][\w-]*)\s*:/.exec(line.trim())
    return !(m && (m[1] === "name" || m[1] === "description" || LIST_SET.has(m[1])))
  })
  lines.push(...keep)
  for (const key of SKILL_LIST_FIELDS) {
    const vals = splitList(form.lists[key])
    if (vals.length) lines.push(`${key}: ${vals.join(", ")}`)
  }
  lines.push("---", "")
  const body = form.body.trim()
  return lines.join("\n") + (body ? `\n${body}\n` : "\n")
}
