"""黑板库瘦身：删除流式增量行并（可选）VACUUM 回收文件。

**为什么能删**：`chat.thinking.delta` / `chat.delta` 每条都带**累计全文**
（实测 seq=1 长 3 → seq=8 长 64），前端只取最后一条覆盖上屏，终稿全文落在
`chat_messages.thinking` / `content`。抽 30 条 delta 验证：**30/30 都是终稿的
前缀**，最长那条与终稿完全相等 —— 所以「残留 delta = 被中断思考的现场审计」
这个旧说法不成立，审计现场就是终稿，删 delta 零信息损失。

**v51 起（2026-10-26）平台不再产生新的 delta**：`chat.thinking.delta` 已降级为
`chat_delta_state` 状态快照（每线程每步一行，覆盖语义），从根上消除了平方级
放大（原全量 8.19 GB / 306 万行）。本脚本因此**只用于清历史存量**，跑一次即可。

**实测收益**（`pentest-20261011-c3c7`）：2004 MB → 272 MB（-86%），
删 597,563 行 35.6s + VACUUM 8.3s。

**VACUUM 之前先看 `PRAGMA freelist_count`**：实测 17 个库合计 18.65 GB 里
有 5.75 GB（31%）是「删了没 VACUUM」的空闲页（`auto_vacuum=0` 下删数据不缩
文件）。单库最高 99% 是空闲页 —— 这种库光 VACUUM 就能还回来，不用删任何行。

用法::

    python scripts/db_compact.py                  # 只报告，不动库
    python scripts/db_compact.py --apply          # 真删（不 VACUUM，文件不缩）
    python scripts/db_compact.py --apply --vacuum # 删完缩文件（需 2x 临时磁盘）

**跑之前必须停平台**：VACUUM 要独占数据库；且运行中的进程持有连接，
删完它会立刻把新 delta 写回来。脚本检测到 `-wal` 有内容会拒绝 --vacuum。
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time

DELTA_KINDS = ("chat.thinking.delta", "chat.delta")
DEFAULT_GLOB = "workspaces/*/blackboard.db"


def _mb(n: float) -> float:
    return n / 1048576


def _probe(path: str, *, with_live: bool = False) -> dict:
    """只读探测：库大小、delta 行数/字节、运行中线程数、wal 是否非空。

    一次 `GROUP BY kind` 扫完整表（每个库一次全扫），不再按 kind 分别查——
    17 个库合计 18 GB，查两遍就是两倍 IO。
    """
    size = os.path.getsize(path)
    wal = path + "-wal"
    wal_bytes = os.path.getsize(wal) if os.path.exists(wal) else 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = nbytes = 0
        for kind, n, b in conn.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(LENGTH(payload)),0)"
                " FROM events GROUP BY kind"):
            if kind in DELTA_KINDS:
                rows += n
                nbytes += b
        running = conn.execute(
            "SELECT COUNT(*) FROM chat_threads WHERE status='running'"
        ).fetchone()[0]
        live_delta = 0
        if with_live and rows:
            # 运行中线程的 delta 不能删（那一轮还在流式写，断了前端上屏）
            kinds = ",".join("?" * len(DELTA_KINDS))
            live_delta = conn.execute(
                f"SELECT COUNT(*) FROM events WHERE kind IN ({kinds})"
                " AND json_extract(payload,'$.thread_id') IN"
                " (SELECT id FROM chat_threads WHERE status='running')",
                DELTA_KINDS).fetchone()[0]
    finally:
        conn.close()
    return {"size": size, "rows": rows, "bytes": nbytes,
            "running": running, "live_delta": live_delta,
            "wal_bytes": wal_bytes}


def _compact(path: str, vacuum: bool) -> tuple[int, float, float]:
    """删 delta（保留运行中线程的），可选 VACUUM。返回 (删除行数, 前 MB, 后 MB)。"""
    before = _mb(os.path.getsize(path))
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA journal_mode=WAL")
        kinds = ",".join("?" * len(DELTA_KINDS))
        # **只删非运行中线程**：运行中的那一轮还在写，删了会让工作台思考面板
        # 卡在半截（下一帧 delta 又是累计全文，能自愈，但会闪一下）。
        cur = conn.execute(
            f"DELETE FROM events WHERE kind IN ({kinds})"
            " AND json_extract(payload,'$.thread_id') NOT IN"
            " (SELECT id FROM chat_threads WHERE status='running')",
            DELTA_KINDS)
        deleted = cur.rowcount
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
    finally:
        conn.close()
    return deleted, before, _mb(os.path.getsize(path))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="黑板库瘦身（删流式增量 + 可选 VACUUM）")
    ap.add_argument("--glob", default=DEFAULT_GLOB, help="库文件通配（默认 %(default)s）")
    ap.add_argument("--apply", action="store_true", help="真删；不加则只报告")
    ap.add_argument("--vacuum", action="store_true",
                    help="删完 VACUUM 缩文件（需 2x 临时磁盘，必须先停平台）")
    ap.add_argument("--min-mb", type=float, default=0.0,
                    help="只处理大于该体积的库（MB），默认全部")
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(args.glob), key=os.path.getsize, reverse=True)
    if not paths:
        print(f"没有匹配 {args.glob} 的库")
        return 1

    print(f"{'库':<44}{'MB':>9}{'delta行':>10}{'delta MB':>10}{'占比':>7}{'运行中':>7}",
          flush=True)
    print("-" * 88, flush=True)
    todo: list[str] = []
    probes: dict[str, dict] = {}
    total = 0.0
    for p in paths:
        info = _probe(p)
        probes[p] = info
        total += info["size"]
        if _mb(info["size"]) < args.min_mb:
            continue
        pct = 100.0 * info["bytes"] / max(info["size"], 1)
        print(f"{p:<44}{_mb(info['size']):>9.0f}{info['rows']:>10d}"
              f"{_mb(info['bytes']):>10.0f}{pct:>6.0f}%{info['running']:>7d}",
              flush=True)
        if info["rows"]:
            todo.append(p)
    print("-" * 88, flush=True)
    print(f"合计 {_mb(total):.0f} MB（{len(paths)} 个库）", flush=True)

    if not args.apply:
        print("\n[dry-run] 未改动任何文件。加 --apply 真删；"
              "再加 --vacuum 缩文件。")
        return 0

    if args.vacuum:
        busy = [p for p in todo if probes[p]["wal_bytes"] > 1 << 20]
        if busy:
            print("\n[拒绝] 下列库的 -wal 非空，说明平台还在跑："
                  "VACUUM 要独占，且运行中的进程会立刻把 delta 写回来。"
                  "先停平台再跑。")
            for p in busy:
                print("  ", p)
            return 2

    print()
    grand_before = grand_after = 0.0
    for p in todo:
        t0 = time.time()
        deleted, before, after = _compact(p, args.vacuum)
        grand_before += before
        grand_after += after
        print(f"{p:<44} 删 {deleted:>8d} 行  {before:>8.0f} → {after:>8.0f} MB"
              f"  ({time.time() - t0:.1f}s)")
    if todo:
        print(f"\n合计 {grand_before:.0f} → {grand_after:.0f} MB"
              f"（回收 {grand_before - grand_after:.0f} MB）")
        if not args.vacuum:
            print("提示：没加 --vacuum，文件本身没缩——空间进了 freelist，"
                  "后续写入会复用，但不会还给磁盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
