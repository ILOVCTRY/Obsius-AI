---

phase: reverse
vuln_class: [reverse, android, electron, game-engine, hardware, polyglot, vm]
title: 平台与框架逆向
summary: 先认「这是什么容器」，再决定拆哪一层——APK 拆到 native、Electron 拆 asar、游戏引擎拆资源包、硬件拆状态机。判据是 flag 到底在哪一层算出来。
---

# 平台与框架逆向

脚本/字节码/esolang 见 [[binary/逆向工程/languages.md|脚本与字节码]]；Go/Rust/Haskell 见 [[binary/逆向工程/languages-compiled.md|编译型语言]]；固件/驱动/Mach-O 见 [[binary/逆向工程/platforms.md|平台与容器]]。

## 0. 判据速查

| 目标形态 | 真正的逻辑在哪 | 去哪节 |
|---|---|---|
| APK + `System.loadLibrary`，但 `.so` 里没有 `Java_*` 符号 | `JNI_OnLoad` 里的 `RegisterNatives` | §1 |
| APK 静态分析看着正常，跑起来行为不同 | native 在内存里改了 DEX | §2 |
| APK 一堆 Java 层校验（PIN/root/随机数）挡着 | 建同名包新工程直接调 `.so` | §3 |
| APK 要过证书固定 / 云函数校验 | Frida 在运行时调真方法 | §4 |
| APK 只响应自己发的本地广播 | smali 把 `onReceive` 搬进 `onCreate` | §5 |
| `.asar` / Electron 目录 | JS 只是壳，flag 在原生 ELF | §6 |
| npm 包被混淆到读不动 | `Object.getOwnPropertyNames` 运行时枚举 | §7 |
| `.rbxlbin` 里最新版是假 flag | 版本历史里翻旧版 | §8 |
| `.pck` 加密 | 先从可执行文件里抠密钥 | §9 |
| Rust 二进制读 JSON 只输出对/错 | serde `Visitor` 的方法名就是 schema | §10 |
| `.v` / `.sv` Verilog 源码 | 状态机 + 移位寄存器当历史 | §11 |
| 同一文件 `ruby` 和 `perl` 都能跑 | 注释块语法差异让两边跑不同代码 | §12 |
| `.ulx` / `.blorb` 互动小说 | 字典表里还留着开发者动词 | §13 |
| 乱码但是定长字母数字 | EBCDIC（`cp500`） | §14 |
| SGX enclave `.so` | ECALL 表 + 标准 x86-64 | §15 |

## 1. Android JNI `RegisterNatives`

Java 里声明了 `native`，但 `.so` 里搜不到 `Java_com_pkg_Class_method` —— 说明走了 `RegisterNatives`，**方法名与符号名被人为解耦**，光搜字符串找不到 handler。

```java
static { System.loadLibrary("audio"); }
private final native ProcessedMessage processMessage(SmsMessage msg);
```

定位链路：`JNI_OnLoad`（永远导出）→ `RegisterNatives(env, clazz, methods, count)` → `methods` 是 `{name, signature, fnPtr}` 数组 → 跟 `fnPtr`。

```c
static JNINativeMethod methods[] = {
    {"processMessage", "(Landroid/telephony/SmsMessage;)LProcessedMessage;", (void*)real_handler}
};
```

**分析架构选 `x86_64` 而不是 `arm64-v8a`**：`unzip` 后看 `lib/x86_64/`，Ghidra 对 x86_64 的反编译质量明显更好，逻辑等价。

## 2. 运行时改 DEX

native 库读 `/proc/self/maps` 找到已加载的 DEX，`mprotect` 改成可写，再 XOR 改掉指定偏移的字节码。**静态看 APK 里的 `classes.dex` 是干净的**，行为全在内存里。

离线复原三步：从 APK 抽出 DEX → 从 `.so` 里找 XOR key 和 patch 偏移 → 施加同样的补丁。

```python
dex = bytearray(open('classes.dex','rb').read())
for i in range(patch_offset, patch_offset + 144):
    dex[i] ^= 0x5A
# 必须重算 DEX checksum 与 SHA-1，否则 jadx 拒读
```

**只在 Dalvik（API < 21）成立**，ART 上 DEX 被编译成 OAT 后不再原地执行。

## 3. 新建工程直接调 `.so`

Java 层堆了一串校验（随机数、PIN、root 检测）挡在 native 调用前面时，**不必逆这些校验**：JNI 符号名本身编码了包路径和类名。

```java
package com.example.puing.a2018codegate;      // 与目标完全一致
public class Main4Activity extends AppCompatActivity {
    static { System.loadLibrary("hello-libs"); }
    protected void onCreate(Bundle b) {
        Log.d("FLAG", stringFromJNI());        // 绕过全部 Java 校验
    }
    public native String stringFromJNI();
}
```

判据：flag 在 native 里算出并返回给 Java，而 Java 在调用前有多道闸。包名/类名/方法签名三者必须逐字对上，否则 `UnsatisfiedLinkError`。

## 4. Frida 在运行时直接调

### 4.1 证书固定

不用起 MITM 代理，也不用改 APK——secret 常常就在类方法里：

```javascript
Java.perform(function() {
    var R = Java.use("com.h1702ctf.ctfone.Requestor");
    console.log("hName: " + R.hName());
    console.log("hVal:  " + R.hVal());
});
```

拿到的是 HTTP header 名和值，服务端校验直接过。**先试这个，再考虑 pinning bypass 脚本**。

### 4.2 Firebase 云函数

客户端负责构造 payload（`uid + 金额 + 时间戳`），服务端只校验格式。登录后 hook 直接调：

```javascript
var auth = Java.use('com.google.firebase.auth.FirebaseAuth').getInstance();
var uid  = auth.getCurrentUser().getUid();
var ms   = Java.use('java.lang.System').currentTimeMillis();
var data = Java.use('java.util.HashMap').$new();
data.put("payload", uid + "+100+" + ms);
Java.use('com.google.firebase.functions.FirebaseFunctions')
    .getInstance().getHttpsCallable("validateScanPayload").call(data);
```

判据：有 `google-services.json`、`build.gradle` 里有 Firebase 依赖。

### 4.3 日志就是密钥材料

安全应用用 `Log.d()` 把 Curve25519 协商值、每消息的临时共享密钥、消息 ID 全打出来时：

```bash
adb logcat | grep -E "(agreement|ephemeral|shared|key)" > crypto_log.txt
```

`key = f(base_agreement, shift_counter)`，`iv = f(ephemeral_shared)`。**先扫 logcat 再逆算法**，能省掉整个密码学部分。

### 4.4 anti-debug 静态绕

native ARM 里三道顺序检查：`/proc/self/status` 的 `TracerPid` 非零 → `su` 存在 → `__system_property_get` 读自定义属性。三道都过才算出正确寄存器值。

**不上调试器**：IDA 图视图走 happy path，反推每个分支处寄存器该是什么值。

## 5. smali 注入

### 5.1 打 `LocalBroadcastManager`

只响应自身广播的 receiver，`adb shell am broadcast` 会被静默丢弃（本地广播不出进程）：

```bash
adb shell cmd package query-receivers --brief -a com.tamu.ctf.START
# No receivers found
```

把注册换成 `Context.registerReceiver` 会因 manifest 不匹配闪退。正解是**把 `onReceive` 的函数体搬进 `onCreate`**，让去混淆器开机就跑，明文进 logcat：

```smali
const/4 v1, 0x0
invoke-static {v1}, Lio/michaelrocks/paranoid/Deobfuscator$app$Debug;->getString(I)Ljava/lang/String;
move-result-object v1
invoke-static {v1, v1}, Landroid/util/Log;->d(Ljava/lang/String;Ljava/lang/String;)I
```

```bash
apktool b app -o app/dist/app.apk
jarsigner -keystore ~/.android/debug.keystore -storepass android \
  app/dist/app.apk androiddebugkey
adb install -r app/dist/app.apk && adb logcat | grep -i flag
```

**改完必须重签**，debug keystore 就够。

### 5.2 改签名参数

native 用 XOR 混淆的 key 存在 `.data`，用前才解。做法：root 设备挂 GDB stub → 在解密例程后下断 → dump 内存区拿明文 key → `baksmali` 找到构造签名请求的 smali → 改成签想要的参数。

**不必完整逆签名算法**，dump 到 key 就能自己算。

## 6. Electron + 原生二进制

Electron 是「JS 壳 + 原生干活」。flag 逻辑几乎总在原生 ELF/DLL 里。

```bash
npm install -g @electron/asar
asar extract resources/app.asar app_extracted/
find app_extracted/ -name "*.node" -o -name "*.so" -o -name "*vault*" -o -name "*auth*"
grep -rn "spawn\|execFile\|ffi\|require.*native" app_extracted/
```

JS 层常把密码校验流程写得明文——正好告诉你原生那边期待什么。加密数据去 ELF 的 `.data`/`.rodata` 找。

常见混淆是「常量 XOR + 移位 + key 循环 XOR」，注意 `>>` 是有符号还是无符号，`>>` 与 `>>>` 在 JS 里完全不同。

## 7. npm 包运行时自省

控制流平坦化 + RC4 字符串编码 + 死代码，静态分析性价比极低。直接 `require` 进来枚举：

```javascript
const mod = require('target-package/dist/lib/crypto.js');
for (const k of Object.keys(mod)) {
    const o = mod[k];
    console.log(k, Object.getOwnPropertyNames(o),
                     Object.getOwnPropertyNames(o.prototype || {}));
}
// 常见隐藏名：__getFullFlag__ / _debug / _raw / getFragment(i)
```

**模块被 `require` 时自己的解密就已经跑完了**，直接调解码后的函数即可。重点看 `__`/`_` 开头的属性。

## 8. Roblox `.rbxlbin`

最新版可能放的是假 flag，真 flag 在旧版。

```bash
curl -H "Cookie: .ROBLOSECURITY=..." \
  "https://assetdelivery.roblox.com/v2/assetId/{placeId}/version/1"
```

chunk 结构：`INST`（类桶与引用 ID）/ `PROP`（逐实例字段，含 `Script.Source`）/ `PRNT`（父子树）。解 `PROP` 里的 `Source`，逐版本 dump 后 diff。

## 9. Godot `.pck`

- `KeyDot` 从游戏可执行文件里抠加密密钥
- `gdsdecomp` 用密钥解包并还原成 Godot 工程
- 在编辑器里打开，搜脚本/资源

## 10. Rust serde_json schema 还原

Rust 二进制从 stdin 读 JSON、`serde_json` 反序列化、只打印对错。**schema 藏在 `Visitor` 实现里**：

- 每个 `visit_map`/`visit_seq` 揭示期望的键与嵌套
- 方法名就是类型：`visit_str` = 字符串、`visit_u64` = 数字、`visit_bool` = 布尔、`visit_seq` = 数组
- 字段名字符串字面量直接留在反序列化代码里

按 visitor 调用层级把键名顺序排出来，flag 往往就是这些键的拼接。

## 11. Verilog / 硬件

```python
TIMING = {                      # 每个动作占几个时钟周期，从状态机数出来
    "insert_coin": 3,
    "select_success": 7,
    "select_fail": 5,
    "cancel_with_coins": 4,
    "cancel_at_zero": 2,
}
# COINS_HISTORY 是每周期移位的寄存器
# H[0]=1, H[7]=4, H[28]=H[33]=H[38]=6, H[63]=H[73]=2, H[80]=9
# (H[19]+H[21]+H[56]+H[69]) mod 32 = 0
```

判据：`always @(posedge clk)` + 移位寄存器 + `case` 里用历史值当门。解法是**从要求的 tap 值倒推每个周期该发生什么动作**，答案通常是一串动作序列（`I9C_SP6_CNL_I2C_SP2_...`）。

**别按「时钟」想，按「动作耗时」想**——每个动作是固定长度的周期块，反推时是在排动作块而不是排周期。

## 12. Ruby/Perl polyglot

同一文件两个解释器都能跑，靠注释块语法差异让两边执行不同代码：

- Ruby：`=begin` … `=end` 是块注释
- Perl：`=begin` … `=cut` 是 POD，`=end` 被忽略

于是两种语言看到的是不同的有效代码行。典型分工：一边给**字符集约束**（如 50 个字符各用一次，且 `XOR(val,(val-16)%257)` 是模 257 的原根），另一边给**顺序约束**（插入排序的逆序数表）。

```python
def reconstruct_from_inversions(chars, inv):
    res, rem = [], sorted(chars)
    for i in range(len(chars) - 1, -1, -1):
        res.insert(inv[i], rem.pop(i))   # inv[i] = 左侧比它大的个数
    return res
```

判据：`ruby f && perl f` 都能跑，或题面提到 polyglot。

## 13. Glulx 互动小说

`.ulx`/`.blorb` 是 VM 字节码，校验常是模 2³² 的矩阵乘法。

**先找隐藏房间再逆校验**：游戏文本和对象表都还在，字典表 `.DictTable` 里往往留着 `xyzzy`/`plugh`/`debug`/`god` 这些开发者动词，进去的侧门直接给出目标向量和常量矩阵。

```python
from sage.all import matrix, Zmod
M = matrix(Zmod(2**32), rows)
answer = M.solve_right(vector(Zmod(2**32), output))
```

Glulx 整数是无符号 32 位，所以是 `Z_{2^32}` 上解线性方程，不是普通整数。

## 14. EBCDIC（AS/400 SAVF）

IBM 大型机原生编码，不是 ASCII：

```python
ascii_data = open('savefile.savf','rb').read().decode('cp500')
flag = ''.join(c for c in ascii_data if c.isupper() or c == '_')
```

**先解码再找交错规律**。SAVF 常把 flag 与垃圾按「取 2 跳 2」之类交错，按「大写字母 + 下划线」过滤是最快的捷径。

## 15. Intel SGX enclave

SGX 代码就是标准 x86-64，IDA 能完整反编译。入口是 `.so` 里的 **ECALL 表**（按 ECALL 号索引的函数指针数组）。

流程：定位 ECALL 表 → 认出远程认证协议 → 用 Python 重实现 → 密钥派生是 **P-256 ECDH 后接 CMAC-AES-128** → 用派生出的 SK 解 AES-128-GCM 的 flag 密文。

```python
shared = priv.exchange(ec.ECDH(), server_pub)
c = cmac.CMAC(ciphers.algorithms.AES(b'\x00'*16))
c.update(shared[:16]); sk = c.finalize()
```

给定 enclave measurement，派生是确定性的——所以重实现能算出同一个 SK，不需要真的进 enclave。

## 16. 纪律

- **先答「flag 在哪一层算出来」**。APK 的答案常在 native、Electron 的答案常在原生 ELF、游戏的答案常在资源包——逆错层等于白做。
- **能在运行时调就不静态逆**。Frida 一句 `Java.use(...).method()` 顶几小时反编译；npm 包 `require` 进来枚举属性同理。
- **「新建工程直接调 `.so`」是绕过 Java 层校验的通用解**，比逐个 patch 校验点快得多。前提是符号名/包名对得上。
- **游戏与 VM 先搜开发者残留**（`xyzzy`/`debug`/`god`/隐藏 chunk），再逆校验逻辑。
- **编码先猜再算**。看到定长、乱码、字母数字混排，先试 EBCDIC/UTF-16/GBK，别急着当加密处理。
