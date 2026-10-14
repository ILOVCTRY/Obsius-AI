---
title: Frida 实战 Hook 脚本速查
summary: Android 动态 hook 代码集：Java 方法/构造器、AES/MD5/HMAC 加密捕获、OkHttp/WebView 网络观察、SSL pinning/root/反调试/模拟器绕过、DEX dump 与工具函数
phase: reverse
vuln_class: [android, frida, dynamic-analysis]
---

# Frida 实战 Hook 脚本速查（Android）

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）；
> 精选自 awesome-frida、Frida-Mobile-Scripts、frida-codeshare-scripts 等开源项目。
> 按场景分类，直接复制使用。配合技能 [android-rev](../../capabilities/binary/skills/android-rev/SKILL.md)。

## 通用 Hook 模板

### Hook 任意 Java 方法

```javascript
Java.perform(function() {
    var TargetClass = Java.use("com.target.ClassName");

    // Hook 无参方法
    TargetClass.methodName.implementation = function() {
        console.log("[*] methodName called");
        var ret = this.methodName();
        console.log("[*] return: " + ret);
        return ret;
    };

    // Hook 有参方法（重载需 overload 指定签名）
    TargetClass.methodName.overload('java.lang.String', 'int').implementation = function(str, num) {
        console.log("[*] methodName(" + str + ", " + num + ")");
        var ret = this.methodName(str, num);
        console.log("[*] return: " + ret);
        return ret;
    };
});
```

### Hook 构造函数 / 枚举所有方法

```javascript
Java.perform(function() {
    var TargetClass = Java.use("com.target.ClassName");
    TargetClass.$init.overload('java.lang.String').implementation = function(arg) {
        console.log("[*] new ClassName(" + arg + ")");
        this.$init(arg);
    };

    var methods = TargetClass.class.getDeclaredMethods();
    methods.forEach(function(method) { console.log(method.toString()); });
});
```

## 加密 / 签名 Hook（捕获密钥与明文）

```javascript
Java.perform(function() {
    var Cipher = Java.use("javax.crypto.Cipher");

    Cipher.doFinal.overload('[B').implementation = function(input) {
        var mode = this.getOpmode ? this.getOpmode() : "?";
        console.log("[Cipher.doFinal] mode=" + mode);
        console.log("  input: " + bytesToHex(input));
        var result = this.doFinal(input);
        console.log("  output: " + bytesToHex(result));
        return result;
    };

    // 捕获密钥
    var SecretKeySpec = Java.use("javax.crypto.spec.SecretKeySpec");
    SecretKeySpec.$init.overload('[B', 'java.lang.String').implementation = function(key, algo) {
        console.log("[SecretKeySpec] algo=" + algo + " key=" + bytesToHex(key));
        this.$init(key, algo);
    };

    // 捕获 IV
    var IvParameterSpec = Java.use("javax.crypto.spec.IvParameterSpec");
    IvParameterSpec.$init.overload('[B').implementation = function(iv) {
        console.log("[IvParameterSpec] iv=" + bytesToHex(iv));
        this.$init(iv);
    };

    // MD5/SHA
    var MessageDigest = Java.use("java.security.MessageDigest");
    MessageDigest.digest.overload('[B').implementation = function(input) {
        console.log("[MessageDigest.digest] algo=" + this.getAlgorithm());
        var result = this.digest(input);
        console.log("  hash: " + bytesToHex(result));
        return result;
    };

    // HMAC
    var Mac = Java.use("javax.crypto.Mac");
    Mac.doFinal.overload('[B').implementation = function(input) {
        console.log("[Mac.doFinal] algo=" + this.getAlgorithm());
        var result = this.doFinal(input);
        console.log("  mac: " + bytesToHex(result));
        return result;
    };
    Mac.init.overload('java.security.Key').implementation = function(key) {
        console.log("[Mac.init] key=" + bytesToHex(key.getEncoded()));
        this.init(key);
    };
});

function bytesToHex(bytes) {
    if (!bytes) return "null";
    var hex = [];
    for (var i = 0; i < bytes.length; i++) {
        hex.push(('0' + (bytes[i] & 0xFF).toString(16)).slice(-2));
    }
    return hex.join('');
}
```

## 网络请求 Hook

```javascript
Java.perform(function() {
    // OkHttp3 请求/响应
    var RealCall = Java.use("okhttp3.RealCall");
    RealCall.execute.implementation = function() {
        var request = this.request();
        console.log("[OkHttp] " + request.method() + " " + request.url().toString());
        var headers = request.headers();
        for (var i = 0; i < headers.size(); i++) {
            console.log("  " + headers.name(i) + ": " + headers.value(i));
        }
        var response = this.execute();
        console.log("[OkHttp] Response: " + response.code());
        return response;
    };

    // URL 连接
    var URL = Java.use("java.net.URL");
    URL.openConnection.overload().implementation = function() {
        console.log("[URL] " + this.toString());
        return this.openConnection();
    };

    // WebView
    var WebView = Java.use("android.webkit.WebView");
    WebView.loadUrl.overload('java.lang.String').implementation = function(url) {
        console.log("[WebView.loadUrl] " + url);
        this.loadUrl(url);
    };
    WebView.evaluateJavascript.implementation = function(script, callback) {
        console.log("[WebView.evaluateJavascript] " + script.substring(0, 200));
        this.evaluateJavascript(script, callback);
    };
});
```

## 绕过类 Hook

### 通用 SSL Pinning 绕过

```javascript
Java.perform(function() {
    // OkHttp3 CertificatePinner
    try {
        var CertificatePinner = Java.use("okhttp3.CertificatePinner");
        CertificatePinner.check.overload('java.lang.String', 'java.util.List').implementation = function() {
            console.log("[*] SSL Pinning bypassed (OkHttp3)");
        };
    } catch(e) {}

    // TrustManagerImpl
    try {
        var TrustManagerImpl = Java.use("com.android.org.conscrypt.TrustManagerImpl");
        TrustManagerImpl.verifyChain.implementation = function(untrustedChain) {
            console.log("[*] SSL Pinning bypassed (TrustManagerImpl)");
            return untrustedChain;
        };
    } catch(e) {}

    // X509TrustManager 空实现
    try {
        var X509TrustManager = Java.use("javax.net.ssl.X509TrustManager");
        Java.registerClass({
            name: "com.bypass.TrustManager",
            implements: [X509TrustManager],
            methods: {
                checkClientTrusted: function() {},
                checkServerTrusted: function() {},
                getAcceptedIssuers: function() { return []; }
            }
        });
    } catch(e) {}

    // Network Security Config (Android 7+)
    try {
        var NetworkSecurityConfig = Java.use("android.security.net.config.NetworkSecurityConfig");
        NetworkSecurityConfig.isCleartextTrafficPermitted.implementation = function() { return true; };
    } catch(e) {}
});
```

### 通用 Root 检测绕过

```javascript
Java.perform(function() {
    var File = Java.use("java.io.File");
    var rootPaths = ["su", "Superuser", "magisk", "busybox", "xposed",
                     "/system/xbin/su", "/system/bin/su", "/sbin/su",
                     "/data/local/xbin/su", "/data/local/bin/su"];

    File.exists.implementation = function() {
        var path = this.getAbsolutePath();
        for (var i = 0; i < rootPaths.length; i++) {
            if (path.toLowerCase().indexOf(rootPaths[i].toLowerCase()) !== -1) {
                console.log("[Root] Blocked: " + path);
                return false;
            }
        }
        return this.exists();
    };

    var Runtime = Java.use("java.lang.Runtime");
    Runtime.exec.overload('java.lang.String').implementation = function(cmd) {
        if (cmd.indexOf("su") !== -1 || cmd.indexOf("which") !== -1) {
            console.log("[Root] Blocked exec: " + cmd);
            throw Java.use("java.io.IOException").$new("Permission denied");
        }
        return this.exec(cmd);
    };

    var Build = Java.use("android.os.Build");
    Build.TAGS.value = "release-keys";
});
```

### 反调试 / 模拟器检测绕过

```javascript
Java.perform(function() {
    var Debug = Java.use("android.os.Debug");
    Debug.isDebuggerConnected.implementation = function() {
        console.log("[AntiDebug] isDebuggerConnected → false");
        return false;
    };
    // TracerPid（native 层）见 android-advanced 的 native hook 段

    var Build = Java.use("android.os.Build");
    Build.FINGERPRINT.value = "google/walleye/walleye:8.1.0/OPM1.171019.011/4448085:user/release-keys";
    Build.MODEL.value = "Pixel 2";
    Build.MANUFACTURER.value = "Google";
    Build.BRAND.value = "google";
    Build.DEVICE.value = "walleye";
    Build.PRODUCT.value = "walleye";
    Build.HARDWARE.value = "walleye";

    var TelephonyManager = Java.use("android.telephony.TelephonyManager");
    TelephonyManager.getDeviceId.implementation = function() { return "352099001761481"; };
    TelephonyManager.getSubscriberId.implementation = function() { return "310260000000000"; };
    TelephonyManager.getSimSerialNumber.implementation = function() { return "89014103211118510720"; };
});
```

## 数据存储 Hook

```javascript
Java.perform(function() {
    var SharedPreferencesImpl = Java.use("android.app.SharedPreferencesImpl");
    SharedPreferencesImpl.getString.implementation = function(key, defValue) {
        var value = this.getString(key, defValue);
        console.log("[SP.get] " + key + " = " + value);
        return value;
    };
    var Editor = Java.use("android.app.SharedPreferencesImpl$EditorImpl");
    Editor.putString.implementation = function(key, value) {
        console.log("[SP.put] " + key + " = " + value);
        return this.putString(key, value);
    };

    var SQLiteDatabase = Java.use("android.database.sqlite.SQLiteDatabase");
    SQLiteDatabase.rawQuery.implementation = function(sql, args) {
        console.log("[SQL] " + sql);
        if (args) console.log("  args: " + JSON.stringify(args));
        return this.rawQuery(sql, args);
    };
});
```

## 脱壳 DEX Dump

```javascript
Java.perform(function() {
    Java.enumerateClassLoaders({
        onMatch: function(loader) {
            try {
                var pathList = Java.cast(loader, Java.use("dalvik.system.BaseDexClassLoader")).pathList.value;
                var dexElements = pathList.dexElements.value;
                for (var i = 0; i < dexElements.length; i++) {
                    var dexFile = dexElements[i].dexFile.value;
                    if (dexFile) console.log("[DEX] " + dexFile.getName());
                }
            } catch(e) {}
        },
        onComplete: function() {}
    });

    // 监控目标类加载
    var ClassLoader = Java.use("java.lang.ClassLoader");
    ClassLoader.loadClass.overload('java.lang.String').implementation = function(name) {
        if (name.indexOf("com.target") !== -1) console.log("[ClassLoader] " + name);
        return this.loadClass(name);
    };
});
```

## 实用工具函数

```javascript
// 打印调用栈
function printStack() {
    console.log(Java.use("android.util.Log").getStackTraceString(
        Java.use("java.lang.Throwable").$new()));
}

// 打印对象所有字段
function printFields(obj) {
    var fields = obj.class.getDeclaredFields();
    fields.forEach(function(field) {
        field.setAccessible(true);
        try { console.log("  " + field.getName() + " = " + field.get(obj)); } catch(e) {}
    });
}

// 搜索内存中的类实例
function findInstances(className) {
    Java.choose(className, {
        onMatch: function(instance) { console.log("[Instance] " + instance); printFields(instance); },
        onComplete: function() {}
    });
}
```

## 参考资源

| 资源 | 说明 | 链接 |
|------|------|------|
| Frida 官方文档 | API 参考 | https://frida.re/docs/ |
| Frida CodeShare | 社区脚本分享 | https://codeshare.frida.re/ |
| awesome-frida | 资源大全 | https://github.com/dweinstein/awesome-frida |
| frida-codeshare-scripts | 全网最全脚本收集 | https://github.com/zengfr/frida-codeshare-scripts |
| Objection | Frida 封装工具 | https://github.com/sensepost/objection |
| r2frida | radare2 + Frida 集成 | https://github.com/nowsecure/r2frida |
