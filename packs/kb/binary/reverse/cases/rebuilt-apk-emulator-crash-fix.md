---
title: 案例：重打包 APK 模拟器两层崩溃修复
summary: 重打包 APK 在 MuMu 模拟器先崩 KSAd 广告 SDK 反欺诈路径、再崩 Fragment 生命周期 super 调用缺失的两层修复复盘
phase: reverse
vuln_class: [android, repackaging]
---

# 案例：重打包 APK 在 MuMu 模拟器的两层崩溃修复

> 来源：reverse-skill 1.0.1 field-journal 收编（2026-09-30，MIT，见 `../../../licenses/`）。
> 案例日期 2026-05-15，原文脱敏。相关技能 [android-rev](../../capabilities/binary/skills/android-rev/SKILL.md)。

## 一句话

Cellular-Pro 重打包 APK 在 MuMu 12 上需要**两层修复**：先短路快手 KSAd 广告 SDK
的设备指纹/广告网络路径（模拟器翻译层崩溃），再恢复被 stub 成 no-op 的 Fragment
生命周期 super 调用（SuperNotCalledException）。

## 为什么是两层

隐私同意崩溃不是单一问题——第一层 native 崩溃修掉后，后续启动阶段又暴露 KSAd
模拟器不兼容，最后是应用侧 Fragment `onResume()` 被清空导致的
`SuperNotCalledException`。**修一个崩一个往下走，要有心理预期是连环坑。**

## 排查套路

```text
1. 确认崩溃只在模拟器出现（真机正常）→ 怀疑三方 SDK 模拟器检测/不兼容
2. 定位崩溃栈中的三方 SDK 路径：
   - com.yxcorp.kuaishou.addfp（设备指纹）
   - com.kwad.sdk.utils.bc
   - com.kwad.sdk.core.network
   → smali 层把这些路径 stub 掉
3. 应用自身崩溃：SuperNotCalledException
   → 检查 Fragment 生命周期方法是否被替换为 return-void
   → 恢复对 androidx.fragment.app.Fragment 的直接 super 调用
```

## 可复用模式

- **重打包 APK + 模拟器 = 三方 SDK 先崩**：广告/风控 SDK 的设备信息收集器
  （addfp/kwad 类）是模拟器崩溃头号嫌疑，先 stub 再看应用自身
- **修复后继续崩 ≠ 修错了**：多层崩溃会依次暴露，逐层修
- Fragment 生命周期被 stub 后必须恢复 super 调用，否则 `SuperNotCalledException`
