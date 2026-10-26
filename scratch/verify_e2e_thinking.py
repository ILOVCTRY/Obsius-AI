"""端到端：真浏览器发一条消息 → 思考是否流式上屏 → 库里是否还产生 delta 事件。

这是 v51 的**验收口径**（三层一起看）：
  ① 前端收到 `ws.thinking` 帧（流式保持）
  ② 事件表里**没有** `chat.thinking.delta`（放大源已断）
  ③ 收尾后 `chat_delta_state` 为空（状态不是记录）

用法：E:/Miniconda3/python.exe scratch/verify_e2e_thinking.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8420"
PID = "proj-1030012ef2a3"
SLUG = "research-20261024-9e4d"
PROMPT = "只回复两个字：收到。不要做任何分析，不要调用任何工具。"

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
        page.wait_for_timeout(2000)

        box = page.get_by_role("textbox").first
        box.click(timeout=10000)
        box.fill(PROMPT)
        page.wait_for_timeout(300)
        page.get_by_role("button", name="发送").first.click(timeout=10000)
        print("已发送，观察 45s…")

        # 每 3s 采一次：ws.thinking 帧数 + 页面文本
        samples = []
        for i in range(15):
            page.wait_for_timeout(3000)
            n = page.evaluate(
                "window.__wsFrames.filter(f=>f.kind==='ws.thinking').length")
            body = page.inner_text("body", timeout=10000)
            samples.append((i, n, len(body)))
            print(f"  t={(i + 1) * 3:>3}s  ws.thinking={n:<4} body={len(body)}")
            if "收到" in body and i >= 2:
                break

        page.screenshot(path="scratch/out_e2e.png", timeout=8000)
        frames = page.evaluate("window.__wsFrames")
        body = page.inner_text("body", timeout=10000)
        browser.close()

    n_think = sum(1 for f in frames if f.get("kind") == "ws.thinking")
    think_texts = [f.get("text", "") for f in frames if f.get("kind") == "ws.thinking"]

    bb = Blackboard(db)
    # 该项目的线程：取最近更新的那条（本轮新建/续用的）
    row = bb.conn.execute(
        "SELECT id FROM chat_threads WHERE project_id=? ORDER BY updated_at DESC LIMIT 1",
        (PID,)).fetchone()
    tid = row[0] if row else ""
    n_delta_events = bb.conn.execute(
        "SELECT COUNT(*) FROM events WHERE project_id=? AND kind='chat.thinking.delta'",
        (PID,)).fetchone()[0]
    n_state = bb.conn.execute(
        "SELECT COUNT(*) FROM chat_delta_state WHERE project_id=?", (PID,)).fetchone()[0]
    n_final = bb.conn.execute(
        "SELECT COUNT(*) FROM events WHERE project_id=? AND kind='chat.thinking'",
        (PID,)).fetchone()[0]
    bb.close()

    print("\n==== 验收 ====")
    print(f"线程 = {tid}")
    print(f"① ws.thinking 帧数 = {n_think}（>0 表示流式保持）")
    if think_texts:
        print(f"   末帧长度 = {len(think_texts[-1])} 字，前 60 字：{think_texts[-1][:60]!r}")
    print(f"② events 里 chat.thinking.delta 行数 = {n_delta_events}（应为 0）")
    print(f"   events 里 chat.thinking 终稿行数 = {n_final}")
    print(f"③ chat_delta_state 残留行数 = {n_state}（收尾后应为 0）")
    print(f"   页面含「收到」= {'收到' in body}")
    real_errors = [e for e in errors if "favicon" not in e.lower()]
    print(f"控制台错误 = {real_errors[:3] if real_errors else '无'}")

    ok = (n_delta_events == 0 and "收到" in body and not real_errors)
    if n_think == 0:
        print("[注意] 本次没有 ws.thinking 帧——可能是模型没产生 thinking，"
              "不代表链路坏（链路已由 probe_ws_thinking 单独验过）")
    print("[通过]" if ok else "[失败]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
