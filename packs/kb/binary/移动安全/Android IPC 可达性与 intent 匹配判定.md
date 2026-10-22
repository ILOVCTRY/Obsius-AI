---
title: Android IPC 可达性与 intent 匹配判定
phase: triage
vuln_class: [android, ipc, manifest, intent, permission]
summary: 跨应用 IPC 题分两关判：先判「能不能到达组件」（exported 默认值 × targetSdk、自定义权限由谁 DEFINES × protectionLevel），再判「到了能不能被接收」（intent-filter × intentMatchingFlags）。两关都过才算载荷有效，最终以接收器账本 state 为真值。
---

# Android IPC 可达性与 intent 匹配判定

## 判据速查

| 现象 | 首判 | 去哪节 |
|---|---|---|
| 组件写了 `exported=true` | 导出 ≠ 可达，看权限保护级别 | §2 |
| 组件没写 `android:exported`（属性缺失） | 看 targetSdk 与有无 intent-filter | §3 |
| 组件挂自定义权限 | 看保护级别，更看**谁** DEFINES 它 | §4 |
| 显式 intent（带 component）的 action 与 filter 不一致 | 看是否启用 `enforceIntentFilter` | §5 |
| intent 的 action 为空 / null | 看是否启用 `allowNullAction` | §6 |
| 有接收器内部账本 / verdict 列表 | 账本 state 是唯一真值，verdict 只作旁证 | §7 |
| 载荷是多段 hex/密文需拼接 | 先定接收集合再解，顺序反了会拼进诱饵 | §8 |
| 明文解不出来、只有零星可打印字节 | 先怀疑接收集合选错，再怀疑变换规则 | §10 |

> 关联：组件静态审计、搭桥链与序列化透传见 [[binary/移动安全/Android 应用安全审计清单（MASTG）.md|MASTG 清单]]；
> 分析分层与工具链见 [[binary/移动安全/分诊与分层（Android、APK）.md|分诊与分层]]；
> 黑盒攻击面分类见 [[web/实战打法/移动端（Android、iOS）安全.md|移动端安全]]。

## 1. 适用条件

**判据**：题目给的是「跨应用 IPC 的观测记录」而不是可执行的 APK——manifest 关键摘要（包名、uid、targetSdk、组件、exported、permission、protectionLevel、DEFINES/requests）+ 逐条 IPC 事务（目标组件、action、verdict）+ 接收器内部账本。题目要求回答「哪条桥真正可达」和「哪些载荷真被接收」。

**动作**：按「可达 → 匹配 → 接收」三段推进，段与段之间不要跳。每一段都要能从原文某一行的字面字段推出结论，推不出就是读漏了字段。

**不成立**：手上有 APK 可跑 → 直接动态验证（`adb shell am start -n` / `am broadcast`）比读摘要更硬，转 MASTG 清单的动态检查节。给的是完整源码 → 直接读代码逻辑，不需要本篇的推演。

## 2. exported=true 只是第一关

**判据**：组件标了 `exported=true`，同时挂 `android:permission`。

**动作**：读出该 permission 的 `protectionLevel` 与**定义者包名**；`signature` / `signatureOrSystem` 才是真保护，`normal` / `dangerous` 任何应用在自己的 manifest 里声明即获得。再看发送方是否请求了该权限、是否已授予。

**不成立**：权限是 `signature` 级且摘要明确写出双方签名密钥不同 → 该桥断。此时不要在这条路上继续调 payload，转 §3 找别的组件。

## 3. exported 属性缺失时的默认值

**判据**：manifest 摘要出现 `android:exported attribute ABSENT` 一类字样。

**动作**：读该包的 `targetSdk`。`< 31` 时，**带 intent-filter 的组件默认导出**、无 filter 的默认不导出；`>= 31` 时一律默认不导出且必须显式声明（缺属性在构建期即报错）。再确认该组件是否真带 filter。

**不成立**：`targetSdk >= 31` 且属性缺失 → 该组件对外不可达，换组件；`targetSdk < 31` 但组件没有 intent-filter → 默认不导出，同样不可达。

## 4. 自定义权限：谁定义比什么级别更致命

**判据**：`DEFINES permission <X> protectionLevel=...` 出现在**发送方自己所在的包**里，而接收方组件把 `<X>` 写成自己的 `android:permission`。

**动作**：画一张三列表——「谁定义 / 谁请求 / 谁授予」。若 `<X>` 由发送方包定义且为 `normal`，发送方安装时即自动获得，接收方这一关等于**零保护**，可达性完全由 §3 的导出语义决定。若 `<X>` 由接收方（或系统）定义，再回到 §2 判级别。

**不成立**：`<X>` 由接收方定义且为 `signature`，同时摘要写明双方签名密钥不同 → 发送方拿不到，此路断。

## 5. 显式 intent 仍可能被 intent-filter 拦

**判据**：帧/日志里出现 `-> exp <pkg>/<component>`（显式 component），但 action 是具体字符串且与组件 filter 里的 action 不同；组件标了 `enforceIntentFilter`。

**动作**：把 filter 的 action 集合与 intent 的 action 逐一比对。启用 `enforceIntentFilter` 时，即使显式指定了 component，action 也必须能解析到 filter，否则匹配失败、载荷不入账。

**不成立**：组件没有该 flag（默认不强制）→ 显式 intent 绕过 filter 直接投递，action 不一致不构成丢弃理由。

## 6. action 为空/缺失的归宿

**判据**：`action=(null)`、action 字段为空或缺省。

**动作**：查组件是否标 `allowNullAction`。有则该空 action 视为匹配通过，进入下一关（权限校验）；无则被 filter 拦下。

**不成立**：空 action 被放行但发送方未持接收方所需权限 → 结局仍是 DENIED。**别把「匹配通过」当成「被接收」**，两关是串联的。

## 7. 账本 state 与 verdict 的分工

**判据**：同时存在逐帧 verdict（DROP / DELIVER / DENIED）与接收器内部账本（`state=ACCEPTED|NOT_ACCEPTED`）。

**动作**：以**账本 state** 为唯一真值筛载荷。verdict 是链路侧观测，账本是接收侧事实，两者不一致时按账本。注意同形 intent 可能一 ACCEPTED 一 NOT_ACCEPTED——差别常在发送方 uid 的权限，而不在 action 形态；账本还可能是「尝试顺序」而非 seq 序排列，取集合时按 seq 值而非行序。

**不成立**：只有 verdict 没有账本 → verdict 必须与 §2–§6 的推演逐帧自洽；任何一帧推不通，说明 manifest 摘要读漏了字段（缺了某个 DEFINES/requests/targetSdk 行），回去补读而不是猜。

## 8. 先定接收集合，再解载荷

**判据**：载荷被拆成多段（多帧 hex、多块密文），并提示按 seq/序号拼接、尾部带校验值。

**动作**：先用 §2–§7 把「真正被接收」的段收敛成集合，再按 seq 升序拼接、还原。校验值只用来确认拼接的**字节集合与顺序**，不要用它反推密钥或反推哪几段被选中。

**不成立**：解出来的明文不可打印 → 先怀疑接收集合选错（把探针块/自测块拼进来了），再怀疑变换规则；不要靠穷举密钥硬凑可打印串。

## 9. 已验证路径

**判据**：需要一份「从 manifest 摘要 + 帧记录走到载荷集合」的完整推演顺序。

**动作**：按下列顺序逐条落结论，每条都要能引用原文行：

1. 列全部组件，标 `exported` 显式值 / 是否缺属性 / 有无 intent-filter；
2. 对缺属性的组件查其包 `targetSdk`，定默认导出语义；
3. 列全部自定义权限，标「定义包 / 保护级别 / 谁请求了 / 是否已授予」；
4. 对每个发送方 × 每个目标组件，先判可达（§2–§4），再判匹配（§5–§6），两关都过才进候选；
5. 拿接收器账本核对候选集合，账本 state=ACCEPTED 才是最终集合；与逐帧 verdict 交叉验证，不一致处必须在 manifest 里找到解释。

**不成立**：五步做完候选集合为空 → 说明该桥确实不通，题目答案可能是「无有效载荷」或另有组件未在摘要里，回头确认摘要是否被裁剪。

## 10. 常见坑

| 坑 | 症状 | 处置 |
|---|---|---|
| 把 `exported=true` 当可达 | 权限是 signature 级却仍去调 payload | 导出只解决「能寻址」，权限才解决「能不能进」 |
| 忽略 targetSdk 对缺省导出的影响 | 认为属性缺失 = 不导出，直接放弃该组件 | `<31` 且有 filter 时默认导出，这是最常见的可达桥 |
| 只看 protectionLevel 不看 DEFINES 归属 | 以为 `normal` 只是「弱」，其实可能完全由发送方自己定义 | 定义包 == 发送方包 → 该关等于零保护 |
| 显式 intent 就当绕过 filter | action 与 filter 不符却判 DELIVER | 有 `enforceIntentFilter` 时显式也要求 action 解析 |
| 空 action 就当被接收 | 忽略接收方权限校验 | 匹配通过 ≠ 被接收，两关串联 |
| 拿 verdict 当账本 | DELIVER 的帧并非都 ACCEPTED | 账本 state 才是真值，verdict 是旁证 |
| 账本按行序取 seq | 账本按尝试顺序排列，seq 值不连续递增 | 取集合按 seq 数值，不按出现位置 |
| 把诱饵块拼进明文 | 明文解不出/只有零星可打印字节 | 明文可读块、`cafebabe` 类假魔数、全同字节填充块、self-test/probe 命名块都是探针 |
| 从 payload 倒推可达性 | 先穷举密钥凑可打印串，再倒推哪几段有效 | 顺序必须反过来：先判权限与匹配，再动载荷 |
