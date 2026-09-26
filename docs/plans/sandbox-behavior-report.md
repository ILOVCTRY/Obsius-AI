# 方案：沙箱行为采集与行为报告（微步式，S1 先行）

- **状态**：**打磨定稿（2026-09-23 夜间自主推进，7 项全消化见 §5），M1-M3 待排期**——S1 首件依赖 wine+strace 容器镜像落位（环境依赖大、非 hermetic），不自主实施
- **拍板记录**：见 §3（5 项决策已确认）；打磨定稿见 §5（7 项）
- **拍板记录**：见 §3（5 项决策已确认）
- **关联代码**：`core/runtime/backends.py`（DockerBackend L3 加固参数：--rm/--network none/512m/64 pids/no-new-privileges/cap-drop ALL）、`core/runtime/gateway.py`（run 网关）、`DESIGN.md` §7（L0-L3 / fakenet 纪律）、`core/agent/loop.py`（任务收尾挂点）
- **实施后**：定稿决策沉淀回 `DESIGN.md` §7，本文保留作方案背景

## 1. 愿景与背景

病毒分析场景需要行为证据：样本运行后**读了什么、改了什么、连了哪里**——微步云沙箱式报告。参照微步结构：静态信息 / 行为四类（**进程·文件·注册表·网络**）/ 威胁判定 / 时间线。

**场景归属**：S1 先服务 research 轨病毒分析（行为理解）；**malware 轨仍等 S2 fakenet 解锁**（解锁条件在此写明，避免误以为 S1 就能开 malware 项目）；蓝图（[blueprint-blackboard-object.md](blueprint-blackboard-object.md)）与行为报告互证——行为对上结构（「读配置文件」↔ 配置解析模块）。

## 2. 现状盘点（2026-09-21 核实）

- L3 = Docker 加固一次性容器（`--rm` / `--network none` / 512m / 64 pids / `no-new-privileges` / `cap-drop ALL`）——**断网**，网络行为不可见。
- fakenet = §7 纪律已定稿（未知样本默认 L3+fakenet）但**未实现**——malware 轨整个后置等它，V2 行为级对比也排队。
- 行为采集**完全空白**：无任何 syscall/文件/进程/网络采集层。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 起步范围 | **S1 先行**：现有 L3 容器内建行为采集 → 统一事件 → 报告 artifact；S2 fakenet、S3 分析层后置 |
| 2 | 采集路线 | **wine+strace 起步，VM 留升级插槽**：接受 wine 语义损耗，报告带忠实度标注防 Agent 过度信任 |
| 3 | 事件 schema | 统一行事件：`ts / category(process/file/registry/network) / op / subject / detail`；S1 断网下 network=N/A、registry=fidelity:low |
| 4 | 报告落点 | **artifact + Agent 消费双轨**：报告 artifact（人看）+ 任务收尾 LLM 判读 → 行为 finding 进发现链路 |
| 5 | VM 插槽 | **沙箱执行器抽象**：L3 wine 容器=第一后端，Windows VM=未来第二后端（接口预留，不做实现）。**优先级提升（2026-09-23，随 [container-execution-architecture.md](container-execution-architecture.md) 拍板 6）**：恶意样本以 Windows PE 为主 + x64dbg 最终归宿在 VM + 宿主 Home 版无 Hyper-V——VM 插槽升为正式里程碑（本方案 M3 / 容器执行架构 M5），不再「仅接口预留」 |

## 4. 设计详述

### 4.1 分层路径

- **S1（本方案实施范围）**：采集 → 事件 → 报告 artifact + Agent 消费。
- **S2 fakenet**：容器内虚拟网络（INetSim 类）解除断网 → 网络行为可见 → **malware 轨解锁评估**。
- **S3 分析层**：恶意评分 / IoC 提取 / ATT&CK 映射（LLM 辅助）→ 微步式前端报告页（时间线+分类聚合）。

### 4.2 S1 采集最小集

| 类别 | 采集内容 | S1 忠实度 |
|---|---|---|
| 文件 | 创建/修改/读取/删除路径清单 | 高 |
| 进程 | 进程树（创建链） | 中（wine 进程模型有折算） |
| 注册表 | wine `~/.wine/*.reg` 变更弱对应 | 低（显式标注） |
| 网络 | S1 断网 → 标 N/A | —（S2 启用） |

### 4.3 事件流与报告

- 统一事件：`ts / category / op / subject / detail (+fidelity)`；
- 报告 artifact：**JSON 全量 + MD 摘要**（时间线+分类聚合），落黑板 artifact 表；
- Agent 消费：任务收尾判读报告 → 行为 finding（category 约定 → 待打磨 #5）→ 进发现链路，与蓝图锚点互证。

### 4.4 沙箱执行器抽象（D5）

- runtime 层抽象沙箱接口：`execute(sample, opts) → 行为事件流`；L3 wine 容器 = 第一实现；Windows VM = 未来第二实现（接口预留不实现，与「原生 Windows 优先、按能力降级」原则不冲突）。

## 5. 打磨定稿记录（2026-09-23，七项全消化）

| # | 打磨点 | 定稿 |
|---|--------|------|
| 1 | 采集 wrapper 形态 | 容器内 `/usr/local/bin/behavior-run`（sh wrapper）：`strace -ff -o <beh_dir>/events.raw -s 4096 -e trace=%file,%process -- wine <样本>`；事件通道=**挂载文件**（workspace 行为目录 bind mount，采集器出容器后归一）——stdout 只属样本自身输出**不进报告管道**（样本不可信，stdout 可欺骗）；样本执行仍走 run 网关 threat_class 校验，wrapper 只改容器内命令组装 |
| 2 | strace 最小集与降噪 | trace 集 S1=%file+%process（network S2 启用）；降噪=**确定性剔除规则表**不做统计学习：① wine 自身预热路径（`~/.wine` 前缀事件降权 fidelity 注记）② 动态库加载预热段（ld.so 首帧）③ wrapper 自身进程树（`-ff` 按 pid 归属剔除）。剔除规则表随镜像版本走，报告 env 段注明 |
| 3 | 报告 artifact 格式 | JSON 全量 schema：`{sample:{sha256,filename,size}, env:{backend:"l3-wine", image, fidelity_note}, events:[{ts,category,op,subject,detail,fidelity}], summary:{by_category, timeline}, verdict_hint}`；MD 摘要=微步式四节（静态信息/行为四类聚合/威胁判定〔verdict_hint 占位，正式判定=Agent 判读落 finding〕/时间线）；落黑板 artifact 表 kind=`behavior-report`（JSON+MD 双件） |
| 4 | Agent 消费点 | **任务收尾钩子**（loop.py 收尾路径）：样本运行任务完成且产出 behavior-report artifact 时，自动追加一轮判读（小步数，category 约定映射）→ 行为 finding；**不做独立判读任务**（多一跳派单复杂度 S1 不值）；判读失败只留 artifact 不落 finding（宁缺勿滥） |
| 5 | 行为 finding category | **不新增 behavior 类**——research 轨既有五类内按语义归档（行为是**证据形态**不是发现类型）；evidence JSON 带 behavior-report artifact 引用，与蓝图锚点互证（蓝图对结构、行为对运行时，双报告交叉） |
| 6 | 沙箱执行器接口 | `core/runtime/sandbox.py`：`SandboxExecutor.execute(sample, opts) → BehaviorEventStream`；L3 wine 容器=第一实现（内部包装 DockerBackend run_once + behavior-run wrapper，**包装而非替代**——exec_in/run_once 语义不动）；Windows VM 后端仅留 Protocol 骨架不实现 |
| 7 | S2 fakenet 选型预研 | INetSim 优先（perl、协议覆盖广、容器友好、社区成熟）；FakeNet-NG 备选（Windows 原生强，容器工具链麻烦）；结论挂 S2 独立方案随 malware 轨解锁评估，本方案不实现 |

## 6. 实施切分（打磨定稿后由用户排期）

- **M1 采集最小闭环**：wine+strace 容器镜像落位（**环境依赖首件**，需用户环境配合）→ behavior-run wrapper → 事件归一 → 报告 artifact（JSON+MD）。
- **M2 Agent 消费**：收尾判读 → 行为 finding → 黑板/链路呈现。
- **M3 沙箱执行器抽象**：接口预留 + VM 插槽。
- **S2/S3**：fakenet 落地时另立方案（INetSim 选型已预研，见 §5 #7），随附 malware 轨解锁评估。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 采集最小闭环**：run_once 包装 strace → 事件收集 → 报告 artifact（JSON+MD）。
- **M2 Agent 消费**：收尾判读 → 行为 finding → 黑板/链路呈现。
- **M3 沙箱执行器抽象**：接口预留 + VM 插槽。
- **S2/S3**：fakenet 落地时另立方案，随附 malware 轨解锁评估。
