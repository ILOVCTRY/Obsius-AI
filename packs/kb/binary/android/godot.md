---
phase: reverse
---

# Godot 引擎 APK 专项

> 来源：r0re 收编改编（2026-09-21，Godot sec2026〔腾讯游戏安全初赛〕实战验证）。
> 参照案例：[cases/godot-sec2026/](cases/godot-sec2026/README.md)（完整五段式）。

## ① markers 清单（命中即切 Godot 流程）

`assets/.godot/` · `assets/project.binary` · `lib/<abi>/libgodot_android.so` ·
`*.gdc`（加密脚本）· `*.gdextension`（扩展清单）· `assets/assets.sparsepck`
（加密资源包）——分诊时声明特征词 `godot_engine`。

## ② 逻辑位置：小扩展不在大引擎

挑战逻辑在**小的自定义 GDExtension .so**（`assets/ext/*.gdextension` 指名的那个，
如 libsec2026.so）和/或加密 `.gdc` 脚本里——**不在** libgodot_android.so（大引擎
库，翻了白翻）。

## ③ 诱饵函数表（godot-cpp 同名陷阱）

| 诱饵名 | 实际是 |
|---|---|
| `get_flag` / `set_flag` | Window 类的 stock 方法 |
| `md5_text` / `sha256_text` | String 类 stock 包装 |

真目标：GDCLASS 自定义类（如 `GameExtension : Node`）自己 bound 的方法
（`Process` / `Tick` 之类）——从 `.gdextension` 指名的 so 里找自定义类注册。

## ④ .gdc / sparsepck 加密信封

FileAccessEncrypted 变体：envelope = `[16B md5][8B pt_len][16B iv][ct]`；
32 字节 script key 在 libgodot_android.so 内、`FileAccessEncrypted::open_and_parse`
附近；**CFB 可能被魔改**（逐字节再 XOR `i%16`）——先拿一份样本验证再套标准实现。

## ⑤ 变常量检测（mutated constants）

复用标准库实现前**先对参考常量**：ChaCha20 的 `expand 32-byte k` 被换成
`fxpaod 31-byse k` 这类换脸，一个 16 字节常量改动让所有 stock 实现全废——
逐字节 diff 参考 SIMD 常量/魔数，对上了再复用库代码。

## ⑥ 一键路径与手工兜底

`godot_ctf_runner.py`（M2 落 tools/ registry 后可用）：识别 → 壳检测 → unicorn
模拟脱壳 → RELATIVE 重定位修复 → 证据提取（init-XOR 恢复的 key/nonce、魔改
常量、字符串 xref）→ report + findings + `unpacked/*_reloc.bin`。手工兜底 =
[unpacking.md](unpacking.md) 全流程 + 本篇要点。

## ⑦ 解题后沉淀

解出新 Godot 题 → 五段式（识别 markers / 解题路径 / 验证向量 / solver / 复用提示）
沉淀 `cases/<案例id>/`（K6 约定，首例 [cases/godot-sec2026/](cases/godot-sec2026/README.md)）。
