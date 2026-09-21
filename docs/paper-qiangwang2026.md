# 强网征文 2026 论文大纲（v1）

> 状态：**大纲草稿，待作者评审**。截稿 2026-10-31。
> 路线定稿：B+A 混合——主贡献 = 黑板协调的一致性治理（B），平台架构与执行网关为载体（A）。
> 写作规范：中文论文参照《信息网络安全》期刊。实验依托 [nyuctf-eval.md](nyuctf-eval.md)（K8 harness 已落骨架）。

## 0. 差异化定位（全文叙事锚点）

**一句话主张**：黑板架构在攻防多智能体系统中被用作「信息共享」，但长时攻防作业真正稀缺的是**共享之上的一致性治理**——并发写冲突、错误结论回收、跨会话持久性与执行边界约束。

- 已被占住的（引言中作为「现有工作」铺垫）：多 Agent 渗透框架（VulnBot / D-CIPHER / PentestAgent / Pentest-Chain）、黑板共享状态（Intentest）、CTF 基准（NYU CTF Bench / CyBench / EnIGMA）。
- 本文独有：① 资源租约 + 发布去重 + 乐观锁的**写冲突治理**；② **证伪撤回传播**（错误发现在多 Agent 间的回收）；③ 任务键快照的**跨会话断点续跑**；④ 执行网关（pathguard/rateguard/deny-driven 升级）+ 红线分级的**攻击性 Agent 自身安全约束**。
- Intentest（arXiv:2609.07344）是最近邻：其黑板只做事实共享不做一致性治理，恰好构成本文的对比与铺垫。

## 1. 题目候选

1. 《面向网络攻防多智能体协作的黑板一致性治理机制设计与实现》（首选，贡献导向）
2. 《cyberstrike-pro：黑板架构驱动的多场景智能攻防平台设计与评估》（系统导向）
3. 《大模型多智能体网络攻防系统中的一致性治理与安全约束研究》

## 2. 摘要骨架（约 300 字）

- 问题：LLM 多智能体渗透/CTF 自动化已有大量工作，但普遍是单会话、单轨、共享状态无并发治理；错误发现无法回收、长任务无法跨会话延续、Agent 行为边界缺乏硬约束。
- 方法：提出黑板一致性治理模型（资源租约 / 发布去重 / 证伪撤回传播 / 乐观锁 / 任务键断点续跑），并构建覆盖 CTF/渗透/红队/逆向/恶意样本五场景的统一平台加以验证。
- 结果：在 NYU CTF Bench 子集上与单 Agent 基线对比，给出解题率 / 步数 / token 成本 / 冲突率 / 撤回时效的量化结果。
- 结论：一致性治理是攻防多智能体系统走向工程可用的必要层次。

## 3. 章节大纲

### 第 1 章 引言（约 1.5 页）
- 网络强国背景 + 攻防自动化需求（对应征文主题基调）。
- LLM Agent 攻防自动化研究现状：单 Agent（PentestGPT 等）→ 多 Agent（VulnBot/D-CIPHER）→ 共享状态黑板（Intentest）。
- 引出空白：现有工作解决「共享」，未解决「共享之上的治理」——四个具体空白点（冲突/撤回/续跑/边界）。
- 本文贡献列表（4 条，见 §0）与文章结构。

### 第 2 章 相关工作（约 1.5 页）
- **2.1 LLM 攻防自动化系统**：PentestGPT、PentestAgent、VulnBot、D-CIPHER、MazeRunner、Pentest-Chain（中文文献）、xOffense、APT-Agent；消融类研究（Agentic Security systematization、Baselines Before Architecture）。
- **2.2 多智能体黑板架构**：经典黑板模型（Nii/Hayes-Roth）→ bMAS/LbMAS → Terrarium（安全视角）；点明与 Intentest 的区别（共享 vs 治理）。
- **2.3 评测基准**：NYU CTF Bench、CyBench、EnIGMA、CTFusion；渗透 Agent 自身安全（arXiv:2609.16694 护栏研究）。
- 每小节末一句「与本文差异」。

### 第 3 章 系统总体架构（约 2 页，A 路线载体）
- 3.1 能力包 × 场景轨正交分类学（五场景统一底座；角色随任务换装）。
- 3.2 分层架构图：黑板（单一写入口）→ Agent 循环（观察-思考-工具）→ 执行网关 → Runtime（host/wsl/docker/sandbox）→ LLM 路由（planner/executor/classifier）。
- 3.3 编排器：任务拆解 / 分批发布 / 三级自主度（L0 提案-L1 审批-L2 全自动）/ 防失控七闸。
- 设计原则：黑板单写入口（类比单一事实源）、宁严勿松、任务才是本体。
- （对应 DESIGN.md §3/§4/§7/§8/§12；本轮不展开逆向管线 R4 等新功能，仅架构级一句带过——架构已定型，后续新增功能不影响本章）

### 第 4 章 黑板一致性治理机制（约 3 页，全文核心，B 路线）
- 4.1 问题形式化：多 Agent 并发写黑板 + 并发作用于同一目标资产 + 发现可证伪 + 会话可中断，四类事件的形式化描述。
- 4.2 写冲突治理：conflict_keys 认领时快照交集 → 资源租约（wait_for 门控/释放重校验/环检测）；发布去重指纹 dedup_fp；revision 乐观锁 CAS。
- 4.3 证伪撤回传播：finding.retracted → 四类系统私信 → stale_refs 三选一处置 → 画布 FP 淡出；含撤回时效分析。
- 4.4 持久性：任务键快照（暂停双写/认领即复活/跨会话 resume）、C10 每步现场、优雅停机现场快照。
- 4.5 执行边界约束（与 3.3 呼应）：pathguard 写扫描、rateguard 限速、deny-driven 一次性权限升级、红线分级 rule_profiles。
- 每个机制配一张时序/状态图 + 与传统分布式系统原语（租约/CAS/two-phase）的类比表。

### 第 5 章 实验与评估（约 2.5 页）
- 5.1 实验设置：NYU CTF Bench 子集（题量按 token 预算定，30–60 题，四分类分层抽样）；模型配置；K8 harness（scripts/eval_ctf.py）判分口径。
- 5.2 主实验：单 Agent 直跑（baseline，可对照 NYU 原文 GPT-4 47%）vs 完整系统（编排 + 多角色 + 黑板）——解题率、平均步数、token 成本、用时。
- 5.3 消融实验：完整系统 vs 去协调（关租约/去重/CAS）——任务冲突率、重复作业次数、返工率；去撤回传播——FP 存活时长。
- 5.4 案例研究：一次完整会话走查（黑板事件流截图脱敏），展示撤回传播与断点续跑的实际过程。
- 5.5 安全性分析：gateway audit deny 统计（拦截类型分布）、红线分级生效情况。
- 表格规划：主结果表 / 消融表 / deny 分布表；图：架构图、撤回传播时序图、解题率对比柱状图。

### 第 6 章 讨论与展望（约 0.5 页）
- 局限：LLM 配额约束下的题量规模；pentest 轨真实环境评估待补；fakenet 未实现。
- 展望：能力基准（G4）、防御轨、报告生成。

### 参考文献（25–35 篇）
国内：《信息网络安全》体例；须含中文文献若干（Pentest-Chain 电信科学、自动化学报多智能体渗透框架、网安综述类）。
国外：Intentest、VulnBot、D-CIPHER、PentestAgent、MazeRunner、Terrarium、bMAS、NYU CTF Bench、CyBench、EnIGMA、ReAct、Reflexion、SWE-agent 等。

## 4. 实验执行与写作的并行拆分

| 周 | 实验/工程线 | 写作线 |
|---|---|---|
| W1 (9/21–9/27) | 冒烟 manifest ≤5 题 + harness 判分验证（K8 待办前两项）；附件/远程实例通路 | 大纲定稿；第 3 章初稿（素材=DESIGN.md） |
| W2 (9/28–10/11) | 主实验放量 + 消融组；失败重跑余量 | 第 1、2 章初稿；第 4 章初稿 |
| W3 (10/12–10/18) | deny 统计导出；案例会话筛选 | 第 5 章 + 全文初稿 v1 |
| W4 (10/19–10/25) | 补跑/复算 | 内审修订；模板排版 |
| W5 (10/26–10/31) | — | 终稿校对、投稿 |

## 5. 风险与对策

- **429 配额**：题量按预算倒推；冒烟先验证单题 token 均值再放量；错峰跑。
- **pwn 题环境依赖**：NYU CTF 需容器/nc 实例，harness 远程实例通路未落——若 W1 冒烟不通过，实验集向 web/crypto/forensics/misc 倾斜（NYU 原文分层即可支持）。
- **撞车风险监控**：Intentest 同方向，投稿前 re-check arXiv 一轮（10 月中）确认空白点未被补。
- **保密**：实验只用公开题库；案例截图脱敏；不含任何真实授权项目数据。

## 6. 素材索引（写作时直接取）

- 系统现状与机制细节：DESIGN.md（§3 Agent 循环 / §4 Skill / §5 黑板 / §6.7–6.9 协调与自主 / §7 Runtime / §12 WebUI）
- 版本演进叙事：DESIGN.md 版本索引 v0.1–v0.70
- 相关工作对标：DESIGN.md §17 J 组（Pentest-AI-Agents/CAI/PentestGPT 对标记录）
- 实验 harness：scripts/eval_ctf.py + docs/nyuctf-eval.md
- deny/事件统计源：黑板 events 表、gateway audit 日志
