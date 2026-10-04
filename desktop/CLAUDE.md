# desktop/

> Electron 桌面壳：把 React UI 装进 `BrowserWindow`，每个浏览器标签渲染为真实 Chromium `WebContentsView`；Python API 经 Electron 远程调试端点（CDP 9222）附着到这些页面，AI 动作与人类输入共用同一页。

## 运行

- `npm install` → `npm start`（`main.js`）。`CS_UI_URL` 指 Vite dev，`CS_PROJECT_ID` 选新标签所属项目，`CS_API_PORT`/`CS_ELECTRON_CDP_PORT` 覆盖端口。
- `main.js` 启动时若 8420 无 API（`apiReady`）会自起 `scripts/serve.py`（`pythonOwner=true`，退出时连带优雅停机）。

## 结构与约定

- `main.js` 主进程：`tabs` Map 管标签；`createBrowserTab`（`human-main` 单例 + 每个 AI sid 一页）/`createTerminalTab(pid,cwd)`（node-pty PowerShell，支持按项目 cwd）；`closeTab` 走 `browser/desktop/detach` 后移除视图。工作台右栏终端复用该原生 tab，切项目时由前端关闭旧 tab，按需新建新项目终端。
- **原生视图可见性**：`WebContentsView` 覆盖在窗口上、不由 React 渲染，可见性只有 `activateTab`/`setTabBounds`/`setBrowserViewsVisible` 控制。`setTabBounds` 把非 active 标签缩到 1×1 并 `setVisible(false)`；**前端切走浏览器模块时必须 `browser:visible=false` 隐藏全部原生视图**（否则留在原位浮在新视图上），重新进入再 `true` 复用同一 activeId 页。
- `preload.js`：`contextBridge` 暴露 `desktopBrowser`（标签/导航/终端/setBounds/**setBrowserVisible**）与 `desktopWindow`（无边框窗控）；类型镜像在 `webui/src/desktop.d.ts`，**改 IPC 面三处同步**（main.js / preload.js / desktop.d.ts）。
- 无边框窗口：原生菜单置空（`Menu.setApplicationMenu(null)`），标题栏由渲染进程自绘（`WindowTitleBar`）。

## 坑

- `WebContentsView` 无 DOM 归属：任何「按 DOM 位置摆放」的覆盖视图都要在组件卸载时显式隐藏，别指望 React 卸载带走它。
- 改 IPC 名/签名务必三处同步，漏改 `desktop.d.ts` 会在 webui `tsc -b` 报错（strict）。
