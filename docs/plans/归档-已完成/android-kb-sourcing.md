# 方案：binary 包 Android 子域收编（r0re）

- **状态**：**已实施（M1+M3 2026-09-21，M2 工具收编 2026-09-23——三里程碑全量闭环）**（打磨两轮共十条消化见 §5；实施记录见 §6）
- **拍板记录**：见 §3（5 项决策）
- **素材库**：`开源优秀项目/逆向/r0re-main`（Android 逆向与 CTF 编排服务，Cairn 血统二开 + muteki 黑板借鉴；许可已由用户确认可直接收编，2026-09-21）
- **关联代码**：`packs/capabilities/binary/`（K2 收编版 pwn/reverse/malware + file-triage/binary-rev/binary-pwn 技能）、`core/tools/decompiler.py`（DECOMPILE_GUIDANCE 引导先例）、[toolchain-registry.md](toolchain-registry.md)（python-tool 类落位）、[dsh-kb-sourcing.md](dsh-kb-sourcing.md)（dsh mobile 分类归并此处）
- **与 [expert-pool.md](expert-pool.md) 咬合**：expert-pool **M0 kb 物理树重组已实施（2026-09-21）**——落位直接用新树 `packs/kb/binary/android/`（原「先 binary/kb/android/ 后随树迁移」的两步路径作废）。

## 1. 愿景与背景

binary 包厚度缺口（对照 dsh binary-analysis 279 篇）中，Android 方向完全空白（无 mobile/android 子域）。用户指定以 r0re 为补源素材库：其方法论 prompt 经实战验证（Godot sec2026 腾讯游戏安全初赛等），含大量稀缺专项知识；自研工具链与已解案例库可一并收编。

## 2. 素材盘点（2026-09-21 核实）

### 2.1 方法论 prompt 30 篇（`src/main/resources/prompts/`）

android-ctf 15 篇 + android-reverse 15 篇，Cairn 状态机阶段化（bootstrap/explore/reason/conclude）。**纯知识部分（收编对象）**：

- **分层判据**：分析关键层四分——Java/smali only / JNI bridge / native validator / dynamic-only blocker；bootstrap 快速定位关键层，不在 Java 摘要上过度花费。
- **native 分析五线**：constants（常量提取）/ JNI（桥定位）/ packed（壳）/ SMC（自修改代码）/ verify（验证逻辑）——细分分类学。
- **Godot 引擎 APK 专项**（explore_native_packed.md，极稀缺）：识别 markers（`assets/.godot/`、`project.binary`、`libgodot_android.so`、`*.gdc`、`*.sparsepck`）；逻辑在小自定义 GDExtension `.so`（`assets/ext/*.gdextension` 指名）不在大引擎库；godot-cpp 诱饵函数名（`get_flag`/`set_flag`=Window 方法、`md5_text`/`sha256_text`=stock 包装）；`.gdc`/`sparsepck` 加密信封 `[16B md5][8B pt_len][16B iv][ct]`、32 字节 script key 在 `libgodot_android.so` 近 `FileAccessEncrypted::open_and_parse`、CFB 魔改（逐字节 XOR `i%16`）；变常量警惕（ChaCha20 `expand 32-byte k` 换脸检测，先对参考常量再复用库代码）；init_array 内 `movz/movk` 建常量 + XOR 循环 = init 期解混淆。
- **脱壳工程细节**：UPX-shlib fold 识别（.text 高熵/内嵌 `\x7fELF`/节表矛盾/init_array 裸 syscall）；unicorn 模拟脱壳（auxv/memfd/mmap，MAP_SHARED 写回munmap 同步是经典丢数据坑）；`R_AARCH64_RELATIVE` 重定位修复（不修则 vtable/成员指针读零）；packed 文件字符串不可信（压缩伪影），只信脱壳镜像；capstone 线性扫描在嵌入数据上失步——adrp+add 逐字手工解码。
- **验证纪律**：候选答案必须 solver 路径 + 验证输出（已知测试向量 / forward+reverse round-trip / 复现 app 观测输出三者之一）；可逆变换优先双向验证；验证记录进状态。
- **诱饵清单**：Morse/Base64/MD5-like 常见诱饵不止步；DEX/native 字符串是候选不是证据——须证明到达 final compare；native 固定缓冲区初始化后复制输入时保留未动后缀字节。
- **状态模型**（ctf_state.json 字段集）：target_apk/package/launcher/java_entry/native_libs/jni_bridge/native_entry/constants/algorithm/solver/verification——Android crackme 分析状态模型可直接提炼进手册。

**剥离项（r0re 机制，不收编）**：blackboard.py 命令（→我方黑板）、`/opt/r0re`/workspace 路径、JSON 输出契约、report_en/zh 双报告、android_ctf_runner 专用家族判定（wbox/AliCrackme3）、swarm 多智能体协议。

### 2.2 自研工具 7 个（`tools/android/`，56KB）

| 工具 | 大小 | 用途 | 处置 |
|---|---|---|---|
| android_ctf_runner.py | 14.1KB | APK 自动分诊（清单/权限/native/JNI 线索） | **收编**（registry python-tool） |
| godot_ctf_runner.py | 17.8KB | Godot 全自动：识别→壳检测→unicorn 脱壳→reloc 修复→常量提取 | **收编**（核心价值） |
| upx_shlib_emu.py | 9.6KB | UPX-shlib unicorn 模拟脱壳 | **收编**（脱壳三件套） |
| apply_relocs.py | 2.8KB | R_AARCH64_RELATIVE 修复 | **收编**（同上） |
| nrv2b.py | 2.4KB | NRV2B 解压（UPX arm64 stub） | **收编**（同上） |
| ali_crackme3_solve.py / wbox_solve.py | 各 ~6KB | 两类 crackme 专用 solver | **归案例库**（随 knowledges 案例，不进 registry） |

依赖：unicorn（python 包）——registry `python-tool` 类 / venv 落位，衔接 toolchain-registry M1。

### 2.3 已解案例库（`tools/android/knowledges/`）

`godot-sec2026/README.md`（2026 腾讯游戏安全初赛 Truck Town）：**识别 markers + 完整解题路径（含逐字节核实的算法细节与魔改点）+ 验证向量表 + solver 脚本 + 复用提示**五段式——与我方 K6 成功案例沉淀（verified 攻击链 → 测试包 `成功案例.md` + `payloads/`）**完全同构**，作为 K6 案例格式（尤其 CTF Android 题）的参照样本。

### 2.4 容器环境（`container-android/`）

Docker 化 Android 分析 worker（scripts/skills/test_apk）——我方 L3 Docker 体系用途不同，**只登记参考不收编**；将来若做 Android 动态分析环境再回头看。

## 3. 定稿决策（2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 落位 | binary 包新子域 android/：kb 树重组前落 `binary/kb/android/`、树后 `kb/binary/android/`（随 expert-pool M0 统一迁移，两形态同名）；dsh mobile 5 篇归并于此；binary 包第 4 技能 `android-rev`（薄路由） |
| 2 | 知识收编 | 30 篇 prompt 提炼改写 **4 篇手册**（见 §4.1），剥 r0re 机制；快照原件不搬运（prompt 形态无原件价值） |
| 3 | 工具收编 | 5 工具进 tools/registry（python-tool/venv），2 专用 solver 随案例库；container-android 只登记 |
| 4 | 案例库 | godot-sec2026 收编为 K6 成功案例参照样本（五段式格式）；后续 Android 题 verified 沉淀照此格式 |
| 5 | 许可确认 | **用户已确认可直接收编**（r0re 虽无 LICENSE 文件，2026-09-21 拍板解除前置闸） |

## 4. 设计详述

### 4.1 知识映射表（30 篇 prompt → 4 篇手册，打磨定稿）

文件名跟 binary 域现状用英文（域内自治，不混 K5 的中文名先例）；正文中文叙述，术语首次出现括注英文。

| 手册 | 素材来源 | 章节骨架 |
|---|---|---|
| `triage-and-layering.md` 分诊与分层 | android-ctf/bootstrap + android-reverse/bootstrap + SKILL.md | ①关键层四分判据表（Java/smali only / JNI bridge / native validator / dynamic-only blocker）②快速分诊命令序列（aapt dump badging / apktool d / jadx / readelf -h / strings 扫 so；**特征词声明对照**：file_features 由 Agent 分诊声明传入路由 ×3，非自动探测——手册给「观察到什么→声明什么」对照表，与 android-rev frontmatter 对齐：APK 容器→is_apk、lib/*.so→has_native_lib、JNI 导出→has_jni、.godot markers→godot_engine、加固 so→packed_so）③分析状态字段模型（ctf_state 字段集提炼）④stop-early 纪律（分诊结论进 finding，不在低价值层空转） |
| `native-five-lines.md` native 五线 | explore_native{,_jni,_smc,_constants,_verify} + reason_native | ①JNI 桥定位（RegisterNatives vs `Java_<pkg>_` 命名）②常量提取线（init_array `movz/movk` 建常量 + XOR 循环 = init 期解混淆）③SMC 线 ④verify 线（到达 final compare 的证明义务）⑤诱饵清单（Morse/Base64/MD5-like、字符串是候选不是证据、固定缓冲区保留未动后缀） |
| `unpacking.md` 壳与脱壳 | explore_native_packed + android-reverse 对应篇 | ①UPX-shlib fold 识别四征（.text 高熵/内嵌 `\x7fELF`/节表矛盾/init_array 裸 syscall）②unicorn 脱壳工程（auxv/memfd/mmap；MAP_SHARED 写回 munmap 同步丢数据坑）③`R_AARCH64_RELATIVE` 重定位修复（不修则 vtable 读零）④packed 字符串不可信，只信脱壳镜像 ⑤capstone 线性扫描失步→adrp+add 手工逐字解码 |
| `godot.md` Godot 专项 | explore_native_packed Godot 节 + godot-sec2026 案例 | ①markers 清单 ②逻辑在小自定义 GDExtension（`assets/ext/*.gdextension` 指名），不在大引擎库 ③诱饵函数表（godot-cpp 同名陷阱）④`.gdc`/sparsepck 加密信封（格式 + 32 字节 key 位置 + CFB 变体）⑤变常量检测（ChaCha20 `expand 32-byte k` 换脸，先对参考常量再复用库代码）⑥godot_ctf_runner 一键路径与手工兜底 |

- 术语表：JNI 桥（JNI bridge）、加固（厂商壳 packing）、脱壳（unpacking/dumping）、SO（native 共享库）、SMC（自修改代码 self-modifying code）、重定位（relocation）、诱饵（decoy）、验证向量（test vector）、双向验证（round-trip）。
- 薄路由技能 `android-rev` frontmatter **定稿**（对照 binary-rev 去重）：

```yaml
---
name: android-rev
description: Android/移动端逆向：APK 分层分诊、JNI/native 五线、加固脱壳、Godot 专项
keywords: android, apk, 安卓, jni, ndk, dex, smali, 加固, 脱壳, godot, crackme, frida, so
file_features: is_apk, has_native_lib, has_jni, godot_engine, packed_so
task_types: triage, reverse, verify
---
```

  去重说明：`crackme` 与 binary-rev 双方声明——Android 场景 `is_apk` 特征（×3 权重）压过 keywords 撞分；`is_elf/is_pe` 不与 `is_apk` 撞（APK 是 zip 容器）；`packed_so`（加固 so）≠ `packed_binary`（通用壳）。正文=特征→手册对照表（`android/triage-and-layering.md` 等四指针）+ 反空转规则（native 存在优先 JNI 线、诱饵不止步、候选必须验证）。手册 frontmatter 照 K5 分面约定（phase/vuln_class）。
- **专家挂载（复查补定稿）**：`reverse-analyst`（research 轨逆向分析师）+ `reverse`（ctf 轨逆向解题）两专家 skills 白名单新增 `android-rev`——对照 binary-rev 现挂载面（code-auditor / pwn-solver / rebuilder / reverse-analyst）对称收窄：Android crackme 偏逆向不偏利用，pwn-solver 不挂。两专家已因 binary-rev 覆盖 binary 包能力面，挂载只增技能偏好与正文可达性，caps_effective 域面不变；未挂载场景下 kb 手册仍可经 route_index 按域注入，但 ×3 特征偏好与反空转正文不进运行时面。

### 4.2 工具注册表草案（衔接 toolchain-registry）

```json
{
  "android-ctf-runner": { "kind": "python-tool", "bin": {"windows": "tools/py/android/android_ctf_runner.py"},
                          "verify": ["--help"], "domains": ["research", "ctf"],
                          "deps": ["unicorn"], "note": "APK 自动分诊" },
  "godot-ctf-runner":   { "kind": "python-tool", "…": "…", "note": "Godot 脱壳+常量提取" },
  "android-unpack-kit": { "kind": "python-tool", "…": "…", "note": "upx_shlib_emu + apply_relocs + nrv2b 三件套" }
}
```

落位 `tools/py/android/`（registry schema 定稿时对齐其 M1）；Agent 引导文本沿用 DECOMPILE_GUIDANCE 先例（工具缺失→降级纯静态）。

### 4.3 案例库（落点定稿）

- 落点 `kb/binary/android/cases/godot-sec2026/{README.md,flag_algo.py}`（M0 已实施，直接新树形态）——`cases/` 子目录隔离「已解案例」与「方法论手册」。
- **K6 沉淀路径约定定稿**：`<域>/<子域>/cases/<案例id>/`（成功案例 verified 后的沉淀目标路径），godot-sec2026 为首例；其五段式（识别 markers / 解题路径 / 验证向量 / solver / 复用提示）写入 K6 案例格式约定，**约定定义处 = DESIGN.md §4 知识库定稿块**（实施 M3 时随收编写入）。
- ali_crackme3 / wbox 两个 solver 落 `cases/` 同级独立目录（r0re 未附完整案例 README，标注「solver 现成、案例待补」）。

### 4.4 binary 包 route_index 增补草案（4 条，全局单表前缀形态 `binary/android/<手册>.md`）

| point | match | kb |
|---|---|---|
| Android 分层分诊 | android, apk, 安卓, dex, smali | android/triage-and-layering.md |
| JNI / native 分析（移动） | jni, ndk, native, so, so库, hook | android/native-five-lines.md |
| 移动加固与脱壳 | 加固, 脱壳, packed, upx, 壳 | android/unpacking.md |
| Godot 引擎 APK | godot, gdc, sparsepck, 引擎 | android/godot.md |

「脱壳/壳」与 reverse/anti-analysis 条目 match 撞词——Top-5 评分按上下文自然分流（APK vs ELF），可接受；后续按 K7 zero-hit 追踪精简。无 tags 全角色可见（对齐 binary K2 骨架惯例）；doctor `route-index-kb-missing` 兜底。

## 5. 打磨定稿记录（2026-09-21，两轮共十条全部消化）

| # | 打磨点 | 定稿 |
|---|--------|------|
| 1 | 4 篇手册成文 | §4.1：文件名（英文，跟 binary 域现状）+ 章节骨架 + 术语表定稿；正文成文属 M1 实施动作 |
| 2 | android-rev frontmatter | §4.1 定稿：file_features=`is_apk/has_native_lib/has_jni/godot_engine/packed_so`；`crackme` 与 binary-rev 撞词保留（is_apk 特征 ×3 压过）；`packed_so`≠`packed_binary`。**实施期修正（2026-09-21）**：核实 router 匹配语义（core/skills/router.py）——分诊声明的文件特征走 `file_features` 查询参数、只匹配技能 `file_features` 字段（`features` 字段对应情景特征查询），故特征词从 `features` 改落 `file_features` 字段，否则 ×3 加权为死标签 |
| 3 | 工具 registry schema | M2 与 toolchain-registry M1 **谁先落地谁定 schema，后落方对齐**（§4.2 草案作基线）；unicorn 依赖 venv 形态随之 |
| 4 | 案例落点 | §4.3 定稿：`<域>/<子域>/cases/<案例id>/` 为 K6 沉淀路径约定，godot-sec2026 首例 |
| 5 | redlines 条款 | **零新增**：research 轨「样本 untrusted/仅授权样本」已覆盖；frida/动态调试属执行类操作走 run() 网关既有策略；噪声档研究轨已有 passive 默认 |
| 6 | dsh mobile 归并 | mobile 5 篇改归本子域（渗透向篇目，refs/ 快照纪律照旧）；dsh 方案 §2.1/§4.4 已同步；miniprogram 5 篇仍归 web 包不变 |
| 7 | 专家挂载清单 | §4.1 定稿：reverse-analyst + reverse 挂 android-rev（对照 binary-rev 现挂载面对称收窄，pwn-solver 不挂；caps_effective 域面不变） |
| 8 | 特征词一致性 | file_features 由 Agent 分诊声明传入（非自动探测）——triage-and-layering 手册②加特征词声明对照表，声明词与 android-rev frontmatter 对齐保 ×3 命中 |
| 9 | K6 约定定义处 | §4.3 定稿：五段式案例格式约定落 DESIGN.md §4 知识库定稿块（M3 实施时写入） |
| 10 | 树形态残留清理 | M0 已实施，§4.3/§4.4/§6「树前/树后」两形态旧表述清理，一律直接按新树 `kb/binary/android/`（拍板记录 #1 保留历史原貌，头部已声明作废） |

## 6. 实施记录

### M1+M3 已实施（2026-09-21，同日一批准）

- **M1 知识与技能**：`packs/kb/binary/android/` 落 4 篇手册（30 篇 prompt 提炼改写，剥 r0re 机制，术语首现括注英文，frontmatter 照 K5 分面 phase）+ index.md 子域入口；`capabilities/binary/skills/android-rev/SKILL.md`（§4.1 定稿 frontmatter 原样 + 手册对照表 + 反空转规则 + 特征词声明 + 与 binary-rev 分工）；专家挂载 reverse-analyst / reverse 两 yaml；route_index 增 4 条（全局形态 `binary/android/<手册>.md`，binary 9→13）+ `binary/route.json` 增 android 键；test_skills.py reverse-analyst skills 断言同步。
- **M3 案例库**：`kb/binary/android/cases/`——godot-sec2026 完整五段式（README + flag_algo.py 原样收编，**正反向向量自测通过**）；ali-crackme3 / wbox 两 solver 原样收编 + 「solver 现成、案例待补」stub README（识别 markers + 已知求解链）；cases/README.md 条目索引。K6 案例格式约定（五段式 + `<域>/<子域>/cases/<案例id>/`）落 DESIGN.md §六（方案原文写「§4」为旧编号，实际知识体系=§六）。
- **验证**：`scripts/pack_doctor.py` 0 error（1 warning=edu-rating 已知预期）；pytest test_skills + test_experts 49 passed。
- **文档同步**：DESIGN.md §六 四层知识体系段、本文件、docs/plans/CLAUDE.md、packs/CLAUDE.md（binary 包行/薄路由 15 个/route_index 计数）。

### M2 已实施（2026-09-23，随 toolchain-registry M1 收尾一并落地）

- **工具落位**：`tools/py/android/` 5 件 copy 自素材库（android_ctf_runner / godot_ctf_runner / upx_shlib_emu + apply_relocs + nrv2b 三件套，同目录 import）；runner 纯 stdlib，godot/unpack-kit 仅依赖 unicorn。2 个专用 solver（ali_crackme3/wbox）照 §3 拍板 #3 不进 registry，已随 M3 落案例库。
- **registry 声明**（§4.2 草案对齐 M1 定稿 schema 后落定）：三条目 `kind: python-tool, acquire: "bundled", bin: {"*": "py/android/<脚本>.py"}`（相对 tools/ 规范位，跨平台单键；草案的 `deps` 字段对应 M1 定稿的 `pip: ["unicorn"]`；`verify` 试跑留 M3 面板故未声明）。tools/registry.json 16→19 条目。
- **Agent 引导文本**（DECOMPILE_GUIDANCE 先例）：android-rev SKILL.md 文末新增「自动化工具面（registry 纳管，缺失即降级）」段——三件清单 + `run_cmd host python` 调用形态 + 分诊起步先跑 runner（结论仍由 Agent 声明）+ **降级纪律**（工具缺失/unicorn 未装不空等安装，回 unpacking.md ②/godot.md ⑥ 手工路径，环境缺什么看 detector 能力清单不自己猜）。
- **venv 衔接**：unicorn 实装随 toolchain-registry M2（tools/venv 建立与运行时落位），当前 status=bundled（脚本文件在）+ pip 声明在册；缺依赖的降级路径已在 SKILL.md 写死。
- **测试守卫**：tests/test_toolchain.py `test_repo_registry_validates` 扩展 android 三件断言（kind/acquire/bin 文件存在）。
- **文档同步**：DESIGN.md §六 android 段补 M2 一笔 + §工具链注册表「首批 16 条目」改 19、tools/CLAUDE.md（目录结构 py/android 行）、本文件 §6 → 归档。
- **真机冒烟**：probe_tools 全表——android 三件 ready source=bundled，ida/ghidra fallback 命中本机实位，jadx/apktool/adb config/tools.json 指认命中。
