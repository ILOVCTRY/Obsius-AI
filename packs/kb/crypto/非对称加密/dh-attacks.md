---

title: 有限域 DH 攻击
phase: crypto
vuln_class: [crypto, diffie-hellman, pohlig-hellman, small-subgroup, logjam]
summary: 三步定生死——先试 g 退化成 0/1/p-1，再看 p-1 是否光滑（Pohlig-Hellman），最后看静态密钥 + p-1 有小因子（Lim-Lee 用 CRT 拼出私钥）。
---

# 有限域 DH 攻击

ECDH 的无效曲线/小子群见 [[crypto/非对称加密/ecc-attacks.md|ECC 攻击]]；BSGS 与 Pohlig-Hellman 的通用原语见 [[crypto/高阶专题/advanced-math.md|高阶数学]]。

## 0. 判据速查

| 看到什么 | 结论 | 去哪节 |
|---|---|---|
| 服务端接受 `g = 0 / 1 / p-1` | 共享密钥变成已知常量 | §1 |
| 服务端不检查 `2 ≤ A ≤ p-2` | 同上，直接发退化值 | §1 |
| `factorint(p-1)` 全是大素数 | 走不通，换思路 | — |
| `p-1` 光滑（因子都 < 40 位） | Pohlig-Hellman + CRT | §2 |
| 每连接重新生成 `p` | 重连到 `p-1` 光滑为止 | §2 |
| 静态密钥 + `p-1` 有多个小因子 | Lim-Lee：逐个 `b mod r` 再 CRT | §3 |
| `p` 只有 512-768 位或是 RFC 5114 已知群 | Logjam，预算好一次 DLP 就行 | §4 |
| 临时密钥（DHE/ECDHE） | 收集次数被限死，只能单次限制 | §4 |

**顺序不能乱**：先试 §1 的三分钟检查，再跑 `factorint`，最后才考虑格或 BSGS。

## 1. 退化生成元

服务端接受攻击者影响的 `g` 或对端公钥且不做校验时：

- `g = 0` → 所有共享密钥都是 0
- `g = 1` → 所有共享密钥都是 1
- `g = p-1`（阶为 2）→ 私钥偶则共享密钥 1、奇则 `p-1`：每次查询漏 1 bit
- 对端发 `A = 0 / 1 / p-1 / p`（`≡ 0 mod p`）→ 共享密钥是已知常量

```python
# 探测：发 A = p-1，用 S = 1 与 S = p-1 分别试 oracle
for guess_secret in (1, p - 1):
    if oracle_accepts(derive(guess_secret)):
        parity = 0 if guess_secret == 1 else 1
        break      # 私钥 mod 2 = parity
```

**`g = p-1` 的 1-bit 泄漏配「私钥低位可预测」时能直接全破。** 这一步不花时间，永远先试。

## 2. `p-1` 光滑时的 Pohlig-Hellman

思路与通用 DLP 一致：分解 `p-1`，在每个素数幂子群上做 BSGS，最后 CRT 合并。

```python
def pohlig_hellman_dh(g, h, p):
    """p-1 光滑时解 g^x = h mod p。
    手写 BSGS（不用 sympy.ntheory.discrete_log：它对合数阶的合法输入会抛
    "Log does not exist"，例如 discrete_log(211, 2, 107)）。"""
    from math import isqrt
    from sympy import factorint
    from sympy.ntheory.modular import crt

    order = p - 1
    res, mod = [], []
    for prime, exp in factorint(order).items():
        pe = prime ** exp
        co = order // pe
        m = isqrt(pe) + 1
        base = pow(g, co, p)
        table, cur = {}, 1
        for j in range(m):
            table.setdefault(cur, j)
            cur = cur * base % p
        factor = pow(pow(base, -1, p), m, p)
        gamma = pow(h, co, p)
        xi = None
        for i in range(m):
            if gamma in table:
                xi = i * m + table[gamma]
                break
            gamma = gamma * factor % p
        res.append(xi); mod.append(pe)
    x, _ = crt(mod, res)
    return int(x % (p - 1))
```

**CTF 常见形态是「服务端每次连接重新生成 `p`」**——重连到 `p-1` 光滑为止，别去攻击固定的弱 `p`。

**`sympy.discrete_log` 在合数阶上会误报「不存在」**，这是必须手写 BSGS 的原因，不是风格偏好。

## 3. Lim-Lee 小子群限制（静态密钥 + CRT）

静态服务端密钥 `b` 跨会话复用时：`p-1 = q · h`，`h` 里有很多小因子。攻击者发一个阶为 `r`（`r | h`）的元素，服务端算 `S = element^b`，oracle 会暴露 `b mod r`——枚举 `r` 个候选即可。对互素的 `r_i` 重复，CRT 拼出完整 `b`。

```python
def subgroup_generator(g, p, r):
    """阶恰为 r 的元素（r | p-1）。"""
    assert (p - 1) % r == 0
    h = pow(g, (p - 1) // r, p)
    assert pow(h, r, p) == 1 and h != 1
    return h

def recover_mod_r(malicious_pub, r, oracle_derive_check):
    for j in range(r):
        if oracle_derive_check(pow(malicious_pub, j, p)):
            return j
    return None
```

```text
对 (p-1)/q 的每个小素因子 r：
  w = subgroup_generator(g, p, r)
  把 w 当对端公钥发出去，服务端算 S = w^b
  b_mod_r = recover_mod_r(w, r, lambda S: try_decrypt_or_verify(S))
  residues.append(b_mod_r); moduli.append(r)
b, _ = crt(moduli, residues)     # prod > q 时是全钥；否则 BSGS 补余下部分
```

**Oracle 的三种形态**：返回 `HMAC(S, msg)`、用 `KDF(S)` 加密已知明文、或对攻击者密文返回成功/失败。`r` 个候选各给出一个 `KDF(S_j)`，服务端接受的那个就是 `b mod r`。

**识别「服务端做了防护」**：`p = 2q+1` 安全素数（只剩 1 bit 泄漏）、有 `A^q == 1 mod p` 的子群成员检查、做 cofactor 清除、用临时密钥。

## 4. 静态 vs 临时 + Logjam

- **静态 DH**：长期 `b` 复用 → Lim-Lee 跨会话收集 `b mod r_i`。
- **临时（DHE/ECDHE）**：每次会话只能做一次限制，收集不到足够余数。
- **无认证的临时密钥**：中间人把两边的 `A`/`B` 都换成小子群元素（如 `-1`），双方各自从极小集合里推出 `S`。
- **Logjam（export 级）**：服务端接受 512 位 `p`，或协商能被降级到弱群。每个 `p` 做一次 NFS/DLP 预计算，之后每次连接都很便宜。CTF 类比：题目钉死小 `p`（512-768 位）或复用 RFC 5114 群——先看 `p.bit_length() <= 768` 和是否已知群，再谈格。

## 5. 纪律

- **顺序是 §1 → `factorint(p-1)` → §3 → §4**。跳过 §1 直接上 BSGS 是最常见的浪费。
- **`g` 和公钥都要试退化值**。服务端只校验了 `g` 没校验 `A` 的情况很常见。
- **`sympy.discrete_log` 不可信**。合数阶上会误报「不存在」，自己写 BSGS。
- **静态密钥是 Lim-Lee 的前提**。看到「临时密钥」就别做多会话收集，收不满。
- **`p` 只有 512-768 位先想 Logjam 而不是格**，位数本身就是线索。
- **报的是「哪个因子那一档失败」**，不是「DH 打不通」。CRT 之后验证 `g^b == A` 能立刻定位是哪个余数错。
