# cyberstrike-pro 设计文档

> 版本: v0.32 (2026-09-16)
本文档是项目蓝图，后续开发以本文档为准；重大变更需更新此文档。
> 
> **状态约定（v0.3 起）**：正文默认以**现在时描述已落地的系统现状**；未落地的定稿设计在节/段首以引用块标注：  
**〔未实施〕**＝代码未动，仅有设计（括注排期，如 F 批 / R1 / Phase 3）；  
**〔部分落地〕**＝注明"已有/缺口"。
> 
> **当前总体状态**：Phase 1 核心平台完成，Phase 2 进行中。已落地的模块级明细见下「落地状态总览」表与 §6.7/§6.8 各节状态标注（此处不再重复罗列）。**未落地**：F 批协调机制余部（1.2、1.3、1.7 传播本体、1.8、1.10、1.5 的 Agent 自由私信侧——1.1/1.4 已落地，见 §17 B 组）、编排与任务语义（C1 暂停语义/C2 编排拆解/C3 作战模式/C5 CTF 线索板，见 §17 C 组）、逆向复用 R1/R2/R3（D1/D2/D3，见 §17 D 组）、development 轨（§15，含 §15.5 AI 反代/破甲专项）、malware 轨与 fakenet（Phase 3）。
> 
> 历史：v0.32 (2026-09-16) 待做清单条目 ID 重编（纯文档）：按新组顺序连续编号——B 组 B1(取消传播+私信)/B2(逆向互斥)/B3(联动审批)/B4(blocked_by+分片)，C 组 C1(暂停语义)/C2(编排拆解)/C3(作战模式)/C4(L2 自动续跑)/C5(CTF 线索板)，D 组 D1(R1 缓存)/D2(R2 函数库报告)/D3(R3 签名)，E 组 E1(fakenet+malware)/E2(development 轨)，F 组 F1(三栏拖拽)/F2(终端页)/F3(assessment 收尾)/F4(杂项)；§17 头部附旧→新对照表，正文互引与外部引用同步；历史行沿用当时 ID 不改写；已落地批次标签在代码/CLAUDE.md 改用机制号消歧。 待做清单重整（纯文档）：B1/B2 移除后，实战走查催生的 E14/E15/E16 与 E11 自原 E 组拆出成立 **C 组 编排与任务语义**（实战优先），逆向复用顺延为 D 组、新场景轨为 E 组、体验与基建归位 F 组；B 组余部重排 B4→B5→B6→B3（价值×成本，B3 仅 D1 前置押后）；补账 **E17 L2 档步数耗尽自动续跑**（E8 后置项原漏挂）；排序理由段重写，外部引用（§6.4/§10/§3 注记）同步。条目 ID 为稳定句柄不随组变。 B1+B2 落地（schema v6→v7，F 批协调底座首批）：机制 1.1 发布去重（dedup_fp 指纹/人类 force 确认/Agent [复用] 回填）+ workset 软声明 + 任务流图建议私信边（kind=suggest 点虚线，A3 原 TODO）；机制 1.4 资源租约（新表 resource_leases + leases.py 键白名单归一化、passive→S/active→X 模式映射、认领转写授予、wait_for 门控 claim_next 排除、收尾/删除/过期释放并重校验、环检测安全网 lock.deadlock_victim；运行期动态锁后置）。起因=落实 B 组商议定稿。 E16 作战模式与 mission 战役目标定稿（§6.9 新小节）：**渗透测试（mode=pentest 缺省）**=挖漏洞形成 verified 证据链，验证上限=**影响证明级**（SQL 注入读敏感表/RCE 一次性回显允许），禁驻留/持久化/横向/提权推进；**红队行动（mode=redteam）**=信息收集+外网打点+社工钓鱼（未开发）+内网渗透+提权+横向，以获取重要高危业务数据与设备权限为主，打穿 mission 判据清单为主要目标；mode 与自主档/能力包正交、角色集共用不建新轨，安全红线不放松——仅 §6.3 第 3 级「主动利用未认领目标」禁令在 redteam ROE 四要素核验+授权范围内放开（切换门槛=二次确认+ROE 必填+mode.changed 审计，收紧无门槛）；项目级 mission{text,criteria} 注入编排 overview，converged 从「零产出停链」升级为「mission 判据达成或资产穷尽」（redteam 判据清单全打完=完成、缺省全部已登记资产；七闸不放松）；社工钓鱼+shell 管理页（被攻陷目标会话化管理视图，复用会话设施不建独立 C2、Agent 无裸 shell 不变）仿 §15.5 专项挂账；§17 19 挂账五批拆解，实施后置；起因=用户重申渗透/红队两个定义与长任务语义（渗透没挖到漏洞自动扫其他资产=mission+E14 低水位承载）。纯文档定稿，无代码改动。v0.28 (2026-09-16) E15 任务暂停语义统一+待人工输入承接定稿（§6.1 新小节；§3 E12 扩展注记；§6.4 放回注记；§12 看板行）：fail_task 结构化 blocked_reason（awaiting_human|error，tasks 幂等加列，缺省 error 向后兼容）——「等人类输入」与真失败在看板可区分、暂停原因上卡片正文不再藏 tooltip；awaiting_human 保留会话快照走 E12 既有「▶ 续跑」（限原会话），三类「停」（人工中断/E8 步数耗尽/Agent 自主挂起）统一为「都能继续」；放回可附人类补充说明写进任务行；有旧 plan 的任务被新会话认领时提示层注入旧 plan（done 步骤带真实性注记不视为已验证）+该任务 findings 摘要；awaiting_human 计入审批铃铛红点（只计数不混 approval 表）；宁严勿松——不自动重试不自动放回、L2 自动链不处置。起因=实测 ROE 未核验自主挂起任务无继续入口、放回重做白烧 2.78M tokens 且新会话误读旧计划 done 标记；§17 18 挂账五批拆解，实施后置。纯文档定稿，无代码改动。v0.27 (2026-09-16) E14 编排器任务拆解+资产分批发布+低水位补任务定稿（§6.4 新小节「任务拆解与分批发布」）：编排 tick 分析新任务可拆解为带 parent_id 的子任务并对不同目标同时开窗——深度 1 层（子任务不可再被编排器拆）、每轮发布硬闸 max_publish_per_tick=5（L0 提案同闸计数）、权威校验收敛到 TaskQueue.check_parent（编排器/人类 POST/提案采纳三写路径共用）、_stats() 注入资产视图（计数+未覆盖清单 cap 30 带 id+uncovered_total，判定为提示层、真护栏 conflict_keys 互斥）、分批 3-5/轮经 worker 空退事件驱动续批（无新触发点）、L0/L1/L2 同逻辑；§17 17 挂账四批拆解，实施后置；机制 1.3 加部分前置注记。纯文档定稿，无代码改动。v0.26 (2026-09-16) E10 Obsidian 接入 + 学习计划落地（§16.3 三批一次实施）：core/intel/ 增 vault.py（index_vault 只读索引=frontmatter 复用/title 三级回退/tags fm+内联去重/SKIP_DIRS，build_tree 嵌套化）+ intel.db SCHEMA_VERSION 1→2（vault_notes/learning_plans 两表，DDL 全 IF NOT EXISTS 幂等迁移）+ config.py load/save_profile 重写为**透传未知键**（手编字段不再丢）+ learning_profile 三来源聚合（声明画像+vault 元数据推断【不读正文】+platform_direction_counts）+ compose_weekly_plan（classifier 只喂**元数据**——隐私红线测试断言 LLM 入参不含笔记正文；LLM 失败降级模板）；API 增 vault+learning 八端点（PUT vault 配了路径即自动索引 Job；learning/plan POST=Job、GET 缺省 latest）；前端 IntelView 学习 tab 换三来源档案+当周计划（生成/复制 md/归档周列表）、IntelSourcePane 加 vault 节（保存并索引/重建索引/n 篇·上次索引）。实施注记（v1 取舍）：tree/search 全走索引（请求路径零 FS 访问，天然无穿越）；索引为全量重建、无自动监听，编辑后手动重建（staleness 可接受）。v0.25 (2026-09-16) 编排开窗赛跑修复：spawn_session 批准处理器加 open 任务终检（无 open 任务不建窗，落 approval.exec_failed，防空窗占 cap）+ spawn_session 工具描述补「既有会话可覆盖时不提案开窗」纪律；起因=L1 tick 的 B 触发 kick 旧窗抢走放回任务、审批落地开出两个空窗占满 4/4。v0.24 (2026-09-16) E13 三栏框架可拖拽调宽定稿（§12 布局定稿）：左导航栏与右侧黑板侧栏改 react-resizable-panels v4 可拖拽 Panel（不引新依赖，复用 SkillsPane 模式；左 48–220px、右 288–640px）、两分支逐字重复的 nav JSX 抽取为 NavRail 组件、宽度 localStorage 持久化（ui.nav-width/ui.board-width 设备级）、折叠钮与 boardOpen 逻辑不动（条件 Panel 容忍度实施时实测）；纯前端无后端改动，§17 17 挂账实施后置。v0.23 (2026-09-16) E12 意外终止任务可续跑落地（§3）：中断保留现场——`_abort_current_task` 不再销毁落盘快照（`task.failed` 带 resumable；仅任务已删除的中断仍清防孤儿）、`revive_snapshot` 限原会话复活、API `POST /tasks/{tid}/resume`（reopen+claim+清 _stop_after_task+续跑，budget 快照缺省 +200）、看板 failed 卡「▶ 续跑」、两处中断确认弹窗、close 会话显式清快照修孤儿泄漏；起因=暂停后误点中断快照被销毁任务单程失败。v0.22 (2026-09-16) E 组收尾商议定稿（纯文档，无代码）：E4 WebUI 终端页设计定稿写入 §12 新小节「终端页与人类命令通路」——E4a 人类 one-shot 命令面板（POST /exec 经 gateway，author=human，审计/审批全继承）+ E4b 交互终端（持久 workspace 容器 cyb-ws-<slug> 惰性创建/空闲回收/reconcile + PTY 三后端 + 双向 WS + xterm.js lazy）；安全边界定稿：人类命令必须过网关、交互终端人类专属（Agent 工具面不新增 PTY 工具）、host 终端默认关闭（settings 显式开+仅 127.0.0.1）、WSL 终端不提供、PTY 不逐键审计（session 级 open/close 代替）；§7 补「人类命令同层」一句。E10/E11 定实施拆批写入 §17 条目（E10 三批：vault 索引→学习档案→周计划，E10 先于 E11；E11 五批：换词表卡流→路标→三级注入 IP 聚合→产物附件→补缺）。v0.21 (2026-09-16) E9 情报面板 v1 落地（§16）：core/intel/ 新模块——全局存储 config/intel/（intel.db SQLite WAL + feeds.json + profile.json，全局 DB 新模式）、触发式抓取（NVD/KEV/GitHub Advisory 经免 key REST/中文社区 RSS，urllib 出站不经网关，getter 可注入，单源失败不中断）、classifier 打分（profile 权重侧乘封顶 100）与中文简报合成（**LLM 缺席降级规则打分与模板简报，不 503**）、run_refresh 管线（文章配比=热点 1–2+技术 3–5）；API `/api/intel/*` 十端点（惰性建库、intel_getter/intel_llm 测试注入口）；前端情报页三 tab（简报/文章/学习）+ App 顶级导航 + 项目页两栏简报卡 + 设置页「情报源」tab；§17 E 组移除 E9 并重编号。实施注记：GitHub Advisory 走 api.github.com/advisories 免 key REST（GraphQL 要 token 弃用）。v0.20 (2026-09-16) E2 能力包级 redlines 补齐落地：web/binary/crypto/forensics/misc 五份 rules/redlines.md 按轨级风格起草（**AI 起草草案待人审**，能力级跨轨纪律：web 验证最小伤害/证据反幻觉/状态机、binary 样本信任/func_kb 查重、crypto 可复现结论/敏感密钥处置、forensics 证据只读/OSINT 边界/最小化、misc 先常规后重型/反幻觉），build_rules_preamble 注入实测通过、doctor missing-redlines 告警清零；§17 E 组移除 E2 并重编号。v0.19 (2026-09-16) E1 红线设置页 UI 重构落地（§12 设置页）：RulesPane 拆至 components/settings/，改左文件列表+右单文件编辑器（●/○ 存在状态点、注入范围 Badge、字数/保存底栏、缺失新建并保存、owner hover ✕ 停用与内联新建），切文件/切轨包 dirty confirm 守卫（修未保存内容被静默覆盖），doctor 红线跳转自动选中；后端 API 与 .history 不动；§17 E 组移除 E1 并重编号。v0.18 (2026-09-16) E6+E7 资产登记改造与扫描/测试状态机落地（§5.2）：register_asset 统一登记入口（人工/Agent 同路）、类型自动识别、domain 自动 DNS 挂载与主域名/别名标记、host 按 IP 去重、bb_add_asset 防重扫回执；assets.status 四态状态机 + bb_asset_status（tested_clean 必带 note）+ asset.status_changed 审计 +「有发现」前端反查徽章 + bb_query status/type 过滤；§17 E 组移除 E6/E7 并重编号。v0.17 (2026-09-16) E8 步数预算与人工引导落地（§3）：max_steps 默认 200、剩余<20 步边界提醒、request_steps 自助加步（+200/剩余>20 拒收/审计）、耗尽自动步数暂停（快照落库可 rehydrate，不 fail）、human_note 引导通道 + 直播间「发任务｜引导会话」切换、resume 支持附引导语/追加步数；§17 E 组移除 E8 并重编号。v0.16 (2026-09-16) 章节调序：情报面板（原 §17）前移为 §16、待做清单移至末位 §17，交叉引用同步改；待做清单内容不变。v0.15 (2026-09-16) CTF 线索板 + 跨轨路标定稿（§5.2：四级线索词表/产物内联/三级注入按 IP 聚合）+ §16 E11 挂账（实施后置；起因：CTF 黑板通用性太高，MISC/取证线索串联无承载）。v0.14 (2026-09-16) 情报面板定稿（新章 §17：漏洞简报/高分文章/Obsidian 学习计划）+ §16 E9/E10 挂账（实施后置）。v0.13 (2026-09-16) 步数预算与人工引导定稿（§3）+ §16 E8 挂账（实施后置；起因：recon 任务 30 步耗尽被自动 fail 走查）。v0.12 (2026-09-16) 资产扫描/测试状态机定稿（§5.2）+ §16 E7 挂账（实施后置，可与 E6 同批）。v0.11 (2026-09-16) 资产登记改造定稿（§5.2）+ §16 E6 挂账（实施后置）。v0.10 (2026-09-15) 直播间事件流新增「路由」筛选 tab（§12）+ 全文精简（重复状态叙述并入落地状态总览，实现坑细节移交各级 CLAUDE.md）；仓库 git init。v0.9 (2026-09-15) A 组五项落地（schema v5→v6，§16 A 组移除、B–E 重编号）。v0.8 (2026-09-15) 时间本地化（§12）+ §16 重排 A–E 组。v0.7/v0.6/v0.5 (2026-09-15) G 批批 6/5/4：L0 提案模式 / L2 全自动链 / L1 开窗审批（§6.8）。v0.4 (2026-09-15) G 批批 2/3 自主配置面/记账 + tick 租约/编排状态持久化（schema v5）。v0.3 (2026-09-15) 状态约定 + 撤回传播（机制 1.6/1.5 系统侧）。v0.2 (2026-09-13) 「能力包 × 场景轨」正交分类学（§4.5）+ src-strike 全量融入（§4）+ 角色软边界/技能全提案制。v0.1 首版定稿。

## 落地状态总览（2026-09-15 代码核查）

| 模块                                                                                  | 状态       | 说明                                                                                          |
| ------------------------------------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------------- |
| Agent 循环（观察-思考-工具-黑板；暂停/恢复/中断；租约心跳）                           | ✅         | §3                                                                                            |
| 黑板存储 schema v6（事件流/资产/findings/func_kb/chains/tasks/session_inbox/orchestrator_state） | ✅         | §5；v4=用量计数，v5=编排游标/tick 租约，v6=tasks.plan + last_replan_at（A 组）                  |
| 证据并集 + 人类 PATCH 单事务（机制 1.12）                                             | ✅         | §5.3                                                                                          |
| 四态任务 + 看板运维（编辑/放回/物理删除）                                             | ✅         | §6.1/§6.4；A 组 A1 起四态皆可删、claimed 步边界急停                                             |
| conflict_keys 认领时快照交集检查 → 资源租约（1.4，schema v7）                          | ✅         | §6.7.1：X/S 租约转写 + wait_for 门控 + 释放重校验 + 环检测安全网（机制 1.4）                        |
| 发布去重 + workset 软声明（1.1，schema v7）                                           | ✅         | §6.7：dedup_fp 指纹命中复用 + workset advisory + 任务流图建议边（机制 1.1）                          |
| 协调 1.5 会话收件箱                                                                   | ◐          | 系统私信两类已落地（basis_stale 撤回/finding_update 增补，A4）+worker 注入/页签红点；Agent bb_notify/handoff/to_role 未做 |
| 协调 1.6 证伪撤回传播                                                                 | ✅         | finding.retracted + 四类私聊 + stale_refs 三选一 + 画布 FP 淡出                               |
| 协调 1.12 + 任务心跳 + packs 写锁（A 批）                                             | ✅         | 证据并集、心跳、packs 进程写锁                                                                |
| 协调 1.9 编排状态持久化 + tick 租约                                                   | ✅         | §6.7/§6.8（G 批批 3；live_sessions 仍只在内存，跨重启靠 rehydrate）                            |
| 协调 1.11 sessions_cap + 用量记账 + 80% 软警告                                        | ✅         | §6.8（G 批批 2；自主动作硬闸、人手动作仅警告）                                                 |
| 协调 1.2/1.3/1.7/1.8/1.10（1.1/1.4 已落地）                                          | ⬜         | F 批余部，§6.7/§17 B 组                                                                       |
| 自主级别 L0/L1/L2 配置面（档段/暂停/cap/双预算）                                      | ✅         | §6.8（G 批批 2 配置面；行为差异批 4/5 已落地，L0 提案批 6）                                    |
| 自主行为：L1 开窗审批（op 注册表+预检/终检，批准即建窗开跑）                           | ✅         | §6.8，G 批批 4                                                                                |
| 自主行为：L2 全自动链（事件驱动无调度器；kick/续 tick；防失控七闸；chain 事件；重启急停） | ✅         | §6.8，G 批批 5                                                                                |
| 自主行为：L0 提案模式（propose_only/orch.proposed/前端行内采纳）                       | ✅         | §6.8，G 批批 6                                                                                |
| Skill：路由/kb CRUD/备份版本/refs 联动/doctor/vocab                                   | ✅         | §4                                                                                            |
| Skill：统一变更提案制（propose_pack_edit/提案队列/review Job）                        | ✅         | §4                                                                                            |
| Runtime L0 host / L1 wsl / L2 docker + policy + 执行网关                              | ✅         | §7                                                                                            |
| Runtime L3 sandbox                                                                    | ◐          | 加固参数（无挂载/限额）+ net=none 可用；**fakenet 未实现（NotImplementedError）**、无 sidecar |
| LLM：多供应商/模型发现/运行中切换                                                     | ✅         | §8                                                                                            |
| research 轨 + rev-generic 工作台 P1/P2                                                | ✅         | §9/§12：headless v3、chains 逆向链、MCP、IDA 双向写回、脚本档                                 |
| 评估攻击链画布（relates_to + FindingsCanvas）                                         | ✅         | §12                                                                                           |
| 逆向复用 R1 全局缓存 / R2 全局函数库 / R3 签名匹配                                    | ⬜         | §9 末                                                                                         |
| WebUI 主体（项目向导/直播间/黑板双工作台/任务看板/Skill 设置/审批收件箱）+ 三栏指挥台 | ✅         | §12                                                                                           |
| WebUI 终端页（workspace 容器/shell、终端抽屉）                                        | ⬜         | §12 页面骨架第 7 项；E4 已定稿（§12「终端页与人类命令通路」），§17 13 拆批                      |
| 报告生成（黑板数据 → 渗透报告初稿）                                                   | ⬜         | §12 Open Questions（随 R2 推进）                                                              |
| development 场景轨                                                                    | ⬜         | §15（仓库已 git init）                                                                      |
| A 组多会话可观察/可急停（A1 detach/终止入口收敛/步边界急停/done 可删、A2 tasks.plan 计划闸、A3 任务流视图、A4 finding_update 私信、A5 编排规划-分派） | ✅ | §3/§6.1/§6.4/§6.7/§12，2026-09-15（schema v6）                                                |
| E12 意外终止任务可续跑（中断保留现场 + 看板「▶ 续跑」限原会话 + 中断确认弹窗） | ✅ | §3，2026-09-16                                                                                |
| E13 三栏框架可拖拽调宽（左导航/右侧黑板侧栏 react-resizable-panels v4 化 + NavRail 抽取） | ⬜ | §12 布局定稿，E13 已定稿挂账 §17 16                                                                  |
| E14 编排器任务拆解+资产分批发布+低水位补任务（parent_id 拆解深度 1/每轮 ≤5/资产视图注入/事件驱动续批） | ⬜ | §6.4 定稿，挂账 §17 17（机制 1.3 部分前置）                                                          |
| E15 任务暂停语义统一+待人工输入承接（blocked_reason 区分/续跑统一/放回附注/认领提示层注入/铃铛红点） | ⬜ | §6.1 定稿，挂账 §17 18（§3 E12 续跑扩展）                                                            |
| E16 作战模式（pentest/redteam）+ mission 战役目标+社工钓鱼/shell 管理页挂账 | ⬜ | §6.9 定稿，挂账 §17 19（§6.3 禁令 redteam ROE 内放开）                                               |
| E8 步数预算与人工引导（max_steps 200/request_steps 自助/耗尽自动暂停+快照落库/human_note 通道） | ✅ | §3，2026-09-16（无 schema 升级）                                                              |
| E6+E7 资产登记统一入口（类型自动识别/DNS 挂载/主域名别名）+ 扫描测试状态机（status 四态/bb_asset_status/有发现反查徽章） | ✅ | §5.2，2026-09-16（无 schema 升级）                                                            |
| E1 红线设置页 UI 重构（左文件列表+右单文件编辑器/dirty 守卫/doctor 跳转选中） | ✅ | §12，2026-09-16（纯前端，无 schema 升级）                                                     |
| E2 能力包级 redlines 补齐（web/binary/crypto/forensics/misc 五份草案，待人审） | ✅ | §4.5，2026-09-16（纯文档，无 schema 升级）                                                    |
| E9 情报面板 v1（config/intel 全局库/触发式抓取/classifier 打分与简报/情报页三 tab/简报卡/情报源 tab） | ✅ | §16，2026-09-16（core/intel 新模块 + 全局 DB 新模式；LLM 缺席降级规则不 503；无 schema 升级）  |
| E10 Obsidian 接入 + 学习计划（vault 只读索引/本地全文搜索/学习档案三来源/LLM 周计划只喂元数据） | ✅ | §16.3，2026-09-16（intel.db SCHEMA_VERSION=2；隐私红线=正文不出本机有测试断言；v1 无自动监听，手动重建索引）  |
| 时间本地化（本地时区 + YYYY-MM-DD HH:mm:ss 统一 formatter，纯前端 datetime.ts）       | ✅         | §12，2026-09-15 落地                                                                          |
| AI 反代 / AI 破甲专项方向（含红线）                                                   | ⬜         | §15.5，随 development 轨，红线先行进 track redlines                                           |
| malware 场景轨                                                                        | ⬜         | Phase 3，依赖 fakenet                                                                         |
| 文档债：五个能力包 `rules/redlines.md`                                                | ◐          | track 级 redlines 齐（ctf/assessment/research）；能力包级全缺                                 |

***

## 1. 项目定位

**AI 驱动的全能安全平台**：统一核心引擎 + **能力包 × 场景轨正交架构**（§4.5）——能力包（web/binary/crypto/forensics/misc，多选）提供方法论技能与知识库，场景轨（ctf/assessment/research/malware，单选）提供规则、角色、任务类型与收尾约定，覆盖 CTF 解题、授权评估、逆向/Pwn、固件/IoT/工控/车联网研究、恶意样本分析等场景；同一套方法论跨场景复用，不再按场景/平台重复维护。assessment 轨另设**作战模式**（mode=pentest|redteam，§6.9）区分渗透测试与红队行动的行为边界与目标语义。

**核心差异点：原生 Windows 支持。** 现有开源方案（PentAGI、CAI、D-CIPHER）均为 Linux/Docker 优先；本项目以 Windows 为第一公民，按能力自动分层降级到 Docker/WSL2。

**产品形态：**

- **WebUI 为主入口**（React 全功能控制台）
- **CLI 为辅助入口**，与 WebUI 操作同一个项目/黑板
- 多个 AI 会话（窗口）并行工作于同一项目，通过黑板协调

**参考项目**：D-CIPHER (nyuctf_agents)、CTF-Dojo、PentAGI、CAI/CSI、ctf-skills、HexStrike AI、VulnBot、GhostWriter。

***

## 2. 总体架构

````
webui (React SPA) ──┐
CLI ────────────────┼──→ core API (FastAPI) ←—— 唯一写入口
多个 AI 会话 ───────┘         │
                              ├─ 黑板服务: 读写 / 任务队列 / 去重 / 事件广播
                              ├─ 包解析: 能力包(多选) × 场景轨(单选) → skills/rules/roles（§4.5）
                              ├─ Agent 循环 + Skill 路由
                              ├─ Runtime 抽象层 (native-win / docker / wsl2)
                              └─ 存储: SQLite(WAL) → 可平移 Postgres
````

**原则：**

- 所有黑板写操作经 core API（单写入口），协调由服务端保证
- WebUI / CLI / AI 会话是平等的 API 消费者
- 领域无关的核心 + 能力包/场景轨扩展，加能力或加场景都不动核心（§4.5）

***

## 3. Agent 循环

**选型：单 Agent 循环 + 动态 Skill 上下文注入，当前不做多 Agent。**

````
用户出题/建任务 → 类别判定（小模型）
              → 在项目能力包×轨的 Skill 候选集内路由（注入系统提示、工具集、checklist）
              → 主循环：观察 → 思考 → 调工具 → 写黑板 → 反思
              → 卡死/多轮无进展 → 策略顾问（换思路，非换 Agent）
````

- 借鉴 D-CIPHER：区分**规划上下文**与**执行上下文**两个会话（非两个 Agent），规划会话持有全局目标，定期读执行摘要，防止上下文被工具输出淹没后丢失全局目标。
- 升级线：只要 Agent 每步读写黑板，未来拆多 Agent = 把同线程两次调用变成两个消费者（CSI blackboard 架构路线）。
- Worker 内建循环：**完成当前任务 → 查任务队列认领 → 干活**；队列空则上报 Orchestrator。
- **会话控制（2026-09-13 定稿；2026-09-15 任务流设计修订命名）**：暂停/恢复/中断三操作，检查点一律落在 LLM 步之间（单步内不可打断，按下后等当前步返回）。

  - **软暂停（软中断）**：当前步做完 → 保存 messages 快照 → worker 正常退出（job done）→ 状态 paused；恢复 = 新 agent-work job 从快照续跑当前任务（含步数计数），不领新任务。不在 worker 线程内阻塞等待——暂停态可正常关页签（detach）/结束会话、UI 无悬挂 job。
  - **硬中断**：当前步做完 → 任务 fail（note=人工中断，**不回队列**）→ 会话空闲；审计落 `session.aborted` 事件。
  - 快照失效（租约被回收/他人持有）→ 丢弃快照走正常认领；结束会话时快照任务仍 claimed → 自动 fail（防租约占坑）。

- **关页签 ≠ 终止（2026-09-15 任务流设计修订，A 组 A1 已落地）**：

  - **关页签（页签 ×，detach）**：仅收起页签，会话 `status` 不变、任务后台继续执行；可从直播间任务流视图（§12）双击对应任务节点重新挂回页签。关页签不是任何任务的生命周期事件。
  - **结束会话（显式入口）**：会话控制组中的"结束会话"＝旧版"关窗"语义（`sessions.status='closed'` + `session.closed`，见 §6.4）。结束前若有执行中/暂停任务，须先走软中断或硬中断；未处理的 claimed 快照任务自动 fail（防租约占坑）。

- **终止任务的入口只有两处（任务流设计收敛，A 组 A1 已落地）**：

  1. **会话窗口内**（直播间点击对应窗口后，会话控制组）：软中断（暂停，可恢复）/ 硬中断（任务 fail 不回队列）；
  2. **任务看板**：执行中（claimed）卡片上的「取消」＝硬中断——worker 在步边界 `_control_point` 立即响应（机制 1.7 的单任务先遣版，见 §6.4/§6.7），不再等 complete/fail 才感知。

  关页签、切视图、关浏览器均不在此列。

  **E12：中断保留现场（2026-09-16 已落地）**——硬中断的**落盘快照不再销毁**（内存态照清）：`task.failed` 事件带 `resumable: true`，看板 failed 卡出「▶ 续跑」（= reopen + 原会话 `revive_snapshot` 载快照 + 提交 worker，budget 快照缺省 +200，**限原会话**——closed 会话不可 rehydrate，故结束会话时快照显式清理）；两处中断按钮加确认弹窗；任务被删除触发的中断仍清快照防孤儿。服务重启导致的中断路径（running 无快照）后置。

  **E15 扩展（2026-09-16 定稿，〔未实施〕§17 18）**——Agent 自主 `fail_task` 携带 `blocked_reason=awaiting_human` 时同样保留落盘快照、看板同显「▶ 续跑」，与人工中断同路径同端点；语义与承接面定稿见 §6.1「任务暂停语义统一与待人工输入承接」。

**步数预算与人工引导（2026-09-16 定稿并〔已实施〕，原 §17 E8）**：

- **max_steps 默认 30→200**（开窗 / 角色 yaml / 项目 config 可调，API 已支持 per-session 覆盖，开窗 UI 补暴露）。
- **步数感知**：剩余 <20 步起，每步边界注入「预算剩余 N 步，请规划收尾或申请增补」——模型对预算无感是步数耗尽事故（2026-09-16 走查）的第一根因。
- **自助加步 `request_steps`**：AI 可为自己申请步数，**一次固定 +200**；**剩余 >20 步时服务端拒收**（防未雨绸缪囤步数），≤20 步才放行；每次增补落 `step.budget_extended` 审计事件。
- **步数耗尽 = 自动步数暂停**（复用软暂停设施）：保存 messages 快照 + 任务保持 claimed + 心跳继续，落「⏸ 步数预算用尽」事件，**不再自动 fail**（原 `_finalize` 自动 fail 仅保留给显式结束会话场景）；直播间会话控制组出「继续」按钮 → 提交 agent-work job 从快照+步数断点恢复，恢复时可附引导语/追加预算。
- **暂停快照落库**（修"纯内存不恢复"已知缺口）：快照持久化到 workspace 文件、`sessions.meta` 存指针，服务重启 rehydrate 后仍可继续。
- **人工引导通道**：session_inbox 新 kind=`human_note`（author=human，新增 POST 端点），worker 步边界 drain 注入 user 消息「💬 人类引导：…」（不打断当前工具调用）；步数暂停恢复时随快照注入；直播间输入框加「发任务｜引导会话」模式切换（选中页签时引导直达该会话），页签红点/已读/事件流审计全复用。

  **实施注记（2026-09-16）**：①`_loop` 改 while 循环、每轮实时读预算上界——耗尽断点恢复时不增补预算也有一轮对话窗口，模型可当场 `request_steps` 自救（range 预计算会截断自救）；②resume 端点对 reason=budget 的快照**缺省自动 +200**（`extra_steps` 可覆盖，0=不加），均落 `step.budget_extended`（by=human）；③快照路径 `<workspace>/<pid>/snapshots/<sid>.json`，`sessions.meta` 存 `resume_snapshot` 指针，rehydrate 命中即回 paused，续跑/中断消费后清快照；④`request_steps` 入控制原语集（不受角色白名单与计划闸限制）；⑤L2 自动续跑后置，见 §17 E 组。


***

## 4. Skill 体系

Skill = **目录约定**（非代码），同时服务 Agent（自然语言）、路由器（元数据）、程序（代码）：

````
packs/capabilities/<cap>/skills/<skill-name>/   # 能力技能（方法论，可跨轨复用）
packs/tracks/<track>/skills/<skill-name>/       # 轨级技能（场景流程，如 ctf 分诊）
├─ SKILL.md          # 给 Agent 读：适用条件、步骤 checklist、常见坑（frontmatter 含触发条件与标签）
├─ helpers/          # 可复用 Python 片段（如 pwntools 模板）
└─ examples/         # 成功轨迹样本（Few-shot 注入）
````

**分层优先级链（2026-09-12 实战校准，源自 src-strike；2026-09-13 路径随正交模型更新）**：

````
rules（红线，永久注入系统提示） > SKILL.md（入口路由） > kb 知识库（方法论，命中才开） > references（弹药库，只给路径按需 Read，禁止通读）
````

- 红线来源 = 项目启用的各能力包 `packs/capabilities/<cap>/rules/*.md` ∪ 场景轨 `packs/tracks/<track>/rules/*.md`，构建系统提示时全量注入；与一切 skill 冲突时以 rules 为准。
- `packs/tracks/<track>/rules/owners/<tag>.md`：按资产 owner 叠加的更严规则（EDUSRC/OSRC/YSRC 模式：资产带标签 → 对应规则生效压过通用默认）。**owner 规则可管理**：文件即规则——删除文件 = 停用该平台规则、新建文件 = 扩展新平台；src-strike 四套（edusrc/edu-rating/ysrc/osrc）完整版作为初始内容预置，设置页（红线 tab）可建/改/删（.history 留备份）。**数据通路（2026-09-13 定稿，AI 自动打标）**：Agent `bb_add_asset` 时按资产特征打 `meta.owner`（`.edu.cn` 系 → `edusrc`；品牌/平台标注 → 对应 tag；无归属不打，纪律在技能正文不在工具层）→ 开窗时 `Blackboard.owner_tags(pid)` 收集去重 → `AgentConfig.owner_tags` → `build_rules_preamble` 注入。注入天然可选：没资产打标就一个 owner 规则都不进提示。
- `packs/tracks/<track>/rules/role-rules/<role>.md`：**角色专属红线**（2026-09-13 定稿），仅当会话绑定该角色时注入（§6.6）。
- `packs/capabilities/<cap>/kb_sources.json`：**包内知识库源登记**（取代旧的包外 `knowledge_sources.json`）。每个源 `{id, root（包内相对路径）, recursive}`；开窗时合并项目启用的全部能力包源。**知识库模块打开走** **`kb_open`** **kb_open** **工具**：入参 `source` + 模块相对路径 → 服务端 resolve 后强制校验落点在该源 root 内（防穿越）→ 支持递归子目录（playbooks/poc 等深层模块全部可达）→ 模块不存在时返回该源可选清单改选（防幻觉模块名）→ 返回绝对路径 + 纪律（只 Read 该文件禁通读目录）+ `kb.open` 审计事件。谁打到什么开什么（exploit 开 xss-test、recon 开 recon-methodology），不按角色预配。
- `packs/capabilities/<cap>/kb/`：**知识基库区**（ctf-skills 英文专题、src-strike 快照等）。该区内容不被 SkillRegistry 扫描（不进路由表）；由包内自写的**中文薄路由技能**以「特征 → kb 相对路径」对照表指向其中文件。

  - **本地基线可改**：快照是平台自洽的起点而非只读文物，设置页 kb 浏览器与 AI 提案通道（见下）均可增改。两条纪律：**英文快照原文不翻译**（翻译即污染上游语义）、**新增经验写新 md，不覆盖原文**（快照原文保持可与上游对照，本地增量另成文）；改名/删除有引用扫描联动（refs 扫描器），删除有引用默认 409。维护策略＝偶尔人工合并上游精华，不持续同步、不回灌。


**触发条件与评分**：关键词匹配 + **目标特征**（has_upload / has_search / returns_401…）+ **文件特征**（`ELF 64-bit + NX` → pwn 类 skill）+ **分类标签**（frontmatter 的 platforms / formats / vuln_classes，见 §4.5）。评分：特征与标签 ×3 > 关键词 ×2 > 描述 ×1。frontmatter `enabled: false` 的技能不参与路由（设置页可启停）。

**src-strike / ctf-skills 基线融入**：

- ctf-skills（MIT，10 技能约 126 篇英文专题）**快照原样融入**：保留 LICENSE 与英文原文，按 web←ctf-web、binary←ctf-pwn/ctf-reverse/ctf-malware、crypto←ctf-crypto、forensics←ctf-forensics/ctf-osint、misc←ctf-misc/ctf-ai-ml 映射进各能力包 `kb/`；solve-challenge 移植为 ctf 轨分诊技能。不追上游、不翻译（Agent 读英文技术文档无障碍），中文索引层由薄路由技能承担。
- src-strike **全量快照融入** `capabilities/web/kb/src-strike/`（49 模块知识库 + references + poc + 方法论规则，原结构保留）；平台合规四套完整版 → `tracks/assessment/rules/owners/`；红线摘编 → 轨 redlines；target/ 实战数据、.git、mcp-servers、tools 不融入。
- 工具类（fofa/playwright MCP）登记进 config/mcp.json（运行时工具桥〔未实施〕；当前 MCP 仅逆向 IDA 桥，见 §9）。

**技能/知识的自我迭代——统一变更提案制**：AI 永不直写技能/kb 文件。Agent 工具统一为 `propose_pack_edit(kind: skill|kb, target, mode: edit|create|rename|delete, content?, new_path?, summary, reason)` → 服务端模拟校验（frontmatter name 一致、标签合法、路径/引用合法）→ 写 `packs/.proposals/pp_<UTCts>_<6hex>.json`，**正式文件不变**。提案**不存旧内容快照**，审批时按磁盘现状实时计算 diff——审阅永远对得上现状，没有"快照已过期"的错位。WebUI 设置页「提案」队列：批准（先 .history 备份再应用，rename 走引用联动）/ 拒绝 / 改后采纳 / 退回修订；apply 仅限人类（`decided_by=human`；本地 demo 自批显式 `decided_by="demo-script(auto)"`，不冒充人类）。**skill 只允许 edit 提案（AI 不许新建/删除技能）；kb 四模式（edit/create/rename/delete）全开**。另有会话「复盘沉淀」入口：后台 planner 复盘任务后把验证有效的手法逐条落成 kb 提案（review-proposals Job，给文件清单防猜路径），每会话限额 3 条。本质是为人服务：AI 只负责发现和草拟，审阅与决定权在人。

***

## 4.5 能力包 × 场景轨（正交分类学，2026-09-13 v0.2 定稿）

### 4.5.1 要解决的问题

CTF（MISC/PWN/REV/CRYPTO/FORENSIC/流量/IoT/工控）、逆向工程（Windows/Android/Linux/iOS、病毒分析、软件破解、反调试）、二进制漏洞（堆/栈/内核/服务/驱动）三类内容高度重叠：栈溢出方法论在 CTF-pwn、漏洞研究、恶意样本漏洞利用里是同一份。平铺领域包（ctf/reverse/pentest/redteam/iot…）会让同一方法论按"场景 × 平台 × 漏洞类"组合复制 N 份，维护成本组合爆炸。

### 4.5.2 正交模型

````
项目 = 能力包(capabilities，多选) × 场景轨(track，单选)
````

- **能力包**回答"**怎么干**"：拥有方法论技能（skills/）、知识库（kb/）、能力级红线（rules/）。可多选——CTF 全能赛勾选 web+binary+crypto+forensics+misc。
- **场景轨**回答"**在什么语境下干、产出什么**"：只含角色（roles/）、轨级流程技能、任务类型注册表（task_types.yaml）、规则（红线/owners/role-rules）、收尾产物约定。单选。

| 能力包      | 内容                                                                      | 第一代知识来源                      |
| ----------- | ------------------------------------------------------------------------- | ----------------------------------- |
| `web`       | Web 攻防方法论、侦察、打点                                                | src-strike 全量快照 + ctf-web       |
| `binary`    | pwn、逆向、恶意样本静态、固件/设备；IoT/工控/车联网专有知识为其 kb 子模块 | ctf-pwn + ctf-reverse + ctf-malware |
| `crypto`    | 密码攻击与编码                                                            | ctf-crypto                          |
| `forensics` | 取证、流量分析、OSINT                                                     | ctf-forensics + ctf-osint           |
| `misc`      | 杂项、AI/ML 题（规模长大后再拆包）                                        | ctf-misc + ctf-ai-ml                |

| 场景轨                         | 收尾产物                            | 角色强度                                            | 关键约束                                           |
| ------------------------------ | ----------------------------------- | --------------------------------------------------- | -------------------------------------------------- |
| `ctf`                          | flag / writeup                      | 弱角色（一题一会话，分诊入口）                      | 只打授权靶机                                       |
| `assessment`                   | 漏洞报告（verified 证据链；redteam 模式为战果链报告，§6.9） | 强角色：recon/external-entry/privesc/lateral        | owner 规则、噪声预算、授权边界；作战模式 mode=pentest\|redteam（§6.9） |
| `research`                     | 研究笔记/func_kb 结论/PoC/0day 报告 | 逆向分析角色（reverse-analyst；rev-generic 工作台） | 单样本深度理解语境；headless 只解析不执行          |
| `malware`（〔未实施〕Phase 3） | IOC/行为报告                        | 静态/动态角色强制分离                               | L3 沙箱 + fakenet **硬强制**（§7，不可被审批放松） |

> 内网/协议类（smb-enum、ad-mapping 等）随 assessment 轨深化后再评估是否拆 `network` 能力包。`research` 轨已落地（任务类型 triage/reverse/analyze/verify，全 passive；工作台 profile 为 `rev-generic`，§4.5.5、§12）；`malware` 轨待 fakenet 配套，〔未实施〕。

### 4.5.3 平台/格式/漏洞类 = frontmatter 标签，不建包

"Windows 逆向""Android 逆向""堆漏洞"等差异用技能 frontmatter 标签表达，路由器按标签加权（与 features 同档 ×3），不为每种组合建包：

```yaml
platforms:    [windows, linux, android, ios, firmware, rtos, plc, can]   # 可扩展
formats:      [elf, pe, mach-o, dex, firmware-image]
vuln_classes: [stack, heap, kernel, driver, service, uaf, race]
```

IoT/工控/车联网的**专有内容**（固件解包、Modbus/S7、CAN、厂商知识）= `binary/kb/` 下的知识模块（iot/、ics/、vehicular/），由薄路由技能按需 kb_open；通用逆向/pwn 方法论不复制。

### 4.5.4 物理布局

````
packs/
├─ capabilities/
│  ├─ web/        # pack.yaml + skills/ + kb/{src-strike/,ctf-web/} + kb_sources.json（rules/ 设计预留，当前缺）
│  ├─ binary/     # skills/{file-triage,binary-rev,binary-pwn} + kb/{ctf-pwn,ctf-reverse,ctf-malware,iot,ics,vehicular}/
│  ├─ crypto/     # skills/crypto-triage + kb/ctf-crypto/
│  ├─ forensics/  # skills/forensics-triage + kb/{ctf-forensics,ctf-osint}/
│  └─ misc/       # pack.yaml + kb/{ctf-misc,ctf-ai-ml}/（技能〔未实施〕）
├─ tracks/
│  ├─ ctf/        # track.yaml + roles/ + task_types.yaml + rules/redlines.md + skills/triage
│  ├─ assessment/ # track.yaml + roles/ + task_types.yaml + rules/{redlines.md,owners/,role-rules/}
│  └─ research/   # track.yaml + roles/(reverse-analyst) + task_types.yaml + rules/redlines.md
├─ .proposals/    # 统一变更提案（§4）
└─ CLAUDE.md
````

> 五个能力包的 `rules/redlines.md` 已于 2026-09-16 补齐（E2，AI 起草**草案待人审**：web 验证/证据/状态机纪律、binary 信任与执行/数据证据、crypto 结论与敏感数据、forensics 证据保全/OSINT 边界、misc 方法与证据）；doctor 不再告警。内容定稿由人审修订。

- 每个能力包 `pack.yaml`：`{kind: capability, name, label(中文显示名), description}`；场景轨 `track.yaml` 同构（`kind: track`）。
- `rules/*.md` 的 glob 不递归（core/skills/rules.py 现状），故 `owners/`、`role-rules/` 子目录天然不进自动注入，由各自的选择器按需加载。
- SkillRegistry 同时扫描 `capabilities/*/skills/*/SKILL.md` 与 `tracks/*/skills/*/SKILL.md`，SkillMeta 记录来源（capability 或 track）。
- kb/ 下任何文件不进注册表（ctf-skills 自带 SKILL.md 也不例外——它们是被 kb_open 打开的资料，不是路由技能）。

### 4.5.5 项目绑定与兼容

- project.json 新建项目写 `track: <slug>` + `capabilities: [...]`；路由候选集 = 启用能力包的全部技能 ∪ 该轨轨级技能，再经角色白名单窄化。
- **旧数据读兼容**：只有 `domain` 字段的旧 project.json 读取时映射——`pentest → track=assessment, capabilities=[web]`；`ctf → track=ctf, capabilities=[binary]`；`reverse → track=research, capabilities=[binary]`（re1 类旧项目重开自动归位，不改 project.json）。不做不可逆迁移；新建一律写新字段。
- 任务类型归属场景轨：`tracks/<track>/task_types.yaml` 是该轨合法 task_type 的**注册表**（含默认噪声预算）；publish 时未知类型拒收（人类/Orchestrator/Agent 同一校验），根治 task_type 拼错静默饿死；角色 yaml 的 task_types 必须是注册表子集（pack doctor 检查）。
- **工作台 profile**：轨×包决定技能/知识，**工作台形态**由正交的第三维 profile 决定，存 `project.config.workbench.profile`。缺省推导：`track=research && capabilities 含 binary ⇒ rev-generic`。逆向内部子类（逆向分析/病毒分析/逆向破解/游戏外挂/逆向开发）以后**只加一份前端 profile 声明 + 字典**，后端通用 API 不分叉；共享内核 = headless 缓存 + func_kb + findings + 同一套端点。

***

## 5. 黑板系统（schema v6）

### 5.1 定位

**项目级多会话知识库**，非单任务记事本：

````
Project（项目）= 一块持久化黑板 + 若干 Session
  例：渗透测试项目（数百域名）→ Session1 Web 主攻 / Session2 子域枚举 / Session3 内网
  例：逆向项目（同一二进制，多目标）→ Session1 挖二进制漏洞 / Session2 协议分析
  黑板随项目持久存在，Session 结束后知识继续积累。
````

### 5.2 数据模型

````
黑板 = 事件流(events) + 知识层 + 协调层 + 审计（每条写入带 author）

events       时序流：命令、输出、Agent 决策（增量通知的来源）
sessions     会话登记（身份、能力、状态）

── 知识层 ──
assets       多态资产表（domain → host → service → url / 二进制 / 函数），支持 claimed 状态；
             树层级经 parent_id 落地（2026-09-13）：pentest 纪律 = DNS 解析出 IP 先建 host 资产，
             域名/服务/URL 挂其下（定稿层级 host → service → url）；WebUI 仅 pentest 域启用
             树视图（无 parent 的旧数据自然退化平铺）。
             资产更新走 `set_asset_parent`（防环/防自挂）与 `update_asset_meta`（merge），
             HTTP 面为 `PATCH /api/assets/{aid}`——upsert 去重键含 parent_id，补挂/改 meta
             必须走更新方法而非重复 upsert。
             自动挂载：bb_add_asset 对 url/service 值解析出 IP 主机部时自动建 host 并挂载
             （域名不猜 DNS，靠技能纪律手动挂）。
             meta 约定：`title`=一句话简述（页面 <title> 等，WebUI 第二行展示）、
             `scanned=true` 已废弃（E7 状态机取代，旧值前端映射「已访问」）、
             `primary_domain`/`alias`=同 IP 主域名/别名标记（E6）
             **资产登记改造（2026-09-16 定稿并〔已实施〕，原 §17 E6）**：
             ① 统一登记入口——新建 core/blackboard/assets.py register_asset，抽取
             bb_add_asset 的 find_asset 合并/自动挂载路径共用，人工（POST /assets）与
             Agent 同一入口（修人工路径同值插重复行）；② 类型自动识别——url/IPv4/
             host:port/完整域名/64hex，识别不出拦截提示手选（WebUI 类型框由硬编码
             binary Input 改下拉：自动/host/domain/service/url/binary 按能力包）；
             ③ domain 添加时后端自动 DNS 解析（getaddrinfo 首个 IPv4）建/复用 host
             挂载，解析失败存独立行（Agent 侧「不猜 DNS」纪律不变）；④ 去重以 IP 为主
             ——host 按 IP 去重，域名完整保留不降级、按完整域名串去重；⑤ 同 IP 多域名
             = host+主域名（host.meta.primary_domain，首个或人工指定）+其余标别名，
             AI 扫描目标粒度按 host+主域名，经现有 PATCH /assets/{aid} 写 meta 不加端点；
             ⑥ AI 防重扫——bb_add_asset 回执命中已有 IP 追加「先 bb_query 查重」提示 +
             web 技能（web-strike-entry/recon-asset-enum）补同 IP 查重纪律。无 schema 升级。
             **实施注记**：register_asset 落在 `core/blackboard/assets.py`（含
             detect_type/resolve_ipv4），测试对 DNS 一律 monkeypatch 断网 hermetic。
             **扫描/测试状态机（2026-09-16 定稿并〔已实施〕，原 §17 E7）**：
             复活 assets.status 死列（无 schema 升级），白名单四态 open（未触碰，默认）/
             visited（已访问）/ scanning（正在扫描）/ tested_clean（已测试·无发现），
             非法值拒收。AI 经新专用工具 `bb_asset_status(asset_id, status, note?)` 流转
             ——**tested_clean 必带 note**（测了什么/怎么测，服务端强制），每次流转落
             `asset.status_changed` 审计事件；技能纪律改写（recon-asset-enum）：
             **访问≠测试**，访问后 visited、开测 scanning、测完才许 tested_clean。
             「已测试·有发现」不由 AI 标：资产挂 verified finding 即由前端反查显
             「有发现」徽章（标 tested_clean 后出 verified finding 自动翻红，结论以
             findings 为准）。bb_query what=assets 增 status/type 过滤并返回 status
             （并发会话可感知"哪些目标正被扫"）。旧 meta.scanned=true 前端映射为
             「已访问」，WebUI「已扫」徽章由四态徽章+有发现徽章取代。
             **实施注记**：set_asset_status 在 store.py（同状态 no-op 不发事件）；
             前端徽章组件 AssetBadges（Blackboard.tsx，树/平铺共用）。
findings     发现：挂任意资产节点（尽量挂，target_asset_id 是按资产筛选的数据基础）；
             severity(info~critical)、evidence、poc 产物引用（poc_artifact_id）、
             verified 状态（未验证/已验证分管）、去重指纹 (target, vuln_class, 参数指纹)。
             **verified 的定义 = 稳定复现**（2026-09-13 定稿）：连续 3 次请求全部触发才算稳；
             evidence.poc 约定 = {type: "http_raw"|"python"|"steps", http_raw?: 报文原文,
             artifact_id?: 产物 id, target: url, stability: "3/3"}——报文进黑板（可检索
             可展示），Python 脚本进 artifacts（kind=poc，sha256 防篡改）引 artifact_id。
             两条形态纪律（2026-09-13 补定稿）：**http_raw = 可复现的最小报文**（请求行 +
             触发必需的头/体，去掉浏览器噪音头——最小化才是可复现的判据）；**脚本 POC
             仅限 Python**（bb_add_artifact kind=poc 强制 .py，工具层拒绝其他语言）。
             **同一发现允许多条 POC**（不同触发路径/报文位置）：evidence.pocs = 数组
             逐条独立 stability；旧单条 evidence.poc 与 findings.poc_artifact_id 保留
             兼容（视为主 POC），不改库表。
             **逆向锚定（2026-09-14）**：逆向发现挂 binary 资产（target_asset_id），
             evidence 必须带 `func_id` + `address` 定位；类别词表五类
             `category ∈ {algorithm, protocol, data-structure, mechanism, risk}`。
             verified 语义扩展：人工确认（confirm_by=human）或**动态验证**
             （confirm_by=dynamic + `debug_log_artifact_id` 指向 debug-log 产物）；
             无证据一律 unverified。
             **CTF 线索板与跨轨路标定稿（2026-09-16，〔未实施〕§17 E11）**：
             复用 findings 换词表（无 schema 升级），按轨映射语义——**CTF**：
             severity → **线索级别四级**（关键突破🔴/有效线索🟠/背景信息⚪/死路⚫），
             vuln_class → 线索类别（信息点/隐写疑似/编码疑似/flag 候选/…），
             status=false-positive 复用为死路（画布淡出设施顺带）；
             **assessment 的 severity 语义不动**。
             **中间产物归 artifact**（纪律：提炼后有效才落库、必附一句"是什么/
             怎么得到"——防垃圾泛滥）；线索卡**内联产物附件**（图片缩略图/脚本
             代码块可展开，复用 POC 弹窗）——复现必要的产物直观可见，不用翻目录。
             **收尾纪律**（修三处割裂）：解题/复现脚本必须落 artifact、writeup 落
             artifact（不再写裸文件）、flag 走 finding 候选→确认。
             **线索串联**：relates_to 卡片链（强边校验/撤回联动复用），不做独立
             图谱视图；线索板按**题目**组织，线索卡以类别标注来源方向（单题跨
             web/rev/misc 为自然状态）。
             **跨轨统一路标**（防 AI 重走/幻觉）：bb_add_finding 增加路标记录
             （死路/已排除方向，带原因 + 已尝试清单）——ctf=死路线索、渗透=已
             排除攻击路径、逆向=已排除假设；路标挂 target_asset_id。
             **三级注入按 IP 聚合**（开局/认领时注入 AI 上下文）：① scope 精确
             匹配的路标全文注入 → ② 同 IP/同主机聚合一行动态摘要（**跨端口可见、
             未覆盖端口显形**——防漏测靠 IP 聚合而非按业务切窄）→ ③ 项目级计数
             + bb_query 查询纪律改写；IP 聚合按资产父链归并。
             **前端融入现有列表**：CTF 发现 tab 渲染线索卡流（级别分色、死路
             默认折叠），各轨发现列表统一加级别筛选，不加新视图。
             **补缺**：CTF 样本上传 UI 入口（samples API 存在但无控件）、远程
             靶机（nc host/port）落 host/service 资产纪律。
func_kb      函数知识库（逆向）：binary哈希+address 主键；名称演变史、分析结论、
             risk_tags、confidence、analyzed_by；防重复分析的主力。
             **三层数据纪律（2026-09-14 定稿，不可混淆）**：
             ① headless 缓存 JSON（artifacts/decompiler-cache/<sha256>.json，v3 契约 §9）
             = 全量**客观**函数（名/地址/伪码/calls/sections/imports），工具产出可重导，
             不进 SQLite；② func_kb 只存**分析过的**函数（人的理解/改名/risk_tags，
             人机共写：改名入 name_history、笔记以「## 笔记 时间 by human」分段追加、
             risk_tags 全量替换、confidence 是 AI 字段人不动）；③ findings 挂 binary 资产。
             反编译前先查 func_kb；伪码不进事件流。
chains       攻击链图（P2 人工建链）：节点必须引用**本项目**既有
             finding/func_kb/artifact（store 层双校验：链归属 + 节点 project_id，
             防幻觉防跨项目拼链）；链为线性序（seq 自动追加，删中间节点后重排 1..n）；
             edge_note 记「这条边为什么成立」；status: hypothesis | validated | exploited；
             实体删除后链不报错，节点以 deleted:true 孤儿占位。当前仅人工建链，Agent 建链未开放。
artifacts    产物库：POC、二进制、抓包、反编译输出；路径+描述+哈希；POC 是一等公民；
             Agent 落产物走 bb_add_artifact 工具（写 <artifacts_dir>/<kind>/ + sha256 落库）。
             kind=debug-log（2026-09-14）：x64dbg 人工动态日志回流，是 finding
             confirm_by=dynamic 的凭据；上传限 16MB、读取放宽 4MB（其余文本产物 256KB）。

── 协调层 ──
tasks        任务队列（见 §6）；context_refs=任务依据的 finding id（显式 refs ∪ 正文
             find-id 自动抽取）、stale_refs=被推翻待自评依据（open/claimed 挂标，
             done/failed 冻结留审计）；plan=子代理执行计划步骤数组
             [{id,title,status: todo|doing|done|blocked,ts}]（A 组 A2，schema v6），
             任务流节点进度即由此渲染（§6.1/§12）
session_inbox 会话私信箱（§6.7 的 1.5/1.6）：当前**只有系统写**（kind='basis_stale'
             撤回送达；kind='finding_update' 引用 finding 实质更新通知，A 组 A4），
             部分唯一索引保证未读 (to_session,ref_id,kind) 去重、读后可再投递；
             to_session 不设外键（会话结束后私信留审计）。
             与审批收件箱（approvals 表）严格分设：知会 vs 待决
orchestrator_state
             每项目一行的编排状态/用量计数（v4 批 2 建表、v5 批 3 扩列，幂等 ALTER）：
             用量 tokens_in/out/cache_read/cache_creation/llm_calls/tasks_published/
             budget_warned（§6.8）；编排 event_cursor/cycles/last_digest_cycle
             （tick 开头装载、结尾落盘，重启后续跑）；chain_active/chain_ticks/
             last_auto_tick_at/auto_ticks_total（L2 自动链列先存后用，批 5 消费）；
             tick_owner/tick_lease_until（tick 租约，TTL 900s + 步间心跳，1.9/§6.8）；
             last_replan_at（A 组 A5 优先级重排 30s 去抖计时，schema v6）。
````

### 5.3 存储与协调机制

- **SQLite + WAL**，所有写经 core API 单入口；规模需要时平移 Postgres，接口不变。
- **认领/去重**：任务级认领（§6）；finding/func_kb 按指纹去重，重复发现合并并累加置信度。
- **证据并集（已落地）**：重复 finding 合并走 key 级并集（旧实现曾在合并分支整体丢弃新证据，已修复）——列表键 `pocs/requests/relates_to/screenshots` 按内容指纹去重追加（`relates_to` 指纹 = finding_id+note）；`notes` 署名分段追加；其余键空缺补入、不覆盖前人结论；severity 就高不就低、status verified 只升不降、confidence 取 MAX、poc_artifact_id 缺者回填。人类 PATCH（patch_finding）保持**浅层 merge**（键级覆盖），与机器上报的合并语义有意区分；三个人类 PATCH（finding/func/asset meta）的读-合并-写均在单个 `_tx()`（BEGIN IMMEDIATE）内，并发不丢更新。
- **finding/asset 物理删除（2026-09-15 落地）**：误报走 PATCH false-positive（触发撤回传播），**删除只用于垃圾/走查数据清理**（`DELETE /projects/{pid}/findings/{id}`、`DELETE /projects/{pid}/assets/{id}`）。删 finding 同事务级联：摘除其他 finding 的反向 `relates_to` 边（含历史悬空边）、摘除任务 `context_refs/stale_refs` 引用（**只摘引用不删任务**）；chain_links 不级联（链详情既有孤儿 `deleted:true` 占位语义）、session_inbox 私信保留（payload 标题快照自包含，同「关窗私信留审计」）、poc 产物不删（可复用），落 `finding.deleted` 审计事件。删资产限**叶子**且无 finding 引用，否则 409，落 `asset.deleted`。
- **变更通知**：Session 经 WebSocket 订阅黑板事件；Agent 周期性"看黑板"步骤消费增量摘要（"自你上次观察：session-2 在 xxx.com 确认 SQL 注入（high）"）。
- **读全局开放**：任何会话可读全部黑板内容（共享情报）；写权限由任务认领决定。

**项目工作区布局（定稿 2026-09-12）**——每项目一个 SQLite 库，项目 = 一个文件夹：

````
workspaces/<slug>/            # slug = 英文短名（容器挂载友好，中文/空格路径易踩坑）
├─ project.json               # 元数据：id、原项目名（可中文，WebUI 显示用）、track + capabilities（§4.5，旧库可能是 domain，读取时映射）、created_at、config
├─ blackboard.db (+wal/-shm)  # 该项目全部黑板数据
├─ samples/                   # 原始样本/附件（人放，untrusted 输入，只读语义；headless 只解析不执行）
├─ artifacts/                 # Agent 产物：脚本、提取数据、POC、截图（Agent 生成）
│  ├─ decompiler-cache/       # headless v3 全量客观导出 <sha256>.json（可删可重导，不进库）
│  ├─ decompiler-db/          # IDA 数据库 <sha256>.i64/.idb（不放 samples/：避开文件锁与 untrusted 语义）
│  ├─ .ghidra-tmp/            # Ghidra 临时工程（按调用隔离子目录，用完即删）
│  └─ debug-log/              # x64dbg 等动态日志回流（finding verified 凭据）
└─ logs/                      # 会话原始日志（可选；事件流已落库）

workspaces/.trash/<slug>-<UTC时间戳>/   # 回收站：删除的项目整目录移入
````

- **归档/备份 = 操作一个文件夹**；不建全局注册表（避免双真相源），项目列表 = 扫描 `workspaces/*/project.json`（忽略 `.trash`）。
- **删除 = 回收站式（定稿 2026-09-13）**：整目录 rename 到 `.trash/<slug>-<UTC时间戳>/`，不物理删除；运行中的项目拒绝删除（409：有 running job 或 claimed 任务）；恢复 = 手动把 `.trash/<目录>/` 移回 `workspaces/<slug>/`（slug 被占用需先改名），清空 `.trash/` 才真正消失。UI 提供确认对话框。
- `samples/` 与 `artifacts/` 分开：信任级不同（untrusted 输入 vs Agent 生成），配额/审计策略不同。
- project.json 的 id 与 blackboard.db projects 表同 id，由 core/projects.py 统一生成。
- 默认根 `workspaces/`（可配置）；`workspaces/` 整体 gitignore。

### 5.4 场景视图（View）

同一底层存储，按场景轨渲染/查询（能力包只决定技能/知识，不改黑板结构）：

| 场景轨 (profile)           | 视图重点                                                                                                          |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| assessment                 | 资产树 + 漏洞表格（列表｜**攻击链画布**子视图，§12）+ POC 附件 + 复验状态                                         |
| ctf                        | 简化线索板（题解、线索、工具输出；E11 定稿：线索卡流+四级词表，见 §5.2）                                          |
| research / **rev-generic** | 样本条 + 三栏逆向工作台（函数浏览器｜结论+伪码｜xref/发现/笔记）+ 384px 挂机侧栏；覆盖率/风险数/工具三态灯（§12） |
| malware（〔未实施〕）      | 在 rev-generic 内核上加 IOC/行为视图 + 静态/动态强制分离                                                          |

**目标即筛选条件**：同一二进制，web 挖洞会话优先拉 web 类链/POC，二进制会话拉 memory-corruption 类链——底层数据同一份。

***

## 6. 任务与多会话协调

### 6.1 任务模型（一等公民）

````
tasks
  id, scope(asset引用，可空=纯分析任务), objective
  status:    open | claimed | done | failed（定稿 2026-09-13：不要的任务物理删除，不再产生 cancelled/blocked；
             历史库可能残留 cancelled 行；一切"等待/门控"语义都在 open 行的新列上表达，见 §6.7，永不开第五态）
  priority:  0~9（整数，小者优先，0 最急；claim ORDER BY priority, created_at）
  claimed_by + lease_until（TTL，可续期；过期回收防会话挂死）
  parent:    父任务（子任务链）
  created_by: orchestrator | session-x | human
  noise_budget: passive | low | medium | high
  context_refs: 依据的 finding id 列表（发布时显式 refs ∪ objective 正文 find-id 自动抽取）
  stale_refs:  已被推翻（false-positive）待执行者自评的依据；open/claimed 可挂，
               done/failed 冻结留审计（见 §6.7 的 1.6）
  plan:        执行计划步骤 [{id,title,status: todo|doing|done|blocked,ts}]——
               子代理认领后先规划再动手（见下；A 组 A2，schema v6，服务端发号 p1..）；
               子代理 publish_task 的 parent_id=当前任务、created_by=会话 id 由服务端钉死（A 组 A5）
````

任务发布三来源：Orchestrator 自动派生（侦察出新子域名→生成枚举任务）、AI 会话（发现值得跟进的东西→生成任务）、人类（WebUI/CLI 手动发布）。

**先规划后动手（2026-09-15 任务流设计，A 组 A2 已落地）**：子代理认领任务后、首个实质性工具调用前，必须先调 `task_plan(steps[])` 写下自己的解决计划；执行中用 `task_step(id, status, note?)` 推进——任意时刻至多一个 `doing` 步骤，`blocked` 必须带原因并落事件。计划允许修订（修订动作落审计）。任务流视图（§12）节点上显示的"任务进度"就是 plan 的 n/m、当前 doing 步骤与 blocked 原因；编排器与人类据此一眼看出每个子代理在干什么、卡在哪，而不必读会话流。

> **〔未实施〕任务暂停语义统一与待人工输入承接（E15，2026-09-16 定稿，§17 18）**
>
> 三类「停」——人工硬中断（E12 快照→看板「▶ 续跑」）、E8 步数耗尽自动暂停（直播间「继续」）、Agent 自主 `fail_task`（无快照、无任何继续入口）——在 UI 上不可区分，第三类恰是最需要人类响应的反而没有承接面（2026-09-16 实测：ROE 未核验自主挂起任务只能「放回」→ 新会话从零重做白烧 2.78M tokens，且误读旧计划 done 标记）。定稿：
>
> - **结构化暂停原因**：`fail_task` 增可选 `blocked_reason`（`awaiting_human`＝等人类输入（授权/ROE 核验、待补凭据）｜`error`＝真失败；缺省 `error` 向后兼容），落 tasks 加列（幂等加列惯例）并进 `task.failed` 事件。三类「停」统一为「都能继续，只是入口与原因展示不同」。
> - **续跑统一**：`awaiting_human` 的自主 fail 同样保留落盘快照，看板 failed 卡与人工中断同显「▶ 续跑」（复用 E12 revive_snapshot/resume 端点，限原会话；会话已 closed/无快照时自动降级为仅「放回」，不报错）。
> - **原因正文化**：暂停原因（fail note）直接显示在失败卡正文（截断+悬停全文），不再藏 tooltip；`awaiting_human` 卡加「⏸ 待人工输入」badge，与 error 卡一眼可辨。
> - **放回附注**：`awaiting_human` 卡出「✅ 已解决，放回继续」——放回时可附人类补充说明，写进任务行并落 `task.reopened` 事件（含 note），把「授权四要素已补齐」这类上下文带回黑板（放回本体语义不变，见 §6.4）。
> - **认领提示层注入**：有旧 plan 的任务被新会话认领时，注入旧 plan 全量步骤+**真实性注记**（`done` 仅供参考、不视为已验证，执行者自行核对黑板产出）+ 该任务挂靠 findings 摘要——杜绝重做与「把 done 标记当真」。
> - **主动通知 + 宁严勿松**：`awaiting_human` 任务计入审批铃铛红点数（只计数、不混入 approval 表、不改审批语义）；不自动重试、不自动放回，L2 自动链对它不做任何处置——只有人类能解。

### 6.2 噪声预算（WAF 对策）

| 模式                  | 并发性                         | 场景                   |
| --------------------- | ------------------------------ | ---------------------- |
| **passive（可共享）** | 同一目标多会话并行             | 逆向分析、被动信息收集 |
| **active（独占）**    | 同一 IP 同时最多一个（可配置） | 主动漏扫、爆破、利用   |

调度硬规则：active 任务按 IP 独占。外网打点阶段每域名一个任务，thin 资产做完即关、不开新会话；仅当 Orchestrator 判断值得深挖（发现高危入口）才生成提权/深测子任务。

### 6.3 行为权限（防越权）

按作用对象三级：

1. **读**黑板/其它目标历史发现 → 永远允许
2. **借力**：把其它目标的漏洞当垫脚石辅助自己认领的任务（如借泄露凭证打自己的目标）→ 允许，但必须在 chain 中记录溯源
3. **主动利用未认领目标** → 默认禁止，走"发现即上报"（**mode=redteam 例外**：ROE 四要素核验通过且在授权范围内允许主动利用+提权+横向——网关/审批/审计/噪声预算照旧，见 §6.9；渗透模式维持本条不变）

**发现即上报协议**：

````
Agent 在 A 资产发现敏感数据泄露（值得跟进）：
① findings 写入（含 POC）
② 生成任务"利用 a-0512 泄露获取凭证"（scope=a-0512）
③ 通知 Orchestrator → 排队/开新会话/转人类
````

项目级配置 `cross_target: ask-human | ask-orchestrator | auto-if-idle`，默认 **ask-orchestrator**。

### 6.4 Orchestrator（主代理/编排器）

> **已落地（A 组，2026-09-15）**：监控/派生/开窗/汇总之外，分析-分解-分派（含子代理 publish_task，A5.1）、优先级规划（L2 事件驱动去抖自动重排 + 手动按钮，A5.2/A5.3）、子代理数量上限（sessions_cap，机制 1.11）、claimed 步边界取消（A1）均已落地；replan 为一次性 planner 轮（单工具 set_priorities、只改 open、逐条审计），无 open 任务零 LLM 空转。

普通 LLM 会话 + 特殊工具集，**不直接干脏活**。任务流模式下职责：

- **分析-分解-分派**：主代理分析用户任务，分解为若干子任务发布到任务队列，并给出建议角色与初始优先级；**子代理也允许向任务队列发布任务**（发现值得跟进的东西即可发布，与既有"AI 会话发布任务"同源），由主代理统一规划。父子关系经 parent_id 落地，是任务流图的实线边。
- **优先级规划**：主代理 tick 之外，**L2 档**下队列出现新任务（人手发布=触发点 D）与 worker 空退（触发点 A）事件驱动触发重排，30s 去抖合并（踩窗口排队等待、睡满重入不丢触发；空转不刷计时）；直播间另设手动「**重排优先级**」按钮，**任何自主档随时可跑**、不受 30s 去抖/预算闸限制。重排=一次性 planner 轮：只调 `set_priorities`，只调整 `open` 行的 priority（0–9，小者优先）并逐行落 `task.updated` 审计，**不碰 claimed/done/failed**；与 tick 共用 tick 租约严格单飞（占用 409）。
- **子代理数量上限**：落地名为 `config.autonomy.sessions_cap`（非 max_subagents），限制非 closed 会话数；人手开窗与编排 spawn 同一硬上限、超限 409（机制 1.11 已落地，见 §6.8）。
- **监控**：队列空转、lease 过期回收、低产出任务、**plan 中出现 blocked 步骤**（子代理卡在哪一步，见 §6.1）。
- **开窗**：唯一有权启动新 AI 会话的角色（控制模型/容器成本；人类也可手动开）。
- **结束会话**（2026-09-13 定稿，2026-09-15 修订）：`sessions.status='closed'` + `session.closed` 事件，编排不再复用；**黑板数据全部保留**（事件仍可查），重开 = 另开新窗。注意**关页签（×）只是 detach、不结束会话**，结束会话走显式入口（页签右键/会话控制组，见 §3）。
- **汇总**：定期写项目简报（人类了解全局的入口）。

人类随时插手：发布任务、调整优先级、点「重排优先级」、否决决定。WebUI 直播间的"⏸ 插话"即此通道。

**任务看板运维**——四态状态机 open/claimed/done/failed，人类操作三条：

- **编辑**：仅 `open`/`failed` 可改（objective / task_type / noise_budget / priority / conflict_keys 五字段，PATCH 白名单）；`claimed`（执行中）只读——改了正在跑的任务语义混乱；`done` 冻结。编辑落 `task.updated` 审计。
- **失败放回**：`failed → open`（清 claimed_by/lease，result_note 保留仅作库存），落 `task.reopened`；lease 与 conflict_keys 随行状态自然回收，放回后认领互斥检查天然安全。**放回附注（E15，§6.1）**：`awaiting_human` 卡「✅ 已解决，放回继续」可附人类说明写进行内并随事件落审计，新会话认领时提示层注入旧 plan+findings 摘要防重做。
- **删除 = 物理删除**（不保留 cancelled 状态；A 组 A1 起 **open/claimed/done/failed 四态皆可删**）：有子任务先拦（409，防孤儿）；删除前落一条 `task.deleted` 事件（含 was_status 与字段快照，快照含 plan）作为唯一留存。删 claimed 任务时 lease/conflict_keys 随行立即释放，worker 在步边界 `_control_point` 的任务存活检查立即感知、当前步工具全部返回后即按人工中断 fail（不回队列，落 session.aborted；行已删则跳过 tq.fail）——机制 1.7 取消传播的单任务先遣版（见 §6.7、§3 终止入口收敛）。看板 claimed 卡按钮文案为「取消」，任务流节点与看板均可删 done。

**演练约定（demo_pentest.py，2026-09-13）**：演练脚本预创建 net=real 审批并可 `--auto-approve` 自批，`decided_by` 固定写 `"demo-script(auto)"`、事件 author="demo"——**不冒充人类审批语义**，审计一眼可辨；该路径仅限本地授权靶机演练，生产审批必须经人类（WebUI 审批页 / decide 端点）。

**任务拆解与分批发布（E14，2026-09-16 定稿，〔未实施〕§17 17）**：发布任务后编排 tick 对新任务做一轮分析，多目标任务（如渗透测试多目标）可拆解为子任务并对不同目标同时开窗——

- **拆解**：`publish_task` 工具增加 `parent_id` 参数（指向顶层任务，任务流图实线边）；**深度 1 层**——父任务必须是顶层任务，子任务不可再被编排器拆解；每轮 tick 发布硬闸 `max_publish_per_tick`（默认 **5**，**L0 提案模式同闸计数**——提案不落实体但灌洪水同样受限，宁严勿松）。
- **权威校验在黑板单一入口**：`TaskQueue.check_parent(project_id, parent_id)`（存在 + 同项目 + 父为顶层），在 `publish()` 内调用——编排器 / 人类 POST /tasks / 提案采纳三条写路径自动共享校验（坏父 → 编排器回填 `[拒绝]` / API 422）。
- **资产视图注入**：编排 LLM 当前完全看不到资产——`_stats()` 增 `assets` 键：`by_type`/`by_status` 计数 + 未覆盖资产清单（cap 30，行带 asset id 作拆解锚点，拆解时写进子任务 scope/conflict_keys）+ `uncovered_total`（续批决策依据）。未覆盖判定是**提示层**（`type:value` 与裸 value 作子串匹配 open/claimed 任务的 scope+objective，或精确命中 conflict_keys），**真护栏是 active 任务 conflict_keys 互斥**（claim 阶段既有）；误判方向=多判已覆盖少派发（宁严勿松）。
- **分批**：资产多时每轮只发 3-5 个资产的任务、发满即 done；worker 空退（触发点 A）→ `_maybe_auto_tick`/`_maybe_replan` 既有事件驱动链自动唤醒下轮 tick 续批；L2 下仅当本轮零产出才 converged 收敛停链——**无新触发点、无 scheduler**。
- **低水位补任务**：overview 任务计数 + `assets.uncovered_total` 足以让 LLM 判断「池空但有未派资产 → 继续发下一批」；**L0/L1/L2 同逻辑**（L0 拆解动作照常分析但只发提案；行内采纳经 POST /tasks 透传 parent_id——不透传会断撤回传播子树与任务图实线边）。

### 6.5 人机共写

- 黑板所有实体支持人工创建/编辑，标记 `author: human`
- Agent 读黑板时 human 条目视为高置信，可直接作为任务来源
- 入口：WebUI 黑板页各区块"+ 手动添加"；CLI `cyberstrike bb add finding/task/note`
- 典型用法：项目创建时预填已知资产、历史漏洞、**渗透授权边界（哪些能打/不能碰，成为 Agent 硬约束）**

### 6.6 会话绑定：项目（轨×能力包）+ 角色

会话创建时两层绑定，解析出全部运行上下文：

**项目绑定（必选）** → 项目的 `track` 解析出：黑板视图、角色集、任务类型注册表、规则与授权边界；`capabilities[]` 解析出：技能/知识库候选集、能力级红线、kb 源；另解析 workspace（产物位置）。

**角色绑定（可选）** → 解决"路由表膨胀"：先按角色分流，路由器只在角色范围内工作。

````
packs/tracks/<track>/roles/
├─ _generalist.yaml    # 默认角色：不过滤，全量能力（不绑角色时的兜底；受保护不可删）
├─ recon.yaml          # 信息收集
├─ external-entry.yaml # 外网打点
├─ privesc.yaml        # 提权
└─ lateral.yaml        # 横向移动
````

```yaml
# tracks/assessment/roles/lateral.yaml 示例
name: lateral
description: "内网横向移动：凭据搜寻、SMB/WMI 横向、AD 枚举；动静小、重溯源"  # 给人/Orchestrator 看的职责说明
persona: "你是内网横向移动专家，谨慎、动静小……"   # 注入系统提示
skills: [web-strike-entry, recon-asset-enum]     # skill 白名单（跨能力包引用，名字须真实存在）
task_types: [lateral-movement, credential-access]# 只能看到/认领的任务类型（轨注册表子集）
default_noise: medium                            # 默认噪声预算（真正生效：缺省取此值，不再写死 passive）
tools: [run_cmd, bb_query, kb_open]              # 工具白名单；缺省/空 = 不限制
max_runtime: docker                              # 运行时软上限：host<wsl<docker<sandbox，只可能比网关策略更严
max_steps: 30                                    # 步数软上限；缺省 = AgentConfig 全局值
```

**角色是过滤器的集合**：① 可见 skill 子集（路由器只在白名单内匹配）② 可认领任务类型 ③ 系统提示人设 ④ 可用工具子集 ⑤ 运行时与步数的更严上限。Orchestrator 派活语法："提权任务 → 开 privesc 角色会话"或派给空闲同角色会话。

**语义定稿：**

- **软边界 + 一次性越界审批（v0.2 定稿）**：角色的 skills/task_types/tools/max_runtime 收窄都是"软边界"而非绝对禁止。Agent 遇明显越界事项时调 `request_escalation(kind: skill|tool|runtime, target, reason)` → 复用 approvals 表与审批收件箱（不新增表）→ 人类批准后 `escalation_poll` 随次调用取回结果。授权为**一次性**：skill/tool 类授权当次任务内有效、`runtime` 授权绑定 task_id，任务 complete/fail 即清。**安全硬底线不可被 escalation 放松**（不可信代码必入容器、未知样本 L3+fakenet、net=real 仍走原审批），escalation 只能临时放宽**角色自设**的软限制；全程 `escalation.requested/decided` 审计。
- **角色红线**：`tracks/<track>/rules/role-rules/<role>.md` 存在时仅注入绑定该角色的会话（glob 不递归，天然不进全员注入）。
- **创建时固定，不中途切换**：需要新角色 = Orchestrator 开新会话。上下文纯净、审计清晰，一个窗口一个职责。
- **角色与路由器是组合关系**：角色解决"我是谁、管哪摊"（几百→十几），路由器解决"这个输入用哪个 skill"（窄域内精确匹配）。
- **角色 = yaml，用户可 CRUD**（设置页）：新建/复制/删除，不动代码；`_generalist` 受保护。CTF 轨角色意义不大（一题一会话），assessment 是角色主场。
- **角色清单服务化**：`GET /api/projects/{pid}/roles` 扫 `packs/tracks/<项目轨>/roles/*.yaml` 返回角色元数据（含 description）；WebUI 开窗（角色下拉）与编排一轮（allowed_roles 多选 popover，空选 = 不限）经该端点取角色——加轨/加角色不改前端。Orchestrator 的 spawn 工具同样拿到角色 description（开窗决策不再只见角色名字符串）。
- **饿死检测**：Orchestrator 态势统计对长期 open 且没有任何角色的 allowed_task_types 能覆盖的任务出告警（task_type 拼错/注册表漏配的兜底信号）。

### 6.7 多会话协调机制总览

下列机制是"多会话协同打同一目标"的完整协调面，分四层共 12 项。**落地状态**：1.5〔部分落地：撤回 + finding_update 两类系统私信已落地（A4），Agent 自由私信未做〕、1.6/1.9/1.11/1.12〔已落地〕（1.9/1.11 由 G 批批 2/批 3 落地，见 §6.8），1.7 的**单任务先遣版**（claimed 步边界取消，无父子传播）已随 A 组 A1 落地、传播本体未做；其余 1.1–1.4、1.8、1.10 为〔未实施〕的 F 批定稿设计。**设计纪律：任务状态机只有 open/claimed/done/failed 四态**——等待 = open 行带门控列、claim_next 的 SQL 排除；取消 = open 物理删除（task.deleted 快照审计）+ claimed 步边界 fail；任何机制都不得新增 blocked/cancelled 状态。

**① 任务分配层**

- **1.1 发布去重 + 工作集软声明**〔已落地，2026-09-16（机制 1.1，schema v7）〕：publish 算指纹（task_type + 归一化 scope + 规范化 objective 的 sha256 截短，tasks.dedup_fp），命中 open/claimed 不新建——API 返回 `{deduplicated:true, task_id, existed_status}`（人类确认"仍要发布"后 force 重发），Agent publish_task 回填 `[复用]` 静默复用。`workset` JSON 列：发布时可选声明（Agent 工具/API 均支持），**纯 advisory**——不阻塞任何人，看板 open 卡与 bb_query 可见（"有人正在分析 0x401000"），供避让。任务流图补**建议私信边**（kind=suggest 点虚线：同父或 context_refs 相交、已认领、尚无私信——机器猜测不落库，A3 原 TODO）。
- **1.2 blocked_by 依赖调度**〔未实施，F 批〕：tasks 增 `blocked_by` JSON 列；被门控任务**状态仍为 open**，claim_next SQL 排除"有依赖未 done"的行（发布者/UI 显"等待依赖"门控标记）；依赖全 done 后下次认领自然可见，无状态翻转。依赖 fail → 编排器决策自动 fail（reason=依赖失败）或解除门控重派，黑板只提供原语。开发工作流（implement→build→test→review，§15）与 map-reduce 汇聚均依赖此原语。
- **1.3 大任务分片（map-reduce）**〔未实施，F 批〕：父任务 `shatter(by: func|url|host, items[])` 派生 N 个子任务（parent_id 已有），分片键写 workset 防重复分片；聚合任务 blocked_by 全部分片；分片失败重派/取消其余是编排器策略，黑板只提供父子关系与依赖原语。**注记（2026-09-16）**：E14（§6.4「任务拆解与分批发布」，§17 17）是其部分前置——编排器一层拆解 + 资产分批先行；shatter 原语/workset 防重分片/聚合依赖仍属 F 批。
- **1.4 conflict_keys 升级为资源租约**〔已落地（发布期/认领期为主，2026-09-16 机制 1.4，schema v7）〕——设计专节见 §6.7.1。落地口径：`resource_leases` 新表（键方案白名单归一化在 `core/blackboard/leases.py`）；锁模式由噪声预算映射（passive→S 共享、active→X 独占，无需新键语法）；认领时 conflict_keys 同事务转写为租约行，同键冲突（X×X/X×S）→ 写 `wait_for` 门控标记保持 open 拒领（**标记在事务内写、ClaimError 在事务外抛——异常回滚会吞标记**）；claim_next 预过滤 wait_for 仍被占行与死锁牺牲者冷却行（等待不占线程）；收尾/删除/租约过期释放租约并同事务重校验等待者（清已空闲的 wait_for）；wait-for 环检测（牺牲者=最年轻，清 wait_for+冷却 5 分钟+`lock.deadlock_victim` 事件）为安全网——发布期门控下环构造上不可达，供运行期动态锁未来路径兜底；租约有效性 = JOIN tasks（claimed 且 lease_until 未过期）判定，免心跳双写。**运行期动态锁申请（bb_acquire/网关自动申请）后置**。

**② 协作感知层**

- **1.5 会话收件箱 + 交接**〔部分落地〕：**已有（系统侧私信通道）**——`session_inbox` 表 + `GET /sessions/{sid}/inbox[/read]` 端点 + worker 认领/步边界注入 + UI 会话页签红点（与审批收件箱 approvals 表/ApprovalsView 严格分设：这是**会话间消息**，知会而非待决，不进审批流）。**缺口**：Agent 自由通信工具 `bb_notify(to: session|role, kind: intel|handoff|assist, content, refs[])`（落 `message.inbox`/`message.handoff`，带 to_session/to_role）、会话领任务时按 scope 注入未读 intel、handoff 接手任务、to_role 角色广播均未做；当前系统私信写方有两类：1.6 撤回传播（kind='basis_stale'）与 finding 增补通知（kind='finding_update'，A 组 A4）。
  - **finding 引用更新私信（2026-09-15 任务流设计新增，A 组 A4 已落地，kind='finding_update'）**：任务 `context_refs` 引用的 finding 发生**实质更新**——新增 POC、severity/verified/relates_to 变化、func 结论影响证据含义——即由系统私信引用方任务的认领会话（撤回仍走 basis_stale，二者不混：撤回=依据被推翻，更新=依据有增补/修正）。复用撤回传播的同一套设施：部分唯一索引合并去重、worker 步边界 drain 注入、页签红点。**会话间的私信关系同时是任务流图上的虚线边**（§12）。
- **1.6 证伪撤回传播**〔已落地〕：finding 转 false-positive 发 `finding.retracted`；引用方（evidence.relates_to/任务 context_refs）经收件箱+事件得到通知；在飞任务打 `stale_refs` 标记（不强制杀，Agent/编排器决定），画布 relates_to 边淡出。已实现行为见本节末「撤回传播实现约定」。
- **1.7 取消传播**〔传播本体未实施，F 批；单任务先遣已随 A 组 A1 落地〕：人工取消/父任务 fail 沿 parent_id 树传播——**open 子任务物理删除**（沿用 delete() 纪律，落 task.deleted 快照，payload 标 cancel_cascade）；**claimed 子任务发 `task.cancel_requested`**，worker 步边界消费后置 fail（reason 注明传播来源），fail 再向下传播一层；派生时标 `keep_independent` 的独立子任务不传播。先遣版=人工直接删 claimed 任务的步边界硬中断（无父子传播，见 §3/§6.4）。

**撤回传播实现约定（1.6 已落地；私信通道即机制 1.5 的系统侧）**

- **引用识别三层合一**：publish_task 显式 `refs[]`（orch 工具与人发任务 API 均透传）∪ 服务端正则从 objective 抽 `find-[0-9a-f]{12}` ∪ 撤回时反向扫 findings.evidence.relates_to 与 tasks.parent_id 树。
- **触发扼流点**：`patch_finding` 事务内比较旧 status，仅「非 false-positive → false-positive」跳变触发一次；重复 PATCH 不重放。先发项目级 `finding.retracted` 广播，再解析私聊目标。
- **四类私聊目标**（human/system 作者不私聊、closed 会话不投递；claimed/open 任务区别处理）：① 被推翻 finding 的作者会话；② relates_to 反向边下游 finding 作者；③ context_refs 命中任务及其 parent_id 子树——claimed 私信执行者**并**挂 stale_refs、open 只挂 stale_refs（认领开场白时必见）、done/failed 只私信作者不改行；④ 命中任务向上一级父任务 claimed 时抄送父认领者（不挂标）。每条新私信发 `message.inbox`（未读去重索引保证不重复打扰，读后同一依据可再投递）。
- **Worker 送达**：认领成功后若 stale_refs 非空，首条 user 消息前置「⚠ 依据撤回」强制三选一（带理由继续 / fail_task / 改道）；长任务进行中撤回在 `_control_point` 步边界 drain inbox 注入，不打断当前工具调用；done 时 stale_refs 非空补发 `task.basis_stale_done` 复核事件（fail 不补；L1+ 编排器据此派生复查，L0 仅暴露给人）。
- **UI**：直播间会话页签未读红点（切页签即标记已读）；事件流 🚫/🔔/⚠ 三样式；评估画布凡端点为 false-positive 的边 0.25 透明 + 点虚线淡出（弱边本就不起源于 FP 节点），点选边浮卡显琥珀警告。

**③ 执行/资源层**

- **1.8 逆向工具互斥锁**〔未实施，F 批〕：headless 全量导出按 (project, sha) 互斥——现状 `decompiler.py` 进程内 Lock + API `_triage_running` 标志均不跨进程且有 TOCTOU，升级为库级锁行（复用 §6.7.1 的 `tool:*` 租约）；并发请求**排队共享同一结果**（后来者等同一 Job 产物，不重复跑 900s）；IDA `.i64` 单实例（现状已有 locked 探测）固化为 `tool:ida:<sha>` 租约，GUI 已开时 headless 请求排队/改道。Agent 无感（工具服务层内部消化）。
- **1.9 编排状态持久化**〔已落地，G 批批 3〕：游标/轮数/digest 轮落 `orchestrator_state`（event_cursor/cycles/last_digest_cycle，tick 开头装载、结尾落盘，serve 重启后 tick 续跑）；**并行 tick 防护** = 项目级 tick 租约（tick_owner/tick_lease_until，单 `_tx()` BEGIN IMMEDIATE 获取，TTL 900s + job 内每 LLM 步心跳续租，崩溃自然到期，owner 身份释放；手动并发第二个同步 **409**、未来自动 tick 则跳过）；tick 返回结构化结果 `{summary, published[], spawned[], digest, proposals[]}`。live_sessions 仍只在内存（跨重启靠会话 rehydrate 重建，不入库）；tick 内不申请任务资源锁，避免与工作线程成环。实现见 `core/orchestrator/state.py`、§6.8。
- **1.10 高噪声联动审批**〔未实施，F 批〕：approval 携带归一化目标（ip/host）；net=real 高噪声审批弹窗显示同目标历史审批（"此前已批准对 X 的 active 动作 N 次"）辅助判断；**只联动展示，绝不自动批准、不放松红线**。
- **1.11 开窗上限 + 预算**〔已落地，G 批批 2〕：`config.autonomy.sessions_cap` 对人手 POST /agents 与编排 spawn 同效（超限 409；L1 审批预检+批准终检为第三检查点，批 4 已落地）；`token_budget/task_budget` 配置面已就位；**用量记账**已落地（每次 chat 落 `llm.usage` 事件 + 原子累加 orchestrator_state，直播间 chip 显 ∑token/任务数/活跃窗）；80% 首发 `budget.soft_warning`（budget_warned 去重且预算调大回落自愈）；**硬阻断只拦编排自主动作**（publish/spawn gate 回调实时重读配置），人手动作超预算仅 ⚠ 警告放行。详见 §6.8。

**④ 一致性治理层**

- **1.12 证据合并语义修复**〔已落地〕：列表键去重追加、标量取严不降级、笔记分段署名——语义见 §5.3（add_finding 合并路径 + finding/func/asset 三个人类 PATCH 事务化）。
- 同期并发修复（非 12 项机制，均已落地）：**租约心跳**（认领后守护线程每 10 分钟 renew_lease，见 §6.1/§6.4）；**packs 进程写锁**（core/skills/writing.py，设置页 packs 写串行化，仅保单进程）。

#### 6.7.1 资源租约与死锁避免（1.4 专节）

> **〔未实施〕** F 批重点项（§17 机制 1.4），代码未动；本节为定稿设计。v6 已被 A 组占用（tasks.plan、orchestrator_state.last_replan_at）；落地时在 v6 幂等加列或升 v7（新表 resource_leases + tasks.wait_for 列）。

现状基线：conflict_keys 是认领时快照交集检查（tasks.py），拿不到就认领失败、线程不阻塞——无死锁但也无等待语义；键由 LLM 裸写无归一化；运行中无法申请新锁。升级设计（**任务仍只有四态**）：

- **资源分类与归一化（服务端做，不信任 LLM 原文）**：键方案白名单——`ip:<ip>`、`host:<idna小写>`、`url:<host>/<path>`、`binary:<sha256>`、`func:<sha256>:<addr>`、`tool:<name>:<arg>`、`user:<ns>:<v>`（受限命名空间）；publish 时把 scope/conflict_keys 原文归一化；非法方案/空值/过宽（`ip:*`）拒收；可疑键（拼写错误、混类）doctor 警告。
- **锁模式**：X 独占（主动动作：打点/爆破/写入）；S 共享（被动分析，多 S 兼容、与 X 互斥）；advisory（工作集软声明，永不阻塞，见 1.1）。**禁止 S→X 就地升级**：须先释放 S 再申请 X（消除两个 S 持有者互等升级的经典死锁）。
- **存储**：新表 `resource_leases(project_id, resource_key, mode, task_id, session_id, granted_at, lease_until)`；授予在单个 `_tx()`（BEGIN IMMEDIATE）内"检查冲突→写租约"原子完成；schema 升级走版本号幂等迁移；tasks 增 `wait_for` JSON 列。
- **等待语义（分阶段定稿，线程都不阻塞）**：

  - *认领/发布期*：资源不满足 → 任务保持 open、写 `wait_for` 门控键，**claim_next 排除仍被占的等待行，worker 线程立即释放不空转**；锁释放（done/fail/租约过期）时按优先级→创建时间 FIFO 清候选 wait_for（housekeeping/事件触发），清后**重新校验再授予**（防唤醒风暴与陈旧授予）。
  - *运行期*（工具执行/升级审批时申请新 X 锁）：**永不阻塞线程**，立即回填 `resource_busy` 工具错误，Agent 按现有"工具异常回填不中断"模式改道；确需等待的目标由编排器另发布带 wait_for/blocked_by 门控的任务。

- **死锁防御六层（排序+TTL+禁升级，再加环检测双保险）**：

  1. **锁管理者唯一**：授予/释放只经黑板事务，Agent 不得私等/轮询；
  2. **全局资源排序**：批量获锁 API `acquire_many(task, keys[])` 强制按归一化键字典序申请，破坏循环等待条件；
  3. **禁止锁就地升级**（见上）；
  4. **TTL + 心跳**：租约 30 分钟，随任务心跳一并续租；会话崩溃由 expire 自动释放——物理上不存在永久死锁；
  5. **等待不占线程**（wait_for 门控 + open 态），不存在 N 个会话互相挂死；
  6. **wait-for 图环检测兜底**：编排 tick（或队列 housekeeping）建"任务→等待资源→持有任务"有向图，检出环时选**最年轻任务**（created_at 最晚）为牺牲者：清其 wait_for、加冷却时间防立刻再撞、本次获锁判失败由编排器重规划，落 `lock.deadlock_victim` 审计事件。环检测是实现 bug 与未来人工强占路径的安全网。

- **与现状兼容**：认领时交集检查保留为快速路径；任务 claimed 后其 conflict_keys 在同一事务转写为租约行；非 passive 必须带键的旧规则不变。工具层互斥（1.8）复用同一租约机制（`tool:*` 键，持有者=系统会话），但"排队共享结果"语义由工具服务层实现，不经任务 wait_for。

### 6.8 项目自主级别 L0/L1/L2（G 批，2026-09-15 设计）

> **〔全部落地〕** 批 2 ✅ 配置面 + sessions_cap + 用量记账/软警告/自主硬闸（机制 1.11）；批 3 ✅ tick 租约 + 编排状态持久化（机制 1.9）；批 4 ✅ L1 开窗审批流（见下「L1 开窗审批」）；批 5 ✅ L2 全自动链（见下「L2 全自动链」）；批 6 ✅ L0 提案模式（见下「L0 提案模式」）。自主三档行为已全部生效，无中间态。

项目级（不是会话级）自主档，唯一配置位置 = `project.config.autonomy`，project.json 与黑板 projects 行双写，`PATCH /api/projects/{pid}/config` 整段替换、服务端 `normalize_autonomy` 归一化（非法 422；旧项目无段读时按轨默认自愈）：

````
autonomy:
  level:           L0 | L1 | L2        # 建项默认：ctf=L0, assessment=L1, research=L1（malware 落地前只给 L0）
  paused:          false               # 暂停一切自动（批 5 消费；手动动作照常）
  sessions_cap:    4                   # 1..20，非 closed 会话硬上限（人手/编排/审批同效）
  max_chain_ticks: 3                   # 1..20，L2 单条自动链 tick 预算（批 5）
  token_budget:    null                # null=不限；四项 token 合计（in/out/cache read/cache creation）
  task_budget:     null                # null=不限；只计 orchestrator 自主发布
````

**档位行为定稿（任何档位都不放松安全层——net:real/越界/L3 审批永远强制，见 §7）**：

| 行为 | L0 全手动 | L1 任务自动·开窗审批 | L2 全自动链 |
|---|---|---|---|
| orch 发任务 | 只提案 ✅（`orch.proposed`，不写实体） | 直接发 ✅ | 直接发（预算硬闸）✅ |
| orch 开窗 | 只提案 ✅（人在事件流行内采纳才建窗） | 进审批收件箱，人批后自动建窗+开跑 ✅ | 直接开窗+自动开跑（cap 硬闸）✅ |
| 自动 worker | 关 | 开 ✅（tick 后/审批后/人手插话后 kick；审批新窗当场一个 job） | 开 ✅（同 L1，另加开窗即起 worker） |
| 自动续 tick | 关 | 关 | 开 ✅（max_chain_ticks/收敛/节流/暂停约束） |
| 人手开窗/插话/跑队列 | 照常（cap 409） | 照常；L1/L2 未暂停时开窗与插话后自动 kick ✅ | 同 L1 |

**已落地的共用设施**：

- **sessions_cap**：计数口径=非 closed sessions 行；检查点①人手 `POST /agents` 超限 409 ✅、②orch spawn 经 gate 回调拒绝并回填 LLM ✅、③L1 审批预检（建单前经同一 gate）+批准终检（执行前实时再数一次）✅。
- **用量记账**：Agent executor/advisor 与 Orchestrator 每次 chat 成功后调 `record_llm_usage`（失败只 log 不阻断主循环，全 0 usage 跳过）：单事务原子累加 `orchestrator_state`（INSERT OR IGNORE 建行），并发 `llm.usage` 事件（source/model/四项 token/total）；达 token_budget 80% 首发 `budget.soft_warning`，budget_warned 持久去重、预算调大用量回落自动复位。GET 项目返 usage 视图（used/budget/pct + 任务计数 + active_sessions），直播间 chip 5s 轮询、≥80% 琥珀 ≥100% 红。
- **硬闸口径**：`hard_block_reason(bb,pid,action)` 经 gate 鸭子回调注入 Orchestrator（本模块不反向 import），每次实时重读配置——cap 拦 spawn、task_budget 拦 publish、token_budget 两类都拦；拒绝以 `[拒绝] <原因>` 回填 LLM 改道。**人手动作超预算只 ⚠ 警告不拦截**（spawnAgent 响应带 warning）。
- **tick 租约（机制 1.9）**：`core/orchestrator/state.py`——`acquire_tick_lease` 单 `_tx()`（BEGIN IMMEDIATE + 进程写锁）内建行→读租约→他人未过期抛 TickLeaseError→否则抢占；手动 tick 端点在构造 LLM/提交 job **之前**同步获取，冲突直接 **409**（不产生 job；503 等同步失败先释放租约），job finally 只按 owner 身份释放；TTL 900s 崩溃自然到期，job 内每个 LLM 步前 `renew_tick_lease` 心跳，易主即抛错停续。未来自动 tick 冲突=跳过不报错。
- **编排状态持久化**：tick 开头经 state_loader 装载 `event_cursor/cycles/last_digest_cycle`（Orchestrator 纯鸭子回调，脚本/旧测试不接线即纯内存），结尾经 state_saver 落盘；digest_every 判定与事件增量游标跨重启生效（游标在态势收集时推进，上一轮自身产生的事件下轮可见，属既定口径；历史 backlog 超 100 条时只喂最新 100 条、游标一次跳到当轮开始时的末端 id，旧事件不逐轮回放）。tick 结构化返回 `{summary, published:[task_id], spawned:[{session_id,role}], digest|null, proposals:[{op,args}]}`，JobRegistry 存结构、前端中文渲染；proposals 仅 L0 提案模式非空（批 6）。
- **L1 开窗审批流（批 4 已落地）**：
  - **Orchestrator 分流**：经鸭子回调 `autonomy_provider()`（API tick 端点注入，实时重读归一化 autonomy；脚本/旧测试不接线=直接开窗）取 level；`L1` 下 `spawn_session(role, reason)` 不调工厂，先过白名单与 gate 预检（cap/预算），再 `bb.request_approval(action={"op":"spawn_session","role","reason"}, risk="low", requested_by="orchestrator")`，工具回填「已提交审批 appr-x，批准后自动建窗开跑」给 LLM；不进结构化结果的 `spawned`（窗未开）。spawn_session 工具 schema 新增可选 `reason`（审批卡展示）；**L1 档 reason 必填**（空白直接 `[拒绝]` 回填，不建审批单——审批人只看得到 role+reason）。L2/未接线维持直接开窗；L0 走提案（见上「L0 提案模式」）。
  - **decide op 处理器注册表**（`core/api/app.py` 内，**字典白名单分派，绝不 eval**）：`POST /api/approvals/{id}/decide` approved 后解析 `action.op`，命中注册表才执行，无 op/未知 op 维持「只翻状态」的旧语义（net_real 等旧式审批不回归）。`spawn_session` 处理器：**批准终检 cap**（实时再数非 closed 会话，超限不执行）+ **open 任务终检**（2026-09-16：项目已无 open 任务——同一 tick 的触发点 B kick 让既有 worker 在审批等待期抢走了任务——则不建窗，落 `approval.exec_failed{error:"无待认领任务…"}`，防空窗占 sessions_cap）→ `_registered_session_factory` 建窗（即注册 app.state.agents，非孤儿窗）→ 发 `session.spawned`（payload 带 approval_id）→ 当场 submit 一个 `agent-work` job（批准即开跑，空队列自然退出）。响应 `{approval_id,status,executed,session_id?,job_id?,error?}`。
  - **失败不回滚批准**：终检超限/角色文件缺失/无 LLM key 等执行失败时审批行保持 approved，落 `approval.exec_failed{approval_id,op,error}` 事件，HTTP 200 返 `executed:false,error`（人可改配置/关窗后手动开窗）。
  - **拒绝**：rejected 不动作；orch 下轮 tick 经事件游标看到的 `approval.rejected`（payload 含 action）感知改道（复用现有事件，不加专用通道）。
  - **防伪造红线**：Agent 工具集没有任何创建审批的入口（越界目前只有「工具白名单拒绝+请人类改配置」），op=spawn_session 审批的唯一生产路径是 Orchestrator；未来 Agent escalation（阶段 4）不得复用该 op 自行建单。
- **L0 提案模式（批 6 已落地）**：`OrchestratorConfig.propose_only`（API `_build_orchestrator` 按实时档位 `level=="L0"` 注入，手动/自动 tick 同装配）。L0 下 `publish_task`/`spawn_session` **校验照跑但不写实体**：
  - **publish_task**：task_type 注册表校验、噪声缺省（取注册表默认并并入提案 args）、入参形状校验照常，未注册类型仍 `[拒绝]` 回填 LLM 改道；**不走 gate 预算闸、不调 `tq.publish`、不触发 `on_task_published` 计数、不进 `published`**，改发 `orch.proposed{op:"publish_task", args:{objective,scope,task_type,noise_budget,priority,conflict_keys,refs}}`（author=orchestrator）。
  - **spawn_session**：`allowed_roles` 白名单与 config `max_sessions` 校验照常；**不走 sessions_cap gate、不调 session_factory、不发 session.spawned、不进 `spawned`**，改发 `orch.proposed{op:"spawn_session", args:{role,reason}}`。cap/预算在人采纳时由 `POST /agents` 的既有 409/警告兜底。
  - **write_digest 照常落地**（简报是黑板写作不是自主动作）；done/LLM 循环/tick 租约/状态持久化/记账均不变。tick 结果 `proposals:[{op,args}]` 收集本轮全部提案；LLM 收到工具回填「已提案 #event-id，等待人类采纳，本轮可继续提案或 done」。系统提示在 L0 下经 `{autonomy_notice}` 槽追加提案模式说明（你不直接派活/开窗，只产提案）。
  - **不污染审批语义**：提案不是 approval（无 decide、无 risk、无收件箱条目），只是事件流上的建议；不碰任务四态（tasks 表无行）。采纳=**人以自己名义**走既有写口：前端事件流 `orch.proposed` 行内「采纳」按钮——发任务→`POST /tasks`（created_by=human）、开窗→`POST /agents`（created_by=human，L0 不自动起 worker）；采纳状态只存前端内存（按钮置「已采纳」），刷新后提案仍在事件流、可再次采纳（人负责，不做去重）。
- **L2 全自动链（批 5 已落地）**：纯事件驱动，**无调度器/无后台轮询**——所有自动动作都是 HTTP 请求或 job 收尾的同步副产物，实现集中在 `core/api/app.py` 两个辅助 + 五个触发点：
  - **两个辅助**：`_kick_workers(pid)` 遍历黑板非 closed/非 paused 会话，经 `_session_job_running` 内存去重（在跑不重复提交）、`_ensure_agent` rehydrate、跳过 `agent.paused`，各 submit 一个 `agent-work` job——空队列时 worker 只花一次 claim SQL 即零成本退出；`_maybe_auto_tick(pid, reason)` 是自动续 tick 的**唯一入口**，自身只判闸门+submit job，绝不直接跑 LLM。
  - **触发点**：A＝worker 队列空退出（仅 `AgentSession.last_claim_idle=True` 才触发；暂停/中断退出不触发；项目仍有 agent-work job 在跑则跳过，最后一个退出的 worker 才续）；B＝tick 完成后处理（手动/自动共用：L1/L2 未暂停先 kick；L2 按本轮 `published/spawned` 是否非空决定链续/止）；C＝开窗落地（L2 orch 直接开的窗由 B 的 tick 后 kick 统一起 worker；人手 `POST /agents` 在 L1/L2 未暂停时也自动起一个 worker；L0 不自起）；D＝人手 `POST /tasks` 插话（L1/L2 未暂停 → kick；L0 不动）；E＝审批批准（处理器已为新窗起 job，再对全项目 kick 一次，唤醒能认领同轮其他任务的旧窗）。
  - **链生命周期与事件**：L2 手动 tick 产出非空且链未激活时 `chain_active=1/chain_ticks=0`、进程内 `app.state.active_chains` 加标记、发 `orch.chain_started{reason:"manual_tick"}`；自动 tick 拿租约成功后 `chain_ticks+1/auto_ticks_total+1/last_auto_tick_at=now`（拿不到租约只跳过不计数）。链停一律发 `orch.chain_stopped{reason,chain_ticks}` 并清 `chain_active` 与进程标记，reason 枚举：`converged`（自动 tick 零 published 零 spawned，收敛终止）、`no_sessions`（有产出但全项目零非 closed 会话，无执行者链无法自驱）、`max_chain_ticks`（自动 tick 数达预算，默认 3）、`paused`（暂停）、`level_changed`（L2 被降级）、`budget_blocked`（publish_task 与 spawn_session 两类自主动作都被硬闸拦住）、`restart`（见急停）、`error`（tick job 内异常，payload 带 error，job 自身 done 不抛断线程）。
  - **防失控七闸**（`_maybe_auto_tick` 顺序判定）：①档位实时重读（≠L2 落 `level_changed` 停链）；②`paused` 落 `paused` 停链；③链预算 `chain_ticks>=max_chain_ticks` 落 `max_chain_ticks`；④收敛终止在 tick 后处理（零产出/零会话）；⑤软去重＝在跑 tick/auto-wait job 直接跳过、硬去重＝tick 租约（自动 job 拿租约失败返 `{"skipped":"lease"}`，不报错不计数）；⑥节流＝距 `last_auto_tick_at` 不足 `AUTO_TICK_MIN_INTERVAL`（默认 10s，模块常量测试可 monkeypatch 为 0）时**不丢弃触发**，改 submit 一个 `orchestrator-auto-wait` job（sleep 剩余秒数后重入全部闸门，防链条搁浅；wait job 同样参与软去重，项目删除时算忙）；⑦预算硬闸实时重读，两类自主动作全被拦才停链（只有一类被拦时照常续 tick，由 LLM 自行改道）。另：项目仍有 worker 在跑不算闸但直接跳过（等收尾方触发）。
  - **paused 语义**：不拦人手显式动作（手动 tick/开窗/插话），但 B/C/D 的自动 kick 在暂停时不发起；在跑 worker 每次认领前重读 `autonomy.paused`，True 则不再认领新任务直接退出（不点暂停会话、不触发 A）；链在下次触发点落 `chain_stopped{paused}`。**取消暂停不自动恢复链**——人再点一次「编排一轮」开新链（与重启同纪律）。paused 不拦人显式「跑队列」/恢复（人工 override，暂停下照常认领消费，与行为表「人手动作照常」一致）；它只让自动消费 job（触发点 C/kick/批准即跑）在认领前零成本退出。
  - **重启=急停（不变量）**：自动续 tick 同时要求 DB `chain_active=1` **与** `pid ∈ app.state.active_chains`（进程内集合，只在本进程 B 启动链时加入；重启后集合为空且服务不预置任何 worker/tick 线程）。进程标记缺失时 `_maybe_auto_tick` 落一次 `orch.chain_stopped{reason:"restart"}` 并清零 `chain_active`，拒绝续链。GET 项目 usage 的 `chain` 视图 `{active,ticks,auto_ticks_total,estranged}`（`estranged`=DB 活但本进程无标记，API 层拼）供直播间提示「链因服务重启已急停，点编排一轮手动恢复」。
  - **自动 tick 参数**：无 HTTP body，用默认 `TickIn`（allowed_roles=None=轨内全部角色、max_sessions=4、digest_every=3、max_steps=12）；sessions_cap/预算等硬闸不受影响，Orchestrator 仍每 tick 新建但经 state_loader 续用 cycles/事件游标。
- **红线**：L2 的"链"指自动 tick 链，**chains 攻击链表永远人工建链**，Agent/Orchestrator 无自动建链工具；重启=急停（chain_active 持久但不自动恢复，必须人手动开新链）✅ 批 5。

**与作战模式正交（E16，§6.9）**：mode=pentest|redteam 与自主档独立——档位管「自动动作的审批强度」，mode 管「行为边界与目标语义」；L2 自动链的行为边界同时受两者约束，任一收紧都以更严者为准。

### 6.9 作战模式与 mission 战役目标（E16，2026-09-16 定稿，〔未实施〕§17 19）

> **〔未实施〕**——本节全部为设计定稿，实施挂 §17 19；起因=用户重申渗透测试/红队行动两个定义与长任务语义（2026-09-16）。

**两个作战定义**：

- **渗透测试（mode=pentest，缺省）**：负责**挖到漏洞**并形成 verified 证据链（收尾产物=漏洞报告）。验证上限=**影响证明级**——允许证明危害的一次性动作（SQL 注入读取敏感表佐证、RCE 一次性命令回显等），**不驻留、不建持久化、不内网横向、不推进提权链**；发现可利用点→记录结论+生成建议任务，是否升级红队由人类决定。
- **红队行动（mode=redteam）**：结合**信息收集、外网打点、社工钓鱼（暂未开发，见下）、内网渗透、提权、横向移动**，以**获取重要高危业务数据与各类设备权限**为主；**打穿 mission 判据清单**为主要目标，允许长周期持续推进。

**正交性**：mode 是 assessment 轨内项目级配置 `mode: pentest|redteam`（缺省 pentest），与自主档（§6.8）、能力包（§4.5.2）正交；角色集（recon/external-entry/privesc/lateral）两模式共用，**不建新轨**。安全红线不因 mode 放松（最小伤害/反幻觉/弱口令力度/越权纪律全部照旧）——mode 放开的仅 §6.3 第 3 级禁令一项。

**mode×行为边界表**：

| 维度 | pentest | redteam |
|------|---------|---------|
| 主动利用未认领目标（§6.3 第 3 级） | 禁止，发现即上报 | **ROE 内放开**：ROE 四要素核验通过且授权范围内允许（网关/审批/审计/active 噪声互斥照旧；未核验或出 scope 仍硬禁） |
| shell | 影响证明级一次性回显即止 | 可获取；管理面未开发前经 run 网关+会话设施记录（Agent 无裸 shell 约束不变） |
| 提权/横向 | 只记录可能性，不推进 | 允许（溯源记录照旧，借力必记 chain） |
| 收尾产物 | 漏洞报告（verified 证据链） | 战果链报告（攻击链+战果资产+取证记录） |

**切换门槛（宁严勿松）**：pentest→redteam 需 ①前端二次确认弹窗（明示放宽范围）②ROE 四要素必填（精确范围/排除系统、测试窗口、允许动作、应急联系人——复用 E15 授权核验语境）③落 `mode.changed` 审计事件；redteam→pentest 随时可切（收紧无门槛）。

**mission 战役目标**：project config 增 `mission: {text 自然语言目标, criteria 完成判据}`；编排 tick 注入 overview，LLM 每轮对照决定派生/续批/收敛——

- **pentest**：判据=漏洞覆盖度与资产覆盖度；「没挖到漏洞就依次自动发布任务扫描其他资产」由 E14 低水位续批（§6.4）+ mission 对照承载；
- **redteam**：判据清单（要打穿的目标清单）**全打完=完成**；未列清单缺省=全部已登记资产打穿；**资产穷尽才收敛**；
- **converged 语义升级**：从「零产出停链」（§6.8）→「**mission 判据达成或资产穷尽**」——redteam 下零产出但 mission 未达成→replan 调整打法继续而非停链；§6.8 七闸/max_chain_ticks 等防失控设施**全部不放松**（持续推进≠失控）。

**社工钓鱼（暂未开发，仿 §15.5 专项挂账）**：红队模式配合作战能力=授权范围内的钓鱼模拟与凭据捕获演练。红线先行：授权与钓饵范围书面确认先行、教育/生产场景默认禁用（设置显式开启）、捕获凭据按敏感数据处置（不入 findings 正文）；依赖=红队模式（E16 本节）落地后评估。

**shell 管理页（暂未开发）**：被攻陷目标的会话化管理视图=战果 shell 清单+经网关命令通道+全程审计；**复用既有会话设施（直播间页签/run 网关/审批）而非独立 C2 框架**，Agent 无裸 shell 约束（§7）不变；依赖=红队模式落地后。

***

## 7. Runtime 执行环境层

````
core/runtime/
├─ detector.py        # 启动探测：Docker Desktop 可达？WSL2？原生工具 PATH 清单
├─ policy.py          # threat_class → allowed_runtimes；NET_MODES none/fakenet/real
├─ gateway.py         # run(cmd, runtime) 服务端校验入口（Agent 不可绕过）
└─ backends.py        # 四后端实现：host / wsl / docker / sandbox（sandbox_docker_args 出 L3 加固参数）
````

- **分层降级策略**：有 Docker 用容器（题目交互、Linux 工具），无则降级 Windows 原生（nmap/sqlmap 原生版、IDA/x64dbg 本就 Windows 优先）。
- 探测结果生成**能力清单**注入 Agent 系统提示（"你知道自己有什么工具"）——Agent 不许自己猜环境。
- 场景映射：

| 场景            | 环境                                                             |
| --------------- | ---------------------------------------------------------------- |
| rev: PE 分析    | Windows 原生（Ghidra headless / idat）                           |
| rev: ELF 分析   | Docker 或 WSL2（Ghidra 跨平台可原生）                            |
| pwn: 题目交互   | Docker（必须）：题目容器 + 工具容器同 network，pwntools 宿主连接 |
| pwn: 动态调试   | Docker 内 gdb / Windows 原生 x64dbg（PE 题）                     |
| web/crypto/misc | 原生 Python 环境                                                 |

### 执行网关与隔离等级（恶意样本防线）

> **落地状态**：L0 host / L1 wsl / L2 docker 三后端、policy 解析（unknown 缺省按 malware_live 只允许 sandbox、默认 net=none）、执行网关校验与审计均已落地。**L3 ◐ 部分落地**：`backends.sandbox_docker_args` 已实现无宿主挂载、降权、CPU/内存/进程限额，net=none 可用；**fakenet（INetSim sidecar）未实现，显式抛 NotImplementedError**，一次性销毁与样本 `docker cp` 投递随 L3 完善——故 malware 轨后置（Phase 3）。

**核心规则：WSL 的信任级别 = 宿主机**（默认挂载全部盘符、共享网络栈、凭证可达），不可信代码绝不跑在宿主或 WSL，只允许 Docker 容器。

**隔离等级：**

````
L0  host-native   宿主原生     只跑本项目自身代码、静态分析适配器
L1  wsl           WSL2         半可信工具（与宿主同级，不跑不可信代码）
L2  docker        普通容器     不可信代码默认环境（pwn 题目、环境复现）
L3  sandbox       加固沙箱     活体恶意样本专用
````

**L3 相对 L2 的加固**：无宿主目录挂载（样本以副本经 `docker cp` 投递，容器不可见宿主 FS）、无真实网络（见下）、seccomp/AppArmor 默认 profile、CPU/内存/进程数限额、**一次性生命周期**（每任务新建、销毁即焚、绝不复用——防样本驻留持久化）。

**沙箱网络模式**（L3）：

````
net: none        完全断网，纯行为观察
net: fakenet     默认。隔离 bridge 网络 + INetSim/FakeNet sidecar 假服务（DNS/HTTP/SMTP），
                 诱导样本回连"假 C2"，全部流量被记录用于 IOC 提取
net: real        真实外网——永不默认，须人工显式批准
````

**强制执行（四层纵深，不依赖 prompt）：**

````
① 策略声明：任务/样本带 threat_class（untrusted | malware-live）
            → policy 解析 allowed_runtimes（如 malware-live → 仅 L3）
② 执行网关（核心）：Agent 无裸 shell，所有命令经 run(cmd, runtime) 网关；
            Agent 填 runtime 参数，服务端按策略校验，越权 → 拒绝 + 审计事件
③ 能力清单：系统提示只呈现被允许的 runtime（体验优化层，非防线）
④ 审计：events 中每条命令带 runtime 标签，拒绝事件触发 UI 告警
````

- **执行面设计**：单网关 + 参数选 runtime（Agent 按场景选，服务端强制校验），不采用每 runtime 独立工具。
- **人类命令同层**（E4a 定稿，§12）：human 的一次性命令同样经执行网关（`gateway.run(..., author="human", threat_class="trusted")`），审计与审批策略与 Agent 同层生效——人侧命令不落审计等于审计流有旁路，不设任何绕过 gateway 的执行端点。
- **未知样本默认按恶意处理**（L3 + fakenet）；静态分析（反编译/字符串/哈希）可在 L0/L1 执行；安全默认值，宁严勿松。
- 预留：Windows 恶意样本（Wine 容器 / Windows VM）走同一策略框架，Phase 3 实现。
- **已知缺口（2026-09-13 记录，暂不修）**：host/wsl runtime 的 backend 不接收 net 参数（`_dispatch` 只传 cmd+timeout），即 L0/L1 + 默认 bridge 事实上可访问任意网络且不经 net 审批。pentest 对授权目标的 HTTP 访问应显式走 `net=real + approval_id`（演练脚本已按此约定）。

L3 沙箱编排（生命周期、fakenet sidecar、样本 `docker cp` 投递）随 Phase 3 malware 轨完善；当前加固参数与 net=none 在 `backends.py`/`gateway.py` 内，无独立 sandbox 模块。

### 工具目录（tools/）

独立顶层目录，统一管理各类工具（取证、反编译、侦察、利用等）。**分发策略：核心内置 + 其余按需。**

````
tools/
├─ registry.yaml        # 全局索引：可自动扫描生成
├─ core/                # 核心内置工具（随仓库分发，开箱即用）
├─ recon/               # 分类目录（按需扩展：osint/ forensics/ exploit/ crypto/ ...）
├─ exploit/
├─ decompiler/
├─ forensics/
└─ installed/           # 按需安装的实际二进制（gitignore）
     └─ <tool-name>/
          ├─ manifest.yaml   # 平台/runtime、探测命令、安装方式、所需 runtime
          └─ adapter.py      # 结构化封装（可选）：规范化输入输出
````

**工具 manifest 示例：**

```yaml
# tools/decompiler/ghidra/manifest.yaml
name: ghidra
category: decompiler
platforms:
  - runtime: native-win
    probe: "where analyzeHeadless"       # 探测命令
    install: winget|scoop|manual 脚本     # 引导安装方式
  - runtime: docker
    image: ghidra/ghidra:latest
adapter: adapter.py
```

**与其它模块的关系：**

- **detector（§7）**：扫描 tools/ 全部 manifest 逐一探测 → 生成能力清单注入 Agent 系统提示
- **skill 预检**：skill manifest 声明 `required_tools: [...]`，加载时对能力清单预检，缺失则提示引导安装（winget/scoop/pip/docker pull），不阻断
- **分层**：`tools/` = 原子工具封装；`core/tools/`（如 decompiler.py）= 建立在适配器之上的组合服务层
- Agent 调用面：adapter 注册为可调用工具 / shell 命令 / MCP 三种方式并存

***

## 8. LLM 接入层

````
core/llm/
├─ provider.py        # 统一接口: chat(messages, tools, stream)
├─ ark.py             # 火山引擎 Ark —— 第一公民
├─ openai_compat.py   # OpenAI 兼容基类（DeepSeek/Qwen/OpenRouter 免费获得）
└─ routing.py         # 任务→模型映射
````

- Ark 为 OpenAI 兼容协议（`https://ark.cn-beijing.volces.com/api/v3`），继承 openai_compat 基类。
- **模型路由**（配置文件）：

  - `planner` / `skill-improve` → 旗舰思考模型
  - `executor`（主循环高频调用）→ 响应快、**工具调用稳**的模型（工具调用稳定性 > 智力）
  - `classifier`（类别判定、日志摘要）→ 小模型

- Ark 特有注意点：endpoint ID / model name 双配置兼容；tool calling 逐模型实测；长输出（伪码注入）上下文预算管理。
- **模型清单与开窗覆盖**：`GET /api/models` 返回路由默认 ∪ `AVAILABLE_MODELS`（实测可用清单）；人类开窗可 per-session 覆盖 executor 模型（`POST /agents` 的可选 model 参数）；编排开窗保持路由默认不掺入。
- **多供应商管理**：`config/providers.json` 登记供应商数组 `{name, base_url, api_key, models[], enabled}`（文件含密钥，gitignore 排除）。种子两条：`ark-coding`（/api/coding，key 留空→回退 `ARK_API_KEY`/.env）、`ark-plan`（/api/plan，独立配额，coding 限额时的分流入口）。**全局默认 = 第一个启用供应商的** **`models[0]`** **models[0]**；不做分角色路由 UI——`config/llm.json` 保留为文件级覆写（值为裸模型名或 `{provider, model}`）。

  - **模型发现**：设置页「获取支持的模型」→ `POST /api/llm/discover` 先 `GET {base_url}/v1/models`（OpenAI 风格 `data[]`，过滤 Shutdown）；端点 404（ark-plan 实测不支持）则降级为**候选清单最小调用探活**；另支持手填模型名单独测活（`POST /api/llm/test-model`）。发现结果由用户勾选保存，`models[]` 有序、首个即该供应商默认。
  - **选择与切换**：直播间开窗两级下拉（供应商→其勾选模型，缺省=全局默认）；**在跑会话可动态切换**（`POST /api/agents/{sid}/llm`）——直接替换 AgentSession 的 provider 引用，下一次 LLM 调用生效，落 `llm.switched` 审计事件；不用重开窗口。
  - API 读出 key 一律脱敏（`has_key` 布尔），保存时空串=保持原 key；至少保留一个启用供应商，否则 Agent 端点 503。


***

## 9. 逆向领域集成（IDA/Ghidra 双通道）

统一抽象，Agent 与工作台不感知差异：

````
core/tools/decompiler.py
├─ decompile(binary, func?) → 伪码      # headless 批量 / MCP 点查，同一格式
├─ list_functions(binary) → 符号表+地址
├─ xrefs(func) → 交叉引用（callers+callees，导入函数地址为 null）
└─ annotate(binary, name, comment) → 写回   # Agent 分析结果写回数据库
````

**工具桥分级（P1/P2 均已落地）**：

- **P1**：① 样本上传后**自动 headless 全量首跑**（零 LLM 后台 Job `binary-triage`；无后端→结构化 `no-tool` 降级而非 500）；② 样本条 IDA/Ghidra/MCP **三态灯**（installed 青 / 仅缓存可读 琥珀 / off 灰）；③ 伪码只读展示 + **库级「IDA 打开」**（detached 启动 GUI 打开分诊产出的 .i64）；④ **x64dbg 下断脚本档**（纯前端模板：`bp` + `SetBreakpointLog`，64 位 rax/rcx/rdx/r8/r9/rsp、32 位 e 前缀，零插件不触网；日志人工导出后回流为动态验证凭据）；⑤ AI triage **只发被动任务**（task_type=triage），不投 Job、不开 Agent。
- **P2**：  
  ① **字符串进 v3 契约**（`strings[]`：address/string/length/type/refs，函数浏览器第三 tab，点引用跳函数）；  
  ② **IDA 精确跳地址**（`POST /open?addr=0x..`：db 同目录写 `_jump_<sha8>.py` 调 `idc.jumpto`，Popen 以相对无空格 `-S` 名 + cwd=db 目录规避引号坑）；  
  ③ **IDA 双向写回**：func_kb→.i64 走新 headless 脚本 `apply_names.py`（`set_name/set_func_cmt` 后显式 `save_database`，单条失败记 results 不中断；后台 Job `binary-writeback`，成功发事件 `binary.annotated`——payload 只放成功条数，注释全文不进事件；GUI 占用时 `.id0/.id1/.id2/.nam/.til` 锁检测→结构化 `locked`，库缺席→`no-db`，无 IDA→`no-tool`，仅 Ghidra→`unsupported`，均非 500）；.i64→func_kb 走 `binary-pull-names`（**库内重导不删库**：直接对现有 .i64 跑导出脚本，绝不能走删库重建的分诊通道，否则 GUI 手改名丢失；diff 后非自动名 `sub_/nullsub/unk_` 才经 patch_func 拉入 name_history，author=`ida-pull`，事件 `binary.names_pulled` 带改名对）；写回入参 `{items:[{address:int|hex,name?,comment?}]}`（1–500 条，每条 name/comment 至少一个）；  
  ④ **Cheat Engine 脚本档**（纯前端 `ce.ts` Lua 模板：module+offset 断点观察 / 改返回值爆破，与 x64dbg 同档同 Dialog）；  
  ⑤ **MCP 实时桥**（见下「MCP 路线」）。
- **P2 仍不做**：MCP stdio 拉起（仅 http 连本机）、MCP 全量列表替换 headless 缓存、Ghidra 写回、CE 进程附加/内存扫描自动化（仅 Lua 脚本档）。
- **Headless 路线**：IDA `idat` + IDAPython 优先，Ghidra `analyzeHeadless` post-script 兜底；detector 在 PATH 外再扫常见安装目录（`C:\Program Files\IDA*` 等）。IDA 9.3 实测坑（统一 `idat.exe`/`.i64` 位宽判定/ELF `.dynsym`/GLIBC 版本后缀剥离）见 `core/tools/CLAUDE.md`。
- **Windows PowerShell 引号坑**：`powershell -Command` 会挪引号改写 IDA `-S` 参数，网关已用 `--%`（停止解析）固化（`gateway_runner`），细节见 `core/runtime/CLAUDE.md`；`--%` 后 `%` 按 cmd 展开，项目/样本路径约定不含 `%`。
- **MCP 路线（P2 已落地）**：插件 **mrexodia/ida-pro-mcp** vendor 在 `tools/mcp/ida-pro-mcp/`（零 pip 依赖），`scripts/install_ida_mcp.py` 幂等装进 IDA plugins；IDA 内起 streamable-http 于 `http://127.0.0.1:13337/mcp`（Ctrl-Alt-M）。平台只连 loopback（config/mcp.json domains 校验白名单与 url 必须 loopback；无配置时对默认端口懒探活，1.5s 超时 + 3s TTL 正负缓存）。在线时：三态灯 MCP 青；写回优先 MCP 实时写 GUI 当前库（channel=mcp）；缓存缺席的单函数伪码实时取（source=mcp）；函数列表/xrefs 仍以 headless 缓存为主。握手细节/真机工具名/双重 JSON 编码等实测坑见 `tools/mcp/CLAUDE.md`。

**headless 导出 v3 契约**（2026-09-14 P2 升版；IDA/Ghidra 的 `scripts/export_funcs.py` 同步）：

```json
{ "export_version": 3, "binary": "...",
  "meta":   {"arch","bits","endian","imagebase","entry"},
  "sections": [{"name","vaddr","size","perms","entropy"}],
  "imports":  {"<dll|.dynsym>": ["fn", "..."]},
  "functions": [{"address","name","size","calls":[],"pseudocode"}],
  "strings":   [{"address","string","length","type":"cstr|unicode",
                 "refs":[{"func_addr","from_addr"}]}] }
```

- 段熵自算（256 桶），>7.2 供壳判定；增强段（meta/sections/imports/strings）各自 try/except，失败给 `{"error": ...}` **绝不阻断 functions 主路径**。
- 字符串：IDA 走 `idautils.Strings()` + `DataRefsTo` 归属引用函数；Ghidra 走 `DefinedDataIterator.definedStrings` + 引用迭代器。API `GET /binaries/{sha}/strings?q=`（q 服务端过滤、5000 行截断 truncated）；缓存内 address/ref 仍为整数，**出 API 转 hex**。
- 缓存按 sha256 存 `artifacts/decompiler-cache/<sha>.json`；`export_version<3` 读时即删并重导；IDA 库写 `artifacts/decompiler-db/<sha>.i64`，Ghidra 临时工程按调用隔离、用完即删。
- **写回脚本** `tools/decompiler/ida/scripts/apply_names.py`（同目录、同 `idc.ARGV` 传参约定）：in/out JSON，`set_name/set_func_cmt` 后显式 `save_database`；仅对持久 .i64 有意义，故写回只支持 IDA。
- **地址一律 hex 字符串出 API**（JS Number 无法安全表示 64 位地址）——v3 起 func_kb 旧出口（GET/POST/PATCH /funcs、Agent bb_funcs、func.updated 事件 payload）同样全部 hex，入参 int/hex 兼收；headless 超时 900s（经网关审计，定性 trusted **解析**——只解析不执行样本；写回是写平台自己产出的 .i64，同样不触碰样本执行）。

### 逆向复用三层 R1/R2/R3（名字避让 §7 Runtime 隔离等级 L1–L3）

> **〔未实施〕** F 批之后，当前零代码；headless 产物仍为 per-project `artifacts/decompiler-cache`，无 `workspaces/.cache`。

- **R1：同 sha256 内容寻址全局缓存**。headless JSON/.i64 从 per-project `artifacts/decompiler-cache` 提至全局 `workspaces/.cache/`（内容寻址），项目行改为引用——同一个样本在 N 个项目里只付一次 900s 全量导出。失效 = 缓存件缺失即重导；项目删除不牵连缓存（引用计数/GC 后置）。
- **R2：全局函数库 + 报告导出**。func_kb 支持跨项目查询（同 sha/同签名函数），他项目结论以"他山之石"姿态展示，人审"晋升采纳"进当前项目——与 §4 变更提案通道合流：采纳 = 一条提案，人审后应用。verified 证据链（finding + debug-log/POC + chains）可导出 Markdown 逆向报告。
- **R3：跨 binary 函数签名匹配**。FLIRT 式特征（常量/模式/调用结构 + 伪码规范化哈希）在不同 sha 的 binary 间匹配同一函数（静态连库版本、换编译器、轻度混淆）。支撑 §4.5.5 展望的游戏版本更新后函数迁移、破解外挂子类：新版本拖入即把旧 func_kb 结论按签名映射到新地址，人审确认，不自动覆盖。

***

## 10. 能力包/场景轨清单与里程碑

> 分类学见 §4.5。下表为当前包清单；曾规划的独立 reverse/redteam/iot/ai-security 包不设立——内容分别由 binary 能力包 + 对应场景轨、binary/kb 子模块、misc 包承载。

````
packs/
├─ capabilities/
│  ├─ web/        # src-strike 全量快照 + ctf-web；技能 web-strike-entry / recon-asset-enum
│  ├─ binary/     # ctf-pwn / ctf-reverse / ctf-malware 快照；file-triage / binary-rev / binary-pwn；
│  │              # kb/iot、kb/ics、kb/vehicular 为设备方向知识模块（固件/Modbus/S7/CAN/厂商）
│  ├─ crypto/     # ctf-crypto；crypto-triage
│  ├─ forensics/  # ctf-forensics / ctf-osint；forensics-triage（含流量分析）
│  └─ misc/       # ctf-misc / ctf-ai-ml（技能未实施）
└─ tracks/
   ├─ ctf/        # 弱角色 + triage 分诊技能；task_types=challenge-*；收尾 writeup
   ├─ assessment/ # 5 强角色 + owners/role-rules；任务类型 recon/entry/privesc/lateral…；收尾报告
   ├─ research/   # triage/reverse/analyze/verify 全 passive + reverse-analyst；
   │              # rev-generic 逆向理解工作台（§5.4/§12）；固件/设备/0day 方向随 binary/kb 知识模块充实
   └─ malware/    # 〔未实施，Phase 3〕：L3+fakenet 硬强制，静态/动态角色分离（UI 复用 rev-generic profile）
````

| 阶段                  | 内容                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Phase 1（已完成）** | 核心（黑板、任务协调、runtime、Ark、Agent 循环、Orchestrator）+ 正交结构落地：ctf 轨 × web/binary/crypto/forensics/misc 五包，ctf-skills 为第一代知识库；assessment 轨骨架（src-strike 快照融入、5 角色、owners、task_types 注册表）+ WebUI 基础版；research 轨 rev-generic 逆向理解工作台 P1（headless 全量首跑 + 三栏工作台 + 人机共写 func_kb）。已通过 pwn/rev/web 解题闭环、三会话并行分析 func_kb 无重复劳动、demo_pentest 端到端、旧 domain 自动归位、无工具结构化降级等验收。 |
| **Phase 2（当前）**   | **已落地**：research P2（chains 攻击链 React Flow、MCP 实时桥、IDA 双向写回、Cheat Engine/x64dbg 脚本档、v3 字符串视图、hex 地址全链路）、评估攻击链画布（§12）、Skill 统一提案制前后端、F 批批 1B（机制 1.6 撤回传播 + 1.5 系统私信）。**未完成**：报告生成、角色体系实战打磨、设备方向知识模块（binary/kb/iot、ics、vehicular）随研究目标充实、五个能力包 redlines 补齐；network 能力包视内网内容量评估拆分；作战模式/mission（E16，§6.9 已定稿〔未实施〕§17 19——红队行动与长任务推进随此深化）。                                                                       |
| **Phase 3**           | malware 轨（依赖 fakenet/INetSim sidecar 与样本投递落地，复用 rev-generic 内核）；AI 安全方向进 misc 或视规模拆包                                                                                                                                                                                                                                                                                                                                                                     |

**执行批次与落地状态**（每批结束跑全量测试/构建并同步 CLAUDE.md）：

- **A 批 ✅**：P0 并发修复——租约心跳接线、人类 PATCH 单事务 + 证据并集（机制 1.12）、packs 进程写锁（§5.3/§6.4）。
- **C 批 ✅**：Skill 页后端——kb 本地基线 CRUD/备份/版本/改名引用联动、统一提案通道、路由 breakdown 与 `skill.routed` 事件可观测、vocab（§4）。
- **D 批 ✅**：Skill 页三栏前端（知识树/编辑预览/提案队列/试算器）。
- **E 批 ✅**：评估攻击链画布（`relates_to` 强关系 + FindingsCanvas，§12）。
- **F 批：协调机制实施（§6.7，部分已落地）**——批 1B ✅（机制 1.6 撤回传播全量 + 1.5 系统侧私信）；机制 1.9/1.11 随 G 批 ✅、1.7 的**单任务先遣版**（claimed 步边界取消）已随 A 组 A1 落地。余部（1.1–1.4、1.7 传播本体、1.8、1.10、1.5 的 Agent 自由私信侧）〔未实施〕，顺序见 §17 B 组（机制 1.1→机制 1.4→B3→B4）；每项机制自带并发测试。
- **G 批：项目自主级别 L0/L1/L2（§6.8，✅ 全部落地）**——批 2 配置面/sessions_cap/用量记账、批 3 tick 租约/编排状态持久化（schema v4→v5）、批 4 L1 开窗审批、批 5 L2 全自动链、批 6 L0 提案模式；明细见 §6.8。
- **任务流批次（✅ A 组已落地，2026-09-15）**：A1 detach/终止入口收敛/步边界取消/done 可删、A2 tasks.plan（schema v6）、A3 任务流视图、A4 finding_update 私信、A5 编排器规划-分派（§3/§6.1/§6.4/§12）。机制 1.4 资源租约已随 v7 落地（resource_leases）。
- **F/任务流之后（§17 C–F 组）**：编排语义 C 组（C1/C2/C3/C4/C5）与逆向复用 D 组（R1→R2→R3）并行推进 → development 轨 P1（§15，依赖 B3 blocked_by，含 §15.5，红线先行）→ L3 fakenet/malware 轨（Phase 3）；体验/基建（§17 F 组）可随时穿插。

***

## 11. 技术栈

| 层        | 选型                                 | 理由                                                                   |
| --------- | ------------------------------------ | ---------------------------------------------------------------------- |
| 核心/后端 | **Python 3.11+**（FastAPI）          | 安全生态碾压：pwntools/angr/capstone/scapy/impacket；参考项目全 Python |
| 存储      | SQLite (WAL)                         | 零依赖，Windows 劝退门槛最低                                           |
| 前端      | **React + TypeScript + Vite SPA**    | 本地工具无需 SSR；全功能控制台、可视化天花板高                         |
| 分发      | uv 管理环境，后期 PyInstaller        | Windows 安装体验                                                       |
| LLM       | 火山引擎 Ark 优先（OpenAI 兼容基类） | 用户主力；其他兼容厂商低成本扩展                                       |

***

## 12. WebUI

**页面骨架：**

1. **Workspaces/Projects** — 项目列表：类型徽章、状态、最近活动
2. **New Task 引导** — 选场景轨（单选）→ 勾选能力包（多选，按轨给推荐组合）→ 上传附件 → 环境能力探测 → 创建项目
3. **Agent 直播间** — 思考/工具调用/输出实时流，可暂停插话（核心页）
4. **黑板/工作台视图** — **工作台 profile 驱动**（§4.5.5）：assessment=资产树+漏洞表；**rev-generic=逆向理解工作台**（样本条 + 三栏：函数浏览器｜结论+伪码｜xref/发现/笔记，详见下）；human 添加入口内置于各 profile。黑板 tab=发现/资产/**函数库**，其中函数库 tab 仅项目 capabilities 含 binary 时挂载（func_kb 只由二进制分析产生，web-only 项目不显示空 tab；判据是能力不是轨）
5. **任务看板** — 四态列 + 认领者徽章 + 手动发布；open/failed 卡片内联编辑五字段，failed 一键放回（或保存并放回）；删除＝物理删除（AlertDialog 二次确认；A 组 A1 起**四态皆可删**、有子任务 409；claimed 卡文案「取消」，当前步结束即步边界硬中断，见 §6.4/§3）；claimed 卡显 plan 进度 ▦done/total 与 blocked 原因（A2）；failed 卡按 blocked_reason 区分（E15，§6.1）——awaiting_human 显「⏸ 待人工输入」badge+暂停原因上卡片正文（截断+悬停全文，不藏 tooltip）+「▶ 续跑」（有快照时）与「✅ 已解决，放回继续」（可附注），error 卡维持现状
6. **Skill 库** — 按轨/能力包浏览、技能/角色 CRUD、启停、路由试算器（query+features+文件特征）、角色×技能×任务类型矩阵、pack doctor 摘要；**AI 技能提案队列**（diff 审阅：批准/拒绝/改后采纳，见 §4 全提案制）
7. **终端**〔未实施，E4 已定稿见下「终端页与人类命令通路」〕— workspace 容器/shell（当前无独立视图，直播间底部终端抽屉亦未做）
8. **审批收件箱** — 全局通知 + 待办（安全审批是一等公民，见下）
9. **项目设置** — 授权边界、cross_target 策略、噪声预算、模型路由；作战模式 mode 切换（二次确认+ROE 四要素必填，§6.9）与 mission 编辑器〔未实施，E16 §17 19〕

### 布局定稿：三栏指挥台

````
┌───┬──────────────────────────────┬───────────────┐
│ ◉ │ 项目名 + 审批铃铛（全局顶栏）    │               │
│   ├──────────────────────────────┤ 📋 黑板侧栏    │
│导航│ 直播间（会话页签+状态点）        │ 假设/发现/产物 │
│   │ 09:42 🤔 分析 ELF             │ 标签页可切换   │
│直播间│ 09:45 🔧 checksec            │ + 人机共写入口 │
│黑板│ [输入框________][发送][插话]    │               │
│任务├──────────────────────────────┤               │
│技能│ ▸ 终端抽屉（按需弹出）           │               │
│设置│                              │               │
└───┴──────────────────────────────┴───────────────┘
````

- 左：窄导航栏（项目层各视图 + 设置）；中：直播间主区；右：黑板常驻侧栏（可折叠）；底部：终端抽屉。
- 直播间与黑板同屏共存——挂机监控时随时瞟结论性状态，无需切页。
- rev-generic profile 下右侧栏换成 **384px 逆向挂机侧栏**（风险函数/发现/样本三 tab：覆盖率、三态灯、上传与重新分诊、点击风险函数切全屏工作台）；全屏工作台占满中右区域。

**框架可拖拽调宽（E13，2026-09-16 定稿，〔未实施〕§17 16）**——左导航栏与右侧黑板侧栏宽度可拖拽调整：

- 机制：react-resizable-panels v4（**已在依赖，不引新包**），复用 SkillsPane 三栏模式（Group/Panel/Separator，number=px、字符串=%）与 `HANDLE_CLS` 样式常量（SettingsView.tsx）。
- 左导航：无 pid/有 pid 两分支包水平 Group，`Panel defaultSize={56} minSize={48} maxSize={220}`；两分支逐字重复的 nav JSX 顺带抽取为 **NavRail 组件**（nav 改 `h-full w-full`，按钮 `w-12`→`w-[calc(100%-8px)]`，拖宽后 label 显示更完整）。
- 右侧黑板侧栏：live 视图内嵌套水平 Group（仿 SkillsPane「横 Group 内 Panel 再嵌 Group」先例），LiveRoom `Panel minSize="40%"` + 侧栏 `Panel defaultSize={384} minSize={288} maxSize={640}`（Blackboard compact 与 RevCompact 共用容器自动受益）；折叠钮 `w-5` 与 boardOpen 逻辑不动；**条件 Panel（boardOpen 切换）需实测 Group 对子数变化的容忍度，不行则改常驻 Panel+collapsible**。
- 宽度持久化：localStorage `ui.nav-width` / `ui.board-width`（**设备级偏好不按 pid**；defaultSize 初值 + 布局回调 debounce ~300ms 写入，回调 prop 名以 v4 类型定义为准，try/catch 包裹）；boardOpen 维持不持久化。
- 坑：Group/Panel 一路 `min-h-0`/`h-full` 不可断（高度链）；主区 Panel 保留 `min-w-0`（事件流虚拟滚动/任务流 React Flow 均 flex 自适应无硬 min-width，拖窄不破）。

### rev-generic 逆向理解工作台（P1 已落地）

中心对象是「**样本 → 函数 → 结论**」而非 host→service→url。全屏布局：

````
┌ 样本条：[样本下拉 ▾] sha16  arch/64bit/le  壳嫌疑  N函数·N串·覆盖X%·N风险  IDA● Ghidra○ MCP○  [上传][重分诊][⬇同步改名][AI triage]
├ subnav：[逆向分析 | 攻击链] ───────────────────────────────────────────────────────────────┤
├──────────────┬─────────────────────────────┬──────────────────────────┤
│ 函数浏览器 288 │ 结论+伪码（flex-1）           │ xref / 发现 / 笔记 Tabs 320│
│ [搜索 名称/地址]│ ■ 函数名 @0x.. size 置信度    │ callers → callees（可跳）  │
│ ⚑ 风险函数     │ [复制地址][IDA打开(跳址)][⛓入链]│ 关联发现（类别/severity）   │
│ ✓ 已分析       │  [脚本档 ▾: x64dbg / CheatEng]│  「传日志确认」→ verified  │
│ # 全部（虚拟滚动）│ 人工结论（func_kb analysis）  │  「⛓ 加入攻击链」         │
│ [函数|导入表|字符串]│ 改名史  [⬆写回.i64]       │ 笔记：改名/风险标签/笔记    │
│              │ 伪码 pre（只读，深底；MCP在线可实时取）│                    │
└──────────────┴─────────────────────────────┴──────────────────────────┘
````

- 数据层：左/中栏来自 headless 缓存（客观全量），人工结论/改名/标签来自 func_kb（join 显示，kb 名优先）；4s 轮询 + WS 事件即刷新。
- **人机共写纪律**：改名/笔记只写 func_kb（confidence 人不动）；无 kb 行时先 POST 建行人再 PATCH。
- **IDA 双向写回（P2）**：[⬆写回 .i64] 把 kb 改名（可选笔记首段作函数注释）经 headless apply_names 推进 .i64（Job；GUI 锁着→locked 提示，MCP 在线则实时写当前库）；样本条 [⬇同步改名] 重导 diff，把人在 IDA 里改的名拉回 name_history（author=ida-pull，自动名不拉）。
- **脚本档（纯前端、零插件、不触网）**：x64dbg 下断日志档 + Cheat Engine Lua 档（module+offset 断点观察 / ret 改返回值爆破，32/64 位寄存器前缀自动）；平台全程不执行样本。
- **动态验证闭环**：x64dbg 脚本档 → 人工在自己的调试器执行 → 导出日志 → 发现卡「传日志确认」上传（artifact kind=debug-log）→ finding 自动转 verified（`confirm_by=dynamic` + 日志 artifact id）。
- 无 headless 后端：样本条三灯全灰、函数栏显「尚未完成 headless 分诊」、[重新分诊] 可随时补跑；AI triage 按钮在缓存就绪前禁用；MCP 在线时单函数伪码可实时取（source=mcp）。

### 终端页与人类命令通路（E4，2026-09-16 定稿，〔未实施〕§17 13）

「人类直接操作终端」是 E4 首次引入的全新执行面；定稿原则：**人侧命令必须过执行网关（审计无旁路），交互终端是人类专属（Agent 工具面不新增 PTY 工具），分期交付（E4a 面板先行、E4b 交互终端后置）**。

**E4a · 命令面板（one-shot，先期批）**

- `POST /api/projects/{pid}/exec`：body `{cmd, runtime, net?, approval_id?, timeout≤600s}` → 线程池执行 `gateway.run(cmd, runtime, author="human", threat_class="trusted", session_id="human")` → 同步返 ExecutionResult；`GatewayDenied` = 403 + reason 原文（网关已落 `command.deny` 审计事件）。
- 审计/审批/net 策略全部在网关层原样生效：`command`/`command.result` 事件落黑板，直播间事件流自然可见人类命令行。UI 不暴露 threat_class（无可信级伪造入口）。
- **诚实降级**：host/wsl runtime 下 net 控件禁用并标注「该 runtime 不受网络策略约束」（对应 §7 已知缺口：host/wsl backend 不消费 net 参数），不做假开关。
- 历史回放：面板打开时拉 `author=human` 的 command 事件 + localStorage 最近 N 条。

**E4b · 交互终端（后置批，依赖持久容器基建）**

- **持久 workspace 容器** `cyb-ws-<slug>`：`docker run -d` 挂载 `workspaces/<slug>` → `/workspace`，默认 `--network none`；**首次使用惰性创建 + 空闲 30min 回收 + 项目关闭 stop / 项目删除 rm -f**；容器 id 记 project.json `runtime.tool_container` 字段（服务端为真源），重启 reconcile 对账孤儿容器。无 Docker 环境零感知（不随项目创建拉起）。
- **PTY 三后端**（`core/runtime/pty.py` 新建，`PtyBackend` 协议 spawn/read/write/resize/kill）：pywinpty（Windows ConPTY）、标准库 pty（POSIX）、DockerExecPtyBackend（`subprocess.Popen` 流式接 `docker exec -it <ctr> bash`——现有 `exec_in` 是阻塞版，此为其流式扩展）。
- **双向 WS** `WS /api/ws/projects/{pid}/term?sid=&shell=&token=`：客户端→服务端 JSON 文本帧 `{t:"stdin"|"resize"|"ping"}`；服务端→客户端 PTY 输出**二进制帧透传**（保 TUI 程序不破版）+ 15s JSON 心跳。不复用单向事件 WS（`/api/ws/projects/{pid}`），避免污染重连语义。
- **会话注册表**：sid→PtySession；最后一端断开宽限 5min、空闲上限 15min 强杀；每项目并发 ≤2；Windows 句柄与 docker exec 进程必须显式 kill 并 reap。
- 前端 xterm.js（`@xterm/xterm`+`addon-fit`）**仅在 TerminalPanel chunk**（React.lazy 深一层，仿 TaskFlow/ChainView 先例）。

**安全边界（定稿，不可放松）**

1. 人类命令必须过 gateway；API 层不新增任何绕过网关的执行路径。
2. E4b 交互终端为**人类专属**；Agent 工具面（tools.py 白名单）不新增任何 PTY 工具，Agent 唯一命令口仍是 `run_cmd`。
3. **host 交互终端默认关闭**（settings 显式开启 + 仅 127.0.0.1 绑定可用）；**WSL 终端不提供**（信任级=宿主机，做了等于 host shell 还制造半隔离错觉）；E4b 首发主推 docker 目标。
4. PTY 内容**不做逐键审计**（量级大且含凭据明文）；以 session 级 `terminal.open/close` 事件（shell 类型+容器 id）代替，后续如需可 opt-in 容器内 `script` 录制。
5. 不可信代码不因终端新增暴露面：容器默认 `--network none`，挂载共享面与现有 run_once 一致；无 Docker 只禁终端、不提供替代性直接 shell。

**前端形态**：直播间底部抽屉（浮层覆盖事件流上方、可拖拽高度、默认收起；**不与 viewMode=flow 互斥**——flow 图上跑命令查 finding 是高频动作）；同 lazy 组件顺手暴露独立路由复用。

**拆批**（依赖链 B3→B4，机制 1.1 可独立交付验收主体）：

| 批 | 内容 | 量级 |
|---|---|---|
| 机制 1.1 | E4a 命令面板：POST /exec（gateway 接线 author=human）+ CommandPanel + 抽屉壳 + 历史回放 + 策略矩阵单测（越权 runtime/net=real 无审批/deny 落审计） | 2–3 天 |
| 机制 1.4 | 独立路由复用组件、命令补全、快捷键 | 0.5–1 天 |
| B3 | 持久 workspace 容器基建：WorkspaceContainerManager（ensure/stop/reconcile/idle reaper）+ project.json runtime 段 + container 端点 + 无 Docker 降级测试（独立于 机制 1.1/机制 1.4） | 3–4 天 |
| B4 | E4b 交互终端：pty.py 三后端 + term WS + 会话注册表 + TerminalPanel（xterm lazy）+ host 终端 settings 开关 | 4–5 天 |
| B5 | 收口：session 级审计事件、审批 UI 打通（面板内发起审批跳收件箱）、fakenet sidecar 挂点预留（与 D2 合并评审） | 1–2 天 |

### 攻击链视图（逆向链，P2 已落地）

subnav 切到「攻击链」全屏替换三栏（分析状态不卸载）：左 260 链列表（新建/状态色点 hypothesis 琥珀·validated 青·exploited 紫/删除二次确认），右 React Flow 画布——节点按 seq 横排（节点宽 200、步距 370＝200+170 间隙；间隙必须容得下边注卡，早期 56/96 时长边注压节点、节点屏幕重叠），三类实体异形卡：ƒ 函数（青边+hex 地址）、🔍 发现（severity 色条+五类 category chip）、📎 产物（kind）；边标签=edge_note，用**自定义 edge**（getBezierPath+BaseEdge+EdgeLabelRenderer portal 出 150px 限宽两行 HTML 卡片、全文悬停可见；默认 edge 的 label 只走 SVG `<text>` 不换行，长中文边注不可用），双击边编辑（卡片 pointer-events-none 不拦截），fitView 上限 zoom 1；末节点虚线「+」弹实体选择（函数知识/发现/产物三 tab 搜索）；节点点击回跳分析视图定位（函数→中栏，发现→右栏发现 tab）。函数详情与发现卡均有「⛓ 加入攻击链」（选链或新建）。链数据经 `/api/projects/{pid}/chains` 全套端点，节点快照在 API 层组装，store 层保证节点真实且属本项目。

### 评估攻击链画布（已落地；与上节逆向链视图并列、另起一个视图）

逆向链视图以函数/产物/发现异形实体讲"样本内部因果"；评估轨要的是"网络位面的横向关系"。两张图**共用 chains/chain_links 表与全套端点**，但前端组件另建（不改 reverse/chains/，依赖只有 @xyflow/react 已在仓库）：

- **画布结构**：每个 host IP 一条**泳道**（资产做泳道头，不占节点；无 host 归属进"未归属"泳道）；泳道内按 severity 分**四列**（info/low｜medium｜high｜critical，2026-09-15 由三列拆开）+ 时间堆叠。筛选三件套（IP/严重度/状态）列表与画布共用。
- **三级边语义**：

  - **弱边**（灰虚线，默认开可关）：同 IP 泳道内同 vuln_class（或同父任务）且严重度升级方向——机器猜测，仅供联想，不落库；
  - **强边**（青实线）：Agent 上报 `evidence.relates_to:[{finding_id,note}]`，内嵌 finding 自身证据 JSON（不建表、不升 schema）；另加引用存在性 + 同项目校验防幻觉（非法 422，严格不静默丢）；
  - **链边**：人把强边/手拖边**确认进命名 chains**（复用现成 `POST /chains/{cid}/links`，node_type=finding、edge_note 必填），按链状态着色：hypothesis 黄 / validated 蓝 / exploited 红。

- **卡片**：severity 色条 + 标题 + vuln_class + 状态角标（✓ verified / 虚线框 unverified / ✕ false-positive）+ POC 角标；紧凑卡 truncate 两行；点击复用现有 POC 详情弹窗（复现+POC 一键复制）。
- **人工确认/补连**：Handle 拖线 → 小对话框（选已有链或「新建攻击链」命名+goal，必填边理由 edge_note）→ 现成 addChainLink；强边可点"加入链"快捷确认；选中边可删（deleteChainLink）；节点拖动位置按项目存 localStorage（不入库）。
- **链工具条**：链下拉（名+goal+状态灯）、新建链、状态流转按钮（hypothesis→validated→exploited，走 updateChain）、minimap/缩放/全屏。
- **入口**：黑板发现 tab 内 `[列表｜链路]` 子视图切换，**仅 assessment track 显示**（与资产树显示条件一致）；compact 侧栏模式不渲染画布。findings 4s 轮询 + WS bump。
- **chains 表语义澄清（两张图共用一表）**：逆向链视图挂函数/产物/发现异形实体；评估画布只挂 finding；chain_links 是"确认后的强边集合"，seq 保留但画布不依赖线性序——一条链可分叉/汇聚。

### 直播间任务流视图（任务拓扑图，A 组 A3 已落地）

> 2026-09-15 任务流设计。直播间内与"直播"平级的「直播｜任务流」切换，是继逆向链视图、评估画布之后的**第三个 React Flow 图**（React.lazy 分包）；数据由 `GET /projects/{pid}/task-graph` 供（`core/blackboard/graph.py`，set-based SQL 无 N+1），全部来自既有 tasks/session_inbox 与 `tasks.plan` 列，**不另建表**。前端实现见 `webui/src/views/live/CLAUDE.md`。

- **节点 = 一个子任务**，节点卡只显示：子任务目标（objective 截断）、计划进度（plan n/m）、当前 doing 步骤、blocked 原因、认领它的会话、状态色——**open 灰 / claimed 青 / done 绿 / failed 红 / paused 琥珀**。编排器（主代理）安排的任务与子代理为解决该任务写下的计划（§6.1 先规划后动手）都在节点上呈现，不展示会话流水。
- **边**：
  - **实线 = 父子关系**：经 tasks.parent_id，父任务与它分解出的子任务相连，左→右 DAG 布局；
  - **普通虚线 = 实际私信**：两任务认领会话间在 session_inbox 有私信（含 basis_stale/finding_update/未来的 bb_notify），可点击查看私信记录；
  - **点虚线 = 建议私信**：同父任务、或 context_refs 高度相交却尚无私信的两个节点，提示"这两个子代理很可能需要交流"。建议边是机器猜测、**不落库**（相交判定待机制 1.1 workset 落地后加入）。
- **节点持久**：任务 done/failed/会话结束后节点**不消失**，任务流一直保存，供复盘；用户可手动删除节点（＝删除该任务，四态均可删、claimed 删除即硬中断，AlertDialog 二次确认，见 §6.4）。
- **双击节点**：在直播间打开/聚焦对应会话页签——若页签已 detach（关页签不关任务，见 §3）即由此挂回；open 无人认领的任务则跳到任务看板对应卡片。
- 布局：parent 层级左→右 DAG，节点拖动偏移按项目存 localStorage（`taskflow-offsets-v1:<pid>`，不入库）；3s 轮询 + WS（task.*/message.inbox/session.* 事件 300ms 去抖重拉）。

### 时间显示约定（2026-09-15 已落地，纯前端）

- 存储一律 UTC ISO 不变；**展示统一转本地时区**（跟随浏览器 `Intl.DateTimeFormat().resolvedOptions().timeZone`，用户机即 Asia/Shanghai），格式固定 `YYYY-MM-DD HH:mm:ss`（年-月-日-时-分-秒；窄列用 `YYYY-MM-DD HH:mm` / `YYYY-MM-DD` / `HH:mm:ss` 变体）。
- 全 UI 时间渲染只准走 `webui/src/lib/datetime.ts` 同一个 formatter（`fmtDateTime/fmtDateTimeMin/fmtDate/fmtTime` + `utcTitle`），**禁止各处 slice/replace 手拼**；鼠标悬停 title 显 `UTC YYYY-MM-DD HH:mm:ss` 原值核对。
- `parseTs` 兼容后端三种时间形态：naive 形 `2026-09-15T11:31:22`（blackboard now()，按 UTC 补 `Z`）、带偏移形 `…+00:00`/`…Z`（原样解析）、packs 历史版本号紧凑形 `20260913T145750Z`。排序/计算仍用 UTC 原串（localeCompare / parseTs 取 epoch），写入端不本地化。
- 2026-09-15 已覆盖全部展示点：直播间事件流与会话 age、审批卡、黑板发现行与发现详情、项目卡日期、设置提案 created/decided、func_kb 改名史、packs 历史版本列表。

**多会话组织：页签 + 状态点（顶栏两行化）**——直播间顶栏分两行：第一行=会话页签（S1/S2/Orchestrator…，多了横向滚动不换行）+ ●live 状态；第二行=动作配置行，开窗三下拉（角色/供应商/模型）**常驻直接铺开**（不用 popover 收纳，角色长描述截断+悬停全文）+[开窗]，右侧会话控制组（跑任务队列/暂停-恢复-中断/**结束会话**，随会话状态切换）/🔀运行中切模型/编排一轮/**重排优先级（A5 手动，任何档可跑）**/复盘沉淀，窄屏兜底换行。页签带状态色点（运行/空闲/等待审批/异常）；**页签 ×=detach**（只收起不结束，后台照跑，「⊟ 已分离」下拉挂回），未选中会话后台继续跑；页签有新审批/私信或异常时红点变色提醒。

**会话控制与思考流**：事件流每 LLM 步落 `llm.thinking` 思考行——折叠显示一行摘要、展开看思考全文（Claude Code 式），另有「思考」过滤页签。

**审批收件箱**：越界审批、net:real、cross_target=ask-human 等待批动作统一进全局铃铛 + 待办列表；每条展示"请求者/动作内容/风险等级"，批准-拒绝-追问三键，全程记审计。审批不埋进直播间滚动流。

### 视觉风格定稿：克制黑客风

- 深灰底 + 单一强调色（已定：青）；状态色语义化（运行=青、审批=琥珀、异常=红）。
- **字体分域**：等宽字体只用于数据区（日志、命令、地址、哈希），界面区用无衬线。
- 质感目标：Linear/Vercel 级专业感；拒绝 CRT 扫描线/荧光绿等重度装饰。

### 前端工程选型

| 项       | 选型                      | 用途                            |
| -------- | ------------------------- | ------------------------------- |
| 框架     | React + TypeScript + Vite | 纯 SPA（本地工具无 SSR 需求）   |
| 组件库   | Tailwind + shadcn/ui      | 组件源码可控，深度定制风格方便  |
| 图可视化 | React Flow                | 攻击链图、（Phase 3）横向路径图 |
| 虚拟滚动 | TanStack Virtual          | 直播间长事件流、大资产表        |

### 直播间实现要求

- 虚拟滚动（长会话数千事件）；事件分层折叠（工具输出默认折叠，思考/决策默认展开）；按类型过滤（只看决策/只看命令）；WS 断线重连后增量回放。
- **事件流类型筛选（2026-09-15 v0.10）**：类型 tab = 全部/思考/决策/**路由**/命令/发现，定义在 LiveRoom `FILTERS` 常量、纯前端按 `kind` 匹配（无服务端过滤）。「路由」= `skill.routed`（任务入口技能路由审计，`core/agent/loop.py` `skill_context_for` 落事件）：命中与未命中都进该 tab，未命中条目摘要自带「未命中 · query」样式区分（前端渲染见 `lib/events.tsx`）。既有多归属：`finding.new` 同时命中决策与发现两个 tab。
- **开窗/编排的轨驱动交互**：开窗 = 角色下拉（`GET /api/projects/{pid}/roles` 取项目所属轨的清单）+ 开窗按钮；编排一轮 = 角色多选 popover（空选 = allowed_roles 不限）+ 开始。track/capabilities 为内部英文 slug，UI 中文显示名经 `lib/taxonomy.ts` 映射（ctf→CTF、assessment→授权评估；web→Web、binary→二进制…），旧 domain 值在展示层兼容映射。

### 待定项（Open Questions）

- [ ] 浅色主题是否提供（低优先级；强调色已定＝青）
- [ ] 报告生成：黑板数据 → 渗透报告初稿的自动化程度（依赖 verified 状态与证据链标准，Phase 2 细化，随 R2 报告导出推进）

***

## 13. 项目目录结构（目标形态）

````
cyberstrike-pro/
├─ DESIGN.md
├─ core/
│  ├─ agent/            # Agent 循环、规划/执行双上下文
│  ├─ blackboard/       # 黑板服务：读写、任务队列、去重、事件广播
│  ├─ runtime/          # detector / native_win / docker / wsl
│  ├─ skills/           # 注册表 + 路由器（角色先窄化，路由器窄域匹配）+ doctor + task_types 注册表
│  ├─ tools/            # 组合服务层：decompiler.py 等（建立在 tools/ 适配器之上）
│  ├─ llm/              # provider / ark / openai_compat / routing
│  └─ orchestrator/     # 主代理：监控、派生、开窗、汇总
├─ packs/               # 能力包 × 场景轨（§4.5/§10）
│  ├─ capabilities/     # web / binary / crypto / forensics / misc；各含 pack.yaml + skills/ + kb/ + kb_sources.json + rules/
│  └─ tracks/           # ctf / assessment / research 已落地（malware 后置）；各含 track.yaml + roles/ + task_types.yaml + rules/ + skills/
├─ tools/               # 工具目录：registry + 分类子目录 + installed/（§7）
├─ webui/               # React SPA
├─ workspaces/          # 项目目录（布局见 §5.3）：project.json + blackboard.db + samples/ + artifacts/
└─ docs/
````

***

## 14. 开发约束

**分目录文档树（强制）**——为了让任何新会话能快速接手：

- 每个大文件夹（`core/`、`packs/`、`tools/`、`webui/`、`workspaces/`、`docs/` 及其一级子目录）必须有中文 `CLAUDE.md`（≤80 行：职责、入口文件、关键约定、坑），Claude Code 会在会话触及相关目录时自动加载。
- 新建大文件夹时同步创建其 `CLAUDE.md`；对某文件夹做实质功能修改（新增/重构/改接口）时，同一次工作中必须更新该目录的 `CLAUDE.md`。
- `CLAUDE.md` 只写"接手需要知道的"；设计推导与决策的唯一真相源是本文件（DESIGN.md），两处不得冲突。
- 完整约束规则见根目录 `CLAUDE.md`。

**其它硬约束**（正文各节已详述，此处速记）：

- 黑板写操作必须经 core API 单一入口（§5）。
- 不可信代码只在容器执行，WSL 信任级 = 宿主机；Agent 无裸 shell，命令一律经执行网关（§7）。
- 安全默认值，宁严勿松（§7：未知样本默认按恶意，L3 + fakenet）。

***

## 15. development 场景轨

> **〔未实施〕** F 批之后做 P1，当前仅设计；仓库已 `git init`（2026-09-15，代码提案制前置已就绪）。

### 15.1 定位

产物 = **代码 artifact + git 提交 + 测试记录**的开发场景：EXP/工具脚本开发、游戏辅助脚本与 mod 开发、平台自举（dogfooding）。与 §4.5.5 逆向子 profile 的分工写明：**dev 轨管"造"，reverse profile 管"析"**——逆向结论（函数地址、协议、校验算法）作为开发任务的输入，实现/构建/测试在 dev 轨闭环；工作台以后加 `dev` profile（§4.5.5 的正交第三维），后端通用 API 不分叉（复用 artifacts/任务/提案/chains 内核）。

**合规红线（定稿，不可被审批放松）**：仅限**单机游戏、自有账号/自有环境或授权研究**；禁止联网对战作弊、绕过反作弊/付费校验、影响他人的内容。构建/试运行走 host runtime + 审批（游戏辅助不进 docker——GUI/输入钩子需要宿主会话，审批正是把关口）；红线文本进 track redlines 与游戏类 kb 模块，escalation 不可放松此硬底线。

### 15.2 角色与任务流

- 角色草案：**analyst**（需求/逆向结论转开发任务）→ **developer**（实现）→ **builder**（构建）→ **reviewer**（审校/测试签收）。
- task_types 注册表：`implement / build / test / review`；强依赖工作流——**test 通过才允许 done**，门控用机制 1.2 blocked_by 原语（implement→build→test→review），不新增任务状态；测试失败由 reviewer 打回 = 新建 implement 任务重建依赖链。
- 依赖链同时是 map-reduce 的小试场：一个工具的多个平台适配可 shatter 成并行 implement 子任务，聚合 build 任务 blocked_by 全部分片（机制 1.3）。

### 15.3 代码提案制与 git 安全网

- 仓库已 `git init`（2026-09-15）。统一"变更提案"通道：packs 内容与代码 diff 走同一审批/改后采纳模型（§4 proposals 扩展 target kind=code；packs 提案直接备份后应用，代码提案必须走分支）。
- **AI 应用代码提案 = 分支提交**，人审合并；Agent 无裸写工作树；commit/push 只在人类明确要求时发生。
- 开发产物（脚本/mod/EXP）进项目 artifacts，git 提交 hash 记进任务 result_note 与事件，测试记录（命令+输出）随 review 任务归档，形成"需求→实现→提交→测试签收"完整审计链。

### 15.4 工作台（dev profile）

文件树 + 代码编辑器 + 构建控制台 + 提案 diff 视图四件套；构建事件进直播间流，测试失败卡片可一键"打回为 implement 任务"。复用现有 run_cmd 网关与审批收件箱，不新建执行通道。

### 15.5 逆向开发专项方向：AI 反代 / AI 破甲（任务流设计新增，〔未实施〕）

> 随 development 轨落地，走"reverse profile 析 → dev 轨造"的分工；红线文本先行进 track redlines，escalation 不可放松。

**AI 反代**＝把一些免费 AI 聊天 Web 窗口逆向封装为 OpenAI 兼容 API，供个人自用接入：

- 技术链：抓包分析登录保活、请求/流式响应协议 → func_kb/findings 记录协议结论 → dev 轨 implement/build/test/review 产出反代服务（`/v1/chat/completions` + SSE 流式兼容）；网络访问属 active 动作，走执行网关 + 审批（§7）。
- **红线（不可被审批放松）**：仅限**自有账号、个人学习与自用**；禁止公开部署、转售或对外提供共享公共服务；**禁止对抗验证码、风控、速率限制与封禁规避**（不做人机验证绕过、不做账号池/批量注册号）；尊重服务条款，收到封禁或法律停止信号即停；不接触他人数据。

**AI 破甲**＝让 AI 完成它通常会拒绝的任务——在本平台定位为 **AI/LLM 安全红队评估**（与 §10 Phase 3「AI 安全方向进 misc」合流）：

- 仅对自有/自建靶标或持有书面授权的系统进行；产物是**弱点证据 + 复现步骤 + 加固建议**评估报告（走 R2 报告导出），任务流同 dev/assessment：分析→探针→证据→报告。
- **红线（不可被审批放松）**：① 禁止对第三方在产公共服务做绕过测试；② 探针以证明安全缺口为度，不得生成违法现实危害内容；③ 不公开分发越狱工具、不批量打服务；④ 全程审批 + 审计 + 授权书留档。

***

***

## 16. 情报面板（Intel，2026-09-16 定稿，〔E9/E10 已实施 2026-09-16〕）

> 跨项目的**全局**学习/情报模块，与项目黑板无关（数据不进任何 blackboard.db）；覆盖三块：每日漏洞简报、优质技术文章推送、个人知识库（Obsidian）接入与学习计划。

**架构基线**（全部复用现有底座模式，无先例处已标注）：

- **全局存储**：`config/intel/` 目录——`intel.db`（sqlite WAL：文章池/简报归档/学习档案）、`feeds.json`（源清单，人可编）、`profile.json`（兴趣画像：七方向 web/AI安全/车联网/逆向/Android/PWN/取证 的权重 + 学习阶段声明）。全局 DB 为新增模式（现全局配置仅 `config/*.json`；`packs/.proposals/` 是全局 JSON 存储的现成范本）。
- **抓取**：触发式 Job（仿 review-proposals 的 JobRegistry 线程），**不引入常驻 scheduler**——打开情报页时若今日无简报自动补跑一次 + 手动「刷新」按钮。出站 HTTP 走标准库 urllib（仿 `providers.py _http_get`，可注入 getter 供测试），**不经执行网关**（平台自身可信出站，与 Agent 命令通道无关）。
- **LLM**：用 **classifier 小模型路由**（现网闲置）做打分与摘要，只喂标题 + RSS 摘要段（每条几百 token，成本可控）；日报合成同 classifier。打分 = 方向相关性（profile 权重）× 技术深度。

### 16.1 漏洞简报（E9）

- 数据范围（定稿：**CVE + 中文社区混合**）：结构化漏洞源 NVD API / GitHub Advisory / CISA KEV（**已在 KEV / 有公开 POC 打优先标**）+ 中文安全社区热点（先知/FreeBuf/安全客等 RSS）。
- 产出：每日一份中文简报（新 CVE 段 + 社区热点段，按用户方向相关性排序），LLM 合成，存 intel.db 归档可回看；**项目列表页右栏渲染今日简报摘要卡**（几条要点 + 「查看全部」跳情报页）。

### 16.2 高分文章（E9）

- 文章池：RSS 全量入池（URL 去重），classifier 打分（方向相关 × 技术深度）；每日推送配比定稿 = **热点事件 1–2 条 + 技术文章 3–5 篇**（热点少、优质技术多）。
- 交互：已读/收藏标记（学习档案素材）、点原文外链；默认源（先知/FreeBuf/安全客/看雪/arXiv cs.CR/PortSwigger Research 等）+ 源清单 CRUD 在设置页新增「**情报源**」tab（仿 McpPane 结构）。

### 16.3 Obsidian 接入 + 学习计划（E10 已实施 2026-09-16）

> **实施注记（v1 取舍）**：文件树/搜索全走索引（只查 intel.db，请求路径零 FS 访问，路径来自 DB 天然无穿越）；索引为全量重建、无自动监听，编辑笔记后手动重建（staleness 可接受）。正文入库 vault_notes.content 仅本地搜索用，隐私红线以测试断言兜底（LLM 入参不含笔记正文）。

- **vault 只读接入**：配置本地 vault 路径后接入文件树浏览 / 全文搜索 / 链接图谱（可选）；**平台绝不写回 vault**——用户手工整理的笔记是圣域。
- **隐私红线（定稿，不可放松）**：LLM 只看**元数据**（文件名/标题/frontmatter 标签/目录结构），笔记正文永不出本机发给 LLM；正文仅平台内搜索用。
- **学习档案** = 用户声明（profile.json 方向与阶段）+ vault 元数据推断（各方向笔记分布/最近活跃）+ 平台侧学习记录（文章已读/收藏）。
- **周学习计划**：LLM 结合学习档案与当周简报/文章生成（学什么、读哪篇、练什么）；计划存平台侧，**可导出 md 由用户自行贴回 Obsidian**（不自动写入）。

### 16.4 前端入口（E9/E10）

- 左侧导航新增顶级页「**情报**」：`App.tsx` 四处注册（View 联合类型 / NAV 数组 `needsProject=false` / 无 pid 分支可渲染 / 视图分发），三 tab = 简报/文章/学习；跨页跳转复用 `goto-*` 自定义事件模式。
- `ProjectsView` 从 `max-w-3xl` 单栏改两栏，右栏为今日简报摘要卡（无简报时显示引导抓取）。
- 设置页 tab 数组加「情报源」（源清单 + profile 权重编辑 + 手动抓取按钮 + E10 vault 配置节）。
- E10 学习 tab = 三来源档案（声明画像 / vault 推断 / 平台已读收藏）+ 当周计划（生成 Job、复制 md、归档周列表）。

***

## 17. 待做清单（Backlog）

> 全部未落地工作的索引。**2026-09-16 v0.31 重整**：原 机制 1.1/机制 1.4（发布去重/资源租约，schema v7）已落地移除；实战走查催生的 C2/C1/C3 与 C5 自原 E 组拆出成立**C 组 编排与任务语义**（实战优先）；逆向复用顺延为 D 组；体验与基建归位 F 组；补账 E8 后置项 **C4 L2 档步数耗尽自动续跑**（原挂账遗漏）。**ID 对照（v0.31 重编前→后）**：B4→机制 1.1、B5→机制 1.4、B6→B3、B3→B4；E15→C1、E14→C2、E16→C3、E17→C4、E11→C5；C1→D1、C2→D2、C3→D3；D2→E1、D1→E2；E13→F1、E4→F2、E3→F3、E5→F4。**历史行与已落地批次沿用当时 ID**（E1=红线 UI 重构、E2=redlines 补齐、E6/E7/E8/E9/E10/E12 为已落地批次，勿与本章 E1/E2 混淆）。每条只给概述与实施拆批，完整设计见所注章节；落地一项即从此处移除并更新文首「落地状态总览」。
>
> **排序理由（v0.31）**：B 组余部按日常正确性价值×成本排——机制 1.1 取消传播（多会话日常正确性）→ 机制 1.4 逆向互斥（消 TOCTOU，机制 1.4 租约机制已就绪、工作量小）→ B3 小件 → B4 大件且仅 E2 前置、押后；C 组编排/任务语义直接服务当前评估实战（C1 止损 → C2 提质 → C3 定界 → C4 小件 → C5 CTF 大件）；D 组逆向复用独立可插队；E 组新轨外部依赖重殿后（E1 独立于 B4 先行、E2 依赖 B4）；F 组体验债穿插。

### B 组：协调机制正确性底座（F 批余部，§6.7/§6.7.1；任务仍保持四态，每项自带并发测试）

1. **机制 1.1 · 机制 1.5 余部 + 1.7 全量取消传播**：Agent 通信工具 `bb_notify/handoff/to_role`（系统私信通道与注入设施已就绪，§6.7 的 1.5；**纪律约束不硬限额**——工具描述+技能纪律约束，观察后再议）；取消沿 parent_id 树传播——open 子任务物理删除（task.deleted 标 cancel_cascade）、claimed 发 task.cancel_requested 步边界 fail 再向下传一层、keep_independent 例外（A1 是其单任务先遣版，已落地）。
2. **机制 1.4 · 机制 1.8 逆向工具互斥锁**：headless 全量导出按 (project,sha) 库级锁替换进程内 Lock/`_triage_running`（现状不跨进程且有 TOCTOU），并发请求排队共享同一 Job 产物（不重复跑 900s）；IDA `.i64` 单实例固化为 `tool:ida:<sha>` 租约（消费 机制 1.4 的 resource_leases 机制，已就绪），GUI 占用时排队/改道。
3. **B3 · 机制 1.10 高噪声联动审批**：approval 携带归一化目标（ip/host），net=real 审批弹窗显示同目标历史审批次数辅助判断；**只联动展示，绝不自动批准、不放松红线**。
4. **B4 · 机制 1.2 blocked_by 依赖调度 + 1.3 大任务分片（map-reduce）**：依赖未 done 的 open 行经 claim_next SQL 排除、无状态翻转，依赖 fail 由编排决策；shatter(func|url|host) 分片+聚合任务 blocked_by，分片键防重复。**1.2 是 E2 development 轨 implement→build→test→review 工作流的前置**。

### C 组：编排与任务语义（2026-09-16 新设，实战优先——源自走查痛点：ROE 挂起 token 白烧、编排手工拆解；条目 ID 沿用不改）

5. **C1 · 任务暂停语义统一 + 待人工输入承接（2026-09-16 定稿，§6.1「任务暂停语义统一与待人工输入承接」）**：fail_task 结构化 `blocked_reason`（awaiting_human|error，tasks 幂等加列，缺省 error 向后兼容）→ awaiting_human 保留快照复用 E12 revive/resume「▶ 续跑」（限原会话；closed/无快照自动降级仅放回）→ 看板 failed 卡 badge+暂停原因正文化+「✅ 已解决，放回继续」（附注写进任务行落审计）→ 认领提示层注入（旧 plan 带「done 仅供参考不视为已验证」注记+该任务 findings 摘要）→ awaiting_human 计入审批铃铛红点（只计数不混 approval 表）。宁严勿松：不自动重试、不自动放回、L2 自动链不处置。**拆批**：① 黑板列+fail 通道（tasks 加列 blocked_reason、TaskQueue.fail 增参数、agent `fail_task` 工具描述补两值语义）→ ② 续跑统一（agent loop fail_task awaiting_human 路径保留快照；resume 端点零改动）→ ③ 看板 UI（TaskBoard badge/正文暂停原因/续跑钮显隐/「已解决，放回继续」附注输入）→ ④ 铃铛红点（顶栏计数纳入 awaiting_human 任务数，approval 表零改动）→ ⑤ 测试（fail 落列与事件/reopen 附注落库/续跑快照路径/认领注入断言）。起因：ROE 未核验自主挂起任务无继续入口、放回重做白烧 2.78M tokens 且新会话误读旧计划 done 标记（2026-09-16 浏览器实测走查）。
6. **C2 · 编排器任务拆解 + 资产分批发布 + 低水位补任务（2026-09-16 定稿，§6.4「任务拆解与分批发布」）**：编排 tick 分析新任务可拆解为带 parent_id 的子任务（**深度 1 层**、每轮 ≤`max_publish_per_tick`=5、L0 提案同闸计数）→ 资产视图注入 `_stats()`（by_type/by_status 计数 + 未覆盖清单 cap 30 带 id + uncovered_total；判定为提示层，真护栏=conflict_keys 互斥）→ 分批 3-5/轮、worker 空退事件驱动续批（**无新触发点**）→ 低水位=池空但有 uncovered 资产继续发批，**L0/L1/L2 同逻辑**。**拆批**：① 黑板校验原语（tasks.py `check_parent` + publish 深度 1 校验，编排器/人类 POST/提案采纳三写路径共用）→ ② 编排器拆解（ORCH_TOOLS publish_task 加 parent_id、OrchestratorConfig.max_publish_per_tick、`_publish_count` 计数闸 L0 同闸、ORCH_SYSTEM_PROMPT 纪律第 7 条（拆解/分批/续批/无活即 done）、`_stats()` 资产视图）→ ③ 通道透传（app.py publish_task 端点补 parent_id=body.parent_id——TaskIn 已有字段，坏父 422；webui api.ts publishTask 类型 + LiveRoom adoptProposal 透传，不透传会断撤回传播子树与任务图实线边）→ ④ 测试（test_orchestrator：拆解落库/深度 1 拒绝/每轮 ≤5/L0 提案带 parent_id 且同闸/资产 uncovered 判定；test_api：parent_id 落库与 422）。机制 1.3 的部分前置（§6.7 注记）。
7. **C3 · 作战模式与 mission 战役目标（2026-09-16 定稿，§6.9「作战模式与 mission 战役目标」）**：project config 增 `mode`(pentest|redteam)+`mission{text,criteria}`→§6.3 禁令按 mode 条件化（redteam ROE 内放开）+tracks/assessment/rules/redlines.md 增两模式边界文本→编排器 mission 注入与 converged 判据升级→UI（mode 徽标/切换二次确认/ROE 必填/mission 编辑器）→测试。**拆批**：① 配置面（ProjectStore config normalize 归一+缺省 pentest、mode.changed 审计、切换 ROE 校验）→ ② 边界接线（§6.3 禁令条件化、redlines.md 两模式边界段、角色 description 按模式语义微调）→ ③ mission 注入编排器（overview 注入+converged 判据「mission 达成或资产穷尽」+ORCH_SYSTEM_PROMPT 纪律补对照决策）→ ④ UI（项目设置 mode/mission 表单+顶栏徽标+二次确认弹窗）→ ⑤ 测试（mode 归一与切换校验/边界条件化/mission 注入与收敛判定/审计事件）。含两个〔未实施〕专项：社工钓鱼、shell 管理页（§6.9 末，依赖红队模式先行）。
8. **C4 · L2 档步数耗尽自动续跑（E8 后置项补账，§3 注记）**：L2 全自动链下 budget_paused 不等人工——自动 resume（缺省 +200，受 token 预算硬闸与 max_chain_ticks/暂停/档位闸约束，复用 _maybe_auto_tick 闸序），防全自动链因步数暂停停摆；L0/L1 维持人工「▶ 继续」。小件，可随 C 组任一批顺带。
9. **C5 · CTF 线索板 + 跨轨路标（2026-09-16 定稿，§5.2）**：findings 换词表复用（四级线索级别含死路、线索类别、false-positive→死路）→ relates_to 卡片链 + 产物内联附件 + 解题脚本/writeup 落 artifact 纪律 → 跨轨统一路标（渗透已排除路径/逆向已排除假设）→ 三级注入按 IP 聚合 → 线索卡流+级别筛选融入现有列表 → CTF 样本上传 UI/远程靶机落资产补缺。无 schema 升级。**拆批**：① CTF 换词表+线索卡流（前端为主：Blackboard Findings 组件四级分色/vuln_class→线索类别/false-positive→死路默认折叠/级别筛选 chips，详情弹窗同步，bb_add_finding 工具描述补 CTF 语义）→ ② 路标记录（bb_add_finding 增死路/已排除方向带原因+已尝试清单，挂 target_asset_id）→ ③ 三级注入按 IP 聚合（loop.py 认领后：scope 精确匹配路标全文 → 资产父链归并同 IP 一行动态摘要（跨端口可见、未覆盖端口显形）→ 项目级计数 + bb_query 查询纪律改写）→ ④ 产物纪律+内联附件（解题脚本/writeup 落 artifact 的技能规则文本、线索卡内联附件复用 POC 弹窗、relates_to 卡片链显示）→ ⑤ 补缺（CTF 样本上传控件——samples 端点已在、非逆向场景需跳过自动 triage；远程靶机 host:port 落资产引导——register_asset 已能识别）。

### D 组：逆向复用（§9 末；独立低风险，可随时插队）

10. **D1 · R1 同 sha256 内容寻址全局缓存**：headless JSON/.i64 从 per-project 提至 `workspaces/.cache/`，项目改引用——同样本 N 个项目只付一次 900s 全量导出；项目删除不牵连缓存。独立、风险低，逆向线立刻受益，**可随时插队**。
11. **D2 · R2 全局函数库 + 报告导出**：func_kb 跨项目查询、他项目结论以"他山之石"展示、人审晋升采纳（与 §4 提案通道合流）；verified 证据链导出 Markdown 报告——assessment 渗透报告与 §15.5 AI 破甲评估报告共用此出口。
12. **D3 · R3 跨 binary 函数签名匹配**：FLIRT 式特征 + 伪码规范化哈希，支撑游戏版本更新/换编译器/轻度混淆后的函数迁移（新版本拖入即按签名映射旧 func_kb 结论到人审确认，不自动覆盖）。

### E 组：新场景轨（外部依赖重，殿后）

13. **E1 · L3 fakenet + malware 轨（Phase 3）**：INetSim/FakeNet sidecar（当前显式 NotImplementedError）、样本 docker cp 投递与一次性销毁容器、malware 静态/动态角色强制分离、IOC/行为视图复用 rev-generic 内核（见 §7、§4.5.2、§10）。
14. **E2 · development 场景轨 P1（§15）**：前置＝仓库 `git init` + B4 blocked_by；tracks/development、dev profile（文件树+编辑器+构建台+提案 diff 四件套）、implement/build/test/review 角色与门控任务流（test 过才 done）、代码提案走分支（§4 proposals 扩 target kind=code，AI 只提分支提交、人审合并）；**含 §15.5 AI 反代/AI 破甲专项，红线文本先行进 track redlines、escalation 不可放松**。

### F 组：体验与基建（任意时机可穿插）

15. **F1 · 三栏框架可拖拽调宽（2026-09-16 定稿，§12 布局定稿）**：NavRail 抽取消重 + 左导航/右侧黑板侧栏 react-resizable-panels v4 化（左 48–220px、右 288–640px）+ localStorage 宽度持久化（`ui.nav-width`/`ui.board-width`）；纯前端无后端改动。
16. **F2 · WebUI 终端页（2026-09-16 定稿，§12「终端页与人类命令通路」）**：① E4a 人类命令面板（POST /exec 经 gateway，author=human，审计/审批全继承）→ ② 独立路由/补全 → B4 持久 workspace 容器基建（`cyb-ws-<slug>` 惰性创建/空闲 30min 回收/reconcile）→ 机制 1.1 E4b PTY 交互终端（双向 WS+xterm.js lazy；host 默认关、WSL 不提供、PTY 不逐键审计）→ 机制 1.4 收口（session 审计/审批 UI/fakenet 挂点与 E1 合并评审）。交互终端为人类专属，Agent 工具面不新增 PTY 工具。
17. **F3 · assessment 轨 Phase 2 收尾**：角色体系实战打磨、设备方向知识模块（binary/kb/iot、ics、vehicular）充实、network 能力包视内网内容量评估拆分；渗透报告随 D2 报告导出落地（自动化程度见 §12 待定项）。
18. **F4 · 杂项小项**：misc 包技能（§4.5.4、§10）；fofa/playwright 等运行时 MCP 工具桥（§4，当前仅逆向 IDA 桥）；浅色主题（§12 待定项，低优先级）。
