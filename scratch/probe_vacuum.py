"""一次性测量：删掉 chat.thinking.delta 后 VACUUM 能把库缩到多少。

**只读原库**：先 shutil.copy2 到 scratch/，在副本上删+缩，测完删副本。
不碰 workspaces/ 下的任何真实数据。
"""
import os
import shutil
import sqlite3
import sys
import time

SRC = "workspaces/pentest-20261011-c3c7/blackboard.db"
DST = "scratch/_vacuum_probe.db"


def mb(n):
    return n / 1048576


def main():
    if os.path.exists(DST):
        os.remove(DST)
    t0 = time.time()
    shutil.copy2(SRC, DST)
    print(f"copy: {mb(os.path.getsize(DST)):.0f} MB, {time.time()-t0:.1f}s")

    c = sqlite3.connect(DST)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=60000")
    print(f"before: {mb(os.path.getsize(DST)):.0f} MB, "
          f"pages={c.execute('PRAGMA page_count').fetchone()[0]}")

    t0 = time.time()
    cur = c.execute("DELETE FROM events WHERE kind IN "
                    "('chat.thinking.delta','chat.delta')")
    c.commit()
    print(f"delete deltas: {cur.rowcount} rows, {time.time()-t0:.1f}s")
    print(f"after delete: {mb(os.path.getsize(DST)):.0f} MB, "
          f"freelist={c.execute('PRAGMA freelist_count').fetchone()[0]} pages")

    t0 = time.time()
    c.execute("VACUUM")
    print(f"vacuum: {time.time()-t0:.1f}s")
    c.close()
    print(f"AFTER VACUUM: {mb(os.path.getsize(DST)):.0f} MB")
    os.remove(DST)
    print("probe removed")


if __name__ == "__main__":
    sys.exit(main())
