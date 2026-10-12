# core/proxy/

> 代理池服务（fir-proxy 托管，2026-10-07，方案 `docs/plans/proxy-pool-integration.md`）：**全局单池（2026-10-11 由项目级改造）**——平台进程持有**一个** serve 子进程 + **一份**池文件（`data/proxy/pool.json`），**所有项目共享**；人类 UI 与 AI（经 MCP 控制面）共享同一池。方法**无 pid 参**。

## 文件

- `pool.py` 单文件。`ProxyConfig`（`config/proxy.json` 缺文件全默认、坏文件容错；`http_port_base=1800`/`socks5_port_base=1801`/`control_port_base=1810`、`region`、`max_latency_ms`、`request_rotation_count`、`target_failover_threshold`、`startup_timeout_s=40`、`python`（None=自动解析）、`auto_start`、**`validate_timeout_s=5`、`validate_target="http://i.zut.edu.cn/new/index.html"`（验证目标 URL，经代理 GET 它；2026-10-12 由 baidu 改为 i.zut.edu.cn，口语**http 明文**故不大量剔除 HTTP-only 代理）、`validate_target_https="https://i.zut.edu.cn/new/index.html"`（HTTPS 能力探测目标，经 CONNECT，指向同站；None/空=不探测）、**`validate_target_b="http://example.com"`（内容注入交叉验证第二目标〔须与 validate_target 内容不同〕；None/空=不做校验）**、`prune_latency_ms=5000`（None/0=不按延迟删）、`sources_file`（None=默认 `config/proxy_sources.json`，不存在则用 runner 内置精选）、`fetch_limit=2000`（单次抓取条数上限，按协议轮转取；0=不限）**、**自动巡检（2026-10-11）`maintenance_interval_s=1800`（30 分钟；0=不巡检）/`auto_fetch_min_http=50`/`auto_fetch_min_https=20`/`auto_fetch_min_socks=20`（低于即自动抓取；0=该协议不补抓）/`auto_fetch_cooldown_s=600`（同协议两次自动抓取最小间隔）**）。
- `ProxyPool`（**全局单实例**，`app.state.proxy_pool`）：单 `_serve` + 单 `_lock`；`start/stop/status/list_proxies/rotate/select` + 池记录 `read_pool/write_pool/add_records/remove_records/replace_pool` + 抓取 `fetch`（走平台 runner `tools/fir-proxy/cyberstrike_fetch.py`，受 `fetch_limit` 限量，**默认抓取后自动验证并剔除失效**）+ 验证 `validate`（子进程调平台 runner `tools/fir-proxy/cyberstrike_check.py`——**单目标连通性**：经代理 GET `validate_target`，通即 `Working` 并记延迟；**默认验证后剔除失效**，经 `_apply_validation` 按地址 merge 回写而非整体覆盖）+ **指定网站检测 `check_url`（2026-10-12）** + 清理 `close_all`（shutdown 兜关，停全局 serve）。**`check_url(url, *, workers, timeout, limit, probe_waf, on_progress)`**：用池内**全部**代理逐一 GET `url`，与平台**直连基线**对比判定「可达 / 被该站 WAF 封禁」（直连被拦时不误判）；走 `_run_cli(..., as_dict=True)`（顶层 `{url,baseline,results}` 不下钻）读 runner 结果，返回 `{url,total,reachable,blocked,usable,results,baseline}`，`usable`=未封禁可达代理按延迟升序取 `limit` 个（供 AI）。**只读报告，绝不写池**（换目标就误删代理无意义）。**路径**：`data_dir()` = `<workspace_root>/../data/proxy/`（与 `data/campaign.db` 同款全局归位契约，**不落 workspaces/**）；`pool_path()` / `_log_path()` / `_run_cli` 的 `cli-*.json` / `_validate_records` 的 `validate-in-*.json` 全落此。**`merge_legacy_pools()`（首启一次性迁移）**：扫 `<workspace_root>/*/proxy/pool.json` 按 proxy 地址去重并入全局（旧记录优先不覆盖；**仅全局池空时执行**，幂等），旧项目池文件**原地保留不删**；`create_app` 装配后显式调一次。**`last_active`**：每次 `status/start` touch，留作 `ProxyConfig.idle_timeout_s` 空闲回收判据（默认 `None`=不回收，**不引 reaper 线程**）。
- `_is_usable(record, prune_latency_ms)` / `_prune(records, ...)` 模块级纯函数：验证后判据 = `status=="Working"` 且（阈值关 或 延迟有限且 `latency*1000<=阈值`）；延迟缺失/非有限时保留（Working 已过连通性检查，仅因缺延迟不判死）。
- **列表视图白名单 `_PROXY_VIEW_KEYS`（坑，2026-10-11）**：`_proxy_view` 只投影白名单键，`list_proxies`（`/api/proxy/proxies` 的 file 路径）走它；**漏 `https_ok` 曾致前端 HTTPS 过滤恒空**（计数来自 `read_pool` 故正常显示 54）——新增字段务必同步白名单；serve runner `cyberstrike_serve.py::_proxy_view` 同款白名单亦已补 `https_ok`（running 时 source=serve 路径）。
- **协议计数与阈值（2026-10-11，均纯函数）**：`SOCKS_PROTOCOLS={socks4,socks5}`；`protocol_counts(records, prune_latency_ms) -> {http, socks, https, total}`——`http`=`protocol=="http"` 且可用、`socks`=`protocol in SOCKS_PROTOCOLS` 且可用、`https`=`https_ok is True` 且可用（**HTTPS 能力叠加，非协议类目**；池本无 https 代理类目，上游把 https 源并入 http）；`auto_fetch_targets(counts, cfg) -> list[list[str]]`——http 不足→`["http"]`、socks 不足→`["socks4","socks5"]`、https 不足→`["http"]`（https 靠 http 抓取后的验证探测补齐；与 http 不足**合并**为一条），阈值 `<=0` 表示该协议不自动补抓。`status()` 出口带 `counts`（前端过滤 + 阈值展示）。
- `select_records` 纯函数（按评分挑代理，镜像 fir-proxy `cli select`）；`proxy_available(tools_root, python)` 能力探测（serve/fetch/check 三 runner + cli 在场 + 解释器可 `import socks,requests,bs4,lxml`，结果按解释器路径缓存）；`resolve_python` 解析顺序 `tools/venv` → `sys.executable`（须是 python）→ PATH。
- **`write_config_file(path, updates)`（2026-10-12，前端配置入口）**：读-合并-整写 `config/proxy.json`——只覆盖传入的 ProxyConfig 字段，保留文件里其它键（逐机覆盖的端口/阈值），原子写（临时文件+replace）；文件不存在/坏文件按空 dict 起步。API 层 `PUT /api/proxy/config` 用它 + `ProxyConfig.from_file` 重建，**热更新** `app.state.proxy_pool.config`（下轮验证/抓取即生效，无需重启）。`create_app(proxy_config_path=...)` 可注入路径（测试落 tmp，防污染仓库 config/）。
- `_progress_reporter(on_progress)` 纯函数：返 `report(phase, done, total, message)`，算 ETA 后回调进度 dict（`phase`/`done`/`total`/`eta_seconds`/`elapsed_seconds`/`rate_per_second`/`message`，形状对齐 `core/tools/decompiler.py`）；相位切换重置计时，`on_progress=None` 返 no-op。`fetch`/`validate` 用它汇报 **fetch→check→prune** 相位（抓取阶段按源数、验证阶段按代理数）。

## 关键约定

- **不动上游**：serve 子进程跑 `tools/fir-proxy/cyberstrike_serve.py`（本仓新增的薄 runner，包上游 `ProxyRotator`/`ProxyServer`），**不改 fir-proxy 自身文件**。上游 `cli.py serve` 无运行中控制口，runner 补 loopback 控制通道（`/status` `/proxies` `/rotate` `/reload` `/stop`，只绑 127.0.0.1）。
- **路径全绝对**：子进程 cwd=`tools/fir-proxy`，故 runner/cli/池文件一律传 `.resolve()`（相对路径会二次拼接找不到）。
- **全局化回归坑（2026-10-11）**：`data_dir()` 用 `workspace_root.parent/"data"/"proxy"`（workspaces 顶层只允许项目目录 + `.trash`，勿把池塞回去）；`_free_port` 的 `taken` 集随单 serve 无冲突。旧 `<项目>/proxy/pool.json` **已是死数据**（不再读写），可留可删。
- **池记录**落 `data/proxy/pool.json`（JSON 数组），日志 `data/proxy/serve.log`；**按 `proxy` 地址去重**（上游 `add_proxy` 语义）。
- **端口**：三端口从 base 起**动态取空闲口**（`_free_port` 探测 bind）；单 serve 已无「多项目冲突」问题。
- **AI 用法**：`status()` 给 http/socks5 端点，AI 自行 `curl -x`；平台**不**注入 browser/run_cmd 的代理。**MCP 侧无 project_id**——`fir-proxy` 是全局域 server（`MCPBridge.PROJECT_AGNOSTIC_SERVERS`），**不注入 `PW_PROJECT_ID`**，工具打 `/api/proxy/*`。
- **运行中 Job 可查**：`GET /api/proxy/status` 由 API 层扫 `app.state.jobs` 附带 `proxy-fetch`/`proxy-validate` 的 `job{id,kind,progress}`（**全局池不再按项目过滤**；进度 dict 是 meta 内引用，读到即最新）——前端切页回来据此重挂 `pollJob`，**进度不丢**。
- **缺依赖=明确报错**：`proxy_available` False → `start`/`fetch`/`validate` 抛 `ProxyError` → API 层 503 结构化，**不静默降级**。

## 生命周期接线（core/api/app.py）

装配 `app.state.proxy_pool = ProxyPool(workspace_root, tools_root or "tools", config=ProxyConfig.from_file("config/proxy.json"))`，**随即 `merge_legacy_pools()`**（首启并入旧项目池）；`add_event_handler("shutdown", ...)` 兜关（置巡检 Event + `close_all` 停全局 serve）。**`delete_project` 前置链已删除代理池一环**（全局化后池/日志落 `data/proxy/`、与项目工作区无句柄关系，无需停服释放）。

**serve 子进程生命周期（2026-10-12 事故）**：`start()` 起的 serve 纳入 **Windows 作业对象**（`create_kill_on_close_job`/`assign_to_job`，`_Serve.job` 持句柄；`_kill` 释放）——后端进程退出时 `KILL_ON_JOB_CLOSE` 随句柄自动焚毁 serve，**根治「后端重启→serve 变孤儿」**（旧实现是每项目一个池，历史遗留 27 个孤儿进程）。存量清理：`reap_orphan_serves()` 扫全进程，杀「命令行含本 runner + `-i` 池路径 **且父进程 pid 已不存在**」的 serve（`_pid_alive` 判活，非 Windows/枚举失败返回 []）。`create_app` 在 `proxy_maintenance` 开关（测试默认关）下启动调一次；`App` 启停双保险：作业兜自动、reap 治存量。

## 自动巡检（2026-10-11，api 层编排）

`create_app(proxy_maintenance=True)` 起 daemon 线程 `proxy-maintenance`（测试默认关，同 `session_containers`）；`shutdown` 钩子置 `app.state._proxy_stop` 停。编排在 **api 层**（`core.api.app`）而非 pool.py——因为要复用 `JobRegistry`（前端状态栏靠既有 `_proxy_running_job` 就能看到自动验证/抓取进度，无需新通道）。两函数：
- `_proxy_maintenance_tick(app)`（**同步、可直测**）：① 有 `proxy-fetch`/`proxy-validate` 在跑→本轮跳过（不并发）；② 提交 `proxy-validate` Job 并**等其完成**（上限 1h 防卡）；③ `_proxy_auto_fetch`。
- `_proxy_auto_fetch(app, pool)`：`protocol_counts` → `auto_fetch_targets` → 逐协议组提交 `proxy-fetch`，受 `app.state.proxy_fetch_cooldown`（协议组→上次时刻）按 `auto_fetch_cooldown_s` 节流。
- `_proxy_maintenance_loop`：`while not stop.wait(interval): tick()` 吞异常（绝不炸线程）。`maintenance_interval_s<=0` = 不巡检。

## 坑

- **Windows 编码**：runner 启动时强制 stdout/stderr UTF-8（默认 GBK 会把 JSONL 里的中文写坏）。
- **stdout 是解析通道**：runner 只往 stdout 写 JSONL 事件（`started`/`stopped`/`error`），日志走 stderr；`_Serve` 的读线程靠 `started` 事件放行 `start()`。
- **PySocks 必需**（`modules/server.py` `import socks`）；`ttkbootstrap` 仅上游 GUI 用，平台不需要。
- **验证判据=单目标连通性（2026-10-08，二次改）+ 内容注入交叉验证（2026-10-11）**：`validate` / `fetch(auto_validate=True)` 经平台 runner `cyberstrike_check.py` 做两件事——① **连通性**：经代理 GET `validate_target`，拿到响应即 `Working`（记延迟），否则 `Failed`；② **内容注入/劫持型出口交叉验证**（`validate_target_b` 非空时）：对已通记录再读 `validate_target`/`validate_target_b` 两目标的响应体指纹，**两目标内容不同却回相同指纹 → 判 hijacked、status=Failed**（识破「对任意 host 回同一伪造页」的劫持出口；探测失败不误杀）。**不用**上游 `cli.py validate`（其判据要求 HTTPS-CONNECT + httpbin + 测速**全过**，免费代理实测 120 只过 9 → 池被清空）。随后 `status≠Working` 或 `延迟>prune_latency_ms` 的**物理删除**；`_apply_validation` 按 proxy 地址 merge 验证结果回池（merge_keys 含 `https_ok`/`hijacked`；本次未验证到的保留原样防误删 / 并发新增不被覆盖），serve 在跑时只发一次 `/reload {"remove":[...]}`。prune 后池可能变空——serve 保持运行、`working=0`，**不自动停服**。
- **不再走上游 checker**：验证走 `cyberstrike_check.py`（复用上游 `ProxyChecker.check_proxy_url` 单目标检查），故上游 TCP 预检 `min(1.5, timeout)` 与三段判据均不参与。想加严（要求 HTTPS/CONNECT 能力）把 `validate_target` 设成 `https://` 目标即可。
- 临时文件名带 uuid（`validate-in-<uuid>.json` / `cli-<uuid>.json`），防 fetch 自动验证与手动验证并发互覆。
- **流式 `_run_cli`（2026-10-08）**：`on_progress=None` 走 `subprocess.run`（**零行为变化**）；非 None 走 `_exec_streaming`——`Popen(stdout=PIPE, stderr=STDOUT)` 逐行读，`{"event":"progress",...}` 行进回调、其余进 tail，超时用 `threading.Timer` 到点 kill。进度经 **job `meta.progress` 可变 dict 引用**（API 端点 `progress.update` 当回调）透出前端。
- **抓取超时 600s**：`fetch` 默认 `timeout=600`（实测全源抓取 ~180s，原 180 会误判超时）；验证子进程用 `validate_timeout`（默认 600）。
