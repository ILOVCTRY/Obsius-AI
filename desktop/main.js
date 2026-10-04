const { app, BrowserWindow, WebContentsView, ipcMain, Menu, session } = require("electron")
const { spawn } = require("child_process")
const path = require("path")
const fs = require("fs")

const ROOT = path.resolve(__dirname, "..")
const API_PORT = Number(process.env.CS_API_PORT || 8420)
const CDP_PORT = Number(process.env.CS_ELECTRON_CDP_PORT || 9222)
const API_BASE = `http://127.0.0.1:${API_PORT}`
const tabs = new Map()
let mainWindow = null
let pythonProcess = null
let pythonOwner = false
let activeId = null

app.commandLine.appendSwitch("remote-debugging-port", String(CDP_PORT))
app.commandLine.appendSwitch("remote-allow-origins", "*")

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
  const url = process.env.CS_UI_URL || `${API_BASE}/`
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

app.whenReady().then(async () => {
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
  try { await createWindow() } catch (error) { console.error(error); app.quit() }
})

app.on("window-all-closed", async () => {
  for (const id of Array.from(tabs.keys())) await closeTab(id)
  if (pythonOwner) { try { await fetch(`${API_BASE}/api/admin/shutdown`, { method: "POST" }) } catch (_) {} }
  if (process.platform !== "darwin") app.quit()
})
