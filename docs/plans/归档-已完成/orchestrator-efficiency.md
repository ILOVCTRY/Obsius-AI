# 方案：编排器效率与平台判据收敛（实战反馈六簇：只读工具面 / 覆盖度对账 / 生命周期 / 去重与授权提案 / 稳定性修复 / 记忆沉淀）

- **状态**：**M1-M6 全量已实施**（2026-09-22 用户拍板「M1+M2 连做 + 429 语义搭车」→ 同日「继续推进下一批」授权 M3、M4；09-23「开工吧」授权 M5+M6，打磨先行的 §0-9/10/11 三项口径全落地）。实施修正注记见 §0
- **实施记录（2026-09-22）**：
  - **M1 稳定性修复包**：E1 spill uuid 后缀（retention.py，`<时间戳>-<工具名>-<uuid6>.txt`）/ E2 拒绝熔断（tools.py 统一常量 `REJECT_PREFIXES` + loop.py 连续 ≥3 次→awaiting_human，夹一次非拒绝清零；落 `agent.reject_breaker` 事件）/ E3 前端思考行默认折叠（events.tsx defaultOpen:false——核实发现终稿后 delta 本就能正确跳过，真实缺口只是思考中行展开刷屏，OrchChatPane 无需改）/ E4 回执三路径（tasks.py：pending_receipts 挂父任务 context 内嵌补投 + `task.receipt_self` 事件替代自收静默 + ref_id=`<task_id>:<status>` 状态后缀修成功回执被吞）
  - **搭车项·429 传输语义**（用户拍板纳入）：loop.py `_fail_task_on_error` 加 LLMError 分支——传输层失败（429/5xx/重试耗尽）不 salvage、双写会话键+任务键现场快照、fail(awaiting_human, resumable=true)，与 E2 拒绝熔断统一「停轮保护」语义；failed 恒可续跑不变
  - **M2 只读工具面**：ORCH_QUERY_TOOLS 四工具（task_detail 全量核对 / bb_overview 分区总览 / budget_status 预算余量 / session_list 窗清单）+ A2 态势放宽（事件行与 result_note 截断 [:120]/[:100]→[:300]）
  - **M3 覆盖度平台化**（同日）：B1 `core/coverage.py`（终态四味 / 收敛传播 / coverage_report 分组对账，口径见 §0-6）/ B2 `_assets_view` 出口新增 `assets.coverage` 分组收敛摘要（tick 态势与 bb_overview 同源）/ B3 门状态单一事实源（`_phase_gate_check` 每轮评估落 meta `phase_gate_state`、`enter_phase` 流转顺写新阶段零空窗、`_phase_section` 只读不重算；口径见 §0-7）；测试：新增 tests/test_coverage.py 五用例 + test_orchestrator（coverage 注入 / phase_section 状态驱动化）+ test_phases（gate_state 单一事实源 / API check 写入）
  - **M4 生命周期补全**（同日）：C1 `TaskQueue.cancel_task`（open/claimed→failed + blocked_reason=cancelled + 清 claimed_by/租约 + attempts 履历 + 资源租约释放镜像 _finish；返回 `{claimed_by, target_session}` 供调用方打断）+ `requeue_task` 工具复用 reopen 原语零新代码 + 编排器两工具自主档三分流（L0 提案 / L1 审批卡 / L2 直执+_interrupt_window request_abort 只打断不关窗）+ 事件 `task.cancelled`（不复用 task.failed——后者是 M4 唤醒触发器）+ API 人工端点 `POST /api/tasks/{id}/cancel`（409 已终态）+ 两个审批处理器（终态跳过语义）+ 前端 LiveRoom 采纳分支与 task.cancelled 事件样式；C3 timeout 提示落 publish_task 工具描述+决策纪律第 7 条（单点编排器提示，不铺 16 份 expert yaml）；测试：test_blackboard cancel_task 单元 + test_orchestrator L0/L1/L2 三档 + test_api 端点与审批处理器，全量回归绿 + build 零 TS 错误
  - **M5 D1+D2 + M6 F1**（2026-09-23，用户「开工吧」授权，口径全落 §0-9/10/11）：
    - **D1 疑似重复警告**：store.add_finding 尾部查询同 target_asset_id+同 vuln_class（非空）+dedup_key 不同的行（`id != 自身`，cap 3 最新优先）→ 返回 dict 加 `dedup_warning`；tools `_tool_bb_add_finding` 拼成「[疑似重复] 同目标已有同类发现：…请补证据而非新开条目」进工具返回值（单次按需，无频控）。注意语义：合并分支（同 key 重报）被并入的本体行不算，但**另一条分裂指纹仍会提示**（「你已并入 k-time，但同目标还有 k-order」）
    - **D2 authorization 审批**：独立工具 `request_authorization(kind, scope_request, justification, evidence_finding_ids?)` 三 kind（scope_expand/impact_escalate/rating_override），payload risk 恒 high；app.py `_exec_approved_authorization` **纯回流处理器**（批准=人类授权，无平台动作；scope_expand 后 Agent 自行 bb_add_asset）+ message.inbox 事件；decide rejected 分支统一 `approval_rejected` 回流（escalation 搭车受益——此前 rejected 无回流=Agent 空等）；共享 `mission_boundary_lines(track, config)`（orchestrator.py 模块级，_mission_section 与 approvals 出口同源）→ API `list_approvals` 每条附 `boundary` 字段，前端审批卡存在才渲染
    - **F1 死路记账**：`_sediment_verdict` 四档判定——done+FP-only（复用 `_task_finding_ids` 事件反查归属）→ campaign 写 `tags=["dead_end"]` 负知识（content 优先 result_note、空则 `〔死路〕FP title` 兜底）、不复盘；done 零产出但 result_note ≥500 → 只跑复盘（每会话上限 1 条，`_sediment_lite_used` 内存计数在产提案前占用）；campaign.add 加 tags 参数（表结构零变更）；`_campaign_section` 注入侧按 tags 分两组（正向「既往战役打法」+「⚠ 既往死路（勿重走）」，recall 池不拆组内照旧打分）
    - 测试：test_blackboard（dedup_warning 同 key 合并/无 target/空 vuln_class/cap3）+ test_agent（三 kind 审批单/拒收/证据 cap10/D1 文案/双 notice 注入对话轮/lite 复盘档会话上限）+ test_campaign（FP-only 死路条目 tags+事件 dead_end/空收尾 title 兜底）+ test_api（批准 authorization 纯回流/拒绝 rejected 回流两 op/杂项 op 不回流/approvals boundary 出口）+ test_orchestrator（campaign 注入死路分组）；全量回归 + build 零 TS 错误
  - **测试**：test_task_receipt_guards_and_dedup 断言升级为 2 条（failed+done 各一，E4-③ 新语义）；test_dispatch_spills_oversized_result glob 适配 uuid 后缀；test_budget_exhaustion 剧本夹 bb_query 清零熔断计数（原剧本 3 连 run_cmd 撞计划闸会触发 E2——顺带验证了熔断对计划闸死循环的拦截有效）；全量回归绿 + 前端 build 零 TS 错误
- **拍板记录**：§3
- **来源**：虚拟编排器 20+ 轮中原工项目实战自我汇报（17 条主张），经**逐条代码+数据验证后立项**——13 条成立（含 4 条精确坐实）、2 条机制修正、2 条不成立（不立项只作澄清）。验证方法：4 路并行代码核查 + 8420 生产数据实测。
- **关联代码**：`core/orchestrator/orchestrator.py`（ORCH_TOOLS :87-147 / REPLAN_TOOLS :152-176 / _dispatch :1210-1219 / _stats :553-629 / result_note[:100] :715 / 事件行[:120] :491,:521 / _assets_view :631-691 / _phase_section :361-402）、`core/blackboard/tasks.py`（_finish 回执 :572-678 / 父未认领不投 :637-641 / 自收抑制 :641 / ref_id 去重吞回执 :660 / fail_interrupted_claims :1194-1219）、`core/agent/retention.py`（spill_text :38-52）、`core/agent/tools.py`（_SPILL_THRESHOLD :53 / 计划闸 :935-943 / request_escalation :1797-1835 / complete,fail_task 沉淀钩子 :1717-1770）、`core/agent/loop.py`（stuck_after :146,:2050-2052 / _sediment_verdict :2149-2185 / _campaign_memory :2187-2218 / thinking 节流 :1188-1221）、`core/blackboard/store.py`（add_finding 去重合并 :1371-1485 / set_asset_status :1194-1236 / inbox_post :519-534）、`core/blackboard/schema.py`（findings UNIQUE :157 / inbox 未读唯一索引 :258-259）、`core/api/app.py`（_phase_post_tick idle+1 :4344-4352 / _phase_gate_check :4288-4342 / _sweep_restarted_project :1001-1055 / _maybe_auto_tick :4016-4074 / _maybe_auto_resume :3689-3722）、`core/phases.py`（gate_metrics :234-243 / evaluate_gate :246-256）、`webui/src/lib/events.tsx`（thinking delta defaultOpen :80-101）、`webui/src/views/LiveRoom.tsx`（FILTERS :492-513 / liveThinking :802-825）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§六 编排器节 / §12 黑板节），同步 `core/orchestrator/`、`core/agent/`、`core/blackboard/`、`core/api/`、`webui/` CLAUDE.md；本文保留作方案背景

## 0. 实施修正注记（2026-09-22，M1+M2 批次）

1. **E3 收敛口径修正**：实施核查发现「终稿到达后前端不收敛 delta」不成立——终稿 `llm.thinking` 带同 stream_id 到达后 items 装配层本就跳过同流 delta 行（且后端 prune 落库），直播窗口内真实缺口只是**思考中行 defaultOpen:true 展开全文刷屏**；修复即改 `defaultOpen:false`，方案原文的「收拢为一行已思考 N 字」无需做。OrchChatPane 经查不消费 thinking/delta，零改动。
2. **E2 前缀统一常量落地**（§5 清单 #7 勾销）：`REJECT_PREFIXES = ("[计划闸]", "[越界拒绝]", "[网关拒绝]", "[拒绝]")` 定义在 tools.py（与 `_TOOL_FAIL_PREFIXES` 同址），`[错误]`/`[冲突]`/`[防幻觉]` 是业务失败不算拒绝；**新增拒绝路径必须复用该常量回填**，私开新前缀会绕过熔断与审计判定。
3. **E4 schema 裁定**（§5 清单 #8 勾销）：pending_receipts 走 tasks.context JSON 内嵌（`_defer_receipt`/`_flush_pending_receipts` 两方法），**零 schema 变更**；rcpt 补 session_id 字段供补投事件 author 用。
4. **task_detail 体量**（§5 清单 #1 勾销）：选定键 json.dumps（plan/context/result_note 全文），context 出口本就解析过的结构化 dict，实测体量可控；暂不做分段拉取，出现超大 context 再议。
5. **budget_status 只读不判定**：不做闸门判定（闸门仍是 API 注入的 gate 回调），仅给余量数字。
6. **B1 传播边界与 group_key 口径**（§5 清单 #2 勾销，M3 实施拍板）：
   - **终态判定**：`status ∈ {tested_clean, na}` ∪ `meta.dead_end` ∪ 有 finding 挂链（FP-only 视作死路味）；`visited/scanning` 算 visited；**budget_stop 算 open**（预算停 ≠ 测完，宁严勿松）；
   - **收敛传播**：叶子（无子）`converged = 自身 terminal`；父节点 `converged = 自身 terminal OR（自身至少 visited AND 全部子 converged）`——url 全部终态但 host 本体连 visited 都不是 → host 不收敛、进 uncovered（host 还有 IP 直连面/服务面未摸，借子收敛必须有自身 visited 佐证）；domain 节点同规则不豁免（DNS 面测完应由 agent 标 tested_clean）；
   - **group_key**：沿 parent_id 链走到根，根 domain → `domain:<value 小写>`；根 host → `host:<value 小写>`；无 parent 的孤儿（url/service 等）自成组 `<type>:<value>`——与 target_keys_of 的 ip:/host:/domain: 归一化键口径对齐（url 派生 host、domain 优先）；
   - **对账面**：只计 host/domain/url/service 四类（与 _assets_view targetable 口径一致），binary 等不入组；
   - **agent 侧 dead_end 标记路径**（bb_asset_status 无法写 meta）**不在 M3 做**——与 M5/M6 的 F1 死路记账口径（§5 #9：category 结构化）同源一起收口；coverage 判定不依赖它（tested_clean/na/finding 挂链三路已通）。
7. **B3 phase_gate_state 写入时机**（§5 清单 #3 勾销，M3 实施拍板）：
   - **写入点**：API 层 `_phase_gate_check` 每次评估后**无论过门与否**都写 project meta `phase_gate_state = {phase, gate, passed, unmet[]}`（gate=False=本阶段无门）；无时间戳字段——内容不变跳过写盘，防每 tick 无谓重写 project.json；
   - **流转重置**：`enter_phase` 抵达校准段同时写入**新阶段**的门状态（gate_metrics 本就在此计算，顺写零额外开销）——不存在「清空空窗」，新阶段第一轮注入即有真值；
   - **注入侧**：_phase_section 只读该状态不重算（无状态/阶段不符 → 显示「待编排校准」）；**动作侧** gate_block_reason/_phase_gate_reject 保持 publish 时现算确定性不变。
8. **C1 cancel 打断链路复用边界与事件语义**（§5 清单 #4 勾销，M4 实施拍板）：
   - **打断复用 request_abort，窗不关**：cancel ≠ 关窗——编排器 `_interrupt_window` 只 `sess.request_abort()`（在跑窗走 _abort_current_task→fail 撞 ClaimError 被 try/except 吞掉——既有容错先例；空闲窗标志在 loop 检查点自清）；窗保持 open 待命可接新任务；
   - **发起方区分靠事件 by 字段**：`task.cancelled` payload.by ∈ {human / orchestrator / approval}（作者=by），不另开事件 kind；人工口 API 端点 by=human、编排器 L2 直执 by=orchestrator、审批处理器 by=approval；
   - **blocked_reason='cancelled' 新档不进唤醒**：与 aborted 同属不进复盘与战役记忆的档位；不进 task.failed 唤醒白名单——task.cancelled 独立事件 kind（task.failed 是 M4 唤醒触发器，主动取消不该触发异常唤醒）；
   - **cancel_task 清 claimed_by**：防 worker 事后 complete/fail 覆写取消态；attempts 履历照记（outcome=failed/blocked_reason=cancelled）；返回 `{claimed_by, target_session}`（取消前值）供调用方打断；
   - **requeue 复用 reopen 原语**：awaiting_human 本就是 failed+blocked_reason 档，状态机自动满足「failed→open」，零新代码；
   - **审批处理器键名坑**：decide 端点 `result.update(**handler_result)` 会覆写响应键——处理器返回**不得用 `status` 键**（decide 响应的 status 承载批准决定），用 `task_status`。
9. **D1 疑似重复警告口径**（§5 清单 #5 勾销，M5 实施前打磨）：
   - **判定口径**：`add_finding` 写入后查 `project_id + target_asset_id IS ? + vuln_class=? + dedup_key != 本次key`（排除合并分支——同 key 已自动并集不算）命中 → 返回 dict 加 `dedup_warning`（cap 3 条）；dedup_key=自身 id 的无键行也参与（无键恰是最易分裂重复的来源）；
   - **载体与频控**：警告只进**工具返回值文案**（`[疑似重复] …坚持新增请忽略；同一问题请 bb_update_finding 补证据而非新开条目`）——**不落事件、不进编排器态势、无频控**：工具返回值天然单次按需、随写入动作走，不存在「重复打扰」，§5 #5 预设的「每会话提醒上限」基于警告会刷屏的假设，实施核查后不成立，不设；
   - **不做**：自动合并放宽（仍只认同 key，宁严勿松）/ 前端展示。
10. **D2 authorization 审批形态**（§5 清单 #6 勾销，M5 实施前打磨）：
   - **独立工具** `request_authorization(kind, scope_request, justification, evidence_finding_ids?)`，**不复用 request_escalation**（那是「命令放行一次」语义、签名带 cmd/runtime；本工具无命令可执行）；三 kind：`scope_expand`（扩大授权目标）/ `impact_escalate`（影响证明升级）/ `rating_override`（突破收录口径）；payload `{op:'authorization', kind, scope_request, justification, evidence_finding_ids[], task_id, session_id}`，**risk 恒 high**（行为边界申请，审批 UI 醒目）；
   - **恒 human 架构上已成立**：Agent 产的审批单只有人类 decide——L2 不自动批无需新机制；但 op=authorization 要**进 _APPROVAL_OP_HANDLERS 白名单做纯回流处理器**（批准后 inbox_post `authorization_result` 回流提交会话 + 事件；**不做任何平台动作**）；
   - **scope_expand 批准后链路**：批准=纯授权，资产登记仍由 Agent 走 bb_add_asset（工具现成，登记后 check_target 自然放行）——处理器不代登记（scope_request 是自由文本非规范资产值）；
   - **rating_override 批准后**：无平台解锁逻辑（C6 门禁只拦 info/结构缺失，不拦 high）——批准=授权 Agent 按更高口径重新登记/patch，回流文案写明；
   - **拒绝回流搭车收口**：现状 escalation rejected 无回流=Agent 空等（decide rejected 分支只翻状态）——decide rejected 时 action.session_id 存在且 op ∈ {escalation, authorization} → inbox_post `approval_rejected`（escalation 同受益）；
   - **审批卡边界全文**：GET /projects/{pid}/approvals 出口每条附 **`boundary` 字段**（server 端拼轨级行动边界：redteam=ROE 四要素 / pentest=验证上限一行，与 _mission_section 同源数据但不经 LLM），前端 ApprovalsView 通用卡存在才渲染折叠区——人类在审批卡里直接看到当前边界，与申请对照。
11. **F1 死路记账识别口径**（§5 清单 #9 勾销，M6 实施前打磨；**推翻原 category 结构化建议**）：
   - **拍板：status='false-positive' 就是死路的结构化口径**——C6 category 词表仅 vuln/intel 两类且全链路（收录门禁/报告过滤/前端线索卡/链路图）按两类消费，加第三类动面宽；而 FP status 已是一等死路标记（撤回传播/C2 死路徽章/C3 路标注入/coverage FP-only 死路味全在消费它），零新约定；title 前缀【死路记账】不采纳（自由文本脆弱，kb 提案里的人工写法不构成平台口径）；
   - **campaign 写入扩档**：`_sediment_verdict` done 分支扩——本任务产出 FP finding（复用 `_task_finding_ids` 事件反查口径：区间+author 双约束）且无 verified/exploited 链 → campaign=True、review=False（死路进全局库不产 kb 复盘提案——负知识入 campaign 够用，避坑方法论可走高质量复盘档）；
   - **写入构造**：tags=**["dead_end"]**（表结构零变更）；content 优先 result_note（收尾总结最完整）、空则 fallback FP finding title；title 照旧取任务 objective；
   - **召回呈现分流**：`_campaign_section` 注入侧按 tags 分两组——正向「既往战役打法」照旧 + 死路条目进「⚠ 既往死路（勿重走）」组；recall 池不拆（死路也是贴合目标的记忆），组内照旧打分排序，usage 计数照常（防重走价值同源）；
   - **高质量复盘档**：done 无产出（无 verified/无 FP/无链）但 result_note ≥500 字 → review=True，**复用 _sediment_proposal 全流程**（kb 对账+提纯 instruction+证据锚点，不另建轻量管线——「轻量」落在量控）；**每会话上限 1 条** = AgentSession 内存计数器（重启清零可接受，人工审批关兜底防滥用面）。

## 1. 背景与验证结论总表

编排器实战汇报 17 条主张的验证裁定（详细证据链见会话记录，此处存结论）：

### 功能点

| # | 主张 | 裁定 | 关键证据 |
|---|------|------|---------|
| 1 | 无只读查询工具；result_note 截断无法独立核对 | ✅✅ 精确成立 | 工具面仅 publish_task/spawn_session/write_digest/done；编排器可见 result_note 仅 :715 的 100 字符 + 事件行 120 字符，500 字符版只进库与父窗私信；预算余量不注入 |
| 2 | 覆盖度对账无平台判据，纯数数烧 LLM | ✅ 成立 | 无站点归组/子资产状态传播；实测 157 资产 tested_clean/dead_end 标记 **0 个**（scanned 仅 17）——mission criteria 终态语义平台未承接 |
| 3 | 生命周期缺 cancel/requeue；断点续跑靠人工 | ✅ 成立 | 编排器对话轮无 cancel/requeue（set_priorities 仅手动触发独立回合）；重启后孤儿任务默认 fail 等人工、idle 窗不自动恢复。**「900s run.cmd 上限」不成立**：默认 120s 可自传无封顶（900s 是反编译 headless 与编排 tick 租约，编排器记混） |
| 4 | findings 重复登记靠手工清理 | ✅ 成立 | 去重机制存在（UNIQUE(project_id,target_asset_id,dedup_key) 自动静默合并），但 dedup_key 由 Agent 自由传——实测 AES 三条并存即各传不同 key 绕过；合并无声无息无冲突提示 |
| 5 | 缺授权升级提案通道 | ✅ 成立 | 提案仅 kb/skill/index/case；request_escalation 只受理 net=real/runtime 超限；48 findings 全 low、high value 长期为空与之吻合 |

### 稳定性与体验

| # | 主张 | 裁定 | 关键证据 |
|---|------|------|---------|
| 6 | 上下文自相矛盾（门未过 0/1 vs 已过门） | ✅✅ 根因精确定位 | 判门函数唯一，但态势注入在 tick 开始（用上一轮 idle）而过门判定在 tick 结束且 idle 先 +1 再判（app.py:4351）——同轮不同步窗口；「已过门」无持久列 |
| 7 | spill 文件同名覆盖丢数据 | ✅✅ 精确成立 | spill 命名仅（秒级时间戳+工具名）两维，查 findings 与查 tasks 同为 bb_query——同秒两次超限必互相覆盖 |
| 8 | plan gate 拒绝后死循环空转 | ✅ 成立 | 拒绝均为纯文本工具结果，无同类拒绝计数熔断；仅 max_steps=200 + stuck_after=8 顾问兜底 |
| 9 | thinking.delta 刷屏淹工具结果 | ✅ 成立 | 思考中行 defaultOpen 展开全文且终稿后前端不收敛（prune 不广播 WS）；工具行默认折叠——视觉权重倒挂 |
| 10 | 空闲窗不回收、idle 烧 token | ⚠️ 机制修正，警报成立 | 窗不因 idle 烧 token；真凶是 L2 下 worker 空退→自动 tick 链→编排器 LLM 轮无人值守起跑 + C4 自动 +200 续跑 |
| 11 | 记忆沉淀靠手、跨项目只存 1 条 | ✅✅ 数据精确证实 | campaign-memory 全局恰 1 条；根因=sediment 触发窄（仅 done+verified finding / failed+error），**死路记账不进记忆**——而渗透里死路是最有价值的负知识 |

### 两个小 bug

| 主张 | 裁定 |
|------|------|
| 据答复选框渲染重叠 [□□] | ❌ 不成立——全库无对应代码，审批交互全是按钮且间距正常（编排器幻觉，澄清记录） |
| 并发发布回执丢失 | ✅✅ 成立——三条真实路径：①父任务未被认领时子任务完成→回执不投且**永不补投**；②父子同窗连做→自收抑制无替代通知；③fail→reopen→complete 的成功回执被 ref_id 去重吞掉 |

## 2. 现状盘点补充

- **编排器态势注入**（_stats 全量 dump）：open 任务 objective[:80] 不含 result_note、recent_closed result_note[:100]、findings 标题[:60]、事件行 payload[:120]、digest[:2000]——聚合+截断的被动只读，无按 id 拉详情通道。
- **资产状态**：六态白名单逐行更新（set_asset_status）不传播，前端以 findings 反查显徽章；_assets_view 有 covered/uncovered 粗推导（visited/scanning/tested_clean 算已覆盖）但无分组收敛语义。
- **sediment 链**：判定单一实现 _sediment_verdict；触发挂在 complete_task/fail_task 工具内；产出=提案草稿（提案制人工审批）；campaign 写 workspaces/campaign.db 全局库（不绑 project_id）。
- **重启恢复链**：优雅停机钩子落快照 → 项目首次打开惰性清扫（孤儿 claimed 一律 fail(result_note='后端重启，任务中断')，持 resume 快照的会话豁免）→ 显式 resume 端点（仅 failed 任务 / paused 会话）。
- **自动消耗链**（idle 警报真凶）：_maybe_auto_resume（C4 budget +200）/ _maybe_auto_tick（L2 链式编排 tick）/ _maybe_mission_auto_tick + 60s 轮询（L1 auto_derive）/ _maybe_orch_wake（异常订阅唤醒）/ _schedule 启动段武装待命窗。

## 3. 定稿决策（用户拍板 2026-09-22）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 验证纪律 | 17 条逐条验证后立项；不成立条目（900s 上限/选框重叠）不进设计，只作澄清 |
| 2 | 范围 | 六簇全收（A-F）；优先级推荐 **M1 稳定性修复包 → M2 只读工具面 → M3 覆盖度平台化**（编排器自估砍约 1/3 任务量与 token，并消除「验证不了执行者结论」治理隐患）→ M4-M6 排期待定 |
| 3 | 跨方案合并 | C2 重启自动复活并入 [independent-verification-audit.md](independent-verification-audit.md) M3 排期（同源快照交棒）；F1 与已归档 experience-sedimentation 的条件判定衔接（扩档不推翻） |
| 4 | 宁严勿松红线 | 授权升级提案**恒 human 决策**（L2 也不自动批）；编排器只读工具零写权；cancel/requeue 走自主档分流 |

## 4. 设计详述

### 4.1 簇 A：编排器只读工具面 + 回执完整性（最痛件）

- **`ORCH_QUERY_TOOLS` 四工具**（orchestrator.py 新增，_dispatch 白名单扩展）：
  - `task_detail(task_id)`：全量 objective/context（reconcile、plan、attempts）/result_note 全文/预算消耗——从任务行直读；
  - `bb_overview(section?)`：资产终态覆盖（对接簇 B coverage_report）/findings 分级统计与最新 N 条全标题/事件尾部；
  - `budget_status`：token/步数/任务窗预算余量（gate 同数据源，补齐「余量被动等 80% 警告」缺口）；
  - `session_list`：live 窗清单（状态/绑定任务/最后活动/步数）。
- **零写权红线**：只读 bb 查询方法，dispatch 层不注册任何写工具；编排器 persona 提示注明「查询用于核对与决策，不用于替代派单执行」。
- **态势截断放宽（A2）**：recent_closed result_note [:100]→[:300]、事件行 payload [:120]→[:300]——有 task_detail 全文兜底后态势仍保持摘要定位（渐进披露，照抄 Agent 侧 bb_query 思想）。
- **验收**：编排器能独立核对执行者结论，「发个任务帮我查一下」类任务归零。

### 4.2 簇 B：覆盖度对账平台化

- **`core/coverage.py`（新模块，地位仿 phases.py）**：
  - `asset_terminal_state(asset, finding_index)` → `terminal(tested_clean|dead_end|finding_attached) | visited | open`——终态 = meta 标记 ∪ findings 反查（target_asset_id 挂链）；
  - **子资产状态传播**：url/service→host→domain 上行归组，子资产全部终态且自身无未测面 → 父组收敛；
  - `coverage_report(bb, pid)` → `{by_group: [{group_key(domain/host), total, terminal, visited, open, uncovered[]}], overall}`。
- **态势注入（B2）**：_assets_view 并入 coverage_report 摘要（每站点一行 + uncovered top N）——「全景对账」类数数任务归零。
- **阶段信息单一事实源（B3，修矛盾窗口）**：_phase_gate_check 判定后把 `{phase, passed, unmet[]}` 写 project meta（`phase_gate_state`），_phase_section 注入**只读该状态不重算**——注入与动作同源，idle 更新时序不再制造矛盾；gate_metrics 保持唯一计算函数。
- **顺带收口**：Agent 侧终态标记纪律（tested_clean/dead_end 写 meta）进执行窗收尾提示——平台判据有了，喂料纪律同步补。

### 4.3 簇 C：生命周期补全

- **编排器 `cancel_task(task_id, reason)` / `requeue_task(task_id)`**：
  - cancel：open/claimed→failed（blocked_reason='cancelled'，claimed 场景复用人工中断链路打断在跑窗）；requeue：failed/awaiting_human→open（清 claimed_by，attempts 履历保留）；
  - 自主档分流照过门先例：L0 记 orch.proposed / L1 审批卡 / L2 直接执行；
  - 被预算泡掉的任务（如 wpn WebVPN 案例）从「记简报下轮手动重发」变为结构化 requeue。
- **C2 重启自动复活**（并入 independent-verification-audit M3 排期）：_sweep_restarted_project 的 fail_interrupted_claims 加分支——持 task-<tid>.resume.json 的孤儿任务不 fail，进待复活队列；项目配置 `auto_revive_on_restart` 缺省 off（宁严勿松）。
- **C3 小件**：派单模板/expert persona 补「长探测命令显式传 timeout（默认 120s）」提示。

### 4.4 簇 D：findings 去重强化 + 授权升级提案

- **dedup_key 强化（D1）**：add_finding 写入前若同 target_asset_id+vuln_class 已有行但 dedup_key 不同 → 返回文案附「[疑似重复] 已有同类 finding <id>: <title>」警告（**不阻塞登记**，Agent 可坚持新增）；finding.merged 事件经 A2 放宽后编排器态势自然可见。
- **授权升级提案（D2）**：扩 request_escalation 受理类型，新增 `authorization` 类审批单——结构化 payload `{scope_request, justification, evidence_finding_ids}`，语义三档（请求扩大授权范围 / 请求影响证明升级 / 请求突破 rating 口径）；**恒 human 决策**（L2 不自动批，宁严勿松红线）；「打不上去」从默默死路记账变为显式暴露给人类。

### 4.5 簇 E：稳定性修复包（小件快跑，半天级）

- **E1 spill 唯一后缀**：retention.py spill_text 命名加 uuid4().hex[:6]（同工具同秒不再互覆）；
- **E2 拒绝熔断**：loop.py 工具结果回填处检测拒绝类前缀（[计划闸]/[越界拒绝]/[网关拒绝]/[拒绝]）连续 ≥3 次 → 停轮转 awaiting_human（拒绝循环是行为错误，走 awaiting_human 非 budget paused）；
- **E3 thinking.delta 收敛**：终稿到达时前端把对应 delta 行收拢为一行「💭 已思考 N 字」defaultOpen:false（修 prune 不广播 WS 的滞留）；OrchChatPane 编排器视图思考流默认压缩；
- **E4 回执三路径修复**：①父未认领→回执挂父任务行 meta.pending_receipts，父被认领时补投 + 事件兜底；②自收抑制→落 task.receipt_self 事件替代静默跳过；③reopen 场景 ref_id 加状态后缀（task_id:status）保证成功回执必达；
- **E5**：「选框重叠」不成立不修（§1 澄清记录）。

### 4.6 簇 F：记忆沉淀放宽

- **触发扩档（F1，衔接 experience-sedimentation 条件判定）**：
  - **死路记账档**：任务产出【死路记账】类 finding → campaign 写入（content 取死路原因）——负知识跨项目复用是渗透测试记忆的核心价值；
  - **高质量复盘档**：done 无产出但 result_note 长度 ≥500 → 轻量提炼（每会话上限 1 条防刷量）；
  - 提炼仍走提案制（人工审批进 kb），campaign 写入条件放宽≠归纳自动化。

## 5. 待打磨清单

1. ~~A1 四工具的返回体量上限~~（✅ 已裁定 §0-4：选定键 json.dumps，暂不做分段拉取）；
2. ~~B1 子资产传播的边界语义（url 全部终态但 host 本体未测 → host 组算收敛吗）；group_key 归组口径（domain 为主 / host 兜底）与 _target_load 的 target_keys_of 对齐~~（✅ 已裁定 §0-6：父借子收敛须自身 visited 佐证；group_key 沿父链到根 domain 为主 host 兜底）；
3. ~~B3 phase_gate_state 写入时机与 enter_phase 抵达校准的联动（流转新阶段时重置）~~（✅ 已裁定 §0-7：每轮评估后写、enter_phase 顺写新阶段状态零空窗）；
4. ~~C1 cancel 打断在跑窗与人工中断链路的复用边界（编排器发起 vs 人工发起的事件 author 区分）~~（✅ 已裁定 §0-8：request_abort 复用+窗不关；by 字段区分发起方）；
5. ~~D1 疑似重复警告的文案与频控（每会话提醒上限）~~（✅ 已裁定 §0-9：警告只进工具返回值无频控——「每会话上限」前提不成立）；
6. ~~D2 authorization 审批单与 mission/ROE 展示的联动（审批卡里人类需要看到当前边界全文）~~（✅ 已裁定 §0-10：approvals 出口附 server 拼好的 boundary 字段，前端存在才渲染）；
7. ~~E2 拒绝前缀清单的完备性~~（✅ 已落地 §0-2：tools.py 统一常量 REJECT_PREFIXES）；
8. ~~E4 pending_receipts 挂任务行的 schema 变更评估~~（✅ 已裁定 §0-3：tasks.context JSON 内嵌，零 schema 变更）；
9. ~~F1 死路记账 finding 的识别口径（title 前缀【死路记账】 vs category 字段——建议落 category 结构化）~~（✅ 已裁定 §0-11：**推翻 category 建议**——status='false-positive' 本就是结构化死路口径，零新约定）。

## 6. 实施切分建议（M1-M6 全量已完成 2026-09-22/23）

| 里程碑 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| M1 稳定性修复包 | E1 spill + E2 熔断 + E3 delta 收敛 + E4 回执三路径（+搭车 429 传输语义） | 无 | ✅ 已实施 |
| M2 编排器只读工具面 | A1 四工具 + A2 态势放宽 | 无 | ✅ 已实施 |
| M3 覆盖度平台化 | B1 coverage.py + B2 态势注入 + B3 阶段单一事实源 | B2 依赖 B1 | ✅ 已实施 |
| M4 生命周期 | C1 cancel/requeue + C3 timeout 提示；C2 并入 iv-audit M3 | C1 独立 | ✅ 已实施 |
| M5 去重+授权提案 | D1 疑似重复警告（§0-9）+ D2 authorization 审批（§0-10，含 escalation rejected 回流搭车） | 独立 | ✅ 已实施（2026-09-23） |
| M6 记忆沉淀放宽 | F1 两档扩条件（§0-11：FP=死路口径 + campaign tags + 复盘档） | 独立（衔接已归档 sedimentation） | ✅ 已实施（2026-09-23） |

测试面：test_orchestrator（四工具只读性/cancel-requeue 分流/态势放宽/campaign 注入分组）、test_coverage（终态判定/传播/分组）、test_tasks（回执三路径）、test_agent（拒绝熔断/sediment 死路档与复盘档/上限/request_authorization）、test_blackboard（add_finding dedup_warning）、test_retention（spill 并发同名）、test_api（decide rejected 回流/approvals boundary 出口）、前端 build 零 TS 错误。
