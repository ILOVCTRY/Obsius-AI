# core/proxy/

> 代理池服务（fir-proxy 托管，2026-10-07，方案 `docs/plans/proxy-pool-integration.md`）：项目级常驻，仿 `core/browser/pool.py` 的 `BrowserPool`。拥有 fir-proxy serve 子进程生命周期 + 池记录；人类 UI 与 AI（经 MCP 控制面）**共享同一池**。

## 文件

- `pool.py` 单文件。`ProxyConfig`（`config/proxy.json` 缺文件全默认、坏文件容错；`http_port_base=1800`/`socks5_port_base=1801`/`control_port_base=1810`、`region`、`max_latency_ms`、`request_rotation_count`、`target_failover_threshold`、`startup_timeout_s=40`、`python`（None=自动解析）、`auto_start`、**`validate_timeout_s=5`、`validate_target="http://www.baidu.com"`（验证目标 URL，经代理 GET 它）、`prune_latency_ms=5000`（None/0=不按延迟删）、`sources_file`（None=默认 `config/proxy_sources.json`，不存在则用 runner 内置精选）、`fetch_limit=2000`（单次抓取条数上限，按协议轮转取；0=不限）**）。
- `ProxyPool`（项目级，`app.state.proxy_pool`）：`start/stop/status/list_proxies/rotate/select` + 池记录 `read_pool/write_pool/add_records/remove_records/replace_pool` + 抓取 `fetch`（走平台 runner `tools/fir-proxy/cyberstrike_fetch.py`，受 `fetch_limit` 限量，**默认抓取后自动验证并剔除失效**）+ 验证 `validate`（子进程调平台 runner `tools/fir-proxy/cyberstrike_check.py`——**单目标连通性**：经代理 GET `validate_target`，通即 `Working` 并记延迟；**默认验证后剔除失效**，经 `_apply_validation` 按地址 merge 回写而非整体覆盖）+ 清理 `close_project/close_all`。
- `_is_usable(record, prune_latency_ms)` / `_prune(records, ...)` 模块级纯函数：验证后判据 = `status=="Working"` 且（阈值关 或 延迟有限且 `latency*1000<=阈值`）；延迟缺失/非有限时保留（Working 已过连通性检查，仅因缺延迟不判死）。
- `select_records` 纯函数（按评分挑代理，镜像 fir-proxy `cli select`）；`proxy_available(tools_root, python)` 能力探测（serve/fetch/check 三 runner + cli 在场 + 解释器可 `import socks,requests,bs4,lxml`，结果按解释器路径缓存）；`resolve_python` 解析顺序 `tools/venv` → `sys.executable`（须是 python）→ PATH。
- `_progress_reporter(on_progress)` 纯函数：返 `report(phase, done, total, message)`，算 ETA 后回调进度 dict（`phase`/`done`/`total`/`eta_seconds`/`elapsed_seconds`/`rate_per_second`/`message`，形状对齐 `core/tools/decompiler.py`）；相位切换重置计时，`on_progress=None` 返 no-op。`fetch`/`validate` 用它汇报 **fetch→check→prune** 相位（抓取阶段按源数、验证阶段按代理数）。

## 关键约定

- **不动上游**：serve 子进程跑 `tools/fir-proxy/cyberstrike_serve.py`（本仓新增的薄 runner，包上游 `ProxyRotator`/`ProxyServer`），**不改 fir-proxy 自身文件**。上游 `cli.py serve` 无运行中控制口，runner 补 loopback 控制通道（`/status` `/proxies` `/rotate` `/reload` `/stop`，只绑 127.0.0.1）。
- **路径全绝对**：子进程 cwd=`tools/fir-proxy`，故 runner/cli/池文件一律传 `.resolve()`（相对路径会二次拼接找不到）。
- **池记录**落 `<项目>/proxy/pool.json`（JSON 数组），日志 `<项目>/proxy/serve.log`；**按 `proxy` 地址去重**（上游 `add_proxy` 语义）。
- **端口**：三端口按项目**动态取空闲口**（`_free_port` 从 base 起探测 bind），多项目不冲突。
- **AI 用法**：`status()` 给 http/socks5 端点，AI 自行 `curl -x`；平台**不**注入 browser/run_cmd 的代理。
- **运行中 Job 可查**：`GET /proxy/status` 由 API 层扫 `app.state.jobs` 附带本项目 `proxy-fetch`/`proxy-validate` 的 `job{id,kind,progress}`（进度 dict 是 meta 内引用，读到即最新）——前端切页回来据此重挂 `pollJob`，**进度不丢**。
- **缺依赖=明确报错**：`proxy_available` False → `start`/`fetch`/`validate` 抛 `ProxyError` → API 层 503 结构化，**不静默降级**。

## 生命周期接线（core/api/app.py）

装配 `app.state.proxy_pool = ProxyPool(workspace_root, tools_root or "tools", config=ProxyConfig.from_file("config/proxy.json"))`；`add_event_handler("shutdown", ...)` 兜关；`delete_project` 前置链在 `browser_pool.close_project` 之后 `proxy_pool.close_project(pid)`（先停 serve 释放池文件/日志句柄，再 rename 目录）。

## 坑

- **Windows 编码**：runner 启动时强制 stdout/stderr UTF-8（默认 GBK 会把 JSONL 里的中文写坏）。
- **stdout 是解析通道**：runner 只往 stdout 写 JSONL 事件（`started`/`stopped`/`error`），日志走 stderr；`_Serve` 的读线程靠 `started` 事件放行 `start()`。
- **PySocks 必需**（`modules/server.py` `import socks`）；`ttkbootstrap` 仅上游 GUI 用，平台不需要。
- **验证判据=单目标连通性（2026-10-08，二次改）**：`validate` / `fetch(auto_validate=True)` 经平台 runner `cyberstrike_check.py` **只做一件事**——经代理 GET `validate_target`，拿到响应即 `Working`（记延迟），否则 `Failed`。**不用**上游 `cli.py validate`（其判据要求 HTTPS-CONNECT + httpbin + 测速**全过**，免费代理实测 120 只过 9 → 池被清空）。随后 `status≠Working` 或 `延迟>prune_latency_ms` 的**物理删除**；`_apply_validation` 按 proxy 地址 merge 验证结果回池（本次未验证到的保留原样防误删 / 并发新增不被覆盖），serve 在跑时只发一次 `/reload {"remove":[...]}`。prune 后池可能变空——serve 保持运行、`working=0`，**不自动停服**。
- **不再走上游 checker**：验证走 `cyberstrike_check.py`（复用上游 `ProxyChecker.check_proxy_url` 单目标检查），故上游 TCP 预检 `min(1.5, timeout)` 与三段判据均不参与。想加严（要求 HTTPS/CONNECT 能力）把 `validate_target` 设成 `https://` 目标即可。
- 临时文件名带 uuid（`validate-in-<uuid>.json` / `cli-<uuid>.json`），防 fetch 自动验证与手动验证并发互覆。
- **流式 `_run_cli`（2026-10-08）**：`on_progress=None` 走 `subprocess.run`（**零行为变化**）；非 None 走 `_exec_streaming`——`Popen(stdout=PIPE, stderr=STDOUT)` 逐行读，`{"event":"progress",...}` 行进回调、其余进 tail，超时用 `threading.Timer` 到点 kill。进度经 **job `meta.progress` 可变 dict 引用**（API 端点 `progress.update` 当回调）透出前端。
- **抓取超时 600s**：`fetch` 默认 `timeout=600`（实测全源抓取 ~180s，原 180 会误判超时）；验证子进程用 `validate_timeout`（默认 600）。
