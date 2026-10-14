# Godot sec2026（Truck Town / 2026 腾讯游戏安全初赛 Android）

> r0re 收编（2026-09-21，用户确认可收编），K6 案例格式首例（五段式）。

## 识别 markers（identification markers）

- Godot 4.5.1 游戏 APK：`assets/.godot/`、`assets/project.binary`、`lib/arm64-v8a/libgodot_android.so`
- 自定义 GDExtension：`assets/ext/sec2026.gdextension` → `lib/arm64-v8a/libsec2026.so`
- `libsec2026.so` 是 UPX 风格壳（.text 高熵、内嵌 \x7fELF、节头伪造、init_array 裸 syscall stub）
- `assets/*.gdc` 加密（FileAccessEncrypted 魔改 CFB）；`assets/assets.sparsepck` 目录加密

## 解题路径（solution path）

1. `godot_ctf_runner.py <apk>`（r0re 工具，M2 落 tools/ registry 后可用）自动完成：
   Godot 识别 → 壳检测 → unicorn 模拟脱壳 → RELATIVE 重定位修复 → 常量提取
   （key/nonce/魔改常量）。手工兜底见 [../../godot.md](../../godot.md) + [../../unpacking.md](../../unpacking.md)。
2. 扩展类 `GameExtension : Node` 暴露 `Tick` 与 `Process(input)->String`；flag 计算在 Process。
3. token→flag 算法（已在还原镜像中逐字节核实 + 3 组向量验证）：
   - GDScript xor_enc: `r[i]=a[i]^a[i+1] (i=0..6)`，`r[7]=a[7]^r[0]`
   - ChaCha20 变体：key=`"Th1s ls n0t a rea1 key!!@sec2026"`（.data 0xED5D2，
     运行时 XOR `0x97A36EF7A74F5E4E` 解密），nonce=`"012345678901"`，counter=0；
     **魔改点**：state 常量 `"expand 32-byte k"` → `"fxpaod 31-byse k"`（0x19ED8）
   - 取 keystream 前 8 字节异或 → `%02X` 大写 hex
   - `flag{sec2026_PART1_<16hex>}`（PART0 为固定示例 `flag{sec2026_PART0_example}`）
4. 全程可逆：[flag_algo.py](flag_algo.py) 同时提供 `token_to_flag()` / `flag_to_token()`
   （内置自测向量：`python3 flag_algo.py`）。

## 验证向量（verification vectors）

| token | flag |
|---|---|
| a1b2c3d4 | flag{sec2026_PART1_2A4C031823617318} |
| cf14eaad | flag{sec2026_PART1_7F4856187736261D} |
| deadbeef | flag{sec2026_PART1_7B1B564F7436201B} |
| 5077dd70 | flag{sec2026_PART1_7F18531A73652449} |

## 复用提示

- 壳是通用 UPX-shlib fold（NRV2B stage1 + LZMA/NRV2B 主块 + memfd MAP_FIXED）；
  同类壳直接用脱壳三件套（M2 后在 tools registry），不要重写脱壳器。
- Godot 题检查清单见 [../../godot.md](../../godot.md)。
