# 方案：沙箱行为采集与行为报告（微步式，S1 先行）

- **状态**：讨论收敛，待打磨（2026-09-21）
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
| 5 | VM 插槽 | **沙箱执行器抽象**：L3 wine 容器=第一后端，Windows VM=未来第二后端（接口预留，不做实现） |

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

## 5. 待打磨清单

1. 采集 wrapper 形态：run_once 入口包装方式、事件 JSON 出容器通道（挂载文件 / stdout）。
2. strace 采集最小集与降噪过滤（库调用/自身进程剔除）。
3. 报告 artifact 格式细节（JSON schema + MD 模板）。
4. Agent 消费点实现位置（任务收尾钩子 or 独立判读任务）。
5. 行为 finding category 约定（research 轨五类之外是否新增 behavior 类）。
6. 沙箱执行器接口形状（与现有 run_once / exec_in 的关系）。
7. S2 fakenet 选型预研（INetSim / FakeNet-NG / 自建 mitm）。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 采集最小闭环**：run_once 包装 strace → 事件收集 → 报告 artifact（JSON+MD）。
- **M2 Agent 消费**：收尾判读 → 行为 finding → 黑板/链路呈现。
- **M3 沙箱执行器抽象**：接口预留 + VM 插槽。
- **S2/S3**：fakenet 落地时另立方案，随附 malware 轨解锁评估。
