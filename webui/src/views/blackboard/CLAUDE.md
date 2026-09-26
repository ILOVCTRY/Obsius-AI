# views/blackboard/（黑板：单站攻击链路图 + 黑板全景图，DESIGN §三/§12）

> 发现 tab「链路」子视图 = **单站攻击链路图**（AttackPath，2026-09-24 替换旧
> FindingsCanvas）；第 4 tab「全景」= boardGraph/ 黑板全景图，见文末。
> 与逆向 `views/reverse/chains/` 互不复用组件，只共用依赖（@xyflow/react v12）。

## 入口与门控

- `Blackboard.tsx`：发现 tab 过滤行右侧 `[列表｜链路]` 子切换；**仅 pentest/redteam
  轨且非 compact 侧栏**才渲染。compact 直播间侧栏永不挂画布。
- `React.lazy` 动态 import，@xyflow/react（约 192KB）与画布都不进主包。
- findings/assets 由 Blackboard 父级持有（4s 轮询 + WS bump）；**资产筛选器
  只列 host/domain（IP/域名）按值搜索**（2026-09-24 定稿：过滤只按 IP 和域名；
  选中后子树展开，url/service 叶子不漏），选中资产切链路即作为 target 传入。
- 画布根 `absolute inset-0`，父级分支须给 `relative min-h-0 flex-1` 定高。

## 文件

- `AttackPath.tsx` — **单站攻击链路图 v3（website-attack-path-graph，主脊：目标→
  意图→收尾）**：默认无 target → `TargetGuide`（host/domain 搜索）；选定后
  `api.attackPath(pid, target)` 4s 轮询。布局常量 `X_STEP=300`/`CARD_W`/`*_H`，
  列序 target→intent→finding，`nodesDraggable/nodesConnectable=false`。
  - 主脊节点：`TargetNode`/`IntentNode`（button：待收尾脉冲点、statement、
    request_count、`OUTCOME_BADGE` 漏洞/有效发现/死路；死路虚线灰卡）/
    `FindingNode`（SEV_COLOR 五档+category）；边 derive/outcome/bypass
    smoothstep 单向（bypass 仅死路隐藏时参与保持连通）。
  - **执行层展开**：点意图卡 toggles 展开（data.expanded/onToggle）——该意图
    attempts 按 `exec_edges` 时间相邻连成尝试带（连接边 `<intent>-><attempt>:conn`）；
    `AttemptNode` 五档色标（found/hint/blocked/no_reaction/skipped）、路径模板、
    query 键、×N、状态码；无意图记录入「未归属尝试」桶。
  - `DetailPane`：点节点右侧栏看代表请求 + `api.browserHistoryRow` 完整原始
    请求；意图节带收尾字段/死因 + 人类「重开意图」钮（`api.reopenIntent`，
    POST …/intents/{id}/reopen）；request_count=0 合成节点只有结论段。
  - 顶栏计数「意图 N（待收尾 M）· 发现 K」；`counts.dead_end>0` 出「死路×N」
    显隐开关（默认隐藏）；空态提示 Agent declare_intent 后自动出现。
  - M4（跨站泳道联动）后置，等 vuln-chain-graph provides/requires 人工确认边。
- `canvas.css` — 深色主题覆盖。xyflow 样式是未分层 plain CSS，层叠压过 Tailwind
  v4 utilities；必须 import 在 `@xyflow/react/dist/style.css` 之后，用根类
  `.fc-dark` 提特异性。AttackPath 与 boardGraph 共用。
- `ChainToolbar.tsx` — 导出 `STATUS_DOT`（hypothesis 黄/validated 蓝/exploited 红）
  供 boardGraph chain 边着色；本视图不渲染工具栏。
- `MappingPane.tsx` — 「测绘」tab（cyberspace-mapping，CTF 轨不挂载）。

## 已删除（2026-09-24，勿复活）

`FindingsCanvas.tsx` / `canvasModel.tsx` / `FindingNode.tsx` / `FindingEdge.tsx` /
`AddChainEdgeDialog.tsx`——旧 findings DAG 两套渲染图退役。**硬切换开关、catView
列表过滤、category 徽章保留在 Blackboard.tsx（只弃图不弃功能）**。

## boardGraph/（黑板全景图，2026-09-20，DESIGN §12 定稿块）

- `BoardGraphCanvas.tsx` 第 4 tab「全景」：五类对象（asset/func_kb/finding/
  artifact/task）× 类型分层 DAG 只读视图；`api.boardGraph` 4s 轮询（label/sub
  服务端拼好）。悬停/点选聚焦一跳邻接、finding 点击复用 FindingDetailDialog、
  其余底部浮卡；死路/孤立缺省折叠（localStorage `board-deadend:<pid>`/
  `board-isolated:<pid>`）；chain 边 STATUS_DOT 着色、stale basis 点虚线 0.25。
  **同页切换档「全景 | 主线」**：主线档渲染 `MainlineView`（ReactFlow 卸载），
  FindingDetailDialog 两档共用；切回全景 setTimeout 120ms 补一次 fitView。
  **坑**：画布 `absolute inset-0`，TabsContent 必须自带 `relative`——缺了盖住
  顶部 tab 栏，用户被困在全景里。
- `MainlineView.tsx` — 战果主线：DOM 三列（目标资产 → verified 发现 → exploited
  链，unverified/FP 不上主线）+ 底部打法效果榜（`api.traceEffect` top5）。
- `boardModel.ts` 布局纯函数：五列固定序、空列左移、行 packing；独立常量
  BOARD_*，勿 import 已删的 canvasModel。
- `BoardNode.tsx` 五类节点卡 + `boardColHeader`/`boardColBg` 自定义 type（无 type
  节点=白卡穿帮）；`BoardEdge.tsx` 独立骨架。受控节点 onNodesChange 回收
  dimensions（ch.dimensions）回灌派生节点的 v12 契约在本画布适用。
