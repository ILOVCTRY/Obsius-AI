"""启动 core API 服务（本机 127.0.0.1，WebUI/CLI 的后端）。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\serve.py              # 无窗模式，127.0.0.1:8420
    E:\\Miniconda3\\python.exe scripts\\serve.py 8421         # 指定端口
    E:\\Miniconda3\\pythonw.exe scripts\\serve.py --window    # 桌面窗口壳（pywebview/WebView2）
    scripts\\serve.py --window --url http://localhost:5173    # 显式指定加载地址（跳过探测）
    scripts\\serve.py --window --debug                        # 窗口右键可开 DevTools

窗口模式 owner/attach（desktop-app-shell 方案，DESIGN.md §1）：
- 启动探测 127.0.0.1:<port>：未在跑 = **owner**——本进程拉起服务（子线程 uvicorn），
  最后一窗关闭 → 进程内置 server.should_exit 优雅停机（shutdown 钩子给在跑会话落
  断点快照，不必走 HTTP）；attach 附窗不建服务只开窗连过去，关窗仅退出本进程。
- 二次双击「启动平台（窗口）.bat」= 再开一个窗口连已有服务（attach，多窗并行）。
- 加载地址探测顺序：--url 显式 > webui/dist 存在 → 同源静态版 8420（M2）>
  Vite dev 5173 可达 → dev 版（热更新照常）> 都不满足 → 窗内指引占位页。
- pythonw（无控制台）下 stdout/stderr 为 None，重定向到 logs/serve-window.log。
- windowed exe（M3 打包，scripts/build_exe.py）同样适配：frozen 时默认窗口模式。

v0.64 优雅停机：进程持有 uvicorn Server 句柄并挂 POST /api/admin/shutdown
（置 should_exit 优雅退出）——FastAPI shutdown 钩子借机给所有在跑会话落
断点快照（app.state.shutdown_persist），重启后任务不丢现场。
"""

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path

# cwd 兜底（硬要求）：config/、packs/、workspaces/、providers.json 全是 cwd 相对
# 路径惯例，从快捷方式/别处启动时路径不能飘。frozen 分支为 M3 打包（exe）预留。
if getattr(sys, "frozen", False):
    _ROOT = Path(sys.executable).resolve().parent
else:
    _ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _vite_dev_open(port: int = 5173) -> bool:
    """Vite dev 只绑 IPv6 ::1（IPv4 拒连），两个地址任一可连即视为在跑。"""
    return any(_port_open(port, host) for host in ("::1", "127.0.0.1"))


def _static_dir_if_built() -> str | None:
    """webui/dist 产物存在（有 index.html）→ 交 create_app 挂同源静态托管。"""
    dist = _ROOT / "webui" / "dist"
    return str(dist) if (dist / "index.html").is_file() else None


def _make_server(port: int):
    """建 app（含静态托管探测）+ uvicorn Server 句柄，不 start。"""
    import uvicorn
    from core.api import create_app

    app = create_app(workspace_root="workspaces", packs_root="packs",
                     tools_root="tools", static_dir=_static_dir_if_built())

    @app.post("/api/admin/shutdown")
    def _shutdown() -> dict:
        """优雅停机（仅 serve.py 进程注册；库/测试用法不受影响）：
        置 uvicorn should_exit → shutdown 钩子先落快照再退出。"""
        server = getattr(app.state, "uvicorn_server", None)
        if server is None:
            return {"status": "noop", "detail": "非 serve.py 进程，无 Server 句柄"}
        server.should_exit = True
        return {"status": "shutting_down"}

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="info"))
    app.state.uvicorn_server = server
    return app, server


def _run_headless(port: int) -> None:
    app, server = _make_server(port)
    print(f"obsius core API -> http://127.0.0.1:{port}")
    print(f"  文档: http://127.0.0.1:{port}/docs")
    if _static_dir_if_built():
        print("  前端: 同源静态托管（webui/dist）—— / 即 WebUI")
    server.run()


_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Obsius</title>
<style>
  body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#16181d;
       color:#c9d1d9;display:flex;align-items:center;justify-content:center;
       min-height:100vh;margin:0}
  .card{max-width:560px;background:#1d2026;border:1px solid #30363d;border-radius:10px;
        padding:28px 32px;line-height:1.7}
  h1{font-size:18px;color:#58d5c9;margin:0 0 12px}
  code{background:#0d1117;border:1px solid #30363d;border-radius:4px;
       padding:1px 6px;font-size:13px;color:#8bd5ca}
  p{margin:8px 0;font-size:14px}
</style></head><body><div class="card">
<h1>Obsius 桌面窗口</h1>
<p>后端与前端都没有可加载的地址。请任选其一：</p>
<p><b>① 静态模式（推荐）</b>：构建前端产物后重启本窗口——<br>
<code>cd webui &amp;&amp; npm run build</code></p>
<p><b>② 开发模式</b>：另开终端起 Vite dev（窗口会自动探测到 5173）——<br>
<code>cd webui &amp;&amp; npm run dev</code></p>
<p>后端服务：<code>python scripts/serve.py</code>（127.0.0.1:8420）。
本窗口关闭不会影响已在运行的服务。</p>
</div></body></html>"""


def _pick_url(port: int, explicit: str | None) -> str | None:
    """加载地址探测（desktop-app-shell §4.2）：--url 显式 > 静态版 > Vite dev > None。"""
    if explicit:
        return explicit
    if _static_dir_if_built() and _port_open(port):
        return f"http://127.0.0.1:{port}/"
    if _vite_dev_open():
        return "http://localhost:5173/"
    return None


_WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def _webview2_available() -> bool:
    """WebView2 运行时注册表探测（Win11 全量自带；Win10 偶缺，提示装一次）。"""
    if os.name != "nt":
        return True  # 非 Windows 交给 webview 自己报
    try:
        import winreg
    except ImportError:
        return True
    subs = (
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_ID}",
        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_ID}",
    )
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in subs:
            try:
                with winreg.OpenKey(root, sub):
                    return True
            except OSError:
                continue
    return False


def _warn_dialog(msg: str) -> None:
    print(f"[提示] {msg}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Obsius", 0x30)
    except Exception:  # noqa: BLE001 —— 弹窗失败不掩盖后续降级
        pass


def _redirect_stdio_if_none() -> None:
    """windowed exe/pythonw（无控制台）下 stdout/stderr 为 None，
    print/uvicorn 日志/argparse 输出均会炸 → 落 logs/serve-window.log（幂等）。"""
    if sys.stdout is None or sys.stderr is None:
        log_path = _ROOT / "logs" / "serve-window.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stderr = sys.stdout


def _run_window(port: int, explicit_url: str | None, debug: bool) -> int:
    _redirect_stdio_if_none()

    owner = not _port_open(port)

    # 依赖/运行时探测在 owner 判定之后、建服务之前——attach 模式同样要拦
    # （否则 pywebview 缺失时裸 ImportError 而非人类可读提示）
    try:
        import webview
    except ImportError:
        _warn_dialog("未安装 pywebview（pip install pywebview），窗口模式不可用。\n"
                     f"本次回退为无窗模式：服务照常运行，浏览器访问 http://127.0.0.1:{port}。")
        if owner:
            _run_headless(port)
        return 0 if owner else 1
    if not _webview2_available():
        _warn_dialog("未检测到 Microsoft Edge WebView2 运行时，桌面窗口无法启动。\n\n"
                     "安装一次即可（Win10 偶缺，Win11 自带）：\n"
                     "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                     f"本次回退为无窗模式：服务照常运行，浏览器访问 http://127.0.0.1:{port}。")
        if owner:
            _run_headless(port)
        return 0 if owner else 1

    server = None
    server_thread = None
    if owner:
        _app, server = _make_server(port)
        server_thread = threading.Thread(
            target=server.run, daemon=True, name="uvicorn")
        server_thread.start()
        deadline = time.time() + 30
        while not server.started and time.time() < deadline:
            time.sleep(0.1)
        if not server.started:
            print("[错误] 后端服务 30s 未就绪（详见上方 uvicorn 日志），退出",
                  file=sys.stderr)
            server.should_exit = True
            return 1

    url = _pick_url(port, explicit_url)
    if url is None:
        print("[提示] dist 未构建且 Vite dev 未启动，窗口显示指引占位页")

    remaining = [0]
    lock = threading.Lock()

    def _on_closed() -> None:
        with lock:
            remaining[0] -= 1
            last = remaining[0] <= 0
        if last and server is not None:
            # owner 最后一窗 = 优雅停机（进程内直调句柄，shutdown 钩子落快照）
            server.should_exit = True

    win = webview.create_window(
        "Obsius", url or _PLACEHOLDER_HTML,
        width=1480, height=920, min_size=(1100, 700))
    remaining[0] += 1
    win.events.closed += _on_closed
    webview.start(debug=debug)  # 阻塞至全部窗口关闭

    if server is not None:
        server.should_exit = True  # closed 处理器已置则幂等
        server_thread.join(timeout=20)  # 等 shutdown 钩子落完快照再退
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="obsius core API 服务（无窗/桌面窗口壳）")
    p.add_argument("port", nargs="?", type=int, default=8420,
                   help="监听端口（默认 8420）")
    p.add_argument("--window", action="store_true",
                   help="桌面窗口壳（pywebview/WebView2；端口在跑=attach 附窗，否则 owner）")
    p.add_argument("--url", default=None,
                   help="窗口加载地址（显式指定则跳过 dist/5173 探测顺序）")
    p.add_argument("--debug", action="store_true",
                   help="窗口右键可开 DevTools（pywebview debug）")
    return p.parse_args(argv)


def main() -> None:
    _redirect_stdio_if_none()  # windowed exe：进 main 即落日志（argparse 也可能输出）
    os.chdir(_ROOT)  # cwd 兜底：全项目 cwd 相对路径惯例，从别处启动不飘
    args = _parse_args(sys.argv[1:])
    if getattr(sys, "frozen", False):
        args.window = True  # M3 打包 exe：双击即弹窗（无窗形态用源码 serve.py，不设开关）
    if not args.window:
        _run_headless(args.port)
        return
    sys.exit(_run_window(args.port, args.url, args.debug))


if __name__ == "__main__":
    main()
