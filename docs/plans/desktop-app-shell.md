# 方案：桌面化壳（exe 双击开窗，Windows 原生）

- **状态**：讨论收敛，待打磨（2026-09-21）；**优先级低，不着急**（用户定调）
- **拍板记录**：见 §3（4 项决策已确认）
- **关联代码**：`scripts/serve.py`（启动入口 + 优雅停机句柄）、`core/api/app.py`（create_app，**尚未挂 StaticFiles**）、`启动平台.bat` / `停止平台.bat`（现行双击入口）、`webui/`（React+Vite SPA）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§1 部署形态或新增小节），本文保留作方案背景

## 1. 愿景与背景

现状是「bat 拉起两个服务窗口 + 开浏览器」：`启动平台.bat` 起 `serve.py`（127.0.0.1:8420）与 Vite dev server（localhost:5173，仅 IPv6），就绪后自动开浏览器。目标形态：**双击一个 exe → 直接弹应用窗口**，无 cmd 窗口、无浏览器标签页。

项目架构对此天然友好：后端 FastAPI + 前端 SPA 本就是「本地服务 + 壳」形态，换壳不动核心逻辑；优雅停机链路（`POST /api/admin/shutdown` → shutdown 钩子给在跑会话落断点快照）已有现成语义，关窗时衔接即可。

## 2. 现状盘点（2026-09-21 核实）

- `serve.py`：uvicorn 单进程 + 持 Server 句柄注册 shutdown 端点（v0.64）；端口参数可传。
- `core/api/app.py` **无 StaticFiles**——前端完全依赖 Vite dev server（5173），`vite build` 产物从未被托管。
- 端口占用检测（已在监听则跳过）目前写在 bat 里，不在 Python 侧。
- 无任何桌面壳/打包设施；WebView2 运行时 Windows 11 全量自带（Win10 绝大多数自带，缺可提示装）。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 目标形态 | **exe 双击直接出窗口**（Windows 原生优先，与项目定位一致） |
| 2 | 技术主线 | **档 2：pywebview（WebView2）+ PyInstaller onedir**；Tauri 暂缓（分发对象就是自己这台 Windows，不值得引入 Rust 链） |
| 3 | 共同地基 | **前端静态托管先行**（阶段 A）——无论将来选哪条壳路线都必须做，且独立有价值：落地后 bat 不再需要 npm/5173，后端单进程自足 |
| 4 | 优先级 | **低，不着急**；A/B/C 三阶段可独立排期，顺序无硬依赖（B 依赖 A） |

## 4. 设计详述

### 4.1 三档路线对比

| 档 | 形态 | 改造内容 | 结论 |
|----|------|---------|------|
| 1. 伪桌面 | Edge 无边框独立窗口（`msedge --app=http://127.0.0.1:8420`） | 仅启动脚本改造 | **不单独做**；静态托管（阶段 A）落地后它顺带可得 |
| 2. 真 exe | 双击 exe → pywebview 窗口 | 阶段 B + C | **主线** |
| 3. Tauri 2 壳 | 同上，体积小/可托盘/自更新 | Rust 工具链 + sidecar | 暂缓，需分发他人时再评估 |

### 4.2 阶段 A：前端静态托管（共同地基）

- `vite build` 产物由 FastAPI StaticFiles 托管 + SPA history fallback（非 `/api`、非 `/docs` 路径回 index.html）。
- `serve.py` 启动时检测产物存在 → 单进程自足；**开发期保留 Vite dev 模式**（bat 分支：有产物走静态、无产物走 dev）。
- 前端清理写死 5173 的硬编码；前后端同源后 CORS/代理配置消失，WS 与 API 前缀核对一遍。

### 4.3 阶段 B：窗口壳（serve.py --window 模式）

> **窗口与打包解耦**：pywebview 只是 pip 依赖，**源码状态即可弹窗**（`python scripts/serve.py --window`），无需先打包。开发期三形态并存：无窗浏览器（现状日常开发）/ 源码弹窗（验壳行为：关窗停机、双击幂等）/ exe（分发专用，阶段 C 才做）。前端无 dist 产物时窗口加载 Vite dev server（5173）热更新照常，有产物加载 8420 静态版；pywebview `debug=True` 可开 DevTools。

- `serve.py --window`：子线程跑 uvicorn，主线程 `webview.start()` 加载 `http://127.0.0.1:8420`。
- **双击幂等**：启动先探测 8420——已在跑则不开第二个服务，直接再开一个窗口连过去（pywebview 支持多窗）；端口检测逻辑从 bat 收进 Python 侧。
- **关窗行为**：窗口关闭事件里先 `POST /api/admin/shutdown`（落会话快照）再退出进程，不裸杀；托盘常驻作为待打磨选项（§5）。
- 启动时 `os.chdir` 到脚本/exe 所在目录——**cwd 兜底是硬要求**：全项目大量 cwd 相对路径惯例（`config/`、`packs/`、`workspaces/`、providers.json 等），从快捷方式/别处启动时路径不能飘。
- WebView2 缺失检测 → 提示安装一次。

### 4.4 阶段 C：PyInstaller onedir 打包

- **干净 venv 只装 requirements 打包**，不打 Miniconda 全家（体积爆炸）；onedir 形态预期 100-150MB。
- 资源目录随包：webui 构建产物、`packs/`、`config/`（种子）、tools 脚本；`workspaces/` 首启自动建。
- 未签名 exe 的 SmartScreen 首次告警：自用「仍要运行」即可，代码签名后置不做。

### 4.5 边界与不受影响项

- 外部工具依赖（Docker/WSL2/IDA/Playwright/MCP）本就是按能力降级的外部件，exe 化不改变接入方式。
- SQLite WAL + 黑板单一写入口不受影响；WS 在 WebView2 下正常工作。
- 不做跨平台（Windows 原生优先）、不做自动更新、Tauri 暂缓。

## 5. 待打磨清单

1. 关窗行为定稿：直接优雅停机退出 vs 最小化到托盘常驻（pystray）vs 弹确认。
2. 二次双击交互：新开窗口连已有服务 vs 唤起聚焦已有窗口（跨进程唤起需加本地信号管道，评估是否值得）。
3. Playwright 是否进包：Chromium 体积大，倾向 exe 外按需安装（首启检测+提示）。
4. 打包 venv 的 requirements 固定清单与冻结方式。
5. bat 退役节奏：静态托管落地后保留「无产物走 dev」分支多久；`停止平台.bat` 是否被 exe 自身关窗语义取代。
6. 前端 5173/8420 硬编码清点范围（webui/ 全扫）。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **阶段 A 静态托管**：独立有价值，可单独先排（1-2 天）。
- **阶段 B 窗口壳**：`--window` 模式 + 幂等探测 + 关窗停机（依赖 A，约 2-3 天）。
- **阶段 C 打包**：venv 固化 + PyInstaller onedir + 资源布局（约 2-3 天，含踩坑余量）。
