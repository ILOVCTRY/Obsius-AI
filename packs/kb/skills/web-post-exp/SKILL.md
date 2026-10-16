---
name: web-post-exp
description: Web 后渗透入口：webshell/内网横向/提权/隧道/持久化/域内动作的打法路由与对照表（web 落点之后）
keywords: 后渗透, webshell, 内存马, 内网, 横向, 提权, 隧道, 代理, frp, 持久化, 权限维持, 域控, kerberos, adcs, exchange, 凭据收集, mimikatz, 免杀, 逃逸, 落点
vuln_classes: webshell, post-exploitation, lateral-movement, privesc, tunneling, persistence, ad
task_types: exploit, privesc, lateral-movement, credential-access
mode: self-contained
---

# web-post-exp —— Web 后渗透路由

> 分层纪律：本技能只做**落点后的阶段 → 手册对照**。打法在
> `web/后渗透/intranet-postexp/`。按需 `kb_open(module=…)` 单篇，禁止通读。
> 红线提醒：样本/工具执行只允许 docker/sandbox（能力包红线）；
> 借力其它资产须记录溯源（横向角色人设）。

## 阶段 → 手册对照表（`web/后渗透/intranet-postexp/` 下）

| 阶段 | kb_open 模块 |
|---|---|
| 总览（先看这篇定打法） | `web/实战打法/00-index.md` |
| 凭据收集（配置/密钥/内存） | `web/后渗透/intranet-postexp/10-credentials.md` |
| 横向移动 | `web/后渗透/intranet-postexp/11-lateral.md` |
| 提权 | `web/后渗透/intranet-postexp/12-privesc.md` |
| 免杀与规避 | `web/后渗透/intranet-postexp/13-evasion.md` |
| 域内信息/域攻 | `web/后渗透/intranet-postexp/14-domain.md` |
| 隧道与代理 | `web/后渗透/intranet-postexp/15-tunneling.md` |
| 内网侦察（起手必做） | `web/后渗透/intranet-postexp/16-recon.md` |
| 权限维持 | `web/后渗透/intranet-postexp/17-persistence.md` |
| Exchange 打点 | `web/后渗透/intranet-postexp/18-exchange.md` |
| ADCS 误配 | `web/后渗透/intranet-postexp/19-adcs.md` |
| SharePoint | `web/后渗透/intranet-postexp/20-sharepoint.md` |

## 纪律

- 落点第一动作永远是 `web/后渗透/intranet-postexp/16-recon.md` 定位所在网段与可达面，再决定横移/提权。
- webshell/内存马打法见 web 能力包 rce 弹药（`rce/` 下）与 `web/实战打法/unauth-access.md`。
- 每一步写黑板：借力资产、凭据来源、时间线——溯源链不完整等于白打。
