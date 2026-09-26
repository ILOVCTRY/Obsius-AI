# Web Fuzzer：快速 HTTP 报文重放（POC 验证工作台）

> 借鉴 Yakit Web Fuzzer（截图，2026-09-24 用户提供）：渗透验证场景的核心动作=
> 「改一行报文 → 发送 → 看完整响应 → 再改」的秒级迭代。把现有藏在浏览器右栏的
> 单 textarea 重发，升级为独立双栏重放工作台。多请求爆破仍归 Intruder，本方案只做**单请求迭代**。

- **状态**：讨论收敛（2026-09-24，含完整设计；待用户过目后排期实施）
- **拍板记录**：见 §2 决策表（D1-D5；均为工程常规决策——后端零新端点、沿用既有白名单与人类专属红线）
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
