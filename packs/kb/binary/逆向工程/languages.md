---

phase: binary
vuln_class: [reverse, bytecode, esolang, side-channel, vm]
title: 语言与运行时逆向
summary: 语言题的判据是「先认出这是什么运行时」，再决定是反编译、模拟还是当侧信道打。字节码、esolang、函数式语言各有专用入口，硬读指令一定绕远路。
---

# 语言与运行时逆向

平台/框架专项（Android、Roblox、Godot、Electron、Node.js、Verilog、Ruby/Perl polyglot）见 [[binary/逆向工程/languages-platforms.md|平台语言]]；Go 与 Rust 见 [[binary/逆向工程/languages-compiled.md|编译型语言]]；静态工具链细节见 [[binary/逆向工程/tools.md|静态工具]]。

## 0. 判据速查

| 看到什么 | 第一动作 | 去哪节 |
|---|---|---|
| `.pyc` / `dis.dis` 输出 | 跳过头部 `marshal.load` 再反汇编 | §1 |
| 反编译器报 opcode 错误 | 操作码被重映射 | §2 |
| 题目指定 Python 版本（含 alpha） | 必须编译那个版本才能反汇编 | §3 |
| 有 `pyarmor_runtime` | 1shot 静态脱壳 | §4 |
| PE 的 DOS stub 异常大 | stub 里藏着 16 位程序 | §5 |
| `libil2cpp.so` + `global-metadata.dat` | Il2CppDumper；失败说明元数据加密 | §6 |
| `.hap` / `.abc` | abc-decompiler CLI 模式 | §7 |
| 源码全是 `+-<>[],.` | Brainfuck，三种打法 | §8 |
| 固件里有 `PE32+` | UEFI DXE 驱动 | §9 |
| 混淆 VM 的字节码 | 转译成 C 让 `-O3` 折叠 | §10 |
| 给了代码覆盖率 JSON | 覆盖率就是侧信道 | §11 |
| `.impl` / `.sign` / `IF..FI` | OPAL 纯函数式 | §12 |
| S-box 有碰撞 | 非双射替换，需消歧 | §13 |
| 题目提分数、质因数分解 | FRACTRAN | §14 |
| 只有 `Makefile` | 图灵完备，抽转移表 | §15 |

---

## 1. Python 字节码

### 最常见形态：分索引 XOR 校验

1. 检查 flag 长度
2. 偶数位与 `key1` 异或，比对 `p1`
3. 奇数位与 `key2` 异或，比对 `p2`

```python
flag = [''] * flag_length
for i in range(len(p1)):
    flag[2*i]   = chr(p1[i] ^ key1)      # 偶数位
    flag[2*i+1] = chr(p2[i] ^ key2)      # 奇数位
print(''.join(flag))
```

### 指令级判据

| 指令 | 含义 |
|---|---|
| `LOAD_CONST` + `COMPARE_OP` | 期望值直接可见 |
| `BINARY_XOR` / `BINARY_ADD` | 变换类型 |
| `BUILD_TUPLE` / `BUILD_LIST` + 常量 | 目标数组 |
| `FOR_ITER` + `BINARY_SUBSCR` | 遍历 flag 字符 |
| `ord` 上的 `CALL_FUNCTION` | 字符转整数 |

**关键**：Python 字节码把算法摊成显式栈操作，不需要跑起来。盯住 `LOAD_CONST`（期望输出）、`BINARY_XOR`（变换）、`BUILD_TUPLE`（目标数组）三样就够还原。

---

## 2. 操作码重映射

**判据**：反编译器报 opcode 错误（不是语法错误）。

1. 在 PyInstaller 包里找被改过的 `opcode.pyc`
2. 与标准 Python 操作码表对比
3. 建映射 `{新操作码: 原操作码}`
4. 打回目标 `.pyc`
5. 正常反编译

**捷径**：如果题目自带改过的解释器（如 `./py`），把 `uncompyle2`/`uncompyle6` 装进**那个解释器的环境**，用它自己反编译——改过的解释器懂自己的映射，不需要手工还原。

**工具按版本选**：`uncompyle6` 支持 Python 2.x–3.8；3.9+ 用 `pycdc`（`git clone && cmake . && make`）。

---

## 3. 版本特定字节码

**判据**：题目指定某个 Python 版本（含 alpha/beta）。alpha 版的操作码与稳定版不同，必须编译那个确切版本才能反汇编。

```bash
wget https://www.python.org/ftp/python/3.14.0/Python-3.14.0a4.tar.xz
tar xf Python-3.14.0a4.tar.xz
cd Python-3.14.0a4 && ./configure && make -j$(nproc)
./python -c "import dis, marshal; dis.dis(marshal.loads(open('challenge.pyc','rb').read()[16:]))"
```

**常见校验**：flag 与「ASCII 平方值元组」比对 → `flag[i] = chr(isqrt(expected[i]))`。

---

## 4. Pyarmor

判据：目录里有 `pyarmor_runtime`，脚本头部是 `PY` + 六位数字（8.x/9.x）；`PYARMOR` 头是 7 及更早，**不在 1shot 支持范围**。

工具与完整参数见 [[binary/逆向工程/tools.md|静态工具]] §5。

**唯一要记的**：反汇编是准的，反编译源码是实验性的——两者冲突时以反汇编为准。

---

## 5. DOS stub

PE 可以在 DOS stub（PE 头之前）里藏一个完整可用的 16 位 DOS 程序。

1. Ghidra/IDA 里看 DOS stub 是否异常大
2. 用 IDA 按 **16 位 DOS** 载入
3. 或在 DOSBox 里直接跑
4. 找 `int 16h`（键盘输入）

**判据**：stub 体积远超常规（正常只有几十字节的 `This program cannot be run in DOS mode`），题目逻辑可能完全在 stub 里。

---

## 6. Unity IL2CPP

- 用 Il2CppDumper 导出符号
- **Dumper 失败 → `global-metadata.dat` 被加密**：在主二进制里搜 metadata 加载路径，找到自定义解密后再 dump
- 找 `Start()` 函数
- 密钥派生常见形态：`key = SHA256(companyName + "\n" + productName)`，用它解服务端响应

**文件位置**：PC 平台是 `GameAssembly.dll` 或 `*Assembly.dll`，Android 是 `libil2cpp.so`。

平台侧（资源提取、Mono 反编译、运行时补丁）见 [[binary/逆向工程/platforms.md|平台专项]]。

---

## 7. HarmonyOS HAP / ABC

`.hap` 是 zip，先解出 `.abc`，再用 abc-decompiler。

**启动方式必须是 CLI 入口**（`java -jar` 会起 GUI）：

```bash
java -cp "./jadx-dev-all.jar" jadx.cli.JadxCLI -m simple --log-level ERROR -d "out_abc_simple" "modules.abc"
```

完整参数与排错顺序见 [[binary/逆向工程/tools.md|静态工具]] §7。

---

## 8. Brainfuck / Esolang

先确认是否用已知工具（BF-it）编译，再理解纸带模型。

### 逐字符静态分析

**判据**：` ,` 后面跟着一长串 `+`，其个数就是该字符的期望 ASCII 值。

```python
bf_code = open('challenge.bf').read()
segments = bf_code.split(',')        # 逗号 = 读入一个字符
expected = []

for seg in segments[1:]:             # 跳过第一个逗号前的引导段
    plus_count = 0
    for ch in seg:
        if ch == '+':
            plus_count += 1
        elif ch in '-.[]><':
            break                # 遇到第一个非自增操作就停
    if plus_count > 0:
        expected.append(chr(plus_count % 256))

print(''.join(expected))
```

**变体**：
- 用 `-`：值 = `256 - minus_count`
- `+`/`-` 混用：看净增量
- 字符之间有 `[-]`（清空）：每段独立
- 循环乘法 `[->>+++<<]`：数内层操作次数

### 读入计数侧信道

**判据**：逐字符校验的 BF 程序，字符正确才会继续读下一个。数 `,` 执行了多少次，读得最多的候选就是对的。

```python
def bytes_read(bf_code, input_iter, braces):
    tape = [0] * 30000
    ptr = ip = reads = 0
    input_list = list(input_iter); input_idx = 0
    while ip < len(bf_code):
        c = bf_code[ip]
        if c == ',':
            if input_idx < len(input_list):
                tape[ptr] = input_list[input_idx]; input_idx += 1; reads += 1
            else:
                return reads
        elif c == '+': tape[ptr] = (tape[ptr] + 1) % 256
        elif c == '-': tape[ptr] = (tape[ptr] - 1) % 256
        elif c == '>': ptr += 1
        elif c == '<': ptr -= 1
        elif c == '[' and tape[ptr] == 0: ip = braces[ip]
        elif c == ']' and tape[ptr] != 0: ip = braces[ip]
        ip += 1
    return reads

PRINTABLE = list(range(32, 127))
flag = []
for _ in range(50):
    baseline = bytes_read(bf, flag + [PRINTABLE[0]], braces)
    found = None
    for b in PRINTABLE[1:]:
        if bytes_read(bf, flag + [b], braces) > baseline:
            found = b; break
    if found is None: break
    flag.append(found)
```

### 比较惯用法检测

**判据**：由高级语言编译出来的 BF 会复用固定惯用法。等值比较是 `<[-<->] +<[>-<[-]]>[-<+>]`，在解释器里检测这个模式就能直接从纸带上读到两个操作数（一个是输入字节，一个是期望字节）。

**三个必记惯用法**：
- `[-]` — 清空单元格
- `[->+<]` — 把值右移一格
- `<[-<->] +<[>-<[-]]>[-<+>]` — 两格等值比较

---

## 9. UEFI

```bash
7z x firmware.bin -oextracted/
file extracted/* | grep "PE32+"
```

UEFI 二进制就是 PE32+，按普通 PE 载入 Ghidra/IDA。

**判据**：Bootkit 会替换 boot loader，所以逻辑在 **DXE 驱动**与 boot services protocol 里，不在 setup 阶段。若见到自定义 VM，把 VM 字节码 lift 成 C。

---

## 10. 转译成 C

重度混淆的 VM 字节码，直接逐指令翻译成 C，交给编译器优化：

```python
for opcode, args in instructions:
    if opcode == 'XOR':
        print(f"r{args[0]} ^= r{args[1]};")
    elif opcode == 'ADD':
        print(f"r{args[0]} += r{args[1]};")
```

`gcc -O3` 会做常量折叠与死代码消除，把算法自动简化掉。

**判据**：手写去混淆比让编译器干慢得多。只要指令集能一对一映射到 C 语句，就该走这条路。

---

## 11. 代码覆盖率侧信道

**判据**：题目给了 XDebug 的覆盖率 JSON 和加密输出。

PHP 侧：
- `xdebug_start_code_coverage(XDEBUG_CC_UNUSED | XDEBUG_CC_DEAD_CODE | XDEBUG_CC_BRANCH_CHECK)`
- 加密用数据相关分支：`if ($xored == chr(0)) ... if ($xored == chr(1)) ...`
- 覆盖率 JSON 暴露加密时走了哪些分支
- 这就泄漏了出现过的所有 XOR 中间值

```python
import json

with open('coverage.json') as f:
    cov = json.load(f)

executed_xored = set()
for line_no, hit in cov['encrypt.php']['lines'].items():
    if hit > 0:
        executed_xored.add(extract_value_from_line(line_no))   # 行号 → chr(N)

for pos in range(len(ciphertext)):
    candidates = [kb for kb in range(256)
                  if (plaintext_byte ^ kb) in executed_xored]
    # 配合已知明文前缀（flag{）即可唯一定 key
```

**关键**：任何带数据相关分支的加密都会通过覆盖率泄漏信息。反向判据——见到无分支/常数时间实现，说明作者已经防了这一手。

---

## 12. 函数式语言（OPAL）

**识别标记**：
- `.impl`（实现）与 `.sign`（签名）源文件
- `IMPLEMENTATION` / `SIGNATURE` 关键字
- 嵌套 `IF..THEN..ELSE..FI`
- 函数名是 `f1`、`f2` … `fN`
- 大量 `seq[nat]`、`string`、`denotation` 类型

**打法**：
1. 纯函数数学上可逆，逐步反演
2. 认出变换链 `f_final(f_n(...f_2(f_1(input))...))`
3. 对每个函数构造逆函数

### 打乱函数的聚合爆破

**判据**：变换累积了依赖「原始未知值」的状态（鸡生蛋问题）。

```python
decoded = base64_decode(target)
for total_S in range(256):                     # 聚合效果只有 256 种，不是指数级
    candidate = [(b - total_S) % 256 for b in decoded]
    recomputed = sum(contribution(i, candidate[i]) for i in range(len(candidate))) % 256
    if recomputed == total_S:
        result = apply_inverse_substitution(candidate)
        if all(32 <= c < 127 for c in result):
            print(bytes(result))
```

**关键**：爆破**聚合效果**（常是 mod 256，256 种），而不是所有中间状态（指数级）。

---

## 13. 非双射替换

**检测**：

```python
if len(set(sbox)) < len(sbox):
    print("非双射！存在碰撞")
```

**建反向表**：

```python
from collections import defaultdict
rev = defaultdict(list)
for i, v in enumerate(sbox):
    rev[v].append(i)          # rev[输出] = [所有可能输入]
```

**四种消歧手段**（按优先级）：
1. 已知明文格式（`ENO{`、`flag{`）钉死已知位置的字节
2. 侧信道数据（覆盖率、时序）排除不可能候选
3. 可打印 ASCII（32-126）压候选空间
4. 拿候选重新加密，与已知密文比对

---

## 14. FRACTRAN

计算过程是「反复乘分数表」。输入编码成质因数分解（ASCII 值当连续素数的指数）。

**反演**：把每个分数的分子分母对调，让目标输出倒着跑过反向程序。

```python
def fractran_step(n, fractions):
    for num, den in fractions:
        if (n * num) % den == 0:
            return (n * num) // den
    return None            # 停机

inverted = [(d, n) for n, d in fraction_table]    # 对调分子分母
# 用目标输出跑 inverted，得到输入
```

**判据**：题目提到分数、质因数分解，或直接给一串有理数。

---

## 15. GNU Make 图灵机

**判据**：题目只给一个 `Makefile`。

纸带是 unary 编码（`+` 是 1、`-` 是 0），状态转移表是 14 个空白分隔的词（每个 9 bit），递归 `$(eval)` 驱动状态机直到停机。

```make
PROGRAM := 0 1A 1R 1 1B 1L 1 1C 1R 0 1D 1L 0 1A 1L 1 1E 1R 1 1A 1L \
           1 1A 1L 0 1E 1R 0 1F 1R 0 1F 1L 1 1E 1L 1 1H 1R 0 1C 1R
```

```python
def decode_transition(word):
    bits  = int(word, 2)
    write = bits >> 7 & 1
    move  = "LR"[bits >> 6 & 1]
    state = chr(ord("A") + (bits & 0x3F))
    return write, move, state

tape = [0] * 4096
head, state = 2048, "A"
while state != "H":
    symbol = tape[head]
    idx = (ord(state) - ord("A")) * 2 + symbol
    write, move, state = decode_transition(PROGRAM[idx])
    tape[head] = write
    head += 1 if move == "R" else -1
print(sum(tape))       # 忙碌海狸得分 → 再哈希成 flag
```

**关键**：`Makefile` 靠递归 `$(eval)` 与字符串替换达到图灵完备，所以「只给一个 Makefile」的题可能在藏一个完整解释器。最快的路是把转移表当文本抽出来、逐词解码，然后本地模拟或直接查忙碌海狸数据库——手搓的 BB 机器大多在前 6 个状态内能对上公开的。`make -n` 看展开后的命令而不执行，`make -d` 看递归 eval 图。

---

## 16. 纪律

- **先认运行时再动手**：`.pyc`、`.abc`、`global-metadata.dat`、BF 源码、`Makefile` 是完全不同的五种题，工具选错白耗一轮。
- **反编译失败先怀疑元数据加密**：IL2CPP 的 dumper 失败、HarmonyOS 的 `auto` 模式失败、Pyarmor 反编译输出乱——都不是「工具不行」，是「输入被加密了」。
- **esolang 不追逻辑，抽常量**：BF/FRACTRAN/Make 这类题的答案在常量里，不在控制流里。
- **有侧信道就先打侧信道**：覆盖率、读入计数、时序——比静态还原整个算法快得多。
- **鸡生蛋问题就爆聚合值**：mod 256 的聚合效果只有 256 种，别去爆中间状态。
- **报的是运行时不是难度**：OPAL、Moxie、FRACTRAN 看着唬人，其实都是「换入口」。
