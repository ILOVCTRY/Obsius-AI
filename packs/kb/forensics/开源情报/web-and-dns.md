---
title: Web 与 DNS 情报
phase: forensics
vuln_class: [osint, dns, whois, shodan, wayback, git]
summary: 域名题的判据是「信息在注册层还是在服务层」——注册层查 WHOIS/历史 WHOIS/反查，服务层查 TXT/AXFR/指纹/存档。端口号不能当服务类型用。
---

# Web 与 DNS 情报

图与坐标见 [[forensics/开源情报/geolocation-and-media.md|地理定位与媒体]]；账号与帖子见 [[forensics/开源情报/social-media.md|社交媒体]]；总入口见 [[forensics/开源情报/index.md|开源情报]]。

DNS 协议异常利用（ECS 伪造、NSEC walking、IXFR、隧道、rebinding、TFO）见 [[misc/协议异常/dns.md|协议异常与 DNS 利用]]。

## 0. 判据速查

| 观察到的现象 | 结论 | 去哪节 |
|---|---|---|
| 只给了域名 | 先 TXT/CNAME/MX，flag 常在子域 TXT | §2 |
| TXT 什么都没有 | 试 AXFR、看 DMARC/SPF 记录里的 IP | §2 |
| 要给域名找人 | 反查 WHOIS（同邮箱/同组织） | §4 |
| 隐私保护挡了注册人 | 历史 WHOIS（隐私启用前的快照） | §4 |
| 要「以前的版本」 | Wayback CDX 列全部归档 URL | §5 |
| 端口开着但服务可疑 | `nmap -sV` 或 `nc` 读 banner | §6 |
| 要给 Tor 隐藏服务去匿名 | SSH host key 指纹反查 Shodan | §7 |
| 只有用户名没有口令 | 挖公开仓库的 author email | §8 |
| 站点目录被藏起来 | 抓 `.DS_Store` 拿文件名 | §9 |
| 要人脸照片 | Google 图片 `tbs=itp:face` 过滤 | §1 |
| 拿到 TTF 但字符是私用区码位 | 字形轮廓比对，与 cmap 无关 | §10 |

## 1. Google dork 与图片过滤

```text
site:example.com filetype:pdf
intitle:"index of" password
inurl:admin
"confidential" filetype:doc
```

图片搜索的 `tbs` 过滤参数（追加到 URL）：

| 目的 | 参数 |
|---|---|
| 只要人脸 | `itp:face` |
| 剪贴画/图标 | `itp:clipart` |
| 动图 | `itp:animated` |
| 指定主色 | `ic:specific,isc:green` |
| 透明背景 | `ic:trans` |
| 大图 | `isz:l` |
| 最小分辨率 | `isz:lt,islt:2mp` |

`itp:face` 的价值在于**剔掉 logo、banner、UI 截图**，只留人像。配合 `site:` 与 `after:YYYY-MM-DD` 就是定向人脸检索。

## 2. DNS 记录

**flag 常在子域的 TXT，不在根域**：

```bash
dig -t txt subdomain.ctf.domain.com
dig -t any domain.com
dig -t txt _dmarc.domain.org
dig axfr @ns.domain.com domain.com
```

**顺序**：TXT → CNAME → MX → `_dmarc` / `_spf`（SPF 记录里能读出邮件基础设施与授权 IP）→ AXFR。前几个都空再考虑子域爆破。

## 3. Google Docs / Sheets

嫌疑人常把表格链接发在社交帖里。公开分享状态下这些路径直接可读：

| 路径 | 作用 |
|---|---|
| `/export?format=csv` | 导出 CSV |
| `/pub` | 已发布版本 |
| `/gviz/tq?tqx=out:csv` | Visualization API 的 CSV 出口 |
| `/htmlview` | 纯 HTML 视图 |

**Sheet ID 是稳定标识**：即使分享权限改了，ID 不变，可以拿去 Wayback 找旧的可读快照。

## 4. WHOIS 三层

```bash
whois example.com          # 当前注册信息
whois 1.2.3.4              # IP 归属：看 NetName / OrgName / CIDR / abuse
whois -h whois.radb.net AS12345   # ASN；或 https://bgp.tools/as/12345
```

- **当前 WHOIS**：注册人（多被隐私服务遮蔽）、创建/到期时间（可与事件时间线对齐）、NS（判断是否共享主机）、注册商（能看出投入程度）。
- **历史 WHOIS**：隐私保护启用**之前**的注册人信息在 SecurityTrails / WhoisXML / DomainTools。
- **反向 WHOIS**：按注册邮箱/组织/电话找同一实体的所有域名——这是把孤立域名连成基础设施图的关键一步。

```bash
curl "https://api.securitytrails.com/v1/domain/example.com/whois" -H "APIKEY: YOUR_KEY"
curl "https://reverse-whois-api.whoisxmlapi.com/api/v2" \
  -d '{"searchType":"current","mode":"purchase","basicSearchTerms":{"include":["target@email.com"]}}'
```

## 5. Wayback

```bash
curl "http://web.archive.org/cdx/search/cdx?url=example.com*&output=json&fl=timestamp,original,statuscode"
```

**CDX 是索引不是内容**：先列出所有被归档的 URL，再按需取具体快照。冷门路径可能只被归档过一两次，直接猜时间戳（`/web/20230101000000*/URL`）比逐页翻快。

## 6. 假服务 banner

**判据：SYN 扫描只能证明端口开着，不能证明服务是什么。**

```bash
nmap -sS target.ctf            # 22/tcp open ssh —— 仅此而已
nmap -sV -sC target.ctf -p 22  # banner 里可能直接写着 flag
nc target.ctf 22               # 最直接
echo "" | timeout 3 nc -w 3 target.ctf 22
```

题面提示 scan / SYN / port 时，正确解法常常是「枚举端口，然后读 banner」，根本不需要利用。TLS 包裹的服务用 `openssl s_client -connect host:port`。

## 7. 指纹反查去匿名

SSH host key 每台服务器唯一，拿指纹反查 Shodan/Censys 就能定位真实 IP——对 Tor 隐藏服务、CloudFlare 后面的源站同样有效。

```bash
ssh-keyscan -t rsa target.onion 2>/dev/null | ssh-keygen -lf - -E md5
# 或 ssh-audit target.onion
shodan search 'ssh.fingerprint:"ab:cd:ef:..."'
```

```python
import shodan
api = shodan.Shodan('YOUR_API_KEY')
for r in api.search('ssh.fingerprint:"ab:cd:ef:..."')['matches']:
    print(r['ip_str'], r['port'], r['data'][:200])
```

TLS 证书同理：`shodan search 'ssl.cert.fingerprint:"SHA256_HASH"'`。**先取 SHA-256 再取 MD5**，Shodan 两个字段都索引但 SHA-256 命中率更高。

## 8. 从公开仓库挖身份

git 历史是一份签名审计日志：作者、提交者、共同作者都在里面，改邮箱之后**旧地址仍留在历史里**。

```bash
git clone https://github.com/<user>/<repo>.git && cd repo
git shortlog -sne
git log --format="%an <%ae>%n%cn <%ce>" | sort -u
gh api "users/<user>/events/public" --paginate | jq -r '.[] | .payload.commits[]?.author.email' | sort -u
```

**同时看 `.mailmap`、`CONTRIBUTORS`、`git log --show-signature`（GPG 签名里的 UID 常是真名）**。每个捞到的邮箱都当作目标服务的候选登录名——很多密码重置流程就认它。

代码之外的三个面也要查：issue 评论、PR review、wiki 编辑历史（`gh api repos/OWNER/REPO/issues/comments`）。

## 9. `.DS_Store` 泄露目录

macOS 自动生成，经常被误传到生产环境。它只暴露**文件名**，但那已经足够找到隐藏面板和备份文件。

```bash
curl -sO https://target/.DS_Store
python3 -m dsstore .DS_Store
```

根目录不中时试 `/uploads/`、`/static/`、`/assets/`——`.DS_Store` 是按目录生成的，子目录里的那份往往更有价值。

## 10. 自定义字体 CAPTCHA

字体把 glyph ID 重映射到随机 `cmap`，浏览器照样显示「5」，但底层码位是 `U+E042`。**字形轮廓不受 cmap 影响**，所以比对轮廓就能还原真字符。

```bash
ttx -t glyf -g -d glyph_out font.ttf
diff glyph_out/font.glyf/zero.ttx reference/zero.ttx
```

参考库用任意标准字体建一次即可，跨题复用。

## 11. 跨题基础设施复用

多题共用一套 Docker 网络时，从**最弱的那题**漏出的 `REMOTE_ADDR` / 路由表可以喂给别的题——尤其是按 `md5(IP)` 生成上传路径、或校验 `X-Forwarded-For` 的题。

```text
A 题泄漏 REMOTE_ADDR = 10.0.10.254
B 题期望上传到 /uploads/md5(10.0.10.254)/md5(time()).ext
```

**先画共享子网，再从弱题往强题搬信息**。

## 12. 纪律

- **区分注册层与服务层**。WHOIS 答「谁注册的」，DNS/TXT/指纹答「上面跑什么」，两层的工具集不重叠。
- **别信端口号**。22 不一定是 SSH，80 不一定是 HTTP。`-sV` 或 `nc` 的成本极低，漏掉 banner 里的 flag 最亏。
- **先 CDX 再取快照**。不知道有哪些归档 URL 就开始翻页，等于随机漫步。
- **历史数据要专门去查**。当前 WHOIS 与当前页面都可能是「已清洗过」的版本，价值常在 Wayback 与历史 WHOIS 里。
