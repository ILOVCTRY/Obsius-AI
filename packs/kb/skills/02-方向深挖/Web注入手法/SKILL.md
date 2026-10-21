---
name: Web注入手法
description: 注入类漏洞入口：SQL/SSTI/RCE/反序列化/XXE/JNDI/文件包含/路径穿越/原型污染的特征路由与对照表
keywords: 注入, sql, sqli, 盲注, 堆叠, ssti, 模板注入, 命令执行, rce, 反序列化, php, java, python, xxe, 实体注入, jndi, log4j, el注入, spel, xslt, 文件包含, lfi, rfi, 路径穿越, traversal, 原型污染, prototype, 表达式
features: has_search, has_filter
vuln_classes: sqli, ssti, rce, deserialization, xxe, jndi, lfi, path-traversal, prototype-pollution, el-injection
task_types: exploit
mode: self-contained
---

# Web注入手法 —— 注入类漏洞路由

> 分层纪律：本技能只做**注入点特征 → 手册对照**。方法论在 `web/漏洞分类/`，
> 弹药在 `web/实战打法/`。按需 `kb_open(module=…)` 单篇，禁止通读。
> payload 优先查弹药库，现场构造须标注（领域红线 4）。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`web/漏洞分类/` 下） | 弹药（`web/实战打法/` 下） |
|---|---|---|
| SQL 注入（联合/报错/盲注） | `web/漏洞分类/SQL注入/注入类漏洞测试手册（SQL注入、命令注入、SSTI）.md` | `web/实战打法/SQL 注入.md` |
| SSTI（模板注入：jinja2/twig/freemarker） | — | `web/实战打法/rce/14-RCE — SSTI 模板注入 payload 库.md` |
| 命令注入/拼接 | — | `web/实战打法/rce/11-RCE — 命令注入、通用代码执行 payload 库.md` |
| 框架/组件已知 RCE | — | `web/实战打法/rce/10-RCE — 框架、中间件漏洞 payload 库.md` |
| Java/PHP 反序列化 | `web/漏洞分类/反序列化/反序列化测试（deserialization）.md` | `web/实战打法/rce/12-RCE — 通用反序列化 payload 库.md` |
| XXE（有回显/OOB） | `web/漏洞分类/XXE/XXE（XML 外部实体注入）全手法.md` | `web/实战打法/rce/15-RCE — XXE 实体注入 payload 库.md`、`web/漏洞分类/XXE/XXE（XML 外部实体注入）全手法.md` |
| JNDI / Log4j | `web/漏洞分类/JNDI注入/JNDI 注入测试.md` | — |
| EL / SpEL / OGNL 表达式 | `web/漏洞分类/表达式注入/EL 表达式注入测试.md` | — |
| XSLT 注入 | `web/漏洞分类/XSLT注入/xslt-injection-test（几乎不交）.md` | — |
| 文件包含 / LFI / RFI | `web/漏洞分类/路径穿越/路径穿越、LFI 测试.md` | `web/实战打法/path-traversal/10-LFI 基础、目录遍历技术 payload 库.md`、`web/实战打法/path-traversal/11-RFI、日志投毒 LFI payload 库.md` |
| PHP 伪协议 / phar / session | — | `web/实战打法/path-traversal/12-PHP 伪协议链(Filter、Input、Data、Zip、Wrapper)payload 库.md`、`web/实战打法/path-traversal/13-Phar、Session、Proc 高级 LFI payload 库.md` |
| 原型污染（Node） | `web/漏洞分类/原型链污染/原型链污染测试.md` | `web/实战打法/rce/17-RCE — 原型链污染 payload 库.md` |
| 文件上传 RCE 链 | `web/漏洞分类/文件上传/文件上传漏洞测试手册.md` | `web/实战打法/file-upload/00-文件上传(任意写入、Webshell)— 决策索引.md`（上游 外网打点入口 路由） |
| 无回显验证 | `web/漏洞分类/DNS外带/DNSLog OOB 外带回连（默认方法 · 不询问）.md` | — |

## 纪律

- 先判注入点上下文（回显/报错/盲/OOB）再选手册；盲注先配 dnslog。
- PHP 弱类型与 %00 截断见 `web/漏洞分类/类型混淆/PHP 类型混淆（松散比较）全手法.md`（认证绕过面归 Web认证会话手法）。
- RCE 链组成（上传→包含→执行）先画链路再动手，逐环落证据。
