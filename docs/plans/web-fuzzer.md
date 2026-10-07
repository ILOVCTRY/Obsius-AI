# Web Fuzzer：快速 HTTP 报文重放（POC 验证工作台）

> 借鉴 Yakit Web Fuzzer（截图，2026-09-24 用户提供）：渗透验证场景的核心动作=
> 「改一行报文 → 发送 → 看完整响应 → 再改」的秒级迭代。把现有藏在浏览器右栏的
> 单 textarea 重发，升级为独立双栏重放工作台。多请求爆破仍归 Intruder，本方案只做**单请求迭代**。

- **状态**：**M1 已实施（2026-10-07，见文内 §0 实施记录）；M2/M3 待排期**
- **拍板记录**：见 §2 决策表（D1-D5；均为工程常规决策——后端零新端点、沿用既有白名单与人类专属红线）

## 0 实施记录（2026-10-07）

**已落地**：独立顶级「重放」视图（`webui/src/views/fuzzer/`，NavRail 第 9 项，轨门控 pentest/redteam/ctf）+ 双栏工作台（react-resizable-panels 默认 50/50）+ 响应区（状态行/响应头·体 tab/JSON 美化/字符集重解/定位）+ 历史抽屉（source=replay，打开才拉）+ 抓包行「去重放台」与直播间顶栏 🧪 两处入口（`goto-fuzzer` CustomEvent 带 raw 预填）。

**补修（2026-10-07）**：
- `httpmsg.parse_raw_request` 增 `Host` 头兜底——贴标准 origin-form 报文（相对路径请求行 + `Host` 头，DevTools/Burp/Yakit 复制出来的即此形态）此前因无 `base_url` 直接 422「相对路径 URL 需要 base_url 兜底」；现 `base_url` 为空时用报文自身 `Host` 拼 `http://<host>`（报文不带 scheme 故默认 http，https 由「强制HTTPS」重写）。绝对 URL 与抓包预填链路不受影响；错误文案改为面向用户。
- `replay.ReplayClient.replay` 剥 `Content-Length`/`Transfer-Encoding`——改包后 body 长度常变，由客户端按实际 body 重算 framing（同 Burp/Yakit 自动重算，与 capture 拦截路径同口径），否则 httpx 报「Too little data for declared Content-Length」请求发不出去。
- 前端响应区改**完整原始报文单栏**（去「响应体/响应头」tab）；双栏 `defaultSize` 修 `"50%"` 字符串（v4 里 number=px，曾塌成 20%）。

**与本文设计的偏离（实施时用户拍板，以此为准）**：
1. **D5「后端零新端点」不再成立**——用户要求「全量对齐截图」，控件超出本方案 D2/D3 与 §5「明确不做」的范围：新增 强制HTTPS / 国密TLS / 跟随重定向开关 / 设置代理（显式）/ 响应体长度限制 / 停止 / 构造请求。后端 `ReplayClient.replay` 扩 `ReplayOptions` + `stop_event`，`POST /browser/replay` 扩参并返回 `run_id`，新增 `POST /browser/replay/{run_id}/stop` 与 `GET /browser/gm-status`。
2. **§5「明确不做」的 TLS/设置代理/响应体长度限制三项本期做了**；代理语义定为**显式代理**（绝不跟随系统代理，`trust_env=False` 红线不破）。
3. **国密TLS 走 Go sidecar**（本方案未涉及）：Python 生态无带 SM 密码套件的 TLS 栈，故新增 `tools/gmhttp/`（Go + tjfoc/gmsm gmtls）+ `scripts/build_gmhttp.py`；二进制缺失=国密不可用、开关置灰，**绝不静默降级**。
4. **BrowserView 右栏「重发」tab 未退役**（本方案 D1/§2.2 曾计划收口）——本次只做「新增工作台」，不删既有 UI；是否收口待后续拍板。
5. **422 一键登记资产（D3）未做**——用户拍板「保持任意目标」，不设门禁。
6. 编码工具钮（§3 M3）未做。

**剩余**：M2（FindingDetailDialog 预填 / 历史两条 Diff / curl 导入 / HTML 预览 / CodeMirror）与 M3（编码工具钮 / 一键挂 finding 证据）待排期。
- **关联代码**：
  - `core/browser/replay.py`（`ReplayClient.replay`：raw 原始报文解析/白名单/入 http_history source="replay"，已完备）、`core/browser/httpmsg.py`（parse/render 报文纯函数）、`core/browser/policy.py`（check_target）
  - `core/api/app.py`（`POST /browser/replay` 202 Job、history GET/单行 GET 已全）
  - `webui/src/views/browser/ReplayForm.tsx`（现有重发表单，h-64 textarea + max-h-40 结果块；`toRawRequest` 可复用）、`webui/src/views/browser/CaptureHistory.tsx`（「✉ 重发」入口）、`webui/src/views/LiveRoom.tsx`（顶栏会话页签行）、`webui/src/App.tsx`（View 导航/goto-* 事件模式）
  - `webui/src/lib/api.ts`（browserHistory/source 过滤、browserHistoryRow、browserReplay 三方法现成）

## 1 背景与现状盲区

后端重放链路已经完整且经过事故加固（trust_env=False 防系统代理劫持、连接失败也入库、目标白名单硬校验）。盲区纯在前端形态：

1. **空间局促**：重发表单在 BrowserView 右栏（300–560px）内，报文框 h-64、响应体 max-h-40——看不全响应头/长响应，POC 验证要反复滚动。
2. **迭代无历史**：改包再发后上一条响应被覆盖，无法回看/对比；历史要去「抓包」tab 混在浏览流量里翻 source=replay。
3. **入口深**：要先到「浏览器」视图 → 右栏「重发」tab；用户在会话页看 Agent 打不上去时，不能当场开重放台手工验 POC。

## 2 设计定稿

### 2.1 决策表

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 形态与入口 | 新增独立顶级视图「重放」（NavRail 第 9 项，pentest/redteam/ctf 门控，其余轨整页灰显）；App 的 View 加 `"fuzzer"`，监听 `goto-fuzzer` CustomEvent `{raw?}`（仿 goto-session 模式）。**LiveRoom 顶栏会话页签行加「🧪」图标钮**——用户明确要求渗透页面有入口；抓包行「✉ 重发」改为**跳转重放视图预填**（不再弹小 Dialog） |
| D2 | 主界面 | 左右双 Panel（react-resizable-panels，可拖拽默认 50/50、各最小 280px）：左=原始报文编辑器（等宽 textarea，发送后内容保留），右=响应区。顶部细工具条：**发送（Alt+Enter / Ctrl+Enter）**、📜历史、⬆导出、⬇导入、✕清空。发送中按钮禁用，不做请求排队 |
| D3 | 响应区 | 状态行：`状态码 · 耗时ms · 字节数`（连接失败显红色 meta.error）；子 tab「响应头 / 响应体」；响应体两子模式「原文 / 美化」——美化自动识别 JSON（parse+2 空格缩进，失败回原文并置灰）；显 content-type、body_truncated 标注；is_binary 显占位+下载（前端用行数据直接 Blob 下载，零新端点）。**422 asset_missing → 错误区「一键登记资产」（POST /assets 自动识别，复用 NavShotPane 现成模式）后自动重发** |
| D4 | 历史抽屉 + 导入导出 | 左侧滑出抽屉：本项目 source=replay 历史（list 接口 source 过滤，倒序 cap 100；抽屉关闭不轮询、打开才拉取），行=方法+路径+状态色点+ms+时间；点击回填编辑器并载入该次完整响应（单行 GET）。导入/导出纯前端：导出当前报文为 `.http`（Blob）、导入 `.txt/.http`（FileReader 填入） |
| D5 | 审计与红线（声明沿用，零变更） | 人类 UI 专属红线不变：Agent 无任何发起入口，只能经 http_history 只读结果；白名单 check_target + trust_env=False + http_history 入库=审计三合一全部现成；**不新增事件类型**（单发行本身即审计，不套 Intruder 的 start/done 批次事件） |

### 2.2 关键机制

**POC 迭代闭环**：报文在编辑器里改 → Alt+Enter → 202 Job（现成 browserReplay）→ pollJob → 响应区落结果；报文不动、响应可从历史抽屉按次切回。M1 不做两条响应 diff（M2），状态行的字节数/状态码对比已能覆盖「改 payload 后响应是否变化」的大半判断。

**BrowserView 同步收口**：右栏「重发」tab 退役（Tabs 减为 抓包/拦截/爆破 三项），重发操作由抓包行「✉ 重发」承担且更强（跳工作台预填）。浏览器视图职责=浏览+实时画面+拦截+抓包，重放视图职责=POC 报文工作台，两处不再维护重放 UI 两份。

**与既有能力边界**：多请求/标记位/并发 → Intruder（人类专属红线）；Agent 手工验 POC 的需求本期不做（Agent 的验证路径=run_cmd/verify 规格，人机分工红线不变）；MITM/TLS 配置不进本视图。

## 3 实施切分

### M1 重放工作台闭环（建议一次落地）

1. `App.tsx`：View 加 `"fuzzer"` + NavRail 项（轨门控灰显）+ `goto-fuzzer` 监听（带 raw 预填）。
2. `webui/src/views/fuzzer/`（新目录，同步建 CLAUDE.md）：
   - `FuzzerView.tsx`（双 Panel + 顶部工具条 + 轨门控）
   - `RequestPane.tsx`（raw 编辑器、快捷键发送）
   - `ResponsePane.tsx`（状态行 + 响应头/体 tab + JSON 美化 + 失败/422 资产登记）
   - `HistoryDrawer.tsx`（source=replay 历史、回填/载入）
3. 接线：`CaptureHistory.tsx` 「重发」改 dispatch `goto-fuzzer` 带 `toRawRequest(row)`；`BrowserView.tsx` 重发 tab 退役；`LiveRoom.tsx` 顶栏加 🧪 钮（空报文新 blank）。
4. `api.ts/types.ts`：M1 全部复用既有方法，预计零新增。
5. 测试与验证：后端零改动→无后端新测试，靠现有 replay 用例回归；前端 `npm run build` 零 TS 错误；手工冒烟矩阵（见下）。

### M2 迭代增强（按实战反馈排期）

1. FindingDetailDialog「🧪 验证 POC」：从 evidence.repro_steps 的 cmd/报文预填重放台。
2. 历史抽屉多选两条 → **响应 Diff**（复用 DiffView；状态/长度/正文差异高亮）——盲打 POC 核心场景。
3. curl 命令导入（后端加 curl→raw 解析，shlex 起步）。
4. HTML 渲染预览（sandbox iframe srcdoc，禁沙箱外权限）。
5. CodeMirror 替换 textarea（报文高亮；先评估 bundle 体积）。

### M3（观察后再议）

- 编码工具钮（Unicode 转义/URL/Base64 一键解码，呼应截图「编码」按钮）。
- 一键把已验证重放挂为 finding 证据（人工确认后写 evidence）。

## 4 风险与对策

| 风险 | 对策 |
|------|------|
| 顶级导航膨胀（8→9 项） | 门控三轨；浏览器/重放职责显式切分，不开第二个同类入口；后续小工具走弹层不再加导航 |
| 重发 tab 退役被视为功能砍除 | 抓包行「重发」操作保留且更强（预填+完整工作台）；行为无删除只是换落点 |
| 大响应/二进制撑爆前端 | body_max 截断标注已有；二进制走 Blob 下载不入 DOM；spill 类逻辑不套用于此 |
| textarea 编辑器体验弱（无高亮） | raw 报文迭代对高亮依赖低，M1 先闭环；CodeMirror 留 M2 按体积评估 |
| 422 登记资产后忘记重发 | 登记成功回调内自动重放原报文（同 NavShotPane 重试语义），用户零额外操作 |

## 5 明确不做

- Yakit 左侧配置全家：TLS/真实 Host/设置代理/代理检测、前端渲染数量、响应体长度限制、SNI、批量目标、随机分块、并发/重复发包——多请求一律 Intruder；系统代理已由 trust_env=False 硬忽略。
- Fuzztag/yak.fuzz 变量语法（Intruder 的 §标记§ 已覆盖替换需求）。
- HEX 编辑器、Cookie/JWT 专有插件、HEX/编码转换（M3 再议）。
- AI 发起重放 / AI 自动消费重放结果（人类专属红线，信号给人不给 Agent 动作）。
- 任务中途的任何自动化退出语义（与 stuck-convergence D6 边界一致，无关本方案）。
