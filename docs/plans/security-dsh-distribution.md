# 安全版 dsh 发行版：能力全量插件化（可行性研究）

> **状态：部分实施——M0 尖兵 + M1 网关底座已落地验收（2026-09-26），M2–M8 待用户排期**
>
> **拍板记录：**
>
> | 决策点 | 结论 | 日期 |
> |---|---|---|
> | D1 适配形态 | **安全版 dsh：能力全量插件化**——以 dsh 为底座做安全发行版，黑板/执行网关/packs 工具技能/专家池/编排器全部做成 dsh bundle/插件（TS 实现），UI 用 dsh web。用户在三选项（薄封装调用 / 双引擎并存 / 全量插件化）中亲选此项，覆盖推荐项 | 2026-09-26 |
> | D2 文档性质 | 可行性研究文档，落 docs/plans/，走「打磨 → 用户排期」常规流程 | 2026-09-26 |
>
> **关联代码：** `core/`（blackboard 18 表 3.5 万行 77 文件 / runtime 网关三后端 / orchestrator / agent / projects.py / api ~140 端点）、`packs/`（31 SKILL.md + 675 kb md + 17 experts yaml + tracks 40 文件）、`webui/`（2.6 万行 TS，目标形态下退役）

---

## 0. 一句话结论

**可行，但本质是「在 dsh 上用 TypeScript 重建平台」，不是「适配」。** dsh 的 Cordis「一切皆插件」模型对平台的每一项核心能力都提供了具体接缝（seam），不存在结构性死路；但 `core/` 约 3.5 万行 Python 的大部分需以 TS 重写，`webui/` 整体由 dsh web + Slots 插件取代。**唯一可零损复用的是 `packs/` 内容资产**（手册/知识库/专家 yaml/场景轨文件，约 760 件）。粗估工作量 **21–28 人周（单人全职 5–7 个月）**，且承担 dsh 开发者预览版破坏性变更的外部风险。

### 0.1 dsh 关键事实（本文论据基线）

- DeepSeek Harness：MIT、TS/pnpm monorepo；`npx @deepseek-ai/dsh web` 起 web（127.0.0.1:3080）；README 明示「未来将出现破坏兼容性的变更」。
- **一切皆插件（Cordis）**：插件贡献 service、类型化事件、可逆副作用；无特权内核；插件卸载即撤销注册（`ctx.effect`，支持 HMR）。
- **Profile / Bundle**：profile = Harness home 中的命名装配（bundle 列表 + `cordis.patch.yml`）；bundle = Cordis 配置项 + 挂载代码。层序：profile bundles（按序）→ profile patch → home patch → `--patch`。patch 按 id 定位、整体替换或插入配置项。
- **能力接缝模型**：Service Definition + Provider + Consumer；替换一个 provider（fs/shell/sandbox/subagent）即改变整个产品；fs 与进程 provider 共享同一执行世界。
- **单实现 seam**（如 `ctx.shell`、`ctx.workflowEngine`）：一上下文仅一个实现，第二个通过插件配置替换而非共存；**命名注册表 seam**（`ctx.tools`、`ctx.subagents`、`ctx.skills`、`ctx.storage`）：多实现按名注册。

---

## 1. 目标形态：一个自定义 profile + 八个安全 bundle

新建 profile **`sec`**：复用 `web` profile 的 bundle 全量列表，追加下列安全 bundles，并以 `cordis.patch.yml` 替换三个接缝实现（`ctx.shell` → 安全网关执行器；`ctx.fs`/`ctx.sandbox` 视里程碑需要；不注册裸 bash 工具给模型）。

| # | Bundle | 职责 | 主要 seam |
|---|---|---|---|
| B1 | **sec-gateway** | 执行网关：`run(cmd, runtime)` 三后端（docker/wsl/native）+ pathguard/rateguard + 沙箱 | 自定义 `ShellExecutor`（`ctx.shell`）+ 自定义 `SandboxProvider` + `tools/pre-execute` 瀑布 + `ctx.tools.guard` |
| B2 | **sec-blackboard** | 黑板域：projects/assets/findings/artifacts/tasks/func_kb/chains/blueprints/intents/approvals/events/http_history 全部对象 + 单一写入口 + 面向 Client 的 Remote API | `ctx.storage`（sqlite backend）+ `ctx.storageDomain`（sec 域 v1）+ `TypertRemoteService`/`@Remote` |
| B3 | **sec-tools-bb** | 黑板工具组（bb_add_asset/bb_add_finding/bb_query/declare_intent/complete_task… 约 20 个） | `ctx.tools.register(defineTool)` |
| B4 | **sec-packs** | packs 内容挂载：31 技能转 dsh skill 目录包；675 kb md 作数据资源；route_index 单表入 sec 域；kb_open/kb_search/route_lookup/skill_open 工具 | `ctx.skills`（skill-filesystem / `DSH_BUNDLED_SKILL_DIR`）+ section/`inject` |
| B5 | **sec-experts** | 17 专家 yaml → agent 预设（persona/技能/工具可见性/任务类型/噪声档） | `ctx.agentPresets`（YAML 预设，per-session 组合）+ subagent `persona`/`toolFilter`（spawn provider） |
| B6 | **sec-orchestrator** | 编排器：Lead + 可继续子 agent + 任务板（DAG/优先级/派单）+ 阶段剧本 + 收件箱 | `ctx.subagents`（startContinuable）+ sec 任务板域 + `turn/end` followup；`ctx.agentTeams` 仅参考（实验性） |
| B7 | **sec-web** | 安全业务页面进 dsh web：资产/发现/画布/测绘/攻击路径/函数库/审批/设置扩展 | Client Slots（`ctx.slots.inject`）+ Remote 消费 |
| B8 | **sec-integrations** | 杂项集成：Ark 模型适配器 + planner/executor/classifier 薄路由、MCP servers、内置浏览器/Replay、FOFA/情报、凭据、goal/schedule | `registerAdapter`（LlmAdapter）、一 MCP server 一插件、`ctx.credentials`、`ctx.goals`、`ctx.schedule` |

边界纪律：**B1 是安全底座，先于一切业务**；B2/B3 共同构成「黑板单写入口」；packs 内容（B4）只做机械搬运与 frontmatter 适配。

### 1.1 包粒度约定（2026-09-26 打磨收敛）

**bundle = 用户启用/发行单元（上表八个）；package = pnpm 开发/构建单元，一个 bundle 由多 package 组成。** 不做单巨包（可替换实现必须独立，否则失去 dsh「换 provider 即换产品」的核心价值；且 Host/Client 是两个独立 aggregate，物理上不能同包）；也不一工具一包（同一领域服务的强相关工具合一包）。

| 包（packages/sec/，宿主侧） | 角色 | bundle |
|---|---|---|
| `gateway` | ShellExecutor + docker/wsl/native SandboxProvider + pathguard/rateguard | B1 |
| `gateway-tools` | run_cmd 模型工具 + 结果卡片（consumer） | B1 |
| `blackboard-core` | sec 域定义、单写入口服务、图/树查询 | B2 |
| `blackboard-controller` | `@Remote` 对外 API | B2 |
| `tools-blackboard` | bb_* 约 20 工具整组（consumer） | B3 |
| `tools-kb` | kb_open/kb_search/route_lookup/skill_open | B4 |
| `experts` | preset loader + 17 yaml | B5 |
| `orchestrator` | 任务板服务 + continuable 派单 + phases 引擎 | B6 |
| `orchestrator-tools` | delegate/done 等 Lead 工具面 | B6 |
| `llm-ark` | Ark 模型适配器 | B8 |
| `tools-browser` / `tools-intel` | 内置浏览器、FOFA/情报 | B8 |

- **MCP 例外**：一 server 一独立小包，不合并。
- **Client（packages/sec-client/）一页面方向一包**：ui-assets / ui-findings / ui-canvas / ui-cyberspace / ui-attackpath / ui-funcs / ui-approvals。
- **packs 内容不是代码包**：760 件 md/yaml 保持文件树，作 bundled resource 挂载；31 技能由 skill-filesystem 直接发现，零 loader 代码。

---

## 2. 能力映射总表（cyberstrike 现状 → dsh 接缝）

| cyberstrike 现状 | dsh 接缝 | 落点 | 坑 / 备注 |
|---|---|---|---|
| 项目 `core/projects.py` + workspaces 目录 | `ctx.workspaceRegistry`（Workspace = 稳定 uuid + 规范路径 + 会话有序账本） | B2 + B6 | 项目元数据（track/目标/场景档五件套物化）另建 sec 域记录；workspace 对模型不可见，正合现有定位 |
| 黑板 18 表（schema v24，SQLite WAL） | `ctx.storage` sqlite backend + `ctx.storageDomain`（defineDomain: name/version/layout/tables） | B2 | KV  facet 模型，**无 SQL join**：chains/graph 的图查询须在域服务内重建；domain `version` 自带版本迁移纪律 |
| assets/findings/artifacts/tasks/func_kb/chains/blueprints/intents/approvals/events/http_history | sec 域内 domain tables | B2 | 见 §3 数据模型注记 |
| core API ~140 端点（单写入口载体） | `TypertRemoteService` + `@Remote` 方法 → Client `ctx.remote.sec.*` | B2/B7 | 失败面统一为 `RemoteError('<domain>/<reason>')`；Client 按 code 分支 |
| bb_* 黑板工具约 20 个 | `defineTool({name,description,parameters,output,execute})` | B3 | execute 收预校验 args、须服从 `exec.signal`、throw=isError；卡片用纯函数 presentCall/presentResult |
| `run_cmd` + Native/WSL/Docker 三后端（backends.py） | 自定义 `ShellExecutor`（resolve/execute）作为 `ctx.shell` 唯一实现；runtime 选择为其内部策略 | B1 | 非零退出/超时/中止全部 resolve（exitCode/signal/timedOut 正交上报）；stdin/env 等仅可信进程内可达，不暴露模型 |
| 沙箱分级 L0–L3、fakenet | 自定义 `SandboxProvider.confine(argv, policy)`：docker-exec（L2/L3）/ wsl / windows-acl（L0/L1） | B1 | dsh 原生无 Docker/WSL provider，**必须自建**；`SANDBOX_UNAVAILABLE` fail-closed，静默直通永不合法；sandbox 只管文件效应，网络另控 |
| pathguard / rateguard | `tools/pre-execute`（allow/deny/ask 瀑布）+ ShellExecutor 内策略 + `ctx.tools.guard()` 单调否决 | B1 | 输入改写不在 pre-execute 允许范围内；加宽策略须审批且一次性 |
| approvals 表与审批流 | `ctx.approval.request()`（ask/never） | B1/B7 | **必须有开放 turn**（idle 时 ask 抛错）；无超时、无跨重启 pending；never 在瀑布前确定性拒绝——「等待人类」队列需自建（见 R5） |
| 专家池 17 yaml（persona/skills/task_types/default_noise/tracks） | `ctx.agentPresets` YAML 预设 + subagent `persona`（scoped persona-prefix section）+ `toolFilter` | B5 | preset 编写细则 M0 对照源码确认；外部产品 provider（acp/codex/claude-code）不支持 per-child 模型/persona，专家差异化只押 spawn/fork/dsh-sdk |
| 编排器 10 工具（delegate/done/bb_overview/budget_status/requeue/set_priorities…） | B6 Lead agent 工具面 + sec 任务板域（CAS 状态转换） | B6 | dsh 无声明式中央调度器；模型驱动协作 |
| 会话窗派单/会话账本 | `ctx.subagents` continuable：持久 Session + Activation + 唯一 inbox FIFO + startContinuable | B6 | 消息仅邻接（直接父子）；inbox 接受后调用方取消无效；冷恢复走 `ctx.agents.resume()` 不经 provider |
| phases 阶段剧本（enter_phase/门指标/L0–L2 分流） | sec-orchestrator 自建：阶段状态域 + `turn/end` followup 驱动 + pre-step 注入 | B6 | dsh 无原生阶段概念；门指标判定移植为纯函数 |
| session_inbox 收件箱 | Agent inbox（`Agent.steer()`）+ sec 域持久 mailbox | B6 | `running`  steer 最近 step / `waiting` 唤醒 / 无 Activation 冷恢复 |
| 31 个 SKILL.md | dsh skill 目录包 `<name>/SKILL.md`（kebab-case，不递归） | B4 | frontmatter 仅识别 `disable-model-invocation`/`user-invocable`，现有 frontmatter 需适配；走 bundled 目录（`DSH_BUNDLED_SKILL_DIR`）或项目 `.dsh/skills` |
| 675 kb md + route_index | 随包数据资源（bundled resource）+ kb_open/kb_search/route_lookup 工具 + pre-step section | B4 | 内容零改写；目录前缀/单表结构入 sec 域；资源按需解析、不枚举 |
| tracks 40 文件（角色/技能/退场红队向） | preset 与 prompt 资源文件 | B5/B6 | 机械迁移 |
| LLM：Ark 优先 + planner/executor/classifier 模型路由 | `registerAdapter` 新 LlmAdapter（OpenAI 兼容基类）+ sec 自建薄路由服务 | B8 | 凭据经 `ctx.credentials` |
| MCP manager | 一个 MCP server 一个插件（dsh 原生模式） | B8 | 比自建管理器更省，manager 功能可退役 |
| 内置浏览器 pool/replay、FOFA、情报 | 自建工具（browser_navigate 等）+ client UI；browserUse 接缝评估 | B8/B7 | Replay/FOFA 为安全特有，无原生 seam |
| webui 全部页面 | dsh web Client Slots 插件；对话/会话/会话列表/设置原生获得 | B7 | 资产/发现/画布/测绘/攻击路径等重做；Slots 组件收 props 不收 ctx |
| 会话/trace 链 | session JSONL 持久事件（turn/*、step/*、tool/*）+ trace 经工具事件投影 | B2/B6 | 「模型可见即已记录」；trace-effect 可由 post-execute 落 sec 域 |
| 启动 .bat / 桌面打包 | dsh web/desktop 启动；Windows 原生开发受支持 | B8 | PyInstaller 线退役；dsh desktop 替代（其打包形态 M0 验证） |
| 自治循环 / goal | `ctx.goals` + goal-round-driver；`turn/end` followup；`ctx.schedule`（idle followup / busy inject） | B6/B8 | 异步状态≠同步状态，自动化区间须显式定义（inbox 回执→idle） |

---

## 3. 黑板数据模型注记

- 一个 sec 存储域（`defineDomain({name:'sec', version:1, layout:'per-record', tables:[...]})`），18 表现存对象逐一映射为 domain table；关系型查询（chain_links 图遍历、assets 树派生、blueprint edges DAG）在 B2 域服务内以代码实现，现有 Python 侧逻辑（assets.py 树派生、graph.py 图算法）是移植规格而非重写设计。
- **单写入口在 dsh 形态下的实现**：写操作只存在于 B2 控制器服务；B3 工具与 B7 Client（Remote）都汇入它。sqlite backend 按名注册后，**不向 sec 之外发布该 backend 名 / 不 mount 给其他插件**，从接缝层收口旁路写——比 Python 时代更依赖装配自律，在 profile patch 中以配置审计 + doctor 体检兜底。
- schema v24 → sec 域 v1 的一次性迁移：随 M8 编写，旧库为输入、sec 域为输出，旧线不删（遵守不删文件纪律，Python 侧整体保留只读）。

## 4. 平台安全不变量的保持方式

| 不变量（现硬约束） | dsh 形态下的保证 |
|---|---|
| 黑板写操作经单一入口，禁旁路直写 | B2 唯一控制器 + backend 名不对外发布 + Remote/工具双汇流 + doctor 配置体检 |
| 不可信代码只在 Docker 执行；WSL 信任级=宿主机 | 自建 docker SandboxProvider 为 L2/L3 唯一执行面；wsl provider 标 trusted 同级；**windows-acl 原生仅部分执行（硬链接/读不可限等），绝不承载不可信代码** |
| 未知样本默认恶意（L3 + fakenet） | 样本类工具默认 sandboxPolicy 只读/容器 + 网络默认禁网；fakenet 随 sandbox-behavior-report / windows-vm-execution 既定方案接 |
| Agent 无裸 shell，一切命令经网关 | 不注册 bash/run_code 给模型，模型工具面仅 run_cmd 等网关件（`tools.restrict` 可见性收口 + reserved 名拒绝）；`ctx.shell` 唯一实现是 B1 |
| 审批 fail-closed | `ctx.approval`：missing/错误应答=unavailable=拒；加宽一次性；无头 never |
| 凭据不泄漏 | dsh 自带 env scrub（*KEY*/*SECRET*/*TOKEN*/*PASSWORD*）+ 0700 随机私有临时文件；凭据走 `ctx.credentials`；FOFA key 等绝不入库纪律不变 |

## 5. 迁移路线（里程碑，每段独立可验证）

| 里程碑 | 内容 | 验证 | 粗估人周 |
|---|---|---|---|
| ~~M0 尖兵验证~~ ✅ 2026-09-26 | 见下方「M0 实施结论」：构建/profile/patch/外部插件/Ark 模型端到端全部落地 | 模型实调 sec_hello 成功并回报原文 | 实耗约 1 |
| ~~M1 网关底座~~ ✅ 2026-09-26 | 见下方「M1 实施结论」：ShellExecutor + docker SandboxProvider + pre-execute 闸 + run_cmd 单工具全部落地 | 宿主/容器双世界、策略否决、fail-closed 冒烟（61+12 检查全绿、Ark 双会话） | 实耗约 2 |
| **M2 黑板域** | B2：sec 域 v1（projects/assets/findings/tasks/events 子集）+ 控制器 + Remote | 单写纪律、域版本、Client 读写通 | 3–4 |
| **M3 packs 内容** | B4：skill 包适配 + kb 资源 + kb/route/skill 工具 | 31 技能目录可发现可注入、kb_search 召回 | 1–2 |
| **M4 专家池** | B5：17 yaml → presets + persona/toolFilter | 专家会话差异化、工具可见性 | 2 |
| **M5 编排器** | B6：任务板 + continuable 派单 + phases | delegate→派单→收尾→重派全链 | 3–4 |
| **M6 Web UI** | B7：资产/发现/画布/测绘/攻击路径 Slots + 审批对接 | 旧壳核心页面在 dsh web 对等可用 | 4–6 |
| **M7 集成件** | B8：MCP 全家、浏览器/Replay、FOFA/情报、goal/schedule、设置 | 各集成件冒烟 | 3–4 |
| **M8 切流退役** | v24→sec 域迁移、双跑对账、启动器替换、Python 线只读保留 | 存量项目完整搬迁、新启动器一键起 | 2 |

### M0 实施结论（2026-09-26）

1. **构建链**：从源码起 web 须跑完整 `pnpm build`（`pnpm typecheck` 不含 client tsdown，会报 63 client packages failed to compose）；3080 被占时 `--port 3081` 避开，sec web 正常启动。
2. **装配链**：sec profile（web 模板）→ `pnpm dsh plugin --profile sec add` 链入外部包 → cordis.patch.yml `insert`；`pluginManager/listPlugins` 显示 `@sec/sec-toy` enabled/active。
3. **工具链**：无 tools 查询 RPC（插件面板只列官方精选），写 [dsh/scripts/check-tools.mts](../../dsh/scripts/check-tools.mts) 引导 sec（剔除 web-app 层免起服务）枚举注册表：共 25 工具含 `sec_hello`，execute 回显 `hello sec — sec toy plugin live`。
4. **模型实调已打通（2026-09-26 下午）**：**无需写新 adapter**——dsh-base 自带的 `llm-deepseek-api-key` 走的就是 Anthropic Messages 协议（默认端点即 `api.deepseek.com/anthropic`），在 profile patch 覆盖 id=`llm-deepseek` 行的 config：`baseURL=https://ark.cn-beijing.volces.com/api/coding`、`apiKeyEnv=ARK_API_KEY`、models 换成 `ark-code-latest`、`maxTokens=131072`（Ark 硬上限，默认 256000 会被拒）；key 由启动 shell 环境注入。模型成功调用 sec_hello 并原文回报，12 秒、15.5K tok。
5. 引导期 typert-loader 的 "parameter codec has no create() factory" 为非致命日志，不影响挂载。
6. **TaskStop 杀 pnpm 不杀 node 孙进程**：端口会被残留 node（hermes 发行版）继续占用，需按端口查出 PID 强杀。

### M1 实施结论（2026-09-26）

交付三包：`@sec/gateway`（入口插件：SecShellExecutor 替 ctx.shell + DockerSandboxProvider 替 ctx.sandbox + tools/pre-execute 守卫）、`@sec/gateway-tools`（`run_cmd` 模型唯一命令口），sec-toy 保留。policy/pathguard/rateguard 从 Python 侧逐行移植为纯函数，`wouldDeny` 为拒因唯一来源。

**验收记录（全绿，未提交）：**

1. **主冒烟 [dsh/scripts/m1-smoke.mts](../../dsh/scripts/m1-smoke.mts) 61/61**：威胁矩阵三威胁类×四 runtime、未知/非法值归 malware_live、net=real M1 显式拒绝；pathguard 写逃逸拦截（Windows 绝对路径/UNC/`~`/相对逃逸/`-o`/Out-File/tee，host 与 docker 双风味）与放例（重定向到现场、`%TMP%`/`$var`、`2>&1`、NUL、URL、`grep -o` 不误伤）；rateguard 拒/放全矩阵；**拒因三方逐字一致**——pre-execute 钩子 == runCommand 直调兜底 == wouldDeny 纯函数；生产层 web-app patch 禁用 tool-bash/tool-pwsh 已加机械断言。
2. **真实 Docker 冒烟 [dsh/scripts/m1-docker-smoke.mts](../../dsh/scripts/m1-docker-smoke.mts) 12/12**：L2 pentest-box 内 uname/nmap/ffuf/python3 真跑 exit 0；nmap 全端口合规命令放行真跑；ffuf `-t 1` 放行真跑；中文 UTF-8 无乱码；容器写入 `x.txt` 在宿主 `<profile>/workspace/scratch/` 真实存在且内容一致；**L3 零挂载**——`/workspace` 不存在、不可见宿主文件；malware_live 走 L2 被矩阵拒不触容器。
3. **fail-closed 实证**：daemon 停止时 docker/sandbox 一律 `SANDBOX_UNAVAILABLE`（daemon/镜像缺失三态可操作指引），**零降级到 host/wsl**；冒烟按 daemon 实时状态分支断言。
4. **Ark 真实 LLM 端到端**：经 dsh web（127.0.0.1:3081）完成两次模型自主会话——host 输出 `llm-host-ok`、docker 输出 `llm-docker-ok`，命令均经 run_cmd 工具与守卫链。
5. Python 侧同步：rateguard ffuf 接受 `-t`（[core/runtime/rateguard.py](../../core/runtime/rateguard.py) + tests/test_rateguard.py，12 passed），TS/Python 单源口径不变。

**实施修正（相对原方案/冒烟初稿）：**

- 容器内 `--cap-drop ALL` 下 nmap 无 raw socket：必须 `-sT` TCP connect 扫；**不加 `-T2`**——sneaky 模板发包间隔把全端口扫拖到数分钟，`--max-rate` 本身即 rateguard 认可的有效节流；`-n` 免反向 DNS（net=none 下 DNS 拖超时）。
- pentest-box 镜像内 ffuf 为 1.1.0，无 `-rate` 也无 `-rl`：rateguard 改为接受 `-t` 限并发，拒因 hint 保留新版文案 `-rl 50` 并注明旧版用 `-t 5`。
- L2 挂载/工作目录最终定为 `<ws>:/workspace` + `-w /workspace/scratch`；pathguard 各 runtime 按路径语义（host Windows / wsl /mnt / docker /workspace；L3 零挂载不检查）传边界。

排序纪律：**M1 不通过不写业务层**（已通过）；M2/M3 可并行；M6 可随 M2 后提前起步（单页面随域对象落地）。

## 6. 风险登记

- **R1（最高）dsh 开发者预览破坏性变更**：README 明示。profile/bundle/patch、subagent、skills、Typert 词汇均可能改。缓解：钉上游 commit、每个接缝外裹薄适配层、M0 即建立跟随节奏。
- **R2 重写规模**：core 3.5 万行/77 文件 + webui 2.6 万行；TS 侧估 2–3 万行新代码。packs 内容零重写是主要减压项。
- **R3 Windows 沙箱部分执行**：不可信代码面只能押自建 Docker provider；Home 版对 Docker Desktop 的依赖、无 Hyper-V 约束延续。
- **R4 agentTeams 实验性**：若直接押注，R1 风险叠加。决策：以一级 seam（subagent continuable）自建薄任务板，agentTeams 仅作设计参考。
- **R5 approval 语义落差**：open-turn 限制、无超时、无跨重启 pending、headless 恒拒；现有「awaiting_human」长等待 UX 需以 sec 域请求队列 + 空闲期 followup 重建。
- **R6 `domain/changed` 仅进程内**：多窗口/桌面窗实时性须走 session/event 或 Remote stream，无现成跨进程推送。
- **R7 KV 模型无关系查询**：图/树/聚合查询全部域服务内重建；无并发写序列化（per-call 原子），多写方设计须避开。
- **R8 上游跟进的长期维护税**：发行版与 dsh master 的分叉维护持续存在。
- **R9 特有集成点无接缝**：F6 浏览器、IDA MCP（可转 MCP 插件）、x64dbg、FOFA 等需自建工具与 UI。
- **R10 排期竞争**：按排期铁律工程先于论文；本工程体量将独占多个月排期，由用户统筹。

## 7. 施工期过渡策略（不改终态）

M0–M2 期间 TS 侧黑板未建成前，允许 dsh 插件经 HTTP 调用旧 Python core API（localhost:8420）作为**临时桥**，以便 M1 网关先跑真实工具链；桥接代码在 M8 随 Python 线一并退役。终态仍为全量 TS 插件，不构成双引擎产品形态。

## 8. 待打磨清单

1. profile 与 bundles 的分发形态：仓库内目录 + 本地 profile，还是独立 npm 包发布？
2. sec 域 v1 各 table schema 与 v24 字段级映射明细（M2 前展开为附表）。
3. 编排器任务板：自建域结构的 CAS/revision 细则；是否吸收 agentTeams 的 mailbox 去重 source 设计。
4. skill frontmatter 适配清单：逐文件核对 31 个 SKILL.md 现有 frontmatter 到两键策略的映射（含模型不可调用技能的处置）。
5. Ark 适配器之外，classifier/planner/executor 三路由在 TS 侧的服务形态（薄路由服务 vs 多 adapter）。
6. 桌面形态：dsh desktop 对现有「窗口模式 owner/attach + 关窗快照」语义的对等程度，M0 验证后补结论。
