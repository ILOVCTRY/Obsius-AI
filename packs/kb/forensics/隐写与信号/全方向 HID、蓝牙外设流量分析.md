---
title: USB/HID 与蓝牙外设流量分析
phase: forensics
vuln_class: [usb, hid, peripheral]
summary: 拿到 USB/蓝牙抓包先按本表穷举：设备类型→传输类型→载荷格式，HID 键码/鼠标轨迹/存储/自定义协议逐项排除。含 tshark 字段真名与「输出为空」的判定顺序（-c 陷阱/rc 三义/MSC 载荷归属）。
---

# 全方向 HID、蓝牙外设流量分析

**用法**：USB 抓包不是「只有键盘一种」。先判**设备类型**，再判**传输类型**，最后才解载荷。
判错设备类型就会拿键盘解码器去解鼠标数据，白费时间。

## 0. 先判设备类型（决定后面全部路线）

```bash
# 列出抓包里出现过的所有 USB 设备
tshark -r cap.pcapng -Y "usb.device_address" -T fields -e usb.src -e usb.dst | sort -u
# 看设备描述符（厂商/产品字符串直接暴露设备类型）
tshark -r cap.pcapng -Y "usb.bDescriptorType==1" -V | grep -E "idVendor|idProduct|iProduct"
```

| 设备类型 | 判据 | 典型题型 |
|---|---|---|
| HID 键盘 | `bInterfaceClass=3`，报告 8 字节 | 敲击内容、LED 摩斯、方向键导航 |
| HID 鼠标/画笔 | `bInterfaceClass=3`，报告 4-7 字节，含相对位移 | 轨迹还原成图/文字 |
| HID 游戏手柄 | 报告含多个按钮位 + 摇杆 | 按键序列 → 编码 |
| USB 存储（U盘） | `bInterfaceClass=8`，`SCSI`/`BOT` 命令 | 提取文件、删除恢复 |
| USB 网卡/串口 | `bInterfaceClass=2/10` | 协议还原 |
| 打印机/扫描仪 | `bInterfaceClass=7` | 打印内容还原 |
| 蓝牙 | `btatt`/`btrfcomm` 协议 | RFCOMM 重组、ATT 特征值 |

## 1. 按设备类型的全部方向

### 1.1 HID 键盘

| 方向 | 判据 | 动作 |
|---|---|---|
| 明文敲击 | 报告 8 字节，byte2-7 为键码 | 键码表映射，注意 Shift（byte0 bit1） |
| 大小写/符号 | 同一键码两种字符 | 结合 byte0 修饰位判 |
| 方向键导航 | 只有方向键码（0x4F-0x52） | 还原光标轨迹（可能是迷宫/编辑器操作） |
| LED 摩斯 | 输出报告（host→device）的 LED 位有规律闪烁 | 按时序转摩斯（长短间隔） |
| 组合键/快捷键 | 多键同时按下 | 还原为 Ctrl+X 等操作序列 |
| 输入节奏编码 | 击键间隔本身承载信息 | 提取时间差序列 |
| 结果串里有孤立怪字符（`flag{}inKJ`） | 退格被忽略，需模拟编辑 | 重放 Backspace/DEL/方向键，见下 |

**键盘报告格式**（8 字节）：`byte0` 修饰键（bit0=LCtrl, bit1=LShift, bit2=LAlt, bit3=LGUI…），
`byte1` 保留，`byte2-7` 最多 6 个同时按下的键码。**0x00 表示该槽位空**。

#### 编辑键必须模拟，不能只做键码映射

直接 `键码 → 字符` 拼串会得到一个「带着退格没生效」的错误串。真实输入过程里 Backspace 是**删除前一个字符**，而 `DEL`（0x4C）删的是**后一个字符**（前进删除），两者语义相反。

```python
BACKSPACE, DEL = 0x2A, 0x4C

def replay(keys):                     # keys: 按时间顺序的键码
    buf, pos = [], 0
    for k in keys:
        if k == BACKSPACE:
            if pos > 0:
                buf.pop(pos - 1); pos -= 1
        elif k == DEL:
            if pos < len(buf):
                buf.pop(pos)
        elif k == 0x50:               # Left
            pos = max(0, pos - 1)
        elif k == 0x4F:               # Right
            pos = min(len(buf), pos + 1)
        elif k in (0x4B,):            # PageUp 之类，按需
            pass
        else:
            buf.insert(pos, KEYMAP.get(k, '?')); pos += 1
    return ''.join(buf)
```

**判据**：原始串里出现 `flag{}inKJ` 这种「花括号里没内容、后面跟着怪字符」的形态，说明**有退格被忽略**。上面的例子正确重放后是 `flag{inop97bc6g9}`——被删掉的 `iloveyou`、`KJ`、`ya` 恰好是干扰项。

**必须同时处理的三件事**：

1. **Backspace 与 DEL 方向相反**，别合并成一个。
2. **光标位置**：有方向键时位置会移动，退格删的是「光标前」而非「串尾」。
3. **修饰键持续按下**：修饰位在按住期间每个报告都置位，不是只在按下那一帧。

**验证方法**：重放结果里若残留 `{}` 之外的可疑片段（如 `inKJ`），把那段前后的键码单独打印出来核对——`0x2A` 是否被漏处理。

### 1.2 HID 鼠标 / 画笔 / 数位板

| 方向 | 判据 | 动作 |
|---|---|---|
| 轨迹还原成图 | 报告含 dx/dy 相对位移 | 累加坐标后绘制 |
| 多图层绘制 | 报告含 mode/pen 字段 | 按 mode 分色分别绘制（叠加才能读出内容） |
| 按钮状态编码 | button 字节有规律 | 按按钮状态分段 |
| 滚轮数据 | byte6 非零 | 滚轮值可能是独立信道 |
| 绝对坐标设备 | 报告含绝对 X/Y（数位板） | 直接用绝对坐标，不累加 |

**鼠标报告格式**（常见 7 字节）：`byte0` 按钮，`byte1` 模式/填充，`byte2-3` dx(int16 LE)，
`byte4-5` dy(int16 LE)，`byte6` 滚轮。**注意 dx/dy 是有符号 16 位**，直接当无符号会画出乱线。

### 1.3 USB 存储

| 方向 | 判据 | 动作 |
|---|---|---|
| 文件提取 | 有 SCSI READ(10) 命令 | 按 LBA 重组扇区 → 挂载/雕刻 |
| 删除恢复 | 有 READ 但文件系统标记已删 | 走磁盘恢复流程（见 disk-recovery） |
| 隐藏分区 | 容量与分区表不符 | 手工解析分区表 |

### 1.4 蓝牙

| 方向 | 判据 | 动作 |
|---|---|---|
| RFCOMM 串口数据 | `btrfcomm` 帧 | 重组字节流 |
| ATT 特征值读写 | `btatt` 有 handle/value | 按 handle 分组还原 |
| 配对/密钥 | SMP 协议 | 提取密钥后解密后续流量 |

## 2. 通用提取命令

```bash
# 提取全部 URB 载荷（HID 等中断设备最常用）
tshark -r cap.pcapng -T fields -e usb.capdata | grep -v '^$'

# 只取中断传输（HID 走这个）
tshark -r cap.pcapng -Y "usb.transfer_type==1" -T fields -e usb.capdata

# 控制传输（设备描述符、LED 输出报告）
tshark -r cap.pcapng -Y "usb.transfer_type==2" -T fields -e usb.capdata
```

`usb.capdata` 输出是**连续十六进制、无冒号**（`0200040000000000`），直接 `bytes.fromhex()` 即可。
若见到冒号分隔（`02:00:04:...`）那是 Wireshark GUI 的显示格式，`-T fields` 不会产生。

## 2.1 取不到载荷时的判定顺序（2026-10-20 实测重写）

**先别断言「环境不支持」。** 按下面四步走，每步都有确定判据：

### ① `-c N` 与 `-Y` 不能混用（最常见的假空）

`-c` 在**读取阶段**生效：先只读前 N 帧，**再**做显示过滤。所以

```bash
tshark -r cap.pcapng -Y "usb.endpoint_address==0x01" -c 3 -V   # ✗ 几乎必然空
```

只要前 3 帧端点不是 `0x01` 就必然空输出、`rc=0`、无任何报错。实测某题：前 3 帧全为 `0x80`，
该命令输出 0 行；去掉 `-c 3` 后同一过滤器匹配 **522 帧**。

```bash
# 要限量：过滤后用 head
tshark -r cap.pcapng -Y "usb.endpoint_address==0x01" -V | head -80
# 或者只取字段再 head
tshark -r cap.pcapng -Y "usb.endpoint_address==0x01" -T fields -e usb.capdata | head -20
```

**`-c` 只表达「我就要前 N 帧」这一个语义，永远不要拿它做「取样探查」。**

### ② 十六进制过滤器写法没问题，别甩锅给它

实测等价（同一文件命中帧完全相同）：

| 写法 | 结果 |
|---|---|
| `usb.endpoint_address==0x81` | ✅ 与 `==129` 一致 |
| `usb.endpoint_address==0x02` | ✅ 与 `==2` 一致 |

**不要写「hex 写法静默失败、要换十进制」这类经验**——它会让人放弃合法写法，掩盖真正的 `-c` 问题。

### ③ 空 stdout 必须结合 rc 判语义（见 §2.2）

### ④ MSC（U 盘）设备的载荷不在 `usb.capdata`，这是**正常**的

USB Mass Storage 的 bulk 数据会被 **USBMS dissector 当作 SCSI Payload 消费掉**，
`usb.capdata` / `usb.data_fragment` 全为空是解码正确的结果，不是 dissector 失能。

```bash
# 确认是 MSC 设备
tshark -r cap.pcapng -T fields -e usb.bInterfaceClass | sort -u   # 0x08 = Mass Storage

# 看 bulk 帧的真实结构：-V 里会出现 SCSI Payload 子树
tshark -r cap.pcapng -Y "usb.transfer_type==0x03" -V | grep -A5 "SCSI Payload"
```

**关键区分**：
- **SCSI 元数据（CDB 参数）可以导出** —— 见 §2.3，字段名带命令集前缀。
- **SCSI 数据载荷没有可导出字段**（`scsi.data` / `usbms.data` 都不存在），只能
  `-V` 读文本树，或按 USBPcap 27 字节 pseudoheader 手工切 payload。

## 2.2 空 stdout 的三种语义（tshark 专用判定）

| stdout | rc | 含义 | 该做什么 |
|---|---|---|---|
| 空 | 0 | **查询成功但无匹配**（过滤器/字段组合没命中） | 检查 `-c`、过滤器值、字段名 |
| 空 | 1 | **字段名无效**（`-e` 写错，整条查询未执行） | 查真名（§2.3） |
| 空 | 2 | **输入文件读不了**（不存在/权限/格式） | 查路径与 `file` 输出 |

`-e` 无效是**原子失败**：`-e 有效 -e 无效` 混写也 `rc=1` 且 stdout 一字节不出（与顺序无关）。

```bash
# ✗ 禁止对 tshark 用裸 2>/dev/null —— 会把唯一的诊断信息吞掉
tshark -r f -e usb.urb_func 2>/dev/null        # 看到"成功但空"，其实是字段名错

# ✓ 先取 rc 再分流
out=$(tshark -r f -T fields -e "$FIELD" 2>&1); rc=$?
[ $rc -eq 0 ] || echo "rc=$rc 不是无匹配，是查询失败：$out"
```

## 2.3 查字段真名：用 PDML，不要用 `-V`

`-V` 只输出**给人读的 showname**（`Logical Block Address (LBA): 4660`），拿不到机器字段名。
`-T pdml` 才有 `name="..."`：

```bash
tshark -r cap.pcapng -c 1 -T pdml | grep -o 'name="[^"]*"' | sort -u
```

`-G fields` 是 **tab 分隔**，按空格 `awk` 会列漂移（`$3` 可能是 blurb 的碎片）：

```bash
tshark -G fields | awk -F'\t' '$1=="F" && $3 ~ /^scsi/ {print $3}'   # ✓
tshark -G fields | grep -P '\tscsi_sbc\.rdwr10\.lba\t'               # ✓
```

**USBPcap 常见字段真名（4.0.17 实测）**：

| 想拿什么 | 错误写法（不存在） | 正确字段名 |
|---|---|---|
| SCSI Read(10) 的 LBA | `scsi.cdb.lba`、`scsi.lba` | **`scsi_sbc.rdwr10.lba`** |
| SCSI Read(10) 传输块数 | `scsi.cdb.len` | **`scsi_sbc.rdwr10.xferlen`** |
| SCSI opcode | `scsi.cdb.opcode` | **`scsi_sbc.opcode`** |
| SCSI LUN | `scsi.lun_id` | **`scsi.lun`** |
| CBW 签名 | `usbms.signature` | **`usbms.dCBWSignature`** |
| CBW Tag | `usbms.tag` | **`usbms.dCBWTag`** |
| CSW 签名 / 状态 | `usbms.csw_*` | **`usbms.dCSWSignature` / `usbms.dCSWStatus`** |

`usbms.*` 全部只有 15 个：`dCBW*`（7）+ `dCSW*`（3）+ `setup.*`（5）。
SCSI 字段名一律带命令集前缀：`scsi_sbc.*`（块设备 SBC）/ `scsi_mmc.*`（多媒体）/ `scsi.cdb.*`（通用位域）。

```bash
# 一次性导出 SCSI 关键参数（实测可正常出值）
tshark -r cap.pcapng -T fields \
  -e frame.number -e usbms.dCBWTag -e scsi_sbc.opcode \
  -e scsi_sbc.rdwr10.lba -e scsi_sbc.rdwr10.xferlen -e scsi.lun
```

## 2.4 `io,phs` 出现 `text` 嵌套 ≠ dissector 失能

`text` 是 **"Line-based text data"** 的短名，出现在 USB **控制传输的枚举描述符**
（端点 `0x00` / `0x80`）上属正常解码。实测某题 41 个 `text` 帧全部是端点 `0x80`/`0x00`
的 GET_DESCRIPTOR，`_ws.col.Protocol` 仍是 `USB`。

**不要据此判断「dissector 没识别这份抓包」**，那是错误归因。真要确认解码是否正常，
看 `usb.bInterfaceClass`、`usbms.*`、`scsi_sbc.*` 有没有出值。

## 2.5 重建大文件前必须查 snaplen 截断

抓包时 snaplen 若设为 65535，而 USBPcap 的「包长」口径**含 27 字节 pseudoheader**，
则每满 64KB 的 URB 会丢 28 字节（27 + 1 对齐）：

```bash
# 有差异帧 = 数据先天缺块，别去怀疑文件系统链损坏
tshark -r cap.pcapng -T fields -e frame.number -e frame.cap_len -e frame.len \
  | awk -F'\t' '$2!=$3 {print; n++} END{print "差异帧=" n}'
```

实测某题：51 帧 `cap_len=65535 / len=65563`，差 28 字节。从这种 pcap 重建磁盘镜像时，
FAT 链会表现为「只有首簇，fat(首簇)=EOF」，看起来像**文件链损坏**——真因是抓包截断。
**先对 `usb.data_len` 与实得 payload 长度，再决定要不要去翻未分配区。**

## 3. 判据速查

- 报告**长度**先判设备：8 字节 → 键盘，4/7 字节 → 鼠标，其他 → 查描述符。
- 数据里**大量 0x00** → 多为键盘空闲槽位，说明是敲击流。
- **dx/dy 同时为 0 且按钮为 0** → 鼠标悬停帧，绘制时跳过。
- 键盘解出**乱码**但键码合法 → 大概率没处理 Shift 或键盘布局（QWERTY vs DVORAK）。
- 键盘解出**全是方向键** → 不是打字题，是导航题（迷宫/光标移动）。
- `usb.capdata` **全空先看 `bInterfaceClass`**：`0x08`（MSC）走 SCSI 子树，属正常（§2.4 ④）。
- 命令**输出空但 rc=0** 先查 `-c` 与过滤器值，**别归因为「环境不支持」**（§2.1 ①）。


## 4. 交叉引用

- 通用网络流量（DNS/TCP/ICMP/SMB/RADIUS）：[[forensics/网络取证/高级网络取证与隐蔽信道.md|network-advanced.md]]
- 音频/频谱/摩斯/DTMF：[[forensics/隐写与信号/硬件信号与物理介质取证.md|signals-and-hardware.md]]
- 磁盘镜像与删除恢复：[[forensics/磁盘与内存/磁盘数据恢复与容器链提取.md|disk-recovery.md]]
