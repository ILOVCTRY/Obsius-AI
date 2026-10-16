---
name: web-api-attack
description: API 与协议层攻击入口：REST/GraphQL/WebSocket/网关签名/HTTP 走私/Host 头/缓存投毒/SSRF 的特征路由与对照表
keywords: api, 接口, rest, graphql, websocket, swagger, openapi, 接口文档, 网关, 签名, 重放, hpp, 参数污染, 请求走私, smuggling, http2, host头, 缓存投毒, ssrf, 内网请求, webhook, 云, metadata, 邮件头
features: has_graphql, has_websocket
vuln_classes: ssrf, graphql, websocket, http-smuggling, cache-poisoning, hpp, host-header, api-abuse
task_types: exploit
mode: self-contained
---

# web-api-attack —— API 与协议层攻击路由

> 分层纪律：本技能只做**API 面特征 → 手册对照**。方法论在 `web/漏洞分类/`，
> 弹药在 `web/实战打法/`。按需 `kb_open(module=…)` 单篇，禁止通读。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`web/漏洞分类/` 下） | 弹药（`web/实战打法/` 下） |
|---|---|---|
| REST 接口（swagger/openapi/大量 id 接口） | `web/漏洞分类/api-gateway/手册.md` | `web/实战打法/api-rest/10-rest-api.md` |
| GraphQL | `web/漏洞分类/graphql/手册.md` | `web/实战打法/graphql.md`、`web/实战打法/api-rest/11-graphql.md` |
| WebSocket | `web/漏洞分类/websocket/手册.md` | `web/实战打法/api-rest/13-websocket.md` |
| 网关签名/时间戳/重放 | `web/漏洞分类/api-gateway/手册.md` | — |
| HPP 参数污染 | `web/漏洞分类/hpp/手册.md` | — |
| HTTP 走私 | `web/漏洞分类/http-smuggling/手册.md` | `web/实战打法/http-smuggling.md` |
| HTTP/2 特有攻击 | `web/漏洞分类/http2-attacks/手册.md` | — |
| Host 头投毒/密码找回投毒 | `web/漏洞分类/host-header/手册.md` | — |
| Web 缓存投毒/欺骗 | `web/漏洞分类/cache-poisoning/手册.md` | — |
| SSRF（核心打法） | `web/漏洞分类/ssrf/手册.md` | `web/实战打法/ssrf-cache-host/10-ssrf-core.md` |
| SSRF 打云（metadata/STS） | `web/漏洞分类/ssrf/手册.md` | `web/实战打法/ssrf-cache-host/11-cloud.md` |
| 缓存与 SSRF 组合 | `web/漏洞分类/cache-poisoning/手册.md` | `web/实战打法/ssrf-cache-host/12-cache.md` |
| 邮件头注入 | `web/漏洞分类/email-header-injection/手册.md` | — |

## 纪律

- SSRF 是内网入口：打到内网后转 web-post-exp / intranet-recon 路由，别在入口耗尽时间。
- 签名算法看不清时先走 `web/漏洞分类/js-reverse/手册.md`（web-client-side 路由）再回来重放。
- 接口枚举先 bb_query 查重，已测接口勿重扫（去重是硬规则）。
