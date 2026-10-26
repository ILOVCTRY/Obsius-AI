"""SearXNG Docker lifecycle and HTTP probes."""

from __future__ import annotations

import base64
import json
import secrets
import subprocess
import socket
import socketserver
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

def _persisted_secret(config: dict, suffix: str) -> str:
    raw_path = str(config.get("settings_path") or "").strip()
    path = Path(raw_path) if raw_path else None
    secret_file = path.with_suffix(path.suffix + suffix) if path is not None else None
    if secret_file is not None:
        try:
            value = secret_file.read_text(encoding="ascii").strip()
            if len(value) >= 32:
                return value
        except OSError:
            pass
    value = secrets.token_urlsafe(48)
    if secret_file is not None:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(value, encoding="ascii")
    return value


def _settings_secret(config: dict) -> str:
    return _persisted_secret(config, ".secret")


def _relay_password(config: dict) -> str:
    return _persisted_secret(config, ".relay-secret")


class _ResponseContext:
    def __init__(self, response):
        self.response = response
    def __enter__(self):
        return self.response
    def __exit__(self, *_):
        close = getattr(self.response, "close", None)
        if close:
            close()


class _RelayHandler(socketserver.BaseRequestHandler):
    """Authenticate container proxy requests before forwarding to the host proxy."""
    def handle(self):
        try:
            self.request.settimeout(5)
            header = bytearray()
            while b"\r\n\r\n" not in header and len(header) < 65536:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                header.extend(chunk)
            end = header.find(b"\r\n\r\n")
            if end < 0:
                return
            request_head, remainder = bytes(header[:end]), bytes(header[end + 4:])
            lines = request_head.split(b"\r\n")
            expected = f"Basic {self.server.auth_token}".encode("ascii")
            auth_ok = any(line.partition(b":")[0].strip().lower() == b"proxy-authorization"
                          and line.partition(b":")[2].strip() == expected
                          for line in lines[1:])
            if not auth_ok:
                return
            clean_head = b"\r\n".join(
                line for index, line in enumerate(lines)
                if index == 0 or line.partition(b":")[0].strip().lower() != b"proxy-authorization")
            upstream = socket.create_connection(self.server.upstream, timeout=5)
            upstream.sendall(clean_head + b"\r\n\r\n" + remainder)
            self.request.settimeout(None)
            # upstream 也必须解除超时。`create_connection(timeout=5)` 给 socket
            # 留下了 5 秒超时，只清 client 侧不够：隧道建立后若 TLS 握手超过 5 秒，
            # `upstream.recv` 抛 `socket.timeout`（OSError 子类）→ 被 except 吞掉
            # → finally 关闭两端，客户端看到 `SSL_connect: Connection closed
            # abruptly`。实测表现为「同一代理时通时不通」（6 次里 1 次失败）。
            upstream.settimeout(None)
        except OSError:
            return

        def forward(source, target):
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    target.sendall(data)
            except OSError:
                pass
            finally:
                # 只关目标方向的写端，让另一方向把剩余数据读完。
                # 若在这里 shutdown 两端，先结束的那个方向会连带砍掉还在传输的
                # 另一个方向（TLS 握手/响应体中途被截断的另一个来源）。
                try:
                    target.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threading.Thread(target=forward, args=(self.request, upstream), daemon=True).start()
        forward(upstream, self.request)
        upstream.close()


class _ThreadingRelay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# 不能只写 "google"：google 的出口 IP 常被 403 风控、bing 偶发超时，单引擎
# 会让整次检索直接归零。这里给一组已验证可用的组合，坏一个还有别的顶上。
DEFAULT_ENGINES = "google,bing,brave"

DEFAULTS = {
    "enabled": False,
    "auto_start": False,
    "image": "docker.m.daocloud.io/searxng/searxng:latest",
    "container": "cyberstrike-searxng",
    "port": 8888,
    "relay_port": 7891,
    "upstream_proxy": "",
    "engines": DEFAULT_ENGINES,
    "settings_path": "",
}

# `engines` 配置是逗号分隔的引擎名，直接拼进 SearXNG 的 settings 时只认
# `{"name","engine","shortcut","disabled"}` 结构。只允许已知引擎名，避免把
# 随意字符串当模块名注入（SearXNG 会按 `engine` 字段动态 import）。
_KNOWN_ENGINES = ("google", "bing", "brave", "duckduckgo")


def _enabled_engines(raw: Any) -> list[dict]:
    """把 `"google,bing,brave"` 转成 SearXNG settings 的 engines 列表。"""
    names: list[str] = []
    for token in str(raw or "").replace("，", ",").split(","):
        name = token.strip().lower()
        if name in _KNOWN_ENGINES and name not in names:
            names.append(name)
    if not names:
        names = [n.strip() for n in DEFAULT_ENGINES.split(",")]
    return [
        {"name": name, "engine": name, "shortcut": name[:2], "disabled": False}
        for name in names
    ]


class SearXNG:
    def __init__(self, config: dict, runner: Callable = subprocess.run,
                 opener: Callable = urllib.request.urlopen):
        self.config = {**DEFAULTS, **(config or {})}
        self.runner = runner
        self.opener = opener
        self._owns_relay = False
        self._relay = None
        self._relay_thread = None
        self._relay_lock = threading.Lock()

    def _docker(self, *args: str) -> str:
        try:
            result = self.runner(["docker", *args], capture_output=True, text=True,
                                 timeout=15, check=False)
        except OSError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _open(self, url: str, timeout: int):
        response = self.opener(url, timeout=timeout)
        if hasattr(response, "__enter__"):
            return response
        return _ResponseContext(response)

    def _probe_proxy(self) -> bool:
        value = str(self.config.get("upstream_proxy") or "").strip()
        if not value:
            return False
        try:
            parsed = urlparse(value if "://" in value else f"http://{value}")
            with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=2):
                return True
        except (OSError, ValueError):
            return False

    def status(self) -> dict:
        try:
            docker = self.runner(["docker", "info"], capture_output=True, text=True,
                                 timeout=5, check=False)
            available = docker.returncode == 0
        except OSError:
            available = False
        image = bool(self._docker("image", "inspect", self.config["image"])) if available else False
        state = self._docker("inspect", "-f", "{{.State.Status}}", self.config["container"]) if available else ""
        running = state == "running"
        http_ok = False
        if running:
            try:
                with self._open(f"http://127.0.0.1:{int(self.config['port'])}/", timeout=3) as response:
                    http_ok = 200 <= response.status < 400
            except Exception:
                pass
        proxy_configured = bool(str(self.config.get("upstream_proxy") or "").strip())
        proxy = self._probe_proxy() if proxy_configured else None
        return {"docker": available, "image": image, "container": bool(state),
                "running": running, "http": http_ok, "proxy": proxy,
                "proxy_configured": proxy_configured,
                "enabled": bool(self.config["enabled"]), "auto_start": bool(self.config["auto_start"])}

    def _start_relay(self) -> bool:
        proxy = str(self.config.get("upstream_proxy") or "").strip()
        relay_port = int(self.config.get("relay_port") or 0)
        if relay_port < 1:
            return False
        if not proxy:
            return True
        try:
            parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
            if not parsed.hostname or not parsed.port:
                return False
            if not self._probe_proxy():
                return False
            with self._relay_lock:
                if self._relay is not None:
                    return True
                try:
                    with socket.create_connection(("127.0.0.1", relay_port), timeout=0.2):
                        return False
                except OSError:
                    pass
                relay = _ThreadingRelay(("127.0.0.1", relay_port), _RelayHandler)
                relay.upstream = (parsed.hostname, parsed.port)
                password = _relay_password(self.config)
                self.config["relay_auth_token"] = f"cyberstrike:{password}"
                relay.auth_token = base64.b64encode(
                    self.config["relay_auth_token"].encode("ascii")).decode("ascii")
                self._relay = relay
                self._relay_thread = threading.Thread(
                    target=relay.serve_forever, daemon=True, name="searxng-proxy-relay")
                self._relay_thread.start()
                self._owns_relay = True
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _stop_relay(self) -> None:
        if not self._owns_relay:
            return
        with self._relay_lock:
            relay, thread = self._relay, self._relay_thread
            self._relay = self._relay_thread = None
            self._owns_relay = False
        if relay is not None:
            relay.shutdown()
            relay.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def _write_settings(self) -> str:
        path = str(self.config.get("settings_path") or "").strip()
        if not path:
            return ""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        proxy = str(self.config.get("upstream_proxy") or "").strip()
        relay_port = int(self.config.get("relay_port") or 0)
        # userinfo 必须是**明文** `cyberstrike:<密码>`，不能预编码成 base64。
        # curl 拿到 userinfo 后会自己 base64 出 `Proxy-Authorization: Basic ...`；
        # 若这里再放已编码的串，实际发出的就是 base64(base64(...))，relay 校验
        # `expected = "Basic " + auth_token` 必然不等 → relay 直接 return 关连接，
        # curl 报 `(56) Proxy CONNECT aborted`，SearXNG 记成 `proxy error`、结果 0 条。
        # `token_urlsafe` 会产出 `-`/`_`，且密码可能被手工改成含 `+`/`/`/`=`；
        # 这些字符在 URL 的 userinfo 里有特殊含义，必须转义后才能可靠解析。
        credentials = (f"cyberstrike:{quote(_relay_password(self.config), safe='')}@"
                       if proxy else "")
        container_proxy = f"http://{credentials}host.docker.internal:{relay_port}" if proxy else ""
        settings = {
            "use_default_settings": True,
            "server": {"secret_key": _settings_secret(self.config), "limiter": False},
            "search": {"safe_search": 0, "formats": ["html", "json"]},
            # SearXNG 上游默认把 `google` 标成 `disabled: true`（只有 brave/
            # duckduckgo/wikipedia 等默认开启，而这些在国内出口基本不可达）。
            # 不显式覆盖，即使请求里写了 `engines=google` 也拿不到任何结果，
            # 且 google 不会出现在 `unresponsive_engines` 里——表现为「0 条结果
            # 但没有任何报错」，极难定位。
            # 只写死 google/bing 是不够的：google 的出口 IP 常被 403 风控、
            # bing 偶发超时，两者同时失效就是「0 条结果」。必须把配置里声明的
            # 引擎全部显式启用，否则 `brave` 这类默认开启的引擎虽能供结果，
            # 但一旦上游默认值变化就静默归零。
            "engines": _enabled_engines(self.config.get("engines")),
        }
        if container_proxy:
            # 值必须是**字符串**，不能是列表。
            # SearXNG 的 `_proxy_kwargs()` 把 `all://` 映射成 `{"proxy": <值>}` 后
            # 直接交给 curl_cffi；curl_cffi 的 `proxy` 参数只接受 str，传 list 会
            # 抛 ProxyError，SearXNG 记成 `unresponsive_engines: [["google","proxy error"]]`
            # —— 结果数为 0，但网络其实是通的（实测容器经 relay 可完成 TLS1.3 握手）。
            settings["outgoing"] = {"proxies": {"all://": container_proxy}}
        temp = p.with_suffix(p.suffix + ".tmp")
        temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(p)
        return str(p)

    def start(self) -> dict:
        st = self.status()
        if not st["docker"]:
            return {"status": "error", "detail": "Docker 不可用"}
        if st["running"]:
            if str(self.config.get("upstream_proxy") or "").strip() and not self._start_relay():
                return {"status": "error", "detail": "宿主代理不可用，无法启动 SearXNG 出网转发"}
            return {"status": "running", "reused": True}
        if st["container"]:
            if str(self.config.get("upstream_proxy") or "").strip() and not self._start_relay():
                return {"status": "error", "detail": "宿主代理不可用，无法启动 SearXNG 出网转发"}
            out = self._docker("start", self.config["container"])
        elif st["image"]:
            if not self._start_relay():
                return {"status": "error", "detail": "宿主代理不可用，无法启动 SearXNG 出网转发"}
            cmd = ["run", "-d", "--name", self.config["container"], "-p",
                   f"127.0.0.1:{self.config['port']}:8080",
                   "--add-host", "host.docker.internal:host-gateway"]
            settings = self._write_settings()
            if settings:
                cmd += ["-v", f"{settings}:/etc/searxng/settings.yml:ro"]
            cmd.append(self.config["image"])
            out = self._docker(*cmd)
        else:
            self._stop_relay()
            return {"status": "error", "detail": "镜像不存在；不会自动拉取"}
        if not out:
            self._stop_relay()
        return {"status": "started" if out else "error",
                "reused": bool(st["container"])}

    def stop(self) -> dict:
        st = self.status()
        if not st["running"]:
            self._stop_relay()
            return {"status": "stopped", "stopped": False}
        out = self._docker("stop", self.config["container"])
        self._stop_relay()
        return {"status": "stopped" if out else "error", "stopped": True}

    def connection_test(self) -> dict:
        state = self.status()
        return {"ok": bool(state["docker"] and state["image"] and state["container"]
                          and state["running"] and state["http"]
                          and (state["proxy"] is not False)), **state}

    def validate_search(self, query: str = "权限提升") -> dict:
        params = urllib.parse.urlencode({"q": query or "权限提升", "format": "json",
                                         "engines": self.config.get("engines") or DEFAULT_ENGINES,
                                         "time_range": "week"})
        url = f"http://127.0.0.1:{int(self.config['port'])}/search?{params}"
        try:
            with self._open(url, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            rows = data.get("results", [])
            degraded = data.get("unresponsive_engines") or []
            # 判据是「拿没拿到结果」，不是「有没有引擎降级」。多引擎配置下
            # （google 常被 403、bing 偶发超时）必然有引擎降级，若把 degraded
            # 也算失败，就会出现「验证检索失败：15 条」这种自相矛盾的结论。
            # 降级只作为提示信息返回，交给调用方展示。
            return {"ok": bool(rows), "count": len(rows),
                    "results": rows[:5],
                    "engines": sorted({r.get("engine", "") for r in rows if r.get("engine")}),
                    "dated": sum(1 for r in rows if r.get("publishedDate")),
                    "degraded": degraded, "time_range": "week"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc), "count": 0, "results": [], "degraded": []}
