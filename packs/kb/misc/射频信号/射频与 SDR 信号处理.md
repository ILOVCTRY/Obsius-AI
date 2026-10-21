---

title: 射频与 SDR 信号处理
phase: misc
vuln_class: [misc, rf, sdr, iq, qam, dsp]
summary: 拿到 IQ 文件先认格式（cf32/cs16/cu8），再画星座图。判据是星座形状——圆环=固定频偏、螺旋=频偏漂移、网格散点=同步正常只剩噪声。
---

# 射频与 SDR 信号处理

硬件信号（GPIO/MMIO/逻辑分析）见 [[forensics/隐写与信号/硬件信号与物理介质取证.md|信号与硬件]]；音频隐写见 [[forensics/隐写与信号/高级隐写：频域、音频与结构变换.md|进阶隐写]]。

## 0. 判据速查

| 星座图长什么样 | 结论 | 去哪节 |
|---|---|---|
| 圆环（点绕成圈） | 固定载波频偏 | §2 |
| 螺旋/外扩弧 | 频偏随时间漂移（伴随 AGC 抖动） | §2 |
| 网格上的团块 | 同步正常，只有噪声 | — |
| 4 个固定朝向的同一图案 | DD 载波恢复锁到了错误相位 | §2 |
| 完全随机 | 符号率或调制阶数猜错 | §1 |
| 符号率未知 | 做平方律循环平稳检测 | §1 |

## 1. 加载与预处理

**格式先认准**，读错 dtype 后面全废：

| 格式 | 含义 | 读法 |
|---|---|---|
| cf32 | 复浮点 32（GNU Radio 标准） | `np.fromfile(path, dtype=np.complex64)` |
| cs16 | 复有符号 16 位 | `np.fromfile(path, dtype=np.int16).reshape(-1,2)` 后 `I + jQ` |
| cu8 | 复无符号 8 位（RTL-SDR 原始） | 同上，`dtype=np.uint8` 并去直流 |

```python
import numpy as np
from scipy import signal

iq = np.fromfile('signal.cf32', dtype=np.complex64)

# 频谱：找占用频段
fft_data = np.fft.fftshift(np.fft.fft(iq[:4096]))
power_db = 20*np.log10(np.abs(fft_data)+1e-10)

# 循环平稳找符号率：平方律
x2 = np.abs(iq_filtered)**2
fft_x2 = np.abs(np.fft.fft(x2, n=65536))
# fft_x2 的峰值就是符号率 → samples_per_symbol = 1/peak_freq

# 频移到基带
t = np.arange(len(iq))
baseband = iq * np.exp(-2j*np.pi*center_freq*t)

# 低通隔离
lpf = signal.firwin(101, bandwidth/2, fs=1.0)
filtered = signal.lfilter(lpf, 1.0, baseband)
```

**`cu8` 必须先减 127.5 去直流**，否则整个星座图会偏移到一侧，看起来像「严重频偏」。

**带宽与符号率的关系**：`BW = Rs × (1 + α)`，`α` 是滚降因子（0 到 1）。反推 `Rs` 时要知道 `α`。

### 1.1 不是 SDR 原始采样：遥控器与编码芯片

**判据**：题目给的不是 `.cf32`/`.s8`，而是一串**脉宽**（微秒级）、或者 HackRF/逻辑分析仪抓的
单比特电平流，且载波在 315/433/868 MHz → 这是**固定编码芯片**的 OOK 信号，不是调制解调题。
不要套 SDR 那套载波恢复，直接按芯片协议解。

国内赛最常见两种（编码格式不同，**判据是同步头长度**）：

| 芯片 | 同步头 | 帧结构 | 判据 |
|---|---|---|---|
| EV1527 / PT2262 | 1 高 + 31 低（约 4:124 比例） | 20 bit 地址 + 4 bit 数据 | 帧长 24 bit |
| PT2242 | 4 bit 同步 + 20 bit 地址 + 4 bit 功能 + 1 bit 停止 | 单帧 29 bit | 帧长 29 bit，末位固定 |

**PT2242 的位编码**：每位用「高脉宽 : 低脉宽」的比例表示 0/1（典型 1:3 与 3:1）。
先把脉宽序列归一化成「短/长」二值，再按 4 位一组切同步码，剩下的按 20/4/1 切。

```python
# 脉宽序列 → 比特（阈值取中位数，抗抖动）
import statistics
w = [int(x) for x in open('pulses.txt')]        # 微秒
thr = statistics.median(w)
bits = ''.join('1' if x > thr else '0' for x in w)
print(len(bits), bits[:64])
```

**判据链**：切出的地址段**多次重复且完全一致** → 地址对，数据段才是要的；
地址每次都变 → 切错位置，调整同步头长度重切。

**逻辑分析仪多通道**（非射频但常一起考）：`.sr` / `.sal`（sigrok）用 `sigrok-cli` 导出 CSV 再解；
判据是**通道数 ≥2 且各通道波形周期成整数比** → 可能是并行总线（数据 + 时钟），
按时钟上升沿采样数据线，比按时间阈值切更可靠。

## 2. 载波与定时恢复

QAM-16 的核心难点是**载波频偏导致星座旋转**——点转成圈，判决全错。

### 2.1 判决导向（DD）二阶环

```python
carrier_bw = 0.02                     # 越大跟踪越快、噪声越大
damping = 1.0
theta_n = carrier_bw / (damping + 1/(4*damping))
Kp = 2 * damping * theta_n            # 比例增益
Ki = theta_n ** 2                     # 积分增益

carrier_phase = carrier_freq = 0.0
for raw_sample in samples:
    symbol = raw_sample * np.exp(-1j * carrier_phase)      # 按当前相位估计去旋转
    nearest = min(constellation, key=lambda p: abs(symbol - p))   # 判决
    error = np.imag(symbol * np.conj(nearest)) / (abs(nearest)**2 + 0.1)
    carrier_freq  += Ki * error
    carrier_phase += Kp * error + carrier_freq
```

**`+0.1` 是防止判决点接近原点时除零**，去掉会在 QAM 中心点附近炸掉。

### 2.2 Mueller-Muller 定时误差

```python
timing_error = (Re(y[n]-y[n-1]) * Re(d[n-1]) - Re(d[n]-d[n-1]) * Re(y[n-1])) \
             + (Im(y[n]-y[n-1]) * Im(d[n-1]) - Im(d[n]-d[n-1]) * Im(y[n-1]))
# y = 收到的符号，d = 判决结果（最近的星座点）
```

### 2.3 四重模糊

DD 恢复可能锁在 0/90/180/270 四个相位之一。**四种都试一遍**，看哪种解出的 bit 流有正确的帧结构或 flag 前缀。

### 2.4 AGC 归一化

```python
scale = np.sqrt(target_power / measured_power)
```

幅度不稳时先归一化，否则判决边界全是错的。

## 3. 匹配滤波的取舍

- 发送端说「RC 脉冲成形」→ 接收端**直接采样即可**，不需要匹配滤波器
- 发送端说「RRC」→ 接收端要**做一次 RRC 匹配滤波**

搞反的后果是引入额外 ISI，星座点模糊成一片。

## 4. 帧结构

常见排列：

1. 空闲/同步图案（链路空闲时循环）
2. 起始定界符（常是单个符号，如 0）
3. 数据载荷（QAM-16 按半字节：高 4 位在前）
4. 结束定界符（与起始同值）

**空闲图案里可能就含定界符的值**——靠上下文区分：它是 16 符号重复图案的一部分，还是独立出现。

## 5. 常见坑

- **GNU Radio 的 QAM-16 默认映射不是格雷码**。题给了星座图就照它，别套标准表。
- **星座成圆先怀疑频偏，不要先怀疑噪声**。噪声是让点变模糊，不是让它转圈。
- **符号率能靠循环平稳单独测出**，不必先知道调制阶数。
- **`cu8` 的直流偏置**会让所有判据都偏，先减再分析。

## 6. 纪律

- **先画星座图再动算法**。图上一眼能看出的东西（圆环/螺旋/网格）比任何指标都直观。
- **`cu8` 先去直流**。这一步漏掉，后面所有调参都是在补偿一个假频偏。
- **四重相位模糊必试全**。锁对了但转了 90°，解出来是乱码而不是失败。
- **`α` 决定带宽与符号率的关系**，反推时不能忽略。
- **报的是「频偏没收敛」还是「定时没收敛」**：前者星座成环、后者星座点在符号间拖尾——两者调的是不同的环。
