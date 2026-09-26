# 卡死升级与跨窗方向去重（stuck-convergence）

> 止损的收敛性维度（借鉴 hxbai「假设空间重复度」+ open-code-review 零新增收敛）：卡死检测从「反复问顾问」升级为「有出口的停轮」，接手窗从「从零开始」升级为「带着历次已试方向进场」，任务完成从「自报即信」升级为「饱和确认背书」。衡量的是收敛性而非步数。

- **状态**：**M1 + D7 + D9 已实施（2026-09-24；D9 活跃探索静默延长见 §2.1；见 DESIGN.md §六），剩 M2（D3 跨窗方向指纹）待排期**
- **拍板记录**：见 §2 决策表（D1-D4 原始四项；D6 收尾确认 2026-09-23 用户批准增补；D5 预留待拍板；**D7 波次 2 顾问裁决（替代机械终止）+ 硬闸后移第 3 轮，2026-09-24 讨论定稿当日实施**；**D9 卡死预检：活跃探索静默延长（不叫顾问），2026-09-24 讨论定稿当日实施**；均为工程常规决策——升级语义复用既有停轮保护、相似度判定交 LLM 不自研算法）
- **思想来源**：inwpu/hxbai（Tsecbench v1 第一名，93.4 分）stoploss 多维止损 + runlearn 死路负知识；**源码核实**：其开源代码无独立命令相似度实现，对应物 = `dry_streak`（连续无新事实会话数判停）+ 并行车道分工注入 + `add_deadend` 死路库三件的组合——思想有效、算法需自设计。D6 另借 alibaba/open-code-review（阿里两年生产验证 AI 评审 CLI）主评审循环的零新增收敛退出（`len(newlyConfirmed)==0 → break`）+ 已确认意见注入防重复的两前提设计
- **关联代码**：`core/agent/loop.py`（stuck_after=12 卡死检测 :2168（2026-09-24：8→12 用户拍板） / 顾问干预重置观察窗 :1493 / `_advisor_prompt` / E2 拒绝熔断停轮保护 / C10 `_handover_notice` 接手注入）、`core/agent/tools.py`（`last_progress_step` 十余个更新点 = 黑板状态变化即进展）、`core/blackboard/traces.py`（`_session_task_windows` (lo,hi] 任务窗区间 + `_session_events` 命令事件回放，D3 数据源）

## 1 背景与现状盲区

现有卡死检测（`AgentConfig.stuck_after=12`）：连续 12 步无黑板写入类工具成功（`last_progress_step` 更新点 = asset/finding/plan/route_lookup/decompile 等状态变化）→ 召唤 planner 策略顾问。重复跑同一条失败命令本就不更新进展——「不收敛」**已经被抓到**。两个真盲区：

1. **「顾问-无进展」无限循环**：顾问干预后 `dispatcher.last_progress_step = step` 只重置观察窗不计数（loop.py:1493）——Agent 无视建议继续打转 → 又 12 步 → 再召唤（每次顾问 = 一次 planner LLM 调用）→ **没有升级出口**，烧到 max_steps 兜底。对照同文件 E2 拒绝熔断有完整升级语义（连续 3 次拒绝 → awaiting_human 停轮保护）。
2. **跨窗重踩**：任务 fail→reopen 换窗（C10 接手），接手窗 stuck 计数从零开始；上一窗试死的方向无机械信号注入——transcript 只含对话不含命令全景，死路知识靠 campaign 沉淀+召回（事后），**当场重踩照烧**。
3. **自报 done 无饱和背书**：执行者调 task_complete 即完成，无验收条目的任务「提前宣布胜利」（拿到首个低危就收）无任何机械检查——对照编排器层已有 converged 停链（项目级零新增收敛，§6.8），任务层缺同构信号。

## 2 设计定稿

### 2.1 决策表

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 卡死升级语义（M1） | 顾问干预重置观察窗时记 `_stuck_waves`（每任务开头复位；夹一次有进展清零，镜像 `_reject_streak`）；同任务第 2 轮卡死**不再召唤顾问**，升级停轮保护：落 `agent.stuck_escalate` 事件（含 waves/last_progress_step/最近命令摘要）+ `awaiting_human` + fail(resumable=true)——**复用 E2 停轮保护语义**（快照+续跑链路原样），宁停不烧 |
| D2 | 顾问视野增强（M1 搭车） | `_advisor_prompt` 注入最近命令去重统计（**完全相同命令 ×N 排行**，从 recent_events command 事件聚合，cap 10 行）——收敛性判断交 LLM，不自研相似度算法（归一化难调：「换 IP 的同型命令」算进展还是重复？语义交给模型判断） |
| D3 | 跨窗方向指纹（M2） | C10 认领注入时，从 command 事件按任务历史窗口（traces 区间语义）机械聚合「历次尝试已执行方向」：同命令 ×N 排行 + 按首词聚类的探测面统计（nmap ×3 目标 10.0.0.1 / curl ×7 路径族 /…，cap 15 行），随 `_handover_notice` 注入「⚠ 历次尝试已执行方向（勿原路重踩，死路见上窗结论）」——**正交补强 independent-verification M4**（M4 注入「为什么失败」，D3 注入「试过什么」，可同批实施） |
| D4 | 明确不做 | 自研命令相似度/归一化算法（收益模糊、规则难调）；编排器方向粒度去重（resource_leases 已有 target 粒度互斥，方向粒度成本高）；编排器消费 stuck_escalate 自动止损派单（信号已交人类，编排器消费后置观察） |
| D5 | （预留）工具失败连击假接受 | 同源参照 open-code-review `tool_failure_streak`：per (task,tool) 连败三级升级——重试错误 → 点名重复 → 第 3 次起**假接受**「已跳过，继续下一个」，补齐 D1 会话级停轮与动作级放弃之间缺的中间档；**待用户拍板后补细则** |
| D6 | 收尾收敛确认（M1） | 执行者申报 complete 后追加**收尾确认轮**（封顶 2 轮）：注入已登记 findings/assets/plan 完成度清单问「对照目标还有遗漏吗」——有则续干，一轮零新增黑板写入 → done 落定退出（借鉴 OCR 零新增收敛）；搭车**干尾巴启发式**：complete 时最近 `stuck_after-2` 步黑板零新增 → 直接放行免确认轮（常态零额外成本）。**只放收尾边界**（任务中途零新增退出不做，见 §5）；与验收条目正交——reconcile 管真伪，D6 管遗漏，两道门并存 |

| D7 | 波次 2 顾问裁决（D1 修订，待排期） | 第 2 轮卡死不再**机械**终止，改调顾问**裁决模式**（advisor.verdict，与建议模式 advisor.suggest 分立）：输入=上次建议全文 + 之后 12 步实际命令/工具（tail 现成）+ 该窗有无黑板写入硬事实 + kb 召回（可并接 campaign.recall）；结构化三选一：`终止`（走原 `_stuck_escalate` 同落点：awaiting_human + fail resumable，带裁决理由）/`继续`（必须给与上次不同、未尝试过的具体指令，再开一个 12 步窗、waves=2）/`信息不足请求人工`。**硬闸后移不拆除**：继续后的窗再卡满（累计 36 步无写入、2 次顾问）→ 无条件机械终止；解析失败/超时回落=终止（宁严勿松，回落即今天行为）。继续指令以**半强制**注入（最高优先 user 指令，要求本轮先回应如何执行/为何不适用，不锁死工具面）；新事件 `advisor.verdict` {decision, reason, wave} 全留痕 |
| D8 | （已撤回） | 曾议「设置页顾问开关」，2026-09-24 用户决定不做 |
| D10 | （独立成文，**2026-09-24 已实施**） | 顾问四参数项目级配置+设置页 UI：见 [advisor-settings-ui.md](归档-已完成/advisor-settings-ui.md)（已归档；总开关仍不做） |
| D9 | 卡死预检：活跃探索静默延长（M1） | 观察窗到期且 **waves=0**（顾问尚未介入）时不立即叫顾问，先零成本机械预检 `_active_exploration`：取 (lps,step] 窗内本会话 command/file.read 事件，命中「新文件」或「命令演进」任一 → **静默延长**（last_progress_step=step，extensions+1，落 `agent.stuck_extend` 事件），不叫顾问、不注入、零 LLM；不活跃或延长上限（2 次）用满 → 走原 waves 链。夹一次真黑板进展，extensions 与 waves 一并清零；预检仅 waves=0 生效，顾问介入后链路不变；硬闸不拆。配套：command 事件补 step 字段、read_file 成功落 `file.read` 轻事件（不存内容）；js-reverse 手册增补留痕纪律（先粗 task_plan、中间脚本即 add_artifact）。**背景**：JS 逆向等长程任务的真实循环全在 run_cmd/read_file（均不记进展），12 步必被顾问打断；信号缺失要在「叫顾问之前」补，而不是放宽阈值 |

### 2.2 关键机制

**D1 升级链**：第 1 轮卡死 → 召唤顾问（现状，干预后重置观察窗 + waves+1）；第 2 轮卡死 → `agent.stuck_escalate` + awaiting_human 停轮（与 `agent.reject_breaker` 事件/分支同构，走同一段停轮保护代码）。**不自动 fail 重派**——停轮把判断交人类（可续跑/可换角色重开/可改 objective），与「宁停不丢现场」一致。D2 的命令统计同时进 stuck_escalate 事件 payload，人工处置时看得见卡在哪。

**D3 聚合口径**：纯机械（首词分词 + 命令原文精确计数），不做语义归一化；区间复用 traces `_session_task_windows` 的 (lo,hi] 窗口语义（含未收尾硬闭合），历次尝试=该任务全部窗口并集；注入时机在认领期 `_handover_notice` 之后追加一段，单次认领注入一次（快照恢复路径已含历史注入不重跑）。

**D7 顾问裁决（D1 修订）**：把 D1 第 2 轮的「机械终止」改为「顾问裁决、硬闸兜底」。两调用面分立——`advisor.suggest`（现有建议模式，自由文本、无强制力）与 `advisor.verdict`（新增裁决模式，结构化三选一、结论驱动流程）。顾问对照上次建议与后续 12 步事实自行区分「建议被无视→倾向继续」「建议被执行但失败→倾向终止」，命令-建议的语义匹配不写代码（D4 口径），但机械供给「该窗零黑板写入」硬事实。三选一中「信息不足请求人工」是给模型的体面出口，压乐观偏差。全程有界：累计 ≤36 步无写入 + ≤2 次 planner，第 3 轮硬闸保证断路器不拆；裁决终止与硬闸终止同落点（快照 + resumable），仅裁决者与事件不同。

**D9 活跃探索预检**：判据纯机械、保守（OR）——①**新文件**：窗内 file.read 路径出现本会话此前未读过的（旧集合从历史事件聚合；无 step 字段的旧事件不参与，宁可叫顾问也不假延长）；②**命令演进**：窗内命令 n≥3、去重后不同命令 ≥3、最高重复次数 ≤n/2（半数以上同一条 = 打转，不是演进）。延长上限 2 次；有界时间线：真打转 12→24→36 步链不变（suggest→verdict→硬闸），活跃长任务 12/24 步静默延长 → 36 步 suggest → 48 步 verdict → 60 步硬闸（≤2 次 planner、断路器不拆）。延长落 `agent.stuck_extend` 事件（带命中信号明细），直播间可见为什么没叫顾问。

**D6 收尾确认**：complete 闸（reconcile 通过后）追加确认轮——产出清单全部黑板现成数据（findings/assets/artifacts + plan 步完成态）；模型续干则回正常循环、新产出照常计入事件流；答无遗漏且该轮零新增黑板写入 → done 落定。取信逻辑对照 OCR 两前提：①清单注入使模型「明知已报什么」——零新增 ≠ 忘了/重复；②零新增由事件流计数判定、非模型自报。**干尾巴阈值取 `stuck_after-2`（=10）与 D1 观察窗错位**：干满 12 步先撞 D1 停轮、10-11 步干尾巴申报 complete 走 D6-B 放行，两判定无竞态；确认轮期间任务仍 claimed、事件照常落库（可观测）。

## 3 实施切分

### M1 会话内升级（2026-09-24 已实施）

D1+D2+D6 一次落地，新增 8 用例、全量 986 passed。实施与设计一致，落点注记：

- D1：`_stuck_waves` 在 `loop.py` 每任务开头复位、有黑板进展即清零；第 2 轮卡死走 `_stuck_escalate` → 复用 C1 awaiting_human 停轮分支（快照 + fail resumable），新事件 `agent.stuck_escalate` payload = waves/step/last_progress_step/repeated_commands/recent_commands。
- D2：`_command_repeat_stats`（本会话 command 事件、命令原文 strip 精确计数、×≥2、降序 cap 10）注入 `_advisor_prompt`「最近重复命令」段；单次出现的不进排行。
- D6：收尾确认闸做在 `tools.py` 的 `ToolDispatcher` 内（`_closing_gate`，reconcile 闸之后、`complete_task` 真收尾之前），每任务 `reset_closing`；新事件 `agent.closing_confirm` {round, pathway（"" 首轮 / zero_new / new_output / dry_tail）, step}；干尾巴阈值 `stuck_after−2`；确认封顶 2 轮。
- 测试坑（非设计偏差）：预算暂停态有意保留心跳，构造「claimed 未收尾」场景的用例必须显式中止释放，否则守护线程泄漏进全局孤儿心跳断言；未过 A2 计划闸的 run_cmd 连拒会先触发 E2 熔断，命令重复类用例须先 task_plan。
- 当日真实事故补修（task-97e1d5d6c597，113 事件长会话）：`recent_events(limit=N)` 取最早 N 条，升级事件的 recent_commands/重复排行与顾问 digest 全看到会话开头（人工看到开场 spill grep 而非卡点处的 JS 分析）；四处统一改 `tail=N`（sediment digest 与 bb_query events 同修），新增 1 回归用例，全量 995 passed。

### D9 批次（活跃探索静默延长，2026-09-24 已实施）

1. `gateway.py`：`run()` 加 `step` 透传参数，command 事件 payload 带 step（旧调用方均不受影响）。
2. `tools.py`：run_cmd 传 `step=self._step`；read_file 成功（拒绝/错误路径不落）追加 `file.read` 轻事件（path/step，不存内容）。
3. `loop.py`：`_stuck_extensions`（每任务复位、夹进展清零）；主循环卡死分支前置预检（仅 waves=0、extensions<2）；新增 `_active_exploration`/`_extend_stuck_window` + `agent.stuck_extend` 事件。
4. `packs/kb/web/webapp/js-reverse/手册.md`：开头补「长程逆向留痕纪律」（粗 task_plan + 中间产物 add_artifact）。
5. 测试：新增 4 用例（命令演进延长/新文件延长/重复命令不延长/延长上限后走顾问链）。

### M2 跨窗方向指纹（待排期，可与 independent-verification M4 合批）

1. `loop.py`：认领期方向聚合注入（D3）；`traces.py` 复用既有窗口函数出「任务历次命令聚合」助手。
2. 测试：多尝试任务注入行数/去重；单尝试/零命令任务零注入。

### D1 修订批次（D7 顾问裁决，2026-09-24 已实施）

1. `loop.py`：波次分支三档（waves=0 建议 / waves=1 裁决 / waves≥2 硬闸）；新增 `_advisor_verdict`（结构化三选一 + 任何异常回落终止）、`_parse_verdict`（非 JSON/非法 decision/continue 缺 instruction 均回落 terminate）、半强制指令注入。
2. 事件：新增 `advisor.verdict`（decision/reason/wave/step/instruction）；`_stuck_escalate` 增 trigger（advisor_terminate/hard_backstop/mechanical）+ verdict_reason。
3. 顾问接 campaign.recall 未做（留待后续；不加新检索、不上向量）。
4. 测试：新增 5 用例（裁决 terminate 落点 / continue→硬闸 / human 请求 / LLM 异常回落 / 解析单测），改写 2 个旧机械行为用例，全量 1007 passed。

## 4 风险与对策

| 风险 | 对策 |
|------|------|
| 误停轮：真实慢场景（exp 构建多步无黑板产出、长扫描单命令多步等待）被第 2 轮卡死误杀 | 第 2 轮才停（给足一轮顾问干预 + 观察 12 步；阈值 2026-09-24 由 8 放宽为 12）；awaiting_human 恒 resumable，续跑无损失；停轮事件带命令摘要供人工快速判断 |
| D7 顾问乐观偏差，反复判「继续」无出口 | 第 3 轮机械硬闸 + 累计 ≤2 次 planner 封顶；继续须给与上次不同的具体路径；解析失败回落终止 |
| D7 裁决解析/LLM 失败致流程歧义 | 任何异常一律回落终止（=今天行为），绝不回落继续；「信息不足」第三项给合法挂起出口 |
| 顾问本来就是 LLM，D2 统计可能已被执行摘要覆盖 | 执行摘要是事件流杂拌，重复统计是聚焦去重视图——成本一行级，先观测收益 |
| D3 注入膨胀（长任务几百条命令） | 精确计数排行 cap 10 + 首词聚类 cap 15，全段 ≤20 行；无命令窗口零注入 |
| D6 确认轮拖收尾/烧 token | 封顶 2 轮 + 零新增即退；干尾巴启发式让常态任务零额外轮；确认轮期间任务仍 claimed、事件照常可观测 |

## 5 明确不做

- 命令相似度算法（2-gram/Jaccard/embedding）——归一化语义难调，D2 把判断交 LLM。
- 任务中途的零新增退出（D6 的反面）——探索型任务材料不固定（边打边发现新攻击面），中途零新增可能只是探测间隙而非饱和；中途退出权永远归 stuck/预算管，D6 只认收尾边界。
- 编排器按「方向」派单去重——target 粒度已有租约互斥，方向粒度留给观察。
- 停轮后的自动重派/自动换角色——人在环上，编排器消费 stuck_escalate 后置（与 task-type-evolution M4 第二执行人交汇再议）。
