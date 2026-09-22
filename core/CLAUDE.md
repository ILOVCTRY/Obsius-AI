# core/

> 领域无关核心引擎：黑板 / 任务协调 / Agent / 执行网关 / LLM。设计唯一真相源是 DESIGN.md（§3-§9）。

## 目录索引（各目录 CLAUDE.md 是接手入口）

| 目录/文件 | 职责 |
|-----------|------|
| [`skills/`](skills/CLAUDE.md) | 能力包×场景轨分类学：注册表 / 路由评分 / 规则链 / **专家池（M2 起运行时唯一角色源 + caps_effective/allowed_roles 推导面；M3 专家 CRUD `/api/experts`）** / **场景档 profiles.py（M4a：五件套预设物化即弃）** / task_types 注册表 |
| [`agent/`](agent/CLAUDE.md) | AgentSession 主循环 + 工具分发 + 角色软边界（tools/max_runtime/default_noise）+ kb_open + 认领后计划闸（A2）+ 子代理分解 publish_task（A5） |
| [`orchestrator/`](orchestrator/CLAUDE.md) | 主代理 tick：监控/派生/开窗/汇总 + 轨注册表拒收 + 饿死告警 + gate 预算闸门 + **replan_priorities 优先级重排（A5）** + state.py（tick 租约/状态持久化/**v21 derive_idle_rounds**）+ **分阶段接线（`_phase_section` 注入 + `_phase_gate_reject` 派单门）** |
| [`blackboard/`](blackboard/CLAUDE.md) | SQLite 单一写入口 + 任务队列 + 事件总线 + 任务图 graph.py（A3）（schema v11：tasks.plan 计划步、session_inbox/撤回私信、artifacts.meta、findings.rating_basis 判级依据 F11） |
| [`browser/`](browser/CLAUDE.md) | F6 内置浏览器（渗透/红队轨）：Playwright 托管 Chromium 实例池（每项目常驻，Page=会话）+ 资产白名单 + 抓包（路由拦截入 http_history v15）+ 重发/爆破（人类 UI 专属） |
| [`intel/`](intel/CLAUDE.md) | 情报面板（E9，§16）：全局 config/intel/ 存储 + 触发式抓取（NVD/KEV/GHSA/RSS，getter 可注入不经网关）+ classifier 打分/简报（LLM 缺席降级规则，不 503） |
| [`api/`](api/CLAUDE.md) | FastAPI 唯一 HTTP 入口（全项目唯一 import fastapi 处）+ WS + Job |
| `runtime/` | Level L0-L3 / policy / gateway / backends / detector；unknown 按 malware_live，默认 net=none |
| `llm/` | Anthropic /v1/messages 内部标准；Ark 接入 + 多供应商（config/providers.json）+ ModelRouter |
| `tools/` | 组合服务（decompiler：headless v3 全量导出含 strings，IDA→Ghidra 选路；P2 IDA 双向写回 writeback/refresh_db_cache/diff_pulled_names；`MCPBackend` 实时桥=streamable-http 懒握手/3s TTL 探活，只做写回直写+缓存缺席单函数 decompile+xref 降级，绝不替代全量缓存，断了静默降级）+ detector |
| `projects.py` | ProjectStore：`create_project(name, track="ctf", capabilities=None, config=None, experts=None)`（注入 autonomy 默认档归一化；**M2：experts 非空才写 meta 键=存量直通语义**）；`update_config` 双写 project.json+黑板行；**`update_experts` 换将（M2：重写 meta.experts，空清单剥键恢复直通态；黑板行无此列，单一真相源 project.json）**；**`update_phase_goal` / `update_orchestrator_persona`（对话化编排器 M2/M3，2026-09-21）：meta.phase_goal（None=剥键清空）与 meta.orchestrator_persona（None=恢复缺省）整键写，事件留痕（goal.confirm/goal.clear）由 API 层落**；**`inherit_knowledge(pid, src)`（M4b 知识继承，2026-09-21）：三段复制只增不覆盖——binary 资产（find_asset 同 sha 判重，样本文件 copy2 到目标 samples/、meta.path 更新+`inherited_from` 标记、author 沿源）/ func_kb（(project,sha,address) 判重）/ 蓝图（(project,sha,name) 判重，非 draft 状态尽力保留）；源项目只读不动，失败不阻建项**；旧 domain 透明映射；回收站式删除（删前 close_all 双层关闭闸门 + rename 0.05–0.2s 退避重试）；`Project.close()` 后 `proj.bb` 抛 BlackboardClosedError 不重建 |
| `autonomy.py` | 自主档 L0/L1/L2（§6.8）+ **auto_derive mission 自动派生开关（C2 §6.9）**：默认档按轨、normalize/autonomy_of、sessions_cap 计数、`record_llm_usage`（记账+llm.usage+80% 软警）、`hard_block_reason`/`human_warning` 闸门（每次实时重读，不缓存）、usage_view（批 5 起被 api L2 链状态机消费，本文件无链逻辑；**出口带 `derive:{last_at,last_result}`——mission 自动派生上次判定，v13 orchestrator_state 新列，前端状态灯消费**）、**`normalize_rule_profiles`（F11：rule_profiles 三态归一化，projects.update_config 与 API 层共用）** |
| `phases.py` | **分阶段工作流引擎（pentest-phased-workflow M1+M2，2026-09-22，DESIGN §四）**：阶段剧本受约束 yaml 解析 + 轨级/项目覆写加载（load_track_phases）、门指标判定（evaluate_gate 纯确定性 + idle 空闲逃生）、拦截判定（gate_block_reason）、流转原语 enter_phase（状态落 meta + 剧本首发 noise=passive + fired 指纹去重 + 抵达校准，`publish=False`=只登记）；不 import core.orchestrator（其反向依赖），idle_rounds 由调用方读 orchestrator_state 传入；过门 L0/L1/L2 分流在 API 层 |

## 全局约定

- 时间 UTC ISO；ID `<前缀>-<12hex>`。
- 黑板写操作只走 `Blackboard`/`TaskQueue` 方法；API 层零业务逻辑，只做 HTTP↔core 翻译。
- 项目绑定 = **场景轨 track（单选）× 专家组队 experts（M2，多选可空）**；能力包隐退为知识组织单位（运行时知识面=caps_effective 推导，专家 yaml `skills` 白名单跨包引用）；无绑定存量项目直通 meta.capabilities 零翻译；旧 domain 读取时经 `LEGACY_DOMAIN_MAP` 映射（pentest→assessment+[web]，ctf→ctf+[binary]，reverse→research+[binary]），新建一律写新值。
- 逆向工作台（P1/P2）：track=research && caps 含 binary ⇒ profile=rev-generic（前端 `deriveWorkbenchProfile`，config.workbench.profile 可覆盖）；三层数据——headless 缓存 JSON（**v3 契约**：客观全量+strings，可删重导）/ func_kb（只存分析过的函数）/ findings（挂 binary 资产，evidence 带 func_id+address）；headless 是 trusted **解析**工具，平台绝不执行样本。
- Agent 无裸 shell：唯一命令口是经网关的 run_cmd；不可信代码只进 docker/sandbox，WSL 信任级=宿主机。
- 安全默认宁严勿松：未知样本按恶意处理（L3 + fakenet）；fakenet 尚未实现（显式 NotImplementedError）。
- 工具异常回填文本不中断循环；LLM 传输层对 429/5xx/超时重试 3 次。

## 测试

- 全量：`E:\Miniconda3\python.exe -m pytest tests -q`（控制台先设 `$env:PYTHONIOENCODING="utf-8"`）。
- 真机演练在 scripts/（demo_agent / demo_orchestrator / demo_pentest），需要 Ark key；靶机不通时 demo_pentest 直接退出。
