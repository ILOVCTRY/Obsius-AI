---
title: Android 动态调试绕过框架速查
summary: root 检测、SSL pinning、模拟器检测、反调试四合一绕过框架（FridaBypassKit）的用法与部署方式
phase: reverse
vuln_class: [android, frida, anti-analysis]
---

# Android 动态调试绕过框架速查

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）。
> 开箱即用的四大绕过框架合集：**root 检测 / SSL pinning / 模拟器检测 / 反调试**。
> 单项 hook 的手写代码见 [frida-cookbook.md](frida-cookbook.md)。

## 框架选择对照

| 需求 | 方案 | 速度 | 可定制 |
|------|------|------|--------|
| 快速全绕过（拒绝服务加固） | Objection（`android root disable` + `android sslpinning disable`） | 秒 | 低 |
| 精细控制 + 组合绕过 | 本框架合集（bypass 脚本按模块加载） | 秒 | 高 |
| 复杂对抗（native 检测、多卡点） | Magisk 模块（Shamiko/Hide My Applist）+ 本框架 | 分钟 | 最高 |

## 一键加载

```bash
# Spawn 模式加载全部绕过（推荐）
frida -U -f com.target.app -l bypass_all.js --no-pause

# 附加模式
frida -U com.target.app -l bypass_all.js
```

`bypass_all.js` = root 绕过 + SSL pinning 绕过 + 模拟器检测绕过 + 反调试绕过
四模块按序加载（源码骨架见 frida-cookbook 对应小节）。

## root 检测绕过要点

拦截面：`File.exists`（su/magisk/busybox 路径）、`Runtime.exec`（which su）、
`Build.TAGS`（test-keys → release-keys）、`/proc/self/mounts`、
`PackageManager.getInstalledPackages`（magisk 包名）。

升级对抗（应用检测更隐蔽时）：

```text
- Magisk 随机化包名 + Zygisk + Shamiko（白名单模式）
- Hide My Applist：对目标应用隐藏 root 相关包
- 关键：Java 层绕不过的 native 检测 → 用 native hook open/access/opendir
```

## SSL pinning 绕过要点

三层拦截（自上而下，命中即停）：OkHttp3 `CertificatePinner.check` →
conscrypt `TrustManagerImpl.verifyChain` → `NetworkSecurityConfig.isCleartextTrafficPermitted`。

Flutter 应用额外用 libflutter.so 特征码 patch（见
[android-advanced.md](android-advanced.md) Flutter 专项段）。

## 模拟器检测绕过要点

覆盖 `Build` 指纹字段（FINGERPRINT/MODEL/MANUFACTURER/BRAND/DEVICE/PRODUCT/HARDWARE）+
`TelephonyManager`（getDeviceId/getSubscriberId/getSimSerialNumber/getLine1Number）
+ 传感器/电池/通话状态常见模拟器特征。

常用设备指纹参考值：Pixel 2 (walleye) / Android 8.1.0 / release-keys。

## 反调试绕过要点

| 检测点 | 拦截 |
|--------|------|
| `Debug.isDebuggerConnected` | 返回 false |
| `android.os.Debug.waitingForDebugger` | 返回 false |
| `/proc/self/status` 的 TracerPid | native hook 文件读取，篡改返回值为 0 |
| `Ptrace` 反附加 | native hook `ptrace(0,0,0,0)` 返回 0 |
| 定时器自杀（被调试延时退出） | hook `System.exit` / `Process.killProcess` 空转 |

## 验证清单

```text
□ frida -U -f 启动后应用不闪退
□ logcat 中 Frida 脚本输出 [Root]/[SSL]/[Emulator]/[AntiDebug] 各模块生效
□ 目标应用核心功能可正常操作（登录/下单等，确认没有静默检测）
□ 若应用仍异常：logcat 抓 native 层检测点，转 android-advanced.md native hook 段
```

## 参考资源

FridaBypassKit: https://github.com/okankurtuluss/FridaBypassKit ·
Objection: https://github.com/sensepost/objection ·
iOS bypass 对照版见原项目 Frida-iOS-Bypass-Kit。
