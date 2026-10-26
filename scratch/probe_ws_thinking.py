"""验证 v51 投递链路：写一条思考快照 → WS 应收到无 id 的 ws.thinking 帧。

只测**后端**这一段（upsert → recent_delta_states → ws_events 推帧）。
前端渲染另用真浏览器验证。

用法：E:/Miniconda3/python.exe scratch/probe_ws_thinking.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

sys.path.insert(0, ".")

BASE = "http://127.0.0.1:8420"
# 系统代理（127.0.0.1:6023）会拦本机请求 → 必须显式清空
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path: str):
    with OPENER.open(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


async def main() -> int:
    import websockets

    from core.blackboard.store import Blackboard

    projects = get("/api/projects")
    if not projects:
        print("没有项目，无法验证")
        return 2
    # 挑最近有对话的库（events 最多的那个更可能在用）
    proj = projects[0]
    pid, slug = proj["id"], proj["slug"]
    db = os.path.join("workspaces", slug, "blackboard.db")
    print(f"project={pid} slug={slug} db={db}")

    uri = f"ws://127.0.0.1:8420/api/ws/projects/{pid}"
    frames: list[dict] = []

    async with websockets.connect(uri, max_size=8 << 20) as ws:
        async def reader():
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    frames.append(json.loads(raw))
            except Exception:
                return

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.5)
        bb = Blackboard(db)
        # 模拟 3 帧连续增量（覆盖语义：表里应恒 1 行）
        for i, txt in enumerate(("探针思考", "探针思考：第二帧", "探针思考：第二帧，第三帧"), 1):
            bb.upsert_delta_state(pid, "probe-thread-v51", 0, txt, seq=i)
            await asyncio.sleep(0.15)
        rows = bb.conn.execute(
            "SELECT COUNT(*) FROM chat_delta_state WHERE thread_id=?",
            ("probe-thread-v51",)).fetchone()[0]
        print(f"写完 3 帧后表内行数 = {rows}（应为 1 = 覆盖语义）")
        await asyncio.sleep(1.2)
        bb.prune_delta_states(pid, "probe-thread-v51")
        bb.close()
        task.cancel()

    kinds: dict[str, int] = {}
    for f in frames:
        kinds[f.get("kind", "?")] = kinds.get(f.get("kind", "?"), 0) + 1
    print("收到帧统计:", kinds)
    hits = [f for f in frames if f.get("kind") == "ws.thinking"]
    if not hits:
        print("[失败] 没有收到 ws.thinking 帧")
        return 1
    h = hits[-1]
    print("[通过] 末帧:", {k: h.get(k) for k in ("thread_id", "step", "seq", "ts")},
          "| text =", h.get("text"))
    if "id" in h:
        print("[失败] ws.thinking 带了 id —— 会污染前端游标")
        return 1
    if rows != 1:
        print("[失败] 快照没有覆盖语义")
        return 1
    print("[通过] 无 id、覆盖语义、投递正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
