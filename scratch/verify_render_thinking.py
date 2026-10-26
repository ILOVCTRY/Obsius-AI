"""渲染验收：造一个 running 线程 + 写快照 → 思考面板必须显示 ws.thinking 的文本。

为什么需要「造」：真实对话里模型不一定会产生 thinking（本轮实测 provider 没返回），
而 `liveThinking` 的渲染条件是 `running && 有本轮 user 消息`。所以这里用库直接构造
这两个前提，只验**前端渲染那一段**（帧 → 面板文本）。

用完把线程删掉（不留垃圾）。

用法：E:/Miniconda3/python.exe scratch/verify_render_thinking.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8420"
PID = "proj-1030012ef2a3"
SLUG = "research-20261024-9e4d"
MARK = "渲染探针思考标记九三六"

HOOK = """
window.__wsFrames = [];
const _WS = window.WebSocket;
window.WebSocket = function (url, protocols) {
  const ws = protocols === undefined ? new _WS(url) : new _WS(url, protocols);
  ws.addEventListener('message', (ev) => {
    try { window.__wsFrames.push(JSON.parse(ev.data)); } catch (e) {}
  });
  return ws;
};
window.WebSocket.prototype = _WS.prototype;
Object.assign(window.WebSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
"""


def main() -> int:
    from core.blackboard.store import Blackboard
    from core.chat import store as chat_store

    db = os.path.join("workspaces", SLUG, "blackboard.db")
    bb = Blackboard(db)
    # 造前提：running + 一条本轮 user 消息（liveThinking 只消费 user 消息之后的事件）
    t = chat_store.create_thread(bb, PID, "pentest-controller",
                                 title="渲染探针（自动清理）")
    tid = t["id"]
    chat_store.append_message(bb, tid, "user", "渲染探针：随便说一句")
    chat_store.update_thread(bb, tid, status="running")
    print(f"已造线程 {tid}（running + 1 条 user 消息）")

    ok = False
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-proxy-server", "--disable-gpu"])
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.add_init_script(HOOK)
            page.on("console",
                    lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

            page.goto(BASE, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)
            page.get_by_text("逆向分析功能测试", exact=False).first.click(timeout=15000)
            page.wait_for_timeout(2000)
            page.get_by_text("渲染探针", exact=False).first.click(timeout=15000)
            page.wait_for_timeout(2000)
            page.screenshot(path="scratch/out_render_1.png", timeout=8000)
            before = page.inner_text("body", timeout=10000)
            print("选线程后页面含标记（应为 False）:", MARK in before)

            bb.upsert_delta_state(PID, tid, 0, MARK, seq=1)
            print("已写快照，等前端渲染…")
            page.wait_for_timeout(2500)
            after = page.inner_text("body", timeout=10000)
            page.screenshot(path="scratch/out_render_2.png", timeout=8000)
            n = page.evaluate(
                "window.__wsFrames.filter(f=>f.kind==='ws.thinking').length")
            print(f"前端收到 ws.thinking 帧 = {n}")
            print("渲染后页面含标记（应为 True）:", MARK in after)
            ok = MARK in after and MARK not in before
            browser.close()
    finally:
        bb.prune_delta_states(PID, tid)
        chat_store.delete_thread(bb, tid)
        bb.close()
        print("已清理探针线程")

    real_errors = [e for e in errors if "favicon" not in e.lower()]
    print("控制台错误:", real_errors[:3] if real_errors else "无")
    print("[通过] 思考面板按 ws.thinking 帧流式渲染" if ok else "[失败] 未渲染出帧文本")
    return 0 if ok and not real_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
