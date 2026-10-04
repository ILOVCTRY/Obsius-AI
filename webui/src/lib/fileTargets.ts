// 文件卡数据源（会话流改造，2026-10-03）：从工具调用参数/结果与叙述正文里抽出
// 「本轮提到/产出的文件」，供 components/chat/FileCard 渲染。纯函数，零 React 依赖。
//
// 两条来源：① 工具（结构化，最可靠）② 叙述 markdown（反引号片段/裸路径），
// 后者与同轮已知目标对账（同 basename 时指向真实路径，避免只显文件名）。

export type FileIcon = "md" | "code" | "json" | "doc" | "image" | "binary" | "file"

export interface FileTarget {
  /** 相对 scope 根的路径（文件卡副标题；后端 /files/open 收的就是它） */
  relPath: string
  /** 展示名（basename） */
  name: string
  badge: string
  icon: FileIcon
  /** 来源：tool=结构化参数，prose=叙述正文 */
  source: "tool" | "prose"
}

const EXT_ICON: Record<string, { badge: string; icon: FileIcon }> = {
  md: { badge: "MARKDOWN", icon: "md" },
  markdown: { badge: "MARKDOWN", icon: "md" },
  py: { badge: "PYTHON", icon: "code" },
  ts: { badge: "TYPESCRIPT", icon: "code" },
  tsx: { badge: "TYPESCRIPT", icon: "code" },
  js: { badge: "JAVASCRIPT", icon: "code" },
  jsx: { badge: "JAVASCRIPT", icon: "code" },
  c: { badge: "C", icon: "code" },
  h: { badge: "C", icon: "code" },
  cpp: { badge: "C++", icon: "code" },
  hpp: { badge: "C++", icon: "code" },
  go: { badge: "GO", icon: "code" },
  rs: { badge: "RUST", icon: "code" },
  java: { badge: "JAVA", icon: "code" },
  sh: { badge: "SHELL", icon: "code" },
  ps1: { badge: "POWERSHELL", icon: "code" },
  yaml: { badge: "YAML", icon: "json" },
  yml: { badge: "YAML", icon: "json" },
  json: { badge: "JSON", icon: "json" },
  toml: { badge: "TOML", icon: "json" },
  csv: { badge: "CSV", icon: "json" },
  txt: { badge: "TEXT", icon: "doc" },
  log: { badge: "LOG", icon: "doc" },
  pdf: { badge: "PDF", icon: "doc" },
  docx: { badge: "DOC", icon: "doc" },
  xlsx: { badge: "SHEET", icon: "doc" },
  png: { badge: "IMAGE", icon: "image" },
  jpg: { badge: "IMAGE", icon: "image" },
  jpeg: { badge: "IMAGE", icon: "image" },
  gif: { badge: "IMAGE", icon: "image" },
  webp: { badge: "IMAGE", icon: "image" },
  svg: { badge: "IMAGE", icon: "image" },
  exe: { badge: "BINARY", icon: "binary" },
  dll: { badge: "BINARY", icon: "binary" },
  so: { badge: "BINARY", icon: "binary" },
  elf: { badge: "BINARY", icon: "binary" },
  bin: { badge: "BINARY", icon: "binary" },
  apk: { badge: "BINARY", icon: "binary" },
  i64: { badge: "IDA DB", icon: "binary" },
  idb: { badge: "IDA DB", icon: "binary" },
  gpr: { badge: "GHIDRA", icon: "binary" },
}

/** 扩展名 → 徽章/图标档。未知扩展名回落 FILE。 */
export function fileBadge(relPath: string): { badge: string; icon: FileIcon } {
  const base = relPath.replace(/\\/g, "/").split("/").pop() ?? relPath
  const dot = base.lastIndexOf(".")
  if (dot <= 0) return { badge: "FILE", icon: "file" }
  const ext = base.slice(dot + 1).toLowerCase()
  return EXT_ICON[ext] ?? { badge: ext.toUpperCase().slice(0, 8), icon: "file" }
}

function mk(relPath: string, source: FileTarget["source"]): FileTarget | null {
  const p = String(relPath || "").replace(/\\/g, "/").trim()
  if (!p || p.length > 512) return null
  const { badge, icon } = fileBadge(p)
  return { relPath: p, name: p.split("/").pop() ?? p, badge, icon, source }
}

/**
 * 去掉项目工作区绝对前缀，落成相对路径（后端 `/files/open` 收相对 scope 根的路径）。
 * packs/tools 下的绝对路径原样保留（后端同样接受，见 `_open_scope_path`）。
 * 绝对路径下取不到工作区根时原样返回（后端会 422，不静默错指）。
 */
export function relToWorkdir(raw: string, workdir?: string | null): string {
  const p = String(raw || "").replace(/\\/g, "/").trim()
  if (!p) return p
  const wd = String(workdir || "").replace(/\\/g, "/").replace(/\/+$/, "")
  if (wd && p.toLowerCase().startsWith(wd.toLowerCase() + "/")) return p.slice(wd.length + 1)
  if (wd && p.toLowerCase() === wd.toLowerCase()) return ""
  return p
}

function str(v: unknown): string {
  return typeof v === "string" ? v : ""
}

// 工具名 → 参数里指向文件的键（结果里出现的路径另算）
const ARG_PATH_KEYS: Record<string, string[]> = {
  read_file: ["path"],
  search_files: ["path"],
  strings_search: ["binary"],
  func_xrefs: ["binary"],
  decompile: ["binary"],
  list_symbols: ["binary"],
  propose_pack_edit: ["target"],
  bb_add_artifact: ["filename", "path"],
  skill_open: ["path"],
  kb_open: ["path"],
  browser_screenshot: ["path"],
}

/**
 * 从一次工具调用抽出文件目标。args 与 result 都扫（result 里常见「已写入 <path>」
 * 一类回执）；结果路径以绝对路径为主，调用方按 scope 归一后再喂给后端。
 */
export function targetsFromTool(
  name: string,
  args?: Record<string, unknown> | null,
  result?: string,
): FileTarget[] {
  const out: FileTarget[] = []
  const push = (raw: string) => {
    const t = mk(raw, "tool")
    if (t && !out.some((x) => x.relPath === t.relPath)) out.push(t)
  }
  for (const key of ARG_PATH_KEYS[name] ?? []) push(str(args?.[key]))
  // 结果里显式路径（`写入 <path>` / `path: <p>` / `已保存到 <p>` 三形态）。
  // **必须过 looksLikePath**（2026-10-04 修误报文件卡）：kb_open/skill_open 的正文里
  // 常出现「文件：」一类字样，其后整句中文会被字符类吞下（该类不排除中文标点），
  // 不加校验就抽成「文件名」——曾把「；验证上限=影响证明级；…tested_clean」当路径。
  if (result) {
    for (const m of result.matchAll(/(?:写入|保存到|已生成|path[:：]|文件[:：])\s*([^\s"'`,;)]{3,300})/g)) {
      if (looksLikePath(m[1])) push(m[1])
    }
  }
  return out
}

// 反引号片段与裸相对路径（含 / 或已知扩展名才认，避免把普通词当文件）
const INLINE_CODE = /`([^`\n]{2,300})`/g
const BARE_PATH = /(?:^|\s)([\w.@-]+(?:\/[\w.@-]+){1,8}\.[A-Za-z0-9]{1,8})(?=\s|$|[,;:)])/g
const KNOWN_EXT = new Set(Object.keys(EXT_ICON))

function looksLikePath(s: string): boolean {
  const t = s.trim()
  if (!t || t.length > 300 || /\s/.test(t)) return false
  if (t.startsWith("http://") || t.startsWith("https://") || t.startsWith("/api/")) return false
  const base = t.split("/").pop() ?? t
  const dot = base.lastIndexOf(".")
  if (dot <= 0) return false
  return KNOWN_EXT.has(base.slice(dot + 1).toLowerCase())
}

/**
 * 从 assistant 叙述正文抽文件目标。`known`=同轮工具目标——同 basename 时
 * 指向真实路径（对齐 cc-haha reconcileTargetsWithChangedFiles），否则按原文路径。
 */
export function targetsFromProse(text: string, known: FileTarget[] = []): FileTarget[] {
  const out: FileTarget[] = []
  const add = (raw: string) => {
    const t = mk(raw, "prose")
    if (!t) return
    const hit = known.find((k) => k.name === t.name)
    const final = hit ? { ...hit, source: "prose" as const } : t
    if (!out.some((x) => x.relPath === final.relPath)) out.push(final)
  }
  for (const m of (text ?? "").matchAll(INLINE_CODE)) if (looksLikePath(m[1])) add(m[1])
  for (const m of (text ?? "").matchAll(BARE_PATH)) if (looksLikePath(m[1])) add(m[1])
  return out
}

/** 合并去重（工具来源优先保留，prose 只补新路径），cap 上限防卡面刷屏。 */
export function mergeTargets(...lists: FileTarget[][]): FileTarget[] {
  const out: FileTarget[] = []
  for (const list of lists) {
    for (const t of list) if (!out.some((x) => x.relPath === t.relPath)) out.push(t)
  }
  return out.slice(0, 6)
}
