# 方案：任务级容器（一任务一容器）

- **状态**：讨论收敛（2026-09-24 四项拍板），待打磨
- **拍板记录**：见 §0
- **关联代码**：`core/runtime/backends.py`（`DockerBackend.run_once` :195-229 现 per-command、`exec_in` :231-234 复用先例）、`core/runtime/gateway.py`（`run` 主入口、`would_deny` :255 策略干跑、`_dispatch` :292 docker 分支）、`core/runtime/pathguard.py`（`ALLOWED_SPECIAL_TARGETS` :36、写目标提取 :105-113）、`core/agent/loop.py`（run_task 任务体——容器作用域挂点）、`core/blackboard/tasks.py`（claim :409、fail :633、`_finish` :668、租约回收 :1395）、`scripts/serve.py`（shutdown 钩子——冻结挂点）、`core/projects.py`（项目关闭/删除闸门）
- **关系**：本方案**翻转** [container-execution-architecture.md](container-execution-architecture.md) 拍板 2 的 L2 生命周期结论（2026-09-23 per-command → 2026-09-24 per-task），是其下一个实施切片；其拍板 3（**L3 仍 per-execution**）不变，本方案作用域 = L2 docker；并消解 pentest-tools-container-m0 的 M2「默认切换」
- **实施后**：决策回写 `DESIGN.md` §7，同步 `core/runtime/CLAUDE.md`、`core/agent/CLAUDE.md`、`webui/CLAUDE.md`

## 0. 拍板记录（2026-09-24）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | net=real 审批口径 | **任务级网络窗口**：审批绑定 task；容器创建时 net=none，审批通过后 `docker network connect bridge`，任务全程有效，冻结/销毁时断网；窗口开闭落审计事件 |
| 1b | 渗透轨默认网络 | **pentest 任务容器默认 bridge 出网，免逐条审批**（2026-09-24 追加）：断网=无法作业，none 是恶意样本轨的默认；全程流量审计留痕，fakenet/断网可手动切换。**出网范围（仅授权目标 vs 全网）见 §2.3，范围收窄作为硬化里程碑不阻塞默认出网** |
| 2 | 失败/等人容器 | **立即冻结，保留 24h**：收尾收证后 `docker stop` 释放内存，文件层留 24h 可续跑，超时 reap |
| 3 | docker 下 pathguard | **改告警 + 证据声明**：容器内绝对路径（含 /tmp）不再硬拒（触不到宿主），写工作区外落 warning 并要求关键产物 bb_add_artifact；host/wsl 硬拒不变 |
| 4 | 关窗语义 | **确认弹窗二选一**：关页签时选「后台继续 / 中断任务」；pywebview 最后一窗仍走优雅停机 |

触发背景：04:19 三连拒熔断——pathguard 对 `2>/dev/null;`（紧贴分号）误报 + 一命令一容器下 `/tmp` 纪律过硬。

## 1. 模型总览

- 任务 claim 后首次执行时创建**任务容器（T-container）**：`docker run -d --restart no`，唯一名 `cs-<pid>-<tid12>`，labels `cs.platform=1 / cs.project / cs.task / cs.level=L2 / cs.created`；entrypoint `sleep infinity`
- 任务内所有命令走 `docker exec`，每条强制 `-w /workspace/scratch`
- 卷与挂载沿用 M0：`<workspace>:/workspace` 整卷读写；容器内写 `/workspace` = 写宿主工作区，写其余路径只动容器 rootfs
- 资源限额创建时钉死：`--memory 512m --pids-limit 64 --cpus 1.0 --security-opt no-new-privileges`（L2 保留默认 caps——nmap 等工具需要；数值走 config）
- 镜像由任务属性决定，默认 `cyberstrike/pentest-box:0.1`
- 容器状态机：`RUNNING → FROZEN（stopped，FS 保留）→ GONE`；生命周期**绑定任务，不绑定窗口或后端进程**

新增 `core/runtime/taskcontainers.py`：`TaskContainerManager`（ensure/exec/freeze/resume/remove/connect_net/cp_out/reconcile），Docker CLI 全部经 `DockerBackend` 新增原语发出。

## 2. 命令约束

1. **状态残留纪律**：每条 exec 独立 `sh -c`，强制工作目录；`cd/export` 不跨命令，Agent 纪律文案保留；**后台进程跨命令存活是特性**（监听/pwn 交互），由 pids-limit 限数 + 任务结束容器必毁兜底。
2. **中断真杀**：现「杀 docker CLI 客户端、容器内进程残留」改为——exec 包装器把远端 pgid 写 `/tmp/.cs-<callid>.pgid`，abort 时再发 `docker exec kill -TERM -<pgid>`，短超时升级 SIGKILL；整条任务中止时容器直接 stop。
3. **网络（拍板 1/1b）**：容器初始网络按**轨默认**——pentest=bridge（默认可出网，无需审批）；其余轨=none。非默认轨要用 net=real 时走 approval kind=`net_window`（携带 task_id，approved 后不因首条命令 consumed）：网关幂等 `network connect bridge` + 落 `network.window{open,approval_id}`；freeze 物理断网（docker stop），resume 后按轨默认/有效窗口重连；remove 时落 `network.window{close}`；每条 exec 的 command 事件自带网络标注。
   - **出网范围两层走**：M1 先保证 pentest 能出网（bridge 全网可达，和现在 host/wsl 渗透作业口径一致）；硬化里程碑做**授权目标 egress 白名单**——来源=项目规则 scope 段（rules-four-section 已定结构）+ 目标资产 host/CIDR，经 sidecar 防火墙或宿主侧 egress 规则限定，范围外目的地触发审批，审计事件不变。ctf 远程题轨将来照此同办（建项目标已知即默认出网）。
4. **pathguard 容器侧降级（拍板 3）**：`would_deny` 的工作区逃逸分支按 runtime 分流——host/wsl 维持硬拒；docker 下逃逸结果转为 warning 写入 command/command.result 事件 payload，不落 audit.deny、不计 E2 熔断。
5. **顺带修 `/dev/null;` 误报**：写目标提取归一化时剥除尾部 shell 元字符（`;` `&` `|` `)`）再比白名单/判路径；host/wsl/docker 三通道同修；钉测试 `2>/dev/null;`、`2>/dev/null|grep`、`>/etc/passwd`（仍拦）。
6. 网关加任务作用域：AgentSession 在 run_task 体外层 `with gateway.task_scope(pid, tid, ...)`；作用域内 docker 命令路由到 exec；作用域不负责销毁（销毁只由任务终态路径触发）。

## 3. 关键产物：销毁前必落宿主

声明制 + 收尾前门 + 收集器三段：

1. **声明**：Agent 经 `bb_add_artifact(path, kind, desc)` 声明证据（artifacts.meta 已存在），路径为容器内绝对路径。
2. **done 前门**：任务走 done 前，管理器在容器内逐路径 stat 校验存在性；缺失 → 文本打回 Agent 循环补救（镜像 verify.py 收尾钩子，容器还活着），不产生终态。
3. **终态收集**（done/fail 同流程，在 stop 之后；`docker cp` 支持停容器）：
   - 路径在 `/workspace` 下 → 本就在宿主：算 sha256、回写 host_path；
   - 路径在外（/tmp 等）→ `docker cp` 到宿主 `workspaces/<pid>/artifacts/<tid>/`；
   - 另采 best-effort 场景包：`ps aux`、网络连接表、/tmp 与 $HOME 清单 → `artifacts/<tid>/_scene/`（与 task-workspace.md 的任务目录约定将来合流）；
   - 事件：`evidence.collected{path,sha256,bytes}` / `evidence.missing{path}`。
4. **顺序铁律**：cp 失败 → 容器保持 FROZEN 不删，落 `evidence.error` 转 awaiting_human；TTL reaper 到期必须**重试 cp，成功才删**，管理员强制清理需显式端点二次确认。

## 4. 异常生命周期矩阵

| 情形 | 容器去向 |
|---|---|
| 任务 done | stop → 收集 → rm |
| 失败可续跑 / awaiting_human（含拒绝熔断） | stop（立即）→ 收集 → FROZEN 保留 24h；续跑 = `docker start`，网络按轨默认恢复（pentest 自动 bridge），有效 net_window 随容器续用 |
| 后端崩溃 / 被硬杀 | `-d --restart no`：容器在 daemon 侧存活（daemon 重启则停），**启动对账**（见下） |
| 优雅停机（停止平台.bat / 最后 pywebview 窗） | shutdown 钩子扩展：中断当前 exec（真杀）→ 落既有任务快照 → 冻结全部 T-container |
| 用户关 LiveRoom 页签（拍板 4） | 弹确认：后台继续（不动）/ 中断任务（abort → 失败可续跑 → 冻结）；仅切换视图无影响 |
| 项目关闭/删除 | close_all 闸门内**先**按 `cs.project` label stop 容器（释放挂载文件句柄，Windows 删目录前必做），再走原删除流程；终态容器同样先收证 |
| docker daemon 不可用 | create/exec 报错文本回灌 → 任务失败可续跑；daemon 恢复后对账 |

**启动对账（serve 引擎初始化段，新进程必跑一次）**：`docker ps -a --filter label=cs.platform=1` 取 labels/状态，与黑板任务表 join：

1. 项目/任务不存在或任务已终态 → 若 RUNNING 先 stop → 尝试收证（可能是唯一副本）→ rm；
2. 任务 failed/awaiting → 确保 FROZEN，落 `container.adopted`，刷新 24h TTL；
3. 任务 claimed/in-progress 但 worker 已死 → 经现有租约回收把任务退回可续跑 → ensure FROZEN + 落 `container.orphaned`；重派后 `start` 续跑，**容器 rootfs 即现场**，弥补崩溃时来不及写快照。

## 5. 实施切分（由用户排期）

- **M1 容器核心**：`taskcontainers.py` + DockerBackend 新原语（detached run/exec/stop/start/rm/network/cp/ps-by-label）+ gateway.task_scope + worker 挂点（首跑 ensure；done→stop+收集+rm；失败/await→stop+冻结）+ TTL 24h sweeper + `/dev/null;` 修复 + docker pathguard 告警分流；feature flag `containers.task_scoped`，关闭时回落 per-command。
- **M2 证据链**：done 前门前置校验 + 终态 cp-out/卷内校验 + checksum 回写 + 场景包 + evidence.* 事件。
- **M3 异常恢复与网络窗**：启动对账 + shutdown 钩子冻结 + 项目关闭顺序 + 轨默认网络（pentest 默认 bridge 免审）+ net_window 审批种类与 connect/disconnect + doctor `container-*` 体检 + admin 容器列表/强制清理端点。（授权目标 egress 白名单作硬化里程碑另排）
- **M4 前端**：任务卡片容器状态徽标（运行/冻结 + TTL 倒计时）+ 续跑/立即销毁操作 + 关页签确认弹窗二选一 + 生命周期事件行 + net=real 审批展示任务窗口语义。Playwright 回归。

## 6. 测试要点

- manager 全链路用假 docker（命令行断言）：创建参数/labels/幂等 ensure/exec -w/冻结-恢复/收证顺序/失败不删；
- `/dev/null;` 紧贴元字符放行 + 真逃逸（`>/etc/passwd`、`>../x`）仍拦；
- 对账四分支（消失/终态/挂起/claim 中孤儿）；shutdown 冻结；项目删除时容器先停；
- 网络：pentest 容器创建即 bridge 零审批、freeze 断网、resume 自动重连；非默认轨 net_window approved→connect 且首命令不消费、终态 consumed；
- M4 Playwright：关页签弹窗两路径、冻结态 TTL 展示、续跑后命令落同一容器名。

## 7. 待打磨

1. 24h TTL 是否按项目/轨可配（ctf 跨天比赛场景）。
2. net_window 审批在 FROZEN 期间是否随冻结停表、续跑后是否需重新审批——暂按「审批 24h 内有效随容器走」。
3. ~~渗透轨是否默认出网~~ 已定（拍板 1b）；待打磨的是 egress 白名单落地形态（sidecar 防火墙 vs 宿主规则）与 ctf 轨是否同默认。
3. resume 后容器内时钟/随机状态不一致对 exploit 的影响提示文案。
4. 卷最小化（container-execution-architecture §4.6：只挂子目录、样本 ro）与现整卷挂载的收窄时机。
