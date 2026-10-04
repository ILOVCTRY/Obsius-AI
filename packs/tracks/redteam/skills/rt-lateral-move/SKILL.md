---
name: rt-lateral-move
description: 横向移动入口：凭据复用、PTH、WMI/PsExec/SMB 类横向、Kerberos 攻击、Relay 的打法路由（redteam 轨）
keywords: 横向, 横向移动, pth, hash传递, wmi, psexec, smbexec, winrm, kerberoasting, asreproast, relay, ntlm, 域, 域控, 凭据, mimikatz, 内网
task_types: lateral-movement, credential-access
mode: self-contained
---

# lateral-move —— 横向移动路由（redteam 轨）

> 分层纪律：本技能只做**横向阶段 → 手册对照**。打法在 web 能力包
> `playbooks/intranet-postexp/`。按需 `skill_open(path=…)` 单篇，禁止通读。
> ROE：本技能动作受 redteam 红线 ROE 条目约束（授权边界/操作红线）。红线：借力其它资产作垫脚石必须在攻击链中记录溯源；动静克制（角色人设）。

## 阶段 → 手册对照表（web 包 `playbooks/intranet-postexp/` 下）

| 阶段 | skill_open 模块 |
|---|---|
| 凭据收集（横向的弹药） | `10-credentials.md` |
| 横向移动打法总纲 | `11-lateral.md` |
| 域内信息/域攻面 | `14-domain.md` |
| Exchange / 邮件面 | `18-exchange.md` |
| ADCS 误配横向 | `19-adcs.md` |
| SharePoint 面 | `20-sharepoint.md` |
| 隧道打通新网段 | `15-tunneling.md` |

## 纪律

- 横向前必过内网侦察（intranet-recon）：没有可达面判断的横移是盲打。
- 凭据来源与使用位置逐一写黑板（凭据复用是溯源链核心环节）。
- Kerberos 攻击（kerberoasting/asreproast）先看域内账号属性再选目标。
