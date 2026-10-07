# 方案：容器执行架构（大脑宿主、容器为手）

- **状态**：讨论收敛（2026-09-23 两轮商议定稿 7 项拍板），待打磨
- **拍板记录**：见 §3（2026-09-23 用户拍板 7 项）
- **关联代码**：`core/runtime/backends.py`（`sandbox_docker_args` :130-144 L3 加固参数、`DockerBackend.run_once` :166-177——L2 无 `--rm` 残留坑、`exec_in` :179-182 pwn 交互先例）、`core/runtime/gateway.py`（`run` :93 主入口、workspace 仅 host/wsl 生效 :116/:153「docker/sandbox 零挂载本不落盘」、:275-280 后端分派）、`core/runtime/pathguard.py`（写边界静态判定——卷挂载后语义对接点）、`core/runtime/detector.py`（能力探测——镜像内 manifest 探测对接点）、`DESIGN.md` §7（L0-L3 隔离模型）、[toolchain-registry.md](toolchain-registry.md)（M4 容器侧、拍板 3 本方案修订）、[sandbox-behavior-report.md](sandbox-behavior-report.md)（wine-sandbox 镜像=其 M1 首件、VM 插槽、fakenet S2）
- **参照**：`开源优秀项目/逆向/r0re-main`（容器架构分析 2026-09-23：DockerCli.java exec/watchdog/kill-pid 模型、增量补丁镜像、Windows 卷路径 `\`→`/` 修正、回环→host.docker.internal 重写、conclude 抢救式收尾）——**借工程不借安全姿态**（r0re 无恶意样本场景，容器内 root + Agent CLI 全放开不可取）
- **实施后**：定稿决策沉淀回 `DESIGN.md` §7（执行网关），同步 `core/runtime/CLAUDE.md`；本文保留作方案背景

## 1. 愿景与背景

toolchain-registry M4（容器侧）细化中暴露的执行架构四问（快照 / LLM 是否进容器 / 容器粒度 / 工具处理）+ 用户三问（x64dbg 放哪 / 宿主-容器通信与防逃逸 / Windows+Linux 双容器且 Windows 样本为主），两轮商议收敛出容器侧的执行架构定稿。一句话：**大脑在宿主，容器只有手；现场放卷、容器即弃；exec 通信零网络面；Linux 容器自动分析 + Windows VM 真执行的双执行环境**。

## 2. 现状盘点（2026-09-23 核实）

- L2/L3 = `run_once` 每条命令一次性容器；L3 带 `--rm`+断网+512m+pids 64+cpus 1.0+`no-new-privileges`+`cap-drop ALL`；**L2 无 `--rm`，已停容器残留堆积**（backends.py 注记「容器生命周期兜底」——兜底未实现，现存小坑）；
- **容器零挂载**：workspace 只对 host/wsl 生效（pathguard 写边界 + scratch 重定向），docker/sandbox「零挂载本不落盘」→ 容器内无现场，中间文件跨命令全丢、无法装依赖；
- `exec_in` 已存在（pwn 交互用）——「复用运行中容器」有半个先例；
- fakenet = INetSim sidecar 挂账（`sandbox_docker_args` NotImplementedError）；
- Agent 循环在宿主 core，LLM 从宿主发 Ark，容器只接命令回 stdout；
- 宿主 Docker Desktop（WSL2 后端）可用；**Windows 11 Home 无 Hyper-V → Windows 容器基本不可用**（进程隔离要求 Pro/Server + 版本匹配）——硬约束；
- 用户拍板信息：**当前分析的恶意样本以 Windows PE 为主**。

## 3. 定稿决策（用户拍板 2026-09-23）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | LLM/Agent 位置 | **宿主（确认现状）**，容器只有手——自研 Agent 无需进容器（r0re 塞容器是因调度第三方 CLI）；LLM key 不出宿主 + L3 断网自洽；模型路由/限速/审计/黑板单入口控制面单一 |
| 2 | L2 现场载体 | **workspace 卷挂载（样本 ro + scratch rw）**；~~生命周期保持 per-command~~ **2026-09-24 拍板翻转为长驻 + docker exec**；**2026-10-07 已实施为「会话级常驻容器」**（原「一任务一容器」锚点随任务机制退役，改由会话承接；见 `core/runtime/sessioncontainers.py` 与 DESIGN.md §7） |
| 3 | L3 生命周期与取证 | **per-execution 一次性不变**；取证=终态关键目录 cp-out 进 `sandbox-scene/`（与沙箱行为报告 artifact 合流）；docker commit 只做可选兜底档不默认；CRIU 排除（Docker Desktop Windows 不支持） |
| 4 | 通信通道 | **exec/cp/卷三件套走 daemon named pipe，零网络面**；服务型工具例外=专用内部 docker 网络+端口仅绑 127.0.0.1；黑板写永在宿主、凭据不进容器 |
| 5 | x64dbg 与人工调试 | **Agent 自动动态分析=frida 不用 x64dbg**（GUI-first 不适合 Agent）；x64dbg 短期放**宿主 + 人工调试豁免审计事件**；中期迁 **Windows VM 插槽**（快照回滚） |
| 6 | 双执行环境 | **Linux 容器三镜像（tools / sandbox-min / wine-sandbox）+ Windows VM 插槽**（优先级提升，VirtualBox/VMware 在 Home 版可用）；Windows 容器远期（Home 版硬约束，headless 脚本类样本才用得上） |
| 7 | 防逃逸 | 卷最小化 / 零凭据 / 永不挂 docker.sock / 动态分析能力显性化（SYS_PTRACE 专用镜像）/ 行为监测兜底 |

## 4. 设计详述

### 4.1 执行模型：大脑宿主、容器为手

- Agent 循环、LLM 调用、模型路由、黑板写、事件流全部留在宿主 core；容器只接收命令、回传结果；
- 通信与安全的连锁红利：容器网络可锁死（L3 断网/fakenet），LLM API 流量天然与容器无关，黑板单入口红线自动成立（容器里没有 core API 地址和凭据）。

### 4.2 通信通道：exec / cp / 卷三件套

**工具在镜像里是「可执行文件」不是「服务」——调用即通信，不需要通信通道。**

```
宿主 core（大脑+LLM）
   │  docker exec / run（走 daemon named pipe \\.\pipe\docker_engine，零网络面）
   ▼
容器（手+工具）
   │  stdout/stderr 原路拉回 → 宿主写黑板；文件 docker cp / 挂载卷
```

- 命令下行 `run_once` / `exec_in`（后者已有先例）；结果上行 stdout/stderr 拉回，黑板写只发生在宿主侧；
- **服务型常驻工具例外**（fakenet/INetSim、frida-server、将来 adb）：专用内部 docker 网络 + 端口只绑 127.0.0.1；业务容器接旁路网络，与外网隔离不变；fakenet 甚至无需宿主端口（样本→fakenet 在容器网络内部完成，宿主只收行为日志/事件文件）；
- Windows 宿主注意：daemon 通道 = named pipe，卷路径 `\`→`/` 修正（r0re DockerCli.resolveHostPath 先例）。

### 4.3 现场与快照：现场放卷、容器即弃

「快照」拆三层，答案不同：

| 快照含义 | 结论 |
|---|---|
| 现场/文件状态 | **不是快照问题，是现场放哪的问题**——现场落 workspace 卷（宿主 `workspaces/<pid>/`），容器即弃现场持久；已有 shutdown 任务现场快照免费覆盖，快照退化为目录本身 |
| 容器文件系统（commit） | 仅 L3 取证有价值；正确姿势=**终态 cp-out 关键目录**（`/tmp`、`$HOME`、cwd → `workspaces/<pid>/sandbox-scene/`），与沙箱行为报告 artifact 同管道；commit 作可选「全量兜底」档不默认（膨胀、难检索） |
| 进程/会话态（CRIU） | Docker Desktop Windows 不支持，**排除不做** |

- L2 开卷挂载后「写脚本→跑脚本」跨命令成立；「容器内装依赖」需求随工具进镜像基本消失（剩余场景=临时 pip 装，可后续观察再议 per-session 工作台）；
- 停机场景：abort+杀进程树已有；现场在卷 → 容器停毁现场不丢——卷方案的附带好处；
- 已停容器残留堆积：**L2 补 `--rm`（2026-10-07 已修根因）** + 会话容器管理器的 TTL/reap/启动对账兜底（见 `core/runtime/sessioncontainers.py`）。

### 4.4 生命周期矩阵

| runtime | 生命周期 | 挂载 | 网络 | 备注 |
|---|---|---|---|---|
| L0 host / L1 wsl | 现状不变 | pathguard 写边界（现状） | 宿主栈 | 不在本方案范围 |
| L2 docker | **per-session 常驻（2026-10-07 已实施；原 per-task 锚点随任务机制退役改会话承接）** | workspace 卷（整卷，将来收窄子目录） | none；net=real 任务级窗口 | pathguard 容器侧改告警制；会话关闭/项目删除时销毁 |
| L3 sandbox-min | per-execution + `--rm` | 零挂载（取证走 cp-out） | none → fakenet（S2） | 加固参数现状保留 |
| L3 wine-sandbox | per-execution + `--rm` | 行为目录 rw（behavior-run wrapper 约定，见沙箱行为报告 §5 #1） | none → fakenet | `--cap-add SYS_PTRACE` 例外显性化 |
| 服务容器（fakenet 等） | 项目会话期常驻 | — | 内部 docker 网络 | 会话终态清理；端口仅 127.0.0.1 或不映射 |

### 4.5 容器镜像矩阵（对接 toolchain-registry M4）

```
Linux 侧（现有 Docker Desktop/WSL2）
├─ cyberstrike-tools   # L2：python3.11 + 逆向/渗透常用 CLI + Ghidra（构建时 COPY tools/ 内容）
├─ sandbox-min         # L3 加固执行：最小化分析件（cap-drop ALL）
├─ wine-sandbox        # L3 Windows PE 动态分析：wine + strace + 行为采集件（=沙箱行为报告 M1 首件）
└─ fakenet sidecar     # INetSim（沙箱行为报告 S2，选型已预研）
Windows 侧
├─ 宿主（过渡）：x64dbg 人工调试豁免（§4.7）
└─ Windows VM 插槽：x64dbg + 全真执行 + 快照回滚（优先级提升）
```

- **工具进容器方式（修订 toolchain-registry 拍板 3 的「大件挂载」）**：大件走**构建时 COPY**（构建脚本从 tools/ 目录 COPY——项目自包含目标不变：镜像从项目目录构建）+ **增量补丁镜像**迭代（`FROM` 旧镜像叠层，r0re 验证过的模式，规避全量重建）；bind mount 仅限 L2 现场/缓存卷（小 I/O）——Docker Desktop Windows 目录挂载走文件共享层，Ghidra 类重 I/O 慢数倍，且 L3「无宿主挂载」硬规则不容挂载；
- **容器内 runtime ≠ 自带 runtime**：`tools/runtime/python-3.11` 是 Windows 便携版，Linux 容器用不了；容器侧 python 来自镜像 base，python 依赖=镜像构建时 pip（requirements 一份两侧各装）；registry per-runtime 字段表达：l0/l1 → tools/ 路径，l2/l3 → 镜像内路径；
- **探测**：镜像构建时落 `tools-manifest.json` 进镜像（工具名→路径→版本），detector 对 L2/L3 读 manifest，不 exec 进容器 `which`；
- Windows 工具链（x64dbg/PE-bear/API Monitor 类）服务宿主人工侧 + VM，**不进 Linux 容器**；Linux 容器侧 PE 能力 = radare2/python 逆向栈（静态）+ wine（动态）+ frida，够自动分析用。

### 4.6 防逃逸清单

| 层 | 措施 |
|---|---|
| 已有 | cap-drop ALL / no-new-privileges / 512m / pids-limit / 断网 / L3 零挂载 |
| 卷最小化 | 只挂 workspace **子目录**（样本 ro、scratch rw）；永不挂 workspaces/ 上层、永不挂宿主任意路径——pathguard 写边界=卷边界 |
| 零凭据 | LLM key / API token / config/ 一律不进容器；L2 exec env 也只注非敏感值 |
| 无 docker.sock | Docker-in-Docker 逃逸经典口子，红线 |
| 能力显性化 | 动态分析要 ptrace（frida/调试器）→ 专用 wine-sandbox 镜像 `--cap-add SYS_PTRACE`，与加固执行镜像分开——**能力开在哪里，镜像名就说在哪里** |
| 监测兜底 | 容器内行为异常（探测 docker.sock、mount 系统调用、/proc/1 触碰）→ 行为报告层告警（沙箱行为报告衔接） |

### 4.7 人工调试豁免（x64dbg 的边界澄清）

- 网关「人类命令同层经网关」管的是**平台通道发起的执行**（Agent/终端工具/编排）；用户在桌面双击 x64dbg 不经平台——豁免事件是把这个活动**显性化**而非拦截，是「宁严勿松」对人工活动的合理边界；
- 平台职责：样本副本落 `workspaces/<pid>/` + 审计事件 `manual.debug`（样本路径 + 警示文案 + 时间戳）；风险归用户（宿主裸调样本是逆向日常，属用户自身风险决策，平台不背书）；
- 中期 Windows VM 接管（快照回滚，安全性与可重放远超宿主裸跑），x64dbg 最终归宿在 VM，宿主只是过渡。

### 4.8 衔接

- **toolchain-registry M4**：镜像构建脚本、registry per-runtime 字段、manifest 探测——工具面的容器侧落点在本方案矩阵之上；
- **sandbox-behavior-report**：wine-sandbox 镜像=其 M1 首件（wine+strace+behavior-run wrapper）；取证 cp-out 与其 artifact 管道合流；VM 插槽=其 M3 第二后端（优先级提升随本方案）；fakenet=其 S2；
- **desktop-app-shell M3**：镜像不入发布包，构建脚本随包（首启/按需构建）；

## 5. 待打磨清单

1. L2 卷挂载与 pathguard 语义对接：pathguard 现按 host/wsl 写逃逸判定（docker 零挂载语义下不生效），L2 开卷后写拦截是否/如何覆盖 docker 通道（挂载区即写边界 vs 命令文本仍需静态判定）。
2. wine-sandbox 行为采集件清单与降噪规则表——归沙箱行为报告 §5 #2 细化，本方案只定镜像位。
3. Windows VM 插槽选型：VirtualBox vs VMware Workstation（Home 可用性、VBoxManage 快照 API、VM 内 Agent 通信通道：共享文件夹/网络/控制 API）。
4. 人工调试豁免事件 schema 与入口形态（终端命令触发？面板按钮？）。
5. fakenet sidecar 网络拓扑细节（INetSim 容器 + 业务容器 docker network + DNS 劫持指向）。
6. ~~`exec_in` 长驻容器生命周期统一~~ **2026-10-07 已实施**：exec 目标即会话常驻容器（`csp_<pid>_<sid>`），生命周期归 `SessionContainerManager`（会话关闭删、项目删除先停、启动对账清残留）；pwn 交互天然成立。
7. 服务型工具内部 docker 网络的命名与复用策略（per-project? per-session?）。
8. Windows 容器远期条件（Pro/Server 或 CI 环境出现时重启评估，headless 脚本类样本场景）。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 L2 现场卷挂载 + `--rm` + 生命周期清理**：改动最小（backends.py/gateway.py），立即解决现场丢失 + 残留堆积两坑；pathguard 对接（待打磨 1）。
- **M2 容器镜像矩阵首批**：cyberstrike-tools + sandbox-min 构建脚本 + manifest + detector 探测（衔接 toolchain-registry M4）。
- **M3 L3 取证 cp-out + 人工调试豁免事件**：sandbox-scene 目录约定 + `manual.debug` 审计。
- **M4 wine-sandbox + fakenet sidecar**：与沙箱行为报告 M1 同件协同（镜像本体归本方案，采集 wrapper 归其 M1）。
- **M5 Windows VM 插槽**：选型（待打磨 3）+ 快照管理 + x64dbg 迁入 VM（衔接沙箱行为报告 M3 沙箱执行器抽象第二后端）。
