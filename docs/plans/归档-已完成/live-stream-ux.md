# 方案：直播间事件流 UX 六项修复（工具语义摘要 / 收尾完整显示 / finding 人话渲染 / 拖选复制 / 时间本地化）

- **状态**：**已实施（M1+M2 全量，2026-09-23「开工吧」授权夜间自主推进）**；决策已沉淀 DESIGN.md §12，本文保留作方案背景
- **实施记录（2026-09-23）**：
  - A1：`TOOL_SUMMARIZERS` 表（events.tsx）登记 7 工具——bb_asset_status（资产值→status+note）/ bb_query（查 what+过滤条件拼装）/ bb_add_asset（value（type））/ complete_task / fail_task（note 200 + awaiting_human 标注）/ finish / bb_add_finding（severity 徽章+title）；TOOL_ARG_KEY 保留为未登记工具兜底；
  - A2：`LEN_SHORT=60`/`LEN_LONG=200` 分级（后端 _truncate_args 上限本就 200，前端对齐即可，后端零改动）；
  - A3：LiveRoom 自建 assets 映射（api.assets 一次 + WS **asset.new**〔方案原文 asset.add 有误，实际事件 kind=asset.new，payload={asset_id,type,value}〕增量维护）经 `SummaryCtx` 传 eventSummary；反查不到显 id 尾 6 位；
  - B2/B3：task.done/task.failed 专属摘要（eventSummary 新增 kind 入参），failed+awaiting_human 标「（待人类处理）」；
  - C1：store.py finding.new/merged payload 补 `title`/`target_asset_id`/`status`（待打磨 #1 拍板：status 入 payload 不渲染）；C2：finding 分支渲染 `[高危] title（vuln_class） · 目标 x`，SEVERITY_COLOR 提升到 lib/events.tsx 共用（Blackboard 改导入）；C3：终兜底改「首个字符串值截 60」，纯结构化才显键名；
  - D1：`selectionCollapsed()` 守卫挂 CommandPairRow/ThinkingRow/通用行/TurnRow 过程钮 onClick（待打磨 #3 拍板：简单守卫即可，精细版不做）；D2 核实展开体无 user-select 限制；
  - E1/E2：`TimeTag` 组件（fmtTime 微标 + `timeTitle` 双标悬停）挂全部行（命令对/思考/Agent 回复/对话轮两气泡/通用行）；E3 OrchChatPane 不动；
  - 待打磨 #4 拍板：bb_query 命中数不做（result_head 文本解析不稳）；#5：初版 7 工具清单即终版，未登记走兜底；
  - 测试：test_blackboard finding.new payload 断言扩展（128 passed）；npm build 零 TS 错误。
- **拍板记录**：§1 审计结论表（根因均已代码坐实；修复方向待用户确认后进实施）
- **关联代码**：`core/agent/tools.py`（dispatch tool.call 发点 :881-899，payload={name, args, ok, duration_s, result_head[:400]}，args 经 _truncate_args 每字符串截 200 :2007-2014；complete_task/fail_task/finish schema :646-680；bb_asset_status :136-159；bb_query :297-324）、`core/blackboard/store.py`（finding.new/merged payload={finding_id, vuln_class, severity, rating_basis, category}——**无 title/无 target_asset_id** :1475-1483）、`core/blackboard/tasks.py`（task.done/failed payload note=全文 :610-621）、`webui/src/lib/events.tsx`（TOOL_LABELS/TOOL_ARG_KEY :161-186、tool.call 摘要分支 :266-293、note 兜底 :378、keys.join 终兜底 :429-433）、`webui/src/views/live/EventRow.tsx`（全行 onClick toggle + title=utcTitle；展开体纯 JSON :331）、`webui/src/lib/datetime.ts`（fmtTime/fmtDateTime/utcTitle 现成，本地时区渲染正确）、`webui/src/views/LiveRoom.tsx`（无 assets state，需自建反查）、`webui/src/views/Blackboard.tsx`（assetName 反查模式 :151-155 与发现页签列表现状 :378-411）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§12 黑板节事件流 / WebUI 节），同步 `webui/CLAUDE.md`、`core/blackboard/CLAUDE.md`（若动 payload）；本文保留作方案背景

## 1. 审计结论总表（6 条反馈 → 根因 → 修复方向）

| # | 反馈 | 根因（已坐实） | 修复方向 |
|---|------|---------------|---------|
| 1 | 工具调用无摘要：看不出看了哪个资产、调用原因 | `bb_asset_status` 摘要取 `asset_id`（不可读 id 非资产值），调用原因 `args.note` 从不显示 | A1 per-tool 摘要组装 + A3 资产 id→value 前端反查 |
| 2 | 黑板查询同样无摘要 | `bb_query` 不在 TOOL_ARG_KEY 表，fallback 取 args 第一个字符串，查询维度（what/过滤条件）不成话 | A1：bb_query 专配「查 what（过滤条件）」 |
| 3 | 任务完成最终描述不完整 | `complete_task` 摘要 60 字截断（TOOL_ARG_KEY 无此工具走 fallback）；fail_task/finish 同病 | B1 收尾三件套截断放宽 200（后端 args 上限）+ B3 task.done 专属摘要行 |
| 4 | 发现（事件流）看不出添加了啥 | `finding.new` payload **无 title 无 target_asset_id**，前端只有 keys.join 键名罗列兜底 | C1 后端 payload 补 title/target_asset_id + C2 severity 徽章摘要分支 |
| 5 | 文字无法鼠标长按选择复制 | 无 CSS 禁选（grep 证实）；根因=整行 onClick 展开折叠：拖选松键后的 click 事件触发行折叠，选择丢失 | D1 onClick 加 selection 守卫：选区非空跳过 toggle |
| 6 | 会话时间显示错误（时区错觉） | datetime.ts 本地渲染全正确；EventRow 全部行**无本地时刻**、悬停 title 只有 UTC 原值（差 8 小时）→ 与 OrchChatPane 气泡（本地 fmtDateTimeMin，正确）对照成错觉 | E1 行内加本地 HH:mm:ss 微标 + E2 悬停改「本地完整时间 · UTC 对照」双标 |

排除项：编排器侧 result_note[:100] 态势截断属 [orchestrator-efficiency.md](orchestrator-efficiency.md) A2 范围，本方案不动（零重叠已确认）。发现页签（Blackboard 列表）现状已可读（severity+title+vuln_class+资产+判级依据），本轮反馈主诉是事件流。

## 2. 设计详述

### 2.1 A：工具调用语义化摘要（#1/#2）

- **A1 per-tool 摘要组装**：`TOOL_ARG_KEY` 单字段模型升级为 `TOOL_SUMMARIZERS: Record<string, (a, ctx) => string | ReactNode>`，登记高频工具；TOOL_ARG_KEY 保留作未登记工具的通用兜底（单字段+60 截断，现状不变）。初版登记：
  - `bb_asset_status`：`资产状态 · <资产值|id 尾 6 位> → <status>` + note 有值时「（note）」——调用原因就地可见；
  - `bb_query`：`查 <what>` + 过滤条件拼装（target_asset_id 反查资产值 / type / status）：`查 assets（目标 http://x.test · status=open）`；
  - `bb_add_asset`：`<value>（<type>）`；
  - `complete_task`/`fail_task`/`finish`：见 B；
  - `bb_add_finding`：severity 徽章 + title（与 C2 同款渲染）；
  - 已有良好摘要的（publish_task/kb_*/browser_* 等）不动。
- **A2 截断分级**：常量 `LEN_SHORT=60`（默认）/ `LEN_LONG=200`（note/result_note/summary 类长文案，后端 _truncate_args 上限 200 对齐，展开体可看全）。
- **A3 资产反查**：LiveRoom 自建 assets 映射（`api.assets(pid)` 一次 + WS `asset.add` 增量维护，照 Blackboard assetName 模式）；经 eventSummary 新增可选第三参 ctx（`{ assetName?: (id) => string | undefined }`）传入。反查不到（事件先于资产列表到达/历史回放）→ 显 id 尾 6 位，不阻塞渲染。

### 2.2 B：收尾与任务完成完整显示（#3）

- **B1 收尾三件套**（complete_task/fail_task/finish）摘要截断放宽到 200，result_note/summary 基本完整呈现；
- **B2 fail_task 语义标注**：`✗ 任务失败（awaiting_human：需要人类…）`，blocked_reason 中文标注（error→真失败 / awaiting_human→待人类）；
- **B3 task.done/task.failed 事件行专属摘要**：`✅ 任务完成 · <note 全文>`（payload note 本就全文，tasks.py:610 现走裸 note 兜底无图标与失败标注），与收尾 tool.call 行图标呼应，一眼分清「动作」与「结果」。

### 2.3 C：finding 人话渲染（#4）

- **C1 后端补字段**（store.py:1475-1483 两行）：finding.new / finding.merged payload 增加 `title` 与 `target_asset_id`——加字段向后兼容，事件表增量每条几十字节；
- **C2 前端摘要分支**：`[高危] SQL 注入（vuln_class） · 目标 <资产值>`——severity 中文徽章（critical 严重 / high 高危 / medium 中危 / low 低危 / info 信息，色值复用 SEVERITY_COLOR，从 Blackboard 提升到 events.tsx 共用）；title 缺省（C1 前的旧事件）回退显 vuln_class；
- **C3 终兜底修复**：无匹配分支从「键名罗列」（keys.join）改为「首个字符串值截 60」，纯结构化对象才显键名——任何事件行至少给一条可读信息。

### 2.4 D：行内文字选择复制（#5）

- **D1 selection 守卫**：EventRow 通用行与专属行（CommandPairRow/ThinkingRow/AgentChatRow/TurnRow）的 onClick 统一加——`window.getSelection()` 非空则跳过 onToggle（提取共用帮助函数）。拖选松键后的 click 不再折叠行，选择完整保留；
- **D2** 展开体/prose 行确认无 user-select 限制，纯点击冲突问题，守卫即解；
- 边界（可接受）：上一次选择未清除时首次点击不折叠，需再点一次——宁多一次点击不丢选择；精细版（mousedown 快照选区）进待打磨。

### 2.5 E：时间本地化（#6）

- **E1 行内本地时间微标**：全部事件行行头尾部加 `fmtTime(created_at)`（HH:mm:ss，10px 等宽弱色；datetime.ts 现成），提取 TimeTag 小组件统一挂；
- **E2 悬停双标**：title 改 `fmtDateTime(created_at)（本地） · UTC 原值`——本地完整时间为主、UTC 对照次之，消除「差 8 小时」错觉源；
- **E3** OrchChatPane 气泡已正确（fmtDateTimeMin），不动。

## 3. 实施切分建议（待用户排期）

| 里程碑 | 内容 | 依赖 |
|---|---|---|
| M1 事件流前端批次 | A1-A3 + B1-B3 + C2/C3 + D1 + E1/E2（全落 events.tsx / EventRow.tsx / LiveRoom 传参） | 无 |
| M2 后端 finding 补字段 | C1（store.py 两行）+ 测试断言扩展 | 独立（C2 对缺 title 旧事件有回退，先后皆可） |

测试面：后端 finding.new payload 断言补 title/target_asset_id（test_store 相应用例）；前端 `npm run build` 零 TS 错误；手测清单——直播间拖选复制、行内时间微标、finding 徽章行、bb_asset_status/bb_query 摘要、complete_task 全文显示、悬停双标。

## 4. 待打磨清单

1. C1 是否再补 `status`（unverified/verified）入 finding payload——事件行要不要显验证状态；
2. 发现页签卡片是否补 impact 首行副文本（超出本轮主诉，可选）；
3. D1 selection 残留边界是否需要 mousedown 快照精细版；
4. bb_query 摘要是否解析 result_head 显命中数（result_head 为文本解析不稳，倾向不做）；
5. TOOL_SUMMARIZERS 初版登记清单确认（高频 8 个 + 兜底）是否覆盖用户常用视图。
