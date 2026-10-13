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
 *
 * WSL 路径 `/mnt/<盘符>/…` 先映射回 `<盘符>:/…`（宿主工作区前缀才能剥掉），
 * 否则 `/mnt/e/…` 既不是合法 Windows 绝对路径也不在工作区下，后端会拼成
 * `<盘>:\mnt\e\…` 并 422「越界」。
 */
export function relToWorkdir(raw: string, workdir?: string | null): string {
  let p = String(raw || "").replace(/\\/g, "/").trim()
  if (!p) return p
  const wsl = p.match(/^\/mnt\/([a-zA-Z])\/(.*)$/)
  if (wsl) p = `${wsl[1].toLowerCase()}:/${wsl[2]}`
  const wd = String(workdir || "").replace(/\\/g, "/").replace(/\/+$/, "")
  if (wd && p.toLowerCase().startsWith(wd.toLowerCase() + "/")) return p.slice(wd.length + 1)
  if (wd && p.toLowerCase() === wd.toLowerCase()) return ""
  return p
}

function str(v: unknown): string {
  return typeof v === "string" ? v : ""
}

// 工具名 → 参数里指向文件的键（结果里出现的路径另算）
//
// 注意：bb_add_artifact 的 `filename` 只是**纯文件名**，真实落盘路径是
// `<artifacts_dir>/<kind>/<filename>` = 项目根下 `artifacts/<kind>/<filename>`。
// 所以不在此表里按裸名抽，而在 targetsFromTool 里特判构造（见下）。
const ARG_PATH_KEYS: Record<string, string[]> = {
  read_file: ["path"],
  search_files: ["path"],
  strings_search: ["binary"],
  func_xrefs: ["binary"],
  decompile: ["binary"],
  list_symbols: ["binary"],
  propose_pack_edit: ["target"],
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
  // bb_add_artifact：filename 是纯文件名，落盘在 `artifacts/<kind>/<filename>`。
  // 工具结果虽也返回 `path=<kind>/<filename>`，但那是相对 artifacts_dir 的——
  // 这里直接按参数构造项目根相对路径，避免结果解析漏/错。
  if (name === "bb_add_artifact") {
    const fn = str(args?.filename)
    if (fn) push(`artifacts/${str(args?.kind) || "file"}/${fn}`)
  }
  // 结果里显式路径（`写入 <path>` / `path: <p>` / `已保存到 <p>` 三形态）。
  // **必须过 looksLikePath**（2026-10-04 修误报文件卡）：kb_open/skill_open 的正文里
  // 常出现「文件：」一类字样，其后整句中文会被字符类吞下（该类不排除中文标点），
  // 不加校验就抽成「文件名」——曾把「；验证上限=影响证明级；…tested_clean」当路径。
  if (result) {
    for (const m of result.matchAll(/(?:写入|保存到|已生成|path[:：]|文件[:：])\s*([^\s"'`,;)]{3,300})/g)) {
      if (!looksLikePath(m[1])) continue
      let p = m[1]
      // bb_add_artifact 的返回 `path=<kind>/<filename>` 是相对 artifacts_dir 的，
      // 需补 `artifacts/` 前缀才是项目根相对路径（与上方 args 构造对齐）。
      if (name === "bb_add_artifact" && !p.startsWith("artifacts/")) p = `artifacts/${p}`
      push(p)
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
 *
 * 关键纪律（2026-10-13 修「复制路径不对」）：反引号里的**裸文件名**（不含 `/`）
 * 无法定位到工作区里的具体文件——必须靠 `known` 对账出真实路径才能收下；
 * 对账时优先取带目录信息（relPath 含 `/`）的已知目标，避免裸名覆盖真实路径。
 */
export function targetsFromProse(text: string, known: FileTarget[] = []): FileTarget[] {
  const out: FileTarget[] = []
  const add = (raw: string) => {
    const t = mk(raw, "prose")
    if (!t) return
    const matches = known.filter((k) => k.name === t.name)
    // 优先带目录的已知目标，否则取第一个（通常就是它）
    const hit = matches.find((k) => k.relPath.includes("/")) ?? matches[0]
    if (hit) {
      const final = { ...hit, source: "prose" as const }
      if (!out.some((x) => x.relPath === final.relPath)) out.push(final)
      return
    }
    // 无已知目标对账：只有带目录的路径才能定位文件，裸文件名丢弃（否则复制出不存在的路径）。
    if (!t.relPath.includes("/")) return
    if (!out.some((x) => x.relPath === t.relPath)) out.push(t)
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
