---
title: Android 高级逆向参考
summary: native so 分析流程与 IDA JNI 技巧、Frida native hook 与内存 patch、SSL pinning 分框架绕过、加固厂商识别表、React Native/Flutter 逆向
phase: reverse
vuln_class: [android, jni, native, frida]
---

# Android 高级逆向参考（native / 框架 / 加固）

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）。
> 覆盖 Native SO 分析、Frida 高级用法、SSL Pinning 绕过、Root 检测对抗、加固脱壳、
> Flutter/React Native 逆向。与 [native-five-lines.md](native-five-lines.md)（五线纪律）
> 互补：五线讲「按什么线推进」，本篇补「IDA/Frida 具体怎么操作」。

## Native SO 逆向分析流程

```text
1. 从 APK 提取 .so
   unzip app.apk lib/arm64-v8a/*.so -d extracted/

2. 确认架构和基本信息
   file libxxx.so
   rabin2 -I libxxx.so

3. 找 JNI 入口
   - 搜索 JNI_OnLoad（动态注册）
   - 搜索 Java_com_xxx_yyy（静态注册）
   - nm -D libxxx.so | grep -i java

4. IDA/Ghidra 加载分析（技巧见下节）

5. 定位关键逻辑
   - 从 Java 层 native 方法名追踪
   - 从字符串（密钥、URL、错误信息）交叉引用
   - 从 crypto 库函数（AES/MD5/SHA）调用追踪
```

### JNI 函数注册两种形态

```c
// 静态注册：函数名 = Java_包名_类名_方法名
JNIEXPORT jstring JNICALL Java_com_example_app_Security_getSign(
    JNIEnv *env, jobject thiz, jstring input) { ... }

// 动态注册：在 JNI_OnLoad 中调用 RegisterNatives
static JNINativeMethod methods[] = {
    {"getSign", "(Ljava/lang/String;)Ljava/lang/String;", (void*)native_getSign},
};

JNIEXPORT jint JNI_OnLoad(JavaVM *vm, void *reserved) {
    JNIEnv *env;
    vm->GetEnv((void**)&env, JNI_VERSION_1_6);
    jclass clazz = env->FindClass("com/example/app/Security");
    env->RegisterNatives(clazz, methods, sizeof(methods)/sizeof(methods[0]));
    return JNI_VERSION_1_6;
}
```

### IDA 中分析 JNI 的技巧

```text
1. 导入 JNI 类型库：File → Load File → Parse C Header → jni.h
2. 标注第一个参数为 JNIEnv*（右键参数 → Set type）
   → env->FindClass / env->GetMethodID 等调用自动识别
3. 找 RegisterNatives：搜索对 JNIEnv vtable offset 0x35C (ARM64) 的调用
   → 第三个参数是 JNINativeMethod 数组 → 提取所有 native 函数地址
```

## Frida 高级用法

### Hook native 函数 / 内存搜索与 patch

```javascript
// Hook libc 函数（如拦截 root 检查的 open）
Interceptor.attach(Module.findExportByName("libc.so", "open"), {
    onEnter: function(args) {
        this.path = args[0].readUtf8String();
    },
    onLeave: function(retval) {
        if (this.path.includes("su") || this.path.includes("magisk")) {
            console.log("[open] Blocked root check: " + this.path);
            retval.replace(-1);  // 返回失败
        }
    }
});

// Hook 自定义 SO 中的函数（按偏移）
var base = Module.findBaseAddress("libsecurity.so");
Interceptor.attach(base.add(0x1234), {
    onEnter: function(args) { console.log("arg0: " + args[0].readUtf8String()); },
    onLeave: function(retval) { console.log("return: " + retval.readUtf8String()); }
});

// 内存搜索字符串
Memory.scan(Module.findBaseAddress("libtarget.so"), size, "48 65 6C 6C 6F", {
    onMatch: function(address, size) { console.log("Found at: " + address); }
});

// patch 指令为 NOP
var addr = Module.findBaseAddress("libsecurity.so").add(0x5678);
Memory.patchCode(addr, 4, function(code) {
    var writer = new Arm64Writer(code, {pc: addr});
    writer.putNop();
    writer.flush();
});
```

## SSL Pinning 绕过（通用方案 + 分框架）

```javascript
// 通用三步：TrustManager 空实现 → SSLContext 替换 → OkHttp CertificatePinner
Java.perform(function() {
    var TrustManager = Java.registerClass({
        name: 'com.custom.TrustManager',
        implements: [Java.use('javax.net.ssl.X509TrustManager')],
        methods: {
            checkClientTrusted: function(chain, authType) {},
            checkServerTrusted: function(chain, authType) {},
            getAcceptedIssuers: function() { return []; }
        }
    });
    var SSLContext = Java.use('javax.net.ssl.SSLContext');
    var sslContext = SSLContext.getInstance("TLS");
    sslContext.init(null, [TrustManager.$new()], null);

    try {
        var CertificatePinner = Java.use('okhttp3.CertificatePinner');
        CertificatePinner.check.overload('java.lang.String', 'java.util.List').implementation = function() {};
    } catch(e) {}
});
```

| 框架 | 绕过方法 |
|------|---------|
| OkHttp3 | Hook `CertificatePinner.check` 返回空 |
| Retrofit | 同 OkHttp（底层用 OkHttp） |
| Volley | Hook `HurlStack` 的 SSL 工厂 |
| Flutter | Hook `dart:io` 的 `SecurityContext`（需要特殊脚本，见下） |
| React Native | Hook `OkHttpClientProvider` |
| WebView | Hook `WebViewClient.onReceivedSslError` |

### Flutter 专项

```javascript
// Flutter SSL Pinning 绕过（找 ssl_verify_peer_cert 特征码）
var flutter_lib = Module.findBaseAddress("libflutter.so");
var pattern = "FF 03 05 D1 FD 7B 0F A9";  // ARM64 特征
Memory.scan(flutter_lib, Module.findModuleByName("libflutter.so").size, pattern, {
    onMatch: function(address) {
        Interceptor.replace(address, new NativeCallback(function() {
            return 0;  // 返回成功
        }, 'int', []));
    }
});
```

## Root 检测绕过对照

| 检测方式 | 绕过方法 |
|---------|---------|
| 检查 `/system/app/Superuser.apk` | Hook `File.exists()` 返回 false |
| 检查 `su` 命令 | Hook `Runtime.exec()` 拦截 su 调用 |
| 检查 `/proc/self/mounts` | Hook 文件读取，过滤 magisk 相关 |
| SafetyNet/Play Integrity | Magisk Hide / Zygisk + Shamiko |
| 检查 Magisk 包名 | 随机化 Magisk 包名 |
| 检查 `/data/adb/` | Hook `opendir`/`access` |

Java 层通用 Frida 绕过代码见 [frida-cookbook.md](frida-cookbook.md)。

## 加固/壳识别与脱壳

| 加固 | 识别特征 | 脱壳方式 |
|------|---------|---------|
| 360 加固 | `libjiagu.so`、`com.stub.StubApp` | FART / Frida dump dex |
| 腾讯乐固 | `libshell*.so`、`com.tencent.StubShell` | FART / BlackDex |
| 梆梆加固 | `libDexHelper.so`、`com.secneo.apkwrapper` | FART |
| 爱加密 | `libexec.so`、`s.h.e.l.l` | Frida dump |
| 网易易盾 | `libnesec.so` | Frida dump |
| 娜迦 | `libnaga.so` | Frida dump |

通用脱壳方法：

```text
方法 1: FART（ART 环境脱壳）——刷入 FART ROM 或 Frida 版，自动 dump 所有 dex
方法 2: Frida DEX Dump——hook DexFile::OpenMemory dump 内存 dex
        frida -U -f com.target.app -l dex_dump.js
方法 3: BlackDex——免 root，装 APK 直接脱
方法 4: 手动——Frida 枚举 ClassLoader → 取 DexFile 对象 → 读内存保存
```

平台侧 unicorn 脱壳工程见 [unpacking.md](unpacking.md)。

## React Native / Flutter 逆向

```text
React Native:
1. 解压 APK → assets/index.android.bundle（JS 代码）
2. 格式化 JS → 搜索 API 地址、密钥、签名逻辑
3. Hermes 字节码（.hbc）→ 用 hermes-dec 反编译
4. Frida hook Java 层 ReactBridge

Flutter:
1. Flutter 代码编译为 libapp.so（Dart AOT），无法直接反编译回 Dart 源码
2. reFlutter：patch libflutter.so 获取 snapshot
3. Doldrums：解析 Dart snapshot 恢复类/函数信息
4. Frida hook libflutter.so 关键函数
5. 网络分析：Flutter 不走系统代理，需特殊处理 SSL
```

## 工具速查

| 工具 | 用途 | 安装 |
|------|------|------|
| jadx | Java 反编译 | bootstrap 可装 |
| apktool | 解包/重打包 | bootstrap 可装 |
| Frida | 动态 Hook | `pip install frida-tools` |
| Objection | Frida 封装（更易用） | `pip install objection` |
| MobSF | 自动化移动安全分析 | Docker 部署 |
| BlackDex | 免 root 脱壳 | APK 安装 |
| FART | ART 脱壳 | 刷入 ROM 或 Frida 版 |
| hermes-dec | Hermes 字节码反编译 | npm 安装 |
| reFlutter | Flutter 逆向辅助 | pip 安装 |
| Magisk + Shamiko | Root 隐藏 | 刷入 |

## 参考资源

OWASP MASTG: https://mas.owasp.org/ · FridaBypassKit:
https://github.com/okankurtuluss/FridaBypassKit · SSL-bypass:
https://github.com/0xCD4/SSL-bypass · awesome-frida:
https://github.com/dweinstein/awesome-frida · Android Security Awesome:
https://github.com/ashishb/android-security-awesome
