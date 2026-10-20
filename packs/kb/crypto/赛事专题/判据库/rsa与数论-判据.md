---
title: RSA 与数论攻击判据库
phase: crypto
vuln_class: [rsa, number-theory, lattice]
summary: RSA 题不看参数规模，看「额外信息是什么形状」。本文按「已知量 → 对应攻击」建表，覆盖弱密钥、共享因子、部分泄露、格攻击四类，每条给成立条件与失效判据。
---

# RSA 与数论攻击判据库

> 来源：`crypto/赛事专题/Writeup合集` 中 120 篇 Crypto WP（530 条观测 / 357 条失败尝试）。
> 主库 `crypto/非对称加密/rsa-attacks.md` 已覆盖 Coppersmith 家族与 oracle 攻击，
> 本文补**主库没有的具体数值判据**与**失效条件**（哪一步会白做）。

## 0. 判据速查

| 现象 | 判据 | 去哪节 |
|---|---|---|
| `n` < 600 bit | factordb 秒解 | §1.1 |
| `n` 是单素数 `getPrime(k)` | `phi = n-1`，不用分解 | §1.2 |
| `p` 与 `q` 接近（差 < 2^20） | 费马分解 | §1.3 |
| 给了 `p+q` 或 `p*q` 与 `p²+q²` | 韦达/消元，O(1) | §1.4 |
| 两个 `n` 共享因子 | `gcd(n1, n2)` | §2.1 |
| `e` 很小（3/13/17）且多个 `n` | 低指数广播 | §2.2 |
| `e` 很大（接近 `n`） | Wiener 连分数 | §2.3 |
| 给了 `dp`/`dq` 的部分位 | 部分密钥暴露 | §3.1 |
| 给了 `p` 的高位或低位 | Coppersmith | §3.2 |
| 有签名 oracle | 分 LSB / 填充 / Manger 三类 | §4 |
| `hint` 与 `p` 有整除关系 | `gcd(hint, n)` 直接拆 | §5.1 |
| 密文短且 `e` 小 | 直接开 `e` 次根 | §5.2 |

## 1. 弱密钥

### 1.1 规模判据

```text
n < 512 bit   → factordb / yafu 秒解
512–600 bit   → yafu（多核几分钟）
600–1024 bit  → 除非有额外信息，否则放弃
```

判据是**位数**。1024 bit 以上的 RSA 没有额外信息就是不可解，不要浪费时间试分解。

### 1.2 单素数

```python
n = getPrime(2025)     # 只有一个素数
phi = n - 1            # 因为 phi(p) = p-1
d = inverse(e, n - 1)
```

判据是**`n` 的位数等于「一个素数的位数」而不是两个**。或者源码里 `n = getPrime(...)` 没有乘。

### 1.3 费马分解

`p` 与 `q` 接近时，`n = a² - b²`，`a = (p+q)/2`，`b = (p-q)/2`。

```python
a = isqrt(n) + 1
while True:
    b2 = a*a - n
    if isqrt(b2)**2 == b2:
        p = a - isqrt(b2); q = a + isqrt(b2); break
    a += 1
```

判据是 **`|p - q|` 小于 `n^0.25`**。差几千位以上就退化成暴力，不适用。

### 1.4 韦达与消元

给了 `p+q` 与 `p*q`：

```python
# x² - (p+q)x + pq = 0
p = (s + sqrt(s*s - 4*n)) // 2
```

给了 `p²+q²` 与 `pq`：

```python
# (p+q)² = p²+q² + 2pq
s = isqrt(p2q2 + 2*pq)
```

判据是**给的两个量能组成对称多项式**。不能组的话考虑 `p-q` 的平方。

### 1.5 特殊构造的素数

```text
p = next_prime(iroot(n, 2))     → p² ≈ n，一次爆破即可
n = p^k                         → 反复 iroot
n = p*q*r（多素数）              → phi = (p-1)(q-1)(r-1)，需全部分解
n = p^a * q                     → 先 iroot 找 a
```

判据是**`n` 的位数与单个素数的位数不匹配**（比如 `n` 是 2048 bit 但 `p` 是 1024 bit，说明只有两个；`n` 是 3072 bit 但 `p` 是 1024 bit，可能是三个）。

## 2. 共享与广播

### 2.1 共享因子

```python
g = gcd(n1, n2)
if g != 1: p = g  # 两个模数共享一个素数
```

判据是**给了多个 `n`**。批量跑 `gcd` 是 O(k²)，几十个模数秒出。

**变体**：`n1 = p*q1`，`n2 = p*q2`，`gcd` 直接出 `p`。这在「同一系统不同用户」的题里极常见。

### 2.2 低指数广播（Hastad）

```text
条件：同一个 m，多个不同的 n，同一个小的 e，且 e 个密文
原理：CRT 把 e 个密文合成 M = m^e mod (n1*n2*...*ne)
      因为 m^e < 乘积，所以 M 就是 m^e（没有取模）
      直接开 e 次根
```

判据是**密文个数 ≥ e**。少于 `e` 个就凑不出完整的 `m^e`，只能试 Coppersmith（需要填充）。

```python
# e=3 且只有 2 个密文 → Hastad 不成立，改试 Coppersmith
```

### 2.3 Wiener 攻击

```text
条件：d < N^0.25
原理：e/N ≈ k/d，用连分数展开 e/N 逼近，收敛项里找 d
```

```python
from fractions import Fraction
for k, d in convergents(Fraction(e, n)):
    if k == 0: continue
    if (e*d - 1) % k == 0:
        phi = (e*d - 1) // k
        # 解 x² - (n-phi+1)x + n = 0
```

判据是**`e` 很大（接近 `n`）**。`e` 正常（65537）时 Wiener 不成立。

**扩展**：Boneh-Durfee 支持 `d < N^0.292`，需要格，成本高得多。

## 3. 部分密钥暴露

### 3.1 `dp` 泄露

```text
给了 dp = d mod (p-1)
则 e*dp ≡ 1 (mod p-1)
→ e*dp - 1 = k*(p-1)，k < e
→ 枚举 k ∈ [1, e)，p = (e*dp - 1)/k + 1，验证 n % p == 0
```

判据是**`e` 不大**（枚举 `k` 到 `e`）。`e` 是 65537 时枚举 6 万次，秒出。

**坑**：`dp` 溢出（`dp > p`）时 `m2 = c^dp mod p` 会退化成 `0^0 = 1`，这是另一类题。

### 3.2 Coppersmith

| 已知 | 方法 | 界 |
|---|---|---|
| `p` 的高位（约一半） | 一元 Coppersmith | 已知 ≥ `n^0.5` 的位 |
| `p` 的低位 | 一元 Coppersmith（`f(x) = p_low + x`） | 已知 ≥ `n^0.5` 的位 |
| `d` 的低位 | Boneh-Durfee 变体 | `d < n^0.292` |
| 两个未知数（`p`、`q` 各知一部分） | 二元 Coppersmith | 需 `xy < n^0.5` 量级 |

判据是**已知位数占比**。少于一半的位，Coppersmith 不成立。

**关键数值**：`p` 是 1024 bit 时，已知 512 bit 是临界点。已知 500 bit 大概率失败。

## 4. Oracle 攻击

| Oracle 类型 | 判据 | 复杂度 |
|---|---|---|
| LSB（返回 `m mod 2`） | 服务端返回奇偶 | `log2(n)` 次 |
| 填充（Bleichenbacher） | 返回「填充是否合法」 | 约 `2^16` 次 |
| Manger（返回「m < B」） | RSA-OAEP 的简化版 | `log2(n)` 次 |
| 奇偶 + 二分 | 两者结合 | `log2(n)` 次 |

判据是**服务端返回 1 bit 信息**。1 bit 就够二分出全部明文。

**坑**：网络延迟大时，`log2(2048) = 2048` 次请求可能要几分钟。先本地模拟确认逻辑，再打远程。

## 5. 隐式信息

### 5.1 `hint` 与 `p` 的整除关系

```text
hint = k*p        → gcd(hint, n) = p
hint = p + q      → 韦达
hint = p - q      → (p-q)² + 4n = (p+q)²
hint = p*q mod x  → 需具体分析
hint = 2^hint ≡ 1 mod p → p | (2^hint - 1)，gcd(2^hint-1, n)
```

判据是**`hint` 的构造方式**。源码里 `hint = ...` 那一行决定一切，先读懂它。

**最高频**：`hint` 是 `p` 的倍数但加了噪声，此时 `gcd(hint, n)` 不通，要试 `gcd(hint + delta, n)` 对小 `delta` 枚举。

### 5.2 小 `e` 直接开根

```python
m = iroot(c, e)   # 如果 m^e < n，密文没有取模
```

判据是 **`c` 的位数远小于 `n`**。`c` 很小说明没取模，直接开根。

### 5.3 负指数

```text
e = -1, -2, -3
c = m^e mod n = (m^-1)^|e| mod n
→ 先求 c 的模逆，再开 |e| 次根
```

判据是**源码里 `e` 是负数**。`pow(m, -1, n)` 在 Python 3.8+ 直接支持。

## 6. 工具与流程

```bash
# 第一步永远是信息收集，不是分解
RsaCtfTool --publickey pub.pem --attack all
# 或
python -c "import gmpy2; print(gmpy2.iroot(n, 2))"
```

**流程**：

```text
1. 读源码，找所有给的量（hint / dp / 部分位 / 多个 n）
2. 判 n 的位数 → 能不能直接分解
3. 判给的量能组成什么方程
4. 选攻击（查 §0 表）
5. 本地验证（用小素数模拟一遍）
6. 打远程
```

判据是**第 1 步决定后面全部**。源码没读透就开始分解是最大的浪费。

## 7. 纪律

- **先读源码再动手**。RSA 题的解法全在「给了什么额外信息」里。
- **`n` 超过 1024 bit 且无额外信息就放弃**，别试分解。
- **低指数广播要求密文数 ≥ e**，不够就换 Coppersmith。
- **Coppersmith 的临界点是已知一半的位**，差一点就失败。
- **`e` 大用 Wiener，`e` 小用广播，`e` 正常找 oracle**。
- **本地用小素数模拟一遍再打远程**，远程试错成本高。
- **`gmpy2.iroot` 比 `int(n**0.5)` 可靠**，浮点会丢精度。
