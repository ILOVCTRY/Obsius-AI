"""启动 core API 服务（本机 127.0.0.1，WebUI/CLI 的后端）。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\serve.py          # 默认 127.0.0.1:8420
    E:\\Miniconda3\\python.exe scripts\\serve.py 8421     # 指定端口
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    import uvicorn
    from core.api import create_app

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8420
    app = create_app(workspace_root="workspaces", packs_root="packs", tools_root="tools")
    print(f"cyberstrike-pro core API -> http://127.0.0.1:{port}")
    print(f"  文档: http://127.0.0.1:{port}/docs")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
