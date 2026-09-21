"""启动 core API 服务（本机 127.0.0.1，WebUI/CLI 的后端）。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\serve.py          # 默认 127.0.0.1:8420
    E:\\Miniconda3\\python.exe scripts\\serve.py 8421     # 指定端口

v0.64 优雅停机：进程持有 uvicorn Server 句柄并挂 POST /api/admin/shutdown
（置 should_exit 优雅退出）——FastAPI shutdown 钩子借机给所有在跑会话落
断点快照（app.state.shutdown_persist），重启后任务不丢现场。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    import uvicorn
    from core.api import create_app

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8420
    app = create_app(workspace_root="workspaces", packs_root="packs", tools_root="tools")

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
    print(f"cyberstrike-pro core API -> http://127.0.0.1:{port}")
    print(f"  文档: http://127.0.0.1:{port}/docs")
    server.run()


if __name__ == "__main__":
    main()
