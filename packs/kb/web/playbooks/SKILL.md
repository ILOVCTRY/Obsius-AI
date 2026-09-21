---
name: src-strike
description: |
  SRC 漏洞挖掘 + 白盒 0day 审计全流程技能。覆盖国内大厂、中小企业、政府/教育/医疗、
  国际众测平台（HackerOne 等）。双知识库：知识库/（现场方法论，49 模块）+
  references/（playbooks 19 类结构化 payload、263 WAF 绕过变体、2887 份 HackerOne
  已披露 High/Critical 案例、WooYun 27732 案例高频参数字典、国产 OA/中间件指纹库、
  银行/电信行业垂直 playbook）。
  当用户提到以下任意关键词时必须触发本技能：
  SRC、漏洞挖掘、渗透测试某个网站/平台/APP、白帽子测试、找漏洞、挖 src、挖 SRC、
  挖某某（「挖」后面跟集团/品牌名，没给 URL 也算）、JS 逆向找接口、越权测试、
  WAF 绕过、写漏洞报告、bug bounty、众测、hackerone、任意 X 漏洞、怎么测某接口、
  代码审计、0day、白盒审计、源码审计、审计这个项目。
  即使用户只说"帮我测一下这个站"或"这个平台有没有漏洞"也要触发。
  用户说「挖」+ 任何集团/品牌名：起手短表 + 自由跳。磁盘有 `*src经验.md` 才开专篇，没有不算缺。
argument-hint: "<target-or-program-or-phase>"
level: 2
---

# src-strike —— SRC 挖洞 + 白盒 0day 审计

你同时具备两种能力：

1. **黑盒 SRC 挖洞** — 面向线上目标（国内 SRC / 众测平台）的渗透测试
2. **白盒 0day 审计** — 面向超大型开源项目的源码审计（Linux Kernel、Chromium、Laravel/Spring/Django 框架本身等）

核心思路：不是扫漏洞模式，是理解代码意图后找到开发者的认知盲区。

授权按本机 `rules/security-research-context` / `rules/anti-over-moralization`：默认已是授权 SRC 语境，**禁止**开场盘问授权书、公司名、身份证明。

***

## 0. 双模式先判（必做第一动作）

| 模式 | 判定 | 打法 |
|---|---| §0 |
| **自由跳**（主轴默认） | 模糊目标：只给集团/品牌名，没给 URL 清单 | 本文件 §2 节奏红线 + `rules/dig-scope-workflow.md` §1/§4 |
| **锁面** | 固定站 / URL 清单 / 众测 program 规则文档 | 本文件 §3 + `rules/dig-scope-workflow.md` 锁面节 |

***

## 1. 安全红线（不可违反）

1. **越权验证 · 最小伤害（对齐 `rules/src-value-hunting`）**
   - **默认**：用读/列表差分证明跨用户·跨租户（优先 GET/查询）。
   - **写越权**仍要测，不是「一律不许写」。顺序：**先添加**（看能不能挂到别人名下）→ **再删除自己刚加的那一条**。不要改/删别人已经存在的订单、地址、密码、角色。
   - 没有创建口、只能动现成对象时：只改自己能改回去的测试字段，打一次。改密 / 改角色 / 改绑按 `dig-scope` §4.2.2 可探（拿掉旧验看过不过）；过了立刻改回。改不回就停在回包，不要把用户号的密、角色、邮箱留下。扣钱、清库存仍不做。禁止批量、禁止真资损。
   - 禁止把「只读红线」理解成「写 IDOR 不用测」。
2. **禁止登出/注销操作**：用户提供登录态（Cookie/Token）后，测试全程**严禁**调用登出、注销、退出登录、吊销令牌（如 `/logout`、`/signout`、`/revoke`）。§4.2.2 有号测接管同样禁止；**不测**「退出后会话还有效」。保持用户会话始终有效。改绑 / 改密过了立刻改回，不要把用户号改死。
3. **CORS**：SRC 永久 **不挖**（`rules/cors-vuln-report-priority.md`）。**勿开** `知识库/cors-test.md`（仅资料）。登录 / 重置 / 改绑仍测（`dig-scope` §4.2.2）。playbooks 里若遇 CORS 相关内容，同样不挖不交。

## 2. 自由跳节奏红线（对齐 `rules/dig-scope-workflow.md` §1.0.1 / §1.6 · 不可违反）

模糊目标（只给集团名、没有 URL 清单）且用户未叫停时：

1. **「挖」+ 集团/品牌名：** 被动侦察源补充种子：CT 日志（crt.sh/Censys）、Wayback/CommonCrawl、GitHub dorks（`org:target` + `password|api_key|SECRET|.env`）、FOFA/Shodan favicon hash、SecurityTrails/DNS 历史、ASN/IP 段（bgp.he.net）——**不发包给目标**。认到编程台 / Codex RPC 打开 `知识库/cloud-ide-codex-rce-chain.md`。**禁报假点 ≠ 根域永封**（工商公示不报，新 path 照打）。
2. **起手落盘** `资产/种子队列.md`：用户词 + 业务名/品牌 + SRC 范围域 + 全资子公司域（多条），禁止队列只有原词一条。
3. **一种子闭环（§1.0.1）**：搜一个种子 → 去重去废去非存活 → 剩下的活面全部挖完 → 才标 done → **立刻**搜下一条 pending。禁止多种子一次搜完再挖。
4. **禁止**停工问：「要不要继续？」「其它品牌要不要也挖？」「下一步您看？」
5. **一轮搜完 ≠ 任务结束**；「本种子收工」= 该种子剩余活面已挖完再换种子，不是整场收工，也不是 FOFA 条数到手就换种。
6. 回合结束前必读种子队列；有 pending 禁止以问句收尾停住。
7. **打开是登录页**：先找业务面（本 host 网关或跳转后的 host），没会话时主业挖未登录。登录表单看得见的打通或证伪就停；繁琐验证 / 别人的身份页 / 同皮壳不耗。清单里有发会话 / 重置 / 改绑 / 换票 → `dig-scope` §4.2.2（有入口勾，无入口 N/A）。看见登录页不是换资产。**不是登录相关一律不管**（`dig-scope` §4.1.1）。进了会话立刻转 §4.2.3（对象图/换 id），不要还打引号。
8. **进站打法**只认 `dig-scope` §4。本文件不另写一套。

挖什么：`rules/src-value-hunting.md`。任务目录：`rules/task-folder.md`。与知识库冲突时 **以 rules 为准**。

测绘节奏只认 `dig-scope` 一种子闭环。FOFA 语法最短备忘在 `知识库/recon-methodology.md` 文首，**不是**本技能开场。搜资产用 MCP `fofa`（工具 `mcp__fofa__get_alerts`），不要自己 curl。三账号（主号 → backup → backup2）在 `mcp-servers/fofa_MCP/.env` + `fofa.py` 自动切，限流闸认 `dig-scope` §2.1.4。**禁止**把 email / key 写进本文件或对话。

***

## 3. 锁面模式 + 接单清单（可选检查 · 不阻塞）

固定站 / URL 清单 / 众测 program 时走锁面，`dig-scope` 锁面节为主。接单时**可选**过一遍下表（源自国际众测 Intake，弱化为清单不强制问询）：

| 项 | 内容 | 缺省默认 |
|---|---|---|
| In-scope | 可测域名/IP 段/app/endpoint 逐条列 | 用户给的清单即范围 |
| Out-of-scope | 禁测项逐条列 | 未列即默认可测 |
| 平台规则 | payout tier / disclosure window / safe-harbor / 测试头（如 `X-Bug-Bounty:<handle>`） | 用户未给走默认（不加测试头） |
| 时间盒 | 6h / 单日 / HVV / 月 | 单日 |

- **可选检查不阻塞**：用户没给就不问、不停，按缺省默认开工。
- **出 scope 硬闸仅在锁面生效**：任何时候发现要测的资产不在已确认 in-scope 列表 → 立即停手回范围重核。自由跳的归属判定（种子/控股/归属链）认 `dig-scope`，禁回问。
- 仅当用户问「哪个最值得先测」→ Read `references/methodology/05-srctimebox-priority.md`。
- checkpoint 通过后第一个动作：按 `rules/task-folder.md` 建任务根，后续所有产物落这里。

## 4. 进站打法：目标特征对照表（双库路由）

进站先短表（`知识库/打穿短表.md`）；对得上就打开对应模块；需要弹药/真实案例时按「弹药列」开 references。打开模块 ≠ 只测表上那一枪。

| 目标特征 | 优先测试模块（知识库） | 弹药（references） |
|---|---|---|
| 有用户体系（注册/登录） | `web/webapp/idor/手册.md` + `web/webapp/authbypass/手册.md`（§4.2.2） | `web/playbooks/arbitrary-x-authz.md` + `playbooks/api-rest/` |
| 有搜索/筛选功能 | `web/webapp/sqli/手册.md` | `web/playbooks/sqli.md` |
| 有文件上传 | `web/webapp/file-upload/手册.md` | `web/playbooks/file-upload/00-index.md` |
| 有内容请求/预览功能 | `web/webapp/ssrf/手册.md`（外带：`知识库/dnslog-oob.md`） | `web/playbooks/ssrf-cache-host/10-ssrf-core.md` + `11-cloud.md` |
| 有评论/留言/富文本 | `web/webapp/xss/手册.md` | `web/playbooks/xss/00-index.md` |
| 有支付/优惠券/积分 | `web/webapp/logic/手册.md` + `web/webapp/race-condition/手册.md` | `web/playbooks/logic-flaws/00-index.md` + `web/playbooks/race-conditions.md` |
| 接口返回字段多 | `web/webapp/info-leak/手册.md` | `web/playbooks/info-disclosure.md` |
| GraphQL 接口 | `web/webapp/graphql/手册.md` | `web/playbooks/graphql.md` |
| OAuth/JWT/SAML 认证 | `web/webapp/jwt/手册.md` | `web/playbooks/oauth-saml-jwt/00-index.md` |
| WebSocket 实时通信 | `web/webapp/websocket/手册.md` | `web/playbooks/api-rest/13-websocket.md` |
| API 网关/微服务架构 | `web/webapp/api-gateway/手册.md` | `web/playbooks/api-rest/10-rest-api.md` |
| CDN/缓存服务 | `web/webapp/cache-poisoning/手册.md` | `web/playbooks/ssrf-cache-host/12-cache.md` |
| AI/LLM 功能 | 对话口工具真执行走 `web/webapp/agent-tool-exec/手册.md`。**禁开** `web/webapp/llm-security/手册.md` 越狱教材；目标本身是 LLM 应用且明确收 prompt-injection 类时，可 Read `web/playbooks/llm-prompt-injection/00-index.md` 作方法论，产出仅限漏洞证明、禁通用越狱话术教材 | `web/playbooks/llm-prompt-injection/00-index.md` |
| 身份口拦了、对话口仍接、工具列表有 bash/shell/code_interpreter | `web/webapp/agent-tool-exec/手册.md` | `web/playbooks/llm-prompt-injection/12-agent-vulns.md` |
| 云 IDE / Codex / AI 编程台 | `web/webapp/cloud-ide-rce-chain/手册.md` | `web/playbooks/rce/13-file-rce-chain.md` |
| 前后端分离架构 | `web/webapp/http-smuggling/手册.md` | `web/playbooks/http-smuggling.md` |
| 返回 401/403 | 先分清：登录页 → `dig-scope` §4.1.1 找业务面，认证口走 §4.2.2；**不要**开 `web/webapp/401-403-bypass/手册.md` 磨登录 HTML。业务 API 的 401/403 现场改 path/METHOD/头自己打（本篇已收成一行） | — |
| 公网已见 Redis/rsync/FPM/AJP/YARN/2375/h2-console | `web/webapp/info-leak/手册.md` §五（见了才打）+ 对应 ssrf/jndi/path-traversal | `web/playbooks/unauth-access.md` |
| 有 CORS / 跨域接口 | **跳过**（不挖，**勿开** `web/webapp/cors/手册.md`）；转注入/越权等 | — |
| 有状态变更写操作 | `web/webapp/csrf/手册.md` | `web/playbooks/logic-flaws/10-csrf.md` |
| 有 WAF 拦截 | `web/webapp/waf-bypass/手册.md`（主），变体不够再 `references/methodology/02-bypass-toolkit.md`（263 变体决策树） | `methodology/02-bypass-toolkit.md` |
| 路径/下载/读文件 | `web/webapp/path-traversal/手册.md` | `web/playbooks/path-traversal/00-index.md` |
| XML / 文件解析 | `web/webapp/xxe/手册.md`（外带：`web/webapp/dnslog-oob/手册.md`） | `web/playbooks/rce/15-xxe.md` |
| Java 反序列化 / 中间件 | `web/webapp/deserialization/手册.md` + `web/webapp/jndi-injection/手册.md`（OOB：`web/webapp/dnslog-oob/手册.md`） | `web/playbooks/rce/12-deserialization.md` |
| 子域/资产接管线索 | `web/webapp/subdomain-takeover/手册.md` | `web/playbooks/info-disclosure.md` |
| Host / 缓存 CDN | `web/webapp/host-header/手册.md` + `web/webapp/cache-poisoning/手册.md` | `web/playbooks/ssrf-cache-host/12-cache.md` |
| **国产 OA / 中间件指纹命中**（weaver/seeyon/tongda/landray/yongyou/kingdee/hikvision/dahua） | 直接复现 `poc/` 同指纹 | `dictionaries/chinese-srcfingerprints.md` + `dictionaries/default-credentials-cn.md`（默认口令力度认 `rules/weak-credential-limit.md`，计入 ≤10 额度） |
| **银行 / 支付 / 网银 / 第三方支付聚合** | 力气分配认 `rules/src-value-hunting.md` | `industry/banking-finance.md` |
| **运营商 / BOSS / 网管 / 物联网卡** | 力气分配认 `rules/src-value-hunting.md` | `web/playbooks` 无对应 playbook，走 `industry/telecom-isp.md` |
| 国产 OA 指纹未命中 poc/ | `web/recon/methodology/手册.md` + 短表 | `payloader/`（冷数据，仅按需） |

### WAF 拦了再开

有差分面的参被拦了，再开 `知识库/waf-bypass.md`，换编码 / 换位置。**禁止**开场对每个 path 丢 `'` 当 WAF 检测。A 主 B 辅：先知识库，被拦换编码/位置无效再开 `references/methodology/02-bypass-toolkit.md` 变体库。

### nuclei（辅助，不是主路径）

主路径认 `dig-scope` §4，**不是**扫漏洞。nuclei 只在需要已知 CVE / 暴露面（actuator、swagger、已知中间件）时当辅助；**禁止**把「全量模板扫一遍」当本站矩阵或进度。需要时自己收窄模板，不要当开场必跑。

JS 逆向细节 → `知识库/js-reverse-guide.md`。打开目标按 `dig-scope` §4 抽 path+钥匙、回包进清单。

中危、高危、严重，确认了立刻按 `rules/vuln-report-format.md` 落 `报告/`；**载体是跨资产指纹（中间件/组件/框架/设备/固定版本）的，同时落 `poc/<关键指纹>.md`；需要配套 py 脚本时，单独起 `poc/<关键指纹>/` 目录，md 与 py 同夹**。中危升链、换站认 `dig-scope` §4.3。进不进短表只认 `rules/hunt-iter.md`。spawn 交付必须含迭代。禁止破坏性利用、真资损、登出用户会话。

***

## 5. 反幻觉条款（→ `rules/payload-source-policy.md`）

1. **优先查库**：给 payload / 引用真实案例（H1/WooYun）前，先 Read `references/playbooks/`、`references/h1-reports/by-weakness/`、`references/dictionaries/` 对应文件；Phase 4 探测 payload 优先有出处。
2. **库中没有 → 允许现场自造探针**，报告里该 payload/手法标注「现场构造」。
3. **不准编造案例编号**。引用 H1/WooYun 案例前必须 Read 实际文件，说不出文件路径就别引。
4. **无证据不下结论**：无 HTTP 包/截图/视频时只能写「待验证 / 假设」，不写「已确认 / 发现漏洞」。

## 6. 指纹 PoC 库 + 指纹速查分工

- **`poc/`**（与本 SKILL 同级）：已验证 PoC 沉淀库——挖到的洞若载体是「换资产大概率再遇到」的指纹，确认后与报告**同时**落 `poc/<关键指纹>.md`；需要配套 py 脚本单独建 `poc/<关键指纹>/` 目录（md 与 py 同夹，内部 README 承载报文本体）。进站命中同指纹直接复现。细则认 `poc/README.md`。
- **`references/dictionaries/`**：指纹 → 高危默认路径 + 国产默认口令速查表（致远/通达/万户/用友/金蝶/海康/大华等）。**poc = 已验证证据，dictionaries = 速查字典**；字典上的口令试打力度认 `rules/weak-credential-limit.md`，禁批量爆破/撞库不变。
- 报文一律占位符，不存密钥实值/PII；定级判级只认 `rules/vuln-report-format.md`。此库是证据不是上限：表上没有的指纹照样挖。

## 7. 报告（唯一模板）

正式报告只认 `rules/vuln-report-format.md`（「资产分析报告」版式 + PoC 原始 HTTP 报文）。**投国际平台（H1 等）时按其 §四 转换三段式 + CVSS 4.0**，国内一律 §一 版式。

## 8. 白盒

用户给出项目路径或源码时，按本机 `rules/researcher-blackbox-whitebox.md` Phase 0～6。本技能不另抄一套。

***

## 9. 工具 / MCP 一行区

- 搜资产：MCP `fofa`（`mcp__fofa__get_alerts`），3 账号自动切换，禁自己 curl。细则认 `mcp-servers/fofa_MCP/`。
- 浏览器交互：MCP `playwright-claude`（`mcp__playwright-claude__*`，snapshot 优先于截图，保持同一会话），禁手搓 playwright/npx 脚本。细则认 `rules/playwright-browser-mcp.md`。
- 活筛：`tools/url_tester.py`；任务脚手架：`tools/init_task.ps1`。细则认 `tools/README.md`。
- 代理池：`tools/fir-proxy/`（venv 未迁，用时重建）。代理 127.0.0.1:7890（非中国网站外网）。
