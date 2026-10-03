# cyberstrike-pro（Obsius-AI）

`cyberstrike-pro` 是一个 AI 驱动的安全研究工作台，面向 CTF、渗透测试、红队行动、逆向分析、Pwn、IoT/车联网研究和恶意样本分析。项目以 Windows 为主要运行环境，按任务需要使用 Docker 或 WSL2 执行隔离工作。

项目仍在持续迭代中。README 介绍当前已经落地的能力、启动方式和开发约定；功能设计的详细说明以 [`DESIGN.md`](DESIGN.md) 为准。

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

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.11+、FastAPI、Uvicorn |
| 前端 | React、TypeScript、Vite、Tailwind CSS |
| 存储 | SQLite WAL，经 Core API 统一写入 |
| 浏览器 | Playwright 托管 Chromium（可选） |
| 运行时 | Windows 优先，按能力降级到 Docker/WSL2 |
| 测试 | pytest |

## 快速开始

### 环境要求

- Windows 10/11（推荐）
- Python 3.11 或更高版本
- Node.js 和 npm（源码开发或未构建前端时需要）
- 可选：Docker、WSL2、Microsoft Edge WebView2
- 可选：Playwright Chromium；浏览器功能未安装依赖时会降级并给出安装提示

### 安装后端

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[api]"
```

按需安装可选能力：

```powershell
python -m pip install -e ".[browser]"  # 浏览器与抓包能力
python -m pip install -e ".[window]"    # pywebview 桌面窗口
python -m playwright install chromium   # 安装托管浏览器
```

### 安装前端

```powershell
cd webui
npm install
```

开发时启动 Vite：

```powershell
npm run dev
```

构建生产静态文件：

```powershell
npm run build
```

构建完成后，后端会自动从 `webui/dist` 同源托管前端，不再需要单独启动 Vite。

### 启动平台

最简单的方式是双击项目根目录的 [`启动平台.bat`](启动平台.bat)：

- 后端监听 `http://127.0.0.1:8420`。
- 如果存在 `webui/dist/index.html`，使用后端同源静态模式。
- 如果没有静态产物，自动安装前端依赖并启动 Vite `http://localhost:5173`。
- API 文档位于 `http://127.0.0.1:8420/docs`。

也可以直接启动后端：

```powershell
python scripts/serve.py
```

桌面窗口模式：

```powershell
python scripts/serve.py --window
```

或双击 [`启动平台（窗口）.bat`](启动平台（窗口）.bat)。窗口模式会优先加载静态前端，其次探测 Vite；关闭最后一个 owner 窗口时会优雅停止服务并保存任务现场。停止平台请使用 [`停止平台.bat`](停止平台.bat)，它会先请求后端保存现场，再在超时后结束进程。

## 日常使用流程

1. 启动平台并创建或打开项目。
2. 在项目中配置模型供应商、能力包和需要的工具。
3. 导入资产或上传样本，查看资产状态和项目文件树。
4. 创建任务，选择主控或通用智能体；需要人工确认的动作会进入审批区。
5. 在任务面板查看事件流、黑板、子任务和执行结果，可暂停、恢复或取消任务。
6. 在浏览器工具中进行页面操作、抓包、拦截和重放；Playwright MCP 默认复用项目内嵌浏览器。
7. 在样本分析中查看文件预览；对 PE、ELF、Android 等样本启动逆向分析并查看函数、伪代码、反汇编和 XRef。
8. 需要复盘时，从任务现场快照、事件流和黑板恢复上下文。

## 样本与逆向分析

样本分析包是项目内的文件集合：单个文件或文件夹上传后都直接落在分析包根目录。文件树支持目录展开、不同格式预览、移动、删除和最近一次变更撤销。分析包和项目绑定，一个项目当前只支持一个分析包。

逆向工作台负责消费分析适配器产出的结果，展示：

- 函数清单及分析状态
- 伪代码和反汇编
- 函数调用关系与 XRef
- 字符串、逻辑块和逆向蓝图
- 分析进度、预计时间和实时新增函数

当前逆向分析可能需要较长时间，具体取决于样本规模、分析器和本机资源。未知或可疑样本应在隔离运行时中动态分析；宿主机、WSL 和 Docker 的信任级别与限制见 [`DESIGN.md`](DESIGN.md) 的执行网关章节。

## 浏览器与 MCP

浏览器相关能力通过后端浏览器池管理上下文，前端通过项目 API 与其通信。常用入口包括浏览器、抓包、拦截、重放和 Playwright MCP。MCP 配置位于 [`.mcp.json`](.mcp.json)；项目内嵌浏览器优先用于当前项目，避免不同会话之间混用页面状态。

浏览器能力需要安装可选依赖：

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

## 配置与数据位置

- `config/`：供应商和本机配置。敏感凭据不要提交到 Git。
- `workspaces/`：项目工作区和任务现场；每个项目拥有独立目录。
- `data/`：全局运行时数据，例如 `campaign.db`。
- `logs/`：服务日志；窗口模式日志为 `logs/serve-window.log`。
- `packs/`：能力包和场景轨。
- `tools/`：分析脚本与工具适配器。
- `.env`：本地环境变量；请使用 `.env.example`（如有）作为参考，不要写入真实密钥。

## 安全边界

- 所有黑板写操作必须经过 Core API，禁止旁路直接写数据库。
- 智能体执行命令必须经过执行网关和服务端策略校验。
- 不可信代码默认按恶意样本处理，动态运行优先使用 Docker 沙箱和受控网络。
- WSL 与宿主机同属高信任级运行时，不等同于强隔离沙箱。
- 逆向和动态分析应在授权范围内进行，样本、凭据和外部目标的处理责任由使用者承担。

## 开发与测试

后端测试：

```powershell
python -m pytest
```

前端检查与构建：

```powershell
cd webui
npm run lint
npm run build
```

后端服务的 OpenAPI 文档可通过 `http://127.0.0.1:8420/docs` 查看。开发目录中的 `CLAUDE.md` 记录对应目录的入口、约定和接手注意事项；修改核心模块时请同步维护相应文档。

## 目录结构

```text
core/        后端核心：API、智能体、黑板、编排、LLM、浏览器、运行时
packs/       能力包与场景轨
tools/       逆向、情报和 MCP 工具适配器
webui/       React + TypeScript 前端
workspaces/  项目工作区、样本包和任务现场
config/      本地配置与供应商设置
data/        全局运行时数据
scripts/     启动、迁移、构建和维护脚本
docs/        方案与补充文档
tests/       自动化测试
```

## 文档地图

- [`DESIGN.md`](DESIGN.md)：当前已实施功能的详细设计和系统约束。
- [`CLAUDE.md`](CLAUDE.md)：项目级开发约束和目录文档规则。
- [`docs/plans/`](docs/plans/)：尚未实施或正在规划中的方案。
- 各目录下的 `CLAUDE.md`：对应模块的入口、约定和常见陷阱。

## 已知限制

- 部分能力依赖本机工具、Docker/WSL2、WebView2 或第三方模型供应商，未安装时会按模块降级。
- 逆向分析结果的完整度取决于分析器和样本格式；大型样本的函数、伪代码和 XRef 可能分批生成。
- 项目仍处于快速迭代阶段，接口和界面可能变化，使用前请查看当前分支的 `DESIGN.md` 与测试结果。

## 许可证

项目许可证见 [`LICENSE`](LICENSE)。项目中引用的第三方代码和素材遵循其原始许可证。
