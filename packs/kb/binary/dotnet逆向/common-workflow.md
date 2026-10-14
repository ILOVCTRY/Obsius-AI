---
title: .NET 逆向工作流与 IL patch
summary: 识别→脱混淆→静态→动态→patch 六阶段细节；IL 编辑优于 C# 重编译；async 状态机识别、dnlib 脚本化、配置/C2 提取模式
phase: reverse
vuln_class: [dotnet]
---

# .NET 逆向通用工作流（六阶段 + IL patch）

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）。
> 完整工作流细节、IL patch 可靠性、字符串解密器提取、状态机识别、dnlib 脚本化。
> 配合技能 [dotnet-rev](../../capabilities/binary/skills/dotnet-rev/SKILL.md) 使用；
> 脱壳手法详见 [obfuscators.md](obfuscators.md)。

## 端到端六阶段

```text
1. Identify  → 确认是 .NET 托管程序（不是 native）
2. Detect    → DIE / de4dot --detect 识别混淆器
3. Deobf     → de4dot 脱混淆（保留原样本）
4. Static    → dnSpyEx 浏览 C# 视图定位，IL 视图看关键逻辑
5. Dynamic   → dnSpyEx 调试器在关键方法下断，看运行时明文
6. Patch     → IL 编辑器修改，Save Module
```

每一步产物落盘：原样本 `target.exe` → 脱壳 `target-clean.exe` → patch 后
`target-patched.exe`。

## IL patch vs C# patch 可靠性

**核心结论：关键修改用 IL 编辑器，不要用 C# 编辑器。**

| 维度 | C# 编辑器 (Edit Method C#) | IL 编辑器 (Edit IL) |
|------|---------------------------|---------------------|
| 编译失败风险 | 高（缺引用、语法、lambda 重写失败） | 几乎为零 |
| 信息保真 | 编译器重新生成 IL，可能与原 IL 不同 | 原样替换，逐指令改 |
| 适用 | 改个字符串、改个常量、简单逻辑 | 改判断、删校验、改控制流 |
| async/await/状态机 | 经常编译失败或扭曲 | 直接改状态机字段，可靠 |

dnSpyEx 的 C# 反编译器是只读反编译 + 尝试重编译，对编译器生成的代码（状态机、
闭包、`yield`）重编译极易失败。IL 编辑器逐指令编辑，所见即所得。

### 典型 IL patch 模式

```text
改判断（if (check) → 永远 true）：
  原: call bool Foo::Check()
      brfalse.s SKIP
  改: ldc.i4.1            ; push true
      brfalse.s SKIP      ; 现在永远不跳
  或更直接：ldc.i4.1 + ret

改判断（→ 永远 false）：ldc.i4.0 + ret

删整段校验：全部 nop，或改成 ret + 正确返回值

改字符串常量：C# 编辑器改 ldstr 通常 OK；字符串在资源/加密里则要改解密逻辑

改数字常量：ldarg / ldc 指令直接改操作数
```

## 状态机识别（async/await / yield）

C# 的 `async/await` 和 `IEnumerator` yield 编译成**状态机**：编译器生成嵌套类，
`MoveNext()` 里用 `state` 字段做 switch dispatch。C# 视图可能反编译失真，IL 视图看
`MoveNext` 最准。

```text
async/await 的 MoveNext 结构：
  switch(this.<>1__state) {
    case 0: ... await 前的逻辑; this.<>1__state = 1; await MoveNext;
    case 1: ... await 后的逻辑;
  }

要 patch async 逻辑：改 MoveNext 里的 state 转移或具体 case 里的判断。
C# 编辑器改 async 几乎必失败 → 必须用 IL。
```

## dnlib 脚本化（批量解字符串 / 元数据编程）

dnlib 是 .NET 元数据编程事实标准（de4dot 内部就用它）。自定义脱混淆脚本首选：

```csharp
// 思路：加载原 assembly → 找解密方法 → 反射调用拿明文 → 遍历所有方法把
// call Decryptor(token) 替换成 ldstr "明文" → module.Write 写回
using dnlib.DotNet;
using dnlib.DotNet.Writer;

var module = ModuleDefMD.Load(args[0]);
var decryptorToken = uint.Parse(args[1], System.Globalization.NumberStyles.HexNumber);
foreach (var type in module.GetTypes())
    foreach (var method in type.Methods)
    {
        if (!method.HasBody) continue;
        var instrs = method.Body.Instructions;
        for (int i = 0; i < instrs.Count; i++)
        {
            // 识别 call 解密器模式：MethodInfo.Invoke 拿明文 →
            // instrs[i] = OpCodes.Ldstr + operand=明文
        }
    }
module.Write("target-decrypted.exe", new ModuleWriterOptions(module));
```

## 动态调试要点

dnSpyEx 调试器对 .NET 比 native 友好得多：

- **断点在方法入口**：右键方法 → Add Breakpoint
- **看对象值**：断住后 Locals / Watch 直接看对象字段、字符串内容
- **内存写入**：直接改运行时变量值（Edit Value）
- **异常断点**：Debug → Exceptions 勾选目标异常——混淆器常用异常驱动控制流，
  断异常能看到真实路径

### 异常驱动控制流

部分混淆器把正常逻辑塞进 `try`，用 `throw` + `catch` 做跳转。静态看 IL 像异常
处理，实际是控制流：

```text
try { throw new CustomException(0x42); }
catch (CustomException e) {
    switch(e.Code) {
        case 0x42: 真实逻辑A; break;
        case 0x43: 真实逻辑B; break;
    }
}
```

下异常断点（断 `CustomException`），跟踪 `Code` 值流转，比硬啃 IL 快。

## 模块初始化器（Module .cctor）

`<module>` 的静态构造函数在 assembly 加载时最先执行，混淆器常把 anti-tamper /
解密初始化放这里：

```text
1. 先看 <module>.cctor——解密/反调试初始化
2. 再看 Program.Main / Startup
3. anti-tamper 在 .cctor 里 → 先 patch .cctor 再脱壳
```

## 提取配置 / C2 / Key 的通用模式

红队工具和 loader 常把配置加密嵌在资源或字段里，运行时解密：

```text
定位流程：
1. strings 看有无明文 URL/IP（混淆后通常没有）
2. 找 byte[] 字段 + 解密方法（AES/XOR）
3. 动态断在解密方法的返回点，dump 解密后的明文
4. 常见：AES-256-CBC with Key==IV（Codegate 2013 模式）
```

红队工具的具体配置结构见 [sharp-tools.md](sharp-tools.md)。

## 与平台 native 逆向的边界

- **IL2CPP / NativeAOT** → 编译成 native，没有 CLR 元数据 → 走 binary-rev 技能
  （IDA/r2），本篇仅做识别
- **托管 .NET**（标准 C# exe/dll、Mono/Unity 托管层、Xamarin）→ 本篇
- **混合（native loader + .NET payload）** → loader 走 binary-rev，dump 出
  .NET payload 后切本篇

## 落盘产物清单

- `target-original.exe`（原样本，不动）
- `target-clean.exe`（de4dot 脱壳后）
- `notes.md`（识别的混淆器、解密器 token、关键方法地址、配置/C2/key）
- `target-patched.exe`（patch 后，如需要）
- `il-diff.txt`（patch 前后 IL 对照，如做 patch）
