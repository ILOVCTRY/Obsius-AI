# docs/plans/

> 方案目录：设计讨论收敛后的**方案文档**存放地，走「提出 → 细化打磨 → 实施」路线。

## 生命周期与规则

- 设计讨论收敛后，方案写到这里（**一方案一文件**）；**不写进 DESIGN.md、不挂账**。
- 方案在文档上多轮**细化打磨**（直接迭代修订，消化文内「待打磨清单」；重大改向需用户确认）。
- 打磨定稿后由**用户安排排期**实施；实施落地时才把定稿决策沉淀回 `DESIGN.md`（唯一真相源），并同步相关目录 CLAUDE.md。
- 部分实施：方案头部状态标「Mn 已实施、剩余后置」，**已完成里程碑从实施切分中剔除**，只留待办。
- **完全实施 → 归档**：头部状态改「已实施」，文件移入 `归档-已完成/`（下方归档清单同步），实施记录留在文内作设计推导背景。

## 约定

- 命名：`<主题>.md`，kebab-case。
- 文档头部必带：**状态**（讨论收敛 / 打磨中 / 已定稿 / 已实施）、**拍板记录**（决策点 → 结论表）、**关联代码**。
- 全中文。

## 现存方案（未完全实施）

**部分实施（只剩后置里程碑）：**

- [desktop-app-shell.md](desktop-app-shell.md) — 桌面化壳（M1 源码弹窗 / M2 静态托管 / M3 打包；关窗=owner 最后一窗优雅停机 / 二次启动 attach 新窗；pywebview+PyInstaller 主线），**M1+M2 已实施（2026-09-21，见 DESIGN.md §一「部署形态」），剩 M3 打包（venv 固化 + PyInstaller onedir + 资源布局）待排期**。
- [conversational-orchestrator.md](conversational-orchestrator.md) — 编排器专家化（对话窗 / goal 商议存 meta / 虚拟单例 / C2 对话窗全替代），**M1+M2+M3 已实施（2026-09-21，见 DESIGN.md §四「对话化编排器」），剩 M4 异常订阅唤醒（白名单 5 项触发）待排期**。
- [android-kb-sourcing.md](android-kb-sourcing.md) — binary 包 Android 子域收编（r0re：4 篇手册 + android-rev 技能 + route 4 条 + K6 案例参照），**M1+M3 已实施（2026-09-21，见 DESIGN.md §六），剩 M2 工具链（tools/py/android/ + registry 声明 + unicorn venv）待排期**——registry schema 与 toolchain-registry M1 谁先落地谁定。
- [pentest-phased-workflow.md](pentest-phased-workflow.md) — 渗透测试项目分阶段工作流（重心+配额 / 三阶段剧本 / 入场门），**M1+M2 已实施（2026-09-22，见 DESIGN.md §四「分阶段工作流」；实施修正注记见文内 §0），剩 M3 专家与内容 + M4 前端阶段条待排期**。

**定稿待排期：**

- [experience-sedimentation.md](experience-sedimentation.md) — 经验沉淀减产提纯（上游减产：campaign 条件写入+复盘条件触发共用一处机读判定 / blocked_reason 扩 aborted 档细分失败五来源 / campaign 收窄成功打法索引层、失败教训走 kb / 复盘 kb 对账+证据引用强制；业界借鉴：复用效果负向反馈+kb 结构体检入 M3、批量归纳后置观察、不追全自动自进化），2026-09-22 必要性审计+业界对照定稿。

**打磨中：**

- [retrieval-upgrade.md](retrieval-upgrade.md) — 检索质量与上下文效率升级（M1 prompt caching 请求侧打标 / M2 领域同义词表 synonyms.yaml 查询扩展 / M3 漏召回对账 retrieval-stats+黄金集 / M4 campaign 2-gram 召回；**伪差距明确不追**：向量库/GraphRAG/语义分块/重排/Mem0），2026-09-22 四项方向拍板，待打磨。
- [rules-four-section.md](rules-four-section.md) — 项目规则四段一体（frontmatter+正文 / 轨下 templates/ 模板库 / 判据并入，三级模型对齐 judgments）。
- [blueprint-blackboard-object.md](blueprint-blackboard-object.md) — 蓝图黑板一等知识对象（模块级知识 / 四表草案 / confirmed 冻结状态机 / mod 配方预留）。
- [sandbox-behavior-report.md](sandbox-behavior-report.md) — 沙箱行为采集与报告（S1 wine+strace 先行 / 统一事件 schema / artifact+Agent 双轨 / VM 插槽；fakenet=malware 解锁条件）。
- [toolchain-registry.md](toolchain-registry.md) — 工具链注册表与项目自包含（全手动放置 / tools/ 目录规范 / 网关注入 PATH+JAVA_HOME / 设置页工具面板与 MCP 并列 / 容器混合；Ghidra+JRE 自包含）。
- [dsh-kb-sourcing.md](dsh-kb-sourcing.md) — 能力包内容补源：dsh 知识库收编（cloud 包全量 / miniapp 种子并入 web / fpdb 指纹包入 tools），pentest 方案 M3 内容专题。

## 归档（已完全实施，`归档-已完成/`）

- [expert-pool.md](归档-已完成/expert-pool.md) — 专家池+项目绑定专家+kb 物理树重组+场景档/知识继承/看板默认视图，M0-M4 全量（2026-09-21）。
- [execution-trace-chain.md](归档-已完成/execution-trace-chain.md) — 执行轨迹链路双轨制+战果主线视图+打法效果榜，M1-M3 全量（2026-09-22）。
- [findings-canvas-dag-layout.md](归档-已完成/findings-canvas-dag-layout.md) — 链路画布单向零重叠布局（因果流水线：列=最长路径层恒向右 / 弱边只画右向 / 三路由正交不穿卡〔adjacent/long 空行带/cross 泳道底绕行〕/ hover 显注记 / 锁定拖拽 / MiniMap 折叠），D1-D4+R1-R4 全量（2026-09-22；实施简化=单边多拐点 path 替代 elbow 节点编组，见文内 §7）。
- [inbox-subscription-interrupt-flush.md](归档-已完成/inbox-subscription-interrupt-flush.md) — 收件箱消费订阅声明化 + 中断落盘（2026-09-21）。
- [finding-report-format.md](归档-已完成/finding-report-format.md) — 漏洞收录格式与报告渲染模板三件套（结构化 repro_steps[] + findings 新列 impact/remediation + 渲染器三处共用 + verified 门禁新口径），M1+M2 已实施（2026-09-22，schema 实际 v19→v20）；M3 报告链随 pentest-phased-workflow M4。
- [design-md-restructure.md](归档-已完成/design-md-restructure.md) — DESIGN.md 重构功能导向参考手册，骨架版（2026-09-21），展开区渐进填充为持续维护不在本方案。
