---
title: 古典密码与结构化 XOR 攻击
phase: crypto
vuln_class: [classical-cipher, vigenere, substitution, xor, otp, crib-dragging]
summary: 古典密码先判断密钥是否重复、是否复用、是否泄漏明文结构；随后用周期、频率、已知文件头、校验和或约束搜索缩小密钥空间。
---

# 古典密码与结构化 XOR 攻击

高阶数学、格和 ECC 见 [[crypto/高阶专题/advanced-math.md|高阶数学]]、[[crypto/格与后量子/lattice-and-lwe.md|格与 LWE]]、[[crypto/非对称加密/ecc-attacks.md|ECC 攻击]]；基础编码见 [[misc/编码解码/encodings.md|编码与解码基础]]。

## 0. 判据速查

| 现象 | 优先方向 | 关键验证 |
|---|---|---|
| 字母密文、周期性重复 | Vigenère/Kasiski | 重复片段距离的 GCD |
| 数字只含 1..5 且成对 | Polybius | 5×5 坐标与 I/J 合并 |
| 一一替换、空格保留 | Atbash/monoalphabetic | 词形、频率和字母映射 |
| 多段密文同长度或已知前缀 | repeating XOR/OTP reuse | `C1 xor C2 = P1 xor P2` |
| 文件头被破坏但格式可猜 | known-header XOR | magic 与尾部结构一致 |
| 轮换/网格/图像发生位移 | 结构化置换 | 旋转、行列或像素偏移约束 |
| 验证器只汇总比较值 | 弱校验 | 找碰撞概率与交互预算 |

## 1. Vigenère 与替换密码

Vigenère 对字母按 `C=(P+K) mod 26`，解密为 `P=(C-K) mod 26`。先确认非字母是否消耗 key；很多实现只对字母推进 key index。已知明文（如 flag 前缀）可直接导出对应 key 片段：

```python
def derive_key(cipher, plain):
    out = []
    for c, p in zip(cipher.upper(), plain.upper()):
        if c.isalpha() and p.isalpha():
            out.append(chr((ord(c)-ord(p)) % 26 + 65))
    return ''.join(out)
```

无已知明文时用 Kasiski：找重复 n-gram，统计出现位置差，候选 key length 通常是这些距离的公因数。GCD 可能是实际长度的因数，需用 Friedman/重合指数和分列频率继续验证。候选长度确定后，每列是 Caesar，可用卡方或语言模型评分。短密文、非英语文本、带填充 key 和非重复 key 会使 Kasiski 失效。

Atbash 是固定映射 `A↔Z`，保留空格和标点；Polybius 通常用 1..5 的行列坐标，5×5 表合并 I/J。数字数量必须为偶数，坐标越界直接说明表布局或输入假设错误。旋转轮替换则枚举轮盘起点，并用已知 flag 前缀、词形和整体可读性筛选。

## 2. 重复 XOR、级联 XOR 与 OTP 复用

重复多字节 XOR 将密文按候选 key length 分列，每列独立求单字节 XOR。评分应结合空格、字母、标点、UTF-8 和已知文件 magic，不要只看 printable：

```python
def xor_repeat(data, key):
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def column_score(col, k):
    p = bytes(b ^ k for b in col)
    return sum(p.count(x) for x in (b' ', b'e', b't', b'\n'))
```

key length 可由重复距离、归一化汉明距离或逐列最佳分数估计；候选长度的倍数常同时得高分，必须用完整明文和文件格式复核。

级联 XOR 若 `c[i]=p[i] xor p[i-1]` 或类似递推，通常只需枚举首字节并按规则展开；必须先从代码/样例确认递推使用前一明文还是前一密文。

OTP 只有 key 真正随机、与明文等长且只用一次才安全。复用 key 时：

```text
C1 xor C2 = P1 xor P2
P2 = C1 xor C2 xor P1
```

未知明文可 crib-drag：把常见单词或 flag 前缀滑过 `C1 xor C2`，用可打印性、词边界和后续语义筛选。已知一条明文时先验证长度、padding、方向和 key 是否从同一偏移开始。

## 3. 已知结构恢复 XOR key

若密文应为 PNG/PDF/ZIP/ELF，可将开头与已知 magic XOR 得到 key 的前缀，再用重复出现的 header、块长度、CRC 或文件尾验证周期。避免把单字节 key 误当重复 key：至少需要多个已知字节和非连续结构确认。

解密后依次检查 `file`、magic、声明长度、目录结构和 checksum；“前 8 字节像 PNG”不足以证明整文件正确。二进制 key 保持 bytes，禁止中途转文本。

## 4. 结构化密钥空间

5×5 网格若只独立打乱行和列，空间是 `5!×5!=14,400`，不是 25!；枚举 row/column permutation 后用词典、已知前缀或 n-gram 评分。行列操作是否可交换取决于具体映射，需用样例验证正向/逆向方向。

图像 Caesar 变换先按列/行求循环位移，再判断位移是固定参数、线性函数还是 ASCII。比较原图与变换图时应逐行/逐列匹配完整像素，避免重复纹理造成假偏移；每个偏移序列还要检查范围、重复压缩和 flag 字符合法性。

3D/多维 Vigenère、网格和轮换题常把同一 key 施加到对称位置。先写出坐标到 key index 的公式，再利用镜像位置的差分消去 key；不要凭视觉猜维度顺序。

## 5. 特殊映射与可变长度替换

可变长度 homophonic substitution 中，单字符频率不可靠。统计 1..N-gram，寻找子串与其组成片段出现频次一致的候选 codeword；按最长匹配替换后，再将结果作为普通替换密码处理。候选 codeword 可能重叠，必须用动态规划/回溯和完整明文评分，不要无条件 `.replace()`。

Nihilist/书本密码等依赖坐标或文本位置时，先确认坐标基数、空格处理、页/行/词编号和起点。已知双 crib 能解出重复偏移，但需用更多位置验证；hash 只能作为最终候选确认，不能证明中间映射正确。

信号量、nibble 重组和随机 padding 题应先分离元数据、padding 与 payload。对 nibble 的高低半字节、端序、随机填充长度和对齐分别枚举，再用 magic/校验和筛选。

## 6. 弱验证器与确定性 OTP

如果服务端把整段逐字节比较错误地汇总成一个 XOR/异或字节，再与单个值比较，随机响应可能以约 `1/256` 概率通过。分析时先确认服务端是否有重试上限、速率限制和状态副作用；只在授权题目服务上验证。更可靠的修复/审计结论是：比较应使用常数时间的逐字节完整相等，而不是汇总后比较。

确定性 OTP 若每次连接重置 keystream，已知明文可恢复同连接 keystream；负载均衡时需确认目标密文与样本来自同一 backend。用连接标识、响应模式或重复探测做匹配，不要默认所有后端共享 key。

## 7. 实现与验证纪律

- 明确字母表、大小写、非字母是否推进 key、端序和 padding。
- 密钥搜索先用便宜判据缩小范围，再用完整解析、已知前缀、CRC 或 hash 复核。
- 频率分析只适合足够长且语言分布稳定的文本；短密文应优先使用结构线索。
- 每次候选解码保留 key、偏移、方向、评分和失败原因。
- 题名和赛事名称只能引导假设，不能替代密文结构证据。
- 对真实远程服务的弱校验、密钥恢复和破解仅限明确授权环境。
