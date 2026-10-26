"""真实鼠标点击 + 主进程日志双路取证。

渲染进程侧 openExternal 是 contextBridge 冻结对象，无法打桩（已验证 patchable=false、
Cannot redefine property）。因此计数证据改从主进程日志 logs/desktop.log 取证：
成功与失败都会写 `shell:open-external ...` 行。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CDP = "http://127.0.0.1:9222"
ROOT = Path(r"E:\ILOVCTRY\cyberstrike-pro")
LOG = ROOT / "logs" / "desktop.log"

ARM = r"""
() => {
  window.__d = { resolve: 0, clicks: [] };
  document.addEventListener('click', (ev) => {
    const a = ev.target && ev.target.closest ? ev.target.closest('a') : null;
    window.__d.clicks.push({ tag: ev.target && ev.target.tagName, href: a ? (a.getAttribute('href') || '').slice(0, 50) : null });
  }, true);
  const of = window.fetch;
  window.fetch = function (...args) {
    const u = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    if (u.includes('/api/intel/search/resolve')) window.__d.resolve++;
    return of.apply(this, args);
  };
  return { armed: true, hasOpenExternal: !!(window.desktopBrowser && window.desktopBrowser.openExternal) };
}
"""


def log_size() -> int:
    return LOG.stat().st_size if LOG.exists() else 0


def tail_since(offset: int) -> list[str]:
    if not LOG.exists():
        return []
    with LOG.open("rb") as fh:
        fh.seek(offset)
        return fh.read().decode("utf-8", "replace").splitlines()


def main() -> int:
    from playwright.sync_api import sync_playwright

    before = log_size()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        pages = [pg for ctx in browser.contexts for pg in ctx.pages
                 if pg.url.startswith("http://127.0.0.1:8420")]
        if not pages:
            print("未找到 UI 主窗")
            browser.close()
            return 1
        page = pages[0]
        print("装探针:", json.dumps(page.evaluate(ARM), ensure_ascii=False))

        # 先进情报视图（左侧导航），再切「检索」tab。
        # 壳重启后停在 Mission Control，直接找 tab 会超时。
        page.get_by_role("button", name="情报", exact=True).first.click()
        page.wait_for_timeout(2500)
        page.get_by_role("tab", name="检索", exact=True).first.click()
        page.wait_for_timeout(2000)

        links = page.locator('a[href*="news.google.com"]')
        n = links.count()
        print("Google 中转链接数:", n)
        if n == 0:
            print("无链接，先做一次检索")
            browser.close()
            return 1

        href = links.first.get_attribute("href") or ""
        print("即将点击:", href[:70])

        links.first.click()
        page.wait_for_timeout(9000)

        d = page.evaluate("() => window.__d")
        err = page.evaluate("""() => {
          const el = document.querySelector('[class*="destructive"], [data-error], .text-destructive');
          return el ? (el.textContent || '').trim().slice(0, 200) : '';
        }""")

        print("\n--- 渲染进程 ---")
        print("clicks:", json.dumps(d["clicks"], ensure_ascii=False))
        print("resolve 次数:", d["resolve"])
        print("页面错误文案:", repr(err))
        browser.close()

    lines = tail_since(before)
    hits = [l for l in lines if "shell:open-external" in l]
    print("\n--- 主进程日志（本次新增 %d 行，外链相关 %d 条）---" % (len(lines), len(hits)))
    for l in hits:
        print(" ", l)

    print("\n--- 判定 ---")
    if d["resolve"] == 0:
        print("[FAIL] onClick 未进入解析流程")
        return 1
    if not hits:
        print("[FAIL] 解析已发起，但主进程未收到 shell:open-external")
        print("  → 说明渲染进程侧 await openExternal 之前就抛错，或被 target=null 分支挡住")
        return 1
    if any("已交系统浏览器" in l for l in hits):
        print("[PASS] 已交给系统浏览器打开")
        return 0
    print("[WARN] 主进程收到调用但未成功（见日志）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
