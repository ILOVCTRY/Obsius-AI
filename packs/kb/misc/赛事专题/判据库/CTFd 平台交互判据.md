---
title: CTFd 平台交互判据
phase: misc
vuln_class: [platform, automation]
summary: 赛题平台是 CTFd 时，浏览器不是唯一入口。本文给「无浏览器/批量/脚本化」场景下的 REST API 交互判据：指纹识别、Token 认证、题目清单与附件下载、flag 提交与限速、排行榜与提示解锁，以及 403/429/空列表等异常的成因分流。
---

# CTFd 平台交互判据

> 来源：`CTFd Platform Navigation (No Browser)` 英文长文的判据化重构（2026-10-21）。
> 定位：**平台侧操作判据**，不涉及具体题型解法（解法在各域手册）。
> 典型触发：桌面壳/无头环境没有浏览器、要批量拉题目附件、或要把提交动作编进自动化脚本。

## 0. 判据速查

| 现象 / 需求 | 判据 | 去哪节 |
|---|---|---|
| 不知道平台是不是 CTFd | `/api/v1/` 返回 Swagger/RESTX 文档，或 HTML 含 `/themes/core/`、登录表单有 `nonce` 隐藏域 | §1 |
| 没有平台凭证 | API Token 只能由本人从 Web UI（Settings > Access Tokens）生成，**先问用户要** | §2.1 |
| 有 Cookie 没有 Token | 带 session POST `/api/v1/tokens` 自签一个 | §2.1 |
| 表单登录卡住 / `nonce` 取不到 | 反爬或页面改版，放弃 session 流，改走 Token | §2.2 |
| 附件下载 403 | 文件 URL 的 `?token=` 过期，重新拉题目详情拿新 URL | §3.2 |
| 提交返回 `ratelimited` | 默认 10 次/分钟错误提交上限，退避等 60s | §4 |
| 题目列表为空 | 比赛未开始或账号未验证邮箱，先查 `/users/me` 的 `verified` | §5.1 |
| 连接信息不在题面里 | 动态实例型平台，另找 instance API / Docker 端点 | §5.2 |
| 排行榜数字对不上 | 服务端缓存 60 秒，不是漏算 | §6 |

## 1. 识别 CTFd 平台

**判据**：命中下列任一条即可判定为 CTFd，后续全走 `/api/v1/`：

| 探针 | 命中形态 |
|---|---|
| `curl -sI "$URL" \| grep -i 'ctfd\|powered-by'` | `Server` / `X-Powered-By` 含 `CTFd` |
| `curl -s "$URL/api/v1/"` | 返回 Swagger UI 或 RESTX JSON 文档 |
| `curl -s "$URL" \| grep -oE '(ctfd\|/themes/core)'` | 静态资源路径含 `/themes/core/` |
| `curl -s "$URL/login" \| grep -oE 'name="nonce"'` | 登录表单带 `nonce` 隐藏域（CSRF） |

**动作**：判定后设 `CTF_URL` 与 `CTF_TOKEN` 两个环境变量，全部后续操作用 API，不再解析 HTML。

**不成立**：只命中 `/themes/core/` 但 `/api/v1/` 404——平台改过主题或反代拦截了 API 路径，此时退回 HTML 解析，别硬套本手册接口。

## 2. 认证

### 2.1 API Token（首选）

**判据**：Token 形如 `ctfd_<32 位十六进制>`，只能由用户本人在 Web UI 的 **Settings > Access Tokens** 页面生成；脚本无法凭空获得。

**动作**：
1. 直接问用户要 Token（这是唯一正道，不要尝试爆破或猜解）；
2. 用户没有时，给指引：浏览器登录 CTFd → Settings → Access Tokens → 创建 → 粘贴回来；
3. 已有 session cookie 时代办自签：

```bash
curl -s -X POST "$CTF_URL/api/v1/tokens" \
  -H "Content-Type: application/json" -b cookies.txt \
  -d '{"expiration": "2026-12-31", "description": "CLI access"}' | jq .
```

4. 之后每个请求都带 `-H "Authorization: Token $CTF_TOKEN"`；
5. 自检：`curl -s -H "Authorization: Token $CTF_TOKEN" "$CTF_URL/api/v1/users/me"` 返回自己的账号信息即通。

**不成立**：返回 401——Token 过期或写错；让用户重新生成，不要在脚本里重试同一个 Token。

### 2.2 Session 登录（Cookie 流，备选）

**判据**：只有在拿不到 Token 时才走；登录 POST 必须带 `nonce`，它是 CSRF 令牌，每次会话变化。

**动作**：三步——先从 `/login` 页面正则抠出 `nonce` 的 value，再 POST `name/password/nonce` 存 cookie，最后用 cookie 调 API：

```bash
NONCE=$(curl -sc cookies.txt "$CTF_URL/login" | grep 'name="nonce"' | grep -oE 'value="[^"]*"' | cut -d'"' -f2)
curl -sb cookies.txt -c cookies.txt -X POST "$CTF_URL/login" \
  -d "name=username&password=password&nonce=$NONCE" -L -o /dev/null -w '%{http_code}'
```

**不成立**：`nonce` 抠不到——登录页改版或有反爬/JS 挑战，**不要**继续在正则上较劲，转 §2.1 问用户要 Token。

## 3. 题目清单、详情与附件

### 3.1 清单与详情

**判据**：`/api/v1/challenges` 返回 `{success, data[]}`，每条含 `id/type/name/value/solves/solved_by_me/category/tags`；详情在 `/api/v1/challenges/$ID`。

**动作**：
- 按类目过滤加 `?category=web`；
- 未解题目筛 `solved_by_me == false`，按 `value` 降序排就是性价比队列；
- 题面是 HTML，管道过 `sed 's/<[^>]*>//g'` 再读；
- 连接信息（`nc`/`ssh`/`http`）直接从 description 里 `grep -oE '(nc |ssh |https?://)[^ <"]+'` 抠，比肉眼翻快。

**不成立**：`data` 为空但比赛已开始——先看 §5.1；返回 403——看 §5.1 的 `verified` 分支。

### 3.2 附件下载

**判据**：题目详情的 `files[]` 是**相对路径**且带签名查询串（`/files/xxx?token=...`），Token 有时效。

**动作**：路径前拼 `CTF_URL` 再下载；文件名要先 `basename | sed 's/?.*//'` 去掉查询串，否则落盘文件名里带一长串 token：

```bash
for f in $(curl -s -H "Authorization: Token $CTF_TOKEN" "$CTF_URL/api/v1/challenges/$ID" | jq -r '.data.files[]'); do
  curl -s -H "Authorization: Token $CTF_TOKEN" -o "chall_$ID/$(basename "$f" | sed 's/?.*//')" "${CTF_URL}${f}"
done
```

**不成立**：下载 403——**不是**权限问题，是 URL 里的 `?token=` 过期了；重新拉一次题目详情拿新 URL，不要反复重试同一个旧 URL。

## 4. 提交 flag

**判据**：POST `/api/v1/challenges/attempt`，body 为 `{"challenge_id": <id>, "submission": "<flag>"}`；返回 `data.status` 五种取值：

| `status` | 含义 | 处置 |
|---|---|---|
| `correct` | 通过 | 记录并进入下一题 |
| `incorrect` | 错误 | 检查 flag 格式与大小写再试 |
| `already_solved` | 本人/本队已解 | 不用再提交 |
| `ratelimited` | 触发限速 | 停手，等 60s |
| `paused` | 比赛暂停中 | 等待恢复 |

**动作**：批量/爆破提交时必须**主动限速**——默认上限 10 次错误提交/分钟/用户，超了临时锁死。

**不成立**：显示 `incorrect` 但本地自检 flag 正确——先确认提交的是**该题**的 flag（`challenge_id` 串题是自动化脚本的高频错），再怀疑平台侧。

## 5. 异常分流

| 症状 | 根因 | 动作 |
|---|---|---|
| 401 Unauthorized | Token 过期/无效 | 让用户重新生成 |
| 403 下载附件 | 文件 URL token 过期 | 重拉题目详情（§3.2） |
| 403 看题目 | 比赛未开始 或 邮箱未验证 | 查 `/users/me` 的 `verified` 字段 |
| 429 Rate Limited | 错误提交超 10 次/分 | 等 60s，之后加限速 |
| 题目列表为空 | 比赛未开始 | 看通知里的开赛时间 |
| `nonce` 取不到 | 登录页改版/反爬 | 改用 API Token（§2.1） |
| 连接信息不在 API | 动态实例型平台 | 找题目专属 instance API 或 Docker 端点 |

**判据**：先按 HTTP 状态码分（401/403/429），再按业务字段分（`verified`/空列表/`ratelimited`）——两类根因完全不同，混排会查错方向。

**动作**：按上表「症状 → 根因 → 动作」逐行对；403 要先分清是**下载附件**（token 过期，重拉详情即可）还是**看题目**（账号未验证/未开赛，重试无用），两者动作相反。

**不成立**：以上全排查完仍不通——平台可能反代拦截了 API 或用了非标准路径，抓包看真实端点，别继续按本手册猜。

## 6. 排行榜与辅助端点

**判据**：
- 排行榜 `/api/v1/scoreboard`（全量）与 `/api/v1/scoreboard/top/N`，**服务端缓存 60 秒**——刚提交成功立刻刷看不到自己涨分是正常的，不是接口坏了；
- 提示：详情里的 `hints[]` 列出提示，`POST /api/v1/unlocks`（`{"target": <hint_id>, "type": "hints"}`）解锁付费提示，**扣分**，解锁前先问用户值不值；
- 通知 `/api/v1/notifications` 支持 `?since_id=` 增量轮询，HEAD 请求的 `X-Total` 头给未读条数——开赛后公告（规则变更、题目修复）从这里看，比刷页快；
- 个人信息 `/users/me`、`/users/me/solves`、`/users/me/fails`；队伍模式另有 `/teams/me` 与 `/teams/$ID/solves`。

**动作**：把「我的解题记录」和「失败记录」拉下来对账，能快速发现「以为解了其实提交错了题」。

**不成立**：`fails` 里有自己没印象的提交——多人共用账号/队伍，先确认再下结论。

## 7. 脚本化封装

**判据**：同一套动作（列表→详情→下载→提交）要重复跑时，封一个最小 client 比每次拼 curl 可靠；核心只有 `_get/_post` 两个方法加 `Authorization: Token` 头。

**动作**：最小可用实现（其余端点按需加）：

```python
class CTFdClient:
    def __init__(self, url, token):
        self.url = url.rstrip('/')
        self.s = requests.Session()
        self.s.headers['Authorization'] = f'Token {token}'

    def _get(self, path, **kw):
        r = self.s.get(f'{self.url}/api/v1{path}', **kw)
        r.raise_for_status()
        return r.json()

    def _post(self, path, json=None):
        r = self.s.post(f'{self.url}/api/v1{path}', json=json)
        r.raise_for_status()
        return r.json()

    def challenges(self, category=None):
        params = {'category': category} if category else {}
        return self._get('/challenges', params=params)['data']

    def unsolved(self):
        return [c for c in self.challenges() if not c.get('solved_by_me')]

    def download_files(self, cid, dest='.'):
        info = self._get(f'/challenges/{cid}')['data']
        dest = Path(dest); dest.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in info.get('files', []):
            url = f'{self.url}{f}' if f.startswith('/') else f
            fname = re.sub(r'\?.*', '', f.split('/')[-1])
            out = dest / fname
            out.write_bytes(self.s.get(url).content)
            paths.append(str(out))
        return paths

    def submit(self, cid, flag):
        d = self._post('/challenges/attempt',
                       json={'challenge_id': cid, 'submission': flag})['data']
        return d['status'], d['message']
```

**不成立**：`raise_for_status()` 把 403/429 直接抛异常打断批量循环——提交类批量操作要按 §4 的状态表分流处理，`ratelimited` 应当作「等待」而非「失败」。
