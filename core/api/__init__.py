"""core API：HTTP + WebSocket 单一写入口（唯一允许 import fastapi 的模块）。"""

from core.api.app import create_app

__all__ = ["create_app"]
