"""变异测试：撤掉质量护栏，测试必须变红。

不做这步的话，「44 passed」可能只是新测试没查住关键点，
下次重构把垃圾过滤或相关性排序改回去也照样全绿。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"E:\ILOVCTRY\cyberstrike-pro")
PY = r"E:\Miniconda3\python.exe"

MUTATIONS = [
    (
        "spam_score 恒返回 1.0（垃圾过滤失效）",
        "core/intel/ranking.py",
        '    text = f"{item.get(\'title\', \'\')} {item.get(\'summary\', \'\')}".casefold()\n    if any(p in text for p in _SPAM_PATTERNS):\n        return 0.0',
        '    return 1.0\n    text = f"{item.get(\'title\', \'\')} {item.get(\'summary\', \'\')}".casefold()\n    if any(p in text for p in _SPAM_PATTERNS):\n        return 0.0',
    ),
    (
        "ranking_score 丢掉相关性权重（回到只看质量×时效）",
        "core/intel/ranking.py",
        "    blended = (relevance ** 0.9) * (quality ** 0.6) * (freshness ** 0.25)",
        "    blended = (quality ** 0.7) * (freshness ** 0.3)",
    ),
    (
        "新闻站不再降权",
        "core/intel/ranking.py",
        "    score -= 0.25 * newsy",
        "    score -= 0.0 * newsy",
    ),
    (
        "search_intel 重新回落 Google News",
        "core/intel/search.py",
        '    if not rows:\n        raise WebSearchError(',
        '    if not rows:\n        return google_news_search(cfg, query, num=num), "google_news"\n    if False:\n        raise WebSearchError(',
    ),
]


def run(extra: list[str]) -> tuple[int, str]:
    bt = Path(tempfile.mkdtemp(prefix="mut_bt_"))
    p = subprocess.run([PY, "-m", "pytest", "tests/test_intel.py", "-q",
                        "-p", "no:cacheprovider", f"--basetemp={bt}", *extra],
                       cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def main() -> int:
    code, out = run([])
    tail = [l for l in out.splitlines() if "passed" in l or "failed" in l]
    print("基线:", tail[-1] if tail else out[-200:])
    if code != 0:
        print("[FAIL] 基线不绿")
        return 1

    bad = 0
    for desc, rel, old, new in MUTATIONS:
        tmp = Path(tempfile.mkdtemp(prefix="mut_src_"))
        try:
            shutil.copytree(ROOT / "core", tmp / "core")
            shutil.copytree(ROOT / "tests", tmp / "tests")
            for extra in ("config", "packs"):
                if (ROOT / extra).exists():
                    shutil.copytree(ROOT / extra, tmp / extra)
            for f in ("pyproject.toml",):
                if (ROOT / f).exists():
                    shutil.copy2(ROOT / f, tmp / f)
            target = tmp / rel
            # Windows 上 copytree 之后立刻 open 偶发 Errno 9（句柄尚未释放），
            # 先读入内存再统一写入。
            try:
                text = Path(ROOT / rel).read_text(encoding="utf-8")
            except OSError:
                import time as _t
                _t.sleep(0.3)
                text = Path(ROOT / rel).read_text(encoding="utf-8")
            if old not in text:
                print(f"[SKIP] {desc}: 锚点未命中（变异对输入等价）")
                bad += 1
                continue
            with open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text.replace(old, new, 1))
            bt = Path(tempfile.mkdtemp(prefix="mut_bt2_"))
            p = subprocess.run([PY, "-m", "pytest", "tests/test_intel.py", "-q",
                                "-p", "no:cacheprovider", f"--basetemp={bt}"],
                               cwd=tmp, capture_output=True, text=True)
            ok = p.returncode != 0
            summary = [l for l in (p.stdout + p.stderr).splitlines()
                       if "passed" in l or "failed" in l]
            print(f"[{'OK' if ok else 'MISS'}] {desc} -> {summary[-1] if summary else p.returncode}")
            if not ok:
                bad += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if bad:
        print(f"[FAIL] {bad}/{len(MUTATIONS)} 个变异未被拦住")
        return 1
    print(f"[PASS] {len(MUTATIONS)}/{len(MUTATIONS)} 个变异全部被拦住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
