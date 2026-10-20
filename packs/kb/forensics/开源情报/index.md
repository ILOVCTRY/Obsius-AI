---
name: ctf-osint
description: 开源情报题总入口。用于从公开来源拼出答案——社交媒体账号追踪、地理定位、DNS/WHOIS 溯源、用户名枚举、反向图搜、Google dork、Wayback 存档、Tor 中继、FEC 政治献金、未知哈希与坐标格式识别。判据是先分清线索是「一个字符串」「一张图」还是「一个账号」。
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for OSINT lookups.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# 开源情报总入口

三册分工：**图/坐标** → 地理定位与媒体；**账号/帖子** → 社交媒体；**域名/IP/仓库** → Web 与 DNS。

- [[forensics/开源情报/geolocation-and-media.md|地理定位与媒体]] —— 反向图搜、裁切区域搜、镜像文字、路牌与建筑判国、MGRS / Plus Code / What3Words、Street View 全景匹配、Overpass 空间查询、EXIF
- [[forensics/开源情报/social-media.md|社交媒体]] —— Twitter/X User ID 与 Snowflake、Tumblr、BlueSky 公开 API、Discord、用户名枚举、同形字隐写、Strava、游戏平台角色查询
- [[forensics/开源情报/web-and-dns.md|Web 与 DNS]] —— Google dork、Docs/Sheets 导出、TXT/AXFR、WHOIS 反查与历史、Wayback、GitHub 评论、Telegram bot、Shodan 指纹、假服务 banner

## 0. 判据速查

| 手上有什么 | 结论 | 去哪 |
|---|---|---|
| 一张照片/截图 | 先 EXIF，再反向图搜，最后地形与招牌 | 地理定位 |
| 图里有文字但左右反了 | 水面/玻璃镜像，水平翻转再搜 | 地理定位 |
| 全景图但不确定哪段街 | 特征点 + 多指标相似度排序 | 地理定位 |
| 一串坐标/网格码 | 认格式：MGRS / Plus Code / W3W | §4 |
| 一个用户名 | 多站枚举，注意平台假阳性 | 社交媒体 |
| 一个数字 ID | Twitter Snowflake 可解出注册时间 | §3 |
| 一条帖子含异常 Unicode | 同形字隐写，ASCII=0、异体=1 | 社交媒体 |
| 一个域名/IP | TXT、AXFR、WHOIS 反查、Shodan | Web 与 DNS |
| 要「以前的版本」 | Wayback CDX / IXFR / 历史 WHOIS | §6 |
| 端口开着但服务不像 | 别信端口号，指纹识别 banner | Web 与 DNS |
| 40/32/64 位十六进制串 | SHA-1 / MD5 / SHA-256 | §2 |

## 1. 环境

```bash
pip install shodan Pillow
apt install whois dnsutils nmap libimage-exiftool-perl imagemagick curl   # Debian
brew install whois bind nmap exiftool imagemagick curl                    # macOS
```

## 2. 字符串形态识别

| 形态 | 是什么 |
|---|---|
| 40 hex | SHA-1（也常是 Tor relay fingerprint） |
| 64 hex | SHA-256 |
| 32 hex | MD5 |
| 27-28 位大写字母数字（无 `0OIL`） | Plus Code |
| `XXXX+XXX` 短码 | Plus Code 局部码，需附近地名补全 |
| 4V FH 246 677 这种「区带+方格+偏移」 | MGRS |
| 三个英文单词 | What3Words |

## 3. ID 与时间戳数学

- **Twitter/X Snowflake**：`(id >> 22) + 1288834974657` = Unix 毫秒。注册时间能直接定位账号创建批次。
- **持久 User ID**：`https://x.com/i/user/<id>` 改名后仍然有效，是账号改名追踪的锚点。
- **Discord Snowflake** 同构但纪元不同（`1420070400000`），别混用。

## 4. 坐标格式互转

- **MGRS**：`4V FH 246 677` → 在线转换器 → 经纬度 → Google Maps。
- **Plus Code**：字母表 `23456789CFGHJMPQRVWX`（去掉易混的 `0OILU`）。Google Maps 上落一个 pin，详情里直接给 Plus Code，免费无 key。
- **What3Words**：三词 → 坐标需要 API key，CTF 里通常反向做（有坐标去网站查词）。
- **判据**：短码缺地名时算不出唯一位置——这是 Plus Code 局部码的正常行为，不是解析错。

## 5. 元数据

```bash
exiftool image.jpg
pdfinfo document.pdf
mediainfo video.mp4
identify -verbose image.jpg | head -30
```

**关键坑：主流社交平台会剥离 EXIF**。从 Twitter/微博下载的图基本查不到拍摄信息，此时 EXIF 缺失本身不是「被刻意清除」，别在这上面浪费时间，直接转反向图搜。

## 6. 存档与历史

```bash
curl "https://web.archive.org/web/20230101*/target.com"   # 某时间点所有快照
curl "http://archive.org/wayback/available?url=target.com&timestamp=20200101"
```

- **Wayback CDX API** 能列全部归档 URL，比逐页翻快。
- **历史 WHOIS**：隐私保护启用前的注册人信息在 SecurityTrails / WhoisXML / DomainTools，普通 whois 查不到。
- **反向 WHOIS**：按注册邮箱/组织/电话找同一实体的所有域名。

## 7. 账号与平台追踪

- **用户名枚举**：[whatsmyname.app](https://whatsmyname.app)（741+ 站）、[namechk.com](https://namechk.com)。**必须人工复核命中**——大量站点对任意用户名都返回 200。
- **游戏平台**：Steam `steamcommunity.com/id/<user>`、SteamID 用 steamid.io、Minecraft 用 NameMC（含改名历史）、WoW 用 Raider.IO / WoWProgress。
- **BlueSky**：`public.api.bsky.app/xrpc/` 下 `app.bsky.feed.searchPosts`、`app.bsky.actor.searchActors`、`app.bsky.feed.getAuthorFeed`，**无需鉴权**。官方号的回复区常是藏 flag 的地方。
- **GitHub**：`gh api` 查 issue 评论、PR review、commit message、wiki 编辑历史——这些是「代码之外」的信息面。

## 8. 什么时候换方向

- 文件/流量已经在本地，要提取或雕刻 → [[forensics/磁盘与内存/disk-and-memory.md|磁盘与内存取证]]
- 变成对活 HTTP 服务的实际利用 → [[web/README.md|Web]]
- 溯源过程中拿到恶意样本/beacon → [[binary/逆向工程/index.md|逆向工程]]

## 9. 纪律

- **先定线索类型再选工具**。字符串题查格式表，图题走图流程，账号题走平台——三者的工具集几乎不重叠，混着试最慢。
- **平台假阳性是最大时间黑洞**。用户名「在 50 个站点都注册了」通常意味着枚举器把「页面存在」当成了「用户存在」，逐个人工确认。
- **别信端口号**。22 端口不一定是 SSH，`nmap -sV` 或 `nc` 看一眼 banner，flag 经常就写在 banner 里。
- **存档查不到不等于不存在**。CDX 只索引被爬过的 URL，冷门路径要直接猜快照时间戳。
