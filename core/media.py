"""图片探测与编码（vision 通道，2026-10-19）。

**为什么单独一个模块**：`view_image` 工具需要「这个文件到底是不是图 / 多大 /
能不能直接塞给模型」，而 `read_file` 是纯文本读（`read_text(errors="replace")`，
读 PNG 只能得到乱码）。这套判定不属于工具层也不属于 LLM 层，放这里两边都能用。

**零硬依赖**：只用标准库做 magic 判定与头部尺寸解析（PNG/GIF/JPEG/BMP/WEBP）。
Pillow 是**可选**增强——装了才能缩图（`max_edge`），没装则超限时返回明确指引
（让 Agent 用 run_cmd 调 ImageMagick/ffmpeg 缩，或调大 max_edge）。绝不在 import
期依赖 Pillow：`pyproject.toml` 的 base 依赖只有 anthropic/openai/httpx。

**为什么不支持 SVG**：矢量图不是像素，模型看不了；且 SVG 是可执行内容（脚本/
外部引用），当图片喂给模型既不安全也没意义。SVG 走 `read_file` 看源码。
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from pathlib import Path

# 单张图默认长边上限（像素）。1568 是 Anthropic 的推荐值，也是多数网关不缩图
# 的甜点：再大只是徒增 token（实测 1920×1080 ≈ 1029 token，1280×720 ≈ 639）。
DEFAULT_MAX_EDGE = 1568
# 单张图 base64 后体积上限（字节）。base64 膨胀 4/3，故原始上限 ≈ 该值 × 0.75。
# 6MB 是常见网关请求体上限之下的安全值；超过一律缩图或报错，绝不硬塞。
DEFAULT_MAX_B64 = 6 << 20

# magic → media_type（顺序敏感：WEBP 要先于 RIFF 通判）
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

# 扩展名 → media_type（magic 认不出时的兜底；只用于「报错文案说人话」）
_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
}


@dataclass(frozen=True)
class ImageInfo:
    """一张已识别图片的元信息。"""

    media_type: str
    width: int
    height: int
    size: int          # 原始字节数

    @property
    def edge(self) -> int:
        return max(self.width, self.height)

    def describe(self) -> str:
        return (f"{self.media_type} {self.width}×{self.height}"
                f"，{_human(self.size)}")


def _human(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f}MB"
    if n >= 1 << 10:
        return f"{n // (1 << 10)}KB"
    return f"{n}B"


def sniff_media_type(data: bytes) -> str:
    """按 magic 判 media_type；认不出返回 ''（**不猜**——猜错会让网关 400）。"""
    if not data:
        return ""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _png_size(d: bytes) -> tuple[int, int]:
    # IHDR 必为第一个 chunk：8 字节签名 + 4 长度 + 4 类型 + 宽 4 + 高 4
    if len(d) < 24:
        return 0, 0
    return struct.unpack(">II", d[16:24])


def _gif_size(d: bytes) -> tuple[int, int]:
    if len(d) < 10:
        return 0, 0
    return struct.unpack("<HH", d[6:10])


def _bmp_size(d: bytes) -> tuple[int, int]:
    if len(d) < 26:
        return 0, 0
    w, h = struct.unpack("<ii", d[18:26])
    return abs(w), abs(h)


def _jpeg_size(d: bytes) -> tuple[int, int]:
    """扫段找 SOF0/1/2/…（0xC0-0xCF 除 C4/C8/CC）。"""
    i, n = 2, len(d)
    while i + 9 < n:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        seg_len = struct.unpack(">H", d[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                break
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return w, h
        i += 2 + max(seg_len, 2)
    return 0, 0


def _webp_size(d: bytes) -> tuple[int, int]:
    if len(d) < 30:
        return 0, 0
    fourcc = d[12:16]
    if fourcc == b"VP8X":
        w = int.from_bytes(d[24:27], "little") + 1
        h = int.from_bytes(d[27:30], "little") + 1
        return w, h
    if fourcc == b"VP8 ":
        w, h = struct.unpack("<HH", d[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if fourcc == b"VP8L":
        if len(d) < 25:
            return 0, 0
        bits = int.from_bytes(d[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return 0, 0


_SIZE_READERS = {
    "image/png": _png_size,
    "image/gif": _gif_size,
    "image/bmp": _bmp_size,
    "image/jpeg": _jpeg_size,
    "image/webp": _webp_size,
}


def probe_image(data: bytes) -> ImageInfo | None:
    """字节 → ImageInfo；非图片返回 None。

    尺寸解析失败（截断/畸形）时 width/height 记 0 —— **仍返回 ImageInfo**：
    能确认是图片就够了（model 看的是像素，不依赖我们报的尺寸），别因为量不出
    宽高就把一张合法图片判死。
    """
    mime = sniff_media_type(data)
    if not mime:
        return None
    reader = _SIZE_READERS.get(mime)
    w = h = 0
    if reader is not None:
        try:
            w, h = reader(data)
        except (struct.error, IndexError, ValueError):
            w = h = 0
    return ImageInfo(media_type=mime, width=w, height=h, size=len(data))


def read_image(path: Path, *, max_bytes: int = 64 << 20) -> tuple[bytes, ImageInfo] | str:
    """读文件并探测；失败返回**人读错误串**（调用方直接当工具结果回填）。

    不在这里抛异常：工具层约定「失败 = 带前缀的字符串」，异常会穿出去炸整轮。
    """
    try:
        if not path.is_file():
            return f"文件不存在: {path}"
        size = path.stat().st_size
        if size > max_bytes:
            return (f"文件过大（{_human(size)} > {_human(max_bytes)}）："
                    "先切片或缩图，不要整读")
        data = path.read_bytes()
    except OSError as e:
        return f"读取失败: {e}"
    info = probe_image(data)
    if info is None:
        ext = path.suffix.lower()
        hint = f"（扩展名像是 {_EXT_MIME[ext]}）" if ext in _EXT_MIME else ""
        return (f"不是可识别的图片（PNG/JPEG/GIF/BMP/WEBP 的 magic 都对不上）"
                f"{hint}。若确实该是图片，用 `file`/`xxd | head` 看文件头，"
                "可能是改了扩展名的其它格式或被切片/加密的载体")
    return data, info


def downscale(data: bytes, info: ImageInfo, max_edge: int) -> tuple[bytes, ImageInfo] | str:
    """缩到长边 ≤ max_edge（Pillow 可选）。无法缩时返回错误串。

    **只在确实超限时调用**——Pillow 缺失是常态（base 依赖里没有），所以这里
    的失败文案要能直接指导 Agent 换路，而不是干巴巴一句「不支持」。
    """
    if info.edge <= max_edge:
        return data, info
    try:
        from PIL import Image  # noqa: PLC0415 —— 可选依赖，只在需要缩图时 import
    except ImportError:
        return (f"图片长边 {info.edge}px 超过上限 {max_edge}px，且未安装 Pillow 无法"
                "自动缩图。两条路：① run_cmd 用 ImageMagick 缩"
                f"（`convert in.png -resize {max_edge}x{max_edge}\\> out.png`）后重新 "
                "view_image；② 直接调大 view_image 的 max_edge（代价是 token 变多）")
    import io  # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            fmt = (im.format or "").upper()
            im.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            # JPEG 无 alpha 通道；带 alpha 的转 PNG 保真（隐写题里 alpha 可能是载体）
            if fmt == "JPEG" and im.mode not in ("RGBA", "LA", "P"):
                im.convert("RGB").save(buf, "JPEG", quality=90, optimize=True)
            else:
                im.save(buf, "PNG", optimize=True)
    except Exception as e:  # noqa: BLE001 —— 畸形图/解码器缺失都走同一降级
        return f"缩图失败（{type(e).__name__}: {e}）；可改用 ImageMagick 预处理后再试"
    out = buf.getvalue()
    new_info = probe_image(out)
    if new_info is None:
        return "缩图后无法识别（内部错误）；请用 ImageMagick 预处理后再试"
    return out, new_info


def to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
