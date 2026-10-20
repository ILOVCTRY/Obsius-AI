---
title: 编码与二维码
phase: misc
vuln_class: [misc, encoding, base64, qr, esolang]
summary: 编码题的判据是「字符集」而不是「看起来像什么」——全 0-9a-f 就先按 hex（base64 也接受这些字符），能看出压缩 magic 就先解压，字符集是 A-Z2-7 就是 base32。
---

# 编码与二维码

进阶编码（Gray 码、二叉树编码、SMS PDU、base65536 变体、MaxiCode 等）见 [[misc/编码解码/encodings-advanced.md|进阶编码]]；图片隐写见 [[forensics/隐写与信号/图片隐写.md|图片隐写]]。

## 0. 判据速查

| 观察到什么 | 结论 | 去哪节 |
|---|---|---|
| 字符集 `A-Za-z0-9+/=` | base64 | §1 |
| 字符集 `A-Z2-7=`，无小写无 0/1/8/9 | base32 | §1 |
| 全是 `0-9a-f` 且长度偶数 | **先 hex**，不要先 base64 | §1 |
| 开头 `78 01`/`78 9c` / `1f 8b` / `BZh` / `fd 37 7a 58 5a 00` | zlib / gzip / bz2 / xz | §2 |
| 一串数字，含小数与科学计数法 | IEEE 754 float32，4 字节一字符 | §1 |
| 文本「变成日文」了 | UTF-16 端序反了 | §1 |
| 每字节两位十进制 | BCD | §1 |
| 全是空格/Tab/换行，或 S/T/L | Whitespace | §4 |
| 重复的少量主题词 | 自定义 Brainfuck 映射 | §4 |
| 一片 CJK 汉字墙 | base65536 | §5 |
| 二维码被切成块 | 看块名/目录名是否带序号 | §3 |
| 二维码残缺 | 纠错码可恢复，先修图 | §3 |

## 1. 基础编码

```bash
echo "encoded" | base64 -d
echo "OBUWG32DKRDHWMLUL53TI43OG5PWQNDSMRPXK3TSGR3DG3BRNY4V65DIGNPW2MDCGFWDGX3DGBSDG7I=" | base32 -d
echo "68656c6c6f" | xxd -r -p
echo "uryyb" | tr 'a-zA-Z' 'n-za-mN-ZA-M'     # ROT13
```

**hex 优先规则**：纯 `0-9a-f` 的字符串**同时满足 base64 的字符集**，但正确解法是先 hex。判断依据是长度——hex 要求偶数长度且解出的字节可打印；base64 通常含 `+`/`/`/`=` 或大小写混排。

**ROT13 识别词**：`gur` = the、`synt` = flag、`pna` = can。

**IEEE 754**：给一串混合整数/小数/科学计数法的数字时，每个 float32 恰好 4 字节：

```python
import struct
for v in [240600592, 212.2753143310547, 2.7884192016691608e+23]:
    print(struct.pack('>f', v))    # b'Meta' b'CTF{' b'fl04'
```

双精度用 `'>d'`（8 字节）。**端序先试大端**，大端解出乱码再试小端。

**UTF-16 端序反转**：

```python
fixed = mojibake.encode('utf-16-be').decode('utf-16-le')   # 存为 LE 但按 BE 读
fixed = mojibake.encode('utf-16-le').decode('utf-16-be')   # 反之
```

判据：文本变成一堆 CJK 字符，且题面提到 translation / endian / 乱码。

**BCD**：每字节拆两个半字节各表一位十进制。

```python
dec = ''.join(f'{(b>>4)&0xf}{b&0xf}' for b in data)
ascii_text = ''.join(chr(int(dec[i:i+2])) for i in range(0, len(dec), 2))
```

## 2. 多层解码：不要中途有损

**核心纪律：全程保持 bytes，不要在中途用 `errors='replace'` 转成 str**——那会把后续 pass 的输入永久污染。文本解码器（hex/base64）只在 ASCII 视图上跑，压缩/XOR 在 bytes 上跑。

**magic 表**：

| 压缩 | magic |
|---|---|
| zlib | `78 01` / `78 9c` |
| gzip | `1f 8b` |
| bz2 | `42 5a 68`（`BZh`） |
| lzma/xz | `fd 37 7a 58 5a 00` |

```python
def try_xor(raw, hint=b'flag'):
    # 第一遍：精确匹配 hint（区分大小写），避免 0x20 把 FLAG 变 flag 的假命中
    for k in range(256):
        d = bytes(b ^ k for b in raw)
        if hint in d:
            return d
    # 第二遍：大小写不敏感 + 可打印兜底
    best = None
    for k in range(256):
        d = bytes(b ^ k for b in raw)
        if all(32 <= b < 127 or b in b'\n\r\t' for b in d[:64]):
            if b'flag' in d.lower():
                return d
            best = best or d
    return best
```

**两遍的顺序不能反**：先精确匹配再模糊，否则 `key=0x20` 会把 `FLAG` 映射成 `flag` 而抢先命中，导致后面全错。

**钓鱼 flag**：多层题常在中间插假 flag。看到「keep decoding」「REAL_DATA_FOLLOWS:」这类标记就继续解，别停。

## 3. 二维码

```bash
zbarimg qrcode.png            # 解码
zbarimg -S*.enable qr.png     # 所有条码类型
qrencode -o out.png "data"    # 生成
```

**结构**：三个 7×7 定位图案在左上/右上/左下；版本公式 `边长模块数 = version*4 + 17`。

**残图修复**：

```python
gray = np.mean(np.array(Image.open('damaged_qr.png')), axis=2)
binary = (gray < 128).astype(int)
rows, cols = np.any(binary, axis=1), np.any(binary, axis=0)
rmin, rmax = np.where(rows)[0][[0, -1]]
cmin, cmax = np.where(cols)[0][[0, -1]]
qr = binary[rmin:rmax+1, cmin:cmax+1]
```

定位图案模板（`1` 为黑）：

```text
1 1 1 1 1 1 1
1 0 0 0 0 0 1
1 0 1 1 1 0 1
1 0 1 1 1 0 1
1 0 1 1 1 0 1
1 0 0 0 0 0 1
1 1 1 1 1 1 1
```

**拼块题分两类，判据是「块有没有带序号」**：

- **带序号**（目录名/文件名是 base64 的 `MDAx` → `001`）→ 解 base64 得索引，排序拼图即可。`side = int(math.isqrt(n))`，按 `divmod(i, side)` 贴到画布。
- **不带序号（打乱）** → 靠结构约束回溯：先用定位/定时/对齐图案钉住约 50% 的块，再按 QR 规范的像素不变量做约束搜索。工具用 `segno`。

## 4. 深奥语言

| 语言 | 特征 |
|---|---|
| Brainfuck | `++++++++++[>+++++++>` |
| Whitespace | 只有空格/Tab/换行，或 S/T/L 替换 |
| Ook! | `Ook. Ook? Ook!` |
| Malbolge | 极度混淆 |
| Piet | 图像即代码 |

**Whitespace 指令集**（IMP = 指令修饰参数）：

| 指令 | 编码 | 动作 |
|---|---|---|
| PUSH | `S S` + 符号 + 二进制 + `L` | 压栈（S=0, T=1, L 终止） |
| 输出字符 | `T L S S` | 弹栈按 ASCII 打印 |
| 退出 | `L L L` | 结束 |

```python
def solve_whitespace(content):
    code = [c for c in content if c in 'STL'] if any(c in content for c in 'STL') \
           else [m for c in content for m in [{' ': 'S', '\t': 'T', '\n': 'L'}.get(c)] if m]
    stack, out, i = [], "", 0
    while i < len(code):
        if code[i:i+2] == ['S', 'S']:
            i += 2
            sign = 1 if code[i] == 'S' else -1; i += 1
            val = 0
            while i < len(code) and code[i] != 'L':
                val = (val << 1) | (code[i] == 'T'); i += 1
            i += 1
            stack.append(sign * val)
        elif code[i:i+4] == ['T', 'L', 'S', 'S']:
            i += 4
            if stack: out += chr(stack.pop())
        elif code[i:i+3] == ['L', 'L', 'L']:
            break
        else:
            i += 1
    return out
```

**自定义 Brainfuck 映射的识别**：ASCII 文本、超长行、词表只有 5-8 个词、**有一个词专门做行尾**（对应 `.`）、一个词出现次数远多于其他（对应 `+`）。

```python
from collections import Counter
freq = Counter(content.split())      # 最高频 → +/-，行尾词 → .
mapping = {'arch':'+', 'linux':'-', 'i':'>', 'use':'<', 'the':'[', 'way':']', 'btw':'.'}
bf = ''.join(mapping.get(w, '') for w in content.split())
```

**输出不是 ASCII 时先交换 `+`/`-` 或 `>`/`<`**，这两组是最常见的映射歧义。

**Piet**：PNG 没有明显隐写时，试 `npiet challenge.png`。多层链常见形态：Piet → base64 → Malbolge、Brainfuck → Ook → Whitespace、JSFuck → JS。

## 5. base65536

一片 CJK 汉字墙，实际是 base65536：每个码位承载 2 字节。

```bash
pip install base65536
python3 -c "import base65536,sys; sys.stdout.buffer.write(base65536.decode(open('blob.txt').read()))" > out.bin
file out.bin
```

**反直觉点**：base64 是 3 字节 → 4 字符（膨胀 33%），base65536 是 2 字节 → 1 码位，但码位在磁盘上占 1-4 个 UTF-8 字节，**实际膨胀约 2 倍**。视觉上却显得紧凑，这就是它的伪装价值。

**同类变体**：base1024（BMP）、base2048、base4096、base32768。判据都是「一片非拉丁 Unicode，且码位分布集中」。

## 6. 多跳面包屑

flag 藏在一串外部资源后面，每跳用不同编码。

```python
url2 = base64.b64decode("aHR0cHM6Ly9naXN0Lmdp...").decode()
url3 = bytes.fromhex("68747470733a2f2f...").decode()
flag = codecs.decode("hgsynt{...}", 'rot_13')
```

**提示常在周围文本里**：「Three letters follow」暗示 3 字符编码（hex）、诗歌/注释/文件名都可能指路。**看到 `68747470733a2f2f` 就知道是 `https://`**——这是 hex 编码 URL 的固定开头，可以直接认出来。

## 7. 纪律

- **按字符集判，不按形状猜**。纯 hex 字符串也是合法 base64，先 hex 能避免走弯路。
- **全链保持 bytes**。中途 `errors='replace'` 会把数据永久改坏，而症状是「后面几层解出来是乱码」，很难回溯定位。
- **压缩先看 magic**，不要对每个输入都试一遍 decompress——顺序错了会解出垃圾并覆盖原数据。
- **别被钓鱼 flag 骗停**。多层题看到「keep decoding」就继续，最终 flag 常在最内层。
