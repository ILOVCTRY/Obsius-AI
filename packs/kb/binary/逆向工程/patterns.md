## 已验证路径：XOR 型 flag 校验还原 + Unicorn 指令级双分支仿真

来源：task-19c7f55c576a（样本 b1nary.exe，sha256=88b3f5ab1609c1142a…），证据 find-415d919209c6（verified/critical），产物 art-8f9cd02940ab（poc/solve_flag.py）。

1. 定位校验函数：从 main 的成功/失败分支字符串（"Well down!"）交叉引用回溯到 `main_flag_check@0x401160`，确认逐字节比较循环（共 19 项，计数存 esi）。
2. 追踪双输入：expected 数据区在 `0x403258`；异或 key 不在 main 内联，而是由独立初始化函数 `init_global_key_string` 写入全局缓冲 `0x405424`（内容 `Mht!^okHGfdCbn!@4t>`）。只读 main 会漏 key，必须沿「谁写校验函数引用的地址」回溯到 init 函数。
3. 静态还原：`flag[i] = expected[i] ^ key[i]`（等长 19 字节），离线解得 `We!COm3_2_Nu4actf1>`。
4. Unicorn 指令级双分支仿真验证（不依赖真实执行环境）：
   - 正向：以解出的 flag 填输入缓冲 → 19 项比较全部通过（esi=19）→ 走 "Well down!" 成功分支 → PASS；
   - 负向：篡改首字节（'W'→'X'）→ 确认在第 0 项即失败、进入失败分支 → 排除恒真比较导致的假阳性。

## 坑

- key 串由 init 函数启动时写入全局缓冲，字符串表里能看到但归属易误判；以校验函数实际引用的地址（0x405424）为锚回溯写入者，而不是直接取最近的字符串常量。
- 双分支仿真都要跑：只跑正向无法证明比较逐字节生效；负向篡改应落在首个比较项，失败才闭环。
- 流程坑：`bb_add_artifact` / `bb_add_finding` 的返回头可能被省略看不到 id，落库后须用 `bb_query` 复核（本例确认 find-415d919209c6 已入库）。