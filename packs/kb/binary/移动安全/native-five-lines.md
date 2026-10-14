---
phase: reverse
---

# Native 五线（JNI / 常量 / SMC / 验证 / 诱饵）

> 来源：r0re 收编改编（2026-09-21）。APK 的 native 校验拆五条线推进；每条线只报
> 增量事实（offset / symbol / section），没有证据的叙述等于没做。ELF 通用纪律
> （func_kb 查重、大文件读取、变换速查）沿 [binary-rev](../../capabilities/binary/skills/binary-rev/SKILL.md)
> 技能，本篇只补 Android/移动专项。

## ① JNI 桥定位线（JNI bridge）

从 Java 侧 `native` 声明或 `System.loadLibrary` 出发：

- **静态注册**：导出符号 `Java_<pkg>_<cls>_<meth>` 命名直查；
- **动态注册**：符号表里看不到 Java 名——先看 `JNI_OnLoad` 里的 `RegisterNatives`；
- 参数编组：GetStringUTFChars / byte[] / direct buffer；返回路径语义（boolean？新字符串？）；
- 产出一条调用链：`Java 方法 → JNI 入口 → 核心 native 函数 → 校验/比较段`；
  桥很薄就点名下一跳即停；
- 桥已知后**不漂回 Java 大扫荡**。

## ② 常量提取线（constants）

- 翻 `.rodata`、`.data`、栈上初始化缓冲区、字面量池（literal pool）、解密后的堆；
- 找：memcmp/strcmp/手工比较循环、XOR/加减表、置换表、digest/base64 常量、
  候选专属期望字节；
- **init_array 期解混淆**：`movz/movk` 拼 64 位常量 + `adr` + XOR 存储循环 =
  init 期自解密，恢复出来的多半是 key/nonce——先看 init_array 再说「没有常量」；
- 区分「原始常量」与「变换后缓冲区」；报字节数组带偏移，别用散文描述。

## ③ SMC 线（自修改代码 self-modifying code）

- 先确认活动节 VA 是否等于文件偏移（不等的先修映射再读）；
- 找 `mprotect` / `cacheflush` / `mmap` 解密编排流；
- 恢复四件：加密区起止、密钥地址/派生方式/周期、解密后函数入口、字面量表是否单独加密；
- 解密区又调另一段加密区 → 显式说明是第二级；两段证据冲突时明说，不硬圆。

## ④ verify 线（到达 final compare 的证明义务）

候选分层，缺一层就不算证明：
**hint 推出的候选 → 第一级变换后的候选 → 第二级缓冲区 → 最终比较条件**。

- DEX/native 字符串是**候选不是证据**——必须证明它到达 final compare；
- 反推方向：从最终比较条件倒推反向变换（reverse transform），恢复真正期望输入；
- **验证纪律**：候选必须 solver 路径 + 验证输出，三者之一——
  ①已知测试向量；②forward+reverse round-trip（可逆变换优先双向验证：流密码、
  XOR 链、hex/base64 编码）；③复现 app 观测输出；
- 验证命令与输出随 finding 落黑板；旧报告/历史候选/图上旧事实一律当假设。

## ⑤ 诱饵清单（decoy）

- Morse/Base64/MD5-like 常见诱饵**不止步**——命中诱饵 = 排除一条路，落 finding 继续；
- native 固定缓冲区初始化后复制输入时**保留未动后缀字节**：初始化序列（如 00..0f）
  中未被输入覆盖的后缀是反推输入长度的钥匙（wbox 先例：07..0f 未动 → 反推用户前缀，
  见 [cases/wbox/](cases/wbox/README.md)）；
- 提示语/flag 格式字符串直接定位校验函数（字符串引用反查是 JNI 线的起点之一）。

## 变换速查

XOR 常量 / 逐字节加减 / 查表替换 / 简易 TEA·XTEA / 魔改 base64 / 反转+位移——
多层嵌套先分层落 finding 再逐层求解；so 分析结论照 binary-rev 纪律落 func_kb
（address + 语义名 + 算法结论），并行会话共享。
