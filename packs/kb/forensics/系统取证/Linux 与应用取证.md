---
title: Linux 与应用取证
phase: forensics
vuln_class: [forensics, linux, browser, git, logs, docker]
summary: Linux 取证的判据是「痕迹在哪个抽象层」——文件层看 mtime/SUID，数据库层看 SQLite 表，容器层看 image config 的 history，内存层看 /proc。取证题最常漏的是容器构建历史与 git 不可达对象。
---

# Linux 与应用取证

磁盘与内存镜像见 [[forensics/磁盘与内存/磁盘与内存镜像取证.md|磁盘与内存]]；流量见 [[forensics/网络取证/网络流量取证与协议隐写.md|网络取证]]；编码转换见 [[misc/编码解码/编码与二维码.md|编码解码]]。

## 0. 判据速查

| 手上有什么 | 结论 | 去哪节 |
|---|---|---|
| 给的是 `.tar` 且里面有 `manifest.json` | Docker 镜像，查 config 的 `history` | §3 |
| 目录里有 `.git` 且历史异常干净 | rebase/squash 过，查 reflog + fsck | §6 |
| `.git/objects` 里有对象读不出 | 单字节损坏，按 SHA-1 爆破修复 | §7 |
| 给 `Login Data` / `Local State` | Chrome 凭据，AES-GCM 解 | §4 |
| 给 `places.sqlite` | Firefox，`moz_places` 表 | §5 |
| 给 `.kdbx` | KeePass，先判 v3 还是 v4（Argon2） | §8 |
| 给 `auth.log` + `bash_history` + pcap | 拼时间线 | §2 |
| 给 Excel 且单元格是大数字 | VBA 变换，反推公式 | §9 |
| 进程还在但源码被删 | ptrace 注入取 code object | §10 |
| 给 pcap 且含 USB 等时传输 | 抽 ISO 数据当音频 | §11 |
| TLS 1.2 且套件不带 PFS | 弱 RSA → 造私钥解密 | §12 |

## 1. 日志与碎片重组

```bash
grep -iE "(flag|part|piece|fragment)" server.log
grep "FLAGPART" server.log | sed 's/.*FLAGPART: //' | uniq | tr -d '\n'
sort logfile.log | uniq -c | sort -rn | head
```

**碎片题的判据是「有没有序号」**：有 `part1/part2` 就按序号排，没有就靠时间戳。`uniq` 去重后再拼，重复请求在日志里非常常见。

## 2. 攻击链时间线

```bash
grep -A2 "session opened" /var/log/auth.log      # SSH 会话
cat /home/*/.bash_history                        # 用户命令
find /usr/bin -newer /var/log/auth.log -name "ms*"   # 新落地文件
tshark -r capture.pcap -Y "tftp" -T fields -e tftp.source_file
```

**三类证据的对应关系**：`auth.log` 给「谁在什么时候登录」，`bash_history` 给「做了什么」，`pcap` 给「传了什么」。时间线要交叉对齐，单看一份日志经常缺环。

**常见恶意样本模式**：AES-ECB 加密后再用同一密钥 XOR，存成 `.enc`。解密时顺序要反过来（先 XOR 再 AES 解密）。

### 2.1 应急响应五个必查点

拿到系统快照或登录权限，先跑这五条，判据是**里面出现「不该有的」**：

```bash
grep ':0:' /etc/passwd                              # 特权账号（uid=0 但不是 root）
grep -E '(/bin/bash|/bin/sh)$' /etc/passwd          # 可登录账号
cat /etc/sudoers; ls /etc/sudoers.d/                # sudo 权限
find / -perm -4000 -type f 2>/dev/null              # SUID 文件
crontab -l; ls -la /etc/cron.*; cat /etc/crontab    # 定时任务
```

`uid=0` 的非 root 账号是最高优先级线索。

### 2.2 进程侧：找已删除的可执行文件

```bash
ss -antp; lsof -i -P -n            # 网络连接与对应进程
ps auxf                            # 进程树（看异常父子关系）
ls -l /proc/<pid>/exe              # 可执行文件路径
cat /proc/<pid>/environ | tr '\0' '\n'   # 环境变量（常含 flag 或密钥）
```

判据是**`/proc/<pid>/exe` 指向 `(deleted)`**——这是典型的木马自删除，进程还在跑但磁盘上已经没有文件。

### 2.3 `ctime` 与 `mtime` 的差值

```bash
stat /etc/passwd /etc/crontab /root/.bash_history
find / -newermt '2024-01-01' -type f -printf '%T+ %p\n' 2>/dev/null | sort
```

判据是**`ctime > mtime` 说明 mtime 被伪造过**。`ctime` 只能被系统改（写 inode 元数据时触发），用户 `touch` 改不了。攻击者能回拨 mtime，回拨不了 ctime——这条比任何日志都硬。

### 2.4 Web 日志里找攻击者 IP：先看哪一列

**判据**：Apache/Nginx `access.log` 的 `REMOTE_ADDR` 是**直连对端**，
`X-Forwarded-For` 是**客户端自称**。中间有反代时两者不同，**取证要的是前者**。

```bash
# 标准 combined 格式：第 1 列就是 REMOTE_ADDR
awk '{print $1}' access.log | sort | uniq -c | sort -rn | head -20
```

| 场景 | 取哪列 | 原因 |
|---|---|---|
| 无反向代理 | 第 1 列 | 直连即真实 |
| 有 Nginx/CDN 前置 | 第 1 列是代理 IP，真 IP 在 XFF 最后一跳 | XFF 可伪造，只有代理追加的那跳可信 |
| 要证明「谁在打」 | 第 1 列 + 请求行 | 结合 `POST /shell.php` 的时序 |

**判据**：某 IP 的请求**集中在少数路径且带参数**（`?cmd=`、`?a=`、`.php?id=`）→ 扫描或 webshell 交互；
正常用户 IP 的路径分布分散、Referer 完整。

### 2.5 Webshell 已删：从编译缓存反推源码

**判据**：`webapps/ROOT/` 下找不到 jsp，但 **`work/Catalina/localhost/ROOT/` 里有 `.class`**。

Tomcat 把 JSP 编译成 `.class` 放在 `work/` 目录，**删 jsp 不会删 class**。
攻击者清理现场时通常只删 jsp，这是最可靠的还原入口。

```bash
find / -path "*/work/Catalina/*" -name "*.class" -newermt '2024-01-01'
# 反编译
cfr shell.class --outputdir out/      # 或 procyon / jadx
```

**同族落点**（其他语言的「编译/缓存副本」）：

| 语言 | 缓存位置 | 说明 |
|---|---|---|
| Java/Tomcat | `work/Catalina/.../*.class` | JSP 编译产物 |
| PHP | `/tmp/sess_*`、OPcache（`/dev/shm`） | 会话文件含源码执行痕迹 |
| Python | `__pycache__/*.pyc` | 反编译可还原 |
| .NET | `Temporary ASP.NET Files/` | 动态编译的程序集 |

### 2.6 AES 加密流量的密钥从哪找

**判据**：pcap 里是 HTTPS/自定义 TCP，载荷高熵、无 TLS 记录头 → **不是标准 TLS，是应用层自己加密**。
此时 Wireshark 的 TLS 解密完全无效，密钥在**服务端代码或配置文件里**。

寻找顺序：

1. **webshell 源码 / 反编译的 class** —— 密钥常硬编码在加密函数旁边
2. **配置文件** —— `config.php`、`application.yml`、`.env`
3. **进程命令行** —— `ps aux` 或内存镜像的 `cmdline`，`-Dkey=xxx` 形式
4. **内存镜像 strings** —— `strings mem.raw | grep -E "^[0-9a-f]{32}$"`（32 位 hex 大概率是 AES-256 key）

```bash
# 拿到 key/iv 后解（假设 AES-128-CBC）
openssl enc -d -aes-128-cbc -K <hexkey> -iv <hexiv> -in payload.bin -out plain.bin
```

**判据链**：解出来的明文**能认出下一层结构**（HTTP 头、`PK`、JSON）→ key 对；
是乱码 → 试 CBC/ECB/CTR 三种模式，或 key 是 hex 还是原始字节搞反了。

### 2.7 勒索样本的家族与扩展名

**判据**：文件被批量改名成**统一后缀**、原文件内容高熵、目录里有勒索信（`README.txt`/`HOW_TO_DECRYPT`）。

| 扩展名 | 家族 | 备注 |
|---|---|---|
| `.wnry` / `.wcry` / `.WNCRY` | WannaCry | 同时有 `@Please_Read_Me@.txt`、`.WNCRYT` |
| `.locky` / `.zepto` | Locky | |
| `.cerber` | Cerber | |
| `.ryk` / `.ryuk` | Ryuk | 无勒索信，直接投递 |
| 随机 5-8 字符 | 多数新变种 | 看勒索信里的邮箱/洋葱地址认家族 |

**应急取证的顺序**（勒索题常问"密钥在哪"）：

1. **内存镜像优先** —— 密钥在加密时驻留内存，进程退出前可 dump
2. `strings mem.raw | grep -E "BEGIN (RSA )?PRIVATE KEY"` —— 硬编码私钥
3. **注册表 / SAM** —— 有些变种把密钥存 `HKLM\SOFTWARE\<random>`
4. **U 盘插拔记录** —— `setupapi.dev.log`、`USBSTOR`，常是密钥载体
5. 文件恢复 —— `photorec` 找加密前的残留（如果是"先复制再加密"的变种）

**判据**：镜像里**找不到勒索进程本身**是正常的——现代勒索跑完就退出，
残留的是**文件系统痕迹**（被改名的文件、勒索信、`$Recycle.Bin` 里的原文件）。

## 3. Docker 镜像：构建历史

**镜像 config JSON 的 `history` 数组永久保存所有 `RUN` 命令，与后续清理无关。**

```bash
tar xf app.tar
python3 -m json.tool blobs/sha256/<config_hash> | grep -A2 "created_by"
```

步骤：解 tar → 读 `manifest.json` 找 config blob 哈希（**不是 layer blob**）→ 解析 config 里的 `history[].created_by`。

**判据**：即使后面某层 `rm -f secret.txt`，前面 `RUN echo "flag{...}" > secret.txt` 仍然明文可见。清理只影响最终文件系统，不影响历史记录。

## 4. Chrome/Edge 凭据

```python
from Crypto.Cipher import AES
import sqlite3

with open('master_key.txt', 'rb') as f:
    master_key = f.read()

conn = sqlite3.connect('Login Data')
for url, user, enc in conn.execute('SELECT origin_url, username_value, password_value FROM logins'):
    nonce, ct, tag = enc[3:15], enc[15:-16], enc[-16:]   # 前 3 字节是 v10/v11
    pw = AES.new(master_key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
    print(f"{url}: {user}:{pw.decode()}")
```

**主密钥在 `Local State` 的 `os_crypt.encrypted_key`**，base64 解出来去掉前 5 字节 `DPAPI` 前缀，再用 `CryptUnprotectData` 解。CTF 里通常直接给你 `master_key.txt`。

**偏移是最容易错的地方**：`v10`/`v11` 占 3 字节，nonce 12 字节，tag 16 字节在尾部。切错一位就解不开，且报的是 MAC 校验失败（看起来像密钥错）。

## 5. Firefox

```bash
sqlite3 places.sqlite "SELECT url FROM moz_places WHERE url LIKE '%flag%'"
```

表：`moz_places`（URL 与标题）、`moz_bookmarks`、`moz_cookies`；表单在 `formhistory.sqlite` 的 `moz_formhistory`。

**时间戳单位**：Firefox 用 Unix 微秒（`last_visit_date/1000000`）；**Chrome 用 WebKit epoch，1601 年起算的微秒**（`last_visit_time/1000000-11644473600`）。两者差一个常量，混用会得到 1970 或 1601 附近的荒谬日期。

## 6. Git 不可达对象

```bash
git reflog --all
git fsck --unreachable --no-reflogs
git show <commit>
git show <commit>:path/to/file
```

**`git rebase --squash` 只是把提交从分支上摘掉，对象仍在**。默认 `git gc` 的过期期是两周，所以只要仓库是近期打包的，被「删掉」的提交都还在。

**判据**：历史干净得反常（单个 commit、或全是 squash merge）+ 题面提到 rewrite / rebase / squash / clean history。

**`.git` 暴露在 Web 上时**用 `gitdumper.sh`（internetwache/GitTools）拉全量，比手工拼对象可靠。

## 7. Git blob 单字节损坏

git 用 SHA-1 内容寻址，所以**期望哈希从 commit tree 里就能读到**，即使对象已损坏。

```python
import subprocess
def repair_blob(filepath, target_hash):
    data = bytearray(open(filepath, 'rb').read())
    for pos in range(len(data)):
        orig = data[pos]
        for val in range(256):
            if val == orig:
                continue
            data[pos] = val
            open(filepath, 'wb').write(data)
            out = subprocess.run(['git', 'hash-object', filepath],
                                 capture_output=True, text=True).stdout.strip()
            if out == target_hash:
                print(f"Fixed byte {pos}: 0x{orig:02x} -> 0x{val:02x}")
                return True
            data[pos] = orig
    open(filepath, 'wb').write(data)
    return False
```

**先 `zlib.decompress` 再改**：`.git/objects/XX/...` 是 zlib 压缩的，直接在压缩流上改字节不会得到目标哈希。改完再 `zlib.compress` 写回。

**多字节损坏时**：单靠哈希爆破是 `256^n`，必须加语境约束（源码要能编译、常量要合法）。

## 8. KeePass

```bash
# v3 用官方；v4（Argon2）要 fork
./keepass2john system.kdbx > hash.txt          # ivanmrsulja/keepass2john
python3 keepass4brute.py -d wordlist.txt system.kdbx
hashcat -m 13400 hash.txt wordlist.txt         # Argon2
```

**判据**：`file` 看 `KDBX 4.x` 字样，或 `keepass2john` 报解析失败。**官方 keepass2john 不支持 v4**，这是最常见的卡点。

**字典要带语境**：`cewl http://target:8080 -d 2 -m 5 -w words.txt` 抓站内词，再手工加题面关键词。纯 rockyou 对主题密码命中率低。

**凭据常在 Notes 或附件字段**，不是 Password 字段——解开后每个条目都要展开看。

## 9. VBA 宏：单元格 → 二进制

```python
import csv
with open('data.csv') as f, open('binary', 'wb') as out:
    for row in csv.reader(f):
        for cell in row:
            if cell.strip():
                out.write(bytes([int((int(cell) - 78) / 3)]))
```

**永远重写宏逻辑，不要执行宏**。用 `olevba` 导出 VBA 源码看变换公式（`CByte` / `Chr` / `Write` 是典型信号），再用 Python 复现。

**判据**：Excel 单元格里全是大数字、且数字分布不像正常数据（量级集中在某个区间）。

## 10. 进程内存里的 Python 源码

```bash
pgrep -f python
pyrasite-shell <PID>
```

```python
import sys, uncompyle6
for name, obj in globals().items():
    if hasattr(obj, 'func_code'):
        uncompyle6.main.uncompyle(sys.version_info[0] + sys.version_info[1]/10.0,
                                  obj.func_code, sys.stdout)
print(globals())
```

**源码删了不影响内存里的 code object**。`/proc/<PID>/fd/` 可能仍指向已删除的文件（`ls -l` 会显示 `(deleted)`），但通常直接从 code object 反编译更快。

**前提**：`ptrace` 权限。查 `/proc/sys/kernel/yama/ptrace_scope`，为 1 时只能 attach 子进程。

**Python 3.9+ 用 `pycdc`**：`uncompyle6` 不支持新字节码，需先 `marshal.dump` 出 `.pyc` 再喂给 `pycdc`。

## 11. USB 等时传输里的音频

```bash
tshark -r capture.pcap -T fields -e usb.iso.data > audio_data.txt
```

导入 Audacity 时按**有符号 16 位 PCM、单声道**试，采样率从 8000 试到 48000。

**判据**：USB 传输类型是 `URB_ISOCHRONOUS`（等时传输专门给实时音视频）。批量传输（BOT）是存储设备，中断传输是 HID（键盘鼠标）。

## 12. 弱 RSA 解 TLS

TLS 1.2 + `TLS_RSA_WITH_AES_256_CBC_SHA`（不带 PFS）才能这么解。

```bash
openssl x509 -in public.der -inform DER -noout -modulus   # 拿 n
# 分解 n（factordb / yafu / dCode）→ rsatool -p P -q Q -o private.pem
# Wireshark: Preferences -> Protocols -> TLS -> RSA keys list
```

**先确认套件**：ECDHE 系列有 PFS，拿到私钥也解不开。判据是 Server Hello 里的 cipher suite。

解密后 `File -> Export Objects -> HTTP` 抽下载的文件。

## 13. 其他速查

- **TFTP netascii 会破坏二进制**：`0d 0a → 0a`、`0d 00 → 0d`，导出后手工替换。
- **ROT18** = 字母 ROT13 + 数字 ROT5，多阶段题的常见收尾层。
- **区块链追踪**：按「金额相近（扣手续费）、时间接近、一对多扇出」找混币器；交易数 5-100 的是中间钱包，1000+ 的是交易所/水龙头，直接忽略。
- **常见编码**：`base64 -d`、`xxd -r -p`、`tr 'A-Za-z' 'N-ZA-Mn-za-m'`（ROT13）。

## 14. 纪律

- **先判抽象层**。同一道题里「文件系统 / 数据库 / 容器历史 / 进程内存」是四层，工具完全不同。看到 `.tar` + `manifest.json` 就该想到构建历史，而不是去翻 layer 文件。
- **时间戳纪元必须确认**。Chrome 的 WebKit epoch 与 Unix epoch 差 11644473600 秒，混用会得到看起来「有数据但荒谬」的结果。
- **「已删除」不等于「不存在」**。git 不可达对象、Docker 历史、已删文件的 `/proc` 引用，都是默认还活着的。
- **宏与脚本永远重写不执行**。反推变换公式的成本远低于中招的成本。
