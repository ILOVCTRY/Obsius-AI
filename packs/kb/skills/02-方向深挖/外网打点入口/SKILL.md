---
name: 外网打点入口
description: Web/SRC 渗透入口技能：双模式判定（锁面/自由跳）+ 目标特征路由对照表
keywords: 渗透, src, 挖洞, 测试, 漏洞, 目标, 网站, 外网, 打点, 入口, 利用, 上传, 越权, idor, 未授权, 登录, 弱口令, 验证码, 支付, 竞态, 逻辑漏洞, jwt, oauth, graphql, websocket, waf, 401, 403
features: has_user_system, has_upload, has_search, has_pay, has_graphql, has_oauth, has_websocket, returns_401, waf_detected
task_types: exploit, privesc, lateral-movement
mode: self-contained
---

# 外网打点入口 —— 渗透入口：双模式判定 + 特征路由

> 分层纪律：本技能只做**入口路由**。方法论 = webapp 测试包手册
> （下表 module 直开单篇），弹药在 `web/实战打法/`。**按需 kb_open
> 单篇，禁止通读。**

## 0. 双模式先判（必做第一动作）

| 模式 | 判定 | 打法 |
|---|---|---|
| **自由跳** | 模糊目标：只给集团/品牌名，没有 URL 清单 | 被动侦察落种子队列 → 一种子闭环 |
| **锁面** | 固定站 / URL 清单 / 众测 program | 锁定给定字符串，出 scope 硬闸 |

## 1. 目标特征 → 模块路由对照表

进站先识别特征，按下表用 **`kb_open(module=…)`** 打开对应手册（module 为
kb 域内路径；不存在时服务端回可选清单，照清单改选，禁止猜名）。
打面家族分工：注入类专精 `Web注入手法`、认证会话专精 `Web认证会话手法`、
API 面 `Web接口攻击手法`、客户端面 `Web客户端手法`（均已挂载，按 query 择优注入）。

| 目标特征（features） | 方法论手册（`kb_open` 单篇） | 弹药（`web/实战打法/` 下） |
|---|---|---|
| has_user_system（有注册/登录） | `web/漏洞分类/越权/越权漏洞（IDOR-权限绕过）.md` + `web/漏洞分类/认证绕过/认证绕过（登录-会话-密码重置-2FA）.md`（验证码见 `web/漏洞分类/验证码识别/图形验证码 OCR 破解（ddddocr）.md`，弱比较见 `web/漏洞分类/类型混淆/类型混淆（Type Juggling）测试.md`） | `web/实战打法/arbitrary-x-authz.md` |
| has_search / has_filter（查询/列表过滤） | `web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）.md` | `web/实战打法/sqli.md` |
| has_upload（文件上传/导入/附件） | `web/漏洞分类/文件上传/文件上传漏洞.md` | `web/实战打法/file-upload/00-index.md` |
| has_pay（支付/优惠券/积分） | `web/漏洞分类/业务逻辑/逻辑漏洞.md` + `web/漏洞分类/条件竞争/竞态条件.md` | `web/实战打法/logic-flaws/00-index.md` |
| has_graphql | `web/漏洞分类/GraphQL/GraphQL 安全.md` | `web/实战打法/graphql.md` |
| has_oauth / JWT / SAML | `web/漏洞分类/JWT/OAuth-JWT-SAML 安全.md` | `web/实战打法/oauth-saml-jwt/00-index.md` |
| has_websocket | `web/漏洞分类/WebSocket/WebSocket 安全.md` | `web/实战打法/api-rest/13-websocket.md` |
| returns_401/403 | `web/漏洞分类/401与403绕过/401-403 状态码绕过测试.md`（分清登录页 vs 业务 API，path/METHOD/头现场改） | — |
| waf_detected | `web/漏洞分类/WAF绕过/WAF 绕过测试.md`（被拦再开，禁开场丢探针） | `web/实战打法/methodology/02-bypass-toolkit.md` |
| 参数回显点（搜索词/昵称/UA 等进 HTML/属性/JS） | `web/漏洞分类/XSS/XSS测试.md`（CSP 拦了再开 `web/漏洞分类/CSP绕过/CSP 绕过测试.md`） | `web/实战打法/xss/00-index.md` |
| 跳转参数（redirect/url/next/goto） | `web/漏洞分类/开放重定向/开放重定向测试.md` | — |
| 请求出网点（webhook/URL 导入/外链头像/PDF 渲染） | `web/漏洞分类/SSRF/SSRF测试.md` | `web/实战打法/ssrf-cache-host/00-index.md` |
| 文件下载/预览/包含点（download/file/path 参数） | `web/漏洞分类/路径穿越/路径穿越-LFI.md` | `web/实战打法/path-traversal/00-index.md` |
| XML/SOAP 接口（xml 请求体/wsdl） | `web/漏洞分类/XXE/XXE测试.md` | `web/实战打法/rce/15-xxe.md` |
| Java 栈特征（jsp/struts/序列化串/JNDI 报错） | `web/漏洞分类/反序列化/反序列化测试.md` + `web/漏洞分类/JNDI注入/JNDI 注入测试.md` | `web/实战打法/rce/12-deserialization.md` |
| 模板/表达式可配置点（自定义模板/邮件签名） | `web/漏洞分类/表达式注入/EL 表达式注入测试.md`（SSTI 弹药见下格） | `web/实战打法/rce/14-ssti.md` |
| 报错栈/.git/.env/备份/SCM 路径 | `web/漏洞分类/信息泄露/信息泄露测试.md` + `web/漏洞分类/SCM配置泄露/不安全的 SCM 配置泄露测试.md` | `web/实战打法/info-disclosure.md` |
| 前端加密参数/厚 JS（sign/token 前端生成） | `web/漏洞分类/JS逆向/JS 逆向配合接口挖掘指南.md` | — |
| 邮件功能（注册/找回密码/邀请） | `web/漏洞分类/邮件头注入/邮件头注入测试.md` + `web/漏洞分类/Host头注入/Host 头注入与密码重置毒化.md`（重置投毒） | — |
| 缓存层特征（CDN/X-Cache/Age 头） | `web/漏洞分类/缓存投毒/缓存投毒测试.md` | `web/实战打法/ssrf-cache-host/12-cache.md` |
| 导出报表（CSV/Excel） | `web/漏洞分类/CSV公式注入/CSV 公式注入测试.md` | — |
| AI 功能面（对话窗/文档问答/Agent 工具） | `web/漏洞分类/大模型安全/LLM 安全测试（禁开越狱教材）.md` | — |
| swagger/kong/actuator 暴露 | `web/漏洞分类/API网关/API 网关安全.md` | — |
| API 面厚（REST 批量接口/多端点） | 转交 `Web接口攻击手法`（走私/Host 头/HPP/邮件头路由表） | `web/实战打法/api-rest/00-index.md` |

### 补充特征（易漏，逐项过一遍）

上面按「目标特征」路由，下面这批按「**容易被忘掉但必须测**」列出——不是所有站点都会暴露这些特征，
但打点阶段应该主动探测它们存不存在，而不是等特征出现。

| 特征 / 探测点 | 模块 | 说明 |
|---|---|---|
| 反向代理/负载均衡/CDN 后面 | `web/漏洞分类/HTTP请求走私/HTTP 请求走私测试.md` | CL/TE 混淆、TE 头变体；前后端解析不一致才成立 |
| 支持 HTTP/2 | `web/漏洞分类/HTTP2攻击/HTTP2 攻击测试.md` | H2.CL/H2.TE 降级走私、伪头注入 |
| 有状态变更操作（改密/转账/领券/下单） | `web/漏洞分类/跨站请求伪造/CSRF.md` | 缺 token / token 可预测 / SameSite 未设 |
| 跨域资源共享（`Access-Control-*` 头） | `web/漏洞分类/跨域配置错误/CORS 技术资料（SRC 禁用）.md` | 反射 Origin、`null` 源、子域信任过宽 |
| 前端框架/富交互（React/Vue + 用户输入进对象） | `web/漏洞分类/原型链污染/原型链污染（Prototype Pollution）测试.md` | 原型链污染 → 常与 XSS/RCE 链式组合 |
| 参数重复提交/数组参数 | `web/漏洞分类/HTTP参数污染/HTTP 参数污染（HPP）测试.md` | 前后端取第一个/最后一个不一致 → 绕过校验 |
| 用户输入进响应头（Location/Set-Cookie/自定义头） | `web/漏洞分类/CRLF注入/CRLF 注入测试.md` | CRLF 注入 → 响应拆分 |
| 富文本/HTML 白名单过滤 | `web/漏洞分类/悬垂标记注入/悬空标记（Dangling Markup）测试.md` | 无脚本也能外带数据 |
| 可嵌入页面（无 X-Frame-Options） | `web/漏洞分类/点击劫持/点击劫持测试.md` | 点击劫持；常与 CSRF 组合 |
| XSLT/XML 转换点 | `web/漏洞分类/XSLT注入/XSLT 注入测试.md` | 与 XXE 不同：这是 XSLT 引擎的代码执行 |
| 子域枚举（CNAME 指向第三方） | `web/漏洞分类/子域接管/子域接管测试.md` | 悬空 CNAME → 接管子域 |
| 依赖安装/包名可控（内网 registry、包名混淆） | `web/漏洞分类/依赖混淆/依赖混淆测试.md` | 依赖混淆投毒 |
| 出网受限但可发 DNS | `web/漏洞分类/DNS外带/DNSLog OOB 外带回连.md` | OOB 外带通道（盲注/XXE/SSRF 无回显时） |
| DNS 重绑定可用 | `web/漏洞分类/DNS重绑定/DNS 重绑定测试.md` | 绕过 SSRF 的 IP 白名单 |
| 认证体系本身（登录/会话/多因素） | `web/漏洞分类/认证与会话/` 三篇：`auth-and-access.md` / `auth-jwt.md` / `auth-infra.md` | 不止"绕过登录"，还包括会话固定、JWT 算法混淆、认证基础设施 |
| 通用注入面（非 SQL 的注入） | `web/漏洞分类/注入类/` 多篇：`server-side.md` / `server-side-exec.md` / `server-side-advanced.md` / `server-side-deser.md` | 命令/SSTI/表达式/反序列化总入口 |
| AI Agent 工具调用 | `web/漏洞分类/AI代理工具执行/对话口工具真执行（agent-tool-exec）.md` | 工具参数注入、越权调用 |
| 云 IDE / 在线开发环境 | `web/漏洞分类/云IDE远程执行链/云 IDE-AI 编程平台弱口令→RCE→凭证链.md` | 云 IDE 到 RCE 的链路 |
| 权限模型存在缺口（角色切换/审批流） | `web/漏洞分类/控制缺失审计/控制缺失型审计（端点-权限矩阵与认证旁路）.md` | 控制措施缺失类问题 |
| 有源码/可读代码 | `web/漏洞分类/源码审计/源码白盒审计方法论（覆盖率矩阵与三策略）.md` + `web/漏洞分类/污点分析/污点分析与数据流追踪（source→sink）.md` | 白盒审计入口，污点分析 |

**注**：`seatable` / `期刊采编-e-tiller` / `ghost-bits-cast` 是特定产品或特定漏洞的专题，
不属于通用打点特征，遇到对应产品时直接 `kb_open` 该目录即可，不列进上表。

**纵深转交**：SSRF 打通内网 / RCE 拿下落点后需要推进时——redteam 轨转
`Web后渗透`（落点后阶段路由）；pentest 轨到发现即止（owners 红线禁内网
渗透/主机提权），证据齐即上报。

**云入口**：metadata SSRF / AK-SK 泄露 / 对象存储公开读——caps 含 cloud
时直开 `cloud/` 域 kb 单篇验证；否则落 findings(unverified) 上报，勿盲打。

**平台归属 → `meta.owner`**：登记/更新资产时按特征打标（`.edu.cn` 系 → `edusrc`；
萤石 → `ysrc`；OPPO/realme/OnePlus → `osrc`；无归属不打）——命中的平台规则
（收录标准/验证边界）会注入会话，**测前先看自己有没有踩平台红线**。

## 2. 反空转规则

- 打开是登录页：先找业务面（网关/跳转后的 host），主业挖未登录；
  看见登录页 ≠ 换资产；人机验证合理几轮即停，不做 OCR/轨迹模拟。
- nuclei 只是辅助（已知 CVE/暴露面），不是主路径，禁止全量模板扫当进度。
- **同 IP/同域名查重先行（E6 防重扫）**：对任何目标动手前先
  `bb_query what=assets`（可按 type/status 过滤）查既有资产——目标已被登记、
  正被扫（scanning）或已访问/已测（visited/tested_clean）时不要重复登记、
  不要重跑相同探测；`bb_add_asset` 回执带「命中既有资产」提示 = 已重复，
  立即停手改道。
- 中危同一对象先升链；肥面深挖、瘦壳一眼；禁偏科（连日只堆未授权读）。

## 3. 产出落点（黑板联动）

- 发现 → findings（无证据 = unverified）；POC → artifacts（kind=poc）。
  跨资产指纹的洞可参考快照沉淀规范（kb_open
  `web/PoC与模板/poc/README.md`），但**只读参考**——POC 一律落黑板 artifact，
  不写进 技能目录 references/目录；
- 越界/发现他人资产有价值线索 → 发现即上报协议（DESIGN.md §6.3），不私自跨目标。

## 4. verified 前必须稳定复现（硬纪律）

**verified 的唯一标准 = 稳定触发**：HTTP 报文**连续 3 次请求全部触发**才允许标 verified。
3 次里有失败 → 老实留 unverified 或走脚本路线，禁止一次成功就宣称 verified。

两种 POC 形态（按稳定性选，能报文不脚本）：

1. **报文能稳触** → `evidence.poc = {type: "http_raw", http_raw: "<最小报文>",
   target: "<url>", stability: "3/3"}`——http_raw 存**可复现的最小报文**：
   请求行 + 触发必需的头/体（如注入参数、认证 cookie），去掉浏览器噪音头
   （sec-ch-ua / Accept-Language 之类非触发必需的一律删）——最小化才能一眼
   看出触发点，也才是可复现的判据；
2. **报文不稳**（需多步编排/时序竞争/参数生成/会话链）→
   `bb_add_artifact(filename, content, kind="poc")` 落**自包含 Python 脚本**
   （**仅限 .py**，工具层拒绝其他语言；不依赖会话现场状态，拿到就能跑）→
   `evidence.poc = {type: "python", artifact_id: "<bb_add_artifact 返回的 id>",
   stability: "3/3"}`，并 `bb_add_finding` 带 `poc_artifact_id` 关联。

- finding 尽量挂 `target_asset_id`（URL 先 bb_add_asset 拿 id）——按资产筛选依赖它；
- **同一发现允许多条 POC**（不同触发路径 / 不同参数位各一条报文）：
  `evidence.pocs = [POC1, POC2, …]` 数组，每条独立 stability 与 name
  （如 "GET 探测"/"POST 利用"）；
- 已有 unverified 发现验证通过后，**重报同一 finding 带 status=verified + evidence.poc**
  （黑板按指纹合并，不会插重复行）。
- **升链/强关联必须带 `relates_to`**：新发现由旧发现升级、利用或证伪而来时
  `bb_add_finding(relates_to=[{finding_id, note}])`（note 一句话写理由，如
  「同一 id 参数，UNION 注入升级到 DBA」），攻击链画布据此画强边；被引发现
必须已登记在本项目，弱相关不填（画布按同类/同 IP 自动推弱边）。
