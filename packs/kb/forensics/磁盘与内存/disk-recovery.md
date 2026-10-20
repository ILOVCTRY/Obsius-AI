---
title: 磁盘数据恢复与容器链提取
phase: forensics
vuln_class: [disk-recovery, fat, ext, btrfs, xfs, zip-repair, carving, git-forensics]
summary: 恢复类题目的主线是“元数据在哪一层”：文件系统元数据、压缩容器头、应用记录结构或 git 历史都可能比数据本身更可靠，先确认哪层仍完整再决定恢复手段。
---

# 磁盘数据恢复与容器链提取

Volatility、磁盘镜像、VM/OVA/VMDK、云与容器取证见 [[forensics/磁盘与内存/disk-and-memory.md|磁盘与内存取证]]；分区表/ZFS/APFS/RAID/内存 carving 见 [[forensics/磁盘与内存/disk-advanced.md|磁盘与内存进阶]]；Unix 提取见 [[forensics/磁盘与内存/linux-forensics.md|Linux 系统取证]]。

## 0. 判据速查

| 现象 | 优先动作 | 常见误区 |
|---|---|---|
| 挂载后文件缺失 | 先列删除项/孤儿 inode/子卷 | 直接 carving 全盘 |
| 挂载正常但看不到内容 | 检查 shadow 子卷/快照/未挂载分区 | 只挂默认子卷 |
| carving 找不到 magic | 文件系统级 extent 提取 + 反混淆 | 只跑 binwalk/foremost |
| 容器打不开 | 看头尾 magic 与 CRC 字段，先修结构 | 直接报“文件损坏” |
| zip 列出为空 | 核对 LFH/CDE 长度字段是否一致 | 重下/换工具不解结构 |
| zip 有口令 | 区分 ZipCrypto/AES；ZipCrypto 可已知明文攻击 | 直接暴力字符集 |
| 深度嵌套文件 | 逐层 file/识别并递归提取 | 手工逐层点开 |
| 密钥在历史版本 | git 历史/回收站/快照/旧提交 | 只查当前工作树 |

## 1. 内存与运行态密钥恢复

加密卷的主密钥常驻留在进程内存中。AES key schedule 具特殊数学结构，可用 `aeskeyfind` 搜索候选 key；对 LUKS/dm-crypt 等格式，拿到 master key 后可添加新口令再打开卷：

```bash
aeskeyfind memory.elf
echo "<hex key>" | xxd -r -p > master.key
cryptsetup luksAddKey --master-key-file master.key <device>
```

RSA 私钥用 `rsakeyfind`。候选 key 需要验证（能否解密、magic 是否出现），不要把顺序排列的候选直接当 key。

若密钥由时间种子 PRNG 生成，先用文件 mtime/ctime、日志与题面时间圈定窗口，再逐秒重放 PRNG 并用解密后的文件 magic 验证。窗口要覆盖时钟偏移、时区与文件系统粒度，不能只取单一时间戳。

## 2. 文件系统级恢复

### 2.1 删除项与孤儿 inode

FAT 删除只在目录项首字节写 `0xE5` 并把簇标记为空闲，数据仍在。优先用 TSK 列表删除项并按 inode 提取，比盲 carved 更能保留文件边界：

```bash
fls -r -d image.img        # 列出已删除项
icat image.img <inode> > recovered.bin
```

ext2/3/4 删除保留 inode 与数据块；`e2fsck` 可把未挂接 inode 连回 `lost+found`，也可用 `debugfs` 的 `lsdel`/`extundelete` 按 inode 提取。命名破坏（如 `_lag.png`）可结合同目录项顺序与内容 magic 还原。`.git` 等目录删除后，对象文件按 inode 逐个提取即可重建对象库，再用 `git fsck`/`git log --all` 恢复历史。

### 2.2 空闲簇与反 carving

FAT16/32 空闲簇（FAT 项为 0）不清零，按引导扇区参数解析布局后逐个读取即可恢复隐藏或已删数据：

```python
# boot sector → bytes/sector, sectors/cluster, reserved, nFATs, sectors/FAT, root entries
cluster_size = bytes_per_sector * sectors_per_cluster
fat_start = reserved_sectors * bytes_per_sector
root_dir_start = fat_start + nFATs * sectors_per_fat * bytes_per_sector
data_start = root_dir_start + root_entries * 32
```

若 carving 无命中而文件系统显示大小正常，先看是否存在字节级混淆：例如每字节后插入 `0x00` 使体积翻倍、容量翻倍与 magic 断续是典型特征。按文件系统 extent 提取原始块，再做去混淆。

### 2.3 BTRFS 子卷与快照

COW 文件系统删除文件不会立即擦除被快照引用的块。默认挂载只显示活动子卷，先枚举全部子卷再逐一提权挂载：

```bash
btrfs subvolume list /dev/loop0
mount -o subvol=@backup /dev/loop0 /mnt/backup
btrfs-find-root /dev/loop0     # 找孤立旧根
```

子卷 ID、generation 和快照时间是重要证据；不要只依据可见树判定“文件不存在”。

### 2.4 XFS 元数据重建

superblock 损坏但 inode 完整时，可直接解析 inode 内嵌 extent（`startoff/startblock/blockcount`）并按块大小 `dd` 抽取；extent 超过内联容量时循 B+tree 根。修正块号与文件长度后按 magic 校验。文件大小与 extent 覆盖范围不一致时以 extent 为准复核。

## 3. 嵌套容器链

### 3.1 递归提取原则

外层载体打不开、搜不到内容时，假定每个提取物仍是载体。流程固定为：`file` 识别 → 按 magic 提取 → 对新目标重复。`binwalk --dd=".*"` 可批量落地所有签名命中；DOCX/XLSX/PPTX/APK/JAR 本质是 ZIP，直接解包即可。

### 3.2 尾部追加与顺序歧义

- PDF/PNG 之后常附数据：检索 `%%EOF`/`IEND` 之后的内容再 `strings | tail`。
- tar 允许多个同名条目：默认提取只保留最后一个，用 `--occurrence=N` 取中间条目，或程序化遍历成员。
- 深度嵌套 zipped chain：口令若由正则描述，不要穷举字符集，用 `exrex` 生成少量候选后逐层自动提取；每层读提示并更新下一个口令。

### 3.3 文件系统套文件系统

镜像可层层嵌套压缩文件系统（可达数十层）。每步循环执行：识别类型 → 解压/挂载 → 在新层中找下一目标。少见文件系统需预装驱动；无分区表时手动计算偏移挂 loop。

## 4. 压缩与格式修复

### 4.1 头尾互推与 CRC

格式损坏时优先看尾部 footer：XZ 的 footer 保存 stream_flags，可与 header magic、flags、CRC32 一起重建 12 字节头；同样思路适用于 gzip、zstd 等“尾端保存关键字段”的容器。原则是检查最后几字节的真实 footer 签名，再决定修头还是修尾。

### 4.2 ZIP 结构修复

ZIP 列出为空但 magic 正常，通常是 Local File Header（偏移 26）与 Central Directory（偏移 28）的 filename/extra 长度字段被破坏。两处长度必须一致且匹配实际文件名；修好后重写并校验。若仍失败，从候选偏移直接 `zlib.decompressobj(-15)` 试探 raw deflate 数据。

### 4.3 ZipCrypto 与 AES 口令

ZipCrypto（传统 PKZIP 流密码）在已知 12 字节以上明文时可完全恢复内部状态，`bkcrack` 兼容性优于 `pkcrack`；需要未加密参考文件或可猜文件头。AES 加密 zip 只能爆破。先用 `file`/头部字节区分类型，不要混用工具。

### 4.4 特殊压缩模型

FemtoZip 等共享字典压缩需要配套 `.model` 才能解压；按题面提供模型解压后，再按元数据条件筛选产物文件。识别点是出现 `.model` 且大量同构小文件。

## 5. 应用层结构取证

### 5.1 VBA/单元格编码

宏可能把二进制编码进单元格数值（如 `value = byte*3 + 78`）。先读宏找编码公式，逆向得 `byte = (value - c) // m`，再批量还原并用 `file` 校验。注意取数顺序（行优先/列优先）与空单元格处理。

### 5.2 SQLite serial-type

记录中的 serial-type varint 同时表达类型与长度：`type ≥ 13` 表示文本，长度为 `(type - 13)/2`。两份数据库逐字节 diff 后，定位被改动的 serial-type，再读相邻文本载荷即可还原隐藏字段。先理解记录结构再解释差异字节。

### 5.3 Git 历史

删除不等于消失。`git log --diff-filter=D` 找删除提交，`git show <commit>^:<path>` 恢复删除前内容；所有历史提交中的密钥材料都可取回。这类密钥只能用于授权环境下的取证验证。

## 6. 纪律

恢复按“元数据优先”排序：文件系统 → 容器结构 → 应用结构 → carving → 暴力猜测。所有操作在工作副本进行，记录 inode、偏移、块号、口令来��和步骤；每恢复一层都验证 magic/长度/CRC/可解码性。对 zip/加密容器先判断算法再选工具，避免把可解析问题升级为纯爆破。
