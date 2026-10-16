# Obsius-AI

`Obsius-AI` 是一个 AI 驱动的安全研究工作台，面向 CTF、渗透测试、红队行动、逆向分析、Pwn、IoT/车联网研究和恶意样本分析。项目以 Windows 为主要运行环境，按任务需要使用 Docker 或 WSL2 执行隔离工作。

## 核心能力

- **项目与工作区**：每个项目拥有独立配置、资产、任务、样本分析包和运行现场。
- **智能体协作**：支持主控、通用智能体和子智能体，提供任务编排、黑板、审批、暂停、恢复、取消和现场快照。
- **资产与情报**：资产导入、发现、状态标记、事件流、意图和可审计的黑板写入。
- **执行网关**：通过统一的 `run(cmd, runtime)` 网关调用宿主机、WSL、Docker 或沙箱运行时；智能体不直接使用裸 shell。
- **内置浏览器与 MCP**：项目内嵌浏览器、抓包、拦截、重放、爆破，以及 Playwright MCP 集成。
- **样本分析**：支持单文件和文件夹上传为一个分析包，在文件树中预览、移动、删除并撤销最近一次文件树变更。
- **逆向工作台**：集成 IDA/Ghidra 分析结果，展示函数、伪代码、反汇编、XRef、字符串和逆向蓝图。
- **知识与规则**：技能、角色、红线、知识库和项目级约束参与智能体路由与审批。
- **多模型接入**：支持项目配置的模型供应商，以及 OpenAI/Anthropic 兼容协议；LLM 默认请求超时为 600 秒，传输失败按错误类别有限重试。

## 渗透测试

![](https://raw.githubusercontent.com/ILOVCTRY/note-gen-image-sync/main/blog-img/2026-10-10/image_1791618494_82a.png)

![](https://raw.githubusercontent.com/ILOVCTRY/note-gen-image-sync/main/blog-img/2026-10-10/image_1791618587_1663.png)

![](https://raw.githubusercontent.com/ILOVCTRY/note-gen-image-sync/main/blog-img/2026-10-10/image_1791618654_ffbb.png)

### 启动平台

最简单的方式是双击项目根目录的 `启动平台.bat`：

- 后端监听 `http://127.0.0.1:8420`。
- 如果存在 `webui/dist/index.html`，使用后端同源静态模式。
- 如果没有静态产物，自动安装前端依赖并启动 Vite `http://localhost:5173`。
- API 文档位于 `http://127.0.0.1:8420/docs`。

也可以直接启动后端：

```ps1
python scripts/serve.py
```

桌面窗口模式：

```ps1
python scripts/serve.py --window
```

或双击 `启动平台（窗口）.bat`。窗口模式会优先加载静态前端，其次探测 Vite；关闭最后一个 owner 窗口时会优雅停止服务并保存任务现场。停止平台请使用 `停止平台.bat`，它会先请求后端保存现场，再在超时后结束进程。

## 样本与逆向分析

![](https://cdn.jsdelivr.net/gh/ILOVCTRY/note-gen-image-sync@main/blog-img20261014235212027.png)

![](https://cdn.jsdelivr.net/gh/ILOVCTRY/note-gen-image-sync@main/blog-img20261014235257664.png)

## CTF

![](https://raw.githubusercontent.com/ILOVCTRY/note-gen-image-sync/main/blog-img/2026-10-16/image_1792124131_8bb0.png)

## 系统架构

```text
┌─────────────────────────────────────────────────────────┐
│ React + TypeScript + Vite WebUI                         │
│ 项目 / 智能体 / 任务 / 浏览器 / 样本 / 逆向 / 设置      │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTP / WebSocket
┌──────────────────────────▼──────────────────────────────┐
│ FastAPI Core API（127.0.0.1:8420）                      │
│ API 路由 · 任务编排 · 黑板 · 浏览器池 · 分析适配器       │
└──────────────┬───────────────────┬─────────────────────┘
               │                   │
       SQLite（WAL）黑板       执行网关
       项目数据与审计事件       Host / WSL / Docker / Sandbox
               │                   │
               └──────────┬────────┘
                          ▼
                 LLM 供应商与外部工具
```

### 技术栈

|层 |技术 |
|---|---|
|后端 |Python 3.11+、FastAPI、Uvicorn |
|前端 |React、TypeScript、Vite、Tailwind CSS |
|存储 |SQLite WAL，经 Core API 统一写入 |
|浏览器 |Electron WebContentsView 真实 Chromium + Python CDP（可选） |
|运行时 |Windows 优先，按能力降级到 Docker/WSL2 |
|测试 |pytest |

## 快速开始

### 环境要求

- Windows 10/11（推荐）
- Python 3.11 或更高版本
- Node.js 和 npm（源码开发或未构建前端时需要）
- 可选：Docker、WSL2
- Electron 桌面端需要 Node.js/npm；Python 端需要 Playwright Chromium

### 安装后端

在项目根目录执行：

```ps1
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[api]"
```

按需安装可选能力：

```ps1
python -m pip install -e ".[browser]"  # 浏览器与抓包能力
cd desktop
npm install                              # Electron 壳、node-pty 与真实浏览器标签
cd ..
python -m playwright install chromium   # 安装托管浏览器
```

### 安装前端

```ps1
cd webui
npm install
```

开发时启动 Vite：

```ps1
npm run dev
```

构建生产静态文件：

```ps1
npm run build
```

构建完成后，后端会自动从 `webui/dist` 同源托管前端，不再需要单独启动 Vite。