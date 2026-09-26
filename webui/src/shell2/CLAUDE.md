# shell2/（TRAE 化新壳）

> 对话即工作台新壳，方案见 `docs/plans/webui-trae-shell.md`。**M6（2026-09-25）起为默认壳**：从未选过（ui.shell=null）即进，旧壳经「旧壳」按钮回切、两壳双向通道，旧壳保留观察不退役。M5：欢迎页名称+模板卡、模板中心 8 场景档+三类录入向导。M4：四画布+五工具窄 tab。M3：三 chip+附件，后端小补丁（schema v23）。M4/M5/M6 后端零改动。
> **会话中心化（2026-09-25，DESIGN §四）**：tasks 零新列、语义=窗内「委托」，无任务池/认领——画布默认 tab「会话看板」（SessionBoard）+「会话协作流」（SessionFlow，session-graph 四类边）；中途换人 RoleSwitchButton→`api.switchSessionRole`（POST /sessions/{sid}/role），历史/黑板全保留；旧壳冻结、这些能力只进新壳。

## 文件

- `NewShell.tsx`：壳根——数据轮询、选中/姿态/屏幕状态、落地态与持久化。
- `SideBar.tsx`：姿态分段 + ＋新建作战轻弹窗 + 资源入口 + 底部铃铛/设置/旧壳。
- `ProjectTree.tsx`：项目折叠组（编排器固定首行 + 任务行 + 无绑定辅助窗行）。
- `ConversationPane.tsx`（M2）：对话主区——事件圈定/状态派生/审批合并/滚动分页；编排态=OrchChatPane+运行记录折叠，会话态=column-reverse 气泡流。
- `ConversationComposer.tsx`（M2）：常驻 composer，草稿/模式按 sid 隔离（切行不丢）；note/指派/orch/task 四模式 + 继续/跑队列 chip。
- `ApprovalCard.tsx`（M2）：审批卡（裁决）+ Receipt 回执 + ApprovalAuditChip（裁决后紧凑态）。
- `Chips.tsx`（M3）：三 chip——AutonomyChip（编排态，写 config.autonomy，持久）/ RuntimeChip（会话态，写任务 preferred_runtime）/ ModelChip（写 config.executor_llm，持久+热换）；共用自写弹层（无 popover 原语）。hooks 勿在早返前漏声明。
- `FilesView.tsx`（M3）：产物文件只读浏览（kind 过滤/名称搜索/文本预览/下载），10s 刷新。
- `NarrowTabs.tsx`（M4）：姿态内窄 tab 共用件，导出持久化键 `ui.shell2.canvas-tab`/`tools-tab`。
- `CanvasView.tsx`（M4）：tab 序=**会话看板（默认，2026-09-25 会话中心化）**/发现/资产/黑板全景/任务流/攻击路径/轨迹链，rev-generic 追加逆向工作台；重型视图二次 lazy（xyflow 不进主包）。TaskFlow 喂空 pausedSids/wsBump=0 走自带轮询；内含 AttackPathHost/TraceHost。
  - **发现/资产（2026-09-26 补齐）**：lazy 复用 `@/views/Blackboard` 的导出件 `Findings`/`Assets`（旧壳 Blackboard 静态引用该模块，实际随主包加载）；Findings 传 `showCanvas={false}`（攻击路径已是独立 tab），CTF 线索级别/pentest 硬切换等 track 门控原样保留。
- `SessionBoard.tsx`（会话中心化，2026-09-25）：会话看板——五列（待命/工作中/暂停/完成/失败，状态即窗态），窗卡带当前委托与排队数，双击/单击进会话；委托队列读时派生、无公共池。
- `SessionFlow.tsx`（会话中心化）：会话协作流——`api.sessionGraph`→GET /projects/{pid}/session-graph，节点=编排器+会话窗，边四类 delegate/derive/inbox/dm，@xyflow/react 二次 lazy；轮询自动 fit。
- `RoleSwitchButton.tsx`（会话中心化）：窗卡/会话内「换人」入口→`api.switchSessionRole`（重定义窗底色、跨委托持续，区别于委托建议的热换装）；closed/专家不在池按错误码直显。
- `ToolsView.tsx`（M4）：浏览器/测绘/情报/产物文件/插件·MCP；浏览器/测绘无 pid 显 NoProjectHint。
- `WelcomePane.tsx`（M5）：无项目欢迎页（名称+轨+档卡，名称空禁用）。
- `TemplatesView.tsx`（M5）：模板中心（8 档卡+3 向导触发）；`TemplateWizards.tsx`：渗透目标/样本/CTF 三向导，全走既有端点。
- `placeholders.tsx`：资源占位兜底。`types.ts`：内部类型。

## 接手约定

- 写操作全走 `lib/api.ts` 现有口（note/publishTask/orchChat/abort/resume/goal/persona…），新壳不含业务逻辑、不旁路后端。
- 共用件：装配走 `lib/turnStream.ts`（`buildStreamItems`），编辑器在 `views/live/editors.tsx`——两边共用，改它旧壳同变。
- **审批无请求事件**：pending 靠 4s 轮询，按归属 sid+`created_at` 时间位注入；裁决后 extras 留回执 120s，超时由全量审批（20s）派生的审计 chip 接档。
- 铃铛：有 pending→跳首条归属会话；无 pending（仅 awaiting_human）→回落旧 ApprovalsView。
- 持久化：`ui.shell2.state`（姿态/选中/展开）；壳选择=`ui.shell`（null=新壳默认），URL `?shell=` 落账剥参统一在 `App.tsx`（两壳共用）。
- M6 坑：`expanded` 已随项目删除剪枝（死 pid 不再永久 404，启动首帧或有一轮）；M5 addInitScript 打的 fetch stub 会残留浏览器上下文，复测对不上数据先 browser_close。
- M4 坑：headless `visibilityState=hidden` 冻结 RO/rAF 致 xyflow 0 边（旧壳同样，非回归）；🔌插件入口先写 tools-tab=mcp 再切姿态；画布节点双击靠 `goto-session` 事件回会话；McpPane 是 `@/components/McpPane` 与 SettingsView 共用件。
- 资源入口（文件等）切主区屏幕，工作台仅 hidden 不卸载（对话保活）；排查"没切过去"记得 textContent 含 hidden 内容，要看可见性。
- chip 写口：autonomy/模型走 `patchProjectConfig`，runtime 走 PATCH task，无旁路。
