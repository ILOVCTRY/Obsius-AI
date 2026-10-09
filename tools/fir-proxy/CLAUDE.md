# tools/fir-proxy/

> 代理池工具（vendor 自外部 fir-proxy 1.2，2026-10-07）：抓取/验证/筛选/轮换代理 + 本地 HTTP/SOCKS5 转发服务。**平台经 `core/proxy/pool.py` 托管它的 serve 子进程**；本目录是上游代码 + 三个薄 runner（serve/fetch/check），别把平台逻辑放这里。

## 目录

```
fir-proxy/
├─ cyberstrike_serve.py   # ★ 平台入口（本仓新增）：包 ProxyRotator/ProxyServer + loopback 控制通道
├─ cyberstrike_fetch.py   # ★ 平台入口（本仓新增）：可配置源清单的抓取 runner（覆盖上游 ProxyFetcher 源列表）
├─ cyberstrike_check.py   # ★ 平台入口（本仓新增）：宽松单目标连通性验证 runner（取代上游 cli.py validate 的严判据）
├─ cli.py                 # 上游 AI 友好 CLI（fetch/validate/check-url/normalize/select/serve）
├─ modules/               # 上游核心：rotator(池/轮换/评分) / server(HTTP+SOCKS5 转发+故障切换)
│                         #            fetcher(在线源+网页抓取) / checker(验证) / asset_searcher(fofa/hunter)
├─ main.py hq.py xdl.py   # 上游 Tkinter GUI 与旧独立脚本（平台不用，保留供人手工跑）
├─ config.json            # 上游 GUI 配置（fofa/hunter key 已清空，勿提交真实 key）
└─ requirements.txt       # requests[socks] / ttkbootstrap / beautifulsoup4 / lxml
```

## cyberstrike_serve.py（平台唯一入口）

上游 `cli.py serve` 是**一次性、文件驱动**的长驻进程，**没有运行中控制口**——查不到实时当前代理、无法手动轮换。本 runner 包 `ProxyRotator`/`ProxyServer` 补上：

- `GET /status` → 服务状态 + 当前代理 + 池计数 + 地区分布
- `GET /proxies` → 池内全部代理记录（UI 列表 / AI 取可用 IP）
- `POST /rotate` → 强制轮换到下一个代理
- `POST /reload` → 热增删代理（`{"add":[…],"remove":["host:port"]}`）
- `POST /stop` → 停服务并退出

**约定**：控制通道**只绑 127.0.0.1**；stdout 走 JSONL（`started`/`stopped`/`error`，平台解析通道），日志走 stderr；启动时强制 stdout/stderr UTF-8（Windows 默认 GBK 会把 JSON 里的中文写坏）。

## cyberstrike_fetch.py（平台抓取入口，2026-10-08）

上游 `cli.py fetch` 的源清单**硬编码**在 `modules/fetcher.py::ProxyFetcher`（含已停服的 openproxylist.xyz、不稳的 proxyscan.io、5 个第三方自建 `/fetch_all` 口=供应链风险）。本 runner **只替换实例属性**（`online_sources`/`scraping_sources`），**不改上游文件**：

- 内置精选 `DEFAULT_SOURCES`（proxyscrape v3 / TheSpeedX/PROXY-List / monosans/proxy-list / proxy-list.download / geonode JSON）+ `DEFAULT_SCRAPERS`（6 个爬虫，剔除表结构易变的 free-proxy-list.net）。
- `--sources <json>` 覆盖：`{api:{http:[...],socks5:[...]}, scrapers:[短名...]}`（`scrapers:[]`=不跑爬虫，未给的协议沿用默认）；缺/坏文件回落内置默认。
- 接口兼容 cli.py：`-o/--output-format/--quiet/--protocol`；`--limit N` 限量（按协议轮转取，纯函数 `limit_records`，0=不限）；输出 `-o` JSON 数组 `[{protocol,proxy}]`。
- **进度**：drain `log_queue` 的守护线程按「每源一条终止行 `[+]`/`[-]`/`[!]`」计数，stdout 发 `{"event":"progress","phase":"fetch","done":k,"total":源数}`（平台流式解析 → 状态栏）。
- 纯函数 `build_sources(config)` / `limit_records(...)` 可离线直测（`tests/test_proxy_pool.py`）。
- 平台侧 `core/proxy/pool.py::fetch` 调用它（受 `fetch_limit` 限量），**默认抓取后自动验证并清理失效**（见 `core/proxy/CLAUDE.md`）。

## cyberstrike_check.py（平台验证入口，2026-10-08）

上游 `cli.py validate` 判据过严：要求代理**同时**通过 HTTPS(CONNECT) 延迟检测 + httpbin 匿名检测 + cachefly 测速，任一失败即 Failed——只会明文转发 HTTP 的免费代理被误判死（实测同批 120 条：宽松单目标过 52、上游完整三段只过 9 → 平台池被清空）。本 runner 换成**单目标连通性**判据，**复用上游 `ProxyChecker.check_proxy_url`**（**不改上游文件**），只在其外层计时得 latency：

- 经代理 `GET <--target>`（默认 `http://www.baidu.com`，走 80 口、不强制 CONNECT），拿到 2xx/3xx 即 `Working` 并记延迟；否则 `Failed`。
- 纯函数 `check_records(records, target, timeout, workers, checker_factory=None, on_progress=None)` 可注入假 checker 离线直测（`tests/test_proxy_pool.py`）。
- **进度**：每完成 ~20 条 stdout 发 `{"event":"progress","phase":"check","done":k,"total":N}`（平台流式解析 → 状态栏 + ETA）。
- 接口兼容 `_run_cli`：`-i/--input`、`-o/--output`、`--output-format`、`--target`、`--timeout`、`--workers`、`--quiet`、`--log-file`；输出 `-o` JSON 数组 `[{protocol,proxy,status,latency,error}]`（**含 Failed**，供上层剔除）。
- 平台侧 `core/proxy/pool.py::_validate_records` 调用它；判据口径见 `core/proxy/CLAUDE.md`。

## 依赖与解释器

- **CLI 路径只需 `requests[socks]` + `beautifulsoup4` + `lxml`**（`ttkbootstrap` 仅 GUI 用）。
- 平台侧 `core/proxy/pool.py` 自动解析解释器（`tools/venv` → `sys.executable` → PATH），缺依赖时 `proxy_available()` 返回 False、服务不启动（不静默降级）。

## 坑

- **路径**：平台起子进程时 cwd=本目录，故 runner/cli/池文件一律传**绝对路径**（相对路径会二次拼接找不到）。
- **PySocks 必需**：`modules/server.py` `import socks`，缺则 serve 起不来。
- **池去重按 `proxy` 地址**（`ProxyRotator.add_proxy`），同地址不同协议只留一条。
- `config.json` 的 fofa/hunter key 属敏感信息，**绝不入库**（当前已清空）。
