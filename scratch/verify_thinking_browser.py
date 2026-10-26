"""真浏览器验证 v51：前端 WS 是否收到并处理 ws.thinking 帧（无 id 瞬时帧）。

做法：init_script 给 WebSocket 包一层，把收到的每帧记进 window.__wsFrames。
不调 LLM、不改库结构——只从外部写一条快照，看它是否真的经 WS 到达页面。

用法：E:/Miniconda3/python.exe scratch/verify_thinking_browser.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8420"
PID = "proj-1030012ef2a3"
SLUG = "research-20261024-9e4d"
MARK = "浏览器探针思考标记七四二"

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

    db = os.path.join("workspaces", SLUG, "blackboard.db")
    errors: list[str] = []
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
        page.wait_for_timeout(2500)
        page.screenshot(path="scratch/out_wb_1.png", timeout=8000)
        print("已进入工作台；截图 scratch/out_wb_1.png")

        n_before = page.evaluate("window.__wsFrames.length")
        print("连接已建立，收到历史帧:", n_before)

        bb = Blackboard(db)
        # 线程 id 故意用页面当前不认识的值：这一轮只验「帧到达 + 前端不崩」，
        # 渲染归属由 AgentWorkbenchView 的 thread_id 过滤保证（见其 useMemo）。
        bb.upsert_delta_state(PID, "probe-browser", 0, MARK, seq=1)
        print("已写快照，等前端接收…")
        page.wait_for_timeout(2500)
        frames = page.evaluate("window.__wsFrames")
        bb.prune_delta_states(PID, "probe-browser")
        bb.close()

        thinking = [f for f in frames if f.get("kind") == "ws.thinking"]
        kinds: dict[str, int] = {}
        for f in frames:
            kinds[f.get("kind", "?")] = kinds.get(f.get("kind", "?"), 0) + 1
        print("前端收到帧统计:", kinds)
        page.screenshot(path="scratch/out_wb_2.png", timeout=8000)

        ok = True
        if not thinking:
            print("[失败] 前端没收到 ws.thinking 帧")
            ok = False
        else:
            h = thinking[-1]
            print("[通过] 前端收到 ws.thinking:",
                  {k: h.get(k) for k in ("thread_id", "step", "seq")},
                  "| text =", h.get("text"))
            if "id" in h:
                print("[失败] 帧带了 id，会污染游标")
                ok = False
            if h.get("text") != MARK:
                print("[失败] 帧文本不符")
                ok = False
        # 前端不该因为无 id 帧崩掉（游标/排序污染会在这里冒出来）
        real_errors = [e for e in errors if "favicon" not in e.lower()]
        print("控制台错误:", real_errors[:5] if real_errors else "无")
        if real_errors:
            ok = False
        browser.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
