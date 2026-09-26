# 方案：桌面化壳（exe 双击开窗，Windows 原生）

- **状态**：**已实施（M1+M2 2026-09-21；M3 打包 2026-09-23，实施记录见 §6）**
- **拍板记录**：见 §3（7 项决策已确认）
- **关联代码**：`scripts/serve.py`（启动入口 + 优雅停机句柄）、`core/api/app.py`（create_app，**尚未挂 StaticFiles**）、`启动平台.bat` / `停止平台.bat`（现行双击入口）、`webui/`（React+Vite SPA）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§1 部署形态或新增小节），本文保留作方案背景

## 1. 愿景与背景

现状是「bat 拉起两个服务窗口 + 开浏览器」：`启动平台.bat` 起 `serve.py`（127.0.0.1:8420）与 Vite dev server（localhost:5173，仅 IPv6），就绪后自动开浏览器。目标形态：**双击一个 exe → 直接弹应用窗口**，无 cmd 窗口、无浏览器标签页。

项目架构对此天然友好：后端 FastAPI + 前端 SPA 本就是「本地服务 + 壳」形态，换壳不动核心逻辑；优雅停机链路（`POST /api/admin/shutdown` → shutdown 钩子给在跑会话落断点快照）已有现成语义，关窗时衔接即可。

**窗口与打包解耦**是本方案的主轴：pywebview 只是 pip 依赖，**源码状态即可弹窗**；打包只是把源码 + Python 环境固化成 exe 用于分发。因此里程碑重切为「M1 源码弹窗（开发测试即用）→ M2 静态托管 → M3 打包（后置）」。

## 2. 现状盘点（2026-09-21 核实）

- `serve.py`：uvicorn 单进程 + 持 Server 句柄注册 shutdown 端点（v0.64）；端口参数可传。
- `core/api/app.py` **无 StaticFiles**——前端完全依赖 Vite dev server（5173），`vite build` 产物从未被托管。
- 端口占用检测（已在监听则跳过）目前写在 bat 里，不在 Python 侧。
- 无任何桌面壳/打包设施；WebView2 运行时 Windows 11 全量自带（Win10 绝大多数自带，缺可提示装）。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 目标形态 | exe 双击直接出窗口（长期）；**源码弹窗（`serve.py --window`）提前为第一里程碑**，开发测试即用 |
| 2 | 技术主线 | **pywebview（WebView2）+ PyInstaller onedir**；Tauri 暂缓（分发对象就是自己这台 Windows，不值得引入 Rust 链） |
| 3 | 窗口/打包解耦 | 源码即可弹窗，M1 不依赖 M2/M3；开发期三形态并存：无窗浏览器（现状）/ 源码弹窗 / exe |
| 4 | 优先级 | **提前（二次拍板）：下一个实施计划**；原「低，不着急」作废 |
| 5 | 关窗行为 | **owner 关最后一窗 = 优雅停机**：`--window` 进程自己拉起的服务，最后一个窗口关闭触发 shutdown 落会话快照再退出；attach 附窗关闭仅退出自身；无窗 `serve.py` 模式照旧自主管理 |
| 6 | 二次双击 | **再开一个窗口连已有服务**（attach 新窗，探测 8420 已在跑则不建服务直接开窗，零跨进程通信；多窗并行可看不同页面） |
| 7 | 里程碑顺序 | M1 窗口壳 → M2 前端静态托管 → M3 打包（后置）；M1 可加载 Vite dev 5173，不受 M2 未做影响 |

## 4. 设计详述

### 4.1 三档路线对比

| 档 | 形态 | 改造内容 | 结论 |
|----|------|---------|------|
| 1. 伪桌面 | Edge 无边框独立窗口（`msedge --app=http://127.0.0.1:8420`） | 仅启动脚本改造 | **不单独做**；静态托管（M2）落地后它顺带可得 |
| 2. 真 exe | 双击 exe → pywebview 窗口 | M1 + M3 | **主线** |
| 3. Tauri 2 壳 | 同上，体积小/可托盘/自更新 | Rust 工具链 + sidecar | 暂缓，需分发他人时再评估 |

### 4.2 M1：窗口壳——源码弹窗最小闭环（已实施，见 §6）

- `serve.py --window`：子线程跑 uvicorn，主线程 `webview.start()`；`debug=True` 支持右键开 DevTools；`--url`/`--port` 参数透传。
- **owner / attach 双语义**（决策 #5/#6 的落地）：
  - 启动探测 8420：**未在跑 = owner**——拉起服务并跟踪活动窗数，最后一窗 `closed` 事件 → 置 `server.should_exit`（进程内直调句柄，不必走 HTTP）优雅停机；
  - **已在跑 = attach 附窗**——不建服务，仅开窗连过去，关窗只退出本进程；owner 的服务与窗数不受影响。
  - 无窗模式（不传 `--window`）行为完全不变。
- **URL 探测顺序**：`webui/dist` 存在 → 加载 8420 静态版；无 dist 且 5173 可达 → 加载 Vite dev（热更新照常）；都不满足 → 显式 `--url` 或窗内指引。
- 启动时 `os.chdir` 到脚本/exe 所在目录——**cwd 兜底是硬要求**：全项目大量 cwd 相对路径惯例（`config/`、`packs/`、`workspaces/`、providers.json 等），从快捷方式/别处启动时路径不能飘。
- WebView2 缺失检测 → 提示安装一次。
- 可选：新增「启动平台（窗口）.bat」——起服务后开窗口替代开浏览器（是否本里程碑做见待打磨 #2）。

### 4.3 M2：前端静态托管（共同地基）

- `vite build` 产物由 FastAPI StaticFiles 托管 + SPA history fallback（非 `/api`、非 `/docs` 路径回 index.html）。
- `serve.py` 启动时检测产物存在 → 单进程自足；**开发期保留 Vite dev 模式**（bat 分支：有产物走静态、无产物走 dev）。
- 前端清理写死 5173 的硬编码；前后端同源后 CORS/代理配置消失，WS 与 API 前缀核对一遍。

### 4.4 M3：打包（后置，不着急）

- **干净 venv 只装 requirements 打包**，不打 Miniconda 全家（体积爆炸）；onedir 形态预期 100-150MB。
- 资源目录随包：webui 构建产物、`packs/`、`config/`（种子）、tools 脚本；`workspaces/` 首启自动建。
- 未签名 exe 的 SmartScreen 首次告警：自用「仍要运行」即可，代码签名后置不做。

### 4.5 边界与不受影响项

- 外部工具依赖（Docker/WSL2/IDA/Playwright/MCP）本就是按能力降级的外部件，exe 化不改变接入方式。
- SQLite WAL + 黑板单一写入口不受影响；WS 在 WebView2 下正常工作。
- 不做跨平台（Windows 原生优先）、不做自动更新、Tauri 暂缓。

## 5. 待打磨清单（实施时消化，2026-09-21）

1. **URL 都不可达的窗内指引**：✅ 取**内嵌占位 HTML**（深色风指引页：构建产物 `npm run build` / 起 Vite dev / 后端 8420 三条路，附「本窗口关闭不影响已运行的服务」说明）——自包含、不丢上下文，胜过回退系统浏览器。
2. **「启动平台（窗口）.bat」**：✅ 随 M1 一并交付——pythonw 起 `serve.py --window`（无控制台黑窗，日志落 serve-window.log），pythonw 缺失退 python.exe；体量十几行。
3. **M2 bat 退役节奏**：✅ 「无产物走 dev」分支**保留**（开发期 HMR 不可替代，`npm run build` 后自动切静态，无感）；`停止平台.bat` **保留**（无窗模式与 dev 模式仍需要，窗口模式关最后一窗即优雅停机、两者并存）。
4. **M3 Playwright 是否进包**：✅ **不进包**（维持倾向——exe 外按需安装，首启检测+提示；spec excludes 已落 playwright）。
5. **M3 workspaces 初始化随包策略**：✅ **不随包，首启自建**（ProjectStore mkdir / ProviderStore 种子 / CampaignMemory；打包产物零用户数据，copy_resources 不含 workspaces/）。
6. **前端 5173/8420 硬编码清点**：✅ 已全扫——`webui/src` 零硬编码（全部相对路径 `/api/*` + `location.host` 拼 WS），仅 vite.config.ts dev 代理与端口配置（开发期保留，M2 不动前端）；同源后 CORS/代理问题天然消失。

## 6. 实施记录

- **M1 窗口壳（已实施 2026-09-21）**：`scripts/serve.py` 重写——argparse（port 位置参数兼容旧用法 + `--window/--url/--debug`）；owner/attach 双语义（socket 探测 127.0.0.1:port）；owner=子线程 uvicorn（等 `server.started` 最多 30s）+ 主线程 `webview.start()`，`window.events.closed` 计数、最后一窗进程内置 `should_exit`（webview.start 返回后 join(20s) 等快照落盘）；URL 探测 `_pick_url`（--url > dist 静态 > 5173〔::1/IPv4 双探测〕> 占位页）；WebView2 注册表探测（HKLM WOW6432Node/HKCU 三键）缺失弹窗+回退无窗；pywebview 未装同回退；pythonw stdout/stderr 为 None → 重定向 serve-window.log；`os.chdir(项目根)` cwd 兜底（frozen 分支 M3 预留）。「启动平台（窗口）.bat」新增（pythonw 无控制台）。**真机冒烟**：attach 关窗只退自己（服务 8420 不动）/ owner 窗口加载静态版全链 200（assets+api 流量见 uvicorn 日志）/ PostMessage WM_CLOSE → 优雅停机端口释放 → 进程 exit 0。
- **M2 静态托管（已实施 2026-09-21）**：`create_app(static_dir=)` ——兜底 GET 路由注册序最后（API/WS/docs 先匹配）；真实文件 resolve 防穿越直出、其余回 index.html；`/api|/docs|/redoc|/openapi.json` 例外 404；缺 index.html 抛 ValueError 防半挂载。`serve.py` 自动探测 dist 决定 static_dir。前端零改动（本就走相对路径）。「启动平台.bat」改造：有 `webui\dist\index.html` → 只起后端开 8420 静态版；无 → 回退原 Vite dev 全流程。测试 `test_api.py::test_spa_static_hosting`（直出/fallback/例外/防穿越/真实 API 优先/坏 static_dir 报错）。真机 curl 冒烟六项全过；全量回归 767 passed。
- **M3 打包（已实施 2026-09-23）**：`scripts/build_exe.py` 四步一键（ensure_frontend dist 缺失自动 npm build → ensure_venv 干净 `.build-venv` 只装 8 个 BUILD_DEPS〔不打 Miniconda 全家，§4.4 定稿〕→ `python -m PyInstaller cyberstrike-pro.spec` → copy_resources packs/tools/webui.dist 随包，COPY_IGNORE 排 .history/__pycache__/kb-backups/.trash）+ 根目录 `cyberstrike-pro.spec`（onedir + windowed；hiddenimports collect_submodules("uvicorn") + collect_all("webview"/"clr_loader") + collect_submodules("pythonnet")——webview 官方 hook 之外的兜底；excludes playwright/tkinter/pytest）+ serve.py frozen 分支（`_ROOT=Path(sys.executable).parent`、frozen 强制 window=True 双击即弹窗、`_redirect_stdio_if_none()` windowed 下 stdout=None 落 serve-window.log）。产物 dist/cyberstrike-pro/ **59MB**（干净 venv 比预估 100-150MB 精简）。config/、workspaces/ 不随包首启自建（ProviderStore 种子/ProjectStore mkdir），敏感凭据绝不进包。**真机冒烟**：双击 owner 起服务 8420 + 静态托管全 200 + 首启自建 config/intel、providers.json、workspaces/campaign.db + WM_CLOSE 优雅停机端口释放。
  - **钉版教训**：venv `pip install "fastapi>=0.110"` 拉到 0.12x——其移除 Starlette `add_event_handler`（app.py shutdown 钩子所用）→ 冒烟即炸；BUILD_DEPS 钉 `fastapi==0.116.1`、`uvicorn==0.35.0`（与开发环境一致）。
  - **工程坑**（复打必读）：①`--specpath` 不允许与 .spec 文件同用（makespec 专属选项）；②shell 管道 `python … | tail` 吞真实退出码（tail 成功即 0），PyInstaller 失败被掩盖——看结果要前台重跑；③PyInstaller 重建先清空 dist/cyberstrike-pro/（资源随包一并被删）→ 重跑 build_exe.py（幂等）补回；④真 windowed（双击）与 bash 管道起跑的 stdout 语义不同，serve-window.log 只在前者落盘——冒烟误报「日志缺失」虚惊一例。
