# webui/

> React 19 + TS(strict) + Vite SPA——三栏指挥台（DESIGN.md §12），core API 的平等消费者，不含业务逻辑。

漏洞详情按结构化报告顺序预览（编辑状态/等级/类型/报告字段/多 POC）；黑板记录由 AI 创建、人类编辑，不提供人工快速登记入口。

**壳现状**：TRAE 化新壳已移除（2026-09-26 定稿，不再维护）——`src/shell2/` 全删，旧壳为唯一壳；留存资产 `lib/turnStream.ts`、`views/live/editors.tsx`、schema v23 后端。会话中心化配套 UI 随新壳移除，后端端点保留（DESIGN §四）。

## 运行

- 开发 `npm run dev`（5173；`/api` 与 WS 代理到 127.0.0.1:8420）；Vite 只绑 IPv6，浏览器用 `http://localhost:5173`。
- 构建 `npm run build`（tsc -b + vite build，**必须零 TS 错误**）；产物 `dist/` 由后端**同源静态托管**（desktop-app-shell M2，`create_app(static_dir=)` SPA fallback——前端本就走相对路径 `/api/*` + `location.host` 拼 WS，**同源零改动**；src/ 勿写死端口）。改 core 需重启后端，前端 HMR 仅 dev 模式。
- 桌面窗口模式（M1）：`serve.py --window` 或双击「启动平台（窗口）.bat」。**窗口标题栏已拆件**（无独立条）：品牌 `NavBrand` 在左导航顶部（兼作拖动区，双击最大化），窗口按钮 `WindowControls` 项目页并入 `.topbar` 右侧、其余页绝对定位到内容区右上角（`.window-controls-float`）；无标题栏页用 `.window-drag-strip` 作拖动区，顶部 36px 由各视图根节点 `pt-9` 承担，首页 `ProjectsView` 自身是唯一纵向滚动容器；项目页外壳和主区使用 `min-h-0/overflow-hidden`，滚动只由具体视图内部承担，避免右侧出现外层滚动条。项目顶栏取消底部硬分割线，使用 surface token 渐变自然衔接主区。拖动靠 `-webkit-app-region`，交互元素须 `no-drag`（`.topbar button` 等已在 index.css 声明）。

## 页面（src/views/）

- `ProjectsView.tsx` 项目列表+新建向导（先选**场景轨**单选再**专家组队**多选，能力包多选已退役）+回收站；**E9 两栏化**（左主栏 + 右 `IntelBriefCard` 今日简报，「刷新情报」=intelFetch+pollJob）。
- `LiveRoom.tsx` 直播间：顶栏两行（会话页签横滚+●live）；底部输入行 Claude Code 化（附件 chips + 无边框多行 textarea + 工具行 chips）——**编排器工具栏/会话控制/切模型/自主档/用量 全部并入 composer chips 弹层**（`＋开窗`/`⚡编排一轮`/`🛡行动边界`〔仅 redteam ROE〕/`🧠自主档`+auto_derive 开关与 deriveLamp/`∑用量`/`⛓链状态`/`⋯`〔重排优先级+清理工作区〕）。**模式徽章随上下文收敛**：选中会话=不显徽章（会话态唯一模式即引导会话），编排器态=「发任务/指挥编排」两态循环。**发送钮双态**：canAbort（running/paused）时 ↑ 变 **■** 单击直接 `controlSession("abort")` 中断本轮（不区分暂停/中断）；否则 ↑ 发送。**页签 ×=结束会话**（armed/running/paused 先 confirm；closeSession 处理 `{status:"closing"}`）。**引导轮末注入**：canAbort 态走轮末注入（human_note 留收件箱等下一轮认领期），卡片上方出现排队引导条（⚡立即发送=中断当前轮）；空闲态后端立即回应落 `agent.chat` 事件。**页签状态灯 `tabStatus`**（优先级）：`status==="paused"`（DB 稳定事实，最优先——事件窗口约 2 分钟即挤出，纯事件推导会让暂停钮退回）> `worker_running`（青灯恒亮，**执行态唯一权威**）> `worker_armed` 且事件流派生 idle/finished → **armed 青绿点**（F9 worker 排空也落 session.finished 非终态，armed 必须盖过它）。**任务尝试树**：顶栏「直播｜任务树」→ `views/live/TaskTree.tsx`（React.lazy 分包，见 [`views/live/CLAUDE.md`](src/views/live/CLAUDE.md)）。**多智能体协调**并入为第三段「直播｜任务树｜协调」→ `views/live/CoordinationPane.tsx`（React.lazy，见末条）。**编排一轮**消费批 3 结构化 job 结果（summary/📤n 任务/🪟m 窗/💡n 提案/📝简报）；并发 tick 服务端 **409** 直显。
- `Blackboard.tsx` 黑板：发现/资产/函数库+human 共写；**函数库 tab 仅 capabilities 含 binary 挂载**，**资产树全轨启用**；发现 tab「列表｜链路」子切换仅 pentest/redteam；**C2 ctf 轨线索卡流**（severity→四级线索级别）；**E6/E7 资产**：类型下拉默认「自动」（422 显行内红字），`AssetBadges` 优先显示 API 读时派生的 `effective_status`（所有子节点收敛后根节点显示「已测试·干净」；缺失时回退持久化 `status`），前端不自行推导、不回写根节点；「有发现」红徽章由 verified findings 每 4s 反查，徽章紧贴资产值（`shrink truncate` 在前、`flex-1` 占位其后），行内 🗑 指定资产删除。攻击链画布见 [`views/blackboard/CLAUDE.md`](src/views/blackboard/CLAUDE.md)。
- `TaskBoard.tsx` 任务看板（四态即四窗态，「待认领→**待执行**」）：删除钮四态皆可（claimed 文案「取消」+步边界硬中断警示）；claimed 卡显 `▦ done/total` 计划进度；failed 卡 `resumable` 显「▶ 续跑」；awaiting_human 卡显「⏸ 待人工」+「✅ 已解决，放回继续」；`↻N 次尝试` 徽章（`task.context.attempts`≥2）。**双击任务卡直开会话（全态通用）**→ dispatch `goto-session`（open 任务跳转后追加 `api.agentWork(sid, task.id)` 按该任务 ID 显式起跑）。
- **审批=对话内联卡（2026-10-04 改版，独立审批页/左导航项已删）**：需要审批时直接在对话页弹「审批问答卡」（`components/chat/ApprovalCard.tsx`：图标+标题 / op 专属说明 / 批准·拒绝单选 / 附加说明 / 提交 / 和 Agent 聊聊）。数据源 `lib/usePendingApprovals.ts`（3s 轮询 `api.approvals(pid,"pending")`；新 `approval.requested` 事件唤醒重拉——**事件载荷字段不足**，完整 `action`/`boundary` 只有列表端点给得到，故后端零改动）。接入两处：直播间 `LiveRoom` 事件流内联（命中待审批→卡；已决策/未到→紧凑审计行）、工作台 `AgentWorkbenchView` 贴 composer 悬浮（隐藏「和 Agent 聊聊」）；`TraeView` 同走共享卡。提交=附加说明先 `api.sessionNote` 再 `api.decideApproval`（`executed:false` 失败回执卡内红字，不回滚批准）。**顶栏铃铛保留**：计数=待审批+`awaiting_human` 任务，点击跳「有审批的会话」（无则任务看板/直播间）。
- `browser/` F6 内置浏览器页（**轨门控 pentest/redteam/ctf**，见 [`views/browser/CLAUDE.md`](views/browser/CLAUDE.md)）：三栏（动作时间线/URL+CDP 实时画面流/抓包|拦截|重发|爆破 Tabs）；**v3 去会话化**（人工走后端隐式会话 human-main）；拦截请求/响应两开关（仅人工流量，二进制只读）；重发改单一原始报文 textarea；未装 playwright 顶幅降级横幅；**重发/爆破/拦截裁决人类 UI 专属（红线）**。
- `IntelView.tsx` 情报页（E9/E10，顶级导航 needsProject=false **+ homeOnly**）：三 tab=简报/文章/学习（三来源档案+当周学习计划）；打开页时今日无简报自动补跑一次。`SettingsView.tsx` 设置 **11 tab**。
- `views/reverse/`（DESIGN §12）：`ReverseWorkbench.tsx` 全屏（SampleBar+subnav：逆向分析｜攻击链｜蓝图｜业务逻辑，**同级条件渲染**）；`RevCompact.tsx` 挂机侧栏；`chains/` 人工建链（React Flow，React.lazy 切包）；`blueprints/`（R4，人类侧 status 流转）；`logic/`（schema v27 业务逻辑块）；三层数据 headless v3 缓存 JSON / func_kb / findings，**所有地址都是 hex 字符串**。
- `components/workbench/WorkbenchContextRail.tsx` 工作台右侧上下文栏：与发现栏合并为单列抽屉；收起态贴近右缘且不显示图标，仅在鼠标移入时显示向左箭头，点击后展开同一面板切换发现或浏览器/终端/文件。浏览器仅展示 `origin=agent`（Electron 可点击激活，Web 只读）；终端仅 Electron 复用 node-pty PowerShell 原生 tab、按项目 cwd 按需创建；文件树复用受限 `api.workspaceTree` 与 `api.openProjectFile`，服务端过滤敏感目录/文件并限制深度与节点数。
- `views/AgentWorkbenchView.tsx` 智能体工作台（K9，View="agents"）：对话式挖洞（蛙池式）。左栏 agent 触发卡+线程列表；中栏时间线（**会话流改造 2026-10-03，cc-haha 风格**：无气泡——用户一行 `❯`、agent 叙述平铺 `MarkdownView`、一轮内连续工具调用折成一行灰色摘要、叙述提到的文件挂文件卡、**思考折成单行 `ThinkingBlock`（v31 持久化，轮结束后仍显「已思考」）**、顶部 `ChatStats` 统计行；装配纯函数 `buildWbItems` 在本文件（`useMemo` 记忆化），`ChatMessage[]` 模型与直播间的 `BBEvent[]` 不同故各自实现）。**2026-10-04 三修**：①「发送后空白」——乐观行 `pendingIn` 只在持久化 user 消息进了 `messages` 后才清（此前 WS 事件先到即清、轮询未到→空窗），事件改触发即时补拉，且无线程/空线程回退 `Hero`；②自动滚底补 `liveThinking` 依赖；③思考改单行折叠。底部 **wb-dock 输入大卡片**（斜杠命令面板+引用 chips+用量圆环）。样式集中 index.css `.wb-*` 段——**会话流部分已迁至 Tailwind 工具类**（气泡/卡片/工具块一族 CSS 已删），`.wb-shell/.wb-aside/.wb-dock/.wb-composer/.wb-panel` 等保留。右栏「漏洞/发现」+ 链路视图内联（`FindingsRail.tsx`：原地切换 list|chain，懒加载 AttackPath）。数据链：REST 2s 轮询为主，`chat.delta`/`chat.thinking.delta` 事件做实时增强。
- `components/chat/`（**新，2026-10-03；2026-10-04 加 `ThinkingBlock`**）：直播间与工作台**共用**的会话流件——`ActivityGroup`（折叠活动摘要行）/ `FileCard`（文件卡 + 「打开方式」下拉四动作）/ `ChatStats`（token·最后更新·条数统计行）/ `ThinkingBlock`（思考单行折叠，预览跟随尾部）。数据源是纯函数：`lib/activity.ts`（摘要归并 + 中文动词表 + `formatDuration` + `thinkingPreview`）、`lib/fileTargets.ts`（工具/叙述两路抽文件目标 + 徽章）。详见 [`src/components/chat/CLAUDE.md`](src/components/chat/CLAUDE.md)。
- **本地文件动作（2026-10-03）**：文件卡「打开方式」经 `api.openProjectFile(pid, path, action)` 打 `POST /api/projects/{pid}/files/open`（resolve/open/reveal/content），后端 scope 白名单=项目工作区 ∪ packs ∪ tools（越界 422）。**无鉴权且不按扩展名设限**——samples/ 内样本也能一键交给系统程序，与 `/binaries/{sha}/open` 同一风险等级（已知取舍，见 core/api/CLAUDE.md）。
- `App.tsx` 监听 window `goto-settings`/`goto-intel` 跨视图切换；**事件订阅隔离（2026-10-04）**：顶栏统计的去抖重拉订阅挪进返回 null 的 `EventsDebouncedRefresh` 子组件——此前 `useEvents` 挂在 App 根上，流式期每条事件（~6-8Hz）都重渲整棵应用树（工作台/发现栏/直播间全跟着重渲）；`FindingsRail` 同款（订阅挪进 `FindingsWatcher` + 组件 `memo`）。直播间 skill.routed 命中行**路由名单击**即 dispatch 跳技能（F12）。**深链选中的技能默认预览模式打开**；**点链接落到二进制包已修**（project effect 异步回调按维度守卫，缺省包取 `capabilities_bound`）。

### 设置 11 tab（components/settings/）

**表头已移除（2026-10-04）**：原「OBSIUS / CONTROL CENTER」表头 + 同步提示 + 场景/能力包 选择器**项目内外都不再渲染**（相关 CSS `.settings-header/-select/-search/-notice/-icon-button` 已删）；只保留 `DoctorBar`（健康度，全局信息）且**仅首页**（`pid` 为空）显示。`main.settings-main` 恒补 12px 顶距（原 `.is-bare` 条件已并入）。**已知副作用（用户确认接受）**：首页 skills/矩阵/安全红线 等 tab 的 track/cap 入口仅剩矩阵点选与深链，无独立选择器。
**侧栏精简（2026-10-04）**：「控制中心」品牌块（`.settings-brand*`）删除（左侧主导航已有「设置」入口），改为单行 `.settings-context`（live dot + 轨/包；**无底边线**——那条线只在侧栏宽度内、到右栏就断，反而生硬；左内边距 21px 与导航项对齐），窄栏（≤760px）隐藏。
**`pt-9` 按分支条件加（2026-10-04）**：`settings-shell` 的 36px 顶距是给**首页无顶栏**分支（`App.tsx` 的 `window-drag-strip` 让位）用的；项目内那条分支**已有 `.topbar`(h-16)**，再无脑加 `pt-9` 就是顶栏下多出一截死区。故 `className={cn("settings-shell", !pid && "pt-9")}`——`SettingsView` 是唯一被两个分支复用的视图，其余 `pt-9` 视图（KnowledgeView/IntelView/ProjectsView/SkillsView）都只在首页分支出现，不受影响。

1 **专家** ExpertsPane（替换 RolesPane，轨角色写端点已退役 410）：左列表右表单，保存=全字段覆写，variants 只读，protected 拒删；`ExpertCreateDialog` 新建（slug 409/422 直显）。
2 **Skill** SkillsPane：**react-resizable-panels 三栏**（列表/文档/试算+大纲），只管理 `packs/*/skills/`，与知识库彻底分离（K7）；一级导航「技能库」= `SkillWorkspace`（概览/文件两态，**概览底部正文与文件态 .md 均 Markdown 渲染**——概览剥 frontmatter 只渲染正文，文件态默认预览、可切编辑，对齐 `KbPane`）。
2.5 **知识库** KbView（K7 从技能页拆出）：三栏（KbTree/KbPane/MarkdownOutline），管理 `packs/kb/<cap>/`；`KbTree` 双通道搜索（文件名过滤 + ≥2 字符 300ms 防抖调 `api.kbSearch` 正文搜索）。
3 **矩阵** MatrixPane 只读**转置布局**（行=包∪轨技能，列=轨角色）。
4 **红线** RulesPane：左文件列表+右单文件编辑器（轨红线/包红线/owners/rating 四组），**默认 markdown 预览渲染（可切编辑，对齐 SkillWorkspace）**，**F11 生效档案勾选区**；切文件/切轨包前 dirty confirm；文件缺失 GET 200 `{exists:false}`。
4.5 **顾问** AdvisorPane（D10）：三数字行（stuck_after/stuck_max_extensions/closing_max_rounds）+ 顾问模型两级 select；保存 PATCH config.advisor 整段替换。
5 **模型** LlmPane：**图一/图二式行列表**（↑↓ + 状态点 + 名称 + `根地址 · 首个模型` 副标题 + 兼容格式/默认徽章 + 行内「测试/编辑」+ 启停开关 + 删除）。**行内「测试」**=测该供应商首个模型是否可用（结果就地显示 ✓/✗）。**「编辑」/「+ 添加供应商」共用一个弹窗**（`components/settings/ProviderDialog.tsx`，`initial=null` 为新增、非 null 为编辑）：新增态顶部**预设 chips**（`lib/providerPresets.ts`：一点即填名称/根地址/兼容格式/Key 获取链接，模型留空避免硬编码过时 id）；卡内**「获取模型」**= `api.discoverLlm`（上游 `/v1/models`，不支持则降级探活候选）+「测试连接」；模型清单含每模型「上下文 token」输入与逐个「测试」；编辑态密钥留空=沿用已存密钥。**根地址口径**：填**根**，后端自动追加 `/v1/{chat/completions|responses|messages}`（见 core/llm）。
6 **MCP** McpPane（仅配置）；**6.5 网关** GatewayPane（只读快照+宿主能力探测实况，底部「永不前端可编辑」）；**6.6 工具** AgentToolsPane（`api.agentTools` 静态只读目录，8 组）。
7 **情报源** IntelSourcePane（feeds 行编辑+画像七方向+**Obsidian vault 节**）。
8 **提案** ProposalsPane（左列表+右详情含实时 diff，批准/拒绝/**改后采纳**）。

## 结构与约定

- `lib/api.ts` 唯一 fetch 客户端 + pollJob/wsUrl/httpUpload（multipart **勿手设 Content-Type**）；**http() 204 特判**返 undefined 不调 `.json()`（后端 204 端点空 body 曾致前端报「Unexpected end of JSON input」）；失败抛 **ApiError**（`status`/`data`）。
- `lib/types.ts` 全部接口；`lib/workbench.ts` deriveWorkbenchProfile（**research 轨直判 rev-generic**，`config.board_view.default` 非 funcs 交还渗透黑板）；`lib/skillfm.ts` frontmatter 七键解析；`lib/taxonomy.ts` TRACK/CAP/bindingBadge；`lib/roles.ts` **角色显示名唯一出口**（roleLabel/roleName/sessionLabel，勿再硬编码 `_generalist` 特判）。
- `lib/datetime.ts` 时间展示**唯一入口**：后端 UTC 三形态（naive 无 Z 按 UTC 补 Z 勿直接 `new Date`／带 `+00:00`／packs 紧凑版本号）；展示只用 `fmtDateTime/fmtDateTimeMin/fmtDate/fmtTime`；**禁止再 slice/replace ISO 手拼**。
- `lib/events.tsx` 事件样式/摘要（**只管筛选匹配/标签/摘要**，渲染布局外移至 `views/live/EventRow.tsx`）：`TOOL_SUMMARIZERS` per-tool 语义摘要表 + `clip` 分级截断 + `SummaryCtx.assetName` 资产反查（`eventSummary` 第 3 参 ctx、第 4 参 kind）；`FILTERS` 类型筛选 tab（全部/思考/决策/路由/命令/工具/发现/计划）；「计划」是**特殊标签**（选中渲染 `views/live/PlanPanel.tsx` 结构化面板替代事件流）。批 6：`orch.proposed` 事件在 LiveRoom 显行内「采纳」钮（走人类写口 created_by=human）。
- `lib/useEvents.ts` 事件流分页加载：首屏 REST `eventsTail` 拉最新 50 条 → WS 带缓存游标只推增量；上翻 `loadEarlier()` 50 条/批；已加载事件入**模块级 Map 缓存**（多实例共享，切走再回零请求水合）。**会话维度分页+滑动窗口**：签名 `useEvents(pid, sessionId?)`，会话页签走会话源（cache 键 `pid:sid`），`trimDom(max, keep)`=滑动窗口裁剪出口（LiveRoom MAX_DOM=300）。**切页签竞态**：effect 内**所有异步路径**守卫一律用 effect 局部 `let alive`（勿用共享 ref——旧 WS 的异步 close 会自我续命成孤儿）。**卡住看门狗+心跳**：服务端空闲 tick 帧刷新 `lastMsgAt`，看门狗 5s 间隔——`readyState===OPEN`（勿读闭包 state）且 **20s 零帧=半开连接**→主动 close 走既有重连+游标回放；握手 10s 超时；visibilitychange 回前台落缓冲+补拉一拍。
- 自主配置：`Autonomy/ProjectUsage(+chain?)/ChainState/OrchTickResult` 在 types.ts；`api.patchProjectConfig(pid,{autonomy:整段})` 打 PATCH /config（服务端整段归一化，前端始终以当前 usage 拼全字段再 spread 补丁）；`deriveLamp(usage)` 四态（灰=auto_derive 关／琥珀=paused·token pct≥100·last_result error|empty／绿=其余）。
- 风格：克制黑客风，等宽只用于数据区。**主题**：`src/lib/theme.ts` 维护 `THEMES`/`THEME_IDS`/`applyTheme`，内置 `cyber-dark`（暗黑，默认）/ `warm-dark`（暖炭：暖灰+珊瑚橙）/ `light`（明亮）三套；`document.documentElement.dataset.theme` + localStorage `ui.theme`，`index.html` 首帧脚本防 FOUC（**新增主题必须同步该脚本的白名单**），设置页「外观主题」选择器切换（每张卡自带 `data-theme` 作用域，预览即该主题真实配色——token 选择器是属性匹配，可局部嵌套套用）。颜色唯一 token 源是 `index.css`：**每个主题块须定义同一套 token**——shadcn 语义 token + `--brand-*`/`--surface-*`/`--viz-*`/`--status-*-bg` + `--theme-raw-*` 兼容层（旧组件消费的精确值），`color-scheme` 也在此声明（勿在组件/规则里硬编码）。**新增颜色不得在组件写 hex/oklch**，画布色用 `--viz-*`（canvas.css/tree.css 同样走 `--viz-*`）。**全局滚动条**（index.css @layer base）透明轨道只留暗色滑块，勿在组件里再单写。packs 写后 dispatch `packs-changed`；提案落定 dispatch `proposals-changed`；写操作服务端自动 .history 备份。跨 tab 跳转选中用 focus nonce（`{...,n:Date.now()}`，effect 依赖 focus?.n）。
- 轮询：sessions 5s、黑板/逆向 4s、任务 3s、审批 5s（WS 断了照常用）；App 顶栏统计订阅 WS 后 400ms 去抖重拉。**顶栏 getProject 轮询收到 404 自动 setPid(null) 退回项目列表**（只认 404，409/网络错误不退）。
- **多智能体协调已并入直播间（2026-10-04）**：原独立顶级页 `MultiAgentCoordinationView.tsx`（及左导航「多智能体协调」项）删除，迁为 `views/live/CoordinationPane.tsx`——直播间顶栏「直播｜任务树｜**协调**」第三段（`LiveRoom.tsx` `viewMode: "live"|"tree"|"coord"`）。计划/依赖图/驾驶舱/对象冲突只读面复用，数据源不变（`/api/projects/{pid}/coordination...` 3s 轮询）；协调子视图内点会话行或已绑定节点 → 切回直播视图并选中该会话。**智能体工作台不动**。

- **LiveRoom 协调团队**：`CoordinationPlan.team` 是后端基于 `config.team` 输出的成员名册，节点 `member_id` 对应团队席位；提案卡、确认弹窗和协调页按成员展示职责及任务归属。存量计划的 `source=legacy_derived` 表示按 role 兼容推导。依赖 DAG 是工作流，不是成员/会话关系图；实际执行仍走 preflight/start/delegate。

## 坑

- **高度链**：App.tsx 主区包装统一 `h-full w-full`（rev 另加 overflow-hidden），画布类定高视图一路 `min-h-0` 不可断。
- **@xyflow/react v12 受控节点**：受控 `nodes` 按引用比较，重渲染给不带 `measured` 的新节点会清测量→边卸载；必须在 `onNodesChange` 回收 dimensions 回灌派生节点。EdgeLabelRenderer 容器 pointer-events:none，标签要自加 pointer-events-auto。**无头/自动化排查图问题先查 `document.visibilityState`**（隐藏标签页冻结 rAF 与 ResizeObserver，非产品缺陷）。
- **react-resizable-panels 是 v4 API**：`Group(orientation)/Panel/Separator`（非旧版 PanelGroup/PanelResizeHandle）；Panel 的 number 尺寸=像素、带单位字符串=%。**全局侧栏收起**：App.tsx `navCollapsed`（localStorage `ui.nav-collapsed`）条件渲染卸载 nav Panel+Separator。
- **左导航「更多」折叠分组**：`NAV_GROUPS` 第三组 `collapsible:true`——会话/任务降为二级入口默认折叠；`moreActive` 时强制展开保高亮，窄栏恒展开，`locked` 不产生空组。
- **radix Tabs 在 mousedown/focus 激活不是 click**：JS 合成 `.click()` 不切 tab，用真实点击或 `.focus()`。
- **Tailwind v4.3 起 `bg-[--var]` 简写被移除**：编译成 `background-color: --status-x`（缺 `var()`，声明非法被丢弃）——类名在 DOM 上 computed 却是 transparent，**CSS 无报错、灯/语义色全部静默失效**。全库统一 v4 变量简写 `bg-(--status-x)`/`text-(--status-x)`（括号成对）；**变量本身也必须先在 index.css 定义**（`--status-ok` 曾漏定义致绿灯隐形）。
- shadcn `add` 可能把 cn 导入写成 `"cn"` 包，改回 `@/lib/utils`；tsconfig.app.json 不写 baseUrl。
- Vite 只听 `::1`，curl 127.0.0.1:5173 拒连；中文 JSON body 用 Python/httpx 测，勿用 Git Bash curl 管道（乱码成假象）。
- **设置页按轨/包拉列表必须带竞态守卫**（`let alive = true` + effect cleanup 丢弃过期响应，RolesPane/SkillsPane/MatrixPane 三处同款）。
- **Radix `AlertDialogAction` 点击即关弹**：Action 默认语义是 click 后立刻 close——若删除失败原因渲染在弹窗内，错误到达时弹窗已关（「关了没删」零反馈）。二选一：①弹窗内显失败原因 → onClick `e.preventDefault()` 拦关弹，成功才关；②失败原因显弹窗外 → 用普通 Button。
- **弹窗内长文本溢出 = grid min-content 撑爆，用 `wrap-anywhere` 不用 `break-words`**：`AlertDialogContent` 是 grid，超长不可断 token 的 min-content 沿链把轨道撑爆（`line-clamp`/`overflow-hidden` 形同虚设）；`overflow-wrap:anywhere` 才收窄 min-content（`break-words` 按 spec 不影响 min-content，对 grid/flex 撑爆无效）；`AlertDialogContent` 已加 `max-h-[85dvh] overflow-y-auto` 兜底。
- **删项目 422「目录被占用」排查口诀**：WinError 5/32 = 有进程攥着 `workspaces/<proj>/` 句柄（SQLite WAL 的 -wal/-shm 或进程 cwd 在目录内）——先查是否有**多个 serve.py 并存**与遗留测试/工具进程，`Get-CimInstance Win32_Process` 看 CommandLine，强杀后重试。
- **排队引导条清条 effect 必须用事件 id 游标只看新事件**（`claimCursor` ref）——否则历史 task.claimed 在每次 events 变化时误清排队条（曾致条闪现即消）。
- **会话页加载卡顿三件套**：`useEvents` 回入只水合尾部 `HYDRATE_LIMIT=300` 条（更早的留缓存，`loadEarlier` 先吃缓存再打网络，缓存总量 `CACHE_LIMIT=2000` 裁最旧并降级 `loadedAll=false`）；WS 增量 **批量 flush**（`useEvents(pid, sid, {flushMs})`，默认 100ms；**工作台流式期传 50ms≈20Hz**，否则前端 10Hz 封顶把后端 ~20Hz 的 delta 合并成「一顿顿的」，2026-10-04）；`EventRow` **memo 化**——比较器用**事件对象引用相等**（BBEvent 不可变跨重算引用稳定），`onToggle`/`handleRouteJump` 已 useCallback 收口勿传新函数。虚拟滚动挂账未做（仍卡再上 react-virtuoso）。
