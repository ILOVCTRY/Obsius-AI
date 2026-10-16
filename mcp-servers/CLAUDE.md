# mcp-servers/

> 外部 MCP server 的**本地安装与启动器**（不是平台代码）。平台经 `config/mcp.json` 引用，`core/chat/mcp_bridge.py` 按会话起进程。

## FOFA 密钥统一（2026-10-11）

fofa MCP server（外部仓库 `E:\ILOVCTRY\SRC\src-strike\mcp-servers\fofa_MCP\fofa.py`）自己读的是**它自己目录**的 `.env`（`FOFA_KEY`），与平台「网络空间测绘」配置 `config/fofa.json` 是**两份、会漂移**。修法：平台在 `mcp_bridge._env_for_entry()` 对 `FOFA_SERVERS`（`fofa`/`fofa-search`/`fofa_mcp`）**注入来自 `config/fofa.json` 的 `FOFA_KEY`/`FOFA_EMAIL`**（`_fofa_env_from_config()` 实时读，绝不复制密钥）。fofa.py 优先取进程 env（`os.getenv`）且 `load_dotenv` 不覆盖已存在变量，故注入生效、其自带 `.env` 被旁路。`config/mcp.json` 条目的显式 `env`（如 `{"env":{"FOFA_KEY":"..."}}`）优先级更高，供刻意覆盖。**单一真相源恒为 `config/fofa.json`**——改 key 只需改测绘设置，MCP 立即跟随。

## fir-proxy-mcp/

- `fir_proxy_mcp.py` 单文件（stdio，`mcp.server.fastmcp`）：**薄客户端**——不自己管池、不起 serve，只把平台代理池 API（默认 `http://127.0.0.1:8420`）暴露成 12 个工具（`proxy_status/list/start/stop/rotate/select/add/remove/fetch/validate` + **`proxy_check_url`** + **`proxy_use`**〔2026-10-15 精确切换：`{"proxy":"host:port"}`〕），让 AI 取端点/IP 列表后自行 `curl -x`。`proxy_use` 补「判出可达即钉死」——顺序 `proxy_rotate` 是碰运气。**`proxy_check_url(url, limit=10, workers=50, probe_waf=True)`（2026-10-12）**：对**指定网站**做代理可达性 + WAF/封禁检测（提交 `/api/proxy/check-url` Job → `_poll_job` 轮询至终态），返回 `usable`（未被封禁的可用代理，≤limit，按延迟升序）供 AI 直接使用；场景=「拿到目标站，想知道哪些代理能访问它、哪些被它的 WAF 拦了」。
- **无 project_id（2026-10-11 全局化）**：代理池已是**全局单池**（进程级单 serve + `data/proxy/pool.json`，所有项目共享），工具打 `/api/proxy/*`，**无 `project_id` 形参**、平台**不注入 `PW_PROJECT_ID`**（`mcp_bridge.PROJECT_AGNOSTIC_SERVERS`）。平台基址可经 mcp.json 的 `env.CYBERSTRIKE_API` 覆盖。
- `config/mcp.json` 条目（**设置页 MCP 连接面板可开关**）；`domains:[pentest]`。启动 `python mcp-servers/fir-proxy-mcp/fir_proxy_mcp.py`——`python` 是**平台解释器占位**（`core/chat/mcp_bridge.py::_resolve_command` 解析为 `sys.executable`，本进程环境已带 `mcp`/`httpx`）。
  - **勿用 `uv run --active`**（2026-10-08 修）：`--active` 依赖环境变量 `VIRTUAL_ENV`，平台自 `启动平台.bat` 直起时为空 → uv 落到自管的无 `mcp` 解释器 → `tools/list` 失败、面板显「工具发现失败或为空」。`uv run --with 'mcp<2'` 临时环境实测 ~7s，逼近 8s 探活超时，亦不可取。
- **为什么是薄客户端**：平台 MCP server 是**按项目托管的外部子进程**，随会话/轮次起停；serve 与池若住进去会随 MCP 生死、多项目各起一个、UI 反向依赖 MCP 在线。详见 `docs/plans/proxy-pool-integration.md`。

## playwright-mcp/

- `package.json` 钉 `@playwright/mcp` **0.0.79**（本地固定安装，避免每次 `npx @latest` 联网拉取）。
- `bin/playwright-claude.mjs` **唯一启动器**：Claude Code 的 `.mcp.json` 与平台 `config/mcp.json` 的 playwright 条目都指向它。**改完需重启会话 / 重连 MCP 才生效**。

### 启动器要点

- **双模式**（`main()` 按环境变量分流）：
  - 平台注入 `PW_BROWSER_CDP_ENDPOINT` → **内嵌模式**：只连 `BrowserPool` 项目持久浏览器的 loopback CDP，**不起 Chrome、不占锁、不走空闲看门狗**（登录态/抓包链路与人工页面共享）。
  - 无该变量 → **自管/复用模式**：本启动器拉起或复用系统 Chrome（独立 profile + 锁 + CDP 端口）。
  - `PW_NO_CHROME=1` → **探针模式**：只 `tools/list`（平台装配 LLM 工具面用），不起浏览器。
- **会话隔离**：`PW_SESSION_ID` → 独立 profile 目录 `profile-<hash>` + 锁 `claude-<hash>-<port>.lock` + 按哈希错开起始端口（默认基址 **14000**）。修「多会话抢同一持久化 profile → Browser is already in use」。
- **空闲自动关闭（2026-10-07 改「无操作」口径，默认 30 分钟）**：代理 MCP stdio，在每次 `tools/call` 打点；连续 `PW_IDLE_MS`（默认 30min）无操作 → 关 Chrome + 删该会话 profile + 释放锁 + 退出，顺带释放并发配额。**判据是「有无操作」不是「有无活动页」**——旧口径只要挂着一个非 `about:blank` 页面就永不回收。仅自管/复用模式启用。
- 空闲时长显示口径见 `webui`（浏览器卡片「未操作 Ns」用 pool 的 `last_action_at`，与此看门狗无关）。

### 坑

- `--user-data-dir` 必须排在 `--remote-debugging-port` **之前**：否则机器上已有常规 Chrome 时，新进程因 Chrome 进程单例机制移交后直接退出，调试端口永不监听（表现为「CDP did not come up」并静默降级自托管、丢隔离）。
- Windows 的 **8421~9880** 是 Hyper-V/WinNAT 保留段（`netsh interface ipv4 show excludedportrange`）：Node 能绑定但 **Chrome 绑不上调试端口**，故基址取 14000 并在选端口时跳过该段。
- stdio 全托管给 `@playwright/mcp`，诊断只走 stderr（stdout 是 MCP 协议通道，勿污染）。
- `node_modules/` 不入库；`.playwright-mcp/`（snapshot/console 日志）已 gitignore。
