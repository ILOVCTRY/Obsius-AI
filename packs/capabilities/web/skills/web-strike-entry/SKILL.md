---
name: web-strike-entry
description: Web/SRC 渗透入口技能：双模式判定（锁面/自由跳）+ 目标特征路由对照表
keywords: 渗透, src, 挖洞, 测试, 漏洞, 目标, 网站, 外网, 打点, 入口, 利用, 上传, 越权, idor, 未授权, 登录, 弱口令, 验证码, 支付, 竞态, 逻辑漏洞, jwt, oauth, graphql, websocket, waf, 401, 403
features: has_user_system, has_upload, has_search, has_pay, has_graphql, has_oauth, has_websocket, returns_401, waf_detected
task_types: exploit, privesc, lateral-movement
mode: self-contained
---

# web-strike-entry —— 渗透入口：双模式判定 + 特征路由

> 分层纪律：本技能只做**入口路由**。方法论 = webapp 测试包手册
> （下表 module 直开单篇），弹药在 `playbooks/`。**按需 skill_open
> 单篇，禁止通读。**

## 0. 双模式先判（必做第一动作）

| 模式 | 判定 | 打法 |
|---|---|---|
| **自由跳** | 模糊目标：只给集团/品牌名，没有 URL 清单 | 被动侦察落种子队列 → 一种子闭环 |
| **锁面** | 固定站 / URL 清单 / 众测 program | 锁定给定字符串，出 scope 硬闸 |

## 1. 目标特征 → 模块路由对照表

进站先识别特征，按下表用 **`skill_open(path=…)`** 打开对应手册（module 为
kb 域内路径；不存在时服务端回可选清单，照清单改选，禁止猜名）。
打面家族分工：注入类专精 `web-injection`、认证会话专精 `web-authn-session`、
API 面 `web-api-attack`、客户端面 `web-client-side`（均已挂载，按 query 择优注入）。

| 目标特征（features） | 方法论手册（`skill_open` 单篇） | 弹药（`playbooks/` 下） |
|---|---|---|
| has_user_system（有注册/登录） | `references/web/webapp/idor/手册.md` + `references/web/webapp/authbypass/手册.md`（验证码见 `captcha-ocr/手册.md`，弱比较见 `type-juggling/手册.md`） | `arbitrary-x-authz.md` |
| has_search / has_filter（查询/列表过滤） | `references/web/webapp/sqli/手册.md` | `sqli.md` |
| has_upload（文件上传/导入/附件） | `references/web/webapp/file-upload/手册.md` | `file-upload/00-index.md` |
| has_pay（支付/优惠券/积分） | `references/web/webapp/logic/手册.md` + `references/web/webapp/race-condition/手册.md` | `logic-flaws/00-index.md` |
| has_graphql | `references/web/webapp/graphql/手册.md` | `graphql.md` |
| has_oauth / JWT / SAML | `references/web/webapp/jwt/手册.md` | `oauth-saml-jwt/00-index.md` |
| has_websocket | `references/web/webapp/websocket/手册.md` | `api-rest/13-websocket.md` |
| returns_401/403 | `references/web/webapp/401-403-bypass/手册.md`（分清登录页 vs 业务 API，path/METHOD/头现场改） | — |
| waf_detected | `references/web/webapp/waf-bypass/手册.md`（被拦再开，禁开场丢探针） | `methodology/02-bypass-toolkit.md` |
| 参数回显点（搜索词/昵称/UA 等进 HTML/属性/JS） | `references/web/webapp/xss/手册.md`（CSP 拦了再开 `csp-bypass/手册.md`） | `xss/00-index.md` |
| 跳转参数（redirect/url/next/goto） | `references/web/webapp/open-redirect/手册.md` | — |
| 请求出网点（webhook/URL 导入/外链头像/PDF 渲染） | `references/web/webapp/ssrf/手册.md` | `ssrf-cache-host/00-index.md` |
| 文件下载/预览/包含点（download/file/path 参数） | `references/web/webapp/path-traversal/手册.md` | `path-traversal/00-index.md` |
| XML/SOAP 接口（xml 请求体/wsdl） | `references/web/webapp/xxe/手册.md` | `rce/15-xxe.md` |
| Java 栈特征（jsp/struts/序列化串/JNDI 报错） | `references/web/webapp/deserialization/手册.md` + `references/web/webapp/jndi-injection/手册.md` | `rce/12-deserialization.md` |
| 模板/表达式可配置点（自定义模板/邮件签名） | `references/web/webapp/el-injection/手册.md`（SSTI 弹药见下格） | `rce/14-ssti.md` |
| 报错栈/.git/.env/备份/SCM 路径 | `references/web/webapp/info-leak/手册.md` + `references/web/webapp/insecure-scm/手册.md` | `info-disclosure.md` |
| 前端加密参数/厚 JS（sign/token 前端生成） | `references/web/webapp/js-reverse/手册.md` | — |
| 邮件功能（注册/找回密码/邀请） | `references/web/webapp/email-header-injection/手册.md` + `references/web/webapp/host-header/手册.md`（重置投毒） | — |
| 缓存层特征（CDN/X-Cache/Age 头） | `references/web/webapp/cache-poisoning/手册.md` | `ssrf-cache-host/12-cache.md` |
| 导出报表（CSV/Excel） | `references/web/webapp/csv-formula-injection/手册.md` | — |
| AI 功能面（对话窗/文档问答/Agent 工具） | `references/web/webapp/llm-security/手册.md` | — |
| swagger/kong/actuator 暴露 | `references/web/webapp/api-gateway/手册.md` | — |
| API 面厚（REST 批量接口/多端点） | 转交 `web-api-attack`（走私/Host 头/HPP/邮件头路由表） | `api-rest/00-index.md` |

**纵深转交**：SSRF 打通内网 / RCE 拿下落点后需要推进时——redteam 轨转
`web-post-exp`（落点后阶段路由）；pentest 轨到发现即止（owners 红线禁内网
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
  跨资产指纹的洞可参考快照沉淀规范（skill_open
  `references/web/refs/poc/README.md`），但**只读参考**——POC 一律落黑板 artifact，
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
