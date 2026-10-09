"""cyberstrike 代理池验证 runner（平台侧宽松连通性检查）。

上游 ``cli.py validate`` 的判据过严：要求代理**同时**通过 ① HTTPS(CONNECT) 延迟检测
② httpbin 匿名检测 ③ cachefly 测速，任一抛异常即 ``Failed``——大量只会明文转发 HTTP
的免费代理被误判死（实测同批 120 条：宽松单目标过 52，上游完整三段只过 9）。

本 runner 换成**单目标连通性**判据：经代理 ``GET`` 一个可配置目标，拿到响应即 ``Working``，
并记录延迟。**复用上游** ``ProxyChecker.check_proxy_url``（本仓不改上游文件），只在
其外层计时得到 latency + 拼装 JSON。

用法（平台 ``core/proxy/pool.py::_validate_records`` 调用）::

    python cyberstrike_check.py -i proxies.json -o out.json \
        --target http://www.baidu.com --timeout 5 --workers 50 \
        --output-format json --quiet

输出：``-o`` 指向的 JSON 数组
``[{"protocol":..., "proxy":"host:port", "status":"Working|Failed", "latency":秒|None, "error":...}, ...]``
（**含全部记录**，Failed 也写，供上层 ``_prune`` 剔除）；
stdout 另发**进度行** ``{"event":"progress","phase":"check","done":k,"total":N}``（平台流式解析）。

独立进程运行（cwd 应为 fir-proxy 目录），**不 import 平台任何模块**。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

DEFAULT_TARGET = "http://www.baidu.com"


def check_records(records: list[dict], target: str, timeout: int, workers: int,
                  checker_factory=None, on_progress=None) -> list[dict]:
    """经代理 GET ``target``：通即 Working（记 latency 秒），否则 Failed。含全部记录。

    ``checker_factory`` 为可注入工厂（无参返回带 ``check_proxy_url`` 的对象），
    测试传假件即可离线直测，不触网。``on_progress(payload)`` 每 ~20 条回调一次
    （``{"event":"progress","phase":"check","done":k,"total":N}``）。
    """
    if not records:
        return []

    if checker_factory is None:
        from modules.checker import ProxyChecker
        checker_factory = lambda: ProxyChecker(timeout=timeout)  # noqa: E731
    checker = checker_factory()

    def one(record: dict) -> dict:
        t0 = time.time()
        try:
            res = checker.check_proxy_url(record, target, timeout)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)}
        dt = time.time() - t0
        ok = bool(res.get("ok"))
        return {
            **record,
            "status": "Working" if ok else "Failed",
            "latency": dt if ok else None,
            "error": "" if ok else (res.get("error") or "不可达"),
        }

    total = len(records)
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as ex:
        futures = [ex.submit(one, r) for r in records]
        done = 0
        for fut in as_completed(futures):
            out.append(fut.result())
            done += 1
            if on_progress is not None and (done % 20 == 0 or done == total):
                on_progress({"event": "progress", "phase": "check",
                             "done": done, "total": total})
    return out


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    # Windows 默认控制台代码页（GBK）会把中文日志写坏；两路强制 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="cyberstrike_check.py",
        description="fir-proxy 代理池验证 runner（宽松单目标连通性）")
    parser.add_argument("-i", "--input", required=True, help="代理输入文件（JSON/TXT/CSV）")
    parser.add_argument("-o", "--output", required=True, help="输出 JSON 文件路径")
    parser.add_argument("--output-format", default="json", help="兼容 cli.py 接口（仅 json）")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="验证目标 URL")
    parser.add_argument("--timeout", type=int, default=5, help="单代理检查超时（秒）")
    parser.add_argument("--workers", type=int, default=50, help="并发数")
    parser.add_argument("--quiet", action="store_true", help="兼容 cli.py 接口")
    parser.add_argument("--log-file", help="兼容 cli.py 接口（本 runner 不落日志文件）")
    args = parser.parse_args(argv)

    import cli

    records, _invalid = cli.load_proxies(args.input)
    results = check_records(records, args.target, args.timeout, args.workers,
                            on_progress=_emit)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    working = sum(1 for r in results if r["status"] == "Working")
    _emit({
        "ok": True,
        "command": "check",
        "target": args.target,
        "count": len(results),
        "working": working,
        "failed": len(results) - working,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
