# views/blackboard/（评估攻击链画布，E1，DESIGN §12 评估画布）

> 发现 tab「链路」子视图：finding 卡片 × host IP 泳道 × 严重度列，弱/强/链三级边。
> 与逆向 `views/reverse/chains/` **互不复用组件**，只共用依赖（@xyflow/react v12）与 chains API。

# views/blackboard/（评估攻击链画布 E1 + 黑板链路图，DESIGN §12）

> 发现 tab「链路」子视图：finding 卡片 × host IP 泳道 × 严重度列，弱/强/链三级边。
> 与逆向 `views/reverse/chains/` **互不复用组件**，只共用依赖（@xyflow/react v12）与 chains API。
> `boardGraph/` 子目录=黑板链路图（2026-09-20，全轨第 4 tab「全景」），见文末。

## 入口与门控

- `Blackboard.tsx`：发现 tab 过滤行右侧 `[列表｜链路]` 子切换；**仅 pentest/redteam 轨且非 compact 侧栏**才渲染（`showCanvas={!compact && (track === "pentest" || track === "redteam")}`；历史上写 assessment，R2 拆轨后按轨名判断）。compact 直播间侧栏永不挂画布。
- `React.lazy` 动态 import，@xyflow/react（约 192KB）与本画布都不进主包。
- findings/assets 由 Blackboard 父级持有：4s 轮询 + WS bump，IP/sev/status 三件套筛选列表与画布**共用同一 `visible`**；画布只展示、不自己拉 findings。chains 由画布自拉（4s）。
- 画布根 `absolute inset-0`，父级 Blackboard 画布分支须给 `relative min-h-0 flex-1` 定高（高度链见 webui/CLAUDE.md 坑）。

## 文件

- `FindingsCanvas.tsx` — 主视图（ReactFlowProvider 包一层）：工具条 + ReactFlow + 选中边浮卡 + 两个对话框；所有 chains 写操作走 `lib/api`（addChainLink/updateChain/deleteChainLink/createChain），无旁路。**单向零重叠（2026-09-22，findings-canvas-dag-layout 全量）**：锁拖拽（`nodesDraggable=false`）、MiniMap 折叠开关（title 展开小地图，localStorage `findings-minimap:<pid>`）、`hoveredEdgeId` hover 注记、corridorOff 同走廊多边 ±3px 阶梯（key=`g1/gd:<laneKey>:<col>`，`(i-(n-1)/2)*3`，n=1 居中 0）、crossBandY=**所有**泳道 height 最大值+24（中间泳道可能更高，勿只取两端）、`interactionWidth: 14` 命中带（自定义透明命中 path 已删）、弱边 `isRightward` 过滤不画左向。
- `canvas.css` — 深色主题覆盖（控件/小地图）。**xyflow 样式表是未分层 plain CSS，层叠压过 Tailwind v4 utilities layer（任意选择器带 `!` 也不稳）**；必须 import 在 `@xyflow/react/dist/style.css` 之后，用根类 `.fc-dark` 提特异性覆盖。泳道背景节点必须注册自定义 `laneBg` 类型（见 FindingNode）——无 type 的节点被 xyflow 按内置 default 渲染成**白卡片+连接点**，深色画布穿帮。
- **C2 画布降噪（2026-09-16）**：①布局双模式——无资产筛选=单一全局泳道（所有发现共用一个三分区画布，不按 IP 分块），选 host 才按泳道；②重心排序（barycenter 2 轮往返）+ 强/链边连通分量聚簇——强边两端对齐减少交叉；③~~info/low 紧凑卡~~（**2026-09-18 已废**：info 停收后前提消失，全部统一完整卡，行距恒 CARD_STEP——`isCompact` 仅剩三分区「噪声/孤立」分拣用途）+ 聚焦开关（hover/点选只亮一跳邻接，其余 opacity 0.08）+ 边三态（全部/仅强边/仅链边，localStorage 持久，缺省仅强边）+ 边路由已升级三形态正交（2026-09-22，见 FindingEdge 条目）+ onlyRenderVisibleElements。**卡片放大（2026-09-18）**：CARD_W 224→320（w-80）、CARD_STEP 118→148、LANE_GAP→44、fitView maxZoom 1→1.4（节点少时初始/适应视图放大而非缩小留白）；尺寸常量单源在 `canvasModel.ts`，FindingsCanvas 用 `CARD_W` 组装节点勿再硬编码。
- **F13 孤立节点默认隐藏（2026-09-17 落地）**：工具条「孤立节点 (N)」开关（localStorage `findings-showisolated:<pid>`，**缺省隐藏**）——N=model.isolatedIds.size（强/链边连通分量=1 的真孤点）；隐藏时 buildLanes 收 `showIsolated:false` 跳过 noise/orphans 分区（真孤点不进 place，泳道宽高只按串联区收缩），**端点被隐藏的边在 flowEdges 里过滤**（place 缺失→null）；打开恢复三分区全量。
- `canvasModel.ts` — 纯函数模型：`buildLanes`（host 子树→泳道，无 host 归属→「未归属」；全局模式=单泳道「全部发现」。**三分区布局（2026-09-17，替代旧严重度四带/带内折行）**：泳道内按强边/链边连通分量（并查集）分三区——①**噪声区最左**=无任何连接的 info/low（列优先折行 `BAND_ROWS=8` 行）；②**串联区居中**=连通分量按最早发现时间排序、货架式 packing（行高 BAND_ROWS 放不下右移换货架）；分量内**层=距链头的最长路径**（`layerOf`，边方向 source→target=基→新/链 seq，环路防御 fallback 0），链头在最左、每跳右移一列；行分配**逐层类重心（2026-09-22 R3）**——保留前驱同行优先（层差=1 同行=水平直线），同层多节点按前继行均值升序，**逐层「排序→放置」交错**（排序依赖行号、行号依赖放置；无已布前驱排层尾，created_at 兜底）；③**孤立中高危区最右**=无连接的 critical/high/medium（SEV_RANK 升序，折行）。行 y=前缀和——构造上保证卡片零重叠；泳道 width/height/各 lane x 由最终放置阶段回填（host 泳道 x 按前序实际宽度累计 + LANE_GAP）；产出 `place` id→槽位（**含 col/row**，col 供弱边右向过滤与长边路由）+ `laneOf` + `occupied`（`${laneKey}:${col}:${row}` 集）；`sevColumn`/barycenter 旧版已移除）、`buildWeakEdges`（同泳道同 vuln_class/同父任务且严重度升级，灰虚，纯推导不入库）、`buildStrongEdges`（evidence.relates_to，青实，label=note）、`buildChainEdges`（链详情相邻 visible finding 成边，label=edge_note，按链状态着色）、**`longRunY`（2026-09-22）**=同泳道长边水平长跑 y（「中间列全空」行带中心距源/目标行中点最近者，构造上不穿卡；全占用落 maxRow+1）、**`isRightward`**=弱边右向判定（R1②）
- `FindingNode.tsx` — finding 卡（w-80 完整形态：severity 色条/标题 3 行截断/vuln_class/✓ verified/虚线框 unverified/✕ false-positive/POC 角标，hover Link2=快捷入链；compact 形态已废）+ LaneHeader（泳道头不占节点）+ LaneBackground（泳道底板，深色无边连接点）；卡片左右两个青色 Handle。data 类型必须 `type` 不用 `interface`（xyflow 的 Record<string,unknown> 约束）。
- `FindingEdge.tsx` — 自定义边：**三路由正交 path（2026-09-22）**——`adjacent` 层差=1（源右缘→列缝走廊垂直拐→目标左缘，同行 ±12px 内退化直线）/ `long` 层差>1（longY 空行带长跑）/ `cross` 跨泳道（底部绕行带）；`orthoPath` 折线→圆角 path（拐角沿两邻段截短二次曲线）喂 BaseEdge（`interactionWidth` 命中带沿全路径），走廊 x=实测锚点 ± `GAP_HALF`（14，中缝半宽）；EdgeLabelRenderer 标签**默认隐藏，hovered/selected 才渲染**，落点=最长段中点（长跑段在空带、竖直段在中缝——均避开卡体）。
- `AddChainEdgeDialog.tsx` — 手拖连线/强边确认对话框：选已有链或新建（名+goal），**edge_note 必填**；来源不在链先补挂链首（无 note），目标带 note 追加链尾。
- `ChainToolbar.tsx` — 链下拉（名+goal+状态灯+link_count）、＋新建链、状态单向流转 hypothesis→validated→exploited（终态按钮禁用，宁严勿松）。导出 `STATUS_DOT`（黄 #d29922 / 蓝 #58a6ff / 红 #f85149）供边着色共用。

## 关键约定

- **受控节点契约（v12，踩过坑）**：传 `nodes` prop 时 xyflow 在 StoreUpdater 里按**引用相等**比较，变化即 `setNodes` → `adoptUserNodes` 重建 internalNode：派生节点不带 `measured` 会清掉 measured/handleBounds（`parseHandles` 在有 measured 时沿用旧 bounds、无 measured 置 undefined），边整体卸载。本画布做法：`onNodesChange` 回收 `dimensions` change（注意字段名是 **`ch.dimensions`**，不是 measured）存入 `measuredById`，每个派生节点（含 bg/laneHeader）都带上 measured；节点身份只在结构变化（findings/measured）时变。
- **布局锁定（2026-09-22 D3）**：`nodesDraggable=false`，拖拽 offsets 全链路退役（localStorage `findings-canvas-offsets-v1:<pid>` 与 RotateCcw 重置钮已删）——布局恒为算法产物，position change 不再产生，onNodesChange 只回收 dimensions。
- EdgeLabelRenderer 容器是 `pointer-events:none`，可点的标签/按钮必须自己加 **`pointer-events-auto`**（漏了点标签会穿透到边/pane）。
- 选中联动：选中节点 → 非上下游卡 `opacity-20`、非相关边 opacity 0.15；选中边 → 底部浮卡（strong=加入链，chain=删除链边，window.confirm 后软删 link，相邻边随之变化）；onPaneClick 清空选择。
- **误报边淡出（批 1B，§6.7 的 1.6）**：`findings` 推派生 `fpIds`（status=false-positive），flowEdges 中任一端点命中的边（主要是 strong relates_to；弱边本就不起源于 FP）置 `stale`：opacity 0.25 + `strokeDasharray "3 4"` 点虚线，FindingEdge 标签加 opacity-30+line-through 与「依据已被推翻」title；选中边浮卡对 strong/chain 显琥珀警告。4s 轮询重拉 findings 即生效，无新订阅。
- 详情弹窗复用 `components/blackboard/FindingDetailDialog`（列表同款，POC 复制在里面）。

## boardGraph/（黑板链路图，2026-09-20，DESIGN §12 定稿块）

- `BoardGraphCanvas.tsx` 第 4 tab「全景」：五类对象（asset/func_kb/finding/artifact/task）× 类型分层 DAG 只读视图；`api.boardGraph` 4s 轮询（label/sub 服务端拼好）。悬停/点选聚焦一跳邻接、finding 点击复用 FindingDetailDialog、其余底部浮卡；死路/孤立缺省折叠（localStorage `board-deadend:<pid>`/`board-isolated:<pid>`）；chain 边 STATUS_DOT 着色、stale basis 点虚线 0.25。**同页切换档「全景 | 主线」（2026-09-22，execution-trace-chain R7）**：工具条分段钮切 `mode`——主线档渲染 `MainlineView`（ReactFlow 整体卸载，死路/孤立/聚焦/适应视图按钮随之隐藏），FindingDetailDialog 两档共用；切回全景 setTimeout 120ms 补一次 fitView（主线档卸载过 xyflow，视口重置）。**坑（2026-09-20 实测）**：画布是 `absolute inset-0`，TabsContent 必须自带 `relative`（Blackboard.tsx board 分支）——缺了锚到更外层把顶部 tab 栏整个盖住，用户被困在全景里切不回去；补 relative 后 tab 栏可见即可切回，退出钮冗余（曾加过又移除）。
- `MainlineView.tsx` — **战果主线（2026-09-22，R7+M3）**：board_graph 过滤子图 DOM 三列（目标资产 → verified 发现 → exploited 链；unverified/FP 不上主线）+ 底部打法效果榜（`api.traceEffect` top5，(skill × kb) × verified 组合计数，随 graph 轮询同节奏重取）；verified 发现卡与链成员发现可点→openFinding 进详情弹窗；不做独立 tab、不进 xyflow。
- `boardModel.ts` 布局纯函数：五列固定序、空列左移、行 packing 跟已放置邻居同行（edge 方向无关）；独立常量 `BOARD_*` **勿 import canvasModel**（布局结构不同，只复制不抽象）。
- `BoardNode.tsx` 五类节点卡 + `boardColHeader`/`boardColBg` 自定义 type（无 type 节点=白卡穿帮坑同上）；`BoardEdge.tsx` 复制 FindingEdge 骨架。受控节点契约同上（节点不可拖也必须回收 dimensions）。
