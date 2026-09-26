# 方案：独立验证与审计闭环（收口判定独立化 + Auditor 第三角色）

- **状态**：**M1 已实施（2026-09-23），M2-M4 定稿待排期**（2026-09-22 方向拍板：①验证器+②审计员合并本方案，③交棒+⑤转向并入，④评测基线并入 retrieval-upgrade；不对标 VulnHouse 平台业务；M1 实施记录见 §0）
- **拍板记录**：§3
- **思想来源**：斗象科技《100 小时长征：史上首个 Agent 自主不间断渗透完整战报》（2026-09-07 挑战；完整报告 https://m-wiki.freebuf.com/article-detail?id=230327 ）——两件工程核心被本项目直接吸收：Sonic Harness 的 MEA 三角（Manager/Executor/**Auditor** + 每轮全新上下文 + Harness 唯一状态写入方）与 VulnHouse 验证引擎 VE（四种验证策略 + 独立判定 + 证据链 + **flag 对 Agent 脱敏防反套**）。核心命题：*题目做对了没有，不是 Agent 自己说了算的*。
- **关联代码**：`core/blackboard/tasks.py`（:132 `_initial_context` acceptance→`reconcile=[{id,text,state,note}]`、:477 `_check_reconcile` done 硬闸、:508 `set_reconcile_state` met/failed/blocked、:588 attempts 履历 cap 20）、`core/agent/tools.py`（:577 `task_reconcile` 工具）、`core/api/app.py`（:3690 `_maybe_auto_resume` C4 同窗+200、:5170 虚拟单例 orchestrator 实现先例）、`core/agent/loop.py`（:179-215 快照三件套 `<sid>.json`/`task-<tid>.json`/`task-<tid>.resume.json`、E8 max_steps）、`core/runtime/gateway.py`（run(cmd, runtime)）、`core/skills/proposals.py`（L0 分流）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§六 提案制治理 / 任务收口相关节），同步 `core/blackboard/`、`core/agent/`、`core/api/` CLAUDE.md；本文保留作方案背景

## 0. 实施记录（M1 独立验证器，2026-09-23）

- **acceptance Union 升级**：`str | {text, verify}` 混排（`AcceptanceIn` pydantic 模型 + `_initial_context` 归一化，纯字符串条目零迁移）；verify 规格发布期经 `validate_verify_spec` 校验（未知键/缺键/坏正则 ValueError→422，fail-fast 不入脏规格）。
- **`core/verify.py` 新模块**（地位仿 phases.py）：四策略 `evaluate` 全实现——flag_capture（file: 工作区相对路径防穿越 / cmd 二选一，exact/contains/regex 三匹配）、effect_proof（checks 清单逐项，失败点名第几项）、poc_crash（signal 退出码启发〔负值/0xC0000005/0x80000003/134/139，超时不算崩溃〕/ asan 特征扫 stdout+stderr）、oracle（输出抠 JSON `{pass, detail}`，非法输出 fail-closed）；规格问题/网关拒绝/文件越界一律 fail-closed 判未过，绝不向 Agent 回显敏感内容。
- **执行面**：验证器平台身份 trusted + host + 工作区隔离（workspace=黑板 db 父目录），经 `ExecutionGateway.run` 全量策略校验（command/command.result 审计事件照落）；gateway 惰性 import 防环（gateway 顶层依赖 blackboard.store）。
- **权力红线**：`set_reconcile_state` 加 `as_verifier=False` 参——verify 条目 met/failed 非 verifier 写入直接 ValueError（工具回 [拒绝]），blocked（附不适用理由）仍开放；verifier 写入条目标 `by:"verifier"`。
- **收尾钩子**：`TaskQueue.complete` 先跑 `run_reconcile_verifications`（pending **与 failed** 都重跑=「修正后重新 complete 重验」语义，blocked 跳过）→ 未过条目 ValueError 拦下本次 complete（与 reconcile_blocked 同口径）；逐条落 `verify.result` 事件（author=verifier，payload 带脱敏 evidence_head）。
- **脱敏红线（照抄 VE）**：evidence_head 恒为结构化摘要（匹配结论 + 输出长度 + sha256 前 12），输出原文/预期值/oracle detail 任何情况不回显——工具返回与事件 payload 同源同规。
- **前端**：events.tsx `verify.result`「🔬 独立验证通过/未过」（未过默认展开）+ 摘要 `[策略] 条目#N · ✅/❌ · 摘要`；TaskCard ☑ 徽章 title 标 🔬 条目与红线说明；LiveRoom「决策」筛选收录。
- **测试**：tests/test_verify.py 15 个（规格校验/四策略/防穿越/脱敏断言/钩子拦断与重验/平台身份断言/红线拒绝/混排发布）+ test_api.py 1 个（acceptance Union 422/入库）；全量 928 passed。
- **M1 拍板记录**（实施时定，原文待打磨 #1/#2/#5）：①重试语义=complete 触发、未过拦断、Agent 修正后再 complete 自动重跑（不自动重派不进失败复盘——任务未完成，verify.result 事件即审计痕）；②派单入口 M1=API body（编排器自动生成规格后置）；③verifier 走网关**全量策略校验**（宁严勿松，不做白名单直通）。file: 路径语义收窄为**工作区相对路径**（方案原文 file:/flag.txt 的绝对路径形态在宿主执行模型下即穿越，防穿越红线优先）。



## 1. 背景与问题

- **收口自报信任缺口（主问题）**：验收条目的 met/failed 由 Agent 经 `task_reconcile` 自行勾选，done 硬闸只查「有没有收口」不查「收口是否为真」——Agent 虚报 met 即可通过。verified 门禁同理主体是自报证据。宁严勿松原则下这是当前最大的结构性信任缺口。
- **长程任务上下文腐坏**：G3 compact 是被动缓解；C4 的 L2 自动续跑是**同窗 +200 步**——膨胀的上下文继续膨胀。Sonic 的反面做法：Executor 每轮全新上下文，靠任务合约交接。
- **同路径空转**：C10 有 attempts 履历但无主动干预；JBoss 案例的价值在「评估成本及时放弃 + 6 次策略转向」，我们缺把失败教训喂给重派的机制。

**明确不做（防过度工程）**：MEA 全套重构（编排器=Manager、会话窗=Executor 已分离，只补 Auditor 三角）；VulnHouse 式靶场平台业务（要的是验证引擎思想，不是产品形态）；MEG 全新任务回合表（回合账本用现有事件+attempts 承载）。

## 2. 现状盘点（2026-09-22 核实）

- **reconcile 链路**：publish `acceptance: list[str]` → `_initial_context` 转 `context.reconcile=[{id,text,state,note}]`（state∈pending/met/failed/blocked）→ Agent `task_reconcile` 勾选 → complete 时 `_check_reconcile` 硬闸拦 pending（`task.reconcile_blocked` 事件）——**判定权在 Agent**，硬闸只对账分母；
- **C4 自动续跑**：L2 + budget paused → 同窗 `max_steps+200` 续跑（token 预算硬闸不覆盖）；非任务窗与任务窗无差别；
- **快照三件套现成**：会话 `<sid>.json` / 任务现场 `task-<tid>.json` / 跨窗复活凭证 `task-<tid>.resume.json`，C6 resumeTask 链路已通——交棒的全部底层已就绪，缺的只是自动触发；
- **attempts**：`_finish` 追加 `{outcome, result_note}` 履历（cap 20），`render_attempts_lines` 已有渲染器；C10 卡片 `↻N` 徽章已显；
- **虚拟单例先例**：`list_experts` 恒追加 `{id:"orchestrator", kind:"virtual"}`（运行时合成，非 yaml 文件）——Auditor 照抄此模式；
- **效果榜/轨迹**：execution-trace-chain 已实施，Auditor 的输入面（轨迹摘要、(skill×kb)×verified 统计）现算可得。

## 3. 定稿决策（用户拍板 2026-09-22）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 方案合并 | ①独立验证器 + ②Auditor 审计员合成本方案一体的治理闭环；③自动交棒、④卡住转向作为附属里程碑并入 |
| 2 | 任务级评测基线 | 并入 [retrieval-upgrade.md](retrieval-upgrade.md) 作 M5（黄金任务集 + 两口径评分卡），不在本方案重复 |
| 3 | 对标边界 | 不做靶场平台业务、不做 MEA 重构（§1 明确不做清单） |

## 4. 设计详述

### 4.1 M1 独立验证器（verify spec + 服务端判定）

- **acceptance 条目升级**：`list[str]` → `list[str | {text, verify?}]`（pydantic Union 向后兼容，纯字符串=现状零迁移）。verify 规格受约束 schema（仿 phases.yaml 精神，未知键拒绝）：

  ```yaml
  # flag_capture：读文件或执行检查命令取输出，三种匹配
  {strategy: flag_capture, src: "file:/flag.txt" | cmd: "...", match: exact|contains|regex, value: "..."}
  # effect_proof：检查命令 + 预期输出逐项比对（asset 状态变化的机械化）
  {strategy: effect_proof, checks: [{cmd: "...", expect: contains|regex|exit0, value?}]}
  # poc_crash：输入使目标崩溃（退出信号 / ASan 特征）；双容器差分后置
  {strategy: poc_crash, cmd: "...", detector: signal|asan}
  # oracle：自定义脚本输出 JSON {pass, detail}（业务逻辑/复杂链兜底）
  {strategy: oracle, cmd: "..."}
  ```

- **判定引擎 `core/verify.py`（新模块，地位仿 phases.py）**：`evaluate(spec, ctx) → {passed, evidence_head, duration_s}`；执行经网关（平台身份，runtime 继承任务/资产上下文，**net 缺省 none 宁严勿松**）；不 import orchestrator；写全部走 bb 现有写口（`set_reconcile_state` + `append_event`）；
- **权力划分（本方案的核心红线）**：**verify 条目的 met/failed 由验证器写**——Agent 对 verify 条目调 `task_reconcile` 置 met 直接拒绝（错误文案「该条由验证器判定」），可置 blocked（附 note 说明不适用）；挂点=任务收尾钩子（仿 sediment 钩子先例）在 `_check_reconcile` 之前跑未判定条目——**verify 失败 → state=failed → done 硬闸自然拦住，零新闸**；
- **脱敏红线（照抄 VE）**：判定回执（工具返回/事件 payload）只含 passed + 匹配摘要（长度/哈希前缀），flag/token 原文**不回显**——防 Agent 经验证接口反套答案；`value` 存任务行（黑板本可见）不受限，受限的是**接口响应**；
- **事件与前端**：新事件 `verify.result` {task_id, item_id, strategy, passed, evidence_head, duration_s}；TaskCard ☑ 徽章 title 区分「验证器判定」与「Agent 自报」；
- **发布入口**：M1 以 API/编排器为主（派单 body 带规格），前端结构化编辑器后置。

### 4.2 M2 Auditor 审计员（补齐三角）

- **形态**：**LLM 裁决调用，非完整 agent loop**（仿 planner/consultant 模式，无工具循环）；虚拟单例 `_auditor`（照抄 orchestrator 虚拟行实现，不进组队/管理多选）；
- **输入**：任务合约（objective + reconcile 全表 + verify 结果）+ result_note + 证据锚点 + 轨迹摘要（trace 现算）——只读，零写工具；
- **输出**：结构化裁决 `{verdict: confirm|reject|escalate, reasons[], risk_notes}` → 事件 `audit.verdict`；
- **权力边界：建议权不裁决权**（对齐 L0 精神、人类最终裁决）——confirm 挂「已复核」标（verified 自动置位是否开放待打磨 #3）；reject → 任务回 open + `blocked_reason=audit_rejected` + 裁决理由注入重派（喂 M4）；escalate → awaiting_human；
- **审计面三档** off/sample/full（项目配置可调）：拟议缺省 pentest/redteam=sample（触发：attempts≥2、产出 verified finding、verify 有 failed→met 翻案）、ctf=off（flag 判定够硬）——成本与宁严的平衡待打磨；
- **挂点**：任务 done 后 sediment 复盘链附近（同批钩子位），API 侧兜底触发。

### 4.3 M3 步数耗尽自动交棒（回合换防，轻量版回合制）

- **改造 C4 分流**：**任务绑定窗**（bound_task_id 非空且任务未终态）步数耗尽 → **快照交棒**替代同窗+200：现有快照落盘 → 当前窗收尾 → 开新窗载 `task-<tid>.resume.json` 续跑同任务（C6 链路自动化）→ 事件 `session.handoff` {task_id, from_sid, to_sid}；**非任务窗维持 C4 +200 不动**；
- **回合账本零新表**：轮次记 `meta.rounds`，交棒不计 attempts（attempts=失败履历语义不变，待打磨 #4）；
- **自主档分流**（复用过门 L0/L1/L2 分流先例）：L2 自动交棒；L1 审批卡；L0 提案；
- **上限宁严勿松**：同任务 rounds≥3 → 停自动回 awaiting_human。

### 4.4 M4 卡住转向（重派注入失败教训）

- **两小件**：①failed 任务重派（人工续跑/编排器重发）时上下文自动注入「前情提要」——`render_attempts_lines` 渲染历次 outcome/result_note + sedimentation 失败复盘提炼的 kb 教训引用（**经验复用闭环**：失败→复盘沉淀→重派命中）；②orchestrator 监控段加 stuck 告警：attempts≥N 的任务进 tick 态势 stuck 段，促发换路派生（换专家/降级拆解/campaign 召回同型历史成功打法）；
- 数据源全现成：ctx.attempts + trace-effect + campaign.recall，零新采集。

## 5. 待打磨清单

1. M1 verify 失败的重试语义（complete 重触发 vs Agent 请求重跑 vs 自动重派；与 attempts/复盘口径的联动——verify_failed 是否入 sedimentation 复盘）；
2. M1 verify spec 派单入口（编排器自动生成规格的责任面与模板；CTF flag value 的来源与录入）；
3. M2 缺省审计面档位与触发条件细化；confirm 是否允许自动置 verified；
4. M3 交棒 rounds 上限取值；L1 审批卡形态（复用 spawn_session 卡 vs 新 op）；
5. M1 verifier 平台身份经网关的 policy 面（verifier 命令走策略校验全量 vs 白名单直通）；
6. poc_crash 双容器差分（vul 崩 fix 不崩）的实现依托（L3 沙箱实例编排），后置；
7. 三个新事件 kind（verify.result / audit.verdict / session.handoff）的 FILTERS 收录与前端徽章细化。

## 6. 实施切分建议（待用户排期）

- **M1 验证器**：acceptance Union 升级 + core/verify.py 四策略 + 收尾钩子接线 + 脱敏 + 事件——最大件，向后兼容独立可上；
- **M2 Auditor**：虚拟单例 + 裁决 prompt + 三档审计面 + audit.verdict——依赖 M1 的结构化判定输入更佳，不强依赖；
- **M3 交棒**：C4 分流改造 + 快照自动续跑 + 自主档分流——独立；
- **M4 注入**：重派前情注入 + orchestrator stuck 段——最小件，独立。

依赖关系：M1→M2 松耦合（输入面质量）；M3/M4 独立可并行；四者共享「收口判定独立化」一条主线，建议 M1 先行立骨架。
