# 执行网关管道死锁：poll 轮询 + stdout=PIPE 大输出必挂

## 状态

**已实施**（2026-09-23 用户拍板「开工」随快捷修复包落地：`backends.py` 三后端统一走 `_execute_piped` 排水线程版，`_wait_or_kill` 删除；真链路冒烟单行 120KB grep 0.2s 返回；全量回归 971 passed）。

## 拍板记录

| 决策点 | 结论 |
|----|----|
| 根因定性 | 确认：`NativeBackend._run` 的 `poll()` 轮询不读管道，输出 >64KB 管道缓冲即死锁（实验复现） |
| 修复取向 | 排水线程（保住 ■ 即点即停语义）；2026-09-23 实施 |

## 关联代码

- `core/runtime/backends.py:86-101`（`NativeBackend._run`——Popen + `_wait_or_kill` poll 轮询）
- `core/runtime/backends.py:46-65`（`_wait_or_kill`——`proc.poll()` 循环，从不消费 stdout/stderr）
- `core/runtime/backends.py:157`（`DockerBackend._docker` 同形态）
- `core/runtime/gateway.py`（run → _dispatch → backend.execute 全链路）

## 1. 事故现场（2026-09-23 中原工学院项目）

Agent 拿 spill 落盘文件（`spill/20260923-200407-bb_query-f80394.txt`，**单行 120KB JSON**）定位 authserver 资产：

```
RUN wsl | grep -n -i "authserver" ../spill/20260923-200407-bb_query-f80394.txt | head -40
  -> exit -1, timed_out=True, duration=120.8s
RUN wsl | grep -n -i "202.196.33.2" ../spill/…同文件… | head -40
  -> exit -1, timed_out=True, duration=120.8s
```

**两条命令实际都匹配成功了**（`command.result` 的 stdout_head 里能看到 `1:[{"id": "asset-5d00cf8b73b7"…` 整行输出）——但命令挂满 120 秒被网关杀树，Agent 视为失败。随后 Agent 自救换 `grep -o`（只输出匹配片段，几百字节）→ **3.6 秒成功**拿到 `asset-5aff732f5665`。

## 2. 根因（决定性实验复现）

`backends.py` 的 `_run`：`stdout=PIPE` 起进程 → `_wait_or_kill` 用 `poll()` 轮询等退出**从不读管道** → 子进程输出超过 Windows 管道缓冲（64KB）后写阻塞 → 父进程等退出 → **双方死锁**，直到网关 timeout 杀树。

实验（复刻同形态）：

```python
proc = subprocess.Popen(['powershell', …, 输出 820KB 的命令],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
while proc.poll() is None: time.sleep(0.2)   # _wait_or_kill 同款
# → DEADLOCK CONFIRMED: no exit in 12s
```

**影响面**：host / wsl 两后端的**一切命令**——任何 stdout 或 stderr 超 64KB 的调用（大查询结果、扫描输出、日志 dump）必死锁烧满 timeout。wsl 侧单行 JSON spill 文件 + `grep -n`（输出整行）是最易撞的形态；此前大量「命令超时」现象疑似同根因。DockerBackend._docker 同形态同样暴露。

附带效应：单行 JSON 的 spill 文件对行工具（grep -n / head 分页）不友好——grep 匹配即输出 120KB 整行；`grep -o` 输出片段是唯一稳定姿势（Agent 自救已验证）。

## 3. 修复方案（M1）

**排水线程**：`_wait_or_kill` 前起两个 daemon 线程分别消费 `proc.stdout` / `proc.stderr`（读到 EOF 存局部），主循环 `poll()` + abort_event 检查语义不变；正常结束或杀树后 join 线程取输出。改动集中在 `NativeBackend._run` / `DockerBackend._docker` 两处，■ 即点即停完全保留。

- 杀树后管道关闭 → reader 线程 EOF 自然退出，无悬挂。
- 实施后回归验证两场景：①纯 host PowerShell 输出 >64KB（实验命令）秒回不挂；②wsl `grep -n` 单行 120KB spill 文件——**若 wsl.exe 中继层仍有「子进程退出后 wsl.exe 不退」的独立 bug（排水救不到），追加 M1b 处理 wsl.exe 退出策略，以实测为准**。
- M2（可选，另一方案文档）：spill 落盘「行化」——单行 JSON 改为一行一对象，行工具天然可用；若 M1 修复后 Agent 靠 `grep -o` 自救已足够，此项降级不实施。
