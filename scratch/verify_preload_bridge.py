"""确认桌面壳 preload 的 pickDirectory 桥真的注入了（一次性脚本）。"""
import asyncio
import sys

from playwright.async_api import async_playwright


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        page = browser.contexts[0].pages[0]
        info = await page.evaluate(
            """() => {
                const d = window.desktopBrowser;
                return {
                    hasBridge: !!d,
                    pickDirectoryType: d ? typeof d.pickDirectory : "n/a",
                    keys: d ? Object.keys(d).length : 0,
                };
            }""")
        print("[bridge]", info)
        ok = info.get("hasBridge") and info.get("pickDirectoryType") == "function"
        print("[RESULT]", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
