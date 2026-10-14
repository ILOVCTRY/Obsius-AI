---
title: 案例：ARM64 自解压包无 IDA 源码恢复
summary: Android ARM64 自解压壳多层展开、控制流平坦化跳转表静态求解、循环 XOR 字符串解密器重放，低依赖恢复业务文本与伪源码
phase: reverse
vuln_class: [arm64, control-flow-flattening, self-extracting]
---

# 案例：Android ARM64 自解压程序源码恢复

> 来源：reverse-skill 1.0.1 field-journal 收编（2026-09-30，MIT，见 `../../../licenses/`）。
> 案例日期 2026-07-14，原文脱敏。环境：Windows，Python 3.13 + radare2 6.1.8 +
> pyelftools + Capstone；目标 Android ARM64（NDK r17 / Clang 6.0.2）。

## 执行链路

```text
1. 只读分诊：清单/大小/魔数/SHA-256，不读凭证明文
2. 识别第一层「Shell 前导 + bzip2 尾部流」：BZh 有效流测试定位精确偏移
3. 第二层 __ARCHIVE_BELOW__ 自解压脚本：逐成员校验后安全展开 tar.gz
   （拒绝绝对路径/../链接/设备节点）
4. 得到 AArch64 PIE 主程序 + 共享库：pyelftools/Capstone 出 ELF 头、节、符号、
   导入、字符串、入口反汇编
5. 保护库（保留可读 C++ 符号）：逐函数伪代码，确认 /proc 扫描、TracerPid、
   三级进程终止、后台线程
6. 主程序 main：符号长度 >> CFG 识别长度 → 确认间接跳转控制流平坦化
7. 静态求解跳转表：target = table_entry + fixed_delta，枚举唯一真实基本块
8. 识别同构字符串解密器：「前 N 字节循环 XOR 密钥 + 后 M 字节密文」
9. 对每个真实基本块做 AArch64 常量传播，解析间接解密器调用目标与 x1 数据源，
   批量恢复业务文本
```

## 踩坑表

| 问题 | 解决 |
|------|------|
| PowerShell 执行策略拦截 bootstrap | 单次 `powershell.exe -ExecutionPolicy Bypass -File` |
| radare2 bootstrap 撞 GitHub API 403 | 从 `releases/latest` 302 和 `expanded_assets/<tag>` 拿资产+SHA-256 |
| winget 装 Rizin 静默失败 | 停止重试，切回 radare2 官方 ZIP |
| `r2pm -U` 卡 git clone | 终止可选插件路线，用 `pdc` + Capstone 自定义恢复 |
| radare2 只识别 main 前部 CFG | 按跳转表公式枚举真实块，不依赖默认 CFG |
| 直接字符串扫描只见少量路径 | 文本是每字符串独立的循环 XOR，静态重放算法 |

## 关键代码

```python
# 通用循环 XOR 文本布局
key = blob[:key_length]
encrypted = blob[key_length:key_length + output_length]
plain = bytes(value ^ key[index % key_length]
              for index, value in enumerate(encrypted))

# 间接跳转表静态求解
targets = {(entry + fixed_delta) & 0xFFFFFFFFFFFFFFFF for entry in jump_table_entries}
```

## 可复用模式

1. 先扫描**有效压缩流**（内存中完整解压测试）而非只找魔数
2. 自解压归档永远逐成员安全写出，不直接执行、不信任成员路径
3. 符号表函数长度 >> CFG 识别长度 → 优先查 BR/BLR 间接跳转表
4. 同构解密器批量识别：`add x16,x1,#key_len` + `cmp w16,#output_len` +
   `ldrb/eor/strb` 指令组合
5. 平坦化块做**局部常量传播**通常足以恢复间接调用目标与字符串源地址——
   无需先完整去平坦化
