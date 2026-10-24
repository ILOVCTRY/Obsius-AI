"""统计历史 chat.error 的类别分布（读全库）。"""
import glob
import json
import sqlite3
import sys

from collections import Counter

def main():
    counter = Counter()
    traced = []
    for p in glob.glob("workspaces/*/blackboard.db"):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            rows = c.execute(
                "select payload, created_at from events where kind='chat.error'"
            ).fetchall()
        except Exception:
            continue
        for payload, ts in rows:
            try:
                d = json.loads(payload)
            except Exception:
                continue
            cat = d.get("category") or "?"
            counter[cat] += 1
            if cat == "unknown":
                traced.append((p, ts, (d.get("message") or "")[:90]))
    total = sum(counter.values())
    print(f"chat.error 总计 {total}")
    for cat, n in counter.most_common():
        print(f"  {cat:<16} {n}")
    print("\nunknown 明细：")
    for t in traced[-15:]:
        print(" ", t)


if __name__ == "__main__":
    sys.exit(main())
