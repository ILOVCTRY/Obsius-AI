# 知识库索引

实战方法论 / 测试清单 / 场景矩阵。与 `SKILL.md` 流程配合使用。

## 使用约定

- 进站先读 `打穿短表.md`；对得上再打开对应模块看细节。文件不长就整篇开；超长篇可先开点名节，不够就继续开。禁止每站通读本目录
- 磁盘有 `*src经验.md` 才开专篇，没有不算缺。开 `SKILL.md` 不会再带集团日记
- 短表和「注入/SSRF/XSS/RCE」都不是上限。本站过全类型矩阵；四件套打在有差分面上（防空窗），不是只测这四类，也不是每个 path 喷 `'`。有会话时越权/逻辑与四件套同硬（`dig-scope` §4.2.3）
- 方便和能力优先；省 token 是顺带，不挡开模块
- 短表点名的手法用标题搜。有指针的肥篇只留实战中文 + 指针段；禁开/几乎不交的篇已收成一行。手法行不删、算成不改矮。
- 篇内跳转已改成本目录真实文件（`web/webapp/idor/手册.md` 一类）；不要再跟 `../xxx/SKILL.md`
- 与 `rules/` 冲突时 **以 rules 为准**（挖什么 `src-value`；CORS 不挖 `cors-vuln-report-priority`；写不写 `vuln-report-format`）
- **`web/webapp/cors/手册.md` / `web/webapp/llm-security/手册.md`：不挖/禁开越狱。** `web/webapp/401-403-bypass/手册.md` 不磨登录 HTML。
- 正式 SRC 报告：`rules/vuln-report-format.md`
- 指纹级已验证 PoC 沉在同级 `poc/`（进站 grep 命中直接复现，不在本库）

## 文件清单

| 文件 | 说明 |
|------|------|
| `打穿短表.md` | 挖洞手法索引（一行/指针；正文仍在各模块） |
| `web/webapp/401-403-bypass/手册.md` | **禁开磨登录 HTML**（已收成一行）；业务 API 401 现场自己打 |
| `web/webapp/api-gateway/手册.md` | API 网关 |
| `web/webapp/agent-tool-exec/手册.md` | 对话口工具真执行（不是越狱、不是云 IDE RPC） |
| `web/webapp/authbypass/手册.md` | 认证绕过（未登录改密/IDaaS + 短表指针；英文字典已砍） |
| `web/webapp/cache-poisoning/手册.md` | 缓存投毒/欺骗（原有+补充） |
| `web/webapp/captcha-ocr/手册.md` | 图形验证码 OCR 破解（ddddocr）解锁登录爆破/越权/找回链路（不报洞，只当钥匙） |
| `web/webapp/clickjacking/手册.md` | 缺头不写（已收成一行） |
| `web/webapp/cloud-ide-rce-chain/手册.md` | 云 IDE/Codex 系：弱口令→RPC RCE→集群/API Key 链（短表有指针） |
| `web/webapp/cors/手册.md` | **不挖勿开**（已收成一行） |
| `web/webapp/crlf-injection/手册.md` | 几乎不交（已收成一行） |
| `web/webapp/csp-bypass/手册.md` | 几乎不交（已收成一行）；XSS 走 `web/webapp/xss/手册.md` |
| `web/webapp/csrf/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/csv-formula-injection/手册.md` | 几乎不交（已收成一行） |
| `web/webapp/dangling-markup/手册.md` | 几乎不交（已收成一行） |
| `web/webapp/dependency-confusion/手册.md` | 几乎不交（已收成一行） |
| `web/webapp/deserialization/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/dns-rebinding/手册.md` | 几乎不交（已收成一行）；SSRF 走 `web/webapp/ssrf/手册.md` |
| `web/webapp/el-injection/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/email-header-injection/手册.md` | 几乎不交（已收成一行） |
| `web/webapp/file-upload/手册.md` | 文件上传（STS/列桶/分享鉴权等指针） |
| `web/webapp/ghost-bits-cast/手册.md` | Ghost Bits 原理+常用字+公式；逐字节两套表已砍 |
| `web/webapp/graphql/手册.md` | GraphQL（原有+补充） |
| `web/webapp/hpp/手册.md` | 几乎不交（已收成一行） |
| `web/webapp/host-header/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/http-smuggling/手册.md` | 请求走私（原有+补充） |
| `web/webapp/http2-attacks/手册.md` | 几乎不交（已收成一行）；走私走 `web/webapp/http-smuggling/手册.md` |
| `web/webapp/idor/手册.md` | 越权（中文主线 + 短表指针） |
| `web/webapp/info-leak/手册.md` | 信息泄露 |
| `web/webapp/sqli/手册.md` | 注入（OR+total / 邮件订阅 iframe / SSTI 探测；英文百科已砍） |
| `web/webapp/insecure-scm/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/jndi-injection/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/js-reverse/手册.md` | JS 逆向 |
| `web/webapp/llm-security/手册.md` | **禁开越狱教材**（已收成一行）；对话工具走 `web/webapp/agent-tool-exec/手册.md` |
| `web/webapp/logic/手册.md` | 业务逻辑（支付/流程 + 商家促销绑定） |
| `web/webapp/jwt/手册.md` | OAuth/JWT/SAML/OIDC（原有+多源补充） |
| `web/webapp/open-redirect/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/path-traversal/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/prototype-pollution/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/race-condition/手册.md` | 竞态（原有+补充） |
| `web/recon/methodology/手册.md` | 侦察方法论 |
| `web/webapp/ssrf/手册.md` | SSRF（IMDS 路径差 / GOPROXY / 对象存储回源） |
| `web/webapp/subdomain-takeover/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/type-juggling/手册.md` | 专题知识（hack-skills 导入或融合） |
| `web/webapp/waf-bypass/手册.md` | WAF 绕过 |
| `web/webapp/websocket/手册.md` | WebSocket（原有+补充） |
| `web/webapp/xslt-injection/手册.md` | 几乎不交（已收成一行） |
| `web/webapp/xss/手册.md` | XSS（中文开场 + 冷门事件 + XSS→RCE / 自定义协议） |
| `web/webapp/xxe/手册.md` | 专题知识（hack-skills 导入或融合） |

**合计：49 个知识文件**（不含本 README）。SRC 报告版式不在本库：见 `rules/vuln-report-format.md`。定级只认 format，本库不定级。
