# views/live/（A3 直播间任务流）

> 直播间第三个 React Flow 图：任务节点 × parent 实线 × 会话私信虚线。
> 经 `React.lazy` 从 LiveRoom 分包加载，@xyflow/react 不进直播间主包。

## 入口

- `LiveRoom.tsx` 第一行页签栏右侧「直播｜任务流」分段切换（原生 button，不用 radix Tabs——mousedown 激活坑见 webui/CLAUDE.md）；切到 flow 时只保留页签行，主区 `<Suspense>` 渲染 `<TaskFlow key={pid}>`。
- 入参三件套：`pausedSids`（LiveRoom 用 sessionStatus(events) 派生的暂停会话集）、`wsBump`（task.*/message.inbox/session.* 事件计数）、`onAttachSession`（挂回页签并切回直播）。

## 文件

- `PlanPanel.tsx` — 直播间类型过滤栏「计划」**特殊标签**（2026-09-16）的结构化面板：选中时由 LiveRoom 渲染本组件替代事件流。GET /tasks 3s 轮询（仅激活时挂载），按 activeTab 过滤（`sess-x`=claimed_by 匹配，`__all`/`__orch`=全项目总览）；任务卡=四态徽章+▦ done/total+步骤列表（○/▶/●/■ 图标对齐后端 _render_plan，blocked 显 note，时间走 datetime.ts）；排序 doing/blocked 步在前→任务态→priority→updated_at；不做事件订阅，纯轮询。
- `OrchChatPane.tsx` — **编排页签对话流（对话化编排器 M1-M3，2026-09-21）**：LiveRoom 在 `activeTab==="__orch"` 时渲染（三段式的 goal 条 + 对话流两段；「运行记录」折叠区留 LiveRoom）。props `{events, busy, persona, goal, onEditGoal, onEditPersona}`——events 过滤 `kind==="orch.chat"` 组装轮（human 开轮 / orch 追加，**孤儿 orch 自成一轮不丢**）；orch 气泡走 MarkdownView（无 raw HTML 防注入）+ tool_trace `<details>` 折叠；busy 显思考行；**接近底部（<60px）才自动滚跟**，上翻不抢滚动条。goal 条入口（设定/编辑/清空）与 🎭 身份设定经回调交 LiveRoom 弹层（GoalEditor/PersonaEditor），本组件零 fetch。
- `EventRow.tsx` — **直播流渲染布局层（2026-09-18，Claude Code 终端风格）**：导出 `StreamItem`（`{type:"pair",command,result?}` 命令对 / `{type:"single",event}`）与 `EventRow({item,open,onToggle,roleNames,action?,onRouteJump?})`。**memo 化（2026-09-20）**：比较器用事件对象引用相等（BBEvent 不可变；delta 原位替换换新对象恰好需重渲）——调用方 `onToggle`/`onRouteJump` 必须引用稳定（LiveRoom 已 useCallback）。分派五形态：①命令对=`● <RUNTIME> <cmd 首行>` 折叠行，展开 IN/OUT 块（`ml-5 border-l pl-3`，OUT 头显 exit/耗时/超时）；result 缺失按 created_at 年龄 <10min 显青点闪烁（悬空兜底），有 result 按 exit_code/timed_out 灰/红。②`llm.thinking`=`● 思考 Ns`（payload.duration_s），展开全文（**非等宽**）；折叠态首行摘要**不依赖 duration_s**（2026-09-18 修复：预览曾被 dur 门控，旧数据（无该字段）折叠成空「思考」行）。`llm.thinking.delta`（2026-09-19 思考流式）也走 ThinkingRow：streaming 态=`running` 脉冲点+「思考中…」label（text-primary），展开正文=payload.thinking 累计全文；终稿到达后该组被 LiveRoom items 跳过。③**`agent.chat`=`AgentChatRow`（2026-09-19）**：正文即行 prose 平铺——无「🤖 Agent 回复」标签、无首行摘要复述（旧式标签+摘要+全文三叠渲染显突兀已废），作者小字靠右，无 text 载荷退回通用 JSON 行。④错误/审批醒目行（`ERROR_KINDS`/`WARN_KINDS` 白名单：红/琥珀点+`border-l-2` 提示条）。⑤其余简洁单行。`lib/events.tsx` 的 eventStyle/eventSummary 只管筛选/标签/摘要，本文件管布局；时间戳列已移除（`title=utcTitle` 悬停显 UTC）；状态点只用 `bg-(--status-*)` 括号语法+内置 `animate-pulse`，无新 CSS 变量。**skill.routed 命中行（F12）**：路由名 span 悬停变色+下划线，单击 `stopPropagation` 跳技能（不再双击、不触发行展开），摘要尾部「· N 分 · 命中词」自拼与 eventSummary 同口径。配对在 LiveRoom 的 `items` useMemo（call_id→就地闭合；旧数据同会话相邻游标降级）。
- `TaskFlow.tsx` — 主视图（自带 ReactFlowProvider）：3s 轮询 `api.taskGraph` + wsBump 300ms 去抖重拉（**本组件不另开 WS**）；选中私信边浮卡；**单击节点弹详情浮卡**（objective 全文 + plan 全步骤逐条 ○/▶/●/■，随 3s 轮询实时跟进；**浮卡尾段=「执行轨迹」**（2026-09-22 execution-trace-chain M1：TaskTraceList 共用组件，max-h-44 滚动，api.taskTrace 现算不落库）；xyflow 层 `onNodeClick` 挂事件——拖拽不误触、删除钮 stopPropagation 天然隔离；与私信边浮卡互斥，pane 点击全清）；节点删除 AlertDialog（四态可删、claimed 硬中断文案、409 子任务错误直显）。
- `TaskNode.tsx` — 任务卡：四态色条/边框（open 灰、claimed 青、done 绿、failed 红）、objective 两行截断、`▦ done/total` + doing 标题 + blocked 琥珀原因、认领会话名（claimed 且暂停时 ⏸ 琥珀）、hover 显删除钮；**单击**看详情（TaskFlow onNodeClick）、**双击**三分支（有活会话挂回直播 / done·failed 开任务窗 / 否则派 window `goto-tasks` 事件跳看板，App.tsx 监听后切任务看板并高亮卡片）；**C10** `↻N` 徽章（node.attempts≥2，来自 graph.py 节点字段）。
- `TaskFlowEdge.tsx` — parent 灰实线箭头（不可点）；inbox 紫虚边 + EdgeLabelRenderer 内 ✉/✉n 圆形标签，点击选中出浮卡（basis_stale=⚠依据撤回 / finding_update=🔵发现增补，refs 全列）。
- `flowModel.ts` — 纯函数：`layoutTasks` parent 深度分列（左→右 DAG），同列按 claimed→open→failed→done、priority、updated_at 堆叠；`planStats`。
- `flow.css` — `.tf-dark` 深色控件覆盖，必须在 xyflow style.css 之后 import（plain CSS 压 Tailwind 层）。

## 关键约定

- **v12 受控节点契约**：onNodesChange 必须回收 `ch.dimensions` 存 measuredById 并回灌每个派生节点，否则重渲染清测量、边整体卸载；拖拽位置相对自动槽位记 dx/dy。
- **首挂丢边坑（实测定论）**：边依赖节点 measured+handleBounds，二者只由 ResizeObserver 首包驱动。①**后台/隐藏标签页** Chromium 冻结 rAF 与 RO 首包（`document.visibilityState==="hidden"`），此时无边是浏览器行为，转可见即恢复——无头自动化必现，真人前台不复现；②dev StrictMode 双挂载可能让 RO disconnect 后不重连。组件内有兜底：setTimeout 链对全部节点 `updateNodeInternals` 强测（**不要用 rAF**，隐藏页会冻），收齐 measured 即停（上限 ~2.4s）；ready 门只控 fitView 时机，**边始终供给，不要两阶段喂空边**（会干扰内部初始化）。
- 手动偏移存 localStorage `taskflow-offsets-v1:<pid>`（只本地不入库），RotateCcw 清空；首次全部节点测量完成后 rAF 触发一次 fitView（maxZoom≤1）。
- 节点 data 类型用 `type` 不用 `interface`（xyflow Record<string,unknown> 约束）。
- EdgeLabelRenderer 容器 pointer-events:none，✉ 标签必须自加 `pointer-events-auto`。
- A5 重排只改 open 任务 priority（task.updated 事件经 wsBump 的 task.* 匹配自动重拉，列内按 priority 重排），无专用接线。
- 后端组装在 `core/blackboard/graph.py`（5 条 set-based SQL 无 N+1）：inbox 边按 `(kind,ref_id)` 聚类、会话映射「最近任务」、closed 会话不映射、同任务对多簇合并一条边 refs 保留全部；建议/proposal 协作边留 TODO(B1 workset)。
