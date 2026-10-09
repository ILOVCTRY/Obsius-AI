"""cyberstrike 代理池 serve runner（平台侧薄包装）。

包 fir-proxy 的 ``ProxyRotator`` / ``ProxyServer``，额外提供 **loopback 控制通道**——
上游 fir-proxy 的 ``cli.py serve`` 是一次性、文件驱动的长驻进程，没有运行中控制口，
无法查实时当前代理、无法手动轮换。本 runner 补上这一层：

- ``GET  /status``  → 服务状态 + 当前代理 + 池计数 + 地区分布
- ``GET  /proxies`` → 池内全部代理记录（UI 列表 / AI 取可用 IP）
- ``POST /rotate``  → 强制轮换到下一个代理，返回新当前代理
- ``POST /reload``  → 热增删代理，body ``{"add": [record...], "remove": ["host:port"]}``
- ``POST /stop``    → 停止服务（进程随后自行退出）

**只绑 127.0.0.1**（``--control-host`` 缺省即回环），命令行入口给平台 ``ProxyPool`` 用。
stdout 走 JSONL 事件（``started`` / ``stopped``），日志走 stderr——stdout 是平台的解析通道，
勿污染。

独立进程运行（``python cyberstrike_serve.py ...``），平台经子进程管理；
不 import 平台任何模块。
"""

from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 本文件与 fir-proxy 同目录；作为脚本运行时脚本目录已在 sys.path[0]，
# 但仍显式兜底一次，便于被 import 或经 -m 运行。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Windows 默认用控制台代码页（GBK）写 stdout/stderr——stdout 是平台的 JSONL 解析通道，
# 中文（地区名 / 错误文案）会碎成非法 UTF-8。强制两路 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import cli as fir_cli  # noqa: E402  (fir-proxy 的 CLI 纯函数层)
from modules.rotator import ProxyRotator  # noqa: E402
from modules.server import ProxyServer  # noqa: E402

CONTROL_HOST_DEFAULT = "127.0.0.1"


def _emit(event: str, **payload) -> None:
    """stdout JSONL 事件（平台侧解析）。"""
    print(json.dumps({"event": event, **payload}, ensure_ascii=False,
                     separators=(",", ":")), flush=True)


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _proxy_view(record: dict) -> dict:
    """池记录 → 精简视图（UI / AI 消费）。"""
    return {
        "proxy": record.get("proxy"),
        "protocol": record.get("protocol"),
        "location": record.get("location"),
        "latency": record.get("latency"),
        "speed": record.get("speed"),
        "anonymity": record.get("anonymity"),
        "score": record.get("score"),
        "status": record.get("status"),
    }


class _State:
    """控制通道与转发服务共享的可变状态。"""

    def __init__(self, rotator: ProxyRotator, server: ProxyServer,
                 http_endpoint: str, socks5_endpoint: str):
        self.rotator = rotator
        self.server = server
        self.http_endpoint = http_endpoint
        self.socks5_endpoint = socks5_endpoint
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def status(self) -> dict:
        with self.lock:
            current = self.rotator.get_current_proxy()
            rows = self.rotator.get_all_proxies_for_revalidation()
            regions = self.rotator.get_available_regions_with_counts()
        return {
            "ok": True,
            "http": self.http_endpoint,
            "socks5": self.socks5_endpoint,
            "current": _proxy_view(current) if current else None,
            "count": len(rows),
            "working": self.rotator.get_active_proxies_count(),
            "regions": regions,
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def rotate(self) -> dict:
        with self.lock:
            nxt = self.rotator.get_next_proxy()
        _log(f"手动轮换 → {nxt.get('proxy') if nxt else '无可用代理'}")
        return {"ok": True, "current": _proxy_view(nxt) if nxt else None}

    def reload(self, add: list, remove: list) -> dict:
        added, removed = 0, 0
        with self.lock:
            for addr in remove or []:
                if self.rotator.remove_proxy(str(addr)):
                    removed += 1
            for record in add or []:
                if not isinstance(record, dict) or not record.get("proxy"):
                    continue
                record = dict(record)
                record.setdefault("status", "Working")
                before = len(self.rotator.get_all_proxies_for_revalidation())
                self.rotator.add_proxy(record)
                if len(self.rotator.get_all_proxies_for_revalidation()) > before:
                    added += 1
            if self.rotator.get_current_proxy() is None:
                self.rotator.get_next_proxy()
        _log(f"热更新池：+{added} / -{removed}")
        return {"ok": True, "added": added, "removed": removed}


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self) -> None:  # noqa: N802 (http.server 约定)
            path = self.path.split("?", 1)[0]
            if path == "/status":
                self._send(state.status())
            elif path == "/proxies":
                with state.lock:
                    rows = state.rotator.get_all_proxies_for_revalidation()
                self._send({"ok": True,
                            "proxies": [_proxy_view(r) for r in rows]})
            else:
                self._send({"ok": False, "error": "unknown path"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/rotate":
                self._send(state.rotate())
            elif path == "/reload":
                body = self._read_body()
                self._send(state.reload(body.get("add") or [],
                                        body.get("remove") or []))
            elif path == "/stop":
                self._send({"ok": True, "stopping": True})
                state.stop_event.set()
            else:
                self._send({"ok": False, "error": "unknown path"}, 404)

        def log_message(self, *args) -> None:
            pass  # 静默：勿污染 stderr

    return Handler


def _build_rotator(records: list, region: str, max_latency_ms: float | None):
    rotator = ProxyRotator()
    usable = [dict(r, status=r.get("status", "Working")) for r in records
              if r.get("status", "Working") == "Working"]
    for record in usable:
        rotator.add_proxy(record)  # 按 proxy 地址去重
    rotator.set_filters(region, max_latency_ms)
    return rotator, rotator.get_active_proxies_count()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cyberstrike_serve.py",
        description="fir-proxy 代理池 serve runner（含 loopback 控制通道）")
    parser.add_argument("-i", "--pool-file", required=True, help="代理池文件 TXT/JSON/CSV")
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=1801)
    parser.add_argument("--socks5-host", default="127.0.0.1")
    parser.add_argument("--socks5-port", type=int, default=1800)
    parser.add_argument("--control-host", default=CONTROL_HOST_DEFAULT)
    parser.add_argument("--control-port", type=int, required=True)
    parser.add_argument("--region", default="All")
    parser.add_argument("--max-latency-ms", type=float, default=None)
    parser.add_argument("--request-rotation-count", type=int, default=None)
    parser.add_argument("--target-failover-threshold", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        records, invalid = fir_cli.load_proxies(args.pool_file)
    except (OSError, ValueError) as exc:
        _log(f"读取代理池失败: {exc}")
        _emit("error", message=f"读取代理池失败: {exc}")
        return 2

    rotator, usable = _build_rotator(records, args.region, args.max_latency_ms)
    if usable == 0:
        _log("池内没有 status=Working 的可用代理")
        _emit("error", message="池内没有可用代理")
        return 2

    current = rotator.get_next_proxy()
    if not current:
        _log("筛选条件下没有可用代理")
        _emit("error", message="筛选条件下没有可用代理")
        return 2

    log_queue: "queue.Queue[str]" = queue.Queue()
    server = ProxyServer(args.http_host, args.http_port,
                         args.socks5_host, args.socks5_port,
                         rotator, log_queue)
    server.configure_rotation(
        args.request_rotation_count is not None,
        args.request_rotation_count or 10,
        args.target_failover_threshold is not None,
        args.target_failover_threshold or 3,
    )
    if not server.start_all():
        _log("本地代理服务启动失败（端口占用？）")
        _emit("error", message="本地代理服务启动失败")
        return 1

    state = _State(rotator, server,
                   f"{args.http_host}:{args.http_port}",
                   f"{args.socks5_host}:{args.socks5_port}")
    control = ThreadingHTTPServer((args.control_host, args.control_port),
                                  _make_handler(state))
    control.daemon_threads = True
    threading.Thread(target=control.serve_forever, daemon=True).start()

    def _sig(signum, _frame):
        _log(f"收到信号 {signum}，停止服务")
        state.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _sig)
        except (OSError, ValueError):
            pass

    _emit("started", http=state.http_endpoint, socks5=state.socks5_endpoint,
          control=f"{args.control_host}:{args.control_port}",
          current=_proxy_view(current), proxy_count=usable, invalid_lines=invalid)
    _log(f"HTTP {state.http_endpoint} / SOCKS5 {state.socks5_endpoint} / "
         f"控制 {args.control_host}:{args.control_port}（{usable} 个代理）")

    try:
        while not state.stop_event.wait(0.5):
            while True:  # 排空 fir-proxy 的日志队列到 stderr
                try:
                    _log(log_queue.get_nowait())
                except queue.Empty:
                    break
    except KeyboardInterrupt:
        state.stop_event.set()
    finally:
        server.stop_all()
        control.shutdown()
        _emit("stopped", ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
