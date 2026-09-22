---
phase: triage
---

# 分诊与分层（Android / APK）

> 来源：r0re 收编改编（2026-09-21，方法论经实战验证后剥离其编排机制）。本篇回答
> 「这个 APK 该往哪一层打」——分诊错了，后面全是空转。术语：JNI 桥（JNI bridge）、
> 加固（厂商壳 packing）、SO（native 共享库 .so）、诱饵（decoy）。

## ① 关键层四分判据

| 层 | 判据 | 主战场 | 何时放弃该层 |
|---|---|---|---|
| Java/smali only | 无 `System.loadLibrary`、无 `native` 方法、lib/ 无 .so | jadx 反编译源码直接读校验逻辑 | 校验不在这层（字符串/常量全是诱饵） |
| JNI bridge | 有 native 声明，但 so 内逻辑薄，真校验还在 Java | 定位桥接点后回 Java | so 才是校验本体 |
| native validator | so 里有比较循环/常量表/加密实现 | so 静态分析（五线，见 [native-five-lines.md](native-five-lines.md)） | so 被壳折叠且静态还原不了 → 动态 |
| dynamic-only blocker | 加固 + 反调试 + 环境校验叠加，静态读不到有效逻辑 | frida 动态 hook / 模拟执行（执行类操作一律走 run() 网关） | 能静态还原时绝不先动态 |

纪律：**native 存在优先 JNI 线**——Java 摘要上不过度花费；Java 只门卫（输错弹
toast）时，直接转 JNI 桥定位，不继续磨 Java。

## ② 快速分诊命令序列

```bash
file <target.apk>
aapt dump badging <target.apk>          # 包名/版本/入口 Activity
aapt dump permissions <target.apk>
apktool d <target.apk> -o apktool_out/  # smali + 资源 + 清单
jadx --no-res --show-bad-code -d jadx_out/ <target.apk>
readelf -h lib/arm64-v8a/*.so           # 架构
strings -a lib/*/lib*.so | grep -iE "key|secret|encrypt|flag|jni"
readelf -Ws lib/*/lib*.so | grep -iE "JNI_OnLoad|Java_|encrypt|verify"
```

grep 模式速查（jadx_out/）：`password|secret|token|api_key|encrypt|decrypt|AES|RSA|MD5|HMAC`；
网络 `http://|https://`；存储 `SharedPreferences|sqlite|ContentProvider`。

### 特征词声明对照（file_features 由分诊 Agent 声明传入，路由 ×3 加权）

| 观察到 | 声明 |
|---|---|
| APK 容器（zip + AndroidManifest.xml） | `is_apk` |
| lib/\<abi\>/*.so 存在 | `has_native_lib` |
| `Java_*` 导出符号 / JNI_OnLoad / RegisterNatives | `has_jni` |
| assets/.godot/、project.binary、libgodot_android.so | `godot_engine` |
| classes.dex 异常小或加密、lib/ 带解壳 stub（360/腾讯乐固/百度） | `packed_so` |

## ③ 分析状态字段模型（ctf_state 字段集提炼）

按序推进，每项确认即落 finding（`bb_add_finding`，evidence 必须带命令输出）：

```
target_apk → package → launcher → java_entry → native_libs → jni_bridge
→ native_entry → constants → algorithm → solver → verification
```

`verification` 未通过前，任何候选答案只是假设——不许把 hint 推出的候选当结论登记。

## ④ stop-early 与层覆盖自检

- 分诊结论（包名/入口/关键层判定/下一步最有价值方向）落 finding 后**立即停**，
  不把分诊做成解题；低价值层（纯资源翻找、重复反编译）不空转。
- 层覆盖自检（哪层没碰过哪层就是盲区）：manifest（导出组件/权限/deep link）·
  Java/smali · 资源（strings.xml 硬编码密钥）· native（JNI/加固/反调试）·
  网络（端点/请求签名/证书绑定）· 存储（SharedPreferences/SQLite/keystore）。
- 目标类型 → 高价值意图对照：

| 目标 | 高价值意图 |
|---|---|
| 加密算法 | Cipher.getInstance → SecretKeySpec 来源 → IV 生成 → 模式/padding |
| API 端点 | OkHttp/Retrofit → interceptor → base URL → 签名头构造 |
| 认证逻辑 | SharedPreferences token → LoginActivity → verify 方法 → 会话管理 |
| 隐藏功能 | 导出组件 → deep link handler → debug flag → 功能开关 |
| native 算法 | native 方法声明 → JNI_OnLoad/RegisterNatives → 参数编组 |
| 绕 root/SSL 检测 | 检测点 → 异常处理 → 开关/flag |

- 诱饵初判：Morse/Base64/MD5-like 常见诱饵**不止步**（处置见
  [native-five-lines.md](native-five-lines.md) §⑤）。
