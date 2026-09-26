# Windows 子进程隐藏窗口：CREATE_NO_WINDOW 全执行点覆盖

## 状态

**已实施**（2026-09-23 用户拍板「开工」随快捷修复包落地：`backends.py` 模块常量 `NO_WINDOW_FLAGS` 覆盖 `_run`/`_docker`/`_kill_tree`，detector/decompiler 导入复用；全量回归 971 passed）。

## 拍板记录

| 决策点 | 结论 |
|----|----|
| 根因 | 确认：Popen 无 creationflags，窗口模式（pythonw 无控制台）下每条命令新建控制台 |
| 修复范围 | 只改子进程**创建点**加标志，run 网关/命令拼装层不动（孙进程实验已证链路生效）；2026-09-23 实施 |

## 关联代码

- `core/runtime/backends.py:90`（`NativeBackend._run`——**弹窗主源**，agent 每条命令）
- `core/runtime/backends.py:25`（`_kill_tree` 的 taskkill——每次中断/超时杀树闪一个）
- `core/runtime/backends.py:157`（`DockerBackend._docker`——docker CLI 调用）
- `core/runtime/detector.py:58`（`_run_probe`——启动时环境探测弹几下）
- `core/tools/decompiler.py:145`（`_default_runner`——每次反编译弹一个）

## 1. 根因

桌面窗口模式（pythonw / 打包 exe）下 serve 进程**无控制台**。Windows 规则：子进程默认继承父控制台，父进程没有就为 console 子系统程序**新建一个控制台窗口**。`backends.py` 起的 powershell.exe 不带 `creationflags` → agent 每步跑一条命令弹一个窗。控制台模式（`启动平台.bat`）下不弹，只在此模式暴露。

## 2. 实测定论（2026-09-23 实验）

`CREATE_NO_WINDOW` 起的 powershell 内部再起 python 孙进程，测得孙进程 `GetConsoleWindow()=0`——**隐藏标志对整条进程链生效**。因此只需改平台自己创建子进程的点；agent 命令内部的工具（nmap/python 等）经 powershell 链自动继承无窗状态，`tools/py/` CTF runner 等被拉起脚本无需单独处理。

IDA 启动（`app.py:2372` 带 flags）与 `ida_mcp_manager.py`（DETACHED_PROCESS）已处理，不动。

## 3. 修复方案

五个创建点统一加 `creationflags`：Windows = `subprocess.CREATE_NO_WINDOW`，POSIX = `0`（subprocess 接受，无副作用）。

- 常量建议在 `backends.py` 定义一次（`NO_WINDOW_FLAGS`），detector / decompiler 导入复用。
- 改动约 10 行，零行为变化（纯窗口抑制）；测试不改断言（Linux CI 上 creationflags=0 合法）。
