# webui/src/views/browser/

> 项目内真实 Chromium Page 多标签浏览器：人类主页面与 AI 页面均可观察和接管。

## 组件树

- `BrowserView.tsx` 入口：标签栏显示人类主页面和 AI Page，状态每 2 秒刷新；未手动选定标签时跟随最近动作的 AI Page。右侧抓包工具默认收起。**默认只开一个标签**：挂载时等全局标签表加载完，仅当全局无 `human-main` 才新建一次（`human-main` 是全应用单例，切回模块组件重挂、`tabs` 初值空，不能凭本项目 `tabs.length` 判定否则会重复新建）。**原生视图可见性契约**：真实 Chromium 页是 Electron 覆盖在窗口上的 `WebContentsView`，不由 React 树渲染——本组件挂载时 `desktop.setBrowserVisible(true)`、卸载（切到其它模块）时 `false`，否则切走原生视图会留在原位浮在新视图上（`main.js` 的 `setBrowserViewsVisible`）。
- `ActionTimeline.tsx` browser.* 事件 3s 增量（api.events since_id 游标，
  isBrowserEvent 过滤；v3 自 SessionBar.tsx 迁出，**SessionBar 已删**）。
- `NavShotPane.tsx` URL 栏 + 实时画面：
  - WS `browserWsUrl(pid, sid)` 帧流：`{"type":"frame",data,metadata}` →
    base64 JPEG `<img>`；`{"type":"error"}` → 结构性错误不再重连；断线指数退避
    重连 1s→5s；「● 实时（页面静止时自动停推）」角标；空态「连接隐式浏览会话…」。
  - 接管按钮调用后端 takeover 接口，暂停该 AI Page 输入动作；释放后恢复。开启后
    img 容器（tabIndex=0 + outline 焦点）接管 onPointerDown/Up/Move、
    onClick/onDoubleClick、onWheel（preventDefault）、onKeyDown、onPaste（insertText）
    ——坐标按容器 boundingRect 与帧 `metadata.deviceWidth/deviceHeight`（缺省回退
    1280×720）缩放（`frameCoord`），`{type:"input", kind, ...}` 回传 WS；move 50ms
    节流；onClick 用 `React.MouseEvent`（TS2323：PointerEvent 类型不兼容）。
  - 导航支持任意 URL。
- `InterceptPanel.tsx` 拦截 AI 与人类浏览流量：
  - 两个独立开关钮（拦截请求 / 拦截响应 → POST intercept/toggle，返快照）。
  - pending 列表 2s 轮询（方向徽章/method/status/url/倒计时 s）。
  - 行点击 Dialog：原始报文 textarea（editable=false → 只读 `<pre>` 显「二进制
    body 不可编辑」）+「放行（改后）=带 raw / 放行原文 / 丢弃」→ decide；
    404（已裁决/超时）行由下一轮轮询自然消失。
  - 超时 120s / 关开关 / 满 50 由后端自动放行原文——前端不处理，只展示倒计时。
- `CaptureHistory.tsx` http_history 3s 增量游标（上限 500 行）；行点击开详情
  Dialog（browserHistoryRow 单条全量；**本项目 dialog.tsx 无 DialogHeader**，
  DialogTitle 直放 DialogContent 内）；「✉ 重发」弹窗预填 `toRawRequest(row)`。
- `ReplayForm.tsx`：`toRawRequest(row)` 纯格式化（请求行+头+空行+体，供预填）；
  `ReplayForm` **单一原始报文 textarea**（v3 删 method/url/headers 控件与
  parseHeaders/fmtHeaders——解析全在后端）→ `{raw}` 202 + pollJob 1s；422 显
  missing 徽章；`IntruderForm` 爆破不动（§POS§ 标记、payload 编辑器、并发硬顶 5）。
  重发结果支持复制为 HTTP POC。

## 节奏与约定

- 轮询：抓包/时间线 3s、拦截 pending 2s、状态 5s；实时画面走 WS 不轮询。
- 导航、抓包、拦截、重发、爆破和 AI 浏览器工具允许任意目标，不再要求项目资产表。
- 类型在 lib/types.ts（`InterceptState`/`InterceptPending`）、方法在 lib/api.ts
  （browser* 前缀 + `browserWsUrl(pid, sid)`；browserReplay body=`{capture_id?,raw?}`）；
  202 Job 用 pollJob。
