---

phase: binary
vuln_class: [reverse, macho, firmware, kernel-driver, game-engine, automotive]
title: 平台专项逆向
summary: 平台专项的共同点是「格式先行」——Mach-O/固件镜像/驱动/游戏资源各有一套容器结构，先定位容器再谈逻辑。判据是 `file` 的架构与容器魔数。
---

# 平台专项逆向

硬件与异构架构（GPIO/MMIO/CP2/MBR/ARM64/RISC-V 指令级）见 [[binary/逆向工程/platforms-hardware.md|硬件与异构架构]]；静态工具链与异构格式见 [[binary/逆向工程/tools.md|静态工具]]；Unity IL2CPP 基础见 [[binary/逆向工程/languages.md|语言与运行时]]。

## 0. 判据速查

| 目标 | 第一动作 | 去哪节 |
|---|---|---|
| `file` 报 Mach-O / universal | `lipo -info` 拆架构，`otool -l` 看 load commands | §1 |
| 打完补丁在 macOS 上跑不起来 | 签名失效，要 ad-hoc 重签 | §2 |
| 反汇编里满屏 `objc_msgSend` | Objective-C 派发，去 `__objc_methname` 找选择子 | §3 |
| 符号形如 `s14MyApp...` | Swift 名字改编，`swift demangle` | §4 |
| `.ipa` 且 `cryptid = 1` | App Store 加密，需脱壳设备 | §5 |
| 固件 `.bin` 打不开 | 先 `binwalk` 找嵌套文件系统 | §6 |
| 拿到 SquashFS/JFFS2/UBI 镜像 | 按格式选解包器 | §7 |
| 是 ARM/MIPS 且跑不起来 | qemu-user + sysroot | §8 |
| 有 `xTaskCreate` / `k_thread_create` | RTOS 任务表就是函数表 | §9 |
| `.ko` 可重定位 ELF | 找 `file_operations` 里的 ioctl | §10 |
| `.sys` PE | `DriverEntry` + `IRP_MJ_DEVICE_CONTROL` | §12 |
| 有 `.pak` / `.uasset` / `.umap` | UE 资源容器 | §13 |
| 有 `Assembly-CSharp.dll` | Mono，dnSpy 直接反编译 | §14 |
| 有 `libil2cpp.so` + `global-metadata.dat` | IL2CPP，上 Il2CppDumper | §16 |
| 有 `.luac` / `lua51.dll` | Lua 字节码反编译 | §15 |
| CAN 报文 / UDS 服务 | 先认服务号 | §17 |

---

## 1. Mach-O

```bash
file binary                    # "Mach-O 64-bit executable arm64"
otool -l binary                # load commands（段、dylib、入口）
otool -L binary                # 链接的动态库

lipo -info universal_binary                              # fat 二进制含哪些架构
lipo universal_binary -thin arm64 -output binary_arm64   # 抽单一架构

otool -l binary | grep -A5 "segment\|section"
```

**关键段/节**：
- `__TEXT` 代码、`__DATA` 全局、`__LINKEDIT` 符号
- `__text` 指令、`__cstring` C 字符串、`__objc_methname` 方法名

**关键 load command**：
- `LC_MAIN` → 入口（取代老的 `LC_UNIXTHREAD`）
- `LC_LOAD_DYLIB` → 依赖库
- `LC_CODE_SIGNATURE` → 签名 blob
- `__DATA_CONST.__got` → GOT
- `__DATA.__la_symbol_ptr` → 懒绑定符号指针（相当于 PLT）

---

## 2. 签名与权限

```bash
codesign -dvvv binary
codesign --verify binary
codesign -d --entitlements - binary      # 提取 entitlements（能力权限）
codesign --remove-signature binary       # 去签名（为了打补丁）
codesign -f -s - binary                  # ad-hoc 重签（本地测试）
```

**判据**：打过补丁的二进制在 macOS 上直接跑会因签名失效被拒；`-s -` 的 ad-hoc 签名足够本地测试。

**关键 entitlements**：`com.apple.security.app-sandbox`、`com.apple.security.network.client`。

---

## 3. Objective-C 运行时

```bash
class-dump binary > classes.h      # 导出 @interface / @protocol / 方法签名
```

```text
# 唯一派发机制：objc_msgSend(receiver, selector, ...)
# RDI = self（接收者），RSI = selector（方法名字符串）

objc_msgSend(obj, "checkPassword:", input)
# 选择子字符串在 __objc_methname 节
# 交叉引用选择子就能找到实现
```

lldb 里直接跑 ObjC 表达式：
```text
(lldb) expression -l objc -O -- [NSClassFromString(@"ClassName") new]
```

**反篡改判据**：出现 `method_exchangeImplementations` / `class_replaceMethod` 就是方法交换（swizzling）。

**class-dump 替代**：`dsdump`（更快，支持 Swift）、`otool -oV`（导 ObjC 段）、Ghidra 里开 "Objective-C" 分析器。

---

## 4. Swift

```bash
strings binary | grep "swift"
otool -l binary | grep "swift"        # __swift5_* 节

swift demangle 's14MyApp0A8ClassC10checkInput6resultSbSS_tF'
# → MyApp.MyAppClass.checkInput(result: String) -> Bool
```

**运行时函数判据**：
- `swift_allocObject` → 堆分配
- `swift_release` → 引用计数减
- `swift_bridgeObjectRetain` → ObjC ↔ Swift 桥接保留
- `swift_once` → 懒初始化（相当于 `dispatch_once`）

**字符串布局**：
- 小字符串（≤15 字节）内联在 16 字节缓冲里，带 tagged pointer
- 大字符串堆分配：指针 + 长度 + flags

**类型操作**：值见证表（VWT）做类型操作，协议见证表（PWT）做动态派发（相当于 vtable）。`Array<T>` 指向 `ContiguousArrayStorage`（头 + 元素），`Dictionary<K,V>` 是开放寻址哈希表。

**Ghidra**：开 "Swift" 语言模块；`__swift5_types`/`__swift5_proto` 里的类型描述符能被解析。

---

## 5. iOS 应用

```bash
unzip app.ipa -d extracted/
ls extracted/Payload/*.app/

otool -l extracted/Payload/*.app/binary | grep -A4 "LC_ENCRYPTION_INFO"
# cryptid = 1 加密，0 已解密

frida-ios-dump -H jailbroken_ip -p 22 "App Name"   # 需越狱设备
class-dump decrypted_binary > headers.h
```

**越狱检测的五类判据**：Cydia/Sileo 存在、`/private/var/lib/apt` 存在、`fork()` 成功（沙箱内不允许）、`/etc/apt` 与 `/bin/sh` 可写打开、substrate/substitute 库存在。

```javascript
var paths = ["/Applications/Cydia.app", "/bin/sh", "/etc/apt",
             "/private/var/lib/apt", "/usr/bin/ssh"];
Interceptor.attach(Module.findExportByName(null, "access"), {
    onEnter(args) { this.path = Memory.readUtf8String(args[0]); },
    onLeave(retval) {
        if (paths.some(p => this.path && this.path.includes(p)))
            retval.replace(-1);       // 伪装成文件不存在
    }
});
```

### dyld

```bash
DYLD_PRINT_LIBRARIES=1 ./binary              # 打印加载的 dylib
DYLD_INSERT_LIBRARIES=hook.dylib ./binary    # 注入 dylib（相当于 LD_PRELOAD）
```

**坑**：SIP 会拦掉对系统二进制的注入；加固运行时也会忽略这些变量。

---

## 6. 固件提取

```bash
binwalk firmware.bin            # 识别内嵌文件系统与压缩数据
binwalk -e firmware.bin         # 提取所有识别出的组件
binwalk -Me firmware.bin        # 递归提取（套娃）
binwalk --dd='.*' firmware.bin  # 全量裸提取

hexdump -C firmware.bin | grep "hsqs"    # SquashFS 魔数
hexdump -C firmware.bin | grep "UBI#"    # UBI 魔数
```

**物理提取四路**：

| 接口 | 能给什么 | 工具 | 怎么认 |
|---|---|---|---|
| UART | 串口控制台，常直接给 root shell 或 bootloader | USB-UART 转接板，波特率通常 115200 | 4 个引脚（GND/TX/RX/VCC），万用表找 |
| JTAG | CPU 直接调试：读写 flash、停核、下断 | OpenOCD、J-Link、Bus Pirate | 10/14/20 针排针，JTAGulator 自动识别 |
| SPI Flash | 直接读整片固件 | flashrom、CH341A 编程器 | 8 脚 SOIC（Winbond/Macronix 等） |
| eMMC | 路由器/手机常见 | eMMC 读卡器，或直接焊测试点 | 板上 eMMC 芯片 |

---

## 7. 固件解包

```bash
unsquashfs -d output/ squashfs-root.sqfs      # 路由器最常见
# 自定义压缩时换 -comp xz|lzma|lzo|gzip

jefferson -d output/ jffs2.img
ubireader_extract_images firmware.ubi
ubireader_extract_files ubifs.img
cpio -idv < initramfs.cpio
dtc -I dtb -O dts -o output.dts device_tree.dtb

binwalk -e firmware.bin        # 找 zImage / uImage / vmlinux
# 从压缩内核还原 vmlinux：vmlinux-to-elf
```

**判据**：`unsquashfs` 报未知压缩算法时不要放弃，逐个试 `-comp`；固件厂商经常改压缩器参数。

---

## 8. 架构专项

### ARM

```bash
apt install gcc-arm-linux-gnueabihf gdb-multiarch

qemu-arm -L /usr/arm-linux-gnueabihf/ ./arm_binary
qemu-arm -g 1234 ./arm_binary                     # 起 GDB server
gdb-multiarch -ex 'target remote :1234' ./arm_binary
```

**关键判据**：ARM 指令 4 字节、Thumb 2 字节；**函数指针的 LSB 就是模式位**（0=ARM，1=Thumb）。Ghidra 里右键 → Processor Options 切 ARM/Thumb。

### MIPS

```bash
file binary    # "MIPS32 rel2 (MIPS-II), big-endian" 或 little-endian

qemu-mips  -L /usr/mips-linux-gnu/  ./mips_binary     # 大端
qemu-mipsel -L /usr/mipsel-linux-gnu/ ./mipsel_binary # 小端
```

**三个必记特性**：
- **分支延迟槽**：跳转指令**后面那条**一定执行
- `$gp`（全局指针）用于 PIC，指向 `.got`
- `lui` + `addiu` 成对装 32 位常量（高 16 + 低 16）

### AArch64 / RISC-V

AArch64 调用约定与 gadget 见 [[binary/逆向工程/platforms-hardware.md|硬件与异构架构]]；RISC-V 的 Capstone 反汇编见 [[binary/逆向工程/tools.md|静态工具]]。

---

## 9. RTOS

```text
FreeRTOS：
  - 任务（类似线程）：xTaskCreate → 函数指针 + 栈
  - 字符串线索："IDLE"、"Tmr Svc"、任务名
  - xQueueSend/xQueueReceive → 任务间通信
  - vTaskDelay() 看时序，xSemaphoreTake() 看同步

Zephyr：
  - k_thread_create → 内核线程创建
  - k_msgq_put/k_msgq_get → 消息队列
  - CONFIG_* 符号直接暴露内核配置

裸机（无 OS）：
  - 中断向量表在 0x0 或 0x08000000（STM32）
  - 主循环形态：while(1) { read_input(); process(); output(); }
  - 外设寄存器在内存映射地址（查数据手册）
```

**判据**：`xTaskCreate` 的参数表就是「任务函数 → 栈大小」映射，比追调用图快得多。

---

## 10. Linux 内核模块

```bash
file module.ko                   # "ELF 64-bit LSB relocatable"
modinfo module.ko
nm module.ko | grep -v " U "     # 导出符号
strings module.ko | grep -i "flag\|secret\|ioctl\|device"
```

**找 ioctl 处理器**：`file_operations` 结构体里 `.unlocked_ioctl = my_ioctl_handler`。Ghidra 里找「函数指针结构体」，按位置认字段。

**常见 CTF 骨架**：

```c
alloc_chrdev_region(&dev, 0, 1, "challenge");   // 创建 /dev/challenge
cdev_init(&cdev, &fops);

long my_ioctl(struct file *f, unsigned int cmd, unsigned long arg) {
    switch (cmd) {
        case CUSTOM_CMD_1: break;
        case CUSTOM_CMD_2: break;
    }
}

copy_from_user(kernel_buf, (void __user *)arg, size);   // 用户 → 内核
copy_to_user((void __user *)arg, kernel_buf, size);     // 内核 → 用户
```

**调试**：

```bash
qemu-system-x86_64 -kernel bzImage -initrd initrd.cpio -s -S \
  -append "console=ttyS0 nokaslr" -nographic

gdb vmlinux
(gdb) target remote :1234
(gdb) lx-symbols
(gdb) add-symbol-file module.ko 0x<加载地址>
```

**坑**：`.ko` 是可重定位的，基址随便给；真正地址要 `cat /proc/modules`（需 root）。

---

## 11. eBPF

```bash
bpftool prog list
bpftool prog dump xlated id <N>    # 反汇编（eBPF 字节码）
bpftool prog dump jited id <N>     # JIT 后的机器码

llvm-objdump -d ebpf_prog.o
```

**寄存器约定**：11 个 64 位寄存器（r0-r10）；`r0` 返回值，`r1-r5` 参数，`r10` 帧指针。**每条指令 8 字节**。

**关键 helper**：`bpf_map_lookup_elem`（读 map）、`bpf_map_update_elem`（写 map）、`bpf_probe_read`（读内核内存）、`bpf_trace_printk`（调试输出）。

---

## 12. Windows 驱动

`.sys` 是标准 PE，按普通 PE 载入 IDA/Ghidra。入口 `DriverEntry(PDRIVER_OBJECT, PUNICODE_STRING)`。

**关键 API 判据**：
- `IoCreateDevice` → 创建设备对象
- `IRP_MJ_DEVICE_CONTROL` → ioctl 处理器
- `MmMapIoSpace` → 内存映射 I/O
- `ObReferenceObjectByHandle` → 从句柄拿内核对象
- `ZwCreateFile`/`ZwReadFile` → 内核态文件操作

---

## 13. Unreal Engine

```bash
unrealpak.exe extract GameName.pak -output extracted/
# 或 quickbms + unreal_tournament_4.bms
```

**资源格式**：`.uasset` 序列化 UObject（网格/贴图/蓝图）、`.umap` 关卡、`.ushaderbytecode` 编译后着色器。GUI 工具 FModel。

**蓝图逆向**：蓝图编译成 `.uasset` 里的字节码。用 UAssetGUI/FModel 浏览，逻辑在 Kismet 字节码里（`K2_SetTimer`、`DoOnce`、`Branch`、自定义事件）。**flag 逻辑常在蓝图事件图里，不在 C++**。

**C++ 逆向**：`UObject` 万物基类、`AActor` 世界实体、`UGameInstance` 游戏状态、`APlayerController` 玩家输入。反射系统由 `UCLASS()`/`UPROPERTY()`/`UFUNCTION()` 宏生成元数据，运行时可见；Ghidra 里找 `UClass::StaticClass()` 调用即可定位类型。

**字符串**：`FString`（UTF-16）、`FName`（哈希标识符）、`FText`（本地化）。内存布局 `FString = {TCHAR* Data, int32 ArrayNum, int32 ArrayMax}`。

---

## 14. Unity（Mono）

```bash
# 托管程序集在 Data/Managed/
dnspy Assembly-CSharp.dll      # 反编译 + 调试
ilspy Assembly-CSharp.dll      # 只反编译
```

**常见入口**：`MonoBehaviour.Start()` 初始化、`Update()` 每帧逻辑、`PlayerPrefs.GetString("key")` 存数据、`SceneManager.LoadScene("level")` 场景切换。

**资源提取**：AssetStudio（贴图/模型/音频/脚本）、AssetRipper（全面提取）、UABE（底层编辑）。

**flag 常藏在**：文本资源（`.txt`/`.json`）、TextMesh/UI Text 组件、着色器源码、ScriptableObject、PlayerPrefs 存档。

---

## 15. 反作弊与 Lua 游戏

### 反作弊

| 产品 | 形态 | 判据 |
|---|---|---|
| EasyAntiCheat | 内核驱动 `EasyAntiCheat_EOS.sys` + 用户态模块注入 | 完整性检查游戏内存 |
| BattlEye | `BEService.exe` → 注入 `BEClient.dll` | 加密信道、截屏、进程扫描 |
| VAC | **纯用户态**，无内核驱动 | 模块哈希、内存扫描、服务端检测、延迟封禁 |

**CTF 打法**：先 `strings` + 已加载模块确认是哪家；通常只需绕过某个具体检查而非整个反作弊；改内存里的游戏状态比运行时作弊简单；改存档往往比改内存更简单。

### Lua 游戏

```bash
# 线索：lua51.dll / luajit.dll / 资源里的 .lua
luadec bytecode.luac > decompiled.lua     # Lua 5.1-5.3
unluac bytecode.luac > decompiled.lua     # 替代方案

luajit -bl bytecode.lua                   # LuaJIT 反汇编
python3 ljd bytecode.lua                  # LuaJIT 反编译

strings binary | grep "lua_\|luaL_\|LUA_"
# hook lua_pcall 拦脚本执行
```

---

## 16. IL2CPP

```bash
Il2CppDumper libil2cpp.so global-metadata.dat out/
grep -r "https://" out/
```

**关键**：IL2CPP 看着不可读，但 `global-metadata.dat` 里带着**每个类型名、方法名、字符串字面量**。够还原 CTF 级逻辑，完全不必碰原生反汇编。

Unity IL2CPP 基础见 [[binary/逆向工程/languages.md|语言与运行时]]。

---

## 17. 汽车 / CAN 总线

```bash
sudo ip link set can0 type can bitrate 500000
sudo ip link set up can0

candump can0                    # 实时抓
candump -l can0                 # 落盘
cansniffer can0                 # 只高亮变化的帧
canplayer -I logfile.log can0   # 重放
cansend can0 7DF#0201000000000000   # 单帧（OBD-II 请求）
```

**帧结构**：ID（11 或 29 位）、DLC（数据长度 0-8 字节）、Data（最多 8 字节）。

**UDS 常用服务**：
- `0x27` Security Access（种子-密钥认证）
- `0x2E` Write Data By Identifier
- `0x31` Routine Control

**CTF 套路**：种子-密钥绕过（从 ECU 固件反推密钥派生算法）、CAN 报文重放（抓合法指令重放解锁功能）、经 UDS/KWP2000 从 ECU 抽固件。

---

## 18. 案例：格式层的四个陷阱

### RISC-V + GLIBC 符号版本改写（Pwn2Win 2018）

目标 RISC-V Debian 本地没有。取 Debian 的 libc6 与动态链接器，把二进制要求的符号版本（`GLIBC_2.25` → `GLIBC_2.27`）在十六进制里改掉，用 `qemu-riscv64 -L <sysroot>` 跑。

```bash
ar x libc6_2.27-5_riscv64.deb && tar xf data.tar.xz
sed 's@GLIBC_2.25@GLIBC_2.27@g' -i binary
objdump -p binary          # 记下旧的版本哈希，用 xxd/hexedit 一起改
qemu-riscv64 -L ./sysroot ./binary
```

**关键**：`ld.so` 的符号版本靠符号旁边的**哈希标识**校验，字符串和哈希都要改，才能不重建 libc 就绕过。

### APK 证书 SHA-256 当 AES 密钥（ASIS Finals 2018）

```python
from hashlib import sha256
import base64, zipfile

cert = zipfile.ZipFile('app.apk').read('META-INF/CERT.RSA')
key  = base64.b64encode(sha256(cert).digest())[:16]
```

**判据**：`SHA-256(packageInfo.signatures[0].toByteArray())` 截 16 字节——签名证书就在 APK 里，密钥离线可复原，完全不用逆 native。审计 `getSignature`/`getPackageInfo`/`MessageDigest` 的这类形状。

### Moxie 自定义操作码（SECCON 2018）

`strings` 在 ELF 里翻到帮助文本，直接声明了自定义操作码 `SETRSEED (0x16)`、`GETRAND (0x17)` 与非标准 xorshift32。用 Python 模拟这两个操作码恢复 PRNG 流，再异或密文。

```python
def xorshift32(s):
    s ^= (s << 13) & 0xffffffff
    s ^= (s >> 17)
    s ^= (s << 15) & 0xffffffff     # 注意：不是标准的 (<< 5)
    return s & 0xffffffff
```

**判据**：冷门 ISA 常靠小规模自定义扩展内联实现密码学；作者自己的调试帮助文本经常留在二进制里。

### Unity 运行时补丁（SECCON 2018）

```bash
apktool d game.apk -o game_src
# 用改过的 Assembly-CSharp.dll 替换 game_src/assets/bin/Data/Managed/
apktool b game_src -o patched.apk
jarsigner -keystore debug.keystore -storepass android patched.apk androiddebugkey
adb install -r patched.apk
```

**判据**：Unity 游戏打开 DLL 之后就是纯 C#。被错误角度渲染或被浮层遮住的 flag，删掉那段动画代码就露出来了。

---

## 19. 纪律

- **先定容器再谈逻辑**：Mach-O/固件/驱动/游戏资源各有容器结构，容器没拆开就别读指令。
- **平台题的卡点几乎都是「工具没选对」**，不是「逻辑太复杂」。`.ipa` 加密、`.ko` 重定位、IL2CPP 元数据都是格式问题。
- **补丁之后必重签**：macOS 不重签直接跑不起来，Windows 驱动要处理签名强制。
- **固件先 `binwalk -Me`**，套娃提取完再找 `/etc/init.d`、`/etc/passwd`、web 服务配置。
- **游戏题先找资源容器**：`.pak`/`.uasset`/`Assembly-CSharp.dll`/`global-metadata.dat` 里往往直接有明文线索。
- **报的是格式不是难度**：IL2CPP、eBPF、Moxie 看起来唬人，其实都是「换工具」。
