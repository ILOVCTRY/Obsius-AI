"""诊断：点「样本分析」后页面到底停在哪（一次性脚本）。"""
import asyncio
import sys

from playwright.async_api import async_playwright

CDP = "http://127.0.0.1:9222"
TARGET = "火眼证据分析逆向"


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP)
        page = browser.contexts[0].pages[0]
        print("[url]", page.url)

        home = page.get_by_role("button", name="首页")
        if await home.count():
            await home.first.click()
            await page.wait_for_timeout(1200)
        await page.get_by_text(TARGET, exact=True).first.click()
        await page.wait_for_timeout(2500)

        nav = page.get_by_text("样本分析", exact=True)
        print("[nav count]", await nav.count())
        if await nav.count():
            box = await nav.first.bounding_box()
            print("[nav box]", box)
            await nav.first.click()
            await page.wait_for_timeout(2500)

        print("[url after]", page.url)
        # 所有可见按钮的可访问名
        names = await page.evaluate(
            """() => {
                const out = [];
                for (const b of document.querySelectorAll('button')) {
                    const r = b.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    out.push((b.innerText || b.getAttribute('title') || '').trim().slice(0, 40));
                }
                return out;
            }""")
        print("[buttons]", len(names))
        for n in names[:60]:
            print("   |", n)

        # 左侧栏文本
        body = await page.evaluate("() => document.body.innerText.slice(0, 1200)")
        print("[body]", body.replace("\n", " / ")[:1200])
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
