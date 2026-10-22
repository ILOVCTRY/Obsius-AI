"""情报库的进程级共享访问（intel-pentest-link，2026-10-22）。

**为什么要共享**：`intel_vulns` 工具在 `ToolDispatcher` 里执行，而每个会话线程各持一个
dispatcher——若每个 dispatcher 各 `IntelStore(...)` 一次，同一份 intel.db 会被开出几十条
连接（SQLite WAL 能扛，但每条连接都要跑一遍 PRAGMA + DDL + 迁移，白烧启动时间，且
`_lock` 是**实例级**的，多实例之间并不互斥）。

**为什么不用 app.state**：`app.state.intel` 只在 API 层（`_intel()` 惰性建库），而
`ToolDispatcher` 的构造点在 `core/chat/runtime.py` 与 `core/agent/loop.py`，两者都拿不到
`app`。工具层是纯执行层，不该反向依赖 Web 框架。

**口径**：按 **解析后的绝对路径**缓存单实例；路径不同（测试用 tmp 目录）= 不同实例。
`set_shared` 是测试注入口（传入预置 store 后不再建库），`reset_shared` 清缓存。
任何异常都让调用方自己接——本模块不吞错，情报读不到要明确告诉模型。
"""

from __future__ import annotations

import threading
from pathlib import Path

from core.intel.store import IntelStore

__all__ = ["DEFAULT_INTEL_DIR", "get_shared", "set_shared", "reset_shared",
           "set_default_dir"]

# 与 app.py 的 `intel_dir` 默认值同源（那边是 API 入参默认，这边是工具层默认）
DEFAULT_INTEL_DIR = Path("config") / "intel"

_lock = threading.Lock()
_stores: dict[str, IntelStore] = {}
_override: IntelStore | None = None
# 默认目录可被 API 层改写（`create_app(intel_dir=...)` 传自定义路径时，工具层必须
# 跟到同一份库——否则「情报面板看得到、工具查不到」）
_default_dir: Path = DEFAULT_INTEL_DIR


def set_default_dir(intel_dir: str | Path | None) -> None:
    """API 层装配时调用：把工具层的默认情报目录对齐到 `create_app(intel_dir=)`。"""
    global _default_dir
    _default_dir = Path(intel_dir) if intel_dir else DEFAULT_INTEL_DIR


def _key(intel_dir: str | Path) -> str:
    try:
        return str(Path(intel_dir).resolve())
    except OSError:
        return str(intel_dir)


def get_shared(intel_dir: str | Path | None = None) -> IntelStore:
    """取（必要时建）该目录下的共享 IntelStore。"""
    if _override is not None:
        return _override
    path = intel_dir or _default_dir
    k = _key(path)
    with _lock:
        st = _stores.get(k)
        if st is None:
            st = IntelStore(path)
            _stores[k] = st
        return st


def set_shared(store: IntelStore | None) -> None:
    """测试注入：非 None 时 `get_shared` 恒返回它（不再建库/读盘）。"""
    global _override
    with _lock:
        _override = store


def reset_shared() -> None:
    """清空缓存（测试隔离用；不 close——调用方可能仍持有引用）。"""
    global _override, _default_dir
    with _lock:
        _stores.clear()
        _override = None
        _default_dir = DEFAULT_INTEL_DIR
