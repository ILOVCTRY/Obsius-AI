"""真浏览器验证：大附件 chip 显示真实进度 + 失败原因可见 + 胶囊不变形。

要点（MEMORY.md 踩过的坑）：
- 工作台**没有 file input**，附件只走粘贴/拖拽 → 必须构造真实 drop 事件；
- 本项目无 URL 路由，`goto("#/x")` 会静默重载 → 一律走真实点击链；
- 系统代理会拦本机请求 → `--no-proxy-server`；
- CDP 的合成 PointerEvent 过不了 React 的指针逻辑，但 drop 只需 DataTransfer，
  用 `evaluate_handle` 造一个带 files 的 DataTransfer 再 dispatchEvent 即可。

用法：E:/Miniconda3/python.exe scratch/verify_attachment_ui.py
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8420"
SHOT = Path(__file__).resolve().parent / "shots"
# 必须 > ATTACHMENT_CHUNKED_THRESHOLD(32MB)，才能命中分片路径。
# 取 40MB 而非 120MB：内容要在**页面内**生成，太大（base64 传 160MB 字符串）
# 会把渲染进程直接顶崩——第一版 120MB 就是这么 TargetClosed 的。
BIG_MB = 40


def write_blob(path: Path, mb: int) -> int:
    size = mb * 1024 * 1024
    block = bytes(range(256)) * 4096
    with open(path, "wb") as fh:
        written = 0
        while written < size:
            n = min(len(block), size - written)
            fh.write(block[:n])
            written += n
    return size


# 内容在页面内生成（不传大字符串）：只传 name 与 size，避免 base64 顶爆渲染进程。
DROP_JS = """
async ([name, size]) => {
  const buf = new Uint8Array(size);
  for (let i = 0; i < size; i++) buf[i] = i & 0xff;
  const file = new File([buf], name, { type: 'application/octet-stream' });
  const dt = new DataTransfer();
  dt.items.add(file);
  // 目标必须是挂 onDrop 的那个元素（.wb-composer）；派到 .wb-composer-wrap 上
  // React 的合成事件拿不到 handler，表现为"drop 了但 chip 不出现"。
  const target = document.querySelector('.wb-composer');
  if (!target) throw new Error('找不到 .wb-composer');
  const ev = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt });
  target.dispatchEvent(ev);
  return file.size;
}
"""


def main() -> int:
    SHOT.mkdir(exist_ok=True)
    blob_path = SHOT / f"traffic_{BIG_MB}mb.pcapng"
    size = BIG_MB * 1024 * 1024
    print(f"[准备] {blob_path.name} = {size} B（{BIG_MB}MB，必走分片）")

    problems: list[str] = []
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-proxy-server", "--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(BASE, wait_until="networkidle", timeout=25000)
        page.wait_for_timeout(1200)

        # 真实点击链：进入一个 CTF 项目 → 工作台
        page.get_by_text("CTF测试3").first.click()
        page.wait_for_timeout(2500)
        if not page.locator(".wb-composer-wrap").count():
            problems.append("未进入工作台 composer")
            browser.close()
            print("[FAIL]", problems)
            return 1

        # 注入 drop（内容在页面内生成）
        page.evaluate(DROP_JS, [blob_path.name, size])
        print("[注入] drop 事件已派发")

        # 采样 chip 状态：应看到「上传中 / N%」，结束时应变回字节数
        samples: list[str] = []
        pct_seen = False
        shot_taken = False
        deadline = time.time() + 180
        while time.time() < deadline:
            chip = page.locator(".wb-composer-wrap").locator("span").filter(has_text=blob_path.name)
            txt = ""
            if chip.count():
                txt = chip.first.inner_text().replace("\n", " ").strip()
            if txt and txt not in samples:
                samples.append(txt)
            if "%" in txt:
                pct_seen = True
                if not shot_taken:
                    page.screenshot(path=str(SHOT / "chip_progress.png"))
                    shot_taken = True
            # 结束条件：出现百分比 100% 或 chip 上不再有「上传中」
            if txt and "%" not in txt and "上传中" not in txt and pct_seen:
                break
            page.wait_for_timeout(200)

        print(f"[采样] {len(samples)} 个状态：")
        for s in samples[:10]:
            print(f"   - {s}")
        if not pct_seen:
            problems.append("chip 全程没显示百分比（进度未推上去）")
        if not shot_taken:
            page.screenshot(path=str(SHOT / "chip_noprog.png"))

        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOT / "chip_ready.png"))
        final = page.locator(".wb-composer-wrap").inner_text().replace("\n", " ")
        print(f"[最终] {final[:120]}")
        if "MB" not in final and blob_path.name not in final:
            problems.append("上传结束后 chip 未显示文件（可能已消失）")

        # 反向：错误原因必须可见。用例要挑**必然失败**的那个：
        # 空文件（0 字节）走单请求 → 服务端 422「空文件」。
        # 别用非法文件名试——`_safe_upload_name` 会把控制符替换成 `_` 后照常成功，
        # 那样只会验出"上传成功"，白跑。（第一版就是这么踩的）
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.get_by_text("CTF测试3").first.click()
        page.wait_for_timeout(2000)
        if page.locator(".wb-composer-wrap").count():
            page.evaluate(DROP_JS, ["empty_probe.bin", 0])
            page.wait_for_timeout(3000)
            chip_txt = page.locator(".wb-composer-wrap").inner_text().replace("\n", " ")
            print(f"[错误态] chip = {chip_txt[:120]}")
            page.screenshot(path=str(SHOT / "chip_error.png"))
            if "空文件" in chip_txt:
                print("[错误态] chip 显示了服务端原文「空文件」✓")
            elif "上传失败" in page.content() or "重试" in chip_txt:
                problems.append("失败态可见但没带原因（仍是旧行为）")
            else:
                problems.append("失败时 chip 没给出任何原因文案")
        browser.close()

    blob_path.unlink(missing_ok=True)
    # 错误态用例会**故意**打出一个 422，浏览器对任何非 2xx 都记一条 console error。
    # 那是用例本身产生的预期噪音，不能算产品缺陷——否则这个验证永远红，等于废弃。
    expected_noise = ("Failed to load resource", "422")
    real_errors = [e for e in errors
                   if not (all(k in e for k in expected_noise))]
    if real_errors:
        print(f"[console error] {len(real_errors)} 条（已排除故意触发的 422）:")
        for e in real_errors[:5]:
            print("  ", e)
        problems.append(f"{len(real_errors)} 条非预期 console error")
    elif errors:
        print(f"[console error] {len(errors)} 条均为故意触发的 422 噪音（不计）")
    print()
    if problems:
        print("[FAIL]")
        for x in problems:
            print("  -", x)
        return 1
    print("[PASS] 进度可见 / 错误原因可见 / 0 console error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
