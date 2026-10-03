"""桌面打包一键脚本（desktop-app-shell M3）：干净 venv 固化 + PyInstaller onedir + 资源随包。

产物 dist/cyberstrike-pro/：
  cyberstrike-pro.exe（windowed，双击弹窗）+ _internal/（Python 运行时与依赖）
  packs/  webui/dist/  tools/（资源随包——serve.py frozen 分支 _ROOT=exe 目录，
  cwd 相对路径惯例直接命中）
  config/、workspaces/ 不随包：首启自建（ProviderStore 写种子 providers.json /
  ProjectStore mkdir）——敏感凭据（providers key / fofa.json）绝不进包。

用法（项目根）：
  E:\\Miniconda3\\python.exe scripts\\build_exe.py             # 全流程（.build-venv 缺失自动建）
  E:\\Miniconda3\\python.exe scripts\\build_exe.py --no-venv   # 调试：跳过 venv 用当前环境打
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".build-venv"
DIST_APP = ROOT / "dist" / "cyberstrike-pro"

# 与 pyproject.toml [project.optional-dependencies] 对齐（api + window）+ 打包器。
# fastapi/uvicorn 钉到与开发环境一致的版本：fastapi 0.12x 移除 Starlette
# add_event_handler（app.py shutdown 钩子所用），>=0.110 语义放行会打出坏包（2026-09-23 实测）
# anthropic/openai：core/llm 自 2026-10-03 起以官方 SDK 为主引擎（pyproject 核心依赖），
# 缺失则打包件一导入 core.llm 即崩。httpx 由它们带入，此处仍显式钉住。
BUILD_DEPS = [
    "fastapi==0.116.1", "uvicorn==0.35.0", "httpx>=0.27",
    "anthropic>=0.40", "openai>=1.40",
    "python-multipart>=0.0.9", "openpyxl>=3.1",
    "pywebview>=5.0", "pyinstaller>=6.0",
]

# 资源随包时排除运行产物与缓存（.history 写备份/kb-backups/__pycache__ 等）
COPY_IGNORE = shutil.ignore_patterns(
    ".history", "__pycache__", "*.pyc", "kb-backups", ".trash", ".git")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def ensure_frontend() -> None:
    if (ROOT / "webui" / "dist" / "index.html").is_file():
        return
    npm = shutil.which("npm")
    if npm is None:
        sys.exit("[build] webui/dist 缺失且未找到 npm——先 cd webui && npm run build 再打包")
    print("[build] webui/dist 缺失 → npm install + build（首次较慢）")
    run([npm, "install"], cwd=ROOT / "webui")
    run([npm, "run", "build"], cwd=ROOT / "webui")


def ensure_venv(base_python: str) -> str:
    """干净 venv 只装 BUILD_DEPS（不打 Miniconda 全家，方案 §4.4 定稿）；幂等可重复跑。"""
    py = VENV_DIR / "Scripts" / "python.exe"
    if py.is_file():
        print(f"[build] venv 已就绪：{VENV_DIR}")
    else:
        print(f"[build] 创建干净 venv → {VENV_DIR}")
        run([base_python, "-m", "venv", str(VENV_DIR)])
    run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", *BUILD_DEPS])
    return str(py)


def build_exe(python: str) -> None:
    # 注：--specpath 不允许与 .spec 文件同用（makespec 专属选项）；spec 就在 cwd
    run([python, "-m", "PyInstaller", "cyberstrike-pro.spec", "--noconfirm",
         "--distpath", "dist", "--workpath", "build"], cwd=ROOT)


def copy_resources() -> None:
    if not (DIST_APP / "cyberstrike-pro.exe").is_file():
        sys.exit("[build] PyInstaller 未产出 dist/cyberstrike-pro/cyberstrike-pro.exe")
    for name in ("packs", "tools"):
        print(f"[build] 资源随包：{name}/")
        shutil.copytree(ROOT / name, DIST_APP / name,
                        dirs_exist_ok=True, ignore=COPY_IGNORE)
    print("[build] 资源随包：webui/dist/")
    shutil.copytree(ROOT / "webui" / "dist", DIST_APP / "webui" / "dist",
                    dirs_exist_ok=True, ignore=COPY_IGNORE)


def _du_mb(p: Path) -> str:
    total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return f"{total / 1048576:.0f} MB"


def main() -> None:
    ap = argparse.ArgumentParser(description="cyberstrike-pro 桌面打包（M3）")
    ap.add_argument("--no-venv", action="store_true",
                    help="调试用：跳过 venv，用当前 Python 环境打包")
    ap.add_argument("--python", default=sys.executable,
                    help="建 venv 用的基础解释器（默认当前 python）")
    args = ap.parse_args()

    ensure_frontend()
    if args.no_venv:
        print("[build] --no-venv：用当前环境打包（调试模式）")
        py = sys.executable
    else:
        py = ensure_venv(args.python)
    build_exe(py)
    copy_resources()
    print(f"[build] 完成：{DIST_APP}（{_du_mb(DIST_APP)}）")
    print(f"  双击 {DIST_APP / 'cyberstrike-pro.exe'} 即弹窗"
          f"（8420 已在跑则 attach 附窗；首启自动建 config/ workspaces/）")


if __name__ == "__main__":
    main()
