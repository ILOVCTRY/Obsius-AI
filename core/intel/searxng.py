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
from typing import Callable
from urllib.parse import urlparse

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
                for stream in (source, target):
                    try:
                        stream.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=forward, args=(self.request, upstream), daemon=True).start()
        forward(upstream, self.request)
        upstream.close()


class _ThreadingRelay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


DEFAULTS = {
    "enabled": False,
    "auto_start": False,
    "image": "docker.m.daocloud.io/searxng/searxng:latest",
    "container": "cyberstrike-searxng",
    "port": 8888,
    "relay_port": 7891,
    "upstream_proxy": "",
    "engines": "google",
    "settings_path": "",
}


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
        auth_token = str(self.config.get("relay_auth_token") or "")
        credentials_raw = f"cyberstrike:{_relay_password(self.config)}"
        encoded_credentials = base64.b64encode(credentials_raw.encode("ascii")).decode("ascii") if proxy else ""
        credentials = f"{encoded_credentials}@" if encoded_credentials else ""
        container_proxy = f"http://{credentials}host.docker.internal:{relay_port}" if proxy else ""
        settings = {
            "use_default_settings": True,
            "server": {"secret_key": _settings_secret(self.config), "limiter": False},
            "search": {"safe_search": 0, "formats": ["html", "json"]},
        }
        if container_proxy:
            settings["outgoing"] = {"proxies": {"all://": [container_proxy]}}
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
                                         "engines": self.config.get("engines") or "google",
                                         "time_range": "week"})
        url = f"http://127.0.0.1:{int(self.config['port'])}/search?{params}"
        try:
            with self._open(url, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            rows = data.get("results", [])
            degraded = data.get("unresponsive_engines") or []
            return {"ok": bool(rows) and not degraded, "count": len(rows),
                    "results": rows[:5],
                    "engines": sorted({r.get("engine", "") for r in rows if r.get("engine")}),
                    "dated": sum(1 for r in rows if r.get("publishedDate")),
                    "degraded": degraded, "time_range": "week"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc), "count": 0, "results": [], "degraded": []}
