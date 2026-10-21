# 知识库索引

实战方法论 / 测试清单 / 场景矩阵。与 `SKILL.md` 流程配合使用。

## 使用约定

- 进站先读 `打穿短表.md`；对得上再打开对应模块看细节。文件不长就整篇开；超长篇可先开点名节，不够就继续开。禁止每站通读本目录
- 磁盘有 `*src经验.md` 才开专篇，没有不算缺。开 `SKILL.md` 不会再带集团日记
- 短表和「注入/SSRF/XSS/RCE」都不是上限。本站过全类型矩阵；四件套打在有差分面上（防空窗），不是只测这四类，也不是每个 path 喷 `'`。有会话时越权/逻辑与四件套同硬（`dig-scope` §4.2.3）
- 方便和能力优先；省 token 是顺带，不挡开模块
- 短表点名的手法用标题搜。有指针的肥篇只留实战中文 + 指针段；禁开/几乎不交的篇已收成一行。手法行不删、算成不改矮。
- 篇内跳转已改成本目录真实文件（`web/漏洞分类/越权/越权漏洞（IDOR-权限绕过）.md` 一类）；不要再跟 `../xxx/SKILL.md`
- 与 `rules/` 冲突时 **以 rules 为准**（挖什么 `src-value`；CORS 不挖 `cors-vuln-report-priority`；写不写 `vuln-report-format`）
- **`web/漏洞分类/跨域配置错误/CORS 技术资料（SRC 禁用）.md` / `web/漏洞分类/大模型安全/LLM 安全测试（禁开越狱教材）.md`：不挖/禁开越狱。** `web/漏洞分类/401与403绕过/401-403 状态码绕过测试.md` 不磨登录 HTML。
- 正式 SRC 报告：`rules/vuln-report-format.md`
- 指纹级已验证 PoC 沉在同级 `poc/`（进站 grep 命中直接复现，不在本库）

## 文件清单

| 文件 | 说明 |
|------|------|
| `打穿短表.md` | 挖洞手法索引（一行/指针；正文仍在各模块） |
| `web/漏洞分类/401与403绕过/401-403 状态码绕过测试.md` | **禁开磨登录 HTML**（已收成一行）；业务 API 401 现场自己打 |
| `web/漏洞分类/API网关/API 网关安全.md` | API 网关 |
| `web/漏洞分类/AI代理工具执行/对话口工具真执行（agent-tool-exec）.md` | 对话口工具真执行（不是越狱、不是云 IDE RPC） |
| `web/漏洞分类/认证绕过/认证绕过（登录-会话-密码重置-2FA）.md` | 认证绕过（未登录改密/IDaaS + 短表指针；英文字典已砍） |
| `web/漏洞分类/缓存投毒/缓存投毒测试.md` | 缓存投毒/欺骗（原有+补充） |
| `web/漏洞分类/验证码识别/图形验证码 OCR 破解（ddddocr）.md` | 图形验证码 OCR 破解（ddddocr）解锁登录爆破/越权/找回链路（不报洞，只当钥匙） |
| `web/漏洞分类/点击劫持/点击劫持测试.md` | 缺头不写（已收成一行） |
| `web/漏洞分类/云IDE远程执行链/云 IDE-AI 编程平台弱口令→RCE→凭证链.md` | 云 IDE/Codex 系：弱口令→RPC RCE→集群/API Key 链（短表有指针） |
| `web/漏洞分类/跨域配置错误/CORS 技术资料（SRC 禁用）.md` | **不挖勿开**（已收成一行） |
| `web/漏洞分类/CRLF注入/CRLF 注入测试.md` | 几乎不交（已收成一行） |
| `web/漏洞分类/CSP绕过/CSP 绕过测试.md` | 几乎不交（已收成一行）；XSS 走 `web/漏洞分类/XSS/XSS测试.md` |
| `web/漏洞分类/跨站请求伪造/CSRF.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/CSV公式注入/CSV 公式注入测试.md` | 几乎不交（已收成一行） |
| `web/漏洞分类/悬垂标记注入/悬空标记（Dangling Markup）测试.md` | 几乎不交（已收成一行） |
| `web/漏洞分类/依赖混淆/依赖混淆测试.md` | 几乎不交（已收成一行） |
| `web/漏洞分类/反序列化/反序列化测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/DNS重绑定/DNS 重绑定测试.md` | 几乎不交（已收成一行）；SSRF 走 `web/漏洞分类/SSRF/SSRF测试.md` |
| `web/漏洞分类/表达式注入/EL 表达式注入测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/邮件头注入/邮件头注入测试.md` | 几乎不交（已收成一行） |
| `web/漏洞分类/文件上传/文件上传漏洞.md` | 文件上传（STS/列桶/分享鉴权等指针） |
| `web/漏洞分类/幽灵位截断/Ghost Bits Cast 攻击测试.md` | Ghost Bits 原理+常用字+公式；逐字节两套表已砍 |
| `web/漏洞分类/GraphQL/GraphQL 安全.md` | GraphQL（原有+补充） |
| `web/漏洞分类/HTTP参数污染/HTTP 参数污染（HPP）测试.md` | 几乎不交（已收成一行） |
| `web/漏洞分类/Host头注入/Host 头注入与密码重置毒化.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/HTTP请求走私/HTTP 请求走私测试.md` | 请求走私（原有+补充） |
| `web/漏洞分类/HTTP2攻击/HTTP2 攻击测试.md` | 几乎不交（已收成一行）；走私走 `web/漏洞分类/HTTP请求走私/HTTP 请求走私测试.md` |
| `web/漏洞分类/越权/越权漏洞（IDOR-权限绕过）.md` | 越权（中文主线 + 短表指针） |
| `web/漏洞分类/信息泄露/信息泄露测试.md` | 信息泄露 |
| `web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）.md` | 注入（OR+total / 邮件订阅 iframe / SSTI 探测；英文百科已砍） |
| `web/漏洞分类/SCM配置泄露/不安全的 SCM 配置泄露测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/JNDI注入/JNDI 注入测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/JS逆向/JS 逆向配合接口挖掘指南.md` | JS 逆向 |
| `web/漏洞分类/大模型安全/LLM 安全测试（禁开越狱教材）.md` | **禁开越狱教材**（已收成一行）；对话工具走 `web/漏洞分类/AI代理工具执行/对话口工具真执行（agent-tool-exec）.md` |
| `web/漏洞分类/业务逻辑/逻辑漏洞.md` | 业务逻辑（支付/流程 + 商家促销绑定） |
| `web/漏洞分类/JWT/OAuth-JWT-SAML 安全.md` | OAuth/JWT/SAML/OIDC（原有+多源补充） |
| `web/漏洞分类/开放重定向/开放重定向测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/路径穿越/路径穿越-LFI.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/原型链污染/原型链污染（Prototype Pollution）测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/条件竞争/竞态条件.md` | 竞态（原有+补充） |
| `web/信息收集/methodology/手册.md` | 侦察方法论 |
| `web/漏洞分类/SSRF/SSRF测试.md` | SSRF（IMDS 路径差 / GOPROXY / 对象存储回源） |
| `web/漏洞分类/子域接管/子域接管测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/类型混淆/类型混淆（Type Juggling）测试.md` | 专题知识（hack-skills 导入或融合） |
| `web/漏洞分类/WAF绕过/WAF 绕过测试.md` | WAF 绕过 |
| `web/漏洞分类/WebSocket/WebSocket 安全.md` | WebSocket（原有+补充） |
| `web/漏洞分类/XSLT注入/XSLT 注入测试.md` | 几乎不交（已收成一行） |
| `web/漏洞分类/XSS/XSS测试.md` | XSS（中文开场 + 冷门事件 + XSS→RCE / 自定义协议） |
| `web/漏洞分类/XXE/XXE测试.md` | 专题知识（hack-skills 导入或融合） |

**合计：49 个知识文件**（不含本 README）。SRC 报告版式不在本库：见 `rules/vuln-report-format.md`。判级只认注入的 rating 口径，本库不定级。
