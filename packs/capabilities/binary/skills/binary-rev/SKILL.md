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

## 大文件读取纪律（2026-09-20：先定位后阅读，禁止盲猜行号来回补读）

反汇编/日志等大文件，**每轮 LLM 调用都很贵**——先用便宜的手段定位，再精确读取：

1. **先定位**：`run_cmd wsl grep -n` 找函数边界（objdump 函数头 `<name>:` 格式）、
   字符串引用、关键常量；定位成本 O(1) 轮，盲读平均浪费 3+ 轮。
2. **read_file 分段读**：带行号、`offset` 起始 `limit` 行数（上限 400）、`offset=-N`
   读末尾；单行超长自动截断标注。objdump 输出**优先按地址切片**
   （`objdump -d --start-address=0x... --stop-address=0x...`），地址比行号稳定。
3. **反汇编 >2000 行先拆函数文件**落 `scratch/funcs/`，之后逐函数读：
   `run_cmd host python split_disasm.py`（脚本用 host python 写，约 10 行：按
   `re.match(r"^[0-9a-f]+ <(.+)>:$", line)` 遇函数头即换文件，shell 转义零烦恼）。
4. 输出被截断会有显式标注——看到标注立即改用更小窗口/更精确定位，**不要凭语义猜没读到**。
5. 有 IDA 时优先 `decompile` 工具（伪代码一屏顶百行汇编），平台会按需自动拉起 IDA。

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
