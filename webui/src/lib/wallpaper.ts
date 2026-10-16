/** 背景壁纸（2026-10-15）：配合「毛玻璃」主题的半透明表面使用。
 *
 *  - 图片本体存 IndexedDB（localStorage 5MB 配额放不下真实壁纸）；
 *  - 遮罩浓度/填充方式存 localStorage（小设置，随点随存）；
 *  - 呈现：一个 fixed 的 `.wallpaper-layer` div（body 直接子元素，z-index:-1，
 *    在所有内容之下、body 背景之上）。CSS 变量挂在 documentElement：
 *    `--wallpaper-image`（JS 覆写时的内联值优先于主题块里的内置默认 SVG）、
 *    `--wallpaper-dim`、`--wallpaper-fit`。
 *  不透明主题下壁纸被表面完全盖住——只有 glass 主题能看见。
 *  模块自身拥有 objectURL 生命周期（缓存唯一一个，换图/清图才 revoke）。
 */

export interface WallpaperSettings {
  /** 壁纸上的暗色遮罩浓度 0–0.8（保证文字可读） */
  dim: number
  /** 填充方式 */
  fit: "cover" | "contain"
}

const DB_NAME = "obsius-wallpaper"
const DB_VERSION = 1
const DB_STORE = "images"
const DB_KEY = "current"
const SETTINGS_KEY = "obsius.wallpaper"
const MAX_BYTES = 24 * 1024 * 1024

export const WALLPAPER_DEFAULTS: WallpaperSettings = { dim: 0.3, fit: "cover" }

let dbPromise: Promise<IDBDatabase> | null = null
let cachedUrl: string | null = null   // 当前自定义壁纸的 objectURL（模块单例）

function openDB(): Promise<IDBDatabase> {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION)
      req.onupgradeneeded = () => {
        const db = req.result
        if (!db.objectStoreNames.contains(DB_STORE)) db.createObjectStore(DB_STORE)
      }
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error ?? new Error("IndexedDB 打开失败"))
    })
  }
  return dbPromise
}

/** 读设置（损坏/缺失回落默认值）。 */
export function readWallpaperSettings(): WallpaperSettings {
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY)
    if (!raw) return { ...WALLPAPER_DEFAULTS }
    const parsed = JSON.parse(raw) as Partial<WallpaperSettings>
    const dim = typeof parsed.dim === "number" && Number.isFinite(parsed.dim)
      ? Math.min(0.8, Math.max(0, parsed.dim)) : WALLPAPER_DEFAULTS.dim
    const fit = parsed.fit === "contain" ? "contain" : "cover"
    return { dim, fit }
  } catch {
    return { ...WALLPAPER_DEFAULTS }
  }
}

/** 写设置并立即应用到 CSS 变量（返回完整设置供 UI 回填）。 */
export function writeWallpaperSettings(patch: Partial<WallpaperSettings>): WallpaperSettings {
  const next = { ...readWallpaperSettings(), ...patch }
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(next))
  } catch { /* localStorage 受限：至少当前会话生效 */ }
  const root = document.documentElement
  root.style.setProperty("--wallpaper-dim", String(next.dim))
  root.style.setProperty("--wallpaper-fit", next.fit)
  return next
}

/** 确保壁纸层 div 存在（幂等）。 */
export function ensureWallpaperLayer(): HTMLDivElement {
  let el = document.querySelector<HTMLDivElement>(".wallpaper-layer")
  if (!el) {
    el = document.body.appendChild(document.createElement("div"))
    el.className = "wallpaper-layer"
    el.setAttribute("aria-hidden", "true")
  }
  return el
}

/** 应用/撤销自定义壁纸。url=null = 撤销内联变量，回落到主题块的内置默认。 */
export function applyWallpaperImage(url: string | null): void {
  if (cachedUrl && cachedUrl !== url) URL.revokeObjectURL(cachedUrl)
  cachedUrl = url
  const root = document.documentElement
  if (url) root.style.setProperty("--wallpaper-image", `url("${url}")`)
  else root.style.removeProperty("--wallpaper-image")
  ensureWallpaperLayer()
}

/** 取自定义壁纸的 objectURL（无则 null）。缓存唯一一个，重复调用不新建。 */
export async function getWallpaperUrl(): Promise<string | null> {
  if (cachedUrl) return cachedUrl
  const db = await openDB()
  const blob = await new Promise<Blob | null>((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readonly")
    const req = tx.objectStore(DB_STORE).get(DB_KEY)
    req.onsuccess = () => resolve((req.result as Blob | undefined) ?? null)
    req.onerror = () => reject(req.error ?? new Error("读取壁纸失败"))
  })
  if (!blob) return null
  cachedUrl = URL.createObjectURL(blob)
  return cachedUrl
}

/** 存自定义壁纸（替换旧的）。返回新 objectURL。 */
export async function saveWallpaperBlob(blob: Blob): Promise<string> {
  if (blob.size > MAX_BYTES) throw new Error("图片过大（上限 24MB），换张小一点的")
  if (!blob.type.startsWith("image/")) throw new Error("只支持图片文件")
  const db = await openDB()
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readwrite")
    tx.objectStore(DB_STORE).put(blob, DB_KEY)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error ?? new Error("保存壁纸失败"))
  })
  const url = URL.createObjectURL(blob)
  applyWallpaperImage(url)
  return url
}

/** 删除自定义壁纸（回落到主题内置默认图）。 */
export async function clearWallpaperBlob(): Promise<void> {
  const db = await openDB()
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readwrite")
    tx.objectStore(DB_STORE).delete(DB_KEY)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error ?? new Error("删除壁纸失败"))
  })
  applyWallpaperImage(null)
}

/** 启动时调用（main.tsx）：建层 + 落设置 + 恢复自定义壁纸。失败只静默。 */
export async function initWallpaper(): Promise<void> {
  try {
    ensureWallpaperLayer()
    writeWallpaperSettings({})
    await getWallpaperUrl().then(applyWallpaperImage)
  } catch {
    // IndexedDB 不可用（隐私模式等）：壁纸功能整体降级为不可用，不影响主题。
  }
}
