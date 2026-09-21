# core/browser/

> F6 内置浏览器（DESIGN.md §7）：Playwright 托管 Chromium + 抓包/拦截/重发/爆破。
> 渗透/红队轨专属（轨门控装配在 core/api/app.py）；未装 extra 一切优雅降级不 500。
> **F6-v3 去会话化**：人工=隐式会话 `human-main`（懒创建、不计并发上限、永不清）；
> AI=按 agent 会话 id 建 Page（`_browser_pair` 幂等重建），任务收尾自动 close。

## 文件

- `policy.py` 目标白名单（纯逻辑零 playwright）：`normalize_target`（无 scheme 补
  http://、IPv6 方括号剥离、小写）+ `check_target`（IP→host 精确 / domain→精确+子域
  从左剥标签 ≤2 层（`_SUBDOMAIN_MAX_STEPS`），`domain_scope="exact"` 收紧 / url 资产
  hostname 相等）+ `deny_event`（browser.deny）。**导航前零网络副作用（不做 DNS）**。
- `pool.py` 实例池：`BrowserConfig`（config/browser.json 缺文件全默认；v3 补
  `intercept_timeout_s=120.0`）、`HUMAN_MAIN_SID="human-main"`、`ensure_human_session()`
  （API 层懒创建唯一入口）、`BrowserInstance`（每项目一个常驻，Page=会话，并发上限 2
  **只数 AI 页**；`__init__` 注入 `self._intercept=InterceptHub(...)`，会话关/实例停
  `cancel_all` 放行未裁决包）、`BrowserPool`、`browser_available()`/`chromium_available()`。
- `httpmsg.py` **原始报文解析/渲染纯函数层**（零依赖，重放与拦截共用）：
  `parse_raw_request(text, base_url=)`（相对路径拼绝对/裸 authority 换 netloc；坏行
  ValueError→422）、`parse_raw_response`、`render_raw_request/render_raw_response`
  （二进制 body → `<binary:N bytes>` 占位 + editable=False）、`BINARY_PLACEHOLDER_RE`。
- `intercept.py` 拦截枢纽：`PendingHold`（hold_id=`ih-<12hex>`、raw、editable、
  future）+ `InterceptHub`（挂 `inst._intercept`；req/resp 两独立开关；pending
  threading.Lock；MAX_PENDING=50 溢出自动放行原文）。API 线程：snapshot/toggle
  （关开关自动放行该方向全部）/decide（KeyError=不存在/已裁决/已超时→404）。
- `capture.py` 抓包+拦截：`CaptureTap` 附着 persistent context（`route("**/*")`）。
  `_on_route` 五段：①请求向 hold（fetch 前，仅 human-main 且开关开）→ ②
  `route.fetch(**覆写)` → ③响应向 hold → ④fulfill（改 body 走 status/headers/body，
  否则 `fulfill(response=resp)`）→ ⑤`_store` **裁决后按改后值入库**
  `meta.intercept={request_modified,response_modified,hold_ms}`。drop→`route.abort()`
  不入库落 `browser.intercept` 事件。`normalize_body` 三方共用规则（文本 utf-8 /
  其余 base64+is_binary / >64KB 截断）。
- `replay.py` 重发+爆破（纯 httpx 零 playwright，恒可测）：`ReplayClient.replay`
  （v3 签名 `capture_id?/raw?`——raw 经 parse_raw_request 解析 modified=True）；
  `Intruder.run`（§POS§ 标记、token-bucket 限速、并发硬顶 5、max_requests/stop_event/
  连续 10 次连接失败三停）。

## 红线

- **import 红线**：全包任何模块 import 时**不得 import playwright**——只在
  `BrowserInstance` loop 线程内延迟 `from playwright.async_api import ...`
  （未装 browser extra 时 `import core.browser` 不炸，no-tool 降级的前提）。
- **线程模型**：全项目调用方是同步线程，playwright sync API 绑定创建线程 →
  每实例独占 asyncio loop 线程（daemon，`run_forever`），公开 API 统一
  `_submit(coro, timeout)` = `run_coroutine_threadsafe(...).result(timeout)`，
  全部异常转 `BrowserError`；loop 线程内异常全捕获不杀线程。
- **拦截三红线（v3）**：①只挂 human-main 流量（判定在 capture._on_route，
  intercept.py 不感知 sid）；②**asyncio.Future 只能在实例 loop 线程
  set_result**——API 线程一律经 `inst._loop.call_soon_threadsafe`；③改后 URL
  必须过 check_target（API 层 422 asset_missing，挂起包继续等修正重提）。
- **profile 锁与删除顺序**：`user_data_dir` 是进程级排他锁——删除项目目录前
  **必须先 `BrowserPool.close_project(pid)`**（core/api delete_project 已接线），
  否则 Windows 上 rename 必 422。
- **白名单语义宁严勿松**：未命中一律拒绝 + browser.deny 事件；重发/爆破/拦截
  改包目标同样过 `check_target`（人类 UI 与 AI 同池同白名单）。
- **爆破/重发/拦截裁决人类 UI 专属（红线）**：Agent 工具面无任何发起入口，只能读
  http_history；唯一入口是 core/api 的 browser/replay 与 browser/intruder 端点。
- **接管输入 human-* 专属（红线，F6-v2）**：`human_input(sid, kind, **kw)` 对
  非 `human-` 前缀会话一律 BrowserError——AI 会话只读观看，人机不抢同一 Page
  输入。注入用 Playwright page.mouse/keyboard API（不用裸 CDP Input.dispatch*）。

## 实时画面流与接管（F6-v2）

- **CDP screencast**：`attach_screencast(sid, sink)` / `detach_screencast(sid,
  sink)`（每 sid 多订阅者，`_casts` dict；首订阅者懒起 `Page.startScreencast`、
  末订阅者退出即停流）。清理覆盖三条路径：close_session / `_aclose` / 末订阅者。
- **`Page.screencastFrameAck` 必须回**（用事件里的 `sessionId` ack）——否则
  Chromium 推 2 帧即停。帧 `{data: base64 JPEG 原样不重编码, metadata, ts}` 逐个
  分发给 sink（单个 sink 异常只摘除它自己，不炸分发）。
- **sink 线程模型**：CDP 回调跑在实例 loop 线程；sink（core/api 的 WS 桥）内部
  再 `call_soon_threadsafe` 投进 uvicorn 事件循环的 `asyncio.Queue`——pool 层
  不感知 uvicorn，队列满丢帧保最后是 WS 桥的职责。
- **审计口径**：click/dblclick/wheel/key/type 落 `browser.action(action=
  "human-input", origin="human")`（text/键名截 80）；move/down/up **不审计**
  （move 50ms 节流在前端做，仍防洪泛）。

## 审计与坑

- 事件：`browser.action`（每动作，author=owner，origin=human|agent 按 sid 前缀）、
  `browser.deny`、`browser.download`、`browser.capture_error`、`browser.intercept`
  （drop/裁决留痕）、`browser.intruder.start/done`（批次级）。
- http_history 只走 `Blackboard.add_http_history` 唯一写入口（schema v15）。
- route 拦截的 `resp.body()` 是协程必须 `await`（async API）；playwright async 的
  route.abort/continue_/fulfill/fetch **全是协程必须 await**；WebSocket/流式大文件
  fetch 失败走 `continue_()` 兜底不入库（宁缺勿卡）。
- 改包头必须剥：请求向 content-length/host（fetch 重算）、响应向
  content-length/content-encoding（body 已解压明文）。
- `_store` 的 mime 查找**大小写不敏感**（改包报文里用户可写 `Content-Type`）。
- 截图 PNG bytes；AI 侧由 dispatcher 落 `artifacts/browser-shots/` + artifact 事件。
