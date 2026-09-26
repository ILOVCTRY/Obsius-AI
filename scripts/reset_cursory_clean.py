"""回退粗略收口的 tested_clean 资产（tested-clean-intent-backing 配套，2026-09-25）。

背景：中原工学院项目（assessment-20260915-7d70）2026-09-24 两个会话
（sess-05b9dc2b6783「同模板 200 随批次收口」92 条 / sess-130528d12a81
「80/443 各 1 GET」17 条）在无意图、无证据记录下把 109 个叶子资产标为
tested_clean。平台门禁已收紧（clean 须 closed/dead_end 意图背书），
本脚本把这批存量脏值回退到收口前状态（事件 payload.old：open/visited）。

只回退**当前状态仍为 tested_clean** 的资产；之后已被流转过的不动。
写操作全部走 Blackboard.set_asset_status（author=demo-script(reset-clean)）。
默认 dry-run 只打印计划；--apply 落库。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\reset_cursory_clean.py                  # dry-run
    E:\\Miniconda3\\python.exe scripts\\reset_cursory_clean.py --apply          # 落库
    E:\\Miniconda3\\python.exe scripts\\reset_cursory_clean.py <slug> --sessions=sess-a,sess-b --apply
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.projects import ProjectStore

AUTHOR = "demo-script(reset-clean)"
DEFAULT_SLUG = "assessment-20260915-7d70"
DEFAULT_SESSIONS = ("sess-05b9dc2b6783", "sess-130528d12a81")


def collect_resets(bb, pid: str, sessions: set[str]
                   ) -> list[tuple[str, str, str, str]]:
    """待回退清单 [(asset_id, old_status, by, 原 note)]。

    同一资产多条目标会话 clean 事件 → 取最早一条的 old（回到首次被标 clean
    之前）；当前状态非 tested_clean 的资产剔除。
    """
    first: dict[str, tuple[str, str, str]] = {}
    for e in bb.conn.execute(
        "SELECT payload FROM events"
        " WHERE project_id=? AND kind='asset.status_changed'"
        " ORDER BY id",
        (pid,),
    ):
        p = json.loads(e["payload"])
        if p.get("new") != "tested_clean" or p.get("by") not in sessions:
            continue
        aid = p.get("asset_id")
        if aid and aid not in first:
            first[aid] = (p.get("old") or "open", p.get("by") or "",
                          (p.get("note") or "")[:80])
    out = []
    for aid, (old, by, old_note) in first.items():
        row = bb.conn.execute(
            "SELECT status FROM assets WHERE id=? AND project_id=?",
            (aid, pid),
        ).fetchone()
        if row is not None and row["status"] == "tested_clean":
            out.append((aid, old, by, old_note))
    return out


def main() -> None:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    sessions = set(DEFAULT_SESSIONS)
    for a in sys.argv[1:]:
        if a.startswith("--sessions="):
            sessions = {s.strip() for s in a.split("=", 1)[1].split(",") if s.strip()}
    slug = positional[0] if positional else DEFAULT_SLUG

    store = ProjectStore()
    project = store.open_project(slug)
    try:
        bb, pid = project.bb, project.id
        plan = collect_resets(bb, pid, sessions)
        print(f"== {slug}：{len(plan)} 个 tested_clean 资产待回退"
              f"（会话 {sorted(sessions)}）==")
        from collections import Counter
        print("目标 old 分布:", dict(Counter(old for _, old, _, _ in plan)))
        done = skipped = 0
        for aid, old, by, old_note in plan:
            if not apply:
                print(f"  [reset→{old}] {aid}（{by}：{old_note}）")
                continue
            try:
                bb.set_asset_status(
                    aid, old,
                    note=f"回退 {by} 粗略收口（原注：{old_note}）——"
                         "tested_clean 须死路意图背书，重测后按门禁收口",
                    author=AUTHOR)
                done += 1
                print(f"  [reset→{old}] {aid}")
            except (ValueError, LookupError) as ex:
                skipped += 1
                print(f"  [跳过] {aid} — {str(ex)[:100]}")
        if apply:
            print(f"\n完成：回退 {done} 个，跳过 {skipped} 个")
        else:
            print("\n（dry-run：加 --apply 落库）")
    finally:
        project.close()


if __name__ == "__main__":
    main()
