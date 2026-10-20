---
phase: webapp
vuln_class: [js-reverse, obfuscation, client-side, ctf-web]
title: 前端 JS 逆向与混淆还原（纯静态站点找 flag）
summary: 站点无后端接口、全站只有静态资源时，flag 通常藏在被混淆的前端 JS 里；本手册给适用条件、抓取步骤、obfuscator.io 指纹识别与离线解密复现路径
---

# 前端 JS 逆向与混淆还原

> 适用：CTF Web 题给一个 URL，首页是静态 HTML/游戏/工具页，**没有任何后端接口**。
> 这类题不是「打漏洞」，是「读被混淆的前端代码」。目录爆破和注入扫描在这里是空转。

## 适用条件（命中就走本手册，别再爆破）

- 首页 `Server:` 是 nginx/openresty/Apache，`Etag`/`Last-Modified` 是**静态文件特征**（如 `Etag: "7ad-5ccfc80d7bb40-gzip"`），没有 `Set-Cookie`、没有 `X-Powered-By`
- 目录扫描只有 `index.html` + `js/` + `src/` + `images/` + `favicon.ico`，且目录本身 403（禁列）
- 全站 JS 里 grep 不到 `XMLHttpRequest` / `fetch(` / `document.cookie` / `token` / `/api`
- 页面标题/内容是一个**游戏、计算器、登录壳、验证工具**（常见：HTML5 小游戏、Canvas 动画）
- 附件形态是「前端源码包」同理

> 判定成立后**立刻停止**注入/上传/爆破方向的投入，把全部时间花在读 JS。

## 步骤一：抓全前端资产（漏一个文件就可能漏 flag 的一半）

```bash
B=http://target
curl -sS "$B/" -o index.html
# 从 index.html 的 src=/href= 提取，再顺着 JS 里引用的资源递归一层
grep -oE '(src|href)="[^"]+"' index.html
# 逐个下载（保持目录结构，方便本地 grep）
for f in src/R.js src/Utils.js src/views/Player.js js/quark.base-1.0.0.alpha.min.js; do
  mkdir -p "$(dirname $f)"; curl -sS "$B/$f" -o "$f"
done
```

顺手看一眼静态站点的「额外文件」，成本极低：

| 路径 | 说明 |
|---|---|
| `/.DS_Store` | macOS 上传残留，能列目录名（本类题高频）；解析：提 UTF-16BE 串 |
| `/index.html.bak`、`/www.zip`、`/site.zip` | 源码包 |
| `/favicon.ico` | 查 PNG 尾部/额外 chunk 是否塞了数据 |
| `/images/*` | 二进制尾部、tEXt chunk（`after-IEND` 长度必须为 0） |

## 步骤二：定位混淆点（grep 指纹）

```bash
grep -rnoiE '_0x[0-9a-f]{4,}|atob\(|fromCharCode|eval\(|unescape|Function\(.return' --include=*.js .
grep -rnoiE 'innerHTML|alert\(|document\.write|location\.' --include=*.js .
```

`_0x3f85=['MxXCgw4=',...]` 这种「字符串数组 + 十六进制变量名」= **obfuscator.io（javascript-obfuscator）默认输出**。
它生成的解密器有三个固定特征，认出任何一个就不用真去跑浏览器：

1. 自实现 atob：
   `_0x53018a['atob']||(_0x53018a['atob']=function(...))`，内部用 `ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=` 查表
2. 取全局对象的骚操作：
   `Function('return (function() {}.constructor("return this")()')()`
3. 解密函数本体（本例是 RC4，标准 KSA+PRGA，密钥就是调用处的第二个实参）：
   ```js
   var _0x44b03c=function(_0x5781e3,_0x3dff02){ ...
     _0x5781e3=atob(_0x5781e3);
     for(...) _0x20a9f5+='%'+('00'+_0x5781e3['charCodeAt'](i)['toString'](0x10))['slice'](-0x2);
     _0x5781e3=decodeURIComponent(_0x20a9f5);   // ← 关键：先 base64 再 URL 解码再 RC4
     ... RC4 KSA/PRGA ...
   }
   ```
   调用形态：`_0x57fd('0x1','dhCd')` —— 第一个参数是数组下标，第二个是 **RC4 密钥**。

## 步骤三：复现解密（已验证路径，优先走这条）

| 路线 | 适用 | 做法 |
|---|---|---|
| ① 在线/工具解混淆 | 控制流平坦化、死代码注入很重 | `deobfuscatejavascript.com`、`webcrack`、`synchrony`；只还原字符串数组与代理函数 |
| ② AST 还原 | 要批量处理几十个文件 | `babel`/`esprima` 解析 → 把 `_0x57fd(0x1,'key')` 折叠成字符串常量 |
| ③ **手写复现解密器**（推荐） | 只有一两处关键混淆（本题就是） | 照抄 JS 的三步链路到 Python，10 行搞定 |

Python 复现模板（对应上面 obfuscator.io + RC4 组合）：

```python
import base64, urllib.parse

def rc4(text, key):
    S = list(range(256)); j = 0
    for i in range(256):
        j = (j + S[i] + ord(key[i % len(key)])) % 256
        S[i], S[j] = S[j], S[i]
    i = j = 0; out = []
    for ch in text:
        i = (i + 1) % 256; j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        out.append(chr(ord(ch) ^ S[(S[i] + S[j]) % 256]))
    return ''.join(out)

def dec(payload, key):
    raw = base64.b64decode(payload)
    text = urllib.parse.unquote(''.join('%%%02x' % b for b in raw))  # = decodeURIComponent
    return rc4(text, key)
```

## 坑一：漏掉 decodeURIComponent 会解出乱码

只用 base64+RC4 解出来是乱码（本次实测：`HhP[` 直接 RC4 得 `co(ã`，
补上中间那步 URL 解码才得到 `coin`）。链路顺序必须是 base64 → decodeURIComponent → RC4。

## 坑二：flag 被拆成多段，别解出一段就收工

出题人常把 flag 切成 2 段放在**不同分支**里（本次：`DASCTF{d91c2bd2d78dfc9` + `28169f1f35c88c1a2}`）。
所以：

- 把所有混淆调用点**全部**解一遍
- 拼接前先看两段是否首尾互补（一段以 `{` 后无 `}`、另一段以 `}` 结尾）
- 解出的 alert 文案本身也常含提示（`this is your flag1:` / `flag2:`）

## 坑三：分支被源码夹死，别去「玩游戏触发」

本题明文逻辑：

```js
if(this.coin == 0xc8)       alert(flag1);   // coin==200
if(this.coin > 0xf4240)     alert(flag2);   // coin>1000000
...
if(this.coin > 999999) this.coin = 999999;  // ← 上一行刚把上限夹到 999999
```

`coin` 被硬夹到 999999，**flag2 分支在真实运行中永远不可达**。凡是遇到
「条件分支 + 同一函数里把变量夹到边界」的组合，一律判定为 **dead branch，只能静态解密**，
不要花时间去改内存/抓包重放/写自动化脚本玩通关。

同理：`alert()` 在无头环境本来也不弹，别指望 headless 浏览器「跑出来」——直接在 DevTools Console
里粘贴解密调用，或在本地 `node -e` 里跑，比自动化点击快一个量级。

## 常见混淆变体速查（识别与还原）

| 形态 | 识别 | 还原 |
|---|---|---|
| obfuscator.io 字符串数组 | `_0x` + 数组 + 代理函数 | 本手册步骤二/三 |
| JSFuck | 只有 `[]()!+` | 浏览器 Console 直接 eval |
| jjencode / aaencode | 满屏颜文字 / `$=~[]` | Console eval，或 `jsfuck-decoder` |
| `eval(atob('...'))` | 单层 | `echo xxx \| base64 -d` |
| 数组洗牌 + 自校验 | 有 `while(!![])` + `parseInt` 轮转 | 断点让轮转跑完再 dump 数组，或 AST 求值 |
| 控制流平坦化 | 大 `switch` + `while(1)` | `deobfuscatejavascript.com` / `synchrony` |
| 自定义 XOR / XXTEA / RC4 | 密钥字面量出现在调用处 | 抄算法到 Python（同步骤三） |

## 复现清单与收尾

- 解密脚本必须落产物（`bb_add_artifact` kind=poc），写清「哪个文件第几行 → 什么算法 → 什么密钥」
- 证据链：`curl 首页 → 判定静态 → grep 定位混淆行号 → 解密输出明文 → 拼接 flag`
- 反幻觉：flag 必须来自**解密函数的实际输出**，不能凭前缀猜后半段
