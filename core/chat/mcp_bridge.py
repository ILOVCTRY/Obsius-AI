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

    def __init__(self, command: str, args: list[str],
                 env_extra: dict[str, str] | None = None,
                 cwd: str | Path | None = None):
        self._cmd = [command, *args]
        self._env_extra = dict(env_extra or {})
        self._cwd = str(cwd) if cwd else None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}  # id -> {"event","resp"}
        self._next_id = 0
        self._ready = False   # MCP 握手是否已完成（进程重启后复位）

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._ready = False   # 新进程：握手需重做
        try:
            env = None
            if self._env_extra:
                import os
                env = {**os.environ, **self._env_extra}
            self._proc = subprocess.Popen(
                self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", env=env, cwd=self._cwd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            self._proc = None
            return False
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return True

    def is_alive(self) -> bool:
        """连接是否可用：未起过（懒启动，待用）或进程运行中 → True；已退出 → False。"""
        return self._proc is None or self._proc.poll() is None

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

    def _write_raw(self, body: dict) -> bool:
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(body) + "\n")
            self._proc.stdin.flush()
            return True
        except (OSError, AssertionError):
            return False

    def _ensure_ready(self) -> bool:
        """确保进程已拉起并完成 MCP 握手（initialize → initialized）。

        2026-10-01：此前缺握手，@playwright/mcp 的 tools/call 会被拒（tools/list
        却可能成功），表现为「server 离线或协议错误」。每次进程重启后重做一次。"""
        if not self._ensure():
            return False
        if self._ready:
            return True
        with self._lock:
            if self._ready:
                return True
            if self._proc is None or self._proc.poll() is not None:
                return False
            self._next_id += 1
            mid = self._next_id
            waiter: dict[str, Any] = {"event": threading.Event(), "resp": None}
            self._pending[mid] = waiter
            if not self._write_raw({
                    "jsonrpc": "2.0", "method": "initialize", "id": mid,
                    "params": {"protocolVersion": MCP_PROTOCOL_VERSION,
                               "capabilities": {},
                               "clientInfo": {"name": "cyberstrike-pro-chat",
                                              "version": "0.1"}}}):
                self._pending.pop(mid, None)
                return False
        if not waiter["event"].wait(_PROBE_TIMEOUT):
            self._pending.pop(mid, None)
            return False
        msg = waiter.get("resp")
        if not isinstance(msg, dict) or msg.get("error") is not None:
            return False
        if not self._write_raw({"jsonrpc": "2.0",
                                "method": "notifications/initialized",
                                "params": {}}):
            return False
        self._ready = True
        return True

    def _rpc(self, method: str, params: dict | None = None,
             timeout: float | None = None):
        if not self._ensure_ready():
            return None
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            waiter: dict[str, Any] = {"event": threading.Event(), "resp": None}
            self._pending[mid] = waiter
            body = {"jsonrpc": "2.0", "method": method,
                    "params": params or {}, "id": mid}
            if not self._write_raw(body):
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

    def __init__(self, name: str, cfg: dict,
                 env_extra: dict[str, str] | None = None,
                 cwd: str | Path | None = None):
        self.name = name
        self.transport = str(cfg.get("transport") or "stdio")
        self.domains = cfg.get("domains") or []
        cwd = cwd or cfg.get("_cwd")
        if self.transport in self.HTTP_TRANSPORTS:
            self._conn: Any = _HttpConn(str(cfg.get("url") or ""))
        else:
            self._conn = _StdioConn(
                str(cfg.get("command") or ""), list(cfg.get("args") or []),
                env_extra=env_extra, cwd=cwd)
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

    def is_alive(self) -> bool:
        """底层连接是否还活着（进程运行中 / HTTP 恒真）。"""
        checker = getattr(self._conn, "is_alive", None)
        return bool(checker()) if callable(checker) else True


class MCPBridge:
    """全部已启用 server 的桥：按项目域过滤，聚合工具清单与调用。

    **会话隔离（2026-10-01）**：名字在 ``SESSION_SCOPED_SERVERS`` 里的 stdio server
    （目前 = playwright）按 ``(server, session_id)`` **各起一个独立子进程**，用环境
    变量 ``PW_SESSION_ID`` 把会话标识传给启动器 —— 启动器据此派生独立 Chrome
    profile / CDP 端口，避免多个会话抢同一个持久化 profile（"Browser is already
    in use"）。同会话内多轮复用同一进程（登录态跨轮保留）。其余 server 仍按项目共享。
    ``MAX_BROWSER_SESSIONS`` 限制并发浏览器会话数，超限时回结构化错误引导。
    """

    # 需要按会话隔离的 stdio server 名（浏览器类，进程/资源重）
    SESSION_SCOPED_SERVERS = {"playwright"}
    # 并发浏览器会话上限（超出时调用回错误引导）；最多允许 10 个会话
    MAX_BROWSER_SESSIONS = 10
    # 传给会话级 server 进程的环境变量名
    SESSION_ENV_KEY = "PW_SESSION_ID"

    def __init__(self, config_path: str | Path, domains: list[str] | None = None,
                 *, project_id: str | None = None, browser_pool=None):
        self.config_path = Path(config_path)
        self._cwd = self.config_path.resolve().parent.parent
        self._domains = {d for d in (domains or []) if d}
        self.project_id = project_id
        self.browser_pool = browser_pool
        self._servers: dict[str, MCPServerRuntime] = {}
        self._loaded_at = 0.0
        # (server, session_id) -> 会话级 runtime
        self._session_servers: dict[tuple[str, str], MCPServerRuntime] = {}
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
                if name in self.SESSION_SCOPED_SERVERS:
                    # 会话级 server：此处只登记"该 server 启用"，runtime 延迟到
                    # 按会话首次调用时再建（那时才知道 session_id）。
                    servers[name] = _LazySessionServer(name, entry, cwd=self._cwd)
                    continue
                try:
                    runtime_cfg = {**entry, "_cwd": str(self._cwd)}
                    servers[name] = MCPServerRuntime(name, runtime_cfg)
                except (ValueError, OSError):
                    continue  # 非法端点等：跳过该 server
            self._servers = servers
            self._loaded_at = time.monotonic()
            return servers

    def _server_for_session(self, name: str, entry: dict,
                            session_id: str) -> MCPServerRuntime:
        """取（或建）会话级 server runtime；超并发上限时回错误。"""
        key = (name, session_id)
        with self._lock:
            srv = self._session_servers.get(key)
            if srv is not None:
                if srv.is_alive():
                    return srv
                # 进程已退出（空闲回收 / 崩溃）→ 丢弃重认领配额
                self._session_servers.pop(key, None)
            # 回收其它已死会话，释放并发配额
            dead = [k for k, v in self._session_servers.items() if not v.is_alive()]
            for k in dead:
                self._session_servers.pop(k, None)
            if len(self._session_servers) >= self.MAX_BROWSER_SESSIONS:
                raise _TooManyBrowserSessions(
                    f"并发浏览器会话已达上限 {self.MAX_BROWSER_SESSIONS}"
                    f"（当前会话 {session_id[:12]} 无法再开浏览器）")
            env = {self.SESSION_ENV_KEY: session_id}
            if self.project_id:
                env["PW_PROJECT_ID"] = self.project_id
            if name == "playwright" and self.browser_pool is not None and self.project_id:
                try:
                    env["PW_BROWSER_CDP_ENDPOINT"] = self.browser_pool.prepare_embedded(
                        self.project_id)
                except Exception as exc:  # noqa: BLE001
                    raise OSError(f"项目内置浏览器启动失败: {exc}") from exc
            runtime_cfg = {**entry, "_cwd": str(self._cwd)}
            srv = MCPServerRuntime(name, runtime_cfg, env_extra=env)
            self._session_servers[key] = srv
            return srv

    def status(self) -> list[dict]:
        out = []
        for name, srv in self._load_servers().items():
            if isinstance(srv, _LazySessionServer):
                # 会话级 server 的探针只设置 PW_NO_CHROME，不启动真实浏览器；
                # 仍需拉取工具清单，否则前端只能显示“工具发现失败或为空”。
                try:
                    tools = srv.list_tools()
                except Exception:  # noqa: BLE001
                    tools = []
                out.append({"name": name, "transport": "stdio",
                            "domains": sorted(srv.domains),
                            "online": bool(tools), "tools": tools,
                            "session_scoped": True})
                continue
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
        """LLM 工具面：mcp__<server>__<tool> 规格列表（OpenAPI function 形状）。

        会话级 server 在装配工具面时（尚无 session_id 上下文）用其**模板配置**静态
        列出工具：直接读该 server 的工具清单开销大且需起进程，故会话级 server 的工具
        以「按需透传」方式提供 —— 这里仍尝试用其共享模板列举一次，失败则跳过。
        """
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
                    # 保留 required/items/enum 等完整 JSON Schema；只透传 properties
                    # 会让模型遗漏必填参数，也会破坏 MCP 工具的数组与枚举输入。
                    "input_schema": t.get("input_schema") or {
                        "type": "object", "properties": {}},
                })
        return specs

    def _entry_for(self, server: str) -> dict | None:
        """读回该 server 的原始配置条目（会话级 server 建进程时用）。"""
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        for entry in cfg.get("servers") or []:
            if isinstance(entry, dict) and str(entry.get("name") or "") == server:
                return entry
        return None

    def call(self, server: str, tool: str, args: dict,
             session_id: str | None = None) -> str:
        servers = self._load_servers()
        srv = servers.get(server)
        if srv is None:
            return f"[错误] MCP 调用失败：server '{server}' 未启用或域名不匹配"
        if isinstance(srv, _LazySessionServer):
            sid = (session_id or "").strip()
            if not sid:
                # 无会话上下文：退回模板进程（单实例），仍可工作但无隔离
                sid = "__default__"
            entry = self._entry_for(server) or {"name": server,
                                                "transport": "stdio",
                                                "command": srv.command,
                                                "args": srv.args}
            try:
                rt = self._server_for_session(server, entry, sid)
            except _TooManyBrowserSessions as e:
                return (f"[错误] {e}。请等某个会话的浏览器空闲回收"
                        f"（默认 1 小时无活动自动关闭），或减少并发会话数后重试。")
            except (ValueError, OSError) as e:
                return f"[错误] MCP 调用失败：无法启动 {server} 会话进程: {e}"
        else:
            rt = srv
        try:
            return rt.call(tool, args)
        except Exception as e:  # noqa: BLE001 —— 工具崩溃回文本，不断轮
            return f"[错误] MCP 调用异常: {e}"


class _TooManyBrowserSessions(RuntimeError):
    """并发浏览器会话数超限（MCPBridge.MAX_BROWSER_SESSIONS）。"""


class _LazySessionServer:
    """会话级 server 的占位：登记启用状态 + 静态配置，不建进程。

    ``list_tools`` 用一份**临时**共享 runtime 探一次工具面（供 LLM 工具面装配），
    真正 ``call`` 时再按 session_id 建独立进程。探针 runtime 失败即回空清单。
    """

    def __init__(self, name: str, entry: dict,
                 *, cwd: str | Path | None = None):
        self.name = name
        self.transport = str(entry.get("transport") or "stdio")
        self.domains = entry.get("domains") or []
        self.command = str(entry.get("command") or "")
        self.args = list(entry.get("args") or [])
        self.cwd = str(cwd) if cwd else None
        self._probe: MCPServerRuntime | None = None
        self._probe_lock = threading.Lock()

    def list_tools(self, *, refresh: bool = False) -> list[dict]:
        with self._probe_lock:
            if self._probe is None:
                try:
                    # PW_NO_CHROME：探针进程只列工具清单，不起浏览器
                    self._probe = MCPServerRuntime(
                        self.name, {"transport": self.transport,
                                    "command": self.command, "args": self.args,
                                    "_cwd": self.cwd},
                        env_extra={"PW_NO_CHROME": "1"})
                except (ValueError, OSError):
                    return []
            try:
                return self._probe.list_tools(refresh=refresh)
            except Exception:  # noqa: BLE001
                return []
