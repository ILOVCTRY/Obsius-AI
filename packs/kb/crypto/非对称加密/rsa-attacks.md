---
title: RSA 攻击判据手册
phase: crypto
vuln_class: [rsa, coppersmith, oracle-attack, implementation-flaw]
summary: 先做参数体检（e/d 大小、模数共享、phi 变体、oracle 类型），再决定取整根 / 格 / 区间二分；RSA 的坑九成在实现而不在数学
---

# RSA 攻击判据手册

## 0. 判据速查

| 现象 | 攻击 | 关键式 / 门槛 |
|---|---|---|
| `e=3`，明文短，`c` 是完全立方 | 整数开方 | `m^e < n`，`iroot(c, e)` 精确 |
| 同 `n`，两个互素 `e1,e2` | 共模 | `a·e1+b·e2=1`，`m = c1^a·c2^b` |
| `e` 接近 `n`（超大） | Wiener | `d < N^0.25`，连分数逼近 `e/n` |
| Wiener 失败，`d≈N^0.28` | Boneh-Durfee | `d < N^0.292`，`f(x,y)=(N+1+y)x+1 mod e` |
| 已知 `d` 的一半位 | 部分密钥 | `d = known + 2^t x`，枚举 `k<e` |
| `p-1` 光滑 | Pollard p−1 | `gcd(a^(B!)−1, n)` |
| `p-1` 不光滑但 `p+1` 光滑 | Williams p+1 | Lucas `V_M`，`gcd(V_M−2, n)`，`M=lcm(1..B)` |
| `p±1` 都不光滑，因子不大 | ECM | `B1=1e5` 万条曲线抓 ~40 bit |
| 同明文、`e` 个不同模数 | Hastad 广播 | CRT 后开 `e` 次根 |
| 同明文、各自线性填充 | Hastad+Coppersmith | CRT 成 `e` 次多项式求小根 |
| 单模数、明文有已知前缀 | 定式 Coppersmith | `m=K+x`，`|x| < n^{1/e}` |
| `dp,dq` 都很小 | small-CRT | `dp,dq < N^0.073`（实践到 0.1） |
| `|p−q|` 小 / `q=next_prime(p)` | Fermat | 从 `isqrt(n)` 上下试 |
| `q ≈ k·p` | 近似 Coppersmith | `q̄=k·isqrt(N/k)`，`f=x+q̄`，`X=2^200` |
| `p` 的部分位已知 | 结构化 Coppersmith | `p=base+10^k·x` |
| 多模数共享素因子 | batch GCD / 三角 | `gcd(n_i, n_j)` |
| 共享低位/高位但 `gcd=1` | 隐式分解 | `t > 2αn`（1024 位需 >512 bit） |
| oracle 返回奇偶 | LSB 二分 | `c·2^e`，`2m<n` ⟺ 偶，~log n 次 |
| oracle 返回 `0x00 0x02` 合法性 | Bleichenbacher | 区间收缩，~10k 次 |
| oracle 返回 `m < B` | Manger | 阈值 oracle，~128 次（64 位） |
| 解密/签名拒绝特定值 | 同态绕过 | `S(a)S(b)=S(ab)`，`Dec(a·b)` |
| `p=q` 能过校验 | 伪 totient | `φ(p²)=p(p−1) ≠ (p−1)²` |
| `gcd(e,φ)>1` | 逐素因子开根 | 每素数 `e` 个根 + CRT |
| 泄漏 `k·φ(n)` 的倍数 | Miller-Rabin 分解 | `gcd(a^(m/2)−1, n)` |
| 验签接受自选 `(n,e)` | `e=1` | `n = s − pad` |
| `n` 是 `p²q` | Schmidt-Samoa | `φ=p(p−1)(q−1)` |

---

## 1. 参数体检（先花两分钟）

1. `bitlen(n)`、`bitlen(e)`、`bitlen(d)`（若给）→ 定 e/d 大小分支。
2. `gcd` 所有能拿到的模数两两算一遍 → 共享素因子 / 三角。
3. 试 `iroot(n, k)`（k=2,3）→ 完全幂、`p²q`。
4. 看 `phi` 是怎么算的 → 变体 totient。
5. 看服务行为 → 错误码 / 时序 / 长度 → oracle 类型。
6. 看输入是怎么解析的 → 缓冲区/截断类实现漏洞。

**phi 变体清单**（`n` 能正常分解但标准 `d` 解不出时逐个试）：

```python
cands = [(p-1)*(q-1), (p**4-1)*(q**4-1), p*(p-1)*q*(q-1),
         lcm(p-1, q-1), p*(p-1)*(q-1), n-1]
for phi in cands:
    try: d = pow(e, -1, phi); pt = long_to_bytes(pow(c, d, n))
    except ValueError: continue
```

「分解成功但解不出」= 几乎必然是 `phi` 用错了，不是分解错了。

---

## 2. e 小 / d 小

**e 小**：`m^e < n` 时密文就是裸幂，`gmpy2.iroot(c, e)` 精确即中。若填充把 `m` 顶过 `n`，走三条路：

- 已知明文长度（AES-CTR 保长度）→ 试 `iroot(c + k·n, 3)`，`k` 小范围扫；
- 多模数 → Hastad；
- 单模数 + 已知前缀 → 定式 Coppersmith。

**e 超大** = `d` 很小的信号。Wiener 用 `e/n` 的连分数收敛子，`d < N^0.25` 必中。落在 `N^0.25 ~ N^0.292` 之间要上 Boneh-Durfee：

```python
f = (N + 1 + y) * x + 1     # 对 x 首一，小根 (k, d)，y = -(p+q)
# delta = log_e(d)，m ≈ 4..7，t ≈ 1..3，维数 (m+1)(m+2)/2
# 边界附近 m 调到 6~7、t 调到 2~3（维数 45~70）
```

2048 位、`d≈N^0.28` 用 `m=4,t=1` 在 30 秒内出结果。`δ<0.292` 是启发式界，卡边就加维数。

**已知 `d` 的一半位**：`d = known + 2^t·x`，`|x|<2^t`。由 `e·d ≡ 1 + k·φ(N)` 枚举 `k<e`（`e=65537` 时 `k` 常小于 50），每个 `k` 建一元多项式求小根。35×35 的格就能从「2048 位 `d` 只泄漏 1056 位」里恢复。`e=65537` 时 FNP 改进能多榨约 17 bit。

---

## 3. 模数分解

### 3.1 光滑因子三连

```python
def pollard_p1(n, B=100000):
    a = 2
    for j in range(2, B + 1): a = pow(a, j, n)
    return gcd(a - 1, n)
```

`p-1 | B!` ⟹ `a^(B!) ≡ 1 (mod p)`。**不要停在 p−1**：`p+1` 光滑同样常见，用 Lucas 序列 `V_M(P,1)`，`M = lcm(1..B)`，`gcd(V_M − 2, n)`。两者都不行再上 ECM：`B1=1e5` + 1 万条曲线 ≈ 40 bit 因子，`B1=1e6` ≈ 50 bit。

三连顺序固定：`pollard-p1 → williams-p1 → ecm`。有 2049 位模数但 512 位因子 `p+1` 光滑的题，Williams 5 秒出结果。

### 3.2 共享素因子

- **batch GCD**：乘积树 + 余数树，`O(n log n)`，几千个模数可行。
- **三模数三角**：`N1=p1p2, N2=p1p3, N3=p2p3` → 三次 `gcd` 全解。
- **隐式分解**：`p1 ≡ p2 (mod 2^t)` 且 `gcd(N1,N2)=1`，batch GCD 失效，但 `t > 2αn`（`α=bitlen(p)/bitlen(N)`，平衡 RSA 取 0.5）时 2 模数格仍能分解。1024 位需 `t>512`；不够就加维数或收第三个模数。
- **低熵 keygen**：`p = next_prime(2^k + small_delta)` 会让大量 delta 撞到同一个素数 → 两两 `gcd` 就中。

### 3.3 结构化素数

| 结构 | 方法 |
|---|---|
| `q = next_prime(p)`，`|p−q|` 小 | 从 `isqrt(n)` 向上下 `prevprime/nextprime` 试 |
| `q = k·p + delta` | `q̄ = k·isqrt(N//k) + 2^512`，`f = x + q̄`，`X=2^200`，`beta=0.5` |
| `p` 只用数字 {6,7} | 从 LSB 逐位：`q ≡ n·p^{-1} (mod 10^k)`，按数字集剪枝 |
| `p = base + 10^k·x` | `f = x + base·(10^k)^{-1} mod N`，`X=2^70` |
| `p = kp·B + tp`（`B` 大、`kp<2^12`） | `n = A·B² + C·B + D`，爆破 `kp·kq`（2^24）再解二次 |
| ROCA（CVE-2017-15361） | `p = k·M + (65537^a mod M)`；`roca-detect` 判指纹，`neca` 分解 512 位 |
| `p²q` | `iroot(n,3)` 附近试除，`φ = p(p−1)(q−1)` |
| 依赖素数 `q ≡ e^{-1} (mod p)` | `e·q = k·p+1`，`k·p² + p − e·n = 0`，`k<2^16` 枚举判别式开方 |

**依赖素数的判别式**：`disc = 1 + 4k·e·n` 必须是完全平方，命中即得 `p`。任何「用公开算术关系从 `p` 导出 `q`」的 keygen 都会塌缩成小搜索。

---

## 4. Coppersmith 家族怎么选

先数模数个数和已知结构：

| 场景 | 多项式 | 界 |
|---|---|---|
| `e` 个模数、同明文 | CRT 合并成 `Π((x)^e − c_i) mod N` | 无需小根，直接开根 |
| `e` 个模数、线性填充 `a_i m + b_i` | CRT 后 `e` 次多项式 | `X = min(n_i)` 或 `N^{1/e}` |
| 1 个模数、已知前缀 `K` | `(K+x)^e − c` | `X < n^{1/e}`（e=3 约 `n^0.33`） |
| 1 个模数、`p` 部分已知 | `x + base·(10^k)^{-1}` | 线性，`X < N^0.25` |
| `dp,dq` 都小 | 双变量 | `X≈N^0.073`，`Y≈N^0.5` |
| `d` 部分已知 | 每 `k<e` 一元 | `X=2^{剩余位}` |

**Hastad vs 定式**：`broadcast = e 份密文`，`stereotyped = 1 份密文 + 已知 K`。数模数个数就能分开。

**界是硬的**：`X` 超过 `n^{1/e}` 格就失败，只能缩短未知段（多给已知前缀）或换更高次的组合。实践常用 `fpylll` 的 Howgrave-Graham 矩阵（`hg_matrix`）+ `LLL`，Sage 的 `small_roots` 只是同一套东西的封装。

**Franklin-Reiter（相关消息）**：`e=3`、两段明文只差已知 padding 时，`gcd((x+pad1)^3 − c1, (x+pad2)^3 − c2)` 在 `Zmod(n)` 上直接给出 `m`。要求两多项式首一（首项系数是单位），中间余式非首一时用模逆归一化。

---

## 5. oracle 攻击：三种别混

| 类型 | oracle 判据 | 查询量 | 收缩方式 |
|---|---|---|---|
| LSB | 返回 `m & 1` | `~log₂n`（1024 位 ≈1024 次） | 二分：`c·2^e`，偶 ⟺ `2m<n` |
| Bleichenbacher | 返回「是否 `0x00 0x02`」 | ~10k | 区间 `[a,b]` + 搜 `s` |
| Manger | 返回「`m < B`」 | ~128（64 位密钥） | 先倍增 `f` 越界，再二分 |

```python
# LSB 二分
lo, hi, cur = 0, n, c
for _ in range(n.bit_length()):
    cur = (cur * pow(2, e, n)) % n
    if oracle(cur) == 0: hi = (lo + hi) // 2      # 2m < n
    else:                lo = (lo + hi) // 2
```

**Bleichenbacher**：`B = 2^{8(k-2)}`，从 `s = ceil(n/(3B))` 起找第一个合法 `s`，然后按区间分支逐步收缩。TLS 变体就是 ROBOT。

**Manger**：`B = 2^{8(k-1)}`。第一步 `f1` 倍增到 `f1·m ≥ B`，第二步找 `f2` 使 `n ≤ f2·m < n+B`，第三步二分。

**时序版（Marvin, CVE-2023-46809）**：即使错误被统一，分支差异仍泄漏。典型是 Python 短路求值：

```python
if Y != 0 or not H_verify(L, DB[:hLen]) or os2ip(PS) != 0:
    return error
```

`Y != 0` 时后面那个 200 万轮 PBKDF2 根本不跑 → 快响应 = `m ≥ B`，慢响应 = `m < B`。先做校准（用已知 `<B` 和 `>B` 的密文各测 5 次），再做带重试的阈值判定。

**噪声 oracle**：单次二分可能不收敛。跑多次取每个字节的众数，正确字节在多次运行中占多数——不要试图让单次完全正确。

---

## 6. 签名与同态

**无填充 RSA 签名可乘**：`S(a)·S(b) ≡ S(ab) (mod n)`。三类用法：

1. **黑名单绕过**：拒绝签 `m`，就签 `x` 与 `m/x`（试小因子直到两者都不在黑名单）。
2. **CRC32/线性哈希伪造**：把目标摘要分解成 `< 2^32` 的素因子，用 `crchack` 为每个因子造一条无害命令使 CRC32 恰好等于该因子，取签名后连乘。
3. **解密 oracle 绕过**：拒绝解密 `c_flag`，就解密 `c_flag · Enc(r)` 再乘 `r^{-1}`。

**`encrypt(-1) = -1`**（`e` 为奇数）：乘上 `pow(-1, e, n)` 把明文变成 `-m mod n`，解出后取负即可，绕过黑名单。

**低指数签名伪造（Bleichenbacher, e=3）**：PKCS#1 v1.5 格式是 `00 01 FF…FF 00 DigestInfo Hash`。构造一个带正确前缀、尾部随意填充的整数，取整数立方根（向下取整），尾部垃圾吸收余数。**只在前缀正确、不校验填充是否铺满整块**的实现（CVE-2006-4339）上成立。

**`e=1` + 自选模数**：验签接受用户给的 `(n,e)` 时，令 `e=1`、`n = s − PKCS1_pad(hash)`，则 `pow(s,1,n) = pad` 直接通过。

---

## 7. 实现坑（比数学题更多）

| 坑 | 机理 | 用法 |
|---|---|---|
| `p=q` | 服务端按 `(p−1)(q−1)` 算 `φ`，但 `φ(p²)=p(p−1)` | 校验全过、解密失败 → 错误信息泄漏密文 |
| `gcd(e,φ)>1` | `d` 不存在 | `e'=e/g`，解出 `m^g` 再开 `g` 次根；或逐素数开根 + CRT 枚举（`3^13≈1.6M`） |
| `n=p²q` | `φ` 用错 | 先把密文降到 `mod q` 再逆 |
| 泄漏 `k·φ(n)` 倍数 | `re·rd−1` 是 `φ` 的倍数 | Miller-Rabin 平方根法：`gcd(a^{m/2}−1, n)`，单次成功率 ≥1/2 |
| 只泄漏 `dp,dq,qinv` | `dp=d mod (p−1)` | `p = (dp·e−1)/k + 1`，`k<e` 试素数，`O(e)` 即出 |
| CRT 单比特故障 | 故障签名/正确签名 = `2^{2^k}` | 逐位恢复 `d` |
| `d_p` 缓冲区溢出 | 送 33 个 `\x00` 使 `d_p=0`，`m₂=0⁰=1` | `gcd(sig−1, N)` 得 `p` |
| `strlen−1` 截断 | 空串时写到缓冲区前一字节 | 恰好覆盖 `N` 的最后一字节，`N±255` 内可分解 |
| 明文可塑 | 送 `c·k^e` 观察回绕 | `N = 2m₁ − m₂` |
| 加密残差 GCD | `m^e − Enc(m)` 是 `n` 的倍数 | 两个明文取 `gcd` 恢复隐藏的 `n` |
| `p^p` 组合泄漏 | 两个表达式共享秘密因子 | 直接 `gcd`，再用小素数剥余因子 |
| 密钥只覆盖前 64 字节 | 512 bit 密钥上限 | PNG/ZIP/PDF 尾部按格式规范补，别硬解 |
| 多项式哈希有平凡根 | `g(0)=0` | `0^d = 0`，构造明文使 `≡0 mod P` 即得签名 0 |

**Schmidt-Samoa（`n=p²q`）降域技巧**：

```python
phi = p*(p-1)*(q-1)
enc = pow(enc, pow(q, -1, phi), n) % q     # 降到 mod q，m < q
m = pow(enc, pow(p*p, -1, q-1), q)         # 在 φ(q)=q-1 上逆 e·p²
```

---

## 8. 纪律

- **先体检再攻击**：`e`/`d` 位数、模数两两 `gcd`、`iroot`、`phi` 来源、oracle 类型，五项决定后面全部路线。
- 「分解成功但解不出」永远先怀疑 `phi` 变体（`p⁴−1`、`p(p−1)`、`lcm`），不要怀疑分解。
- `p−1` 失败立刻试 `p+1`，再试 ECM；三者覆盖所有小因子场景。
- 有 `e` 份密文才是 Hastad；只有一份但有已知前缀是定式 Coppersmith，界都是 `n^{1/e}`。
- 三种 oracle 靠**返回值语义**区分：奇偶 = LSB，`0x00 0x02` = Bleichenbacher，`m<B` = Manger。查询量差两个数量级，选错会超时。
- 无填充 RSA 的乘法同态既是加密侧也是签名侧的万能钥匙；看到「拒绝某个值」先想同态。
- 低指数签名伪造只对「不检查填充铺满」的实现有效，先确认校验代码。
- 服务端解析私钥字段、`strlen−1`、缓冲区边界这类实现漏洞，往往比密码分析更快出 flag。
- 相关数学见 [[crypto/高阶专题/advanced-math.md|高阶数学攻击]]（LLL / Coppersmith / CRT 实现），协议层 oracle 见 [[crypto/对称加密/modern-ciphers.md|现代密码协议]]。
