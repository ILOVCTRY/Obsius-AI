# views/blackboard/（黑板：单站攻击链路图 + 测绘/函数库，DESIGN §三/§12）

> 发现 tab「链路」子视图 = **单站攻击链路图**（AttackPath，2026-09-24 替换旧
> FindingsCanvas）。
> 与逆向 `views/reverse/chains/` 互不复用组件，只共用依赖（@xyflow/react v12）。

## 入口与门控

- `Blackboard.tsx`：发现 tab 过滤行右侧 `[列表｜链路]` 子切换；**仅 pentest/redteam
  轨且非 compact 侧栏**才渲染。compact 直播间侧栏永不挂画布。
- **compact 侧栏渗透轨不挂测绘/函数库 tab**（2026-09-26 用户要求：渗透项目右侧
  窄栏用不上；主黑板视图不受影响）。
- **主视图渗透/红队轨也不挂函数库 tab（2026-10-16，Nan 需求「去掉渗透测试中
  函数库的 UI」）**：挂载条件从「capabilities 含 binary」收窄为「含 binary
  **且 track ≠ pentest/redteam**」（`tabs` 构造与 TabsContent 两处同步）。
  成因：`directions_effective` 推导下绑定 knowledge=null 的通才专家（如
  `_generalist`）→ 项目拿到**全量 kb 方向**（含 binary）→ 渗透项目误挂 funcs
  tab，而 func_kb 只由二进制分析产生、渗透轨该 tab 永远空数据。CTF 二进制题
  与逆向分析保留。注意轨道判据不看 capabilities 单独成立。
- `React.lazy` 动态 import，@xyflow/react（约 192KB）与画布都不进主包。
- findings/assets 由 Blackboard 父级持有（4s 轮询 + WS bump）；**资产筛选器
  只列 host/domain（IP/域名）按值搜索**（2026-09-24 定稿：过滤只按 IP 和域名；
  选中后子树展开，url/service 叶子不漏），选中资产切链路即作为 target 传入。
- 画布根 `absolute inset-0`，父级分支须给 `relative min-h-0 flex-1` 定高。

## 文件

- `AttackPath.tsx` — **单站攻击链路图 v3（website-attack-path-graph，主脊：目标 →
  子目标/意图 → 收尾：漏洞|有效发现|死路 → 意图 → 收尾 → …，可循环延伸）**：
  默认无 target → `TargetGuide`（host/domain 搜索）；选定后
  `api.attackPath(pid, target)` 4s 轮询。布局常量 `X_STEP=300`/`CARD_W`/`*_H`，
  **按依赖层级自动分层**（`layoutGraph`：无前驱非根落 level 1），`nodesDraggable/nodesConnectable=false`。
  **`place()` 父卡只下移不上溢（2026-10-12 修重叠）**：父卡垂直居中于子块时
  `yOf = yTop + Math.max(0, (blockH − cardH) / 2)`——子块矮于卡片（如叶子子目标、
  挂 1 条 finding 的意图）时旧式 `(yTop+y)/2 − h/2` 会**向上溢出**分配块，侵入前一
  兄弟块，同列间隙 `Y_GAP=20` 小于上溢量即相撞；截负值后只下移，不改正常居中。
  - **子目标节点 `SubtargetNode`（2026-10-01）**：根的直接子资产成节点（孙节点不上图）；
    带终态徽章 `SUBTARGET_STATUS`（已测清/待测/扫描中…）——`node.settled`（子树意图
    全部收尾且至少一条 dead_end）为真时显「已测清」；`node.findings` 显示发现数。
    意图按资产锚点归属到子目标子树，derive 边自子目标起（无归属仍挂根）。
  - 主脊节点：`TargetNode`/`SubtargetNode`/`IntentNode`（button：待收尾脉冲点、
    statement、request_count、`OUTCOME_BADGE` 漏洞/有效发现/死路；死路虚线灰卡）/
    `FindingNode`（SEV_COLOR 五档+category）；边 derive/outcome/bypass
    smoothstep 单向（bypass 仅死路隐藏时参与保持连通）。
    **IntentNode/卡片内部用 flex 流式布局（2026-09-26 修文字重叠）**：陈述
    `flex-1 overflow-hidden` 占剩余高度、徽章行常规流——勿改回 absolute
    底部定位（正文过长时会压住徽章/时间戳）；当前 INTENT_H=144，正文最多显示五行，
    超出部分可通过卡片悬停提示查看完整陈述。
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
  `.fc-dark` 提特异性。AttackPath 使用。
- `MappingPane.tsx` — 「测绘」tab（cyberspace-mapping，CTF 轨不挂载）。
- `IntentsPane.tsx` — **「意图」tab（2026-10-13，全轨）**：意图清单列表视图——把
  Agent `declare_intent` 的规划产物摊平成人类可核对的列表（**待收尾/已收尾分组**，
  含目标资产〔`api.assets` 反查 value/type〕/维度/推导依据/收尾结果〔漏洞/有效发现/死路〕/
  作者/时间；已收尾可「重开」→`api.reopenIntent`）。4s 轮询 `api.intents(pid)`（open+closed 全量）。
  动机：渗透轨本有「发现 → 链路」图可看意图，**其余轨（如逆向）此前无任何查看入口**，
  只能从对话流的 `declare_intent` 工具行看到「声明了意图」却看不到落点。纯只读列表，
  不画图（逆向无 `http_history`，链路图执行层会空）。

## 已删除（勿复活）

- 2026-09-26 **黑板全景图 boardGraph/**（`BoardGraphCanvas`/`MainlineView`/
  `boardModel`/`BoardNode`/`BoardEdge`，2026-09-20 上线的第 4 tab「全景」+
  「主线」切换档）——用户要求下线，tab 与前端文件全删；后端
  GET /projects/{pid}/board-graph 只读端点与 `graph.board_graph` 保留（tests
  覆盖不动）。同删无引用的 `ChainToolbar.tsx`。
- 2026-09-24 `FindingsCanvas.tsx` / `canvasModel.tsx` / `FindingNode.tsx` /
  `FindingEdge.tsx` / `AddChainEdgeDialog.tsx`——旧 findings DAG 两套渲染图退役。
  **硬切换开关、catView 列表过滤、category 徽章保留在 Blackboard.tsx（只弃图不弃功能）**。
