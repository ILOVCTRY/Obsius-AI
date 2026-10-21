---
title: Frida 实战 Hook 脚本速查
summary: Android 动态 hook 代码集：Java 方法/构造器、AES/MD5/HMAC 加密捕获、OkHttp/WebView 网络观察、SSL pinning/root/反调试/模拟器绕过、DEX dump 与工具函数
phase: reverse
vuln_class: [android, frida, dynamic-analysis]
---

# Frida 实战 Hook 脚本速查（Android）

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）；精选自 awesome-frida、Frida-Mobile-Scripts、frida-codeshare-scripts。配合技能 [[skills/04-专项/安卓逆向手法/SKILL.md|安卓逆向手法]]；native 层 hook 见 [[binary/移动安全/Android 高级逆向参考（native、框架、加固）.md|Android 进阶]]；脱壳见 [[binary/移动安全/壳与脱壳（packed、unpacking）.md|脱壳]]。

## 0. 判据速查

Hook 之前先定**要拿什么**，再选挂点。挂错点的表现是「脚本跑起来了但什么都没打印」。

| 目标 | 挂哪个点 | 去哪节 |
|---|---|---|
| 明文密码 / 请求体 | `Cipher.doFinal`、`MessageDigest.digest` | §2 |
| 密钥 / IV | `SecretKeySpec.$init`、`IvParameterSpec.$init` | §2 |
| URL 与 header | `okhttp3.RealCall.execute` | §3 |
| 混淆后方法在哪被调用 | `ClassLoader.loadClass` + 方法枚举 | §1.2 |
| 抓不到包（pinning） | `CertificatePinner.check`、`TrustManagerImpl.verifyChain` | §4 |
| 拒绝启动（root 检测） | `File.exists`、`Runtime.exec`、`Build.TAGS` | §5.1 |
| 拒绝启动（模拟器检测） | `Build.FINGERPRINT` 等一串字段 | §5.2 |
| 本地存了什么 | `SharedPreferencesImpl`、`SQLiteDatabase.rawQuery` | §6 |
| 壳把 dex 藏在内存 | `BaseDexClassLoader.pathList` | §7 |

**最高频错误**：以为 hook 没生效，其实是方法名/签名不对。**先枚举再 hook**，不要猜。

## 1. 通用模板

### 1.1 Hook 方法

```javascript
Java.perform(function() {
    var C = Java.use("com.target.ClassName");

    C.methodName.implementation = function() {              // 无参
        var ret = this.methodName();
        console.log("[*] ret=" + ret);
        return ret;
    };

    // 有参：重载必须指定签名，否则报 ambiguous
    C.methodName.overload('java.lang.String', 'int').implementation = function(s, n) {
        console.log("[*] " + s + "," + n);
        return this.methodName(s, n);
    };

    C.$init.overload('java.lang.String').implementation = function(a) {   // 构造器
        console.log("[*] new C(" + a + ")");
        this.$init(a);
    };
});
```

**不写 `overload(...)` 是最常见的失败**。同名多签名时 Frida 直接抛 `has more than one overload`，不会自动挑。

### 1.2 枚举（不知道名字时）

```javascript
Java.perform(function() {
    // 类里的方法及完整签名，直接复制进 overload()
    Java.use("com.target.ClassName").class.getDeclaredMethods()
        .forEach(function(m) { console.log(m.toString()); });

    // 已加载的类名（反查混淆）
    Java.enumerateLoadedClasses({
        onMatch: function(n) { if (n.indexOf("com.target") !== -1) console.log(n); },
        onComplete: function() {}
    });
});
```

## 2. 加密与签名

```javascript
Java.perform(function() {
    var Cipher = Java.use("javax.crypto.Cipher");
    Cipher.doFinal.overload('[B').implementation = function(input) {
        console.log("[doFinal] mode=" + (this.getOpmode ? this.getOpmode() : "?"));
        console.log("  in:  " + hex(input));
        var r = this.doFinal(input);
        console.log("  out: " + hex(r));
        return r;
    };

    // 密钥（只 hook doFinal 拿不到密钥，必须单独挂这里）
    Java.use("javax.crypto.spec.SecretKeySpec").$init
        .overload('[B', 'java.lang.String')
        .implementation = function(k, algo) {
            console.log("[Key] " + algo + " " + hex(k));
            this.$init(k, algo);
        };

    Java.use("javax.crypto.spec.IvParameterSpec").$init
        .overload('[B').implementation = function(iv) {
            console.log("[IV] " + hex(iv));
            this.$init(iv);
        };

    Java.use("java.security.MessageDigest").digest
        .overload('[B').implementation = function(i) {
            var r = this.digest(i);
            console.log("[Digest] " + this.getAlgorithm() + " " + hex(r));
            return r;
        };

    var Mac = Java.use("javax.crypto.Mac");
    Mac.init.overload('java.security.Key').implementation = function(k) {
        console.log("[Mac.init] " + hex(k.getEncoded()));
        this.init(k);
    };
    Mac.doFinal.overload('[B').implementation = function(i) {
        var r = this.doFinal(i);
        console.log("[Mac] " + this.getAlgorithm() + " " + hex(r));
        return r;
    };
});

function hex(b) {
    if (!b) return "null";
    var o = [];
    for (var i = 0; i < b.length; i++) o.push(('0' + (b[i] & 0xFF).toString(16)).slice(-2));
    return o.join('');
}
```

- **`getOpmode()` 1 = 加密、2 = 解密**。分不清方向看这个。
- 应用自己实现加密（不走 JCE）时以上全部不生效，要挂业务方法或 native。
- `hex()` 只处理 byte 数组。先 `console.log(input)` 看类型，对象要另处理。

## 3. 网络请求

```javascript
Java.perform(function() {
    var RealCall = Java.use("okhttp3.RealCall");
    RealCall.execute.implementation = function() {
        var req = this.request();
        console.log("[OkHttp] " + req.method() + " " + req.url().toString());
        var h = req.headers();
        for (var i = 0; i < h.size(); i++) console.log("  " + h.name(i) + ": " + h.value(i));
        var resp = this.execute();
        console.log("[OkHttp] " + resp.code());
        return resp;
    };

    Java.use("java.net.URL").openConnection.overload().implementation = function() {
        console.log("[URL] " + this.toString());
        return this.openConnection();
    };

    var WebView = Java.use("android.webkit.WebView");
    WebView.loadUrl.overload('java.lang.String').implementation = function(u) {
        console.log("[WebView] " + u);
        this.loadUrl(u);
    };
    WebView.evaluateJavascript.implementation = function(s, cb) {
        console.log("[WebView.eval] " + s.substring(0, 200));
        this.evaluateJavascript(s, cb);
    };
});
```

`execute` 只覆盖同步请求。用 `enqueue` 的异步请求要挂 `RealCall.enqueue`，或直接挂 `Interceptor` 链更通用。

## 4. 绕过 SSL Pinning

```javascript
Java.perform(function() {
    // 四段全挂一遍，不要任选其一 —— 应用可能同时用了多套
    try {
        Java.use("okhttp3.CertificatePinner").check
            .overload('java.lang.String', 'java.util.List')
            .implementation = function() { console.log("[*] pinning off (OkHttp3)"); };
    } catch(e) {}

    try {
        Java.use("com.android.org.conscrypt.TrustManagerImpl").verifyChain
            .implementation = function(chain) {
                console.log("[*] pinning off (Conscrypt)");
                return chain;
            };
    } catch(e) {}

    try {
        var X509 = Java.use("javax.net.ssl.X509TrustManager");
        Java.registerClass({
            name: "com.bypass.TM",
            implements: [X509],
            methods: {
                checkClientTrusted: function() {},
                checkServerTrusted: function() {},
                getAcceptedIssuers: function() { return []; }
            }
        });
    } catch(e) {}

    try {
        Java.use("android.security.net.config.NetworkSecurityConfig")
            .isCleartextTrafficPermitted.implementation = function() { return true; };
    } catch(e) {}
});
```

**每段必须包 `try/catch`**：类不存在时 `Java.use` 抛异常，不包会中断整个 `Java.perform`，后面的 hook 全不生效。

**native 层实现（BoringSSL / Cronet）时 Java 层无效** —— 要挂 `SSL_CTX_set_custom_verify`。抓不到包时先判断 pinning 在哪一层。

## 5. 绕过环境检测

### 5.1 Root

```javascript
Java.perform(function() {
    var paths = ["su", "superuser", "magisk", "busybox", "xposed",
                 "/system/xbin/su", "/system/bin/su", "/sbin/su",
                 "/data/local/xbin/su", "/data/local/bin/su"];

    Java.use("java.io.File").exists.implementation = function() {
        var p = this.getAbsolutePath().toLowerCase();
        for (var i = 0; i < paths.length; i++) {
            if (p.indexOf(paths[i].toLowerCase()) !== -1) {
                console.log("[Root] blocked: " + p);
                return false;
            }
        }
        return this.exists();
    };

    Java.use("java.lang.Runtime").exec.overload('java.lang.String').implementation = function(cmd) {
        if (cmd.indexOf("su") !== -1 || cmd.indexOf("which") !== -1) {
            console.log("[Root] blocked exec: " + cmd);
            throw Java.use("java.io.IOException").$new("Permission denied");
        }
        return this.exec(cmd);
    };

    Java.use("android.os.Build").TAGS.value = "release-keys";   // 静态字段用 .value
});
```

### 5.2 模拟器 / 调试器

```javascript
Java.perform(function() {
    Java.use("android.os.Debug").isDebuggerConnected.implementation = function() {
        console.log("[AntiDebug] → false");
        return false;
    };

    // 这一组必须一起改，只改 MODEL 会被一致性检查抓到
    var B = Java.use("android.os.Build");
    B.FINGERPRINT.value  = "google/walleye/walleye:8.1.0/OPM1.171019.011/4448085:user/release-keys";
    B.MODEL.value        = "Pixel 2";
    B.MANUFACTURER.value = "Google";
    B.BRAND.value        = "google";
    B.DEVICE.value       = "walleye";
    B.PRODUCT.value      = "walleye";
    B.HARDWARE.value     = "walleye";

    var T = Java.use("android.telephony.TelephonyManager");
    T.getDeviceId.implementation        = function() { return "352099001761481"; };
    T.getSubscriberId.implementation    = function() { return "310260000000000"; };
    T.getSimSerialNumber.implementation = function() { return "89014103211118510720"; };
});
```

## 6. 数据存储

```javascript
Java.perform(function() {
    Java.use("android.app.SharedPreferencesImpl").getString
        .implementation = function(k, d) {
            var v = this.getString(k, d);
            console.log("[SP.get] " + k + " = " + v);
            return v;
        };

    Java.use("android.app.SharedPreferencesImpl$EditorImpl").putString
        .implementation = function(k, v) {
            console.log("[SP.put] " + k + " = " + v);
            return this.putString(k, v);
        };

    Java.use("android.database.sqlite.SQLiteDatabase").rawQuery
        .implementation = function(sql, args) {
            console.log("[SQL] " + sql);
            if (args) console.log("  args: " + JSON.stringify(args));
            return this.rawQuery(sql, args);
        };
});
```

`$EditorImpl` 的 `$` 是内部类分隔符，写成 `.EditorImpl` 找不到。

## 7. DEX 枚举

```javascript
Java.perform(function() {
    Java.enumerateClassLoaders({
        onMatch: function(loader) {
            try {
                var pl = Java.cast(loader, Java.use("dalvik.system.BaseDexClassLoader")).pathList.value;
                var els = pl.dexElements.value;
                for (var i = 0; i < els.length; i++) {
                    var df = els[i].dexFile.value;
                    if (df) console.log("[DEX] " + df.getName());
                }
            } catch(e) {}
        },
        onComplete: function() {}
    });

    Java.use("java.lang.ClassLoader").loadClass.overload('java.lang.String')
        .implementation = function(n) {
            if (n.indexOf("com.target") !== -1) console.log("[Load] " + n);
            return this.loadClass(n);
        };
});
```

**这只列 dex 名，不 dump 内容**。拿内存里的 dex 要用 `frida-dexdump` 或 hook native 的 `OpenMemory`/`mmap`。360、梆梆这类壳会把路径抹掉，此时只能走 native dump，见 [[binary/移动安全/壳与脱壳（packed、unpacking）.md|脱壳]]。

## 8. 工具函数

```javascript
// 调用栈 —— 找不到方法是谁调的时候用
function printStack() {
    console.log(Java.use("android.util.Log").getStackTraceString(
        Java.use("java.lang.Throwable").$new()));
}

// 对象全部字段
function printFields(obj) {
    obj.class.getDeclaredFields().forEach(function(f) {
        f.setAccessible(true);
        try { console.log("  " + f.getName() + " = " + f.get(obj)); } catch(e) {}
    });
}

// 堆里已存在的实例（对象未创建时找不到，先跑业务流程）
function findInstances(cls) {
    Java.choose(cls, {
        onMatch: function(o) { console.log("[Inst] " + o); printFields(o); },
        onComplete: function() {}
    });
}
```

## 9. 纪律

- **先枚举再 hook**。`getDeclaredMethods()` 花 10 秒，猜错方法名浪费半小时。
- **重载必须写 `overload(...)`**，否则抛 `has more than one overload`。
- **每个绕过块都包 `try/catch`**，一个类不存在会中断整个 `Java.perform`。
- **密钥要单独挂 `SecretKeySpec.$init`**，只 hook `doFinal` 拿不到。
- **模拟器指纹字段要一起改**，改一半会被一致性检查抓到。
- **静态字段用 `.value`，方法用 `.implementation`**。写错不报错，只是不生效。
- **Java 层 hook 对 native pinning 无效**。抓不到包先判断 pinning 在哪一层。
- **`Java.choose` 只找已存在的实例**，找不到时先跑业务流程，别怀疑脚本。
