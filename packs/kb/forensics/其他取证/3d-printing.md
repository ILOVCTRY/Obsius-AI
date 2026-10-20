---

title: 3D 打印与 CAD 文件取证
phase: forensics
vuln_class: [forensics, gcode, qoi, file-format]
summary: `.g` 不是文本 G-code 而是 PrusaSlicer 的 bgcode 二进制块格式（magic `GCDE`）。判据是文件头，然后逐块解压 heatshrink/deflate，flag 常在注释、metadata 或缩略图里。
---

# 3D 打印与 CAD 文件取证

图片格式与元数据通用手法见 [[forensics/隐写与信号/图片隐写.md|图片隐写]]；非图片隐写见 [[forensics/隐写与信号/steganography.md|非图片隐写]]。

## 0. 判据速查

| 看到什么 | 结论 | 去哪节 |
|---|---|---|
| 文件头 `GCDE` | PrusaSlicer 二进制 G-code（bgcode） | §1 |
| 扩展名 `.g` / `.bgcode` 但 `cat` 出二进制 | 同上 | §1 |
| 块内压缩类型 1 | Deflate（zlib） | §1 |
| 块内压缩类型 2/3 | Heatshrink（窗口/前瞻 11-4 或 12-4） | §1 |
| 缩略图格式 2 | QOI（`qoif` magic） | §2 |
| G-code 文本里有 `;TYPE:Custom` 或可疑注释 | 分层注释藏 flag | §3 |
| G-code 正常但看不到 flag | 画 XZ/YZ 侧视图 | §4 |

## 1. PrusaSlicer bgcode

**magic `GCDE`（4 字节）**，之后是版本（4）和校验类型（2）。

```text
头部: "GCDE"(4) + version(4) + checksum_type(2)
块:   type(2) + compression(2) + uncompressed_size(4)
      [+ compressed_size(4) 仅压缩时]
      + 类型专属字段
      + data + CRC32(4)
```

**块类型**：

| type | 内容 | 额外头字段 |
|---|---|---|
| 0 | FileMetadata | encoding(2) |
| 1 | GCode | encoding(2) |
| 2 | SlicerMetadata | encoding(2) |
| 3 | PrinterMetadata | encoding(2) |
| 4 | PrintMetadata | encoding(2) |
| 5 | Thumbnail | format(2) + width(2) + height(2) |

**压缩**：0=None / 1=Deflate / 2=Heatshrink(11,4) / 3=Heatshrink(12,4)
**缩略图**：0=PNG / 1=JPEG / 2=QOI

```python
import struct, zlib
import heatshrink2      # pip install heatshrink2

data = open('file.g', 'rb').read()
pos = 10                # 跳过头部
while pos < len(data) - 8:
    block_type = struct.unpack('<H', data[pos:pos+2])[0]
    compression = struct.unpack('<H', data[pos+2:pos+4])[0]
    uncompressed_size = struct.unpack('<I', data[pos+4:pos+8])[0]
    pos += 8
    if compression != 0:
        compressed_size = struct.unpack('<I', data[pos:pos+4])[0]
        pos += 4
    else:
        compressed_size = uncompressed_size
    if block_type in (0, 1, 2, 3, 4):
        pos += 2        # encoding
    elif block_type == 5:
        pos += 6        # format + width + height
    block_data = data[pos:pos+compressed_size]
    pos += compressed_size + 4      # data + CRC32

    if block_type == 1:
        if compression == 3:
            gcode = heatshrink2.decompress(block_data, window_sz2=12, lookahead_sz2=4)
        elif compression == 1:
            gcode = zlib.decompress(block_data)
```

**两个容易漏的点**：`compressed_size` 只在压缩时存在（未压缩块里它就是 `uncompressed_size`）；每块尾部有 4 字节 CRC32，跳它的时候别少算。

## 2. QOI 缩略图

**magic `qoif`（4）+ width(4 BE) + height(4 BE) + channels(1) + colorspace(1)**

PrusaSlicer 的缩略图常用它。用 `qoi` 库或手写 struct 解，解出来直接看图——**缩略图里嵌 flag 是很常见的偷懒出题方式**。

## 3. G-code 藏点

```bash
grep -i "flag\|meta\|ctf\|secret" output.gcode
grep ";.*FLAG\|;.*LAYER_CHANGE" output.gcode
```

四个常见位置：

1. **分层注释**：`;=== FLAG_CHAR ... ===` 出现在特定层高处
2. **自定义段**：`;TYPE:Custom`
3. **元数据字段**：对象名、耗材信息
4. **缩略图**：见 §2

## 4. 侧视图可视化

G-code 本质是坐标列表。**挤出段在某个 Y 区间内会拼出文字**，画 XZ 或 YZ 投影就能看见浮雕/刻字。

```bash
grep "^G1" output.gcode | awk '{print $2, $3}' > coords.txt
# 然后用 matplotlib 画 XZ（第 1 列 vs 第 3 列），并按 Y 过滤
```

**不要只看俯视 XY 图**——文字往往是竖着刻在侧面或底面的，投影轴选错就什么都看不到。

## 5. 不常见 magic 速查

| magic | 格式 | 扩展名 | 注 |
|---|---|---|---|
| `GCDE` | PrusaSlicer bgcode | `.g`, `.bgcode` | heatshrink 压缩 |
| `qoif` | Quite OK Image | `.qoi` | 轻量图片，常被嵌入 |
| `OggS` | Ogg 容器 | `.ogg` | 音视频 |
| `RIFF` | RIFF 容器 | `.wav`, `.avi` | 看子格式 |
| `%PDF` | PDF | `.pdf` | 查元数据与内嵌对象 |

## 6. 纪律

- **先看 magic 别看扩展名**。`.g` 是二进制不是文本，`cat` 出乱码就该换解析器而不是换编码。
- **QOI 和 heatshrink 都要装库**，手写解码器容易在索引/差分那几步错位。
- **G-code 的答案是坐标，不是文本**。文本 grep 不到就立刻画图，别继续 grep。
- **侧视图三个轴都试**。XY 看不到就 XZ、YZ，文字可能在底面或侧面。
