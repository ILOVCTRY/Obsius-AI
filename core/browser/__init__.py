"""内置浏览器能力（F6，DESIGN.md §7）：Playwright 托管 Chromium + 抓包/重发/爆破。

import 红线：本包任何模块 import 时不得 import playwright——只在
BrowserInstance 的 loop 线程内延迟 import（未装 browser extra 时
``import core.browser`` 不炸，no-tool 降级的前提）。
"""

from core.browser.pool import (
    BrowserConfig, BrowserError, BrowserInstance, BrowserPool,
    browser_available, chromium_available,
)

__all__ = [
    "BrowserConfig", "BrowserError", "BrowserInstance", "BrowserPool",
    "browser_available", "chromium_available",
]
