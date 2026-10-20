---
title: 硬件信号与物理介质取证
phase: forensics
vuln_class: [hardware-signal, uart, i2c, vga, hdmi, displayport, side-channel, physical-media]
summary: 硬件题先确认采样时钟、极性、位序、帧边界和信道编码，再恢复数据；缺同步信号时不要盲目假设默认波特率、色彩深度或行宽。
---

# 硬件信号与物理介质取证

通用音频处理见 [[forensics/隐写与信号/steganography.md|隐写与信号]]；外设抓包（USB/蓝牙）见 [[forensics/隐写与信号/peripheral-capture.md|外设抓包分析]]。

## 0. 判据速查

| 输入 | 先确认 | 常见误判 |
|---|---|---|
| 原始电平/逻辑分析仪 CSV | 采样率、极性、位序、时钟源 | 不识别时钟边直接按时间等分 |
| 波形音频 | 波特率、起始/停止位、是否需要放大滤波 | 用默认 9600 解码非 UART 音频 |
| 逐字节采集的显示帧 | 每样本字节数、同步位、板内/板外分辨率 | 忘了裁剪 blanking 或忽略位深差异 |
| 10-bit 符号流 | 是 8b/10b 还是 TMDS，disparity/LFSR 是否复位 | 把 TMDS 规则套到 DisplayPort |
| 功耗/电磁轨迹 | 对齐、泄漏点、样本坐标轴含义 | 不降噪直接按单轨迹 argmax |
| 键盘/敲击音频 | 事件检测、特征窗、参考标注 | 用平均谱代替单次瞬态 |
| 光学介质/物理载体 | 是否有前向纠错交织、几何参数 | 不做去交织直接渲染 |

## 1. 逻辑电平与 UART

### 1.1 从电平恢复信号

逻辑分析仪 delta 时间序列表示状态翻转间隔；先累积成时间轴，再按电平段切片。UART 空闲是高电平，起始位是下降沿，数据通常 LSB first，接着停止位：

```python
def uart_bits(durations, states, initial_state, samples_per_bit, msb=False):
    """从电平段恢复 bit；durations/states 为电平段序列。"""
    bits = []
    for dur, st in zip(durations, states):
        n = max(1, round(dur / samples_per_bit))
        bits.extend([st] * n)
    return bits
```

解码前必须确定：极性是否反相、波特率、每帧位数、是否有校验/多位停止位、字节间是否有任意长空闲。自动判定通常用最短脉冲估每 bit 数，再用模板匹配起始位；若结果全 `0xFF`/`0x00` 或同步错乱，优先检查极性和波特率。

Saleae `.sal` 等专用格式按各自文档解析 delta；外部导出 CSV 与内部二进制编码不同，不能用同一解析器混读。输出常还有一层 base64/压缩，先看是否有可识别 magic。

### 1.2 WAV 中的 UART

音频采样不能直接把幅度中值当电平：先确认是否单声道、有无关断、削波、AGC 和带内噪声。常用流程是带通/低通→包络或阈值化→找下降沿→按估得 bit period 在 bit 中心采样。多速率候选应分别解码，并用可打印率、已知前缀和校验位筛选。

## 2. I²C 与总线协议

I²C 是双线总线：SCL 时钟、SDA 数据，START 为 SCL 高时 SDA 下降，STOP 为 SCL 高时 SDA 上升。每个字节 8 位后跟 ACK，地址通常 7 位 + R/W。提取步骤：检测起止条件→按时钟沿采样→按 ACK 分组→按地址过滤重复 start（restart）后的读写方向。

设备地址可能复用，传输方向决定数据含义；CMD/寄存器索引常紧跟在地址后。把消息按时间和地址排成事务流，再用 payload magic 或 ASCII 判断；不要把 SDA 采样点选在时钟跳变处。

## 3. 显示信号：VGA、HDMI、DisplayPort

### 3.1 VGA

常见结构是每像素 5 字节（R、G、B、HSYNC、VSYNC），整帧含消隐区。先确认总宽高与有效区，再按有效区裁剪。颜色可能是 6-bit，看起来偏暗要检查是否乘 4 或移位到 8-bit：

```python
import numpy as np
from PIL import Image

bytes_per_sample = 5
total_h, total_w = 525, 800
active_h, active_w = 480, 640

raw = np.fromfile("vga.bin", dtype=np.uint8)
if raw.size % bytes_per_sample:
    raw = raw[: raw.size // bytes_per_sample * bytes_per_sample]
samples = raw.reshape(-1, bytes_per_sample)
frame = samples.reshape(total_h, total_w, bytes_per_sample)
rgb = frame[:active_h, :active_w, :3].astype(np.uint16) * 4
Image.fromarray(rgb.clip(0, 255).astype(np.uint8)).save("vga.png")
```

同步位只用于定位，不属于像素；HSYNC/VSYNC 极性也需确认，先把同步位当作帧边界验证。

### 3.2 HDMI TMDS

10-bit TMDS 先按第 9 位决定是否取反，再用第 8 位选择 XOR/XNOR 链；解码从低位/高位顺序取决于位排列约定，必须用小样本或已知图案对齐。三个数据通道分别恢复为 R/G/B，再按行/帧重组。帧参数（total/active、时序）需从同步序列推断，不能假定 640x480。

### 3.3 DisplayPort

DisplayPort 数据通道是 8b/10b 符号，之后还可能有 scrambling。Transport Unit 每 64 符号中有数据列与开销列；LFSR dest scrambler 在遇到 control/BE/BS 等控制符号时复位。没有正确识别控制符号与复位点，输出会持续错乱。

8b/10b 解码必须跟踪 running disparity；只看 256 项主表会出现同一码字正反映射歧义。先确认 lane 数、速率控制/断链方式，再按 TS1/TS2 等训练序列同步。

## 4. 外部接口与时钟恢复

逻辑/总线数据常见形式是 CSV 多列电平或二进制 delta。无论哪种，先统一到时间轴，识别哪个列是时钟、哪个列是数据；只有单列时要从跳变估计时钟。采样点应稳定位于 bit 中段，受抖动影响时可用锁相/中值滤波。

Clock edge extraction 时注意：上升沿/下降沿约定、样本是否已重采样、是否存在多个时钟域。若同一份数据里出现两种以上 bit period，应拆分通信阶段（命令/响应/引导）。

## 5. 声学与功耗侧信道

### 5.1 键盘敲击声

先用能量包络检测事件，再对每个事件提取短时特征（MFCC、过零率、谱质心）。类别划分应以时间间隔和起始瞬态为主；较宽的窗口会混入释放噪声。用标注参考库做近邻/聚类时，保留全部单次样本而非平均谱，并报告混淆矩阵；不要只输出准确率最高的一版。

### 5.2 DPA/功耗轨迹

先理解数组形状：常见是 `位置×猜测×轨迹×样本`。降噪前确认单位、采样率和泄漏模型假设。把正确猜测从噪声中分开的通用做法是先按轨迹求平均，再找不同猜测间方差最大的时间点作为泄漏点，最后按泄漏点排序/分类。单点结论必须用多次运行、不同猜测集合和统计显著性复核；没有假设模型时，结论只能写“观测到某位差异显著”，不能直接断定 key。

## 6. 光学介质与物理载体

CD-DA 等光学介质可能带 CIRC 交织，直接渲染只能看到径向结构，角度信息被破坏。流程：确认文件采样布局→按已知参数去交织→用标定文件验证几何（每轨字节、半径、尺度）→再处理目标文件。标定样例是判断参数是否正确的关键证据，不要跳过。

没有标定文件时，先从可见的重复/校准图形反推参数，并只在试运行中比较亮度、对齐与可读性。密度归一化、总长度和填充边界都要记录；渲染结果出现明显偏亮/偏暗时先检查二值阈值与字节极性。

## 7. MIDI、敲击事件与遥控信号

USB MIDI/Launchpad 类是带时戳消息流：先解析事件类型（note on/off、CC），再按通道、音高和时间重建。多个 note 同时按下可能有不确定顺序，用时间戳排序并保留原始事件长度。

`.sub` 等射频文件包含脉冲时序和噪声字段。先按格式剥离头部与 batch 变量引用，再按 hint 或已知协议恢复 bit；不要把噪声字节当作有效样本参与 XOR/解码。

Linux `input_event` 是 24 字节定长记录（timeval、type、code、value）。只取 `EV_KEY` 且 `value==1` 的按键，映射用内核 `input-event-codes.h`；修饰键、大写、重复和补丁都要独立处理。

## 8. 纪律

硬件恢复流程固定为：确认采集参数→恢复信号/事件→解析协议/几何→恢复数据→按 magic/校验/标定复核。任何“复活”的数据都应能通过原始层的独立检查，包括已知图案、控制符号、标定样例或文件结构。缺少证据时明确写出假设（极性、波特率、位深、交织参数）以及假设失效时的失败表现。
