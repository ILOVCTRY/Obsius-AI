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
- **顶层导航守卫（2026-10-06 白屏事故）**：主窗只允许停在 `CS_UI_URL` 同源——`will-navigate`/`will-redirect` 拦截其余并 `shell.openExternal` 交系统浏览器；`setWindowOpenHandler` 一律 `deny`（`target="_blank"`/`window.open` 外开，不在壳里再开裸窗）。**无守卫时任何顶层导航都会把无边框窗口带走**：没有地址栏/后退键，用户永久卡在空白页，整窗只剩 `BrowserWindow.backgroundColor`（实测主窗停在外部站点的 39 字节空文档，`history.length=3`）。**守卫只挂主窗 `webContents`**——内置浏览器 `WebContentsView` 标签必须能自由导航到任意目标（http/https 任意目标；**非 http(s) 协议另设守卫**，见下条）。
- **标签协议守卫（2026-10-07）**：标签 `webContents` 挂 `will-navigate`/`will-redirect` 拦**非 http(s)/about 协议**——此前网页里的自定义深链（抖音 `bytedance://`）无拦截，Chromium 当外部协议交给 OS，Windows 弹「获取打开此'bytedance'链接的应用」系统窗；`setWindowOpenHandler` 把 `_blank`/`window.open` 的 http(s) 改在**当前标签**打开（避免冒出裸 `BrowserWindow`），其余 `deny`。**该守卫同时覆盖 AI 页**（AI 与人类共用同一 `WebContentsView`，Playwright 经 CDP 9222 附接）。
- **单实例锁**：`requestSingleInstanceLock()` 拿不到即 `app.quit()`（`whenReady` 内再以 `hasSingleInstanceLock()` 提前 return）；`second-instance` 聚焦已有窗，若它已被导航离开 UI **或渲染进程已死**则重载自愈（见下条）。此前可多开，两实例共用同一 Chromium `user-data-dir`（CDP 端口/缓存/存储互踩，后启动的连 9222 都绑不上），且出现空白窗时用户无从判断该关哪个。
- **主窗渲染进程死亡自愈（2026-10-07 白屏事故）**：主窗渲染进程一旦退出，窗口只剩 `BrowserWindow.backgroundColor`（无边框/无地址栏/无菜单）＝用户看到的「白屏」，而且**重启应用也救不回来**——新实例拿不到单实例锁直接 `app.quit()`（实测 exit=0），`second-instance` 原先又只按「URL 非同源」自愈，而渲染进程死后 `getURL()` **仍报 UI 源**，同源判断直接漏掉白屏。现三处兜底：①主窗挂 `render-process-gone` → 记日志 + 退避重载（1/2/4/8/15s；连续 5 次为上限，加载成功后稳定 60s 才清零计数，防重载风暴）；②`second-instance` 自愈条件改为「非同源 **或** `mainCrashed`」；③手工补回 `Ctrl+R`/`F5` 重载、`Ctrl+Shift+I` DevTools——`Menu.setApplicationMenu(null)`（无边框窗口）会把 Electron 默认加速键一起删掉，否则窗口里没有任何刷新入口。壳级事件（启动/崩溃/无响应/自愈）一律落 `logs/desktop.log`（gitignored）。

## 坑

- `WebContentsView` 无 DOM 归属：任何「按 DOM 位置摆放」的覆盖视图都要在组件卸载时显式隐藏，别指望 React 卸载带走它。
- 改 IPC 名/签名务必三处同步，漏改 `desktop.d.ts` 会在 webui `tsc -b` 报错（strict）。
- **白屏排查口诀**：先看进程表有没有 `--type=renderer` 子进程——**没有＝渲染进程已死**（窗口只剩背景色），此时 CDP 的 `Runtime.evaluate` 超时、`Page.captureScreenshot` 报 Internal error，但 `Page.reload` 即可救活；死因看 `logs/desktop.log`（`reason=` 字段）。注意 CDP 注入按键验加速键要用 `rawKeyDown`，`keyDown` 走不到 `before-input-event` 的 pre-handle 路径。
