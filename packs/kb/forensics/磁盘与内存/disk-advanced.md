---
title: 进阶磁盘与内存取证
phase: forensics
vuln_class: [forensics, disk, memory, filesystem, raid, snapshot]
summary: 进阶镜像题的判据是「数据在哪个版本/哪个副本里」——写时复制文件系统查快照，RAID 查奇偶校验，GUID/元数据字段查非常规编码，哈希数据库查哈希槽位而非插入顺序。
---

# 进阶磁盘与内存取证

基础镜像流程（Volatility / Sleuth Kit / OVA / 容器 / 云桶）见 [[forensics/磁盘与内存/disk-and-memory.md|磁盘与内存镜像取证]]；文件系统修复与雕刻见 [[forensics/磁盘与内存/disk-recovery.md|磁盘恢复]]。

## 0. 判据速查

| 观察到的现象 | 结论 | 去哪节 |
|---|---|---|
| `fdisk -l` 看不到分区但镜像有数据 | 分区表被删，`testdisk` / `kpartx` | §1 |
| 镜像里有 `APSB` / 提到 snapshot | 写时复制快照，跨 XID 读同一 inode | §2 |
| 多个同尺寸磁盘镜像 | RAID，缺的那块用 XOR 补 | §3 |
| GPT 的 GUID 看着像 ASCII/magic | GUID 是 16 字节任意数据 | §4 |
| ZFS 四个 label 全 0 | nvlist 在别处，Fletcher4 要重算 | §5 |
| 有 `.wcm` / Lotus 等旧文档格式 | 宏里嵌加密数组 | §6 |
| 只给 minidump | `strings -a` 先捞，再做结构化解析 | §7 |
| VMDK 挂不上 | 稀疏格式，手解 grain 表 | §8 |
| ZIP 缺中央目录 | 逐个读 local header | §9 |
| HFS+ 里 carving 一无所获 | Resource Fork 是第二条数据流 | §10 |
| 未知 `.db`，`strings` 里有 `KCPH` | Kyoto Cabinet，按哈希槽恢复顺序 | §11 |
| 有 `type/position/text` 三列的表 | 编辑历史 diff，逐条回放 | §12 |

## 1. 分区表被删

```bash
fdisk -l image.img        # 无分区
testdisk image.img        # 交互式恢复
kpartx -av image.img      # 映射为 /dev/mapper/loop0p1
mount /dev/mapper/loop0p1 /mnt/evidence
find /mnt/evidence -name ".*"
```

**flag 常藏在路径分量里**（`/.Meta/CTF/{f/l/a/g}`）——所以 `find -name ".*"` 之后要**逐层 `ls -la`**，光看文件名不够，目录名本身就是数据。

## 2. 写时复制快照

APFS / ZFS / Btrfs 都保留历史状态。**「数据被篡改/投毒」类题的解法几乎一定是去旧快照取原值。**

```python
import struct
mm = open("apfs_partition.img", "rb").read()
snaps, pos = [], 0
while True:
    idx = mm.find(b"APSB", pos)
    if idx < 0:
        break
    hdr = idx - 32
    xid = struct.unpack_from("<Q", mm, hdr + 16)[0]
    snaps.append((xid, hdr // 4096))
    pos = idx + 1
```

```bash
icat -f apfs -P apfs -B <blk> apfs_partition.img 449414
```

**用同一个 inode 号在不同 `-B <blk>`（对应不同 XID）下读**，比较内容就能定位投毒发生的时点，然后取投毒前的值去解密。

**判据**：题面出现 poisoned / tampered / corrupted / original value，且文件系统是 APFS/ZFS/Btrfs 之一。这三个都是 CoW，旧块在被回收前一直可读。

## 3. RAID 5 缺盘

任一条带内 `Disk1 ⊕ Disk2 ⊕ ... ⊕ DiskN = 0`，所以缺的那块等于其余全部 XOR。

```python
disk1 = open('disk1.img','rb').read()
disk3 = open('disk3.img','rb').read()
open('disk2.img','wb').write(bytes(a ^ b for a, b in zip(disk1, disk3)))
```

```bash
mdadm --create /dev/md0 --level=5 --raid-devices=3 disk1.img disk2.img disk3.img
mount -o loop,ro disk2.img /mnt/recovered
```

**两个坑**：

- **RAID 5 的校验块位置逐条带轮转**，不是固定在某一块盘上。直接整盘 XOR 只在「校验块位置恰好对齐且条带大小等于整盘」时成立——真实阵列要先按 `chunk size` 分条带再逐条带补。
- **盘序可能被打乱**，`mdadm --create` 前要确认顺序，错了会得到全乱的文件系统。

## 4. GPT GUID 编码

**GPT 分区 GUID 是 16 字节任意值**，可以塞任何东西。

```python
import struct
data = open('image.img','rb').read()
for i in range(128):
    entry = data[1024 + i*128 : 1024 + (i+1)*128]   # 分区项从 LBA2 开始，每项 128 字节
    guid = entry[16:32]
    if guid != b'\x00'*16:
        print(f'Partition {i}: {guid.hex()}')
```

**判据是 GUID 里出现文件 magic**：`BZh11AY&SY` 是 bzip2 头，`PK\x03\x04` 是 ZIP，`\x1f\x8b` 是 gzip。看到就把所有 GUID 按分区顺序拼起来，再按那个格式解。

**题面双关是提示**：「LLMs only」+「undercut」不是 AI 的 GPT，是 GUID Partition Table。

## 5. ZFS

1. **四个 label 可能全被清零**——用 `strings` + 偏移搜索在镜像别处找 nvlist 数据。
2. **MOS 对象修复**：把已知良好的 nvlist 字节拷回块位置，重算 Fletcher4。
3. **加密破解**：从 ZAP 对象取 PBKDF2 参数（迭代数、salt），GPU 加速 PBKDF2-HMAC-SHA1，CPU 验 AES-256-GCM unwrap。

```python
def fletcher4(data):
    a = b = c = d = 0
    for i in range(0, len(data), 4):
        a = (a + int.from_bytes(data[i:i+4], 'little')) & 0xffffffff
        b = (b + a) & 0xffffffff
        c = (c + b) & 0xffffffff
        d = (d + c) & 0xffffffff
    return (d << 96) | (c << 64) | (b << 32) | a
```

**Fletcher4 是累积和不是 CRC**——每一级都把前一级加上去，所以改一个字节会让后面所有累加器变化。改完必须整块重算，只改校验字段没用。

## 6. 旧文档格式里的宏

WordPerfect `.wcm`、Lotus 1-2-3 等可以内嵌可执行宏与加密数组。

**注意**：宏里可能写成 `(bb + kb) - 2*(bb & kb)`，这在数学上**等价于 XOR**（`a+b-2*(a&b) == a^b`）。看到这个式子别当成新算法，直接按 XOR 处理。

```python
import string
allowed = set(map(ord, string.ascii_lowercase + string.digits + "_{}"))
cands = []
for j in range(4):
    cands.append([k for k in range(256)
                  if all((docbody[i] ^ k) in allowed for i in range(j, len(docbody), 4))])
for k0 in cands[0]:
  for k1 in cands[1]:
    for k2 in cands[2]:
      for k3 in cands[3]:
        key = [k0,k1,k2,k3]
        pt = ''.join(chr(c ^ key[i%4]) for i,c in enumerate(docbody))
        if pt.startswith("srd") and pt.endswith("}"):
            print(pt)
```

**每个密钥字节可以独立筛**：位置 `i` 的字节只影响 `i mod 4` 的字符，所以 4×256 就能把候选压到个位数，不用 2^32。

## 7. Minidump

```bash
strings -a -n 6 minidump.dmp | grep -iE "flag|ctf|SYNC|SSH_CLIENT|SESSION_KEY"
```

**minidump 含完整内存区域**，字符串常量、密钥、环境变量都在。**先 `strings` 再上工具**——这一步通常就结束了。

**SSH 痕迹**：`SSH_CLIENT` 给出源 IP 与临时端口，`SSH_CONNECTION` 给出四元组。这类环境变量只在会话进程的内存里，磁盘上没有。

## 8. VMDK 稀疏格式

挂不上时手解：读稀疏头（grain size、GD 偏移、GT 覆盖范围）→ 沿 grain directory → grain table → data grain → 算出跨分割文件的绝对偏移 → 再按文件系统挂。

**判据**：`file` 说是 VMware 但 `mount` 失败，且镜像是 `-s001.vmdk` 这种分片命名。**先试 `7z`**（能直接读多数 VMDK），不行再手解。

## 9. 从内存里抽出的二进制 / 无中央目录的 ZIP

**内存里的可执行文件要抽出来真跑真逆，不要信 `strings`**——字符串表可能是诱饵。

```python
key = b"..."   # 可以是几百字节的 ASCII art
cipher = open('extracted.bin','rb').read()
plaintext = bytes((b - 0x32) ^ key[i % len(key)] for i, b in enumerate(cipher))
```

**密钥长度没有上限**：ASCII art、lorem ipsum、整段源码都能当循环密钥。别假设密钥短。

ZIP 中央目录损坏时逐个读 local header：

```python
import struct, zlib
pos, files = 0, {}
while True:
    off = dec.find(b"PK\x03\x04", pos)
    if off < 0: break
    (ver, flag, method, _, _, crc, csize, usize, nlen, xlen) = struct.unpack_from("<HHHHHIIIHH", dec, off+4)
    name = dec[off+30:off+30+nlen].decode()
    data_off = off + 30 + nlen + xlen
    comp = dec[data_off:data_off+csize]
    files[name] = zlib.decompress(comp, -15) if method == 8 else comp
    pos = data_off + csize
```

**`method == 8` 是 deflate，解压必须用 `-15`**（raw deflate，无 zlib 头）。用默认的 `zlib.decompress()` 会报 `incorrect header check`。

## 10. HFS+ Resource Fork

**HFS+ 每个文件有两条数据流**：Data Fork 与 Resource Fork。`binwalk` / `foremost` / `strings` **只看 Data Fork**，所以全都没结果时不要放弃。

- HFSExplorer 看 catalog，找 Resource Fork 大小非 0 的条目
- 010 Editor + HFS 模板看 extent 记录，拿起始块与长度
- 跨多个 extent 时逐段 `dd` 再 `cat` 拼

```bash
dd if=image.dmg bs=4096 skip=$BLOCK1 count=$LEN1 of=part1.bin
dd if=image.dmg bs=4096 skip=$BLOCK2 count=$LEN2 of=part2.bin
cat part1.bin part2.bin > recovered_binary
```

**`.fseventsd` 是 HFS+ 的操作日志**：`FSEventsParser` 能还原「某文件何时被创建/删除」，用来定位隐藏文件的时间窗口。

## 11. 哈希数据库的顺序恢复

**核心洞察：哈希库按 key 的哈希值决定槽位，不是插入顺序。** key 被清零后，值的顺序就丢了。

```bash
for i in $(seq -w 000 088); do
    cp tokyo.kch test.kch
    kchashmgr set test.kch "$i" "probe"
    diff <(xxd tokyo.kch) <(xxd test.kch) | head -5   # 变化的偏移 = 该 key 占的槽
done
```

**逐个插入探针 key 再二进制比对**，就能反推「槽位 → 原 key」的映射，按 key 排序后取对应 value 拼出 flag。

**为什么必须逐个插**：一次插多个时不知道哪个槽位对应哪个 key；而且探测 key 会互相覆盖。

## 12. 编辑历史回放

```python
db = sqlite3.connect('notes.db')
doc = ""
for op, pos, text in db.execute("SELECT type, position, text FROM diffs ORDER BY id"):
    if op == 'insert':
        doc = doc[:pos] + text + doc[pos:]
    elif op == 'remove':
        doc = doc[:pos] + doc[pos+len(text):]
    if 'flag{' in doc or 'CTF{' in doc:
        print(doc)
```

**每一步都要检查**，不能只看最终文档——flag 常是「打出来后又被删掉」。

**判据**：表里有 `type`/`operation`、`position`、`text` 三列。`.schema` 一看就知道。

## 13. Linux 勒索：内存找密钥

Volatility 的 Linux 插件常因符号表/KASLR 不匹配返回空——**这时的正确动作是原始内存候选扫描，不是修符号表**。

1. **先验压缩包完整性**：`unzip -l` 列出的文件与解出来的比对，不一致就重新解。
2. **快速逆勒索样本**：`strings -a ransomware.elf | grep -E "enc_key|EVP_aes|PUBLIC KEY|.veg"`。典型是 `AES-256-OFB`、IV 前置、全局 32 字节密钥、RSA 公钥硬编码。
3. **原始扫描找密钥**：用内存里的**锚点字符串**（`/home/.../enc_key.bin`、HOME 路径）推候选偏移，取页对齐窗口内的 32 字节。
4. **用 magic 当密钥 oracle**：拿每个候选解密多个 `.veg` 的首块，看是否出现 `%PDF-` / `PK\x03\x04` / `\x89PNG\r\n\x1a\n`。**同时满足多个独立签名**的才是真密钥。
5. **全量解密后核对文件数**与 zip 清单一致，注意 `snap/*/Downloads/` 这类镜像目录要去重。

**OFB 的偏移**：`iv = 前 16 字节`，密文从 `+16` 开始。**OFB 是流模式，不分组**——所以能只解首块做验证，也能从任意位置开始解。

**假 flag 防御**：只出现在元数据里的 `CTF{...}` 很可能是诱饵，要用全语料一致性校验（`rg -a '[A-Za-z]+CTF\{[^}]+\}' recovered_full`）。

## 14. 纪律

- **先问「数据在哪个副本里」**。快照、奇偶校验、旧 layer、旧 version——「被删/被改」的题答案几乎都在另一个副本里，而不是在恢复被删的那一份。
- **元数据字段是合法载荷**。GUID、时间戳、扩展属性、文件名、路径分量都能编码，别只盯着文件内容。
- **顺序也是数据**。哈希库的槽位顺序、编辑历史的操作序列、RAID 的条带轮转——顺序错了内容对也没用。
- **别信 `strings` 的输出当结论**。它给的是候选，真正的答案要抽出二进制或解出明文再验。
