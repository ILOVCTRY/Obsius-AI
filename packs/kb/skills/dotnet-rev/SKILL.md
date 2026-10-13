---
name: dotnet-rev
description: .NET/C# 托管逆向：CLR 识别、混淆器识别与脱壳（de4dot）、dnSpyEx 静态/动态调试、IL patch 优先于 C# 重编译、红队 Sharp* 工具分析与配置提取
keywords: dotnet, csharp, c#, .net, il, dnspy, dnspyex, de4dot, ilspy, 混淆, 脱壳, confuser, smartassembly, reactor, 托管, clr, necrobit, rubeus, sharphound
file_features: dotnet, mono
task_types: reverse, analyze, verify
mode: self-contained
---

# dotnet-rev —— .NET 托管逆向：识别 → 脱壳 → 静态/动态 → IL patch

## 适用场景

- 标准 C# exe/dll、Mono/Unity 托管层、Xamarin、红队 Sharp* 工具（Rubeus/SharpHound
  等）的分析与配置提取
- 混淆对抗：ConfuserEx / SmartAssembly / .NET Reactor(necrobit) / Eazfuscator

**切出**：IL2CPP / NativeAOT 编译成 native，没有 CLR 元数据 → 用 binary-rev（IDA/r2）；
native loader + .NET payload 混合体 → loader 走 binary-rev，dump 出托管 payload 后切本技能。

## 手册对照表（特征 → 打开哪篇，用 skill_open）

| 场景/特征 | 手册 |
|---|---|
| 拿到 exe 不知道是不是 .NET / 哪种混淆 | `references/binary/dotnet/obfuscators.md`（总决策表 + de4dot --detect） |
| de4dot 失败 / anti-tamper / necrobit | `references/binary/dotnet/obfuscators.md`（退路顺序：dump → dnlib → 动态优先） |
| 完整工作流 / IL patch vs C# 重编译 / async 状态机 | `references/binary/dotnet/common-workflow.md` |
| 异常驱动控制流 / 提取配置与 C2 / 密钥 | `references/binary/dotnet/common-workflow.md`（动态调试 + 配置提取段） |
| Sharp* 红队工具结构 / 改特征 / 工具安装矩阵 | `references/binary/dotnet/sharp-tools.md` |
| Linux/macOS 上没有 dnSpyEx 怎么办 | `references/binary/dotnet/sharp-tools.md`（CLI 矩阵：ilspycmd + dnlib） |

## 红线与收尾

- **IL 编辑优先于 C# 编辑**——async/状态机/闭包重编译几乎必失败
- 每一步落盘：原样本 → target-clean（脱壳）→ target-patched，notes.md 记录
  混淆器/解密器 token/关键地址
- 动态优先：跑起来在解密点下断看明文，往往比硬脱壳快
- 结合方式：dnSpyEx 交互分析 → 关键结论落 func_kb（bb_upsert_func）→ 平台
  writeback / MCP 同步（如注册了 dnSpy MCP 后端）
