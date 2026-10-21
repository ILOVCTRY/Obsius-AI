# cyberstrike-pro 设计文档

> **形态**：功能导向参考手册（2026-09-21 重构）。一级标题=大架构系统，二级标题=功能，每功能一两句话速览；功能下的**展开区**（实现细节、关键坑）渐进填充中。
> **边界**：只写**已实施**功能；未实施方向在 `docs/plans/` 方案文档里，实施落地后才按功能回写本文。历史推导与旧版全文见 git 历史。
> **地位**：当前功能的唯一真相源，与各目录 `CLAUDE.md` 不冲突；硬约束速记见「一、平台总览」。

# 一、平台总览

## 项目定位

AI 驱动的全能安全平台：场景轨 × 知识方向架构（2026-10-13 能力包退役，技能扁平化）覆盖 CTF 解题 / 渗透测试 SRC / 红队行动 / 逆向 Pwn / IoT 工控车联网研究 / 恶意样本分析；同一套方法论跨场景复用。核心差异点=原生 Windows 优先，按能力自动降级 Docker/WSL2。

## 总体架构

core API（127.0.0.1:8420，FastAPI）+ WebUI（生产=同源静态托管于 8420 的「/」，开发期走 Vite dev 5173）+ SQLite(WAL) 黑板单写入口 + LLM 三角色路由；多个 AI 会话与 WebUI/CLI 是平等的 API 消费者，协调由服务端保证。服务起停：`启动平台.bat`（有 dist 走静态同源，无产物回退 dev）或「启动平台（窗口）.bat」桌面窗口（`serve.py --window`，见下节）；`停止平台.bat` 优雅停机。

## 部署形态（桌面壳 + 静态托管 + 打包，desktop-app-shell M1-M3 全量实施）

三形态并存（窗口与打包解耦是主轴）：无窗浏览器（现状）/ 源码窗口（开发测试即用）/ exe 打包（M3，2026-09-23 实施）。
- **M1 窗口壳 `serve.py --window`**：pywebview(WebView2) 窗口 + 子线程 uvicorn，主线程 GUI 循环。**owner/attach 双语义**：启动探测 127.0.0.1:<port>——未在跑=owner（本进程拉服务，最后一窗 closed 事件 → 进程内直置 `server.should_exit` 优雅停机，shutdown 钩子落会话快照，不走 HTTP）；已在跑=attach 附窗（不建服务只开窗，关窗仅退本进程；二次双击「启动平台（窗口）.bat」= 再开一窗连已有服务，多窗并行）。**加载地址探测**：`--url` 显式 > `webui/dist` 存在 → 静态版 8420 > Vite dev 5173 可达（IPv6 ::1 探测）→ dev 版 > 都不满足 → 窗内指引占位页（内嵌 HTML）。`--debug` 右键开 DevTools；cwd 兜底 `os.chdir(项目根)`；pythonw（无控制台）下 stdout/stderr 重定向 `logs/serve-window.log`（`_redirect_stdio_if_none`，frozen windowed exe 同样适配）；WebView2 运行时注册表探测，缺失弹窗提示装一次 + 回退无窗模式（pywebview 未装同理）。
- **M2 静态托管（共同地基）**：`create_app(static_dir="webui/dist")` 挂 **SPA 同源静态托管**——兜底路由注册序最后（全部 API/WS/docs 先匹配不遮蔽）：真实文件直出（resolve 防穿越）、其余 GET 回 index.html（history fallback）；`/api`、`/docs`、`/openapi.json` 例外 404 不兜底（未知 API 调试不被坑）。前端本就走相对路径（`/api/*` + `location.host` 拼 WS），同源后**零改动**；Vite dev 代理仅开发期保留（vite.config.ts）。「启动平台（窗口）.bat」用 pythonw 起窗（无控制台黑窗）。
- **M3 打包（2026-09-23 实施；2026-10-05 改 onefile 单 exe）**：`scripts/build_exe.py` 四步一键——①ensure_frontend（dist 缺失自动 npm install+build）；②ensure_venv 干净 `.build-venv` 只装 8 个 BUILD_DEPS（不打 Miniconda 全家；**fastapi/uvicorn 钉版** ==0.116.1/==0.35.0——fastapi 0.12x 移除 Starlette add_event_handler 会打出坏包）；③PyInstaller **onefile 单 exe**+windowed（根目录 cyberstrike-pro.spec；hiddenimports 兜底 collect webview/clr_loader/pythonnet；excludes playwright/tkinter/pytest）；④copy_resources 把 packs/、tools/、webui/dist **旁挂到 exe 同目录**（排 .history/__pycache__/kb-backups）。产物 `dist/cyberstrike-pro/`：**单个 `cyberstrike-pro.exe`（Python 运行时与依赖全内嵌，运行时自解压到 %TEMP%\\_MEIxxxxx，退出清理）** + 同级 packs/tools/webui 资源目录；双击 exe 即弹窗（**frozen 强制 window 模式**，`_ROOT`=exe 目录 cwd 相对路径全部命中，语义与 onedir 时一致）。**资源不进 exe**（55MB packs 进包会拖慢每次启动且 packs 运行时会被写）。**config/、workspaces/ 不随包**：首启自建（providers 种子/intel 目录/项目库），**敏感凭据（providers key / fofa.json）绝不进包**。未签名 exe SmartScreen 首次「仍要运行」。

## 技术栈

Python 3.11+ / FastAPI、React + TypeScript + Vite SPA、SQLite (WAL)（可平移 Postgres）、火山引擎 Ark 优先（OpenAI 兼容基类，内部协议统一 Anthropic /v1/messages）。

## 目录结构

`core/`（核心引擎：agent / blackboard / orchestrator / **team（Team/Member/Run 执行单元）** / runtime / skills / llm / tools / api / intel）、`packs/`（capabilities 能力包 × tracks 场景轨）、`tools/`（反编译脚本区 + MCP 适配）、`webui/`（React SPA）、`workspaces/`（项目数据目录，**顶层契约=只允许项目目录 + `.trash/` + CLAUDE.md**）、`data/`（全局运行时数据归口：campaign.db）、`logs/`（服务日志归口：serve-window.log + archive/ 历史散落日志）、`config/`、`scripts/`、`docs/`。

## 顶层目录契约与卫生体检（workspace-hygiene，2026-09-23 实施）

**目录归位（D1/D2）**：全局战役记忆库 `workspaces/campaign.db` → `data/campaign.db`（`_migrate_legacy_db` 默认构造惰性迁移：主库 rename 先行、失败降级留老位置下次启动重试〔shutdown 僵尸场景天然自愈〕，主库成功后 -wal/-shm 尽力而为绝不回退——老位置无主库时回退会 sqlite 新建空库丢数据；app 工厂传 workspace_root 同级 data/，测试传 tmp workspace 保持 hermetic）；serve 窗口模式日志 → `logs/serve-window.log`；workspaces 根四份历史散落日志一次性归档 `logs/archive/`。**遗物收编（D3/D4）**：项目根 `artifacts/` 早期调试遗物（曾被 git 误跟踪）mv 进 `workspaces/.trash/artifacts-legacy-<ts>/` + `git rm --cached` 解跟踪，re1_probe 空壳 ×5 同收；`.gitignore` 补 `data/`/`logs/`/`artifacts/`（防遗物再入库，恢复=从 .trash 手动移回）。**膨胀感知（D6）**：`GET /api/workspace-hygiene` 纯只读三段体检——workspaces 根陌生条目（无 project.json 目录/散文件，白名单 .trash/CLAUDE.md，迁移降级残留从这可见）/ 各项目 `.tmp/scratch/spill/browser-profile` 超阈（100MB warning、500MB error）/ .trash 规模；只报告不处置（处置入口=已有 scratch/clear 与手动移回）。**项目回收站（2026-10-10）**：项目删除=整目录 rename 进 `workspaces/.trash/<slug>-<UTC>/`（不物理删除），前端左导航「回收站」页一键查看/恢复——`GET /api/projects/trash`（每项带 `trash_name`=目录名=恢复句柄）+ `POST /api/projects/{trash_name}/restore`（反向 rename 移回，同名自动加 `-2/-3` 后缀、项目 name/id 不变）。**明确不做**：自动删除/自动老化任何产物、.trash 自动清空、回收站彻底删除、存量项目内部布局治理（归 task-workspace 方案域）。

## 开发硬约束

黑板所有写操作必须经 core API 单一入口，禁止旁路直写存储；不可信代码只允许在 Docker 容器执行（WSL 信任级=宿主机）；安全默认值宁严勿松（未知样本默认按恶意处理，L3 沙箱 + fakenet）；Agent 无裸 shell，一切命令经 `run(cmd, runtime)` 网关 + 服务端策略校验。

# 二、智能体系统（core/agent）

## 双循环同构

执行轮（Team 成员经 `run_team_execution(ExecutionContext)` 驱动，2026-10-06 起）与对话轮共用同一条单步流水线（可中断 LLM 调用 + 工具分发 + 上下文裁剪），差异全在纪律（工具面/步数/现场持久化/退出语义）而非结构；会话即窗口。

## 单步流水线

每步按序过：persona 热换装 → 步号预算提醒 → 卡死检测（12 步无进展先机械预检活跃探索=静默延长≤2 次，否则召唤 planner 顾问，第 2 轮交顾问裁决，第 3 轮硬闸停轮，见下；阈值 2026-09-24 由 8 调为 12）→ 可中断 LLM 调用（即点即停 + thinking/回复双流式 + 截断整轮重试 + 中断落盘）→ 记账审计 → 工具串行分发 → 每步落盘现场文件 → 控制点。步数耗尽走预算暂停（保持现场等人工续跑），宁停不丢现场。

## 会话状态单一容器（session-state 收敛，2026-10-03 实施）

任务与闸门状态（finished/awaiting_human/plan_only_mode/closing_round/current_task_id/last_progress_step/delegation_* + reject_streak/plan_gate_count/stuck_waves/stuck_extensions/cadence_*/resume_state/live_state/salvage_ctx/stale_alerted）原先散落 `AgentSession` 与 `ToolDispatcher` 两处，复位点分散在 `_loop_body` 开头十余行与 `reset_closing()`——历史上反复踩「上一任务残留字段带进新任务」（stale `finished` 误 fail、`_resume_state` 泄漏被下轮快照消费分支清盘）。收敛为 `core/agent/session_state.py:SessionState` 单一实例 + `reset_for_task()` 单入口复位；两持有者各自以 `property`（`state_proxy(attr)` 工厂）**保留原属性名**代理到共享实例，`AgentSession.__init__` 建实例后注入 `dispatcher._state`。**边界**：只收敛存取，控制流与闸门评估顺序一字未动（隐式时序是最大风险源）；`reset_for_task` 只列原先真被复位的字段，`current_task_id`/`last_progress_step`/`resume_state`/`live_state`/`salvage_ctx`/`stale_alerted` 由各自生命周期路径（收尾/中断/异常）管理。

## token 分层计数（tokenizer，2026-10-03 实施）

上下文预算此前用 `sum(len(json.dumps(m)))`（纯字符数）当近似——中英混写下偏差可达 2-3 倍，同一个 `context_char_budget` 对英文会话偏松（该压不压、真撞供应商上下文墙）、对中文会话偏紧（过早压缩丢细节）。改为 `core/llm/tokenizer.py` 按模型分层：**OpenAI 系**（模型名前缀可映射 encoding）走 **tiktoken 真 BPE 精确**；**Anthropic / Ark / GLM / DeepSeek**（无公开 tokenizer）走 `HeuristicCounter` 加权估算（CJK 1 token/字、ASCII 4 字符/token、其余 2 字符/token，外加每消息/每工具固定开销）；两者皆不可用时退 `CharCounter`（迁移前口径，保证不抛）。消费点是 `AgentSession._count_tokens`（`_trim` / `_maybe_summarize` / `llm.compact` 事件全走它）。

**口径（关键）**：计数器返回**等价字符数**（token×2），不是 token 数——`context_char_budget` / `context_summary_chars` 的语义与取值（默认 120k/60k、`apply_context_budget` 的 `tokens×2` 换算）是既有契约且被 7 处测试钉死，单位改成 token 会连带改掉全部阈值语义。故**阈值一个字未动，升级的只是「这段历史值多少」的算法**。`tiktoken` 落可选 extra `tokens`（惰性 import，未装静默降级估算器）。`calibrate(samples)` 用历史 `usage.input_tokens` 回归核查估算偏差。

## 卡死升级与收尾收敛（stuck-convergence M1 + D7 + D9，2026-09-24 实施）

**D9 活跃探索静默延长（同日定稿实施）**：观察窗到期且 **waves=0**（顾问未介入）时不立即叫顾问，先零 LLM 成本机械预检 `_active_exploration`：取窗 (lps,step] 内本会话 command/file.read 事件，命中任一信号即**静默延长**（last_progress_step=step、extensions+1、落 `agent.stuck_extend` 事件），不叫顾问、不注入。信号口径保守：①**新文件**——读到本会话此前未读过的路径；②**命令演进**——窗内命令 n≥2、去重 ≥2、最高重复 ≤n/2（半数以上同一条=打转；预检在步开始执行，窗内动作最多 stuck_after−1 个，下限取 2）。延长上限 2 次，用满后走原顾问链；夹一次真黑板进展，extensions 与 waves 一并清零。有界时间线：真打转 12→24→36 步链不变；活跃长任务 12/24 步不打断 → 36 步 suggest → 48 步 verdict → 60 步硬闸（planner ≤2 次、断路器不拆）。配套：command 事件补 step 字段（gateway.run 透传）、read_file 成功落 `file.read` 轻事件（只记路径/步号不存内容）；js-reverse 手册补「长程逆向留痕纪律」（先粗 task_plan、中间脚本即 artifact——artifact 按内容指纹去重自动算进展）。

**D7 顾问裁决（D1 修订，同日定稿实施）**：D1 初版第 2 轮卡死是**机械终止**；当日修订为——第 2 轮卡死改调顾问**裁决模式**（`advisor.verdict`，与自由文本建议模式 `advisor.suggest` 分立）：输入=上次建议全文 + 之后实际命令（tail 窗）+「自第 N 步零黑板写入」硬事实 + kb 召回；结构化三选一 `terminate`/`continue`/`human`。terminate 走原停轮落点（带裁决理由）；continue 的新指令以**半强制**注入（要求本轮先回应如何执行/为何不适用，不锁工具面）、重开 12 步窗（waves=2）；human=顾问自认信息不足直接挂起。**硬闸后移不拆除**：continue 后再满窗（第 3 轮，累计 36 步无写入）→ 无条件机械终止（trigger=hard_backstop），planner 全程 ≤2 次调用；裁决解析/调用失败一律回落终止（宁严勿松，回落即机械停轮）。裁决事件带 decision/reason/wave，升级事件带 trigger（advisor_terminate/hard_backstop/mechanical）。

**D1 卡死波次升级**：顾问干预重置观察窗时记 `_stuck_waves`（每任务复位，夹一次黑板进展即清零）；初版同任务第 2 轮卡死机械停轮（同日由上方 D7 取代——第 2 轮交顾问裁决、第 3 轮硬闸）。**D2 顾问视野**：`_advisor_prompt` 注入「最近重复命令」排行（本会话 command 事件、命令原文精确计数 ×≥2、降序 cap 10），收敛性判断交 LLM，**不自研相似度/归一化算法**。**D6 收尾确认轮**：reconcile 闸之后 complete 首次申报被拦截，注入 findings/assets/artifacts + plan 完成度清单问「对照目标还有遗漏吗」；一轮零新增黑板写入后再次 complete 才落 done（零新增机械计数、非模型自报），确认轮封顶 2、全程落 `agent.closing_confirm` 事件；**干尾巴启发式**——complete 时最近 `stuck_after−2`（=10）步零新增直接放行，常态任务零额外成本；阈值与 D1 的 12 步观察窗错位（干满 12 步先撞 D1，10–11 步申报走干尾巴），两判定无竞态。与验收条目正交：reconcile 管真伪、D6 管遗漏。**D10 顾问参数项目级配置（2026-09-24 定稿实施，方案 advisor-settings-ui）**：卡死/顾问四参数下放到 `project.config.advisor` 段（经现有 PATCH config 整段替换写入、双写，不新增端点），归一化在 `core/autonomy.py:normalize_advisor`（非法值→422）。schema：`stuck_after`（缺省 12，6-30）、`stuck_max_extensions`（D9 静默延长上限，缺省 2，0-4，0=关闭静默延长）、`closing_max_rounds`（D6 收尾确认轮，缺省 2，0-3，**0=首次 complete 申报即放行**——放行分支在 `_closing_gate` round==0 干尾巴判定之前，pathway=cap_zero）、`provider`/`model`（顾问模型项目级覆写，缺省=跟随全局 planner，model 须与 provider 同时出现）。**工厂构造期消费**：全部 6 个建窗点经 `_registered_session_factory` 内层 `factory()` 读取——**在跑会话不生效，下次新建会话窗生效**。planner 覆写必须在 factory() 内层：外层会污染 Orchestrator 自身的 plan_llm；覆写同时作用于顾问建议/顾问裁决/收尾复盘三个消费点，Orchestrator 编排器自身恒用外层 plan_llm。保存时不硬校验供应商存在；供应商事后被删/停用致 `ProviderStore.build` 失败 → 记 warning 静默回退全局 planner，绝不开窗失败。**明确不做**：顾问总开关（D8 撤回决定不变；waves≥2 机械硬闸不可关闭、不可调）、暴露硬闸阈值与 dry_tail（由 stuck_after−2 派生）。**明确不做**：任务中途零新增退出（中途退出权归 stuck/预算）、停轮自动重派、编排器方向粒度去重。M2（D3 跨窗方向指纹）待排期。**事件窗方向修复（2026-09-24，真实事故 task-97e1d5d6c597 复盘）**：`recent_events(limit=N)` since_id=0 取**最早** N 条——长会话（113 事件）升级时 recent_commands/重复排行/顾问 digest 全看到会话开头的 spill 调试，人工误判卡点；四处（`_command_repeat_stats`/`_recent_command_summary`/`_advisor_prompt`/sediment digest）与 bb_query 的 events 查询统一改 `tail=N`（升序返回，下游口径不变）。

## 注入语义（订阅声明表）

黑板收件箱事件的注入口径唯一真相源是模块级订阅声明表 `_INBOX_SUBSCRIPTIONS`（kind → 消费场景：认领期/快照恢复期/步边界/空闲对话轮），四处消费点统一走 `_drain_inbox(scenario)` 单一入口；未登记 kind 滞留收件箱红点可见，绝不静默吞。注入只发生在步边界，与审计事件天然对账。

## 现场持久化与恢复

四层：内存态 → 任务现场文件 task-<tid>.json（每步落盘）→ 会话暂停快照 → 任务键快照（跨会话认领即复活）。恢复按优先级（会话快照 → 任务键快照 → transcript 末 60 条接手 → 全新），装载前 sanitize 清悬空 tool_use 半对防 API 拒。**续跑原窗就近接手（resume-origin-window，2026-09-23 实施）**：resume 端点三级瀑布——原窗存活（非 closed）即原窗接手，两种 mode 皆然（snapshot=revive 快照+reopen+claim 断点复活，budget +200 兼容；transcript=reopen+清延续闸门+worker 首轮认领零搬运，任务现场文件归任务所有不随窗走）；仅原窗已关才新建 armed 任务窗。

## 收尾路径五分支

finish / 异常穿出（停心跳→salvage 抢救→fail，幂等）/ awaiting_human（快照+fail(resumable)）/ 步数耗尽（预算暂停保持 claimed）/ 人工中断（当前步作废+快照保留）——全部收敛到幂等。**停轮保护双来源（orchestrator-efficiency M1+搭车项，2026-09-22）**：①**E2 拒绝熔断（2026-09-24 口径重构，plan-gate-breaker-refine）**——拒绝分两类、计数单位从「回执张数」改为「**模型步**」（并行批多张拒绝票只计 1，模型始终拿到下一轮改道机会）：**硬拒绝**（[越界拒绝]/[网关拒绝]/[拒绝]，tools.py `HARD_REJECT_PREFIXES`；[错误]/[冲突] 是业务失败不算）连续 ≥3 个模型步出现即撞闸循环，置 awaiting_human 停轮保护+落 `agent.reject_breaker`，任何不含硬拒绝的步（含纯文本步）清零；**教练类**（[计划闸]）不进硬熔断——同一任务累计 2 个模型步撞计划闸 → 注入强提示 + 工具面临时收缩到计划/控制原语（`agent.plan_nudge`），强提示后下一模型步末计划仍空 → 挂人（`agent.plan_gate_block`），计划落黑板即清状态。配套三项：`skill_open` 纳入计划前放行（读手册非动手）；新增非 shell 的工作区只读检索工具 **`search_files`**（正则/子串，替代计划前 run_cmd grep——修掉「bb_query 溢出落盘提示教模型 run_cmd、计划闸又禁 run_cmd」的自相矛盾）；run_cmd 被闸挡回时照落 tool.call 审计（payload `gated=true`，此前这类拒绝在事件流完全隐形）。拒绝死循环不再靠烧完 max_steps=200 兜底；②**LLM 传输层失败**——LLMError（429/5xx 重试 2 次耗尽；连接类重置 4 次耗尽——2026-10-01 按类别，见 llm/CLAUDE.md）不 salvage（提炼也要调 LLM 必炸），现场快照双写会话键+任务键、fail(awaiting_human, resumable)，与人工续跑链路（C6/会话续跑）无缝衔接；failed 恒可续跑语义不变。spill 落盘带 uuid 后缀（同工具同秒互覆修复，E1）。

## 会话窗对话化

**人工引导降级为纯对话（2026-10-06 任务机制退役）**：空闲窗打字=对话回复（Claude Code 式，chat_max_steps=24），有价值的阶段性结论随手 `bb_add_finding` 入黑板可追溯；`agent_message` 私信（三分类 subkind）走异步收件箱，不做同步对话接力。**task_receipt 子任务回执 / 回执三路径投递（E4）随任务一并退役**（`context.pending_receipts`、`task.receipt_self` 移除）；团队成员的执行由 Team Run 驱动，不再经窗内任务队列认领。

**手动持久压缩 `/compact`（2026-10-06 实施）**：直播间 composer 两态均支持（输入 `/` 浮出候选）。**会话态**：会话对话历史持久化在 `chat-<sid>.json`，`run_chat` 每轮整段装入上下文（读末 60 条、文件滚动 120 条）；此前压缩只在超阈值时自动触发且**只作用于本轮内存 messages**（G3 `_maybe_summarize`），轮末即丢、文件永远全量。新增入口 `POST /api/sessions/{sid}/compact` → `AgentSession.compact_session_chat()`：把旧问答对经 LLM 按九要素压成摘要后**原子重写**文件为「摘要头 + 近 8 条」，后续对话轮从压缩后历史起跑；落 `llm.compact{manual:true}` 事件（前端「🧹 上下文压缩」行）。**指挥态**：对话历史=events 表 `orch.chat`（`_chat_history` 取最近 40 条），入口 `POST /api/projects/{pid}/orchestrator/compact` → `Orchestrator.compact_chat()`：较早消息压成摘要落 `orch.compact{summary,cutoff_id}` 事件，`_chat_history` 只回放 `id>cutoff_id` 并前置摘要。两态均：历史过短 noop 不烧 LLM、摘要失败不动现场、执行/巡检在跑 409。**不动暂停快照 `snapshots/<sid>.json`**（in-flight 现场，压缩会破坏 resume 语义）。

**上下文用量 `/context` + 85% 轮末自动压缩（2026-10-06 实施）**：直播间两态均可输入 `/context` 打开**上下文用量浮层**（分母=供应商 `providers.json` `model_context`，未配置默认 **256K**；显示已用/百分比、system/tools/messages 分段条与 **85% 阈值线**）。`GET /api/sessions/{sid}/context` → `AgentSession.context_usage()`；`GET /api/projects/{pid}/orchestrator/context` → `Orchestrator.context_usage()`——占用优先取最近一次 LLM 调用的真实 input（会话排除摘要调用 `agent-compact`、指挥排除 `orchestrator-compact`），无则按 system+tools+历史估算，三块 breakdown 按真值归一。**自动压缩**：一轮正常结束（会话 `run_chat` / 指挥 `chat_turn`）后若占用 ≥ 窗口 85% → 自动执行持久压缩（与 `/compact` 同语义，事件 `manual:false`），整段吞异常绝不阻断对话轮。窗口分母单一出处 `core/llm/tokenizer.context_window_tokens()`（`DEFAULT_CONTEXT_TOKENS=256_000`，与 `core/agent` 的上下文预算默认同源）。

## 技能命中审计

每次技能路由落 `skill.routed` 审计事件（含未命中，带 route_points），可观测，同时是沉淀飞轮的数据源。

# 三、黑板系统（core/blackboard）

## 数据模型

SCHEMA_VERSION=33，每版迁移幂等（PRAGMA 检缺列 ALTER / IF NOT EXISTS，绝不破坏性改）。去重：findings/func_kb 用 UNIQUE 指纹，重复写走合并而非新增；含 blueprints（v17 蓝图）与 http_history（v15 浏览器抓包）扩展表；v19 增 chains.origin（manual/trace）与 chain_links.trace_ref（轨迹物化幂等键）两列，存量行 DEFAULT 兜底；v20 增 findings.impact/remediation（收录格式三件套，v19→v20——方案原文写 v18→v19，v19 已被轨迹链占用故顺延）；v21 增 orchestrator_state.derive_idle_rounds（阶段门派单空闲轮数）；v22 增 intents 表（渗透链路图意图规划产物，零 ALTER 建表，见「单站攻击链路图」）；**v32 增 Team 执行单元独立表**（teams/team_members/team_runs/team_run_members/execution_audits，不转换旧任务）；**v33 删 tasks 与 resource_leases 表**（任务机制退役 2026-10-06）。

## 单一写入口

黑板是全项目写存储的唯一入口，API 层也只调这里、禁止旁路直写；schema 边界纪律（如 orchestrator_state 编排列只经 core/orchestrator/state.py 白名单读写）。

## 并发模型

WAL + 线程局部连接 + `_tx()`（进程内写锁 + BEGIN IMMEDIATE）串行化写 + close_all 关闭闸门（专治 Windows 删项目时 WS/轮询线程重开连接锁死）。单进程设计，多进程需换 Postgres。

## 执行单元：Team（2026-10-06 任务机制退役）

**旧任务队列整体退役**：schema v33 DROP `tasks` 与 `resource_leases`，`TaskQueue`/`leases.py`/`tasktree.py`/`core/coordination/` 删除，任务端点 / 任务工具 / 调度层移除。执行单元改为 **`core/team`（Team / Member / Run）**——schema v32 独立表 `teams/team_members/team_runs/team_run_members/execution_audits`，由 `core/team/store.py` 独立写入口（不改写黑板旧域）。**创建 Team 只登记 roster**；**start 必须经 preflight revision 乐观锁 + 人工四项确认**（members/goal/safety/execution），再事务建 Run 快照并 fan-out 专属 Agent session。Agent 侧执行入口 `AgentSession.run_team_execution(ExecutionContext)`（`core/agent/execution.py`）——不设置旧 `current_task_id`、不走旧认领/完成工具，命令/文件/黑板仍走原单一写入口与网关、安全边界仍由 role/runtime/ROE/approval/gateway/预算/session cap 提供。详见 [`core/team/CLAUDE.md`](core/team/CLAUDE.md)。

## 独立验证器（independent-verification-audit M1，2026-09-23 实施）

**收口判定独立化**：题目做对了没有，不是 Agent 自己说了算的。验收条目从纯字符串升级为 `str | {text, verify}`（向后兼容零迁移）——带 verify 规格的条目其 met/failed 由服务端验证器自动判定，**Agent 自报被存储层拒绝**（`set_reconcile_state` 红线，只能置 blocked 附不适用理由；人工可审计）。规格受约束 schema（未知键拒绝，仿阶段剧本精神）六策略（core/verify.py，地位仿 phases.py）：flag_capture（file: 工作区相对路径防穿越 / cmd 取输出，exact/contains/regex 三匹配）/ effect_proof（检查清单逐项）/ poc_crash（signal 退出码启发 / asan 特征）/ oracle（脚本输出 `{pass, detail}` JSON 兜底）/ **nonce_exec**（2026-10-19，借鉴 hxbai `engine/verify.py`：服务端发随机 nonce，命令模板必须含 `{nonce}` 占位符，让**目标**算 `sha256(nonce)[:16]` 回显；命中才算打通，只回显 nonce 无哈希判「反射/假回显」——治「模型自称命令执行成功」）/ **differential**（BASELINE 与 PROOF **两次独立 executor 调用**取各自输出，PROOF 独有行（去空、≥4 字符）达 `min_diff` 才算成立；写死「同一段输出里同时出现两段标记」= 伪造形状直接拒——模型无法一手编造两条工具结果）。**执行面**：平台身份 trusted + host + 工作区隔离经执行网关全量策略校验（审计事件照落）；**收尾钩子**：complete 先跑未判定条目（failed 也重跑=「修正后重新 complete 重验」，blocked 跳过），未过 ValueError 拦下本次 done——零新闸复用对账硬拦；逐条 `verify.result` 事件（author=verifier）。**脱敏红线（照抄 VulnHouse VE）**：判定回执只含 passed + 结构化摘要（长度/sha256 前 12），输出原文/预期值/oracle detail 绝不回显——防 Agent 经验证接口反套答案；规格异常/网关拒绝一律 fail-closed 判未过。M2 Auditor / M3 交棒 / M4 重派注入见 docs/plans/independent-verification-audit.md。

## CTF 解题内核（ctf-kernel，2026-10-19 实施）

**来源**：对标开源项目 hxbai（内部名 hxctf，LLM 自主交战引擎，51 个 engine 模块约 15.6k 行）。它是 Linux 单容器 + claude CLI 子进程 + TSG 事件源状态图 + EVSI 预算调度的形态，**整条引擎不可搬**（调度/执行器与我们的会话/Team/黑板三层直接打架）；可搬的是**无形态依赖的判定与抽取内核**。故新建独立包 `core/ctf/`（零黑板依赖、纯 stdlib、可单测），四模块：

| 模块 | 解决的问题 | 接线点 |
|------|-----------|--------|
| `flagjudge.py` | 「这串东西是不是 flag」——候选抽取（剔模板语法/URL 编码）、置信打分（前缀可信 + 内层熵 + 诱饵惩罚）、变换回环、跨题家族 | `ctf_submit_flag` 工具、`_flag_neighbor_owner` |
| `observe.py` | 零 LLM 观测结构化——nmap 行→端口/服务、URL/路径、凭据形态（含 base64/URL 内嵌/字段配对）、salient 要点、易变值归一化 + 观测指纹 | `run_cmd` 回执尾部摘要 |
| `ammo.py` | 凭据弹药管线——产品指纹（需语境才认）→ 厂商默认口令 → 年份/后缀/leet 变异 → `user:pass` 成对 | `ctf_ammo` 工具（写 scratch 候选表） |
| `pacing.py` | 时长无关化——预算随「比赛总时长 × 题量」缩放，clamp [0.25, 3.0]；endgame 收尾窗口 | `ChatTurn._pacing_context()` 注入 system |

**反伪造核心口径**：「模型说自己证明了不算，证据链说了算」。落到三处机械判定：

1. **格式置信**（`flagishness`）——`FLAGISH_FLOOR=2.0`。打分表在 hxbai 原版基础上**补强两行**（原版只看前缀，漏判题面示例最常见的「前缀正常 + 内层占位」形态）：内层是诱饵词（`flag{example}`/`flag{test}`/`flag{fake_flag}`）→ **-5**；内层过短（<3 字符，`flag{x}`）→ **-3**。于是 `flag{example}`=-2（拒）、`flag{x}`=0（拒）、`flag{abc123}`=4（过）。另有同形簇抑制（同前缀 ≥4 个不同内层 = 刷屏假阳性全丢）与内层去重（同内层只留最高分）。
2. **变换回环**（`roundtrip_verify`）——模型声明 `source` + `transform` 时，把 flag **编码回去**检查是否出现在 source 里：真解码必然回环，瞎猜不会。变换名不认识返回 None（不做断言，宁松勿误判）。
3. **跨题防串**（`neighbor_owner`）——内层长度 ≥8 且互为子串 → 判为同族，owner 不是本题则拒。治多题并发时 A 题 flag 出现在 B 题共享目录/上游扫描结果里被当 B 题答案。

**`ctf_submit_flag`（工具，group=黑板，flags=intent_flow）**：CTF 轨 flag 判定与登记的唯一出口。参数 `flag`/`evidence`/`source`/`transform`/`challenge_id`/`candidate`。流程：非 CTF 轨拒 → 从原文抽候选取最高分 → 自然语言内层拒 → 置信 < 阈值拒（提示 `candidate=true` 这条出路）→ 给了 source+transform 时回环 False 拒 → 跨题串号拒 → **人工否决清单**（`is_rejected`：被标过错误 flag 的串连理由一起拒）→ 定归属（显式 `challenge_id` > 线程题目兜底，都无则拒**不编造归属**）→ 正式 flag 走 ① 进 **flag 台账**（`challenge_flags`，`record_flag` 落 `verdict='pending'`，唯一真值）② 同时落一条 `intel/unverified` **过程留痕** finding（不受「正式漏洞五项必填」契约约束——CTF flag 不是漏洞）③ `set_finding_id` 双向关联。`candidate=true` 落 `high/unverified/intel` 且**不进台账**（线索不是答案，进了就会把题目算成 solved）。**本工具不联网提交**，只做本地判定与登记。（语义在 challenge-flag-ledger v47 变更，见下节。）

**`run_cmd` 观测摘要**：CTF 轨工具回执尾部追加 `observe.summarize_output` 结果（`[观测] 端口: …` / `[观测] 凭据 N 组: …（机械抽取，有效性须自行验证）` / `[观测] flag 候选 N 个: …（须核实真伪，正式提交用 ctf_submit_flag）`）。非 CTF 轨、无输出、抽不出信号 → 回执**零变化**；摘要**不写黑板**（写不写仍由模型经 `bb_add_finding` 决定）。

**flag 显式观测段（2026-10-19）**：`observe.extract_flags` 复用 `flagjudge.confident_flags` 的三道闸（自然语言内层 / 诱饵 / 刷屏同形簇全剔）单列一段，**不再让 flag 只当「要点」里的普通一行**——一次 200 行 `strings`/`cat` 输出要点只留 8 行，flag 极易被挤掉，而它是本轨唯一「必须看见」的信号。**只做观测提示，不判定解出**（是否计入题目仍由台账决定）。顺带修了一个真缺陷：`_clean_twin` 原先把 `fake_flag{deadbeef99}` 洗成干净孪生候选 `flag{deadbeef99}`，白送 +3 前缀分绕过三道闸——诱饵前缀现不再参与孪生清洗（中性上下文 `theflag{}`/`picoCTFflag{}` 仍救回）。

**`run_cmd` 原地打转提示（2026-10-19）**：`ToolDispatcher._with_repeat_guard` 在回执尾部按**完全相同**的命令串计数（本会话最近 120 条 `command` 事件，不做归一化/相似度——`?id=1` 与 `?id=2` 是不同尝试，误并会冤枉正常枚举）：≥3 次 `[重复执行]` 轻提示，≥5 次 `[原地打转]` 明说「继续跑不会带来新信息」并引导把否定结论落黑板。**不阻断、不改命令**——模型没有跨轮的精确命令计数器，本函数只补它看不见的事实。动因：`AgentSession._command_repeat_stats` 只喂策略顾问（多步无进展才触发），**对话线程（CTF 主控/子专家）此前零反馈面**，可以对着同一题把同一条 `curl` 跑十几遍。回执出口统一为 `_finish_cmd_receipt`（原地打转 → CTF 观测摘要），顺带修了「环境恢复后的重试回执不带观测摘要」的漏挂。

**`ctf_ammo`（工具，group=执行）**：弱口令题的候选字典生成器。输入 `obs_text`（响应头/banner/nmap）/ `hint_text`（题面线索）/ `users` / `known_creds`，`pairs=true` 给 `user:pass` 行、否则纯口令表；产出**写进 `scratch/ctf-ammo-<sha8>.txt`**（文件名按素材哈希，同素材幂等覆盖）并回候选前 20 条 + 用法示例。**不执行爆破、不发网络请求**——口径同时写在工具描述与回执里（模型读到「已生成」极易误以为「已经打过了」）。候选表恒不含 flag 形态串（防自己泄题）。仅 CTF 轨可用。

**pacing 接线**：`ChatTurn._pacing_context()` 在 CTF 轨把 `Pacing.describe()` 注入 `_challenge_context()`——`- 时间预算（ctf-pacing）：scale=1.00 总时长=6.0h 题量=63 剩余=2.1h；单题硬上限按 150 分钟算`。进 endgame 追加「**已进收尾窗口**——停止开新战线，只做能交的题收尾与 writeup」。配置来源 `config.ctf.{deadline, tasks}`（`deadline` 收 ISO8601 或 epoch 秒，经 `_to_epoch` 归一；解析失败**退回阶梯推断而不猜假时间**），未配置也注入（按本线程 `created_at` 起的已跑时长向上猜赛制）——**不知道赛制不等于没有时间感**。非 CTF 轨返空串。

**CTF 维度清单**：`packs/kb/ctf/dimensions.yaml` 首版四个面——triage（分诊归类）/ analyze（静态分析）/ reproduce（动态验证）/ flag（结论与 flag 验证）。**刻意比渗透轨少得多**：渗透是「一个资产穷举攻击面」，面越细越好；CTF 是「一题一条链路，解出即完成」，切十个面会变成机械凑数（模型被迫为不适用的面写 dead_end）。`applies_to` 只挂 binary/host/service/url，不挂 challenge——题目级一题一线程由主控自己解，维度收敛闸只对「被分配资产的子专家」生效，不必要地约束主控。

**前端体现（2026-10-19 对齐，零 schema 迁移）**：内核改动本身**不新增任何前端数据契约**，落在既有视图上——① **设置页「工具」tab**（`AgentToolsPane.tsx` ← `GET /api/agent-tools`）自动多出 `ctf_submit_flag`（黑板组）/ `ctf_ammo`（执行组）两张工具卡，无需前端改动；② **黑板「意图」tab**（`IntentsPane.tsx`）的「维度 {id}」会显示 ctf 四面 `triage`/`analyze`/`reproduce`/`flag`；③ **诊断页「系统提示词」**（`DiagnosticsView.tsx`）能看到 `_pacing_context()` 注入的 `- 时间预算（ctf-pacing）：scale=… 总时长=… 题量=… 剩余=…` 与 endgame 提示——排查「时间感到底进没进上下文」不必再猜；④ **黑板题目行 🚩 徽章 + 发现详情 FLAG 区块**（`extractFlag`）；⑤ **黑板「CTF-Agent」tab 的「赛制时间」面板**（新增写入口，见下）；⑥ **题目行「编辑」按钮 + 编辑题目表单**（`ChallengeEditDialog`，见下）。④ 本轮**修了两个真缺陷**：`FLAG_RE` 前缀改可选（旧式要求 `flag`/`ctf` 前至少 1 字符，`ctf_submit_flag` 写入的裸 `flag{…}`/`htb{…}` 在 UI 上**完全不显示**，与 `flagjudge` 的候选口径不一致）；`flagByChallenge` 只认 `status==="verified"`（`candidate=true` 的线索也写 `evidence.flag`，不加闸绿色 🚩 会把「疑似线索」冒充成「已解出」），详情弹窗按 `row.status` 分「FLAG（绿）/ 候选 FLAG（警示色 + 说明）」两态。

**`config.ctf` 写入口（2026-10-19）**：pacing 此前只有读路径（`_pacing_context`），配置只能手改 `project.json`。现补 `core/ctf/pacing.py::normalize_ctf_config`（`core/projects.py::update_config` 调用，非法 **422**，空段剥键回落阶梯推断）+ 前端面板（黑板「CTF-Agent」tab →「赛制时间」：截止时间文本框 / 题目总数 / 保存赛制，走 `api.patchProjectConfig(pid, {ctf})`）。**归一化不是可选项**：`{"tasks": "abc"}` 若静默进盘，读路径 `int()` 会吞成 0 再落 `ANCHOR_NTASKS`——配置没生效但人看不出来。`tasks` 收正整数（数字串容忍），`deadline` 收非空字符串（ISO8601）或正数（epoch 秒）。

**编辑题目（challenge-edit，2026-10-19）**：`PATCH /api/challenges/{aid}` 与 `ChallengePatchBody` 早已就绪，但前端**零消费**——题目名打错、`flag_count` 填错（直接决定「拿满几条算解出」）、方向选错，此前只能删掉重建，而删除会**连带删掉该题目的 flag 台账与全部发现**。现补 `ChallengeEditDialog`（题目行「编辑」按钮）：
- **只提交改过的键**（`undefined` = 后端不动该键；空串 = 清该键）。全量提交会让「AI 后来补的 meta」被前端旧快照覆盖回空值。
- **入口点只读**——入口是子资产（`entries` 走资产树），`PATCH` 不接受该字段；表单里展示只读清单并提示「请在题目树里增删」，避免两套写入口打架。
- **`flag_count` 调低到 ≤ 已计入条数 = 题目立刻解出**（实测：quota 3→1 且已有 2 条 accepted 时状态从 `partial` 直接变 `solved`）。这是**合法**操作（「这题其实只要一个 flag」），故**不拦**，但提交前弹一次确认说明后果——否则用户以为只是改个数，实际调度层已不再派 Worker。

## CTF 解题主控（ctf-controller，2026-10-19 实施）

**要解决的问题**：CTF 的失败模式几乎从来不是「不会解」，而是**时间分配错了**——死磕一道已探尽的题到比赛结束，或一次快检没命中就放弃一道再挖十分钟就出的题。这两个判断此前完全交给模型，而模型的可靠性在这里极低：它没有跨轮的精确计数器，「这题我试了 2 轮」和「试了 5 轮」在上下文里长得一模一样，而放弃判据的阈值恰好卡在这些数字上。

**解法：代码调度 + LLM 只定方向。** 新增第四个控制器类智能体 `ctf-controller`（`tracks: [ctf]`，`packs/experts/ctf-controller.yaml`），与现有三个主控的根本区别是**决定权归属**：

| 主控 | LLM 决定 | 代码决定 |
|------|---------|---------|
| `chat-orchestrator` | 派谁、派几批、何时收工 | 只提供并发原语 |
| `aggressive-controller` | 派谁 | 「先返先触发」的并发语义 |
| `artex-controller` | 只操舵 | 派发权全在 Engine（Planner 生成 Intent） |
| **`ctf-controller`** | **往哪走（提方向）** | **准不准派、派几轮、何时放弃** |

**四个内核在 `core/ctf/session.py` 合流**（`CtfSession`）：

| 内核 | 在会话里的角色 |
|------|---------------|
| `scheduler.decide` | 每轮给每个方向一个 verdict（`revive`/`requeue`/`drop` + 人话理由） |
| `lease.ClaimTable` | 同一方向不许两个 worker 同时做（TTL 30min 自动回收） |
| `attest` | worker 声称「拿到执行」→ 强制目标机自证（nonce+sha256） |
| `fold` | 把「flag 台账 + 能力 + 事实」压进 token 预算（四级降级） |

### 放弃决策（`scheduler.py`，移植 hxbai `_revive_policy`）

13 条判据严格自上而下，先命中先返回：轮次用尽（≥8）→ 提示预算耗尽（≥预算 1/3）→ 单题硬上限 → **零产出 hungry 让位（`requeue`）** → 零产出绝对上限（60min）→ 零交付空转 → flag 停滞 → **里程碑续投（`revive`）** → 未到最少轮次（<2）→ 多 flag 部分续投 → α≈0 停滞 → 有界耐心 → 兜底。

**`requeue` ≠ `drop` 是本模块最重要的语义区分**：前者是「这题还有救，但先让别人上」（30 分钟 hungry 让位，还有题没开时的机会成本），后者是「这题没救」。合并它们会让前端与复盘分不清「暂时让位」和「判定死亡」。两个零产出上限**不随赛制缩放**——一小时没 foothold 在任何赛制下都是磨洋工。

### 方向互斥（`lease.py`，移植 hxbai `WorkClaimTable`）

子目标 → 归一方向键 → 单持有者 TTL 租约。**中文方向键是本项目的扩展**（hxbai 只处理 `[a-z0-9.]`，中文串整个被丢掉 → 全部归一成空键 → 空键互相冲突反而把并发锁成 1）：中文段剥噪声词 + 同义词归一（口令/密码/passwd → `pass`）+ 2-gram 残余切分，ASCII 段剥动作词/填充词 + 复数归一（backups→backup），最后排序去重。

**刻意的取舍：只做确定性字面归一，不做模糊合并。** `"probe robots.txt for hidden backup paths"` 与 `"check robots.txt to find backup files"` 会得到两个不同的键——它们确实是同一件事，但把「hidden path」与「file」合并需要语义理解，而任何基于 token 重叠的近似匹配都会顺手把「文件上传绕过 WAF」和「文件上传漏洞」这类**真不同**的方向合并。**误锁死并发比重复劳动贵得多**：重复劳动只浪费一个槽位，误锁死会让一个方向永远没人做。模糊判定留给主控的提示词（它看得见 `render_table()` 的占用表）。

空键（抽不出实义词）**放行而不占用**：把无关工作锁死比重复劳动更糟。

### 目标机自证（`attest.py`，移植 hxbai `verify.make_challenge`）

worker 说「SSTI 打通了，`id` 返回 `uid=0`」——这可能是真的，也可能是它把题面样例抄了一遍，或者在自己机器上跑 `id`。解法是**让目标机自己算一道题**：主控生成随机 nonce（16 字节 hex），期望值 = `sha256(nonce)[:16]`，要求 worker 通过那个执行原语让**目标**计算并返回摘要。

判据分四种拒绝形状（hxbai 只返回 confirmed/inconclusive）：`fabricated_in_assertion`（摘要只在模型自述里）/ `echo_not_execution`（回执里只有 nonce 原文 = 反射）/ `no_target_vector`（有摘要但命令看不到目标载体）/ `wrong_digest`（算了但算错）。**`no_target_vector` 是 inconclusive 而非 reject**——交互式 shell 里跑 `sha256sum` 时命令行确实看不到载体，硬判会误杀真战果。**expected 绝不进给 worker 的提示**（进了就等于把答案给了模型），只进主控侧判定与日志。

### 上下文降级（`fold.py`，移植 hxbai `folded_projection`）

一道题跑两小时会攒下几百条侦察笔记，全塞进提示词会打满窗口、淹没关键结论、烧钱。解法是**按预算逐级降级**：`full` → `compact`（最近一半 + 强信号 + 显著条目补齐）→ `salient`（只留显著，按显著度降序）→ `headline`（每段只留强信号至多 2 条）→ 截断条目长度。

三个本项目特有的口径：
1. **必保段永不降级**（`mandatory=True`）：flag 台账是解题进度唯一真值，砍了主控就忘记自己拿了几分，下一轮重复派工；
2. **强信号分级**（`salience_score` 0/1/2）：凭据/flag/执行 = 2，路径/端口 = 1，普通素材 = 0。二元判断时四十条目录扫描会把唯一一条凭据挤出窗口——降级就白降了；
3. **中文 token 估算**：hxbai 的 `len//4` 是英文口径，中文一字约一 token，按 `len//4` 估会低估 3-4 倍，预算形同虚设。

### 接线（`core/chat/runtime.py`）

- **工具面**（`_CTF_CONTROLLER_TOOLS`）：`ctf_dispatch`（唯一的派发入口，提交方向拿回准派/拦下计划）+ `ctf_review`（代码侧 verdict 表 + 互斥表 + 分诊结论置顶 + 耗时）+ `ctf_inspect`（只读核对：台账证据链原文 / 文件清单+provenance+头部魔数）+ `call_expert` / `todo_write` / `bb_declare_challenge` / `ctf_submit_flag` / `ctf_conclude` / `bb_query` / 知识库 / 意图三件套。**没有任何执行工具**（无 `run_cmd`/`read_file`/`browser_*`/`decompile`）——主控一旦能跑命令就会自己解题，绕过整套调度（与 ARTEX 主控同一事故教训）。`ctf_ammo` 也不给（要 `run_cmd` 配合，它没有）。`ctf_inspect` 是「能核对不能执行」的落实处：没有任意路径读取、不返回全文、不跑命令。
- **越权派发闸**（`_tool_call_expert`）：每条 `call_expert` 必须在本轮 `ctf_dispatch` 的准派计划内。没有这道闸，模型可以跳过 `ctf_dispatch` 直接派工——方向互斥、放弃判据、轮次记账、自证核验全部失效，整套代码调度退化成装饰。文案给出下一步动作（先 dispatch），否则模型会反复重试同一次派发。**闸的判键必须剥掉计划 task 的模板壳**（见下节「键口径一致性」），否则「按计划原样派发」会被自己的闸拦下。
- **轮次记账**（`_tool_call_expert` 尾部）：`passes` 由实际派发推进；`flags_added` 取**台账实际增量**（派发前后各数一次 `accepted_flags`），不信 worker 摘要。读库失败记 0（宁可少算进展，也不要因一次 DB 抖动误判有进展而续投）。每笔记 `chat.ctf_verdict` 事件（`action`/`code`/`reason`/`passes`/`attest_*`）。
- **系统提示**（`_CTF_CONTROLLER_STYLE`）：与另外两个主控文案互斥（它们都强调「你来决定派什么」，本段强调「你只提方向，派不派由代码判」）。不明说这一点，模型会按训练数据里的默认行为绕过 `ctf_dispatch`。
- **题目上下文**（`_challenge_context`）：ctf-controller 分支讲「只定方向 + 怎么登记 flag」，**不讲**「怎么消费 ctf_ammo」——它没有 `run_cmd`，讲那句等于教它调半截工具。
- **启用先返先触发**（`is_first_return_controller` 含 ctf-controller，2026-10-20 修正）：一批派 3 个 `call_expert` 探 3 个方向时，第一个返回就唤醒主控，其余转后台并以 `[后台并发结果]` 在后续步边界补达。旧实现等整批收齐，导致「一个方向 40 秒就证伪，却要等另两个跑满 10 分钟」——期间主控既不能改道也不能收尾，纯空转。
  **旧版「等整批」的理由不成立**（曾据此把 ctf-controller 排除在外）：`record()` 的调用点在 `_tool_call_expert` 尾部，由 `_dispatch(tc)` 在**每个 worker 自己的线程**里执行（`_parallel_expert_dispatch::run_one`），本来就是「单方向完成即记账」，从不依赖整批收齐。提前返回只是主线程不等了，worker 照旧跑完自己的记账与落库。`runtime.py:1215` 当初只是漏了 `or self.is_ctf_controller` 这一项。
  **注入文案必须按轨分叉**（`_drain_probe_overflow`）：原文案写死激进主控的渗透探针纪律（`[命中]→立即派深挖专家；[未命中]→放弃该方向`）。CTF 轨照搬会让主控在「[命中]」后直接 `call_expert` 派下一批，**绕过 `ctf_dispatch` 的准派闸**。现按 `is_ctf_controller` 分叉为「登记 flag / close_intent(dead_end) 后重新 dispatch / ctf_conclude 收尾 / ctf_abandon 放弃」。

### 前端体现

**零新增前端数据契约**（与 ctf-kernel 同口径）：
- **composer 智能体选择器**自动多出「CTF解题主控」（`_chat_agents` 从 `list_experts(packs_root, track)` 枚举，CTF 项目可见）；
- **诊断页「系统提示词」**能看到 `_CTF_CONTROLLER_STYLE` 全段与 `_challenge_context` 的主控分支——排查「代码调度到底进没进上下文」不必再猜；
- **工具卡**（设置页「工具」tab）自动多出 `ctf_dispatch` / `ctf_review` 两张（`GET /api/agent-tools` 从 `AGENT_TOOLS` 枚举，**本主控的两个工具刻意不进 `AGENT_TOOLS`**——它们是会话级调度原语，不是通用工具，故只在主控工具面动态挂载，设置页看不到是预期行为）；
- **事件流**：每笔记账落 `chat.ctf_verdict`，前端可按需渲染「方向 → verdict」时间线（当前无专用视图，事件可在诊断/日志看到）。

### 与既有实现的关系

- **`ctf-controller` 是新增，不改动其余三个主控的任何行为**：`CONTROLLER_IDS` 加一项、`_build_dispatcher`/`_tool_specs` 加一个 `elif` 分支、`_tool_call_expert` 加一段 `if self.is_ctf_controller`。其余分支逐字未动。
- **`ctf_dispatch`/`ctf_review`/`ctf_inspect` 不进 `AGENT_TOOLS`**：它们是轨道语义而非通用工具（与 `record_fact`/trace 三件套同一定位），故走动态挂载 + 专用 `_dispatch` 分支，不经 `ToolDispatcher`。
- **`_ctf_session_for()` 惰性构造**：`Pacing` 要读项目 config 与线程创建时间（有 IO），只有真正调 `ctf_dispatch`/`ctf_review` 时才建。构造失败返回 `None`——调度不可用时主控仍能靠 `call_expert` 干活，不该整轮炸。
- **`ctf_inspect` 的兜底只做读**：它不调 `ToolDispatcher._challenge_fallback_asset()`（那个会**按线程标题补建题目**），只用 `chat_store.resolve_challenge_asset_id()` 沿祖先链解析。核对通道不该有副作用。

**不搬的部分**：11GB 单镜像 / 纯正则 decide / `run_bench` 平台特化 hack / TSG 调度与 EVGI 预算（依赖它的事件源状态图）。

### 键口径一致性（2026-10-20 事故修复）

**现象**：`ctf_dispatch` 明明返回「准派 1 个」并给出 task，模型按计划把 task **原样**喂给 `call_expert`，却被 `[错误] 该方向不在本轮准派计划内` 打回。`ctf-controller` 因此**从未真正跑通过**——计划与执行之间有一道自相矛盾的闸。

**根因**：`_task_text()` 拼给 worker 的 task 是「【方向】X + 【要求】三条模板 + 可选【自证任务】」，而 `allowed()` 直接对整段文本调 `direction_key()`。模板里的「只做这个方向」「结论以 [命中] 开头」「用 ctf_submit_flag 登记（候选线索加 candidate=true）」贡献了 `只/命中/开头/方向/结论/登记/候选/证据/命令/输出` 十来个 token，键必然与 `subgoal` 的键不同。**代码自己生成的 task，自己判越权。**

**三处派生缺陷**（同一个「键口径分叉」，只是表现不同）：

| 位置 | 表现 | 后果 |
|------|------|------|
| `allowed()` | 原样派发必拒 | 主控完全不可用（用户报的就是这个） |
| `record()` | 回执按整段 task 算键 → 查不到原 state → 新建 `passes=0` 的孤儿 | 放弃判据分母永远归零，`drop` 永不触发（**比派不出去更隐蔽：调度看着在跑，实际不记账**） |
| `claims.release()` | 释放时键不同 → 静默失败 | 租约挂到 30min TTL 到期，期间该方向谁都派不出去 |

**修复**：统一入口 `session.key_of(text)` = `direction_key(subgoal_of(text))`，`subgoal_of()` 只剥**代码自己写入的**标记（`【方向】` / `【要求】` / `【自证任务】`），模型自由发挥的文本原样进归一（不引入任何模糊猜测）。`plan` / `allowed` / `record` / `_resolve_key` 四处强制走同一口径；`record` 释放租约改用 `st.subgoal`（建租时那把键），不拿本轮回执文本重算。

**`allowed()` 同时看计划与当前状态**：一轮并发派多个 worker，先返回的那个在 `record()` 里把方向判死时 `_last_plan` 仍是旧的——只查 `plan.keys` 等于「判死了还在派」。`requeue` 必须放行（让位不是判死），只拒 `dropped`/`solved`，与 `plan()` 的拦截口径对齐。

**方向族单向包含放行**：模型回贴计划 task 时常顺手补一句说明（「…，顺便看 LSB」），键变长但仍指同一件事——`plan_key ⊆ 派发键` 放行。**反向不放行**（派发键 ⊆ 计划键 = 砍掉了计划里的关键约束）。判据是确定性集合包含，与 `_shadow_of` 同源，不引入相似度阈值。

**回归必须走真实工具面**：直接 `sess.plan()` + `sess.allowed()` 测不到这个 bug（测试自己传的是纯 subgoal，天然同键）——这正是它全绿逃逸的原因。`test_call_expert_allowed_for_plan_task_verbatim` 从 `_tool_ctf_dispatch()` 取 `_last_plan.granted[0]["task"]` 再喂 `_tool_call_expert()`，复刻模型复制粘贴的真实路径。


## CTF flag 台账（challenge-flag-ledger v47，2026-10-19 实施）

**要解决的问题**：v47 前 flag 是「一条 findings 行」——而 findings 的「正式漏洞五项必填契约」（复现步骤/测试环境/验证结果/风险影响评估/POC）是为**渗透结论**设计的，CTF flag 不是漏洞，只能硬写套话过闸（`risk_assessment` 写「CTF 题目环境内的解题结论，不对应真实业务风险」这类废话）。且「这道题解出来没有」没有单一真值：`solved` 散在 findings 的 status/severity 里靠前端猜。

**台账是 flag 的唯一真值**（`challenge_flags` 表，schema v47，DDL `IF NOT EXISTS` 建表无 ALTER，旧库打开即建）：

| 字段 | 语义 |
|------|------|
| `flag` / `flag_format` / `confidence` | 值 + 题目声明格式（只加权不减）+ 判定置信 |
| `source` / `transform` | 解题路径摘要 + 变换名（回环验证用） |
| `judged_by` | `ai` / `human`——**谁说的算**在这一列 |
| `verdict` | `pending`（AI 判定通过待复核）/ `accepted`（人工确认）/ `rejected`（人工否决） |
| `verdict_reason` / `finding_id` | 否决理由（再提交时连理由一起拒）/ 过程留痕 finding 反查 |

**题目状态是派生值，不独立存储**（`challenge_state`）：`solved` > `partial` > `abandoned`（唯一非派生位——「放弃」是人类意图不是数据事实）> `running` > `untouched`。

**`verdict` 三分与 solved 的关系（2026-10-20 方案 A 修正）**：

| verdict | 语义 | 计入 solved |
|---------|------|-------------|
| `pending` | **候选**：AI 形态判定通过、未经人工确认 | **否** |
| `accepted` | 人工确认有效（强证据） | 是 |
| `rejected` | 人工标记错误 | 否（且拦重复提交） |

`solved = accepted 数 ≥ quota 且无格式冲突`。**`pending` 曾与 `accepted` 同计入 solved**，后果是 AI 提交一条假 flag 就判 solved、调度层立刻停派，**题目被一条假答案提前终止**（用户实测 `flag{E574...}` 与 `DASCTF{6f93...}` 同时入账，显示 2/1「已拿满」）。改法是把「候选」与「已确认」在数据层就分开，而不是靠前端判断。

**`conflict`（同题内前缀互不相同）压过 solved**：`_flag_prefix` 取 `{` 前的前缀（`flag{x}`→`flag`），同题内出现两个不同前缀 → 至少一条是假的 → 状态 `partial` + `conflict` 列表回传，等人工裁决。**判据不能用 `meta.flag_format`**——题面声明的格式是**提示不是约束**（写 `flag{}` 实际 `DASCTF{}` 极常见），拿它比对会误杀真 flag；只用台账内部事实。
**`over_quota`（accepted 数 > quota）不压 solved**：最典型成因是用户主动把 `flag_count` 从 3 改成 1（「这题只要一个」），此时多条前缀相同、都经人确认，判 partial 反而违背用户意图。它只作异常信号回传。

**`ctf_conclude`：主控主动收尾（停派工，但不冒充解出）**。`pending` 不计 solved 后，AI 判定通过只能把题目停在 `partial`——有人值守时正确（人工确认是强证据），但**无人值守通宵跑题会全卡在 pending 白烧预算**：明明解出来了、没人点按钮，调度层就一直派 Worker。故给一个显式收尾出口：`meta.concluded` → `challenge_gate.blocked` 收该题、不再派工，但状态仍是 `partial`、前端显示「已收尾·待确认」。
- 与 `abandoned` 的区别：放弃=「不做了，可能没解出」；收尾=「做完了，等确认」。两者都不派工，语义与徽标不同。
- 与 `solved` 的区别：`concluded` **不参与 solved 派生**。人工后来否决了那条候选，题目退回 partial 且 `concluded` 仍在——刻意的：主控已判过一轮，再派只是重复劳动，重开需人撤销。
- 要求**至少提交过一条 flag**（候选也算）。一条都没有就收尾 = 静默弃题（既不 solved 也不再跑），最坏结局，必须拒并指回 `ctf_submit_flag` / `ctf_abandon` / `close_intent(dead_end)`。

**`verdict='rejected'` 是真闸**：立刻从 solved 派生里扣除（状态自动生效，无需改任何存储），且同串再提交被 `ctf_submit_flag` 的 `is_rejected` 直接拦（连理由一起回执）——否则模型会反复提交同一串，每次白跑一遍 flagjudge + 落一条 finding。

**人工确认 = 通知收工，不是掐断（challenge-confirm-notice，2026-10-20）**：`PATCH …/flags/{fid}` 落 `accepted` 后，向**该项目下所有在跑轮次投递一条通知**（`core/chat/inbox.py::NoticeInbox`，fire-and-forget，不等待）——人点确认就是强证据，题目已解出，还在跑的 Worker 全是重复劳动（用户实测：确认之后调度层照样把剩下的方向派完，白烧十几分钟）。`rejected` **不**投递（题目还没解出，该继续跑）。

**为什么不是置位中止事件**（原 `challenge-confirm-stops` 已推翻）：`abort_event` 会让轮次被掐在半个工具调用里，`_persist_stopped` 只落一句「已按人类要求停止」——模型**没有任何机会写回**手上已识别的结论（解题路径、未登记的证据），这些观察随轮次一起蒸发，用户看到的是「一堆线程突然红了」而不是「主控说这题解出了，我收工」。改为投递后**语义从「外部强制终止」变成「外部告知事实」**：模型在下一个步边界读到「这题已拿到正确 flag，尽快收尾」，自己决定停派方向、写回结论、给一句总结。终止权仍归模型，因为它比外部更清楚自己此刻在哪个方向、拿到了什么。

**投递范围必须按 pid 限定**：`chat_threads` 属于各项目自己的黑板库，通知正文讲的是「哪道题解出了」，投给别的项目的轮次就是错消息（曾误写成遍历 `app.state.projects` 全量）。`ChatTurn` 只在**步边界**取通知（`_take_notices` → `_inject_nudge`），不在流式帧里取——中途改 messages 会让当前这次请求的半截响应失去对应上下文；且必须走 `_inject_nudge` 而非裸 append（步边界尾部通常是 user，再 append 一条就是连续两条 user，Anthropic 严格角色交替会 400）。

**投递面必须覆盖子 agent（challenge-confirm-notice 修正，2026-10-20 晚）**：用户实测「点了确认有效，其它子 agent 还在跑」（截图里确认已落账，子线程仍在 `curl -sL -o libc-2.23.so`）。两处叠加的根因：
- **存活判据错**：旧实现用 `app.state.chat_running` 过滤，而那个集合**只登记顶层轮次**（`_chat_launch_turn`）——`call_expert` spawn 的子专家、ARTEX Worker 都不经过它，DB 里 `status=running` 却被 `if tid in running` 全部静默丢弃。现改查 `core/chat/inbox.py` 的**进程级登记表**：`ChatTurn` 构造即 `box_for(tid)`、`run()` 的 `finally` 释放，所以「有箱」精确等于「这一轮正持有它」。
- **箱子根本没传下去**：`ChatTurn` 有三个构造点，此前只有 `_chat_launch_turn` 传 `notice_inbox`，另两处（`runtime._tool_call_expert`、`artex_worker.make_worker`）的 `_take_notices()` 恒返回空——**子 agent 从来收不到任何通知**。现改为构造时默认自取（显式传 `None` 仍可关掉），三处自动接上；登记表必须放模块级，因为后两个构造点在 `core/chat/` 里跑、**拿不到 `app.state`**。

**范围按 (pid, 题目) 双重限定**：只按 pid 限不够——一个 CTF 项目常并行多题，A 题确认后投给 B 题的 worker 会让它停掉自己在做的**另一道题**（跨项目误投的同一种失败，粒度更细）。归属走 `chat_store.resolve_challenge_asset_id`（子线程沿 `parent_thread_id` 继承，与调度层同口径）；解析不出归属的通用会话仍收（它可能在帮这题做事却没显式绑定）。

**通知文案必须按角色分叉**：主控口径是「停止派新方向（不要再 ctf_dispatch / call_expert）」，而这条通知现在也投给子专家与 Worker——它们手上没有这两个工具。让 worker 去「停止 ctf_dispatch」等于给一条无法执行也无法自检的指令，模型只能猜，常见结果是继续把手上那步跑完（用户看到的「还在跑」）。worker 版讲的是「正在跑的方向不必做完、不要再启动新探测、把中间结论写回、一句话收尾」。

**已解出必须是硬闸，不能只靠通知**（同日）：通知是给模型读的**软**约束，而「派不派方向」本来就在代码判据手里。实测人工确认后主控照样派完剩下方向，白烧十几分钟。故 `_solved_challenge_block()` 在**两个入口**都拦：`ctf_dispatch`（管「提方向」）与 `call_expert`（管「真派出去」，含绕过 dispatch 的直调）。判据取台账派生值 `challenge_state(...).solved`，不看本会话记过什么——人工确认可能发生在任意时刻甚至上一轮。这道闸在 `call_expert` 里**必须排在专家名校验之前**：结论是「这题已无活可干」，与派的是哪个专家无关；排后面时一个拼错的专家名会先被「未知专家」拒掉，模型据此以为换个名字就能派。

**`ctf_submit_flag` 语义变更**：正式 flag ① 进台账（`record_flag`，AI 落 `pending`）② 同时落一条 `intel/unverified` 的**过程留痕** finding（不受五项必填契约约束，因为它是留痕不是结论）③ `set_finding_id` 双向关联。`candidate=true` 的候选线索**不进台账**（线索不是答案），只落 intel finding。回执必须写明「已确认 N/M（候选 K 条待人工确认）」+「仅形态判定通过，未做变换回环」（`transform` 为空时证据强度确实弱一档，不说明模型会当它已验完）。

**API**：`GET /api/challenges/{aid}/flags`（含 rejected——人工裁决界面要看到才能撤销误否决）、`POST …/flags`（人工录入 `judged_by=human`，默认 accepted）、`PATCH …/flags/{fid}`（裁决 pending/accepted/rejected + reason）、`POST …/abandon`（放弃/撤销放弃）。存量 flag 由 `backfill_from_findings` 惰性补登（`vuln_class='flag-capture'` 且 `evidence.flag` 非空）。

**前端体现（2026-10-19 接入，2026-10-20 按方案 A 修正）**：① 题目行 **🚩 台账 N** 按钮 → `ChallengeFlagLedger` 弹层（逐条裁决 / 手工录入 / 放弃这道题）；② 题目行**派生状态徽章**（`ChallengeStateBadge`：已解出/部分解出/求解中/已放弃，多 flag 题显示 `2/3`，**候选另显 `+K待确认`**，`concluded` 另显「已收尾·待确认」，`conflict` 另显红字「格式冲突 N 条」）；③ 题目行 🚩 徽章的取值**改为台账优先**——`flagByChallenge` 先取**已确认**（accepted）的 flag，没有再退回候选（pending），findings 反查降级为兜底（仅 `verified && category=vuln`）。③ 是必须的：台账重构后正式 flag 的留痕 finding 是 `unverified/intel`，若只认 verified 反查，🚩 会**永远不显示**。
④ 台账弹层顶部徽章把「已确认 N/M」「候选 K」「否决 J」「已收尾·待确认」分列，并在「只有候选未确认」时显一条黄色提示（**候选不算解出，调度层仍在派 Worker**）、在 `conflict` 非空时显红字提示（标出冲突的前缀，要求标记掉错的那条）。

**候选 flag 唤醒主控（ctf-flag-wake，2026-10-20）**：候选 flag 落库即**唤醒 CTF 解题主控说一段话**，对话页贴一张「复制 / 确认有效 / 标记为错误」卡。起因是一个真实的空窗：后台并发派发（`executor.shutdown(wait=False)`）让主控先返、worker 后到成为常态，而 `_probe_overflow` 只在本轮步边界 drain，`ChatTurn` 实例一丢就永远补不回来——候选 flag 躺在台账里没人管，用户看不到、主控也不说话。
- **挂载点是事件总线**：订阅 `challenge.flag.recorded`（`verdict=pending` + `judged_by=ai`），不改 `ctf_submit_flag`。`ToolDispatcher` 拿不到也不该拿 `app.state.jobs`/线程表；而 `record_flag` 本来就发这个事件（最短路径、对提交方完全无感）。
- **订阅必须在 `_project()` 里每次幂等补挂**：API 建项目走另一条路径直接塞 `app.state.projects`，只在「首次打开」分支挂会永远挂不上——症状是「手工调唤醒函数有效、真实提交毫无反应」（实测踩过）。
- 四条边界：① 主控 `status=running` **不唤醒**（worker 是同步 spawn，本轮主控自己就会看到；硬塞第二轮撞 409）；② 同一 `flag_id` 只唤醒一次（事件记账 `chat.ctf_flag_wake`，重启后仍有效；幂等更新走 `challenge.flag.updated`，不在白名单）；③ 人工 accepted **不走这里**——那是 `_notify_project_chat_turns` 的地盘，语义是「告诉在跑的那一轮：题解出了，收工」而不是「开一轮新对话解释候选」；④ `judged_by != "ai"` 不唤醒（人工录入不需要 AI 再解释）。
- **唤醒文案必须有反例分支**：三句——flag 怎么来的（解题路径原文）、能不能自证（有 transform = 可回环复算；没有 = **只是形态像**）、请人类点哪个按钮；外加两条硬约束「本轮不调任何工具/不派方向」「候选不计解出，别说已解出」。这是本轮反复踩的同一类坑：给模型看的判断类文案必须由**本次数据事实**触发，无条件拼接的固定说法会与事实相反。
- **找主控线程不猜**：`find_controller_thread()` 优先**未挂父线程的顶层会话**，退一步接受任意绑定该题的 ctf-controller 线程；都不匹配返回 None，**绝不按线程标题猜**（`_challenge_fallback_asset()` 式补建会把唤醒打到错题上）。
- **人类发消息与系统唤醒共用 `_chat_launch_turn()`**（`chat_send` 退化成薄壳）：两处各写一份装配代码迟早分叉，漏传 `artifacts_dir`/`browser_pool` 就是子专家工具全灭（同规则链透传缺口）。
- 前端 `components/chat/CandidateFlagCard.tsx` + `lib/useCandidateFlags.ts`：3s 轮询台账、`wakeKey` 变化即重拉。**卡片数据取台账不取事件载荷**——事件里 flag 被截到 120 字符（`record_flag` 有意为之防事件表膨胀），拿它做「复制」会复制出半截串，比没有更糟。「标记为错误」强制填理由：理由写进台账后同串再提交会被 `is_rejected` 拦下，不填等于让模型反复提交同一串。回归 `tests/test_ctf_flag_wake.py`（9 例）。

### 核对通道与首轮分诊（ctf-controller-hardening，2026-10-20）

三个 P0 缺口来自同一次实战复盘（USBPcap 题）：主控**全程靠 worker 自述判断真假**、3 条方向**盲派且执行上重叠**、产物**落在哪不可寻址**。

**P0-2 `ctf_inspect`：给核对，不给执行**。复盘的核心矛盾是一句话——
> 系统给了主控「必须一手核实」的纪律，却没给它任何核实的工具。

那一轮主控对「这条 flag 是不是误报」的唯一依据是另一个 agent 的**自述摘要**：它没看过 `solve_usb_pcapng.py`、没看过 `out.txt`、没看过 `flag.zip`，于是自述里 `linktype 249` 与 `220` 互相矛盾，它无法发现。**但不能直接给 `read_file`/`run_cmd`**——主控能跑命令就会自己解题（ARTEX 主控事故 `3d4834f`），这是硬红线。
- `what="evidence"`：把台账里每条 flag 的**完整证据链原文**摊开（`source` 解题路径原文、`transform`、置信、判定者、关联 finding 的 `repro_steps`）。**不摘要不评价**——让主控自己比。这些内容本来就在台账里。
- `what="files"`：题目子树资产 + `artifacts/attachments` + `scratch`/`work` 的文件清单 + provenance + **头部 32 字节魔数**。**绝不返回全文**（`_INSPECT_HEAD_BYTES=32`）：够判「这是 zip/图片/流量包」，不够解题。魔数认不出时回「**未知形态**（认不出，别猜——派 worker 看）」，不猜。
- 三条红线即它与 `read_file` 的全部区别：**没有任意路径读取**（只认题目子树 + 三个已知工作区根）、**没有全文**、**没有命令执行**。测试 `test_inspect_files_gives_magic_not_content` 直接断言正文 marker 不出现在回执里。
- 它**不按线程标题补建题目**（`ToolDispatcher._challenge_fallback_asset()` 会补建）：核对通道不该有副作用，只做沿 `parent_thread_id` 的读解析。

**P0-3 产物 provenance 四档**（`_workspace_files_context` + `_file_origin`）：主控不知道哪些是「用户给的原件」、哪些是「上一轮 agent 的产物」，于是从零派 3 条方向，3 个 agent 各写一遍解析器（同一题 3 份重复劳动）。判据全是**路径前缀**（确定性、无猜测）：`artifacts/attachments/`=`[题目原件]`（不可信不可改）、`scratch/`+`work/`=`[上轮产物]`（可复用可覆盖）、`samples/`=`[样本]`、其余=`[未知来源]`。清单每行带 mtime；命中 `_REUSE_SCRIPT_RX`（`solve_*/extract_*/decode_*/parse_*/carve_*/dump_*/recover_*/unpack_*/exp/exploit/poc`）时加硬提示：「清单里有 `[上轮产物]` 的 `solve_*.py` —— 这题很可能已被解过，第一动作应该是派 worker 读并复现它」。

**「已解过」提示必须由清单事实触发**（ctf-inspect-reuse-hint，2026-10-20）：`ctf_inspect(what="files")` 尾部那句「清单里有 `[上轮产物]` 的 `solve_*/extract_*/parse_*` 就说明这题已经解过」原先**无条件**拼接，于是新开项目、新派的题（清单只有一条 `[题目原件]` 的 pcapng）也会被告知「已解过，别再派从零分析」——与事实相反，还会把主控推去「复现」不存在的产物。现在与 `_workspace_files_context` 同口径：遍历时用 `_note_reuse()` 收集「`[上轮产物]` 且文件名命中 `_REUSE_SCRIPT_RX`」的条目，**只有命中才**输出硬提示并点名脚本；**未命中则反向明说**「没有可复用的既有成果，按新题从零分析是正常路径」，堵住模型自行脑补「也许解过」而绕路。回归 `test_inspect_files_does_not_claim_solved_on_new_challenge` / `test_inspect_files_reuse_hint_needs_script_shape`。

**P0-5 无条件首轮分诊**（`session._plan_locked` 的 `triage_blocked`）：3 条方向盲派，第 1 条（基础分诊）与第 3 条（TLS/流重组）**执行上完全重叠**（都走 USB 载体识别 → SCSI LBA 重组 → 解 flag.zip → 解 XOR）。
- **归一算法解决不了这个，也不该去解决**：实测 `pcapng` 只出现在 A 的键里，B 的键是 `tcp tls udp 加密 协议 流 重组 熵`，字面毫无交集。「两条路径会走到同一终点」是**执行后才可知**的事实，任何措辞相似度判据都是在猜（`lease.py` 已为这个理由放弃模糊匹配）。
- 所以不猜重叠，改**编排**：`rounds <= _TRIAGE_MAX_ROUNDS` **且分诊尚未得出结论**时，若整批方向里**没有任何一条**命中 `_TRIAGE_RX`（`分诊|triage|题型|识别/判断/确认+是|魔数|magic|题面|file|载体`），则**整批拦下**并回专门文案（讲清「为什么」+「怎么做：这一轮只提这 1 条」）。
- **闸的判据是「分诊结论存在」，不是「第几轮」**（ctf-triage-retire，2026-10-20 修正）：原实现只看 `rounds <= 3`，于是第 1 轮正确做了分诊、第 2 轮派深挖时 `rounds` 仍是 2 ≤ 3，**闸再次拦下**——用户实测「分诊做完了却还被硬拦」即此。轮次只是安全阀，判据必须是「这件事办完了没有」。
- **用户裁决是无条件**（「明显题目也做分诊」）：分诊成本 30 秒，而「我以为一眼就看出来」恰恰是误判率最高的时刻。`_TRIAGE_MAX_ROUNDS=3` 是**上界**不是「第几轮」——模型死活不听话时 3 轮后放行，闸的作用是纠正一轮，不是把控制器锁死。
- **宽进严出**：只要有一条像分诊，其余深挖方向照常走后续判据（允许一轮里既分诊又试一个明确的面）。
- **被拦下不消耗方向槽位**：不建 `DirectionState`、不占租约——否则纠正一轮就烧掉一个方向。
- 分诊结论回来后 `ctf_review` **置顶**展示（`_triage_summary_locked`），并由 `triage_concluded()` 判定「是否真的知道了载体/格式」：只派出分诊不等于分诊完成，`[未命中] file 命令不存在` 不能解锁深挖；明确的 `pcapng/linktype/ZIP/USB/图片/音频` 或「无附件/纯交互题」才算结论。结论一旦落账，方向状态变为 `retired`（**使命完成，不代表题目已解出**），释放租约并立即解除分诊闸；后续再派同一分诊方向会被拒，避免截图所示的「分诊方向仍 open → 再派廉价续接 → 深挖批次继续被硬拦」。

**P1-1 冲突主动上报**（`_ledger_context`）：两个 agent 都能通过 `bb_query` 看到台账（所以都说「2/1 已满」），但没一个提出「已经有另一个 flag 了，我们冲突了」——因为台账对 worker 是**只读数据**，没有「发现异常要上报」的通道，`conflict` 只在**提交时**触发。故改为每轮 system 注入（不污染消息历史、每轮重投影）：`conflict`→「至少一条是错的」+ 怎么自证；`over_quota`→软提示「配额被下调过」（**不当错误**，那多是用户主动改 `flag_count`）；`concluded`→别再派工 + 怎么撤销；有候选零确认→「补证据升级或继续找，别停在候选上」。**只报异常不报常规进度**——每轮都刷的长文本会被模型当噪音跳过。

**P1-2 耗时记账**：`chat.ctf_verdict` 事件加 `elapsed_s`（该方向累计墙钟，从 `first_at` 算）；`review()` 表格加「耗时」列（`45s`/`12m`/`1.4h`，`_fmt_dur`）+ 末尾「**本题累计**：N 轮派发 / M 次执行 / 时长 / 存活 K 个方向」。**不改任何判据**（时间判据已在 `scheduler` 里用），只让人看得见——机会成本不量化，复盘就没法回答「这题值不值 3 个并发槽位」。回归 `test_elapsed_does_not_change_verdict` 钉死这条边界。

**展示用可读方向名**（`session.dir_label`，ctf-dir-label，2026-10-20）：复核表、派发计划、互斥表、linktype 矛盾、重复产物**一律展示方向原文**，不再展示 `DirectionState.key`。键是 `direction_key()` 的 2-gram 归一串（中文会切出跨词碎片，如「先分诊：file 魔数…」→ `file 先分 关键 分诊 判断 断载 载体 键句 面关 题型 题面 魔数`），模型认不出那是自己刚派过的方向，会当成「另一套版本」重复派工。**键仍保留在 state 里做去重与互斥**，只是不给人看；`ClaimResult.describe(label)` 同理（不传 label 回落键，向后兼容）。

**P1-3 产物路径提权**（`fold.salience_score`）：实测 `'flag.zip 落在 /workspace/scratch/att-abc/flag.zip'` 只算 1（一般显著），降级时被挤掉，而**产物路径是复核的钥匙**。故 `_ARTIFACT_PATH_RX` 认 `/workspace/…` 与 `scratch/`、`work/` 前缀 → `salience=2`（与「凭据=2」同源：可操作的事实，不是过程描述）。**裸相对文件名不算**（否则 `a.py` 也提权，强信号贬值）。

### 跨回执一致性（ctf-cross-observation，2026-10-20，P2）

**要解决的问题**：单条回执里的说法语法上完全合法，无从判断对错；矛盾只在**两条回执并置**时才出现。而 worker 之间互相看不到对方的回执，主控两条都信——于是按两套互相排斥的前提各干一遍。

两类信号，都是**只报不改判据**（`test_p2_signals_do_not_change_verdict` 钉死）：

| 信号 | 抽取 | 判定 | 落点 |
|---|---|---|---|
| **linktype 矛盾** | `observe.parse_linktypes()` | 同一题内出现 ≥2 个不同值 | `review()` 表格**之前**的警示段 |
| **重复产物** | `observe.parse_script_names()` | **≥2 个不同方向**都产出了脚本 | `review()` 累计行之后的警示段 |

**linktype**：复盘里两个 worker 分别报 220 和 249，主控两条都信，按两个不同的链路层布局各解一遍，至少一半白跑。抽取只认**显式声明**（`linktype 220` / `Encapsulation type: … (220)` / `LINKTYPE_USB_LINUX (249)` / `link_type=249`），**不认裸数字**——`0x000000dc` 可能是任意偏移量，误报会把正常回执判成矛盾，而假警报会训练模型忽略真警报。上限 1024 挡住「年份 (2024)」误抽；间隔上限 80 字符（tshark 的标准措辞中间有 46 字符，40 会漏掉最常见那条）。**代码不替模型判谁对**——它不知道哪个值是真的，自动挑一个等于赌；只能告诉它「这里有矛盾，去用 `capinfos`/`tshark -r x -V` 一手复核」。

**重复产物**：复盘里三个 worker 各写了一遍 USB 解析器（`solve_usb_pcapng.py` / `usbpcap_extract.py` / `parse.py`）。**按名字比对抓不到**（三个名字全不一样），**按语义类别也抓不到**（分属 solve/extract/parse 三档，各档各一份）。最终判据退到最朴素的一条：**「≥2 个不同方向都写了脚本」本身就是方向重叠的强信号**——正常情况一个方向写完就解出来了，写脚本说明卡住了，两个方向各写一遍说明在做同一件事。同一方向写三版是正常迭代，不算。**只做事后信号不做拦截**：发现时活已经干完了，价值在两处——事后复盘调 `_TRIAGE_RX` 的覆盖面，以及本轮提示「后续方向先复用，别写第四遍」。

### 证伪收敛（ctf-refute-converge，2026-10-20）

**现象**：某个方向已经被明确判定「这条路不通」，但调度层仍反复给它续投——三个方向在同一测试面上重复派发到预算烧光（用户复盘中「证伪方向不收敛」）。

**根因是一处正则误判**：`[未命中] 0 帧 DNS，该方向证伪` 这类回执里含**「命中」二字**（`未命中` 的后半），旧口径 `_WEAK_PROGRESS_RX.search()` 直接命中 `命中` 分支 → 判成 `weak=True`（有侦察进展）→ `scheduler` 的 ⑫ `bounded_patience` 判据「有弱进展且耐心未耗尽 → 续投」反复放行，**证伪被当成进展，方向永远不收敛**（假性存活）。

**修复分两层**：
1. `session.record()` 新增 `_REFUTED_RX` 并**先于**弱进展匹配：命中即 `refuted=True`、`refuted_reason=...`，且**不置 `weak`**（证伪是结论不是进展）。顺序不能反——两个模式会同时命中同一段文本。
2. `scheduler.decide` 新增判据 **⑧'「已证伪 → drop」**，位置在 **⑨ min_passes 地板之前**：证伪是确定性结论，不是「试得还不够」；放在地板之后就等于「未命中也要再试两轮」。
   - **唯一例外**：`correct > 0` 的多 flag 题（已拿到部分 flag）不因单方向证伪而 drop——那说明整题还有戏，交给 ⑩ `partial_multiflag` 判。

`_REFUTED_RX` 覆盖：`证伪|已排除|排除掉|不成立|(未|没有|无)命中|(未|没有|无)发现|not found|no hit/finding/result/evidence|dead_end|此路不通|走不通|确认不存在|不存在该`。

## 报告契约与收尾类别必须按轨分叉（report-contract-by-track，2026-10-21 实施）

**问题**：CTF 轨一道 pwn 题（栈溢出 + 可执行栈 + 栈地址泄漏）解出来后连撞两道**必错**的门禁，模型两轮都拿不到结论落库：

| 报错 | 位置 | 性质 |
|---|---|---|
| `正式漏洞缺少必填项：测试环境、操作步骤、验证结果、风险影响评估、POC` | `store.validate_formal_vuln` | 模型明明交了 4 条完整 `repro_steps`（每条 code+expected） |
| `收尾类别不符: … category=vuln（finding 收尾要求 intel）` | `intents.close_intent` | **必然失败**，与模型行为无关 |

**根因一：同一批 verified 门禁，一半按轨放行、一半不按轨。** `_check_vuln_gates` 第一行就是 `if track not in ("pentest", "redteam"): return`，而 `validate_formal_vuln` **根本没有 track 参数**。DESIGN 里 challenge-flag-ledger 的立项理由（「正式漏洞契约是为渗透结论设计的，CTF flag 不是漏洞」）在文档里成立、在代码里只落实了一半。现在 `validate_formal_vuln` 加 `track` 参数：渗透/红队轨套十项契约，其余轨**降级为「有复现证据即可」**——降的是报告字段，不是红线（`has_repro_evidence` 仍强制，CTF 也不能无证据下结论）。

**`track=None` 刻意仍走严格契约**：人工 API 未指定轨、旧代码路径按「渗透结论」对待，宁严勿松。只有明确是 CTF/研究轨才降级——`test_formal_vuln_contract_defaults_to_strict` 钉死这一点。

**根因一附带的双重口径**：契约判 `"POC": pocs`（只认顶层 `pocs`），而 `has_repro_evidence` 认 `evidence.repro_steps` 里的 code+expected——**两个函数对「有没有证据」给出相反答案**。复现步骤本身就是可执行 POC，不该被判成没交。现改为 `pocs or has_repro_evidence(evidence)`，两处同源。

**根因二：引导文案与 CTF 题目级并入直接打架。** 这条是**结构性**的，不是概率：

1. 模型先 `bb_add_finding` 登记栈溢出（默认 `category=vuln`）；
2. `ctf_submit_flag` 落 flag 留痕（`category=intel`）；
3. 题目级并入（`add_finding` 的 challenge-dedup「一题一条」）把留痕并进 vuln 条目、**且不降级**（intel 不压解题结论）→ 留痕 id 实际指向 `category=vuln`；
4. `close_intent(outcome="finding")` 要求 `intel` → **必拒**。

而四处引导文案（`loop.py` / `tools.py` / `runtime.py` ×2）全写 `outcome="finding"`，模型照着做就撞墙；改 `outcome="vuln"` 能过但语义是错的（flag 不是漏洞，让模型宣称「确认漏洞」会污染链路图）。最坏的是**时灵时不灵**：题目下没登记过 vuln 时留痕就是 intel，同一文案能过——模型只能猜。

**修法：CTF 轨 `close_intent` 整体跳过 category 匹配**。CTF 的「解出」由 flag 台账派生，`vuln`/`intel` 这个二分在 CTF 轨没有消费者（台账不看 findings 类别）。`false-positive` 拦截**全轨保留**——误报发现仍不能拿来收尾（误报支持的是 dead_end）。渗透轨不受影响：那里 vuln/finding 是真语义，且没有「一题一条强制并入」。

**拒绝必须用 `[拒绝]` 前缀**：`bb_add_finding` 此前让 `ValueError` 直接穿到 `_dispatch` 兜底，变成 `[工具异常] ValueError: …`——那是「工具坏了」的语义，模型会倾向重试同一个调用，而这里要的是**改参数**。现捕获后返回 `[拒绝] … + 可执行改法`（同族 `ctf_submit_flag` 一直是这个口径）。注意 `[拒绝]` 进 `HARD_REJECT_PREFIXES`（连续 3 步硬拒 → E2 熔断挂人），所以文案必须带具体出口，否则会把模型逼进熔断。

**schema 边界**：`evidence` 的 description 现在点明「验证结论/预期vs实际 属于**顶层** `verification_result`，不要塞进 evidence」——用户参数里 `verification_result` 就写在 `evidence` 对象内（模型认为「验证结果」属于「证据」很自然），而 store 只读顶层字段，塞进去既不渲染也不被门禁认可。

## CTF 轨调度与收敛（artex-ctf-track v47，2026-10-19 实施）

**问题**：CTF 轨与渗透轨的「收敛」口径完全不同——渗透轨问「资产测完了吗」，CTF 轨问「题目解出来了吗」。v47 前 CTF 轨沿用资产口径，出现三个具体病症：① 覆盖度显示「已测资产 0/3」——题目不是拿来测的，这个数字既无意义又诱导模型「随便测一下就算覆盖」；② 拿到一条 finding 就被判「有发现即达成」的假收敛；③ 已解出的题仍会被反复派 Worker（模型看到题目还在队列里就会再打一遍，提示词约束不住）。

**黑板是唯一资产来源**（用户 2026-10-19 定稿）：CTF 轨资产=**题目**（`type='challenge'`），入口点做成**子资产**（web 题是 `url`、附件题是 `file`/`binary`，`meta.role="entry"`）——天然支持多入口（web 题既有 URL 又有源码包、pwn 题多附件），且复用 `register_asset` 单一写入口（E6 红线）。**findings 仍存在但不进 CTF 终态判据**：它是过程留痕（intel/unverified），不是「解出来没有」的证据。

**题目 `value` 的稳定性**：`register_asset` 去重键是 `UNIQUE(project_id, type, value, parent_id)`。显式名 → `value` 用名字；无名字 → 内容指纹 `challenge-<sha1[:12]>`（seed = brief/description/title + entry_hint + entries JSON，只拼非空部分）；都空 → `ValueError`。**AI 自动命名只写 `meta.title`（展示层），不改 `value`**——展示名与去重键分离后，「AI 改名」是零风险操作。

**两级并发约束**（用户拍板「单题上限 5 / 全局默认 15」）：全局 `config.artex.max_workers` 默认 **15** = 单题 `config.artex.per_challenge_workers`（默认 **5**）× 3 道并发题 + 余量。**全局必须显著大于单题上限**，否则单题上限永远打不满、形同虚设。

**题目闸落在调度层，不落提示词**（`core/blackboard/artex_intents.py::challenge_gate` + `claim_next_intent` 两个闸）：① 已解/已放弃题目的 pending 意图**批量置 `cancelled`**（不是「跳过」——跳过会让它们永远堵在队首）；② 同题在跑 Worker 数达 `per_challenge_limit` 时**跳过**该题的 pending（不置状态，等槽位释放）。`challenge_gate` 每 tick 只算一次（要扫 assets + 在跑 Intent），非 CTF 轨返回全空集合，四个新入参全空时 `claim_next_intent` 行为与引入前**完全一致**。

**CTF 轨两处口径分派**：① `task_coverage` 走 `challenge_coverage`（分母=题目数、分子=已解题目数、`scope_source="challenges"`）；② `evaluate_task` 走 `_ctf_task_state`（全部题目 solved → `goal_reached`；有 Worker 在跑 → `exploring`；都不在跑且未全解 → `needs_review`），metadata 带 `track="ctf"` + `challenge_stats`。范围=任务锚点里的题目 ∪ Intent 锚定的题目，都空则全部题目。无题目时两条路径都返回 `None` 走原渗透口径。

**一个 ARTEX 主控只绑定一个任务**：`chat_threads.artex_task_id`（v46 已有字段）由 `artex_plan` 新建任务时立刻写入（`bind_task_to_thread`），`resolve_task` 改为三级 **显式传参 > 线程绑定 > `active_task_id`（降级）**。降级路径必须保留——老线程/API 直连/工作台无会话都没有线程绑定。ARTEX 面板**不提供新建任务入口**（ARTEX 是唯一主控，要建题就让它建）；保留「从已有题目创建任务」作为降级入口（用户拍板「保留」）。

**主控状态回执**：`artex_status` 在目标段之后插入 `_challenge_status_lines`——进度 `解出 N/M 道`、逐题 `✓/◐/▶/✗/○` + 状态标签 + `got/quota` + Worker 数 + 否决数，末尾一句「CTF 轨口径」说明（覆盖度=已解题目数，不是已测资产数）。非 CTF 轨返回空段。

### 存量补建只在真有遗留数据时发生（2026-10-20 修复）

**现象**：新建的空 CTF 项目打开「题目」tab，列表里凭空多出一道**与项目名完全相同**的题（右侧标 `system`），用户实测项目名「hxbai测试」直接变成了题目。

**根因**：`_backfill_challenges`（`core/api/app.py`）是给**老项目归拢遗留数据**的迁移逻辑——v38 之前 CTF 项目的 findings `target_asset_id` 全为空、附件直挂根资产，补建一道题把它们收编进子树。但它的触发条件只判「CTF 轨 + 无 challenge 行 + `challenge_backfill_done` 未标记」：**新建的空项目同样满足这三条**，于是补建逻辑把项目名当题目名建了一道空壳题。

**修复**：把「有没有待归拢数据」提到**建题之前**判——无 `target_asset_id` 的 findings 与游离根资产（`parent_id IS NULL` 且非 challenge）**都为空时直接返回**，并落 `challenge_backfill_done` 标记（语义是「本项目存量迁移已判定完毕」，不是「补建成功」），避免每次读题目清单都重跑那两条查询。

**这不是关掉补建能力**：真有遗留数据的老项目照旧补建并收编（`test_backfill_adopts_orphan_root_assets`、`test_backfill_moves_project_level_findings` 仍绿）；人工建题（`POST /api/projects/{pid}/challenges`）与 Agent 的 `bb_declare_challenge` 完全不受影响。

**已污染的库**：删掉 `author='system'` 且 `value == 项目名` 且**零子资产/零发现/零 flag** 的空壳行即可（本机 `hxbai` 项目已按此清理并落标记）。清理前必须确认三者全零——非空壳的补建题可能已收编了真实遗留数据，删它等于丢数据。


## 业务语义层

发现门禁（info 全类别拒收、verified 无复现证据拒〔repro_steps 新口径或旧 POC legacy 直通，has_repro_evidence add/patch 共用〕、rating_basis 随 severity 就高覆盖——「无证据不下结论」写进存储层）；合并=证据并集；**资产合并同键发现折并（merge-assets-finding-union，2026-10-09）**：`merge_assets` 遇源/目标各一条同 `dedup_key` 的发现（撞 `UNIQUE(project_id,target_asset_id,dedup_key)`）不再整单拒绝，按上口径折并进目标行（证据并集、severity 就高、status 只升、保留目标 id），迁移全项目对源行的引用（`chain_links`/`evidence.relates_to`/意图 `basis_refs(finding:)`/`outcome_refs`）后删源行；binary/子资产重复/后代目标仍拒；撤回传播（四类目标+私信）；TOCTOU 防护（读-合并-写整体单事务 + 乐观锁 CAS）；资产状态回流（tested_clean 才算挖完）、high_value 标记。**疑似重复警告（orchestrator-efficiency M5 D1，2026-09-23）**：add_finding 返回值附 `dedup_warning`（cap 3 最新优先）——**同资产链**+同 vuln_class（非空）的行=可能被指纹/锚点分裂的重复，只提示不阻塞（Agent 可坚持新增）；本次并入的本体行不算；无 target 或 vuln_class 空（逆向常态）不查；警告只进工具返回值（单次按需），不落事件无频控。**finding-dedup-chain（2026-10-15，治「同一漏洞新会话验证/升链时重复上报而非改写」）**：①add_finding 精确同资产未命中时，对 `category=vuln` 的报错类发现在**同资产链**（自身∪祖先∪后代，两方向 CTE，**不含兄弟**）内找**唯一**默认指纹（`dedup_key==vuln_class`）条目并入（走既有并集合并、severity 就高、status 只升）——锚点漂移（同一端点分别挂 domain/url）与自造 dedup_key 两种成因都归位到原条目；显式指纹不作候选（AI 主动拆分语义保留）、同链多桶不猜（返 `chain_ambiguous=<n>`）、intel 类不走兜底（按观察点自由分桶）；②`dedup_warning` 项补 `dedup_key`/`target_asset_id`，bb_query findings/site 回传 `dedup_key`，bb_add_finding 回执回显 `dedup_key` 并在 `[疑似重复]` 里给出「拿旧 key 重报自动并入」的可执行指引——修掉「查不到旧指纹只能自造新键」这一环；③工具 schema 给 `dedup_key` 写清语义：**不填=按 vuln_class 归一（推荐），同一漏洞切勿自造新键**。

## 黑板查询过滤（bb-query-filters，2026-09-24 实施；site 单站全貌 2026-09-26 增补）

**取数从「全量拉回本地找」改为「按条件精确取」**：`bb_query` 接出底层早已实现、工具层没暴露的过滤——findings：`min_severity`（info/low/medium/high/critical，语义=不低于该级）、`verified_only`、`category`；assets：`tag`（meta.tags 大小写不敏感）。events：`kinds`（多值 IN 过滤，store `recent_events` 新入参，与 session_id/tail/before_id 三分支正交）、`session_id`；输出增量带出 id/session_id/created_at（查事件即可知归属，不必反查落盘）。六面统一 `limit`（1-200；events 走 tail 默认 50、其余应用层裁剪默认全量——**默认行为全部不动**）。闭集入参（severity/status/type）非法值显式返 `[错误] 非法 X（允许: …）`，修掉「拼错参数静默返回 [] 被误判为没有」；limit 非法钳 1-200 不报错。M3 关键词 `q=`（LIKE/FTS5）后置观察：看实战是否仍频繁全量查询再立项。

**`what=site` 单站全貌（2026-09-26 增补）**：`asset` 入参（根资产 id 或精确 value，host/domain 优先消歧）一次返回 `{root, counts, assets（子树全部）, findings（挂在子树上）, intents（target/basis 命中子树）, hint}`——子树与意图归属复用 attackpath 的 `_subtree_ids`/`_intent_in_site`（单一口径）。动机：实战（vpn.zut.edu.cn 核实，2026-09-26）agent 按 type 分片全量拉（host/service/url/domain 各一把、条数多还漏看）再本地过滤，两分钟烧近 20 次查询；小 limit 试探漏看后重查、spill 解析踩坑再重查进一步放大。配套把「查站点一律用 site、findings 带 target_asset_id、events 带 kinds、limit 勿小值试探」写进工具 description 显眼处（不加 result 侧提示——回执尾部拼文本会毒化模型对 spill 文件的 json.load）。

## 漏洞收录格式（finding-report-format，2026-09-22 实施）

每条 vuln 发现自带三件套，渲染为报告「逐发现详情」节，报告工程师零脑补：**危害描述（impact）/ 复现步骤（evidence.repro_steps）/ 修复建议（remediation）**——结构化存储（findings 新列 impact/remediation，schema v20；repro_steps=`[{desc 必填, type: http|python|cmd|image, code, expected 复现自证锚点, artifact_id（image 步必填）, stability, target}]`，cmd 渲染 bash 围栏、image 嵌图片产物），不做 Markdown 大字段。**verified 门禁新口径**（仅 pentest/redteam 轨）：repro_steps 至少一步 code/artifact_id 非空且该步 expected 非空，或旧结构 poc/pocs/poc_artifact_id legacy 直通（`has_repro_evidence` add/patch 共用；`validate_repro_steps` 全轨写入口坏结构宁拒不存）；**impact/remediation 不进门禁**（写入放行，报告侧才校验齐备；合并旧值非空保留、空缺由新报补入，patch None=不动/空串=清空）。渲染器三处共用：发现详情弹窗 / 一键复制文稿（三节 Markdown 模板，空节「（待补充）」）/ 报告逐发现详情（M3 随 pentest-phased-workflow M4）。旧数据读时兼容：repro_steps 优先，旧 pocs 映射等价步骤；repro_steps 进证据并集（内容指纹去重追加，重复上报不堆步）。边界：vuln_class 词表 / CWE 映射 / 聚合去重 / 报送流转不混入。

## 链路与全景图

chains/chain_links 攻击链（假设→验证→利用）+ board_graph 黑板全景图（5 类节点 9 种边，全来自既有字段，只读聚合）；全景 chain 边只取 origin='manual'（任务轨迹自动链 origin='trace' 已随任务机制退役，2026-10-06）。**黑板全景图前端 UI 已下线（2026-09-26 用户要求）**：旧壳「全景」tab（含同页「主线」切换档）与新壳「黑板全景」tab 移除、boardGraph/ 前端删除；board_graph 只读聚合端点 GET /projects/{pid}/board-graph 与 `graph.board_graph` 保留（数据接口，tests 不动）。**会话协作图 session_graph**（编排器+会话窗 × delegate/derive/inbox/dm 边）见 §四「指挥模型」。**单站攻击过程图**（对某 IP/域名的全部探测归一化）见下文「单站攻击链路图」。

## 执行轨迹链路（execution-trace-chain，2026-09-22 实施；2026-10-06 部分退役）

**任务机制退役后，原轨 A 轨迹视图（`build_task_trace`）与轨 B 持久链（`materialize_task_trace`）随任务一并删除**——任务区间切分与任务链物化不再存在，`GET /trace/{task_id}` 端点与前端 `TaskTraceList` 组件移除。`core/blackboard/traces.py` 保留的只读统计口径：**效果榜 `effect_stats`**（(skill × kb 模块) 组合 × verified finding 计数，打法效果榜）与 `kb_module_feedback`/`retrieval_stats` 检索质量对账；数据接口 `GET /trace-effect` 保留。

## 任务尝试树（task-attempt-tree，2026-09-27 实施；**2026-10-06 退役**）

任务机制退役后删除：`core/blackboard/tasktree.py`、`GET /api/projects/{pid}/tree/{task_id}` 端点、前端 `webui/src/views/live/TaskTree.tsx`（其 `tree.css` 无消费者）。**意图（intent）体系本身保留**——`intents` 表、`declare_intent`/`close_intent`/`reopen_intent` 工具与「单站攻击链路图」见下节，意图归属/门禁/收尾口径不变；「边干边写」（`category=intel`+`status=unverified` 的执行中认知）纪律不变。

## 单站攻击链路图（website-attack-path-graph v3，2026-09-24 实施）

发现页「链路」子视图由旧 findings DAG 画布（FindingsCanvas 两套渲染图退役）替换为**单站攻击链路图 v3**。用户定稿语义：「思考，规划，执行——**意图就是规划产物，每个意图必须收尾：要么发现，要么漏洞，要么死路**」。

**主脊（D8）**：``目标 → 子目标/意图 → 收尾：漏洞 | 有效发现 | 死路 → 意图 → 收尾 → …``——是一条**可循环延伸**的链：收尾产出的发现/漏洞又可作新意图的推导依据继续往下长；布局按依赖层级**自动分层**（不固定「意图一列、目标一列」）。产出可回流出新意图/新目标。目标=选定的 host/domain 资产根（未选显 TargetGuide 引导，只按 IP/域名搜索）。意图=一句可证伪假设，独立轻表 `intents`（**schema v22**，零 ALTER 建表；不复用 tasks/chains），字段 statement/target_asset_id/basis_refs/status/open|closed/outcome_type/outcome_refs/dead_reason/evidence_refs/revision。

**子目标节点（2026-10-01）**：根目标的**直接子资产**渲染为 `subtarget` 节点（第二层「子目标」显式化，如根域下的子域/站下的路径端口；孙节点不上图，保持清爽）。意图按资产锚点（target_asset_id ∪ basis_refs 的 `asset:<id>`）**归属到其所在子目标子树**——derive 边自该子目标起；无明确归属的意图仍挂根。子目标节点带**终态徽章**：`settled`=名下意图**全部收尾**且至少一条 dead_end（与 tested_clean 背书同口径）；另有 status/发现数。

**四条硬规则（D10）**：①**意图必收尾**——Agent 五处快照带 open_intents 清单，`finish` 首次有未收尾意图拒绝并列清单（**第二次 finish 允许**，人工兜底），续跑注入收尾提醒；②**收尾必带证据（宁严勿松）**——vuln 收尾引用 finding 必须存在/同项目/非 FP/**category=vuln**，finding 收尾要求 category=intel；FP 发现拒收并指引走 dead_end；死路必须带非空 dead_reason（什么证据排除假设）+ ≥1 条 http/event/artifact 证据引用；证据不足保持 open；③**边仍是逻辑推导**——derive（target/finding → intent，basis_refs 是数据源，无主依据由 target 起边）、outcome（intent → finding）；服务端 Kahn 断言主脊无环（端点缺失同样 AssertionError）；时间先后只影响布局；④**死路是意图关闭态**——持久在意图上，default_hidden 默认隐藏，服务端预复合 bypass 穿通边（入边×出边，已有直连不重复造）；新证据/被引发现标 FP → reopen，**只重开自身不级联下游**，reopen 清收尾字段但保留 evidence_refs；AI 可自收尾，人类侧栏可驳回/重开。

**意图先行闸（口径 Y，2026-10-01；2026-10-09 升为「每意图级」）**：意图是「主脊/规划产物」，intent 管假设细粒度（旧 `task_plan` 粗粒度层随任务机制退役，2026-10-06）。**每一个实质动作前都必须有 open 意图**——对每条假设先 declare_intent（细）再动手。落在 dispatcher 层（`_dispatch_once`，sess-/chat- 会话、A2 计划闸之后）：本会话无 open 意图且动作不在只读放行面 `_INTENT_PRE_ALLOWED` 时回 `[拒绝] 意图先行闸：…先 declare_intent(statement=…)`。放行面=全部只读侦察（bb_query/kb_open/kb_search/route_lookup/list_symbols/decompile/disasm/read_file/search_files/browser_* 等）+ 控制原语 + 协调原语（`build_team`/`bb_notify`/`bb_add_asset`——防多代理死锁与「请人批准」类提案 `request_*`/`propose_pack_edit` 被误拦）。**逐动作现查**（**一次性放行标志 `_intent_lead_passed` 已删**，2026-10-09）——plan→execute→plan 自循环里一条意图收尾后再动手必须先声明下一条，事后声明（干完活再补票，实测 `sess-b9a539e3ebfe` declare 与 finding 仅隔 12 秒）在结构上不可能；拒绝路径复用 `[拒绝]` 前缀（连续 3 个模型步硬拒 → E2 熔断挂人）。与 bb_add_finding 既有意图闸分工：本闸管「实质动作前须有 open 意图」，后者管「登记发现时本会话须有 open 意图且意图声明之后须有执行动作」。

**意图必有资产锚点（2026-10-01）**：`declare_intent` **工具层硬门禁（仅 Agent）**——必须有资产锚点：`target_asset_id` 非空，或 `basis_refs` 至少一条 `asset:<id>`；否则拒绝（游离意图落不到链路图子目标下、其 dead_end 收尾也无法给任何资产背书 tested_clean）。人类/系统路径豁免（门禁落在工具层，与 bb_add_finding 意图闸同策略）。存量兼容：**只把无锚点且仍 open 的意图落 `intent.anchor_required` 审计事件**（schema v29 迁移，幂等），closed 历史一律不动。

**执行层（读时归属，不改造写入链）**：http_history 按 M1 归一为测试点尝试组——method+路径模板（数字→{id}/UUID→{uuid}/≥8hex→{hash}）+ query 键排序去重、同点 30min 窗折叠、五档 found/hint/blocked/no_reaction/skipped、跨任务跨会话不切割。attempt.intent_id 查询时按意图存活窗 `[created_at, closed_at]`（open 尾端 +∞）归属；重叠窗归**最新声明**意图；无时间的 curl 缺口合成节点（request_count=0）按收尾发现反查 owner。执行层时间序相邻边单独出 `exec_edges`（同意图桶，含无主桶），前端点意图卡才展开为尝试条。站点边界=目标全后代子树；意图入图=target_asset_id 在子树或 basis_refs 命中子树。

**API**：GET /api/projects/{pid}/attack-path?target=（返回 target/nodes/edges/attempts/exec_edges/counts；目标不存在 404）、GET …/intents?status=、POST …/intents/{iid}/reopen；Agent 工具 declare_intent（计划组）/close_intent/reopen_intent（控制组）；写全走 intents.py → `bb._tx()`，事件 intent.declared/closed/reopened。**保留不动**（只弃渲染图不弃功能）：漏洞/有效发现硬切换、catView 列表硬过滤、category 徽章。M4 跨站泳道联动**后置**——等 vuln-chain-graph provides/requires 人工确认链边。

## 战役记忆

campaign.db 独立全局库（仿 intel 先例，跨项目不绑项目；**落 `data/campaign.db`——workspace-hygiene D1 2026-09-23 从 workspaces/ 根归位，老位置惰性迁移**），按 mission + 目标值召回历史打法注入编排态势。**条件写入（experience-sedimentation M1，2026-09-22；M6 F1 扩档 2026-09-23）**：campaign 收窄为「成功打法索引层」——任务收尾经 `AgentSession._sediment_verdict` 单一判定（campaign 写入与复盘触发共用一套口径）：done 且任务窗内有 verified finding（`traces._session_task_windows` (lo,hi] 区间 + author=session_id 双约束查 finding.new/finding.merged 事件）或 exploited 链（origin='trace' 且节点挂该任务）→ 写入，content=result_note 纯净收尾（原 findings 标题拼接退役）；**done 且 FP-only（本任务 finding 全 false-positive，无 verified/exploited）→ 死路记账档**：写入 `tags=["dead_end"]` 负知识（表结构零变更），content 优先 result_note、空则 `〔死路〕FP title` 兜底，不复盘——**status='false-positive' 本身就是死路的结构化口径**（撤回传播/死路徽章/coverage FP-only 全在消费，零新约定，M6 打磨推翻 category 结构化建议）；done 无产出但 result_note ≥500 字 → **高质量复盘档**：只跑 kb 复盘提案（reason 引用任务 id 过证据锚点闸），每会话上限 1 条（`_sediment_lite_used` 内存计数在产提案前占用，重启清零可接受）；failed+error 只走失败复盘提案不写 campaign（失败教训沉淀 kb「坑」段）；aborted（人工中断/关窗未收尾/worker 异常兜底三调用点）/ awaiting_human / 其余无产出 done 一律不写；判定异常按无产出处理（宁少勿滥）。**recall 马太修正（M2）**：usage 热度权重改 `decay × log1p(usage_count) × 1.5`（原 min(usage,10)×0.5 让老记录恒霸榜），新验证打法可凭近期命中上位；召回即对全部命中条目 bump usage。

## 会话与事件流

events 表全量审计 + EventBus 同步落库、尽力广播；前端按游标增量消费。

## 资产批量导入与网络空间测绘（cyberspace-mapping M1+M2，2026-09-23 实施）

黑板资产的批量入口，落库恒走 register_asset 单一入口（E6 语义全复用，零旁路）。**M1 文件导入**：`core/assetimport.py` 解析器（表头别名匹配 0.9 优先 / 无表头逐列内容投票 ≥0.8 / 首条文本列兜底 title 0.5；CSV utf-8-sig→gbk 回退恒可用、xlsx openpyxl import-guard 缺失端点 503）→ `import_assets`（assets.py）：quiet=True 静音逐行 asset.new、整批单条 `asset.imported` 汇总事件（带 `batch_id` 预留按批撤销）；行路由 url（主机部域名无既有行先补登）/ host（**FOFA host 常回「ip:port」**——剥端口后 IP 形态有端口→service 无→host；域名→domain 且行带端点时域名+端点双锚点）/ ip+port→service / 仅 ip→host；domain 行先行排序提高 url 挂载率；**DNS 线程池预热**（16 workers 填充批次内缓存 `_DNS_WARM`，finally 即清——不做进程级负缓存，防 DNS 恢复后域名永远挂不上）。**M2 FOFA 中转查询**：`core/fofa.py` 纯客户端（qbase64 + fields 白名单 `ip,port,protocol,host,domain,title,product`〔不含 header/banner/cert 保 1 万档〕+ size clamp 10000〔文档默认最大 1 万；字段白名单不含 header/banner/cert 故不受其 2000 限制〕+ 页×条≤1 万 clamp）；错误四态签名分类——「已用完」QuotaExhausted **熔断绝换端点不重试**（防封号）/「账号无效」ConfigError /「key 不存在」AuthError /「[官方错误信息]」OfficialRetryable 换备用重试；网络错误/HTTP 非 200/非 JSON 也切备用（主 fofoapi.com 备 107.173.248.139）。端点面：GET/PUT /api/fofa/config（key 脱敏前4后4，PUT 空串=不修改防回显误覆盖）、POST /api/fofa/test（info_my 免费）、POST …/fofa/search（结果逐行 existing 三锚点 domain/service/host 标注，熔断 429/配置 400/其余 502）、POST …/assets/import/preview（multipart，不落库）、POST …/assets/import（mapping 二维数组或 dict 行两形态，≤10000 行，source 白名单 xlsx/csv/fofa/manual）。**key 安全**：只存 config/fofa.json（.gitignore），不入库不入日志；**信任边界**：查询语句与 key 明文流经第三方中转——敏感项目慎用（页面常驻注记）。**轨适用**：黑板「测绘」页签 CTF 轨不挂载（2026-09-23 用户反馈：CTF 无资产收集场景；track!=="ctf" 才进 tabs，与 funcs 按能力条件挂载同模式；M4c board_view 指向 mapping 时按既有回退机制落 findings）。

## 资产树归并、CDN 判定与根状态读时派生（asset-tree-derived-clean M1+M2，2026-09-24 实施）

**树形态**：host(IP) 为根 → domain 子节点 → url/service 再挂。register_asset 为唯一登记入口：domain 自动 DNS 解析挂 host，url/service 的 IP 主机部挂 host，域名主机部只精确挂既有 domain（不猜 DNS、不造行）；同 IP 首域名记 `host.meta.primary_domain`、其余 `meta.alias`。**CDN 判定**（core/blackboard/cdn.py）：共享 CDN IP 上的域名彼此无关——解析命中 CDN 的 domain **保持根行、不建 host、不挂树**；优先级 `meta.cdn` 人工覆盖（True/False）> CNAME 后缀 > IP CIDR；清单= `packs/data/cdn_ranges.json` 随包基线 + `config/cdn.json` 同结构增补，坏文件 ValueError fail-fast；**拿不准默认非 CDN**（不并只是保守，误并才会错绑结论）。DNS 漂移（重报解析到新 IP/CDN）经 `set_asset_parent` 改挂/摘挂并发 `asset.reparent` 事件。

**根状态读时派生（effective_status，core/coverage.py `effective_status_map`）**：有子资产节点的 tested_clean 不由 AI/人工显式设置——叶子 effective=显式状态经 `tri_state_of` 归一（basis=explicit）；父节点 effective 由全部子节点读时派生（basis=derived）：**有洞 → visited**（不派生 clean）、全子 `tested_clean` → `tested_clean`、全子 `open` → `open`、混合 → `visited`；**新增子资产立即破除 derived clean**（加未测子 → 父掉出 clean），无需事件联动；has_findings 沿子树向上传播。写入门：有子资产节点显式写 tested_clean 一律 ValueError（na 不挡——人工裁定）。黑板前端徽章优先消费 API 的 `effective_status`，`status` 仅代表持久化显式状态，前端不自行推导也不回写根节点。前端黑板资产筛选器**只列 host/domain（IP/域名）按值搜索**（2026-09-24 用户定稿，推翻同日早先「四类放开」口径——过滤只按 IP 和域名），选中后沿子树展开过滤，url/service 叶子 finding 不漏。存量处理：`scripts/rebuild_asset_trees.py`——DoH（默认 223.5.5.5，避开 Clash fake-ip 198.18/15 与系统代理）重解析挂树，CDN 根行无操作、有子根行 tested_clean→reset-open；默认 dry-run，`--apply` 落库，全经 Blackboard 方法。

## 资产三态（asset-tri-state，2026-10-09 定稿并实施）

**要解决的问题**：旧口径把**有 finding 挂链**也算「收口」（`terminal-finding` ∈ settled）——方向性错误而非精度问题：①带洞资产（11 个适用面只测 1 个就出高危）被标「已测尽」、从 uncovered 消失，恰恰最该继续深挖；②父节点因**子节点出洞**被派生 `tested_clean`，同时 `has_findings=True`（自相矛盾的行）；③叶子显式 `tested_clean` 后补挂洞，走 explicit 分支永远显示 clean。

**定稿三态**（`core/coverage.py`，词表与 DB status 同名，前端/API 零新枚举）：

| 状态 | 落库值 | 判据 |
|---|---|---|
| **未测试** | 不设（`open`） | 从没碰过 |
| **已访问** | `visited` | 碰过 **∧** 有未覆盖适用面 |
| **已测试干净** | `tested_clean` | 碰过 **∧** 适用面全覆盖 |

要点：**有洞与状态正交（2026-10-10）**——finding 挂链是**独立标签**（`has_findings` 布尔位），**不参与状态判定**：显式 `tested_clean` 的资产即使后补挂非 FP 发现仍为 `tested_clean`（`settled=True`）；父节点「全子 clean」即 clean，不因子树有洞被阻断。前端徽章位拆成**两枚并列**（状态 + 「有发现」），允许 `已访问+有发现` / `已测试·干净+有发现` 同时出现。「有洞 ≠ 测干净」中的「测干净」改由**写入门禁**把关（`store.set_asset_status`：有子拒写 + 叶子须死路意图背书 + 须带 note），而非由 findings 覆写状态。`is_settled` 收口味 = `tested_clean`/`na`/`dead_end`（**不含 finding**；`na` 归 clean——测过了结论是「没什么可测」，`dead_end` 归 clean——此路不通）；`terminal-finding` 只描述**无显式状态**的裸资产挂洞（视作 `visited`——碰过但未收口）。**`scanning` 并入 `visited`**（派发是一批一批派、非任务认领，无孤儿半程；写入门仍接受 scanning/存量行，读时归一到 visited，零迁移）。`coverage_report` 与 `effective_status_map` 同口径：裸资产出洞不收敛、留 uncovered，组内 `terminal` 计 = 已测干净数（含挂洞的显式 clean）。

**消费方**：`orchestrator._assets_view` 出口按三态重排——`untested`（未测试清单）/`in_progress`（已访问，未测尽，**含带洞资产**）/`clean_count`（已测干净，只给计数防重复派）；`_covered_ids` = 已测干净（`high_value.covered` 语义随之收紧）；兼容键 `uncovered`↔`untested`、`done_count`/`clean_count` 同值。**主控（workbench chat-orchestrator）态势注入（D8，2026-10-09 已落地）**：`coverage.situation_snapshot` 三态分桶快照（untested/visited 清单 cap + clean_ids + counts）+ `core/chat/runtime.py::_situation_context()` 注入主控 system——主控不必再靠 bb_query 手动搜资产拼现状，每轮重投影、读取失败降级占位；与子专家 `_mission_context`（分配资产台账+维度面）对称。实施记录见 `docs/plans/归档-已完成/asset-tri-state.md`。

## tested_clean 意图死路背书（tested-clean-intent-backing，2026-09-25 实施；2026-10-01 改读链路图口径）

**「这个资产没洞」是被证据证伪的假设，不是访问观感。** 触发：中原工学院项目两个会话把 109 个叶子以「80/443 各 1 GET 见登录页」「同模板 200 随批次收口」粗略标 clean，盘上零意图零证据。写入门禁：叶子资产标 tested_clean（在「note 非空」「无子节点」两检查之后）服务端强制有死路意图背书。

**2026-10-01 口径（与攻击链路图同源，子目标级）**：`intents.dead_end_backing_target(conn, pid, aid, root_id=None)` 先把 aid 归到它所属的**子目标**（自根向下、离根最近的那层祖先；root 缺省取资产链顶），再判定该子目标子树是否满足「**名下意图全部收尾**（无 open）**且至少一条 dead_end**」，返回该子目标 id（背书来源）/ None。要点：①**子目标级**——根的直接子资产是子目标；对叶子标净实为校验其所属子目标的整棵子树，父/子树共识可背书后代；②**全部收尾**——子树内残留任一条 open 意图即无背书（宁严勿松）；③**逐子目标、不跨旁支**——无关旁支不互相背书（保留 2026-09-29「同模板一致」批量误判修复精神；当时移除的是跨分支祖先链批次覆盖）。

死路收尾在 close_intent 已强制 dead_reason 非空 + ≥1 真实证据引用（http/event/artifact），证据语义由意图层继承不重复造。无背书 → ValueError 指引「先 declare_intent 声明可证伪假设，close_intent(dead_end) 带证据收尾后再标；如有 open 意图先收尾」（工具层 `[拒绝]`、API 422）。na（人工裁定）/budget_stop（被迫停手）口径不动；父节点 tested_clean 读时派生不动；同状态 no-op 在背书检查之前返回=存量行兼容。存量处理：`scripts/reset_cursory_clean.py`——按会话（默认上述两会话）回退当前仍为 tested_clean 的资产至收口事件 old 值（open/visited），默认 dry-run，`--apply` 落库（已执行：109 个全部回退，其余会话的 136 个未授权不动）。

## 意图资产锚点（intent-asset-anchor，2026-10-01 实施）

`declare_intent` **必须有资产锚点**（`target_asset_id` 或 `basis_refs` 含 `asset:<id>`），否则**工具层拒绝**（仅 Agent；人类/系统豁免）——游离意图落不到链路图子目标下、其 dead_end 收尾也无法背书 tested_clean。schema v29 迁移把存量**无锚点且 open** 的意图落 `intent.anchor_required` 审计事件（closed 不动，meta 键幂等）。

## 测试维度面（test-dimensions，agent-path-intent-loop M1，2026-10-09 实施）

**要解决的问题**：覆盖度（`coverage.py`）只回答「这个资产摸过没有」，回答不了「测的面够不够」——一个资产可 coverage=100% 却只测了可达性就被标 `tested_clean`。本功能把「该测哪些面」显式成一张**可判定的维度清单**，让收敛判据从「覆盖度」变成「面是否都有结论」。

**维度清单**：`packs/kb/<track>/dimensions.yaml`（**2026-10-13 能力包退役后由 `packs/tracks/` 迁入 `packs/kb/`**，`core/skills/taxonomy.track_dir` 即 `packs/kb/<track>/`）——轨级静态表（仿 `phases/*.yaml`，受约束 yaml 极简解析，零 PyYAML），项目可经 `config.dimensions` 同名 id 整体覆写。每个维度：`id`（稳定标识，意图按此归属）/`name`/`applies_to`（适用资产类型 domain/host/service/url/binary）/`intent`（意图模板，`{asset}` 由调用方替换）/`evidence_hint`（收尾为 dead_end 时什么样的证据才算「确实排除了」）。pentest 轨首版 11 个面：unauth/authz/weakpass/sqli/upload/rce/traversal/ssrf/xss/infoleak/deserial；**ctf 轨 2026-10-19 补四面**：triage/analyze/reproduce/flag（刻意比渗透少——CTF 是一题一条链路，切太细会变成机械凑数；详见「CTF 解题内核」）。

**面覆盖判定**（`core/dimensions.py::dimension_coverage`，纯函数）：**面覆盖 ⟺ 该面有 ≥1 条收尾意图（`status=closed`）且无 open 意图**。返回 `{covered, uncovered, open}`；`is_converged(intents, dims)` = 所有适用面全覆盖（dims 空则不拦）。面是**下限不是上限**——一个面可派多条意图往深挖，不漏面即可。

**面归属落库**：`intents.dimension` 列（**schema v34**，幂等 ALTER，历史行不回填）。`declare_intent` 工具层校验归属面 id 对本轨合法（填了不合法则拒；不填放行，向后兼容）；存储层不校验（无 packs 访问）。

**演进（接 skill）**：后续把维度条目迁进各 skill 的 `covers` 字段，由「专家 skill 白名单 → 维度清单」推导取代本静态表；在此之前本表是唯一真相源。

**消费方（分阶段落地）**：M1 已落「表 + 加载器 + 面覆盖判定」；M2 起由子代理 plan→execute→plan 自循环消费（领资产 → 按资产 type 取维度子集 → 逐面声明意图并收尾 → 全封面才许收尾），与「主代理单一派发」配套。

**M2（2026-10-09）已落对话链**：主控 `call_expert` 新增 `asset_ids`（服务端逐个校验同项目存在）——**单一派发**下不同子专家分不同资产；`ChatTurn` 接 `assigned_asset_ids` 并透传子线程，`_mission_context()` 把「分配资产（含『未分配资产仅可 bb_add_asset 上报』）+ 维度面清单 + plan→execute→plan 纪律」注入子专家 system（不进消息历史）。**维度收敛闸** `_dimension_gate_status()`：仅 spawn 子专家且（分配资产 ∪ 本线程意图锚定资产）非空时启用，逐资产做 `dimension_coverage`，未覆盖/未声明意图 → 纯文本收尾前推回 plan（`_DIMENSION_GATE_MAX=5` 次后放行并落 `chat.dimension_gate`）。收敛判据是「意图面是否都有结论」，非覆盖率。

**M3（2026-10-09）资产越界闸**：`ToolDispatcher.assigned_asset_ids` 非空时，命中 `_ASSET_REF_TOOLS`（bb_asset_status/bb_add_finding/declare_intent/bb_delete_asset/bb_merge_assets）且引用集外资产 id → `[越界拒绝]`（与角色白名单同前缀，进 E2 熔断）；**bb_add_asset 恒放行**（唯一上报通道，新资产仍须等主控派发）、**bb_query 不拦**（只读侦察）。未分配资产的会话/任务链/主控零影响。

**M4（2026-10-09）发现回喂**：`finding.new` 进编排器异常订阅白名单（`Orchestrator.WAKE_TRIGGERS`，冷却 300s）——子专家挖到高危产出即时唤醒主控复判（是否派生意图/加派子专家/修正 HVT），不必等下轮 tick 的被动注入（`_stats` findings 段仍在，二者互补）。**严重度闸门 `WAKE_FINDING_SEVERITIES=("high","critical")`**：低档发现静默（避免唤醒轮被 info/low 噪声烧穿）；`finding.merged` 不唤醒（合并入既有发现不改变打法）。`wake_brief_text` 把多条发现成组渲染（一条复判指引 + 逐条 · 摘要）。

## 指定资产删除（2026-09-25 补 UI 入口）

走查垃圾/误登记资产的人工清理通道，**物理删除不进回收站、不级联**。后端早已就位：`Blackboard.delete_asset(aid, author)`（store，2026-09-15）——门控：仍被 finding.target_asset_id 引用，或存在子资产 → ValueError（API 409，文案指名先处理发现/子资产）；不存在 → LookupError（404）；删后落 `asset.deleted` 审计事件（含 value/parent_id 快照）。API：`DELETE /api/projects/{pid}/assets/{aid}`（404 资产不属于本项目 / 409 门控）。**误报走 PATCH false-positive 不走删**（发现口径不变）。前端 [Blackboard.tsx](webui/src/views/Blackboard.tsx) 资产行内 🗑 按钮（平铺 AssetRow / 树 renderNode / 标签筛选三路径，`api.deleteAsset`）：点击先 confirm 确认窗，409 文案落底部错误行；compact 侧栏不挂删除入口。

## 项目回收站终点操作（2026-10-18）

删除=整目录 rename 进 `workspaces/.trash/`（不物理删除、可恢复）之外，回收站提供两个人工终点操作（[TrashView.tsx](webui/src/views/TrashView.tsx) 每行按钮，均先 confirm）：**永久删除**——`ProjectStore.purge_trashed` 物理抹除 `DELETE /api/projects/trash/{trash_name}`，不可恢复；**清除项目内文件**——`ProjectStore.clear_trashed_contents`（`POST /api/projects/trash/{trash_name}/clear`）抹除条目内除 project.json 外的一切（黑板库/样本/产物/日志），meta 落 `trash_purged_at` 墓碑戳：条目仍留在回收站清单（名字/轨道/专家绑定可展示，回执 freed_bytes），但数据没了——`restore_project` 对墓碑条目明确 ValueError（API 422，防恢复出残缺项目），前端禁用「恢复」、仅剩「永久删除」收口。句柄校验（拒分隔符/上跳）与 Windows 占用退避重试（0/0.05/0.1/0.2s）复用删除/恢复同款口径。

### Windows 删除两个必踩坑（2026-10-19 线上 500 复盘）

线上「永久删除」连续三次 500，根因是**两层叠加**：路由已热更但异常处理未热更（旧字节码只 `except PermissionError`，`OSError` 漏成裸 500——`core/` 改动不热加载，必须重启平台）；以及 `_remove_entry` 撞到两种 Windows 上根本删不掉的条目形态。二者都已在 `core/projects.py` 收口，**新代码上线后必须重启平台才生效**。

1. **WSL LX 符号链接（reparse tag `0xA000001D`）在 Windows 上删不掉。** Agent 在 WSL 里建 venv 会留下 `bin/python3 -> /usr/bin/python3`、`lib64 -> lib`，从 Windows 看是 `attrs=0x400/0x410/0x420` 的 reparse point。实测（真机 E: 盘 NTFS）对它：`os.unlink` / `DeleteFileW` / `RemoveDirectoryW` / `SetFileInformationByHandle(FileDispositionInfo|Ex，含 POSIX 语义)` / `NtSetInformationFile` / `shutil.rmtree` / `cmd del` / `cmd rd` / PowerShell `Remove-Item` / `robocopy /MIR` / MSYS `rm -f` / `takeown` **一律 WinError 5（ERROR_ACCESS_DENIED）**；但同路径 `os.rename` 成功、`CreateFileW(DELETE)` 也成功——只有「删除操作」被拒。同时 `os.path.islink()` 返回 False、`os.readlink()` 抛 `ValueError`（不是 OSError）、`os.stat()/is_dir()` 会 follow 并抛 **WinError 1920（ERROR_CANT_ACCESS_FILE）**——这正是旧 `shutil.rmtree` 死掉的地方。**唯一可行路径**：以 `DELETE|FILE_WRITE_ATTRIBUTES`（`0x00010100`）打开，`FSCTL_GET_REPARSE_POINT` 读 tag，再 `FSCTL_DELETE_REPARSE_POINT` **摘掉重解析标签**，条目退化为普通文件/空目录，之后常规 `unlink`/`rmdir` 即成功（见 `_win_reparse_handle` / `_strip_reparse_point` / `_remove_link`）。只给 `DELETE` 权限打开会让 FSCTL 报 err 5，`WRITE_ATTRIBUTES` 是必需项。
2. **WSL 写的文件常带 READONLY 位**（如 X11 的 `.X99-lock`，`attrs=0x21`），Windows 上 `os.unlink` 对只读文件同样 WinError 5。`_unlink_retry`/`_rmdir_retry` 在首次 `PermissionError` 时调 `_clear_readonly`（`os.chmod(path, S_IWRITE)`，实测 `0x21 → 0x20`）后重试。

分类层配套：`_fs_kind` 只读 `st_file_attributes` 属性位（reparse point=链接），绝不 follow；`_du_bytes` 用 `os.scandir` 栈式遍历，链接计 0 字节；`_remove_entry` 按 kind 分派且不 follow，错误聚合后抛 `PermissionError` 或 `OSError`（API 层对应 422 带因）。另注：`cp -r` 会把 LX 链接静默变成空普通文件，**复制目录无法复现此 bug**，只能用真条目或 `os.rename`（rename 保留标签）——回归测试用 junction 等价覆盖（`test_purge_trashed_with_reparse_point` / `test_strip_reparse_point_degrades_to_plain_dir`）。

# 四、编排与执行（core/orchestrator 指挥 + core/team 执行）

## 指挥模型（编排器=主 agent：建队 + 亲自执行，2026-10-06 定稿）

旧「任务池 / 会话即委托 / 委派 delegate」心智退役，改为 **指挥（编排器）=项目主 agent**，职责两条：

- **组建队伍 `build_team`（核心职责）**：给团队名/共享目标/成员名册——成员 role 从本轨专家池优先选取，留空=按职责现场定义的动态成员。创建后是 `draft`，**需人类在「指挥」页查看并配置**（`components/team/TeamConfigDialog`：preflight + 四项确认）后才启动；本工具只登记 roster，不启动、不派单。
- **亲自执行 `execute`**：指挥自己下场干一件活（完整 Agent 工具面：命令/文件/黑板/知识库/浏览器…），产出落黑板，适合小范围验证/补刀/需要指挥自己判断的活，受活跃窗上限约束。

团队成员=子 agent（独立会话），看不到指挥的态势与对话，故成员 `responsibility` 必须自包含。执行单元见 §三「执行单元：Team」。**会话协作图 / 中途换人等旧会话中心化 UI 随新壳移除**，后端 `switch_session_role`/`session-graph` 端点保留（旧壳为唯一壳）。

## 主代理 tick

态势收集 → LLM 工具循环（**build_team / execute / write_digest / done** / **只读查询三工具**）→ 结构化落盘；tick 租约单飞（TTL 900s + 每 LLM 步心跳续租）是编排层唯一硬互斥。**2026-10-06 任务退役**：删 `delegate`/`plan_work`/`cancel_task`/`requeue_task`，新增 `build_team`（建队）+ `execute`（亲自执行），工具表仅剩 build_team/execute/write_digest/done。**只读工具面**：`bb_overview`（资产覆盖/发现分级统计/事件尾部分区总览）/ `budget_status`（预算余量数字，不做闸门判定）/ `session_list`（窗清单）——**零写权红线**，tick 与 chat_turn 共用；用于核对执行者结论与自主决策，不替代派单执行（`task_detail` 随任务退役）。态势截断配套放宽：事件行/近期产出 result_note 截断统一 [:300]（渐进披露）。**覆盖度对账并入态势（M3 B2，2026-09-22）**：`_assets_view` 出口新增 `assets.coverage` = `core/coverage.py` `coverage_report` 分组收敛摘要（groups_done/converged 比值 + 每组一行「domain:x 收敛 n/m · 未收口: …」）——「全景对账」类数数任务归零；对账异常置 None 不阻断态势注入。**上下文预算（orch-context-budget，2026-09-27）**：全量段压常数级——`_stats.findings` 只进 top 20（verified/exploited 优先 → severity 降序，带 findings_total/findings_truncated）、`_stats.sessions` closed 出清只留活跃（带 sessions_closed 计数）、事件窗（tick 与 chat 两处）剔除纯观测 kind `llm.usage`/`llm.thinking.delta`（`recent_events` 新增 `exclude_kinds` 参数，游标照推不重放）——被裁细节走 `bb_overview`/`task_detail` 按需拉；**不做事件表归档/分表**（SQLite 查询非瓶颈，动表破坏 traces/tasktree 事件回放语义）。

### 覆盖度对账（M3 B1，2026-09-22）

`core/coverage.py` 纯函数查询层（地位仿 phases.py，不 import core.orchestrator），**对账面 = host/domain/url/service 四类资产**（与 _assets_view targetable 同口径，binary 不入组）。口径（宁严勿松）：

- **对账态（2026-10-09 改三态、2026-10-10 有洞解耦，见 §三「资产三态」）**：`status ∈ {tested_clean, na}` ∪ `meta.dead_end` 为**收口味**（可计入已测试干净）；**有 finding 挂链不参与状态判定**——显式 clean 的资产挂洞仍收口，只有**无显式状态**的裸资产挂洞由 `terminal-finding` 视作 visited（FP-only=死路味）；`visited/scanning` 算已访问；**budget_stop 算 visited**（预算停 ≠ 测完，宁严勿松）。判定顺序：状态机终态 > 死路标记 > 发现挂链 > 半程 > open。
- **收敛传播**：叶子 converged=自身收口；父节点 converged=有子 AND 全部子 converged——**父收敛只看子树，不要求自身 visited 佐证**（D3，2026-09-24 修订；host 的 IP 直连/服务面经子节点记账）。
- **归组 group_key**：沿 parent_id 链走到根——根 domain → `domain:<value>`、根 host → `host:<value>`、孤儿自成组 `<type>:<value>`（与 target_keys_of 归一化键口径对齐）。
- **输出**：`coverage_report(bb, pid)` → `{overall:{groups,groups_done,assets,converged}, by_group:[{group,total,terminal,converged,done,uncovered[]}]}（uncovered open 态优先排序）`。agent 侧 dead_end 标记路径随 M5/M6 的 F1 死路记账口径一起收口。
- 同文件另有 `effective_status_map` / `attach_effective_status`：资产树节点的根状态读时派生（口径见 §三「资产树归并」定稿块），与分组收敛对账是两个消费面，勿混用。

### 生命周期收编（M4 C1，2026-09-22；**2026-10-06 退役**）

`cancel_task`/`requeue_task` 两工具及 `TaskQueue.cancel_task` 写口随任务机制一并删除（`task.cancelled` 事件、`blocked_reason='cancelled'` 档、打断在跑窗逻辑均不再存在）。`run_cmd` 的 timeout 参数（默认 120s）与「长任务显式给 timeout+拆步」提示保留。团队 Run 的取消走 `POST /teams/{id}/cancel`。

## 任务派生与判据（goal 统一，2026-09-22）

**goal=唯一目标判据层**：meta.phase_goal（阶段目标，人类确认）判据居四层之首（goal > mission 存量 > judgment_templates.json 用户模板 > 内置默认，resolve_criteria）驱动 auto_derive 自动**建队/执行**（`_mission_on_done` 现读 `result.teams`）；原 v14 防碎发布闸 dedup_fp 随任务退役。原「作战计划/红队行动」弹层退役——mission {text, criteria} 写入口关闭（存量读兼容，判据解析居 goal 之下），目标编辑唯一入口=对话窗「🎯 设定阶段目标」（GoalEditor，含「应用模板」下拉；存删模板 API 端点保留）；auto_derive 开关与派生状态灯迁自主档弹层（🧠）。**行动边界段与目标解耦**（_mission_section 只管轨级行为语义）：redteam ROE 四要素注入（缺省按 pentest 上限兜底提醒；前端「🛡 行动边界」弹层编辑留档，仅 redteam）；pentest 恒注入「验证上限=影响证明级；禁驻留/持久化/横向/提权推进」。campaign 召回 query 换 goal.text 优先（mission 存量回退）。

## 进程调度两段式

**随任务机制退役（2026-10-06）**：原「绑定段（L1/L2 建待命窗）+ 启动段 + 六路触发点 + 60s sweep」调度层删除。Team 的启动改由**人类确认**触发（`TeamConfigDialog` 四项确认 → `POST /teams/{id}/start` 事务建 Run 并 fan-out 专属 Agent session）；自主档 L0/L1/L2 语义保留（L0 提案 / L1 建窗待命+审批 / L2 自动起跑），作用于建队与执行动作。

## 自主档与审批收件箱

L0 提案模式（propose_only）/ L1 建窗待命 + 执行审批单 / L2 自动武装起跑（`delegate_window` 审批 op 随任务退役，建队启动改走人类四项确认）；预算闸门 gate(action)；paused 项目级一次性闸，配置实时重读无缓存。**op 处理器白名单字典分派**（批准后动作只准分派绝不 eval）：spawn_session / escalation / phase_transition / **authorization（M5 D2，2026-09-23）**。**authorization 审批（行为边界授权申请）**：Agent 工具 `request_authorization(kind, scope_request, justification, evidence_finding_ids?)` 三 kind（scope_expand 扩大授权目标 / impact_escalate 影响证明升级 / rating_override 突破收录口径），risk 恒 high、恒人类决策（L2 无自动批路径）；批准=**纯回流**——处理器不做任何平台动作（scope_expand 后 Agent 自行 bb_add_asset 登记、rating_override 后按更高口径重新登记/patch），authorization_result 收件箱回流+message.inbox 事件；这是「打不上去」的显式出口（替代默默死路记账）。**rejected 回流**：escalation/authorization 被拒统一落 `approval_rejected` 收件箱（此前 rejected 无回流=Agent 空等真缺口），其余 op 拒绝维持只翻状态。**行动边界随审批卡出口**：`mission_boundary_lines(track, config)` 模块级共享（编排器 _mission_section 与 API approvals 出口同源），审批单每条附 server 拼好的 `boundary` 全文，前端审批卡对照当前边界审申请。

## C2 指挥指令

人类指令经 `_pending_directives` 以「⚠ 人类指令（最高优先）」注入编排器态势，轮末落 directive.done 留痕。**2026-09-21 对话化编排器上线后人机入口由对话窗替代**：`POST /orchestrator/directive` API 标 deprecated 兼容保留，tick 内指令消费机制不变（对话轮不消费指令）。

## 对话化编排器（M1-M4 全量，2026-09-22）

编排器专家化=给脸不重构：派单主循环（tick + 门控/配额/优先级）原样保留，对话是插队轮。

- **M1 对话通道**：`POST /orchestrator/chat` 同步抢 tick 租约（busy 409 不排队，前端不禁用输入只提示）→ 人类消息落 events `orch.chat {role:"human",text[:2000]}`（author=human）→ Job `orchestrator-chat` → `chat_turn()` LLM 工具循环（ORCH_TOOLS 与 tick 全闸门同源：dedup/gate/L0 提案/L1 审批；发布/开窗动作留 tool_trace）→ 回复落 `orch.chat {role:"orch",text[:2000],tool_trace}`。**无 tool_calls 即 break**（纯文本=回答完毕，与 tick 催促调工具的退出语义不同）；步数上限 `CHAT_MAX_STEPS=8`；每 LLM 步 heartbeat 续租；记账 source="orchestrator-chat"。对话历史 = events orch.chat 最近 40 条单一来源（human→user / orch→assistant，开头连续 orch 跳过），零新表零文件。
- **插队轮只读边界**：不推进 event_cursor（态势走 `_overview_for_chat` 固定最近 100 条事件窗）、不计 cycles、不动 last_digest_cycle、不消费 C2 指令（不标 directive.done）、不发饿死告警、不写 state_saver——巡检节奏状态零触碰。
- **M2 goal 闭环**：`meta.phase_goal={text, criteria[]（验收判据；goal 统一后为判据四层之首，自动派生朝它推进）, phase?, source:"chat", created_at, confirmed_by}`；`GET/PUT /goal`（text 空=剥键=清空重议）；`goal.confirm`/`goal.clear` 事件留痕（payload 全文快照，变更历史可回放）；goal 段「当前阶段目标（人类确认）」注入 tick 与对话轮系统提示；meta_loader 实时读 project.json meta（确认即生效，无需重启）。**goal 统一（2026-09-22）**：goal 升格唯一目标判据层（见「任务派生与判据」），判据来源经 resolve_criteria 四层解析供 L1 判跳闸与提示共用。
- **M3 虚拟单例专家**：`GET /api/experts` 恒追加 `{id:"orchestrator", kind:"virtual", protected:true}`（不入 experts/\*.yaml 文件池、不认领任务不执行命令、删不掉；带 pid 时 name 读 meta）；`PUT /orchestrator/persona` 写 `meta.orchestrator_persona`（display_name 贯穿页签/气泡，不注入提示；persona 只注入对话轮系统提示「## 你的身份」段，tick 决策语气不受影响）；专家池消费点（组队/管理面板/建项多选）按 kind=virtual 过滤。
- **前端编排页签三段式**：顶部 goal 条（引导设定 / 展示 text+criteria+phase，编辑/清空弹层）→ 中部对话流（OrchChatPane：human 右气泡 / orch 左气泡 MarkdownView + tool_trace 折叠，busy 显思考行，接近底部才自动跟随）→ 「运行记录」折叠区（原事件流剔除 orch.chat，防对话重复渲染）。composer 编排器态改双态「与编排对话（orch，缺省）/发任务」，**C2 指令态退役**；「决策」筛选含 goal.confirm/goal.clear。
- **M4 异常订阅唤醒（2026-09-22 实施；2026-10-09 增补 finding.new）**：黑板异常时编排器主动开口向人类简报——**触发白名单 4 kind**（`team.run.finished` 团队 Run 收尾〔完成/失败/取消〕→ 唤醒复盘 / `budget.soft_warning` 预算 80% 软警 / `phase.gate_open` 阶段出口门满足 / **`finding.new` 高危发现落库**〔严重度闸门 `WAKE_FINDING_SEVERITIES=("high","critical")`，低档静默；`finding.merged` 不唤醒〕→ 唤醒复判是否派生意图/加派子专家），kind 独立冷却窗（600s / 3600s / 300s）防轰炸；**零新表**——上次唤醒锚点 = proactive `orch.chat` 事件（`triggers` 字段 kind 级 id 防重 + created_at 冷却双维度），无锚点只看 `WAKE_LOOKBACK=1800s` 回看窗；`Orchestrator.collect_wake_triggers`（模块级纯函数，finding.new 摘要带级别+标题+资产）+ `wake_brief_text`（合成「〔主动唤醒〕…请向人类简报现状」user 消息，**只进 LLM messages 不落历史**——简报语境随轮消散属定稿口径；多条发现成组渲染）+ `chat_turn(wake=)`（唯一差异 = 回复 payload 加 `proactive:true, triggers:[kind]`）；API 层 `_maybe_orch_wake`：`_post_tick` 挡位闸前插入（**paused 不打扰，L0/L1/L2 全唤醒**），`app.state.orch_wake_pending` 防同项目重复提交，Job `orchestrator-wake` 内抢 tick 租约（占用静默放弃=宁少勿扰）+ 构造失败放弃 + LLM 异常 `_emit_llm_error`；前端 orch 气泡 🔔 主动唤醒徽章（amber，triggers 中文映射）。测试直调口 `app.state.orch_wake_check`。

## 分阶段工作流（pentest-phased-workflow M1-M4 全量，2026-09-22）

渗透测试项目三阶段推进：信息收集 recon ⇄ 渗透测试 pentest → 报告 report（可逆流转）。**重心+配额软引导，入场门是唯一硬约束**（方案 docs/plans/pentest-phased-workflow.md，实施修正注记见其 §0）。当前仅 pentest 轨配剧本。

- **阶段剧本**：`packs/tracks/<track>/phases/<phase>.yaml`（文件名 stem=阶段 id，`name` 中文显示；项目 `config.phases` 同名条目整体覆写、坏条目加载侧跳过不走 doctor）。字段 `name/goal/order/focus`（重心配额）`/gate`（min_assets / min_verified / idle_rounds；**min_high_value 已随人工高价值标签废除，2026-10-12**）`/gate_types/tasks`（task_type/role/objective/acceptance）`/next`。解析为受约束 yaml 子集（零 PyYAML，0/2 空格缩进 + tasks 项 4 空格续键）；doctor 体检 7 码（phase-bad-yaml / phase-tasktype-unregistered / phase-expert-missing / phase-next-missing / phase-field-missing / phase-gate-unknown-key / phase-gatetype-unregistered）。
- **引擎 `core/phases.py`**：load_track_phases / gate_metrics / evaluate_gate / enter_phase 等原语；**门评估状态原语 read/save_gate_state（M3 B3）**；不 import core.orchestrator（其反向依赖本模块），空闲轮数由调用方读 `orchestrator_state.derive_idle_rounds`（schema v21）传入。阶段状态存项目 meta（project.json 单一真相源）：`current_phase / phase_history / playbook_fired / gate_open_notified / phase_gate_state`（M3 门评估状态 `{phase, gate, passed, unmet[]}`）。
- **重心配额注入**：当前阶段 focus 按 task_type 权重拼「## 阶段工作流」段（阶段目标 / 配额建议 / 门进度）注入编排器态势（`_phase_section`），纯软引导——任何阶段可发任何任务类型。**门进度只读不重算（M3 B3 单一事实源）**：出口门行读 meta `phase_gate_state`（无状态/阶段不符=「待编排校准」）——注入在 tick 开始、判定在 tick 末，现算会用上轮 idle 制造矛盾窗口；派单撞门的实时拒绝仍由 `_phase_gate_reject` 现算（动作侧确定性不变）。
- **入场门（双层拦截）**：当前阶段 gate 未过且 task_type ∈ 该阶段 `gate_types` → 编排器派单侧拒收（`_phase_gate_reject`，拒绝原因回 tool_result）+ 发布 API 422（detail 带未达标明细）；认领侧不动。判定纯确定性；非 idle 指标全达标=过门，未达标但 idle_rounds 达标=收集饱和逃生。gate_types 每阶段自声明（recon=[exploit]、pentest/report 无）——若全局拦 exploit 会鸡生蛋（exploit 正是 verified 的生产者）。
- **过门分流（自主档）**：API 层 `_post_tick` 开头 `_phase_gate_check`（先于挡位闸，L0 也计；轮内 published→idle 计数归零否则 +1）：L0 落事件 `phase.gate_open` / L1 提审批单 `op=phase_transition`（pending 去重，批准即流转 by=approval）/ L2 自动流转（by=orchestrator）。**每次评估后无论过门与否都写 meta `phase_gate_state`**（M3 B3：无门阶段 gate=False；内容不变跳过写盘）——注入侧单一事实源；`enter_phase` 流转顺写新阶段状态，零空窗。set_gate_notified 抵达校准：进入新阶段时其门已过则标记已分流，防人工回退后被 L2 秒弹回；人工流转抵达剥键。
- **流转 API**：`GET /projects/{pid}/phase`（enabled/current/spec/**phases 全阶段序**〔M4 阶段条渲染用，按 order 排序〕/gate 进度/history）+ `POST /projects/{pid}/phase`（人工最终，不强制门但限 spec.next 内）；方向以 order 定：前向（递增）走门与自动分流，回退（递减）不走门只留痕。事件 `phase.changed` 留痕。
- **剧本首发**：显式流转进入阶段时 tasks[] 原样发布（`created_by=playbook`、acceptance→判据、**noise=passive**——阶段引导 objective 级不占 active 互斥键、priority=1）；`playbook_fired` 按（阶段，指纹）去重——回退重进不重发，同款已在队（find_dedup_target）=吸收指纹。**建项只登记初始阶段不直发**（`publish=False`；mission/目标商议前不发静态任务），首发随显式流转触发；读侧 current_spec 回落首阶段，门拦截与重心注入不依赖登记。
- **前端呈现（M4）**：项目顶栏阶段条 `webui/src/views/live/PhaseBar.tsx`——三阶段序 chips（当前高亮/到访过亮字/未到灰）+ 当前阶段门进度（达标绿「已达标」/未达标琥珀显 unmet 明细）+ 前向流转按钮（人工流转不强制门，422 原因行内显；到访过的阶段回退重进、剧本已发任务不重发），GET /phase 5s 轮询、轨无剧本（enabled=false）渲染 null；挂直播间页签行下，直播/任务流两视图共用。任务卡「📋 剧本」徽章（created_by=playbook）。事件样式：`phase.changed`「🔄 阶段流转」/ `phase.gate_open`「🚪 渗透门开启」（摘要=summary）默认展开，进「决策」筛选组。
- **报告链（finding-report-format M3，随本批落地）**：report 阶段剧本 acceptance = 出报告前逐条盘点 verified 发现收录三件套（impact 非空 / evidence.repro_steps 每步 desc+code+expected 齐备 / remediation 非空），缺项落事件流回补清单且报告附录列「待回补」项——不现编、不卡黑板写入；report-writer 专家 persona 按三节模板取字段渲染（危害描述取 impact、复现步骤按 repro_steps 逐步、修复建议取 remediation；pentest 主 persona 与 redteam 变体同步）。
- **M3 专家与内容（2026-09-22，内容专题见 dsh-kb-sourcing 方案）**：三剧本 tasks[] 充实——recon 三条（被动测绘 / 资产重要性评级 / 云面盘点〔role=cloud-security〕：对象存储·云控制台·云 API 暴露与前端 AKIA/ASIA/LTAI/AKID 凭证泄露面，凭据明文不落黑板）、pentest 两条（外部入口漏洞验证 / 云面凭证与配置缺陷验证〔role=cloud-security〕：只读 API 优先、变更性动作先过审批、verified 附四要素闭环可到达性证明）、report 一条（三件套盘点）；云安全专家 `packs/experts/cloud-security.yaml`（skills:[cloud-entry]，task_types:[recon,asset-enum,exploit]，tracks:[pentest]）落池。miniapp 面按 D5 后置（种子已入 web 包 kb，角色等内容攒够再挂）。

## 战役记忆召回

按 mission + 目标值从 campaign.db 召回 top-5 历史打法注入编排态势（单条截 300 字）；段头带元信息行「本项目 verified 发现 N 个（campaign 收 verified 产出/exploited 链打法与死路记账负知识）」+「仅参考——贴合当前目标再采用」提示（experience-sedimentation M2）——零产出项目看到跨项目打法时知道本项目还什么都没验证过。**注入侧按 tags 分两组（M6 F1，2026-09-23）**：死路条目（tags 含 dead_end）单独成「⚠ 既往死路（勿重走；确需重走先确认前提已变化）」组防误当正面经验，正向打法照旧带热度标——recall 池不拆（死路也是贴合目标的记忆），组内照旧打分排序。未注入/零命中/异常一律空段，召回失败绝不阻断编排。**打分升级（retrieval-upgrade M4，2026-09-23 实施）**：切分复用 kbindex `_query_segments`（就地导入防环；中文 2-gram+停用词替代整段 LIKE，「办公自动化系统」↔「OA 系统」靠共段跨写法命中；ASCII 词 ≥3 字符整词）；**字段加权 title 命中 2.0 > 正文独有 1.0**（双现段按 title 计不重复加分）；轨/能力加成与 `decay(线性 30 天)×log1p(usage)×1.5` 热度项沿用。campaign 黄金集 `tests/fixtures/campaign-golden.yaml`（mission 口吻 query→期望召回条目，top-3 必含）进 pytest 防打分链回归。

## 优先级重排 A5

**随任务机制退役（2026-10-06）**：`set_priorities`/`REPLAN_TOOLS`/`replan_priorities` 删除（无 open 任务可重排）。

## 饿死检测

**随任务机制退役（2026-10-06）**：`task.starvation` 告警与「未注册任务类型 / 无专才角色」检测删除。

# 五、执行网关（core/runtime）

## 隔离模型

L0 host < L1 wsl < L2 docker < L3 sandbox（--rm 一次性 / 断网 / 512m / cap-drop ALL 加固容器）；trusted 全等级、untrusted 仅容器、unknown 按 malware_live 最严处理。三条硬规则写死代码：WSL 信任级=宿主机、未知按活体恶意样本、net=real 不默认且必须绑定当前项目已批准的一次性审批。

## 渗透命令容器化（pentest-tools-container-m0 M1，2026-09-23 定稿并实施）

**大脑宿主、容器为手**：LLM/agent loop/编排器留 Windows 宿主（F6 浏览器/桌面壳/打包线/Windows 特调/调试链路五理由）；trusted 渗透命令引导走 Linux 容器——`docker` runtime 默认镜像 `cyberstrike/pentest-box:0.1`（Debian slim 三层：基础 curl/jq/dnsutils 等 → web 渗透 nmap/sqlmap/dirsearch/ffuf → python3+requests/pymssql/pycryptodome 等；tools/pentest-box/Dockerfile，`scripts/build_pentest_box.py` 构建）。workspace 传入时挂载 `<ws>:/workspace` 读写 + cwd=`/workspace/scratch`（对齐 host/wsl 相对路径习惯），pathguard 切容器内 posix 语义校验；sandbox 忽略挂载（L3 零挂载铁律）。引导面：run_cmd 描述 + STRICT_PROMPT_TAIL 第 9 条（docker=渗透首选镜像就绪时 / host=Windows 特调 / wsl=bash 兜底）；detector 查镜像缺失给构建指引。实战动机：环境摩擦耗 1/3 步数（PowerShell 转义/编码/工具链现装），容器一次性消除整类问题。M2 默认切换待实战反馈。

**出网口径（2026-10-11 修）**：L2 docker 的 `--network` 经 `docker_network(net)` 单一映射（backends.py，L2/L3 共用）——**real/bridge→bridge（NAT 出网）、none→none**。此前 `run_once`/`run_detached` 用 `net if net in {"none","bridge"} else "none"`，把 real 落 else 变 none：**docker+real 实际断网**，而 command 事件/审计仍记 `net:real`（与「显式要真网」语义相反）。实战事故（pentest-20261010-0a36，河南农业）：同一目标 docker+real 全 `exit 6`（curl 无法解析）而 wsl+real `code=301`，容器内 `getent/getent hosts` 空、路由表只有表头 —— 即此坑；agent 换 wsl 才通。语义说明：real 走 docker bridge NAT（**非** `--network host`），拿的是 docker 网段出口 IP。会话级常驻容器 `_Container.net` 记录建容器网络，net 变了 ensure 先 rm 再建（docker 网络不可热改）。

## 内置浏览器证书口径（2026-09-24 定稿并实施）

F6 内置浏览器持久化上下文（`core/browser/pool.py` launch_persistent_context）**默认 `ignore_https_errors=True`**——证书过期/自签/CN 不符一律放行加载（用户定稿）。风险边界可控：导航前必经 `check_target` 资产白名单硬校验，MITM 面只可能发生在已登记授权目标，且浏览器流量全程经 route 抓落入 http_history 可审计；`config/browser.json` 可设 `ignore_https_errors:false` 关回严格口径（`BrowserConfig` 白名单字段）。背景事故：目标 booklocation.zut.edu.cn 证书 2026-05-06 过期，Chromium `ERR_CERT_DATE_INVALID` 致 Agent 三次合理换路重试被错计为「拒绝熔断」挂起任务；与 curl `-k` 侦察口径对齐。

## 代理池（fir-proxy 托管，2026-10-07 M1 实施；2026-10-11 全局化）

**形态**：vendor 外部 fir-proxy（代理池工具）进 `tools/fir-proxy/`，平台侧 `core/proxy/pool.py` **全局单池托管**其 serve 子进程（仿 `BrowserPool`：`app.state.proxy_pool` 装配 + shutdown 兜关），池记录落 `data/proxy/pool.json`（全局归位，与 `data/campaign.db` 同款契约）。**人类 UI 与 AI 共享同一池**。

**全局化（2026-10-11，由项目级改造）**：此前「项目级」= 实例内部按 pid 分区（`_serves`/`_locks` 两个 dict + `<项目>/proxy/*` 路径 + 每个公开方法首参 pid），14 个项目各起一个 serve、各占 3 端口、各一份池——**池被切碎、抓取/验证重复**。现收敛为**进程级单 serve + 单池**：`_serve`/`_lock` 单实例、方法无 pid、路由改 `/api/proxy/*`（无 pid）、前端「代理池」升为全局视图（`needsProject:false`）。**旧项目池经 `merge_legacy_pools()` 首启并入**（按 proxy 地址去重，旧文件原地保留不删，仅全局池为空时执行一次、幂等）。**`delete_project` 前置链不再停代理池**——池/日志已移出项目工作区，无句柄需释放。**AI 侧 `fir-proxy` MCP 不注入 `PW_PROJECT_ID`**（`mcp_bridge.PROJECT_AGNOSTIC_SERVERS`）。语义依据：fir-proxy serve 是**纯代理转发，与项目 workspace 无关**（不承担免二次认证的出口角色），故全局单 serve 正确；单 serve 亦无并发保活需求（多项目共享不互关）。

**为什么托管而非塞进 MCP 子进程**：本项目 MCP server 是平台按项目托管的**外部子进程**（`app.state.chat_mcp_bridges` 按 `proj.id` 缓存），随会话/轮次起停、崩溃重拉；serve 与池若住进去会随 MCP 生死、多项目各起一个、UI 还得反向依赖 MCP 在线。

**不动上游**：上游 `cli.py serve` 是**一次性、文件驱动**的长驻进程，**无运行中控制口**（查不到实时当前代理、无法手动轮换）。故本仓新增薄 runner `tools/fir-proxy/cyberstrike_serve.py` 包上游 `ProxyRotator`/`ProxyServer`，补 **loopback 控制通道**（`/status` `/proxies` `/rotate` `/reload` `/stop`，只绑 127.0.0.1）；stdout 走 JSONL（`started`/`stopped`/`error`）供平台解析，日志走 stderr，启动强制 UTF-8。**不改 fir-proxy 自身文件**（便于上游同步）。

**端口与依赖**：三端口（http/socks5/control）从 base 起**动态取空闲口**（单 serve 无冲突）；解释器自动解析（`tools/venv` → `sys.executable` → PATH），缺 `requests[socks]`/`bs4`/`lxml` 时 `proxy_available()` False → 端点 **503 结构化**，**不静默降级**。

**AI 用法（甲）**：MCP 控制面给 http/socks5 端点 + 可用 IP 列表，**AI 自行 `curl -x`**；平台**不**注入 browser/run_cmd 的代理（用户定稿）。

**API**：`/api/proxy/{status,start,stop,proxies,rotate,add,remove,select,fetch,validate}`（**全局，无 pid**；后两者 202 Job）；UI 顶级「代理池」视图（`webui/src/views/proxy/`，全局 `needsProject:false`）。

**抓取/验证口径（2026-10-08）**：抓取走平台薄 runner `tools/fir-proxy/cyberstrike_fetch.py`（**内置精选源清单 + `--sources` 可覆盖，不改上游**；**单次限量 `fetch_limit`〔默认 2000，按协议轮转取〕**）——免费源普遍 90%+ 死代理，换源收益有限，真正提升靠验证闭环。**抓取/验证 Job 汇报 `meta.progress`**（可变 dict 引用，相位 fetch→check→prune + `done/total` + `eta_seconds`/`elapsed_seconds`）→ 前端代理池状态栏显进度条与「预计剩余」。**验证走平台薄 runner `tools/fir-proxy/cyberstrike_check.py`**（复用上游 `check_proxy_url`，不改上游）：经代理 GET 一个**可配置目标**（`validate_target`，默认 `http://www.baidu.com`，走 80 口、不强制 CONNECT），拿到响应即 `Working` 并记延迟——**不用**上游 `cli.py validate`（其判据要求 HTTPS-CONNECT + httpbin + 测速**全过**，实测同批 120 只过 9 → 池被清空；宽松单目标同批过 52）。**抓取后自动验证、验证即清理**：`status≠Working` 或延迟 > `prune_latency_ms`（默认 5s）的代理从池中**物理删除**；验证经 `_apply_validation` 按地址 merge 回池 + 单次 `/reload` remove，不再整体覆盖池文件。前端「在线抓取」「验证全部」复用共享 `pollJob` 并回执清理计数。

**自动巡检 + 协议过滤（2026-10-11）**：① **后台巡检线程**（`create_app(proxy_maintenance=True)`，daemon，每 `maintenance_interval_s` 默认 1800s）——验证全部 → 按协议计数低于阈值（http 50 / https 20 / socks 20）自动抓取（`auto_fetch_cooldown_s` 节流防风暴）。② **协议过滤**（前端 chips）分 http / https / socks 三维：`http`=协议类目、`socks`=socks4+socks5 合并、**`https`=能力探测**（池本无 https 协议类目——上游把 https 源并入 http；故经 CONNECT 另测一个 https 目标，结果记 `https_ok`，**不参与 status 判定**，只作能力标注与阈值判据）。进程内单 serve 无并发保活需求（多项目共享不互关）。

**内容注入/劫持型出口交叉验证（2026-10-11）**：免费代理中存在**重写响应体**的出口——对**任意目标 host 的 `/` 都返回同一伪造页**（如 334 字节跳转页、固定 ETag），使早期「目标是个 SPA/特定 Web 客户端」的探测结论成为**误判**（可信出口应对 example.com 等返回与直连一致的内容、且支持 CONNECT）。故验证在单目标连通性之上加**多目标一致性**交叉验证：对已通记录再读两个**内容不同**目标（`validate_target` / `validate_target_b`，默认 baidu.com / example.com）的响应体指纹（`状态码:长度:sha256前16`），**两目标回相同指纹 → 判 `hijacked`、`status=Failed` 剔除**（`merge_keys` 带回 `hijacked` 标志）；探测本身失败不误杀。这是「同一出口对不同目标给出同一内容=内容被劫持」的判据——纯内容指纹、无外部基线依赖，离线可测。

**待续**：M2 MCP 控制面（`mcp-servers/fir-proxy-mcp/`，`config/mcp.json` enabled 开关）与 M3「放开红线让 Agent 发起重放/爆破」见 `docs/plans/proxy-pool-integration.md`。

## Web Fuzzer 重放工作台与国密 TLS sidecar（2026-10-07 实施）

**背景**：单条重发原先是 F6 浏览器右栏（300–560px）里的一个原始报文 textarea，改包→发送→看响应的秒级 POC 迭代被空间局促与无历史拖累。升格为**顶级「重放」视图**（前端 `webui/src/views/fuzzer/`，轨门控 pentest/redteam/ctf）：左 raw 报文编辑器 / 右完整响应（react-resizable-panels 默认 50/50），工具条 = 发送（Alt/Ctrl+Enter）/ 停止 / 构造请求（表单建包）/ 历史抽屉 / 强制HTTPS / 国密TLS / 跟随重定向 / 跳过证书校验 / 设置代理 / 响应体长度限制；响应区含状态行（状态码·耗时·字节数）+ 响应头/体 tab + JSON 美化 + 字符集重解 + 定位。入口走 `goto-fuzzer` CustomEvent（抓包行「去重放台」、直播间顶栏 🧪）。

**传输双路**（`core/browser/replay.py::ReplayClient`）：默认 httpx（`ReplayOptions` 全量对齐控件：`force_https`/`follow_redirects`/`proxy`/`body_max_bytes`/`insecure`/`timeout_s`/`server_name`/客户端证书与 CA；恒 `trust_env=False`，**显式 `proxy` 是用户主动指定**，与「不打系统代理」红线不冲突）；勾「国密TLS」切 `gmhttp` sidecar。失败/中断也入库（status=None + meta.error），meta 记 `gm_tls`/`proxy`。**中断语义**：httpx = 放弃等待（sync 不可真中止），国密 sidecar = kill 子进程（真停止）。API：`POST /browser/replay` 返回 `{job_id, run_id}`，`POST /browser/replay/{run_id}/stop` 中断，`GET /browser/gm-status` 能力探测。

**国密 TLS 为什么外挂 Go**：Python 的 `ssl` 是 OpenSSL 薄绑定——本机 OpenSSL 未编入 SM 密码套件（`get_ciphers()` 仅 3 个标准 TLS1.3 套件、任何 SM 名报 `No cipher can be selected`），Python 又未暴露 `set_ciphersuites`；PyPI 的 gmssl/gmalg/pygmssl/gmssl-python 只有 SM2/SM3/SM4 **密码学原语**（`gmssl-python` 文档明写「只能覆盖除 SSL/TLS/TLCP 之外的功能算法」），`tlslite-ng` 纯 Python TLS 亦无 SM4/SM2。各生态通行解法是自带 SM 套件的 TLS 栈（Go `tjfoc/gmsm/gmtls` / Java BouncyCastle / C 侧 Tongsuo·GmSSL），**Python 无对应物**；用原语手写 TLCP 属研究级安全关键工程，不该由 UI 开关承载。故引入 `tools/gmhttp/`（Go + gmtls，纯 Go 零 cgo，一次性进程 stdin JSON → stdout JSON、body base64），构建 `scripts/build_gmhttp.py` → `tools/bin/gmhttp.exe`（registry `acquire=bundled`，桌面打包随包旁挂）。

**红线**：**二进制缺失 = 国密不可用**（`gm_available()` 探测 → 前端开关置灰 + 安装指引），**绝不静默降级成普通 TLS**——否则会制造「以为走了国密、实际没走」的安全假象。**重发/爆破 2026-10-07 放开红线**（`proxy-pool-integration` M3）：Agent 工具面新增 `browser_replay` / `browser_intruder`，与人类 UI 共用 `ReplayClient`/`Intruder`，两者均可传 `proxy` 走代理池做 IP 轮换；**拦截裁决仍人类 UI 专属**。目标不设门禁（授权边界由使用者负责，与 F6 浏览器同口径）。

## 网关流水线

`run(cmd, runtime)` 九步序：would_deny 干跑（与真实执行同口径，杜绝两处校验漂移）→ 网络模式缺省 → net=real 项目上下文与已批准一次性审批校验 → 工作区隔离改造（cwd/TEMP 重定向；docker 卷挂载）→ 限速检查 → 执行前审计事件 → 按后端执行 → 结果审计 → 审批一次性消费 → 拒绝统一协议（GatewayDenied 回填改道不炸循环）。

## 静态护栏双子星

pathguard 写逃逸静态判定（只拦写不拦读；**命令词跟踪：管道/分号重置命令边界，grep 族 `-o*` only-matching 豁免**——模式串前导 / 曾被当写目标误拒，nmap -oG 写文件仍拦，2026-09-23；**重定向目标在引号外 `;|&` 处截断**——`2>/dev/null;` 粘连写法曾把 `/dev/null;` 当绝对路径误判逃逸，实战 43 条拦截中 23 条误拦，2026-09-25 修复）+ rateguard 扫描频控（拦的是「不限速地扫」，拒因文案自带放行配方）。

## 后端三实现

host（PowerShell/bash）/ WSL（env 不透传；**`--exec` argv 直通**——包装层剥引号吞 `$var`/awk `$1`，2026-09-23）/ Docker（run_once 挂载+cwd + exec_in pwn 交互）；协作取消三入口收 abort_event，0.2s 轮询杀进程树，宁误杀勿悬挂；**管道排水**（stdout=PIPE 时 daemon 线程持续消费，防 64KB 管道缓冲写阻塞死锁，2026-09-23）；**NO_WINDOW_FLAGS** 全创建点隐藏窗口（pythonw 桌面模式弹 PowerShell 窗根治，2026-09-23）。

## 会话级常驻容器与容器回收（2026-10-07 实施）

**背景（根因）**：`DockerBackend.run_once` 的 `--rm` 只在 L3 sandbox 分支加了，**L2 `docker` 分支漏传**——而渗透命令走的正是 L2（带 `-v` 挂载那条路），于是每次 `run_cmd(runtime="docker")` 都留一个 exited 容器、只增不减，累积上千个后 Docker Desktop 仪表盘（Electron）枚举全量容器 + 逐容器轮询 stats，UI 线程被拖死（用户报「仪表盘卡」的直接原因）。项目文档 `docs/plans/container-execution-architecture.md` §2 早在 2026-09-23 就记录过此坑。

**修复三层**：
1. **止血**——L2 分支补 `--rm`，与 sandbox 语义对齐（一次性容器跑完即焚）。
2. **可识别 + 自愈**——常驻容器用确定性名 `csp_<pid>_<sid>` + labels `csp.managed/csp.project/csp.session/csp.runtime`；`SessionContainerManager`（`core/runtime/sessioncontainers.py`，照 `ida_mcp_manager` 同构：懒 reaper + `shutdown_all` 兜关）启动对账清遗留、周期 TTL 回收（`config/containers.json` 覆盖，`session_scoped:false` = 一键全局回落）。
3. **消除 per-call 开销**——**会话级常驻容器**：会话首次执行 docker 命令时 `docker run -d … sleep infinity` 起一个容器，后续命令走 `docker exec`，把 create/destroy 从「每工具调用一次」降到「每会话一次」。作用域锚点 = **会话**（`sess-<12hex>`）——原「一任务一容器」设计的任务锚点随任务机制退役（schema v33，2026-10-06），改由会话承接；workspace 仍是项目根卷挂载（同项目多会话各持一容器，共享挂载）。

**边界铁律**：只作用 **L2 docker**；**L3 sandbox 恒 per-execution**（零挂载/断网/`--rm` 不变），网关 `_dispatch` 里 sandbox 分支前置判定、永不经管理器。网关 `ExecutionGateway(containers=...)` 默认 None（关闭）；仅 `sess-*` 会话走 exec，其余（`chat-*`/`rev-workbench`/无会话）与建容器失败一律回落 `run_once`——**绝不因新机制让命令执行失败**。会话关闭删容器、项目删除前按 label 停删（释放 Windows 卷挂载句柄，否则 rename 必 422）、shutdown 钩子兜关清空。workspace 是宿主卷 → 容器 rootfs 即弃，故进程重启直接对账清空、下次按需重建（~0.5s），不做跨进程续用。

## 双层权限模型

角色仅通过工具白名单约束可调用面；网关 threat_class/runtime 负责执行环境硬校验。人类命令同层经网关，审计流无旁路。

## 审计与体验

command / command.result 事件时序协议（BackendError 时悬空运行中=语义诚实）；截断可见化标记（防 Agent 语义猜）；detector 能力探测结果注入系统提示（Agent 不许自己猜环境）。

## 策略快照可视化（gateway-config-view M1，2026-09-23 定稿并实施）

## Agent 工具目录可视化（agent-tools-view，2026-09-24 定稿并实施）

设置页新增「工具」页签（网关之后）：Agent 工具**静态只读目录**——`GET /api/agent-tools` 直出 `AGENT_TOOLS` 全集（name/description/input_schema）+ 静态分组，无项目依赖、无 IO、无敏感字段。分组由 `core/agent/tool_registry.py::agent_tool_group` 读 `ToolSpec.group` 给出，组序固定：**执行/文件/黑板/知识/浏览器/协作/计划/控制**。前端 `AgentToolsPane`：名称/描述搜索 + 分组卡片 + 参数表（类型/必填*/enum chips/说明，类型字段按数组/anyOf 兜底渲染）。端点测试断言「无工具落其他」=新工具漏配分组的拦截器。**不做**：工具编辑（定义是代码）、角色 yaml 白名单/browser 依赖/网关 runtime 的动态可用性标注（后置观察）。

**工具注册表（tool-registry，2026-10-03 实施）**：上述目录此前靠六处耦合维持——`AGENT_TOOLS` 静态表 + `TOOL_GROUPS` + `_CONTROL_TOOLS`/`_PLAN_TOOLS`/`_INTENT_FLOW_TOOLS`/`_COLLAB_TOOLS`/`_KNOWLEDGE_EXTRA`/`_FILE_TOOLS`/`_SPILL_SKIP` 各集合 + `agent_tool_group` 前缀规则 + `_tool_<name>` 命名约定；新增一个工具要同步改 4-6 处，漏一处即**静默**改变计划闸/意图闸放行面或让工具落「其他」。现收敛为 `core/agent/tool_registry.py` 单一真相源：`ToolSpec(name, description, input_schema, group, flags, handler)` 一处定义，`flags`（control/plan/plan_pre/intent_flow/intent_pre/spill_skip/collab/knowledge/file）是闸门口径的声明式表达，`AGENT_TOOLS`、各白名单常量、`_PLAN_PRE_ALLOWED`/`_INTENT_PRE_ALLOWED` 的推导关系、`agent_tool_group` 全由注册表派生——**导入名逐字保留**，`core/api/app.py`/`loop.py`/`chat/runtime.py` 零改动。三道防漂移：`tool()` 装饰器当场拒重名/非法 group；`tools.py` 末尾**导入期自检**逐个校验 `ToolSpec.handler` 在 `ToolDispatcher` 上真实存在（此前要运行到该工具才暴露）；`tests/test_tool_registry.py` 把 flags 推导出的常量与迁移前快照**逐集合**比对（放行面等价性回归网）。新增工具的成本从 4-6 处降到 1 处（注册一段 + 补 `_tool_<name>` 实现）。

设置页新增「网关」页签（MCP 之后）：**只读快照 + backends 实况**，单一事实源仍是代码——`GET /api/gateway/config` 做代码→JSON 映射（runtime_levels 直出 policy 语义 / threat×runtime 放行矩阵〔unknown 按恶意行〕/ 网络三档 real 标「须审批」/ pathguard 手工语义摘要〔测试断言条数防遗漏〕/ rate_rules 直出 `rateguard.RATE_RULES` 表〔与 `_RULES` 校验函数表键一致性测试，防新增工具忘登记；masscan 阈值等数值参数从函数体迁入表，校验函数读表〕/ exec_params）。**backends 实况**：复用 `app.state.inventory`（启动探测结果）+ `POST /api/gateway/probe` 手动重探测（替换 app.state.inventory 即生效——后续新开窗系统提示注入随之更新，不做 TTL 缓存）；顶部结论化告警条：docker 不可用=「⚠ L2/L3 通道不可用：不可信代码与恶意样本任务将被拒绝（宁严勿松：拒绝不降级 host）」硬警告、wsl 不可用=弱提示（与 host 同信任级影响有限）、全绿=四通道就绪；四通道卡（L0-L3 徽章+可用性圆点+detector detail）+ 工具探测清单（工具面板最终归 toolchain-registry，此处只读快照为临时归宿）。**安全红线**：THREAT_ALLOWED / unknown→按恶意 / real 须审批等宁严勿松策略**永不前端可编辑**——前端改安全边界=配置漂移破坏 §7 纵深，页签底部显性注记。后置不做：参数可配置化（等 fakenet/专用分析镜像/toolchain-registry 里程碑）、直播间拒绝事件深链跳页签。

# 六、技能与知识体系（core/skills + packs/）

## 场景轨 × 知识方向（2026-10-13 能力包退役）

**能力包（web/binary/crypto/forensics/misc/cloud）已退役**——不再是项目绑定维度，降级为 kb **知识方向**（资料组织单位）。唯一内容根 `packs/kb/`：`skills/<name>/`（技能**扁平**、cc 风格自包含，registry 只扫这里；无包/轨归属）、`<track>/`（场景轨 ctf/pentest/redteam/research：track.yaml / task_types.yaml / profiles/ / phases/ / dimensions.yaml）、`<方向>/`（web/binary/… 资料树，不进 registry）、`安全红线/`（`轨红线/<track>.md` 全量注入 + `平台规则/<tag>.md`〔owners〕+ `评级与价值口径/<tag>.md`〔F11 判级〕**后两者渗透独有** + `templates/`）。平台/格式/漏洞类只走 frontmatter 标签不建包。LEGACY_DOMAIN_MAP/LEGACY_TRACK_MAP 读取层兼容旧轨/旧域。kb 源按方向合成恒递归（`load_kb_sources`），kb_open module 用全局形态 `<方向>/<快照>/<路径>`。

**知识树状化重构（2026-10-14 一次全量）**：各方向内部由旧的「英文快照平铺」（recon/webapp/playbooks/refs/notes…）改为**三层知识树**——一级=方向（运行时轨名不可动）、二级=**中文类目模块**（如 `web/漏洞分类`、`binary/逆向工程`、`crypto/非对称加密`）、三级=模块内类目/英文小类。旧 `refs/` 原料库按类别拆入各方向模块（用户决策：中文化类目名、拆入知识树）。分层语义：知识模块=收编版（可就地修订）；`<方向>/赛事专题/题目知识/ctf-<x>/`=上游 ctf-skills 快照原件（不翻译，`import_kb.py --force` 只会重建该目录），两者字节相同的副本已去重（正本留收编版，快照内被去重文件的引用改写到正本）。迁移脚本 `scripts/refactor_kb_tree.py`，链接修复 `scripts/fix_kb_rel_links.py`；各方向 README 为策展导航。

**项目绑定** = 场景轨 track（单选）× 专家组队 experts（多选可空）。kb 可见方向由 **`directions_effective(packs_root, track, experts, fallback)`**（原 caps_effective）推导——有绑定→专家 yaml **`knowledge` 白名单**并集（任一专家 knowledge=null → 全部方向 `list_directions`）；无绑定→fallback（meta.capabilities，存量直通零翻译）；fallback 空→轨默认（`TRACK_DEFAULT_CAPS`：research→binary）。专家 yaml **`skills` 白名单为硬选**——会话可见/可路由技能=绑定专家并集（`expert_skills`，null/空=不限定=全量）。推导只发生在 API meta 响应层（`directions` + `directions_bound`=盘上原始绑定）与 AgentSession 构造层，盘上原始绑定不动（`Project.capabilities` 恒返回盘上值）。前端创建页无 caps 选择器（能力包退役）。

## 四层知识体系

SKILL.md 薄路由入口 → kb/route.json 任务导航表（**M0 留域内** `packs/kb/<cap>/route.json`，值带域前缀）→ `packs/kb/route_index.yaml` 全局单表测试点索引（**M0**：条目 kb 全局形态，load_route_index 按启用域过滤视图注入）→ kb/ 厚方法论手册与弹药。route.json 值为目录前缀（如 `cloud/native/k8s/`）时，**渲染时展开为具体文件**（route-injection-hardening，2026-09-24：`kbindex._expand_prefix` 取前 3 篇 `*.md`〔排除 .history〕+ 目录总篇数提示，数据形态不改；声明目标物理不存在则原样透传，由 doctor 兜底）。

**Android 子域与 K6 案例沉淀（2026-09-21 实施，源自 android-kb-sourcing 方案 M1+M3）**：binary 域新增 `android/` 子域（r0re 收编改编，上游无 LICENSE 文件经用户确认可直接收编）——4 篇手册 `triage-and-layering` / `native-five-lines` / `unpacking` / `godot` + index + cases/；binary 第 4 技能 `android-rev`（file_features=`is_apk/has_native_lib/has_jni/godot_engine/packed_so`——实施期核实路由匹配语义：分诊 Agent 声明走 `file_features` 查询参数，只匹配技能 frontmatter `file_features` 字段，故特征词须落该字段而非 `features`〔情景特征〕，挂 reverse-analyst〔research〕/reverse〔ctf〕两专家白名单，caps_effective 域面不变）；route_index 全局单表增 4 条（binary 域 9→13）。**M2 工具收编（2026-09-23）**：r0re 的 5 件 python 分析脚本落位 `tools/py/android/`（分诊 runner android_ctf_runner / godot_ctf_runner / 脱壳三件套 upx_shlib_emu+apply_relocs+nrv2b，纯 stdlib 或仅依赖 unicorn），registry 声明三件 bundled python-tool（见 §工具链注册表）；android-rev 技能补「自动化工具面」引导段——缺失/依赖未装**不空等安装，降级手册手工路径**（DECOMPILE_GUIDANCE 先例）。**K6 案例格式约定**：已解案例沉淀 `<域>/<子域>/cases/<案例id>/`，五段式（识别 markers / 解题路径 / 验证向量 / solver / 复用提示），verified 攻击链经人审闸门沉淀防自投毒——与既有 case 提案（测试包 `成功案例.md` 段 + `payloads/` 弹药）并存：kb 树内 cases/ 是方法论文档形态，首例 godot-sec2026（flag_algo.py 正反向向量自测通过）。

**cloud 域与 dsh 知识库收编（2026-09-22 实施，源自 dsh-kb-sourcing 方案）**：第 6 能力包 `packs/capabilities/cloud/`（pack.yaml + 薄路由技能 `cloud-entry`〔唯一改写件：dsh cloud-playbook 剥离七门门禁/operation-state 台账/子代理编排等平台机制，保留四要素闭环攻击主线 + 六源凭证入口 + 提级序/凭证循环/信任链横向战法 + 8 行特征→kb 路由表 + 反空转规则；frontmatter features 8 项 has_cloud_meta/has_aksk_leak/has_bucket_public 等〕+ rules/redlines.md 草案〔AI 起草待人审〕）。kb 新建 cloud 域（快照纪律原样搬运）：knowledge 5 + native 16 + vendors 36（aliyun/aws/azure/gcp/huawei/tencent × 6）+ detection 4 = 61 篇 + README 索引；`kb/cloud/NOTICE.md`（MIT © 2026 SeaOf0 声明 + detection 蓝队辅助视角标注）与 `kb/licenses/dsh-redteam-model-MIT` 双落点；域内 route.json 12 键 + route_index 全局表增 9 条（90→99，无 tags 全角色可见）。miniapp 种子 5 篇并入 web 包 `kb/web/miniprogram/`（独立包等内容攒够再拆）。指纹包首批 `tools/data/fpdb/fpdb_seed.json`（dsh asset-mapping，规则制 83 规则 + 16 path_rules，pentest 指纹三段式第二段「本地指纹包」落位；registry data 类声明随 toolchain-registry M1）。专家池 +cloud-security（16→17，见上节）。

## 运行时路由链路

评分公式（features/标签 ×3 > task_type 命中 +10 > 角色偏好 +5 > keywords ×2）每次落 skill.routed 审计（payload 携带 kb_hits 供检索对账）；kb hints 中文 2-gram 段落级索引；测试点 Top-5 注入 + route_lookup 按需查，命中技能正文不整段进 system（控上下文膨胀）。

**匹配口径统一（route-injection-hardening，2026-09-24 实施）**：router keywords、route.json 键（kb_route_hints）、route_index match（score_entry）三处收口到共享原语 `core/skills/matching.py::term_matches`——ASCII 词走**词边界**正则 `(?<![a-z0-9])…(?![a-z0-9])`（underscore/连字符/标点=边界；短词 `ak/sk/ai/pe` 不再误命中 `make/stack/main/expense`），中文词=子串（中文无词边界，行为不变），尾缀 `*`=前缀语义（ASCII 只保留 lookbehind，现行仅 `ret2*`/`house*`）；各增强层坏了静默降级不阻断主链，route_index.yaml 单文件解析**维持 fail-fast**（doctor 立即报 route-index-bad-yaml）。注入标题去歧义：测试点注入 head=「📖 测试点手册索引」。Agent `read_file`/`search_files` **读路径放开（2026-10-01）**：宿主侧读工具不再限定工作区根（只有**写**——run_cmd 的 pathguard——才拦边界），容器 `/workspace/…`（docker 挂 <ws>）与 WSL `/mnt/<盘符>/…` 绝对路径经 `_resolve_read_path` 映射回宿主真实路径——容器/WSL 会话里模型据此能读回自己产出的 scratch/spill 文件（此前一律 `[拒绝]`）。

**检索质量升级（retrieval-upgrade M2+M3，2026-09-23 实施）**：**同义词查询扩展**——`packs/kb/synonyms.yaml` 全局单表（groups: [{id, terms}]，21 组起步，提案制演进），objective 命中组内任一成员（lower 子串）→ 全组成员各自 2-gram 切段并入匹配（查询侧扩展、索引侧零改动；SkillRouter keywords 暂不挂防误报面扩大）；doctor 体检四档（synonyms-missing info / empty warning / dead-group warning 全成员零命中 / idle-term info 部分空转）。**漏召回对账**——`GET /api/projects/{pid}/retrieval-stats` 离线统计（traces.retrieval_stats，扫近 5000 事件，per 任务窗切分）：hinted_opened（提示∩打开）/ hinted_not_opened（低优，提示质量）/ opened_no_output（正常试错）/ **missed**（打开且产 verified 但未进 kb_hits=漏召回信号，审阅后转同义词/route_index 增补提案）。**检索黄金集**——`tests/fixtures/retrieval-golden.yaml` 20 条人工标注（中文任务口吻 query→期望命中 kb 模块，命中判定=cap4 hints 内出现任一 expect，良性命中扩 expect 不算错），pytest 断言 recall@4≥0.8 + expect 路径存在自检；**匹配链任何改动（词表/索引/停用词/切分）必须跑黄金集防退化**。

**中文检索与中文技能名（2026-10-19 实施）**：两处中文支持缺陷的修复。①**中文问句检索零命中**——kb_search 把中文串切 2-gram 后按 **AND** 拼 SQL，「如何测试文件上传漏洞」切出的 何测/试反 等词元跨词边界、语料里不存在，全 AND 必然零结果（实测该问句修复前 0 命中），等于要求用户把自然语言改写成关键词；现新增 `matching.py::query_tokens` 统一切词（最长匹配剥虚词表 `_QUERY_STOPWORDS`，**含单字虚词** 的/了/和——只按 2-gram 过滤会剩「过的」这类跨虚词边界碎片，它在语料里真实出现能骗过覆盖率门槛；剩余实义片段 ≤4 字整段保留、>4 字切 2-gram），kbsearch（FTS 索引）与 writing（legacy 扫描器）共用同一口径，严格 AND 零结果时走 **IDF 加权覆盖率降级**：ASCII 词元恒为硬约束，中文词元按 df 加权、泛词（df>60% 语料）剔除，命中权重占比 ≥0.34 **且** 命中词元数占比 ≥0.6 双门槛才入选（缺覆盖率门槛则泛词刷权重，缺词数门槛则「不存在的专题xyz」这类全泛词查询蒙出无关结果）。纯 ASCII 查询行为不变（FTS + OR 回退），检索黄金集 recall@4 未退化。②**中文技能名一半操作被拒**——`writing._SKILL_NAME_RE` 是纯 ASCII 白名单而 API 层 `app._NAME_RE` 用 `\w`（Unicode-aware），设置页能建出 `skills/注入测试/`，提案 create/delete 走 writing 时却判「非法技能名」；现两处统一为 Unicode 词字符口径 `[\w][\w.-]{0,63}`（仍禁路径分隔符、Windows 非法字符、尾随空格/句点），中文技能名的创建/注册/skill_open 读正文与资源/删除/提案落地全链路打通。护栏 `tests/test_kb_chinese.py`（33 例：切词口径、自然语言问句召回、泛词查询零结果、ASCII 行为不变、legacy 与索引同口径、命中次数排序、中文技能名全链路）。③**排序退化为字母序（同日追加）**——覆盖率降级的 `cov` 只记「词元命中与否」，同批词元都命中的文档 cov 必然完全相等（实测「如何测试文件上传漏洞」10 篇全是 cov=9.524/hits=5），而 `layer` 在现网恒为 1，`ORDER BY cov DESC, d.layer DESC, d.relpath` 于是退化成 relpath 字母序：正文命中 758 次的《文件上传漏洞.md》排到第 3 位，`提权` 的首位是命中 3 次的越权 PoC 而非命中 36 次的 `12-privesc.md`。修复：`_coverage_rows` 与严格 AND 两条路径都加 `tf` 次级键（`(LENGTH(c)-LENGTH(REPLACE(c,term,'')))/len(term)`），**必须写进 SQL 的 ORDER BY**（Python 侧重排会被 `LIMIT` 先按字母序截断）；`writing._kb_scan_coverage` 与 `search_kb` 的 Python 排序同步加 `matches` 次级键。tf 的 REPLACE 占位符要传纯词元（传 `%term%` 恒为 0）。

## 角色与规则链

角色（M2 起实现为专家，见下节）字段全是软边界（skills/task_types/knowledge 为 null=不过滤），缺专家静默回退 _generalist，expert_exists 防拼错放行；规则链 轨红线（硬红线，全轨恒注入）/ 平台规则（授权边界，按资产 owner tag，**渗透独有**）/ 评级与价值口径（判级口径，**渗透独有**）分层注入，resolve_rule_profiles 纯显式解析（2026-10-01 去三态：勾哪个生效哪个，未配=不注入）。**role-rules/ 通道随能力包退役删除（2026-10-13）**。

## 项目规则四段一体（rules-four-section M1，2026-09-23 定稿并实施）

**目标形态三级模型**（轨默认 redlines → 模板库 → 项目实例，与 judgments 三级优先同构）：规则从散文转「frontmatter 结构化 + md 正文」（D1），owner 标签变模板选择器（D2），判据并入四段（范围/规则/评级/判据，D3）。**M1 已实施部分**：`core/skills/rules.py` 增 `parse_rule_doc`（frontmatter PyYAML + 正文分离，缺失/坏 yaml 容错回空 meta——散文永远可注入，结构化是增量红利）+ `validate_rule_meta`（schema fail-fast 对齐 registry 惯例：`trigger`〔=文件 stem，owner 标签命中即选〕/`scope.in·out`/`forbidden`/`uncollectable`/`noise_caps`〔键=task_type、值 ∈ {passive,low,medium,high}=噪声**上限**档〕/`rating_ref`〔self=正文含判级条款〕）+ `load_rule_templates`（`packs/kb/安全红线/templates/*.md` → [{name, meta, body}]，读取侧降级容错；2026-10-13 前为 `tracks/<轨>/rules/templates/`）。doctor 增两体检：rule-template-invalid（error，schema 不过）/ rule-template-no-trigger（warning，M2 选择器按 trigger 命中，缺了等于死件）。pilot 模板 `tracks/pentest/rules/templates/osrc.md`（owners/osrc.md + rating/osrc.md 四段一体合并，scope.in=15 域名通配、forbidden §10 硬禁、uncollectable §6 白话清单、noise_caps 不臆造留空、rating_ref: self）；F11 残留 owners/edu-rating.md（与 rating 版正文逐字重复）入 `rules/.history/trash/` 回收站。**打磨定稿七项**（模板选择器多标签宁严勿松消解：forbidden/uncollectable 并集、noise_caps 取最严档、scope 交集/并集；用户模板 `config/rule_templates/*.md`；实例存 `workspaces/<pid>/project-rules.md` 独立文件整体压过模板；机器消费 M3 顺序=编排器 forbidden 收窄先行；role-rules 通道保留零迁移〔四轨均无存量〕；注入三级合成序与 legacy owners/rating 回退）见方案文档 §5——**M2（实例层+注入切换+前端规则一页）/M3（机器消费）待排期，现状注入面零改动**。

## 专家池与项目绑定（M1-M4）

**专家池 `packs/experts/<id>.yaml`（M1，2026-09-21 定稿并实施，源自 expert-pool 方案）**：17 个扁平单文件（23 角色按方案 §4.3 映射合并——4 对 pentest/redteam 镜像合并走轨变体、_generalist 四合一、ctf/recon 独立为 triage 专家；**2026-09-22 dsh-kb-sourcing 增补第 17 位 cloud-security 云安全专家**，tracks:[pentest]、skills:[cloud-entry]），字段与角色 yaml 全兼容，新增 tracks（可服务轨域，缺省=全轨）/protected（仅 _generalist）/variant_<track>_*（轨变体字段级覆写，零解析器改动）。`load_expert(root, name, track)` 加载时按当前轨应用前缀键覆写、产出与角色同形状 dict（下游 system 组装零改动），缺失回退 _generalist；`expert_exists`=池内存在且 track∈tracks（发布链路校验，判存在勿用 load_expert——静默回退会放行拼错 id）；`list_experts(track)` 轨过滤清单（组队 UI 数据源）。doctor 专家体检段：skills 归属存在（error）/已禁用（warning）/跨包重名归属不唯一（warning）、task_type 越各轨注册表并集（error）、tracks 与 variant_ 前缀轨名合法性（error）、expert-name-missing（warning）、缺 _generalist 兜底（error）、role-rules 悬空（warning，专家/角色双源核对）。

**M2 项目绑定（2026-09-21 实施）**：运行时唯一角色源=experts/，`packs/tracks/*/roles/`（23 yaml）退役删除（roles.py 模块保留供 `_parse_inline_value` 复用与历史脚本）。项目绑定专家：创建（`experts` 字段，池外/轨外 422 提示可用池）与换将（`PATCH /api/projects/{pid}/experts`，空清单剥键恢复存量直通态；即时生效于下轮会话构造，黑板行无此列、单一真相源 project.json）。API meta 响应层装饰补 `experts` + 推导 `directions`（`_expert_meta_view`，2026-10-13 前为 `capabilities`/caps_effective）；轨 roles 读端点数据源切 `list_experts`（PackRole 兼容形状），角色写端点退役返 410。发布/开窗 role 值域=**绑定专家清单优先**（`allowed_roles`，宁严勿松），未绑定=按轨全池；构造链把 `directions_effective` 推导面与 allowed_roles 贯穿 AgentSession→ToolDispatcher（认领即换装经 expert_exists 校验，绑窗角色失效回退 _generalist）。

**M3 专家 CRUD 与组队 UI（2026-09-21 实施）**：`/api/experts` 五端点（GET 全池 / GET 单个 / POST / PUT / DELETE）——**全字段提交式覆写**（表单即最终态，None/空=不落键，skills 缺键=全量专家语义）；写前置校验 doctor 同口径前移（id 强制 ASCII slug `[a-z0-9][a-z0-9-]{0,47}`、tracks 合法轨、skills 注册表存在、variants 轨名+字段名 `[a-z_]+`、max_steps 正整数，非法 422；**task_types 校验已随字段退役移除（2026-10-21）**）；重名 409、protected 拒删 409、缺失 404；写三件套（pack_write_lock 临界区 + _pack_history_backup + _trash_move）。variants 在 UI 只读展示，yaml 里以 `variant_<轨>_<字段>` 平铺键人工维护。前端（§12）：创建页 **caps 多选退役→专家多选**（按轨过滤分组「通用/轨专属」、搜索、persona 一行预览、默认预选 _generalist；空选=按轨全池直通）、场景档 chips 预填、知识继承折叠区；列表/顶栏徽章 bindingBadge 改「轨 · 专家×N」；直播间编排器态 👥组队弹层（轨过滤多选，保存回执带推导能力包只读徽章）；设置页「角色」tab 换「专家」ExpertsPane（全池管理 + HistoryButton）。

**M4 场景档 / 知识继承 / 看板默认视图（2026-09-21 实施定稿）**：
- **M4a 场景档 `packs/kb/<track>/profiles/<id>.yaml`**（2026-10-13 轨并入 kb）：平铺五件套 experts（组队预设）/rule_profiles_owners+rule_profiles_rating（F11 两键）/playbook/artifacts/knowledge（后三者为 D6 预留字段）+ board_view（M4c）。**D6 物化即弃**——建项选中档即一次性物化：experts→meta.experts、rule_profiles/board_view→config 立即生效、playbook/artifacts/knowledge 随 `config.profile` 快照留档（{id,name,playbook,artifacts,knowledge,materialized_at}），此后档文件只作建项预设、不随项目联动（改档不改存量项目）。档提供缺省、请求体显式值优先（experts/config 显式给出时不兜底）。每轨内置 0~3 档（当前 8 个：ctf full-squad/solo-generalist、pentest standard-engagement/recon-first、redteam full-chain/initial-access、research rev-workbench/code-audit）；`GET /api/tracks/{track}/profiles` 只读清单；doctor 增 profile-expert-missing（组队引用不存在或轨外，error）/ profile-board-view-unknown（warning）。
- **M4b 知识继承**：POST /projects body `inherit_from=<源项目 id>`，源预检 404 先于建项；`ProjectStore.inherit_knowledge` 三段复制**只增不覆盖**——binary 资产（find_asset 同 sha 判重，样本文件 copy2 到目标 samples/、meta.path 更新+`inherited_from` 标记、author 沿源）、func_kb（(project,sha,address) 判重）、蓝图（(project,sha,name) 判重，非 draft 状态尽力保留）；源项目只读不动；继承失败不阻建项（log.warning + 响应 `inherit_error`），成功响应带 `inherited={binaries,func_kb,blueprints,skipped}`。UI=创建页折叠区（源项目下拉）。
- **M4c 看板默认视图**：`config.board_view={"default": "findings|assets|funcs|board"}`（值域 BOARD_VIEWS）；档物化或 PATCH config 透传；前端 Blackboard 受控 tabs，defaultView 不在可用集合（compact 无 board、无 binary 无 funcs）时回退 findings。

## 提案制治理

Agent 永不直写 packs，全部经治理通道：统一提案落 packs/.proposals/ 人审应用（只认 decided_by=human）；受控写通道（pack_write_lock / .history 同秒避让备份 / trash 可恢复 / 1 MiB 上限 / 防穿越校验）；K6 成功链沉淀人审闸门防自投毒。

**任务收尾自动复盘（experience-sedimentation M1+M2，2026-09-22）**：complete_task（done+verified 产出）与 fail_task（blocked_reason=error）双路径条件触发 `_sediment_proposal`——aborted（人工中断/关窗未收尾/worker 异常兜底）/ awaiting_human / 无产出 done 不触发；planner LLM 复盘产出 kb/index/case 三去向提案草稿（K4/K6 口径）或 NONE，与 AI 自主提案共用每会话 3 条 pending 上限。**M2 提纯**：①复盘前 kb 对账（kb_module_hints 注入既有相关模块，已有手册引导 edit 补「已验证路径」「坑」段不 create 重复新建）；②失败模式只提炼方法论避坑教训、环境/临时性问题一律 NONE；③reason 证据锚点双层校验——解析后先验真实 finding id（LLM 编造/漏引用静默跳过），`proposals._validate_agent_kb` 机械 lint 再拦（origin=agent 的 kb create 提案：reason 须引用 find-/task- 证据锚点、content 须有「## 」段落且至少一段命中沉淀口径「坑|注意|已验证|适用…」；human origin 与 revise/apply 不校验——人审专注内容质量）。输出 NONE / 任何失败都静默跳过，绝不影响任务收尾。

**知识库迭代（kb-iteration，2026-10-14，可选功能，落点=智能体主控）**：区别于任务收尾复盘（任务 Agent 已退役），本功能挂在**对话链主控**（`core/chat/runtime.py`）。**项目级开关** `config.kb_iteration={"enabled":bool}`（默认关，`core/projects.py::update_config` 归一化、`core/skills/kbiteration.py::is_enabled` 判读，每轮现读无缓存）。**触发口径=规则触发 + LLM 生成**：轮正常收尾后 `_maybe_kb_iteration()` 按**本轮事件窗**（起轮前记 `MAX(events.id)` + ISO 时间；用窗不用 session id——子专家活动落子线程 session，按 session 过滤会漏）聚合信号 `signals_of` → `should_trigger` 命中即后台 daemon 线程跑 `run_iteration`（列 kb 文件/技能清单喂 LLM，生成 kb/skill 提案，`origin="agent"`，≤5 条）。**四类触发规则**：①发现知识库内容有误（信号：kb 引用后仍有失败）；②任务艰难/试错多（重复同名工具失败≥2 / 工具失败≥3 / 动作≥25）；③发现新 Web 漏洞（`_WEB_VULN_HINT` 正则命中新 finding）；④知识库缺该经验（kb 引用>0 且有失败）。**提案面**：kb 与 skill 均可，skill 允许 `edit/suggest/create/delete`——**新建/删除都走提案，删除进回收站**（`writing.delete_skill`→`kb/skills/.history/skills-trash/`），**全部人类审批才 apply**（只认 `decided_by=human`）。**不打断会话**：提案是轮末后台异步产物，人类在工作区「变更提案」页审批即可。`run_iteration` 整体 try 包裹，任何异常静默绝不阻断会话。

**开关 = Agent 提案总闸（2026-10-15 修正语义）**：`kb_iteration.enabled` 不只门禁轮末自动提案，也是 `propose_pack_edit` 主动沉淀的门禁——关闭时 chat 工具面/系统提示/dispatcher 三处剔除 + `ToolDispatcher._tool_propose_pack_edit` 执行层硬闸（读失败按关），即 Agent 不提任何提案；开启后工具面与提示注入即时恢复（开关于轮内翻转时「关」即时生效、「开」下一轮工具面生效，执行层实时读）。

## 改名联动与体检

refs.py kb 改名全库引用扫描+重写；doctor 体检（error：悬空引用/未注册 task_type；warning：route_index 三类体检/rating 无 owners 配套；info：孤儿技能；**M1 专家池段**：skills 归属/跨包唯一、task_type 越轨并集、tracks 合法性、role-rules 悬空——experts/ 缺省整段跳过；**experience-sedimentation M3 kb-structure-thin**：>800 字符且标题行（# 开头）<2 的单段长文报 warning——缺「## 」分节结构不利 kb 对账与沉淀补段，`.history`/kb-trash/refs/ 子树豁免、短条目不检）零 error 才健康，K7 零命中统计在 API 端点后处理（**route-injection-hardening，2026-09-24 改真使用口径**：`info route-index-zero-hit` 只报「曾被注入路由索引（skill.routed 且 route_index>0）但对应手册从未被 kb_open 打开」的测试点——跨项目聚合，任一项目打开过即算 used；不再是「从未被注入」）。

**kb 待清理建议段（experience-sedimentation M3，doctor 端点后处理）**：`traces.kb_module_feedback` 三象限（opened=kb.open 全历史计数 / positive=verified finding 任务窗 origin='trace' 链内开启的模块 / negative=真失败〔error〕任务窗内开启的模块，弱信号）→ positive=0 且（opened≥3 或 negative≥2）报 info `kb-cleanup-candidate`（negative≥2 标「优先复核」）——只呈现建议、不自动处置；feedback 统计纯只读。

# 七、LLM 接入层（core/llm）

## 协议统一

内部传输协议统一 Anthropic /v1/messages；三个网络注入面（Transport/StreamTransport/HttpGetter）使整个 core/llm 测试零触网。

**传输引擎（LLM SDK 迁移，2026-10-03 实施）**：官方 `anthropic` / `openai` SDK 作主引擎，接管连接 / 代理 / UA / HTTP 状态分类 / 超时 / SSE 事件解析；`sdk_engine.py` 用自定义 `httpx` transport（`TeeTransport`）旁路录制原始字节，并把 SDK 的流式事件**还原成 SSE 行**喂给既有 `_consume_stream`——Ark 特化（截断容错、增量探针、逐帧回调、文案嗅探）零重写，60+ 处注入式测试零改动。`RawFallback` 是双向兜底：SDK 抛 `JSONDecodeError`（Ark SSE 尾部冗余）或**一个事件都没产出**（网关 SSE 缺 `event:` 行 / 无视 `stream:true` 回整份 JSON）时，改用原始字节走自研行式解析（含 `raw_decode` 抢救与非 SSE 兜底）。鉴权头必须由 SDK 自持凭据产生（`Omit` 关掉另一通道 + 阻断 `ANTHROPIC_*` 环境变量回退），否则 SDK 会把凭据与自定义头拼接成 `x-api-key: a, b`。响应解析抽为 `parsing.py` 纯函数供 compat 与 SDK 引擎共用。

## anthropic_compat

协议转换核心：按类别重试矩阵（标准 5xx〔500/502/503/504，OpenAI 侧含 Cloudflare 520/524〕共 11 次尝试、首次 + 10 次重试、退避封顶 30s；429 共 2 次尝试；连接类共 4 次 + 5/10/20s 退避）、重试次数入错误文案（`retry_note`）、thinking/stream/cache_control 400 实例级降级（去参重发）、SSE 状态机（工具参数 JSON 分片拼装）、首帧后不重试（SSE 不可重放）、truncated 截断防御（区分「说完」与「流断」）。

**Prompt caching（retrieval-upgrade M1，2026-09-23 实施）**：请求侧 system 改块数组——`build_system_parts` 拆 stable（规则链+角色+能力清单，会话内字节稳定）/ dynamic（技能指引+任务目标+纪律尾）两块，stable 块末尾打 `cache_control: {"type":"ephemeral"}` 断点；Ark Anthropic 兼容层实测直接接受（同前缀第二跑 cache_read>0 前缀缓存生效），400 文案含 cache_control 时实例级降级剥标重发作保险（`_cache_disabled` 置位后不再打标）。观测面：usage_view 补 cache_read/creation 与命中率 `cr/(in+cr+cc)`（LiveRoom 预算弹窗展示）；预算逻辑不动（缓存 token 仍计数，只是便宜）。消息历史增量断点后置观察。

## ProviderStore

供应商注册表（config/providers.json）：PUT 全量校验（至少一个启用供应商、default 必须命中启用项）、api_key 空串=沿用原 key、masked() 出参永不回显明文（前端只见 has_key 布尔）；真实 key 传播面压缩到文件+内存+请求头三处。

## ModelRouter

planner/executor/classifier 三角色→模型映射（executor 稳定性>智力、classifier 小模型摊成本）；config/llm.json 文件级覆写（不上 UI）；model_context → 上下文字符预算换算（tokens×2 中英混合启发），未声明用 256K 缺省。

## 横切

分层语义化错误（ProviderError 配置面 / LLMError 调用面带 status/body/truncated）；无 key 转 503 可恢复语义（非 500）；record_llm_usage 用量记账。

# 八、工具体系（tools/ + config/mcp.json）

## 工具链注册表（toolchain-registry M1，2026-09-23 实施）

**registry 单表 `tools/registry.json`（入库，机器无关声明）+ 本机路径覆盖层 `config/tools.json`（gitignore，仿 mcp.json 模式）**，消费方探测经 `core/toolchain.py`（纯 stdlib）。kind 四类：binary / runtime（python、jre 基础环境）/ python-tool（venv）/ data（指纹包）。**四来源检测顺序**（每条目命中即止）：①config/tools.json paths 指认（既有安装纳管，最高优先）→ ②tools/ bin 规范位（可下载落位；kind=runtime 且 acquire=bundled 即「开包自带」位，source=bundled/downloaded）→ ③registry fallback glob（常见安装目录；`?:/` 前缀=盘符通配 C-H 逐一尝试）→ ④PATH（search 文件名清单）。**探测只查文件存在性零副作用**，verify 试跑深检留设置页工具面板（M3）；status=ready（source 标注获取来源）/missing（detail 带 guide 指引）。

registry 校验 fail-fast（kind 白名单/数组字段/平台键 bin，坏条目 ValueError——入库声明出错不静默吞）；覆盖层坏文件 `{}` 宁容错。消费三接线：**detector**（能力清单注入 Agent 系统提示，registry 驱动替代原 manifest.yaml 通道——零使用者随之退役；坏 registry 降级单条不阻断 docker/wsl 主探测）、**doctor**（`tool-missing` info / `tool-registry-invalid` error / 探测异常 warning 不阻断）、**decompiler**（`resolve_ida_headless` 前置 registry、新增 `resolve_ghidra_headless` registry→PATH——解「Ghidra 装了探测不到」）。首批 19 条目：ida/ghidra（binary+fallback 本机实位）、jre/python（runtime acquire=bundled 占位，M2 落位 tools/runtime/）、jadx/apktool/adb（binary search+download）、nmap/httpx/nuclei/ffuf/sqlmap/masscan/hydra（pentest 域）、impacket（python-tool pip 声明，M2 venv）、android-ctf-runner/godot-ctf-runner/android-unpack-kit（bundled python-tool，`bin {"*": "py/android/…"}` 相对 tools/ 规范位，随仓自带——android-kb-sourcing M2 收编）、fpdb（data）。网关注入（PATH/JAVA_HOME/venv）与工具面板在 M2/M3。

## MCP 接入

外部服务型工具经 config/mcp.json 注册（FOFA 资产测绘等），domains 校验白名单；禁写密钥（FOFA 账号轮换在 src-strike 侧 .env）。

## 反编译双通道

统一抽象 core/tools/decompiler.py：IDA headless 优先 → Ghidra analyzeHeadless 兜底（按 sha256 缓存全量导出，export_version=3 契约）+ IDA GUI MCP 实时桥（只连 loopback，13337 人手实例）+ 按需拉起 IdaMcpManager（无窗口 idat、13338+ 空闲端口、锁检测不抢 GUI 库、LRU 回收；**2026-10-13 起：现成 `.i64/.idb` 直读复用不重建、无库且 ≥10MB 拒拉起**——见下 P3「自动分析体积上限」）；全不可用退纯静态并出 DECOMPILE_GUIDANCE 安装引导。两腿路径解析（2026-09-23 toolchain-registry M1 起）：registry 四来源优先 → 原生兜底（IDA：PATH→`_IDA_INSTALL_GLOBS`；Ghidra：PATH），registry 命中与原生兜底同源并存，双通道冗余无害。

大样本加速 P1（2026-09-30，复用存量 + 缓存 + 可配超时）：
- **可配超时**：headless 默认 3h（`HEADLESS_TIMEOUT = 3*3600`，原 900s 对大样本不够），可经 gitignore 覆盖层 `config/decompiler.json` 的 `headless_timeout`（秒）覆盖，`resolve_headless_timeout()` 每次取用（缺省/非法/≤0 回退默认），仿 config/mcp.json 模式。
- **直读存量**：`IDAHeadlessBackend.export` 若项目 db 目录已有 `.i64/.idb`，直接 `export_db` 复用、不再 `-o` 重建（返回 `reused:True`）；db 被 GUI 锁（`.id0/.id1/.id2/.nam/.til`）则结构化抛 `ida-db-locked` 绝不强写。
- **全局 sha 缓存**：`DecompilerService(global_cache_dir=…)` 指向 `data/decompiler-cache`；本地缓存缺失时 `_promote_from_global` 原子拉回（跨项目同 sha 零重导），全量导出成功后 `_publish_global` 原子回写；**只全量入全局，MCP 轻量/部分缓存绝不入**（守三层数据纪律）。
- **导入三端点**（复用外部成果，免重跑）：`import-export`（纳入外部 headless 全量 JSON，校验 `export_version>=EXPORT_VERSION`）、`import-ida-db`（纳入外部 `.i64/.idb` 后库内 `export_db` 重导）、`reexport-db`（库内在册 db 重导刷新缓存）。三者皆后台 Job（`binary.db_imported` / `binary.export_imported` / `binary.db_reexported` 事件），大数据不进 Job 结果（reexport 结果剥 `data` 只留 `{status,sha,functions}`）。

大样本加速 P2（2026-09-30，Ghidra 多进程并行分片主产）：
- **大样本选路**：样本 ≥ 阈值（默认 20MB `LARGE_SAMPLE_BYTES`，与 app.py `SAMPLE_LARGE_BYTES` 同口径）时，`DecompilerService._ordered_export_backends` 把 Ghidra 提到最前（多进程并行主产）、其余后端兜底；普通样本维持装配顺序（IDA 优先，保 IDA 质量与 .i64 存量复用）。选路在导出时按真实文件大小判定，到期不改全局 prefer 默认。
- **并行分片**：`GhidraHeadlessBackend(workers=N)` 把并行度作为 postScript 第二实参传给 `tools/decompiler/ghidra/scripts/export_funcs.py`；脚本按函数表切分给 N 个 worker，每 worker 独占一个 `DecompInterface`（各自对应一个独立解编译器进程），原子游标取活避静态分片长尾，按原序合并为 v3 契约。缺参/非法一律回退串行（workers=1，新增前 args 形状不变，兼容既有假 runner）。
- **默认与内存保护**：`resolve_ghidra_workers()` 默认 `max(1, CPU-1)`，config 可覆盖，一律夹 `[1, GHIDRA_MAX_WORKERS=16]`（每 worker 一个解编译器进程吃内存，满核硬起会 OOM/swap，故设绝对上限）。

大样本加速 P3（2026-09-30，长任务 UX：分片进度 + 协作式停止 + 分片重试）：
- **进度**：headless 是单个 analyzeHeadless 子进程内跑完，父进程只阻塞等待——进度经**控制文件**交换。脚本按 postScript 位置实参收 `args[2]=进度文件`（每 ~1s 由报告线程写 `{done,total,phase}`）与 `args[3]=停止文件`；后端 `export(progress=…)` 起**监视线程**轮询进度文件回填 job meta（引用共享，前端 `GET /api/jobs/{id}` 轮询即见），`done/total` 即已反编译/总函数数。
- **协作式停止（保留部分）**：`export(stop_event=…)` 置位 → 监视线程写停止文件 → 脚本在**函数边界**检查并停下（不杀进程），仍写出「已完成函数」的 v3 缓存（函数名/地址全量，伪码只到停点），`meta.partial/stopped` 标记。partial 缓存**只展示不发布全局**、且**不被 `export_to_cache` 复用**（重新「开始分析」即补全）。端点 `POST /binaries/{sha}/triage/cancel`（`app.state.export_cancel[sha]` Event；无在跑幂等）。
- **分片重试**：脚本内单函数反编译失败重试一次（瞬时解编译器故障自愈），仍失败留 `calls=[]` 无伪码，不拖垮整次导出。
- 前端：样本条「开始分析」旁进度环 + 「停止」按钮（`api.cancelTriage`），停止后 hint 提示部分结果已保留。
- **自动分析体积上限（2026-10-13）**：`SAMPLE_AUTO_MAX_BYTES=10MB`（**单一真相源 core/tools/decompiler.py**，后端按真实文件大小判定）——① `POST /binaries/{sha}/triage`（「开始分析」唯一触发口）对 ≥10MB 样本返回 422，前端按钮同步禁用/拦截；② **Agent 点查**（decompile/xrefs 缓存缺席 → `mcp_provider` → `IdaMcpManager.ensure`）**无库且 ≥10MB 时拒绝拉起**（返 None → `NOT_ANALYZED` 引导先「开始分析」），杜绝 Agent 无意识触发大样本全量分析。大样本改走「从 IDA 拉取函数」（IDA 内 Ctrl-Alt-M）。20MB 的 `SAMPLE_LARGE_BYTES` 仅余 Ghidra 大样本选路口径（见 P2）。
- **Agent 只能消费已分析结果 + IDA 库复用（2026-10-13，同一命题）**：① **直读复用**——`IdaMcpManager._launch` 现成 `.i64/.idb` 直读打开（`idat -A -S<bootstrap> <db>`，**不带 `-o`**），绝不从样本重建（重建会覆盖人类 GUI 改名/注释）；库查找**两处**：平台 `db_dir/<sha>.i64|.idb` + 样本旁 IDA 默认名（`<input>.i64`，人直接在 IDA 打开保存的产物）；② **不抢锁**——GUI 正开该库（`.id0/.id1/.id2/.nam/.til` 在）绝不拉起；③ **GUI 重活工具闸**——`survey_binary` 这类全量工具经 `core/chat/mcp_bridge.py` 按当前库源程序大小拦截（`HEAVY_MCP_TOOLS`，默认 30MB；阈值与引擎分析的 10MB **有意不同**——前者是对已开库的重活遍历、后者是新分析），**fail-closed**（`server_health.input_path` 取不到大小即拒）；重活工具调用超时放宽到 **180s**（`HEAVY_CALL_TIMEOUT`，默认 60s 对全量工具不够会先超时报「server 离线或协议错误」）；前端工具行运行中显示「已运行 Ns」（逐秒刷新）。实测（2026-10-13）：`idat -A -S<bootstrap> <db.i64>` 直读可行（169MB 库加载 ~58s）。

## 双向写回

func_kb→.i64（apply_names 后台 Job，锁检测结构化降级）与 .i64→func_kb（pull-names 库内重导不删库，防 GUI 手改名丢失，diff 后非自动名才入 name_history）。

复用外部成果（大样本 P1）：`import-export` / `import-ida-db` / `reexport-db` 三端点（见 §反编译双通道）承接手工/他机已分析产物直读入库，免再跑一轮 headless。

## 工具目录 tools/

decompiler/{ida,ghidra} 脚本区 + mcp/ida-pro-mcp 适配区 + registry.json 注册表（§八工具链注册表）；地址一律 hex 字符串出 API（JS Number 无法安全表示 64 位地址）。`data/fpdb/fpdb_seed.json` = 本地指纹包首批（dsh asset-mapping 收编，规则制：loc+kws AND 语义+path_rules；pentest 指纹三段式第二段，registry data 类首批成员）。

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

api.ts 唯一 fetch 出口（约 130 端点方法，ApiError 结构化错误）；useEvents 模块级手写 query cache——REST tail + WS 增量（since_id 游标）、100ms 批量 flush、回入水合截断 300 条、缓存 2000 条上限裁旧。**会话维度分页（live-event-stream-session-window，2026-09-23 实施）**：签名 `useEvents(pid, sessionId?)`——会话页签走会话源（tail/before 带 session_id，store 层 F8 既支持 API 一行透出；cache 键 `pid:sid` 每会话独立窗口；WS 增量按 sid 过滤、游标恒前进防断线重连回放重复），`__all`/编排页签走全局流——修「已结束会话页签近乎空白」（全局最新 50 条里该会话可能零事件）。

## TRAE 化新壳（shell2，2026-09-25 实施 → **2026-09-26 移除**）

**已移除，不再维护（用户定稿）**：`webui/src/shell2/` 全部代码、App.tsx 壳选择（`?shell=`/`ui.shell`）、旧壳「新壳 →」入口、根目录 `启动新壳.bat` 均删，旧壳为唯一壳。实施记录（M1–M6 三姿态/欢迎页/模板中心/三 chip 等）见方案归档 `docs/plans/归档-已完成/webui-trae-shell.md` 与 git 历史。**后端留存资产**：schema v23（任务 preferred_runtime + `GET/PUT/DELETE /executor-llm` 项目级模型覆写）与 M2 抽件 `lib/turnStream.ts`（事件→turn 纯函数，旧壳 LiveRoom 同在用）、`views/live/editors.tsx`。会话中心化配套 UI 的去留见 §四。

## 事件渲染管线

四层分工：筛选/样式层（eventStyle 纯映射）→ 布局层（EventRow 五形态）→ 装配层（命令对配对/中断去重/流式终稿遮蔽/对话轮 turn 分组）→ 圈定层（页签×类型两步串联）。DB 行是真相、事件流是有损视图（页签状态灯取数 DB 优先）。

事件行人话摘要（live-stream-ux，2026-09-23 实施，方案归档 `docs/plans/归档-已完成/`）：`TOOL_SUMMARIZERS` per-tool 语义摘要表（7 工具，未命中回落 TOOL_ARG_KEY 兜底，LEN_SHORT 60/LEN_LONG 200 分级截断）；SummaryCtx 资产反查（LiveRoom 持 assets 映射 + asset.new 增量，反查不到显 id 尾 6）；task.done/failed 专属摘要随任务机制退役（2026-10-06）；finding.new/merged payload 补 title/target_asset_id/status（store 侧），前端 severity 徽章渲染 `[高危] 标题（漏洞类） · 目标`，终兜底 payload 首字符串截 60；行 onClick `selectionCollapsed()` 守卫（拖选文本不触发折叠）；TimeTag 本地时间微标 + 悬停「本地 + UTC」双标。

直播间消息区窗口三态（live-event-stream-session-window，2026-09-23 实施，方案归档同上）：**铺满视口**——内容不足一屏且未取尽时 effect 循环 loadEarlier（每批 50，通常 1-3 轮）直到铺满或取尽，已结束会话页签打开即满不再手点；**上翻自动加载**——距视觉顶约 200px 触发 loadEarlier（先吃模块缓存零网络）；**滑动窗口裁剪**——DOM 上界 MAX_DOM=300、粒度 PAGE=50，滚回底部附近（column-reverse 贴底 |scrollTop|<200）裁头部一批留余量（贴底视觉零跳动：column-reverse 滚动原点在底部，顶部缩减不位移），裁剪经 `trimDom` 只动展示 state、模块缓存不动（再上翻缓存补回），用户在上方阅读时不裁（atBottomRef）；全局页签（__all/编排）同样套用，长跑项目 DOM 恒有上界。跨会话事件派生（tabStatus/budgetPausedSids/flowBump）依赖 DB 稳定事实与轮询兜底，不依赖全局流窗口。

## 视图组织与重画布

React.lazy 隔离 @xyflow 重依赖（画布永不进主包）；布局算法抽纯函数模型（canvasModel 泳道+重心），组件只消费 place 槽位。（flowModel DAG 随任务流退役、boardModel 五列 packing 随黑板全景 2026-09-26 下线，均删除）

## 链路画布单向零重叠（findings-canvas-dag-layout，2026-09-22 实施）

发现 tab「链路」子视图定稿（用户四拍板 D1-D4 + 设计决策 R1-R4，方案归档 `docs/plans/归档-已完成/`）：

- **单向三保证**：列=距链头最长路径层（链头最左、层递增，恒向右）；**弱边只画右向**（col(target)≤col(source) 不画，升级关系信息详情页仍可查）；链分量保持货架堆叠（D2）。
- **边不穿卡（三路由正交）**：`adjacent` 层差=1（源右缘出→列间中缝 28px 垂直拐→目标左缘入，同行退化直线）；`long` 层差>1（水平长跑 y=longRunY「中间列全空」行带中心，构造上不穿卡）；`cross` 跨泳道（y=所有泳道最大高度+24 底绕行带——中间泳道可能更高，取全泳道 max）。垂直段全在列缝走廊内，同走廊多边 ±3px micro-offset 阶梯。**实施形态=单边多拐点正交 path**（BaseEdge 任意 d 串，无 elbow 虚拟节点/拆段边/编组；走廊 x 渲染端从实测锚点算，无模型几何假设）。
- **行分配**：保留前驱同行优先（层差=1 同行=水平直线一一对照）；同层多节点按前继行均值升序（一轮类重心，逐层交错排序→放置，无已布前驱排层尾）。
- **交互**：边注记**默认隐藏**，hover/选中该边才显（落点=最长段中点，瞬态遮挡可接受）；**拖拽锁定**（布局恒为算法产物，offsets localStorage 与重置钮退役）；MiniMap 默认收起可展开（localStorage 记忆）；聚焦开关/边三态/F13 孤立折叠不变。

## 状态管理哲学

无状态库；useState+props 下行、CustomEvent 上行、模块级缓存跨实例；轮询为底 WS 为翼（WS 断了照常用），单 WS 连接多消费者（直播间事件到达去抖重拉）。

## 页签与设置页

会话页签状态灯；对话轮渲染（human_note 开 turn / 过程折叠组 / 终稿 MarkdownView 不渲染 raw HTML 防注入）；设置页（LLM 供应商 / MCP / **网关**〔gateway-config-view M1，2026-09-23——策略只读快照+探测实况+重新探测按钮，见 §五〕 / **工具**〔agent-tools-view，2026-09-24——Agent 工具静态只读目录+参数表，见 §五〕 / 情报源 / 技能矩阵 / 规则 / 顾问 / 提案 / doctor）；黑板页新增「**测绘**」页签（cyberspace-mapping M1+M2，2026-09-23；CTF 轨不挂载——见 §资产批量导入轨适用注记；`MappingPane` 三区自持：FOFA 设置条〔key 脱敏回显/空串不覆盖/测试连接〕+ 查询工作区〔size=配额消耗显式提示，结果勾选导入、existing 三锚点灰显〕+ 表格导入工作区〔列映射下拉可改嗅探建议，全量行回传前端内存持有确认导入〕；紧凑侧栏不挂；资产行来源徽章 🛰fofa/📥文件 + 指纹 chips ≤3+N；事件流 `asset.imported`「N 行 · x 新建/y 合并」摘要行）。

## 设计系统与工程纪律

克制黑客风（深灰 oklch+青色强调，仅深色主题）；状态语义色收敛 --status-* 六变量（Tailwind v4 括号简写坑已固化约定）；横切唯一出口（datetime.ts 时间 / roles.ts 角色显示名）；竞态守卫 let alive=true 模式。

# 十一、逆向领域集成

## 逆向工作台

research+binary 项目自动切 rev-generic profile：黑板侧栏换逆向挂机侧栏 RevCompact；伪码只读展示、函数浏览器（函数/字符串 tab）、库级「IDA 打开」（detached 启动 GUI）、精确跳地址。

## 调试脚本档

x64dbg 下断 / Cheat Engine Lua 纯前端模板（零插件不触网），日志人工导出后回流为动态验证凭据。

## 三层数据纪律

客观全量层（headless 导出缓存）/ 主观分析层 / 产物分离；样本 untrusted；headless 解析经网关审计定性 trusted（只解析不执行样本）。

## 分析包基础层（2026-10）

分析包代表一个应用、游戏或解包环境，分析目标代表包内可分析文件。第一阶段已落地：

- `core/sample_packages.py` 负责单文件、目录文件、ZIP/7z/TAR 的安全导入；拒绝路径穿越、符号链接和特殊条目，并限制单文件、总展开大小、文件数和目录深度。
- 展开目录生成规范化 manifest hash；相同目录内容复用**项目内内容树**（`<project>/trees/<manifest_sha256>/`），项目保存包版本元数据；原始上传物按 SHA256 保留于 `<project>/raw/`。上传物一律落项目工作区内，不再写全局 `data/sample-store/`。重复导入记录保存在该包的 `imports.jsonl`。
- 自动识别 PE/ELF/Mach-O/DEX/APK/AAB、游戏资源、压缩包和文本候选；同包 PE/ELF/Mach-O 通过导入名建立基础依赖边。候选目标选择与旧 `binary` 资产分开，后续接入静态分析器。
- `ANALYZER_REGISTRY` 将目标格式映射到可扩展插件 ID；`GET/POST .../targets/{target_id}` 提供目标详情和异步静态分析。PE/ELF/Mach-O 复用现有 IDA/Ghidra headless 导出；APK/AAB 导入时保留包级摘要，展开后对 Manifest、DEX 做只读解析，`lib/<abi>/*.so` 进入 ELF 分析；不安装、不启动 Android 应用。
- API：`/api/projects/{pid}/sample-packages` 支持单文件/压缩包和重复 `files` 目录导入；`.../uploads` 提供分片续传；`.../targets` 保存目标选择和分析报告。旧 `/samples` 与 `/binaries/{sha}` 保持兼容。
- **登记为资产（不复制文件，2026-10-12）**：包内 PE/ELF/Mach-O 目标不再在「样本分析」页分析，改 `POST .../targets/{target_id}/register` **登记为 binary 黑板资产**——资产 `meta` 记包坐标（`package_id/version_id/target_id`）+ **包内相对路径**（`path`），**不复制文件**；文件经包坐标 `resolve_target` 还原为**项目内容树**绝对路径（`<project>/trees/`，同项目内跨包复用、不可变），逆向工作台据此分析（引擎选择在 `SampleBar`）。APK/AAB/DEX/文本仍走 `.../analyze`。
- **AI 可读分析包**：样本资产行 `meta.package_id` 指向来源包；AI 用 `bb_query what=package` 取包内 `entries`/`dependencies`/`tree_ref`（项目内容树绝对路径），再用 `read_file <tree_ref>/<path>` 读同包文件（解「分析 exe 时其依赖的同包 DLL 找不到」）。项目内容树受删除护栏保护——删资产/删分析包都不删内容树文件。

## 蓝图数据底座

blueprints 表（schema v17）与 store CRUD/事件已落；**消费面接线（R4 管线 M1，2026-10-13）已落地**——chat 主控/子专家 system 注入蓝图段（`_blueprint_context`）、会话窗 `_mat_blueprint` 按 `binary_sha256` 命中优先注入（此前 AI 侧完全读不到 blueprints 表）。**产出闭环（M2，2026-10-13）已落地**——「蓝图」页签加「生成蓝图」/「按模块深析」按钮：建 chat 线程（主控 chat-orchestrator）+ 下发阶段 objective + `goto-agents` 跳工作台并选中该线程；`blueprint-rebuild`（现名 `逆向重建手法`）技能改写为 chat 主+子口径（主控 `call_expert` 扇出、`module:<名>` 锚点纪律）。**重建执行（M3，2026-10-13）已落地**——蓝图 status=`building` 时「蓝图」页签露「派发重建」按钮，下发阶段四/五 objective：每模块一子专家落码到 `<proj>/artifacts/rebuild/<模块名>/` → 依 func_kb 伪码写自测脚本 → `run_cmd(threat_class=untrusted, runtime=docker/sandbox)` 对拍 → 全过 `bb_blueprint_update(module_status="tested")` → 阶段五拼装 + 「蓝图 vs 实现」差异表经 `bb_add_artifact(kind="project")` 落文本（**产物本体留 artifacts/，该工具只存文本不上传文件**）→ 人类终验 `built`。载体定案：**core/chat 工作台主+子**（非 Team）；人类确认闸 = 蓝图 status（`ready` 放行 / `building` 开工 / `built` 终验）。

# 十二、工程化与运维

## 测试

全量 pytest（700+ 用例），`PYTHONIOENCODING=utf-8` 约定；fake runner/transport 注入模式（网关/LLM/情报测试零触网零实进程）。

## 启动/停机

启动平台.bat（UTF-8 + CRLF，chcp 65001 自举兼容 GBK 控制台；端口已监听则跳过，首次自动 npm install，就绪后开浏览器）；启动平台（窗口）.bat（pythonw 弹窗模式）；停止平台.bat（先优雅停机 POST /api/admin/shutdown——在跑会话落任务现场快照，等端口释放最多 15s，超时才按端口硬杀）；`scripts/build_exe.py` 桌面打包一键（干净 venv + PyInstaller + 资源随包，见 §一 M3）。

## 文档制度

DESIGN.md 功能导向唯一真相源（本文档）+ 各大目录中文 CLAUDE.md（≤80 行，实质修改同步更新）+ docs/plans/ 方案目录（一方案一文件，不写方案不挂账，实施后回写本文）。
