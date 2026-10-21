---
title: PRNG 攻击（CTF 手法）
phase: crypto
vuln_class: [crypto, prng, mt19937, lcg, lfsr, z3]
summary: PRNG 题的判据是「一次观测泄漏了多少位」——满 32 位就凑 624 个直接反推，只漏几位就做约束传播或 GF(2)，漏的是 mod 值就先算清哪几位被丢掉。
---

# PRNG 攻击（CTF 手法）

基础篇（MT19937 标准反推、LCG 参数恢复、ChaCha20、V8 XorShift128+、口令爆破）见 [[crypto/随机数/伪随机数生成器与密钥恢复.md|随机数与密钥恢复]]。本篇只收「输出被遮蔽/只泄漏部分位/状态被外部污染」这类非标准情形。

## 0. 判据速查

| 观测到什么 | 结论 | 去哪节 |
|---|---|---|
| 每次只泄漏 8-24 位，但能多次观测 | 状态建候选集，双向约束传播 | §2 |
| 输出是 `mod 62` / `mod N` 的值 | 先算清模运算丢了哪几位 | §3 |
| 输出字节是 ASCII（高位恒 0） | 每个字节白送一个线性方程 | §4 |
| 输出是线性移位 XOR 折叠 | GF(2) 高斯消元，不要爆破 | §4 |
| 种子 = 时间 + 可写全局 | 改全局 → 种子完全可控 | §5 |
| 随机数被 XOR 进用户可控值 | 直接拿到状态 | §6 |
| 有 DSA/ECDSA 签名且 k 来自 `random` | 喂 624 个 32 位给 randcrack | §7 |
| 拿到了某个中间状态，要往前 | 乘逆回退，不用从头模拟 | §3 |
| 猜错只能靠「快慢」区分 | 计时 oracle，错误猜测 SAT 得快 | §8 |
| 状态转移是元胞自动机 | Z3 布尔数组，符号执行 N 轮 | §9 |
| 只解出 2 个状态字，想还原 32 位种子 | MT 环绕关系，2^32 可爆 | §1 |

## 1. MT19937 种子还原：只要两个字

**环绕关系是唯一入口**：`mt[i] = mt[i-624] XOR twist(mt[i-624], mt[i-623])`。在回绕处 `mt[624]` 依赖**新周期**的 `mt[0]` 和**旧周期**的 `mt[397]`，而 `mt[397] = mt[624-227]` 正是 index 227 那条。所以拿到 index 0 与 index 227 两个值，就能把种子搜索降到 2^32 且有唯一解。

```python
import random
def crack_seed(mt0, mt227):
    for seed in range(2**32):
        r = random.Random(); r.seed(seed)
        out = [r.getrandbits(32) for _ in range(228)]
        if out[0] == mt0 and out[227] == mt227:
            return seed
```

**为什么这两个 index**：不是随便挑的，是「一个在新周期头部、一个恰好被回绕公式引用」的最小组合。换成别的 index 对，关系链断掉，2^32 也搜不出唯一解。

**触发条件**：题目把 MT 输出藏进可解的数学题（子集和、方程）里，每解一题就吐出一个状态字。看到「解出来是 32 位整数」就该往这条路上想。

## 2. 部分位泄漏：候选集 + 双向传播

不凑 624 个完整输出，而是把每个状态字建成候选集，用递推式在**两个方向**收窄。

```python
def propagate_forward(cands, idx):
    # state[idx+624] = f(state[idx], state[idx+1])
    for s0 in cands[idx]:
        for s1 in cands[idx+1]:
            cands[idx+624].add(mt_twist(s0, s1))

def propagate_backward(cands, idx):
    # 由 state[idx] 与 state[idx-623] 反解 state[idx-624]
    for val in cands[idx]:
        for s1 in cands[idx-623]:
            cands[idx-624].add(mt_untwist(val, s1))
```

**量化判据**：每次泄漏 `b` 位，大约需要 `624*32/b` 量级以下的观测才能收敛，实测 24 位 × 20 次左右即可定死全状态。泄漏 8 位就要上百次，此时先确认题目真的给得起这么多次交互。

**反向传播必须实现 `mt_untwist`**：twist 含右移 XOR，反解要逐位回代（`y ^= y >> k` 的逆是 `y ^= y>>k; y ^= y>>2k; ...` 直到覆盖字宽），不是简单减。

## 3. LCG：模运算丢了哪几位

**Java `Random` 的关键结构**：`next(bits)` 返回 `(seed*a + c) >> 16` 再取高位。也就是说**低 16 位被直接丢弃**，所以低 18 位只影响每次 `nextInt()` 的最低位。这就是「48 位种子搜索」被拆成 `2^18 + 2^30` 的原因——不是优化技巧，是结构决定的。

```python
# 阶段一：2^18 低位候选，用 nextInt(62) 的奇偶性筛
for low in range(1 << 18):
    if simulate(low)[:K] == known_prefix:
        candidates.append(low)
# 阶段二：每个候选补高位，2^30
for low in candidates:
    for high in range(1 << 30):
        if simulate((high << 18) | low) == full_known:
            return (high << 18) | low
```

**回退**：任何模 2 的幂的 LCG 都可逆，`prev = a^{-1} * (state - c) mod m`。Java 的乘逆是 `pow(25214903917, -1, 1<<48)`（数值上等于 `-35320271006875 mod 2^48`）。拿到任意中间状态就能双向走，不用从种子重放。

## 4. LFSR / 折叠输出：GF(2) 消元

状态 32 位、输出 8 位，看着像「信息不够」，但**每个输出位仍是状态位的线性组合**，折叠没有引入非线性。

```python
import numpy as np
A = np.array(constraint_rows, dtype=np.uint8)
b = np.array(known_bits,      dtype=np.uint8)
state = gf2_solve(A, b)
```

**免费方程的来源**：输出字节是 ASCII → 高位恒 0 → 每个字节白送一个方程。若还能确定字符集（小写字母 → 高 3 位固定），方程数量直接翻几倍。这是「折叠输出」题真正的破口，比爆破 2^32 快得多。

## 5. 种子偏移：把时间 RNG 变成确定 RNG

`srand((time(0)/10) + bet)` 里 `bet` 是可写全局时，用格式化字符串把它写成已知值，种子就完全可控。

```python
# 1) %Xc%Y$n 写入 &bet
# 2) 本地用目标同一份 libc 预测
from ctypes import CDLL
libc = CDLL('libc.so.6')
libc.srand((int(time.time())//10) + bet_value)
predicted = [libc.rand() for _ in range(n)]
```

**必须用目标的 libc**：`rand()` 的实现随 glibc 版本变（TYPE_3 的加法反馈系数改过），宿主机 libc 猜出来的序列在几十次后会分叉。

## 6. 随机数被 XOR 进可控值

`uuid = time ^ hash_state`：把服务端的时间源指向自己控制的 NTP 并返回 `0`，返回的 UUID 就等于 `hash_state`。

```python
# 1) 指向返回 0 的 NTP
# 2) 注册，拿到 uuid == 内部状态
# 3) 目标 uuid 反推所需 timestamp = uuid ^ state
```

**通用判据**：任何「随机值 ⊕ 用户可控值」都等价于直接送出状态。检查所有外部输入的时间/序号/配置来源，不只是 `rand()`。

## 7. 同进程 RNG 污染（DSA k 预测）

Python 的 `random` 是全局单例，任何泄漏 `random` 输出的接口都会污染 DSA 签名用的 `k`。

```python
from randcrack import RandCrack
rc = RandCrack()
for _ in range(624 // 2):
    v = getrand64()
    rc.submit(v & 0xffffffff); rc.submit(v >> 32)
k = rc.predict_randrange(2, q)
x = ((s*k - h) * pow(r, -1, q)) % q
```

**注意 `submit` 要 32 位对齐**：拿 64 位输出时按低/高各提交一次，顺序不能反。`randcrack` 需要**连续** 624 个 32 位，中间插了别的 `random` 调用就全废——所以要找那个「只做一件事」的泄漏接口。

## 8. 计时 oracle：错猜更快

有些服务内部跑 Z3，猜错时约束立刻可满足（SAT 很快），猜对时反而进入 UNSAT 硬搜索。

```python
for c in string.printable:
    s = Solver(); s.set('timeout', 500)
    s.add(prng_constraints(flag + c, ciphertext))
    t = time.time()
    if s.check() == sat and (time.time() - t) > 0.4:
        flag += c; break
```

**阈值要现场标定**：先跑几个已知错的候选记录基线耗时，再取「明显高于基线」的值。写死 0.4 秒只在特定机器负载下成立，且必须多次取中位数，单次抖动足以判错。

## 9. 元胞自动机 PRNG：Z3 符号执行

Rule 86 的 DNF 是 rule 号 `86 = 01010110` 的 4 个置位项：

```python
from z3 import *
def RULE86(x, y, z):
    return Or(And(Not(x), Not(y), z), And(Not(x), y, Not(z)),
              And(x, Not(y), Not(z)), And(x, y, Not(z)))

state = [Bool(f'b{i}') for i in range(256)]
for _ in range(128):
    state = [RULE86(state[(i-1)%256], state[i], state[(i+1)%256]) for i in range(256)]
s = Solver()
for i, bit in enumerate(known_output):
    s.add(state[i] == (bit == 1))
s.check()
```

**元胞自动机不是单射**：可能有多个前像，`s.check()` 返回 SAT 不代表唯一。要么多解都试一遍，要么用额外的输出位约束。`s.push()`/`s.pop()` 用来逐轮回溯。

**这套写法通用于任何初等 CA**：把真值表编码成布尔式、符号执行 N 轮、约束到已知输出。轮数超过 200 或宽度超过 512 时 Z3 会显著变慢，考虑改成逐位代数反演。

## 10. 纪律

- **先数位数**。「每次泄漏几位 × 能观测几次」决定路线，不是凭手法名选。总位数远小于状态位宽时，别指望恢复全状态，改找「种子空间小」这个别的破口。
- **模运算先算丢了几位**。`mod 62`、`>> 16`、`& 0xff` 各自丢弃的位不同，这直接决定能不能拆分搜索。没算清就写爆破循环，通常会爆到 2^48 跑不完。
- **线性就消元，别爆破**。输出是状态位的线性组合时，GF(2) 高斯消元是多项式时间，爆破是指数时间。
- **本地复现必须用同一实现**。libc `rand()`、Java `Random`、Python `random` 的序列在不同版本/不同语言下都不同，拿错实现验证会在几十次后分叉，且分叉点很靠后容易误判成「快成功了」。
