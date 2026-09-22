# cyberstrike-pro

AI 驱动的全能安全平台（能力包 web/binary/crypto/forensics/misc × 场景轨 ctf/pentest/redteam/research/malware，覆盖 CTF / 渗透测试 SRC / 红队行动 / 逆向 Pwn / IoT 工控车联网研究 / 恶意样本分析），**原生 Windows 优先**，按能力自动降级 Docker/WSL2。

## 文档地图

- **`DESIGN.md`** — 功能导向参考手册，当前已实施功能的唯一真相源（一级=系统、二级=功能、速览+展开区渐进填充）；未实施方向在 `docs/plans/`，实施落地时决策回写它，代码向它对齐。
- **`docs/plans/`** — 方案目录：设计讨论收敛的待实施设计（一方案一文件），走「细化打磨 → 实施」路线；实施落地时决策才回写 DESIGN.md。
- 各大文件夹的 `CLAUDE.md` — 该目录代码的接手指南（本文件末尾的维护约束）。

## 技术栈速查

| 层 | 选型 |
|----|------|
| 核心/后端 | Python 3.11+ / FastAPI |
| 前端 | React + TypeScript + Vite SPA |
| 存储 | SQLite (WAL)，经 core API 单一写入口 |
| LLM | 火山引擎 Ark 优先（OpenAI 兼容基类），模型路由 planner/executor/classifier |

## 一键启动（Windows 双击）

- **`启动平台.bat`**（项目根，双击即用）：自动起后端 `scripts/serve.py`（127.0.0.1:8420）并开浏览器。**静态优先（desktop-app-shell M2）**：`webui/dist/index.html` 存在 → 后端同源托管前端（8420 即 WebUI，不起 Vite）；无产物 → 回退起前端 Vite（localhost:5173，仅 IPv6）的 dev 全流程（首次自动 `npm install`）。端口已在监听则跳过，各服务独立窗口，日志看对应窗口。
- **`启动平台（窗口）.bat`**：桌面窗口模式——pythonw 起 `serve.py --window`（pywebview/WebView2，无控制台黑窗，日志落 serve-window.log）。8420 在跑=attach 附窗（关窗只退自己，可多开），无服务=owner（**关最后一窗=优雅停机并落任务现场快照**）；加载地址自动探测 dist 静态版 > Vite dev > 窗内指引。
- **`停止平台.bat`**：先对 8420 优雅停机（`POST /api/admin/shutdown`，shutdown 钩子给在跑会话落任务现场快照，等端口释放最多 15s），超时才按端口硬杀；5173 照旧硬杀。
- 三个脚本为 **UTF-8 + CRLF**，开头 `chcp 65001` 自举（call 自身重读）以兼容双击时的 GBK 控制台；改动后必须保持 CRLF（LF 会导致 cmd 解析错乱）。

## 文档维护约束（强制，每个会话必须遵守）

1. **每个大文件夹**（`core/`、`packs/`、`tools/`、`webui/`、`workspaces/`、`docs/` 及其一级子目录）**必须有 `CLAUDE.md`**。
2. 新建大文件夹时，必须**同步创建**其 `CLAUDE.md`（哪怕是占位）。
3. 会话中对某文件夹做了**实质功能修改**（新增模块 / 重构 / 改接口），结束前必须**更新该文件夹的 `CLAUDE.md`**——文档更新与代码改动属于同一项工作。
4. `CLAUDE.md` 只写"接手需要知道的"（职责、入口文件、关键约定、坑）；设计推导和决策过程写进 `DESIGN.md`，不重复。**超过 80 行必须精简。**
5. 所有文档语言一律**中文**。

## 开发硬约束（速记）

- 黑板所有写操作必须经 core API 单一入口，禁止旁路直写存储。
- 不可信代码只允许在 Docker 容器执行；WSL 信任级 = 宿主机（见 DESIGN.md §7 执行网关）。
- 安全默认值，宁严勿松：未知样本默认按恶意处理（L3 沙箱 + fakenet）。
- Agent 无裸 shell，一切命令经 `run(cmd, runtime)` 网关 + 服务端策略校验。
