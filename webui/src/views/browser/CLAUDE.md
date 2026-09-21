# webui/src/views/browser/

> F6 内置浏览器页（DESIGN.md §7）：pentest/redteam 轨专属。**F6-v3 去会话化**——
> 前端零会话 UI，「就是一个普通的浏览器」：人工流量走后端隐式会话 human-main
> （自动创建），AI 会话由 Agent 工具自动管理、任务结束自动清除，前端不感知。

## 组件树

- `BrowserView.tsx` 入口：非 pentest/redteam 轨整页灰显；未装 playwright 顶幅降级
  横幅（install_cmd+复制钮，接口 503 也不 500）；正常 = react-resizable-panels
  三栏（左 动作时间线 / 中 导航+实时画面 / 右 抓包|拦截|重发|爆破 Tabs）。
  轮询只剩 browserStatus 5s + browser.* 事件 3s（v3 删会话/AI 会话轮询）。
- `ActionTimeline.tsx` browser.* 事件 3s 增量（api.events since_id 游标，
  isBrowserEvent 过滤；v3 自 SessionBar.tsx 迁出，**SessionBar 已删**）。
- `NavShotPane.tsx` **URL 栏 + 实时画面**：
  - WS `browserWsUrl(pid)`（**无 sid**）帧流：`{"type":"frame",data,metadata}` →
    base64 JPEG `<img>`；`{"type":"error"}` → 结构性错误不再重连；断线指数退避
    重连 1s→5s；「● 实时（页面静止时自动停推）」角标；空态「连接隐式浏览会话…」。
  - **接管恒显**（后端自动 human-main，无 `startsWith("human-")` 判断）：开启后
    img 容器（tabIndex=0 + outline 焦点）接管 onPointerDown/Up/Move、
    onClick/onDoubleClick、onWheel（preventDefault）、onKeyDown、onPaste（insertText）
    ——坐标按容器 boundingRect 与帧 `metadata.deviceWidth/deviceHeight`（缺省回退
    1280×720）缩放（`frameCoord`），`{type:"input", kind, ...}` 回传 WS；move 50ms
    节流；onClick 用 `React.MouseEvent`（TS2323：PointerEvent 类型不兼容）。
  - 导航 422（目标未登记）→ missing 状态条 +「一键登记资产」→ 自动重试。
- `InterceptPanel.tsx` **F6-v3 拦截（仅人工浏览流量）**：
  - 两个独立开关钮（拦截请求 / 拦截响应 → POST intercept/toggle，返快照）。
  - pending 列表 2s 轮询（方向徽章/method/status/url/倒计时 s）。
  - 行点击 Dialog：原始报文 textarea（editable=false → 只读 `<pre>` 显「二进制
    body 不可编辑」）+「放行（改后）=带 raw / 放行原文 / 丢弃」→ decide；
    422 asset_missing → 一键登记后重提（挂起包一直在等）；404（已裁决/超时）
    行由下一轮轮询自然消失。
  - 超时 120s / 关开关 / 满 50 由后端自动放行原文——前端不处理，只展示倒计时。
- `CaptureHistory.tsx` http_history 3s 增量游标（上限 500 行）；行点击开详情
  Dialog（browserHistoryRow 单条全量；**本项目 dialog.tsx 无 DialogHeader**，
  DialogTitle 直放 DialogContent 内）；「✉ 重发」弹窗预填 `toRawRequest(row)`。
- `ReplayForm.tsx`：`toRawRequest(row)` 纯格式化（请求行+头+空行+体，供预填）；
  `ReplayForm` **单一原始报文 textarea**（v3 删 method/url/headers 控件与
  parseHeaders/fmtHeaders——解析全在后端）→ `{raw}` 202 + pollJob 1s；422 显
  missing 徽章；`IntruderForm` 爆破不动（§POS§ 标记、payload 编辑器、并发硬顶 5）。
  **重发/爆破/拦截裁决人类 UI 专属，Agent 无发起入口（红线）**。

## 节奏与约定

- 轮询：抓包/时间线 3s、拦截 pending 2s、状态 5s；实时画面走 WS 不轮询。
- 导航/重发/爆破/拦截改包目标 host 必须在项目资产表（白名单硬校验，服务端做）；
  前端 422 detail `{host, reason, asset_missing}` 驱动一键登记交互。**接管下页面
  自身跳转放行（等价真人开 Chrome），地址栏仍硬校验**（口径见 DESIGN.md §7）。
- 类型在 lib/types.ts（`InterceptState`/`InterceptPending`）、方法在 lib/api.ts
  （browser* 前缀 + `browserWsUrl(pid)`；browserReplay body=`{capture_id?,raw?}`）；
  202 Job 用 pollJob。
