# cyberstrike-pro 设计文档

> **形态**：功能导向参考手册（2026-09-21 重构）。一级标题=大架构系统，二级标题=功能，每功能一两句话速览；功能下的**展开区**（实现细节、关键坑）渐进填充中。
> **边界**：只写**已实施**功能；未实施方向在 `docs/plans/` 方案文档里，实施落地后才按功能回写本文。历史推导与旧版全文见 git 历史。
> **地位**：当前功能的唯一真相源，与各目录 `CLAUDE.md` 不冲突；硬约束速记见「一、平台总览」。

# 一、平台总览

## 项目定位

AI 驱动的全能安全平台：能力包 × 场景轨正交架构覆盖 CTF 解题 / 渗透测试 SRC / 红队行动 / 逆向 Pwn / IoT 工控车联网研究 / 恶意样本分析；同一套方法论跨场景复用。核心差异点=原生 Windows 优先，按能力自动降级 Docker/WSL2。

## 总体架构

core API（127.0.0.1:8420，FastAPI）+ WebUI（生产=同源静态托管于 8420 的「/」，开发期走 Vite dev 5173）+ SQLite(WAL) 黑板单写入口 + LLM 三角色路由；多个 AI 会话与 WebUI/CLI 是平等的 API 消费者，协调由服务端保证。服务起停：`启动平台.bat`（有 dist 走静态同源，无产物回退 dev）或「启动平台（窗口）.bat」桌面窗口（`serve.py --window`，见下节）；`停止平台.bat` 优雅停机。

## 部署形态（桌面壳 + 静态托管，desktop-app-shell M1+M2，2026-09-21 实施）

三形态并存（窗口与打包解耦是主轴）：无窗浏览器（现状）/ 源码窗口（开发测试即用）/ exe 打包（M3 后置）。
- **M1 窗口壳 `serve.py --window`**：pywebview(WebView2) 窗口 + 子线程 uvicorn，主线程 GUI 循环。**owner/attach 双语义**：启动探测 127.0.0.1:<port>——未在跑=owner（本进程拉服务，最后一窗 closed 事件 → 进程内直置 `server.should_exit` 优雅停机，shutdown 钩子落会话快照，不走 HTTP）；已在跑=attach 附窗（不建服务只开窗，关窗仅退本进程；二次双击「启动平台（窗口）.bat」= 再开一窗连已有服务，多窗并行）。**加载地址探测**：`--url` 显式 > `webui/dist` 存在 → 静态版 8420 > Vite dev 5173 可达（IPv6 ::1 探测）→ dev 版 > 都不满足 → 窗内指引占位页（内嵌 HTML）。`--debug` 右键开 DevTools；cwd 兜底 `os.chdir(项目根)`（frozen 分支为 M3 预留）；pythonw（无控制台）下 stdout/stderr 重定向 `serve-window.log`；WebView2 运行时注册表探测，缺失弹窗提示装一次 + 回退无窗模式（pywebview 未装同理）。
- **M2 静态托管（共同地基）**：`create_app(static_dir="webui/dist")` 挂 **SPA 同源静态托管**——兜底路由注册序最后（全部 API/WS/docs 先匹配不遮蔽）：真实文件直出（resolve 防穿越）、其余 GET 回 index.html（history fallback）；`/api`、`/docs`、`/openapi.json` 例外 404 不兜底（未知 API 调试不被坑）。前端本就走相对路径（`/api/*` + `location.host` 拼 WS），同源后**零改动**；Vite dev 代理仅开发期保留（vite.config.ts）。「启动平台（窗口）.bat」用 pythonw 起窗（无控制台黑窗）。
- **M3 打包（后置另排）**：干净 venv 只装 requirements + PyInstaller onedir，资源随包（webui/dist、packs/、config/ 种子；workspaces/ 首启自建）；未签名 exe SmartScreen 首次「仍要运行」。

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

17 张表，SCHEMA_VERSION=20，每版迁移幂等（PRAGMA 检缺列 ALTER / IF NOT EXISTS，绝不破坏性改）。去重：findings/func_kb 用 UNIQUE 指纹、任务用 dedup_fp，重复写走合并而非新增；含 blueprints（v17 蓝图）与 http_history（v15 浏览器抓包）扩展表；v19 增 chains.origin（manual/trace）与 chain_links.trace_ref（轨迹物化幂等键）两列，存量行 DEFAULT 兜底；v20 增 findings.impact/remediation（收录格式三件套，v19→v20——方案原文写 v18→v19，v19 已被轨迹链占用故顺延）。

## 单一写入口

黑板是全项目写存储的唯一入口，API 层也只调这里、禁止旁路直写；schema 边界纪律（如 orchestrator_state 编排列只经 core/orchestrator/state.py 白名单读写）。

## 并发模型

WAL + 线程局部连接 + `_tx()`（进程内写锁 + BEGIN IMMEDIATE）串行化写 + close_all 关闭闸门（专治 Windows 删项目时 WS/轮询线程重开连接锁死）。单进程设计，多进程需换 Postgres。

## 任务队列

一窗一任务（v0.72，公共池退役）：claim_next SQL 三参数正交收窄（target_session 指派 / only_task 绑定 / assigned_only），归属锁死在 SQL 侧。租约双轨（任务 30min TTL 心跳续租 + 资源 X/S 锁）、wait_for 等待-唤醒与死锁环检测、发布闸（dedup_fp 去重 / 同 target 防碎 / acceptance 对账硬拦）。

## 业务语义层

发现门禁（info 全类别拒收、verified 无复现证据拒〔repro_steps 新口径或旧 POC legacy 直通，has_repro_evidence add/patch 共用〕、rating_basis 随 severity 就高覆盖——「无证据不下结论」写进存储层）；合并=证据并集；撤回传播（四类目标+私信）；TOCTOU 防护（读-合并-写整体单事务 + 乐观锁 CAS）；资产状态回流（tested_clean 才算挖完）、high_value 标记。

## 漏洞收录格式（finding-report-format，2026-09-22 实施）

每条 vuln 发现自带三件套，渲染为报告「逐发现详情」节，报告工程师零脑补：**危害描述（impact）/ 复现步骤（evidence.repro_steps）/ 修复建议（remediation）**——结构化存储（findings 新列 impact/remediation，schema v20；repro_steps=`[{desc 必填, type: http|python|cmd|image, code, expected 复现自证锚点, artifact_id（image 步必填）, stability, target}]`，cmd 渲染 bash 围栏、image 嵌图片产物），不做 Markdown 大字段。**verified 门禁新口径**（仅 pentest/redteam 轨）：repro_steps 至少一步 code/artifact_id 非空且该步 expected 非空，或旧结构 poc/pocs/poc_artifact_id legacy 直通（`has_repro_evidence` add/patch 共用；`validate_repro_steps` 全轨写入口坏结构宁拒不存）；**impact/remediation 不进门禁**（写入放行，报告侧才校验齐备；合并旧值非空保留、空缺由新报补入，patch None=不动/空串=清空）。渲染器三处共用：发现详情弹窗 / 一键复制文稿（三节 Markdown 模板，空节「（待补充）」）/ 报告逐发现详情（M3 随 pentest-phased-workflow M4）。旧数据读时兼容：repro_steps 优先，旧 pocs 映射等价步骤；repro_steps 进证据并集（内容指纹去重追加，重复上报不堆步）。边界：vuln_class 词表 / CWE 映射 / 聚合去重 / 报送流转不混入。

## 链路与全景图

chains/chain_links 攻击链（假设→验证→利用）+ board_graph 黑板全景图（5 类节点 9 种边，全来自既有字段，只读聚合）；全景 chain 边只取 origin='manual'——任务轨迹自动链（origin='trace'）不进全景图（R6 防御过滤）。

## 执行轨迹链路（execution-trace-chain，2026-09-22 实施）

双轨制（traces.py）：**轨 A 轨迹视图（build_task_trace，查询时现算零写入）**——会话事件按 events.id 时间序以 `task.claimed → task.done/failed` 区间切分归属（区间外=游离段 idle）；区间内聚合四类节点：技能（每任务一节点，未命中记灰显反例）/ 知识库（同 module 归并+次数）/ 工具组（相邻间隔 ≤120s 并组，组不跨任务与窗口边界）/ 发现产出（区间内 finding.new，富化标题状态）。**轨 B 持久链（materialize_task_trace，双触发物化）**——任务收尾 done（`_finish` 钩子，try/except 不挡收尾）与发现 verified（patch_finding / add_finding 合并分支）两路径重物化；每任务一链（origin='trace'），node_type=task/step/finding，node_id=`{task_id}#{kind}:{value}` 机器可读；trace_ref=`task-<task_id>` 幂等键单事务 DELETE+重插，人工补挂链边（trace_ref=''）零感知保留；链状态只升不降（hypothesis<validated<exploited）。**沉淀（effect_stats）**：基于物化侧统计 (skill × kb 模块) 组合 × verified finding 计数（打法效果榜）。API 面：GET /trace/{task_id}（404=任务不存在）与 GET /trace-effect。前端（R5/R7）：任务详情内嵌执行轨迹段（TaskFlow 详情浮卡 + 任务看板认领过任务 🧭 徽章，不做项目级独立 tab）；全景图同页切换档「全景 | 主线」——主线=过滤子图三列（目标资产 → verified 发现 → exploited 链，unverified/FP 不上主线）+ 底部效果榜 top5。

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

人类指令经 `_pending_directives` 以「⚠ 人类指令（最高优先）」注入编排器态势，轮末落 directive.done 留痕。**2026-09-21 对话化编排器上线后人机入口由对话窗替代**：`POST /orchestrator/directive` API 标 deprecated 兼容保留，tick 内指令消费机制不变（对话轮不消费指令）。

## 对话化编排器（M1-M3，2026-09-21）

编排器专家化=给脸不重构：派单主循环（tick + 门控/配额/优先级）原样保留，对话是插队轮。

- **M1 对话通道**：`POST /orchestrator/chat` 同步抢 tick 租约（busy 409 不排队，前端不禁用输入只提示）→ 人类消息落 events `orch.chat {role:"human",text[:2000]}`（author=human）→ Job `orchestrator-chat` → `chat_turn()` LLM 工具循环（ORCH_TOOLS 与 tick 全闸门同源：dedup/gate/L0 提案/L1 审批；发布/开窗动作留 tool_trace）→ 回复落 `orch.chat {role:"orch",text[:2000],tool_trace}`。**无 tool_calls 即 break**（纯文本=回答完毕，与 tick 催促调工具的退出语义不同）；步数上限 `CHAT_MAX_STEPS=8`；每 LLM 步 heartbeat 续租；记账 source="orchestrator-chat"。对话历史 = events orch.chat 最近 40 条单一来源（human→user / orch→assistant，开头连续 orch 跳过），零新表零文件。
- **插队轮只读边界**：不推进 event_cursor（态势走 `_overview_for_chat` 固定最近 100 条事件窗）、不计 cycles、不动 last_digest_cycle、不消费 C2 指令（不标 directive.done）、不发饿死告警、不写 state_saver——巡检节奏状态零触碰。
- **M2 goal 闭环**：`meta.phase_goal={text, criteria[]（人话验收口径，非机读）, phase?, source:"chat", created_at, confirmed_by}`；`GET/PUT /goal`（text 空=剥键=清空重议）；`goal.confirm`/`goal.clear` 事件留痕（payload 全文快照，变更历史可回放）；goal 段「当前阶段目标（人类确认）」注入 tick 与对话轮系统提示；meta_loader 实时读 project.json meta（确认即生效，无需重启）。
- **M3 虚拟单例专家**：`GET /api/experts` 恒追加 `{id:"orchestrator", kind:"virtual", protected:true}`（不入 experts/\*.yaml 文件池、不认领任务不执行命令、删不掉；带 pid 时 name 读 meta）；`PUT /orchestrator/persona` 写 `meta.orchestrator_persona`（display_name 贯穿页签/气泡，不注入提示；persona 只注入对话轮系统提示「## 你的身份」段，tick 决策语气不受影响）；专家池消费点（组队/管理面板/建项多选）按 kind=virtual 过滤。
- **前端编排页签三段式**：顶部 goal 条（引导设定 / 展示 text+criteria+phase，编辑/清空弹层）→ 中部对话流（OrchChatPane：human 右气泡 / orch 左气泡 MarkdownView + tool_trace 折叠，busy 显思考行，接近底部才自动跟随）→ 「运行记录」折叠区（原事件流剔除 orch.chat，防对话重复渲染）。composer 编排器态改双态「与编排对话（orch，缺省）/发任务」，**C2 指令态退役**；「决策」筛选含 goal.confirm/goal.clear。
- **M4 异常订阅唤醒后置**（黑板异常时编排器主动开口）未实施，见方案文档。

## 分阶段工作流（pentest-phased-workflow M1+M2，2026-09-22）

渗透测试项目三阶段推进：信息收集 recon ⇄ 渗透测试 pentest → 报告 report（可逆流转）。**重心+配额软引导，入场门是唯一硬约束**（方案 docs/plans/pentest-phased-workflow.md，实施修正注记见其 §0）。当前仅 pentest 轨配剧本。

- **阶段剧本**：`packs/tracks/<track>/phases/<phase>.yaml`（文件名 stem=阶段 id，`name` 中文显示；项目 `config.phases` 同名条目整体覆写、坏条目加载侧跳过不走 doctor）。字段 `name/goal/order/focus`（重心配额）`/gate`（min_assets / min_high_value / min_verified / idle_rounds）`/gate_types/tasks`（task_type/role/objective/acceptance）`/next`。解析为受约束 yaml 子集（零 PyYAML，0/2 空格缩进 + tasks 项 4 空格续键）；doctor 体检 7 码（phase-bad-yaml / phase-tasktype-unregistered / phase-expert-missing / phase-next-missing / phase-field-missing / phase-gate-unknown-key / phase-gatetype-unregistered）。
- **引擎 `core/phases.py`**：load_track_phases / gate_metrics / evaluate_gate / enter_phase 等原语；不 import core.orchestrator（其反向依赖本模块），空闲轮数由调用方读 `orchestrator_state.derive_idle_rounds`（schema v21）传入。阶段状态存项目 meta（project.json 单一真相源）：`current_phase / phase_history / playbook_fired / gate_open_notified`。
- **重心配额注入**：当前阶段 focus 按 task_type 权重拼「## 阶段工作流」段（阶段目标 / 配额建议 / 门进度）注入编排器态势（`_phase_section`），纯软引导——任何阶段可发任何任务类型。
- **入场门（双层拦截）**：当前阶段 gate 未过且 task_type ∈ 该阶段 `gate_types` → 编排器派单侧拒收（`_phase_gate_reject`，拒绝原因回 tool_result）+ 发布 API 422（detail 带未达标明细）；认领侧不动。判定纯确定性；非 idle 指标全达标=过门，未达标但 idle_rounds 达标=收集饱和逃生。gate_types 每阶段自声明（recon=[exploit]、pentest/report 无）——若全局拦 exploit 会鸡生蛋（exploit 正是 verified 的生产者）。
- **过门分流（自主档）**：API 层 `_post_tick` 开头 `_phase_gate_check`（先于挡位闸，L0 也计；轮内 published→idle 计数归零否则 +1）：L0 落事件 `phase.gate_open` / L1 提审批单 `op=phase_transition`（pending 去重，批准即流转 by=approval）/ L2 自动流转（by=orchestrator）。set_gate_notified 抵达校准：进入新阶段时其门已过则标记已分流，防人工回退后被 L2 秒弹回；人工流转抵达剥键。
- **流转 API**：`GET /projects/{pid}/phase`（enabled/current/spec/gate 进度/history）+ `POST /projects/{pid}/phase`（人工最终，不强制门但限 spec.next 内）；方向以 order 定：前向（递增）走门与自动分流，回退（递减）不走门只留痕。事件 `phase.changed` 留痕。
- **剧本首发**：显式流转进入阶段时 tasks[] 原样发布（`created_by=playbook`、acceptance→判据、**noise=passive**——阶段引导 objective 级不占 active 互斥键、priority=1）；`playbook_fired` 按（阶段，指纹）去重——回退重进不重发，同款已在队（find_dedup_target）=吸收指纹。**建项只登记初始阶段不直发**（`publish=False`；mission/目标商议前不发静态任务），首发随显式流转触发；读侧 current_spec 回落首阶段，门拦截与重心注入不依赖登记。
- **M3 专家与内容 / M4 前端阶段条后置**，见方案 §6。

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

能力包（web/binary/crypto/forensics/misc）=方法论技能（怎么干）；场景轨（ctf/pentest/redteam/research，单选）=规则+任务类型（什么语境）；平台/格式/漏洞类只走 frontmatter 标签不建包。LEGACY_TRACK_MAP 读取层兼容旧轨名。**kb 知识库全局单根 `packs/kb/`（M0，2026-09-21 定稿并实施，源自 expert-pool 方案）**：一级=能力域，原 `capabilities/<cap>/kb/` 原样搬迁（不翻译不就地改）；kb 源按启用域合成恒递归（`load_kb_sources`，kb_sources.json 登记退役），kb_open module 用全局形态 `<域>/<快照>/<路径>`，上游 LICENSE 收编 `kb/licenses/`。**能力包隐退为知识组织单位（M2，2026-09-21）**：项目不再显式多选包，改为绑定专家（project.json `experts` 字段，无绑定=存量直通 meta.capabilities 零翻译）；运行时知识可见范围由 `caps_effective(packs_root, track, experts, fallback)` 推导——绑定专家技能并集（经轨变体）∪ 轨技能中 capability 类技能的所属包集合，任一全量专家（skills=null）=capabilities/ 全集，绑定专家 yaml 全缺=空面宁严勿松；推导只发生在 API meta 响应层与 AgentSession 构造层，盘上原始绑定不动（Project.capabilities 恒返回盘上值）。**M3 起创建页 caps 多选 UI 退役**（前端仅剩存量项目只读展示与门控兜底，RECOMMENDED_CAPS 保留此用途）。

## 四层知识体系

SKILL.md 薄路由入口 → kb/route.json 任务导航表（**M0 留域内** `packs/kb/<cap>/route.json`，值带域前缀）→ `packs/kb/route_index.yaml` 全局单表测试点索引（**M0**：条目 kb 全局形态，load_route_index 按启用域过滤视图注入）→ kb/ 厚方法论手册与弹药。

**Android 子域与 K6 案例沉淀（2026-09-21 实施，源自 android-kb-sourcing 方案 M1+M3）**：binary 域新增 `android/` 子域（r0re 收编改编，上游无 LICENSE 文件经用户确认可直接收编）——4 篇手册 `triage-and-layering` / `native-five-lines` / `unpacking` / `godot` + index + cases/；binary 第 4 技能 `android-rev`（file_features=`is_apk/has_native_lib/has_jni/godot_engine/packed_so`——实施期核实路由匹配语义：分诊 Agent 声明走 `file_features` 查询参数，只匹配技能 frontmatter `file_features` 字段，故特征词须落该字段而非 `features`〔情景特征〕，挂 reverse-analyst〔research〕/reverse〔ctf〕两专家白名单，caps_effective 域面不变）；route_index 全局单表增 4 条（binary 域 9→13）。**K6 案例格式约定**：已解案例沉淀 `<域>/<子域>/cases/<案例id>/`，五段式（识别 markers / 解题路径 / 验证向量 / solver / 复用提示），verified 攻击链经人审闸门沉淀防自投毒——与既有 case 提案（测试包 `成功案例.md` 段 + `payloads/` 弹药）并存：kb 树内 cases/ 是方法论文档形态，首例 godot-sec2026（flag_algo.py 正反向向量自测通过）。

## 运行时路由链路

评分公式（features/标签 ×3 > task_type 命中 +10 > 角色偏好 +5 > keywords ×2）每次落 skill.routed 审计；kb hints 中文 2-gram 段落级索引；测试点 Top-5 注入 + route_lookup 按需查，命中技能正文不整段进 system（控上下文膨胀）。

## 角色与规则链

角色（M2 起实现为专家，见下节）字段全是软边界（skills/task_types 为 null=不过滤），缺专家静默回退 _generalist，expert_exists 防拼错放行；规则链 redlines（硬红线）/ owners（授权边界，按资产 owner tag）/ rating（判级口径）/ role-rules（按专家 id 匹配）分层注入，resolve_rule_profiles 三态解析。

## 专家池与项目绑定（M1-M4）

**专家池 `packs/experts/<id>.yaml`（M1，2026-09-21 定稿并实施，源自 expert-pool 方案）**：16 个扁平单文件（23 角色按方案 §4.3 映射合并——4 对 pentest/redteam 镜像合并走轨变体、_generalist 四合一、ctf/recon 独立为 triage 专家），字段与角色 yaml 全兼容，新增 tracks（可服务轨域，缺省=全轨）/protected（仅 _generalist）/variant_<track>_*（轨变体字段级覆写，零解析器改动）。`load_expert(root, name, track)` 加载时按当前轨应用前缀键覆写、产出与角色同形状 dict（下游 system 组装零改动），缺失回退 _generalist；`expert_exists`=池内存在且 track∈tracks（发布链路校验，判存在勿用 load_expert——静默回退会放行拼错 id）；`list_experts(track)` 轨过滤清单（组队 UI 数据源）。doctor 专家体检段：skills 归属存在（error）/已禁用（warning）/跨包重名归属不唯一（warning）、task_type 越各轨注册表并集（error）、tracks 与 variant_ 前缀轨名合法性（error）、expert-name-missing（warning）、缺 _generalist 兜底（error）、role-rules 悬空（warning，专家/角色双源核对）。

**M2 项目绑定（2026-09-21 实施）**：运行时唯一角色源=experts/，`packs/tracks/*/roles/`（23 yaml）退役删除（roles.py 模块保留供 `_parse_inline_value` 复用与历史脚本）。项目绑定专家：创建（`experts` 字段，池外/轨外 422 提示可用池）与换将（`PATCH /api/projects/{pid}/experts`，空清单剥键恢复存量直通态；即时生效于下轮会话构造，黑板行无此列、单一真相源 project.json）。API meta 响应层装饰补 `experts` + 推导 `capabilities`（`_expert_meta_view`）；轨 roles 读端点数据源切 `list_experts`（PackRole 兼容形状），角色写端点退役返 410。发布/开窗 role 值域=**绑定专家清单优先**（`allowed_roles`，宁严勿松），未绑定=按轨全池；构造链把 caps_effective 推导面与 allowed_roles 贯穿 AgentSession→ToolDispatcher（认领即换装经 expert_exists 校验，绑窗角色失效回退 _generalist）。

**M3 专家 CRUD 与组队 UI（2026-09-21 实施）**：`/api/experts` 五端点（GET 全池 / GET 单个 / POST / PUT / DELETE）——**全字段提交式覆写**（表单即最终态，None/空=不落键，skills 缺键=全量专家语义）；写前置校验 doctor 同口径前移（id 强制 ASCII slug `[a-z0-9][a-z0-9-]{0,47}`、tracks 合法轨、skills 注册表存在、task_types 越各轨注册表并集、variants 轨名+字段名 `[a-z_]+`、max_steps 正整数，非法 422）；重名 409、protected 拒删 409、缺失 404；写三件套（pack_write_lock 临界区 + _pack_history_backup + _trash_move）。variants 在 UI 只读展示，yaml 里以 `variant_<轨>_<字段>` 平铺键人工维护。前端（§12）：创建页 **caps 多选退役→专家多选**（按轨过滤分组「通用/轨专属」、搜索、persona 一行预览、默认预选 _generalist；空选=按轨全池直通）、场景档 chips 预填、知识继承折叠区；列表/顶栏徽章 bindingBadge 改「轨 · 专家×N」；直播间编排器态 👥组队弹层（轨过滤多选，保存回执带推导能力包只读徽章）；设置页「角色」tab 换「专家」ExpertsPane（全池管理 + HistoryButton）。

**M4 场景档 / 知识继承 / 看板默认视图（2026-09-21 实施定稿）**：
- **M4a 场景档 `packs/tracks/<track>/profiles/<id>.yaml`**：平铺五件套 experts（组队预设）/rule_profiles_owners+rule_profiles_rating（F11 两键）/playbook/artifacts/knowledge（后三者为 D6 预留字段）+ board_view（M4c）。**D6 物化即弃**——建项选中档即一次性物化：experts→meta.experts、rule_profiles/board_view→config 立即生效、playbook/artifacts/knowledge 随 `config.profile` 快照留档（{id,name,playbook,artifacts,knowledge,materialized_at}），此后档文件只作建项预设、不随项目联动（改档不改存量项目）。档提供缺省、请求体显式值优先（experts/config 显式给出时不兜底）。每轨内置 0~3 档（当前 8 个：ctf full-squad/solo-generalist、pentest standard-engagement/recon-first、redteam full-chain/initial-access、research rev-workbench/code-audit）；`GET /api/tracks/{track}/profiles` 只读清单；doctor 增 profile-expert-missing（组队引用不存在或轨外，error）/ profile-board-view-unknown（warning）。
- **M4b 知识继承**：POST /projects body `inherit_from=<源项目 id>`，源预检 404 先于建项；`ProjectStore.inherit_knowledge` 三段复制**只增不覆盖**——binary 资产（find_asset 同 sha 判重，样本文件 copy2 到目标 samples/、meta.path 更新+`inherited_from` 标记、author 沿源）、func_kb（(project,sha,address) 判重）、蓝图（(project,sha,name) 判重，非 draft 状态尽力保留）；源项目只读不动；继承失败不阻建项（log.warning + 响应 `inherit_error`），成功响应带 `inherited={binaries,func_kb,blueprints,skipped}`。UI=创建页折叠区（源项目下拉）。
- **M4c 看板默认视图**：`config.board_view={"default": "findings|assets|funcs|board"}`（值域 BOARD_VIEWS）；档物化或 PATCH config 透传；前端 Blackboard 受控 tabs，defaultView 不在可用集合（compact 无 board、无 binary 无 funcs）时回退 findings。

## 提案制治理

Agent 永不直写 packs，全部经治理通道：统一提案落 packs/.proposals/ 人审应用（只认 decided_by=human）；受控写通道（pack_write_lock / .history 同秒避让备份 / trash 可恢复 / 1 MiB 上限 / 防穿越校验）；K6 成功链沉淀人审闸门防自投毒。

## 改名联动与体检

refs.py kb 改名全库引用扫描+重写；doctor 体检（error：悬空引用/未注册 task_type；warning：route_index 三类体检/rating 无 owners 配套；info：孤儿技能；**M1 专家池段**：skills 归属/跨包唯一、task_type 越轨并集、tracks 合法性、role-rules 悬空——experts/ 缺省整段跳过）零 error 才健康，K7 零命中统计在 API 端点后处理。

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

React.lazy 隔离 @xyflow 重依赖（画布永不进主包）；布局算法抽纯函数模型（canvasModel 泳道+重心 / boardModel 五列 packing / flowModel DAG 分列），组件只消费 place 槽位；黑板全景图同页切换档「全景 | 主线」（主线=DOM 三列过滤子图+效果榜，不进 xyflow）。

## 链路画布单向零重叠（findings-canvas-dag-layout，2026-09-22 实施）

发现 tab「链路」子视图定稿（用户四拍板 D1-D4 + 设计决策 R1-R4，方案归档 `docs/plans/归档-已完成/`）：

- **单向三保证**：列=距链头最长路径层（链头最左、层递增，恒向右）；**弱边只画右向**（col(target)≤col(source) 不画，升级关系信息详情页仍可查）；链分量保持货架堆叠（D2）。
- **边不穿卡（三路由正交）**：`adjacent` 层差=1（源右缘出→列间中缝 28px 垂直拐→目标左缘入，同行退化直线）；`long` 层差>1（水平长跑 y=longRunY「中间列全空」行带中心，构造上不穿卡）；`cross` 跨泳道（y=所有泳道最大高度+24 底绕行带——中间泳道可能更高，取全泳道 max）。垂直段全在列缝走廊内，同走廊多边 ±3px micro-offset 阶梯。**实施形态=单边多拐点正交 path**（BaseEdge 任意 d 串，无 elbow 虚拟节点/拆段边/编组；走廊 x 渲染端从实测锚点算，无模型几何假设）。
- **行分配**：保留前驱同行优先（层差=1 同行=水平直线一一对照）；同层多节点按前继行均值升序（一轮类重心，逐层交错排序→放置，无已布前驱排层尾）。
- **交互**：边注记**默认隐藏**，hover/选中该边才显（落点=最长段中点，瞬态遮挡可接受）；**拖拽锁定**（布局恒为算法产物，offsets localStorage 与重置钮退役）；MiniMap 默认收起可展开（localStorage 记忆）；聚焦开关/边三态/F13 孤立折叠不变。

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
