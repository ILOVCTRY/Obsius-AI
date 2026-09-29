"""MCP 运行时桥（K9，2026-09-29）：把 config/mcp.json 已配 server 的工具动态
注册进智能体工作台对话运行时（主控+子专家均可用）。

- streamable-http：JSON-RPC over HTTP（initialize → Mcp-Session-Id →
  notifications/initialized → tools/list、tools/call），协议时序与
  core/tools/decompiler.py 的 MCPBackend 同款；只允许本机 loopback。
- stdio：子进程 + 行分隔 JSON-RPC（MCP stdio 传输），后台读线程收响应，
  stderr 排干防管道阻塞；进程懒拉起、崩溃自动重启一次。
- 工具名空间：``mcp__<server>__<tool>``；域名白名单沿用配置层
  （pentest/reverse/binary），domains 不相交的 server 不启用。

全程 best-effort：单 server 离线不影响其它 server，tools/list 失败返回空。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MCP_PROTOCOL_VERSION = "2024-11-05"
_CALL_TIMEOUT = 60.0
_PROBE_TIMEOUT = 8.0


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _parse_body(raw: str):
    """streamable-http 响应体解析：JSON 或 SSE（data: 行）兜底。"""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{") or raw.startswith("["):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    for line in raw.splitlines():  # SSE：取最后一条 data JSON
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    return None


class _HttpConn:
    """streamable-http JSON-RPC 连接（协议时序同 MCPBackend）。"""

    def __init__(self, endpoint: str):
        if not _is_loopback_url(endpoint):
            raise ValueError(f"MCP 只允许连本机 loopback 端点: {endpoint}")
        self.endpoint = endpoint
        self._session_id: str | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    def _post(self, method: str, params: dict | None, *, notification: bool,
              timeout: float):
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            self._next_id += 1
            body["id"] = self._next_id
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode("utf-8"),
            method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, _parse_body(raw), dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, _parse_body(raw), dict(e.headers.items())
        except Exception:  # noqa: BLE001 —— 离线/超时一律视为失败
            return 0, None, {}

    def _handshake(self) -> bool:
        self._session_id = None
        status, data, headers = self._post("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cyberstrike-pro-chat", "version": "0.1"},
        }, notification=False, timeout=_PROBE_TIMEOUT)
        if status == 0 or not (200 <= status < 300) \
                or not isinstance(data, dict) or "result" not in data:
            return False
        sid = headers.get("Mcp-Session-Id") \
            or headers.get("mcp-session-id")
        self._session_id = sid or None
        self._post("notifications/initialized", {},
                   notification=True, timeout=_PROBE_TIMEOUT)
        return True

    def _rpc(self, method: str, params: dict | None = None,
             timeout: float | None = None):
        for attempt in (1, 2):
            with self._lock:
                if self._session_id is None and not self._handshake():
                    return None
                status, data, _h = self._post(
                    method, params, notification=False,
                    timeout=timeout or _CALL_TIMEOUT)
                if status == 0:
                    self._session_id = None
                    return None
                if status in (400, 404, 405, 409, 410) and attempt == 1:
                    self._session_id = None  # 旧 session 失效：重握一轮
                    continue
                break
        if not isinstance(data, dict) or data.get("error") is not None:
            return None
        return data.get("result")


class _StdioConn:
    """stdio JSON-RPC 连接：子进程懒拉起，行分隔请求，后台线程收响应。

    MCP stdio 传输 = 每行一条 JSON-RPC 消息。stderr 单独排干（很多 server
    往 stderr 打日志，不排干会堵管道）。请求按 id 关联 Future；通知不等待。"""

    def __init__(self, command: str, args: list[str]):
        self._cmd = [command, *args]
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}  # id -> {"event","resp"}
        self._next_id = 0

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        try:
            self._proc = subprocess.Popen(
                self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            self._proc = None
            return False
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return True

    def _pump_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            mid = msg.get("id")
            if isinstance(mid, int) and mid in self._pending:
                waiter = self._pending.pop(mid)
                waiter["resp"] = msg
                waiter["event"].set()
        # 进程退出：唤醒所有等待者
        for waiter in self._pending.values():
            waiter["event"].set()

    def _drain_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for _line in proc.stderr:
            pass  # 排干防阻塞

    def _rpc(self, method: str, params: dict | None = None,
             timeout: float | None = None):
        with self._lock:
            if not self._ensure():
                return None
            self._next_id += 1
            mid = self._next_id
            waiter: dict[str, Any] = {"event": threading.Event(), "resp": None}
            self._pending[mid] = waiter
            body = {"jsonrpc": "2.0", "method": method,
                    "params": params or {}, "id": mid}
            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(body) + "\n")
                self._proc.stdin.flush()
            except (OSError, AssertionError):
                self._pending.pop(mid, None)
                return None
        if not waiter["event"].wait(timeout or _CALL_TIMEOUT):
            self._pending.pop(mid, None)
            return None
        msg = waiter["resp"]
        if not isinstance(msg, dict) or msg.get("error") is not None:
            return None
        return msg.get("result")


class MCPServerRuntime:
    """单个 server 的运行时连接：懒握手、tools/list 缓存、tools/call。"""

    HTTP_TRANSPORTS = ("streamable-http", "http", "http-stream")

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.transport = str(cfg.get("transport") or "stdio")
        self.domains = cfg.get("domains") or []
        if self.transport in self.HTTP_TRANSPORTS:
            self._conn: Any = _HttpConn(str(cfg.get("url") or ""))
        else:
            self._conn = _StdioConn(
                str(cfg.get("command") or ""), list(cfg.get("args") or []))
        self._tools: list[dict] | None = None
        self._tools_at = 0.0
        self._lock = threading.Lock()

    def list_tools(self, *, refresh: bool = False) -> list[dict]:
        with self._lock:
            if not refresh and self._tools is not None \
                    and time.monotonic() - self._tools_at < 300.0:
                return self._tools
            result = self._conn._rpc("tools/list", {}, timeout=_PROBE_TIMEOUT)
            tools = []
            if isinstance(result, dict):
                for t in result.get("tools") or []:
                    if isinstance(t, dict) and t.get("name"):
                        tools.append({
                            "name": str(t["name"]),
                            "description": str(t.get("description") or "")[:300],
                            "input_schema": t.get("inputSchema") or {},
                        })
            self._tools = tools
            self._tools_at = time.monotonic()
            return tools

    def call(self, tool: str, args: dict) -> str:
        result = self._conn._rpc(
            "tools/call", {"name": tool, "arguments": args},
            timeout=_CALL_TIMEOUT)
        if not isinstance(result, dict):
            return "[错误] MCP 调用失败：server 离线或协议错误"
        texts = [c.get("text", "") for c in result.get("content") or []
                 if isinstance(c, dict) and c.get("type", "text") == "text"]
        text = "\n".join(t for t in texts if t).strip()
        if result.get("isError"):
            return f"[错误] MCP 工具报错: {text[:2000] or '未知错误'}"
        return text or "(空结果)"


class MCPBridge:
    """全部已启用 server 的桥：按项目域过滤，聚合工具清单与调用。"""

    def __init__(self, config_path: str | Path, domains: list[str] | None = None):
        self.config_path = Path(config_path)
        self._domains = {d for d in (domains or []) if d}
        self._servers: dict[str, MCPServerRuntime] = {}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _load_servers(self) -> dict[str, MCPServerRuntime]:
        with self._lock:
            if self._servers and time.monotonic() - self._loaded_at < 60.0:
                return self._servers
            servers: dict[str, MCPServerRuntime] = {}
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cfg = {}
            for entry in cfg.get("servers") or []:
                if not isinstance(entry, dict) or not entry.get("enabled"):
                    continue
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                domains = {d for d in (entry.get("domains") or []) if d}
                if self._domains and domains and not (domains & self._domains):
                    continue
                try:
                    servers[name] = MCPServerRuntime(name, entry)
                except (ValueError, OSError):
                    continue  # 非法端点等：跳过该 server
            self._servers = servers
            self._loaded_at = time.monotonic()
            return servers

    def status(self) -> list[dict]:
        out = []
        for name, srv in self._load_servers().items():
            try:
                tools = srv.list_tools()
            except Exception:  # noqa: BLE001
                tools = []
            out.append({"name": name, "transport": srv.transport,
                        "domains": sorted(srv.domains),
                        # tools/list 成功返回即在线（空清单 server 罕见，v1 从简）
                        "online": bool(tools),
                        "tools": tools})
        return out

    def tool_specs(self) -> list[dict]:
        """LLM 工具面：mcp__<server>__<tool> 规格列表（OpenAPI function 形状）。"""
        specs = []
        for name, srv in self._load_servers().items():
            try:
                tools = srv.list_tools()
            except Exception:  # noqa: BLE001
                continue
            for t in tools:
                specs.append({
                    "name": f"mcp__{name}__{t['name']}",
                    "description": (
                        f"MCP[{name}] {t['description']}".strip()
                        or f"MCP server {name} 的工具 {t['name']}"),
                    "input_schema": {
                        "type": "object",
                        "properties": (t.get("input_schema") or {}).get("properties") or {},
                    },
                })
        return specs

    def call(self, server: str, tool: str, args: dict) -> str:
        srv = self._load_servers().get(server)
        if srv is None:
            return f"[错误] MCP 调用失败：server '{server}' 未启用或域名不匹配"
        try:
            return srv.call(tool, args)
        except Exception as e:  # noqa: BLE001 —— 工具崩溃回文本，不断轮
            return f"[错误] MCP 调用异常: {e}"
