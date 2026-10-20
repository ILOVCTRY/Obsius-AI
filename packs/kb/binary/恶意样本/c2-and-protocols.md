---

phase: binary
vuln_class: [malware, c2, traffic-analysis, crypto]
title: C2 流量与协议分析
summary: 流量题的解法是「先认加密算法，再找密钥，最后按时间拼接解密」。判据是魔数、端口异常、心跳间隔，不是逐个包肉眼看。
---

# C2 流量与协议分析

PE/.NET 样本侧的配置提取见 [[binary/恶意样本/pe-and-dotnet.md|PE 与 .NET]]；脚本混淆与内存取证见 [[binary/恶意样本/scripts-and-obfuscation.md|脚本与混淆]]；流量侧的通用取证见 [[forensics/流量分析/|流量分析]]。

## 0. 判据速查

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| 非标准端口跑已知协议（21 端口不是 FTP） | 端口被改，需要重映射 | §1 |
| 固定间隔、等长的短请求 | Beacon 心跳 | §3 |
| 只有 DNS 查询，子域长且字符集受限 | DNS 隧道 | §3 |
| 明文里出现 `expand 32-byte k` / `0x9E3779B9` | ChaCha20 / TEA | §4 |
| 二进制里 256 字节顺序初始化表 | RC4 | §4 |
| WSS 但证书异常 / 非 443 | 可能自签 TLS 套 RC4 | §5 |
| 密码每隔固定时间变 | 密钥轮转，解密器要同步 | §6 |
| pcap 里是 `PASS`/`JOIN`/`PRIVMSG` | IRC C2 | §9 |
| 流量看起来"什么都没有" | 有隐藏网段，做 ARP 欺骗 | §9 |

**最容易犯的错**：拿到 pcap 就肉眼翻。先跑协议统计和端口分布，通常一眼能看出形态。

## 1. 起点：协议统计

```bash
# 协议分层统计
tshark -r file.pcap -q -z io,phs

# 端口对话
tshark -r file.pcap -q -z conv,tcp

# 按流导出原始 payload
tshark -r file.pcap -Y "tcp.stream eq 3" -T fields -e tcp.payload

# 提取 HTTP 响应体（找投放的载荷）
tshark -r capture.pcap -Y "http.response" -T fields -e http.file_data | xxd -r -p > payload.bin
```

要点：

- **`io,phs` 先看协议分布**。如果 90% 是 DNS 且请求量巨大，直接进 DNS 隧道方向，不用看别的。
- **`conv,tcp` 看谁跟谁说了多少**。少量长连接 + 大量短连接，是「控制通道 + 数据外带」的典型形态。
- 端口被改过时用 `tcprewrite` 重映射，让 Wireshark 能正确解析：

```bash
tcprewrite --infile=in.pcap --outfile=out.pcap \
  --portmap=8080:443 --fixcsum
```

## 2. 自定义加密协议

```text
1. 找硬编码密钥 —— 通常在 .rodata / .rdata，或在配置解密后的结构里
2. 判断是流密码还是块密码 —— 有无 padding、有无固定块长
3. 流密码：把两个方向的 payload 按时间顺序全部拼接
4. 块密码：确认 IV 来源（硬编码 / 密文前 N 字节 / 全零）
```

**流密码必须双向拼接**，这是最常见的失败原因。密钥流状态在双向之间共享，只取客户端→服务端方向，解密出来的第二段开始全是乱的。

**ChaCha20 白拿密钥流**：发一段全零明文，密文就等于密钥流（`0 XOR k = k`）。拿到密钥流后可以直接异或任意密文。

**另一条路：把 pcap 的密文直接喂给样本本身**。样本里有完整的解密逻辑，构造输入让它输出明文，比手工实现加密算法可靠。

## 3. C2 形态识别

| 形态 | 特征 | 提取方法 |
|---|---|---|
| Beacon 心跳 | 固定间隔（±抖动）、等长、内容近乎不变 | 按时间排序看间隔分布 |
| DGA | 每次请求域名无规律但字符集一致 | 收集全部域名做 n-gram / 长度分析 |
| DNS 隧道 | 子域超长、base32/base64 编码、TXT 记录多 | 拼接子域再解码 |
| HTTP 自定义头 | header 里带 base64 长串 | `tshark -T fields -e http.request.full_uri` |
| 加密载荷 | 高熵、等长块 | 判算法后解密 |

```bash
# DNS 隧道：按时间顺序拿全部查询名
tshark -r capture.pcap -Y "dns.qry.name" -T fields -e dns.qry.name -e frame.time_epoch \
  | sort -k2 -n | awk '{print $1}'
```

**顺序很关键**：DNS 隧道的数据是按查询顺序拼接的，`sort -u` 会破坏顺序，必须带时间戳排。

## 4. 按常量认算法

| 算法 | 特征 |
|---|---|
| AES | S-box 起始 `0x637c777b`，表里连续 `0x63` |
| ChaCha20 | 字符串 `expand 32-byte k`，或常量 `0x61707865` |
| TEA / XTEA | `0x9E3779B9`（黄金比例倒数） |
| RC4 | 256 字节顺序初始化的 S-box（`for i in 0..255: S[i]=i`） |
| Base64 | 字符集 `A-Za-z0-9+/=`，长度 %4==0 |

按结构辅助判断：

```text
块密码：固定块长（16 字节）、有 padding、密文长度是块长整数倍
流密码：逐字节、无 padding、密文长度等于明文长度
哈希：无逆、固定输出长度、有轮常量
```

## 5. RC4 over WebSocket（WSS）

**特征**：WSS 跑在非标准端口，证书是自签或异常，TLS 解密后是 WebSocket 帧，帧内还是密文。

```text
解密流程：
1. 从样本源码确认 C2 端口（不是 443）
2. tcprewrite 重映射端口，让 Wireshark 按 TLS 解析
3. 提供 RSA 私钥解 TLS（私钥在样本里，或在题目附件里）
4. 得到 WebSocket 帧
5. 从样本二进制里找 RC4 密钥
6. 逐帧 RC4 解密
```

样本的通信内容通常分三类：

```text
注册：主机名、OS 版本、用户名、权限级别
外带：截图、键盘记录、文件内容
指令：反弹 shell、文件下载、进程列表
```

## 6. 密钥轮转

密钥随时间或消息计数变化时，解密器必须同步：

```python
import hashlib

def get_current_password(timestamp):
    # 每小时换一次
    hour_bucket = timestamp // 3600
    return hashlib.sha256(f"seed_{hour_bucket}".encode()).digest()
```

要点：

- **先找到轮转的触发条件**：时间桶、消息计数、还是服务端下发的 nonce。
- 时间桶要确认**用的是哪个时钟**：UTC、本地时区、还是启动后的相对时间。差 8 小时就全错。
- 消息计数型轮转要求你从**第一条消息**开始数，丢包会导致后续全错位。

## 7. AES-CBC

最常见的三个默认假设，按顺序试：

```python
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import hashlib

# 假设 1：key = MD5(硬编码口令)
key = hashlib.md5(b"hardcoded_password").digest()

# 假设 2：IV = 密文前 16 字节
iv, ct = ciphertext[:16], ciphertext[16:]

# 假设 3：PKCS#7 padding
plaintext = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct), 16)
```

其他密钥来源：硬编码字符串的 SHA256、时间戳派生、PID 派生、PBKDF2。

**先验证再批量解**：拿第一条消息试，能解出可读文本再处理全部。全部试一遍再发现密钥不对，浪费的是时间。

## 8. 具体家族特征

### 8.1 Poison Ivy（Camellia）

```text
密码 → 直接 null 补齐到 32 字节作为密钥，没有任何 KDF
默认口令 "admin"，常被原样保留
端口常见 3460 / 65535
握手阶段有 256 字节密钥交换
密文块长度 8 字节对齐
```

```python
from Crypto.Cipher import Camellia
key = b"admin".ljust(32, b"\x00")[:32]
plaintext = Camellia.new(key, Camellia.MODE_ECB).decrypt(encrypted_data)
```

或用 ChopShop 一步到位（会自动重建会话、还原截图与键盘记录）：

```bash
chopshop -f capture.pcap -s ./output/ "poisonivy_23x -c -w admin"
```

试的口令顺序：`admin` → `password` → `p0ison` → 题目提示。

### 8.2 Cobalt Strike

```text
默认 URI：/submit.php  /pixel  /__utm.gif  /ca  /dpixel
Cookie 里是 base64 编码的元数据
默认 60 秒 sleep，带 jitter
User-Agent 与 Malleable C2 profile 一致
```

```bash
pip install dissect.cobaltstrike
python3 -c "
from dissect.cobaltstrike import BeaconConfig
cfg = BeaconConfig.from_path('beacon.bin', all_xor_keys=True)
print(cfg)
"
```

输出包含 C2 地址、sleep、jitter、URI 路径、User-Agent、watermark。**Malleable C2 只改 HTTP 外观，不改 beacon 的二进制结构**，所以从抓到的载荷里仍能提出完整配置。

### 8.3 DarkComet

```text
键盘记录：%APPDATA%/dclogs/YYYY-MM-DD-N.dc（明文，含窗口标题）
默认端口：1604
互斥体：通常含 "DarkComet" 或自定义串
持久化：HKCU\Software\Microsoft\Windows\CurrentVersion\Run
进程：常注入 svchost.exe / explorer.exe
```

```bash
vol -f memory.dmp windows.filescan | grep -i dclogs
vol -f memory.dmp windows.dumpfiles --dump-dir=output -Q <physical_address>
vol -f memory.dmp windows.registry.printkey --key "Software\Microsoft\Windows\CurrentVersion\Run"
vol -f memory.dmp windows.netscan | grep 1604
```

`.dc` 文件是**明文**，dump 出来直接读，不用解密。

## 9. 非 HTTP C2 的中间人

IRC、自定义 TCP 这类协议，`mitmproxy --mode transparent` 的默认配置不认。用 ARP 欺骗 + 伪造 RST 让客户端重新握手：

```bash
# 1. 毒化 LAN，让受害者的流量经过我们
sudo arpspoof -i tap0 -r -t 172.30.0.2 172.30.0.1

# 2. 把隐藏网段路由到被欺骗的网关
sudo route add -net 172.30.20.0/28 gw 172.30.0.1 dev tap0
```

抓到的是**会话中段**，看不到最初的 `PASS`。用 scapy 伪造 RST 打断现有连接，逼客户端重连：

```python
from scapy.all import sniff, send, IP, TCP

VICTIM, SERVER = "172.30.0.2", "172.30.20.10"

def kill(p):
    if p.haslayer(TCP) and p[IP].src == VICTIM and p[TCP].dport == 6667:
        rst = IP(src=SERVER, dst=VICTIM) / TCP(
            sport=6667, dport=p[TCP].sport,
            seq=p[TCP].ack, flags="R")
        send(rst, verbose=0)

sniff(iface="tap0", filter=f"host {SERVER} and tcp port 6667", prn=kill)
```

重连后的流里就能看到 `PASS underling`、`JOIN #void`。**不需要破解认证，只要逼客户端在你面前再认证一次。**

需要在线改包（不只是旁听）时，改 mitmproxy 的 `rawtcp.py`，在 TCP 中继里加 `buf = buf.replace(b'old', b'new')`，就能对任意协议做透明篡改。

## 10. Telegram Bot 取证

样本用 bot 做外带时，拿到 token 就能直接取回历史数据：

```python
import requests

TOKEN = "bot_token_here"
r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates")

file_id = "..."
r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file_id}")
file_path = r.json()["result"]["file_path"]
requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}")
```

要点：`getUpdates` 只返回**最近 24 小时**的更新，且被 `getUpdates` 取过的会失效。如果 token 还有效，尽快拉。

## 11. 网络指标速取

```bash
# IP
strings malware | grep -E '([0-9]{1,3}\.){3}[0-9]{1,3}'

# 域名
strings malware | grep -E '[a-zA-Z0-9.-]+\.(com|net|org|io|xyz|top)'

# URL
strings malware | grep -iE 'https?://|ws://|wss://'

# DNS 查询全量
tshark -r capture.pcap -Y "dns.qry.name" -T fields -e dns.qry.name | sort -u
```

## 12. 纪律

- **先统计再细看**。`io,phs` 和 `conv,tcp` 两条命令能省掉大半肉眼翻包的时间。
- **流密码必须双向按时间拼接**。单方向解密从第二段开始就是乱码，这是最高频的失败原因。
- **加密流量先认算法，再找密钥**。按魔数认比读反汇编快一个数量级。
- **DNS 隧道要带时间戳排序**。`sort -u` 会毁掉拼接顺序，解出来是乱序数据。
- **AES 先试「MD5(硬编码) + IV 取密文前 16 字节」**，这两个默认假设命中率很高。
- **只解第一条验证，别批量试**。密钥错了批量跑一遍纯浪费。
- **非 HTTP C2 不要指望 mitmproxy 默认模式**。ARP + RST 是最通用的入口。
- **抓到的是会话中段时，逼重连比猜协议快**。你不需要破解认证，只需要它再认证一次。
