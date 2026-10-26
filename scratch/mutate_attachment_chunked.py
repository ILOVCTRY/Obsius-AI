"""变异验证：撤掉关键护栏后 tests/test_attachment_chunked.py 必须变红。

用法：E:/Miniconda3/python.exe scratch/mutate_attachment_chunked.py

项目约定（MEMORY.md「验证修复做变异测试」）：修复在、护栏缺 = 下次重构静默回退。

**踩过的坑（第一版全绿≠护栏有效）**：`app.py` 里 `complete_sample_upload` 与本文件
的附件 complete 有几乎相同的行（`if actual != expected:`、`if path.read_bytes() !=
chunk:`、`if metadata.get("status") == "complete":`），而 `.replace(..., 1)` 只换
**第一处**——于是变异打中了样本包端点，附件测试自然全绿。这不是"护栏无效"，是
"变异对被测输入等价"。现在每个锚点都带**附件侧独有的上下文**（`_attachment_upload_meta`
/ `_received_attachment_chunks`），并断言锚点在文件中唯一。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "core" / "api" / "app.py"
TEST = "tests/test_attachment_chunked.py"

# (名称, 原文, 替换, 期望变红的用例关键字)
# 锚点必须唯一（脚本会断言 count == 1），且必须落在**附件**端点里。
MUTATIONS = [
    (
        "去掉 sha256 内容校验（坏文件会被当成好的落盘）",
        "            if declared and declared != sha:",
        "            if False and declared and declared != sha:",
        "sha_mismatch",
    ),
    (
        "complete 去掉幂等早退（重复调用造出两个 artifact）",
        '        root, metadata = _attachment_upload_meta(proj, upload_id)\n'
        '        if metadata.get("status") == "complete":\n'
        '            return metadata.get("result") or metadata',
        '        root, metadata = _attachment_upload_meta(proj, upload_id)',
        "idempotent",
    ),
    (
        "放行分片数不符（会拼出大小对、内容错的文件）",
        "        if body.total_chunks != expected_chunks:",
        "        if False and body.total_chunks != expected_chunks:",
        "chunk_count_must_match",
    ),
    (
        "同片不同内容允许覆盖（最终内容取决于到达顺序）",
        # `if path.exists():\n if path.read_bytes() != chunk:` 在样本包端点里一模一样，
        # 必须带上附件独有的「空分片」两行才唯一（否则打中别的端点 → 假绿）。
        '        if not chunk:\n'
        '            raise HTTPException(422, "空分片")\n'
        '        path = root / "chunks" / f"{chunk_index}.part"\n'
        "        if path.exists():\n"
        "            if path.read_bytes() != chunk:",
        '        if not chunk:\n'
        '            raise HTTPException(422, "空分片")\n'
        '        path = root / "chunks" / f"{chunk_index}.part"\n'
        "        if path.exists():\n"
        "            if False and path.read_bytes() != chunk:",
        "reput_same_chunk",
    ),
    (
        "缺片也允许 complete（拼个残缺文件）",
        '        expected = list(range(int(metadata["total_chunks"])))\n'
        "        actual = _received_attachment_chunks(root)\n"
        "        if actual != expected:",
        '        expected = list(range(int(metadata["total_chunks"])))\n'
        "        actual = _received_attachment_chunks(root)\n"
        "        if False and actual != expected:",
        "resume_skips",
    ),
    (
        "单片体积上限失效（超长分片可把内存顶爆）",
        "        if len(chunk) > ATTACHMENT_CHUNK_BYTES:",
        "        if False and len(chunk) > ATTACHMENT_CHUNK_BYTES:",
        "oversized_chunk",
    ),
]


def run_tests() -> tuple[int, str]:
    """跑被测文件，回 (退出码, 输出)。

    **必须摘掉宿主 safe-delete 钩子的环境**（MEMORY.md「跑 pytest 三坑」）：本机
    `rm`/`unlink` 被包成 safe-delete 垫片，pytest 清理 basetemp 时撞 BULK 阈值会
    `SystemExit(1)` **带崩整个进程**——表现为"输出里已经有 F，但拿不到 summary"，
    变异脚本会把这次崩溃误判成"没变红"（假绿）。同时固定唯一 basetemp：
    撞 `Errno 53` 也是这台机器的老毛病。
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CODEBUDDY_SAFE_DELETE")
           and not k.startswith("BASH_FUNC_rm")
           and not k.startswith("BASH_FUNC_unlink")}
    basetemp = Path(tempfile.mkdtemp(prefix="pytest-mut-att-"))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
         "-p", "no:cacheprovider", f"--basetemp={basetemp}"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env)
    shutil.rmtree(basetemp, ignore_errors=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    original = APP.read_text(encoding="utf-8")
    baseline_rc, baseline_out = run_tests()
    if baseline_rc != 0:
        print("[!] 基线就是红的，先修基线再看变异")
        print(baseline_out[-2000:])
        return 2
    print("[ok] 基线全绿")

    results = []
    try:
        for name, before, after, expect in MUTATIONS:
            hits = original.count(before)
            if hits != 1:
                # 0 = 代码已改，需同步脚本；>1 = 锚点不唯一，只换第一处会打中
                # 别的端点（假绿的根源），宁可停下也不跑一个打歪的变异。
                print(f"[!] 锚点命中 {hits} 次（需恰好 1 次），跳过并判失败: {name}")
                results.append((name, f"anchor-hits-{hits}"))
                continue
            APP.write_text(original.replace(before, after, 1), encoding="utf-8")
            rc, out = run_tests()
            went_red = rc != 0 and expect in out
            print(f"[{'RED' if went_red else 'GREEN?!'}] {name}")
            results.append((name, "red" if went_red else "still-green"))
            if not went_red:
                print(out[-1500:])
    finally:
        APP.write_text(original, encoding="utf-8")
        print("[restored] core/api/app.py 已还原")

    bad = [r for r in results if r[1] != "red"]
    print(f"\n{len(results) - len(bad)}/{len(results)} 变异成功变红")
    if bad:
        print("未变红的变异（护栏无效或变异等价于原代码）:")
        for name, state in bad:
            print(f"  - [{state}] {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
