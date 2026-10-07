"""构建国密 HTTP sidecar（tools/gmhttp → tools/bin/gmhttp.exe）。

背景（2026-10-07）：Python 的 ssl 是 OpenSSL 薄绑定，本机 OpenSSL 未编入 SM 密码套件，
PyPI 的 gmssl/gmalg/pygmssl 等只有 SM2/SM3/SM4 密码学原语、无 TLS 栈。Go 生态有成熟的
国密 TLS 实现（tjfoc/gmsm 的 gmtls），故重发国密通道走本 sidecar，其余仍走 Python httpx。

产物落 tools/bin/（registry 的 bundled 规范位，gitignore），桌面打包经 scripts/build_exe.py
随包旁挂。纯 Go 零 cgo，构建机需装 Go 1.21+（本机 go1.24.5）。

用法：
    E:\\Miniconda3\\python.exe scripts\\build_gmhttp.py            # 默认走 goproxy.cn
    set GOPROXY=direct && python scripts\\build_gmhttp.py          # 自定义模块代理
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文/符号会 UnicodeEncodeError——强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "gmhttp"
OUT_DIR = ROOT / "tools" / "bin"
OUT = OUT_DIR / ("gmhttp.exe" if os.name == "nt" else "gmhttp")


def main() -> int:
    go = shutil.which("go")
    if not go:
        print("✗ 未找到 go 可执行文件——构建 gmhttp 需 Go 1.21+（https://go.dev/dl/）",
              file=sys.stderr)
        return 1
    if not (SRC / "main.go").is_file():
        print(f"✗ 源码缺失: {SRC / 'main.go'}", file=sys.stderr)
        return 1

    ver = subprocess.run([go, "version"], capture_output=True, text=True)
    print(f"· {ver.stdout.strip() or ver.stderr.strip()}")

    env = dict(os.environ)
    # 国内直连 proxy.golang.org 常失败；默认走 goproxy.cn（可用环境变量覆盖）
    env.setdefault("GOPROXY", "https://goproxy.cn,direct")
    env.setdefault("GOFLAGS", "-mod=mod")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"· 构建 {SRC} → {OUT}")
    # 先 tidy 保证 go.sum 齐全（首次构建需要拉取 gmtls）
    for step in (["mod", "tidy"], ["build", "-o", str(OUT), "."]):
        r = subprocess.run([go, *step], cwd=str(SRC), env=env)
        if r.returncode != 0:
            print(f"✗ go {' '.join(step)} 失败（rc={r.returncode}）", file=sys.stderr)
            return r.returncode

    size_mb = OUT.stat().st_size / 1024 / 1024
    print(f"✓ 产物 {OUT}（{size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
