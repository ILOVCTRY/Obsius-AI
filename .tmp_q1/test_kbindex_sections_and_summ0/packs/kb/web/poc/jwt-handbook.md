---
title: JWT 攻击手册
---
# JWT 攻击手册
本篇覆盖 JWT 常见伪造路径。
## none 算法
把 alg 换成 none 绕过签名校验。
## 弱密钥爆破
hashcat 对 HS256 做字典爆破。
## 密钥混淆
RS256 转 HS256 的公钥滥用。
