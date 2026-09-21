# 编排器架构对标：主代理协调 vs 黑板模型（2026-09-20）

> 回答的问题：编排器下一步怎么演进——转「主代理协调」（central orchestrator agent 持上下文指挥子代理）还是维持/强化其它模式？
> 调研方式为联网检索（WebSearch 摘要多源交叉，未逐篇核原文）；关键判断建议进原文复核。本文件是 DESIGN.md §6.4 编排拓扑定稿块（2026-09-20）的展开佐证，与 docs/pentest-agent-benchmark.md（agent/skill/kb 专题）互补，编排专题不重复那边已写的项目简介。

## 结论先行

**不转主代理协调。** 开源界主代理协调是主流形态（约 40-50%，PentAGI 为标杆），但学术实证不支持它更优；本项目现有「黑板 + tick planner + 决定性代码持状态」恰命中证据最强的象限（黑板/共享状态 + plan-and-execute 混合）。真正差距不在拓扑，在三处工程细节：**worker 侧有界投影、证据纪律/失败免疫、异构并行对抗验证**（→ §17 L1-L3）。

## 一、开源框架编排拓扑分布

| 拓扑 | 占比（主观估计） | 代表 | 关键机制 |
|---|---|---|---|
| **主代理协调**（central + 委派 specialists） | ~40-50% | PentAGI（1 个 Primary Agent 指挥约 13 个 sub-agent，五类分工：执行/顾问/工作流/支撑 + FlowController 映射）；GH05TCREW PentestAgent 的 Crew 模式 | Primary 持编排上下文，专项 agent 在 Docker 容器内独立执行、结构化结果回递；Reflector 专项做错误恢复；长期记忆外挂 Graphiti 时序知识图谱 + 向量库（对话历史不当状态） |
| **黑板/共享状态** | ~15% | **Pentest-Swarm-AI**（Armur.ai，Go）：agent 互不直接通信，经共享黑板（PostgreSQL+pgvector）读写，**费洛蒙（pheromone）触发/衰减**决定攻击链涌现；Scheduler **只管并发/预算/优雅停机，不做计划**（stigmergy 间接协调 + emergence） | 无全局优先级裁决者；并行独立探索强 |
| **计划-执行分离**（决定性代码持状态） | ~20-25% | **PentestGPT v1.0**（Supervisor/Executor 双角色最小事件循环 + SQLite Memory Kernel）；VulnBot/xOffense 的 Penetration Task Graph（强制阶段序） | **有界投影**：Supervisor 只看「开放任务集+最近 4 个关闭任务+依赖+聚合历史」；Executor 只看「单任务+依据+同任务最近 2 次观测+1 条重试诊断」；观测=成功命令收据的精确切除片段。**数小时长任务最稳的工程形态** |
| **单 agent 自主循环** | ~10-15% | RapidPen（单 ReAct + 成功案例复用，IP→shell 200-400s、~60% 成功率）；hackingBuddyGPT（perform_round 滑动历史循环） | 简单任务足够；长任务上下文易崩 |
| **路由式 skill 集群** | 混合 | pentest-ai-agents（显式路由 ~50 YAML agent；无共享编排记忆，跨阶段靠显式传文件 + SQLite） | 适合并行探索，不适合严格依赖链 |

商用补充（公开资料有限，标注不确定）：Horizon3 NodeZero = RL + 攻击图推理 + 多跳记忆的单一「AI hacker」引擎沿 kill chain 编排（非子代理委派）；Pentera/Xbow 编排细节未披露。

## 二、实证证据（论文侧）

- **CSI（arXiv:2605.28334）——本问题最直接的实验**：33 个 Cybench 题，最佳单 scaffold 45.5%（15/33）vs **黑板式多代理（异质 scaffold 并行、共享中间发现）57.6%（19/33）**，相对 +27% 且快 25%。结论：无单一 scaffold 主导，**黑板/共享状态在安全任务上有结构化增益**——但增益有条件（规模化、能力异构时才兑现）。
- **学界对「中心主代理协调」无共识支持**。实证分裂：D-CIPHER（arXiv:2502.10931，Planner+异质 Executor，NYU CTF 22.0%/HTB 44.0%，含单代理消融）支持分层增益；RapidPen/Palisade/EnIGMA 显示单代理+工具+记忆复用往往足够。
- **MAST（Why Do Multi-Agent LLM Systems Fail?，arXiv:2503.13657，NeurIPS'25）**：1600+ 轨迹、7 框架、14 类失败归 3 大类（系统设计缺陷/代理间失对齐/任务验证）；「上下文膨胀/信息丢失」直命中中心主代理的短板（要吞全部子代理回传，长视野易超窗失焦）；另有串行瓶颈与单点（中心规划错全链错）。
- 多代理负面证据：Frontiers 2026 火星决策基准（单代理 token/延迟显著更低、质量无显著差异）；「Two Calls Beat Five Agents」（arXiv:2607.26922）；「When More Is Less」（arXiv:2609.19759）；LLM Multi-Agent Debate Fails。反向条件化：Google scaling agent systems（arXiv:2512.08296）——上下线由**布线**决定。
- **PentestEval（arXiv:2512.14233）**：端到端管线仅 ~31%，PentestGPT/PentestAgent/VulnBot 差异不大，瓶颈在**阶段衔接与验证回路，不在协调拓扑**——「架构决定成败」是论文叙事 > 实证。
- **人类在环的价值**：AutoPenBench（arXiv:2410.03225）全自动单循环 21% vs 人工分派子任务 64%。
- 长视野共识：plan-and-execute 必须配**频繁重规划**（渗透环境高意外率：指纹变化/报错/蜜罐→倾向频繁再决策，纯静态长计划不可靠）；PentestGPT 的**证据纪律/失败免疫**（只有无外部动作收据的失败才重试；有收据的失败绝不自动重放；预算跨重启持久化）比「更多重规划轮次」更能防长任务空转与副作用失控。

## 三、对照 cyberstrike-pro 现状

现状模型（DESIGN.md §6.4/§6.8）：编排器 = 普通 LLM 会话 + 特殊工具集（tick 制、无持久对话上下文），每轮从黑板重组态势（任务/资产/HVT/最近简报/战役记忆/增量事件窗 100）做分析-分解-分派；worker 会话自主认领、conflict_keys 互斥、parent_id 任务树、事件驱动触发（worker 空退→自动 tick）、tick 租约单飞、L0/L1/L2 自主档 + 人类插话。

**已被外部验证、不动的**：
1. 黑板单一写入口 + 结构化状态 ↔ CSI 黑板实证 + Pentest-Swarm 标杆（双验证）；
2. 「决定性代码持状态、LLM 只做提案」↔ PentestGPT v1.0 同构（tick 态势收集→LLM 工具循环→实体校验落库）；
3. 人类在环（L0 提案/L1 审批/插话通道）↔ AutoPenBench 21% vs 64%；
4. 事件驱动 + 租约单飞 + 失败放回（重规划频率与崩溃恢复已覆盖）。

**差距（三项定稿挂账 §17 L1-L3，实施后置）**：
- **L1 有界投影**：编排侧态势注入已有界，但 **worker 认领任务的上下文注入**没有 PentestGPT 式「投影」纪律（单任务+依据+同任务最近观测+重试诊断，而非宽泛摘要）；
- **L2 证据纪律/失败免疫**：task.reopened 放回 / auto chain 可能自动重放**有外部副作用的失败命令**，无「有收据的失败不自动重放」闸；
- **L3 异构并行对抗验证**：CSI 增益来源——同目标异质 scaffold 并行、共享中间发现。黑板天然支持多窗并行，但「同 HVT 双窗异构模型并行打、发现互通」未成编排策略。

**明确不采纳**：PentAGI 式 13-agent 主代理委派（13 个 LLM 会话开销、MAST 上下文膨胀失败模式、无实证优势）；图谱化编排记忆（GraphRAG 前轮对标已定稿不做，见 docs/pentest-agent-benchmark.md）；Pentest-Swarm 费洛蒙涌现整体替换编排器（我们保留全局 planner 裁决优先级——Swarm 无全局裁决是其已知短板，这点不学）。

## 主要来源

- PentAGI：deepwiki.com/vxcontrol/pentagi（7.1 Agent Architecture / 7.3 Agent Roles）
- CAI：deepwiki.com/aliasrobotics/cai；aliasrobotics.com/cai.php
- PentestGPT v1.0：项目 README/loop.py 解读（CSDN 镜像 161250727、157952186）
- pentest-ai-agents：cloud.tencent.com.cn/developer/article/2711363
- hackingBuddyGPT：deepwiki.com/ipa-lab/hackingBuddyGPT
- Pentest-Swarm-AI：Armur.ai 开源仓库页
- 论文：CSI arXiv:2605.28334；MAST arXiv:2503.13657；D-CIPHER arXiv:2502.10931；PentestEval arXiv:2512.14233；AutoPenBench arXiv:2410.03225；RapidPen arXiv:2502.16730；PentestAgent arXiv:2411.05185；xOffense arXiv:2509.13021；Hacking CTFs with Plain Agents arXiv:2412.02776；EnIGMA arXiv:2409.16165；NYU CTF Bench arXiv:2406.05590；Two Calls Beat Five Agents arXiv:2607.26922；When More Is Less arXiv:2609.19759；scaling agent systems arXiv:2512.08296；LLM 多代理黑板 arXiv:2510.01285；PatchBoard arXiv:2605.29313
