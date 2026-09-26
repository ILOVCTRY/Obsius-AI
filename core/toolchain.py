"""工具链注册表（toolchain-registry M1，2026-09-23，DESIGN §七）。

registry 单表 `tools/registry.json`（入库，机器无关声明）+ 本机路径覆盖层
`config/tools.json`（gitignore，仿 config/mcp.json 模式）。四来源检测顺序
（方案 §4.1 定稿）：

  1. config/tools.json paths —— 既有安装纳管（用户指认，最高优先）
  2. tools/ bin 规范位        —— 可下载/手动放置落位（registry bin 相对 tools_root；
                                kind=runtime 且 acquire=bundled 即「开包自带」位）
  3. registry fallback glob  —— 常见安装目录扫描（?:/ 前缀 = 盘符通配 C-H 逐一尝试）
  4. PATH（search 文件名清单）—— 宿主 PATH 探测

status=ready（source=config/bundled/downloaded/fallback/path）/missing。
探测只查文件存在性零副作用；verify 试跑深检留给设置页工具面板（M3）。
纯 stdlib；detector（core/runtime）/ doctor（core/skills）/ decompiler
（core/tools）消费，gateway env 注入与 venv 落位在 M2。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

TOOL_KINDS = ("binary", "runtime", "python-tool", "data")
OVERRIDE_PATH = "config/tools.json"


def load_registry(tools_root: str | Path = "tools") -> dict[str, dict]:
    """读 tools/registry.json 并逐条校验；坏条目 ValueError 带条目名（fail-fast，
    仿 validate_verify_spec 精神——入库声明出错是代码 bug 不该静默吞）。缺文件给 {}。"""
    path = Path(tools_root) / "registry.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"tools/registry.json 不可读: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("tools/registry.json 顶层必须是「工具名 → 条目」对象")
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"registry 条目 {name}: 必须是对象")
        kind = entry.get("kind")
        if kind not in TOOL_KINDS:
            raise ValueError(f"registry 条目 {name}: kind 非法（{kind!r}，允许 {TOOL_KINDS}）")
        for key in ("verify", "fallback", "search", "pip", "domains"):
            if key in entry and not isinstance(entry[key], list):
                raise ValueError(f"registry 条目 {name}: {key} 必须是数组")
        if "bin" in entry and not isinstance(entry["bin"], dict):
            raise ValueError(f"registry 条目 {name}: bin 必须是平台键对象（windows/linux）")
    return data


def load_tool_overrides(path: str | Path = OVERRIDE_PATH) -> dict[str, dict]:
    """读本机路径覆盖层；坏文件给 {} 绝不炸（宁容错，仿 fofa config load）。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _bin_rel(entry: dict) -> str | None:
    """取当前平台的 bin 相对路径（bin 平台键 windows/linux，`*` 兜底）。"""
    bin_map = entry.get("bin")
    if not isinstance(bin_map, dict):
        return None
    v = bin_map.get("windows" if os.name == "nt" else "linux") or bin_map.get("*")
    return str(v) if v else None


def _glob_one(pattern: str) -> Path | None:
    """单条 fallback 模式展开：`?:/` 前缀 = 盘符通配（C-H 常见盘逐一尝试）；
    含通配符走 glob（锚点盘符为 base），字面量直接 exists 判定。"""
    if pattern.startswith("?:"):
        for drive in "CDEFGH":
            if Path(f"{drive}:/").exists():
                hit = _glob_one(f"{drive}{pattern[1:]}")
                if hit:
                    return hit
        return None
    p = Path(pattern)
    if p.is_file():
        return p
    if not any(ch in pattern for ch in "*?"):
        return None
    anchor = p.anchor
    base = Path(anchor) if anchor else Path(".")
    if not base.exists():
        return None
    rest = str(p.relative_to(anchor)) if anchor else str(p)
    try:
        for hit in sorted(base.glob(rest)):
            if hit.is_file():
                return hit
    except OSError:
        pass
    return None


def _venv_python(tools_root: Path) -> Path | None:
    """python-tool 类的落位探测：tools/venv 的解释器（Windows Scripts / posix bin）。"""
    cand = tools_root / "venv" / ("Scripts" if os.name == "nt" else "bin")
    for name in ("python.exe", "python"):
        p = cand / name
        if p.is_file():
            return p
    return None


def resolve_tool(name: str, entry: dict, *, tools_root: str | Path = "tools",
                 overrides: dict[str, dict] | None = None) -> dict:
    """四来源检测（顺序见模块 docstring）。返回
    {name, kind, status, source, path, detail}；status=ready 时 source 标注获取来源。"""
    root = Path(tools_root)
    kind = entry.get("kind")

    def _hit(source: str, path: Path | str, detail: str) -> dict:
        return {"name": name, "kind": kind, "status": "ready", "source": source,
                "path": str(path), "detail": detail}

    def _miss() -> dict:
        return {"name": name, "kind": kind, "status": "missing", "source": None,
                "path": None, "detail": (entry.get("guide") or "未检出")[:160]}

    # 1. config/tools.json paths（用户指认既有安装）
    ov = (overrides or {}).get(name) or {}
    for cand in ov.get("paths") or []:
        p = Path(cand)
        if p.exists():
            return _hit("config", p, "config/tools.json 指认")

    # 2. tools/ bin 规范位（可下载落位 / 开包自带）
    rel = _bin_rel(entry)
    if rel:
        p = root / rel
        if p.exists():
            src = "bundled" if entry.get("acquire") == "bundled" else "downloaded"
            return _hit(src, p, "tools/ 规范位")
    if kind == "python-tool":
        p = _venv_python(root)
        if p:
            return _hit("bundled", p, "tools/venv")

    # 3. fallback glob（常见安装目录扫描）
    for pattern in entry.get("fallback") or []:
        hit = _glob_one(pattern)
        if hit:
            return _hit("fallback", hit, f"fallback 命中: {pattern}")

    # 4. PATH（search 文件名清单）
    for fn in entry.get("search") or []:
        hit = shutil.which(fn)
        if hit:
            return _hit("path", hit, f"PATH: {fn}")

    return _miss()


def probe_tools(tools_root: str | Path = "tools",
                overrides: dict[str, dict] | None = None) -> list[dict]:
    """registry 全条目探测（按名称序）；坏 registry 抛 ValueError 由调用方处置。"""
    registry = load_registry(tools_root)
    return [resolve_tool(n, e, tools_root=tools_root, overrides=overrides)
            for n, e in sorted(registry.items())]
