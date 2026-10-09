# 方案：Windows VM 执行插槽（活体样本 + exe 运行 + x64dbg MCP 调试）

- **状态**：**已定稿（2026-09-25 用户拍板 D1；D2-D8 工程推荐项同批确认），M1-M3 待排期**
- **拍板记录**：见 §2 决策表
- **关联文档**：
  - [container-execution-architecture.md](container-execution-architecture.md)（M5 Windows VM 插槽、决策 #5/#6、§4.7 人工调试豁免）
  - [sandbox-behavior-report.md](sandbox-behavior-report.md)（VM = 沙箱执行器第二后端，M3）
  - `DESIGN.md` §7（L0-L3 / fakenet 纪律，实施时扩展 L4）、§453-455 调试脚本档
- **关联代码**：
  - `core/runtime/policy.py`（隔离等级 / threat_class 放行 / net 模式——加 L4）
  - `core/runtime/backends.py`（三 backend 与 `_execute_piped`——新增 WinVMBackend）
  - `core/runtime/gateway.py`（run 网关接线）、`core/runtime/detector.py`（VBoxManage/VM 探测）
  - `core/tools/decompiler.py`（MCPBackend 模式直接复用）、`config/mcp.json`（x64dbg MCP 条目）

## 1 背景与目标

用户明确的四项必须能力：

1. LLM 可用 VM **运行病毒样本**（活体 PE，真实 Windows 语义）；
2. 可在其中**运行任意 exe 软件**；
3. VM 内**放置 x64dbg**（x32/x64 双版本）；
4. **通过 MCP 直接调用 x64dbg 调试**（加载样本/附加进程/下断/单步/读状态）。

现状：`malware_live` 只允许 L3 Docker 加固容器（断网、wine 无真实 Windows 语义）；x64dbg 短期在宿主 + `manual.debug` 人工豁免。宿主为 **Windows 11 Home，无 Hyper-V**（Windows 容器不可用），但 VirtualBox / VMware Workstation 在 Home 版可用。本方案把 container-execution-architecture 的 M5 插槽填满。

**使用路径两条**：

- **跑样本/跑 exe**：Agent → `run(cmd, runtime="winvm")` 网关，受 threat_class / 网络策略约束，与 docker/sandbox 同纪律；
- **调试**：Agent → MCP 工具（经现有 MCPBackend → VM 内 x64dbg MCP server），LLM 像调 IDA MCP 一样调用。

## 2 定稿决策（2026-09-25）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | Hypervisor | **VirtualBox 主选**：VBoxManage 覆盖快照恢复/guestcontrol 命令执行与文件投递/NAT 端口转发，免费 GPL、Home 版可用、winget 可静默装、社区脚本生态最大。**hypervisor 操作收敛成接口**（snapshot/copy/run/natpf），VMware Workstation Pro 为备选 backend（2026 年已对商业/教育/个人全免费，但 NAT 端口转发无 CLI、vmrun guest 操作 5 分钟硬超时、下载须 Broadcom 账号合规表单——M1-M3 不实施）。不装 Extension Pack（本方案不需要） |
| D2 | 隔离等级 | **新增 L4_WINVM**：完整系统隔离 + 快照回滚。`malware_live → {L3, L4}`、`untrusted → {L2, L3, L4}`、`trusted → 全等级`；net 沿用 none/fakenet/real 三档，默认 none，real 仍人工审批 + 一次性消费 |
| D3 | VM 通信 | **只用 guestcontrol，不挂共享文件夹**（共享夹是宿主感染/逃逸面）。投递 `copyto`、回收 `copyfrom`、执行 `guestcontrol run --wait-stdout`，VM 专用本地低权账号认证；剪贴板/拖拽/共享文件夹全关。M1 输出为进程结束后一次拿回；实时输出需求（见 [command-live-output.md](command-live-output.md)）通过「VM 内写文件 → 宿主轮询 copyfrom」后置补齐 |
| D4 | x64dbg MCP | **先实测评估 ouonet/x64dbg-mcp 的 streamable-http 模式**：ctypes 直连 x64bridge.dll（不依赖 x64dbgpy），load_executable/attach/下断/读写，按 PE 架构自动选 x32/x64dbg；与现有 MCPBackend（loopback 校验、Mcp-Session-Id）同构，`config/mcp.json` 加一条即可。**MCP server 跑在 VM 内**，经 VBox NAT 端口转发为宿主 `127.0.0.1:3000`（端点 `/mcp`），loopback 红线不破。备选 AgentSmithers C# 插件自宿主 HTTP；VM 须开机自动登录 + server 登录态自启（x64dbg 是 GUI 程序） |
| D5 | 网络管控 | **默认断网**：建 VM 即 `--nic1 none`。Windows 侧 fakenet 用 **FakeNet-NG**（VM 内安装），随 sandbox-behavior-report S2 一起评估后置；在此之前 Windows VM 无网络行为可见，对齐「未知样本按恶意处理」 |
| D6 | 快照生命周期 | **黄金镜像 + 每轮分析后 revert**：黄金镜像 = 系统 + Guest Additions + x64dbg + Node 20+ + MCP server 自启 + 专用低权账号 + 无样本痕迹。每样本（含多轮调试）结束后 `snapshot restore golden`；「每样本 vs 每任务」实施时按场景定，默认从严按样本 |
| D7 | VM 硬化 | 零宿主凭据/浏览器密码；不桥接网卡（只 NAT 且默认不接线）；VirtualBox 保持更新（逃逸 CVE 有历史）；VM 磁盘在项目外固定目录；快照与镜像不打入任何安装包 |
| D8 | 与行为方案关系 | Windows VM = sandbox-behavior-report 定稿的**沙箱执行器第二后端**：L3 wine+strace 低成本低保真，Windows VM 高保真（真注册表/进程/网络栈）。VM 内行为采集（Sysmon/Procmon 日志导出 → 统一行为事件）**后置不在首批** |

## 3 总体架构

```
宿主 Windows 11 Home（无 Hyper-V）
  VirtualBox + Windows 10/11 VM「黄金镜像」
    ├─ x64dbg（x32/x64）
    ├─ x64dbg-mcp server（:3000/mcp，仅绑 localhost，登录态自启）
    ├─ Guest Additions（guestcontrol 命令/文件通道）
    ├─ 专用低权本地账号
    └─ 网卡默认 none
宿主平台侧：
    ├─ detector：探测 VBoxManage / VM 注册与运行状态
    ├─ hypervisor 接口（snapshot/copy/run/natpf）→ VBox 实现（VMware 后置）
    ├─ WinVMBackend：接入 run 网关（runtime="winvm"，policy L4）
    └─ MCPBackend → 127.0.0.1:3000（NAT 转发）→ VM 内 x64dbg
```

## 4 实施切分（待用户排期）

### M1 VM 执行底座

- `core/runtime/hypervisor.py`：Hypervisor 接口 + VBoxManage 实现（start/poweroff、snapshot take/restore、copyto/copyfrom、run --wait-stdout、natpf 增删）。
- `core/runtime/backends.py`：新增 **WinVMBackend**（包装 hypervisor，对齐 ExecOutcome；超时 `_kill` 经 `controlvm` / guestcontrol 进程终止）。
- `policy.py`：L4_WINVM + RUNTIME_LEVELS 加 `winvm` + THREAT_ALLOWED 按 D2 收窄。
- `gateway.py`：runtime 分发接线；workspace 语义 = VM 内固定分析目录（copyto 投递 + copyfrom 回收），不走宿主路径挂载，pathguard 仍校验宿主侧写目标。
- `detector.py`：VBoxManage 存在性、VM 是否注册、黄金快照是否存在；缺失给制作指引。
- 默认断网（`--nic1 none`），M1 只支持 net=none。
- **硬前置（用户手工，平台出脚本+步骤）**：安装 VirtualBox；制作黄金镜像 VM（系统 ISO、Guest Additions、低权账号、自动登录）；打 golden 快照。

### M2 x64dbg MCP 接入

- 黄金镜像增项：x64dbg（x32/x64）+ Node 20+ + x64dbg-mcp（streamable-http、自启）。
- NAT 端口转发：`127.0.0.1:3000 → VM:3000`（hypervisor 接口 natpf 落库配置）。
- `config/mcp.json` 加 x64dbg 条目（复用 loopback 校验）；MCPBackend 探活/会话管理零新增。
  - **宿主直连形态已落地（2026-10-08，先于 VM）**：x64dbg 装宿主时插件自启 `http://127.0.0.1:3000/mcp`（streamable-http，80 工具，**无 `Mcp-Session-Id` 响应头**），`config/mcp.json` 条目 `domains:["reverse"]` → 逆向（research 轨）项目的**智能体工作台**经 `core/chat/mcp_bridge.py` 自动获得 `mcp__x64dbg__*` 工具。VM 形态只是把 `127.0.0.1:3000` 换成 NAT 转发，配置条目同构。**IDA 专用 `MCPBackend` 不接 x64dbg**（工具名不兼容）——`select_mcp_endpoint` 已加精确名 `ida` 优先，防其抢占实时桥。
- Agent 工具暴露：调试工具随 MCP 工具清单下发；run_cmd 描述补「样本投递后用 MCP 加载调试」路径。
- 人工监督下跑通全链：**投递样本 → MCP 加载 → 下断 → 单步 → 读寄存器/内存 → 产物回收**。
- 前置评估闸门：ouonet/x64dbg-mcp 若实测不稳（GUI 会话、x64bridge 兼容），启用 C# 插件备选再定。

### M3 回滚纪律 + 对接

- 分析任务收尾自动 `snapshot restore golden`（失败/超时也回滚，防感染态残留）。
- 产物回收清单化（dump、调试日志、截图 captureScreen）→ 黑板 artifact。
- 与 sandbox-behavior-report 对接：VM 后端实现 SandboxExecutor Protocol；Sysmon/Procmon 采集 → 统一行为事件（高保真档）。
- 结束钩子与 shutdown 现场快照协调：VM 运行态在停机时的落盘策略。

## 5 风险与代价

- **资源**：VM 建议 ≥4GB 内存 / ≥40GB 磁盘，快照额外占空间；与平台、Docker 同时运行时内存压力需提示。
- **授权**：VM 内 Windows 测试用可不激活（水印/少量限制）；样本分析用途保留授权证据。
- **黄金镜像为一次性手工活**：平台只脚本化辅助；M1 硬前置未完成则整条线不可用，detector 必须显式提示。
- **逃逸面**：VM 非绝对边界（VirtualBox 有逃逸 CVE 历史）→ D7 硬化 + 保持更新；MCP NAT 转发仅 loopback、无认证仍依赖「转发只绑回环」，须在 natpf 配置中固化。
- **GUI 依赖**：x64dbg 与 MCP server 需桌面会话（自动登录/自启）；服务会话跑不起来，M2 验收必须覆盖重启自启。
- **实时性**：guestcontrol stdout 结束才返回，M1 不支持 VM 内实时输出；长任务靠 result 终态 + command.result 现状语义，直播能力随 command-live-output / D3 后置方案补齐。

## 6 待打磨（实施前/中收敛）

1. 黄金镜像制作脚本形态：PowerShell + VBoxManage 无人值守安装（Autounattend.xml）可行性，vs 纯手工步骤清单。
2. 每样本 revert vs 每任务 revert 的触发点（loop 收尾钩子的具体挂载位置）。
3. FakeNet-NG 在 VM 内的部署形态与 S2 排期关系。
4. VMware backend 接口映射差异（vmrun 5 分钟超时在接口层如何表达）。
