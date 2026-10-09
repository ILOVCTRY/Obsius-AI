"""代理池（fir-proxy 托管）——项目级 serve 生命周期 + 池记录 + 控制通道。

见 ``pool.py`` 与 ``docs/plans/proxy-pool-integration.md``。
"""

from core.proxy.pool import (  # noqa: F401
    ProxyConfig,
    ProxyError,
    ProxyPool,
    proxy_available,
    resolve_python,
    select_records,
)

__all__ = ["ProxyConfig", "ProxyError", "ProxyPool", "proxy_available",
           "resolve_python", "select_records"]
