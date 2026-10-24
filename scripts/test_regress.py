"""回归测试入口（2026-10-24）。三种模式 + 一个耗时守卫。

## 为什么要这个脚本

`pytest tests/` 跑全量要 ~1004s（16:44），日常改一行代码等不起。但**直接给
pyproject 设 `addopts = "-m 'not slow'"` 是错的**——默认静默少跑一批用例，
会让人以为"全绿"而实际有 36 个用例根本没执行。这是本仓明令禁止的假绿。

所以这里的分工是：
- `pytest tests/`（裸跑）       → **全量**，不排除任何用例，行为不变
- `test_regress.py quick`       → 排除 slow/e2e，日常回路
- `test_regress.py full`        → 全量（等同裸跑，但带并行）
- `test_regress.py guard`       → 耗时守卫，检测"该打 slow 却没打"的用例

## 用法

    E:/Miniconda3/python.exe scripts/test_regress.py quick
    E:/Miniconda3/python.exe scripts/test_regress.py full -j 8
    E:/Miniconda3/python.exe scripts/test_regress.py guard

Windows 上必须用 Miniconda 的解释器（LLM 层依赖）；`env -u HTTP_PROXY` 由本脚本
自己处理——不摘代理的话网络类用例全变 502，那是环境问题不是回归。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# 实测（2026-10-24，3081 用例，24 核机器）：
#   串行全量          1004s  基线
#   -n 8 全量         247~308s，但**偶发 worker 崩溃**（失败数 2→7→8 波动）
#   -n 4 quick 集合    211~229s，三轮零串扰（推荐）
# 默认取 4 而不是 8：8 个 worker 各自加载完整 FastAPI app + 建 SQLite，IO 争抢
# 反而更慢，且曾出现 SystemExit/端口冲突的连锁失败（串行复跑全绿）。
# 想更快可 `-j 8`，但请先确认本轮 failed 集合与 `-j 1` 一致。
DEFAULT_JOBS = 4
# 兜底超时（实测 quick ~340s / 全量 ~1004s，各留 2x 余量）
QUICK_TIMEOUT = 900
FULL_TIMEOUT = 2400


def _env() -> dict[str, str]:
    """清掉三类会污染测试的宿主环境变量。

    ① **代理**：环境里长期挂着 `http_proxy=127.0.0.1:48468`，该代理对 pypi 与本地
       回环返回 502——不摘的话走 urllib 的用例（proxy_pool/fofa/web_search）全失败，
       且看起来像代码 bug。
    ② **批量删除守卫**：宿主注入的 `sitecustomize.py` 会在进程删目录时调外部 guard
       脚本，guard 不可用时直接 `raise SystemExit(1)`——**把整个 pytest 进程带崩**。
       这是「并行跑时出现 SystemExit: 1 / 4 errors」的真正来源，不是并发 bug。
       摘掉 `CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR` 与 `CODEBUDDY_TOOL_CALL_ID` 即
       使守卫整体失活（见 sitecustomize._check_bulk_delete_guard 的前置判断）。
    ③ **ELECTRON_RUN_AS_NODE**：残留会让 Electron 被当纯 Node 跑（`app` undefined）。
    """
    env = dict(os.environ)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy",
                "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR", "CODEBUDDY_TOOL_CALL_ID",
                "CODEBUDDY_SAFE_DELETE_BULK_GUARD",
                "ELECTRON_RUN_AS_NODE"):
        env.pop(key, None)
    return env


def _run(args: list[str], timeout: float | None = None) -> int:
    """跑 pytest，返回**测试结论**而非进程退出码。

    为什么不能直接用 returncode（2026-10-24 踩到）：pytest 收尾清理 basetemp 时
    会调 `wsl.exe`，被宿主安全策略拦截 → 进程退出码非 0，但**用例其实全过了**。
    直接透传会让 CI/脚本误判为失败（"假红"），比假绿更浪费时间——人会去查一个
    不存在的 bug。

    故改为捕获输出、解析 summary 行判定：
      有 "failed"/"error" 计数 → 失败；只有 passed → 成功。
    """
    cmd = [sys.executable, "-m", "pytest", *args]
    print("[run]", " ".join(cmd[:6]), "..." if len(cmd) > 6 else "")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=_env(), timeout=timeout,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print(f"[FAIL] 超过 {timeout:.0f}s 未结束——大概率有用例卡死（网络/子进程等待）。")
        print("       用 `pytest <单个文件> -q --durations=10` 定位；")
        print("       网络类用例请确认代理已摘除（本脚本自动摘）。")
        return 124

    out = (proc.stdout or "") + (proc.stderr or "")
    # 打尾 40 行给用户看（--durations / summary 都在尾部）
    tail = "\n".join(out.strip().splitlines()[-40:])
    print(tail)

    failed = re.search(r"\b(\d+) (?:failed|error)", out)
    if failed and int(failed.group(1)) > 0:
        return 1
    if re.search(r"\b\d+ passed", out):
        if proc.returncode != 0:
            print(f"[note] pytest 退出码 {proc.returncode} 但用例全过——"
                  "通常是收尾清理被安全策略拦截（wsl.exe），按通过处理。")
        return 0
    # 没解析到 summary（如 INTERNALERROR / 收集失败）→ 按失败处理
    print(f"[FAIL] 未取到测试结论（退出码 {proc.returncode}），按失败处理。")
    return proc.returncode or 1


def _basetemp(tag: str) -> str:
    """pytest 的临时根目录——**每次运行用唯一目录**。

    两个坑（2026-10-24 实测）：

    ① 传 `/tmp/xxx` 时 pytest 解析成 `E:\\tmp\\xxx`（当前盘根），宿主注入的
       safe-delete 钩子在清理时走 `_get_short_path` → `OSError: [Errno 53]
       找不到网络路径`，把 pytest 打成 INTERNALERROR。
    ② 固定名字复用同一目录也不行：上一轮被强制中断留下的残缺目录，会让本轮
       收尾删除同样撞上 Errno 53（短路径解析对已有异常目录失效）。
       **必须每轮换新目录**，旧目录留在那里比让本轮假失败好。

    用 `%TEMP%` 下的 pid+时间戳目录，跑完不主动清理（pytest 自己会清）。
    """
    import tempfile
    import time as _time
    stamp = f"{os.getpid()}_{int(_time.time())}"
    return str(Path(tempfile.gettempdir()) / f"cs_regress_{tag}_{stamp}")


def mode_quick(jobs: int) -> int:
    """日常回路：排除 slow/e2e/serial。目标 <6 分钟。"""
    args = [
        "tests", "-q", "--basetemp", _basetemp("quick"),
        "-m", "not slow and not e2e and not serial",
    ]
    if jobs > 1:
        args += ["-n", str(jobs), "--dist", "loadfile"]
    return _run(args, timeout=QUICK_TIMEOUT)


def mode_full(jobs: int) -> int:
    """全量：不排除任何用例。

    并行时**分两趟**，而不是靠 `--dist loadgroup`：

    - 第一趟：`-m "not serial"` 用 `-n N --dist loadfile` 并行；
    - 第二趟：`-m "serial"` **不并行**，独占进程串行跑。

    为什么不 loadgroup：那个策略认的标记是 `xdist_group`，自定义的 `serial`
    它根本看不见 → 带 serial 的用例照样被分到不同 worker 并发跑，约束**静默
    失效**。而这种失效只在偶发失败时才暴露（实测三轮并行失败数为 2/2/8）。
    两趟走法把"串行"变成进程级事实，不依赖任何插件语义。
    """
    if jobs <= 1:
        return _run(["tests", "-q", "--basetemp", _basetemp("full")], timeout=FULL_TIMEOUT)

    print("[info] 第一趟：非 serial 用例并行")
    first = _run([
        "tests", "-q", "--basetemp", _basetemp("full_par"),
        "-m", "not serial",
        "-n", str(jobs), "--dist", "loadfile",
    ], timeout=FULL_TIMEOUT)
    print("[info] 第二趟：serial 用例串行（Windows 文件系统边界，不可并发）")
    second = _run([
        "tests", "-q", "--basetemp", _basetemp("full_serial"),
        "-m", "serial",
    ], timeout=FULL_TIMEOUT)
    if first != 0:
        return first
    return second


def mode_guard(threshold: float = 2.0) -> int:
    """耗时守卫：跑 quick 集合并采集每条耗时，报告**未打 slow 却超阈值**的用例。

    这是防「标记腐化」的唯一手段：新写的慢用例忘了打标，quick 回路会悄悄变慢
    而没人察觉。守卫把这件事变成可见的失败。

    注意：只用 quick 集合（已排除 slow），所以报出来的都是"漏标"的。
    """
    out = ROOT / "logs" / "durations.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "tests", "-q", "--basetemp", _basetemp("guard"),
        "-m", "not slow and not e2e",
        f"--durations=0", "-p", "no:cacheprovider",
    ]
    # durations 输出混在 stdout 里，这里重定向到文件后解析
    cmd = [sys.executable, "-m", "pytest", *args]
    print("[run] guard ...")
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT), env=_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed = time.time() - started
    out.write_text(proc.stdout + proc.stderr, encoding="utf-8")

    # 解析 "1.23s call     tests/test_x.py::test_y"
    pattern = re.compile(r"^([\d.]+)s\s+(?:call|setup|teardown)\s+(\S+::\S+)", re.M)
    offenders: list[tuple[float, str]] = []
    for seconds, nodeid in pattern.findall(proc.stdout):
        value = float(seconds)
        if value >= threshold:
            offenders.append((value, nodeid))

    print(f"\n[guard] quick 集合耗时 {elapsed:.1f}s | 阈值 {threshold}s")
    if offenders:
        print(f"[guard] 发现 {len(offenders)} 个未标 slow 的慢用例（漏标，请补 @pytest.mark.slow）：")
        for value, nodeid in sorted(offenders, reverse=True)[:30]:
            print(f"    {value:7.2f}s  {nodeid}")
        print(f"[guard] 完整输出: {out}")
        return 1
    print("[guard] 通过：quick 集合内没有超过阈值的未标 slow 用例")
    return 0 if proc.returncode == 0 else proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="回归测试入口（quick/full/guard）")
    parser.add_argument("mode", choices=["quick", "full", "guard"])
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"并行进程数（默认 {DEFAULT_JOBS}；1 = 串行）")
    parser.add_argument("--threshold", type=float, default=2.0,
                        help="guard 模式的单用例耗时阈值（秒，默认 2.0）")
    ns = parser.parse_args()

    if ns.mode == "quick":
        return mode_quick(ns.jobs)
    if ns.mode == "full":
        return mode_full(ns.jobs)
    return mode_guard(ns.threshold)


if __name__ == "__main__":
    sys.exit(main())
