---
name: recon-asset-enum
description: 被动侦察与资产枚举方法论：指纹识别、入口面枚举、技术栈判断，产出结构化资产清单
keywords: 侦察, 子域, 指纹, 资产, 信息收集, 端口, 目录, 入口, 枚举, recon, fingerprint, subdomain, 存活探测, 测绘, 信息泄露, 技术栈, 后台
features: returns_401, waf_detected
task_types: recon, asset-enum
mode: self-contained
---

# recon-asset-enum —— 侦察与资产枚举

> 分工边界：本技能只做**侦察与枚举**。发现可疑入口落 findings(unverified) 后即止，
> **不做利用验证**——验证任务派生给 exploit 类角色（external-entry）。
> 方法论细节在 src-strike 知识快照，用 `skill_open(path=…)` 按路径开单篇，
> 禁止通读；知识源不可用时本技能正文自足（§0/§1 已含最短纪律），不停摆。

## 0. 噪声纪律先行（recon 的第一美德）

被动阶梯（按序升级，能低不高）：

1. **零请求**：任务描述 / 黑板已有资产 / 授权边界里读线索
2. **被动观察**：单次 GET 的响应头（Server/X-Powered-By/Cookie 名）、HTML 指纹
   （生成器、特征路径、静态资源名）、robots.txt / sitemap.xml
3. **低噪声主动**：单请求存活确认、小字典目录/入口探测（几十条封顶）
4. **禁止**：全量端口扫、大字典爆破、nuclei 全模板跑——那是把噪声当进度
   （反空转红线，见 web-strike-entry §2）

## 1. 双模式在 recon 的含义

| 模式 | 侦察打法 |
|---|---|
| **锁面** | 只枚举给定 host/URL 清单；出 scope 的链接记录但不跟进（硬闸） |
| **自由跳** | 从种子出发落资产清单，新资产入队等派生，不私自深跳 |

### 1.1 深挖细则（src-strike 侦察方法论，按需 Read，禁通读）

- **锁面/自由跳判定与节奏**（一种子闭环、测绘节奏、种子队列）：
  `skill_open(path="references/web/playbooks/rules/dig-scope-workflow.md")`（68K 大文件，
  只看需要的节，禁通读）。
- **挖什么/类型矩阵**：`skill_open(path="references/web/playbooks/rules/src-value-hunting.md")`。
- **测绘语法备忘/侦察方法论**：
  `skill_open(path="references/web/recon/methodology/手册.md")`。
- 知识源引用的是包内快照路径；模块不存在时 skill_open 会回可选清单，照清单改选。

## 2. 产出落点（黑板联动）

- **每条可达入口 → `bb_add_asset`**：type 可省略（平台按值自动识别：url/IP/
  host:port/完整域名/64hex，识别不出会拒收回填）；**登记前先
  `bb_query what=assets` 查重**（同值/同 IP/同域名都算重复——回执带
  「命中既有资产」提示时停止对同一目标重复登记、重复扫描，E6 防重扫）；
  **每条带 `meta.source`**（观察途径，如 "首页导航链接"/"robots.txt"）——
  Orchestrator 派生任务时要引用来源。
  **平台归属 → `meta.owner`**：`.edu.cn` 系打 `edusrc`；资产明确标注品牌/平台时打
  对应 tag（如 `ysrc`/`osrc`）；无归属不打——命中的平台规则（收录标准/测试边界）
  会注入后续所有会话，侦察阶段同样受其无害化约束。
- **资产树纪律**：解析出 IP 时先 `bb_add_asset type=host value=<IP>` 拿资产 id，
  该 IP 上的域名（domain）、服务（service）、URL（url）一律带
  `parent_id=<host 资产 id>` 挂载——WebUI 资产页按 host 折叠展示，
  多域名指向同一 IP 时只显示该 IP 一行，展开看子资产。
  url/service 值里带 IP、domain（平台自动 DNS 解析）都会**自动挂载**；
  同 IP 多域名自动标主域名/别名（AI 扫描目标粒度按 host+主域名，勿对别名重复扫）。
- **展示 meta**：URL 落资产时 `meta.title` 带页面 `<title>` 原文（资产页第二行展示）。
- **扫描/测试状态机（E7，访问≠测试）**：入口访问过 →
  `bb_asset_status(asset_id, "visited")`；开始扫描 → `"scanning"`（并发会话经
  `bb_query what=assets` 按状态过滤可感知哪些目标正被扫）；**测完且无发现才许
  `"tested_clean"`——必附 note**（测了什么/怎么测，服务端强制，缺 note 拒收）；
  测出问题直接 `bb_add_finding`，不要标 tested_clean（verified 发现由平台反查显
  「有发现」徽章，结论以 findings 为准）。`meta.scanned` 已废弃，勿再写。
- **可疑入口 → findings(unverified)**：未授权管理面、非常规参数、疑似注入点、
  异常报错带栈信息。只记录现象与 URL，**不带验证 payload 的执行结果**。
  落 finding 时带 `target_asset_id`（该入口的资产 id，先 bb_add_asset 拿 id）——
  findings 页按资产筛选依赖这个字段，不挂 = 筛不出来。
- 指纹结论（平台/语言/中间件/框架及版本线索）写进对应资产的 meta，
  不单开 finding（信息 ≠ 漏洞）。

## 3. 何时收工

- 资产清单覆盖任务 scope 内全部已知入口，且每条有 meta.source；
- 或者继续枚举的边际收益明显为负（连续 N 条重复/404）。
  收尾在 complete_task 的 result_note 里给出"下一步建议"（如：入口 X 疑似注入，
  建议派 exploit 任务），供 Orchestrator 派生参考。
