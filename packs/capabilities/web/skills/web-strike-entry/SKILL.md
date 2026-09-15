---
name: web-strike-entry
description: Web/SRC 渗透入口技能：双模式判定（锁面/自由跳）+ 目标特征路由对照表
keywords: 渗透, src, 挖洞, 测试, 漏洞, 目标, 网站, 注入, sql, sqli, xss, 上传, 越权, idor, ssrf, csrf, jwt, oauth, graphql, 逻辑漏洞, rce, 命令执行, 文件包含, webshell, 打点, 利用
features: has_user_system, has_upload, has_search, has_pay, has_graphql, has_oauth, has_websocket, returns_401, waf_detected
task_types: exploit, privesc, lateral-movement
---

# web-strike-entry —— 渗透入口：双模式判定 + 特征路由

> 分层纪律：本技能只做**入口路由**。方法论在 src-strike 快照
> （kb_open 前缀 `src-strike/知识库/`），弹药在
> `src-strike/references/playbooks/`。**按需 kb_open 单篇，禁止通读。**

## 0. 双模式先判（必做第一动作）

| 模式 | 判定 | 打法 |
|---|---|---|
| **自由跳** | 模糊目标：只给集团/品牌名，没有 URL 清单 | 被动侦察落种子队列 → 一种子闭环 |
| **锁面** | 固定站 / URL 清单 / 众测 program | 锁定给定字符串，出 scope 硬闸 |

## 1. 目标特征 → 模块路由对照表

进站先识别特征，按下表用 **`kb_open(module=…)`** 打开对应模块（module 均带
`src-strike/` 快照前缀；不存在时服务端回可选清单，照清单改选，禁止猜名）。

| 目标特征（features） | 方法论（`src-strike/知识库/` 下） | 弹药（`src-strike/references/playbooks/` 下） |
|---|---|---|
| has_user_system（有注册/登录） | `src-strike/知识库/idor-test.md` + `authbypass-test.md` | `arbitrary-x-authz.md` |
| has_search / has_filter | `src-strike/知识库/injection-test.md` | `sqli.md` |
| has_upload（文件上传） | `src-strike/知识库/file-upload-test.md` | `file-upload/00-index.md` |
| has_pay（支付/优惠券/积分） | `src-strike/知识库/logic-test.md` + `race-condition-test.md` | `logic-flaws/00-index.md` |
| has_graphql | `src-strike/知识库/graphql-test.md` | `graphql.md` |
| has_oauth / JWT / SAML | `src-strike/知识库/oauth-jwt-test.md` | `oauth-saml-jwt/00-index.md` |
| has_websocket | `src-strike/知识库/websocket-test.md` | `api-rest/13-websocket.md` |
| returns_401/403 | 分清登录页 vs 业务 API；path/METHOD/头现场改 | — |
| waf_detected | `src-strike/知识库/waf-bypass.md`（被拦再开，禁开场丢探针） | `src-strike/references/methodology/02-bypass-toolkit.md` |

**平台归属 → `meta.owner`**：登记/更新资产时按特征打标（`.edu.cn` 系 → `edusrc`；
萤石 → `ysrc`；OPPO/realme/OnePlus → `osrc`；无归属不打）——命中的平台规则
（收录标准/验证边界）会注入会话，**测前先看自己有没有踩平台红线**。

## 2. 反空转规则

- 打开是登录页：先找业务面（网关/跳转后的 host），主业挖未登录；
  看见登录页 ≠ 换资产；人机验证合理几轮即停，不做 OCR/轨迹模拟。
- nuclei 只是辅助（已知 CVE/暴露面），不是主路径，禁止全量模板扫当进度。
- 中危同一对象先升链；肥面深挖、瘦壳一眼；禁偏科（连日只堆未授权读）。

## 3. 产出落点（黑板联动）

- 发现 → findings（无证据 = unverified）；POC → artifacts（kind=poc）。
  跨资产指纹的洞可参考快照沉淀规范（kb_open
  `src-strike/poc/README.md`），但**只读参考**——POC 一律落黑板 artifact，
  不写进 kb/ 快照目录；
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
