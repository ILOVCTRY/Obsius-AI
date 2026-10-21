# Android 已解案例（K6 沉淀）

每个子目录一个已解题型：**识别 markers + 解题路径 + 验证向量 + solver + 复用提示**
（五段式）。遇到新 APK 先比对识别特征；命中则复用 solver 并在工作区重新验证，
不从头发推导。解出新题型后按同格式新增条目——K6 约定：`<域>/<子域>/cases/<案例id>/`，
verified 攻击链经人审闸门沉淀（防自投毒）。

| 目录 | 题型 | 识别特征 | 状态 |
|---|---|---|---|
| [[binary/移动安全/cases/godot-sec2026/Godot sec2026（Truck Town、2026 腾讯游戏安全初赛 Android）.md|godot-sec2026/]] | Godot 4.5 APK + UPX 壳 GDExtension + 魔改 ChaCha20 | assets/.godot、libgodot_android.so、加壳小型自定义 .so、*.gdc | 完整案例（五段式） |
| [[binary/移动安全/cases/ali-crackme3/AliCrackme3（com.ctf.crackme3、AliCrackme_3）.md|ali-crackme3/]] | 阿里移动 crackme3 | package `com.ctf.crackme3`、`lib/armeabi/libcrackme.so` | solver 现成、案例待补 |
| [[binary/移动安全/cases/wbox/wbox（libwbox.so native AES 系列）.md|wbox/]] | native AES wbox 系列 | `libwbox.so`、AES-128-ECB + 索引加法变换 | solver 现成、案例待补 |
