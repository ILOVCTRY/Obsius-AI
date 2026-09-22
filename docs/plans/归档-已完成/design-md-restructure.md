# 方案：DESIGN.md 重构——功能导向参考手册

- **状态**：**已实施（骨架版）**——2026-09-21 四项拍板后当日落地；展开区渐进填充中
- **拍板记录**：见 §4（4 项决策已确认）
- **动机**：现 DESIGN.md（~800 行 14 章）按设计推导过程组织，定稿块/修订记录/版本杂讯混在正文里，显示杂乱。重构为**功能导向参考手册**：只显示当前项目已实施的功能，一级标题=大架构系统，二级标题=功能，每功能一两句话速览，需要时再深入展开实现。
- **关联**：重构对象 `DESIGN.md`（唯一真相源，CLAUDE.md 文档地图首位）；实施时同步核对其入口描述。

## 1. 新形态规范

```markdown
# <系统名>                 ← 一级标题：大架构方向（黑板系统、情报系统…）
## <功能名>               ← 二级标题：该系统的一个功能
一两句话速览（干什么、关键机制、入口文件）
<!-- 展开区（第二阶段逐个填充）：实现说明、关键细节、坑 -->
```

- **只写已实施功能**；未实施方向留在 docs/plans/ 方案里，实施落地后才回写（现行方案制度不变）。
- 速览必须一两句话说清；展开区允许空着渐进填充（重构第一步只落骨架+速览）。
- 全中文；代码引用用文件路径。

## 2. 完整大纲（12 系统 × 功能速览）

### 一、平台总览
- **项目定位**：AI 驱动的全能安全平台；能力包×场景轨覆盖 CTF/渗透/红队/逆向/恶意样本；原生 Windows 优先，按能力降级 Docker/WSL2。
- **总体架构**：core API（127.0.0.1:8420）+ WebUI（5173）+ SQLite(WAL) 黑板单写入口 + LLM 三角色路由；服务起停用 启动平台.bat。
- **技术栈**：Python 3.11+/FastAPI、React+TS+Vite、SQLite WAL、火山引擎 Ark 优先（OpenAI 兼容基类）。
- **目录结构**：core/packs/tools/webui/workspaces/config/scripts 各自职责一句话。
- **开发硬约束**：黑板单写入口、不可信代码只进容器、安全默认宁严勿松、Agent 无裸 shell。

### 二、智能体系统（core/agent）
- **双循环同构**：任务轮与对话轮共用同一条单步流水线，会话即窗口。
- **单步流水线**：每步=态势快照+LLM chat+工具分发+事件落盘，控制点可暂停/中止。
- **注入语义（订阅声明表）**：黑板事件按声明表注入会话（task.found/inbox/…），恢复期与认领期各有口径。
- **现场持久化与恢复**：plan/steps/chat/事件四层落盘，恢复按优先级重建现场。
- **收尾路径五分支**：完成/失败/中止等五种收尾全部幂等收敛。
- **会话窗对话化**：任务窗可随时对话引导，双击延续链路。
- **技能命中审计**：每任务落 skill.routed 事件（含 route_points），可观测与沉淀数据源。

### 三、黑板系统（core/blackboard）
- **数据模型**：17 张表（projects/tasks/findings/assets/func_kb/events/chains…）schema v18 演进而来。
- **单一写入口**：黑板所有写经 core API，禁止旁路直写存储。
- **并发模型**：单机多线程 + `_tx()` 事务串行化写。
- **任务队列**：一窗一任务（v0.72），claim 语义 + assigned_only + 优先级排序。
- **业务语义层**：资产状态回流（tested_clean 才算挖完）、relates_to 强相关、risk_tag 归属、high_value 标记。
- **发现链路图 board_graph**：5 类节点 9 种边，全来自既有字段，固定 ≤6 条查询。
- **会话与事件流**：events 表全量审计 + 事件游标消费。

### 四、任务与编排系统（core/orchestrator + 调度）
- **主代理 tick**：态势收集 → LLM 工具循环（publish_task/spawn_session/write_digest/done）→ 落盘；tick 租约单飞。
- **任务派生与判据**：mission + 判据三层（mission>模板>内置默认）驱动 auto_derive；judgment_templates.json 用户模板。
- **进程调度两段式**：绑定段（全挡位建待命窗）+ 启动段（L1/L2 起跑），六路触发点+60s sweep 兜底。
- **自主档与审批收件箱**：L0 提案模式 / L1 建窗待命+执行审批单 / L2 自动；预算闸门 gate(action)。
- **C2 指挥指令**：人类指令最高优先注入编排器；轮末落 directive.done。
- **战役记忆召回**：跨项目打法库按 mission+目标值召回 top-5 注入编排态势。
- **优先级重排 A5**：一次性 planner 轮只调 set_priorities，无 open 零 LLM。
- **饿死检测**：未注册类型/无专才角色 → task.starvation 告警去重。

### 五、执行网关（core/runtime）
- **隔离模型**：L0 host < L1 wsl < L2 docker < L3 sandbox（加固一次性容器），未知样本默认 L3。
- **网关流水线**：run(cmd, runtime) 九步序（审计/护栏/重定向/环境注入/取消…），Agent 无裸 shell。
- **静态护栏双子星**：pathguard 写逃逸判定（只拦写不拦读）+ rateguard 频控。
- **后端三实现**：host/WSL/Docker，协作取消（abort 杀进程树）。
- **双层权限模型**：网关侧策略 + Agent 工具层校验。
- **审计与体验**：命令审计协议、超时/取消的体验工程。

### 六、技能与知识体系（core/skills + packs/）
- **能力包×场景轨正交**：包=方法论+知识库（怎么干），轨=规则+角色+任务类型（什么语境）；LEGACY_TRACK_MAP 读兼容。
- **技能注册表与薄路由层**：中文薄技能只做「特征→kb 路径」对照，SKILL.md frontmatter 标签路由。
- **运行时路由链路**：route_index 测试点索引按上下文评分 Top-5 注入 + route_lookup 按需查；评分公式特征×3>task_type+10>角色+5。
- **知识库四层与 kb_open**：kb_sources 单源、快照名前缀消歧、kb/route.json 任务导航；import_kb.py 幂等导入。
- **角色与规则链**：roles yaml（persona/skills/task_types/noise）+ redlines/owners/rating/role-rules 分层注入。
- **提案制治理**：AI 只能 propose_skill_edit 落 .proposals/ 人审应用，永不直写；删除进 .history/trash 可恢复。
- **doctor 体检**：悬空引用/route-index-kb-missing/route-index-zero-hit，零 error 才健康。

### 七、LLM 接入层（core/llm）
- **协议统一**：内部只认 Anthropic /v1/messages，标准库 urllib 不引 SDK。
- **anthropic_compat**：OpenAI 兼容上游的协议转换核心。
- **ProviderStore**：供应商注册表（config/providers.json，真实 key 防回显）。
- **ModelRouter**：planner/executor/classifier 三角色路由 + 上下文预算联动。
- **横切**：重试/超时/record_llm_usage 记账。

### 八、工具体系（tools/ + config/mcp.json）
- **MCP 接入**：外部服务型工具（FOFA 资产测绘等）；config/mcp.json 禁写密钥，FOFA 轮换在 src-strike .env。
- **反编译双通道**：IDA headless 优先 → Ghidra 兜底（按 sha 缓存全量导出）+ IDA GUI MCP 实时桥（人在回路）；全不可用退纯静态并出安装引导。
- **工具目录 tools/**：反编译脚本区 + MCP 适配区（自包含工具链为已立项方案）。

### 九、情报系统（core/intel）
- **存储层**：第二个 SQLite 刻意「降配」（独立于黑板）。
- **抓取层**：NVD/源抓取，POC 启发式判定放在落库之前（数据入口完成）。
- **打分与合成**：LLM 三处降级链（LLM→模板→放弃）。
- **vault 接入**：只读圣域 + 隐私红线三重落实（笔记正文永不出本机）。
- **周计划与 API 面**：触发式生命律动，前端情报面板。

### 十、WebUI（webui）
- **应用骨架**：无路由双帧布局 + View 状态机 union type，needsProject 门控。
- **数据获取与实时层**：模块级手写 query cache + 轮询为底 WS 为翼；DB 行是真相、事件流是有损视图。
- **事件渲染管线**：四层分工（解析/去重/归并/渲染）。
- **视图组织与重画布**：React.lazy 隔离重依赖（@xyflow 画布永不进主包）。
- **页签与设置页**：会话页签状态灯、设置页（MCP/情报源/技能矩阵/规则/提案/doctor）。
- **设计系统与工程纪律**：Tailwind v4、组件约定。

### 十一、逆向领域集成
- **复用三层 R1/R2/R3**：rev-generic 工作台 profile、模块划分、重构支撑（名字避让 Runtime L1-L3）。
- **R4 逆向开发管线**：blueprint/reconstruct 任务类型 + rebuilder 角色 + blueprint-rebuild 技能 + spec 钉死红线（蓝图本体为已立项方案）。
- **三层数据纪律**：客观全量层（headless 缓存）/主观分析层/产物分离，样本 untrusted。

### 十二、工程化与运维
- **测试**：pytest 全量（734+），PYTHONIOENCODING 约定；fake runner/transport 注入模式。
- **启动/停机**：启动平台.bat（UTF-8+CRLF 自举）/停止平台.bat（优雅停机快照+超时硬杀）；shutdown 僵尸进程坑。
- **文档制度**：DESIGN.md 唯一真相源 + 目录 CLAUDE.md（≤80 行）+ docs/plans/ 方案目录。

## 3. 旧章 → 新系统映射

| 现有章 | 去向 |
|---|---|
| §1 项目定位 / §2 总体架构 / §11 技术栈 / §13 目录结构 / §14 开发约束 | 一、平台总览 |
| §3 Agent 循环 | 二、智能体系统 |
| §4 Skill 体系 | 六、技能与知识体系 |
| §5 黑板系统 | 三、黑板系统 |
| §6 任务与多会话协调 | 四、任务与编排系统（主代理/调度/审批从 worker/窗口内容中抽出独立列功能） |
| §7 Runtime 执行环境层 | 五、执行网关 |
| §8 LLM 接入层 | 七、LLM 接入层 |
| §9 逆向领域集成 | 十一、逆向领域集成（反编译双通道移入八、工具体系） |
| §12 WebUI | 十、WebUI |
| §16 情报面板 | 九、情报系统 |
| 各章内定稿块/修订记录/版本号杂讯 | **删除**（git 历史留档），重要推导一句话并入对应功能展开区 |
| （新增）十二、工程化与运维 | 现散落各处的测试/启停/文档制度汇聚成章 |

## 4. 拍板记录（用户确认 2026-09-21，四项全按推荐）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 12 个系统粒度 | **照用**（不合并） |
| 2 | 未实施方向（8 份方案） | **不进**重构后 DESIGN.md，实施后按功能回写 |
| 3 | 旧版处置 | **git 留档**，不另存副本 |
| 4 | 展开区填充节奏 | **先骨架后填**：第一步只落骨架+速览，展开区渐进填充 |

## 5. 实施记录（2026-09-21）

- 新 DESIGN.md 已按 §2 大纲落骨架+速览（~200 行，12 系统），旧版 ~800 行由 git 历史留档。
- 偏差说明：旧 §9 的 R1/R2/R3（复用三层）与 R4 逆向开发管线在旧版中标〔未实施〕，按拍板 #2 **未进**新 DESIGN.md；blueprints 表（schema v17）与 store CRUD 已实施，归入「三、黑板系统·数据模型」速览。R4 管线后续随 docs/plans/blueprint-blackboard-object.md 实施时回写。
- 大纲 §2 中「测试 734+」落稿时改为「700+」弱化易过期数字。
