---
name: binary-rev
description: 二进制逆向核心技能：函数定位、算法还原、func_kb 协作、求解验证
keywords: 逆向, reverse, 反汇编, 反编译, crackme, 算法, 校验
features: is_elf, is_pe, has_check_logic, packed_binary
task_types: reverse, solve, verify
---

# binary-rev —— 二进制逆向：定位 → 还原 → 落库 → 求解

## 硬规则（先于一切）

**反编译前必须 `bb_query what=func binary_sha256=<sha>` 查重**；
每分析完一个函数立即 `bb_upsert_func`（地址 + 语义名 + 算法结论）。
并行会话共享这份知识库——你写下的每个函数都替队友省一次反编译。

## 流程

1. **定位关键函数**：字符串引用反查（提示语/flag 格式）→ main → 校验逻辑。
   静态工具优先 host 端脚本（objdump 反汇编 / python capstone 不执行样本）。
2. **还原算法**：逐块读汇编，提取常量表/变换；结论写 func_kb（如
   `check_flag@0x1189: 输入逐字节 XOR 0x37 后与密文比较`）。
3. **求解**：还原算法后本地写脚本（host, trusted——这是你自己的代码）算出正确输入。
4. **验证**：`echo <input> | ./binary` 必须在 docker/sandbox 执行（样本 untrusted）；
   无容器可用时如实标 unverified 并附推导链。
5. **落黑板**：flag/关键结论 `bb_add_finding`（evidence 必含运行输出或完整推导）。

## 常见变换速查（按需深入，勿凭此武断）

XOR 常量 / 逐字节加减 / 查表替换 / 简易 TEA·XTEA / 魔改 base64 / 反转+位移。
遇到多层嵌套先分层落 func_kb，再逐层求解。
