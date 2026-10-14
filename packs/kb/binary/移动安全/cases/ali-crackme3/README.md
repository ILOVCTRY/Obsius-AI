# AliCrackme3（com.ctf.crackme3 / AliCrackme_3）

> r0re 收编（2026-09-21）。**solver 现成、案例待补**——上游未附完整五段式案例
> README；命中识别特征直接跑 solver 复验证，解出新变体后按五段式回填本目录。

## 识别 markers

- package `com.ctf.crackme3`；`lib/armeabi/libcrackme.so`
- 样例 APK/SO sha256 内置于 [ali_crackme3_solve.py](ali_crackme3_solve.py)，运行时自动比对

## 已知求解链（自 r0re 流程描述还原）

```
stored encrypted table → xor key 20 8e 13 39 → index rotations → xor 0x29
→ PRGA-like S-box XOR → byte rotate → 取前 15 字节 = flag_body
```

app 输入即 15 字节 `flag_body`，flag 形态 `flag{<flag_body>}`。

## 使用

```bash
python3 ali_crackme3_solve.py <target.apk> --json   # verification=passed 即解
```
