"""统一反编译抽象（DESIGN.md §9 方案 C：headless 缓存为基 + MCP 实时增强）。

- headless 路线（IDA idat 优先 → Ghidra 兜底）：首次接触样本全量导出，按 sha256
  缓存（cache_dir/<sha>.json，export_version 当前 3：+ strings；<3 读时即删重导）。
  这是三层数据里的客观全量层，函数列表/字符串/xref 都以它为准。
- MCP 实时桥（P2，人在回路）：IDA 进程内插件只操作 GUI 当前打开的库，**无 binary
  维度**，只做三件事——writeback 实时写（在线优先，无锁问题）、缓存缺席单函数
  decompile 实时取（source=mcp）、xref func_profile 降级；绝不替换 headless 缓存。
  断了一切降级回 headless，全部方法 best-effort 不抛死。
- IDA 双向写回：writeback() 在线走 MCP、离线经 apply_names.py 写现有 .i64
  （锁文件=GUI 占用，结构化 locked 绝不强写）；refresh_db_cache() 库内重导 +
  diff_pulled_names() 回拉 GUI 改名。Agent 会话工厂默认不装 MCP（无人值守不赌当前库）。
- 全部不可用 → 返回安装引导文本（Agent 可退回纯静态分析）。

runner(args_list) -> (rc, stdout, stderr) 可注入：生产经执行网关（审计免费），
测试用假 runner。MCP transport(url, body, timeout, headers) -> (status, json, headers)
可注入：测试不触网。
"""

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from core.runtime.backends import NO_WINDOW_FLAGS

log = logging.getLogger(__name__)

EXPORT_VERSION = 3

# headless 大样本自动分析可能很久（自动分析+全量反编译可能数小时）。默认放开到
# 3 小时；可用 config/decompiler.json 的 headless_timeout（秒）覆盖（0/负值忽略）。
# 历史上硬编码 900s——大样本必然超时，逼人回 IDA GUI 再 MCP 回拉（P1 治本点）。
HEADLESS_TIMEOUT = 3 * 3600

# 本机反编译设置覆盖层（gitignore，仿 config/mcp.json）：headless_timeout 等
DECOMPILER_CONFIG_PATH = Path("config/decompiler.json")

# 大样本阈值（2026-09-29 用户口径 >20MB，与 app.py SAMPLE_LARGE_BYTES 同口径）：
# ≥阈值走 Ghidra 多进程并行分片主产（P2），普通样本维持 IDA 优先（保 IDA 质量与存量复用）。
LARGE_SAMPLE_BYTES = 20 * 1024 * 1024

# Ghidra 并行分片 worker 绝对上限（内存保护）：每 worker 一个解编译器进程、吃内存，
# 无脑按满核起会 OOM/swap。实际取值再夹 [1, GHIDRA_MAX_WORKERS]。
GHIDRA_MAX_WORKERS = 16


def load_decompiler_config() -> dict:
    """读 config/decompiler.json；缺失/损坏给空配置（调用方回默认值，绝不炸）。"""
    try:
        if DECOMPILER_CONFIG_PATH.is_file():
            data = json.loads(DECOMPILER_CONFIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def resolve_headless_timeout() -> float:
    """headless 命令超时（秒）：config/decompiler.json 的 headless_timeout 覆盖，
    非法/缺失回 HEADLESS_TIMEOUT 默认（大样本默认数小时，不轻易腰斩分析）。"""
    raw = load_decompiler_config().get("headless_timeout")
    try:
        val = float(raw)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return float(HEADLESS_TIMEOUT)


def resolve_large_sample_bytes() -> int:
    """大样本阈值（字节）：config/decompiler.json 的 large_sample_bytes 覆盖，
    非法/缺失/≤0 回 LARGE_SAMPLE_BYTES 默认（对齐 app.py 20MB 用户口径）。"""
    raw = load_decompiler_config().get("large_sample_bytes")
    try:
        val = int(raw)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return int(LARGE_SAMPLE_BYTES)


def is_large_sample(binary: str | Path) -> bool:
    """样本是否达大样本阈值（选路用）。不可读（不存在/无权限）视为非大样本，绝不抛。"""
    try:
        return Path(binary).stat().st_size >= resolve_large_sample_bytes()
    except OSError:
        return False


# ---------- 反编译引擎选择（2026-10-01：工作台 IDA / Ghidra 双模式） ----------
# 语义：引擎是「偏好」不是「排他」——所选引擎优先生效，不可用时回退另一引擎，
# 保证总能出结果。模式与缓存产出引擎不一致由前端提示用户手动重跑（不自动重跑）。
ENGINES = ("ida", "ghidra")


def normalize_engine(value: object) -> str:
    """任意输入（None/''/未知）一律规整为 'ida' | 'ghidra'；缺省 ida（现状）。"""
    return "ghidra" if str(value or "").strip().lower().startswith("ghidra") else "ida"


def engine_of_backend(name: str) -> str:
    """后端名 → 引擎名（ida-headless/mcp → ida；ghidra-headless → ghidra）。"""
    return "ghidra" if "ghidra" in (name or "") else "ida"


def resolve_ghidra_workers() -> int:
    """Ghidra 并行分片 worker 数：config/decompiler.json 的 ghidra_workers 覆盖，
    否则 max(1, CPU-1)；一律夹在 [1, GHIDRA_MAX_WORKERS]（内存保护）。"""
    raw = load_decompiler_config().get("ghidra_workers")
    workers = None
    try:
        val = int(raw)
        if val > 0:
            workers = val
    except (TypeError, ValueError):
        pass
    if workers is None:
        workers = max(1, (os.cpu_count() or 1) - 1)
    return max(1, min(workers, GHIDRA_MAX_WORKERS))


_DEFAULTS_ROOT = Path(__file__).resolve().parents[2]
_IDA_SCRIPT = _DEFAULTS_ROOT / "tools" / "decompiler" / "ida" / "scripts" / "export_funcs.py"
_IDA_APPLY_SCRIPT = _DEFAULTS_ROOT / "tools" / "decompiler" / "ida" / "scripts" / "apply_names.py"
_GHIDRA_SCRIPT = _DEFAULTS_ROOT / "tools" / "decompiler" / "ghidra" / "scripts" / "export_funcs.py"

# IDA GUI 正开着库时的锁文件后缀（任一存在即绝不能 headless 强写）
DB_LOCK_EXTS = (".id0", ".id1", ".id2", ".nam", ".til")

# pull-names 时跳过的 IDA 自动名（人没在 GUI 里起过名的不回拉，避免噪声覆盖 kb）
# 整名匹配自动名（首尾锚定，防 my_sub_401000 这类自定义名误判）
_AUTO_NAME_RULES = (
    re.compile(r"^sub_[0-9a-fA-F]+$"),
    re.compile(r"^nullsub(_[0-9a-fA-F]+)?$"),
    re.compile(r"^unk_[0-9a-fA-F]+$"),
)


def is_auto_name(name: str | None) -> bool:
    if not name:
        return True
    return any(rx.match(name) for rx in _AUTO_NAME_RULES)


def is_partial_export(data: dict | None) -> bool:
    """导出是否被协作式停止（partial）：meta.partial / 顶层 partial（P3）。

    partial 缓存仅供工作台「已导出部分」展示，绝不当完整缓存复用、绝不发布全局。
    """
    if not isinstance(data, dict):
        return False
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    return bool(data.get("partial") or data.get("stopped")
                or meta.get("partial") or meta.get("stopped"))

DECOMPILE_GUIDANCE = (
    "[反编译器不可用] 检测结果：IDA(idat64) 未安装、Ghidra(analyzeHeadless) 未安装、MCP 离线。"
    "安装引导：IDA → 确认 idat64 在 PATH；Ghidra → winget install Ghidra.Ghidra 或 scoop install ghidra；"
    "MCP → 启动本地 IDA/Ghidra MCP 服务。"
    "在此之前请退回纯静态分析（objdump / 手撕汇编），结论照常落 func_kb。"
)


# IDA 常见安装根（PATH 未配置时兜底；IDA 8/9 默认装 Program Files，自定义盘常见 Tools/IDA*）
_IDA_INSTALL_GLOBS = (
    r"C:\Program Files\IDA*\{exe}",
    r"C:\Program Files (x86)\IDA*\{exe}",
    r"%LOCALAPPDATA%\Programs\IDA*\{exe}",
    r"D:\Tools\IDA*\{exe}", r"D:\IDA*\{exe}",
    r"E:\Tools\IDA*\{exe}", r"E:\IDA*\{exe}",
)


def resolve_ida_headless() -> str | None:
    """探测 headless IDA：8.x 是 idat64，9.x 统一为 idat。

    顺序：工具链注册表四来源（core/toolchain，M1 起；config/tools.json 指认 →
    tools/ 规范位 → fallback glob → PATH）→ 原生兜底（PATH → 常见安装目录 glob）。
    找不到返回 None。
    """
    try:  # registry 命中直接返回（fallback glob 与 _IDA_INSTALL_GLOBS 同源，registry 优先）
        from core.toolchain import load_registry, load_tool_overrides, resolve_tool
        entry = load_registry().get("ida")
        if entry:
            row = resolve_tool("ida", entry, overrides=load_tool_overrides())
            if row["status"] == "ready":
                return row["path"]
    except Exception:  # noqa: BLE001 —— registry 故障回退原生探测，不阻断
        pass
    for cand in ("idat64", "idat"):
        found = shutil.which(cand)
        if found:
            return found
    import glob
    import os
    for exe in ("idat64.exe", "idat.exe"):
        for pat in _IDA_INSTALL_GLOBS:
            hits = sorted(glob.glob(os.path.expandvars(pat.format(exe=exe))))
            if hits:
                return hits[0]
    return None


def resolve_ghidra_headless() -> str | None:
    """探测 headless Ghidra（analyzeHeadless）：工具链注册表四来源
    （core/toolchain，M1 起）→ PATH 兜底。找不到返回 None。
    解「Ghidra 装在自定义目录、PATH 未配置导致后端永不可用」错位。"""
    try:
        from core.toolchain import load_registry, load_tool_overrides, resolve_tool
        entry = load_registry().get("ghidra")
        if entry:
            row = resolve_tool("ghidra", entry, overrides=load_tool_overrides())
            if row["status"] == "ready":
                return row["path"]
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("analyzeHeadless")


def resolve_ida_gui(headless_path: str | None = None) -> str | None:
    """GUI IDA（打开 .i64/.idb 给人深度分析）：优先与 headless 同目录的 ida64/ida。"""
    headless_path = headless_path or resolve_ida_headless()
    if headless_path:
        d = Path(headless_path).parent
        exe = "ida64.exe" if Path(headless_path).name.lower().startswith("idat64") else "ida.exe"
        cand = d / exe
        if cand.exists():
            return str(cand)
    return shutil.which("ida64") or shutil.which("ida")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_runner(args: list[str]) -> tuple[int, str, str]:
    # 每次现取：config/decompiler.json 改了超时无需重启（大样本默认数小时）
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=resolve_headless_timeout(),
                              encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW_FLAGS)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return -1, "", str(e)


def gateway_runner(gateway, *, project_id: str, session_id: str, author: str,
                   timeout: float | None = None, workspace=None):
    """把执行网关包成 runner：反编译命令也走审计（threat_class=trusted——
    Ghidra/IDA 是可信工具解析样本文件，不执行样本）。

    workspace 传入时走工作区隔离（cwd=scratch + TEMP 重定向；§7 2026-09-17）。

    Windows 宿主经 `powershell -Command <cmd>` 执行：PowerShell 会把
    ``-S"script out"`` 的引号挪到整个参数外面（实测 RAW 变成 "-Sscript out"），
    IDA 只认原位置引号。``--%`` 停止 PowerShell 解析后参数原样下传
    （对 .bat 同样有效，Ghidra 的 analyzeHeadless 正是批处理）。
    注意：--% 后路径里的 % 会被 cmd 风格展开，项目/样本路径按约定不含 %。
    """
    def run(args: list[str]) -> tuple[int, str, str]:
        if platform.system() == "Windows" and len(args) > 1:
            cmd = f"{args[0]} --% {' '.join(args[1:])}"
        else:
            cmd = " ".join(args)
        kwargs = {"timeout": timeout} if timeout else {}
        r = gateway.run(cmd, "host", threat_class="trusted",
                        project_id=project_id, session_id=session_id, author=author,
                        workspace=workspace, **kwargs)
        return r.exit_code, r.stdout, r.stderr
    return run


# ---------------- MCP 实时桥（IDA Pro 插件，streamable-http） ----------------
#
# 人在回路通道：vendor 插件（tools/mcp/ida-pro-mcp，mrexodia/ida-pro-mcp）在
# IDA 进程内起 HTTP server，操作的是 IDA GUI 当前打开的库——没有 binary 维度，
# 不能用来全量替换 headless 缓存，只做实时点查/实时写。红线：只连 127.0.0.1，
# 不做 stdio 拉起；断了一切降级回 headless，绝不报 500。

MCP_DEFAULT_ENDPOINT = "http://127.0.0.1:13337/mcp"
MCP_PROBE_TIMEOUT = 1.5    # 握手/探活短超时：离线不拖慢 overview 的 4s 轮询
MCP_CALL_TIMEOUT = 30.0
MCP_HEALTH_TTL = 3.0       # 探活结果缓存：灯近乎实时，又不每个请求都握手
_MCP_HTTP_TRANSPORTS = ("streamable-http", "http", "http-stream")
_MCP_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback_url(url: str) -> bool:
    """红线：MCP 只允许连本机 loopback（IDA 进程内插件）。"""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and (p.hostname or "").lower() in _MCP_LOOPBACK_HOSTS


def select_mcp_endpoint(config: dict | None) -> str:
    """config/mcp.json 选逆向域（domains 含 reverse，兼容旧标 binary）的 http server；
    无配置/无合适条目/条目非 loopback 时回默认本机端点（零配置：装插件 Ctrl-Alt-M 即用）。"""
    for s in (config or {}).get("servers") or []:
        if not isinstance(s, dict) or not s.get("enabled", True):
            continue
        if s.get("transport", "streamable-http") not in _MCP_HTTP_TRANSPORTS:
            continue
        domains = s.get("domains") or []
        if "reverse" not in domains and "binary" not in domains:
            continue
        url = (s.get("url") or "").strip()
        if url and _is_loopback_url(url):
            return url
    return MCP_DEFAULT_ENDPOINT


def _parse_mcp_body(raw: str):
    """响应体兼容两种形态：单条 JSON-RPC JSON，或 SSE（多行 event:/data:）。
    取最后一条 data: 的 JSON（SSE 下 result 在末帧）；202 'Accepted' 纯文本 → None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    payload = None
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk:
            continue
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
    return payload


def _header(headers: dict, name: str) -> str | None:
    lname = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == lname:
            return v
    return None


class MCPBackend:
    """IDA MCP 插件客户端（JSON-RPC over streamable-http），全程 best-effort。

    会话生命周期：initialize（不带 session 头）→ 从**响应头**取 Mcp-Session-Id 缓存
    → 发 notifications/initialized → 后续 tools/call 回带该头；4xx/断线自动重握一次。
    探活懒触发 + 短超时 + TTL（available()）。真机工具名见 tools/mcp/CLAUDE.md。
    可注入 transport(url, body|None, timeout, headers=None) -> (status, json|None, headers)。
    """

    name = "mcp"

    # 真机工具名（vendor api_*.py 的 @tool 函数名；改名同步 tools/mcp/CLAUDE.md 与测试）
    T_DECOMPILE = "decompile"
    T_LIST_FUNCS = "list_funcs"
    T_COUNT_FUNCS = "count_funcs"
    T_RENAME = "rename"
    T_SET_COMMENTS = "set_comments"
    T_FUNC_PROFILE = "func_profile"
    T_ANALYZE_BATCH = "analyze_batch"
    T_ENTITY_QUERY = "entity_query"
    T_HEALTH = "server_health"
    T_IDB_SAVE = "idb_save"

    def __init__(self, endpoint: str = MCP_DEFAULT_ENDPOINT, *,
                 timeout: float = MCP_CALL_TIMEOUT,
                 probe_timeout: float = MCP_PROBE_TIMEOUT,
                 health_ttl: float = MCP_HEALTH_TTL,
                 transport=None):
        if not _is_loopback_url(endpoint):
            raise ValueError(f"MCP 只允许连本机 loopback 端点: {endpoint}")
        self.endpoint = endpoint
        self.timeout = timeout
        self.probe_timeout = probe_timeout
        self.health_ttl = health_ttl
        self._transport = transport or self._http_transport
        self._next_id = 0
        self._session_id: str | None = None
        self._server_tools: set[str] | None = None
        self._health: bool | None = None
        self._health_at = 0.0
        self._lock = threading.Lock()  # overview 轮询与用户操作并发：握手/探活串行

    @staticmethod
    def _http_transport(url: str, body: dict | None, timeout: float,
                        headers: dict | None = None):
        import urllib.error
        import urllib.request
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        hdrs.update(headers or {})
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, _parse_mcp_body(raw), dict(resp.headers.items())
        except urllib.error.HTTPError as e:  # 4xx 也要回 status/headers（session 失效据此重握）
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, _parse_mcp_body(raw), dict(e.headers.items())
        except Exception:  # noqa: BLE001 —— 拒绝/超时/重置一律视为离线
            return 0, None, {}

    # ---- 会话 / JSON-RPC ----

    def _post(self, method: str, params: dict | None, *, notification: bool,
              timeout: float):
        """一次 HTTP 往返（调用方持锁，session 头按当前缓存带）。"""
        headers = {"Mcp-Session-Id": self._session_id} if self._session_id else {}
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            self._next_id += 1
            body["id"] = self._next_id
        return self._transport(self.endpoint, body, timeout, headers)

    def _handshake(self) -> bool:
        """initialize + notifications/initialized（调用方持锁）。成功即缓存 session。"""
        self._session_id, self._server_tools = None, None
        status, data, headers = self._post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "cyberstrike-pro", "version": "0.2"},
        }, notification=False, timeout=self.probe_timeout)
        if status == 0 or not (200 <= status < 300) \
                or not isinstance(data, dict) or "result" not in data:
            self._health, self._health_at = False, time.monotonic()
            return False
        self._session_id = _header(headers, "Mcp-Session-Id")
        self._health, self._health_at = True, time.monotonic()
        # initialized 是通知（无 id）→ 202 Accepted 空体；失败无所谓，session 已注册
        self._post("notifications/initialized", {},
                   notification=True, timeout=self.probe_timeout)
        return True

    def available(self) -> bool:
        """懒探活，TTL 缓存。initialize 成功就是「服务活着且 IDA 库开着」的最强证据。"""
        with self._lock:
            if self._health is not None \
                    and time.monotonic() - self._health_at < self.health_ttl:
                return self._health
            return self._handshake()

    def _rpc(self, method: str, params: dict | None = None, *,
             timeout: float | None = None):
        """JSON-RPC 请求；session 失效（4xx）自动清 session 重握一次（只重试一轮）。
        离线/协议错误返回 None。重握走循环而非递归：每轮重新进锁，避免锁重入。"""
        data = None
        for attempt in (1, 2):
            with self._lock:
                if self._session_id is None and not self._handshake():
                    return None
                status, data, _h = self._post(method, params, notification=False,
                                              timeout=timeout or self.timeout)
                if status == 0:
                    self._health, self._health_at = False, time.monotonic()
                    return None
                if status in (400, 404, 405, 409, 410) and attempt == 1:
                    # 旧 session 失效：清掉后下一轮重新握手（锁已随 with 释放）
                    self._session_id = None
                    self._server_tools = None
                    continue
                break
        if not isinstance(data, dict) or data.get("error") is not None:
            return None
        return data.get("result")

    # ---- tools/call ----

    @staticmethod
    def _payload(result):
        """真机 zeromcp 把工具返回值 json.dumps 进 content[].text：解回结构化对象；
        非 JSON 文本原样返回；isError/空 content → None。"""
        if not isinstance(result, dict) or result.get("isError"):
            return None
        texts = [c.get("text", "") for c in result.get("content") or []
                 if isinstance(c, dict) and c.get("type", "text") == "text"]
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def _tools_cached(self) -> set[str] | None:
        """tools/list 动态发现（best-effort：探不到不阻塞，用内置真名直接调）。"""
        if self._server_tools is not None:
            return self._server_tools
        result = self._rpc("tools/list", {})
        if isinstance(result, dict):
            self._server_tools = {
                t.get("name") for t in result.get("tools") or []
                if isinstance(t, dict) and t.get("name")}
        return self._server_tools

    def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None):
        tools = self._tools_cached()
        if tools is not None and name not in tools:
            return None
        result = self._rpc("tools/call", {"name": name, "arguments": arguments},
                           timeout=timeout)
        return self._payload(result) if result is not None else None

    # ---- 工作台实时通道（地址入参 int/hex 皆收；返回 None 即降级） ----

    @staticmethod
    def _addr_arg(addr: int | str) -> str:
        return hex(addr) if isinstance(addr, int) else str(addr)

    def decompile_at(self, addr: int | str) -> str | None:
        """实时取当前 IDA 库单函数伪码（decompile 也吃函数名）。{addr,code,error?}。"""
        payload = self.call_tool(self.T_DECOMPILE, {"addr": self._addr_arg(addr)},
                                 timeout=60.0)
        if isinstance(payload, dict) and payload.get("code") and not payload.get("error"):
            return payload["code"]
        return None

    def count_funcs(self) -> int | None:
        """当前库函数总数（毫秒级，2026-09-30 进度分母用）。
        工具缺失（旧插件）/离线返回 None——调用方退化为无分母进度。"""
        res = self.call_tool(self.T_COUNT_FUNCS, {})
        if isinstance(res, dict) and isinstance(res.get("count"), int) \
                and res["count"] >= 0:
            return res["count"]
        return None

    def xref_profile(self, addr: int | str) -> dict | None:
        """func_profile(include_lists) 降级取 callers/callees，转 build_xrefs 同构。"""
        payload = self.call_tool(self.T_FUNC_PROFILE, {
            "queries": [{"query": self._addr_arg(addr),
                         "include_lists": True, "max_items": 100}]})
        if not isinstance(payload, list) or not payload \
                or not isinstance(payload[0], dict) or payload[0].get("error"):
            return None
        rows = payload[0].get("data") or []
        prof = rows[0] if rows and isinstance(rows[0], dict) else None
        if prof is None or prof.get("error"):
            return None

        def _pair(c):
            return {"address": c.get("addr"), "name": c.get("name")}

        return {
            "address": prof.get("addr") or self._addr_arg(addr),
            "name": prof.get("name"),
            "callers": [_pair(c) for c in prof.get("callers") or [] if isinstance(c, dict)],
            "callees": [_pair(c) for c in prof.get("callees") or [] if isinstance(c, dict)],
            "source": "mcp",
        }

    def func_detail(self, addr: int | str) -> dict | None:
        """analyze_batch 单查询一次拿齐 伪码+反汇编+callers/callees（40万样本按需详情用）。

        只取需要段（关 strings/constants/basic_blocks/proto），单函数体积可控；
        返回 {address,name,size,pseudocode,disasm_lines,disasm_truncated,callers,callees}
        （callers/callees 为 {addr,name} 对，与 xref_profile 同构）。工具缺失/失败 None。
        """
        payload = self.call_tool(self.T_ANALYZE_BATCH, {"queries": [{
            "query": self._addr_arg(addr),
            "include_decompile": True,
            "include_disasm": True,
            "max_disasm_insns": DETAIL_DISASM_MAX_LINES,
            "include_callers": True,
            "max_callers": DETAIL_MAX_REFS,
            "include_callees": True,
            "max_callees": DETAIL_MAX_REFS,
            "include_strings": False,
            "include_constants": False,
            "include_basic_blocks": False,
            "include_proto": False,
        }]}, timeout=60.0)
        if not isinstance(payload, list) or not payload \
                or not isinstance(payload[0], dict) or payload[0].get("error"):
            return None
        row = payload[0]
        analysis = row.get("analysis") or {}
        disasm = analysis.get("disasm") or {}

        def _pair(c):
            return {"address": c.get("addr"), "name": c.get("name")}

        return {
            "address": row.get("addr") or self._addr_arg(addr),
            "name": row.get("name"),
            "size": int(str(analysis.get("size") or "0x0"), 16),
            "pseudocode": analysis.get("decompile"),
            "disasm_lines": disasm.get("lines") or [],
            "disasm_truncated": bool(disasm.get("truncated")),
            "callers": [_pair(c) for c in (analysis.get("callers") or []) if isinstance(c, dict)],
            "callees": [_pair(c) for c in (analysis.get("callees") or []) if isinstance(c, dict)],
        }

    def writeback_items(self, items: list[dict]) -> dict | None:
        """实时写当前 IDA GUI 库：rename batch + set_comments（无锁问题）。

        离线/传输级失败返回 None（调用方降级 headless apply_by_sha）；
        工具逐条成败进 results，整体 status=ok（与 headless 通道同构，applied=成功条数）。
        注：只改 GUI 内存库，不主动 idb_save——库存活由用户掌控。
        """
        if not self.available():
            return None
        funcs, comments = [], []
        for it in items:
            ea = int(str(it["address"]), 0)
            if it.get("name"):
                funcs.append({"addr": hex(ea), "name": it["name"]})
            if it.get("comment"):
                comments.append({"addr": hex(ea), "comment": it["comment"]})
        rename_rows: list = []
        if funcs:
            # 真机 rename 签名是 rename(batch: RenameBatch)：func/allow_overwrite 必须在 batch 内
            res = self.call_tool(self.T_RENAME,
                                 {"batch": {"func": funcs, "allow_overwrite": True}})
            if not isinstance(res, dict):
                return None
            rename_rows = res.get("func") or []
            if not rename_rows:  # 传输正常但没有逐条结果：保守判失败，交回 headless
                return None
        comment_rows: list = []
        if comments:
            res = self.call_tool(self.T_SET_COMMENTS, {"items": comments})
            if not isinstance(res, list):
                return None
            comment_rows = res
        applied = sum(1 for r in (*rename_rows, *comment_rows)
                      if isinstance(r, dict) and r.get("ok"))
        return {"status": "ok", "channel": "mcp", "applied": applied,
                "results": {"rename": rename_rows, "comments": comment_rows}}


# ---------------- Headless 后端 ----------------

class GhidraHeadlessBackend:
    """analyzeHeadless + Jython postScript 全量导出。

    临时工程每次调用一个独立子目录（固定名 cs-probe 会并发互撞），跑完即焚。
    """

    name = "ghidra-headless"

    def __init__(self, *, headless_cmd: str = "analyzeHeadless",
                 script_path: str | Path | None = None,
                 tmp_project_dir: str | Path | None = None,
                 runner=None, available: bool | None = None,
                 workers: int | None = None):
        self.headless_cmd = shutil.which(headless_cmd) or headless_cmd
        self.script_path = Path(script_path) if script_path else _GHIDRA_SCRIPT
        self.tmp_project_dir = Path(tmp_project_dir or ".ghidra-tmp")
        self.runner = runner or _default_runner
        self._available = available  # 测试注入；None = 实际探测
        # P2 并行分片：postScript 第二参=worker 数（脚本内按函数表分片反编译）；
        # 1（默认）走串行老路径，args 形状不变（兼容既有假 runner/断言）
        try:
            self.workers = max(1, int(workers)) if workers else 1
        except (TypeError, ValueError):
            self.workers = 1

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        return shutil.which(self.headless_cmd) is not None or Path(self.headless_cmd).exists()

    def export(self, binary: str, out_json: Path, *, progress: dict | None = None,
               stop_event: threading.Event | None = None,
               want_disasm: bool = False) -> dict:
        proj_dir = self.tmp_project_dir / f"cs-{uuid.uuid4().hex[:12]}"
        proj_dir.mkdir(parents=True, exist_ok=True)
        proj_name = "csproj"
        # P3 协作式停止 + 进度：进度文件（脚本写）与停止标志文件（父进程写）落 out_json
        # 同目录——临时工程跑完即焚，控制文件不能放工程内。
        progress_path = out_json.with_name(out_json.name + ".progress")
        stop_path = out_json.with_name(out_json.name + ".stop")
        want_ctl = progress is not None or stop_event is not None
        for p in (progress_path, stop_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        args = [
            self.headless_cmd, str(proj_dir), proj_name,
            "-import", str(binary),
            # 2026-10-01 工作台可中断：-noanalysis 跳过内置分析，改由 postScript 内
            # AutoAnalysisManager + 自定义 TaskMonitor 驱动 → 分析阶段可中断 + 报进度。
            "-noanalysis",
            "-scriptPath", str(self.script_path.parent),
            "-postScript", self.script_path.name, str(out_json),
            "-deleteProject",
        ]
        # postScript 位置实参（-deleteProject 前，固定 5 位保证形状稳定）：
        # [worker 数, 进度文件, 停止文件, 自驱分析(1), 反汇编内嵌(1)]。无控制/无内嵌时
        # 对应位填空串；分析开关恒 "1"——-noanalysis 后必须由脚本补跑分析。
        script_args = [
            str(self.workers),
            str(progress_path) if want_ctl else "",
            str(stop_path) if want_ctl else "",
            "1",
            "1" if want_disasm else "",
        ]
        idx = args.index("-deleteProject")
        args[idx:idx] = script_args
        monitor_done = threading.Event()
        monitor: threading.Thread | None = None
        if want_ctl:
            def _watch() -> None:
                # 轮询进度文件更新 job progress（引用共享）；stop_event 置位即写停止文件，
                # 脚本在函数边界自然停下（不杀进程，保留已导出部分）。
                while not monitor_done.is_set():
                    if stop_event is not None and stop_event.is_set():
                        try:
                            stop_path.write_text("stop", encoding="utf-8")
                        except OSError:
                            pass
                    if progress is not None and progress_path.is_file():
                        try:
                            d = json.loads(progress_path.read_text(encoding="utf-8"))
                            if isinstance(d, dict):
                                progress["done"] = int(d.get("done") or 0)
                                progress["total"] = int(d.get("total") or 0)
                                progress["phase"] = str(d.get("phase") or "")
                        except (OSError, ValueError):
                            pass
                    monitor_done.wait(1.0)
            monitor = threading.Thread(target=_watch, daemon=True)
            monitor.start()
        try:
            rc, out, err = self.runner(args)
            if rc != 0 or not out_json.is_file():
                raise RuntimeError(f"analyzeHeadless 失败 rc={rc}: {(err or out)[-400:]}")
            # 末次进度（监视线程可能未读到最后一帧）
            if progress is not None and progress_path.is_file():
                try:
                    d = json.loads(progress_path.read_text(encoding="utf-8"))
                    if isinstance(d, dict):
                        progress["done"] = int(d.get("done") or 0)
                        progress["total"] = int(d.get("total") or 0)
                except (OSError, ValueError):
                    pass
            stopped = False
            try:
                data = json.loads(out_json.read_text(encoding="utf-8"))
                stopped = is_partial_export(data)
            except (OSError, ValueError):
                pass
            return {"name": self.name, "db_path": None, "stopped": stopped}
        finally:
            monitor_done.set()
            if monitor is not None:
                monitor.join(timeout=3)
            for p in (progress_path, stop_path):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            shutil.rmtree(proj_dir, ignore_errors=True)

    # ---- 按需反汇编（2026-10-01 工作台 Ghidra 模式，大样本路径） ----

    def _persist_project_dir(self, binary: str) -> Path:
        """持久工程目录（按 sha）：首次导入+分析后保留，供后续 -process 复用，
        免每次重导入/重分析；落 tmp_project_dir（项目 artifacts，可清理）。"""
        return self.tmp_project_dir / f"ghidra-{sha256_file(binary)}"

    def disasm_on_demand(self, binary: str, addresses: list[int],
                         *, max_lines: int = 400) -> dict[int, list[str]]:
        """指定函数地址的按需反汇编（大样本 Ghidra 模式）：

        首次：analyzeHeadless -import + 自动分析（工程落盘，不 -deleteProject）；
        之后：analyzeHeadless -process -noanalysis 打开既有工程直接反汇编（免重分析）。
        disasm_funcs.py 把 {hex_addr: [lines]} 写到 out.json。失败抛 RuntimeError
        （上层降级为「无反汇编」，不阻断伪码/调用关系）。"""
        addrs = [int(a) for a in addresses]
        if not addrs:
            return {}
        proj_dir = self._persist_project_dir(binary)
        proj_dir.mkdir(parents=True, exist_ok=True)
        proj_name = "csproj"
        out_json = proj_dir / "disasm-out.json"
        try:
            out_json.unlink(missing_ok=True)
        except OSError:
            pass
        script = self.script_path.parent / "disasm_funcs.py"
        addr_arg = ",".join(hex(a) for a in addrs)
        first = not any(proj_dir.glob(f"{proj_name}.gpr"))
        args = [self.headless_cmd, str(proj_dir), proj_name]
        if first:
            args += ["-import", str(binary)]
        else:
            args += ["-process", "-noanalysis"]
        args += ["-scriptPath", str(script.parent),
                 "-postScript", script.name, str(out_json), addr_arg,
                 str(int(max_lines))]
        rc, out, err = self.runner(args)
        if rc != 0 or not out_json.is_file():
            raise RuntimeError(f"Ghidra 按需反汇编失败 rc={rc}: {(err or out)[-400:]}")
        try:
            raw = json.loads(out_json.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Ghidra 按需反汇编输出不可解析: {e}") from e
        out_map: dict[int, list[str]] = {}
        for k, v in (raw or {}).items():
            try:
                out_map[int(str(k), 16)] = [str(x) for x in (v or [])]
            except (TypeError, ValueError):
                continue
        return out_map


class IDAHeadlessBackend:
    """idat(-A -S) + IDAPython 全量导出（Windows 原生命令行版）。

    db_dir 指定时数据库写那里（默认在样本旁边）：样本目录是 untrusted
    只读语义，且 Windows 下 IDA 文件锁会妨碍项目删除。
    数据库扩展名由**目标位宽**决定（64 位 .i64 / 32 位 .idb），与 idat/idat64
    无关（实测 IDA 9.3 的 idat 跑 64 位 ELF 产出 .i64，且会忽略 -o 里写错的扩展），
    所以 -o 只给无扩展主干，产物按 .i64→.idb 顺序探测。
    """

    name = "ida-headless"

    def __init__(self, *, idat_cmd: str = "idat64",
                 script_path: str | Path | None = None,
                 apply_script_path: str | Path | None = None,
                 runner=None,
                 available: bool | None = None,
                 db_dir: str | Path | None = None,
                 db_exts: tuple[str, ...] = (".i64", ".idb")):
        self.idat_cmd = shutil.which(idat_cmd) or idat_cmd
        self.script_path = Path(script_path) if script_path else _IDA_SCRIPT
        self.apply_script_path = Path(apply_script_path) if apply_script_path else _IDA_APPLY_SCRIPT
        self.runner = runner or _default_runner
        self._available = available
        self.db_dir = Path(db_dir) if db_dir else None
        self.db_exts = db_exts

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        return shutil.which(self.idat_cmd) is not None or Path(self.idat_cmd).exists()

    def _db_stem(self, binary: str) -> Path:
        if self.db_dir:
            return self.db_dir / sha256_file(binary)
        return Path(binary).with_suffix("")

    def _find_db(self, stem: Path) -> Path | None:
        for ext in self.db_exts:
            p = stem.with_suffix(ext)
            if p.exists():
                return p
        return None

    def export(self, binary: str, out_json: Path, *, progress: dict | None = None,
               stop_event: threading.Event | None = None,
               want_disasm: bool = False) -> dict:
        # progress/stop_event 为 P3 大样本接口；IDA 路径目前仅在 Ghidra 后端消费
        # （IDA 大样本已降为精解/写回），此处接受并忽略，保持后端签名一致。
        # want_disasm：仅 Ghidra 后端消费（小样本内嵌反汇编）；IDA 模式反汇编走 MCP。
        out_json.parent.mkdir(parents=True, exist_ok=True)
        db_stem = self._db_stem(binary)
        db_stem.parent.mkdir(parents=True, exist_ok=True)
        # P1 直读存量：库已存在（此前分诊或 GUI 深度分析产出的 .i64/.idb）→ 绝不删库重建，
        # 直接库内重导（export_db 不带 -o）。IDA 的自动分析成果一次落库、长期复用，
        # 大样本免去每次重分诊的数小时全量重分析；只有全新样本才从 binary 建库。
        existing = self._find_db(db_stem)
        if existing is not None:
            if self.db_locked(db_stem):
                raise RuntimeError(
                    "ida-db-locked: IDA 库正被 GUI 占用，headless 无法重导；"
                    "请先在 IDA 中关闭该库，或改用 MCP 实时通道")
            self.export_db(existing, out_json)
            return {"name": self.name, "db_path": str(existing), "reused": True}
        # 全新分诊：清掉崩溃残留锁文件，避免 IDA 弹"数据库已存在"
        for ext in (*self.db_exts, ".id0", ".id1", ".id2", ".nam", ".til"):
            db_stem.with_suffix(ext).unlink(missing_ok=True)
        # -o 不带扩展，IDA 按目标位宽产出 .i64/.idb；
        # -S 参数带参需整体加引号（IDA 约定：-S"script args"），
        # Windows PowerShell 下靠 runner 的 --% 保住引号原位
        args = [
            self.idat_cmd, "-A",
            f'-S"{self.script_path} {out_json}"',
            f"-o{db_stem}",
            str(binary),
        ]
        rc, out, err = self.runner(args)
        if rc != 0 or not out_json.is_file():
            raise RuntimeError(f"idat 失败 rc={rc}: {(err or out)[-400:]}")
        db_path = self._find_db(db_stem)
        return {"name": self.name,
                "db_path": str(db_path) if db_path else None}

    # ---- 双向写回（对已存在的 .i64/.idb 操作，绝不删库重建） ----

    def db_stem_for(self, sha: str) -> Path | None:
        """分诊库按 sha256(样本)=<db_dir>/<sha> 落盘；未配 db_dir 无法按 sha 定位。"""
        return self.db_dir / sha if self.db_dir else None

    def db_locked(self, stem: Path) -> bool:
        """锁文件 .id0/.id1/.id2/.nam/.til 任一存在 = IDA GUI 正开着该库。"""
        return any(stem.with_suffix(ext).exists() for ext in DB_LOCK_EXTS)

    def export_db(self, db: Path, out_json: Path) -> None:
        """对**现有库**重跑导出脚本（不删库、不带 -o）：pull-names 时取 GUI 保存后的当前名。

        注意不能用 export()——它会先删 .i64 再从样本重建，GUI 里的人工命名随之丢失。
        """
        out_json.parent.mkdir(parents=True, exist_ok=True)
        args = [
            self.idat_cmd, "-A",
            f'-S"{self.script_path} {out_json}"',
            str(db),
        ]
        rc, out, err = self.runner(args)
        if rc != 0 or not out_json.is_file():
            raise RuntimeError(f"idat 库内重导失败 rc={rc}: {(err or out)[-400:]}")

    def apply_by_sha(self, sha: str, items: list[dict]) -> dict:
        """func_kb 命名/注释写回 <db_dir>/<sha>.i64。结构化状态，绝不强写：

        - no-db：尚未分诊，库不存在；
        - locked：GUI 正开着（锁文件存在）；
        - ok：results 逐条成败，applied 为成功条数。
        in/out 临时 JSON 写 db 同目录（_apply_<uuid>），跑完即删。
        """
        stem = self.db_stem_for(sha)
        if stem is None:
            return {"status": "no-tool", "reason": "ida-db-dir-unconfigured"}
        db = self._find_db(stem)
        if db is None:
            return {"status": "no-db"}
        if self.db_locked(stem):
            return {"status": "locked"}
        tag = uuid.uuid4().hex[:12]
        in_json = stem.parent / f"_apply_{tag}.in.json"
        out_json = stem.parent / f"_apply_{tag}.out.json"
        try:
            in_json.write_text(json.dumps({"items": items}, ensure_ascii=False),
                               encoding="utf-8")
            args = [
                self.idat_cmd, "-A",
                f'-S"{self.apply_script_path} {in_json} {out_json}"',
                str(db),
            ]
            rc, out, err = self.runner(args)
            if rc != 0 or not out_json.is_file():
                raise RuntimeError(f"idat 写回失败 rc={rc}: {(err or out)[-400:]}")
            results = json.loads(out_json.read_text(encoding="utf-8")).get("results", [])
            return {"status": "ok", "results": results,
                    "applied": sum(1 for r in results if r.get("ok"))}
        finally:
            in_json.unlink(missing_ok=True)
            out_json.unlink(missing_ok=True)


# ---------------- pull-names 纯函数（当前缓存 vs func_kb 行 diff） ----------------

def diff_pulled_names(data: dict, kb_rows: list[dict]) -> list[dict]:
    """库内重导后：同地址缓存名 != kb 名且非 IDA 自动名 → 待回拉改名。

    返回 [{func_id, address(hex), old_name, new_name}]；自动名（sub_/nullsub/unk_）
    与同名行不动。func_kb 是人机共写主真相，只拉人在 IDA 里起的有意义名字。
    """
    cache_names = {int(f.get("address", 0)): f.get("name")
                   for f in data.get("functions", [])}
    changed = []
    for row in kb_rows:
        addr = int(row["address"])
        new_name = cache_names.get(addr)
        old_name = row.get("name")
        if not new_name or new_name == old_name or is_auto_name(new_name):
            continue
        changed.append({"func_id": row["id"], "address": hex(addr),
                        "old_name": old_name, "new_name": new_name})
    return changed


# ---------------- xref 纯函数（缓存数据 → 结构化调用关系） ----------------

def build_xrefs(data: dict, address: int) -> dict | None:
    """从 v2 缓存构造某函数的 callers/callees（地址 hex 字符串；导入函数地址为 null）。"""
    funcs = data.get("functions", [])
    by_name: dict[str, int] = {}
    target = None
    for f in funcs:
        a = int(f.get("address", 0))
        nm = f.get("name")
        if nm is not None:
            by_name.setdefault(nm, a)
        if a == address:
            target = f
    if target is None:
        return None
    tname = target.get("name")
    callers = [
        {"address": hex(int(f.get("address", 0))), "name": f.get("name")}
        for f in funcs
        if tname and tname in (f.get("calls") or [])
    ]
    callees = [
        {"address": (hex(by_name[nm]) if nm in by_name else None), "name": nm}
        for nm in (target.get("calls") or [])
    ]
    return {"address": hex(address), "name": tname, "callers": callers, "callees": callees}


STRINGS_LIMIT = 5000

# ---- 按需详情缓存（2026-09-30，40万函数样本：调用关系/伪码/反汇编渐进落盘）----
DETAIL_DISASM_MAX_LINES = 5000       # 单函数反汇编行数上限（MCP disasm 同上限）
DETAIL_PSEUDO_MAX_CHARS = 512 * 1024  # 单函数伪码上限（字符）
DETAIL_MAX_REFS = 500                # callers/callees 各自上限（analyze_batch 支持到 5000）
DETAIL_FILE_MAX_BYTES = 1024 * 1024  # 单详情文件硬上限（超限再砍伪码/反汇编）



def build_strings(data: dict, q: str | None = None) -> dict:
    """从 v3 缓存构造字符串视图行（地址 hex；refs 附所属函数名）。

    返回 {items, truncated}。q 大小写不敏感子串过滤；行数超 STRINGS_LIMIT 截断。
    """
    funcs = data.get("functions", [])
    addr_to_name = {int(f.get("address", 0)): f.get("name") for f in funcs}
    needle = (q or "").lower()
    items = []
    truncated = False
    for s in data.get("strings") or []:
        text = s.get("string") or ""
        if needle and needle not in text.lower():
            continue
        if len(items) >= STRINGS_LIMIT:
            truncated = True
            break
        refs = [
            {"func": hex(int(r.get("func_addr", 0))),
             "func_name": addr_to_name.get(int(r.get("func_addr", 0))),
             "from": hex(int(r.get("from_addr", 0)))}
            for r in (s.get("refs") or [])
        ]
        items.append({"address": hex(int(s.get("address", 0))), "string": text,
                      "length": s.get("length", len(text)),
                      "type": s.get("type", "cstr"), "n_refs": len(refs), "refs": refs})
    return {"items": items, "truncated": truncated}


# ---------------- 组合服务 ----------------

EXPORT_TIMEOUT_HINT = "（headless 导出超时/失败）"


class DecompilerService:
    """Agent 调用面：list_functions / decompile / annotate。选路 + 缓存都在这里。"""

    def __init__(self, *, cache_dir: str | Path,
                 global_cache_dir: str | Path | None = None,
                 mcp_endpoint: str | None = None,
                 mcp_provider: Callable[[str], str | None] | None = None,
                 ghidra: GhidraHeadlessBackend | None = None,
                 ida: IDAHeadlessBackend | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 全局 sha 缓存（跨项目复用）：全量 headless 导出按 sha256 落这里；本地缺缓存时
        # 从此提升（promote）。落盘只放客观全量导出（不含 MCP 轻量/部分缓存）。
        self.global_cache_dir = Path(global_cache_dir) if global_cache_dir else None
        self.backends: list = []
        # MCP 懒探活：构造零网络（available() 内 1.5s 超时 + 3s TTL），先入列保证
        # 点查选路优先；脏配置（非 loopback）直接不装。
        self.mcp: MCPBackend | None = None
        if mcp_endpoint:
            try:
                self.mcp = MCPBackend(mcp_endpoint)
                self.backends.append(self.mcp)
            except ValueError:
                self.mcp = None
        # 样本实例桥（2026-09-20 按需拉起）：binary -> endpoint 动态解析回调
        # （IdaMcpManager.ensure）；与固定桥正交——工作台连人手实例用固定端点，
        # Agent 点查走 provider 拉起的样本实例。失败回调 None 一律降级 headless。
        self.mcp_provider = mcp_provider
        self._sample_mcp_inst: MCPBackend | None = None
        # 缓存解析结果单槽驻留：大缓存 JSON 的 re-parse 是 overview 4s 轮询 +
        # 函数/xref/字符串点查的公共热点（1G 级样本防崩，mtime 失效）
        self._parsed_cache: dict[str, tuple[float, dict]] = {}
        for backend in (ghidra, ida):
            if backend is None:
                continue
            try:
                if backend.available():
                    self.backends.append(backend)
            except Exception:  # noqa: BLE001 —— 探测失败视为不可用
                continue

    # ---------- 内部 ----------

    def _cache_file(self, sha: str) -> Path:
        return self.cache_dir / f"{sha}.json"

    def _cache_path(self, binary: str) -> Path:
        return self._cache_file(sha256_file(binary))

    def _global_cache_file(self, sha: str) -> Path | None:
        return (self.global_cache_dir / f"{sha}.json") if self.global_cache_dir else None

    def _promote_from_global(self, sha: str) -> dict | None:
        """本地缺缓存时从全局 sha 缓存提升：命中即原子拷回本地（跨项目复用，免重跑
        headless），绝不删全局源；旧契约/损坏由 _load_cache 判废返回 None。"""
        gf = self._global_cache_file(sha)
        if gf is None or not gf.is_file():
            return None
        data = self._load_cache(gf)
        if data is None:
            return None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            local = self._cache_file(sha)
            tmp = local.with_name(f"{local.name}.tmp")
            tmp.write_bytes(gf.read_bytes())
            tmp.replace(local)
        except OSError:
            return data  # 拷不动也把数据交出去（只读降级，不阻断）
        return data

    def _write_cache_file(self, path: Path, data: dict) -> None:
        """原子写缓存文件（tmp+rename）并失效解析驻留（引擎标记回写用）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        self._parsed_cache.clear()

    def _publish_global(self, sha: str, data: dict) -> None:
        """全量 headless 导出落全局 sha 缓存（原子写），供其它项目按同一 sha 复用。
        仅客观全量导出调用；MCP 轻量/部分缓存绝不入全局（防遮蔽未来全量导出）。"""
        gf = self._global_cache_file(sha)
        if gf is None:
            return
        try:
            gf.parent.mkdir(parents=True, exist_ok=True)
            tmp = gf.with_name(f"{gf.name}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(gf)
        except OSError:
            pass  # 全局缓存 best-effort：写不成不影响本地导出

    @staticmethod
    def _load_cache(cached: Path) -> dict | None:
        """读缓存并校验 export_version；损坏/旧版一律删除，返回 None 触发重导。"""
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cached.unlink(missing_ok=True)
            return None
        try:
            version = int(data.get("export_version", 1))
        except (TypeError, ValueError):
            version = 1
        if version < EXPORT_VERSION:
            cached.unlink(missing_ok=True)
            return None
        return data

    def _export_json(self, binary: str, *, progress: dict | None = None,
                     stop_event: threading.Event | None = None,
                     engine: str | None = None
                     ) -> tuple[dict | None, str, dict]:
        """返回 (导出数据|None, 失败原因链, 导出元信息{name,db_path,stopped})。

        按 sha256 缓存（命中直接复用——缓存不区分引擎，引擎不一致交前端提示重跑）；
        被停止的 partial 缓存**不复用**（重跑即补全），且**不发布**全局。
        engine 指定时按引擎偏好选路（见 _ordered_export_backends）。"""
        cached = self._cache_path(binary)
        if cached.is_file():
            data = self._load_cache(cached)
            if data is not None and not is_partial_export(data):
                return data, "", {}
        sha = sha256_file(binary)
        # 本地缺 → 全局 sha 缓存提升（跨项目同一样本免重跑 headless，大样本加速 P1）
        promoted = self._promote_from_global(sha)
        if promoted is not None and not is_partial_export(promoted):
            return promoted, "", {}
        # 小样本 + 所选引擎=Ghidra → 导出内嵌反汇编；大样本走持久工程按需
        # （GhidraHeadlessBackend.disasm_on_demand），避免缓存膨胀
        want_disasm = (normalize_engine(engine) == "ghidra"
                       and not is_large_sample(binary))
        errors = []
        for backend in self._ordered_export_backends(binary, engine):
            export = getattr(backend, "export", None)
            if export is None:  # MCP 无导出概念，走点查
                continue
            if progress is not None:  # 前端「停止」可用性：仅 Ghidra 支持协作中断
                progress["stoppable"] = "ghidra" in getattr(backend, "name", "")
            try:
                info = export(binary, cached, progress=progress,
                              stop_event=stop_event,
                              want_disasm=want_disasm) or {"name": backend.name}
                data = json.loads(cached.read_text(encoding="utf-8"))
                info.setdefault("name", backend.name)
                # 标记产出引擎（模式/缓存一致性对账用）：写回缓存 + 随全局发布
                try:
                    meta = data.setdefault("meta", {})
                    if isinstance(meta, dict):
                        meta["engine"] = engine_of_backend(backend.name)
                        self._write_cache_file(cached, data)
                except (OSError, ValueError):
                    pass
                # 协作式停止的 partial 只留本地供展示，绝不发布全局（P3 三层数据纪律）
                if not is_partial_export(data):
                    self._publish_global(sha, data)  # 全量导出落全局，供它项目复用
                return data, "", info
            except Exception as e:  # noqa: BLE001
                errors.append(f"{backend.name}: {e}")
        return None, "; ".join(errors), {}

    def _ordered_export_backends(self, binary: str,
                                 engine: str | None = None) -> list:
        """全量导出候选后端顺序（大样本加速 P2，2026-09-30；引擎偏好 2026-10-01）：

        - 大样本（≥阈值）Ghidra（多进程并行分片主产）优先、其余后端兜底；普通样本
          维持装配顺序（IDA 优先，保 IDA 质量与 .i64 存量复用）。
        - engine 指定时把该引擎的后端提到最前（**偏好非排他**：该引擎不可用仍回退
          另一引擎，保证总能出结果）。只含带 export 的后端。"""
        backends = [b for b in self.backends if getattr(b, "export", None) is not None]
        if is_large_sample(binary):
            ghidra = [b for b in backends if "ghidra" in getattr(b, "name", "")]
            if ghidra:
                others = [b for b in backends if "ghidra" not in getattr(b, "name", "")]
                backends = ghidra + others
        if engine:
            want = normalize_engine(engine)
            head = [b for b in backends
                    if engine_of_backend(getattr(b, "name", "")) == want]
            tail = [b for b in backends
                    if engine_of_backend(getattr(b, "name", "")) != want]
            if head:
                backends = head + tail
        return backends

    def _find_func(self, data: dict, address: int | None, name: str | None) -> dict | None:
        for f in data.get("functions", []):
            if address is not None and int(f.get("address", 0)) == address:
                return f
            if name and f.get("name") == name:
                return f
        return None

    def _sample_mcp(self, binary: str) -> MCPBackend | None:
        """点查用样本实例桥（2026-09-20 按需拉起）：mcp_provider 按 binary 动态
        取端点（IdaMcpManager.ensure，含拉起等待；失败 None 降级）。与固定桥同
        端点时直接复用；端点桥缓存复用（MCPBackend 自带探活 TTL，不重握手）。"""
        if self.mcp_provider is None:
            return self.mcp
        try:
            endpoint = self.mcp_provider(binary)
        except Exception:  # noqa: BLE001 —— provider 失败视为离线，降级不抛死
            return self.mcp
        if not endpoint:
            return self.mcp
        if self.mcp is not None and self.mcp.endpoint == endpoint:
            return self.mcp
        if self._sample_mcp_inst is not None \
                and self._sample_mcp_inst.endpoint == endpoint:
            return self._sample_mcp_inst
        try:
            self._sample_mcp_inst = MCPBackend(endpoint)
        except ValueError:
            return self.mcp
        return self._sample_mcp_inst

    # ---------- 工作台接口（研究轨 rev profile） ----------

    def headless_backends(self) -> list:
        """实际装配且可用的 headless 后端（按选路顺序）。"""
        return [b for b in self.backends if getattr(b, "export", None) is not None]

    def read_cached(self, sha: str) -> dict | None:
        """按 sha 读 v3 缓存；不存在/损坏/旧版返回 None（不触发导出）。
        解析结果单槽驻留（mtime 失效）——大样本防崩：每请求 re-parse 几十 MB
        级 JSON 会把 overview 轮询和所有点查拖垮。"""
        cached = self._cache_file(sha)
        if not cached.is_file():
            # 本地缺 → 试全局 sha 缓存提升（跨项目复用，免重跑 headless）
            self._promote_from_global(sha)
            cached = self._cache_file(sha)
        if not cached.is_file():
            self._parsed_cache.pop(sha, None)
            return None
        mtime = cached.stat().st_mtime
        hit = self._parsed_cache.get(sha)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        data = self._load_cache(cached)
        if data is None:
            self._parsed_cache.pop(sha, None)
            return None
        self._parsed_cache.clear()  # 只驻留最近一个样本（工作台单样本活跃）
        self._parsed_cache[sha] = (mtime, data)
        return data

    def import_ida_mcp_cache(self, sha: str, functions: list[dict],
                             binary_name: str = "", *, partial: bool = False,
                             total: int | None = None,
                             next_offset: int | None = None) -> dict:
        """GUI IDA MCP 拉取的轻量缓存落盘（v3 契约兼容）：函数名/地址/大小
        全量；无伪码/calls/strings/imports——点查伪码与 xref 由既有「缓存缺席
        → MCP 实时降级」通道按需取，大样本的数据大头（伪码）不落平台盘。
        partial=True：断点续拉的部分缓存——meta.partial/total_functions/
        next_offset 供前端渐进展示与续传对账（拉完一页落一次盘）。"""
        meta: dict = {"source": "ida-mcp",
                      "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                 time.gmtime())}
        if partial:
            meta["partial"] = True
            if total is not None:
                meta["total_functions"] = total
            if next_offset is not None:
                meta["next_offset"] = next_offset
        data = {
            "export_version": EXPORT_VERSION,
            "binary": binary_name,
            "meta": meta,
            "functions": functions,
            "sections": [],
            "imports": {},
            "strings": [],
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file(sha).write_text(json.dumps(data, ensure_ascii=False),
                                         encoding="utf-8")
        self._parsed_cache.clear()  # 驻留失效
        return data

    def import_export_json(self, sha: str, src_json: str | Path,
                           *, publish: bool = True) -> dict:
        """导入外部 IDA/headless 全量导出 JSON 为本地缓存（免重跑 IDA 也免 MCP 回拉）。

        只收 export_version >= EXPORT_VERSION 的完整导出；低契约拒绝（让上层重导）。
        返回 {status: ok, functions, export_version} 或 {status: error, reason}。
        """
        src = Path(src_json)
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"status": "error", "reason": f"读取失败: {e}"}
        if not isinstance(data, dict):
            return {"status": "error", "reason": "JSON 顶层不是对象"}
        try:
            version = int(data.get("export_version", 1))
        except (TypeError, ValueError):
            version = 1
        if version < EXPORT_VERSION:
            return {"status": "error",
                    "reason": f"导出契约版本过低（{version} < {EXPORT_VERSION}），请重导"}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file(sha).write_text(json.dumps(data, ensure_ascii=False),
                                         encoding="utf-8")
        self._parsed_cache.pop(sha, None)
        if publish:
            self._publish_global(sha, data)
        return {"status": "ok", "functions": len(data.get("functions") or []),
                "export_version": version}

    def import_ida_db(self, sha: str, src_db: str | Path) -> dict:
        """纳入外部 IDA 数据库（.i64/.idb）到项目 db_dir 并库内重导为缓存。

        免去「IDA GUI 打开 → Ctrl-Alt-M → MCP 分页回拉」一整轮：把已分析好的库直接
        拷进项目，headless 库内重导复用库内既有自动分析（而非从样本重建）。
        返回 {status: ok, functions, db_path} 或结构化 error/locked/no-tool/unsupported。
        """
        ida = self._ida()
        if ida is None:
            if self.headless_backends():
                return {"status": "unsupported", "guidance": self.WRITEBACK_GHIDRA_GUIDANCE}
            return {"status": "no-tool", "guidance": DECOMPILE_GUIDANCE}
        stem = ida.db_stem_for(sha)
        if stem is None:
            return {"status": "no-tool", "reason": "ida-db-dir-unconfigured"}
        src = Path(src_db)
        if not src.is_file():
            return {"status": "error", "reason": f"库文件不存在: {src}"}
        ext = src.suffix.lower()
        if ext not in ida.db_exts:
            return {"status": "error",
                    "reason": f"不是 IDA 库（需 {'/'.join(ida.db_exts)}）: {src.name}"}
        dst = stem.with_suffix(ext)
        if dst.exists() and ida.db_locked(stem):
            return {"status": "locked"}
        stem.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
        except OSError as e:
            return {"status": "error", "reason": f"拷贝失败: {e}"}
        # 清掉另一扩展名的旧库，避免 _find_db 选到过时那份
        for other in ida.db_exts:
            cand = stem.with_suffix(other)
            if cand != dst and cand.exists():
                cand.unlink(missing_ok=True)
        cached = self._cache_file(sha)
        try:
            ida.export_db(dst, cached)
        except Exception as e:  # noqa: BLE001 —— 结构化返回，不抛死
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"[:400]}
        data = json.loads(cached.read_text(encoding="utf-8"))
        self._parsed_cache.pop(sha, None)
        self._publish_global(sha, data)
        return {"status": "ok", "functions": len(data.get("functions") or []),
                "db_path": str(dst)}

    # ---------- 按需详情缓存（40万样本：调用关系/伪码/反汇编渐进落盘） ----------

    def _detail_dir(self, sha: str) -> Path:
        return self.cache_dir / f"{sha}.details"

    def _detail_file(self, sha: str, address: int) -> Path:
        return self._detail_dir(sha) / f"{hex(address)}.json"

    def read_func_detail(self, sha: str, address: int) -> dict | None:
        """读单函数详情（每函数一文件，O(1) 不拖累主缓存轮询）；损坏删文件重拉。"""
        f = self._detail_file(sha, address)
        if not f.is_file():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            f.unlink(missing_ok=True)
            return None

    def save_func_detail(self, sha: str, address: int, detail: dict) -> None:
        """原子写详情文件（tmp+rename）。硬上限兜底：伪码 512K、反汇编 5000 行、
        单文件 1MB（超限再砍伪码/反汇编），防长函数/异常组合撑爆磁盘与解析。"""
        out = dict(detail)
        pc = out.get("pseudocode")
        if isinstance(pc, str) and len(pc) > DETAIL_PSEUDO_MAX_CHARS:
            out["pseudocode"] = pc[:DETAIL_PSEUDO_MAX_CHARS]
            out["pseudocode_truncated"] = True
        lines = out.get("disasm_lines") or []
        if len(lines) > DETAIL_DISASM_MAX_LINES:
            out["disasm_lines"] = lines[:DETAIL_DISASM_MAX_LINES]
            out["disasm_truncated"] = True
        body = json.dumps(out, ensure_ascii=False).encode("utf-8", "ignore")
        if len(body) > DETAIL_FILE_MAX_BYTES:
            out["disasm_lines"] = (out.get("disasm_lines") or [])[:2000]
            out["disasm_truncated"] = True
            if isinstance(out.get("pseudocode"), str) \
                    and len(out["pseudocode"]) > 256 * 1024:
                out["pseudocode"] = out["pseudocode"][:256 * 1024]
                out["pseudocode_truncated"] = True
            body = json.dumps(out, ensure_ascii=False).encode("utf-8", "ignore")
        d = self._detail_dir(sha)
        d.mkdir(parents=True, exist_ok=True)
        f = self._detail_file(sha, address)
        tmp = f.with_name(f"{f.name}.tmp")
        tmp.write_bytes(body)
        tmp.replace(f)

    def _ghidra_backend(self) -> GhidraHeadlessBackend | None:
        return next((b for b in self.backends
                     if isinstance(b, GhidraHeadlessBackend)), None)

    def _ghidra_detail(self, sha: str, address: int, binary: str | None,
                       binary_name: str = "") -> tuple[dict | None, bool]:
        """Ghidra 模式按需详情：伪码/调用关系取自 headless 缓存；反汇编取自缓存
        （小样本导出内嵌 disasm_lines）或持久 Ghidra 工程按需反汇编（大样本）。

        返回 (detail|None, persist)：无伪码又无反汇编的半成品不落盘（下次可自愈重试）。"""
        data = self.read_cached(sha)
        if data is None:
            return None, False
        target = self._find_func(data, address, None)
        if target is None:
            return None, False
        x = build_xrefs(data, address) or {}
        lines = list(target.get("disasm_lines") or [])
        truncated = bool(target.get("disasm_truncated"))
        if not lines:
            g = self._ghidra_backend()
            if g is not None and binary:
                try:
                    got = g.disasm_on_demand(binary, [address],
                                             max_lines=DETAIL_DISASM_MAX_LINES)
                    lines = got.get(int(address)) or []
                except Exception:  # noqa: BLE001 —— 反汇编失败降级为无反汇编
                    log.exception("Ghidra 按需反汇编失败 sha=%s addr=%s",
                                  sha, hex(address))
        detail = {
            "address": hex(int(target.get("address", address))),
            "name": target.get("name"),
            "size": int(target.get("size") or 0),
            "pseudocode": target.get("pseudocode"),
            "disasm_lines": lines,
            "disasm_truncated": truncated,
            "callers": x.get("callers") or [],
            "callees": x.get("callees") or [],
            "source": "cache",
            "engine": "ghidra",
        }
        if binary_name:
            detail["binary"] = binary_name
        return detail, bool(lines) or bool(target.get("pseudocode"))

    def ensure_func_detail(self, sha: str, address: int, binary_name: str = "",
                           *, engine: str | None = None,
                           binary: str | None = None) -> dict | None:
        """按需详情主通道：详情文件命中直接返回（source=cache）；未命中按引擎取：

        - IDA（默认）：MCP 在线 → analyze_batch 拉 callers/callees/伪码/反汇编并落盘
        - Ghidra：伪码/调用关系取自 headless 缓存，反汇编取缓存（小样本内嵌）或
          持久 Ghidra 工程按需反汇编（大样本）；都没有 → None
        """
        cached = self.read_func_detail(sha, address)
        if cached is not None:
            return cached
        persist = True
        if normalize_engine(engine) == "ghidra":
            detail, persist = self._ghidra_detail(sha, address, binary, binary_name)
            if detail is None:
                return None
        else:
            if self.mcp is None or not self.mcp.available():
                return None
            detail = self.mcp.func_detail(address)
            if detail is None:
                return None
            detail["source"] = "cache"
            if binary_name:
                detail["binary"] = binary_name
        detail["pulled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                            time.gmtime())
        if persist:
            self.save_func_detail(sha, address, detail)
        return detail


    def export_to_cache(self, binary: str, *, progress: dict | None = None,
                        stop_event: threading.Event | None = None,
                        engine: str | None = None) -> tuple[dict, dict]:
        """确保样本已导出（缺缓存/旧版/被停止的 partial 则跑 headless）；无后端抛 RuntimeError。

        progress（可变 dict）实时带出分片进度（大样本 P3）；stop_event 置位即协作式
        停止并把已导出部分落盘。engine 指定时按引擎偏好选路（IDA/Ghidra 双模式）。
        返回 (v3 数据, 导出元信息 {name, db_path, stopped})。
        """
        data, err, info = self._export_json(binary, progress=progress,
                                            stop_event=stop_event, engine=engine)
        if data is None:
            raise RuntimeError(err or DECOMPILE_GUIDANCE)
        return data, info

    def xrefs_for(self, sha: str, address: int) -> dict | None:
        """缓存缺席时 MCP 在线则 func_profile 实时降级（source=mcp）；都没有返回 None。"""
        data = self.read_cached(sha)
        if data is not None:
            return build_xrefs(data, address)
        if self.mcp is not None:
            return self.mcp.xref_profile(address)
        return None

    def mcp_online(self) -> bool:
        """overview 三态灯/写回选路用（懒探活，TTL 缓存）。"""
        return bool(self.mcp is not None and self.mcp.available())

    def live_decompile(self, address: int) -> dict | None:
        """缓存缺席的单函数伪码：MCP 在线实时取当前 IDA 库（source=mcp 行级标记）。"""
        if self.mcp is None:
            return None
        code = self.mcp.decompile_at(address)
        if code is None:
            return None
        return {"source": "mcp", "address": hex(address), "pseudocode": code}

    # ---------- IDA 双向写回（DESIGN.md §9；MCP 实时通道阶段 D 在此前插） ----------

    WRITEBACK_GHIDRA_GUIDANCE = (
        "当前只有 Ghidra headless（临时工程用完即删），不支持持久写回。"
        "请安装 IDA（idat64/idat 在 PATH）后重分诊，或在 MCP 实时模式下写回。"
    )

    def _ida(self) -> IDAHeadlessBackend | None:
        return next((b for b in self.backends if isinstance(b, IDAHeadlessBackend)), None)

    def writeback(self, sha: str, items: list[dict]) -> dict:
        """func_kb 命名/注释 → IDA 库。结构化降级，不抛死：

        MCP 在线优先实时写 GUI 当前库（channel=mcp，无锁问题）；传输级失败/离线
        降级 headless。headless 五元组：ok / locked（GUI 开着）/ no-db（未分诊）
        / no-tool / unsupported（仅 Ghidra）。
        """
        if self.mcp is not None:
            live = self.mcp.writeback_items(items)
            if live is not None:
                return live
        ida = self._ida()
        if ida is not None:
            return ida.apply_by_sha(sha, items)
        if self.headless_backends():
            return {"status": "unsupported", "guidance": self.WRITEBACK_GHIDRA_GUIDANCE}
        return {"status": "no-tool", "guidance": DECOMPILE_GUIDANCE}

    def refresh_db_cache(self, sha: str) -> dict:
        """对现有 IDA 库重跑导出覆盖缓存（pull-names 前置）。

        返回 {status: ok, data} 或 locked/no-db/no-tool/unsupported；
        锁文件存在时绝不跑（idat 与 GUI 同写一库会坏库）。
        """
        ida = self._ida()
        if ida is None:
            if self.headless_backends():
                return {"status": "unsupported", "guidance": self.WRITEBACK_GHIDRA_GUIDANCE}
            return {"status": "no-tool", "guidance": DECOMPILE_GUIDANCE}
        stem = ida.db_stem_for(sha)
        if stem is None:
            return {"status": "no-tool", "reason": "ida-db-dir-unconfigured"}
        db = ida._find_db(stem)
        if db is None:
            return {"status": "no-db"}
        if ida.db_locked(stem):
            return {"status": "locked"}
        cached = self._cache_file(sha)
        ida.export_db(db, cached)  # 失败抛 RuntimeError → Job error
        data = json.loads(cached.read_text(encoding="utf-8"))
        self._parsed_cache.pop(sha, None)
        self._publish_global(sha, data)  # 库内重导同为全量导出，落全局复用
        return {"status": "ok", "data": data}

    # ---------- Agent 接口（§9 四接口） ----------

    def list_functions(self, binary: str, name_contains: str | None = None,
                       min_size: int | None = None) -> str:
        # MCP 插件只反映 IDA 当前打开的库、无 binary 维度，绝不用它的全量列表
        # 替换本样本的 headless 缓存（三层数据纪律）——列表只走 headless。
        # 2026-09-27：加 name_contains/min_size 过滤（Agent 免直读全量缓存 JSON）。
        errors = ""
        for backend in self.backends:
            if backend.name == "mcp":
                continue
            data, err, _info = self._export_json(binary)
            if data:
                needle = (name_contains or "").lower()
                rows = [{"address": hex(int(f["address"])), "name": f["name"],
                         "size": f.get("size", 0),
                         "pseudocode": bool(f.get("pseudocode"))}
                        for f in data.get("functions", [])
                        if (not needle or needle in f["name"].lower())
                        and (not min_size or (f.get("size", 0) or 0) >= min_size)]
                # 大样本防崩：数万函数全量 dump 淹没上下文——500 行截断提示过滤
                tail = ""
                if len(rows) > 500:
                    rows = rows[:500]
                    tail = ("\n…（结果超 500 行已截断：请用 name_contains/"
                            "min_size 缩小范围）")
                return json.dumps(rows, ensure_ascii=False) + tail
            if err:
                errors = err
        return DECOMPILE_GUIDANCE + (f"\n失败详情: {errors}" if errors else "")

    def strings_for(self, binary: str, q: str | None = None,
                    limit: int = 200) -> str:
        """Agent strings 检索（2026-09-27）：复用 build_strings（大小写不敏感
        子串过滤，行带地址+引用函数），limit 截断防淹没上下文——治 Agent 用
        run_cmd 直读全量缓存 JSON 的低效模式（实测 b1nary 会话反复 Get-Content
        66KB 文件十余次）。"""
        data, _err, _info = self._export_json(binary)
        if not data:
            return DECOMPILE_GUIDANCE
        view = build_strings(data, q)
        total = len(view["items"])
        items = view["items"][:max(1, min(int(limit or 200), 1000))]
        return json.dumps({"count": len(items), "total_matched": total,
                           "truncated": total > len(items), "items": items},
                          ensure_ascii=False)

    def xrefs_for_func(self, binary: str, address: int | None = None,
                       name: str | None = None) -> str:
        """Agent xref 点查（2026-09-27）：地址入参先经缓存解析成函数名，再走
        xrefs（MCP 在线优先实时 func_profile，降级缓存全量 calls 反查）。"""
        if name is None:
            if address is None:
                return "[错误] func_xrefs 需要 name 或 address 至少其一"
            data, _err, _info = self._export_json(binary)
            if not data:
                return DECOMPILE_GUIDANCE
            f = self._find_func(data, address, None)
            if f is None:
                return (f"[错误] 地址 {hex(address)} 不在任何已知函数入口上"
                        f"——先 list_symbols(name_contains=…) 定位函数名")
            name = f["name"]
        return self.xrefs(binary, name)

    def decompile(self, binary: str, address: int | None = None, name: str | None = None) -> str:
        sha = sha256_file(binary)
        want = address
        if want is None and name is not None:
            data0, _e, _i = self._export_json(binary)
            f0 = self._find_func(data0, None, name) if data0 else None
            if f0 is not None:
                want = int(f0["address"])
        # 点查候选：样本实例桥（按需拉起）优先，其次固定桥；去重后与原选路一致
        sample = self._sample_mcp(binary)
        backends: list = []
        for backend in ([sample] if sample is not None else []) + self.backends:
            if not any(backend is b for b in backends):
                backends.append(backend)
        for backend in backends:
            if backend.name == "mcp":
                # 仅点查走 MCP（人在回路，当前库即目标）；全量概览不做 MCP 全量拉取
                if address is None and name is None:
                    continue
                r = backend.decompile_at(address if address is not None else name)
                if r:
                    return r
            else:
                data, _err, _info = self._export_json(binary)
                if not data:
                    continue
                if address is None and name is None:
                    # 全量概览：每函数伪码截断（防淹没上下文）
                    parts = []
                    for f in data.get("functions", [])[:20]:
                        pc = (f.get("pseudocode") or "（导出无伪码）")[:1500]
                        parts.append(f"== {f['name']} @ {hex(int(f['address']))} ==\n{pc}")
                    return "\n\n".join(parts)
                f = self._find_func(data, address, name)
                if f:
                    if f.get("pseudocode"):
                        return (f"== {f['name']} @ {hex(int(f['address']))} ==\n"
                                + f["pseudocode"])
                    # 名单有但无伪码（IDA 拉取轻量缓存）：落到按需详情通道
                    break
        # 按需详情兜底（详情缓存命中；或 MCP 在线自动 analyze_batch 拉取落盘）——
        # IDA 拉取样本的伪码主通道（headless 缓存只有名单）
        if want is not None:
            detail = self.ensure_func_detail(sha, want)
            if detail is not None and detail.get("pseudocode"):
                return (f"== {detail.get('name') or hex(want)} @ {hex(want)} ==\n"
                        + detail["pseudocode"])
        return DECOMPILE_GUIDANCE

    def disasm(self, binary: str, address: int | None = None,
               name: str | None = None) -> str:
        """Agent 反汇编点查（2026-09-30）：按需详情通道（详情缓存 → MCP analyze_batch
        自动拉取落盘）。与 decompile 同吃 ensure_func_detail，离线只读已缓存部分。"""
        sha = sha256_file(binary)
        want = address
        if want is None and name is not None:
            data0, _e, _i = self._export_json(binary)
            f0 = self._find_func(data0, None, name) if data0 else None
            if f0 is not None:
                want = int(f0["address"])
        if want is None:
            return "[错误] disasm 需要 name 或 address 至少其一"
        detail = self.ensure_func_detail(sha, want)
        if detail is None or not detail.get("disasm_lines"):
            return "[反编译器不可用] 未获取到反汇编（需 IDA + MCP 在线自动拉取；" \
                   "离线只能读此前已缓存过的函数）"
        head = f"== {detail.get('name') or hex(want)} @ {hex(want)} =="
        body = "\n".join(detail["disasm_lines"])
        if detail.get("disasm_truncated"):
            body += "\n…（反汇编超长已截断）"
        return f"{head}\n{body}"

    def annotate(self, binary: str, name: str, comment: str) -> str:
        # Agent 侧只落 sidecar 伴随记录：MCP 实时写/headless apply 的正式通道是
        # 工作台 writeback（按地址、带锁检测），这里按名字直写会绕过 func_kb 主真相。
        sidecar = self.cache_dir / f"{sha256_file(binary)}.annotations.json"
        entries = []
        if sidecar.is_file():
            try:
                entries = json.loads(sidecar.read_text(encoding="utf-8"))
            except ValueError:
                entries = []
        entries.append({"name": name, "comment": comment,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        sidecar.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        return f"已记录（headless sidecar）: {name}——正式结论请同步 bb_upsert_func"

    def xrefs(self, binary: str, func: str) -> str:
        # MCP 在线：func_profile 按函数名实时取（xref_profile 吃名/地址）；
        # 样本实例桥（按需拉起）优先，其次固定桥
        mcp = self._sample_mcp(binary)
        if mcp is not None:
            live = mcp.xref_profile(func)
            if live is not None:
                return json.dumps(
                    {"function": func,
                     "callers": [c.get("name") for c in live.get("callers", [])],
                     "callees": [c.get("name") for c in live.get("callees", [])]},
                    ensure_ascii=False)
        data, _err, _info = self._export_json(binary)
        if data:
            funcs = data.get("functions", [])
            callers = [f["name"] for f in funcs if func in (f.get("calls") or [])]
            target = next((f for f in funcs if f.get("name") == func), None)
            callees = list(target.get("calls") or []) if target else []
            return json.dumps({"function": func, "callers": callers, "callees": callees},
                              ensure_ascii=False)
        return DECOMPILE_GUIDANCE


def build_headless_service(cache_dir: str | Path, *, runner,
                           prefer: tuple[str, ...] = ("ida", "ghidra"),
                           global_cache_dir: str | Path | None = None,
                           ida_db_dir: str | Path | None = None,
                           ghidra_tmp_dir: str | Path | None = None,
                           mcp_endpoint: str | None = None,
                           mcp_provider: Callable[[str], str | None] | None = None,
                           available: bool | None = None) -> DecompilerService:
    """研究工作台工厂：headless 后端按 prefer 顺序选路；mcp_endpoint 非空时加装
    MCP 实时桥（懒探活，工作台经 select_mcp_endpoint 给值）；mcp_provider 装配
    样本实例桥（IdaMcpManager.ensure 按需拉起，Agent 会话工厂接线用）。

    available=None 时真实探测 PATH；测试可注入 available=True 走假 runner。
    """
    constructors = {
        "ida": lambda: IDAHeadlessBackend(idat_cmd=resolve_ida_headless() or "idat64",
                                          runner=runner, db_dir=ida_db_dir,
                                          available=available),
        "ghidra": lambda: GhidraHeadlessBackend(
            headless_cmd=resolve_ghidra_headless() or "analyzeHeadless",
            runner=runner, tmp_project_dir=ghidra_tmp_dir,
            available=available, workers=resolve_ghidra_workers()),
    }
    svc = DecompilerService(cache_dir=cache_dir,
                            global_cache_dir=global_cache_dir,
                            mcp_endpoint=mcp_endpoint,
                            mcp_provider=mcp_provider)
    for key in prefer:
        build = constructors.get(key)
        if build is None:
            continue
        backend = build()
        try:
            if backend.available():
                svc.backends.append(backend)
        except Exception:  # noqa: BLE001 —— 探测失败跳过该后端
            continue
    return svc
