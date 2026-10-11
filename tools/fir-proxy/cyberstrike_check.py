"""cyberstrike 代理池验证 runner（平台侧宽松连通性检查）。

上游 ``cli.py validate`` 的判据过严：要求代理**同时**通过 ① HTTPS(CONNECT) 延迟检测
② httpbin 匿名检测 ③ cachefly 测速，任一抛异常即 ``Failed``——大量只会明文转发 HTTP
的免费代理被误判死（实测同批 120 条：宽松单目标过 52，上游完整三段只过 9）。

本 runner 换成**单目标连通性**判据：经代理 ``GET`` 一个可配置目标，拿到响应即 ``Working``，
并记录延迟。**复用上游** ``ProxyChecker.check_proxy_url``（本仓不改上游文件），只在
其外层计时得到 latency + 拼装 JSON。

用法（平台 ``core/proxy/pool.py::_validate_records`` 调用）::

    python cyberstrike_check.py -i proxies.json -o out.json \
        --target http://www.baidu.com --target-https https://www.baidu.com \
        --timeout 5 --workers 50 --output-format json --quiet

输出：``-o`` 指向的 JSON 数组
``[{"protocol":..., "proxy":"host:port", "status":"Working|Failed", "latency":秒|None,
   "https_ok":bool|None, "error":...}, ...]``
（**含全部记录**，Failed 也写，供上层 ``_prune`` 剔除）；
``https_ok``（2026-10-11）：给了 ``--target-https`` 时，另经代理 GET 一个 https
目标（**走 CONNECT 隧道**）测「能否转发 HTTPS」，通即 True、不通 False；**只作能力
标注，不参与 status 判定**（免费代理普遍只转 HTTP，若并入 status 会把它们全判死）。
``hijacked``（2026-10-11 内容注入补丁）：给了 ``--target-b`` 时做**多目标一致性**交叉
验证——两个内容不同的目标经同一代理若返回**相同响应体指纹**，即判「内容注入/劫持型
出口」（对任意 host 回同一伪造页），``status`` 置 ``Failed``（附 ``hijacked=True``）。
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
DEFAULT_TARGET_HTTPS = "https://www.baidu.com"
# 多目标一致性交叉验证（2026-10-11 内容注入/劫持型出口补丁）：默认第二个目标与首个
# 不同源，用于识破「对任意 host 的 / 都返回同一伪造页」的劫持代理。
DEFAULT_TARGET_B = "http://example.com"


def _content_fingerprint(status, body: bytes) -> str:
    """响应体指纹：状态码 + 长度 + 正文 sha256 前 16 hex。

    用于交叉验证——两个**真实内容不同**的目标经同一代理若得到相同指纹，说明代理
    对任意目标都回同一页（内容注入/劫持）。指纹含长度，避免「同前缀不同体」误判。
    """
    import hashlib
    digest = hashlib.sha256(body or b"").hexdigest()[:16]
    return f"{status}:{len(body or b'')}:{digest}"


def _probe_body(record: dict, url: str, timeout: int) -> dict:
    """经代理 GET ``url`` 并**读取响应体**（上游 check_proxy_url 用 stream 即关、
    读不到体，故独立实现）。返回 ``{ok, status, body, error}``。

    仅做原始读取，不含任何平台依赖；供 ``check_records`` 的内容探测调用。
    """
    proxy = record.get("proxy")
    protocol = (record.get("protocol") or "http").lower()
    proxy_url = f"{protocol}://{proxy}"
    try:
        import requests
    except ImportError as exc:  # noqa: BLE001 —— 缺依赖=不具备内容探测能力
        return {"ok": False, "status": None, "body": b"", "error": f"requests 缺失: {exc}"}
    try:
        resp = requests.get(url, proxies={"http": proxy_url, "https": proxy_url},
                            timeout=timeout, allow_redirects=True)
        body = resp.content or b""
        return {"ok": True, "status": resp.status_code, "body": body, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "body": b"", "error": str(exc)}


def check_records(records: list[dict], target: str, timeout: int, workers: int,
                  checker_factory=None, on_progress=None,
                  target_https: str | None = None,
                  https_checker_factory=None,
                  target_b: str | None = None,
                  probe_factory=None) -> list[dict]:
    """经代理 GET ``target``：通即 Working（记 latency 秒），否则 Failed。含全部记录。

    ``target_https`` 非空时（2026-10-11）额外经代理 GET 该 https 目标（CONNECT 隧道）
    定 ``https_ok``（True/False）；**不参与 status**，只是能力标注。``https_checker_factory``
    可单独注入（测试用），缺省复用 ``checker_factory``。

    ``target_b`` 非空时（2026-10-11 内容注入补丁）做**多目标一致性**交叉验证：对已通过
    连通性的记录，再经代理读 ``target`` 与 ``target_b`` 两个**内容不同**目标的响应体指纹；
    两者指纹相同 → 判定为内容注入/劫持型出口，``status`` 置 ``Failed`` 并标 ``hijacked=True``
    （附 ``error`` 说明）。``probe_factory`` 可注入（测试用），默认用 ``_probe_body``。

    ``checker_factory``/``probe_factory`` 为可注入工厂（测试传假件即可离线直测，不触网）。
    ``on_progress(payload)`` 每 ~20 条回调一次。
    """
    if not records:
        return []

    if checker_factory is None:
        from modules.checker import ProxyChecker
        checker_factory = lambda: ProxyChecker(timeout=timeout)  # noqa: E731
    checker = checker_factory()
    https_checker = (https_checker_factory or checker_factory)() if target_https else None
    probe = (probe_factory or _probe_body) if target_b else None

    def one(record: dict) -> dict:
        t0 = time.time()
        try:
            res = checker.check_proxy_url(record, target, timeout)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)}
        dt = time.time() - t0
        ok = bool(res.get("ok"))
        out = {
            **record,
            "status": "Working" if ok else "Failed",
            "latency": dt if ok else None,
            "error": "" if ok else (res.get("error") or "不可达"),
        }
        if probe is not None and ok:
            # 内容注入交叉验证：两目标真实内容不同，同一代理若回相同体=伪造页。
            pa = probe(record, target, timeout)
            pb = probe(record, target_b, timeout)
            if not (pa.get("ok") and pb.get("ok")):
                # 探测本身失败不判死（可能只是第二个目标偶发不可达），保留连通结论。
                out["hijacked"] = False
            else:
                fp_a = _content_fingerprint(pa.get("status"), pa.get("body") or b"")
                fp_b = _content_fingerprint(pb.get("status"), pb.get("body") or b"")
                if fp_a == fp_b:
                    out["status"] = "Failed"
                    out["latency"] = None
                    out["hijacked"] = True
                    out["error"] = (f"内容注入/劫持型出口：对 {target} 与 {target_b} "
                                    f"返回同一响应（{fp_a}）")
                else:
                    out["hijacked"] = False
        if https_checker is not None:
            try:
                hres = https_checker.check_proxy_url(record, target_https, timeout)
            except Exception:  # noqa: BLE001 —— 探测失败=不具备 https 能力，不删记录
                hres = {"ok": False}
            out["https_ok"] = bool(hres.get("ok"))
        return out

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
    parser.add_argument("--target", default=DEFAULT_TARGET, help="验证目标 URL（HTTP 连通性）")
    parser.add_argument("--target-https", default=None,
                        help="HTTPS 能力探测目标（经 CONNECT）；省略/空=不探测")
    parser.add_argument("--target-b", default=None,
                        help="内容注入交叉验证的第二个目标（与 --target 内容不同）；"
                             "省略/空=不做多目标一致性校验")
    parser.add_argument("--timeout", type=int, default=5, help="单代理检查超时（秒）")
    parser.add_argument("--workers", type=int, default=50, help="并发数")
    parser.add_argument("--quiet", action="store_true", help="兼容 cli.py 接口")
    parser.add_argument("--log-file", help="兼容 cli.py 接口（本 runner 不落日志文件）")
    args = parser.parse_args(argv)

    import cli

    records, _invalid = cli.load_proxies(args.input)
    results = check_records(records, args.target, args.timeout, args.workers,
                            on_progress=_emit,
                            target_https=args.target_https or None,
                            target_b=args.target_b or None)

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
