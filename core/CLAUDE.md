# core/

> 领域无关核心引擎：黑板 / 任务协调 / Agent / 执行网关 / LLM。设计唯一真相源是 DESIGN.md（§3-§9）。Team/Member/Run direct execution 独立于旧任务 DAG，start 经 preflight/人工确认后 fan-out 专属 Agent session。


## 目录索引（各目录 CLAUDE.md 是接手入口）

| 目录/文件 | 职责 |
|-----------|------|
| [`skills/`](skills/CLAUDE.md) | 能力包×场景轨分类学：注册表 / 路由评分 / 规则链 / **专家池（M2 起运行时唯一角色源 + caps_effective/allowed_roles 推导面；M3 专家 CRUD `/api/experts`）** / **场景档 profiles.py（M4a：五件套预设物化即弃）** / task_types 注册表 |
| [`agent/`](agent/CLAUDE.md) | AgentSession 主循环 + 工具分发 + 角色工具白名单边界 + kb_open + 认领后计划闸（A2）+ 子代理分解 publish_task（A5）。**session_state.py（2026-10-03）**：任务/闸门状态单一容器 + `reset_for_task` 单入口复位，两持有者以 property 代理原属性名（只收敛存取，控制流不动）。**tool_registry.py（2026-10-03）**：工具唯一真相源——`ToolSpec`(name/description/schema/group/flags/handler) 一处定义，`AGENT_TOOLS`/各闸门白名单常量/`agent_tool_group` 全由 flags 推导（导入名逐字保留，外部零改动），导入期自检 handler 存在 |
| [`orchestrator/`](orchestrator/CLAUDE.md) | 主代理 tick：监控/派生/开窗/汇总 + 轨注册表拒收 + 饿死告警 + gate 预算闸门 + **replan_priorities 优先级重排（A5）** + state.py（tick 租约/状态持久化/**v21 derive_idle_rounds**）+ **分阶段接线（`_phase_section` 注入 + `_phase_gate_reject` 派单门）** |
| [`blackboard/`](blackboard/CLAUDE.md) | SQLite 单一写入口 + 任务队列 + 事件总线 + 任务图 graph.py（A3）（schema v11：tasks.plan 计划步、session_inbox/撤回私信、artifacts.meta、findings.rating_basis 判级依据 F11） |
| [`browser/`](browser/CLAUDE.md) | F6 内置浏览器（渗透/红队轨）：Playwright 托管 Chromium 实例池（每项目常驻，Page=会话）+ 资产白名单 + 抓包（路由拦截入 http_history v15）+ 重发/爆破（人类 UI 专属） |
| [`intel/`](intel/CLAUDE.md) | 情报面板（E9，§16）：全局 config/intel/ 存储 + 触发式抓取（NVD/KEV/GHSA/RSS，getter 可注入不经网关）+ classifier 打分/简报（LLM 缺席降级规则，不 503） |
| [`api/`](api/CLAUDE.md) | FastAPI 唯一 HTTP 入口（全项目唯一 import fastapi 处）+ WS + Job |
| `runtime/` | Level L0-L3 / policy / gateway / backends / detector；unknown 按 malware_live，默认 net=none |
| `llm/` | Anthropic /v1/messages 内部标准；Ark 接入 + 多供应商（config/providers.json）+ ModelRouter。**传输层 = 官方 anthropic/openai SDK（2026-10-03）**：`sdk_engine.py` 把 SDK 流式事件还原成 SSE 行复用既有 `_consume_stream`（Ark 特化零重写），httpx 垫片旁路录制原始字节作降级重解析；`parsing.py` 抽出双协议响应解析纯函数；**`tokenizer.py`（2026-10-03）token 分层计数**——OpenAI 系 tiktoken 精确 / Ark 系加权估算 / 兜底字符数，返回等价字符数故阈值语义不变（消费方 `agent/loop.py::_count_tokens`） |
| `tools/` | 组合服务（decompiler：headless v3 全量导出含 strings，IDA→Ghidra 选路；P2 IDA 双向写回 writeback/refresh_db_cache/diff_pulled_names；`MCPBackend` 实时桥=streamable-http 懒握手/3s TTL 探活，只做写回直写+缓存缺席单函数 decompile+xref 降级，绝不替代全量缓存，断了静默降级）+ detector；`data/fpdb/fpdb_seed.json`=本地指纹包首批（2026-09-22 dsh 收编，规则制；registry data 类声明随 toolchain-registry M1） |
| `projects.py` | ProjectStore：`create_project(name, track="ctf", capabilities=None, config=None, experts=None)`（注入 autonomy 默认档归一化；**M2：experts 非空才写 meta 键=存量直通语义**）；`update_config` 双写 project.json+黑板行；**`update_experts` 换将（M2：重写 meta.experts，空清单剥键恢复直通态；黑板行无此列，单一真相源 project.json）**；**`update_phase_goal` / `update_orchestrator_persona`（对话化编排器 M2/M3，2026-09-21）：meta.phase_goal（None=剥键清空）与 meta.orchestrator_persona（None=恢复缺省）整键写，事件留痕（goal.confirm/goal.clear）由 API 层落**；**`inherit_knowledge(pid, src)`（M4b 知识继承，2026-09-21）：三段复制只增不覆盖——binary 资产（find_asset 同 sha 判重，样本文件 copy2 到目标 samples/、meta.path 更新+`inherited_from` 标记、author 沿源）/ func_kb（(project,sha,address) 判重）/ 蓝图（(project,sha,name) 判重，非 draft 状态尽力保留）；源项目只读不动，失败不阻建项**；旧 domain 透明映射；回收站式删除（删前 close_all 双层关闭闸门 + rename 0.05–0.2s 退避重试）；`Project.close()` 后 `proj.bb` 抛 BlackboardClosedError 不重建 |
| `autonomy.py` | 自主档 L0/L1/L2（§6.8）+ **auto_derive mission 自动派生开关（C2 §6.9）**：默认档按轨、normalize/autonomy_of、sessions_cap 计数（**active_sessions 仅统计 status=running 的 worker，会话开窗/idle/armed 待命不占用**）、`record_llm_usage`（记账+llm.usage+80% 软警）、`hard_block_reason`/`human_warning` 闸门（每次实时重读，不缓存）、usage_view（批 5 起被 api L2 链状态机消费，本文件无链逻辑；**出口带 `derive:{last_at,last_result}`——mission 自动派生上次判定，v13 orchestrator_state 新列，前端状态灯消费**）、**`normalize_rule_profiles`（F11：rule_profiles 三态归一化，projects.update_config 与 API 层共用）**、**`normalize_advisor` + ADVISOR_DEFAULTS/RANGES（D10，2026-09-24：config.advisor 段归一化，非法值 ValueError→422）** |
| `phases.py` | **分阶段工作流引擎（pentest-phased-workflow M1+M2，2026-09-22，DESIGN §四）**：阶段剧本受约束 yaml 解析 + 轨级/项目覆写加载（load_track_phases）、门指标判定（evaluate_gate 纯确定性 + idle 空闲逃生）、拦截判定（gate_block_reason）、流转原语 enter_phase（状态落 meta + 剧本首发 noise=passive + fired 指纹去重 + 抵达校准，`publish=False`=只登记）；**门评估状态原语 read/save_gate_state（M3：phase_gate_state 落 meta 单一事实源，enter_phase 流转顺写新阶段）**；不 import core.orchestrator（其反向依赖），idle_rounds 由调用方读 orchestrator_state 传入；过门 L0/L1/L2 分流在 API 层 |
| `coverage.py` | **覆盖度对账（orchestrator-efficiency M3，2026-09-22，DESIGN §6.4）**：纯函数查询层（不 import core.orchestrator）——asset_terminal_state 终态四味（tested_clean/na/dead_end/finding 挂链，FP-only=死路味，budget_stop 算 open）+ coverage_report 分组收敛对账（url/service→host→domain 沿父链到根归组 domain:/host: 为主，父借子收敛须自身 visited 佐证；只计 host/domain/url/service 四类，uncovered open 优先）；编排器 _assets_view 消费（B2 态势注入）。**effective_status_map / attach_effective_status（asset-tree-derived-clean M2，2026-09-24，DESIGN §三）**：节点根状态读时派生——叶子 explicit、有子父节点随全部孩子 settled 派生 tested_clean/任一 open 则 open、新增子自动破 clean、has_findings 沿树上传 |
| `fofa.py` | **FOFA 第三方中转客户端（cyberspace-mapping M2，2026-09-23）**：纯客户端零 bb 依赖，transport 可注入（测试 fake 零触网）；错误四态签名分类（「已用完」QuotaExhausted **熔断绝不重试**防封号 /「账号无效」ConfigError=base url 没换 /「key 不存在」AuthError /「[官方错误信息]」OfficialRetryable 换备用）；主备自动切换（网络错误/HTTP 非 200/非 JSON 也切）；size clamp 10000+页×条≤1 万；config load/save（坏文件给 {} 绝不 500）+ mask_key 脱敏；**key 只落 config/fofa.json（.gitignore），绝不入库不入日志** |
| `assetimport.py` | **资产导入解析器（cyberspace-mapping M1，2026-09-23）**：纯函数只读不落库——列映射嗅探（表头别名 0.9 / 无表头逐列内容投票 ≥0.8 / 文本列兜底 title 0.5）+ parse_table（CSV utf-8-sig→gbk 回退；xlsx openpyxl import-guard 缺失抛 XlsxUnavailable→端点 503）+ normalize_rows（kinds 短则 ignore 补齐、products 多列保序去重归并 cap 20、title 截 200）；MAX_IMPORT_ROWS=10000 单批硬上限 |
| `verify.py` | **独立验证器（independent-verification-audit M1，2026-09-23，DESIGN §三）**：验收条目 `str \| {text, verify}` 的服务端判定引擎——validate_verify_spec 受约束 schema 发布期校验（未知键拒绝）+ evaluate 四策略（flag_capture/effect_proof/poc_crash/oracle）+ run_reconcile_verifications 收尾钩子（complete 前跑 pending/failed，未过 ValueError 拦 done）；平台身份 trusted+host+工作区隔离经网关全量校验；**脱敏红线**：回执只含 passed+长度/哈希摘要，原文绝不回显；gateway 惰性 import 防环；M2 Auditor/M3 交棒/M4 重派注入见 docs/plans/ |
| `toolchain.py` | **工具链注册表（toolchain-registry M1，2026-09-23，DESIGN §八）**：纯 stdlib 四来源探测引擎——`load_registry(tools_root)` 读 tools/registry.json 逐条校验（坏条目 ValueError fail-fast，缺文件 {}）/ `load_tool_overrides()` 读 config/tools.json 覆盖层（坏文件 {} 宁容错）/ `resolve_tool` 四来源（config paths → tools/ bin 规范位〔acquire=bundled 即开包自带〕→ fallback glob〔`?:/` 盘符通配 C-H〕→ PATH）/ `probe_tools` 全表按名称序。探测只查文件存在性零副作用，verify 深检留 M3 面板。消费方：runtime/detector（能力清单）、skills/doctor（tool-missing/tool-registry-invalid）、tools/decompiler（ida/ghidra 解析前置 registry） |

## 全局约定

- `sample_packages.py`：分析包基础层。负责单文件/目录文件/ZIP/7z/TAR 的安全导入，内容清单哈希、全局树去重、原始上传物保留、候选目标识别和同包二进制依赖边；`ANALYZER_REGISTRY` 为格式到插件 ID 的注册表。Android 目标提供 APK/AAB 包摘要、Manifest XML/AXML 字符串清单和 DEX 头部计数；目标元数据和静态分析报告保存于 `sample_packages/`，不替换旧 `binary` 资产接口。

- 时间 UTC ISO；ID `<前缀>-<12hex>`。
- 黑板写操作只走 `Blackboard`/`TaskQueue` 方法；API 层零业务逻辑，只做 HTTP↔core 翻译。
- 项目绑定 = **场景轨 track（单选）× 专家组队 experts（M2，多选可空）**；能力包隐退为知识组织单位（运行时知识面=caps_effective 推导，专家 yaml `skills` 白名单跨包引用）；无绑定存量项目直通 meta.capabilities 零翻译；旧 domain 读取时经 `LEGACY_DOMAIN_MAP` 映射（pentest→assessment+[web]，ctf→ctf+[binary]，reverse→research+[binary]），新建一律写新值。
- 逆向工作台（P1/P2）：track=research ⇒ profile=rev-generic（前端 `deriveWorkbenchProfile`，config.workbench.profile 可覆盖；**2026-09-29 修**：M3 起 caps 多选退役、创建恒不传 caps，旧「caps 含 binary」判据已死——M3 后新建 research 项目全误落渗透模板；现按 config.board_view.default 非 funcs〔如 code-audit 档 findings〕交还渗透黑板）；三层数据——headless 缓存 JSON（**v3 契约**：客观全量+strings，可删重导）/ func_kb（只存分析过的函数）/ findings（挂 binary 资产，evidence 带 func_id+address）；headless 是 trusted **解析**工具，平台绝不执行样本。
- Agent 无裸 shell：唯一命令口是经网关的 run_cmd；不可信代码只进 docker/sandbox，WSL 信任级=宿主机。
- 安全默认宁严勿松：未知样本按恶意处理（L3 + fakenet）；fakenet 尚未实现（显式 NotImplementedError）。
- 工具异常回填文本不中断循环；LLM 传输层按类别重试（429/5xx 共 2 次尝试 + 30s 退避；连接类共 4 次 + 5/10/20s 退避；OpenAI 520 首次失败后 5 次重试），连接/TLS/超时分类由 SDK 承担（见 `core/llm/CLAUDE.md`）。

## 测试

- 全量：`E:\Miniconda3\python.exe -m pytest tests -q`（控制台先设 `$env:PYTHONIOENCODING="utf-8"`）。
- 真机演练在 scripts/（demo_agent / demo_orchestrator / demo_pentest），需要 Ark key；靶机不通时 demo_pentest 直接退出。
