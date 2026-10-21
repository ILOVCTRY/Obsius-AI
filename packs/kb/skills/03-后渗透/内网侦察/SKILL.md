---
name: 内网侦察
description: 内网侦察入口：拿到落点后的网段测绘、存活探测、服务枚举、域信息收集的打法路由（redteam 轨）
keywords: 内网侦察, 网段, 存活, 端口扫描, smb, netbios, ldap, 域, ad, 内网资产, fscan, arp, 路由, 可达面, 落点
task_types: recon, lateral-movement
mode: self-contained
---

# intranet-recon —— 内网侦察路由（redteam 轨）

> 分层纪律：本技能只做**内网侦察阶段 → 手册对照**。打法在 web 能力包
> `web/后渗透/intranet-postexp/`。按需 `kb_open(module=…)` 单篇，禁止通读。
> 侦察流量走正常扫描预算，先查黑板资产表去重再扫。

## 阶段 → 手册对照表（web 包 `web/后渗透/intranet-postexp/` 下）

| 阶段 | kb_open 模块 |
|---|---|
| 内网侦察总纲（起手必做） | `web/后渗透/intranet-postexp/16-内网、后渗透 — 信息收集 payload 库.md` |
| 域内信息收集 | `web/后渗透/intranet-postexp/14-内网、后渗透 — 域渗透攻击 payload 库.md` |
| 凭据线索收集（配置/历史/密钥） | `web/后渗透/intranet-postexp/10-内网、后渗透 — 凭证窃取 payload 库.md` |
| 隧道可达性判断（要先搭代理吗） | `web/后渗透/intranet-postexp/15-内网、后渗透 — 隧道代理 payload 库.md` |

## 纪律

- 侦察产出一律落黑板资产（bb_add_asset 带 type 自动识别），禁止只留在会话里。
- 扫描前先看角色 default_noise 与红线（渗透轨弱口令/爆破约束）。
- 域环境先判「是否在域内」（`web/后渗透/intranet-postexp/14-内网、后渗透 — 域渗透攻击 payload 库.md`），再选域内/工作组两条路。
