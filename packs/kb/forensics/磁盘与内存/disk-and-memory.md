---
title: 磁盘与内存镜像取证
phase: forensics
vuln_class: [forensics, memory, disk, volatility, vmdk, container, cloud]
summary: 镜像题的判据是「镜像是什么容器」——raw dmp 走 Volatility，dd/img 走 Sleuth Kit，ova/vmdk 走 7z，容器走 layer，云桶走 versioning。Volatility 失败不代表数据不在，framebuffer 与 MFT 残留往往直接可读。
---

# 磁盘与内存镜像取证

进阶（ZFS / GPT GUID / APFS 快照 / RAID5 / HFS+ / Kyoto Cabinet）见 [[forensics/磁盘与内存/disk-advanced.md|进阶磁盘与内存]]；文件系统修复与雕刻（LUKS / XFS / BTRFS / FAT / ZIP 修复）见 [[forensics/磁盘与内存/disk-recovery.md|磁盘恢复]]；Windows 专有痕迹见 [[forensics/磁盘与内存/windows.md|Windows 取证]]。

## 0. 判据速查

| 手上是什么 | 结论 | 去哪节 |
|---|---|---|
| `.dmp` / `.raw` / `.vmem` | 内存镜像，先 `windows.info` 认 profile | §1 |
| `.dd` / `.img` / `.E01` | 磁盘镜像，先 `fls` 看文件系统 | §2 |
| `.ova` | TAR，里面是 VMDK + OVF | §3 |
| `.vmss` + `.vmem` | VMware 挂起状态，转 dmp | §4 |
| Volatility 报 profile 不匹配 | 当原始像素扫，找 framebuffer | §5 |
| `core` / `core.<pid>` | coredump，gdb 直接读 | §6 |
| KAPE triage ZIP | 已采集好的 Windows 痕迹集 | §7 |
| PowerShell minidump + pcap | 脚本块 + 网络附件 + 内存里的密钥 | §8 |
| `base.apk` / `backup.ab` / android img | Android，看 `shared_prefs` 与 `databases` | §9 |
| `image.tar` + `manifest.json` | Docker，查 layer 与 history | §10 |
| 桶名 / ARN / 存储账号 | 云存储，查 versioning | §11 |
| 文件开头 4 字节像小端长度 | BSON | §12 |
| 无 magic、512 整数倍、高熵 | TrueCrypt/VeraCrypt | §13 |
| 文件被删但 MFT 记录还在 | `mftscan --offset` 直取 `$DATA` | §14 |
| binwalk 什么都找不到 | 无 magic 压缩（Brotli/zstd/snappy） | §15 |

## 1. Volatility 3

```bash
vol -f memory.dmp windows.info
vol -f memory.dmp windows.pslist
vol -f memory.dmp windows.cmdline
vol -f memory.dmp windows.netscan
vol -f memory.dmp windows.filescan
vol -f memory.dmp windows.dumpfiles --physaddr <addr>
vol -f memory.dmp windows.mftscan | grep flag
```

**`mftparser` 是 Vol2 的名字，Vol3 叫 `mftscan`**——这是最常见的命令不存在原因。

**顺序**：`info`（确认 OS 版本与架构）→ `pslist`（有没有可疑进程）→ `cmdline`（启动参数里常有明文密钥）→ `netscan`（外连地址）→ `filescan` + `dumpfiles`（按物理地址抽文件）。**`cmdline` 与 `netscan` 的性价比最高**，很多题的 flag 直接在命令行里。

## 2. 磁盘镜像

```bash
sudo mount -o loop,ro image.dd /mnt/evidence    # 只读挂载，务必 ro

fls -r image.dd                # 递归列文件（含已删除项）
icat image.dd <inode>          # 按 inode 抽文件

photorec image.dd              # 按文件头雕刻
foremost -i image.dd
```

**`fls` 会列出被删条目（前面带 `*`）**，这些条目用 `icat` 往往还能取回——比直接 photorec 精确得多。**先 `fls` 再 `photorec`**：雕刻是无差别扫描，会把所有历史版本都吐出来，噪声大。

**分区表在镜像里时**先 `mmls image.dd` 拿分区偏移，再 `fls -o <offset>`。直接 `fls` 一个带分区的镜像会报「不是文件系统」。

## 3. OVA / VMDK

```bash
tar -xvf machine.ova
7z l disk.vmdk | head -100
7z x disk.vmdk -oextracted "Windows/System32/config/SAM" -r
```

**7z 能直接读 VMDK 不需要挂载**，这在没有 root 或 VMDK 是稀疏格式时特别有用。

高价值文件：`System32/config/{SAM,SYSTEM,SOFTWARE}`（哈希与启动密钥）、`Users/*/NTUSER.DAT`、`Users/*/AppData/`（浏览器与凭据）。

## 4. VMware 挂起快照

```bash
vmss2core -W snapshot.vmss snapshot.vmem    # 产出 memory.dmp
```

**`.vmss` 是 CPU/设备状态，`.vmem` 是物理内存**，两个都要给。转出来的 dmp 可直接喂 Volatility/MemprocFS。

**勒索样本狩猎顺序**：Amcache 看加密时间点前后执行了什么 → 查 Unicode 同形名（`ṙ` 冒充 `r`）→ 从内存 dump 可疑 exe → PyInstaller 打包的用 `pyinstxtractor` 解 `.pyc` → PyArmor 保护的上 PyArmor-Unpacker。

## 5. 原始内存当像素看

**Volatility 报 profile 不匹配、或根本没有 profile 时**，framebuffer 数据仍然在。

GIMP：File → Open → 选「Raw image data」→ 像素格式 RGB、宽度填显示器分辨率（1920/1366/1280/1024）→ 用方向键微调宽度，**宽度对上原始 stride 时画面就出来了**。

```python
from PIL import Image
import numpy as np
data = open('memory.dmp', 'rb').read()
for width in (1920, 1366, 1280, 1024):
    stride = width * 3
    for off in range(0, len(data) - stride * 100, stride * 500):
        chunk = data[off:off + stride * 100]
        if len(chunk) == stride * 100:
            arr = np.array(Image.frombytes('RGB', (width, 100), chunk))
            if 10 < arr.mean() < 245 and arr.std() > 20:
                Image.frombytes('RGB', (width, 100), chunk).save(f'f_{width}_{off}.png')
```

**判据**：`mean` 接近 0 是全黑（未使用内存），接近 255 是全白，`std` 小是纯色——三者都不是真实画面。**宽度不是 3 的倍数时**（如 4 字节 BGRA 的 X11 帧缓冲）要试 `width*4` 并丢 alpha 通道。

## 6. Coredump

```bash
gdb -c core.dump
(gdb) info registers
(gdb) x/100x $rsp
(gdb) find 0x0, 0xffffffff, "flag"
```

**coredump 里栈与堆都在**，`find` 直接搜字符串往往最快。`info proc mappings` 能看加载了哪些库，判断是哪个程序崩的。

## 7. KAPE triage

已采集好的 Windows 痕迹集，不需要整盘镜像。

```text
C/Users/<user>/AppData/Local/Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt
C/Users/<user>/NTUSER.DAT
C/Windows/System32/config/{SAM,SYSTEM,SOFTWARE}
C/Windows/appcompat/Programs/Amcache.hve
C/$MFT
```

**性价比顺序**：PowerShell 历史（最快命中）→ Amcache（执行时间线 + SHA-1）→ `$MFT`（小文件常驻数据）→ 注册表 hive（持久化与凭据）。

```bash
cat "C/Users/*/AppData/Local/Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt"
python3 -c "
from regipy.registry import RegistryHive
reg = RegistryHive('C/Windows/appcompat/Programs/Amcache.hve')
for e in reg.recurse_subkeys(as_json=True): print(e)
" | grep -i "flag\|suspicious"
```

**`$MFT` 里小于约 700 字节的文件内容直接内联在记录里**，所以对 `$MFT` 裸搜字符串就能拿到小 flag 文件：

```python
import re
print(re.findall(rb'utflag\{[^}]+\}', open('$MFT','rb').read()))
```

**小 `.dmp` 可能是 minidump**，`strings -a` 捞环境变量块比上 Volatility 快。

## 8. PowerShell 勒索

1. 从 minidump 提脚本块：`python power_dump.py powershell.DMP`，或 `strings powershell.DMP | grep -A5 "function\|Invoke-"`
2. 认加密：通常是 AES-CBC + SHA-256 派生
3. pcap 里过滤 SMTP，导出附件后 base64 解码
4. 内存里找密钥：`strings powershell.DMP | grep -E '^[A-Za-z0-9]{24}$' | sort | head`

**`Get-Random` 生成的密钥就是定长字母数字**，正则 `^[A-Za-z0-9]{24}$` 是最有效的捞法。压缩包密码同样这么找。

## 9. Android

```bash
adb pull /data/app/com.target.app/base.apk
apktool d base.apk -o decompiled/
adb backup -apk -shared -all -f backup.ab && java -jar abe.jar unpack backup.ab backup.tar

sqlite3 contacts2.db ".tables"
sqlite3 mmssms.db "SELECT * FROM sms"
```

镜像里的关键位置：`/data/data/<pkg>/databases/`（SQLite）、`/data/data/<pkg>/shared_prefs/`（XML，硬编码密钥常在这）、`/data/system/packages.xml`、`/data/misc/wifi/wpa_supplicant.conf`（Wi-Fi 密码）。

**`shared_prefs` 优先于 `databases`**：前者是明文 XML，后者要解表结构。

## 10. 容器

```bash
docker save IMAGE:TAG -o image.tar && tar xf image.tar
docker history IMAGE:TAG --no-trunc
docker create --name extract IMAGE:TAG && docker export extract -o fs.tar && docker rm extract
dive IMAGE:TAG
```

**后一层删掉的文件仍在前一层的 `layer.tar` 里**，`.wh.` 前缀的 whiteout 文件只是标记删除。`--no-trunc` 才能看到完整 Dockerfile 指令（`ARG`/`ENV` 里的密钥）。

**注意 `docker history` 与 image config 的 `history[].created_by` 是同一份数据**——即使没有 docker 命令，解 tar 读 config blob 也能拿到（详见 [[forensics/系统取证/linux-forensics.md|Linux 与应用取证]]）。

## 11. 云存储

```bash
aws s3 ls s3://target-bucket/ --no-sign-request
aws s3api list-object-versions --bucket target-bucket --no-sign-request
aws s3api get-object --bucket target-bucket --key secret.txt --version-id VID out.txt

gsutil ls gs://target-bucket/
az storage blob list --container-name target --account-name storageaccount
```

**versioning 是唯一入口**：对象被「删除」只是加了个 delete marker，旧版本仍可通过 `--version-id` 取回。**先 `list-object-versions` 再下载**，直接 `ls` 看不到已删对象。

## 12. BSON

```python
import bson, struct, base64
data = bytearray(open('data.bson','rb').read())
# 头部 4 字节是小端文档总长；缺字节时按 len 补
data = struct.pack('<I', len(data) + 3)[1:] + data
docs = bson.decode_all(bytes(data))
chunks = sorted(docs, key=lambda d: d.get('index', d.get('i', 0)))
reconstructed = b''.join(base64.b64decode(c.get('data', c.get('d',''))) for c in chunks)
```

**判据**：文件开头 4 字节解成小端整数后，与文件长度接近但不相等 → 头部长度字段坏了或缺了几字节。BSON 没有 magic，只能靠这个长度关系认。

## 13. TrueCrypt / VeraCrypt

**识别特征**：无 magic（设计如此）、大小恰为 512 的整数倍、整文件高熵。上下文线索（相关图片里有 TrueCrypt logo）往往比技术特征更可靠。

```bash
veracrypt -t -p "password" volume.tc /mnt/vc
veracrypt -t -p "" -k keyfile.png volume.tc /mnt/vc     # 仅 keyfile
veracrypt -t --truecrypt -p "password" volume.tc /mnt/vc # 旧 TC 卷
```

**隐藏卷用不同密码挂同一个文件**——同一个 `.tc` 用两个密码能开出两套内容，这是它区别于普通加密容器的核心特性。

## 14. 已删文件：MFT 记录直取

```bash
vol.py -f Challenge.raw --profile=Win7SP1x86 mftparser | grep -A2 "target_filename"
vol.py -f Challenge.raw --profile=Win7SP1x86 mftparser --offset=0x7ca3c00 --dump-dir=./out/
ls ./out/     # file.data.$DATA 就是内容
```

**NTFS「删除」只是把 MFT 记录头 `0x16` 字节的 in-use 位翻成 0**，`$DATA` 属性完整保留直到记录被复用。所以 `filescan`+`dumpfiles` 失败不代表拿不到——用 `mftscan` 找记录偏移再 `--offset` 直取。

**小于约 700 字节的文件是常驻的**（内容内联在 MFT 里），`--offset` 就能完整 dump；更大的文件要靠 cluster run 做 `icat`/`dd`。

## 15. 无 magic 压缩

```python
import brotli
try:
    out = brotli.decompress(blob)
    if b'rrrooottl' in out or b'Brotli' in out:
        print('Brotli')
except Exception:
    pass
```

Brotli 没有固定 magic，binwalk 与 `file` 都认不出。**判据是试解压**：`brotli` → `zstd` → `snappy` → `lzma-alone` 逐个试，能解且不抛异常就是它。

## 16. 其他速查

- **PDF 同 MD5 不同内容**：`enscript | ps2pdf` 造基础 PDF → `pdf.py`（corkami/pocs）+ `fastcoll`。选前缀碰撞（内容可见不同）要 `hashclash`，10-20 CPU 小时。
- **内存 dump 的字符串雕刻**见 [[forensics/磁盘与内存/disk-advanced.md|进阶磁盘与内存]]。

## 17. 纪律

- **先认容器再选工具**。`.ova` 是 TAR、`.vmdk` 能用 7z、`.tar` 可能是镜像也可能是容器——不认就上工具会浪费大量时间。
- **Volatility 失败不等于没数据**。profile 不匹配时原始帧缓冲、MFT 残留、`strings` 都还能用，且往往比修 profile 快。
- **删除了就去 MFT / 旧 layer / 旧 version 找**。三个地方的「删除」语义都是「标记」，不是「抹掉」。
- **`fls` 在 `photorec` 之前**。前者精确到 inode，后者无差别扫描；先精确后暴力，能省掉大量噪声判读。
