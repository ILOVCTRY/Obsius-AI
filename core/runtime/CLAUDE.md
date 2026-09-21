# core/runtime/

> 执行环境层（DESIGN.md §7）：唯一命令出口。Agent 无裸 shell，一切命令经网关 + 策略校验。

## 文件

| 文件 | 职责 |
|------|------|
| `policy.py` | 隔离等级 **L0 host < L1 wsl < L2 docker < L3 sandbox**（注意：L1-L3 一名已被本节占用，逆向复用三层因此命名 R1/R2/R3，见 DESIGN §9）；`allowed_runtimes(threat_class)`——trusted 全等级、untrusted 仅 docker/sandbox、**unknown 缺省按 malware_live 只允许 sandbox**（宁严勿松）；网络模式 none/fakenet/real，默认 none |
| `pathguard.py` | **工作区路径守卫（W 组，2026-09-17）**：纯函数静态提取命令写目标（重定向/curl -o/--output（含 nmap -oG 连写/= 连写）/Out-File/Set-Content/Tee-Object/tee 等）→ `workspace_escapes(cmd, scratch=, workspace=, posix=)` 判逃逸（绝对路径不在工作区内、相对路径 `..` 上穿→拒；`$env:`/`%var%` 变量不可静态解析→放行；/dev/null、NUL、2>&1 放行）。**只拦写不拦读**；强护栏非沙箱（真边界靠 E4b 容器终态）。注意 `_is_flag` 在 posix 下不把 `/path` 当 flag |
| `gateway.py` | `ExecutionGateway(backends=..., bb=?)`：按策略选 backend 执行，违例抛 `GatewayDenied`（Agent 侧回填 `[网关拒绝]` 改道，不炸循环）；net=real 走审批；`allowed_runtimes_names(threat_class)`。**工作区隔离（W1/W2/W4）**：`run(..., workspace=)` host/wsl 时先 pathguard 硬拒（`audit.deny{reason:"workspace-escape"}`）→ cwd 强制 `<ws>/scratch`、TEMP/TMP 重定向 `<ws>/.tmp`（wsl 走 `--cd` + TMPDIR 前缀，`windows_to_wsl_path` 换算）；command 事件补 `cwd`。**扫描限速纪律（2026-09-19 借鉴 dsh）**：工作区检查后调 `rateguard.check_rate(cmd)`，拒因非空即 `_audit_deny`（四规则见 rateguard 行；`audit.deny` 事件 Agent 改道协议零改动）。**事件时序（2026-09-18 直播间终端化）**：`command` 事件**执行前**落（带 `call_id`，前端直播可见「运行中」；被拒命令仍不落 command 只落 deny）；`command.result` 补 `call_id`+冗余 `cmd`（自含可独立渲染防上翻孤儿），stdout/stderr head 截断 500→2000；`_dispatch` 抛 BackendError 时 command 已落 result 缺失=前端「悬空运行中」语义诚实。**■ 即点即停（2026-09-19）**：`run(..., abort_event=)` 透传 backend——命令执行中置位即杀进程树立刻返回（`ExecutionResult.interrupted`+brief「被人手中断」+command.result 事件 `interrupted:true`），不等命令自然结束。**would_deny 干跑判定（H3，2026-09-19）**：`would_deny(cmd, runtime, *, threat_class, net, workspace) -> str|None`——不执行不审计的策略校验（runtime 合法/隔离等级/非法 net/工作区逃逸/限速纪律），run() 内部复用同一方法**策略口径单一出处**；不含 net=real 审批校验（依赖 approval_id，run() 自查）与后端可用性；Agent 的 request_escalation 升级工具用它做前置判定。**审批一次性消费（H3，2026-09-19）**：run() 返回前 `approval_id` 非空即 `_consume_approval`（approved→consumed，幂等尽力而为静默失败）——同单二次执行被拒，封死「net=real 长期通行证」。**截断可见化（2026-09-20）**：`brief(limit=2000)` stdout/stderr 被切时尾部追加 `…[stdout 已截断：2000/3000 字符，请缩小窗口分段读]`——Agent 必须能察觉被切；标记刻意紧凑（+~40 字符/条），长格式会翻转 G3 摘要触发 |
| `rateguard.py` | **限速纪律纯函数（2026-09-19）**：`check_rate(cmd) -> str|None`，`&&/||/;/|` 分段逐段查（复用 pathguard `_clean/_split_tokens`；`_binname` 取首 token basename 去 .exe）。四规则：nmap 全端口（-p- 变体）无 -T0..3/--max-rate/--max-parallelism/--scan-delay 拦；masscan --rate>1000 拦；ffuf 无 -rate/-rl 拦；hydra 无 -t 拦——**拒因一律带放行配方**（怎么改才放行）。纯静态检查不执行命令 |
| `backends.py` | `NativeBackend`（host/wsl 经 PowerShell，`--%` 停止解析坑见 DESIGN §9）、`WSLBackend`（信任级=宿主机）、`DockerBackend`（不可信代码默认）、`BackendError`。**协作取消（2026-09-19）**：`execute/run_once/exec_in` 均收 `abort_event`——Popen 轮询等待（`POLL_INTERVAL=0.2s` 即最大中断延迟），abort 置位或超时经 `_kill_tree` 杀进程树（Windows `taskkill /PID x /T /F` 防子进程留尸）+ `_wait_or_kill` 共用；`ExecOutcome.interrupted` 标记。**已知局限**：WSL/Docker 杀的是客户端进程（wsl.exe/docker CLI），WSL 侧/容器内残留由 distro/容器生命周期兜底 |
| `detector.py` | `HostDetector`/`CapabilityInventory`：探测 IDA/Ghidra/Docker/WSL 等能力（PATH + 常见安装目录），服务于三态灯与降级 |

## 约定与坑

- 不可信代码只允许 docker/sandbox；活体恶意样本必须 L3 + net=none/fakenet，**fakenet 尚未实现（NotImplementedError），malware 轨后置**。
- WSL 不是隔离边界：信任级与宿主同级，escalation 不可放松本条与 L3 硬底线。
- 工具异常一律文本回填 Agent 循环（见 core/agent/CLAUDE.md）。
- 测试：tests 内 FakeDockerBackend 模式（断言 sandbox + net=none）；网关拒绝/降级用例在 tests/test_agent.py。
