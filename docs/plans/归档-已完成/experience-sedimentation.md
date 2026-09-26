# 方案：经验沉淀减产提纯（campaign 与复盘必要性审计）

- **状态**：**已实施**（M1+M2+M3 全量，2026-09-22；M4 批量归纳=后置观察，明确不实施，见 §0）
- **拍板记录**：§3
- **实施记录**：§0（实施修正注记与定稿口径）
- **审计来源**：2026-09-22 逐步骤必要性审计——结论：**无效消息不是漏进来的，是上游两个「宁滥勿缺」全量步骤生产的**（campaign 每任务必写 + 复盘每次 done 必跑）；修复 = 上游减产，不是下游加闸（否掉首轮商议的「四层闸门」方向中的试用期制度，人审效率降为顺带）
- **关联代码**：`core/agent/loop.py`（campaign_hook :2126 / sediment_hook :2159 / 收尾触发 :1724-1732）、`core/agent/tools.py`（bb_propose / PROPOSE_LIMIT_PER_SESSION）、`core/blackboard/campaign.py`（add :75 / recall :100 整词+usage_count 马太）、`core/skills/proposals.py`（create/apply 人审管道、mode 权限矩阵）、`core/skills/kbindex.py`（kb_module_hints 复用给复盘对账）、`core/skills/doctor.py`（K7 zero-hit 统计）、效果榜（trace-effect 已实施）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§六 提案制治理段 / §四 战役记忆段），同步 `core/agent/CLAUDE.md`；本文保留作方案背景

## 0. 实施记录（2026-09-22，M1+M2+M3 全量）

1. **M4 批量归纳（乙）明确不实施**：后置观察项——M1 条件写入上线后 campaign 增速降一个量级，同类聚集达到触发密度再重议（拍板原意即「先观察再定」）。
2. **verified 判定口径（待打磨 #1 定稿）**：任务窗事件区间口径——`traces._session_task_windows`（(lo,hi] 区间）+ `session_id=author` 双约束，`AgentSession._task_finding_ids` 取本任务最近 closed 窗口内 finding.new/finding.merged 事件的 `$.finding_id`；exploited 链兜底=`chains.origin='trace' AND status='exploited' AND chain_links.node_id LIKE '{task_id}#%'`。判定失败按无产出处理（宁少勿滥）。
3. **判定单一实现**：`loop.py _sediment_verdict` 返回 `{campaign, review, finding_ids}`，`_campaign_memory` 与 `_sediment_proposal` 共用（§4.1 口径表逐行落码）。
4. **aborted 档三调用点**：loop.py `_abort_current_task`（人工中断）/ `_finalize`（关窗未收尾）/ `_fail_task_on_error`（worker 异常兜底）统一改传 `blocked_reason="aborted"`；Agent 主动 fail_task 工具恒 `error`（真失败唯一入口）。
5. **证据锚点双层校验（待打磨 #2 定稿）**：①解析后 loop.py 机器校验——done 提案 reason 须含本任务真实 finding id（防 LLM 编造/漏引用，缺失静默跳过）；②`proposals._validate_agent_kb`——agent origin + kb create 限定（human 复核放宽、edit/revise/apply 不拦），reason 须含 find-/task- 证据锚点 + content 须有「## 」段且至少一段标题命中沉淀口径关键词（坑|注意|已验证|路径|步骤|适用|流程|复现|前提）。
6. **丁结构体检口径（待打磨 #9 定稿，现网数据定标）**：doctor 回扫只报「>800 字符且标题行（#/##/###…）<2」的单段长文——H1 分节（payloader playbook 导入风格）/H2/H3 任一分节形态都算有结构；refs/ 子树豁免（上游快照原文不动约定）。现网 635 个长文档命中 2（避免按「无 H2 或标题不命中关键词」初版口径扫出 530 假阳性）。沉淀口径段要求只约束 agent 新建侧。
7. **甲负向反馈（待打磨 #7 简化落地）**：`traces.kb_module_feedback` 三象限纯只读聚合（opened=kb.open 全历史 / positive=有 verified 发现的轨迹链内 kb〔链级去重，effect_stats 同源〕/ negative=真失败任务窗内 kb.open 弱信号）；`app.py _kb_cleanup_issues` 跨项目聚合：positive=0 且（opened≥3 或 negative≥2）报 info `kb-cleanup-candidate`，negative≥2 标优先复核——只呈现不自动处置，处置走提案制。
8. **实施期修 bug**：tools.py `_tool_fail_task` 的 sediment hook 触发点 `tid` 未绑定（收尾 lambda 的参数不进外层作用域）→ NameError 被 try/except 吞掉、失败复盘从未触发——补 `tid = self.current_task_id`，test_sediment_failure_review_on_fail_task 护栏钉死。
9. **recall 马太修正**：`score += decay * log1p(usage_count) * 1.5`（原 `min(usage,10)*0.5` 恒霸榜）；log 增长自然趋缓（热 10 次≈+5.9 vs 冷 1 次≈+1.1），新验证打法可上位。
10. **测试**：test_agent 沉淀组重写（条件判定/失败复盘/懒 planner 替身 `_LazySedimentPlanner` 读真实 finding id）+ 新增 aborted/无产出不触发、kb 提案机械质检、doctor kb-structure-thin、kb_module_feedback 三象限、campaign 无产出不写；全量 824 passed。

## 1. 背景与审计结论

用户担忧：经验沉淀会被无效消息堆满、入库经验不够精纯。

逐步骤必要性审计（2026-09-22）结论：

- **不必要（砍）**：①campaign 全量必写——任务分「常规操作」（每次都一样，无可沉淀）与「非常规路径」（才值得跨项目记），且 events 表本就是任务日志，campaign 对它的唯一增量价值是跨项目召回，覆盖任务占比很低；②复盘全量跑——常规任务产出 NONE 或低质提案，成本不只是 LLM 调用还有人审时间；③campaign content 的 findings 标题拼接——结构性冗余（黑板可查）；
- **必要不可动（安全基石）**：提案制人审管道（apply 只认 decided_by=human / live_diff / mode 权限矩阵）、campaign.memory 事件可见性、每会话 3 条上限（角色从主闸变兜底）、NONE 出口（从主力过滤变兜底）、度量三件套（skill.routed / 效果榜 / K7）；
- **角色重定位**：自主提案（bb_propose）从「可选」变**条件过滤的逃生口**——自动通道收紧后，干活中发现的沉淀随时可提，不受条件限制。

## 2. 现状盘点（2026-09-22 核实）

- **campaign 写入**：收尾 `result.startswith("task=")` 全量触发（成功失败都写、无成败标注）；content = result_note[:300] + 最近 5 条 finding 标题拼接（**拼接不是提炼**）；无淘汰机制；recall 的 usage_count×0.5 马太效应（早期条目霸榜）；
- **复盘**：每次 done 必跑 planner LLM（近 30 事件摘要、payload 截 120 字）→ 三类去向（kb 打法 / index 测试点 / case K6 沉淀）提案草稿；prompt 无 kb 对账（不知道已有手册，盲写重复）；reason 为 LLM 自报证据（无机器校验）；
- **K7 zero-hit**：有统计无消费——没有接清理动作，只出不进的环。

## 3. 定稿决策（用户拍板 2026-09-22）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 治理路线 | **上游减产优先**（非下游加闸）：条件写入 + 条件触发，预计砍 ~80% 无效产出，下游一切压力骤减 |
| 2 | campaign 定位 | **收窄为「成功打法索引层」**：只存值得跨项目记的任务；打法精炼在 kb（复盘提案），campaign 召回给编排器的信号是「历史有类似任务且产出过 verified 发现」，细节按需深查 kb/黑板 |
| 3 | 失败教训去向 | **campaign 只收 success；失败走复盘提案沉淀进 kb 手册「坑」段**——失败表述成本高且「别这么打」更适合手册形态，分工干净 |
| 4 | 自主提案 | 不受条件限制（逃生口），3 条/会话上限保留 |
| 5 | 试用期制度（首轮方向丁） | **后置不做**——源头减产后垃圾量降一个量级，必要性重估 |
| 6 | 人审效率（首轮方向丙） | 降为顺带：pending 老化标记可选轻做 |
| 7 | 「失败」细分（2026-09-22 用户提出） | failed 终态五来源甄别，扩 `blocked_reason` 加 `aborted` 档：人工中断/关窗中断/worker 异常统一标 aborted 不跑复盘，**真失败=blocked_reason='error'**（见 §4.1） |
| 8 | 业界借鉴吸收（2026-09-22 调查五谱系后拍板：全收） | **甲** 复用效果负向反馈（Memp Update 环）+ **乙** 批量归纳触发（AWM 雪球）+ **丁** kb 结构机械体检（nuclei 质检门）全收；**丙** 召回注入精炼已在 §4.2 不另做；不追 EvoHunt 全自动 playbook 自进化（无人在环=记忆投毒直接入账）与 TermiAgent 逐步记忆激活（黑板+事件溯源架构无此病）——见 §4.5 |

## 4. 设计详述

### 4.1 条件口径（核心，一处判定两通道共用）

**「失败」不是同质类——五来源甄别（2026-09-22 核实）**：failed 终态下埋着人工中断（loop.py:2032）/ 关窗中断（`_finalize` :2478）/ worker 异常兜底（:2464）/ Agent 主动 fail_task / awaiting_human 五条路径，前三条现全标默认 `blocked_reason='error'` 但性质是「人的决定 / 环境意外 / 平台异常」，都不是有教训可提的真失败；重启与僵尸租约路径回收为 open 不进 failed，天然不在判定范围。

**落点：扩 `blocked_reason` 枚举 `{error, awaiting_human}` → 加一档 `aborted`**（C1 结构化失败原因字段的自然演进，列已存在零 schema 改动）：

- 人工中断 / 关窗中断 / worker 异常三条路径统一改传 `blocked_reason="aborted"`（fail() 白名单 + 三调用点传参）；
- 判定只发生在任务收尾时刻（hooks），存量 failed 行不回扫，无兼容负担；
- **真失败 = `blocked_reason='error'`（Agent 主动 fail_task 且自填归因）**——唯一进复盘的失败类。

任务收尾时机读判定（verified 判定实现口径：本会话 author 的 findings 中存在 `status='verified'`，或本项目存在含本任务产出的 exploited 链——细节待打磨 #1）：

| 任务形态 | campaign 写入 | 复盘触发 |
|---|---|---|
| done + 产出 verified finding / exploited 链 | ✅ 写 | ✅ 跑（沉淀打法） |
| failed + `blocked_reason='error'`（真失败） | ❌ 不写 | ✅ 跑（**只提炼可复用避坑教训**，prompt 约束「环境/临时性问题输出 NONE」） |
| failed + `aborted`（人工中断/关窗/worker 异常） | ❌ 不写 | ❌ 不跑（无教训：人的决定 / 会放回重跑 / 平台环境） |
| failed + `awaiting_human`（等输入） | ❌ 不写 | ❌ 不跑（不是失败；「已解决放回继续」后续跑有真终态再判） |
| done 无产出（常规操作） | ❌ 不写 | ❌ 不跑 |

- 逃生口：Agent 自主提案随时可用（拍板 #4）；
- 判定函数单一实现，campaign_hook 与 sediment_hook 共用（触发点 loop.py:1724-1732 两分支同判）。

### 4.2 campaign 条目提纯

- **content 瘦身**：砍 findings 标题拼接——content = result_note（条件写入后这些任务的收尾总结质量天然较高）；findings 冗余由 recall 注入侧补元信息（「该项目产出 verified N 个」），不随条目存储；
- **schema 不加 outcome 列**（YAGNI）：条件写入后 campaign 语义恒为成功打法；失败教训按拍板 #3 走 kb；
- **recall 马太修正**：usage_count 权重 ×0.5 改 log 衰减（一行级改动），时间衰减维持现状；
- **自动淘汰暂缓**：条件写入后增速降一个量级，观察数据量再定；存量条目人工处置（待打磨 #4）。

### 4.3 复盘提案提纯

- **prompt 升级**（`_sediment_proposal` instruction）：
  1. **kb 对账前置**：注入「kb 既有相关模块清单」（复用 `kb_module_hints(packs_root, capabilities, 任务 objective)` 现成能力）——已有手册的 → 引导走 edit 修订补段，而非 create 新建；
  2. 失败任务模式（4.1 表）：只提炼避坑教训 + 环境/临时性问题输出 NONE；
  3. 证据引用要求：reason 必须含本任务产出的 finding id（结构化引用，可机器校验）。
- **proposals 校验增强**：kb 打法类提案 reason 强制含 finding 引用（`find-` 前缀正则校验，非法 422 拒收打回）；case/index 提案不强制（case 走链引用、index 是导航条目）——待打磨 #2；
- 每会话 3 条上限 / NONE 出口 / 人审管道全部保留不动。

### 4.4 退场闭环（轻做）

- **doctor 加「待清理建议」段**，两类来源汇总：①K7 zero-hit 条目（长期注入过从未命中）；②kb 模块级零贡献（长期无 verified finding 关联，数据源=效果榜同款统计反向侧）；
- 清单**只呈现不自动处置**——人审提提案（降权/归档/删除走提案制），与 K7 闭环打通最后一公里。

### 4.5 业界借鉴落地（2026-09-22 调查五谱系，拍板 #8 全收）

**甲、复用效果负向反馈（Memp 的 Update/Correct 环——退场闭环缺的最后一格）**

- 现状缺口：条目被召回复用后只有正向统计（效果榜只记 verified 产出），**复用失败无反馈**——错误/过时手册可一直被注入误导且零信号；
- 落地：效果榜扩展**负向侧**——kb 模块被打开（或 campaign 条目被召回）且该任务最终真失败（`blocked_reason='error'`）计入负向计数，数据全现成（skill.routed / kb_open 事件 + 任务终态），纯聚合查询；
- **与 retrieval-upgrade M3 三象限对账同源联动**：retrieval-upgrade 的三象限是「提示质量」对账，本项是「知识条目质量」对账——共用同一数据源（skill.routed / kb_open / 任务终态），口径统一为三分类（missed / 正常试错〔打开没产出不算〕/ 可疑误导〔打开且真失败〕），两处消费各自出报告；
- doctor 待清理判据随之升级：**正向零贡献 + 负向多次 = 优先清理**（弱信号定位，不自动处置只呈现人工判断——「打开且失败」≠「手册误导」，试错正常，防过度归因）。

**乙、批量归纳触发（AWM 雪球效应——从逐条堆积到归纳沉淀）**

- 现状：复盘单任务级逐条提提案逐条人审；AWM 核心是「多条经验归纳成一条通用沉淀」；
- 落地：同 capability+task_type 的成功 campaign 条目攒 ≥N 条 → 出一条**归纳型 create 提案**（N 条单任务经验合并成一篇 kb 手册章节），人审照常；
- 实施顺序后置于 M1：条件写入后 campaign 只收 verified/exploited 任务，先观察同类聚集频率再定 N 与触发者（复盘员顺带检查 vs 独立跑批）——待打磨 #8。

**丁、kb 结构机械体检（nuclei-templates 质检门——人审减负层）**

- nuclei 12000 模板靠 checksum+lint+三阶段 PR 校验撑质量；我们人审闸门之外加一层机械 lint；
- 落地：doctor 体检段扩展 kb 手册结构规范校验（适用条件/步骤/坑段 H2 齐全、frontmatter 完整），提案入库前不合格打回——机械问题机器拦，人审专注内容质量；
- 与 M3 doctor 改造同批做；规范细则待打磨 #9。

**不追清单（安全场景理由，防军备竞赛）**：EvoHunt 式全自动 playbook 自进化（reviser 直写无人在环，记忆投毒直入账——我们的人审闸门正是业界 memory poisoning 研究印证的正确设计）；TermiAgent Located Memory Activation 逐步激活（单 agent 长上下文遗忘的解法，黑板事件溯源可回溯，无此病）；Big Enough 反例的期待校准——campaign/记忆召回杠杆上限有限，规划与任务分解才是长周期任务瓶颈，本方案定位是「经验不腐烂」而非「成功率银弹」。

## 5. 待打磨清单

1. verified 判定实现口径：author=session_id 的 findings 查询（实现简单，拟议）vs 任务窗事件区间（trace 精确但重）；
2. evidence_refs 强制范围：kb 打法类强制、case/index 不强制（拟议）——校验形态（正则 vs 结构化字段）；
3. 复盘 prompt 的 kb 对账注入细节（hints cap、与既有 instruction 的合并格式）；
4. 存量 campaign 数据处置（保留观察 / 一次性人工清理）；
5. pending 老化标记的 N 天与呈现位置（若做，顺带项）。
6. `aborted` 的看板呈现：`awaiting_human` 已有「待人工」徽章，aborted 出不出「已中断」灰章（不做出也不误导——result_note 固定文本已可读）。
7. 甲负向统计口径细则：计数窗口与衰减（近期失败权重高于陈年失败？）、负向计数呈现粒度（kb 模块级 / campaign 条目级）。
8. 乙归纳阈值 N 与触发者（复盘员收尾顺带检查 vs 编排器 digest 时机 vs 独立跑批）、归纳型提案的 prompt 形态——M1 上线观察数据后再定稿。
9. 丁 kb 结构规范细则：哪些 H2 段强制（适用条件/步骤/坑）、frontmatter 必填字段清单、存量手册是否回扫体检。

## 6. 实施切分（已按此落地，见 §0）

- **M1 减产（2026-09-22 ✅）**：`blocked_reason` 扩 `aborted` 档（fail() 白名单 + 中断三调用点传参）+ 条件判定函数 + campaign 条件写入 + 复盘条件触发（loop.py 收尾两分支同判）。
- **M2 提纯（2026-09-22 ✅）**：复盘 prompt 升级（kb 对账 / 失败模式 / 证据引用）+ proposals 校验增强 + recall log 衰减。
- **M3 退场闭环（2026-09-22 ✅）**：doctor「待清理建议」段（K7 + 效果榜零贡献 + **甲负向反馈判据升级**：正向零+负向多优先）+ 效果榜负向侧统计（与 retrieval-upgrade M3 三象限同源口径）+ **丁 kb 结构体检**（doctor 体检段扩展 + 提案入库前机械 lint）。
- **M4 批量归纳（不实施）**：后置观察项，未达触发密度不实施（§0 #1）。
