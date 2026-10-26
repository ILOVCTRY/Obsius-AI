"""对「召回扩展 + 引擎节流」护栏做变异测试。

规则：撤掉修复后护栏必须变红，否则护栏就是摆设。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = "core/intel/search.py"
TEST = "tests/test_intel.py"

# (描述, 旧片段, 新片段, 期望变红的用例)
MUTATIONS = [
    (
        "扩展不做去重（重复 URL 全塞进结果）",
        "                if not url or url in seen:\n                    continue",
        "                if not url:\n                    continue",
        "test_search_intel_expands_recall_and_dedupes",
    ),
    (
        "扩展被限流时抛错（丢掉主查询已有结果）",
        "            rows = _fetch(extra, attempts=1)\n            if not rows:",
        "            rows = _fetch(extra, attempts=1)\n            if not rows:\n                raise RuntimeError('expansion failed')\n            if not rows:",
        "test_search_intel_falls_back_to_primary_when_expansion_rate_limited",
    ),
    (
        "忽略冷却期（连续检索照打扩展，会把引擎打进 suspended）",
        "        if _last_expand_at is None:\n            return True\n        return (time.monotonic() - _last_expand_at) >= _EXPAND_COOLDOWN_S",
        "        return True",
        "test_search_intel_skips_expansion_during_cooldown",
    ),
    (
        "扩展不按术语表约束（随意扩词会把新闻/跑题带进来）",
        '    return extras[:max(0, int(limit))]',
        '    return [base + " 教程", base + " 新闻"][:max(0, int(limit))]',
        "test_expand_queries_only_expands_known_domains",
    ),
    (
        "挂起期间仍照发请求（用户白等 3 次退避，最后 0 条）",
        "    if _engine_suspended():",
        "    if False:",
        "test_search_intel_reports_rate_limit_instead_of_retrying",
    ),
    (
        "成功后不清挂起标记（引擎恢复了仍被判限流，永久卡死）",
        "        _mark_engine_ok()\n                return rows",
        "                return rows",
        "test_search_intel_clears_suspend_flag_after_success",
    ),
    (
        "冷却期不回报 throttled（用户只看到变少，不知道是限流）",
        '        return primary, "searxng+throttled"\n    return primary, "searxng"',
        '        pass\n    return primary, "searxng"',
        "test_search_intel_skips_expansion_during_cooldown",
    ),
]


def run_one(desc: str, old: str, new: str, expect: str) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="mut_exp_"))
    try:
        # 只复制需要的目录，避免 Windows 下大目录 copytree 的句柄问题
        for rel in ("core", "tests", "config"):
            src = ROOT / rel
            if src.exists():
                shutil.copytree(src, tmp / rel,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        target = tmp / TARGET
        # 先读入内存再写，避免 copytree 后立即打开触发的 bad file descriptor
        text = target.read_text(encoding="utf-8")
        if old not in text:
            print(f"[SKIP] {desc}: 锚点未命中（变异对输入等价）")
            return False
        target.write_text(text.replace(old, new, 1), encoding="utf-8")

        env = dict(os.environ)
        env.pop("CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR", None)
        env.pop("CODEBUDDY_SAFE_DELETE_BULK_STATE", None)
        base = Path(tempfile.mkdtemp(prefix="mut_bt_"))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", f"{TEST}::{expect}", "-q",
             "-p", "no:cacheprovider", f"--basetemp={base}"],
            cwd=tmp, capture_output=True, text=True, env=env, timeout=300)
        failed = proc.returncode != 0
        print(f"[{'OK  ' if failed else 'MISS'}] {desc} -> {expect} "
              f"{'变红（护栏有效）' if failed else '仍绿（护栏失效！）'}")
        return failed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    missed = 0
    for desc, old, new, expect in MUTATIONS:
        if not run_one(desc, old, new, expect):
            missed += 1
    print(f"\n变异 {len(MUTATIONS) - missed}/{len(MUTATIONS)} 被拦住")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
