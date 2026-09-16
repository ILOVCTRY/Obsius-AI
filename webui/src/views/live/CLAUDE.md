# views/live/（A3 直播间任务流）

> 直播间第三个 React Flow 图：任务节点 × parent 实线 × 会话私信虚线。
> 经 `React.lazy` 从 LiveRoom 分包加载，@xyflow/react 不进直播间主包。

## 入口

- `LiveRoom.tsx` 第一行页签栏右侧「直播｜任务流」分段切换（原生 button，不用 radix Tabs——mousedown 激活坑见 webui/CLAUDE.md）；切到 flow 时只保留页签行，主区 `<Suspense>` 渲染 `<TaskFlow key={pid}>`。
- 入参三件套：`pausedSids`（LiveRoom 用 sessionStatus(events) 派生的暂停会话集）、`wsBump`（task.*/message.inbox/session.* 事件计数）、`onAttachSession`（挂回页签并切回直播）。

## 文件

- `TaskFlow.tsx` — 主视图（自带 ReactFlowProvider）：3s 轮询 `api.taskGraph` + wsBump 300ms 去抖重拉（**本组件不另开 WS**）；选中私信边浮卡；节点删除 AlertDialog（四态可删、claimed 硬中断文案、409 子任务错误直显）。
- `TaskNode.tsx` — 任务卡：四态色条/边框（open 灰、claimed 青、done 绿、failed 红）、objective 两行截断、`▦ done/total` + doing 标题 + blocked 琥珀原因、认领会话名（claimed 且暂停时 ⏸ 琥珀）、hover 显删除钮；**双击**=有活会话挂回直播、否则派 window `goto-tasks` 事件（App.tsx 监听后切任务看板并高亮卡片）。
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
