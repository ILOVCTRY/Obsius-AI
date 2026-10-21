---
title: 格与 LWE 攻击
phase: crypto
vuln_class: [lattice, lll, lwe, hnp, subset-sum, ntru, ggh, post-quantum]
summary: 格攻击的第一步不是建格，而是回答“什么东西本应很小”：密钥、误差、nonce 残差还是 0/1 指示向量。找到这个“小量”，再选 embedding、CVP 或正交格。
---

# 格与 LWE 攻击

先用这场判据决定是否走格路线：题目给出大量模方程并承诺隐藏值“小、稀疏或彼此接近”，或只泄漏了密钥/nonce/状态的一部分位。题面常见说法：高位已知、误差很小、系数在 `{-1,0,1}`、从截断输出恢复种子、求短向量、解带噪线性方程。

## 0. 判据速查

| 现象 | 建格目标 | 主体模型 |
|---|---|---|
| 签名多次泄漏 nonce 高位 | `d` + 小残差 `delta` | HNP / ECDSA partial nonce |
| LCG 输出被截断 | 隐藏低位 `z_i` 小 | 截断 LCG |
| `b = A*s + e`，`s`/`e` 小 | 短秘密-误差对 | LWE embedding |
| 多项式环 `x^n±1` 上的同款关系 | 系数向量 | Ring/Module-LWE → 先扁平化 |
| `h = αA`，条目在 `{0,1}` | 正交补揭示隐藏基 | HSSP/正交格 |
| 子集和/背包，0/1 指示向量 | 末坐标为 0 的短行 | 低密度 knapsack |
| `h = g*f^{-1} mod q`，`f,g` 小 | `(f,g)` 短向量 | NTRU 格 |
| `c = m*B + e`，`e` 小 | 找格上最近点 | GGH 嵌入 |
| 只有 1 位谓词/区间信息 | 带谓词的 BDD | BDD predicate/LadderLeak |
| `h_i = Σ x_j^{a_ij}` 且指数大 | 隐藏子集和之积 | HSSP |
| DLP 已知 `k` 落在小区间（如 41 bit 时间戳） | 区间约束 | **Pollard kangaroo**（比 LLL 更省） |
| 同一 `x` 被 `k` 个不同模数约简 | CRT 后格 | 多模数 HNP |

### 0.1 选 LLL 还是 kangaroo：先看泄漏形态

**判据**：泄漏是「多条**线性**关系，每条带小误差」→ 建格走 LLL/BKZ；
泄漏是「**一个**离散对数值落在一个已知区间」→ 用 Pollard kangaroo，复杂度 `O(sqrt(区间宽度))`。

| 场景 | 工具 | 为什么 |
|---|---|---|
| 多次签名 nonce 高位已知 | LLL | 多条约束可以互相抵消误差 |
| 一次签名 nonce 是 41 bit 时间戳 | kangaroo | 区间只有 `2^41`，`sqrt` 后 `2^20` 步 |
| nonce 是完整 256 bit 但只有 1 bit 谓词 | BDD / LadderLeak | 位信息太弱，需要专用 |
| 只有 1-2 条约束 | **都不行** | 约束不够，回去找更多数据 |

**kangaroo 的参数判据**：区间宽度 `W`，期望步数约 `2·sqrt(W)`。
`W = 2^41` → 约 200 万步，秒级；`W = 2^128` → 不可行，必须换思路。

**HSSP（Hidden Subset Sum Problem）**：`h_i = Σ_j x_j^{a_ij}`，`x_j` 是未知的小数。
判据是**指数 `a_ij` 很大但模数不大**、`h_i` 数量多于未知数个数。
建格时把 `x_j` 当成基、`a_ij` 当指数，取对数后变成线性（或直接在整数上构造子集和格）。

## 1. 工具链与最小动作

主栈 `fpylll`；Sage 可替代。先用 LLL，不够再 BKZ(`block_size=20..35`)，Babai 用于近似最近向量：

```python
from fpylll import IntegerMatrix, LLL, BKZ, CVP, GSO

B = IntegerMatrix.from_matrix(rows)
LLL.reduction(B)
# 或 BKZ.reduction(B, BKZ.Param(block_size=25))
closest = CVP.closest_vector(B, target)      # 精确，维数小
approx = GSO.Mat(B).babai(target)            # 近似，先归约
```

选 SVP 还是 CVP：只知道“某关系必须很短”用 SVP/embedding；已有目标向量、要找最近格点用 CVP/Babai。Babai 前必须归约，否则结果无意义。

## 2. HNP：nonce/状态部分泄漏

统一形状是 `a_i*x + b_i ≡ e_i (mod q)`，`x` 是长期秘密，`e_i` 小或部分已知。标准 lattice：前 `n` 行放 `q`，最后一行放系数与权重：

```python
def hn_p_lattice(q, coeffs, bounds):
    n = len(coeffs)
    rows = [[0] * (n + 1) for _ in range(n)]
    for i in range(n):
        rows[i][i] = q
    rows.append(list(coeffs) + [bounds])
    return IntegerMatrix.from_matrix(rows)
```

ECDSA 高位 nonce 泄漏时写 `k_i = leaked_i*2^t + delta_i`，代入 `s_i*k_i - h_i ≡ r_i*d (mod q)`：

```text
r_i*d - s_i*delta_i ≡ s_i*leaked_i*2^t - h_i (mod q)
```

未知量变成密钥 `d` 与一组小残差 `delta_i`；按上述 HNP 建格（行缩放让各坐标同量级），LLL 后取短行候选 `d`，用全部签名校验，剩余个别位不确定就小范围暴力。

最小代码：

```python
def ecdsa_partial_nonce_lattice(q, rs, ss, hs, leaked, t):
    n = len(rs)
    rows = [[0] * (n + 2) for _ in range(n + 2)]
    for i in range(n):
        rows[i][i] = q
    for i in range(n):
        rows[n][i] = ss[i] % q
        rows[n + 1][i] = (hs[i] - ss[i] * leaked[i] * (1 << t)) % q
    rows[n][n] = 1
    rows[n + 1][n + 1] = q // (1 << t)
    return IntegerMatrix.from_matrix(rows)
```

## 3. 截断 LCG：换个外衣的 HNP

`x_{i+1} = a*x_i + b (mod m)`，只知高位 `y_i = x_i >> t`。写 `x_i = y_i*2^t + z_i`，`z_i` 小，代入得：

```text
z_{i+1} - a*z_i ≡ a*y_i*2^t + b - y_{i+1}*2^t (mod m)
```

按小未知量 `z_i` 建格即可，LLL 后还原完整状态并精确验证递推。可扩展为 `a`、`b` 也未知（增加对应列并保持小权重）或素数模/多输出混合。

## 4. LWE 与嵌入格

给定 `A`、`b`、`q`，承诺 `b = A*s + e (mod q)` 且 `s`、`e` 小。Embedding lattice：

```python
def lwe_embedding(A, q):
    m, n = len(A), len(A[0])
    dim = m + n
    rows = [[0] * dim for _ in range(dim)]
    for i in range(m):
        rows[i][i] = q
    for j in range(n):
        for i in range(m):
            rows[m + j][i] = A[i][j]
        rows[m + j][m + j] = 1
    return IntegerMatrix.from_matrix(rows)
```

归约后用 Babai 找目标附近格点，再从近零值反推 `s`/`e`。三元或稀疏秘密要把结果映射回 `{-1,0,1}` 并校验行列约定、端点序。CTF 的 LWE 通常远低于真实安全线，先确认秘密或误差是否“故意很小”。

Ring/Module-LWE：对象是 `Z_q[x]/(x^n±1)` 多项式、循环/负循环卷积时先扁平化。`x^n = -1` 的负循环矩阵：

```python
def negacyclic_matrix(coeffs, n, q):
    rows = []
    for i in range(n):
        row = [0] * n
        for j in range(n):
            if j <= i:
                row[j] = coeffs[i - j] % q
            else:
                row[j] = (-coeffs[n + i - j]) % q
        rows.append(row)
    return rows
```

之后按普通 LWE 处理。多数环形题目是参数、符号约定、中心化或 NTT 使用错误导致难度崩塌，先验证这些再谈专用攻击。

NTRU：`h = g*f^{-1} mod q`，`f,g` 小。建 `B = [[qI, 0], [H, I]]`，短向量即 `(g, f)` 的旋转/符号变体。`H` 对 `x^N+1` 取负循环、`x^N-1` 取循环；NTRU Prime 的 `x^p-x-1` 需按其约化构造。Kyber/ML-KEM 是 `x^256+1`、`q=3329`，按 rank 长度扁平化。LLL 后扫短行，并用 `h*f ≡ g`、汉明权重校验；`[-q/2,q/2]` 中心化缺失会直接埋掉短向量。

GGH/LWE CVP：嵌入格扫 `lambda`（`1,2,4,...,128`），`B_emb = [[B_pub, 0], [c, lambda]]`。小 `lambda` 换取嵌入间隙；每个 `lambda` 单独归约并选误差最小候选。

## 5. 正交格与稀疏结构

`h = αA (mod M)`、条目在 `{0,1}` 时，目标是恢复隐藏基。构造格 `[M*I_k | 0; H^T | I_n]`，归约后的短行给出正交补，再取 kernel/正交补并二次归约即可暴露隐藏二进制基。要点：最短向量不是答案，而是通向答案的门。

子集和：`sum(a_i*x_i) = target`，`x_i ∈ {0,1}`。低密度时可建格 `rows[i][i]=1, rows[i][n]=a_i, rows[n][n]=-target`，LLL 后找末坐标为 0、前 `n` 坐标为 0/1 的行。高密度时应先评估 meet-in-the-middle 或参数规模。

## 6. BDD 谓词与梯子泄漏

只有少于 1 位的信息（如 `k < q/2` 为 1）时，可用带谓词的 BDD lattice：收集足够多签名（数百级），把谓词作为额外约束行建格，对少数不确定性位枚举后用 ECDSA 验证 `d`。样本数、谓词 bits 和维度共同决定成功率；不可验证的结果只算候选。

## 7. 常见失败模式

- 忘记中心化：`[0,q)` 与 `[-q/2,q/2]` 混用会让短向量看不见。
- 缩放不一致：`q*I` 块与目标小量量级差太多时 LLL 找错目标；按坐标同量级缩放。
- 符号/端序约定错：`x^n+1` 与 `x^n-1`、行列向量、正负 `a` 均改变 `H`。
- Babai 前未归约；或维度已超工具精确 CVP 能力仍用精确算法。
- 候选未验证：短行可能是噪声，必须用原始方程、签名、`h*g mod p` 等独立校验。
- 维度不足：样本/方程少于隐藏未知量时，先补数据再建格。

## 8. 纪律

格题先写出“什么必须小、小到什么程度、需要多少独立样本”，再写第一行代码。每步保留中间矩阵、候选向量与校验结果；LLL 失败时依次检查缩放、中心化、维度和冗余约束，而不是盲目加大 BKZ。ML-KEM/MLWE 的秩与参数场景见 [[crypto/格与后量子/后量子识别与 LWE 策略选择.md|后量子密码]]。
