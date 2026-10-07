const { app, BrowserWindow, WebContentsView, ipcMain, Menu, session, shell } = require("electron")
const { spawn } = require("child_process")
const path = require("path")
const fs = require("fs")

const ROOT = path.resolve(__dirname, "..")
const API_PORT = Number(process.env.CS_API_PORT || 8420)
const CDP_PORT = Number(process.env.CS_ELECTRON_CDP_PORT || 9222)
const API_BASE = `http://127.0.0.1:${API_PORT}`
const UI_URL = process.env.CS_UI_URL || `${API_BASE}/`
const tabs = new Map()
let mainWindow = null
let pythonProcess = null
let pythonOwner = false
let activeId = null

app.commandLine.appendSwitch("remote-debugging-port", String(CDP_PORT))
app.commandLine.appendSwitch("remote-allow-origins", "*")

// 应用壳只允许停在 UI 自身源。此前无守卫：任何顶层导航（误点外链、拖放、重定向、
// 页面里第三方库发起的导航）都会把无边框窗口带走——没有地址栏/后退键，用户被永久
// 卡在空白页，整窗只剩 BrowserWindow 的 backgroundColor（2026-10-06 实测：主窗停在
// https://adc.zzuli.edu.cn/ 的 39 字节空文档）。外链一律交系统浏览器。
function uiOrigin() {
  try { return new URL(UI_URL).origin } catch { return API_BASE }
}

function guardTopLevelNavigation(webContents) {
  const allow = uiOrigin()
  const block = (event, url) => {
    if (url === "about:blank") return
    let target
    try { target = new URL(url) } catch { event.preventDefault(); return }
    if (target.origin === allow) return
    event.preventDefault()
    if (target.protocol === "http:" || target.protocol === "https:") {
      void shell.openExternal(url)
    }
  }
  webContents.on("will-navigate", block)
  webContents.on("will-redirect", block)
  // target="_blank" / window.open：不要在壳里再开一个裸窗（无 preload、无窗控），
  // 交系统浏览器打开。
  webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) void shell.openExternal(url)
    return { action: "deny" }
  })
}

// ---- 壳级日志（logs/desktop.log）----
// 此前壳没有任何日志落盘：主窗渲染进程静默死亡时（render-process-gone 未挂主窗、
// Windows 事件日志也无记录）事后完全无法定位，用户只能看到一片空白。壳级异常与
// 自愈动作一律落此文件。
const LOG_PATH = path.join(ROOT, "logs", "desktop.log")

function logLine(message) {
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true })
    fs.appendFileSync(LOG_PATH, `[${new Date().toISOString()}] ${message}\n`)
  } catch (_) { /* 日志写失败绝不阻断主流程 */ }
  console.log("[desktop]", message)
}

// ---- 主窗渲染进程死亡自愈（2026-10-07 白屏事故）----
// 主窗渲染进程一旦退出，窗口就只剩 BrowserWindow.backgroundColor（无边框、无地址栏、
// 无菜单），用户看到的正是「白屏」且无从自救；而重启应用也救不回来（见下 second-instance）。
// 挂 render-process-gone 做退避重载，任何死因都能自愈；连续崩溃设上限防重载风暴——
// 只有加载成功后稳定 60s 才清零计数。
let mainCrashed = false
let reloadAttempts = 0
let reloadResetTimer = null

function noteMainLoaded() {
  mainCrashed = false
  clearTimeout(reloadResetTimer)
  reloadResetTimer = setTimeout(() => { reloadAttempts = 0 }, 60000)
}

function recoverMainWindow(reason) {
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (reloadAttempts >= 5) {
    logLine(`主窗自动重载已达上限(5)，停止恢复，请手动重启应用。last=${reason}`)
    return
  }
  const delay = Math.min(1000 * 2 ** reloadAttempts, 15000)
  reloadAttempts += 1
  logLine(`主窗将于 ${delay}ms 后重载（第 ${reloadAttempts} 次）reason=${reason}`)
  setTimeout(() => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      try { mainWindow.webContents.reload() } catch (error) { logLine(`主窗重载失败: ${error}`) }
    }
  }, delay)
}

function attachMainWindowRecovery(win) {
  const wc = win.webContents
  wc.on("render-process-gone", (_event, details) => {
    mainCrashed = true
    logLine(`主窗渲染进程退出 reason=${details && details.reason} exitCode=${details && details.exitCode}`)
    recoverMainWindow((details && details.reason) || "unknown")
  })
  wc.on("unresponsive", () => logLine("主窗无响应（unresponsive）"))
  wc.on("responsive", () => logLine("主窗恢复响应（responsive）"))
  wc.on("did-finish-load", () => noteMainLoaded())
  // 无边框窗口 Menu 置空后，Electron 默认的 Ctrl+R / F5 重载加速键随菜单一起消失，
  // 窗口里没有任何刷新入口——手工补回，兼作渲染进程卡死时的人工逃生口。
  wc.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return
    const key = String(input.key || "").toLowerCase()
    if ((input.control && !input.shift && key === "r") || key === "f5") {
      event.preventDefault()
      logLine("人工触发重载（Ctrl+R / F5）")
      wc.reload()
    } else if (input.control && input.shift && key === "i") {
      event.preventDefault()
      wc.openDevTools({ mode: "detach" })
    }
  })
}

function sendTabs() {
  if (!mainWindow || mainWindow.isDestroyed()) return
  mainWindow.webContents.send("tabs:updated", Array.from(tabs.values()).map((tab) => ({
    id: tab.id, type: tab.type, sid: tab.sid, pid: tab.pid || "", title: tab.title, url: tab.url,
  })))
}

async function apiReady() {
  try {
    const response = await fetch(`${API_BASE}/api/browser/status`)
    return response.ok
  } catch (_) { return false }
}

async function ensurePython() {
  if (await apiReady()) return
  const configuredPython = process.env.CS_PYTHON || "E:\\Miniconda3\\python.exe"
  const python = process.env.CS_PYTHON || (fs.existsSync(configuredPython) ? configuredPython : "python")
  pythonProcess = spawn(python, [path.join(ROOT, "scripts", "serve.py"), String(API_PORT)], {
    cwd: ROOT, windowsHide: true, stdio: "ignore",
  })
  pythonOwner = true
  const deadline = Date.now() + 30000
  while (Date.now() < deadline) {
    if (await apiReady()) return
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  throw new Error(`Python API did not start on ${API_BASE}`)
}

async function postAttach(tab) {
  if (tab.type !== "browser") return
  try {
    const response = await fetch(`${API_BASE}/api/projects/${encodeURIComponent(tab.pid)}/browser/desktop/attach`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ cdp_url: `http://127.0.0.1:${CDP_PORT}`, sid: tab.sid, owner: tab.sid === "human-main" ? "human" : tab.sid }),
    })
    if (response.ok) tab.attached = true
    else tab.error = await response.text()
  } catch (error) { tab.error = String(error) }
  sendTabs()
}

function setTabBounds(id, rect) {
  const tab = tabs.get(id)
  if (!tab || tab.type !== "browser") return
  tab.rect = rect
  const view = tab.view
  const isActive = activeId === id
  view.setBounds(isActive ? {
    x: Math.max(0, Math.round(rect.x)), y: Math.max(0, Math.round(rect.y)),
    width: Math.max(1, Math.round(rect.width)), height: Math.max(1, Math.round(rect.height)),
  } : { x: 0, y: 0, width: 1, height: 1 })
  view.setVisible(isActive)
}

function activateTab(id) {
  if (!tabs.has(id)) return false
  activeId = id
  for (const tab of tabs.values()) {
    if (tab.type !== "browser") continue
    tab.view.setVisible(tab.id === id)
    if (tab.id === id && tab.rect) setTabBounds(tab.id, tab.rect)
  }
  sendTabs()
  return true
}

// 原生浏览器页是覆盖在窗口上的 WebContentsView：前端切走浏览器模块时组件卸载，
// 但原生视图不会自动消失，会留在原位浮在新视图之上。前端挂载/卸载时调用此接口
// 显式显示/隐藏全部浏览器原生视图（隐藏时保持 activeId，重新显示仍用同一页）。
function setBrowserViewsVisible(visible) {
  for (const tab of tabs.values()) {
    if (tab.type !== "browser") continue
    tab.view.setVisible(visible && tab.id === activeId)
  }
  if (visible) {
    const active = tabs.get(activeId)
    if (active?.type === "browser" && active.rect) setTabBounds(activeId, active.rect)
  }
}

function createBrowserTab(pid, requestedSid) {
  const id = `browser-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
  const hasHuman = Array.from(tabs.values()).some((tab) => tab.type === "browser" && tab.sid === "human-main")
  const sid = requestedSid || (!hasHuman ? "human-main" : id)
  const view = new WebContentsView({
    webPreferences: { contextIsolation: true, sandbox: true },
  })
  view.setBackgroundColor("#111419")
  mainWindow.contentView.addChildView(view)
  const tab = { id, sid, pid, type: "browser", title: "新标签页", url: "about:blank", view, attached: false }
  tabs.set(id, tab); activateTab(id)
  view.webContents.on("did-navigate", (_event, url) => { tab.url = url; sendTabs() })
  view.webContents.on("did-navigate-in-page", (_event, url) => { tab.url = url; sendTabs() })
  view.webContents.on("page-title-updated", (_event, title) => { tab.title = title || tab.url; sendTabs() })
  view.webContents.on("dom-ready", async () => {
    try { await view.webContents.executeJavaScript(`window.name = ${JSON.stringify(sid)}`) } catch (_) {}
    await postAttach(tab)
  })
  view.webContents.on("render-process-gone", () => { tab.error = "页面渲染进程已退出"; sendTabs() })
  view.webContents.loadURL("about:blank")
  sendTabs()
  return { id, sid, type: tab.type, title: tab.title, url: tab.url }
}

function createTerminalTab(pid, cwd) {
  const id = `terminal-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
  let pty
  try {
    const nodePty = require("node-pty")
    pty = nodePty.spawn("powershell.exe", ["-NoLogo", "-NoProfile"], {
      name: "xterm-color", cols: 120, rows: 32, cwd: cwd && fs.existsSync(cwd) ? cwd : ROOT,
      env: { ...process.env, TERM: "xterm-256color" },
    })
  } catch (error) {
    return { error: `node-pty unavailable: ${error}` }
  }
  const tab = { id, sid: id, pid: pid || "", type: "terminal", title: "终端", url: "", pty }
  tabs.set(id, tab); activateTab(id)
  pty.onData((data) => mainWindow?.webContents.send("terminal:data", { id, data }))
  pty.onExit(() => { if (tabs.has(id)) { tabs.delete(id); sendTabs() } })
  sendTabs()
  return { id, sid: id, type: tab.type, title: tab.title, url: tab.url }
}

async function closeTab(id) {
  const tab = tabs.get(id)
  if (!tab) return
  if (tab.pty) { try { tab.pty.kill() } catch (_) {} }
  if (tab.view) {
    try {
      await fetch(`${API_BASE}/api/projects/${encodeURIComponent(tab.pid)}/browser/desktop/detach`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ sid: tab.sid, paused: false }),
      })
    } catch (_) {}
    try { mainWindow.contentView.removeChildView(tab.view); tab.view.webContents.close() } catch (_) {}
  }
  tabs.delete(id)
  if (activeId === id) activeId = tabs.keys().next().value || null
  sendTabs()
}

function getTab(id) { return tabs.get(id) }

async function createWindow() {
  await ensurePython()
  mainWindow = new BrowserWindow({
    width: 1500, height: 960, minWidth: 1100, minHeight: 680,
    backgroundColor: "#111419",
    frame: false,
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: false },
  })
  const url = UI_URL
  guardTopLevelNavigation(mainWindow.webContents)
  attachMainWindowRecovery(mainWindow)
  await mainWindow.loadURL(url)
  session.defaultSession.on("will-download", (_event, item) => {
    item.setSaveDialogOptions({ defaultPath: path.join(ROOT, "workspaces", "downloads", item.getFilename()) })
  })
  mainWindow.on("resize", () => {
    mainWindow.webContents.send("desktop:resize")
    mainWindow.webContents.send("window:maximized", mainWindow.isMaximized())
  })
  mainWindow.on("maximize", () => mainWindow.webContents.send("window:maximized", true))
  mainWindow.on("unmaximize", () => mainWindow.webContents.send("window:maximized", false))
  mainWindow.on("closed", () => { mainWindow = null })
}

// 单实例锁：两个实例共用同一 Chromium user-data-dir（CDP 端口 / 缓存 / 存储互相踩，
// 后启动的那个连 remote-debugging-port 都绑不上），且用户看到空白窗时再双击启动只会
// 再叠一个窗，无从判断该关哪个。第二次启动改为聚焦已有窗口；若它已被导航离开 UI
// （旧版本无守卫时可能发生）**或渲染进程已死**（窗口只剩背景色 = 白屏），一律重载自愈。
// —— 2026-10-07：自愈条件原先只比对 URL origin，而渲染进程死亡后 getURL() 仍报 UI
// 源，同源判断直接漏掉白屏；于是「重启后端 / 再双击启动器」全成了空操作，用户被永久
// 卡在空白窗（新实例拿到不到锁就 app.quit，实测 exit=0 立即退出）。
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on("second-instance", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
    let away = false
    try {
      away = new URL(mainWindow.webContents.getURL()).origin !== uiOrigin()
    } catch (_) { away = true }  // 取 URL 失败按「已离开 UI」处理
    if (mainCrashed || away) {
      logLine(`二次启动自愈：重载主窗（crashed=${mainCrashed} away=${away}）`)
      reloadAttempts = 0
      void mainWindow.webContents.reload()
    }
  })
}

app.whenReady().then(async () => {
  if (!app.hasSingleInstanceLock()) return  // 第二实例：已 quit，不再建窗
  logLine(`壳启动 v${app.getVersion()} UI=${UI_URL} cdp=${CDP_PORT} api=${API_BASE}`)
  // The renderer owns the title bar in the frameless window. Keep the native
  // application menu out of the content area on every desktop platform.
  Menu.setApplicationMenu(null)
  ipcMain.handle("tabs:list", () => Array.from(tabs.values()).map((tab) => ({ id: tab.id, sid: tab.sid, pid: tab.pid || "", type: tab.type, title: tab.title, url: tab.url })))
  ipcMain.handle("window:minimize", () => mainWindow?.minimize())
  ipcMain.handle("window:toggle-maximize", () => {
    if (!mainWindow) return false
    if (mainWindow.isMaximized()) mainWindow.unmaximize()
    else mainWindow.maximize()
    return mainWindow.isMaximized()
  })
  ipcMain.handle("window:is-maximized", () => Boolean(mainWindow?.isMaximized()))
  ipcMain.handle("window:close", () => mainWindow?.close())
  ipcMain.handle("browser:create", (_event, args) => createBrowserTab(args?.pid || process.env.CS_PROJECT_ID || "", args?.sid))
  ipcMain.handle("terminal:create", (_event, args) => createTerminalTab(args?.pid, args?.cwd))
  ipcMain.handle("tab:close", (_event, id) => closeTab(id))
  ipcMain.handle("tab:activate", (_event, id) => activateTab(id))
  ipcMain.handle("browser:navigate", (_event, { id, url }) => { const tab = getTab(id); if (tab?.view) return tab.view.webContents.loadURL(url); return false })
  ipcMain.handle("browser:back", (_event, id) => getTab(id)?.view?.webContents.goBack())
  ipcMain.handle("browser:forward", (_event, id) => getTab(id)?.view?.webContents.goForward())
  ipcMain.handle("browser:reload", (_event, id) => getTab(id)?.view?.webContents.reload())
  ipcMain.handle("browser:devtools", (_event, id) => { const view = getTab(id)?.view; if (view) view.webContents.openDevTools({ mode: "detach" }) })
  ipcMain.on("browser:bounds", (_event, { id, rect }) => setTabBounds(id, rect))
  ipcMain.on("browser:visible", (_event, visible) => setBrowserViewsVisible(Boolean(visible)))
  ipcMain.on("terminal:write", (_event, { id, data }) => getTab(id)?.pty?.write(data))
  ipcMain.on("terminal:resize", (_event, { id, cols, rows }) => { try { getTab(id)?.pty?.resize(cols, rows) } catch (_) {} })
  ipcMain.handle("terminal:kill", (_event, id) => closeTab(id))
  try { await createWindow() } catch (error) { logLine(`主窗创建失败: ${error}`); app.quit() }
})

app.on("window-all-closed", async () => {
  for (const id of Array.from(tabs.keys())) await closeTab(id)
  if (pythonOwner) { try { await fetch(`${API_BASE}/api/admin/shutdown`, { method: "POST" }) } catch (_) {} }
  if (process.platform !== "darwin") app.quit()
})
