# WebUI TRAE 化新壳（webui-trae-shell）

- **状态**：**M6 已实施（2026-09-25）——六个里程碑全部完成**。M6 起新壳为默认壳：从未选过（localStorage `ui.shell` 为 null）即进新壳；旧壳保留观察、不退役，两壳双向可切。
- **拍板记录**：三轮讨论共 9 项 + M5 两项（2026-09-25，见 §2 与 §7 M5）；M6 无新增拍板（切默认+并存观察按既定范围执行）。
- **关联代码**：`webui/src/shell2/`（新壳全部）、`webui/src/App.tsx`（壳选择入口与参数落账）；M3 触后端小补丁（schema v23、executor-llm 口，见 §7 M3），**M4/M5/M6 后端零改动**。

## 1. 背景与范式

对标 TRAE Work：**对话即工作台**——composer 常驻、过程与产物在对话内以卡片呈现；侧栏轻量化（项目折叠分组 + 任务行），无独立项目管理页。

本项目后端能力（自主档 / 运行时 / 模型路由 / 任务即窗口 / 模板剧本 / MCP·工具链）全部现成，TRAE 控件均有对应概念。本方案是**前端信息架构的重新收纳**，不是新系统：后端协调模型、session/`bound_task_id` 绑定、安全闸口一行不动。

## 2. 拍板记录

| 轮 | 决策点 | 结论 |
|----|--------|------|
| 1 | 顶部模式 | **工作台 / 画布 / 工具 三姿态**（非按场景轨；轨仍是项目属性） |
| 1 | 侧栏结构 | **项目折叠分组 + 任务行**；编排器固定为项目下首行 🧭 |
| 1 | 过渡方式 | **新壳并存，渐进迁移**（旧壳保留，逐视图搬迁，验证后切默认） |
| 2 | chip 语义 | **混合语义**：chip 改默认值（自主档写项目 config / runtime 写任务 meta / 模型写项目覆写），Agent 单次工具调用可临时覆盖，chip 可一键重置 |
| 2 | 姿态内导航 | **主区顶部窄 tab** |
| 2 | 资源入口形态 | **主区切换但对话保活**（对话组件 hidden 不卸载，草稿/滚动不丢） |
| 3 | L1 审批 | **侧栏铃铛 + 对话内审批卡**（批准/拒绝/改后批准），无独立审批页 |
| 3 | 默认落地 | **最近活跃项目的编排器对话**（记住姿态+选中行）；无项目=欢迎页 |
| 3 | M2 技术路线 | **新写 ConversationPane + 从 LiveRoom 抽 useTurnStream**（旧壳零行为变化） |

## 3. 目标信息架构

```
┌──────────────────────────────────────────────────────────────────┐
│ 侧栏 ~248px                    │ 主区（姿态切换）                 │
│ [工作台][画布][工具]           │ 工作台：goal 条 + 对话流          │
│ ＋新建作战                     │         + 底部常驻 composer      │
│ ─ 资源 ─                       │ 画布：  窄 tab + 全宽画布         │
│ 🧩模板 🔌插件 📡情报 📁文件     │ 工具：  窄 tab + 专用界面         │
│ ─ 作战项目 ─                   │                                  │
│ ▾ OA系统  [pentest]            │ 对话流：人类右气泡 / Agent MD     │
│   🧭 编排器          ●         │   ⚙过程·N 步折叠 / 审批卡        │
│   ◉ 端口侦察  web ✓            │   [composer + chips 常驻]        │
│ ▸ CTF-J组      [ctf]           │                                  │
│ 🔔 3     ⚙ 设置                │                                  │
│ 👤 Nan · 旧壳入口              │                                  │
└──────────────────────────────────────────────────────────────────┘
```

## 4. 现有视图收编表

| 现 NAV/视图 | 新归宿 | 里程碑 |
|---|---|---|
| 项目 ProjectsView | 侧栏项目树 +「＋新建作战」轻弹窗（改名/删除进项目行右键） | M1 |
| 会话 LiveRoom | 工作台对话流（M1 先内嵌 LiveRoom 占位，M2 换 ConversationPane） | M1/M2 |
| 黑板 Blackboard | 画布：boardGraph 全景 | M4 |
| 任务 TaskBoard | 画布：任务流；侧栏任务行 | M1/M4 |
| 情报 IntelView | 工具 tab；侧栏资源入口（M1 已可内嵌） | M1/M4 |
| 浏览器 BrowserView | 工具 tab | M4 |
| 审批 ApprovalsView | 铃铛 + 对话内审批卡，独立页取消 | M2 |
| 设置 SettingsView | 侧栏底部齿轮（主区屏幕/覆盖层） | M1 |
| 全局 header | 轨道徽章入项目行；任务统计入 goal 条 | M4 |

画布 tab（M4）：黑板全景 / 任务流 / 攻击路径 / 轨迹链。
工具 tab（M4）：浏览器 / 测绘 / 情报 / 产物文件 / 插件·MCP。

## 5. 语义定稿

- **chip 混合语义**：切换=改默认；自主档写 `config.autonomy`（持久）；runtime 写任务 meta（任务期默认）；模型写项目 executor 覆写。Agent 工具调用入参可临时覆盖，chip 旁显重置点回跟随项目。
- **窗口**：主区只呈现一个对话，侧栏点行=切换；后端 session/`bound_task_id` 绑定不动；辅助窗也是侧栏一行。
- **审批**：铃铛管全局未读（approvals + `awaiting_human`）；审批卡插对话等待点。
- **资源入口**：主区切换、工作台 hidden 保活。
- **明确不做**：同步对话接力（沿用黑板异步协调定稿）；按轨做模式；跨项目聚合视图。

## 6. 里程碑切分

| | 内容 | 后端 | 验收 |
|---|---|---|---|
| **M3 chip 全通** | 自主档/runtime/模型三 chip 接线＋附件上传＋产物文件浏览 | schema v23：任务 `preferred_runtime`；项目 executor 模型覆写口 | 三 chip 默认值/临时覆盖/重置均可用 |
| **M4 画布/工具** ✓ | 四画布 + 五工具逐个窄 tab 迁入；reverse 重型工作台随画布重生；全局 header 退役 | 零改动（已核实） | 旧 NAV 全部有新归宿，旧入口摘除 |
| **M5 欢迎态+模板** ✓ | 无项目欢迎页（名称输入+模板卡）+ 场景模板中心（8 场景档卡 + 三类录入向导） | 零改动（物化口已由 M4a/phases 提前提供） | 空白态可被模板托底 |
| **M6 切默认** ✓ | 新壳默认（null=新壳）；旧壳保留观察不退役，两壳双向通道 | 零改动 | 首次访问/清缓存即落新壳，旧壳随时可回 |

## 7. 落地细节

### M1

- 目录：`webui/src/shell2/`（`NewShell.tsx` / `SideBar.tsx` / `ProjectTree.tsx` / `placeholders.tsx` + `CLAUDE.md`）。
- 数据：`api.listProjects` 5s 轮询；展开项目的 `api.tasks` + `api.sessions` 合并 5s 轮询（折叠即停）；任务行=task，`target_session` 去重后剩余 session 作辅助窗行。
- 选中：`{pid, sid}`，编排器 sid=`__orch`；LiveRoom `focusSession={{sid, n}}` 非切行不发非次，保持 hidden 保活。
- 持久化：localStorage `ui.shell2.state` = 姿态 / 选中 / 展开集合；首启=首个项目（最近活跃）编排器；无项目=欢迎占位。
- 铃铛：当前项目 pending approvals + `task_stats.awaiting_human`，5s。
- 建项弹窗：名称 + 轨单选（专家留缺省，组队 M4 前回旧壳设置或 LiveRoom chip）。
- 旧壳入口：侧栏底部「切回旧壳」剥 localStorage 并重载。

### M2

- 共用件抽取（零行为变化）：`lib/turnStream.ts`（LiveRoom 事件→turn 装配纯函数 `buildStreamItems`，原逻辑原样搬迁）、`views/live/editors.tsx`（GoalEditor/PersonaEditor）；LiveRoom 改引用，其余不动。
- 新文件：`shell2/ConversationPane.tsx`（对话主区）、`ConversationComposer.tsx`（常驻 composer，草稿/模式按 sid 隔离）、`ApprovalCard.tsx`（审批卡 + 回执 + 裁决审计 chip）。
- 写口全部收编现有 API：note/指派/ orch chat/publish task/中断/恢复/跑队列/goal/persona，无旁路。
- **审批无请求事件**：`request_approval` 不发事件，只有裁决发 `approval.approved/rejected`（且无 session_id）。故卡数据靠 4s pending 轮询，按归属会话 + `created_at` 时间位注入；裁决后 `onDecided`→extras 把卡以回执态留住（EXTRA_TTL=120s，30s 清扫），超时后由全量审批（20s 轮询）派生的紧凑审计 chip 永久接档。
- 编排态 = OrchChatPane + 可折叠运行记录（h-64）；会话态 = column-reverse 气泡流 + 合并审批行。
- 铃铛：有 pending→跳首条归属会话并定位；无 pending（仅 awaiting_human）→回落旧 ApprovalsView。

### M3

- **后端小补丁（全部经既有单一写入口）**：
  - schema v23：tasks 幂等补 `preferred_runtime TEXT NOT NULL DEFAULT ''`（合法值 `''/host/wsl/docker/sandbox`，落库白名单校验）。
  - `TaskPatch.preferred_runtime`：open/failed 任务可改（done/claimed 禁，claimed=在跑固化）；消费点=run_cmd 工具 JSON 入参 `runtime` 缺省时按当前任务补，空默认且未显式传→错误串要求明示；角色 max_runtime 等级软照照旧；run_task 注入「🎛️ 本任务默认运行时」告示。
  - executor 模型覆写：`GET/PUT/DELETE /api/projects/{pid}/executor-llm`，写经 `ProjectStore.update_config`（`config.executor_llm`，provider 空即剥键）；PUT 后 `_apply_executor_llm_live` 对内存中在跑会话热换 LLM 引用（下一次调用生效）。
- **三 chip（`shell2/Chips.tsx`，共用自写弹层原语——无 popover 组件）**：
  - 自主档 chip（仅编排态）：写 `config.autonomy`（持久）；L0/L1/L2 三档（轨默认值同 `core/autonomy.py TRACK_DEFAULT_LEVEL`）+ 暂停全部 / 空时自动派生两开关；重置=等级回轨默认（开关不动）。30s 轮询对账。
  - runtime chip（仅会话态）：写任务 `preferred_runtime`（任务期默认）；宿主能力不支持的 runtime 置灰（docker/wsl），done/claimed 禁用。
  - 模型 chip：写 `config.executor_llm`（持久+在跑会话热换）；供应商/模型两级选择，重置剥覆写。
- **附件上传**：M2 composer 回形针口直传（既有 `/attachments`，≤64MB），随对话/指派/发任务下发，Agent 告示含 📎 工作区相对路径。
- **产物文件浏览（`shell2/FilesView.tsx`）**：侧栏「📁 文件」资源入口，主区切换、对话 hidden 保活；只读——artifacts 注册表为据，kind 过滤 + 名称搜索，文本走 content 口预览（sha256 短码）、下载走 download 口（原始文件名），10s 自动刷新。

### M4

- **共用窄 tab 原语** `shell2/NarrowTabs.tsx`：h-8 底边窄条（选中=primary 高亮），持久化键统一导出 `ui.shell2.canvas-tab` / `ui.shell2.tools-tab`（🔌插件资源入口也要预置写它）。
- **画布宿主** `shell2/CanvasView.tsx`（NewShell 一层 lazy，重型视图内部二次 lazy——`@xyflow/react` 不进壳主包）：
  - 黑板全景 BoardGraphCanvas、任务流 TaskFlow、攻击路径、轨迹链。
  - **TaskFlow 适配**：shell2 无 LiveRoom 的 WS 事件计数，`pausedSids` 传空只读 Set、`wsBump` 恒 0，组件退化用自带 3s 轮询（功能不缺，仅失去 WS 即时去抖）。
  - AttackPathHost：先拉 `api.assets` 再喂组件（组件内部含目标选择引导）。
  - TraceHost：任务下拉（5s 轮询、自动选首任务、陈旧选择校验）+ TaskTraceList 全高；无任务显引导。
  - **逆向 tab 条件挂载**：`deriveWorkbenchProfile(meta)==="rev-generic"`（research 轨+binary 能力包，或 `config.workbench.profile` 显式覆盖）才追加「逆向工作台」，tab 已选但 profile 丢失时回落 board。
- **工具宿主** `shell2/ToolsView.tsx`：浏览器/测绘/情报/产物文件/插件·MCP 五 tab；情报/文件直引（壳内已用），浏览器/测绘无 pid 显 NoProjectHint，情报/MCP 可离项目使用。
- **McpPane 抽取**：SettingsView 私有 MCP 配置块提为 `components/McpPane.tsx`，设置页与工具 tab 共用（独立保存提示样式，行为不变；工具桥注入仍挂账，文案明示）。
- **NewShell 接线**：两姿态宿主 lazy + Suspense 骨架；🔌插件资源→预置 tools-tab=mcp 再切工具姿态；画布节点双击经 `goto-session` CustomEvent（沿用 TaskFlow/AttackPath 旧事件契约）跳回工作台会话；PosturePlaceholder 及姿态清单退役。

### M5

- **欢迎页** `shell2/WelcomePane.tsx`（从 placeholders.tsx 独立升级，拍板=名称输入+模板卡）：名称输入 + 四轨 chips + 当前轨场景档卡片；名称为空卡片禁用；点档=带 profile 建项，回车=不选档全池直通。
- **模板中心** `shell2/TemplatesView.tsx`（🧩资源屏）：三张录入向导触发卡 + 四轨分组 8 场景档卡（卡内名称输入即建项，显示 playbook/产物/组队）。
- **录入向导** `shell2/TemplateWizards.tsx`（共用 WizardShell，全走既有端点）：
  - 🎯 渗透目标录入：pentest 建项（默认 recon-first）→ 目标逐行 `addAsset(type="auto")` 自动分型；个别失败不阻断，错误汇总。
  - 🧬 样本 triage：research 建项（固定 rev-workbench）→ `uploadSample`，headless triage 自动跑。
  - 🚩 CTF 向导：ctf 建项（默认 solo-generalist）→ 题面 `orchChat` 发编排器（失败不阻断）。
- **建项弹窗增强** SideBar CreateDialog：轨切换拉场景档 chips（可再点取消=不选档），提交带 profile。
- **原计划「少量模板物化口」实际零新增**：profile 五件套物化（expert-pool M4a）、初始阶段登记（phases M1）、资产/样本/orch 端点此前均已就位。

### M6

- **默认翻转**（`App.tsx` `shell2Wanted`）：选择优先级 = `?shell=1/2` 参数 > localStorage `ui.shell` > **null（从未选过）= 新壳**；仅显式 `"1"` 才是旧壳。M5 前 NewShell 内自带的参数落账逻辑删除，统一由 App 层 effect 处理（两壳共用）：参数落 localStorage 后 `history.replaceState` 剥参，刷新语义稳定。
- **双向通道**（旧壳保留观察期必须有回头路，超出原计划的纯翻转）：旧壳加 `ToShell2Button`（ghost 按钮「新壳 →」）——无项目帧右上悬浮、项目帧顶栏（紧跟「← 项目」）两处；与新壳 SideBar 底部「旧壳」对称，均为写 localStorage + reload。
- **旧壳保留不退役**：`LegacyApp` 全量代码与入口都在；退役是未来用户决定（观察一版本后）。
- **附带修复**：NewShell `expanded` 持久态不随项目删除剪枝——旧行为下删项目后死 pid 每 5s 永远 404 轮询；落地态对账加 live 集合剪枝（项目清空时连 target 一并清），仅启动首帧可能有一轮 404。

## 8. 风险

- ~~M3 chip 触后端写口~~（已落地：全部对齐 ProjectStore/TaskQueue 既有写入口）；~~M4 reverse 重型工作台归宿~~（已落地：rev-generic 条件 tab）。
- **headless 0 边假象**：Playwright 内 `document.visibilityState==="hidden"` 冻结 ResizeObserver/rAF，xyflow 边测量永不到位（旧壳同版本同样 385 节点/0 边，非 M4 回归）；真实可见浏览器正常。
- 多项目展开时任务轮询放大；M1 折叠即停，项目数小可接受。

## 9. 验证记录

### M1（2026-09-25 落地）

- **范围**：壳入口（`?shell=2` → localStorage `ui.shell=2`，剥 query）；姿态 tabs + 资源入口；只读项目任务树（编排器固定首行 + 任务状态字形，旧任务无 `target_session` 回退 `claimed_by`，无绑定 session 作辅助窗行）；轻量建项弹窗（名称+轨，专家缺省）；工作台内嵌 LiveRoom（hidden 保活，切项目 `key=pid`、切行 focus nonce）；情报/设置内嵌；画布/工具/资源占位。
- **实施修正**：①落地态对账加 `projectsLoaded` 闸（首次列表成功前不碰 target，防瞬态轮询冲掉选中项）+ sid 随会话列表对账（会话彻底消失才退回编排器）；②M1 审批屏暂用旧 ApprovalsView 兜底（铃铛点击进入），M2 改对话内审批卡。
- **验证**：`npm run build` 零 TS 错误；Playwright 冒烟全过（落地选中→切画布/情报/设置→回工作台 LiveRoom 状态保留；刷新后选中/展开持久；建项弹窗开合；旧壳 ProjectsView 零变化）。后端零改动、pytest 不受影响（纯前端批次）。

### M2（2026-09-25 落地）

- **范围**：抽共用件（`lib/turnStream.ts`、`views/live/editors.tsx`，LiveRoom 仅改引用）+ 新写 `ConversationPane`/`ConversationComposer`/`ApprovalCard`；M1 内嵌 LiveRoom 占位退役；铃铛改跳审批归属会话（无 pending 回落旧页）。
- **实施修正**：①spawn 审批赛跑终检（任务已不待执行）返回 `session_id=null+skipped`，回执显式显示跳过原因（旧 ApprovalsView 同场景会显 undefined——新壳修正，未回改旧壳）；②审批裁决审计事件无 sid 且会话态事件流带 `session_id` 服务端过滤拿不到，改为全量审批列表派生紧凑审计 chip（方案：本地另拉全局事件流代价大，弃）。
- **验证**：`npm run build` 零 TS 错误；Playwright 冒烟全过——审批卡时间位注入；批准（跳过/建窗失败）与拒绝三路径回执正确；extras 过 pending 刷新留卡；重载后审计 chip 接档；goal 保存/清空、persona 保存/重置；模式 chip 双态循环；刷新落回选中；铃铛从编排态跳归属会话；旧壳零变化、默认入口仍旧壳。后端零改动。
- **测试数据清理**：全部冒烟审批已裁决（0 pending）、测试目标已清空、人格已重置；夹具脚本移至系统临时目录（不删除）。

### M3（2026-09-25 落地）

- **范围**：三 chip（自主档/runtime/模型，`Chips.tsx`）+ composer 附件上传 E2E + 产物文件浏览 `FilesView.tsx`；后端小补丁=schema v23（tasks 幂等补 `preferred_runtime`，白名单值校验）+ TaskPatch.preferred_runtime + GET/PUT/DELETE `/api/projects/{pid}/executor-llm`（写经 ProjectStore，PUT 热换在跑会话）。
- **实施修正**：①RuntimeChip 早返在 useCallback 之前致 hooks 数不稳（静态版 React minified #310），所有 hooks 声明移到早返之前；②原方案 M3 只列 runtime/模型两 chip，落地时补齐自主档 chip（编排态必须有项目级挡位入口），轨默认值与 `core/autonomy.py` 对齐；③资源「文件」屏切换其实正常——对话工作台仅 `hidden` 保活，main.textContent 仍含对话内容，排查勿再被 textContent 误导。
- **验证**：`npm run build` 零 TS 错误；Playwright 冒烟全过——runtime chip 默认→host→wsl→重置（docker 按宿主能力置灰、done/claimed 禁用）；模型 chip 选 ark-coding/ark-code-latest 后 API+project.json 双落、重置剥覆写；自主档 L0·非默认→L2→暂停/自动派生→重置回 L1；FilesView 58 项清单、kind 过滤（附件→1）、名称搜索（m3→1）、文本预览（真实内容）、sha256 短码、下载端点 200。全量 pytest **1139 passed**（含新增 hermetic `test_worker_chat_notice_carries_attachment_path`：idle 窗带附件 human_note→run_chat 消息含 📎+工作区相对路径+原文件名）。
- **live 末环说明**：附件 E2E 的真人链路当日因 ark 公网 WinError 10061（连接被拒，环境网络问题，非代码缺陷）未跑通，末环由上述 hermetic 测试钉死；网络恢复后可补一次 live 复测。

### M4（2026-09-25 落地）

- **范围**：`NarrowTabs.tsx` 共用窄 tab；`CanvasView.tsx` 四画布（黑板全景/任务流/攻击路径/轨迹链）+ rev-generic 条件「逆向工作台」；`ToolsView.tsx` 五工具（浏览器/测绘/情报/产物文件/插件·MCP）；`McpPane.tsx` 从 SettingsView 抽公共件；NewShell lazy 接线 + 🔌插件路由 + `goto-session` 桥；姿态占位退役。
- **实施修正**：①TaskFlow 的 WS 耦合参数以空集/0 喂入走自带轮询（shell2 无 LiveRoom WS 计数）；②攻击路径/轨迹链在壳内补最小 Host（assets 拉取、任务选择），旧组件零改动；③MCP 设置块提取时保留设置页其余私有样式不连带迁移。
- **验证**：`npm run build` 零 TS 错误（CanvasView chunk 3.9KB / ToolsView 2.3KB，xyflow 系仅在各自 chunk）；Playwright 冒烟全过——四画布（黑板 flow 挂载；任务流 48 节点；攻击路径选目标后 2 节点；轨迹链 48 任务下拉、切任务 46→35 行）、五工具（浏览器动作时间线/拦截/重发；情报文章池 1225；产物文件搜索预览；MCP 配置保存，设置页与工具 tab 两处一致；🔌插件→工具·MCP）、逆向（research+binary 建项后第五 tab 出现，ReverseWorkbench 挂载：选样本/上传/IDA 同步/AI triage）。**0 边为 headless visibilityState 冻结假象**（旧壳同数据同样 0 边，非回归）。冒烟项目已删除（trash）。后端零改动，全量 pytest 见 M4 收尾批次（后端未动，沿用 1139 基线）。

### M5（2026-09-25 落地）

- **范围**：WelcomePane 独立升级（名称+轨+档卡）；TemplatesView 模板中心（3 向导+8 档卡）；TemplateWizards 三向导；CreateDialog 加场景档位。
- **拍板（M5 两项）**：欢迎页=名称输入+模板卡（非大 composer/LLM 判轨——避免新增分类口）；模板中心=场景档卡+三类录入向导。
- **实施修正**：①原计划预估「少量模板物化口」，核实发现物化机制（M4a profile 五件套、phases 初始登记）与录入端点全部现成，M5 实际后端零改动；②CTF 题面投递用 orchChat 异步、失败只 catch 不阻断进项目；③空项目态验证用 Playwright addInitScript 把 GET /api/projects  stub 成 `[]`（真实环境总有项目，欢迎页难直接触发）。
- **验证**：`npm run build` 零 TS 错误（主包 920→932KB，无重型依赖）；Playwright 冒烟全过——欢迎页（stub 空项目：名称空时卡片禁用；输入后启用；切轨 pentest→ctf 卡片组替换）；模板中心（3 向导触发卡+四轨分组 8 张名称输入卡）；渗透向导 E2E（2 目标自动分型 host/url，recon-first 组队 osint+recon 物化，recon 阶段登记）；CTF 向导 E2E（solo-generalist 物化 _generalist，题面落 orch.chat human 消息）；样本向导 E2E（rev-workbench 物化 reverse-analyst+rebuilder，bottle.exe 上传后 headless triage 实跑：255 函数/I64 库/86 导入）；建项弹窗场景档随轨切换（pentest 侦察先行/标准渗透评估队，redteam 全链/入口突破）。三个冒烟项目已全部 trashed。**后端零文件改动**（mtime 核实），pytest 沿用 1139 passed 基线。
- **环境注记**：CTF 向导的编排器 LLM 回复当日因 WinError 10061 失败（题面消息已持久化，进项目可续聊），非代码缺陷。

### M6（2026-09-25 落地）

- **范围**：默认翻转（null=新壳）；`?shell=` 落账剥参统一上收 App 层；旧壳两帧加「新壳 →」；expanded 死 pid 剪枝。
- **实施修正**：①原计划只写「切默认」，落地时补双向通道——旧壳在观察期必须有可见回程，否则误切用户困死；②剪枝修复系验证中发现（M5 冒烟项目 trashed 后 localStorage 残留展开项，8420 持续 404）；③验证遇 M5 addInitScript 的 fetch stub 残留浏览器上下文（`/api/projects` 在页内恒返回 `[]`、curl 正常），browser_close 重开干净上下文即解——后续会话复用 MCP 浏览器时先排查 stub。
- **验证**：`npm run build` 零 TS 错误（主包 932.65KB / gzip 271.68KB）；Playwright 七项矩阵全过——①null+裸 URL→新壳；②`ui.shell=1`→旧壳且右上有「新壳 →」；③`?shell=2` 压过 localStorage=1→新壳、落账剥参；④`?shell=1` 反向同样；⑤无项目帧「新壳 →」→新壳；⑥SideBar「旧壳」→旧壳（双向往返）；⑦旧壳项目帧（打开「中原工学院」）顶栏「新壳 →」→新壳。剪枝：stale state 启动后 expanded 收敛到存活项目，404 仅首帧一轮（等 7s 跨一个轮询周期计数不增）。**后端零改动**，pytest 沿用 1139 passed 基线（纯前端批次）。
- **未决（交用户）**：旧壳退役时点；代码未提交（等用户明确指令）。
