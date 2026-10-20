---

title: 后量子识别与 LWE 策略选择
phase: crypto
vuln_class: [crypto, lattice, lwe, ml-kem, ml-dsa, estimator]
summary: 看到 q=3329/N=256/k=2|3|4 就是 ML-KEM。判据是先认方案参数，再按 (n,q,m,误差界,稀疏度) 选 Arora-Ge / MITM / Kannan / Bai-Galbraith / BKZ。
---

# 后量子识别与 LWE 策略选择

格构造与 NTRU/GGH 见 [[crypto/格与后量子/lattice-and-lwe.md|格与 LWE]]；Coppersmith 小根与 `hg_matrix` 见 [[crypto/高阶专题/advanced-math.md|高阶数学]]。

## 0. 判据速查

| 看到什么 | 结论 | 去哪节 |
|---|---|---|
| `q=3329`, `N=256`, `k=2/3/4` | ML-KEM（Kyber） | §1 |
| 多项式环 `Z_q[x]/(x^256+1)`，密钥系数很小 | Module-LWE，要摊平成普通 LWE | §1 |
| 题给「解密成功/失败」的单比特 oracle | FO 变换失败 oracle，走 BDD | §1.2 |
| NTT 结果对不上 | Kyber 的 NTT 是位反序，先 `bitrev` 再 `inv_ntt` | §1.3 |
| 误差界 `B < q/4` 且样本很多（`m >> n`） | Arora-Ge 线性化 + Gröbner | §2 |
| 密钥稀疏（`h < 0.1n`） | 混合 MITM + BKZ | §2 |
| 维度 `< 80` | Kannan 嵌入 + LLL + `CVP.closest_vector` | §2 |
| 维度 `80-150` | Bai-Galbraith 嵌入 + BKZ 20-30 + `babai` | §2 |
| 维度 `> 150` | 上 estimator 定 block_size，BKZ 40+ 或 flatter | §2 |

## 1. ML-KEM / ML-DSA 参数识别

NIST 已标准化的两套：ML-KEM（Kyber）与 ML-DSA（Dilithium）。CTF 只给 `q=3329`、`N=256`、秩 `k` 三个数就够定位。

| 方案 | k | q | η（密钥） | du/dv（压缩） | 环 |
|---|---|---|---|---|---|
| ML-KEM-512 | 2 | 3329 | 3 | du=10, dv=4 | `Z_q[x]/(x^256+1)` |
| ML-KEM-768 | 3 | 3329 | 2 | du=10, dv=4 | 同上 |
| ML-KEM-1024 | 4 | 3329 | 2 | du=11, dv=5 | 同上 |

`η` 是中心二项分布的参数，`du/dv` 是密文压缩位数——**这两个数决定误差有多大**，直接决定后面能不能用格。

### 1.1 把 Module-LWE 摊平成普通 LWE

`A ∈ R_q^{k×k}` 要变成 `A_flat ∈ Z_q^{256k×256k}`：每个多项式展开成 256×256 的**负循环矩阵**（negacyclic）。

```python
def flatten_mlkem(A_poly, s_poly=None, e_poly=None, q=3329):
    """A_poly: k×k 个长度 256 的多项式；返回 (A_flat, s_flat, e_flat)。"""
    k, N = len(A_poly), 256
    dim = k * N
    A_flat = [[0] * dim for _ in range(dim)]
    for i in range(k):
        for j in range(k):
            poly = list(A_poly[i][j]) + [0] * (N - len(A_poly[i][j]))
            for r in range(N):
                for c in range(N):
                    if r >= c:
                        A_flat[i*N + r][j*N + c] = poly[r - c] % q
                    else:
                        A_flat[i*N + r][j*N + c] = (-poly[N + r - c]) % q   # 负循环的符号
    def flat(polys):
        out = []
        for p in polys:
            out.extend(list(p) + [0] * (N - len(p)))
        return out
    return A_flat, flat(s_poly or []), flat(e_poly or [])
```

**`r < c` 时取负号是负循环的定义**，写成 `poly[N + r - c]` 而不取负就变成普通循环卷积，整个格都错。

摊平后就是标准 `b_flat = A_flat · s_flat + e_flat`，`s/e` 各系数是 `η` 内的小值——直接套 §2 的策略树。

### 1.2 FO 失败 oracle

ML-KEM 用 Fujisaki-Okamoto 变换把 CPA 的 PKE 变成 CCA 的 KEM。解封装时会重新加密比对，**失败与否取决于 `du/dv` 压缩丢掉了多少信息**——压缩是有损的，所以解密失败概率不为零。

题给「这次解密成功了吗」的单比特 oracle 时，这不是普通 padding oracle，而是**误差 `e` 的信息泄漏**，走 BDD（bounded distance decoding）：每次查询把可行 `e` 的空间切一半。

### 1.3 NTT 位反序

Kyber 的 NTT 输出是**位反序**顺序。题里给 `A_ntt` 却不给 `bitrev` 时，直接 `inv_ntt` 会得到乱序结果。

**症状是「解出来的多项式每个系数都像噪声」**，不是报错。先按 7 位反序重排（256 个点）再逆变换。

## 2. 攻击路线选择

### 2.1 三种嵌入的取舍

**Kannan 嵌入**：`[[B 0],[t 1]]`，通用 CVP → BDD。适合「密钥和误差量级差不多」。

**Bai-Galbraith**：调 `λ` 平衡 `‖e‖` 和 `‖s‖`。`q` 中等（如 3329）而密钥很小时明显更优。启发式取 `λ = q / (2·bound)`，实际扫 `1, q/4, q/2` 三档。

**Hybrid MITM**：密钥稀疏（`h=20` 在 `n=512`）时把 `s = s1 + s2` 拆开，对投影格做 BKZ 再中间相遇。

### 2.2 Arora-Ge

误差界 `B < q/4` 且样本远多于维度时，把 `(<a_i,s> - b_i)` 在 `Z_q` 上连乘成多项式方程组，用 Gröbner 或线性化解。

**适用区间是 `m >> n^d`**（如 `n=64, B<8`）。`B ≥ q/4` 时方程组次数爆炸，回格方法。

### 2.3 决策树

```text
LWE 实例 (n, q, m, Xs, Xe)
 ├─ B < q/4 且 m > 2n          → Arora-Ge（线性化 + Gröbner）
 ├─ 密钥稀疏 (h < 0.1n)         → hybrid MITM + BKZ
 ├─ 维度 < 80                  → Kannan 嵌入 + LLL，再 CVP.closest_vector
 ├─ 维度 80-150                → Bai-Galbraith 嵌入 + BKZ 20-30 + GSO.Mat.babai
 └─ 维度 > 150                 → estimator 定 block_size，BKZ 40+，dim>100 可上 flatter
```

`dim < 80` 时 **LLL 直接够**，别一上来就 BKZ。反过来 `dim > 150` 时手工调参数不如直接问 estimator。

### 2.4 用 estimator 定参数

```python
# 离线跑：python -m estimator
# 输入 n=512, q=3329, Xs=CBeta(eta=2), Xe=CBeta(eta=2), m=256k
# 输出 BKZ 40 / Arora-Ge / hybrid 各自代价
from estimator import LWE
LWE.estimate(n, q, Xs, Xe, m)
```

**它的价值是告诉你「哪条路更便宜」**，而不是给你答案。CTF 里时间有限，先跑一次估一下再决定投不投入。

## 3. 纪律

- **先认参数再选方法**。`q=3329/N=256` 一出现就别再想 RSA 那套；反过来看到 `n=64` 的小 LWE 直接 Kannan+LLL，上 estimator 是浪费。
- **摊平必须用负循环**。`r < c` 那一支漏掉负号，格基是对的但解出来的 `s` 全错——这种错最难查，因为程序不报错。
- **NTT 位反序要显式处理**。解出来全是噪声时先怀疑顺序，别怀疑算法。
- **维度决定工具**：`<80` 用 LLL，`80-150` 用 BKZ 20-30，`>150` 先问 estimator。跳档使用要么太慢要么解不出。
- **误差界 `B` 是分水岭**：`B < q/4` 才考虑 Arora-Ge，否则乖乖走格。
