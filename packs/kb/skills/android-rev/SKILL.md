---
name: android-rev
description: Android/移动端逆向：APK 分层分诊、JNI/native 五线、加固脱壳、Godot 专项
keywords: android, apk, 安卓, jni, ndk, dex, smali, 加固, 脱壳, godot, crackme, frida, so
file_features: is_apk, has_native_lib, has_jni, godot_engine, packed_so
task_types: triage, reverse, verify
mode: self-contained
---

# android-rev —— Android 逆向：分诊 → 五线 → 脱壳 → 验证

## 手册对照表（特征 → 打开哪篇，用 skill_open）

| 场景/特征 | 手册 |
|---|---|
| 拿到 APK 不知道往哪层打 | `references/binary/android/triage-and-layering.md`（四分判据 + 分诊命令 + 特征词声明） |
| native so 校验（has_jni） | `references/binary/android/native-five-lines.md`（JNI 桥 / 常量 / SMC / 验证 / 诱饵） |
| so 被壳折叠（packed_so） | `references/binary/android/unpacking.md`（识别四征 + unicorn 脱壳工程） |
| Godot 引擎 APK（godot_engine） | `references/binary/android/godot.md`（markers + GDExtension + 加密信封） |
| 动态 hook（运行时行为/算法观察） | `references/binary/android/frida-cookbook.md`（加密捕获/网络/存储/脱壳 dump 脚本集） |
| 反检测对抗（root/SSL/模拟器/反调试） | `references/binary/android/frida-bypass-kit.md`（四合一绕过框架 + 升级对抗） |
| native 深度操作 / 框架专项 | `references/binary/android/android-advanced.md`（IDA JNI 技巧/native hook/加固厂商表/Flutter/RN） |
| 安全审计视角（产出报告） | `references/binary/android/apk-security-checklist.md`（MASTG 清单） |
| 已解题型比对 | `android/cases/`（命中识别特征直接复用 solver 重验证） |

## 反空转规则（先于一切）

1. **native 存在优先 JNI 线**：Java 摘要上不过度花费——Java 只门卫（输错弹
   toast）时直接转 JNI 桥定位。
2. **诱饵不止步**：Morse/Base64/MD5-like 命中即排除一条路，落 finding 继续，
   不重试死路。
3. **候选必须验证**：字符串是候选不是证据——证明到达 final compare 且有 solver
   验证输出（测试向量 / 双向 round-trip / 复现 app 输出）才算结论；可逆变换
   优先双向验证。
4. 分诊结论（关键层判定）落 finding 即停，不把分诊做成解题。

## 特征词声明

分诊时按观察声明 file_features（路由 ×3 加权，声明传入须与技能 frontmatter
`file_features` 字段同词）：`is_apk`（APK 容器）、
`has_native_lib`（lib/*.so）、`has_jni`（Java_*/JNI_OnLoad/RegisterNatives）、
`godot_engine`（assets/.godot markers）、`packed_so`（DEX 壳/so fold）。与
binary-rev 的 `is_elf/is_pe` 不冲突（APK 是 zip 容器）；`packed_so`（加固 so）
≠ `packed_binary`（通用壳）。

## 与 binary-rev 分工

通用 ELF/PE 逆向纪律（func_kb 查重落库、大文件读取纪律、变换速查、docker 验证）
沿 binary-rev 技能，本技能只补 Android 容器层（APK/DEX/JNI/壳/引擎）与移动专项
知识。样本 untrusted 红线不变：样本执行只进 docker/sandbox，宿主只做静态解析。

## 自动化工具面（registry 纳管，缺失即降级）

工具链注册表（tools/registry.json）声明三件（`tools/py/android/` 随仓自带，
`run_cmd host python <tools>/py/android/<脚本>.py …` 经网关执行）：

- `android-ctf-runner`：APK 自动分诊（清单/权限/native/JNI 线索 → 工作区 + JSON +
  Markdown 报告），纯 stdlib。分诊起步先跑它，输出作分诊判据输入，**结论仍由你声明**。
- `godot-ctf-runner`：Godot APK 全自动（markers→壳检测→脱壳→reloc→常量提取），
  依赖 unicorn。
- `android-unpack-kit`：UPX-shlib 脱壳三件套（upx_shlib_emu 主入口 + apply_relocs +
  nrv2b），依赖 unicorn。

**降级纪律**：工具缺失或依赖（unicorn）未装时**不空等安装**——回手册手工路径
（`unpacking.md` ②脱壳工程 / `godot.md` ⑥手工兜底），结论照常落 finding；
环境能力清单（detector）已列缺什么，不要自己猜。
