---
name: rt-privesc
description: 纵向提权入口：Windows/Linux 落点内提权路径枚举与打法路由——sudo/SUID/内核/服务/令牌/计划任务（redteam 轨）
keywords: 提权, privesc, sudo, suid, 内核, kernel, 服务, 计划任务, 令牌, token, windows 提权, linux 提权, 补丁, misconfig, 权限提升
task_types: privesc
---

# privesc-win-lin —— 纵向提权路由（redteam 轨）

> 分层纪律：本技能只做**提权阶段 → 手册对照**。打法在 web 能力包
> `playbooks/intranet-postexp/`。按需 `kb_open(module=…)` 单篇，禁止通读。
> ROE：本技能动作受 redteam 红线 ROE 条目约束（授权边界/操作红线）。红线：只在已授权落点内行动（角色人设）；内核提权类 exploit 必须 docker/sandbox 验证。

## 阶段 → 手册对照表（web 包 `playbooks/intranet-postexp/` 下）

| 阶段 | kb_open 模块 |
|---|---|
| 提权打法总纲 | `12-privesc.md` |
| 免杀与规避（提权动作被拦时） | `13-evasion.md` |
| 权限维持（提权成功后） | `17-persistence.md` |
| 域内提权（普通域用户 → 域管面） | `14-domain.md`、`19-adcs.md` |

## 纪律

- 先枚举后利用：`12-privesc.md` 的清单跑完再挑路径，别拿到第一个 misconfig 就梭。
- CTF 向 Linux 提权杂项（SUID/能力位等速查）在 misc 包 `misc/misc/linux-privesc.md`。
- 每条提权路径记证据（版本/配置/命令输出），failed 也写原因。
