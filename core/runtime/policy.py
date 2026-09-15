"""隔离策略（DESIGN.md §7 执行网关与隔离等级）。

核心规则：WSL 信任级 = 宿主机。不可信代码只允许容器；
未知威胁等级一律按恶意样本处理（安全默认值，宁严勿松）。
"""

from enum import IntEnum


class Level(IntEnum):
    L0_HOST = 0     # 宿主原生：本项目自身代码、静态分析
    L1_WSL = 1      # WSL2：半可信工具（与宿主同级）
    L2_DOCKER = 2   # 普通容器：不可信代码默认环境
    L3_SANDBOX = 3  # 加固沙箱：活体恶意样本专用


# runtime 名称 → 隔离等级
RUNTIME_LEVELS: dict[str, Level] = {
    "host": Level.L0_HOST,
    "wsl": Level.L1_WSL,
    "docker": Level.L2_DOCKER,
    "sandbox": Level.L3_SANDBOX,
}

# threat_class → 允许的等级集合
THREAT_ALLOWED: dict[str, set[Level]] = {
    "trusted": {Level.L0_HOST, Level.L1_WSL, Level.L2_DOCKER, Level.L3_SANDBOX},
    "untrusted": {Level.L2_DOCKER, Level.L3_SANDBOX},
    "malware_live": {Level.L3_SANDBOX},
}

VALID_THREAT_CLASSES = set(THREAT_ALLOWED) | {"unknown"}


def allowed_levels(threat_class: str) -> set[Level]:
    """未知（unknown）→ 按恶意样本处理（§7：安全默认值）。"""
    return THREAT_ALLOWED.get(threat_class, THREAT_ALLOWED["malware_live"])


def allowed_runtimes(threat_class: str) -> set[str]:
    return {
        name for name, lv in RUNTIME_LEVELS.items() if lv in allowed_levels(threat_class)
    }


# L3 沙箱网络模式（§7）：real 永不默认，需人工审批
NET_MODES = {"none", "fakenet", "real"}
DEFAULT_NET_MODE = "none"  # MVP 先实现 none；fakenet(INetSim sidecar) 为下一里程碑
