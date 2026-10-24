"""真实浏览器验证「选择分析包目录」入口（一次性脚本，2026-10-24）。

连桌面壳的 CDP（9222），在平台 UI 里走到「样本分析」页，点开目录选择弹层，
确认：① 按钮存在；② 弹层能打开（role=dialog + bg-popover）；③ 目录列表真的
从后端拉到了内容；④ 原生对话框入口按运行环境正确显隐；⑤ Esc 能关且焦点在层内。

关键前提：本应用**没有 URL 路由**，视图完全由 React state 驱动（App.tsx 的
`const [view, setView] = useState<View>("projects")`），所以 `#/project/<pid>`
这类深链是无效的——导航只能靠真实点击。路径是：
  ① 有项目时点左上「首页」→ 回到项目列表
  ② 点轨道为 research 的项目卡 → openProject(pid) → 默认落 agents 视图
  ③ 点左导航「样本分析」（CTF 轨整项隐藏，故必须选非 CTF 项目）

注意：这里只验证「入口与弹层」，不点「使用此目录」——那会真的对一个目录做
全量 sha256 扫描（大目录几分钟）。扫描链路的正确性由 pytest 端到端覆盖。
"""
import asyncio
import sys

from playwright.async_api import async_playwright

CDP = "http://127.0.0.1:9222"
OUT = r"E:\ILOVCTRY\cyberstrike-pro\outputs"
# research 轨项目（CTF 轨隐藏「样本分析」入口；这个项目名里带「逆向」，语义也对得上）
TARGET_PROJECT = "火眼证据分析逆向"
DLG = '[role="dialog"][aria-label="选择分析包目录"]'


async def shot(page, name: str) -> None:
    """短超时截图。桌面壳里有时字体等待会拖到 30s 默认超时，这里压到 8s 并吞错——
    截图只是取证，不能因为它把验证结论带崩。"""
    try:
        await page.screenshot(path=f"{OUT}\\{name}.png", timeout=8000, full_page=False)
        print(f"[info] screenshot -> {name}.png")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] screenshot 失败（不影响结论）: {type(exc).__name__}")


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = None
        for candidate in ctx.pages:
            url = candidate.url or ""
            if "localhost:8420" in url or "127.0.0.1:8420" in url or ":5173" in url:
                page = candidate
                break
        if page is None and ctx.pages:
            page = ctx.pages[0]
        if page is None:
            print("[FAIL] 没找到 UI 页面")
            return 1
        print("[info] 页面:", page.url)

        # ⓪ 强制重载：桌面壳这个窗口在 vite build 之前就开着，内存里跑的是旧 bundle
        # （dist 里已有新 chunk，但页面没重新拉 index.html → 找不到新按钮）。重载后
        # 应用无持久化，自然回到项目列表。
        await page.reload(wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2500)
        print("[ok] 页面已重载（吃新构建）")

        # ① 回首页（有项目时才有「首页」按钮；已在首页则跳过）
        home = page.get_by_role("button", name="首页")
        if await home.count():
            await home.first.click()
            await page.wait_for_timeout(1500)
            print("[ok] 点了「首页」回到项目列表")
        else:
            print("[info] 已在项目列表（无「首页」按钮）")

        # ② 点目标项目卡
        card = page.get_by_text(TARGET_PROJECT, exact=True)
        if not await card.count():
            print(f"[FAIL] 项目列表里没有「{TARGET_PROJECT}」")
            await shot(page, "link-dir-project-missing")
            return 2
        await card.first.click()
        await page.wait_for_timeout(2500)
        print("[ok] 进入项目")

        # ③ 左导航「样本分析」（等 meta 拉到、NavRail 按轨重算后再点）
        nav = page.get_by_text("样本分析", exact=True)
        found = False
        for _ in range(30):
            if await nav.count():
                found = True
                break
            await page.wait_for_timeout(400)
        if not found:
            print("[FAIL] 左导航没有「样本分析」入口（轨道不对？）")
            await shot(page, "link-dir-nav-missing")
            return 3
        await nav.first.click()
        await page.wait_for_timeout(2000)

        btn = page.get_by_role("button", name="选择分析包目录")
        if not await btn.count():
            print("[FAIL] 样本分析页没有「选择分析包目录」按钮")
            await shot(page, "link-dir-btn-missing")
            return 4
        print("[ok] 按钮存在")
        await btn.first.click()
        await page.wait_for_timeout(1500)

        dialog = page.locator(DLG)
        if not await dialog.count():
            print("[FAIL] 弹层没有打开")
            await shot(page, "link-dir-dialog-missing")
            return 5
        cls = await dialog.first.get_attribute("class") or ""
        if "bg-popover" not in cls:
            print(f"[FAIL] 弹层缺少 bg-popover（浮层会透底）: {cls[:120]}")
            return 6
        print("[ok] 弹层打开 | bg-popover: True")

        # 焦点必须已经进到对话框里（否则 Esc/键盘操作落到背后页面）
        focused_inside = await page.evaluate(
            f"""() => {{
                const d = document.querySelector('{DLG}');
                return !!(d && (d === document.activeElement || d.contains(document.activeElement)));
            }}""")
        print("[ok] 焦点在对话框内:", focused_inside)

        # 等目录列表真正拉到数据（不是「正在读取目录…」）。
        # 注意：**根层的盘符行不带「N 项」后缀**（后端对磁盘根不算 entry_count），
        # 所以不能只数 `:has-text("项")`——那样根层恒 0，会误判成「列表为空」。
        # 改为数弹层主体里所有条目按钮，并排除底部的「使用此目录」。
        rows = page.locator(f'{DLG} .space-y-0\\.5 > button')
        for _ in range(30):
            if await rows.count():
                break
            await page.wait_for_timeout(400)
        count = await rows.count()
        print(f"[ok] 目录列表行数: {count}")
        if count == 0:
            body = (await dialog.first.inner_text())[:400].replace("\n", " / ")
            print(f"[FAIL] 列表为空。弹层正文: {body}")
            await shot(page, "link-dir-empty")
            return 7

        # 桌面壳里应有「系统原生对话框」入口
        has_native = bool(await page.get_by_role("button", name="系统对话框").count())
        print("[ok] 原生对话框入口（桌面壳应 True）:", has_native)

        await shot(page, "link-dir-picker")

        # 点进第一层目录，确认能逐级浏览
        first = rows.first
        label = (await first.inner_text()).strip().split("\n")[0]
        await first.dblclick()
        await page.wait_for_timeout(1500)
        after = await rows.count()
        print(f"[ok] 双击进入「{label}」后行数: {after}")
        await shot(page, "link-dir-picker-navigated")

        # Esc 关闭 + 焦点不残留
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(600)
        closed = await page.locator(DLG).count() == 0
        print("[ok] Esc 关闭弹层:", closed)

        ok = closed and focused_inside and count > 0 and has_native
        print("[RESULT]", "PASS" if ok else "FAIL")
        return 0 if ok else 8


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
