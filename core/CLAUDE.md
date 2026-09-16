# core/

> 领域无关核心引擎：黑板 / 任务协调 / Agent / 执行网关 / LLM。设计唯一真相源是 DESIGN.md（§3-§9）。

## 目录索引（各目录 CLAUDE.md 是接手入口）

| 目录/文件 | 职责 |
|-----------|------|
| [`skills/`](skills/CLAUDE.md) | 能力包×场景轨分类学：注册表 / 路由评分 / 规则链 / 角色加载 / task_types 注册表 |
| [`agent/`](agent/CLAUDE.md) | AgentSession 主循环 + 工具分发 + 角色软边界（tools/max_runtime/default_noise）+ kb_open + 认领后计划闸（A2）+ 子代理分解 publish_task（A5） |
| [`orchestrator/`](orchestrator/CLAUDE.md) | 主代理 tick：监控/派生/开窗/汇总 + 轨注册表拒收 + 饿死告警 + gate 预算闸门 + **replan_priorities 优先级重排（A5）** + state.py（tick 租约/状态持久化） |
| [`blackboard/`](blackboard/CLAUDE.md) | SQLite 单一写入口 + 任务队列 + 事件总线 + 任务图 graph.py（A3）（schema v6：tasks.plan 计划步、orchestrator_state last_replan_at；v3 session_inbox/撤回+finding_update 私信） |
| [`intel/`](intel/CLAUDE.md) | 情报面板（E9，§16）：全局 config/intel/ 存储 + 触发式抓取（NVD/KEV/GHSA/RSS，getter 可注入不经网关）+ classifier 打分/简报（LLM 缺席降级规则，不 503） |
| [`api/`](api/CLAUDE.md) | FastAPI 唯一 HTTP 入口（全项目唯一 import fastapi 处）+ WS + Job |
| `runtime/` | Level L0-L3 / policy / gateway / backends / detector；unknown 按 malware_live，默认 net=none |
| `llm/` | Anthropic /v1/messages 内部标准；Ark 接入 + 多供应商（config/providers.json）+ ModelRouter |
| `tools/` | 组合服务（decompiler：headless v3 全量导出含 strings，IDA→Ghidra 选路；P2 IDA 双向写回 writeback/refresh_db_cache/diff_pulled_names；`MCPBackend` 实时桥=streamable-http 懒握手/3s TTL 探活，只做写回直写+缓存缺席单函数 decompile+xref 降级，绝不替代全量缓存，断了静默降级）+ detector |
| `projects.py` | ProjectStore：`create_project(name, track="ctf", capabilities=None, config=None)`（注入 autonomy 默认档归一化）；`update_config` 双写 project.json+黑板行；旧 domain 透明映射；回收站式删除（删前 close_all 双层关闭闸门 + rename 0.05–0.2s 退避重试）；`Project.close()` 后 `proj.bb` 抛 BlackboardClosedError 不重建 |
| `autonomy.py` | 自主档 L0/L1/L2（§6.8）+ **auto_derive mission 自动派生开关（C2 §6.9）**：默认档按轨、normalize/autonomy_of、sessions_cap 计数、`record_llm_usage`（记账+llm.usage+80% 软警）、`hard_block_reason`/`human_warning` 闸门（每次实时重读，不缓存）、usage_view（批 5 起被 api L2 链状态机消费，本文件无链逻辑） |

## 全局约定

- 时间 UTC ISO；ID `<前缀>-<12hex>`。
- 黑板写操作只走 `Blackboard`/`TaskQueue` 方法；API 层零业务逻辑，只做 HTTP↔core 翻译。
- 项目绑定 = **场景轨 track（单选）× 能力包 capabilities（多选）**；旧 domain 读取时经 `LEGACY_DOMAIN_MAP` 映射（pentest→assessment+[web]，ctf→ctf+[binary]，reverse→research+[binary]），新建一律写新值。
- 逆向工作台（P1/P2）：track=research && caps 含 binary ⇒ profile=rev-generic（前端 `deriveWorkbenchProfile`，config.workbench.profile 可覆盖）；三层数据——headless 缓存 JSON（**v3 契约**：客观全量+strings，可删重导）/ func_kb（只存分析过的函数）/ findings（挂 binary 资产，evidence 带 func_id+address）；headless 是 trusted **解析**工具，平台绝不执行样本。
- Agent 无裸 shell：唯一命令口是经网关的 run_cmd；不可信代码只进 docker/sandbox，WSL 信任级=宿主机。
- 安全默认宁严勿松：未知样本按恶意处理（L3 + fakenet）；fakenet 尚未实现（显式 NotImplementedError）。
- 工具异常回填文本不中断循环；LLM 传输层对 429/5xx/超时重试 3 次。

## 测试

- 全量：`E:\Miniconda3\python.exe -m pytest tests -q`（控制台先设 `$env:PYTHONIOENCODING="utf-8"`）。
- 真机演练在 scripts/（demo_agent / demo_orchestrator / demo_pentest），需要 Ark key；靶机不通时 demo_pentest 直接退出。
