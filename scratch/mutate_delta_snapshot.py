"""变异测试：验证 v51「思考增量降级为状态快照」的护栏真的会红。

撤掉修复后护栏必须红；**修复在、护栏缺 = 下次重构静默回退**。

用法：`E:/Miniconda3/python.exe scratch/mutate_delta_snapshot.py`
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RT = os.path.join(ROOT, "core", "chat", "runtime.py")
ST = os.path.join(ROOT, "core", "blackboard", "store.py")
PY = sys.executable


def crlf(s: str) -> str:
    """把模式串的 \\n 对齐到文件的 CRLF——不对齐会「未命中」并被误判成假绿。"""
    return s.replace("\r\n", "\n").replace("\n", "\r\n")


MUTATIONS = [
    (
        "MUT1 快照退回事件流（_emit_delta_state 改回 _emit chat.thinking.delta）",
        RT,
        "                self._emit_delta_state(_st, thinking, _pub[\"seq\"])",
        "                self._emit(\"chat.thinking.delta\","
        " {\"text\": thinking, \"seq\": _pub[\"seq\"]})",
        "test_chat_thinking_stream_events",
    ),
    (
        "MUT2 收尾不清剪快照（prune_delta_states 不调）",
        RT,
        "                self.bb.prune_delta_states(self.project_id, self.thread_id)",
        "                pass  # mutated: 不清剪",
        "test_chat_thinking_stream_events",
    ),
    (
        "MUT3 快照退化成 append（去掉 ON CONFLICT 覆盖）",
        ST,
        "                \" ON CONFLICT(project_id,thread_id,step) DO UPDATE SET\""
        "\n                \" seq=excluded.seq, text=excluded.text,\""
        "\n                \" updated_at=excluded.updated_at\",",
        "                \"\",  # mutated: 无覆盖语义",
        "test_delta_state_is_snapshot_not_event_stream",
    ),
    (
        "MUT4 读侧游标用 >=（每轮重推同一批）",
        ST,
        "\" WHERE project_id=? AND updated_at>? ORDER BY updated_at ASC LIMIT ?\",",
        "\" WHERE project_id=? AND updated_at>=? ORDER BY updated_at ASC LIMIT ?\",",
        "test_recent_delta_states_is_incremental",
    ),
]


def run_case(target: str) -> tuple[bool, str]:
    bt = os.path.join(tempfile.gettempdir(), "pt_mut_%d_%s" % (os.getpid(), target[:6]))
    shutil.rmtree(bt, ignore_errors=True)
    p = subprocess.run(
        [PY, "-m", "pytest", "tests/test_chat_workbench.py",
         "-k", target, "-p", "no:cacheprovider", "-q", "--basetemp", bt],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (p.stdout or "") + (p.stderr or "")
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [""]
    # 退出码不可靠（宿主 safe-delete 钩子会带崩进程）→ 解析 summary
    red = " failed" in out or " error" in out.lower()
    return red, tail[0]


def main() -> int:
    bad = 0
    for name, path, old, new, target in MUTATIONS:
        src = io.open(path, encoding="utf-8", newline="").read()
        pat, rep = crlf(old), crlf(new)
        if pat not in src:
            print(f"SKIP  {name}\n      —— 模式未命中（多半是换行/缩进不匹配）")
            bad += 1
            continue
        bak = src
        io.open(path, "w", encoding="utf-8", newline="").write(src.replace(pat, rep, 1))
        try:
            red, summary = run_case(target)
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(bak)
        mark = "红 <<< 护栏有效" if red else "绿 <<< 护栏失效"
        if not red:
            bad += 1
        print(f"{name}\n      {mark}   [{summary}]")
    print(f"\n{len(MUTATIONS) - bad}/{len(MUTATIONS)} 变异被护栏抓住")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
