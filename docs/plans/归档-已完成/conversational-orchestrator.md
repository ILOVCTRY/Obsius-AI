# 方案：编排器专家化——可对话编排（对话窗 + goal 商议 + 虚拟单例）

- **状态**：**已实施（M1-M4 全量，2026-09-22）**——M1 对话通道 + M2 goal 闭环 + M3 专家拟人（2026-09-21：后端 chat_turn/goal/persona/virtual 专家 + 前端编排页签三段式/composer orch 态/C2 态退役）+ M4 异常订阅唤醒（2026-09-22：collect_wake_triggers 白名单锚点扫描 + chat_turn(wake=) + API `_maybe_orch_wake`/Job orchestrator-wake + 前端 🔔 徽章）；定稿决策已沉淀 DESIGN.md §四「对话化编排器（M1-M4 全量，2026-09-22）」
- **拍板记录**：§3（3 项决策）+ §5（九条打磨定稿，整批拍板）
- **关联代码**：`core/orchestrator/orchestrator.py`（tick LLM 工具循环 :718 / ORCH_TOOLS / `_pending_directives` :403 / 态势 `_stats._overview` / A5 replan）、`core/orchestrator/state.py`（tick 租约三函数 / 游标 cycles 白名单）、`core/autonomy.py`（record_llm_usage :157，source 自由字符串、预算不分 source）、`core/api/app.py`（`_build_orchestrator` :4103 装配 / tick·directive·replan 端点 / 审批）、`core/orchestrator/judgments.py`（判据三层）、`webui/src/views/LiveRoom.tsx`（__orch 页签 :539 事件流筛选 / composer 四态含「指挥编排」:37）、project.json meta（`Project.meta`，core/projects.py）
- **实施后**：定稿决策沉淀回 DESIGN.md（新版「四、任务与编排系统」下加功能条目），同步 core/orchestrator/CLAUDE.md；本文保留作方案背景

## 1. 愿景与背景

用户方向：编排器往**可交流**方向走——交流中触发发布任务；goal 模式商议阶段性目标、确认后持续派单；可询问下一步预计发布；**把编排器设计为专家角色**。

**核心判断**：编排器现状已是 LLM 角色（tick LLM 工具循环带 publish_task/spawn_session/write_digest、预算闸门、L0 提案/L1 审批分流、C2 指令单向通道），缺三样东西：**专家身份、双向对话窗、goal 商议闭环**。专家化 = 给它一张脸，不是重构。派单主循环（tick + 门控/配额/优先级）**原样保留**——对话是插队轮，LLM 不进自动派单热路径，成本与可靠性不变。

## 2. 现状盘点（2026-09-21 打磨核实）

| 目标能力 | 现状 | 差距 |
|---|---|---|
| 交流中触发发布 | C2 指令（`_pending_directives`「⚠ 人类指令（最高优先）」注入）+ LLM 工具循环 publish_task（dedup 预检/gate 预算闸门/审批分流） | 指令单向下达，不能对话来回 |
| goal 商议→确认→持续派单 | tick 循环本就是持续派单；mission+判据三层是派单依据，但 mission 是 config 写死文本 | 缺「对话商议→确认落盘→注入派单」动态闭环 |
| 询问下一步计划 | 态势原料齐全（uncovered/门进度/配额/open 任务/目标负载） | 无问答入口翻译成人话 |

**打磨期关键核实**（影响方案形态的事实）：

- **编排器 tick 本就无多轮记忆**：messages 每轮从零开始（orchestrator.py:707），上下文靠「上一份简报常驻（截 2000）+ 新事件窗口 40 条 + 战役记忆召回」重建——对话历史是纯增量，无迁移负担；
- **游标/cycles 是巡检节奏状态**（state.py `_EDITABLE_FIELDS` 白名单）：event_cursor 事件窗口追赶、cycles 驱动 digest_every 节奏——插队轮不应触碰；
- **租约三函数现成**：acquire/renew/release + TickLeaseError（API 手动 tick busy→409 先例）；
- **记账 source 自由字符串**：record_llm_usage 的预算累计不分 source（usage_add_llm 全量），仅事件标注差异；
- **「编排」页签已是事件流筛选视图**（`__orch`：编排任务全生命周期+digest+starvation+LLM 失败），composer 已有四态（发任务/对话/指派任务/指挥编排）；
- **expert-pool 已定稿**（experts/\*.yaml 扁平池 + 项目绑定 ≥1 专家 + roles 退役）——虚拟单例需衔接但不被阻塞。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | goal 落盘 | **结构化阶段目标存项目 meta**：编排系统提示每轮注入，确认时事件留痕（`goal.confirm`）；与 pentest 三阶段方案咬合（goal=阶段目标的对话式实例化） |
| 2 | 专家池身份 | **虚拟单例专家**：可改名字/persona（如「指挥」），工具集固定、不认领任务、不执行命令、不占绑定名额，**删不掉只可改名** |
| 3 | MVP 范围 | **四件套先行**：对话窗 + 计划问答 + 对话发布 + goal 商议；异常订阅唤醒（黑板异常时编排器主动开口）后置 |

## 4. 设计详述（打磨定稿版）

### 4.1 三能力实现路径

- **对话发布**：对话轮 LLM 带 ORCH_TOOLS，publish_task 走现行**全部闸门**（dedup/gate/噪声校验/L1 审批/on_task_published 绑窗+计数同源）——v0.72 口径不变，对话只是起草方式变了，闸门没变；
- **goal 商议**：多轮对话 → 结构化目标草案 → 用户确认 → 写 `meta.phase_goal` + `goal.confirm` 事件 → 下一 tick 起系统提示注入（`goal_section` 槽）→ tick 按 goal 派单 =「确认后不断派发」；
- **计划问答**：编排器读自己态势人话作答——它就是权威（门没过会答「还差 ⭐1 才能开打」）。

### 4.2 对话轮机制（插队轮）

**触发与租约**：新端点 `POST /api/projects/{pid}/orchestrator/chat {text}` → 同步 `acquire_tick_lease(owner="chat-<uuid>")`，busy → **409**「编排器正在思考」→ submit 后台 Job（`orchestrator-chat`），前端 pollJob；对话循环每 LLM 步 `renew_tick_lease` 心跳，收尾 `release_tick_lease`。**不排队**：busy 时前端 toast+输入保留，编排 Job 或 chat Job 在跑时 composer 置 busy 态。

**对话历史 = events 表 `orch.chat` 单一来源**（零新表零文件）：人类消息 `{role:"human", text}` author=human；回复 `{role:"orch", text, tool_trace?}` author=orchestrator，**text 截 2000**（对齐 agent.chat），工具调用过程折叠进 tool_trace（前端可展开，审计零丢失）。多轮上下文 = 取最近 40 条 orch.chat 按序组装 messages；否掉 chat-\<pid\>.json 文件（编排器对话无续跑/快照需求）。

**态势只读**：复用 `_stats()` 组装但**不走游标**——事件窗口固定取「最近 100 条」；不推进 event_cursor、不计 cycles、不动 last_digest_cycle（digest_every 节奏只数 tick 轮次）、不消费 C2 指令（不标 directive.done，存量指令仍由 tick 消费）；对话轮自身不写 state_saver，唯一写面 = 工具副作用 + orch.chat 事件。

**系统提示**：复用 tick 组装（overview/role_catalog/autonomy_notice/mission_section/campaign_section/goal_section）+ persona 段（4.4）+ 对话纪律段（可对话、可发布、发布走闸门、计划问答用态势作答）。

**记账**：`source="orchestrator-chat"`，**预算同源同池**（usage_add_llm 全量累计，gate/软警告不分 source）；record_llm_usage 的 author 判定补分支 `source=="orchestrator-chat" → "orchestrator"`；前端 usage 可按 source 区分 tick 与对话消耗。

### 4.3 goal 闭环

**schema**（存 project.json meta，即 `Project.meta`；与 pentest 方案 meta.current_phase 并列同层）：

```json
meta.phase_goal = {
  "text": "拿到 flag{...} / 完成 OA 系统渗透出报告",
  "criteria": ["……", "……"],
  "phase": "recon | pentest | report | null",
  "source": "chat",
  "created_at": "…", "confirmed_by": "human"
}
```

- `criteria` 是**人话验收口径**（LLM 评估用），不是机读判据——判据三层（judgments）不动，不做机读联动；
- **确认流**：对话中编排器给结构化草案 → 用户确认 → `PUT /projects/{pid}/goal` 写 meta + 落 `goal.confirm` 事件（payload 带全文快照，goal 变更历史=事件流可回放）；清空重议走 `goal.clear` 同款；
- **注入**：tick 与对话轮系统提示都注入 `goal_section` 槽（mission_section 之后）——「## 当前阶段目标（人类确认）」+ criteria □ 清单；goal 与 mission 共存不合并（见 4.7）。

### 4.4 虚拟单例专家

- **不入 experts/\*.yaml 文件池，运行态注册**：虚拟单例是「每项目一个的运行态专家」，无 yaml 文件（不沉淀分享、不进 16 专家迁移映射）；专家池 API 列表（expert-pool M2 落地后）恒追加 `{id:"orchestrator", name: display_name, kind:"virtual"}`；绑定页/组队选择器不出现（不认领不执行）；
- **名字/persona 定制**存 `meta.orchestrator_persona = {display_name, persona}`；persona 只注入**对话轮**系统提示（tick 巡检不需要脸）；页签/头像/气泡用 display_name；
- 对 expert-pool M2 零阻塞——API 追加点在专家池实施时接，M3 可独立先行。

### 4.5 前端信息架构（编排页签三段式）

- **顶部 goal 条**：当前阶段目标一句话 + criteria 进度入口（pentest 方案 M4 阶段条集成）；无 goal 时显示「与编排商议目标」入口；
- **中部对话流主区**：orch.chat 事件渲染——复用会话对话轮范式（human_note 开 turn → 人类右气泡 → 过程折叠组 → 编排器左气泡 MarkdownView 终稿）；tool_trace 默认折叠；
- **底部 composer**：新增第五态「与编排对话」，**缺省推荐**；tick 历史/digest/提案收「运行记录」折叠区（现行 `__orch` 事件筛选原样保留，默认折叠）；
- **C2 态退役（用户拍板：对话窗全替代）**：composer「指挥编排」态删除；`orch.directive` API 端点**标记 deprecated 保留兼容**（CLI/脚本入口，存量未消费指令仍被 tick 消费）；「下轮 tick 执行」的语义由对话自然表达（对话里说清楚，编排器当场确认或插队轮直接办）。

### 4.6 异常订阅唤醒（M4 后置批）

**触发白名单（首批 5 项，宁少勿扰）**：

1. task.failed——N 分钟窗口**聚合**一条，不逐条开口；
2. task.starvation 新告警（饿死）；
3. 绑定窗被关致任务悬 open（复用 starvation ⑤ 分支）；
4. budget.soft_warning（80% 阈值首发）；
5. goal 关联阶段门指标满足（pentest 方案集成后）——「目标已达成，是否流转？」。

**形态**：编排器侧独立声明表（不进会话收件箱 `_INBOX_SUBSCRIPTIONS`）；唤醒=自起对话轮（带异常上下文），落 orch.chat `{role:"orch", proactive:true}`。**白名单制**：加新触发须进此清单评审；同类事件聚合窗口防轰炸。

### 4.7 mission / goal / C2 三者关系

| 层 | 载体 | 语义 | 生命周期 |
|---|---|---|---|
| **mission=北极星** | config.mission（text+criteria） | 轨语义行动边界+项目判据 | 静态配置，项目级 |
| **goal=阶段目标** | meta.phase_goal | 当前阶段的对话式目标 | 动态，商议→确认→可清空重议 |
| **C2=单轮指令** | orch.directive 事件 | 一次性指令最高优先 | 消费即弃（UI 入口已退役，API deprecated 兼容） |

系统提示槽序：**C2 指令段（⚠ 最高优先）> goal_section（当前阶段目标）> mission_section（行动边界/判据）**。三层互补不合并：mission 定边界，goal 定当下，C2 定瞬时。

### 4.8 与自主档/审批的关系（全部现行口径，零新机制）

- L0：对话发布走提案模式（propose_only 现行）；
- L1：publish → 发布即建待命窗 + 执行审批单（现行）；
- L2：直接发布；
- goal 确认 = 事件留痕，不设审批单（goal 非风险动作）。

## 5. 打磨定稿记录（2026-09-21 九条整批拍板）

| # | 打磨点 | 定稿 |
|---|--------|------|
| 1 | 对话历史存储 | events 表 `orch.chat` 单一来源（零新表零文件）；回复截 2000 对齐 agent.chat；上下文取最近 40 条；否掉独立文件（无续跑需求） |
| 2 | 与 tick 状态共用 | 对话轮=纯插队只读：不推进 event_cursor（固定最近 100 条事件窗）/不计 cycles/不动 last_digest_cycle/不消费 C2；工具面全闸门同源；不写 state_saver |
| 3 | goal schema | meta.phase_goal={text, criteria[]（人话口径）, phase（对齐 current_phase，可 null）, source, created_at, confirmed_by}；PUT /goal + goal.confirm/clear 事件留痕；goal_section 槽注入 tick+对话轮；判据三层不动 |
| 4 | 记账口径 | source="orchestrator-chat"，预算同源同池不分 source；author 判定补分支→"orchestrator" |
| 5 | 编排页签信息架构 | 三段式：goal 条 → 对话流主区（复用会话对话轮渲染范式）→ composer 第五态「与编排对话」缺省推荐；tick 历史/digest/提案收「运行记录」折叠区 |
| 6 | busy 交互 | 不排队：同步 acquire 租约 busy→409「编排器正在思考」；前端 Job 在跑时 composer 禁用+输入保留 |
| 7 | 异常唤醒触发清单（M4） | 首批白名单 5 项（task.failed 聚合 / starvation / 绑窗关闭 / budget.soft_warning / goal 阶段门满足）；自起对话轮落 orch.chat proactive:true；白名单制评审 |
| 8 | 虚拟单例呈现 | 不入 experts/\*.yaml 文件池，运行态注册；专家池 API 恒追加 virtual 条目；绑定页不出现；meta.orchestrator_persona 定制，persona 只注入对话轮 |
| 9 | mission/goal/C2 关系 | 分层互补（4.7 表）；**C2 退役路线=对话窗全替代**（用户修订：composer「指挥编排」态删除，API 标 deprecated 保留兼容，存量指令仍被 tick 消费） |

## 6. 实施切分（M1-M3 已实施 2026-09-21，详见 DESIGN.md §四「对话化编排器」）

- ~~M1 对话通道 / M2 goal 闭环 / M3 专家拟人~~——**已实施剔除**（chat 插队轮租约 409 / goal 闭环 goal.confirm 留痕 / 虚拟单例 persona + 前端编排页签三段式 + composer orch 态 + C2 态退役）。
- **M4（待排期，本方案唯一剩余计划）**：异常订阅唤醒（触发白名单 5 项：task.failed 聚合 / starvation / 绑窗关闭 / budget.soft_warning / goal 阶段门满足 + 聚合窗口 + 编排侧声明表，见 §4.6）。
