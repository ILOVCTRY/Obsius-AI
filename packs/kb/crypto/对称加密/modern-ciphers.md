---
title: 分组密码、MAC 与 AEAD 攻击
phase: crypto
vuln_class: [block-cipher, mac-forgery, padding-oracle, aead-nonce-reuse]
summary: 先按模式定攻击面（ECB 拼块 / CBC 翻位 / CTR 重放流 / OFB 可逆态），再看 MAC 是否线性、AEAD 是否 nonce 复用
---

# 分组密码、MAC 与 AEAD 攻击

## 0. 判据速查

| 现象 | 攻击 | 代价 |
|---|---|---|
| 相同明文块出相同密文块 | ECB 拼块 | 块级自由拼接 |
| 服务端区分「填充对不对」 | CBC padding oracle | ≤ 4096 次/块 |
| 服务端区分「UTF-8/JSON/base64 是否合法」 | 非填充类 oracle | 同上，判据换成格式合法性 |
| 无 MAC 的 CBC，返回 IV+密文 | IV 翻位改第 1 块；前块翻位改第 n 块 | 1 次 |
| CBC 密文可截断 | 无长度字段，尾部按 PKCS#7 合法即可 | 1 次 |
| 固定头恰好占 1 块 | 把该块提为新 IV 剥掉头 | 1 次 |
| CTR 计数器不递增 | 16 字节重复密钥流 | 已知明文即得 |
| CTR/OFB + CRC 做完整性 | 翻位 + CRC 线性修正 | 1 次 |
| 同 nonce 两段 GCM 密文 | 禁断攻击：H 可恢复 | 2 段 |
| 同 nonce 两段 ChaCha20-Poly1305 | 素数域 `2^130−5` 求 `r` | 2 段 |
| GCM 但模数是素数而非 GF(2^128) | 一次模逆解 H | 2 段 |
| MAC 用 CRC | 线性，可伪造/反推密钥 | 1 组 |
| MAC 是 `H(secret‖data)` | 长度扩展 | 1 组 |
| 聚合校验是 `XOR(hash_i)` | SHA-256 基攻击 | ~256 个文件 |
| 自定义 hash 只有 XOR/循环移位 | GF(2) 线性，直接求逆 | 一次消元 |
| 明文尾部填充已知 | OFB + 可逆 RNG 反向解全篇 | 1 块已知 |
| 4 轮 AES | Square（积分）攻击 | 256 明文/字节 |
| 轮数不足的轻量级（Ascon 4 轮） | 差分偏差 + 聚类定 key 位 | 多组采样 |
| 验证逐字符且正确字符更慢 | 计时 oracle 逐位 | 每字符一次 |

---

## 1. 模式决定攻击面

### 1.1 ECB

相同明文块 → 相同密文块。两类用法：

- **图像/结构化数据**：直接看图案，重排块。
- **逐字节恢复**：服务端加密 `input ‖ secret`，控制前缀长度把未知字节顶到块尾，每字节试 256 次比对整块。总查询 ≈ `256 × len(secret)`。
- **剪切粘贴**：字段跨块可预测时，把 `false` 所在块替换成构造出的 `true` 块。JSON 容忍多余空白，可用空格把目标对齐到 16 字节边界。

**探测 ECB**：注册 64 个相同字符的用户名，看密文里是否出现重复块；再变长度看块数何时跳变，即可标定边界。

### 1.2 CBC

`P_n = Dec(C_n) XOR C_{n-1}`（`C_0 = IV`）。三个推论：

1. **翻 IV 只改第 1 块**，后续块不受影响。`admin` 与 `` `dmin `` 只差最低位，翻 IV 同位即可提权。
2. **翻 `C_{n-1}` 改 `P_n`**，但 `P_{n-1}` 会变成随机垃圾。只要 JSON/解析器容忍前面块损坏（未知字段、被忽略的垃圾），就能改 `"admin": 0` → `1`。
3. **没有长度字段**，从尾部截掉若干块仍然合法（新末块 PKCS#7 正确即可）。

**剥固定头**：若 `nonce` 恰好占满 1 块，令 `IV' = C[0:16]`、`ct' = C[16:]`，就得到不含 nonce 的合法密文——因为 CBC 除首块外都用前一块密文当 IV。

**oracle 不限填充**：UTF-8 合法性、JSON 可解析、base64 可解码、纯 ASCII，任何「服务端可区分」都能当 oracle。逐字节：改 `C_{n-1}[i]`，试 256 个值，看哪种响应不是「解码失败」，反推中间值。

### 1.3 CTR / OFB

- **计数器常量**：`counter=lambda: secret` 让每块 keystream 相同 → 退化成 16 字节重复 XOR。用文件头（`%PDF-1.`、`PK`、`GIF89a`）起手，按块对齐做 crib-dragging 逐段扩。
- **比特翻转**：CTR 下改密文即改明文，位置精确。
- **OFB 可逆 RNG**：keystream 只由初始态决定。末块是零填充 → 密文本身等于 keystream → 泄漏一个 RNG 状态；若状态转移可逆，从后往前解出全篇。

### 1.4 CFB

**CFB-8 复用 IV**：加密 16 个已知字节后，内部移位寄存器状态完全由这些密文字节确定，可以从已知状态继续伪造新密文。

**IV 来自时间种子**：`random.choice` 用 `int(time())` 播种时，文件 mtime 就是种子。注意 Python 2/3 的 `random` 序列不同；`cp -it` 会重置 mtime，要用原始文件。

---

## 2. MAC 与认证

### 2.1 CBC-MAC vs OFB-MAC

OFB 的 keystream 与明文无关，所以拿到一组 `(P1, MAC1)` 就能伪造任意 `P2`：

```text
new_mac = mac1 XOR block2(P1) XOR block2(P2)
```

CBC-MAC 每块加密依赖前一块密文，没有这个性质。**算的时候别忘 PKCS#7 填充块**；搜索空间小（两位未知数）就直接穷举。

### 2.2 CRC 当 MAC = 没有 MAC

`CRC(a ^ b) = CRC(a) ^ CRC(b) ^ CRC(0…)`，GF(2) 线性。

- **追加 4 字节伪造**：`CRC32(msg ‖ suffix ‖ secret) == target` 的 `suffix` 有解析解，秒出。
- **翻位 + 修 CRC**：改明文 `X` 后，新 CRC 密文 = 旧 CRC 密文 XOR `CRC(X) ^ CRC(0…)`。
- **HMAC-CRC**：`HMAC(key,msg) = CRC(key_opad ‖ CRC(key_ipad ‖ msg))`，可写成 GF(2) 多项式，`K = known · (x^{128+M} + x^128)^{-1} mod CRC_POLY`，一组 `(msg,mac)` 直接反推密钥。

**结论**：CRC 只能查随机错误，对主动篡改零防护。

### 2.3 GCM / Poly1305 nonce 复用

同 `(key, nonce)` 复用 → 同时破机密性与完整性。

**第一步（CTR 复用）**：`C1 ^ C2 = P1 ^ P2`，一边已知即得另一边。

**第二步（恢复认证密钥）**：

- **AES-GCM**：tag 是 GF(2^128) 上的多项式求值。单块空 AAD 时 `GHASH = ((C·H) ^ L)·H`，两式相减得 `T1 ^ T2 = (C1 ^ C2)·H²`，解二次再开方。多块则构造 `P(H) = T1 − T2 − ΔGHASH(H)` 在 GF(2^128) 上分解取根。拿到 `H` 后可对任意消息重算 tag。
- **ChaCha20-Poly1305**：素数域 `p = 2^130−5`，`tag = Σ c_i·r^{n−i} + s (mod p)`，其中 `c_i = le16(块) + 2^128`（末块加 `2^{8·len}`）。两式相减 `s` 消掉，得 `T1 − T2 = ΔPoly(r)`，解多项式得 `r`。RFC 8439 的 clamp（`r &= 0x0ffffffc0fffffff…`，清掉字节 3/7/11/15 的高 4 位与低 2 位）只把候选压到 `2^106`，不阻止求解。
- **GCM 但模数是素数**：`tag = c + Σ b_i·H^{i+1} mod n`（`n` 是 128 位素数）。两式相减直接 `H = (t1−t2)·(m1−m2)^{-1} mod n`，一次模逆，不用多项式分解。若 nonce 只有 2 字节随机（其余固定），生日界下约 256 次查询就能撞到同 nonce。

**短 nonce 暴力**：key 已知且 nonce 只有 1–4 字节时，直接枚举（1 字节 = 256 次）。

### 2.4 自定义 MAC 的线性

| 结构 | 破法 |
|---|---|
| `out[i] = hash[i] ^ secret[selector] ^ chain[i-1]` | 收 ~10 组 `(id,sig)` 解出 3 个 secret 块，再伪造 |
| key stream 每 128 块循环，块间可 XOR 抵消 | 三次查询让填充块互相抵消，剩目标命令的 MAC |
| `sha256((key ^ msg) + msg)`（`+` 是加法） | 送 `msg=0` 得 `sha256(key)`；送 `msg=2^i`，若 key 该位为 1 则 `1^1=0, 0+1=1` 还原，哈希不变；为 0 则进位改变 → 逐位 oracle |
| 多项式哈希 `g(x)=x(x²+ax+b)` | `g(0)=0` → `0^d=0`，构造明文 `≡0 mod P` 即得签名 0 |

### 2.5 哈希长度扩展

Merkle-Damgard（MD5/SHA-1/SHA-256）的输出就是内部状态，`H(secret‖msg)` 可续算出 `H(secret‖msg‖pad‖ext)`。

```bash
hashpump --keylength 8 --signature '<hex>' --data 'original' --additional ';admin=true'
```

```python
import hashpumpy
new_hash, new_data = hashpumpy.hashpump(old_hash, old_data, append, secret_len)
```

- 不知道 secret 长度就试 1–32。
- **ASCII 过滤绕过**：输出里的 `0x80`–`0xFF` 换成等价的 UTF-8 双字节（`\x80` → `\xc2\x80`），哈希结果不变但所有字节 `< 0x80`。
- **与 CBC 组合**：cookie 里既有 `H(secret‖data)` 又有可恢复密钥的 CBC 密文时，先扩展出新的明文与 tag，再用泄漏的 AES key 重新加密，两边对齐。解析顺序「后出现的字段覆盖前面」会让追加的 `\nUID 0\n` 生效。

### 2.6 聚合与碰撞

- **SHA-256 基攻击**：256 个随机文件的哈希几乎必然张成 `GF(2)^256`，可 XOR 组合出任意目标值。破解 `XOR(sha256(file_i)) == expected` 这类聚合校验——注意这不是找碰撞，是利用聚合的线性。
- **MD5 多碰撞**：`fastcol` 一次给一对后缀；MD 结构下碰撞可组合，`k` 对后缀产生 `2^k` 个同哈希文件。
- **SHA-1 选择前缀**：`cpc` 造两份前缀不同、摘要相同的 PDF。协议签 `sign(sha1(M))` 而非 `sign(M)` 时，签一份、重放到另一份即伪造。
- **压缩 oracle（CRIME/BREACH）**：`SALT ‖ input` 先压缩再加密时，输入与 salt 匹配的部分会被压掉，密文变短即命中。逐字节恢复。
- **时间反转**：`state_t = H(state_{t-1})` 用截断哈希时，环长约 `2^{32}`，Floyd/Brent 找环后「后退 N 步 = 前进 L−N 步」。
- **中间态泄漏**：自定义 hash 打印每块后的状态时，`hash(block) = s(i) ^ ROL(s(i+1), 7)` 逐块反解，再爆破 4 字节可打印块。
- **PBKDF2 预哈希**：口令超过哈希块长（SHA-1/SHA-256 为 64 字节）时 HMAC 先做 `K = H(password)`，于是 `PBKDF2(long_pw) == PBKDF2(H(long_pw))`，用哈希登录即可。

---

## 3. 密钥恢复与结构弱点

| 场景 | 方法 |
|---|---|
| 4 轮 AES | Square：取 256 个仅一字节不同的明文，3 轮后任意位置 XOR 和为 0；猜末轮一字节部分解密，和为 0 即对 |
| SPN 列独立 XOR 层 | 每列 256 个候选，用「所有块在该列都是可打印 ASCII」取交集；多轮就一轮一轮剥 |
| 6 位 S-box、36 位分组、3 轮 | 每个 S-box 位置独立，跨 ~200 组明密文取候选交集，108 位降到 6 个 12 位搜索 |
| S-box 非置换（`len(set)<256)` | 找碰撞对与 XOR 差 δ；每密钥字节试 256 个差 δ 的明文，`ct1==ct2` 说明落在碰撞集；每字节 2 路歧义，总计约 4097 次查询 |
| S-box 只换了 3 项 | `C(256,3)×2 ≈ 5.5M` 直接爆破 |
| LCG 输出被截断 | 输出 `state mod N`：按 N 步进枚举；只给高位：爆破低位 32 位并用下一输出校验 |
| 40 组明密文对 | 变换是 GF(p) 线性 → 解 `A·key = b`（`tshark -r x.pcap -Y 'data.len>0' -T fields -e data` 取数据） |
| 可选清零密钥字节 | `offset = index*ENTRY_SIZE` 整数溢出 → 逐字节清零后每字节只剩 256 种，总 4096 次 |

**AES-CBC 只知道第 2 块起的明文**：第 2 块解密不依赖 IV（用 `C_1` 当 IV），先用它爆破缺的密钥字节，再 `IV = P_0 XOR Dec(C_0)` 反推 IV。

---

## 4. 协议与实现坑

| 坑 | 机理 | 用法 |
|---|---|---|
| SRP 只校验 `A != 0 && A != N` | `S = (A·v^u)^b mod N` | 送 `A = 2N` → `S = 0` → 会话密钥 `SHA256(0)` 已知，直接算 HMAC |
| 哈希链种子可公开推导 | `seed = md5(username)` | 从链头正算，回答任意「上一跳」 |
| AES 密钥由公钥派生 | `SHA256(DER(pubkey)) XOR seed` | 公钥是公开的 → 密钥直接算，RSA 私钥毫无作用 |
| DES + OFB | 4 个弱密钥使 `E(E(P,K),K)=P` | keystream 周期 2 → 退化成 16 字节重复 XOR；先试 4 个弱密钥 |
| 三轮 XOR 协议 | `c1=m^k1, c2=m^k1^k2, c3=m^k2` | `c1^c2^c3 = m`，密钥全消 |
| 解密报错回显明文 | 送全零块读中间值 | 与目标明文 XOR 即得密文块，从后往前逐块构造 |
| 填充 oracle + 命令注入 | 先恢复明文，再翻位注入 `;$(cmd)` | 纯密码弱点升级成 RCE |

---

## 5. 现代 AEAD 与轻量级

### 5.1 分区 oracle / 密钥承诺

多数 AEAD（AES-GCM、ChaCha20-Poly1305、AES-GCM-SIV）**不承诺密钥**：一个 `(nonce, ct, tag)` 可在多个密钥下验证成不同明文。若服务端把解密错误分区（`tag_fail` / `padding_fail` / `ok`），一次查询就能测上千个候选口令。

**格视角**：要构造「在 N 个密钥下都合法」的密文，等价于让 N 个 Poly1305/GHASH 多项式同时成立；tag 被截断到 `t` 位时，用 LLL/BKZ 在 tag 格上做 BDD，短向量给出同时满足多密钥的密文字节。缓解是 key-committing AEAD（把 `H(key)` 绑进 tag）加统一错误文案。

### 5.2 Sponge 通用表

| 原语 | state | rate | capacity | 轮数 | 填充 | 端序 |
|---|---|---|---|---|---|---|
| SHA3-256 (FIPS-202) | 1600 | 1088 | 512 | 24 | `0x06` | lane LE |
| Keccak-256（NIST 前） | 1600 | 1088 | 512 | 24 | `0x01` | lane LE |
| Ascon-128 / 128a | 320 | 64 / 128 | 256 / 192 | 12 + 6/8 | `0x80` | 字节 BE |
| Ascon-Hash-256 | 320 | 64 | 256 | 12 | `0x80` | BE |
| Gimli | 384 | 128 | 256 | 24 | `0x1F…0x80` | 字 LE |
| Sparkle-256 / Esch256 | 384 | 256 | 128 | 10 / 7 | `0x1F` | limb LE |

**头号互操作 bug**：`0x06`（FIPS SHA-3）与 `0x01`（原始 Keccak）混用，摘要完全不同。用 `hashlib.sha3_256` 去对挑战里的 `keccak256`（以太坊）必错。

**离线复现顺序**：认 `f` 的常量（Keccak RC、Ascon RC、Gimli SP-box、Sparkle 的 `0x9e3779b9`）→ 定端序 → 用对 suffix → 按 rate 吸收、每块置换、squeeze。

### 5.3 Trivium / Grain 与立方攻击

Trivium：288 位状态（93+84+111），80 位 key + 80 位 IV 载入后跑 **1152 轮预热**（`4×288`）不输出。

```text
t1 = s66  ^ (s91 & s92)  ^ s93  ^ s171
t2 = s162 ^ (s175 & s176) ^ s177 ^ s264
t3 = s243 ^ (s286 & s287) ^ s288 ^ s69
(s1,s2,s3) <<= 1 ; s93=t3 ; s177=t2 ; s288=t1
```

输出 `s66 ^ s93 ^ s162 ^ s177 ^ s243 ^ s288`。

**立方攻击**：把 IV 位当公开立方变量、key 位当未知，对每个立方求和得到 superpoly。**降轮**（288/576 轮预热）时大量 superpoly 退化成 key 的线性式（BLR 测线性），线上查 `2^{|C|}` 次取和，GF(2) 消元解 key。完整 1152 轮能抗到约 30 维立方，所以**先看预热轮数**——小于 `4×state` 就是攻击面。Grain 同理，挑喂给低次单项式的 IV/LFSR 位。

---

## 6. 纪律

- **先认模式**：ECB 看重复块、CBC 看 IV 是否回传、CTR 看计数器、OFB 看状态是否可逆。模式定完，攻击面就定了。
- oracle 不限于 PKCS#7：任何「合法/非法」的可区分响应（UTF-8、JSON、base64、状态码、耗时）都是解密 oracle。
- CBC 的两条基本式必须背下来：`P_n = Dec(C_n) XOR C_{n-1}`；翻 `C_{n-1}` 必毁 `P_{n-1}`。
- 看到 CRC 出现在 MAC 位置，直接判死——线性意味着可伪造且可反推密钥。
- AEAD 先查 nonce 是否复用，再看模数是 GF(2^128) 还是素数域：前者要分解多项式，后者一次模逆。
- 长度扩展的适用条件是 `H(secret‖data)` 且哈希是 MD 结构；HMAC 免疫。ASCII 过滤可用 UTF-8 多字节绕过。
- 轻量级算法先数轮数：轮数不足时的差分/积分/立方偏差是设计好的入口，不必硬啃全轮。
- 相关：流密码（LFSR/RC4）见 [[crypto/对称加密/stream-ciphers.md|流密码攻击]]，LLL/BKZ 细节见 [[crypto/格与后量子/lattice-and-lwe.md|格与 LWE]]，S-box 碰撞与 GF(2) 消元细节见 [[crypto/高阶专题/advanced-math.md|高阶数学攻击]]。
