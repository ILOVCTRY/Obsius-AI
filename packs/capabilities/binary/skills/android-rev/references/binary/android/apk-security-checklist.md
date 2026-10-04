---
title: Android 应用安全审计清单（MASTG）
summary: 基于 OWASP MASTG 的移动应用静态+动态审计清单：manifest 权限、代码危险点、三方库风险、动态检测、网络、存储、认证、泄露面
phase: reverse
vuln_class: [android, security-audit]
---

# Android 应用安全审计清单（MASTG）

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）。
> 基于 OWASP Mobile Application Security Testing Guide (MASTG) 与 MASVS 控制集，
> 按审计阶段组织成 checklist。配合技能 [android-rev](../../capabilities/binary/skills/android-rev/SKILL.md)。

## Manifest 安全检查

```bash
# 反编译拿 AndroidManifest.xml
apktool d app.apk -o app_decoded
cat app_decoded/AndroidManifest.xml
```

| 检查项 | 风险等级 | 说明 |
|--------|---------|------|
| `android:allowBackup="true"` | 中 | 可通过 adb backup 提取私有数据 |
| `android:debuggable="true"` | 高 | 生产包应关闭，可被任意调试 |
| `android:exported="true"`（无权限保护） | 高 | Activity/Service/Receiver 可被任意应用调起 |
| `usesCleartextTraffic="true"` | 中 | 允许 HTTP 明文传输 |
| `android:networkSecurityConfig` 未配置 | 中 | 未强制 HTTPS |
| 过多危险权限（SMS/CONTACTS/LOCATION） | 低-中 | 需业务合理性验证 |
| `android:name` 自定义 Application 被加固替换 | 信息 | 判断加固壳 |

## 代码安全检查（静态）

```bash
# Java/Smali 反编译
jadx -d app_src app.apk
# 关键词扫描
grep -rn "Cipher.getInstance" app_src/          # 弱加密：AES/ECB、DES
grep -rn "SecureRandom" app_src/                # 种子是否固定
grep -rn "TrustAllCerts\|X509TrustManager" app_src/  # 自定义信任所有证书
grep -rn "getDeviceId\|getImei" app_src/        # 隐私合规
grep -rn "Base64.decode\|AES\|DES\|RSA" app_src/ # 内嵌加密
grep -rn "http://" app_src/                     # 明文通信
grep -rn "SQLiteDatabase" app_src/              # SQL 注入拼接
grep -rn "loadUrl\|addJavascriptInterface" app_src/  # WebView 风险
```

| 检查项 | MASTG 参考 |
|--------|-----------|
| 硬编码 API key / 密钥 / 密码 | MASTG-TEST-0x51 |
| 弱加密算法（DES/RC4/MD5/SHA1/AES-ECB） | MASTG-TEST-0x52 |
| 固定 IV / 弱随机数（`new Random()` 用于密码学） | MASTG-TEST-0x53 |
| 自定义 TrustManager 信任所有证书 | MASTG-TEST-0x56 |
| WebView `addJavascriptInterface`（API<17 RCE） | MASTG-TEST-0x58 |
| WebView `setJavaScriptEnabled` + `file://` | MASTG-TEST-0x59 |
| SharedPreferences 存敏感数据明文 | MASTG-TEST-0x60 |
| SQLite 未加密（SQLCipher） | MASTG-TEST-0x61 |
| 日志泄漏（`Log.d` 打印敏感数据） | MASTG-TEST-0x62 |
| 检测已 root 设备但仅提示不阻断 | MASTG-TEST-0x72 |
| SSL pinning 缺失（可被中间人） | MASTG-TEST-0x57 |

## 三方库风险检查

```bash
# 看三方库版本
ls app_decoded/lib/arm64-v8a/           # native 库
grep -rn "version" app_src/*/BuildConfig.java
# 已知漏洞库速查
grep -rn "okhttp3" app_src/ | head -5   # OkHttp < 4.9.2 有 CVE
grep -rn "com.google.android.gms" app_src/ | head -3
```

重点：过时的 OkHttp/Gson/volley/SQLCipher、带已知 CVE 的 native so、
广告 SDK 过多收集数据（KSAd/AdMob 等常带额外上报，逆向时注意排除噪音）。

## 动态安全检查

```bash
# 动态 Frida hook 速查（脚本详见 frida-cookbook.md）
frida -U -f com.target.app -l ssl_bypass.js --no-pause   # SSL 绕过后抓包
frida -U -f com.target.app -l crypto_hook.js --no-pause  # 捕获密钥
```

| 检查项 | 方法 |
|--------|------|
| SSL pinning 是否存在 | Burp + 证书不装 → 若失败说明有 pinning |
| 密钥/IV 是否硬编码 | frida hook SecretKeySpec |
| 本地存储明文敏感数据 | `adb shell run-as com.app cat shared_prefs/*.xml` |
| 日志是否打印敏感信息 | `adb logcat \| grep -i token/password` |
| 截屏保护缺失（FLAG_SECURE） | 手动操作观察最近任务预览 |
| 导出组件未授权访问 | `adb shell am start -n com.app/.ExportedActivity` |
| 内容提供器未授权 | `adb shell content query --uri content://com.app.provider/` |
| 深度链接劫持 | 查看 manifest 的 intent-filter |

## 网络通信安全

| 检查项 | 说明 |
|--------|------|
| 全程 HTTPS | 抓包确认无 HTTP 明文 |
| 证书校验完整（hostname + chain） | 自签名/空 TrustManager 均为风险 |
| 请求签名机制 | 抓包分析 sign 字段来源（逆向确认算法） |
| 时间戳/nonce 防重放 | 重放同一请求观察服务端响应 |
| 响应数据加密 | 抓包看响应是否可读 |

## 数据存储安全

```bash
# 检查内部存储
adb shell run-as com.target.app ls files/ shared_prefs/ databases/
adb shell run-as com.target.app cat shared_prefs/config.xml

# 检查外部存储
adb shell ls /sdcard/Android/data/com.target.app/
```

要点：SharedPreferences 中的 token/密码是否明文；SQLite 是否 SQLCipher；缓存
文件是否含 PII；导出的日志文件位置。

## 认证与会话安全

| 检查项 | 方法 |
|--------|------|
| Token 存储位置（SP/Keystore） | 逆向存储代码 |
| 会话过期机制 | 长时间后重放旧 token |
| 登录接口是否加密传输密钥 | frida hook 登录函数 |
| 生物识别 fallback 是否安全 | hook BiometricPrompt 回调 |
| 本地密码校验逻辑 | 逆向校验函数（常见离线校验可被绕过） |

## 保护/加固完整性评估

```text
1. 加固识别：见 android-advanced.md 加固厂商表
2. 检查项：
   □ DEX 是否加固（多 dex 抽取、字符串加密）
   □ native 层是否有反调试（ptrace / TracerPid）
   □ 是否有完整性校验（CRC32 / MD5 自校验）
   □ 是否有反 Frida（扫描 frida 端口 27042、进程名）
   □ 是否有反模拟器
3. 对抗方法：见 frida-bypass-kit.md
```

## 报告输出清单

```text
□ 应用基本信息（包名/版本/签名/加固）
□ Manifest 高危项列表（引用 MASTG 编号）
□ 代码层风险（附反编译代码片段）
□ 动态测试结果（截图 + 复现步骤）
□ 网络层风险（附抓包样本）
□ 存储层风险（附文件路径与内容样本）
□ 修复建议（按风险等级排序）
```

## 参考资源

OWASP MASTG: https://mas.owasp.org/MASTG/ · OWASP MASVS:
https://mas.owasp.org/MASVS/ · MobSF（自动化）:
https://github.com/MobSF/Mobile-Security-Framework-MobSF
