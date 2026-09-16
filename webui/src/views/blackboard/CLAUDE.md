# views/blackboard/（评估攻击链画布，E1，DESIGN §12 评估画布）

> 发现 tab「链路」子视图：finding 卡片 × host IP 泳道 × 严重度列，弱/强/链三级边。
> 与逆向 `views/reverse/chains/` **互不复用组件**，只共用依赖（@xyflow/react v12）与 chains API。

## 入口与门控

- `Blackboard.tsx`：发现 tab 过滤行右侧 `[列表｜链路]` 子切换；**仅 `track === "assessment"` 且非 compact 侧栏**才渲染（`showCanvas={!compact && track === "assessment"}`）。compact 直播间侧栏永不挂画布。
- `React.lazy` 动态 import，@xyflow/react（约 192KB）与本画布都不进主包。
- findings/assets 由 Blackboard 父级持有：4s 轮询 + WS bump，IP/sev/status 三件套筛选列表与画布**共用同一 `visible`**；画布只展示、不自己拉 findings。chains 由画布自拉（4s）。
- 画布根 `absolute inset-0`，父级 Blackboard 画布分支须给 `relative min-h-0 flex-1` 定高（高度链见 webui/CLAUDE.md 坑）。

## 文件

- `FindingsCanvas.tsx` — 主视图（ReactFlowProvider 包一层）：工具条 + ReactFlow + 选中边浮卡 + 两个对话框；所有 chains 写操作走 `lib/api`（addChainLink/updateChain/deleteChainLink/createChain），无旁路。
- `canvas.css` — 深色主题覆盖（控件/小地图）。**xyflow 样式表是未分层 plain CSS，层叠压过 Tailwind v4 utilities layer（任意选择器带 `!` 也不稳）**；必须 import 在 `@xyflow/react/dist/style.css` 之后，用根类 `.fc-dark` 提特异性覆盖。泳道背景节点必须注册自定义 `laneBg` 类型（见 FindingNode）——无 type 的节点被 xyflow 按内置 default 渲染成**白卡片+连接点**，深色画布穿帮。
- **C2 画布降噪（2026-09-16）**：①布局双模式——无资产筛选=单一全局泳道（所有发现共用 severity 四列，不按 IP 分块），选 host 才按泳道；②重心排序（barycenter 2 轮往返）+ 强/链边连通分量聚簇——强边两端对齐减少交叉；③info/low 紧凑卡（步距 64）+ 聚焦开关（hover/点选只亮一跳邻接，其余 opacity 0.08）+ 边三态（全部/仅强边/仅链边，localStorage 持久，缺省仅强边）+ 路由（同列直线/跨列平滑折线/跨泳道贝塞尔）+ onlyRenderVisibleElements。
- `canvasModel.ts` — 纯函数模型：`buildLanes`（host 子树→泳道，无 host 归属→「未归属」；泳道内 info/low｜medium｜high｜critical 四列（`sevColumn`，2026-09-15 由三列拆开，LANE_W=COL_W*4；泳道头标签居中公式 `i*COL_W+112` 对任意列数通用）、created_at 堆叠，同时产出 `place` id→槽位与 `laneOf`）、`buildWeakEdges`（同泳道同 vuln_class/同父任务且严重度升级，灰虚，纯推导不入库）、`buildStrongEdges`（evidence.relates_to，青实，label=note）、`buildChainEdges`（链详情相邻 visible finding 成边，label=edge_note，按链状态着色）。
- `FindingNode.tsx` — finding 卡（severity 色条/✓ verified/虚线框 unverified/✕ false-positive/POC 角标，hover Link2=快捷入链）+ LaneHeader（泳道头不占节点）+ LaneBackground（泳道底板，深色无边连接点）；卡片左右两个青色 Handle。data 类型必须 `type` 不用 `interface`（xyflow 的 Record<string,unknown> 约束）。
- `FindingEdge.tsx` — 自定义边：14px 透明命中带 + BaseEdge + EdgeLabelRenderer 标签。
- `AddChainEdgeDialog.tsx` — 手拖连线/强边确认对话框：选已有链或新建（名+goal），**edge_note 必填**；来源不在链先补挂链首（无 note），目标带 note 追加链尾。
- `ChainToolbar.tsx` — 链下拉（名+goal+状态灯+link_count）、＋新建链、状态单向流转 hypothesis→validated→exploited（终态按钮禁用，宁严勿松）。导出 `STATUS_DOT`（黄 #d29922 / 蓝 #58a6ff / 红 #f85149）供边着色共用。

## 关键约定

- **受控节点契约（v12，踩过坑）**：传 `nodes` prop 时 xyflow 在 StoreUpdater 里按**引用相等**比较，变化即 `setNodes` → `adoptUserNodes` 重建 internalNode：派生节点不带 `measured` 会清掉 measured/handleBounds（`parseHandles` 在有 measured 时沿用旧 bounds、无 measured 置 undefined），边整体卸载。本画布做法：`onNodesChange` 回收 `dimensions` change（注意字段名是 **`ch.dimensions`**，不是 measured）存入 `measuredById`，每个派生节点（含 bg/laneHeader）都带上 measured；节点身份只在结构变化（findings/offsets/measured）时变。
- 拖拽：`position` change 实时回写 offsets（相对 `place` 槽位的 dx/dy），`dragging===false` 时落 localStorage `findings-canvas-offsets-v1:<pid>`（只本地不入库）；RotateCcw 清空。
- EdgeLabelRenderer 容器是 `pointer-events:none`，可点的标签/按钮必须自己加 **`pointer-events-auto`**（漏了点标签会穿透到边/pane）。
- 选中联动：选中节点 → 非上下游卡 `opacity-20`、非相关边 opacity 0.15；选中边 → 底部浮卡（strong=加入链，chain=删除链边，window.confirm 后软删 link，相邻边随之变化）；onPaneClick 清空选择。
- **误报边淡出（批 1B，§6.7 的 1.6）**：`findings` 推派生 `fpIds`（status=false-positive），flowEdges 中任一端点命中的边（主要是 strong relates_to；弱边本就不起源于 FP）置 `stale`：opacity 0.25 + `strokeDasharray "3 4"` 点虚线，FindingEdge 标签加 opacity-30+line-through 与「依据已被推翻」title；选中边浮卡对 strong/chain 显琥珀警告。4s 轮询重拉 findings 即生效，无新订阅。
- 详情弹窗复用 `components/blackboard/FindingDetailDialog`（列表同款，POC 复制在里面）。
