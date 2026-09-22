# 方案：链路画布单向零重叠布局（因果流水线）

- **状态**：已实施（2026-09-22，D1-D4/R1-R4 全量；实施记录见文末 §7）
- **拍板记录**：用户四项拍板（D1-D4）+ 设计决策（R1-R4）见 §2
- **关联代码**：`webui/src/views/blackboard/canvasModel.ts`（布局纯函数）、`FindingsCanvas.tsx`（组装/工具条）、`FindingEdge.tsx`（自定义边）、`FindingNode.tsx`（不动卡片本体）
- **实施后**：定稿决策已回写 `DESIGN.md` §12 评估画布块，本文保留作方案背景

## 1. 背景与诊断

用户实测（2026-09-22 截图，「仅强边」模式）发现链路画布存在重叠与方向混乱，逐类定位：

| 现象 | 根因 |
|---|---|
| 边注记被卡片裁掉一半（「半个字」最刺眼） | 标签画在边中点（EdgeLabelRenderer），落点不管占位；z 序在卡片之下 |
| 右下角卡片被 MiniMap 盖住 | MiniMap 画布浮层与内容零协调 |
| 长边大弧线贴近/疑似穿越卡片 | 贝塞尔从 handle 到 handle 自由走线，不避卡 |
| 整体观感「不成单向流」 | ①弱边不参与分层，可指向左方；②手动拖拽 offsets 持久化（localStorage），拖过的卡永久偏离网格（截图中两卡 x 非整数列） |

卡片-卡片网格重叠现状满足（COL_W=348>卡宽 320、CARD_STEP=148>卡高 ~130），不动。

## 2. 拍板记录

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 边注记展示 | **默认隐藏，hover/选中该边才显**（浮现标签允许瞬态遮挡卡片——交互态可接受） |
| D2 | 链分量（互不连通的串）排布 | **保持货架堆叠**（省画布宽度；「单向」以边恒向右为准，不强求单主轴串联） |
| D3 | 卡片手动拖拽 | **取消拖拽，锁定网格**——布局由算法单方负责，构造保证不被破坏；offsets localStorage 与「重置布局」钮（RotateCcw）退役 |
| D4 | MiniMap | **默认收起、可展开**（右上角开关，localStorage 记忆状态） |

| # | 设计决策 | 结论 |
|---|----------|------|
| R1 | 单向定义（三条，构造性保证） | ①列 = 距链头最长路径层（现有 `layerOf` 保留），强/链边恒向右（现状已满足）；②**弱边只画右向**——布局后 `col(target) ≤ col(source)` 的弱边不画线（升级关系信息仍可从发现详情看）；③链头最左、层递增不变 |
| R2 | 边不穿卡（零重叠硬要求） | **跨层长边拆段路由**：层差 >1 的边按列间中缝拆成层差=1 的段（Sugiyama 虚拟拐点思路）——每段「源卡右缘出 → 列间中缝（28px gap）垂直拐 → 目标卡左缘入」，水平部分永不跨列内部，构造上不穿卡。实现形态：虚拟拐点 = 不可见 xyflow 小节点（type=`elbow`，pointer-events 关），边拆为多段真实边、按 `groupId` 编组（hover 组内任一段 → 整链高亮；chain 边选中/删除按组处理）。层差=1 的边不拆段，同样走「右缘出→中缝→左缘入」 |
| R3 | 行分配微调 | 保留「前驱同行优先」（层差=1 同行=水平直线）；同层多节点排序从 created_at 改为**按前继行均值升序**（一轮类重心），减少边交叉 |
| R4 | hover 注记实现 | FindingsCanvas 加 `onEdgeMouseEnter/Leave` → `hoveredEdgeId` state；边 data 带高亮态；标签仅 hovered/selected 渲染（居中浮出，pointer-events-auto，超宽省略） |

## 3. 设计详述

### 3.1 布局规则增删

- **保留**：泳道/三分区（噪声左 ｜ 串联中 ｜ 孤立中高危右）、货架 packing（D2）、层 = 最长路径、行 y 前缀和、F13 孤立折叠、死路折叠。
- **新增**：`place` 出口附列号 `col`（供弱边右向过滤与拆段路由计算）；行分配类重心排序（R3）。
- **退役**：拖拽 offsets 全链路（localStorage 读写、RotateCcw、position change 回写）；「重置布局」钮。

### 3.2 边路由（canvasModel 纯函数 `routeWaypoints`）

输入源/目标 place（col、x、y）+ 网格常量（COL_W、中缝半宽），输出拐点序列：

- 层差=1：`[源右缘中点, (中缝x, 源y), (中缝x, 目标y), 目标左缘中点]`——同行时退化为水平直线；
- 层差>1：按段拆（R2），段间经不可见 `elbow` 节点衔接；同列中缝多条边垂直重叠时按边序 micro-offset（±3px 阶梯）保持可辨。

`FindingEdge` 改为按 waypoints 构造 SVG path（BaseEdge 接受自定义 path），命中带 interactionWidth 沿整条路径生效。

### 3.3 交互变化汇总

| 项 | 现状 | 目标 |
|---|---|---|
| 拖拽 | 可拖 + localStorage 持久 | nodesDraggable=false，锁定 |
| 边注记 | 常显（被卡裁切） | hover/选中才显 |
| MiniMap | 恒显右下角 | 默认收起，右上角开关 |
| 弱边 | 全部画（可反向） | 只画右向 |
| 重置布局钮 | RotateCcw 清 offsets | 退役（布局恒为算法产物） |

## 4. 实施拆步

1. `canvasModel.ts`：place 带 col；类重心行分配；`routeWaypoints` 纯函数；弱边右向过滤导出。
2. `FindingsCanvas.tsx`：锁定拖拽、删 offsets/RotateCcw；MiniMap 折叠开关；hoveredEdgeId；长边拆段组装（elbow 节点注册 + 组 id）。
3. `FindingEdge.tsx`：waypoints path；标签 hover 渲染。
4. `npm run build` 零 TS 错误 + 截图验收（对照本方案 §1 四现象逐一销项）。

## 5. 测试要点

- canvasModel 纯函数单测：routeWaypoints 层差=1/>1/同行退化三形态；弱边过滤（反向剔除）；类重心不改变前驱同行优先。
- 回归：布局纯函数既有消费（FindingsCanvas 组装）不破坏受控节点契约（elbow 节点同样回收 dimensions）。

## 6. 明确不做

- **boardGraph 全景图不动**（五列 DAG 布局独立，若用户后续提同类诉求另议）；
- 分量货架堆叠保留（D2）——不追求全图单主轴；
- hover 浮现的注记允许瞬态遮挡卡片（D1 语义内）；
- 弱边反向不画带来的信息取舍（详情页仍可查升级关系）。

## 7. 实施记录（2026-09-22）

**对 §2/§3 的一处实施简化（几何同构、交互原生）**：R2 的「elbow 虚拟拐点节点 + 边拆多段 + groupId 编组」落成**单边多拐点正交 path**——BaseEdge 接受任意 path d 串，一条 xyflow 边即可承载完整折线（`orthoPath`：相邻重复点过滤 + 拐角沿两邻段截短二次曲线）；走廊 x 不由模型假设，渲染端从 xyflow 实测锚点算（源卡右缘 `sourceX` / 目标卡左缘 `targetX` ± GAP_HALF=14）。hover 天然整边生效、chain 删边/选中零改动，用户可见行为与原方案完全一致且更简。

- **模型侧（canvasModel.ts）**：`place` 带 `col/row` + 泳道 `occupied` 集；R3 类重心改**逐层交错**（排序依赖行号、行号依赖放置——按 byLayer 逐层「排序→放置」，层内 baryOf=前驱 localRow 均值，无已布前驱排层尾）；`longRunY`（同泳道长边水平长跑 y=「中间列全空」行带中心，构造上不穿卡，全占用落 maxRow+1）；`isRightward`（弱边右向过滤，R1②）。
- **画布侧（FindingsCanvas.tsx）**：D3 锁拖拽（nodesDraggable=false，offsets/RotateCcw/position change 全链路退役，onNodesChange 只回收 dimensions）；D4 MiniMap 折叠开关（localStorage `findings-minimap:<pid>`）；R4 hoveredEdgeId；corridorOff 同走廊多边 ±3px 阶梯（key=`g1/gd:<laneKey>:<col>`，n=1 居中为 0）；**crossBandY=所有泳道 height 最大值+24**（不能只取两端泳道——中间泳道可能更高，长跑横穿中间泳道也不穿卡）。
- **边组件（FindingEdge.tsx）**：三路由 `adjacent`（层差=1，同行±12px 退化直线）/`long`（层差>1，空行带长跑）/`cross`（跨泳道，底部绕行带）；标签=最长段中点（长跑段在空带、竖直段在中缝——均避开卡体）；命中带改 xyflow 内部 `interactionWidth=14`（删自定义透明命中 path）。
- **验收**：npm run build 零 TS 错误；前端无测试 runner（§5 单测无基建）→ 落运行时截图验收——真实项目（48 发现 16 强边）三态 + 一次性 API 造数项目两组（层差 3 长边落空行带 + 弱边升级对；嵌套 host 双泳道跨泳道绕行带），adjacent/long/cross 三路由 + hover 注记落空带 + MiniMap 展开全部 exercised，验收后临时项目即删。
- **已知既有行为（不在本方案范围）**：fitView 只在首挂跑——切孤立节点/资产筛选后画布变宽不重新适配，需手动点「适应视图」。
