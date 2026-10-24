const { contextBridge, ipcRenderer } = require("electron")

const listen = (channel, callback) => {
  const handler = (_event, payload) => callback(payload)
  ipcRenderer.on(channel, handler)
  return () => ipcRenderer.removeListener(channel, handler)
}

contextBridge.exposeInMainWorld("desktopBrowser", {
  createBrowserTab: (pid, sid) => ipcRenderer.invoke("browser:create", { pid, sid }),
  createTerminalTab: (pid, cwd) => ipcRenderer.invoke("terminal:create", { pid, cwd }),
  closeTab: (id) => ipcRenderer.invoke("tab:close", id),
  activateTab: (id) => ipcRenderer.invoke("tab:activate", id),
  navigate: (id, url) => ipcRenderer.invoke("browser:navigate", { id, url }),
  goBack: (id) => ipcRenderer.invoke("browser:back", id),
  goForward: (id) => ipcRenderer.invoke("browser:forward", id),
  reload: (id) => ipcRenderer.invoke("browser:reload", id),
  openDevTools: (id) => ipcRenderer.invoke("browser:devtools", id),
  setBounds: (id, rect) => ipcRenderer.send("browser:bounds", { id, rect }),
  setBrowserVisible: (visible) => ipcRenderer.send("browser:visible", visible),
  listTabs: () => ipcRenderer.invoke("tabs:list"),
  onTabUpdated: (callback) => listen("tabs:updated", callback),
  onTerminalData: (callback) => listen("terminal:data", callback),
  terminalWrite: (id, data) => ipcRenderer.send("terminal:write", { id, data }),
  terminalResize: (id, cols, rows) => ipcRenderer.send("terminal:resize", { id, cols, rows }),
  terminalKill: (id) => ipcRenderer.invoke("terminal:kill", id),
  // 原生目录选择（「选择分析包」2026-10-24）：只有桌面壳有系统对话框；
  // 浏览器里这个能力不存在，前端据此回落应用内目录浏览器。
  pickDirectory: (defaultPath) => ipcRenderer.invoke("dialog:pick-directory", defaultPath || ""),
})

contextBridge.exposeInMainWorld("desktopWindow", {
  minimize: () => ipcRenderer.invoke("window:minimize"),
  toggleMaximize: () => ipcRenderer.invoke("window:toggle-maximize"),
  isMaximized: () => ipcRenderer.invoke("window:is-maximized"),
  close: () => ipcRenderer.invoke("window:close"),
  onMaximizedChanged: (callback) => listen("window:maximized", callback),
})
