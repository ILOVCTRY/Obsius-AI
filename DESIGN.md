# cyberstrike-pro 设计文档

> **形态**：功能导向参考手册（2026-09-21 重构）。一级标题=大架构系统，二级标题=功能，每功能一两句话速览；功能下的**展开区**（实现细节、关键坑）渐进填充中。
> **边界**：只写**已实施**功能；未实施方向在 `docs/plans/` 方案文档里，实施落地后才按功能回写本文。历史推导与旧版全文见 git 历史。
> **地位**：当前功能的唯一真相源，与各目录 `CLAUDE.md` 不冲突；硬约束速记见「一、平台总览」。

# 一、平台总览

## 项目定位

AI 驱动的全能安全平台：能力包 × 场景轨正交架构覆盖 CTF 解题 / 渗透测试 SRC / 红队行动 / 逆向 Pwn / IoT 工控车联网研究 / 恶意样本分析；同一套方法论跨场景复用。核心差异点=原生 Windows 优先，按能力自动降级 Docker/WSL2。

## 总体架构

core API（127.0.0.1:8420，FastAPI）+ WebUI（localhost:5173）+ SQLite(WAL) 黑板单写入口 + LLM 三角色路由；多个 AI 会话与 WebUI/CLI 是平等的 API 消费者，协调由服务端保证。服务起停用根目录 `启动平台.bat` / `停止平台.bat`。

## 技术栈

Python 3.11+ / FastAPI、React + TypeScript + Vite SPA、SQLite (WAL)（可平移 Postgres）、火山引擎 Ark 优先（OpenAI 兼容基类，内部协议统一 Anthropic /v1/messages）。

## 目录结构

`core/`（核心引擎：agent / blackboard / orchestrator / runtime / skills / llm / tools / api / intel）、`packs/`（capabilities 能力包 × tracks 场景轨）、`tools/`（反编译脚本区 + MCP 适配）、`webui/`（React SPA）、`workspaces/`（项目数据目录）、`config/`、`scripts/`、`docs/`。

## 开发硬约束

黑板所有写操作必须经 core API 单一入口，禁止旁路直写存储；不可信代码只允许在 Docker 容器执行（WSL 信任级=宿主机）；安全默认值宁严勿松（未知样本默认按恶意处理，L3 沙箱 + fakenet）；Agent 无裸 shell，一切命令经 `run(cmd, runtime)` 网关 + 服务端策略校验。

# 二、智能体系统（core/agent）

## 双循环同构

任务轮与对话轮共用同一条单步流水线（可中断 LLM 调用 + 工具分发 + 上下文裁剪），差异全在纪律（工具面/步数/现场持久化/退出语义）而非结构；会话即窗口。

## 单步流水线

每步按序过：persona 热换装 → 步号预算提醒 → 卡死检测（8 步无进展召唤 planner 顾问）→ 可中断 LLM 调用（即点即停 + thinking/回复双流式 + 截断整轮重试 + 中断落盘）→ 记账审计 → 工具串行分发 → 每步落盘现场文件 → 控制点。步数耗尽走预算暂停（保持 claimed 等人工续跑），宁停不丢现场。

## 注入语义（订阅声明表）

黑板收件箱事件的注入口径唯一真相源是模块级订阅声明表 `_INBOX_SUBSCRIPTIONS`（kind → 消费场景：认领期/快照恢复期/步边界/空闲对话轮），四处消费点统一走 `_drain_inbox(scenario)` 单一入口；未登记 kind 滞留收件箱红点可见，绝不静默吞。注入只发生在步边界，与审计事件天然对账。

## 现场持久化与恢复

四层：内存态 → 任务现场文件 task-<tid>.json（每步落盘）→ 会话暂停快照 → 任务键快照（跨会话认领即复活）。恢复按优先级（会话快照 → 任务键快照 → transcript 末 60 条接手 → 全新），装载前 sanitize 清悬空 tool_use 半对防 API 拒。

## 收尾路径五分支

finish / 异常穿出（停心跳→salvage 抢救→fail，幂等）/ awaiting_human（快照+fail(resumable)）/ 步数耗尽（预算暂停保持 claimed）/ 人工中断（当前步作废+快照保留）——全部收敛到幂等。

## 会话窗对话化

空闲窗打字=对话回复（Claude Code 式，chat_max_steps=24 不认领任务），有价值的阶段性结论随手 `bb_add_finding` 入黑板可追溯；agent_message 私信（三分类 subkind）+ task_receipt 子任务回执走异步收件箱，不做同步对话接力。绑 open/claimed 任务的待命窗绝不 kick（审批语义，宁严勿松）。

## 技能命中审计

每次技能路由落 `skill.routed` 审计事件（含未命中，带 route_points），可观测，同时是沉淀飞轮的数据源。

# 三、黑板系统（core/blackboard）

## 数据模型

17 张表，SCHEMA_VERSION=18，每版迁移幂等（PRAGMA 检缺列 ALTER / IF NOT EXISTS，绝不破坏性改）。去重：findings/func_kb 用 UNIQUE 指纹、任务用 dedup_fp，重复写走合并而非新增；含 blueprints（v17 蓝图）与 http_history（v15 浏览器抓包）扩展表。

## 单一写入口

黑板是全项目写存储的唯一入口，API 层也只调这里、禁止旁路直写；schema 边界纪律（如 orchestrator_state 编排列只经 core/orchestrator/state.py 白名单读写）。

## 并发模型

WAL + 线程局部连接 + `_tx()`（进程内写锁 + BEGIN IMMEDIATE）串行化写 + close_all 关闭闸门（专治 Windows 删项目时 WS/轮询线程重开连接锁死）。单进程设计，多进程需换 Postgres。

## 任务队列

一窗一任务（v0.72，公共池退役）：claim_next SQL 三参数正交收窄（target_session 指派 / only_task 绑定 / assigned_only），归属锁死在 SQL 侧。租约双轨（任务 30min TTL 心跳续租 + 资源 X/S 锁）、wait_for 等待-唤醒与死锁环检测、发布闸（dedup_fp 去重 / 同 target 防碎 / acceptance 对账硬拦）。

## 业务语义层

发现门禁（info 全类别拒收、verified 无 POC 拒、rating_basis 随 severity 就高覆盖——「无证据不下结论」写进存储层）；合并=证据并集；撤回传播（四类目标+私信）；TOCTOU 防护（读-合并-写整体单事务 + 乐观锁 CAS）；资产状态回流（tested_clean 才算挖完）、high_value 标记。

## 链路与全景图

chains/chain_links 攻击链（假设→验证→利用）+ board_graph 黑板全景图（5 类节点 9 种边，全来自既有字段，只读聚合）。

## 战役记忆

campaign.db 独立全局库（仿 intel 先例，跨项目不绑项目），按 mission + 目标值召回历史打法注入编排态势。

## 会话与事件流

events 表全量审计 + EventBus 同步落库、尽力广播；前端按游标增量消费。

# 四、任务与编排系统（core/orchestrator + 调度）

## 主代理 tick

态势收集 → LLM 工具循环（publish_task / spawn_session / write_digest / done）→ 结构化落盘；tick 租约单飞（TTL 900s + 每 LLM 步心跳续租）是编排层唯一硬互斥。

## 任务派生与判据

mission + 判据三层（mission 直配 > judgment_templates.json 用户模板 > 内置默认）驱动 auto_derive 自动派单；v14 防碎发布闸 dedup_fp。

## 进程调度两段式

绑定段（全挡位无条件建待命窗，零 LLM 成本）+ 启动段（L1/L2 起跑，逐个重算防超卖）；六路触发点 + 60s sweep 兜底，纯事件驱动无长驻调度线程。

## 自主档与审批收件箱

L0 提案模式（propose_only）/ L1 建窗待命 + 执行审批单（批准=启动既有窗，窗关则退绑重绑）/ L2 自动武装起跑；预算闸门 gate(action)；paused 项目级一次性闸，配置实时重读无缓存。

## C2 指挥指令

人类指令经 `_pending_directives` 以「⚠ 人类指令（最高优先）」注入编排器态势，轮末落 directive.done 留痕。

## 战役记忆召回

按 mission + 目标值从 campaign.db 召回 top-5 历史打法注入编排态势。

## 优先级重排 A5

一次性 planner 轮只调 set_priorities；无 open 任务零 LLM 调用。

## 饿死检测

未注册任务类型 / 无专才角色 → task.starvation 告警（去重）。

# 五、执行网关（core/runtime）

## 隔离模型

L0 host < L1 wsl < L2 docker < L3 sandbox（--rm 一次性 / 断网 / 512m / cap-drop ALL 加固容器）；trusted 全等级、untrusted 仅容器、unknown 按 malware_live 最严处理。三条硬规则写死代码：WSL 信任级=宿主机、未知按活体恶意样本、net=real 永不默认。

## 网关流水线

`run(cmd, runtime)` 九步序：would_deny 干跑（与真实执行同口径，杜绝两处校验漂移）→ net=real 审批校验 → 工作区隔离改造（cwd/TEMP 重定向）→ 限速检查 → 执行前审计事件 → 按后端执行 → 结果审计 → 审批一次性消费（封死长期通行证）→ 拒绝统一协议（GatewayDenied 回填改道不炸循环）。

## 静态护栏双子星

pathguard 写逃逸静态判定（只拦写不拦读）+ rateguard 扫描频控（拦的是「不限速地扫」，拒因文案自带放行配方）。

## 后端三实现

host（PowerShell/bash）/ WSL（env 不透传）/ Docker（run_once + exec_in pwn 交互）；协作取消三入口收 abort_event，0.2s 轮询杀进程树，宁误杀勿悬挂。

## 双层权限模型

角色 max_runtime 软上限（只可能比网关更严）+ 网关 threat_class 硬校验；request_escalation 升级=单次授权（net=real 与超角色上限两类，红线不受理）。人类命令同层经网关，审计流无旁路。

## 审计与体验

command / command.result 事件时序协议（BackendError 时悬空运行中=语义诚实）；截断可见化标记（防 Agent 语义猜）；detector 能力探测结果注入系统提示（Agent 不许自己猜环境）。

# 六、技能与知识体系（core/skills + packs/）

## 能力包×场景轨正交

能力包（web/binary/crypto/forensics/misc，多选）=方法论技能（怎么干）；场景轨（ctf/pentest/redteam/research，单选）=规则+角色+任务类型（什么语境）；平台/格式/漏洞类只走 frontmatter 标签不建包。LEGACY_TRACK_MAP 读取层兼容旧轨名。**kb 知识库全局单根 `packs/kb/`（M0，2026-09-21 定稿并实施，源自 expert-pool 方案）**：一级=能力域，原 `capabilities/<cap>/kb/` 原样搬迁（不翻译不就地改）；kb 源按启用域合成恒递归（`load_kb_sources`，kb_sources.json 登记退役），kb_open module 用全局形态 `<域>/<快照>/<路径>`，上游 LICENSE 收编 `kb/licenses/`。

## 四层知识体系

SKILL.md 薄路由入口 → kb/route.json 任务导航表（**M0 留域内** `packs/kb/<cap>/route.json`，值带域前缀）→ `packs/kb/route_index.yaml` 全局单表测试点索引（**M0**：条目 kb 全局形态，load_route_index 按启用域过滤视图注入）→ kb/ 厚方法论手册与弹药。

## 运行时路由链路

评分公式（features/标签 ×3 > task_type 命中 +10 > 角色偏好 +5 > keywords ×2）每次落 skill.routed 审计；kb hints 中文 2-gram 段落级索引；测试点 Top-5 注入 + route_lookup 按需查，命中技能正文不整段进 system（控上下文膨胀）。

## 角色与规则链

roles yaml 字段全是软边界（skills/task_types 为 null=不过滤），缺角色静默回退 _generalist，role_exists 防拼错放行；规则链 redlines（硬红线）/ owners（授权边界，按资产 owner tag）/ rating（判级口径）/ role-rules 分层注入，resolve_rule_profiles 三态解析。

## 提案制治理

Agent 永不直写 packs，全部经治理通道：统一提案落 packs/.proposals/ 人审应用（只认 decided_by=human）；受控写通道（pack_write_lock / .history 同秒避让备份 / trash 可恢复 / 1 MiB 上限 / 防穿越校验）；K6 成功链沉淀人审闸门防自投毒。

## 改名联动与体检

refs.py kb 改名全库引用扫描+重写；doctor 体检（error：悬空引用/未注册 task_type；warning：route_index 三类体检/rating 无 owners 配套；info：孤儿技能）零 error 才健康，K7 零命中统计在 API 端点后处理。

# 七、LLM 接入层（core/llm）

## 协议统一

内部传输协议统一 Anthropic /v1/messages，标准库 urllib 零 SDK；三个网络注入面（Transport/StreamTransport/HttpGetter）使整个 core/llm 测试零触网。

## anthropic_compat

协议转换核心：重试矩阵（{429,5xx} 最多 3 次线性退避）、thinking/stream 400 实例级降级（去参重发）、SSE 状态机（工具参数 JSON 分片拼装）、首帧后不重试（SSE 不可重放）、truncated 截断防御（区分「说完」与「流断」）。

## ProviderStore

供应商注册表（config/providers.json）：PUT 全量校验（至少一个启用供应商、default 必须命中启用项）、api_key 空串=沿用原 key、masked() 出参永不回显明文（前端只见 has_key 布尔）；真实 key 传播面压缩到文件+内存+请求头三处。

## ModelRouter

planner/executor/classifier 三角色→模型映射（executor 稳定性>智力、classifier 小模型摊成本）；config/llm.json 文件级覆写（不上 UI）；model_context → 上下文字符预算换算（tokens×2 中英混合启发），未声明用 256K 缺省。

## 横切

分层语义化错误（ProviderError 配置面 / LLMError 调用面带 status/body/truncated）；无 key 转 503 可恢复语义（非 500）；record_llm_usage 用量记账。

# 八、工具体系（tools/ + config/mcp.json）

## MCP 接入

外部服务型工具经 config/mcp.json 注册（FOFA 资产测绘等），domains 校验白名单；禁写密钥（FOFA 账号轮换在 src-strike 侧 .env）。

## 反编译双通道

统一抽象 core/tools/decompiler.py：IDA headless 优先 → Ghidra analyzeHeadless 兜底（按 sha256 缓存全量导出，export_version=3 契约）+ IDA GUI MCP 实时桥（只连 loopback，13337 人手实例）+ 按需拉起 IdaMcpManager（无窗口 idat、13338+ 空闲端口、锁检测不抢 GUI 库、LRU 回收）；全不可用退纯静态并出 DECOMPILE_GUIDANCE 安装引导。

## 双向写回

func_kb→.i64（apply_names 后台 Job，锁检测结构化降级）与 .i64→func_kb（pull-names 库内重导不删库，防 GUI 手改名丢失，diff 后非自动名才入 name_history）。

## 工具目录 tools/

decompiler/{ida,ghidra} 脚本区 + mcp/ida-pro-mcp 适配区；地址一律 hex 字符串出 API（JS Number 无法安全表示 64 位地址）。

# 九、情报系统（core/intel）

## 存储层

intel.db 全局 SQLite 刻意「降配」（单连接+写锁，按低流量负载选型，不照抄黑板并发模型）；迁移按 PRAGMA 实测列集幂等 ALTER，不按版本号分支（9515da9 教训）。

## 抓取层

NVD/CISA KEV/GHSA + RSS 纯函数抓取（RawGetter 注入，测试零触网）；POC 启发式判定放在数据入口完成（官方 Exploit 标签 > 域名白名单 > 跨源证据合并，宁缺毋滥）；平台自身可信出站不经执行网关。

## 打分与合成

LLM（classifier 小模型路由）三处降级链（LLM→规则/模板→放弃），情报功能永不上 503；upsert 只升不降（has_poc 证据只增不减）、已打分永不重评；简报 prompt 带硬约束（无 POC 证据的 CVE 不得出现、不编造）。

## vault 接入

Obsidian 只读圣域，隐私红线三重落实：出参剥正文只回 snippet、LLM 只消费元数据、测试断言兜底——笔记正文永不出本机；请求路径零 FS 访问（目录穿越架构上不可能）。

## 周计划与 API 面

16 端点，触发式生命律动（数据新鲜度由用户到访驱动，全站唯一零轮询主视图）；写口与 packs 配置同锁同备份；缺 LLM 降级不缺功能（与黑板层「无 key 503」哲学相反：情报是锦上添花）。

# 十、WebUI（webui）

## 应用骨架

无路由双帧布局：View 状态机 union type 替代路由库 + needsProject 门控 + CustomEvent 导航总线（focus nonce 支持重复触发）；顶栏 5s 轮询作数据心跳，404 自动退项目列表。

## 数据获取与实时层

api.ts 唯一 fetch 出口（约 130 端点方法，ApiError 结构化错误）；useEvents 模块级手写 query cache——REST tail + WS 增量（since_id 游标）、100ms 批量 flush、回入水合截断 300 条、缓存 2000 条上限裁旧。

## 事件渲染管线

四层分工：筛选/样式层（eventStyle 纯映射）→ 布局层（EventRow 五形态）→ 装配层（命令对配对/中断去重/流式终稿遮蔽/对话轮 turn 分组）→ 圈定层（页签×类型两步串联）。DB 行是真相、事件流是有损视图（页签状态灯取数 DB 优先）。

## 视图组织与重画布

React.lazy 隔离 @xyflow 重依赖（画布永不进主包）；布局算法抽纯函数模型（canvasModel 泳道+重心 / boardModel 五列 packing / flowModel DAG 分列），组件只消费 place 槽位。

## 状态管理哲学

无状态库；useState+props 下行、CustomEvent 上行、模块级缓存跨实例；轮询为底 WS 为翼（WS 断了照常用），单 WS 连接多消费者（TaskFlow 用 wsBump 去抖重拉）。

## 页签与设置页

会话页签状态灯；对话轮渲染（human_note 开 turn / 过程折叠组 / 终稿 MarkdownView 不渲染 raw HTML 防注入）；设置页（LLM 供应商 / MCP / 情报源 / 技能矩阵 / 规则 / 提案 / doctor）。

## 设计系统与工程纪律

克制黑客风（深灰 oklch+青色强调，仅深色主题）；状态语义色收敛 --status-* 六变量（Tailwind v4 括号简写坑已固化约定）；横切唯一出口（datetime.ts 时间 / roles.ts 角色显示名）；竞态守卫 let alive=true 模式。

# 十一、逆向领域集成

## 逆向工作台

research+binary 项目自动切 rev-generic profile：黑板侧栏换逆向挂机侧栏 RevCompact；伪码只读展示、函数浏览器（函数/字符串 tab）、库级「IDA 打开」（detached 启动 GUI）、精确跳地址。

## 调试脚本档

x64dbg 下断 / Cheat Engine Lua 纯前端模板（零插件不触网），日志人工导出后回流为动态验证凭据。

## 三层数据纪律

客观全量层（headless 导出缓存）/ 主观分析层 / 产物分离；样本 untrusted；headless 解析经网关审计定性 trusted（只解析不执行样本）。

## 蓝图数据底座

blueprints 表（schema v17）与 store CRUD/事件已落；逆向开发管线（blueprint-driven reconstruction）为已立项方向，实施后回写。

# 十二、工程化与运维

## 测试

全量 pytest（700+ 用例），`PYTHONIOENCODING=utf-8` 约定；fake runner/transport 注入模式（网关/LLM/情报测试零触网零实进程）。

## 启动/停机

启动平台.bat（UTF-8 + CRLF，chcp 65001 自举兼容 GBK 控制台；端口已监听则跳过，首次自动 npm install，就绪后开浏览器）；停止平台.bat（先优雅停机 POST /api/admin/shutdown——在跑会话落任务现场快照，等端口释放最多 15s，超时才按端口硬杀）。

## 文档制度

DESIGN.md 功能导向唯一真相源（本文档）+ 各大目录中文 CLAUDE.md（≤80 行，实质修改同步更新）+ docs/plans/ 方案目录（一方案一文件，不写方案不挂账，实施后回写本文）。
