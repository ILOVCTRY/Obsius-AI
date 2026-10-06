# views/live/（直播间：指挥对话流 + 会话流 + 团队报告）

> 直播间主区由 `LiveRoom.tsx` 装配：指挥页签走 `OrchChatPane` 对话流，会话页签走
> `EventRow` 事件流；`TeamRunReport` 是团队运行报告全屏覆盖视图。**任务树 / 任务流 /
> 计划面板 / 协调驾驶舱已随任务机制退役（2026-10-06）**——顶栏不再有「任务树｜协调」分段。

## 入口

- `LiveRoom.tsx` 第一行页签：`__orch`「指挥」（无 session_id 的编排事件 + `team.*`）+
  各会话页签（原生 button，不用 radix Tabs——mousedown 激活坑见 webui/CLAUDE.md）。
  选中会话 → `EventRow` 事件流；指挥 → `OrchChatPane`。
- 事件源 `useEvents(pid, sid)`；编排页签额外 `api.eventsByKind(pid,"orch.chat")` 全量补拉
  （`orchHistory` 与直播流按 id 归并）+ `api.teams(pid)` 3s 轮询（团队卡实时态）。
- 已退役文件（全删）：`TaskTree.tsx` / `TaskFlow` 五件套 / `PlanPanel.tsx` /
  `CoordinationPane.tsx` / `coordination/*`（PlanDag/PlanControlBar/PlanConfirmDialog/
  CommunicationDrawer/coordinationFlow）/ `TraeView.tsx`；后端 `core/coordination/`、
  `blackboard/tasktree.py` 同步删除。

## 文件

- `OrchChatPane.tsx` — **编排对话流（对话化编排器 M1-M4，2026-09-21/22）**：指挥页签渲染
  （组件零 fetch，复用方自带数据）。`events` 过滤 `kind==="orch.chat"` 组装成轮（human 开轮 /
  orch 追加，**孤儿 orch 自成一轮不丢**）；**`TEAM_EVENT_KINDS` 集合导出**（`team.created/
  updated/run.started/member.updated/run.finished`），`timelineOf` 把 `orch.chat` 轮与 `team.*`
  团队卡合成时间线（每队一张卡，锚在首次出现处；团队卡优先取实时 `Team`，缺省回落事件快照，
  流末补列未锚定的队保「打开运行报告」可达）；`orch.compact`（指挥 `/compact` 手动压缩）
  落一行 `notice` 分隔提示「🧹 上下文已压缩」。**2026-10-06 渲染向开窗事件流看齐**（去气泡）：
  人类消息平铺 `❯` 一行、编排回复平铺 prose（`MarkdownView` 无 raw HTML）+ 作者·时间靠右小字、
  `tool_trace` 复用 `components/chat/ActivityGroup`（折叠摘要行，展开逐条轨迹）、思考复用
  `ThinkingBlock`、回复挂 `FileCardList`。🔔 主动唤醒徽章（`payload.proactive`，`WAKE_LABELS`
  中文映射 `team.run.finished`/`budget.soft_warning`/`phase.gate_open`）。props `{events,busy,
  persona,pid,teams,onEditPersona,orchRunning,onOpenTeamReport,onConfigureTeam}`；`pid` 供文件卡
  动作；busy 显思考行；**接近底部（<60px）才自动滚跟**。goal 条/身份设定经回调交父级弹层。
- `TeamRunReport.tsx`（**2026-10-06**）— **团队运行报告**（参考 cc-haha `AgentTeamsWorkbench`
  顶栏+编队）：直播间内全屏覆盖视图，顶栏（← 返回会话 / 团队名+状态 / 完成·进行中·待启动·失败
  计数 / Run 手选 / 3s 刷新）+ 编队成员卡 + **Run 成员执行明细**（objective/status/error +
  「打开执行会话」跳该会话页签）。数据=3s 轮询 `GET /teams/{id}` + `GET /teams/{id}/runs`
  （取最新或手选）。**Canvas 依赖图/通讯面板不做**（core/team 无共享任务列表/邮箱数据）。
  入口=团队卡「打开运行报告」。
- `editors.tsx` — **GoalEditor/PersonaEditor（2026-09-25 抽离）**：LiveRoom 专用；内容行为原样。
- `EventRow.tsx` — **直播流渲染布局层（2026-09-18，Claude Code 终端风格）**：导出 `StreamItem`
  （`{type:"pair",command,result?}` / `{type:"single",event}` / `{type:"turn",note,process,reply?,
  replyStream?}`）与 `EventRow({item,open,onToggle,roleNames,action?,onRouteJump?,assetName?,pid?})`。
  **memo 化（2026-09-20）**：比较器用事件对象引用相等；调用方 `onToggle`/`onRouteJump` 必须引用
  稳定（LiveRoom 已 useCallback）；`pid` 参与比较。分派五形态：①命令对折叠行（展开 IN/OUT，青点
  闪烁兜底）②`llm.thinking`=`● 思考 Ns`（`ThinkingRow`，非等宽）③`agent.chat`=`AgentChatRow`
  （正文平铺 `MarkdownView`，作者小字靠右）④错误/审批醒目行（`ERROR_KINDS`/`WARN_KINDS` 白名单）
  ⑤其余简洁单行。`lib/events.tsx` 只管筛选/标签/摘要，本文件管布局；状态点用 `bg-(--status-*)`
  括号语法。**skill.routed 命中行（F12）**：路由名单击跳技能。**会话流改造（2026-10-03，cc-haha
  风格）**：`TurnRow` 的 process 组折成 `components/chat/ActivityGroup`（展开仍是逐条
  `EventRowImpl`，**审计零回退**，第一硬约束）；纯函数 `toActivitySteps(process)`/
  `turnFileTargets(process)`；回复挂 `components/chat/FileCard`（`pid` 缺省只读）。
  **`lib/turnStream.ts` 与 `lib/events.tsx` 零改动**（`process: StreamItem[]` 契约不变）。
- `tree.css` — 任务树退役后**无消费者（可清理）**；原 `TaskTree.tsx` 深色控件覆盖。

## 关键约定

- 指挥页签对话流与开窗会话流**共用** `components/chat/*` 会话流件（`ActivityGroup`/
  `ThinkingBlock`/`FileCard`/`TeamCard`）——视觉统一，勿各自另起渲染。
- 团队卡数据双源：实时 `Team`（`LiveRoom` 3s 轮询 `/teams` 传入）优先，事件载荷快照兜底；
  两个回调交父级——配置弹窗 [`components/team/TeamConfigDialog`](../components/team/CLAUDE.md)、
  运行报告本目录 `TeamRunReport`。
