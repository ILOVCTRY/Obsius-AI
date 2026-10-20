---
title: 图像格式隐写与载体修复
phase: forensics
vuln_class: [steganography, image-format, lsb, dct, qr, jigsaw, png-repair, jpeg-forensics]
summary: 图像隐写先选载体层：格式元数据、像素位、辅助 chunk 或多帧容器都可承载数据；提取后必须用 magic、长度、校验或可读性复核，不能把“看起来有内容”当成功。
---

# 图像格式隐写与载体修复

通用隐写方法与音频/文本/终端见 [[forensics/隐写与信号/steganography.md|隐写与信号]]；FFT、SSTV、JPEG XL 等进阶手法见 [[forensics/隐写与信号/stego-advanced.md|图像隐写进阶]]；基础编码判定见 [[misc/编码解码/encodings.md|编码与解码基础]]。

## 0. 判据速查

| 输入特征 | 优先检查 | 常见误区 |
|---|---|---|
| JPEG 有更多 DQT 表 | 未被 SOF 引用的表 ID 2..15 的数值/LSB | 只看能否正常解码 |
| PNG 调色板项多于像素引用 | 未被引用的调色板项、各通道值 | 直接渲染主图 |
| JPEG 实际尺寸非 8 倍数 | 块边界 padding / slack 区 | 只按声明尺寸裁剪 |
| BMP/无损图可精确断言像素 | 逐 bitplane、三通道 parity | 只跑 zsteg 默认 bit0 |
| 两张同源图像 | 差值、survivor 索引、缩放/插值方式 | 先跑 LSB 再逐像素比对 |
| 碎片/多块图片 | 尺寸、EXIF、边缘匹配、拼图求解 | 手动拼 |
| PNG 打不开 | magic、chunk 名大小写、CRC | 只重写 magic 或只用 pngcheck |
| JPEG 花屏/部分损坏 | `0xFF` stuffing 规则、单比特翻转、thumbnail | 无条件整段替换 |
| 视频/多帧动画 | 帧间差分、每帧 palette/元数据 | 只分析单帧 |

## 1. 格式元数据承载

### 1.1 JPEG 未引用 DQT

JPEG 可以携带多个量化表（DQT, `0xFFDB`）。若某表未被 SOF 组件引用，渲染器会忽略它，但字节仍在文件里。先解析 marker 列表，比对表 ID 与组件 `Tq` 值；多余表中的数值或 LSB 可能就是载荷。解析时注意 precision 位（0=8 位、1=16 位）和表长度。

### 1.2 PNG 未使用调色板项与辅助 chunk

`PLTE` 最多 256 项，实际索引可能只用到其中一小部分。先统计像素索引集合，再检查未被引用的项：逐个通道看 R/G/B 是否落在可打印 ASCII 或明显结构化范围。Chunk 名大小写表示 crucial/ancillary/private/safe；小写开头的辅助 chunk 会被忽略但内容保留，`tEXt/iTXt` 同样可能藏线索。

### 1.3 JPEG slack 区

声明尺寸常向上取整到 8×8 块边界（253×195 → 256×200）。编辑器按声明尺寸裁剪，slack 像素仍在熵编码流中。用支持完整块解码的工具（`jpegtran -crop`、原始 DCT 解码或 `jpeg_uncrop`）扩展到真实块尺寸，再检查 padding 区的二值图案。是小黑块/白块组成的结构时，按格式头部（magic、长度、key、消息）解析，不要当噪声。

### 1.4 Header 残留字符串

`steghide` 等工具的口令有时被作者放在 JPEG APPn/JFIF 保留区。`exiftool`/`strings` 不一定会列出，`xxd` 前 256 字节常可直接看到 ASCII run。先 hexdump 再组合使用 steghide/outguess/stegseek；口令本身只是工具参数，后续 payload 仍需校验 magic。

## 2. 像素域：bitplane、parity 与条件载体

### 2.1 多 bitplane

LSB 工具默认看 bit0；二维码/结构图形常藏在 bit1 或 bit2。对每个通道逐 bitplane 生成二值图，检查 finder pattern、边缘连续性或可读性，再用 QR/OCR/统计确认。YCbCr 的亮度通道对比度更适合隐藏二值数据。

### 2.2 RGB parity

`(R+G+B) % 2` 是全通道 parity，与单通道 LSB 不同。先对整图统计 parity 分布，若在黑白区域外仍有长连续结构再提取。结合题面提示（"couples"、"adding colors"）决定方向。

### 2.3 条件载体像素

有时只有满足条件的像素承载数据（如 `R≤1 且 G≤1 且 B≤1`）。先统计各通道值域和二维联合分布，找出被大量使用又被特别标记的子集；对候选子集提取后再转换 bit流。不要对全图一律取 LSB，噪声会把信号埋掉。

### 2.4 坐标链

像素的 R 通道作数据，G/B 作下一个坐标，形成链表遍历；越界、零值和环必须显式处理。起点可能在元数据里，坐标也可能按 `(G*256+B)` 编码以支持宽图。输出应按可见 ASCII 与终止符验证；把三个通道都当 LSB 会破坏链结构。

### 2.5 seed 置换与多 bitplane 交织

像素访问顺序由固定 seed 决定（Fisher-Yates/`RandomState.shuffle`）。seed 常来自 EXIF、文件名、尺寸或题面数字；缺失时按小范围穷举。bitplane 可能交织（同一像素先取 bit0 再 bit1），恢复时按同一置换和交织顺序还原，再重组为目标图形。

## 3. 语义映射：掩码、缩放与嵌套图

### 3.1 缩略图作为选取掩码

JPEG 缩略图与原图像素一一对应时，暗像素可作为文字掩码：先用 `exiftool -b -ThumbnailImage` 导出缩略图，再用 OCR 获得文字网格，按 dark 像素取字符。先验证缩略图尺寸与文字行/列的对应关系，不要按“看上去能对上”就映射。

### 3.2 最近邻下采样

若隐藏图按固定间隔埋在高分辨率图中，用 nearest-neighbor 按该间隔下采样可提取；bilinear/bicubic 会混合像素造成破坏。先用肉眼或相关性确认间隔（常见 16、32），再验证输出为可识别图像。插值方式选错会得到半强度混合噪声，应换插值重试。

### 3.3 嵌套缩放存活点

多次 nearest-neighbor 缩放时，每级只保留源块中的一个像素，其索引由取整规则决定（可能形如 `10i+7`）。构造嵌套图像时要跟踪每个缩放级别后哪些原图像素会保留，再把隐藏图案覆盖到这些精确坐标。缩放置换参数改变会破坏隐藏图；完成后必须逐级缩放并与预期输出逐像素比对。

## 4. 结构重组：拼图、QR tiles 与碎片

### 4.1 通用拼图

尺寸、EXIF 尺寸字段、文件顺序先提供候选。碎片匹配可比较相邻边的像素差；贪心放置通常可用，困难时应加回溯和多目标（左右/上下同时考虑）。碎片过多时优先用模板求解器（如 `gaps` 遗传算法），比手拼快得多。

### 4.2 QR tiles

QR 有强结构约束：finder pattern 必须在三个角，timing pattern 位于 finder 之间，format info 也可作为锚点。先用这些结构固定角块方向，再对剩余 tile 排列；小网格（如 3×3）可直接穷举排列并逐个扫码验证。不要只按几何边匹配，旋转/镜像会增加 8 种方向。

### 4.3 从介质恢复碎片

磁盘镜像先用 `foremost/scalpel` 按 PNG/JPEG magic 提取，而不是手工 offset。提取后立刻看尺寸是否一致、CRC、EXIF 尺寸提示；不一致的碎片可能是干扰项或不同缩放副本。

## 5. 修复与爆破

### 5.1 PNG magic 与 chunk 大小写

PNG 有两个正交门槛：8 字节签名和被改成小写的关键 chunk 名。签名错位或 `idat/iend` 小写都会导致“空图”。先用 `pngcheck -v` 定位，再修 magic 和关键 chunk 名，逐项验证 CRC；修好后 `tEXt/iTXt/EXIF` 往往给出下一步线索。直接全局替换 chunk 名可能误伤 ancillary chunk 或文本内容，应只修关键 chunk。

### 5.2 JPEG 单比特翻转

JPEG 流中 `0xFF` 后必须跟 stuffing `0x00` 或 marker。统计 `0xFF` 后的字节分布可快速定位被破坏区域。对 <10KB 文件，单比特候选数约 `8×size`，可穷举；先尝试缩略图解码作为有效性筛子，再 OCR 筛选命中 flag 的候选。修改任何字节前保留原件和候选集合；熵编码流单比特翻转常导致后续花屏，不宜按“修好一个像素”理解。

### 5.3 GIF 多帧 metadata 载体

多帧格式可为每帧携带 palette/元数据，把二进制切片放进每帧 `PLTE`：重编码帧为 PNG 后逐帧取 `PLTE` body 拼接。这类载体绕过大多数 LSB 检测；同时应核对帧数、每帧长度和最终文件 magic。别把帧像素当作数据主体。

## 6. 时间域与多帧

逐帧差分可恢复时域信息：相邻帧同一像素 `+1` 可能表示 bit 1，未变表示 0；也可能按坐标序列触发。先统一帧尺寸、去重复帧和编码差异，再定义差分规则，最后按 Brainfuck/ASCII/文件 magic 验证。视频压缩、色度抽样和帧重排都会改变像素值，规则须在未压缩帧或原始帧上验证。

## 7. 纪律

先确定数据可能位于哪一层（格式元数据、像素位、辅助结构、时间域、组合结构），再选工具；每层处理保留中间产物和假设（bit 序、坐标起点、缩放比例、通道顺序）。任何提取结果至少通过 magic、长度校验、OCR 可读性或题目给定校验之一。题面提示只用于分流，不能替代字节级验证。
