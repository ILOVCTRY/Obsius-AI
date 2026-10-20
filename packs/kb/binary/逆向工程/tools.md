---

phase: binary
vuln_class: [reverse, toolchain, bytecode, wasm, mobile, dotnet]
title: 静态工具链与异构格式
summary: 先按「目标是什么格式」选工具，而不是先开 IDA——字节码/WASM/APK/HAP/.NET/加壳各有专用链，选错工具会白耗一轮。判据是 `file` 的输出与文件头魔数。
---

# 静态工具链与异构格式

动态工具（Frida/angr/lldb/x64dbg）见 [[binary/逆向工程/tools-dynamic.md|动态工具]]；模拟执行与侧信道见 [[binary/逆向工程/tools-emulation.md|模拟执行]]；Unicorn 手工模拟细节见 [[binary/逆向工程/unicorn-emulation.md|Unicorn 模拟]]；分诊总表见 [[binary/逆向工程/index.md|逆向工程总览]]。

## 0. 判据速查

| 目标是什么 | 第一动作 | 去哪节 |
|---|---|---|
| ELF/PE/Mach-O 原生二进制 | GDB + r2 摸一遍，再上 IDA | §1、§2、§3 |
| 只有 `.pyc` / 有 `pyarmor_runtime` | 跳过 16 字节头再 `marshal.load` | §5 |
| `.wasm` | `wasm2c` 转 C 读，别硬看字节码 | §6 |
| `.apk` / `.hap` | apktool + jadx；Flutter 用 blutter | §7 |
| 有 `BSJB` / `#~` 流 / `mscorlib` 字样 | .NET，dnSpy 直接反编译 | §8 |
| `strings` 里有 `UPX!` / `PYZ` / `MEI` | 先脱壳再谈分析 | §9 |
| 文本是 `define ... @llvm.` | LLVM IR，`llc` 转汇编 | §10 |
| `file` 报 RISC-V / MIPS 且跑不起来 | Capstone 反汇编或 qemu 用户态 | §10 |
| `.gb` / `.gbc` | `mgbdis`，不能用 objdump | §12 |
| 校验是自造位运算哈希 | 上 SMT（boolector 优先于 Z3） | §13 |

---

## 1. GDB

### 基本

```bash
gdb ./binary
run                      # 跑
start                    # 跑到 main（PIE 必须用它解析基址）
b *0x401234              # 绝对地址断点
b *main+0x100            # 相对断点
c / si / ni              # 继续 / 步指令 / 步指令(跳过 call)
x/s $rsi                 # 看字符串
x/20x $rsp               # 看栈
info registers
set $eax=0               # 改寄存器
```

### PIE 二进制

```bash
gdb ./binary
start                    # 关键：先 start，PIE 基址才被解析
b *main+0xca             # 之后才能用相对偏移
b *main+0x198
run
```

**坑**：PIE 下不 `start` 就直接 `b *0x401234`，断点会落在未映射页上，程序跑飞且不报错。

### 一行式自动化

```bash
gdb -ex 'start' -ex 'b *main+0x198' -ex 'run' ./binary
```

### 内存查看的粒度

```bash
x/s $rsi                 # 当字符串
x/38c $rsi               # 逐字符看 38 个（比对逐字节校验最有用）
x/20x $rsp               # 栈上 20 个字
x/10i $rip               # 从 RIP 起 10 条指令
```

---

## 2. Radare2

### 会话

```bash
r2 -d ./binary           # 调试模式
aaa                      # 全量分析
afl                      # 列函数
pdf @ main               # 反汇编 main
db 0x401234 / dc         # 断点 / 继续
ood                      # 重启调试（脚本化爆破的核心）
dr / dr eax=0            # 读寄存器 / 改寄存器
```

### r2pipe 爆破

```python
import r2pipe
r2 = r2pipe.open('./binary', flags=['-d'])
r2.cmd('aaa')
r2.cmd('db 0x401234')

for char in range(256):
    r2.cmd('ood')                 # 每轮必须重启，否则状态残留
    r2.cmd(f'dr eax={char}')
    if 'correct' in r2.cmd('dc'):
        print(f"Found: {chr(char)}")
```

**判据**：`ood` 是不可省的——不改内存只改寄存器时，第二轮起会读到上一轮的副作用。

---

## 3. Ghidra

### Headless

```bash
analyzeHeadless /path/to/project tmp -import binary -postScript script.py
```

### EmulatorHelper 解密

```java
EmulatorHelper emu = new EmulatorHelper(currentProgram);
emu.writeRegister("RSP", 0x2fff0000);   // 必须自己铺栈，Ghidra 不初始化
emu.writeRegister("RBP", 0x2fff0000);
emu.writeMemory(dataAddress, encryptedBytes);
emu.writeRegister("RDI", arg1);          // 按调用约定摆参数

emu.setBreakpoint(returnAddress);        // 在返回地址下断，run 到此处即完成
emu.run(functionEntryAddress);

byte[] decrypted = emu.readMemory(outputAddress, length);
```

**坑**：`RSP`/`RBP` 不设就会在第一条 `push` 上崩；`run` 必须有终止断点，否则跑满地址空间。

### MCP 命令面

- 侦察：`list_functions`、`list_imports`、`list_strings`
- 分析：`decompile_function`、`get_xrefs_to`
- 标注：`rename_function`、`rename_variable`

---

## 4. Unicorn

### 基础

```python
from unicorn import *
from unicorn.x86_const import *

mu = Uc(UC_ARCH_X86, UC_MODE_64)
mu.mem_map(0x400000, 0x10000)            # 代码段
mu.mem_write(0x400000, code_bytes)
mu.mem_map(0x7fff0000, 0x10000)          # 栈（必须显式映射）
mu.reg_write(UC_X86_REG_RSP, 0x7fff0000 + 0xff00)
mu.emu_start(start_addr, end_addr)
```

### 64 → 32 位混合模式切换

判据：64 位 stub 用 `retf` / `retfq` 跳进 32 位代码。

- `retf` 弹 **6 字节**：4 字节 EIP + 2 字节 CS
- `retfq` 弹 **16 字节**：8 字节 RIP + 8 字节 CS

Unicorn 不能原地切模式，必须**新建一个 32 位 `Uc` 实例**，手工搬状态：

```python
uc32 = Uc(UC_ARCH_X86, UC_MODE_32)

reg_map = {
    UC_X86_REG_EAX: UC_X86_REG_RAX, UC_X86_REG_EBX: UC_X86_REG_RBX,
    UC_X86_REG_ECX: UC_X86_REG_RCX, UC_X86_REG_EDX: UC_X86_REG_RDX,
    UC_X86_REG_ESI: UC_X86_REG_RSI, UC_X86_REG_EDI: UC_X86_REG_RDI,
    UC_X86_REG_EBP: UC_X86_REG_RBP,
}
for e, r in reg_map.items():
    uc32.reg_write(e, mu.reg_read(r) & 0xffffffff)

uc32.reg_write(UC_X86_REG_EFLAGS, mu.reg_read(UC_X86_REG_RFLAGS) & 0xffffffff)

# SSE 密集的块必须连 XMM 一起搬，否则浮点/向量结果全错
for xr in [UC_X86_REG_XMM0, UC_X86_REG_XMM1, UC_X86_REG_XMM2, UC_X86_REG_XMM3,
           UC_X86_REG_XMM4, UC_X86_REG_XMM5, UC_X86_REG_XMM6, UC_X86_REG_XMM7]:
    uc32.reg_write(xr, mu.reg_read(xr))
# 32 位跑完再反向搬回 64 位
```

**坑**：`UC_IGNORE_REG_BREAK=1` 只是静音未实现寄存器的警告，寄存器本身仍是 0——搬漏一个就会出现「结果差一位」的诡异现象。

### Hook：定点取值 vs 追踪变化

```python
def hook_code(uc, address, size, user_data):
    if address == TARGET_ADDR:                     # 定点
        print(f"0x{address:x}: rsi=0x{uc.reg_read(UC_X86_REG_RSI):016x}")

prev = [None]
def hook_diff(uc, address, size, user_data):       # 只在变化时打印
    v = uc.reg_read(UC_X86_REG_RSI)
    if v != prev[0]:
        print(f"0x{address:x}: RSI -> 0x{v:016x}")
        prev[0] = v

mu.hook_add(UC_HOOK_CODE, hook_code)
```

**判据**：逐字节校验的循环里，寄存器在每轮末尾才变一次——用 `hook_diff` 能把上万行 trace 压到十几行。

---

## 5. Python 字节码

### 反汇编

```python
import marshal, dis

with open('file.pyc', 'rb') as f:
    f.read(16)              # 头部长度随版本变（3.7+ 是 16 字节）
    code = marshal.load(f)
    dis.dis(code)
```

**坑**：头部长度不固定（3.3-3.6 是 12 字节，3.7+ 是 16 字节）。读错会在 `marshal.load` 上抛 `bad marshal data`——用 `importlib.util.MAGIC_NUMBER` 或按前 4 字节魔数判版本。

### 提取常量

```python
for ins in dis.get_instructions(code):
    if ins.opname == 'LOAD_CONST':
        print(ins.argval)
```

**判据**：校验逻辑常被拆成逐字符 `LOAD_CONST` + 比较；常量表里往往直接躺着答案或 XOR 密钥。

### Pyarmor 静态脱壳（1shot）

仓库：`https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot`

```bash
python /path/to/oneshot/shot.py /path/to/scripts                    # 递归处理
python /path/to/oneshot/shot.py /path/to/scripts -r /path/to/pyarmor_runtime.so
python /path/to/oneshot/shot.py /path/to/scripts -o /path/to/output
```

- 运行前 `oneshot/pyarmor-1shot` 必须已存在（否则脚本直接退）
- 只支持 **Pyarmor 8.x-9.x**（头部 `PY` + 六位数字）
- **Pyarmor 7 及更早**（头部 `PYARMOR`）不在范围内，别浪费轮次
- 反汇编结果可靠；反编译源码是实验性的，**以反汇编为准**

---

## 6. WASM

```bash
wasm2c checker.wasm -o checker.c
gcc -O3 checker.c wasm-rt-impl.c -o checker
```

读 C 比读 `.wasm` 字节码快一个数量级。

**常见符号**：
- `w2c_memory` — 线性内存数组，所有数据读写都走它
- `wasm_rt_trap(N)` — 运行时陷阱，`N` 对应具体错误（越界/OOB/未定义）
- 导出函数名往往直接暴露语义：`flagChecker`、`validate`

**判据**：WASM 里没有「寄存器」，只有线性内存 + 局部变量；看到大段 `i32.load`/`i32.store` 就是数组访问，先定位数组基址再谈逻辑。

---

## 7. Android APK / HarmonyOS HAP

### APK 提取

```bash
apktool d app.apk -o decoded/    # 首选：能正确解出二进制 XML
jadx app.apk                     # 直接反编译成 Java
unzip app.apk -d extracted/      # 最粗暴，只适合拿 assets
```

**关键位置**：
- `res/values/strings.xml` — 字符串资源（flag 常在此）
- `AndroidManifest.xml` — 组件、权限、`android:exported`
- `classes.dex` — Dalvik 字节码
- `assets/`、`res/raw/` — 原始资源，`unzip` 拿不到时用 `apktool`

```bash
grep -r "flag\|CTF" decoded/
strings decoded/classes*.dex | grep -i flag
```

### Flutter APK

```bash
python3 blutter.py path/to/app/lib/arm64-v8a out_dir
```

**判据**：Flutter 的 Dart 代码不在 `classes.dex` 里，`jadx` 只能看到壳。必须走 `lib/arm64-v8a/libapp.so` + blutter。

### HarmonyOS HAP / ABC

仓库：`https://github.com/ohos-decompiler/abc-decompiler`

```bash
unzip app.hap -d hap_extracted/          # 先解出 .abc
```

**启动方式必须是 CLI 入口，不能用 `java -jar`**（后者会起 GUI）：

```bash
java -cp "./jadx-dev-all.jar" jadx.cli.JadxCLI -d "out" ".abc"
java -cp "./jadx-dev-all.jar" jadx.cli.JadxCLI -m simple --log-level ERROR -d "out_abc_simple" ".abc"
```

- 先用 `-m simple --log-level ERROR`
- `auto` 模式失败时，退回 `-m simple` 重试
- **报错不等于全失败**——去 `out_xxx/sources/` 看有没有产出
- 每轮换新的输出目录，旧产物会混淆判断

---

## 8. .NET

### 工具

- **dnSpy** — 反编译 + 调试一体，首选
- **ILSpy** — 纯反编译，稳
- **dotPeek** — JetBrains，IL 视图好用

### NativeAOT

- 找 `System.Private.CoreLib` 字符串——AOT 后仍保留
- 类型元数据还在但结构被重排，不能按托管布局读
- 字符串是 **长度前缀的 UTF-16**，不是 C 字符串，`strings` 抓不全

### 两段式 XOR + AES-CBC（Codegate 2013 模式）

**模式**：.NET 程序存一个加密字节数组，先 XOR 再 AES-256-CBC。同一密钥值同时当 Key 和 IV。

1. dnSpy/ILSpy 里取硬编码字节数组与密钥字符串
2. 逐字节 XOR（可能多趟，如 `0x25` 再 `0x58` → 等价单次 `0x7D`）
3. Base64 解码
4. `RijndaelManaged` 的 AES-256-CBC 解密，Key == IV

```python
from Crypto.Cipher import AES
from base64 import b64decode

data = bytearray(encrypted_bytes)
for i in range(len(data)):
    data[i] ^= 0x7D                  # 0x25 ^ 0x58 合并

ct = b64decode(bytes(data))
pt = AES.new(key, AES.MODE_CBC, iv=key).decrypt(ct)
```

**判据**：`RijndaelManaged` 的 Key 与 IV 用同一个变量是这类题的签名；见 `Rijndael` 就先试 Key == IV。

---

## 9. 加壳

### UPX

```bash
strings binary | grep UPX        # 先确认
upx -d packed -o unpacked
```

**坑**：改过 UPX 头的样本 `upx -d` 会报 `CantUnpackException`，得手工修 `p_filesz`/`p_memsz` 或用 `upx -d --force`。

### 自定义壳

1. 在脱壳 stub 之后下断点（找 `jmp`/`call` 到高熵区的第一条指令）
2. dump 内存（不是 dump 文件——原文件里还没有解压后的代码）
3. 修 PE/ELF 头（`OEP`、节表、`p_filesz`）

**判据**：入口点节的熵接近 8.0 且导入表极稀疏 = 加壳；运行时在 `VirtualAlloc` 返回的 `RWX` 页上执行 = 内存马。

### PyInstaller

```bash
python pyinstxtractor.py binary.exe
# 产物在 binary.exe_extracted/
```

**判据**：`MEI` 魔数 + `PYZ-00.pyz`；解出的 `.pyc` 还要按 §5 去掉头部再反编译。

---

## 10. LLVM IR 与 RISC-V

### LLVM IR → 汇编

```bash
llc task.ll --x86-asm-syntax=intel
gcc -c task.s -o file.o
```

**判据**：IR 里 `getelementptr` 是取址、`icmp` 是比较、`shl`/`lshr` 是移位——照这些还原逻辑比读汇编快，因为 IR 保留类型信息。

### RISC-V（EHAX 2026 / iguessbro）

静态链接、stripped、x86 上跑不起来。

```python
from capstone import *

with open('binary', 'rb') as f:
    code = f.read()

md = Cs(CS_ARCH_RISCV, CS_MODE_RISCVC | CS_MODE_RISCV64)   # 压缩指令必须开 RISCVC
md.detail = True

TEXT_OFFSET = 0x10000        # 静态 RISC-V 常见；准确值查 ELF 头 e_entry
for insn in md.disasm(code[TEXT_OFFSET:], TEXT_OFFSET):
    print(f"0x{insn.address:x}:\t{insn.mnemonic}\t{insn.op_str}")
```

**与 x86 的关键差异**：
- **没有 flags 寄存器**——比较与分支在同一条指令里完成（`beq`/`bne`/`blt`），不会出现 `cmp` + `jcc` 两段
- 参数在 `a0`-`a7`，返回值在 `a0`，callee-saved 是 `s0`-`s11`
- 压缩指令 2 字节、标准指令 4 字节**混排**，不开 `CS_MODE_RISCVC` 会整体错位

**常见反 RE 手法**：
- 假 flag 当字符串常量（`"n0t_th3_r34l"` 之类），别见到就交
- `rdtime` 读计时器做反爆破
- 递增密钥 XOR：`decrypted[i] = enc[i] ^ (key & 0xFF) ^ 0xA5; key += 7`

**模拟**：`qemu-riscv64 -L /usr/riscv64-linux-gnu/ ./binary`（需要交叉工具链 sysroot）

---

## 11. Binary Ninja 与 dogbolt.org

### Binary Ninja

反编译输出 HLIL（High-Level IL），另有伪 C / 伪 Rust / 伪 Python。

```bash
binaryninja binary
```

```python
import binaryninja
bv = binaryninja.open_view("binary")
for func in bv.functions:
    print(func.name, hex(func.start))
    print(func.hlil)
```

优于 Ghidra 的三点：启动快、IL 表示更干净、Python API 更适合脚本化。免费版在 `https://binary.ninja/free/`（云端，功能受限）。

### dogbolt.org

同一个二进制并行跑多个反编译器，结果并排展示。支持 Hex-Rays、Ghidra、Binary Ninja、angr、RetDec、Snowman、dewolf、Reko、Relyze。

**何时用**：
- 某个反编译器输出读不懂，横向对比
- 某构造被一个反编译器处理错，另一个可能对
- 本地不想装全套工具，快速分诊
- 交叉验证反编译器是否有 bug

```bash
curl -F "file=@binary" https://dogbolt.org/api/binaries/
```

**判据**：不同反编译器各有所长；一份输出不可读时，另一份常直接给出清晰伪代码。

---

## 12. Game Boy ROM / LR35902（SM83）

Game Boy ROM 使用 LR35902/SM83 指令集，不能用普通 `objdump` 或 Capstone 的 x86/ARM 模式可靠反汇编。项目 Docker 镜像 `cyberstrike/pentest-box:0.1` 已内置 `mgbdis`，这是专用的 Game Boy ROM 反汇编器，输出 RGBDS 兼容汇编，并支持多 bank 与 `.sym` 文件。

```bash
# 宿主目录中的 ROM 挂载到容器工作目录
docker run --rm -it -v "${PWD}:/workspace/scratch" -w /workspace/scratch cyberstrike/pentest-box:0.1 bash

mgbdis game.gb
# 输出：disassembly/
```

`.sym` 文件可用 `.code`、`.data`、`.text`、`.padding`、`.image` 标记代码、数据、文本、填充和图像块；代码与数据交错时应先用 PyBoy/模拟器运行轨迹确定代码区，避免线性扫描造成指令错位。PyBoy 用于执行和内存/画面观测，使用公开接口或兼容层，不要访问不存在的 `p.cpu` API。镜像内脚本使用 `mgbdis.py`，不会再以 `dis.py` 遮蔽 Python 标准库。

---

## 13. boolector SMT2：自造哈希求逆

**判据**：校验由纯位运算（`xor`/`rol`/`add`/`mul`）构成，没有分支依赖——直接翻译成 SMT2，断言输出，解输入。

```smt
(set-logic QF_BV)
(declare-fun input () (_ BitVec 64))
(assert (bvuge input #x0000000020202020))   ; 可打印下界
(assert (bvule input #x000000007e7e7e7e))   ; 可打印上界

(define-fun hash ((x (_ BitVec 64))) (_ BitVec 64) ...)   ; 逐块 lift 成 bvxor/bvrol/bvadd
(assert (= (hash input) #xdeadbeefcafef00d))
(check-sat) (get-model)
```

```bash
boolector -m --output-format=smt2 hash.smt2
```

**关键**：Z3 是默认选择，但位级哈希题上 boolector 常快 **10-100 倍**。做法是从 IDA/r2 把每个基本块 lift 成 `bvxor`/`bvrol`/`bvadd` 链，让求解器挑原像。

**坑**：不加可打印字符的上下界约束，求解器会返回一堆不可打印字节，白解一轮。

---

## 14. 常用命令

```bash
file binary                       # 格式 + 架构 + 是否 stripped
checksec --file=binary            # 保护（PIE/NX/Canary/RELRO）
rabin2 -I binary                  # r2 版 file

strings binary | grep -iE "flag|secret"
rabin2 -z binary                  # 字符串带地址

readelf -S binary                 # 节表（算文件偏移→虚拟地址必用）
objdump -h binary

nm binary / readelf -s binary     # 符号

objdump -d binary
objdump -M intel -d binary
```

**判据**：`readelf -S` 是「文件偏移 ↔ 虚拟地址」换算的唯一可靠来源，coredump 定位、`seek` 偏移计算都要它。

---

## 15. 纪律

- **先 `file` + `strings`，再选工具**。用 IDA 硬啃 `.pyc` 是纯浪费。
- **优先平台工具而不是手搓命令**：`decompile` / `list_symbols` / `strings_search` / `func_xrefs` 返回过滤后的切片，伪代码一屏顶百行汇编。
- **静态看不懂就换动态**：静态工具只解决「逻辑是什么」，不解决「实际跑了什么」。
- **脱壳先于分析**：壳里没有真代码，dump 内存而不是 dump 文件。
- **报的是格式不是难度**：`.wasm`/`.pyc`/`.apk` 看起来吓人，其实是「换工具」而不是「换思路」。
- **异构架构先确认指令集**：RISC-V 不开 `CS_MODE_RISCVC` 会整体错位；MIPS 有大小端变体；ARM 有 Thumb 模式。

---

## 16. Windows 宿主环境注记（cyberstrike-pro 平台追加）

> 本节为平台运行环境注记（中文），上方英文快照原文不动。

- **优先用平台的 `decompile` / `list_symbols` 工具**（IDA MCP 桥或 headless 导出缓存），
  而不是手动 objdump 全量反汇编——伪代码一屏顶百行汇编，且结果自动落 func_kb 共享。
  平台会在需要时自动拉起无窗口 IDA（idat）并启动 MCP，分析完空闲自动关闭，无需人工干预。
- **字符串/调用关系检索用平台工具（2026-09-27 新增）**：`strings_search(binary, pattern)`
  查字符串表（带地址+引用函数，找提示/密钥/flag 线索第一步）、`func_xrefs(binary, name|address)`
  查调用关系、`list_symbols` 支持 `name_contains/min_size` 过滤。
  **禁止用 run_cmd 直读 decompiler-cache 的 JSON**——全量文件大，反复读会灌爆上下文；
  上述工具返回的是过滤后的切片。
- unix 文本工具链（grep/sed/objdump）在 **wsl** runtime（`run_cmd wsl`，有进程启动与
  /mnt 盘 IO 开销）；宿主是 Windows，host runtime 为 **PowerShell**（sed/awk 不可用）。
  纯文本读取用平台 `read_file` 工具（带行号、可分段、零启动开销）。
- 大文件读取纪律（先 `grep -n` 定位再分段读、>2000 行先拆函数文件）见
  `skills/二进制逆向手法/SKILL.md`「大文件读取纪律」段。
