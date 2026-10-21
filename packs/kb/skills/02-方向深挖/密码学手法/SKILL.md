---
name: 密码学手法
description: 密码题/密码学误用入口：从题面特征（RSA/ECC/分组/流密码/PRNG/古典/格）判定题型并路由到对应攻击专题
keywords: rsa, aes, ecc, dh, 密码, 加密, 解密, 哈希, 签名, 密文, 明文, 异或, xor, 古典密码, 维吉尼亚, padding oracle, nonce, prng, 随机数, lcg, mt19937, 格, lattice, lwe, lll, 背包, zkp, 椭圆曲线
features: math_puzzle_service, numbers_only_attachment
file_features: big_numbers, public_key_file, encrypted_blob
formats: py, sage, txt, pem, pub
vuln_classes: rsa, ecc, block-cipher, stream-cipher, prng, classic-cipher, lattice, hash
task_types: solve, triage
mode: self-contained
---

# 密码学手法 —— 密码题分诊与攻击路由

> 分层纪律：本技能只做**题型判定 → 开专题**。攻击细节与可运行代码在 crypto
> 英文参考资料（每篇一个技术族），用 `kb_open(module=…)` 按表开单篇，禁止通读。
> 拿不准题型先翻 `crypto/知识库.md` 的总索引。

## 0. 先看题面给了什么

- 只有 n,e,c 等大整数 / `.pem`/`.pub` → RSA 族
- 椭圆曲线参数 / P,Q / k,n,G → ECC 族
- p,g,A,B / 交互握手 → 经典 DH
- 加密预言机 / 让你构造密文、回显报错 → 分组密码与预言机族
- 一串"随机"数 / token / 时间种子 → PRNG 族
- 字母表 / 短英文密文 / 转轮书本 → 古典密码
- 矩阵 / 行列式 / 噪声方程组 / LWE/HNP → 格与数学族

## 1. 特征 → 专题对照表（module 路径）

| 题面特征 | kb_open 模块（crypto/） |
|---|---|
| RSA：小 e 开方、共模、Wiener、Pollard p-1、Håstad 广播、Franklin-Reiter、Fermat | `crypto/非对称加密/RSA 攻击判据手册.md` 前半 |
| RSA：dp/dq 部分泄露、多素数、批 GCD、CRT 故障注入、低指数签名伪造、Manger | `crypto/非对称加密/RSA 攻击判据手册.md` Part 2 |
| ECC：小群/无效曲线/异常曲线 Smart、ECDSA nonce 复用、Pohlig-Hellman | `crypto/非对称加密/椭圆曲线攻击.md` |
| DH：平凡生成元、平滑阶、小群 confinement、Logjam | `crypto/非对称加密/有限域 DH 攻击.md` |
| 分组：ECB 泄露/逐字节、CBC padding oracle、CBC bit-flip、Bleichenbacher(ROBOT) | `crypto/对称加密/分组密码、MAC 与 AEAD 攻击.md` 前半 |
| 分组进阶：Blum-Goldwasser、长度扩展、压缩预言、ECB cut-and-paste、Rabin LSB | `crypto/对称加密/分组密码、MAC 与 AEAD 攻击.md` Part 2/3 |
| AEAD：AES-GCM nonce 复用、ChaCha20-Poly1305、key-committing 分割格攻击 | `crypto/对称加密/分组密码、MAC 与 AEAD 攻击.md` Part 4 |
| 流密码：LFSR（Berlekamp-Massey/相关攻击）、RC4 偏置 | `crypto/对称加密/流密码攻击.md` |
| PRNG：MT19937 预测、LCG、时间种子、V8 Math.random、randcrack | `crypto/随机数/伪随机数生成器与密钥恢复.md`、`crypto/随机数/PRNG 攻击（CTF 手法）.md` |
| 古典：维吉尼亚/凯撒/埃特巴什/XOR 多字节频率分析/书密码/OTP 复用 | `crypto/古典密码/古典密码与结构化 XOR 攻击.md` |
| 格：LLL/BKZ/Babai、HNP、截短 LCG、LWE/Ring-LWE、NTRU、背包、Coppersmith | `crypto/格与后量子/格与 LWE 攻击.md`、`crypto/高阶专题/高阶数学攻击.md` |
| ZKP / 秘密分享 / 异或协议 / 杂项代数结构（Paillier 等） | `crypto/高阶专题/零知识证明与高阶密码攻击.md`、`crypto/高阶专题/非常规代数结构与密码体制.md` |
| 后量子识别（Kyber/ML-DSA/Falcon/NTT） | `crypto/格与后量子/后量子识别与 LWE 策略选择.md` |
| 历史密码机（Lorenz 等） | `crypto/古典密码/历史密码机.md` |

## 2. 纪律

- 先判密钥/原语复用，再算数学：nonce 复用、e 共用、p 共用（batch GCD）往往是秒杀点。
- 解法脚本随题写随存 artifact；解出的 flag 按题目格式校验，多个候选做唯一性核对。
- 需要装大工具（sage/fpylll/RsaCtfTool）时先在题面注明，安装与计算放 docker。
