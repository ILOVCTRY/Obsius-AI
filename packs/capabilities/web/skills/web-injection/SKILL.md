---
name: web-injection
description: 注入类漏洞入口：SQL/SSTI/RCE/反序列化/XXE/JNDI/文件包含/路径穿越/原型污染的特征路由与对照表
keywords: 注入, sql, sqli, 盲注, 堆叠, ssti, 模板注入, 命令执行, rce, 反序列化, php, java, python, xxe, 实体注入, jndi, log4j, el注入, spel, xslt, 文件包含, lfi, rfi, 路径穿越, traversal, 原型污染, prototype, 表达式
features: has_search, has_filter
vuln_classes: sqli, ssti, rce, deserialization, xxe, jndi, lfi, path-traversal, prototype-pollution, el-injection
task_types: exploit
---

# web-injection —— 注入类漏洞路由

> 分层纪律：本技能只做**注入点特征 → 手册对照**。方法论在 `playbooks/知识库/`，
> 弹药在 `playbooks/`。按需 `kb_open(module=…)` 单篇，禁止通读。
> payload 优先查弹药库，现场构造须标注（领域红线 4）。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`playbooks/知识库/` 下） | 弹药（`playbooks/` 下） |
|---|---|---|
| SQL 注入（联合/报错/盲注） | `web/webapp/sqli/手册.md` | `sqli.md` |
| SSTI（模板注入：jinja2/twig/freemarker） | — | `rce/14-ssti.md` |
| 命令注入/拼接 | — | `rce/11-command-injection.md` |
| 框架/组件已知 RCE | — | `rce/10-framework.md` |
| Java/PHP 反序列化 | `web/webapp/deserialization/手册.md` | `rce/12-deserialization.md` |
| XXE（有回显/OOB） | `web/webapp/xxe/手册.md` | `rce/15-xxe.md`、`web/webapp/xxe/手册.md` |
| JNDI / Log4j | `web/webapp/jndi-injection/手册.md` | — |
| EL / SpEL / OGNL 表达式 | `web/webapp/el-injection/手册.md` | — |
| XSLT 注入 | `web/webapp/xslt-injection/手册.md` | — |
| 文件包含 / LFI / RFI | `web/webapp/path-traversal/手册.md` | `path-traversal/10-traversal-lfi.md`、`11-rfi-logpoison.md` |
| PHP 伪协议 / phar / session | — | `path-traversal/12-php-wrappers.md`、`13-phar-session-proc.md` |
| 原型污染（Node） | `web/webapp/prototype-pollution/手册.md` | `rce/17-prototype-pollution.md` |
| 文件上传 RCE 链 | `web/webapp/file-upload/手册.md` | `file-upload/00-index.md`（上游 web-strike-entry 路由） |
| 无回显验证 | `web/webapp/dnslog-oob/手册.md` | — |

## 纪律

- 先判注入点上下文（回显/报错/盲/OOB）再选手册；盲注先配 dnslog。
- PHP 弱类型与 %00 截断见 `web/webapp/type-juggling/手册.md`（认证绕过面归 web-authn-session）。
- RCE 链组成（上传→包含→执行）先画链路再动手，逐环落证据。
