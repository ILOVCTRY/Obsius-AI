"""清理历史落库的伪工具调用污染文本（2026-10-20 事故善后）。

背景：网关把工具调用特殊 token（``<｜｜DSML｜｜ calls>`` 等）漏进 content，
旧逻辑把非空 text 当「纯文本终稿」落库。污染行会**自我强化**——回放时模型
看到自己写过这个格式，更容易再写一遍。故代码修复（core/llm/pseudo_toolcall.py
+ 两条链路的守卫）之后，还需就地清洗存量。

处置口径（**不删行**）：
- ``role='assistant'``：剥掉标记，剩余正文写回；剥完为空 → 换成明确说明
  （保留「这里曾经有一轮中断」的事实，不伪造内容）。
- ``role='tool'``：**绝不删行**（会破坏 tool_use / tool_result 配对，回放时
  整个历史被判悬空），只就地剥离标记，剩余部分（如「专家线程 … 已完成」）
  原样保留。
- ``role='user'``：不动（人类输入，即便含标记也是其原话）。

用法::

    python scripts/clean_pseudo_toolcall.py            # dry-run，打印每行前后摘要
    python scripts/clean_pseudo_toolcall.py --apply    # 真写库
    python scripts/clean_pseudo_toolcall.py --db workspaces/hxbai/blackboard.db --apply
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm.pseudo_toolcall import (  # noqa: E402
    has_pseudo_tool_call,
    strip_pseudo_tool_calls,
)

# 剥离后为空时的替身文案（assistant 行）。不删行是为了保住「这一轮发生过什么」
# 的痕迹——审计里「AI 想调工具但格式错了」比「这里什么都没有」有用得多。
EMPTY_ASSISTANT_TEXT = (
    "（此轮回复为工具调用格式错误，内容已清理。"
    "如需重试，请重新发送你的请求）")

# 扫描列：content 与 thinking（思考通道也可能被同一份泄漏灌进去）。
_COLUMNS = ("content", "thinking")


def _preview(s: str, n: int = 80) -> str:
    return s.replace("\n", "\\n")[:n]


def _plan_rows(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    """返回 [(rowid, role, column, new_text)]。"""
    conn.row_factory = sqlite3.Row
    plans: list[tuple[int, str, str, str]] = []
    for col in _COLUMNS:
        rows = conn.execute(
            f"SELECT id, role, {col} AS val FROM chat_messages"
            f" WHERE {col} LIKE '%DSML%' OR {col} LIKE '%tool_call%'"
            f" OR {col} LIKE '%tool▁calls%' OR {col} LIKE '%function_call%'"
        ).fetchall()
        for r in rows:
            val = r["val"] or ""
            if not has_pseudo_tool_call(val):
                continue
            new = strip_pseudo_tool_calls(val)
            if r["role"] == "user":
                continue  # 人类原话不动
            if not new:
                if r["role"] == "tool":
                    new = "（原工具结果为工具调用格式错误文本，已清理）"
                else:
                    new = EMPTY_ASSISTANT_TEXT
            if new != val:
                plans.append((r["id"], r["role"], col, new))
    return plans


def clean_db(db: Path, *, apply: bool) -> tuple[int, int]:
    """返回 (扫描命中数, 实际改写数)。"""
    conn = sqlite3.connect(str(db))
    try:
        plans = _plan_rows(conn)
        if not plans:
            print(f"  {db}: 无污染行")
            return 0, 0
        print(f"  {db}: 命中 {len(plans)} 处")
        for rid, role, col, new in plans:
            old = conn.execute(
                f"SELECT {col} FROM chat_messages WHERE id=?", (rid,)
            ).fetchone()[0] or ""
            print(f"    id={rid} role={role} col={col}")
            print(f"      前: {_preview(old)}")
            print(f"      后: {_preview(new)}")
        if apply:
            with conn:
                for rid, _role, col, new in plans:
                    conn.execute(
                        f"UPDATE chat_messages SET {col}=? WHERE id=?", (new, rid))
            print(f"    → 已写库 {len(plans)} 处")
        else:
            print("    → dry-run，未写库（加 --apply 生效）")
        return len(plans), len(plans) if apply else 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理落库的伪工具调用污染文本")
    ap.add_argument("--db", action="append", default=None,
                    help="指定 blackboard.db（可多次）；缺省扫描 workspaces/*/blackboard.db")
    ap.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
    args = ap.parse_args(argv)

    dbs = [Path(p) for p in (args.db or
                             sorted(glob.glob("workspaces/*/blackboard.db")))]
    if not dbs:
        print("未找到任何 blackboard.db")
        return 1
    print(f"{'写库' if args.apply else 'dry-run'}：扫描 {len(dbs)} 个库")
    total = written = 0
    for db in dbs:
        if not db.exists():
            print(f"  {db}: 不存在，跳过")
            continue
        try:
            hit, w = clean_db(db, apply=args.apply)
        except sqlite3.OperationalError as e:
            print(f"  {db}: 跳过（{e}）")   # 无 chat_messages 表等
            continue
        total += hit
        written += w
    print(f"合计：命中 {total} 处，改写 {written} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
