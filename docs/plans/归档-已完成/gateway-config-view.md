# 方案：设置页网关页签（执行网关策略快照 + backends 实况探测）

- **状态**：**已实施（M1 全量，2026-09-23「开工吧」授权夜间自主推进）**；定稿决策已沉淀回 DESIGN.md §五「策略快照可视化」小节 + §12 设置页清单，本文保留作方案背景
- **实施记录（2026-09-23）**：
  - rateguard 抽表：`RATE_RULES` 模块级表（requirement/params/hint/threshold），校验函数读表组装拒因（`_reject` helper），`set(RATE_RULES)==set(_RULES)` 一致性测试 + masscan 阈值读表测试（tests/test_rateguard.py，四工具既有回归零变形）；
  - `GET /api/gateway/config`：runtime_levels/threat_matrix（unknown 按恶意行）/net_modes/pathguard 语义摘要 7 条（测试断言条数）/rate_rules 直出表/exec_params（app.py；单测断言 unknown allowed==malware_live allowed==["sandbox"]）；
  - `POST /api/gateway/probe`：`app.state.tools_root` 上移（:929），重跑 HostDetector().probe 替换 app.state.inventory，返回与 GET /projects/{pid}.capability 同构（test_api monkeypatch FakeDetector 断言替换与同构）；
  - 前端：`components/settings/GatewayPane.tsx` 新组件（顶部结论化告警条〔docker 硬警告/wsl 弱提示/全绿就绪——待打磨 #2 按细分落地〕、四通道卡 L0-L3 徽章+可用圆点+detail、工具探测清单、策略快照四表、底部红线注记；探测实况初值复用 getProject(pid).capability，无项目空态提示），SettingsView 注册「网关」页签（MCP 之后，8→9 tab），api.ts `gatewayConfig()/gatewayProbe()` + types.ts GatewayConfig/CapabilityInventory 类型；
  - 待打磨 #1 处置：runtime_levels desc 直取 policy.py 注释口径（单一事实源）；#3 处置：masscan 保守值 500 等 hint 全文入表（快照直出）；
  - 测试：test_rateguard +2、test_api +2；webui build 零 TS 错误；相关四文件回归 279 passed。
- **拍板记录**：

| 决策点 | 结论 |
|--------|------|
| 「网管配置」语义 | 执行网关（run(cmd, runtime) 策略面，DESIGN §7），非网络代理/LLM 网关 |
| 范围档位 | 只读快照 + backends 实况（M1）；**参数可配置化不做**（sandbox_image 是占位，等 fakenet/专用分析镜像/toolchain-registry 里程碑一起；default_timeout 抽配置需求不强） |
| 安全红线 | THREAT_ALLOWED / unknown→按恶意 / real 须审批等宁严勿松策略**永不前端可编辑**——前端改安全边界=配置漂移破坏 §7 纵深；页签底部显性注记 |
| backends 探测方式 | **复用 `app.state.inventory`**（启动时 HostDetector().probe 已跑、系统提示已注入、GET /api/projects/{pid}.capability 已返回前端但未消费）+ 新增手动刷新端点——不做 60s TTL 新缓存机制（商议口径的实施细化：常驻快照 + 手动刷新覆盖同一需求，更简） |
| 探测结论化 | 做——顶部告警条：docker/wsl 不可用 → 「⚠ L2/L3 通道不可用：不可信代码与恶意样本任务将被拒绝（宁严勿松不降级 host）」 |
| rateguard 抽表 | 阈值/参数清单抽代码内常量表（快照数据源），分支逻辑保留；表与 _RULES 键一致性测试防新增工具漏登记 |
| pathguard 快照 | 手工语义摘要行（正则启发式族抽不成表）+ 测试断言摘要条数防遗漏 |
| 拒绝深链 | 后置——直播间 [网关拒绝] 事件深链跳页签（文案不带规则代号，跳转落不到条目；改文案动 Agent 可见文本需回归） |
| 数据源纪律 | 单一事实源仍是代码；GET /api/gateway/config 做代码→JSON 映射，改规则必须走代码+测试，页面永不漂移 |

- **关联代码**：`core/runtime/policy.py`（RUNTIME_LEVELS/THREAT_ALLOWED/NET_MODES/DEFAULT_NET_MODE，快照直出源）、`core/runtime/rateguard.py`（_RULES :114-119 四工具校验函数；阈值 1000/-T0-3 等活在各函数体内）、`core/runtime/pathguard.py`（PS 写 cmdlet 集 :18-21、重定向 :28、豁免 :30）、`core/runtime/detector.py`（HostDetector.probe :65-73 / _run_probe timeout=10s / CapabilityInventory.to_json :43-49）、`core/api/app.py`（app.state.inventory 启动探测 :923、capability_prompt 注入 :1307,:1360、GET /api/projects/{pid} 返 capability :1556、gateway 按需 new :1077,:1314,:2691）、`webui/src/views/SettingsView.tsx`（八页签 TabsList :166-168，新增网关页签）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§7 执行网关节增「策略快照可视化」小节 / §12 设置页页面清单），同步 `core/api/CLAUDE.md`、`core/runtime/`（若有 CLAUDE.md 则同步）、`webui/CLAUDE.md`；本文保留作方案背景

## 1. 背景与痛点

网关规则全部硬编码在代码（policy/pathguard/rateguard），前端零可见、API 零暴露、无配置文件。Agent 被 `[网关拒绝]`/限速拒绝时，用户在直播间只能看到拒绝结果，规则是什么全靠翻源码；docker/wsl 可用性虽随 `GET /api/projects/{pid}` 返回（capability 字段）且已注入 Agent 系统提示，但前端无任何展示，且启动后 Docker Desktop 才起会假阴性（探测仅启动时一次，无刷新通道）。

## 2. 设计详述

### 2.1 后端

- **`GET /api/gateway/config`**（app.py 新增，前缀无冲突已核）：
  ```json
  {
    "runtime_levels": [{"name": "host", "level": 0, "label": "宿主原生", "desc": "本项目自身代码、静态分析"}, …],
    "threat_matrix": [{"threat_class": "untrusted", "allowed": ["docker", "sandbox"], "note": "…"},
                       {"threat_class": "unknown", "allowed": ["sandbox"], "note": "未知按恶意处理（宁严勿松）"}],
    "net_modes": {"modes": ["none", "fakenet", "real"], "default": "none", "note": "real 永不默认，须人工审批；fakenet 为后续里程碑"},
    "pathguard_rules": ["只拦写不拦读（产物归置诉求）", "PowerShell 写 cmdlet 清单：out-file/set-content/…", "重定向 > >> 1> 2> 同拦", "豁免：&1 &2 /dev/null nul con $null"],
    "rate_rules": [{"tool": "nmap", "requirement": "全端口扫描必须带限速参数", "params": ["-T0..3", "--max-rate", "--max-parallelism", "--scan-delay"], "hint": "-T3 --max-rate 200（推荐）"}, …],
    "exec_params": {"default_timeout": 120, "sandbox_image": "python:3.11-alpine（占位，专用分析镜像后续经 tools/ 管理）"}
  }
  ```
  映射层直接 import policy 常量组装；pathguard_rules 为手工语义摘要（测试断言条数防遗漏）。
- **rateguard 抽表（半抽取）**：新增模块级 `RATE_RULES: dict[str, dict]`（tool → {requirement, params[], hint, 阈值参数}），masscan 的 1000、nmap 的限速参数集从函数体迁入表，校验函数改读表；`check_rate` 分支逻辑不变；快照端点直出 RATE_RULES。**测试**：`set(RATE_RULES) == set(_RULES)` 防新增工具忘登记。
- **`POST /api/gateway/probe`**：重跑 `HostDetector().probe(tools_root)` 替换 `app.state.inventory`，返回新 inventory JSON（结构与 GET /api/projects/{pid}.capability 完全一致）。同步端点（FastAPI 线程池不卡事件循环），最坏 ~20s（docker+wsl 各 10s 超时，仅用户主动点击时发生）；前端按钮 loading 态。tools_root 存 `app.state.tools_root`（:923 现为局部变量，实施时顺手上移）。刷新即生效：后续新开窗经 `app.state.inventory` 引用取到新清单（:1307）；系统提示注入随之更新。测试注入口：monkeypatch `core.api.app.HostDetector`（import 已落 app 命名空间）。

### 2.2 前端（SettingsView 新增「网关」页签，放 MCP 之后）

- **顶部告警条**（结论化核心）：docker 或 wsl 不可用 → 黄色 `⚠ L2/L3 通道不可用：不可信代码与恶意样本任务将被拒绝（宁严勿松：拒绝不降级 host）`；全绿 → 弱色「四通道就绪」。
- **四通道卡**：host/wsl/docker/sandbox 各一行——等级徽章（L0-L3）+ 语义说明 + 可用性圆点 + detector detail（如 docker Server.Version）+「重新探测」按钮（loading，POST probe 后整页刷新）。
- **工具探测清单**：inventory.tools（tools/**/manifest.yaml probe 结果，可用/缺失）——payload 已含零成本展示；注记：工具面板最终归 toolchain-registry 方案（设置页工具面板与 MCP 并列），此处只读快照为临时归宿，落地时平移不冲突。
- **策略快照区**（表格化）：threat×runtime 放行矩阵 / 网络模式三档（real 标「须审批」）/ pathguard 语义摘要列表 / rateguard 规则表（tool/requirement/hint）/ 执行参数（default_timeout、sandbox_image 占位注记）。
- **底部红线注记**：`安全策略为代码内审计边界（改规则须走代码+测试），不做前端编辑`。
- `api.ts` 加 `gatewayConfig()` / `gatewayProbe()`；`types.ts` 加对应类型。

## 3. 实施切分（单里程碑，半天级）

M1 = rateguard 抽表 + GET /api/gateway/config + POST /api/gateway/probe（app.state.tools_root 上移）+ SettingsView 网关页签全量。后置（不在本期）：参数可配置化、直播间拒绝事件深链、fakenet 状态展示（等 fakenet 落地）。

测试面：test_runtime（RATE_RULES 键一致 + 表读阈值后四工具校验回归不变形）、pathguard 摘要条数断言、test_api（config 端点结构断言含 unknown→malware 同集；probe 端点 monkeypatch 假 detector 断言 app.state.inventory 替换与返回结构）；前端 `npm run build` 零 TS 错误。

## 4. 待打磨清单

1. runtime_levels 的 desc 文案与 tools.py:46 注释、loop.py 提示词口径对齐（同源措辞，避免三处漂移）；
2. 告警条是否细分「wsl 不可用」与「docker 不可用」的影响面差异（wsl 与 host 同信任级，不可用影响小；docker 不可用才是 L2/L3 硬伤）；
3. RATE_RULES 表是否连 masscan 备选保守值（500）等 hint 文案一起入表（倾向是，快照 hint 直出）。
