"""资产批量导入解析器（cyberspace-mapping M1，2026-09-23）。

xlsx/CSV → 列映射嗅探（表头名匹配优先，无表头逐列内容投票）→ 归一化行。
解析只读不落库：落库统一走 assets.import_assets（register_asset 单一入口）。
openpyxl 缺失时 xlsx 解析抛 XlsxUnavailable（API 端点转 503 + 安装指引，
仿 browser extra 降级惯例）；CSV 走 stdlib 恒可用。
"""

import csv
import io
import re
from pathlib import Path

__all__ = ["COLUMN_KINDS", "MAX_IMPORT_ROWS", "PREVIEW_ROWS", "XlsxUnavailable",
           "parse_table", "sniff_columns", "normalize_rows"]

MAX_IMPORT_ROWS = 10000  # 单批硬上限（防误传超大文件拖垮写路径；2026-10-01 由 5000 提至 10000）
PREVIEW_ROWS = 50        # 预览行数（方案 4.1）

# 列语义（映射下拉值域）；ignore=该列不导入
COLUMN_KINDS = ("ignore", "ip", "port", "host", "url", "title", "products")

_HEADER_ALIASES: dict[str, set[str]] = {
    "ip": {"ip", "ipv4", "ip地址", "ip 地址", "ip_addr", "address", "地址"},
    "port": {"port", "端口", "端口号"},
    "host": {"host", "domain", "域名", "主机", "主机名", "hostname", "网站", "站点"},
    "url": {"url", "链接", "网址", "url链接", "url 链接", "带协议url", "site"},
    "title": {"title", "标题", "网站标题", "网页标题"},
    "products": {"product", "products", "指纹", "产品", "指纹/产品", "finger"},
}

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_PORT_RE = re.compile(r"^\d{1,5}$")
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\S+$")
# 表头判定排除：纯数据样貌（IPv4 / 纯数字 / url）的行不当表头
_DATA_LIKE_RE = re.compile(r"^([0-9.]+|[a-zA-Z][a-zA-Z0-9+.\-]*://\S+)$")


class XlsxUnavailable(Exception):
    """openpyxl 未安装（xlsx 解析能力缺失）；API 层转 503 + 安装指引。"""


def _looks_like_header(row: list[str]) -> bool:
    cells = [c.strip() for c in row if c is not None and str(c).strip()]
    if not cells or any(not isinstance(c, str) for c in row):
        return False
    aliases = set().union(*_HEADER_ALIASES.values())
    hits = sum(1 for c in cells if c.strip().lower() in aliases)
    return hits >= 1 and not any(_DATA_LIKE_RE.match(c.strip()) for c in cells)


def sniff_columns(header: list[str] | None, rows: list[list[str]]) -> list[dict]:
    """列映射建议：表头名匹配优先（confidence 0.9）；无表头逐列内容投票
    （≥80% 行命中给建议，首条未匹配文本列兜底 title 建议 0.5）。"""
    n_cols = max((len(r) for r in rows), default=len(header or []))
    if header:
        n_cols = max(n_cols, len(header))
    out: list[dict] = []
    voted_title = False
    for c in range(n_cols):
        h = (header[c] if header and c < len(header) else "").strip().lower()
        if h:
            for kind, names in _HEADER_ALIASES.items():
                if h in names:
                    out.append({"kind": kind, "confidence": 0.9})
                    break
            else:
                out.append({"kind": "ignore", "confidence": 0.0})
            continue
        # 内容投票（最多取 200 行）
        vals = [str(r[c]).strip() for r in rows[:200] if c < len(r) and str(r[c]).strip()]
        if not vals:
            out.append({"kind": "ignore", "confidence": 0.0})
            continue
        ratio_ip = sum(1 for v in vals if _IP_RE.match(v)) / len(vals)
        ratio_port = sum(1 for v in vals
                         if _PORT_RE.match(v) and 1 <= int(v) <= 65535) / len(vals)
        ratio_url = sum(1 for v in vals if _URL_RE.match(v)) / len(vals)
        ratio_host = sum(1 for v in vals
                         if "." in v and " " not in v and not v.startswith(":")) / len(vals)
        if ratio_url >= 0.8:
            out.append({"kind": "url", "confidence": round(ratio_url, 2)})
        elif ratio_ip >= 0.8:
            out.append({"kind": "ip", "confidence": round(ratio_ip, 2)})
        elif ratio_port >= 0.8:
            out.append({"kind": "port", "confidence": round(ratio_port, 2)})
        elif ratio_host >= 0.8:
            out.append({"kind": "host", "confidence": round(ratio_host, 2)})
        else:
            out.append({"kind": "ignore", "confidence": 0.0})
    # 兜底 title 建议：全表无 title 建议时，取第一个 ignore 列且文本丰富的
    if not any(c["kind"] == "title" for c in out):
        for c, col in enumerate(out):
            if col["kind"] != "ignore":
                continue
            vals = [str(r[c]).strip() for r in rows[:200] if c < len(r) and str(r[c]).strip()]
            if len(vals) >= max(1, len(rows[:200]) // 2):
                out[c] = {"kind": "title", "confidence": 0.5}
                voted_title = True
                break
    return out


def parse_table(data: bytes, suffix: str, max_rows: int = MAX_IMPORT_ROWS) -> dict:
    """解析 xlsx/CSV 字节 → {header, rows, columns, total_rows, truncated}。

    全空行在解析期丢弃（稀疏行容错的一部分；单字段缺失留给导入侧降级）。
    suffix 限 .csv/.xlsx；xlsx 需 openpyxl（缺失抛 XlsxUnavailable）。
    """
    suffix = (suffix or "").lower()
    raw: list[list[str]] = []
    if suffix == ".csv":
        text = None
        for enc in ("utf-8-sig", "gbk"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("CSV 编码无法识别（尝试 utf-8/gbk 均失败）")
        raw = [[("" if c is None else str(c)) for c in row]
               for row in csv.reader(io.StringIO(text))]
    elif suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError as e:
            raise XlsxUnavailable(
                "openpyxl 未安装，xlsx 解析不可用：pip install openpyxl"
                "（或 pip install -e \".[api]\"）") from e
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            raw.append(["" if c is None else str(c) for c in row])
        wb.close()
    else:
        raise ValueError("仅支持 .xlsx / .csv 文件")

    header: list[str] | None = None
    if raw and _looks_like_header(raw[0]):
        header = raw[0]
        raw = raw[1:]
    # 全空行丢弃 + 单元格 strip
    rows = []
    for r in raw:
        cells = [c.strip() for c in r]
        if any(cells):
            rows.append(cells)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {"header": header, "rows": rows,
            "columns": sniff_columns(header, rows),
            "total_rows": len(rows), "truncated": truncated}


def _split_products(v: str) -> list[str]:
    parts = re.split(r"[,;，；/|]+", v)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def normalize_rows(rows: list[list], mapping: list[str]) -> tuple[list[dict], int]:
    """按列映射归一化：产出 import_assets 消费的行
    {ip?, port?, host?, url?, title?, products?}；全字段空的行丢弃计数。

    mapping 与列序对齐（短了按 ignore 补齐）；产品列多列可归并（保序去重）。
    """
    width = max((len(r) for r in rows), default=0)
    kinds = (list(mapping) + ["ignore"] * width)[:max(width, len(mapping))]
    out: list[dict] = []
    skipped = 0
    for r in rows:
        rec: dict = {}
        products: list[str] = []
        for c, kind in enumerate(kinds):
            v = "" if c >= len(r) else str(r[c] or "").strip()
            if not v:
                continue
            if kind == "ip" and not rec.get("ip"):
                rec["ip"] = v
            elif kind == "port" and not rec.get("port") and _PORT_RE.match(v):
                rec["port"] = v
            elif kind == "host" and not rec.get("host"):
                rec["host"] = v
            elif kind == "url" and not rec.get("url"):
                rec["url"] = v
            elif kind == "title" and not rec.get("title"):
                rec["title"] = v[:200]
            elif kind == "products":
                products.extend(_split_products(v))
        if products:
            rec["products"] = products[:20]
        if rec:
            out.append(rec)
        else:
            skipped += 1
    return out, skipped
