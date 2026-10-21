# web 域检索入口

> **用途**: 收到 Web 相关问题时，先用本页判断「该查哪一类」，再 `kb_open` 打开具体手册。
> **本页只做索引**：判据、命令、payload 一律在各手册正文里，不要在这里找答案。
> 域内模块清单见 [[web/知识库.md|web/README.md]]；任务导航见 [[web/route.json|web/route.json]]。

---

## 一、按现象找模块

现象比漏洞名更好认——先认现象，再落到具体手法。

| 看到什么 | 去哪里 |
|---|---|
| 输入被拼进 SQL、报错回显数据库信息、页面真假不一 | [[web/漏洞分类/注入类/SQL 注入全手法（按「什么被挡住」索引）.md|注入类/sql-injection.md]]、[[web/漏洞分类/SQL注入/注入类漏洞测试手册（SQL注入、命令注入、SSTI）.md|SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）.md]] |
| 参数进了 `system`/`eval`/模板、能带出命令回显 | [[web/漏洞分类/注入类/代码执行与命令执行.md|注入类/server-side-exec.md]]、[[web/漏洞分类/注入类/服务端高级技巧.md|注入类/server-side-advanced.md]] |
| 反序列化入口（cookie/参数/文件）、POP 链 | [[web/漏洞分类/注入类/反序列化与竞争条件.md|注入类/server-side-deser.md]]、[[web/漏洞分类/反序列化/反序列化测试（deserialization）.md|反序列化/反序列化测试.md]] |
| 能传文件、能改扩展名/内容类型 | [[web/漏洞分类/文件上传/文件上传漏洞测试手册.md|文件上传/文件上传漏洞.md]] |
| 参数控制读取路径、可读任意文件 | [[web/漏洞分类/路径穿越/路径穿越、LFI 测试.md|路径穿越/路径穿越-LFI.md]] |
| 服务端替我们发请求、回显内网结果 | [[web/漏洞分类/SSRF/SSRF 测试手册.md|SSRF/SSRF测试.md]] |
| 前端过滤、CSP、原型链、DOM 汇聚点 | [[web/漏洞分类/客户端攻击.md|client-side.md]]、[[web/漏洞分类/高级客户端攻击.md|client-side-advanced.md]]、[[web/漏洞分类/XSS/XSS 测试手册.md|XSS/XSS测试.md]] |
| 登录态、token、越权、会话固定 | [[web/漏洞分类/认证与会话/认证与访问控制攻击.md|认证与会话/auth-and-access.md]]、[[web/漏洞分类/认证与会话/JWT 与 JWE 令牌攻击.md|认证与会话/auth-jwt.md]]、[[web/漏洞分类/认证绕过/authbypass-authentication-flaws.md|认证绕过/认证绕过（登录-会话-密码重置-2FA）.md]]、[[web/漏洞分类/越权/越权漏洞（IDOR、权限绕过）测试手册.md|越权/越权漏洞（IDOR-权限绕过）.md]] |
| 有源码可读、要系统性找洞 | [[web/漏洞分类/源码审计/源码白盒审计方法论（覆盖率矩阵与三策略）.md|源码审计/源码白盒审计方法论（覆盖率矩阵与三策略）.md]]、[[web/漏洞分类/源码审计/php代码审计.idx — 按需分段读取.md|源码审计/php代码审计.idx.md]] |
| 只有黑盒流量、不清楚入口 | [[web/漏洞分类/污点分析/污点分析与数据流追踪手册（source→sink）.md|污点分析/污点分析与数据流追踪（source→sink）.md]]、[[web/漏洞分类/控制缺失审计/控制缺失型审计手册（端点-权限矩阵与认证旁路）.md|控制缺失审计/控制缺失型审计（端点-权限矩阵与认证旁路）.md]] |
| 特定框架/组件/历史 CVE | [[web/漏洞分类/Web 框架与产品已知漏洞索引.md|cves.md]] |
| 协议层异常（走私、HPP、HTTP2、WebSocket） | `HTTP请求走私/HTTP 请求走私测试.md`、`HTTP参数污染/HTTP 参数污染（HPP）测试.md`、`HTTP2攻击/HTTP2 攻击测试.md`、`WebSocket/WebSocket 安全.md` |

---

## 二、按技术标签定位

标签只标「主手册在哪」，具体判据看正文。

| 标签 | 主手册 |
|---|---|
| `联合注入` `报错注入` `堆叠注入` `盲注` `宽字节` `二次注入` `DNS注入` | [[web/漏洞分类/注入类/SQL 注入全手法（按「什么被挡住」索引）.md|注入类/sql-injection.md]] |
| `system` `passthru` `exec` `shell_exec` `反弹shell` `无回显` `disable_functions` `无字母` | [[web/漏洞分类/注入类/代码执行与命令执行.md|注入类/server-side-exec.md]] |
| `php伪协议` `filter` `input` `日志包含` `session包含` `临时文件` `phar` `截断` | [[web/漏洞分类/路径穿越/路径穿越、LFI 测试.md|路径穿越/路径穿越-LFI.md]] |
| `gopher` `redis` `fastcgi` `DNS-rebinding` `dict` `curl` `内网` | [[web/漏洞分类/SSRF/SSRF 测试手册.md|SSRF/SSRF测试.md]] |
| `POP链` `wakeup` `字符逃逸` `SoapClient` `原生类` `魔术方法` | [[web/漏洞分类/反序列化/反序列化测试（deserialization）.md|反序列化/反序列化测试.md]] |
| `Jinja2` `Twig` `Smarty` `__mro__` `__subclasses__` `__globals__` | [[web/漏洞分类/注入类/服务端高级技巧.md|注入类/server-side-advanced.md]] |
| `None算法` `KID注入` `JKU` `HS256` `RS256` `密钥爆破` | [[web/漏洞分类/认证与会话/JWT 与 JWE 令牌攻击.md|认证与会话/auth-jwt.md]] |
| `弱类型` `preg_match` `变量覆盖` `extract` `strcmp` `md5碰撞` `intval` | [[web/漏洞分类/源码审计/php代码审计.idx — 按需分段读取.md|源码审计/php代码审计.idx.md]] |
| `WebShell` `蚁剑` `MIME绕过` `图片马` `.htaccess` `解析漏洞` | [[web/漏洞分类/文件上传/文件上传漏洞测试手册.md|文件上传/文件上传漏洞.md]] |
| `CSP` `nonce` `prototype pollution` `DOM clobbering` | [[web/漏洞分类/高级客户端攻击.md|client-side-advanced.md]]、[[web/漏洞分类/原型链污染/原型链污染测试.md|原型链污染/原型链污染（Prototype Pollution）测试.md]] |
| `gopher` `CRLF` `Host头` `缓存投毒` `开放重定向` | `CRLF注入/CRLF 注入测试.md`、`Host头注入/Host 头注入与密码重置毒化.md`、[[web/漏洞分类/缓存投毒/缓存投毒与缓存欺骗测试.md|缓存投毒/缓存投毒测试.md]]、[[web/漏洞分类/开放重定向/开放重定向全手法（按校验方式选绕过）.md|开放重定向/开放重定向测试.md]] |

---

## 三、按打法阶段

| 阶段 | 入口 |
|---|---|
| 信息收集 / 资产测绘 | [web/信息收集/](../信息收集/) |
| 单点突破（漏洞验证） | [web/实战打法/](../实战打法/)、[web/漏洞分类/](.) |
| 拿 shell 之后 / 内网横向 | [web/后渗透/](../后渗透/)、[web/后渗透/intranet-postexp/](../后渗透/intranet-postexp/) |
| 报告与合规口径 | [web/行业视野/](../行业视野/)、[packs/kb/安全红线/](../../安全红线/) |

---

## 四、怎么用

1. **先 `kb_search`**：中文关键词检索正文（FTS5+BM25），返回路径与摘要。
2. **认路径**：结果里的 `module` 直接喂给 `kb_open`，不要手拼路径。
3. **大文件用 `.idx.md`**：`*.idx.md` 是分段索引，先读它拿到行号，再 `Read offset/limit` 读正文，别整篇加载。
4. **判据优先**：本库的写作口径是「看到什么现象 → 该做什么 → 什么情况算不成立」。找不到判据的手册说明它还没写完，去 `kb_search` 换关键词。

> 检索是**纯关键词**（FTS5），不认同义词。查「横向移动」找不到「内网渗透」，换词重试比硬猜更省事。
