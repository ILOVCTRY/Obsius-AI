# wbox（libwbox.so native AES 系列）

> r0re 收编（2026-09-21）。**solver 现成、案例待补**——命中识别特征直接跑
> solver 复验证，解出新变体后按五段式回填本目录。

## 识别 markers

- `libwbox.so`
- 验证链：Java input → JNI/native path → 16 字节初始化缓冲区 → 索引加法变换
  （`buf[i] += i`）→ 运行时常量加法 → AES-128-ECB 比较

## 已知求解链（solver 已建模还原逻辑）

AES 步骤求逆 → 去运行时常量（RUNTIME_ADD_CONST）→ 去索引加法 → 识别**未动
后缀**（初始化序列 07..0f 未被输入覆盖）恢复用户前缀。[wbox_solve.py](wbox_solve.py)
内置 key/ciphertext 常量并输出 `verification_passed`。

## 教学点

「固定缓冲区初始化后复制输入保留未动后缀字节」反推输入长度的先例——
见 [../../native-five-lines.md](../../native-five-lines.md) §⑤ 诱饵清单。
