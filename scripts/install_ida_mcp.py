"""一键安装 vendored IDA MCP 插件到 IDA 用户插件目录（幂等，纯标准库）。

把 tools/mcp/ida-pro-mcp/ 下的 ida_mcp.py + ida_mcp/ 复制进 IDA 的 plugins 目录，
使「逆向工作台 MCP 实时桥」仅依赖项目内 vendor 件，无需 clone / pip 安装。

用法：
    python scripts/install_ida_mcp.py            # 同步（幂等，可重复运行）
    python scripts/install_ida_mcp.py --refresh  # 先删目标包再全新复制（升级/清陈旧文件）
    python scripts/install_ida_mcp.py --status   # 只看当前安装版本，不改动

装完在 IDA 中 Edit → Plugins → MCP（Ctrl-Alt-M）启动，监听 127.0.0.1:13337。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "tools" / "mcp" / "ida-pro-mcp"
VENDOR_ITEMS = ["ida_mcp.py", "ida_mcp"]
IGNORE = shutil.ignore_patterns("__pycache__", "tests", "*.pyc")
VERSION_FILE = ".vendor_version"


def _candidate_plugin_dirs() -> list[Path]:
    """按优先级返回 IDA 插件目录候选（存在或可创建）。"""
    candidates: list[Path] = []
    appdata = sys.platform
    if appdata == "win32":
        appdata_dir = Path.home() / "AppData" / "Roaming"
        candidates.append(appdata_dir / "Hex-Rays" / "IDA Pro" / "plugins")
        # PATH 上的 idat/ida 所在安装目录
        for exe in ("idat.exe", "idat64.exe", "ida.exe", "ida64.exe"):
            found = shutil.which(exe)
            if found:
                candidates.append(Path(found).resolve().parent / "plugins")
        # 常见安装根（含实测的 D:\\Tools\\IDA93）
        for root in ("C:/Program Files", "C:/Program Files (x86)", "D:/Tools", "E:/Tools"):
            base = Path(root)
            if base.is_dir():
                candidates.extend(sorted(base.glob("IDA*/plugins")))
    # macOS / Linux 与通用回退
    candidates.append(Path.home() / ".idapro" / "plugins")
    # 去重保序
    seen, uniq = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _vendor_signature() -> str:
    """对 vendor 全部纳入安装的 .py 内容算指纹（安装幂等比对用）。"""
    h = hashlib.sha256()
    for item in VENDOR_ITEMS:
        src = VENDOR_DIR / item
        if src.is_file():
            h.update(src.read_bytes())
        elif src.is_dir():
            for py in sorted(src.rglob("*.py")):
                if "__pycache__" in py.parts or "tests" in py.parts:
                    continue
                h.update(py.relative_to(VENDOR_DIR).as_posix().encode())
                h.update(py.read_bytes())
    return h.hexdigest()[:16]


def _installed_signature(dst: Path) -> str | None:
    vf = dst / "ida_mcp" / VERSION_FILE
    if not vf.is_file():
        return None
    for line in vf.read_text(encoding="utf-8").splitlines():
        if line.startswith("signature:"):
            return line.split(":", 1)[1].strip()
    return None


def _write_version(dst: Path) -> None:
    vf = dst / "ida_mcp" / VERSION_FILE
    vf.write_text(
        f"installed: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"signature: {_vendor_signature()}\n"
        f"source: tools/mcp/ida-pro-mcp (mrexodia/ida-pro-mcp vendored)\n",
        encoding="utf-8",
    )


def _choose_target() -> Path:
    """选目标插件目录：优先已装过 ida_mcp 的，否则首个可创建的候选。"""
    dirs = _candidate_plugin_dirs()
    for d in dirs:
        if (d / "ida_mcp.py").is_file() or (d / "ida_mcp").is_dir():
            return d
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    raise SystemExit("找不到可写的 IDA 插件目录（已探测 %APPDATA%、PATH、常见安装路径、~/.idapro）。")


def install(refresh: bool) -> None:
    if not (VENDOR_DIR / "ida_mcp.py").is_file():
        raise SystemExit(f"vendor 件缺失：{VENDOR_DIR}")
    dst = _choose_target()
    sig = _vendor_signature()
    if not refresh and _installed_signature(dst) == sig and (dst / "ida_mcp.py").is_file():
        print(f"[=] 已是最新（signature {sig}）：{dst}")
        _hint()
        return
    if refresh and (dst / "ida_mcp").is_dir():
        shutil.rmtree(dst / "ida_mcp")
    dst.mkdir(parents=True, exist_ok=True)
    for item in VENDOR_ITEMS:
        src, target = VENDOR_DIR / item, dst / item
        if src.is_file():
            shutil.copy2(src, target)
        else:
            shutil.copytree(src, target, ignore=IGNORE, dirs_exist_ok=True)
    _write_version(dst)
    print(f"[+] 已安装（signature {sig}）→ {dst}")
    _hint()


def status() -> None:
    dirs = _candidate_plugin_dirs()
    printed = False
    for d in dirs:
        if (d / "ida_mcp.py").is_file() or (d / "ida_mcp").is_dir():
            printed = True
            print(f"{d}")
            print(f"    installed signature: {_installed_signature(dst=d) or '(无版本标记)'}")
    print(f"vendor signature: {_vendor_signature()}  ({VENDOR_DIR})")
    if not printed:
        print("（尚未安装到任何候选插件目录）")


def _hint() -> None:
    print("    在 IDA 中 Edit → Plugins → MCP（Ctrl-Alt-M）启动，监听 http://127.0.0.1:13337/mcp")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 老解释器/重定向环境无此方法无所谓
        pass
    ap = argparse.ArgumentParser(description="安装 vendored IDA MCP 插件（幂等）")
    ap.add_argument("--refresh", action="store_true", help="先删目标包再全新复制")
    ap.add_argument("--status", action="store_true", help="只查看安装版本")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        install(args.refresh)


if __name__ == "__main__":
    main()
