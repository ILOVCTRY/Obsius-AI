# 方案：代理池接入（fir-proxy 集成）

- **状态**：**M1 + M2 + M3 均已实施（2026-10-07，见 DESIGN.md「代理池」节与「Web Fuzzer」红线段）**；待打磨清单 10 项仍开放（端口/池落位/依赖/密钥/非可信链路标注等已随实施定案，见 §4）
- **拍板记录**：见 §1
- **关联代码**：
  - 外部工具：`E:\Tools\fir-proxy - 1.2\`（`cli.py` AI 友好 CLI、`modules/server.py` 本地 HTTP/SOCKS5 服务、`modules/rotator.py` 池、`config.json`）
  - 目标落位：`tools/fir-proxy/`（vendor）、`tools/registry.json`、`core/proxy/`（新建）、`core/api/app.py`（装配 + 端点）、`webui/src/App.tsx`（顶级视图）、`core/agent/tool_registry.py`（丙）、`mcp-servers/fir-proxy-mcp/`（新建）
  - 参照先例：`core/browser/pool.py`（BrowserPool 项目级常驻 + `app.state` 装配 + shutdown 钩子 + delete_project 前置链）、`core/chat/mcp_bridge.py`（MCP 装配与开关）、`webui/src/views/SettingsView.tsx` McpPane（MCP 开关 UI）
- **实施后**：定稿决策沉淀回 `DESIGN.md`，同步翻新 `tools/CLAUDE.md`、`core/CLAUDE.md`、`core/api/CLAUDE.md`

## 1. 拍板记录（2026-10-07）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 集成落点 | 甲（Agent 用轮换代理）+ 丙（放开红线让 Agent 发起重放/爆破）+ **新增顶级「代理池」视图** |
| 2 | 甲 形态 | MCP 返回端口 + 可用 IP 列表，**AI 自己拿端口去访问**（`run_cmd` 里 `curl -x`）；平台不改 `browser_navigate`/`run_cmd` 的自动代理注入 |
| 3 | 丙 管控 | **直接放开**（不加 HITL 审批） |
| 4 | 代码落位 | **vendor 进仓库 `tools/fir-proxy/`** |
| 5 | 架构归属 | **平台托管 + MCP 控制面**——`core/proxy/` 拥有 serve 生命周期与池状态；MCP server 只做 AI 控制面（查端口/列表/轮换/抓取验证） |
| 6 | 推进方式 | 方案先行（本文），打磨后由用户排期实施 |

## 2. fir-proxy 现状与接口契约

fir-proxy 是 Python/Tkinter 代理池工具，**自带 `cli.py`（无 Tkinter 依赖，AI/自动化友好）**：

- **子命令**：`fetch`（在线源抓取）/ `validate`（批量验证质量）/ `check-url`（经代理检查目标）/ `normalize` / `select`（挑高质量代理）/ `serve`（起本地服务）。
- **stdout 协议**：JSONL，每行一个事件；末行恒为汇总 `{ok, command, count, output, log_file}`；日志走 stderr 并落 `logs/cli-*.log`；退出码 0/1/2。
- **`serve` 契约（关键）**：读输入文件里的 `status=="Working"` 代理 → 起 HTTP `127.0.0.1:1801` + SOCKS5 `127.0.0.1:1800` → 首帧 `started` 事件带 `{http, socks5, current_proxy, proxy_count}` → 内建轮换（`--request-rotation-count` 按请求数换 IP）+ 目标故障切换（`--target-failover-threshold`）→ `Ctrl+C`/SIGTERM 停。
- **代理记录字段**：`proxy`(host:port) / `protocol` / `location`(国家) / `latency`(秒) / `speed` / `anonymity` / `status` / `consecutive_failures` / `health_failures` / `score`。
- **CLI 路径依赖**：`requests[socks]`、`beautifulsoup4`、`lxml`（`ttkbootstrap` 仅 GUI 需要，CLI 不 import tkinter）。
- **形态局限（影响设计）**：`serve` 是**一次性、文件驱动**的长驻进程，**无运行中控制 API**——不能在线增删代理或强制切 IP；池状态活在 serve 进程内存 + 磁盘文件。GUI 里的「手动轮换」是 GUI 进程内直接调 `ProxyRotator`，CLI `serve` 没有等价入口。

## 3. 目标架构

```
┌─ WebUI「代理池」顶级视图 ──────────────┐
│  池列表 / 起停 serve / 轮换 / 抓取验证 │
└──────────────┬────────────────────────┘
               │ REST /api/projects/{pid}/proxy/*
┌──────────────▼────────────────────────────────────┐
│ core/proxy/  ProxyPool（平台托管，项目级常驻）      │
│  · 仿 BrowserPool：app.state 装配 + shutdown 钩子   │
│  · 拥有 fir-proxy serve 子进程生命周期（起/停/探活）│
│  · 读 serve stdout JSONL 取状态（端口 / 当前代理）  │
│  · 池文件（validated.json）读写 + 抓取/验证编排      │
└──────────────┬────────────────────────────────────┘
               │ 平台 API（127.0.0.1:8420，loopback）
┌──────────────▼────────────────┐
│ MCP server: fir-proxy-mcp     │  ← AI 控制面（config/mcp.json enabled 开关）
│  proxy_status  → 端口+IPC计数 │
│  proxy_list    → 可用 IP 列表 │
│  proxy_start/stop             │
│  proxy_fetch/validate/select  │
│  proxy_rotate                 │
└──────────────┬────────────────┘
               │ AI 拿端口自己用
     run_cmd: curl -x socks5://127.0.0.1:1800 https://target
```

**为什么平台托管而非 MCP 托管**：本项目的 MCP server 是平台按项目托管的**外部子进程**（`app.state.chat_mcp_bridges` 按 `proj.id` 缓存，core/api/app.py:8143），随会话/轮次起停、崩溃重拉。若把 serve 与池塞进 MCP 子进程，会随 MCP 生死、多项目各起一个、人类 UI 还得反向依赖 MCP 在线。平台托管保证：服务不随会话生死、人类 UI 与 AI 共享同一池、重启可恢复。

## 4. 落地切分建议

**M1 — 平台服务 + 顶级视图（不含 AI）** ✅ **已实施（2026-10-07）**
1. vendor：`E:\Tools\fir-proxy - 1.2\` → `tools/fir-proxy/`（剔除 `.git/`、`*.bak`、`*.patch`、`*-verification.txt`、`__pycache__/`、`logs/`、`img/`、代理列表文件；`config.json` 的 fofa/hunter key 与目标域名清空）。
2. `tools/registry.json` 加 `fir-proxy` 条目（`kind=python-tool`，`acquire=bundled`，`domains=[pentest,redteam]`，`pip=[requests[socks],beautifulsoup4,lxml]`）。
3. `core/proxy/pool.py`：`ProxyPool`（项目级常驻）+ serve 子进程管理 + 池文件读写 + `select_records` + `proxy_available` 能力探测；**薄 runner** `tools/fir-proxy/cyberstrike_serve.py`（包上游 `ProxyRotator`/`ProxyServer` + loopback 控制通道）。
4. `app.state.proxy_pool` 装配 + shutdown 钩子 + `delete_project` 前置链（停 serve 再删目录）。
5. API 端点 `/api/projects/{pid}/proxy/{status,start,stop,proxies,rotate,add,remove,select,fetch,validate}`。
6. WebUI 顶级视图「代理池」（`App.tsx` NAV_GROUPS「工具」组新增；`webui/src/views/proxy/`）。
7. 测试 `tests/test_proxy_pool.py`（含本地假 SOCKS5 上游的端到端转发）；`core/proxy/CLAUDE.md`、`tools/fir-proxy/CLAUDE.md`、`webui/src/views/proxy/CLAUDE.md` 目录文档。

**实施拍板（方案打磨未覆盖、实施时定的，均为常规决策）**：
- **轮换走 (c)**：给 vendored 代码加 loopback 控制通道——但**不改 fir-proxy 自身文件**，另写薄 runner 包装其 `ProxyRotator`/`ProxyServer`（便于上游同步）。
- **端口**：不设全局单例，改为**每项目动态取空闲口**（`_free_port` 从 base 起 bind 探测），多项目天然不冲突。
- **池数据落位**：`<项目>/proxy/pool.json`（文件，非黑板表）——与 fir-proxy 的池语义一致、零同步层；AI 经 MCP `proxy_list` 取，不走 `bb_query`。
- **依赖环境**：不新增 `tools/venv`，解释器自动解析（`tools/venv` → `sys.executable` → PATH）；本机 Miniconda 已具备 `requests[socks]`/`bs4`/`lxml`。
- **密钥**：本次**不接** fofa/hunter（在线免费源足够起步），vendored `config.json` 已清空 key；接时按 `core/fofa.py` 先例落 `config/` 且 gitignore。

**M2 — MCP 控制面** ✅ **已实施（2026-10-07）**
8. `mcp-servers/fir-proxy-mcp/fir_proxy_mcp.py`（stdio FastMCP 薄客户端调平台 API，10 工具）+ `config/mcp.json` 条目（`enabled:false`，设置页 McpPane 可开关）。**附带改动**：`mcp_bridge` 给**所有 stdio server** 注入 `PW_PROJECT_ID` + 支持 mcp.json 的 `env` 字段（此前仅会话级注入）——薄客户端型 MCP 靠它反查平台 API。测试见 `tests/test_chat_workbench.py::test_mcp_plain_stdio_server_gets_project_env`。

**M3 — 丙：Agent 重放/爆破工具** ✅ **已实施（2026-10-07）**
9. `core/agent/tool_registry.py` 新增 `browser_replay` / `browser_intruder`（group=浏览器，无闸 flag；handler 在 `tools.py`），调 `ReplayClient.replay` / `Intruder.run`，`author=session_id`；两者均可传 `proxy`。红线表述同步撤改：`core/browser/CLAUDE.md`、`core/api/CLAUDE.md`、`core/agent/CLAUDE.md`、`DESIGN.md`、`webui/CLAUDE.md`、`views/fuzzer/CLAUDE.md`（**拦截裁决仍人类专属**）。
10. `Intruder.run` 加 `proxy` 参数（httpx `proxy=`）+ 落 `meta.proxy` 与 start 事件；`IntruderIn` body 加 `proxy`；爆破 UI（`views/browser/ReplayForm.tsx`）加代理输入框。Agent 侧：AI 从 MCP 拿端口填 `proxy`。

## 5. 待打磨清单

- [ ] **端口分配 / 多项目冲突**：1800/1801 是 fir-proxy 默认。多项目要不要各占一套端口？还是全局单例一个池？→ 倾向**全局单例池**（代理池本就是跨项目资源），或每项目错开基址（仿 playwright-mcp 的端口错开先例）。
- [ ] **serve 无运行中控制 API**：UI「手动轮换 / 增删代理」怎么实现？三选一——(a) 重启 serve（换当前代理）；(b) 只依赖 serve 内建 `--request-rotation-count` + 目标故障切换，不做手动轮换；(c) 给 vendored `modules/server.py` 加一个 loopback 控制通道（HTTP 或 unix socket）。**待用户定**。
- [ ] **池数据落位**：池记录存 fir-proxy 的 `validated.json` 文件，还是入黑板表（新 schema）？前者省事但与平台脱节（AI 无法 `bb_query`）；后者要写同步层。
- [ ] **依赖环境**：`requests[socks]`/`bs4`/`lxml` 装 `tools/venv`（registry pip 声明）还是复用平台 python？serve 子进程用哪个解释器（自带 python 优先，仿 `启动平台.bat`）。
- [ ] **抓取源的密钥**：fofa/hunter key 落 `config/`（`.gitignore`），绝不入库不入日志（对齐 `core/fofa.py` 先例）。
- [ ] **丙 工具 schema**：`browser_replay`/`browser_intruder` 的参数面（对齐前端重放工作台控件）+ 是否复用现有速率/并发硬顶（intruder 并发硬顶 5、token-bucket 限速）。
- [ ] **目标范围红线**：浏览器导航/重放现允许任意目标；经代理访问是否同样不设白名单（授权边界由使用者负责）——倾向**同口径放开**，明确记入 DESIGN。
- [ ] **代理质量与安全**：免费代理会 MITM / 注入响应。经代理的流量是否要标注「非可信链路」（http_history 记 `meta.proxy`，对齐 replay 现有 `meta.proxy`）；重放/抓包结果是否降级标记。
- [ ] **与现有 `ReplayOptions.proxy` 的关系**：重放工作台已有显式代理字段，池开启后是否自动填充 `127.0.0.1:1801`？还是保持手动填（AI 与人类同）。
- [ ] **`serve` 首次启动无可用代理**：池空时端点行为（409 / 明确错误文案），UI 空态引导。
