---
title: 案例：Windows 逆向工具链自举实录
summary: Windows 24H2 上 57/64 工具链自举的过程与踩坑：winget/aria2 下载模式、MCP 注册≠可用、ABI 陷阱、Linux-only 缺口清单
phase: reverse
vuln_class: [toolchain, windows]
---

# 案例：Windows 24H2 逆向工具链完整自举

> 来源：reverse-skill 1.0.1 field-journal 收编（2026-09-30，MIT，见 `../../../licenses/`）。
> 案例日期 2026-07-20。覆盖原生/托管/Android/固件/协议/取证/浏览器/MCP 的工具链
> 安装验证。

## 执行链路

```text
1. 读共享 tool-index 复用已装工具 → 2. PATH 探针找缺口 →
3. 大文件从可信清单取 URL+SHA-256，aria2 禁 IPv6 下载 →
4. 便携工具统一落 {user_profile}\Tools\reverse-bin →
5. 注册 Ghidra/IDA/JS/浏览器流量/Burp MCP →
6. 用真实 PE/APK/.NET/PYC/WASM/固件夹具验证（不是只跑 --version）→
7. 刷新 tool-index + 出安装报告
```

## 踩坑表

| 问题 | 解决 |
|------|------|
| winget 大文件下载无进度（Delivery Optimization + IPv6 不稳） | 从 winget 元数据取官方 URL/散列，`aria2c --disable-ipv6=true` |
| better-sqlite3 ABI 不匹配（Node ABI vs Electron ABI） | 用项目自己的 `pnpm run postinstall` 重建 |
| 裁剪版 Windows 无 WSL/Hyper-V 功能包 | 记录真实 Linux-only 缺口，用 Windows 原生工具 + QEMU full-system，**不造同名伪包装器** |
| Dr. Memory 在 24H2 build 26100 注入崩溃 | 换 AppVerifier/PageHeap/UMDH/CDB/Frida |
| Burp MCP 注册后无工具 | GUI 未加载扩展、9876 未监听——**MCP 注册成功 ≠ 工具可调用**，要分别验证 stdio/HTTP 初始化与 GUI 后端在线 |

## 关键发现

- 通用探针 57/64；7 个缺口全部属于 Linux 用户态/内核能力
- Windows SDK Debugging Tools（CDB/GFlags/UMDH/NTSD/KD）是 Dr. Memory 不兼容时
  的重要补充
- **IDA Free 不能替代合法 IDA Pro 的 idalib/Hex-Rays MCP 后端**

## 可复用模式

- 大文件下载固定模式：官方包元数据核对版本/URL/SHA-256 → aria2 禁 IPv6 →
  验散列 + Authenticode
- capability 状态应区分 `installed / bridge-ready / backend-online /
  runtime-verified` 四级，别用单一布尔
