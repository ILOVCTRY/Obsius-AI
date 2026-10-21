---
name: Web接口攻击手法
description: API 与协议层攻击入口：REST/GraphQL/WebSocket/网关签名/HTTP 走私/Host 头/缓存投毒/SSRF 的特征路由与对照表
keywords: api, 接口, rest, graphql, websocket, swagger, openapi, 接口文档, 网关, 签名, 重放, hpp, 参数污染, 请求走私, smuggling, http2, host头, 缓存投毒, ssrf, 内网请求, webhook, 云, metadata, 邮件头
features: has_graphql, has_websocket
vuln_classes: ssrf, graphql, websocket, http-smuggling, cache-poisoning, hpp, host-header, api-abuse
task_types: exploit
mode: self-contained
---

# Web接口攻击手法 —— API 与协议层攻击路由

> 分层纪律：本技能只做**API 面特征 → 手册对照**。方法论在 `web/漏洞分类/`，
> 弹药在 `web/实战打法/`。按需 `kb_open(module=…)` 单篇，禁止通读。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`web/漏洞分类/` 下） | 弹药（`web/实战打法/` 下） |
|---|---|---|
| REST 接口（swagger/openapi/大量 id 接口） | `web/漏洞分类/API网关/API 网关安全测试手册.md` | `web/实战打法/api-rest/10-REST API 核心攻击 payload 库.md` |
| GraphQL | `web/漏洞分类/GraphQL/GraphQL 安全测试.md` | `web/实战打法/GraphQL.md`、`web/实战打法/api-rest/11-GraphQL payload 库(API 视角).md` |
| WebSocket | `web/漏洞分类/WebSocket/WebSocket 安全测试手册.md` | `web/实战打法/api-rest/13-WebSocket 安全 payload 库.md` |
| 网关签名/时间戳/重放 | `web/漏洞分类/API网关/API 网关安全测试手册.md` | — |
| HPP 参数污染 | `web/漏洞分类/HTTP参数污染/hpp-test（几乎不交）.md` | — |
| HTTP 走私 | `web/漏洞分类/HTTP请求走私/HTTP 请求走私测试.md` | `web/实战打法/HTTP Request Smuggling、HTTP、2 Desync.md` |
| HTTP/2 特有攻击 | `web/漏洞分类/HTTP2攻击/http2-attacks-test（几乎不交）.md` | — |
| Host 头投毒/密码找回投毒 | `web/漏洞分类/Host头注入/Host 头注入与路由滥用.md` | — |
| Web 缓存投毒/欺骗 | `web/漏洞分类/缓存投毒/缓存投毒与缓存欺骗测试.md` | — |
| SSRF（核心打法） | `web/漏洞分类/SSRF/SSRF 测试手册.md` | `web/实战打法/ssrf-cache-host/10-SSRF — 核心 payload 库.md` |
| SSRF 打云（metadata/STS） | `web/漏洞分类/SSRF/SSRF 测试手册.md` | `web/实战打法/ssrf-cache-host/11-云元数据、IAM、K8s、S3 payload 库.md` |
| 缓存与 SSRF 组合 | `web/漏洞分类/缓存投毒/缓存投毒与缓存欺骗测试.md` | `web/实战打法/ssrf-cache-host/12-缓存投毒、缓存欺骗、CDN 绕过 payload 库.md` |
| 邮件头注入 | `web/漏洞分类/邮件头注入/email-header-injection-test（几乎不交）.md` | — |

## 纪律

- SSRF 是内网入口：打到内网后转 Web后渗透 / intranet-recon 路由，别在入口耗尽时间。
- 签名算法看不清时先走 `web/漏洞分类/JS逆向/JS 逆向配合接口挖掘指南.md`（Web客户端手法 路由）再回来重放。
- 接口枚举先 bb_query 查重，已测接口勿重扫（去重是硬规则）。
