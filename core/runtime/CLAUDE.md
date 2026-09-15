# core/runtime/

> 执行环境层（DESIGN.md §7）：唯一命令出口。Agent 无裸 shell，一切命令经网关 + 策略校验。

## 文件

| 文件 | 职责 |
|------|------|
| `policy.py` | 隔离等级 **L0 host < L1 wsl < L2 docker < L3 sandbox**（注意：L1-L3 一名已被本节占用，逆向复用三层因此命名 R1/R2/R3，见 DESIGN §9）；`allowed_runtimes(threat_class)`——trusted 全等级、untrusted 仅 docker/sandbox、**unknown 缺省按 malware_live 只允许 sandbox**（宁严勿松）；网络模式 none/fakenet/real，默认 none |
| `gateway.py` | `ExecutionGateway(backends=..., bb=?)`：按策略选 backend 执行，违例抛 `GatewayDenied`（Agent 侧回填 `[网关拒绝]` 改道，不炸循环）；net=real 走审批；`allowed_runtimes_names(threat_class)` |
| `backends.py` | `NativeBackend`（host/wsl 经 PowerShell，`--%` 停止解析坑见 DESIGN §9）、`WSLBackend`（信任级=宿主机）、`DockerBackend`（不可信代码默认）、`BackendError` |
| `detector.py` | `HostDetector`/`CapabilityInventory`：探测 IDA/Ghidra/Docker/WSL 等能力（PATH + 常见安装目录），服务于三态灯与降级 |

## 约定与坑

- 不可信代码只允许 docker/sandbox；活体恶意样本必须 L3 + net=none/fakenet，**fakenet 尚未实现（NotImplementedError），malware 轨后置**。
- WSL 不是隔离边界：信任级与宿主同级，escalation 不可放松本条与 L3 硬底线。
- 工具异常一律文本回填 Agent 循环（见 core/agent/CLAUDE.md）。
- 测试：tests 内 FakeDockerBackend 模式（断言 sandbox + net=none）；网关拒绝/降级用例在 tests/test_agent.py。
