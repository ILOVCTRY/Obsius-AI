"""Machine-friendly command line interface for fir-proxy.

The GUI remains the default user experience. This module exposes the same
core components through JSON/JSONL-friendly commands for scripts and agents.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
PROTOCOLS = {"http", "https", "socks4", "socks5"}
CANONICAL_PROTOCOLS = {"https": "http"}


def _safe_value(value: Any) -> Any:
    """Make module results safe for JSON serialization."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return value


def _canonical_protocol(protocol: str | None) -> str:
    value = (protocol or "http").strip().lower()
    return CANONICAL_PROTOCOLS.get(value, value)


def _normalize_target_url(url: str) -> str:
    target = url.strip()
    if not target:
        raise ValueError("URL 不能为空")
    return target if "://" in target else f"http://{target}"


def _split_proxy(value: str, default_protocol: str = "http") -> tuple[str, str] | None:
    """Parse protocol://host:port, protocol,host:port, or host:port."""
    raw = value.strip()
    if not raw or raw.startswith("#"):
        return None

    protocol = default_protocol
    address = raw
    if "://" in raw:
        protocol, address = raw.split("://", 1)
    elif "," in raw:
        possible_protocol, possible_address = raw.split(",", 1)
        if possible_protocol.strip().lower() in PROTOCOLS:
            protocol, address = possible_protocol, possible_address

    protocol = _canonical_protocol(protocol)
    if protocol not in {"http", "socks4", "socks5"}:
        return None

    address = address.strip().rstrip("/")
    if "@" in address:
        address = address.rsplit("@", 1)[1]

    # Validate the shape without restricting hostnames to IPv4 addresses.
    if ":" not in address:
        return None
    host, port = address.rsplit(":", 1)
    if not host.strip() or not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    return protocol, f"{host.strip()}:{int(port)}"


def _record_from_item(item: Any, default_protocol: str = "http") -> dict[str, Any] | None:
    if isinstance(item, str):
        parsed = _split_proxy(item, default_protocol)
        return {"protocol": parsed[0], "proxy": parsed[1]} if parsed else None
    if not isinstance(item, dict):
        return None

    protocol = item.get("protocol") or item.get("type") or default_protocol
    value = item.get("proxy") or item.get("url")
    if not value and item.get("host") and item.get("port"):
        value = f"{item['host']}:{item['port']}"
    if not value and item.get("ip") and item.get("port"):
        value = f"{item['ip']}:{item['port']}"
    if not value:
        return None

    parsed = _split_proxy(str(value), str(protocol))
    if not parsed:
        return None

    record = dict(item)
    record["protocol"], record["proxy"] = parsed
    if "latency" not in record and item.get("latency_ms") not in (None, ""):
        try:
            record["latency"] = float(item["latency_ms"]) / 1000
        except (TypeError, ValueError):
            pass
    if "speed" not in record and item.get("speed_mbps") not in (None, ""):
        try:
            record["speed"] = float(item["speed_mbps"])
        except (TypeError, ValueError):
            pass
    for numeric_key in ("latency", "speed", "score"):
        if numeric_key in record and isinstance(record[numeric_key], str):
            try:
                record[numeric_key] = float(record[numeric_key])
            except ValueError:
                if not record[numeric_key].strip():
                    record.pop(numeric_key, None)
    if not record.get("status"):
        record["status"] = "Working"
    if "score" not in record and any(key in record for key in ("latency", "speed", "anonymity")):
        record["score"] = score_result(record)
    return record


def load_proxies(path: str) -> tuple[list[dict[str, Any]], int]:
    """Load proxy records from TXT, JSON, CSV, or stdin."""
    source_name = "<stdin>" if path == "-" else path
    if path == "-":
        text = sys.stdin.read()
        stripped = text.lstrip()
        first_line = next((line for line in text.splitlines() if line.strip()), "")
        if stripped.startswith("["):
            suffix = ".json"
        elif stripped.startswith("{"):
            try:
                json.loads(text)
            except json.JSONDecodeError:
                suffix = ".jsonl"
            else:
                suffix = ".json"
        elif "," in first_line and "proxy" in first_line.lower():
            suffix = ".csv"
        else:
            suffix = ".txt"
    else:
        source = Path(path)
        text = source.read_text(encoding="utf-8-sig")
        suffix = source.suffix.lower()

    items: list[Any]
    if suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            payload = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            payload = json.loads(text)
        if isinstance(payload, dict):
            for key in ("proxies", "data", "results"):
                if key in payload:
                    payload = payload[key]
                    break
        items = payload if isinstance(payload, list) else [payload]
    elif suffix == ".csv":
        items = list(csv.DictReader(text.splitlines()))
    else:
        items = [line.strip() for line in text.splitlines()]

    records: list[dict[str, Any]] = []
    invalid = 0
    seen: set[tuple[str, str]] = set()
    for item in items:
        record = _record_from_item(item)
        if not record:
            if isinstance(item, str) and (not item.strip() or item.strip().startswith("#")):
                continue
            invalid += 1
            continue
        key = (record["protocol"], record["proxy"])
        if key in seen:
            continue
        seen.add(key)
        records.append(record)

    if not records and invalid:
        raise ValueError(f"{source_name} 中没有可识别的代理记录（无效行: {invalid}）")
    return records, invalid


def records_by_protocol(records: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    result = {"http": [], "socks4": [], "socks5": []}
    for record in records:
        protocol = _canonical_protocol(record.get("protocol"))
        if protocol in result:
            result[protocol].append(record["proxy"])
    return result


def score_result(result: dict[str, Any]) -> float:
    latency = result.get("latency")
    speed = result.get("speed") or 0
    score = 0.0
    if isinstance(latency, (int, float)) and math.isfinite(latency) and latency > 0:
        score += (1 / latency) * 50
    score += float(speed) * 10
    if result.get("anonymity") == "Elite":
        score += 50
    elif result.get("anonymity") == "Anonymous":
        score += 20
    return round(score, 3)


class EventWriter:
    """Emit JSONL events to stdout and mirror operational logs to stderr/file."""

    def __init__(self, fmt: str, log_file: str | None, quiet: bool = False):
        self.fmt = fmt
        self.quiet = quiet
        self.log_path = None
        self._log_handle = None
        if log_file:
            self.log_path = str(Path(log_file).resolve())
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self.log_path, "a", encoding="utf-8")

    def emit(self, event: str, **payload: Any) -> None:
        body = {"event": event, **_safe_value(payload)}
        if self.fmt == "json":
            # JSON mode is intended for commands that emit one final document.
            return
        if self.fmt == "text":
            message = payload.get("message") or payload.get("status") or event
            print(f"[{event}] {message}", flush=True)
            return
        print(json.dumps(body, ensure_ascii=False, separators=(",", ":")), flush=True)

    def final(self, payload: dict[str, Any]) -> None:
        body = _safe_value(payload)
        if self.fmt == "text":
            print(json.dumps(body, ensure_ascii=False, indent=2), flush=True)
        else:
            print(json.dumps(body, ensure_ascii=False, separators=(",", ":")), flush=True)

    def log(self, message: str) -> None:
        timestamped = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        if self._log_handle:
            self._log_handle.write(timestamped + "\n")
            self._log_handle.flush()
        if not self.quiet:
            print(message, file=sys.stderr, flush=True)

    def close(self) -> None:
        if self._log_handle:
            self._log_handle.close()


def drain_logs(log_queue: queue.Queue[str], writer: EventWriter) -> None:
    while True:
        try:
            writer.log(log_queue.get_nowait())
        except queue.Empty:
            return


def run_worker_with_heartbeats(
    worker,
    log_queue: queue.Queue[str],
    writer: EventWriter,
    heartbeat_payload,
):
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    last_heartbeat = time.monotonic()
    while thread.is_alive():
        drain_logs(log_queue, writer)
        now = time.monotonic()
        if now - last_heartbeat >= 5:
            writer.emit("progress", **heartbeat_payload())
            last_heartbeat = now
        time.sleep(0.1)
    thread.join()
    drain_logs(log_queue, writer)


def write_records(path: str | None, records: list[dict[str, Any]], output_format: str | None = None) -> str | None:
    if not path:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fmt = output_format or target.suffix.lower().lstrip(".") or "json"
    safe_records = _safe_value(records)

    if fmt in {"txt", "text"}:
        lines = [f"{r.get('protocol', 'http').lower()}://{r['proxy']}" for r in records]
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    elif fmt == "csv":
        fields = ["score", "anonymity", "protocol", "proxy", "latency_ms", "speed_mbps", "location", "status"]
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                row = dict(record)
                if isinstance(row.get("latency"), (int, float)):
                    row["latency_ms"] = round(row["latency"] * 1000, 3)
                if "speed" in row:
                    row["speed_mbps"] = row["speed"]
                writer.writerow(row)
    elif fmt == "jsonl":
        target.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in safe_records)
            + ("\n" if safe_records else ""),
            encoding="utf-8",
        )
    else:
        target.write_text(json.dumps(safe_records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(target.resolve())


def make_log_path(command: str, requested: str | None) -> str | None:
    if requested:
        return requested
    if command in {"fetch", "validate", "check-url", "serve"}:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return str(Path("logs") / f"cli-{command}-{stamp}.log")
    return None


def command_fetch(args: argparse.Namespace, writer: EventWriter) -> int:
    from modules.fetcher import ProxyFetcher

    log_queue: queue.Queue[str] = queue.Queue()
    fetched: dict[str, list[str]] = {}
    protocols = args.protocol or ["http", "socks4", "socks5"]
    worker_errors: list[BaseException] = []

    def worker():
        try:
            fetched.update(ProxyFetcher().fetch_all(log_queue))
        except BaseException as exc:
            worker_errors.append(exc)

    writer.emit("start", command="fetch")
    run_worker_with_heartbeats(
        worker,
        log_queue,
        writer,
        lambda: {"command": "fetch", "message": "仍在获取代理源", "sources": "in_progress"},
    )
    if worker_errors:
        raise worker_errors[0]
    records = [
        {"protocol": protocol, "proxy": proxy}
        for protocol, values in fetched.items()
        if protocol in protocols
        for proxy in sorted(set(values))
    ]
    output_path = write_records(args.output, records, args.output_format)
    writer.final({
        "ok": True,
        "command": "fetch",
        "count": len(records),
        "by_protocol": {p: sum(1 for r in records if r["protocol"] == p) for p in protocols},
        "output": output_path,
        "log_file": writer.log_path,
        "proxies": records if not output_path else None,
    })
    return 0


def command_validate(args: argparse.Namespace, writer: EventWriter) -> int:
    from modules.checker import ProxyChecker

    if args.timeout <= 0 or args.workers <= 0:
        raise ValueError("--timeout 和 --workers 必须大于 0")
    records, invalid = load_proxies(args.input)
    proxy_groups = records_by_protocol(records)
    total = sum(len(values) for values in proxy_groups.values())
    result_queue: queue.Queue[Any] = queue.Queue()
    log_queue: queue.Queue[str] = queue.Queue()
    results: list[dict[str, Any]] = []
    state = {"completed": 0}
    worker_errors: list[BaseException] = []

    def worker():
        try:
            ProxyChecker(timeout=args.timeout).validate_all(
                proxy_groups,
                result_queue,
                log_queue,
                validation_mode=args.mode,
                max_workers=args.workers,
            )
        except BaseException as exc:
            worker_errors.append(exc)

    writer.emit("start", command="validate", total=total, invalid_lines=invalid)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    last_heartbeat = time.monotonic()
    finished_signal = False
    while thread.is_alive() or not result_queue.empty():
        drain_logs(log_queue, writer)
        try:
            result = result_queue.get(timeout=0.2)
            if result is None:
                finished_signal = True
                continue
            result["score"] = score_result(result)
            result = _safe_value(result)
            results.append(result)
            state["completed"] += 1
            writer.emit(
                "result",
                command="validate",
                index=state["completed"],
                total=total,
                result=result,
            )
            if state["completed"] % 10 == 0:
                writer.emit(
                    "progress",
                    command="validate",
                    completed=state["completed"],
                    total=total,
                    working=sum(1 for item in results if item.get("status") == "Working"),
                )
        except queue.Empty:
            pass
        if time.monotonic() - last_heartbeat >= 5:
            writer.emit(
                "progress",
                command="validate",
                completed=state["completed"],
                total=total,
                working=sum(1 for item in results if item.get("status") == "Working"),
            )
            last_heartbeat = time.monotonic()
    thread.join()
    drain_logs(log_queue, writer)
    if worker_errors:
        raise worker_errors[0]
    # The sentinel is useful for normal completion, but a defensive fallback
    # keeps the CLI usable if a future checker implementation omits it.
    if total and not finished_signal and not results:
        writer.log("验证器未返回结果。")

    output_path = write_records(args.output, results, args.output_format)
    working = [item for item in results if item.get("status") == "Working"]
    writer.final({
        "ok": True,
        "command": "validate",
        "input": str(Path(args.input).resolve()) if args.input != "-" else "-",
        "total": total,
        "completed": len(results),
        "working": len(working),
        "failed": len(results) - len(working),
        "invalid_lines": invalid,
        "output": output_path,
        "log_file": writer.log_path,
        "proxies": results if not output_path else None,
    })
    return 0


def command_check_url(args: argparse.Namespace, writer: EventWriter) -> int:
    from modules.checker import ProxyChecker

    if args.timeout <= 0 or args.workers <= 0:
        raise ValueError("--timeout 和 --workers 必须大于 0")
    records, invalid = load_proxies(args.input)
    targets = [_normalize_target_url(url) for url in args.url]
    jobs = [(record, url) for record in records for url in targets]
    results: list[dict[str, Any]] = []
    writer.emit("start", command="check-url", total=len(jobs), invalid_lines=invalid)

    checker = ProxyChecker(timeout=args.timeout)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(checker.check_proxy_url, record, url, args.timeout): (record, url)
            for record, url in jobs
        }
        last_heartbeat = time.monotonic()
        for index, future in enumerate(as_completed(futures), start=1):
            record, url = futures[future]
            try:
                check = future.result()
            except Exception as exc:
                check = {"ok": False, "status_code": None, "error": str(exc)}
            item = {
                "proxy": record["proxy"],
                "protocol": record["protocol"],
                "url": url,
                **check,
            }
            results.append(item)
            writer.emit("result", command="check-url", index=index, total=len(jobs), result=item)
            if index % 10 == 0:
                writer.emit(
                    "progress",
                    command="check-url",
                    completed=index,
                    total=len(jobs),
                    ok=sum(1 for row in results if row["ok"]),
                    failed=sum(1 for row in results if not row["ok"]),
                )
            if time.monotonic() - last_heartbeat >= 5:
                writer.emit(
                    "progress",
                    command="check-url",
                    completed=index,
                    total=len(jobs),
                    ok=sum(1 for row in results if row["ok"]),
                    failed=sum(1 for row in results if not row["ok"]),
                )
                last_heartbeat = time.monotonic()

    output_path = write_records(args.output, results, args.output_format)
    writer.final({
        "ok": True,
        "command": "check-url",
        "total": len(results),
        "ok_count": sum(1 for row in results if row["ok"]),
        "failed": sum(1 for row in results if not row["ok"]),
        "invalid_lines": invalid,
        "output": output_path,
        "log_file": writer.log_path,
        "results": results if not output_path else None,
    })
    return 0


def command_normalize(args: argparse.Namespace, writer: EventWriter) -> int:
    records, invalid = load_proxies(args.input)
    output_path = write_records(args.output, records, args.output_format)
    writer.final({
        "ok": True,
        "command": "normalize",
        "count": len(records),
        "invalid_lines": invalid,
        "output": output_path,
        "proxies": records if not output_path else None,
    })
    return 0


def command_select(args: argparse.Namespace, writer: EventWriter) -> int:
    if args.limit <= 0:
        raise ValueError("--limit 必须大于 0")
    records, invalid = load_proxies(args.input)
    candidates = [
        record for record in records
        if args.include_failed or record.get("status", "Working") == "Working"
    ]
    if args.region and args.region.lower() != "all":
        candidates = [item for item in candidates if item.get("location") == args.region]
    if args.max_latency_ms is not None:
        candidates = [
            item for item in candidates
            if isinstance(item.get("latency"), (int, float))
            and math.isfinite(item["latency"])
            and item["latency"] * 1000 <= args.max_latency_ms
        ]
    candidates.sort(key=lambda item: (item.get("score", 0), -(item.get("latency") or math.inf)), reverse=True)
    selected = candidates[:args.limit]
    writer.final({
        "ok": True,
        "command": "select",
        "count": len(selected),
        "available": len(candidates),
        "invalid_lines": invalid,
        "proxies": selected,
    })
    return 0


def command_serve(args: argparse.Namespace, writer: EventWriter) -> int:
    from modules.rotator import ProxyRotator
    from modules.server import ProxyServer

    records, invalid = load_proxies(args.input)
    usable = [
        dict(record, status=record.get("status", "Working"))
        for record in records
        if record.get("status", "Working") == "Working"
    ]
    if not usable:
        raise ValueError("输入文件中没有可用于启动服务的代理")
    for name, port in (("HTTP", args.http_port), ("SOCKS5", args.socks5_port)):
        if not 1 <= port <= 65535:
            raise ValueError(f"{name} 端口必须在 1 到 65535 之间")

    rotator = ProxyRotator()
    for record in usable:
        rotator.add_proxy(record)
    rotator.set_filters(args.region, args.max_latency_ms)
    current = rotator.get_next_proxy()
    if not current:
        raise ValueError("筛选条件下没有可用代理")

    log_queue: queue.Queue[str] = queue.Queue()
    server = ProxyServer(
        args.http_host,
        args.http_port,
        args.socks5_host,
        args.socks5_port,
        rotator,
        log_queue,
    )
    server.configure_rotation(
        args.request_rotation_count is not None,
        args.request_rotation_count or 10,
        args.target_failover_threshold is not None,
        args.target_failover_threshold or 3,
    )
    stop_event = threading.Event()

    def stop_handler(signum, _frame):
        writer.log(f"收到信号 {signum}，正在停止服务")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop_handler)
        except (OSError, ValueError):
            pass

    if not server.start_all():
        raise OSError("本地代理服务启动失败，请检查监听地址和端口占用情况")
    drain_logs(log_queue, writer)
    writer.emit(
        "started",
        command="serve",
        http=f"{args.http_host}:{args.http_port}",
        socks5=f"{args.socks5_host}:{args.socks5_port}",
        current_proxy=current,
        proxy_count=len(usable),
        invalid_lines=invalid,
    )
    try:
        while not stop_event.wait(1):
            drain_logs(log_queue, writer)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        server.stop_all()
        drain_logs(log_queue, writer)
    writer.final({
        "ok": True,
        "command": "serve",
        "stopped": True,
        "log_file": writer.log_path,
    })
    return 0


def add_input_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-i", "--input", required=True, help="代理输入文件，支持 TXT/JSON/CSV；使用 - 表示 stdin")
    parser.add_argument("-o", "--output", help="输出文件；不指定时直接返回结果")
    parser.add_argument("--output-format", choices=["json", "jsonl", "csv", "txt"], help="输出文件格式，默认按扩展名判断")


def add_runtime_options(parser: argparse.ArgumentParser, default_format: str = "jsonl") -> None:
    parser.add_argument("--format", choices=["jsonl", "json", "text"], default=default_format, help="stdout 格式，网络任务默认 JSONL")
    parser.add_argument("--log-file", help="日志文件路径；网络任务默认写入 logs/cli-*.log")
    parser.add_argument("--quiet", action="store_true", help="不向 stderr 输出运行日志")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python cli.py",
        description="fir-proxy 的无界面、AI 友好命令行接口",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="从在线来源抓取代理")
    fetch.add_argument("--protocol", action="append", choices=["http", "socks4", "socks5"], default=None)
    fetch.add_argument("-o", "--output")
    fetch.add_argument("--output-format", choices=["json", "jsonl", "csv", "txt"])
    add_runtime_options(fetch)
    fetch.set_defaults(handler=command_fetch)

    validate = subparsers.add_parser("validate", help="批量验证代理质量")
    add_input_output_options(validate)
    validate.add_argument("--timeout", type=int, default=5)
    validate.add_argument("--workers", type=int, default=100)
    validate.add_argument("--mode", choices=["online", "import"], default="online")
    add_runtime_options(validate)
    validate.set_defaults(handler=command_validate)

    check_url = subparsers.add_parser("check-url", help="通过代理批量检查目标 URL")
    add_input_output_options(check_url)
    check_url.add_argument("--url", action="append", required=True, help="目标 URL，可重复指定")
    check_url.add_argument("--timeout", type=int, default=15)
    check_url.add_argument("--workers", type=int, default=32)
    add_runtime_options(check_url)
    check_url.set_defaults(handler=command_check_url)

    normalize = subparsers.add_parser("normalize", help="解析并标准化代理列表")
    add_input_output_options(normalize)
    add_runtime_options(normalize, default_format="json")
    normalize.set_defaults(handler=command_normalize)

    select = subparsers.add_parser("select", help="从验证结果中筛选高质量代理")
    add_input_output_options(select)
    select.add_argument("--limit", type=int, default=1)
    select.add_argument("--region")
    select.add_argument("--max-latency-ms", type=float)
    select.add_argument("--include-failed", action="store_true")
    add_runtime_options(select, default_format="json")
    select.set_defaults(handler=command_select)

    serve = subparsers.add_parser("serve", help="启动本地 HTTP 和 SOCKS5 代理服务")
    add_input_output_options(serve)
    serve.add_argument("--http-host", default="127.0.0.1")
    serve.add_argument("--http-port", type=int, default=1801)
    serve.add_argument("--socks5-host", default="127.0.0.1")
    serve.add_argument("--socks5-port", type=int, default=1800)
    serve.add_argument("--region", default="All")
    serve.add_argument("--max-latency-ms", type=float)
    serve.add_argument("--request-rotation-count", type=int)
    serve.add_argument("--target-failover-threshold", type=int)
    add_runtime_options(serve)
    serve.set_defaults(handler=command_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    writer = EventWriter(args.format, make_log_path(args.command, args.log_file), args.quiet)
    try:
        return args.handler(args, writer)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        writer.log(f"[ERROR] {exc}")
        writer.final({"ok": False, "command": args.command, "error": str(exc), "log_file": writer.log_path})
        return 2
    except Exception as exc:
        writer.log(f"[ERROR] 未处理异常: {exc}")
        writer.final({"ok": False, "command": args.command, "error": str(exc), "log_file": writer.log_path})
        return 1
    finally:
        writer.close()


if __name__ == "__main__":
    raise SystemExit(main())
