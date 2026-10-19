"""core.runtime —— 执行环境层（DESIGN.md §7）。

对外入口：
    from core.runtime import ExecutionGateway, GatewayDenied, HostDetector, allowed_runtimes
"""

from core.runtime.backends import BackendError, DockerBackend, NativeBackend, WSLBackend
from core.runtime.detector import CapabilityInventory, HostDetector
from core.runtime.gateway import ExecutionGateway, ExecutionResult, GatewayDenied
from core.runtime.policy import allowed_runtimes
from core.runtime.recovery import (
    EnvironmentErrorCategory,
    EnvironmentErrorKind,
    EnvironmentIssue,
    EnvironmentRecovery,
    EnvironmentRecoveryManager,
    EnvironmentUnavailable,
    RecoveryRequest,
    RecoveryResult,
    classify_backend_error,
    classify_execution_failure,
    environment_issue_from_dict,
)

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
    "EnvironmentErrorCategory",
    "EnvironmentErrorKind",
    "EnvironmentIssue",
    "EnvironmentUnavailable",
    "RecoveryRequest",
    "RecoveryResult",
    "EnvironmentRecovery",
    "EnvironmentRecoveryManager",
    "classify_backend_error",
    "classify_execution_failure",
    "environment_issue_from_dict",
]
