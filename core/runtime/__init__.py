"""core.runtime —— 执行环境层（DESIGN.md §7）。

对外入口：
    from core.runtime import ExecutionGateway, GatewayDenied, HostDetector, allowed_runtimes
"""

from core.runtime.backends import BackendError, DockerBackend, NativeBackend, WSLBackend
from core.runtime.detector import CapabilityInventory, HostDetector
from core.runtime.gateway import ExecutionGateway, ExecutionResult, GatewayDenied
from core.runtime.policy import allowed_runtimes

__all__ = [
    "ExecutionGateway",
    "ExecutionResult",
    "GatewayDenied",
    "HostDetector",
    "CapabilityInventory",
    "NativeBackend",
    "WSLBackend",
    "DockerBackend",
    "BackendError",
    "allowed_runtimes",
]
