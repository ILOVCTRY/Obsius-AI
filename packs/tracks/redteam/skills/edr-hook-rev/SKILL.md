---
name: edr-hook-rev
description: EDR/AV 钩子逆向与遥测致盲：主流厂商指纹调研、ntdll hook 表 dump、unhook/syscall 技术选型、ETW/AMSI bypass、Sysmon 规避（仅授权红队）
keywords: edr, av, defender, mde, crowdstrike, sentinelone, elastic, eset, hook, unhook, syscall, etw, amsi, 遥测, 绕过, t1562, sysmon, pe-sieve, blindside, hells gate
task_types: exploit, credential-access
---

# edr-hook-rev —— EDR 钩子逆向与遥测致盲（仅授权环境）

> **仅限授权红队 / 对抗演练 / 自有产品测试。**

## 适用场景

- 授权评估中发现 implant 被 EDR 拦截 → 调研目标 EDR 监控面 → 选型绕过组合
- 蓝队/产品方视角同样适用：了解攻击方对策以完善检测

## 手册对照表（特征 → 打开哪篇，用 kb_open）

| 场景/特征 | 手册 |
|---|---|
| 先调研：目标机器装了哪家 EDR / 监控什么 | `binary/edr/hook-survey.md`（厂商指纹表 + 快速指纹脚本） |
| 看 ntdll 有没有被 hook / dump hook 表 | `binary/edr/hook-survey.md`（IDA+windbg 流程 / pe-sieve / API Monitor） |
| 内核侧监控点（callback/微过滤） | `binary/edr/hook-survey.md`（内核 callback 段 + windbg 枚举） |
| 选绕过技术：unhook / 直接间接 syscall / SSN 解析 | `binary/edr/unhook-techniques.md`（四法对照 + SysWhispers3 用法） |
| HWBP Blindside / call stack spoof | `binary/edr/unhook-techniques.md`（第 5/6 节 + 选型表） |
| 压 ETW / AMSI / PowerShell 日志 / Sysmon 规避 | `binary/edr/telemetry-blinding.md`（四法 ETW patch + AMSI 三级 + OPSEC 顺序） |
| 操作顺序总纲（先压什么后做什么） | `binary/edr/telemetry-blinding.md`（第 7 节：AMSI→ETW→unhook→spoof→payload） |
| Windows 工具链自举（pe-sieve/SysWhispers 等装不上时） | `binary/reverse/cases/windows-reverse-toolchain-bootstrap.md` |

## 红线与收尾

- **顺序错了先告警**：AMSI → ETW patch → unhook/indirect syscall → stack spoof →
  才轮到 payload；先 unhook 会被 ETW-TI 立刻上报
- 每个技术在 sandbox 内验证后再上真实授权环境；留存决策依据
- 与 rt-* 轨技能的关系：本技能管「让执行不被看见」；横向/凭据动作归
  rt-lateral-move / rt-privesc
