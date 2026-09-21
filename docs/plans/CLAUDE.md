# docs/plans/

> 方案目录：设计讨论收敛后的**方案文档**存放地，走「提出 → 细化打磨 → 实施」路线。

## 生命周期与规则

- 设计讨论收敛后，方案写到这里（**一方案一文件**）；**不写进 DESIGN.md、不挂账**。
- 方案在文档上多轮**细化打磨**（直接迭代修订，消化文内「待打磨清单」；重大改向需用户确认）。
- 打磨定稿后由**用户安排排期**实施；实施落地时才把定稿决策沉淀回 `DESIGN.md`（唯一真相源），并同步相关目录 CLAUDE.md。
- 实施完成后方案文档头部状态改「已实施」保留，作为设计推导背景，不删除。

## 约定

- 命名：`<主题>.md`，kebab-case。
- 文档头部必带：**状态**（讨论收敛 / 打磨中 / 已定稿 / 已实施）、**拍板记录**（决策点 → 结论表）、**关联代码**。
- 全中文。

## 现存方案

- [desktop-app-shell.md](desktop-app-shell.md) — 桌面化壳（exe 双击开窗：pywebview+PyInstaller 主线 / 前端静态托管共同地基 / Tauri 暂缓），2026-09-21 收敛，**优先级低不着急**，待打磨。
- [pentest-phased-workflow.md](pentest-phased-workflow.md) — 渗透测试项目分阶段工作流（重心+配额 / 三阶段剧本 / 入场门），2026-09-20 收敛，**2026-09-21 十条打磨定稿，待排期**。
- [execution-trace-chain.md](execution-trace-chain.md) — 执行轨迹链路（意图-产出-过程三类节点聚合级 / 记录与组装分离双轨制 / 沉淀飞轮 / 战果主线视图借鉴），2026-09-21 收敛，待打磨。
- [expert-pool.md](expert-pool.md) — 专家池+项目绑定专家（轨退规则层 / 能力包隐退 / 专家面推导 caps_effective / **kb 物理树重组**〔翻转登记表合一，`packs/kb/` 一级=能力域〕/ 23 角色→16 专家迁移映射 / 场景档后置），**2026-09-21 打磨定稿；同日 M0 kb 树重组已实施（§4.5），M1-M4 待排期**。
- [rules-four-section.md](rules-four-section.md) — 项目规则四段一体（frontmatter+正文 / 轨下 templates/ 模板库 / 判据并入，三级模型对齐 judgments），2026-09-21 收敛，待打磨。
- [blueprint-blackboard-object.md](blueprint-blackboard-object.md) — 蓝图黑板一等知识对象（模块级知识 / 四表草案 / confirmed 冻结状态机 / mod 配方预留），2026-09-21 收敛，待打磨。
- [sandbox-behavior-report.md](sandbox-behavior-report.md) — 沙箱行为采集与报告（S1 wine+strace 先行 / 统一事件 schema / artifact+Agent 双轨 / VM 插槽；fakenet=malware 解锁条件），2026-09-21 收敛，待打磨。
- [toolchain-registry.md](toolchain-registry.md) — 工具链注册表与项目自包含（全手动放置 / tools/ 目录规范 / 网关注入 PATH+JAVA_HOME / 设置页工具面板与 MCP 并列 / 容器混合；Ghidra+JRE 自包含），2026-09-21 收敛，待打磨。
- [conversational-orchestrator.md](conversational-orchestrator.md) — 编排器专家化（对话窗四件套 / goal 商议存 meta / 虚拟单例运行态注册 / 对话历史=events orch.chat 单一来源 / 插队轮不碰巡检节奏 / **C2 对话窗全替代**·API deprecated 兼容），**2026-09-21 九条打磨定稿，待排期**。
- [finding-report-format.md](finding-report-format.md) — 漏洞收录格式与报告渲染模板（三件套：危害描述/复现步骤〔编号+代码块+预期返回结果〕/修复建议；存储形态 A=结构化 repro_steps[] + findings 新列 impact/remediation + 渲染器三处共用；http/python/cmd 三类步骤；图片产物可引用；门禁联动含旧 pocs 读时兼容），**2026-09-21 定稿，待排期**。
- [inbox-subscription-interrupt-flush.md](inbox-subscription-interrupt-flush.md) — 收件箱消费订阅声明化（`_INBOX_SUBSCRIPTIONS` 订阅表 + `_drain_inbox` 单一入口，修恢复期吞 escalation_result / 认领期吞纯私信 basis_stale）与中断落盘（abort 半截回复落 `agent.chat` 带 `interrupted:true`），2026-09-21 已实施。
- [dsh-kb-sourcing.md](dsh-kb-sourcing.md) — 能力包内容补源：dsh 知识库收编（cloud 包全量 / miniapp 种子并入 web / fpdb 指纹包入 tools），pentest 方案 M3 内容专题，2026-09-21 收敛，待打磨。
- [android-kb-sourcing.md](android-kb-sourcing.md) — binary 包 Android 子域收编（r0re：4 篇手册 + android-rev 技能 + 5 工具入 registry + K6 案例参照〔cases/ 路径约定〕；许可已确认可收编），**2026-09-21 打磨定稿，待排期**。
- [design-md-restructure.md](design-md-restructure.md) — DESIGN.md 重构为功能导向参考手册（12 系统×功能速览+展开区渐进填充；只写已实施，旧版 git 留档），2026-09-21 骨架版已实施，展开区渐进填充中。
