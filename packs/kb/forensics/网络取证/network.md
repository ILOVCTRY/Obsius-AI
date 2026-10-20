---
title: 网络流量取证与协议隐写
phase: forensics
vuln_class: [pcap, tcpdump, wireshark, tls, usb-hid, covert-channel, exfiltration]
summary: 网络取证先按流量层分流：文件对象、明文协议、加密握手、设备总线和隐蔽信道分别处理；不要把“看见 TCP”误当成“答案在 TCP payload”。
---

# 网络流量取证与协议隐写

网络包分析的目标是恢复证据链：先确认封装和会话，再提取对象，最后判断编码、加密或信道。TLS 私钥、内存转储与弱 RSA 见 [[forensics/系统取证/linux-forensics.md|Linux 系统取证]]；DNS 语义、隧道和 rebinding 见 [[misc/协议异常/dns.md|DNS 协议异常]]；USB/磁盘镜像见 [[forensics/磁盘与内存/disk-and-memory.md|磁盘与内存取证]]。

## 0. 判据速查

| 现象 | 优先动作 | 误区 |
|---|---|---|
| 有 `.pcap/.pcapng`，未知内容 | `capinfos`、协议统计、会话统计 | 一上来只搜 `flag` |
| HTTP/FTP/SMB 有文件传输 | Export Objects 或 `--export-objects` | 只看 TCP 文本，漏掉二进制对象 |
| TCP 流只有乱码 | 先看 TLS/自定义编码/压缩，再看密钥 | 乱码不等于加密 |
| ClientHello 后出现可用 keylog | 导入 NSS Key Log | 用服务器私钥处理 ECDHE |
| 大量 UDP/ICMP、长度呈规律变化 | 按包序、方向、长度重组 | 只看 payload，不看元数据 |
| USB interrupt 中一包多键 | 解析 HID report，考虑 chord/stegano | 按普通键盘逐键翻译 |
| SYN-ACK 集中返回多个端口 | 统计源/目的和 TCP flags | 把扫描器自身端口当服务端口 |
| 同一数据有多个流/方向 | 按 stream、时间、五元组分别导出 | 直接拼全包导致顺序污染 |

## 1. 起手：确认捕获范围

```bash
capinfos capture.pcap
file capture.pcap
tshark -r capture.pcap -q -z io,phs
tshark -r capture.pcap -q -z conv,ip
tshark -r capture.pcap -q -z conv,tcp
```

快速过滤和查看：

```bash
tcpdump -r capture.pcap -nn -tttt
 tcpdump -r capture.pcap -A | grep -aiE 'flag|password|token'
tshark -r capture.pcap -Y 'tcp.stream == 5' -T fields \
  -e frame.number -e ip.src -e ip.dst -e tcp.seq -e tcp.len
```

Wireshark 常用过滤器：

```text
frame contains "flag"
tcp.stream eq 5
http.request.method == "POST"
tcp.flags.syn == 1 && tcp.flags.ack == 1
udp.length > 100
```

确认 `pcapng` 的 interface、linktype 与时间戳；不同链路类型不能直接把二层头当成 Ethernet。必要时先用 `editcap` 切分，再逐流处理。

## 2. 对象优先：HTTP、FTP、SMB

有明确应用层协议时，先导出对象，再回到流中核对上下文：

```bash
tshark -r capture.pcap --export-objects http,./http_objects
tshark -r capture.pcap --export-objects smb,./smb_objects
tshark -r capture.pcap -Y 'http.request' -T fields \
  -e frame.number -e http.request.method -e http.host -e http.request.uri
```

Multipart 上传通常表现为 `POST`、`Content-Type: multipart/form-data`、边界字符串和较大的 request body。检查导出的文件的 magic、压缩层和图片文本；文件名、User-Agent、上传端点可能只是线索，不能直接当结论。

从 TCP 流导出时必须使用双向重组：

```bash
tshark -r capture.pcap -q -z 'follow,tcp,ascii,5'
tshark -r capture.pcap -q -z 'follow,tcp,raw,5' > stream5.bin
```

若 TCP 有重传、乱序或截断，按 `tcp.seq` 去重后再拼接；只按抓包行号拼接会产生伪文件。

## 3. TLS：先找钥匙，再判断套件

客户端 keylog 是最可靠入口：

```bash
export SSLKEYLOGFILE=/tmp/sslkeys.log
curl https://target/secret
# Wireshark: Preferences -> Protocols -> TLS -> (Pre)-Master-Secret log filename
```

NSS key log 行形如：

```text
CLIENT_RANDOM <client_random_hex> <master_secret_hex>
```

也可用 tshark：

```bash
tshark -r capture.pcap \
  -o 'tls.keylog_file:/tmp/sslkeys.log' -Y http
```

服务器 RSA 私钥仅适用于 RSA key exchange；`ECDHE`/`DHE` 使用 forward secrecy，单有服务器私钥不能解密。确认 ClientHello/ServerHello 的 cipher suite 后再选方法。若证书模数很小，提取 modulus 并因式分解，再生成私钥：

```bash
tshark -r capture.pcap -Y 'tls.handshake.type == 11' \
  -T fields -e tls.handshake.certificate | head -1
python rsatool.py -p <p> -q <q> -e 65537 -o server.key
```

若 keylog 与流量时间、`client_random` 对不上，先检查是否导错文件或存在多个 TLS 会话；不要用“解出少量文本”证明整条流已正确解密。

## 4. 扫描、反连与凭据痕迹

开放端口通常由 SYN-ACK 判断：

```bash
tshark -r capture.pcap \
  -Y 'tcp.flags.syn==1 && tcp.flags.ack==1' \
  -T fields -e ip.src -e ip.dst -e tcp.srcport -e tcp.dstport | sort -u
```

反连或后渗透流常见 shell 提示、`no job control`、固定周期 beacon、出站连接和紧随其后的文件上传。先列 `conv,tcp`，再 follow 具体 stream，记录方向和时间。

凭据不能只搜 `password`：检查 HTTP 表单、Cookie、Authorization、`.env`、`wp-config.php`、`debian.cnf` 和 shell 输出。

```bash
tshark -r capture.pcap -q -z 'follow,tcp,ascii,<stream>' \
  | grep -aiE 'pass(word)?|token|cookie|authorization|secret'
```

WordPress 流量可按 `wp-content/plugins`、REST 用户接口、generator meta 和 WPScan User-Agent 分流；这些结果只用于枚举，不等于漏洞已存在。

## 5. SMB、Wi-Fi 与设备识别

SMB3 加密流量先判断是否能取得 NTLMv2 凭据材料：

```bash
tshark -r capture.pcap -Y 'ntlmssp.messagetype == 0x00000003' \
  -T fields -e ntlmssp.ntlmv2_response.ntproofstr \
  -e ntlmssp.auth.username
hashcat -m 5600 ntlmv2_hash.txt wordlist.txt
```

SMB 3.1.1 解密需要正确的密码、协商参数、preauth integrity hash 和方向密钥；不能只拿 NT hash 直接套 AES。不同实现的 transform header、nonce、tag 偏移必须按协议版本核对。

Wi-Fi 先区分 WEP、WPA/WPA2 四次握手和 WPA3；没有握手或 PMK 时，单个 beacon 不能还原密码。MAC OUI 只能说明注册厂商，不能单独证明网关角色：

```bash
tshark -r capture.pcap -Y arp -T fields \
  -e arp.src.hw_mac -e arp.src.proto_ipv4 | sort -u
```

## 6. 隐蔽信道：不要只读 payload

### 6.1 UDP/ICMP 长度、顺序和方向

```bash
tshark -r capture.pcap -Y 'icmp || udp' -T fields \
  -e frame.number -e ip.src -e ip.dst -e udp.length -e data.len
```

按五元组、方向和时间排序，分别测试：长度映射、包间隔、首字节状态、payload bit、DNS label 和丢包位置。若只有长度携带信息，不能把 padding 当数据；应先建立长度到符号的频数表，再验证重复序列与校验位。

### 6.2 BCD 与自定义编码

BCD 每个 nibble 表示一个十进制数字；先检查所有 nibble 是否落在 `0..9`，否则不要强行解码。保持 `bytes`，最后一步才转文本：

```python
def bcd_decode(data: bytes) -> str:
    out = []
    for b in data:
        hi, lo = b >> 4, b & 0xf
        if hi > 9 or lo > 9:
            raise ValueError("不是合法 BCD")
        out.extend((str(hi), str(lo)))
    return ''.join(out)
```

### 6.3 USB HID chord

USB interrupt report 中同一报告出现多枚非零 keycode，且持续表现为组合键，优先考虑 stenography/chord，而非普通输入：

```bash
tshark -r capture.pcap -Y 'usb.transfer_type == 1' \
  -T fields -e frame.number -e usb.capdata
```

保留每个 report 的时间和 modifier，先去重相同状态，再映射 Plover 等 stenography 字典。把每个 keycode 单独转 ASCII 会丢失组合语义。

## 7. 邮件、5G 与协议专用数据

邮件先看 `Received` 链、时区、Message-ID、MIME boundary 和附件编码；base64 解码后继续识别压缩和文件 magic。`From` 不是可信来源，路由链更有价值。

5G/NR 在 Wireshark 中启用 NAS-5GS、RLC-NR、PDCP-NR、MAC-NR 后，先确认层是否真正存在，再按 IEI 和长度解析。iMelody 等载荷可能把音符映射成 Morse，但必须验证重复节拍与分隔规则，不能看到字母 `c4` 就直接当点划线。

## 8. 重组、修复与证据保全

- 先复制原始 pcap，所有切分、去重和解码写到工作目录。
- `.pcap` 损坏时先尝试 `pcapfix`，再检查修复后包数与时间范围；修复工具可能丢包，不能覆盖原件。
- split archive 先按 TCP 序号和传输方向重组，再识别 `PK`、gzip 或 7z magic。
- 多个流同名文件要记录 stream、五元组和 sha256，避免后导出的对象互相覆盖。
- 结论至少保留过滤器、命令、输入文件 hash、输出 hash 和关键包号。

## 9. 纪律

先做协议统计和对象导出，再进入加密或隐写分析。看到乱码时依次排查：流方向、TCP 重组、压缩、编码、密钥和自定义信道。任何“成功解码”都要用长度、校验、重复样本或协议语义复核；题目名称、User-Agent 和文件名只能作为分流线索，不能替代证据。
