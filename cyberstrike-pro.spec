# -*- mode: python ; coding: utf-8 -*-
"""cyberstrike-pro 桌面打包 spec（desktop-app-shell M3，DESIGN.md §一「部署形态」）。

由 scripts/build_exe.py 在 .build-venv 内驱动：
    .build-venv\\Scripts\\python.exe -m PyInstaller cyberstrike-pro.spec --noconfirm
产物 dist/cyberstrike-pro/（exe + _internal/）；packs/、webui/dist/、tools/ 资源由
build_exe.py 复制到 exe 旁（serve.py frozen 分支 _ROOT=exe 目录，cwd 相对路径直接命中）。
config/、workspaces/ 不随包：首启自建（ProviderStore 种子 / ProjectStore mkdir）。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = [
    # uvicorn 运行时按字符串动态导入 loops/protocols/lifespan，全收防漏
    *collect_submodules("uvicorn"),
]

# pywebview 6.x Windows 后端：winforms/edgechromium 经 pythonnet/clr_loader；
# 包内 js/ 静态资源与 WebView2Loader.dll 必须随包——collect_all 连 datas/binaries 收齐
for pkg in ("webview", "clr_loader"):
    pkg_datas, pkg_bins, pkg_hiddens = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_bins
    hiddenimports += pkg_hiddens
hiddenimports += collect_submodules("pythonnet")

a = Analysis(
    ["scripts/serve.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    # playwright 未装（exe 外按需，方案定稿）；tkinter/pytest 与运行无关
    excludes=["playwright", "tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cyberstrike-pro",
    console=False,  # windowed：双击无黑窗；stdout 落 logs/serve-window.log（serve.py 处理）
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="cyberstrike-pro")
