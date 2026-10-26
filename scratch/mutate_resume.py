"""变异测试：验证 ask_expert(resume=true) 的护栏真的会红。

每个变异「撤掉一处修复」，跑对应用例，期望 rc=1（变红）。
换行符必须保留（newline=""），否则会把 CRLF 文件改成 LF，后续匹配全失效。
"""
import io
import subprocess
import sys

PY = sys.executable
RUNTIME = "core/chat/runtime.py"
STORE = "core/chat/store.py"

MUTATIONS = [
    # (标签, 文件, old, new, 用例选择)
    (
        "MUT1 不拼回收块（退回「只跑子轮」）",
        RUNTIME,
        '        head = ""\n        if harvest is not None:',
        '        head = ""\n        if False:',
        "test_ask_expert_resume_recovers_then_continues",
    ),
    (
        "MUT2 续跑不带墙钟预算",
        RUNTIME,
        "run_budget_s=_RESUME_BUDGET_S if resume else 0.0,",
        "run_budget_s=0.0,",
        "test_ask_expert_resume_passes_wallclock_budget",
    ),
    (
        "MUT3 交付正文取 parts[1]（正文含 --- 就截断）",
        STORE,
        '        return n, "\\n---\\n".join(parts[1:-1]).strip()',
        '        return n, parts[1].strip()',
        "test_harvest_thread_recovers_delivery_and_skips_tombstone",
    ),
    (
        "MUT4 不跳过平台墓碑",
        STORE,
        "    return bool(t) and t.startswith(\"（\") and any(",
        "    return False and any(",
        "test_harvest_thread_recovers_delivery_and_skips_tombstone",
    ),
    (
        "MUT5 占位文案不分叉（退回统一短文案）",
        STORE,
        '    if tool_name in ("call_expert", "ask_expert"):',
        '    if False:',
        "test_dangling_placeholder_points_at_resume",
    ),
    (
        "MUT6 resume 不再自动起草指令",
        RUNTIME,
        "            if not message:\n                message = self._resume_draft_message(harvest)",
        "            if not message:\n                message = \"继续\"",
        "test_ask_expert_resume_recovers_then_continues",
    ),
    (
        "MUT7 占位检测按新整串匹配（认不出旧文案）",
        STORE,
        '    return t.startswith("[错误]") and _DANGLING_MARK in t[:160]',
        '    return t.startswith(_DANGLING_TOOL_PREFIX)',
        "test_harvest_detects_interrupted_placeholder_old_and_new",
    ),
    (
        "MUT8 结论位置不分叉（开头那句当结论）",
        RUNTIME,
        '            if harvest.get("final_after_tools"):',
        '            if True:',
        "test_format_harvest_labels_midflight_text_as_nonconclusion",
    ),
]


def read(path):
    return io.open(path, "r", encoding="utf-8", newline="").read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def crlf(text):
    """把模式串的 \\n 对齐到文件真实换行——两个文件都是 CRLF，不做这步
    多行变异会「未命中」而被误当成「护栏不需要」（假绿）。"""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def run(expr):
    return subprocess.run(
        [PY, "-m", "pytest", "tests/test_kb_dispatch_rules.py",
         "tests/test_chat_workbench.py", "-q", "-p", "no:randomly", "-k", expr],
        capture_output=True, text=True,
        env={k: v for k, v in __import__("os").environ.items()
             if k not in ("CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR",
                          "CODEBUDDY_TOOL_CALL_ID")})


def main():
    results = []
    for label, path, old, new, expr in MUTATIONS:
        original = read(path)
        old_c, new_c = crlf(old), crlf(new)
        if old_c not in original:
            results.append((label, "SKIP 未命中", ""))
            continue
        write(path, original.replace(old_c, new_c, 1))
        try:
            p = run(expr)
            ok = p.returncode != 0
            tail = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
            results.append((label, "红 OK" if ok else "绿 <<< 护栏失效", tail))
        finally:
            write(path, original)
    print()
    for label, status, tail in results:
        print(f"{status:24s} {label}   {tail}")
    bad = [r for r in results if not r[1].startswith(("红", "SKIP"))]
    print("\n失效变异数:", len(bad))


if __name__ == "__main__":
    main()
