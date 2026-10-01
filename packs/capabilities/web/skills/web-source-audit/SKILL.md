---
name: web-source-audit
description: Web 源码白盒审计技能：拿到源码/可读代码时的审计入口路由（覆盖率矩阵/污点流取证/控制缺失），证据锚定+终局结构化
keywords: 源码审计, 代码审计, 白盒, 源码, 审计, 静态分析, 污点, 污染流, 数据流, source, sink, taint, 覆盖率, 越权, 认证绕过, 反序列化, 供应链, 0day, audit
features: has_source_code
task_types: analyze, verify
---

# web-source-audit —— 源码白盒审计入口

> 分层纪律：本技能只做**入口路由 + 纪律**。方法论 = 下面的白盒手册（`kb_open(module=…)`
> 单篇直开），具体漏洞类型的 payload 与绕过在其专属手册（目录名即类型：idor/sqli/ssrf/…）。
> **按需 kb_open，禁止通读。**
>
> 与黑盒技能的分工：`web-strike-entry` 管**在线探测**（目标只有 URL）；本技能管
> **有源码时**的静态审计（source→sink 可达性）。两者结论互补，白盒定位的漏洞
> 仍按黑盒技能第 4 节「stable 复现」纪律落 verified。

## 0. 先判形态（第一动作）

| 形态 | 判定 | 主路径 |
|---|---|---|
| **有源码** | 仓库/代码包/解压产物可读 | 本技能：静态审计 |
| **仅固件/二进制** | 只有可执行体 | 转 `binary-rev`/`file-triage`（反编译后按本技能读伪码） |
| **仅在线目标** | 只有 URL | 转 `web-strike-entry`（黑盒探测） |

## 1. 审计目标 → 手册路由

进审计前先识别问题类型，按 `kb_open(module=…)` 打开对应方法论手册：

| 审计诉求 | 方法论手册（`kb_open` 单篇） |
|---|---|
| 全量系统审计、不知从哪查、"查完没" | `web/webapp/source-audit/手册.md`（覆盖率矩阵 D1-D10 + 三策略） |
| 单点数据流：某参数能否到危险函数 | `web/webapp/taint-analysis/手册.md`（source→sink 追踪 + 报告模板） |
| 接口缺鉴权、越权、认证绕过、CRUD 权限不一致 | `web/webapp/control-gap/手册.md`（端点-权限矩阵） |
| 具体漏洞类型的可测手法与 payload | 该类型专属手册：`web/webapp/idor/手册.md`、`web/webapp/sqli/手册.md`、`web/webapp/ssrf/手册.md`、`web/webapp/deserialization/手册.md` 等 |
| 威胁建模/侦察 SOP | `web/playbooks/methodology/00-index.md` |

## 2. 审计纪律（硬要求）

- **证据锚定**：报告里的 `file_path` 必须是你**实际 Read 过**的文件；引用代码行必须真实，
  **知识库示例不是目标项目代码**。宁可漏报，不可误报。
- **终局结构化**：结论落黑白板（findings/func_kb）须带 位置(file:line) / source→sink 链 /
  PoC 或触发条件；**发现 1 个漏洞 ≠ 审计结束**——收尾前逐条自问"还有哪些攻击面没碰"，
  未覆盖维度显式标注。
- **部署模式感知**：不同 profile（application-*.yml / Dockerfile 变体）可启用或禁用关键
  过滤器，端点须按 profile 分别判定。
- **静态结论默认 unverified**；动态验证须在容器内、授权范围内执行。
- 重复模式**归并上报**（1 条发现 + 受影响文件清单），不逐文件搬运。

## 3. 产出落点（黑板联动）

- 发现 → findings（无证据 = unverified）；PoC/复现脚本 → artifacts(kind=poc)；
- 已审函数/位置 → func_kb（research 轨纪律）；与既有条目**先查重再写**。
