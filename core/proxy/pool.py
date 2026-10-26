"""代理池服务（fir-proxy 托管）——**全局单池**。

平台进程持有**一个** serve 子进程 + **一份**池文件（``data/proxy/pool.json``），
所有项目共享（人类 UI 与 AI 经 MCP 控制面取端点自行 ``curl -x``）。经 loopback
控制通道查状态 / 手动轮换 / 热增删代理。

设计要点见 ``docs/plans/proxy-pool-integration.md``：
- **平台托管**：serve 不随会话/MCP 生死，人类 UI 与 AI 共享同一池，重启可恢复。
  （MCP 子进程随会话起停、崩溃重拉——池若住进去会随之生死。）
- **不动上游**：包 fir-proxy 的 ``ProxyRotator``/``ProxyServer``（``tools/fir-proxy/cyberstrike_serve.py``），
  不改 fir-proxy 自身文件。
- **全局化（2026-10-11）**：此前「项目级」=每项目一个 serve + 一份池（内部按 pid 分区），
  池被切碎、抓取/验证重复；现收敛为进程级单 serve 单池，旧池经 ``merge_legacy_pools()``
  首启合并。三端口从 base 起取空闲口（单 serve 无冲突问题）。
- **AI 用法**：AI 拿 ``status()`` 的 http/socks5 端点自行 ``curl -x``，平台不注入。
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

from core.runtime.backends import (
    assign_to_job,
    close_job,
    create_kill_on_close_job,
)

log = logging.getLogger(__name__)

RUNNER_REL = "fir-proxy/cyberstrike_serve.py"
RUNNER_FETCH_REL = "fir-proxy/cyberstrike_fetch.py"
RUNNER_CHECK_REL = "fir-proxy/cyberstrike_check.py"
CLI_REL = "fir-proxy/cli.py"
PORT_SCAN_SPAN = 400


class ProxyError(RuntimeError):
    """代理池操作失败（供 API 层转 4xx/5xx）。"""


# ---------------------------------------------------------------- 配置

@dataclass
class ProxyConfig:
    """代理池配置；``config/proxy.json`` 缺文件全默认（坏文件亦容错）。"""

    http_port_base: int = 1800
    socks5_port_base: int = 1801
    control_port_base: int = 1810
    region: str = "All"
    max_latency_ms: float | None = None
    request_rotation_count: int = 10
    target_failover_threshold: int = 3
    startup_timeout_s: float = 40.0
    python: str | None = None            # 显式解释器；None=自动解析
    auto_start: bool = False             # 项目首个代理动作时是否自动起服务
    validate_timeout_s: int = 12         # 单代理验证总超时；连接阶段由 runner 限制，读取阶段允许更宽
    # 验证基准网站（**2026-10-15 由 i.zut.edu.cn 改为 Cloudflare 204 探测点**）：原站整站
    # 返 502（实测站点根与 https 均不可达），会让**所有**代理被判 Failed 而全量剔除——
    # 抓得再多也留不下。改用 204 探测点：http 明文（大多代理能做，不大量剔除纯 HTTP 转发型）、
    # 无响应体（最省流量）、全球 CDN（最稳）。判据 `200 <= status_code < 400` 认 204。
    validate_target: str = "http://cp.cloudflare.com/generate_204"  # 验证目标 URL（经代理 GET 它，通即 Working）
    validate_target_https: str | None = "https://cp.cloudflare.com/generate_204"  # HTTPS 能力探测目标（经 CONNECT）；None/空=不探测
    # 内容注入/劫持型出口交叉验证（2026-10-11）：第二个目标（须与 validate_target 内容不同）。
    # 两个目标经同一代理返回**相同响应体指纹**即判 hijacked → status=Failed 剔除。
    validate_target_b: str | None = "http://example.com"  # None/空=不做多目标一致性校验
    prune_latency_ms: float | None = 5000.0  # 验证后按延迟剔除阈值（ms）；None/0=不按延迟删
    sources_file: str | None = None      # 抓取源清单覆盖路径；None=默认 config/proxy_sources.json（不存在则用 runner 内置）
    # 单次抓取条数上限（按协议轮转取）；0=不限。前端「抓取条数」输入框与 API `limit` 可逐次覆盖。
    # 2026-10-15：源清单扩到 ~18k 条原始量后，默认 2000 会明显截断 → 提到 3000。
    fetch_limit: int = 3000
    # ---- 自动巡检（2026-10-11）：后台每 maintenance_interval_s 验证全部 + 阈值补抓 ----
    maintenance_interval_s: float = 1800.0  # 巡检间隔（30 分钟）；0=不巡检
    auto_fetch_min_http: int = 50        # http 可用数低于此即自动抓取；0=该协议不自动补抓
    auto_fetch_min_https: int = 20       # https（能力探测）可用数低于此即自动抓取
    auto_fetch_min_socks: int = 20       # socks（socks4+socks5）可用数低于此即自动抓取
    auto_fetch_cooldown_s: float = 600.0  # 同协议两次自动抓取最小间隔（防抓取风暴）

    @classmethod
    def from_file(cls, path: str | Path | None) -> "ProxyConfig":
        cfg = cls()
        if not path:
            return cfg
        p = Path(path)
        if not p.exists():
            return cfg
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("config/proxy.json 解析失败，使用默认配置: %s", p)
            return cfg
        if not isinstance(data, dict):
            return cfg
        allowed = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key in allowed:
                setattr(cfg, key, value)
        return cfg


def write_config_file(path: str | Path, updates: dict) -> None:
    """把 ``updates``（仅 ProxyConfig 字段）合并写回 ``config/proxy.json``（API 配置入口用）。

    读-合并-整写：保留文件里其它键（如逐机覆盖的端口/阈值），只覆盖传入字段。
    原子写（临时文件 + replace）。文件不存在/坏文件按空 dict 起步（写入即建文件）。
    """
    p = Path(path)
    data: dict = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}
    allowed = {f.name for f in fields(ProxyConfig)}
    for key, value in updates.items():
        if key in allowed:
            data[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------- 工具探测

def runner_path(tools_root: str | Path) -> Path:
    return Path(tools_root) / RUNNER_REL


def cli_path(tools_root: str | Path) -> Path:
    return Path(tools_root) / CLI_REL


def runner_fetch_path(tools_root: str | Path) -> Path:
    """平台侧抓取 runner（可配置源清单）；缺失时由 _run_cli 报 ProxyError。"""
    return Path(tools_root) / RUNNER_FETCH_REL


def runner_check_path(tools_root: str | Path) -> Path:
    """平台侧验证 runner（宽松单目标连通性）；缺失时由 _run_cli 报 ProxyError。"""
    return Path(tools_root) / RUNNER_CHECK_REL


def _candidate_pythons(tools_root: str | Path, override: str | None):
    if override:
        yield override
    venv = Path(tools_root) / "venv" / ("Scripts/python.exe" if os.name == "nt"
                                        else "bin/python")
    yield str(venv)
    if sys.executable and Path(sys.executable).name.lower().startswith("python"):
        yield sys.executable
    for name in ("python3", "python"):
        found = _which(name)
        if found:
            yield found


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


_probe_cache: dict[str, tuple[bool, str]] = {}


def proxy_available(tools_root: str | Path, python: str | None = None) -> tuple[bool, str]:
    """能力探测：runner/cli 在场 + 解释器可 import socks。返回 (ok, 说明)。"""
    if not runner_path(tools_root).exists():
        return False, f"fir-proxy runner 缺失: {runner_path(tools_root)}"
    if not cli_path(tools_root).exists():
        return False, f"fir-proxy cli 缺失: {cli_path(tools_root)}"
    if not runner_check_path(tools_root).exists():
        return False, f"fir-proxy 验证 runner 缺失: {runner_check_path(tools_root)}"
    py = resolve_python(tools_root, python)
    if not py:
        return False, "未找到可用 Python 解释器"
    key = py
    if key in _probe_cache:
        return _probe_cache[key]
    try:
        proc = subprocess.run(
            [py, "-c", "import socks, requests; import bs4, lxml"],
            capture_output=True, timeout=20,
            creationflags=_no_window(), text=True, encoding="utf-8",
            errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"解释器探测失败: {exc}")
    else:
        result = ((True, py) if proc.returncode == 0
                  else (False, f"解释器缺少依赖（requests[socks]/bs4/lxml）: {py}"))
    _probe_cache[key] = result
    return result


def resolve_python(tools_root: str | Path, override: str | None = None) -> str | None:
    for cand in _candidate_pythons(tools_root, override):
        if cand and Path(cand).exists():
            return cand
    return None


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ---------------------------------------------------------------- 孤儿 serve 回收

#: 只认本平台的 serve runner（防误杀同名他人进程）
_SERVE_CMD_MARKERS = ("cyberstrike_serve.py", "cyberstrike_serve")


def _pid_alive(pid: int | None) -> bool:
    """OS 层判进程是否存活（OpenProcess 拿得到句柄即活）。pid 非法/拿不到→False。"""
    if os.name != "nt":
        return True  # 非 Windows 不做孤儿回收（reap 直接早退），保守为「活」
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        h = k32.OpenProcess(0x0800, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    except Exception:  # noqa: BLE001
        return False


def _taskkill_tree(pid: int) -> bool:
    """杀进程树（taskkill /T /F）；成功返回 True。"""
    try:
        k = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15,
                           creationflags=_no_window())
        return k.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _list_processes() -> list[dict]:
    """枚举全进程（pid/ppid/命令行）；失败返回 []。"""
    import json as _json
    try:
        ps = ("Get-CimInstance Win32_Process | "
              "Select-Object ProcessId,ParentProcessId,CommandLine | "
              "ConvertTo-Json -Compress")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           creationflags=_no_window(), encoding="utf-8")
        procs = _json.loads(r.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if isinstance(procs, dict):
        procs = [procs]
    return procs


def reap_orphan_serves() -> list[int]:
    """回收「父进程已死」的 cyberstrike_serve 孤儿（2026-10-12 事故）。

    后端每次重启都会让上一进程的 serve 子进程变孤儿（旧实现是**每项目一个池**，
    历史遗留一批），占着 http/socks5/control 三端口与池文件句柄。旧进程的
    ``_serve`` 引用随进程消失，新进程的 ``shutdown`` 也够不着——只能在启动时扫。

    判定：命令行含本 runner + 记 ``-i <pool.json>``；**父进程 pid 已不存在**即杀
    （taskkill /T /F）。正在被本进程父进程持有的 serve 不动（其父存活）。
    非 Windows / 枚举失败一律返回 []（绝不误杀）。返回被杀的 pid 列表。
    """
    if os.name != "nt":
        return []
    killed: list[int] = []
    for p in _list_processes():
        cmd = p.get("CommandLine") or ""
        if not any(m in cmd for m in _SERVE_CMD_MARKERS) or "-i" not in cmd:
            continue
        pid = p.get("ProcessId")
        ppid = p.get("ParentProcessId")
        if not pid or _pid_alive(ppid):
            continue  # 父存活=活的托管 serve，不动
        if _taskkill_tree(int(pid)):
            killed.append(int(pid))
    if killed:
        log.info("回收孤儿代理 serve 进程: %s", killed)
    return killed


def _free_port(start: int, taken: set[int]) -> int:
    for port in range(start, start + PORT_SCAN_SPAN):
        if port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        taken.add(port)
        return port
    raise ProxyError(f"在 {start}..{start + PORT_SCAN_SPAN} 内找不到空闲端口")


# ---------------------------------------------------------------- 池记录

_PROXY_VIEW_KEYS = ("proxy", "protocol", "location", "latency", "speed",
                    "anonymity", "score", "status", "https_ok")


def _proxy_view(record: dict) -> dict:
    return {k: record.get(k) for k in _PROXY_VIEW_KEYS}


def select_records(records: list[dict], limit: int = 1, region: str | None = None,
                   max_latency_ms: float | None = None,
                   include_failed: bool = False) -> list[dict]:
    """按评分挑代理（纯逻辑，镜像 fir-proxy ``cli select``）。"""
    out = []
    for r in records:
        if not include_failed and r.get("status") not in (None, "Working"):
            continue
        if region and r.get("location") != region:
            continue
        latency = r.get("latency")
        if max_latency_ms is not None:
            if not isinstance(latency, (int, float)):
                continue
            if latency * 1000 > max_latency_ms:
                continue
        out.append(r)
    out.sort(key=lambda r: r.get("score") or 0, reverse=True)
    return out[: max(0, limit)]


def _is_usable(record: dict, prune_latency_ms: float | None) -> bool:
    """验证后判据：``status=="Working"`` 且（阈值关 或 延迟有限且 ``latency*1000<=阈值``）。

    延迟缺失/非有限时**保留**——Working 已过连通性检查，仅因缺延迟不判死。
    """
    if record.get("status") != "Working":
        return False
    if not prune_latency_ms:
        return True
    latency = record.get("latency")
    if not isinstance(latency, (int, float)) or not math.isfinite(latency):
        return True
    return latency * 1000 <= prune_latency_ms


def _prune(records: list[dict], prune_latency_ms: float | None
           ) -> tuple[list[dict], list[dict]]:
    """按 ``_is_usable`` 拆分 ``(保留, 剔除)``。"""
    keep: list[dict] = []
    dropped: list[dict] = []
    for record in records:
        (keep if _is_usable(record, prune_latency_ms) else dropped).append(record)
    return keep, dropped


#: socks 口径（2026-10-11）：socks4 与 socks5 合并计（用户选定）。
SOCKS_PROTOCOLS = frozenset({"socks4", "socks5"})


def protocol_counts(records: list[dict], prune_latency_ms: float | None = None) -> dict:
    """按协议分桶计数（纯函数，2026-10-11）：``{http, socks, https, total}``。

    - ``http``：``protocol=="http"`` 且可用（``_is_usable``，非 Working/超延迟不计）。
    - ``socks``：``protocol in SOCKS_PROTOCOLS``（socks4+socks5 合并）且可用。
    - ``https``：``https_ok is True`` 且可用（HTTPS **能力**，见 check runner 探测；
      池本无 https 协议类目）。
    - ``total``：池内总条数（不过滤）。

    分桶互斥口径不保证（同一条 http 代理若 https_ok 亦计入 https）——这是有意的：
    https 是**能力叠加**而非协议分类。
    """
    counts = {"http": 0, "socks": 0, "https": 0, "total": len(records)}
    for r in records:
        if not _is_usable(r, prune_latency_ms):
            continue
        proto = (r.get("protocol") or "").lower()
        if proto == "http":
            counts["http"] += 1
        elif proto in SOCKS_PROTOCOLS:
            counts["socks"] += 1
        if r.get("https_ok") is True:
            counts["https"] += 1
    return counts


def auto_fetch_targets(counts: dict, cfg: ProxyConfig) -> list[list[str]]:
    """按阈值判定需补抓的协议参数（纯函数，2026-10-11）。

    返回给 ``fetch(protocols=...)`` 的**协议参数列表**的列表（去重合并）：
    http 不足→``["http"]``；socks 不足→``["socks4","socks5"]``；https 不足→``["http"]``
    （https **不是** fetch 协议，靠 http 抓取后的验证探测补齐；若 http 本身也要补，
    两个 ``["http"]`` 合并为一条）。阈值 ``<=0`` 表示该协议不自动补抓。

    例：http=30/阈 50 且 https=5/阈 20 → ``[["http"]]``（合并）；socks=10/阈 20 →
    ``[["http"], ["socks4","socks5"]]``。
    """
    http_short = cfg.auto_fetch_min_http > 0 and counts.get("http", 0) < cfg.auto_fetch_min_http
    https_short = cfg.auto_fetch_min_https > 0 and counts.get("https", 0) < cfg.auto_fetch_min_https
    socks_short = cfg.auto_fetch_min_socks > 0 and counts.get("socks", 0) < cfg.auto_fetch_min_socks
    targets: list[list[str]] = []
    if http_short or https_short:
        targets.append(["http"])
    if socks_short:
        targets.append(["socks4", "socks5"])
    return targets


def _progress_reporter(on_progress: Callable[[dict], Any] | None):
    """返回 ``report(phase, done, total, message)``：算 ETA 后回调进度 dict。

    ``on_progress`` 为 None 时返 no-op（零开销）。形状对齐 ``core/tools/decompiler.py``
    的进度 dict（``phase``/``done``/``total``/``eta_seconds``/``elapsed_seconds``/
    ``rate_per_second``）。相位切换时重置计时。
    """
    if on_progress is None:
        return lambda *a, **k: None
    state = {"phase": None, "started": 0.0}

    def report(phase: str, done: int, total: int, message: str = "") -> None:
        now = time.time()
        if phase != state["phase"]:
            state["phase"] = phase
            state["started"] = now
        elapsed = now - state["started"]
        rate = done / elapsed if elapsed > 0 and done > 0 else 0.0
        eta = int(round((total - done) / rate)) if rate > 0 and total > done else None
        on_progress({
            "phase": phase, "done": done, "total": total,
            "eta_seconds": eta, "elapsed_seconds": round(elapsed, 1),
            "rate_per_second": round(rate, 3), "message": message,
        })
    return report


# ---------------------------------------------------------------- 子进程

class _Serve:
    """一个项目的 serve 子进程与其控制面句柄。"""

    def __init__(self, proc: subprocess.Popen, http: int, socks5: int, control: int,
                 log_file: Path, job: int | None = None):
        self.proc = proc
        self.http = http
        self.socks5 = socks5
        self.control = control
        # 作业对象句柄（2026-10-12）：KILL_ON_JOB_CLOSE 托管 serve 进程树；后端
        # 进程退出时随句柄关闭自动焚毁，杜绝「后端重启→serve 变孤儿」的历史泄漏。
        self.job = job
        self.started = threading.Event()
        self.start_error: str | None = None
        self.tail: deque[str] = deque(maxlen=200)
        self.log_file = log_file
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._drainer = threading.Thread(target=self._drain_stderr, daemon=True)

    def begin(self) -> None:
        self._reader.start()
        self._drainer.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            self.tail.append(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "started":
                self.started.set()
            elif event.get("event") == "error":
                self.start_error = event.get("message") or "启动失败"
                self.started.set()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            with self.log_file.open("a", encoding="utf-8") as fh:
                for raw in self.proc.stderr:
                    fh.write(raw)
                fh.flush()
        except OSError:
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def endpoint(self) -> dict:
        return {"http": f"127.0.0.1:{self.http}",
                "socks5": f"127.0.0.1:{self.socks5}",
                "control": f"127.0.0.1:{self.control}"}


# ---------------------------------------------------------------- 主服务

class ProxyPool:
    """全局代理池：单份池文件 + 单 serve 子进程 + 控制通道。

    **全局单池（2026-10-11 由项目级改造）**：整机一个 serve、一份 `<data>/proxy/pool.json`，
    所有项目共享同一池（人类 UI 与 AI 经 MCP 控制面取端点自行 `curl -x`）。此前按项目
    各起一个 serve、各占 3 端口、各一份池——池被切碎、抓取/验证重复。旧项目池经
    ``merge_legacy_pools()`` 首启合并入全局。
    """

    def __init__(self, workspace_root: str | Path, tools_root: str | Path,
                 config: ProxyConfig | None = None):
        self.workspace_root = Path(workspace_root)
        self.tools_root = Path(tools_root)
        self.config = config or ProxyConfig()
        self._serve: _Serve | None = None
        self._lock = threading.Lock()
        self.last_active: float = time.monotonic()   # 空闲回收判据（见 idle_timeout_s）

    # ---- 路径与锁

    def data_dir(self) -> Path:
        """全局代理数据目录 `data/proxy/`（与 data/campaign.db 同款全局归位契约，
        **不落 workspaces/**——workspace-hygiene 顶层只允许项目目录 + .trash）。"""
        d = self.workspace_root.parent / "data" / "proxy"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def pool_path(self) -> Path:
        return self.data_dir() / "pool.json"

    def _log_path(self) -> Path:
        return self.data_dir() / "serve.log"

    def _touch(self) -> None:
        """记一次活动（status/start 等公开入口调）——供可选空闲回收判据。"""
        self.last_active = time.monotonic()

    # ---- 池文件

    def read_pool(self) -> list[dict]:
        path = self.pool_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("池文件解析失败，按空池处理: %s", path)
            return []
        if isinstance(data, dict):
            data = data.get("proxies", [])
        return [r for r in data if isinstance(r, dict) and r.get("proxy")] \
            if isinstance(data, list) else []

    def write_pool(self, records: list[dict]) -> None:
        path = self.pool_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    def merge_legacy_pools(self) -> int:
        """首启一次性迁移（2026-10-11）：把旧 per-project 池并入全局池。

        扫 ``<workspace_root>/*/proxy/pool.json``，按 proxy 地址去重并入全局——
        **旧记录不覆盖全局已有同地址记录**（全局优先）。**仅在全局池不存在或为空时**
        执行（避免每次启动重复扫叠加）；旧项目池文件**原地保留不删**（零风险回退）。
        返回并入的记录数（未执行返 0）。坏文件跳过不炸。
        """
        if self.read_pool():
            return 0                                   # 全局池非空：已迁移过或已在用
        merged: dict[str, dict] = {}
        for child in sorted(self.workspace_root.glob("*/proxy/pool.json")):
            try:
                data = json.loads(child.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("旧池文件解析失败，跳过: %s", child)
                continue
            if isinstance(data, dict):
                data = data.get("proxies", [])
            if not isinstance(data, list):
                continue
            for r in data:
                if not isinstance(r, dict) or not r.get("proxy"):
                    continue
                addr = r["proxy"]
                if addr not in merged:                 # 首次出现优先（旧记录保真）
                    r.setdefault("status", "Working")
                    merged[addr] = r
        if not merged:
            return 0
        self.write_pool(list(merged.values()))
        log.info("已合并 %d 条旧项目池记录入全局池 %s", len(merged), self.pool_path())
        return len(merged)

    def add_records(self, records: list[dict]) -> dict:
        """合并入池（按 proxy 地址去重）并热推给运行中的 serve。"""
        with self._lock:
            current = self.read_pool()
            seen = {r.get("proxy") for r in current}
            added = []
            for r in records:
                if not isinstance(r, dict) or not r.get("proxy"):
                    continue
                if r["proxy"] in seen:
                    continue
                r.setdefault("status", "Working")
                current.append(r)
                seen.add(r["proxy"])
                added.append(r)
            self.write_pool(current)
            serve = self._serve
            if added and serve and serve.alive():
                self._control(serve, "POST", "/reload", {"add": added})
            return {"added": len(added), "pool_size": len(current)}

    def remove_records(self, addrs: list[str]) -> dict:
        with self._lock:
            current = self.read_pool()
            wanted = set(addrs)
            kept = [r for r in current if r.get("proxy") not in wanted]
            removed = len(current) - len(kept)
            self.write_pool(kept)
            serve = self._serve
            if removed and serve and serve.alive():
                self._control(serve, "POST", "/reload", {"remove": list(wanted)})
            return {"removed": removed, "pool_size": len(kept)}

    def replace_pool(self, records: list[dict]) -> dict:
        with self._lock:
            old = {r.get("proxy") for r in self.read_pool()}
            self.write_pool(records)
            new = {r.get("proxy") for r in records}
            serve = self._serve
            if serve and serve.alive():
                self._control(serve, "POST", "/reload",
                              {"add": list(new - old), "remove": list(old - new)})
            return {"pool_size": len(records)}

    # ---- 生命周期

    def start(self) -> dict:
        """起 serve（幂等：已在跑直接返状态）。池空/依赖缺失给 ProxyError。"""
        self._touch()
        with self._lock:
            serve = self._serve
            if serve and serve.alive():
                return self.status()
            ok, detail = proxy_available(self.tools_root, self.config.python)
            if not ok:
                raise ProxyError(detail)
            records = [r for r in self.read_pool()
                       if r.get("status", "Working") == "Working"]
            if not records:
                raise ProxyError("代理池为空（无 status=Working 的代理），先抓取或导入")
            py = resolve_python(self.tools_root, self.config.python)
            taken: set[int] = set()
            http = _free_port(self.config.http_port_base, taken)
            socks5 = _free_port(self.config.socks5_port_base, taken)
            control = _free_port(self.config.control_port_base, taken)
            log_file = self._log_path()
            cmd = [py, str(runner_path(self.tools_root).resolve()),
                   "-i", str(self.pool_path().resolve()),
                   "--http-port", str(http), "--socks5-port", str(socks5),
                   "--control-port", str(control),
                   "--region", str(self.config.region),
                   "--request-rotation-count", str(self.config.request_rotation_count),
                   "--target-failover-threshold", str(self.config.target_failover_threshold)]
            if self.config.max_latency_ms is not None:
                cmd += ["--max-latency-ms", str(self.config.max_latency_ms)]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            # 作业对象（2026-10-12）：托管 serve，后端退出即随句柄自动焚毁防孤儿
            job = create_kill_on_close_job()
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(self.tools_root / "fir-proxy"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env, creationflags=_no_window(),
                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            except OSError as exc:
                if job is not None:
                    close_job(job)
                raise ProxyError(f"启动 serve 失败: {exc}") from exc
            if job is not None and not assign_to_job(job, proc):
                close_job(job)  # 纳不进作业：降级为无托管（stop()/杀树仍兜）
                job = None
            new = _Serve(proc, http, socks5, control, log_file, job=job)
            new.begin()
            self._serve = new
            if not new.started.wait(timeout=self.config.startup_timeout_s):
                self._kill(new)
                self._serve = None
                raise ProxyError("serve 启动超时")
            if new.start_error or not new.alive():
                msg = new.start_error or "serve 进程意外退出"
                self._kill(new)
                self._serve = None
                raise ProxyError(msg)
            log.info("代理池已启动（全局）http=%s socks5=%s", http, socks5)
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            serve = self._serve
            self._serve = None
            if not serve:
                return {"running": False}
            if serve.alive():
                try:
                    self._control(serve, "POST", "/stop")
                except ProxyError:
                    pass
                deadline = time.time() + 8
                while serve.alive() and time.time() < deadline:
                    time.sleep(0.2)
            self._kill(serve)
            log.info("代理池已停止（全局）")
            return {"running": False}

    def status(self) -> dict:
        self._touch()
        serve = self._serve
        pool = self.read_pool()
        # counts（2026-10-11）：按协议分桶（http/socks/https 能力），供前端过滤与阈值展示。
        counts = protocol_counts(pool, self.config.prune_latency_ms)
        base = {"running": False, "endpoint": None, "current": None,
                "count": 0, "working": 0, "regions": {},
                "pool_size": len(pool), "counts": counts}
        if not serve or not serve.alive():
            return base
        try:
            data = self._control(serve, "GET", "/status")
        except ProxyError:
            return base
        return {"running": True, "endpoint": serve.endpoint,
                "current": data.get("current"), "count": data.get("count", 0),
                "working": data.get("working", 0), "regions": data.get("regions", {}),
                "uptime_s": data.get("uptime_s"), "pool_size": len(pool),
                "counts": counts}

    def list_proxies(self) -> dict:
        """可用代理列表：运行中取 serve 实时池，否则读池文件。"""
        serve = self._serve
        if serve and serve.alive():
            try:
                data = self._control(serve, "GET", "/proxies")
                return {"source": "serve", "proxies": data.get("proxies", [])}
            except ProxyError:
                pass
        return {"source": "file", "proxies": [_proxy_view(r) for r in self.read_pool()]}

    def rotate(self) -> dict:
        serve = self._serve
        if not serve or not serve.alive():
            raise ProxyError("代理服务未运行，无法轮换")
        return self._control(serve, "POST", "/rotate")

    def use_proxy(self, address: str) -> dict:
        """精确切换到指定代理（服务须在跑）。

        与 ``rotate`` 互补——访问测试判出对目标站点可达的 IP 后直接钉死它，
        不再靠顺序轮换碰运气。地址不在池/非 Working 由 serve 侧返 ``ok:False``
        （这里照样抛 ProxyError，前端统一按错误提示）。
        """
        serve = self._serve
        if not serve or not serve.alive():
            raise ProxyError("代理服务未运行，无法切换代理")
        target = str(address or "").strip()
        if not target:
            raise ProxyError("缺少要切换的代理地址")
        out = self._control(serve, "POST", "/select", body={"proxy": target})
        # serve 侧对「不在池/非 Working」返 ok:False（HTTP 200）——这里统一翻成
        # ProxyError，让 API 层走 503 + 人话错误，前端 guard() 直显
        if not out.get("ok", True):
            raise ProxyError(out.get("error") or f"切换失败: {target}")
        return out

    def select(self, limit: int = 1, region: str | None = None,
               max_latency_ms: float | None = None,
               include_failed: bool = False) -> list[dict]:
        return select_records(self.read_pool(), limit=limit, region=region,
                              max_latency_ms=max_latency_ms,
                              include_failed=include_failed)

    # ---- 抓取 / 验证（子进程调平台 fetch / check runner）

    def _exec_blocking(self, cmd: list[str], timeout: float, env: dict) -> None:
        """跑子进程等它结束（无进度）；rc≠0 / 超时抛 ProxyError。"""
        try:
            proc = subprocess.run(cmd, cwd=str(self.tools_root / "fir-proxy"),
                                  capture_output=True, timeout=timeout, env=env,
                                  creationflags=_no_window(),
                                  text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise ProxyError(f"fir-proxy 命令超时（{timeout:.0f}s）") from exc
        if proc.returncode != 0:
            tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise ProxyError(f"fir-proxy 命令失败(rc={proc.returncode}): {tail[0][:300]}")

    def _exec_streaming(self, cmd: list[str], timeout: float, env: dict,
                        on_progress: Callable[[dict], Any]) -> None:
        """跑子进程并逐行读 stdout：``{"event":"progress",...}`` 行进回调，其余进 tail。

        超时用定时器到点 kill（与 ``_exec_blocking`` 同文案）。
        """
        proc = subprocess.Popen(cmd, cwd=str(self.tools_root / "fir-proxy"),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, creationflags=_no_window(),
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            try:
                proc.kill()
            except OSError:
                pass

        timer = threading.Timer(timeout, _kill)
        timer.start()
        tail: list[str] = []
        try:
            assert proc.stdout is not None
            while True:
                raw = proc.stdout.readline()      # 逐行读，保证进度实时（勿用 for-in 迭代）
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    tail = (tail + [line])[-5:]
                    continue
                if isinstance(msg, dict) and msg.get("event") == "progress":
                    try:
                        on_progress(msg)
                    except Exception:  # noqa: BLE001 —— 回调异常不得中断子进程
                        log.exception("代理进度回调失败")
                else:
                    tail = (tail + [line])[-5:]
            proc.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise ProxyError(f"fir-proxy 命令超时（{timeout:.0f}s）")
        if proc.returncode != 0:
            raise ProxyError(f"fir-proxy 命令失败(rc={proc.returncode}): "
                             f"{(tail[-1] if tail else '')[:300]}")

    def _run_cli(self, args: list[str], timeout: float,
                 script: Path | None = None,
                 on_progress: Callable[[dict], Any] | None = None,
                 as_dict: bool = False):
        ok, detail = proxy_available(self.tools_root, self.config.python)
        if not ok:
            raise ProxyError(detail)
        py = resolve_python(self.tools_root, self.config.python)
        target = script or cli_path(self.tools_root)
        if not target.exists():
            raise ProxyError(f"fir-proxy 脚本缺失: {target}")
        out_file = self.data_dir() / f"cli-{uuid.uuid4().hex[:8]}.json"
        cmd = [py, str(target.resolve()), *args,
               "-o", str(out_file.resolve()), "--output-format", "json", "--quiet"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        if on_progress is None:
            self._exec_blocking(cmd, timeout, env)
        else:
            self._exec_streaming(cmd, timeout, env, on_progress)
        if not out_file.exists():
            return {} if as_dict else []
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {} if as_dict else []
        finally:
            try:
                out_file.unlink()
            except OSError:
                pass
        if as_dict:
            # check-url 模式（2026-10-12）：runner 顶层是 {url,baseline,results}，
            # 不做键下钻（下钻到 results 会丢 baseline）。
            return data if isinstance(data, dict) else {}
        if isinstance(data, dict):
            for key in ("proxies", "results", "data"):
                if key in data:
                    data = data[key]
                    break
        return [r for r in data if isinstance(r, dict) and r.get("proxy")] \
            if isinstance(data, list) else []

    def _sources_file(self) -> Path | None:
        """解析源清单覆盖路径（存在才返回，供 --sources 传绝对路径）。"""
        candidate = Path(self.config.sources_file or "config/proxy_sources.json")
        return candidate if candidate.exists() else None

    def _validate_records(self, records: list[dict],
                          timeout: float, workers: int,
                          on_progress: Callable[[dict], Any] | None = None) -> list[dict]:
        """调 check runner 做单目标连通性验证，返回带结果的记录（含 Failed）。空批次返 []。"""
        if not records:
            return []
        in_file = self.data_dir() / f"validate-in-{uuid.uuid4().hex[:8]}.json"
        in_file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        try:
            args = ["-i", str(in_file.resolve()),
                    "--target", self.config.validate_target,
                    "--timeout", str(int(self.config.validate_timeout_s)),
                    "--workers", str(workers)]
            if self.config.validate_target_https:   # HTTPS 能力探测（可选）
                args += ["--target-https", self.config.validate_target_https]
            if self.config.validate_target_b:        # 内容注入交叉验证（可选）
                args += ["--target-b", self.config.validate_target_b]
            return self._run_cli(
                args,
                timeout,
                script=runner_check_path(self.tools_root),
                on_progress=on_progress)
        finally:
            try:
                in_file.unlink()
            except OSError:
                pass

    def _apply_validation(self, validated: list[dict], prune: bool) -> dict:
        """把验证结果按 proxy 地址 merge 回池，按需剔除失效。

        替代整体 ``replace_pool``：本次未验证到的记录保留原样（runner 少返回不误删）、
        并发新增的记录不被覆盖（锁内重读）。
        """
        threshold = self.config.prune_latency_ms
        merge_keys = ("status", "latency", "speed", "anonymity", "location", "score",
                      "https_ok", "hijacked")
        by_addr = {r.get("proxy"): r for r in validated if r.get("proxy")}
        with self._lock:
            current = self.read_pool()
            kept: list[dict] = []
            removed: list[str] = []
            for record in current:
                addr = record.get("proxy")
                fresh = by_addr.get(addr)
                if fresh is None:
                    kept.append(record)                 # 本次未验证到：保留原样
                    continue
                if prune and not _is_usable(fresh, threshold):
                    removed.append(addr)
                    continue
                merged = dict(record)
                for key in merge_keys:
                    if key in fresh:
                        merged[key] = fresh[key]
                kept.append(merged)
            self.write_pool(kept)
            serve = self._serve
            if removed and serve and serve.alive():
                self._control(serve, "POST", "/reload", {"remove": removed})
        working = sum(1 for r in kept if _is_usable(r, threshold))
        return {"validated": len(validated), "working": working,
                "removed": len(removed), "pool_size": len(kept)}

    def check_url(self, url: str, *, workers: int = 50, timeout: float = 600.0,
                  limit: int = 10, probe_waf: bool = True,
                  on_progress: Callable[[dict], Any] | None = None) -> dict:
        """指定网站可达性 / WAF 封禁检测（2026-10-12）。

        用池内**全部**代理逐一 GET ``url``，与平台**直连基线**对比判定「可达 / 被该站
        封禁」；只读报告，**绝不写池**（换目标就误删代理无意义）。返回：

        ``{"url","total","reachable","blocked","usable":[...],"results":[...],"baseline":{...}}``
        ——``usable`` = 未被封禁且有响应的代理，按延迟升序取前 ``limit`` 个
        （``{proxy,protocol,latency}``），供 AI 直接使用。
        """
        url = (url or "").strip()
        if not url:
            raise ProxyError("目标 URL 不能为空")
        pool = self.read_pool()
        if not pool:
            raise ProxyError("代理池为空，无可用代理")
        report = _progress_reporter(on_progress)
        report("check-url", 0, len(pool), "检测中…")
        in_file = self.data_dir() / f"checkurl-in-{uuid.uuid4().hex[:8]}.json"
        in_file.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
        try:
            args = ["-i", str(in_file.resolve()), "--url", url,
                    "--timeout", str(int(self.config.validate_timeout_s)),
                    "--workers", str(workers)]
            args += ["--probe-waf"] if probe_waf else ["--no-probe-waf"]
            raw = self._run_cli(
                args, timeout,
                script=runner_check_path(self.tools_root),
                on_progress=lambda evt: report(
                    "check-url", int(evt.get("done") or 0), int(evt.get("total") or 0),
                    f"检测中 {evt.get('done')}/{evt.get('total')}"),
                as_dict=True)
        finally:
            try:
                in_file.unlink()
            except OSError:
                pass

        results = []
        baseline = None
        if isinstance(raw, dict):
            results = raw.get("results") or []
            baseline = raw.get("baseline")
        results = [r for r in results if isinstance(r, dict)]
        baseline_valid = bool((baseline or {}).get("ok"))
        for row in results:
            confirmation = row.get("confirmation") or {}
            if confirmation.get("confirmed") is False:
                if row.get("blocked") is True:
                    row["blocked"] = False
                    row["reason"] = f"二出口复测未复现异常；{row.get('reason', '')}".strip()
                elif row.get("ok") is False and confirmation.get("ok") is True:
                    row["ok"] = True
                    row["status_code"] = confirmation.get("status_code")
                    row["latency"] = confirmation.get("latency")
                    row["reason"] = "第二出口复测可达，首出口异常未确认"
        reachable = [r for r in results if r.get("ok")]
        blocked = [r for r in results if r.get("blocked") is True]
        usable = sorted(
            (r for r in results if baseline_valid and r.get("ok") and r.get("blocked") is False),
            key=lambda r: (r.get("latency") if isinstance(r.get("latency"), (int, float))
                           else float("inf")))
        report("check-url", len(results), len(results), "完成")
        return {
            "url": url,
            "total": len(results),
            "reachable": len(reachable),
            "blocked": len(blocked) if baseline_valid else None,
            "usable": [{"proxy": r.get("proxy"), "protocol": r.get("protocol"),
                        "latency": r.get("latency"), "status_code": r.get("status_code")}
                       for r in usable[: max(0, limit)]],
            "verdict": "comparable" if baseline_valid else "indeterminate",
            "baseline_valid": baseline_valid,
            "results": results,
            "baseline": baseline,
        }

    def fetch(self, protocols: list[str] | None = None,
              timeout: float = 600.0, auto_validate: bool = True,
              validate_timeout: float = 1200.0, workers: int = 100,
              limit: int | None = None,
              on_progress: Callable[[dict], Any] | None = None) -> dict:
        """从在线源抓取入池（受 ``limit``/``fetch_limit`` 限量）；默认抓取后自动验证并剔除失效。

        ``limit``：**本次**抓取条数上限，覆盖 ``config.fetch_limit``（``None``=用配置值；
        ``0``=不限）。前端「抓取条数」输入框经 API 的 ``limit`` 逐次传入，不改配置。

        ``on_progress(dict)`` 汇报 fetch/check/prune 相位进度与 ETA（形状见 ``_progress_reporter``）。
        验证失败（子进程错/超时/无结果）直接抛 ``ProxyError``——不静默塞未验证记录。
        """
        report = _progress_reporter(on_progress)
        report("fetch", 0, 0, "抓取中…")
        args: list[str] = []
        for proto in protocols or []:
            args += ["--protocol", proto]
        effective_limit = self.config.fetch_limit if limit is None else int(limit)
        if effective_limit:
            args += ["--limit", str(effective_limit)]
        sources = self._sources_file()
        if sources is not None:
            args += ["--sources", str(sources.resolve())]
        records = self._run_cli(
            args, timeout, script=runner_fetch_path(self.tools_root),
            on_progress=lambda evt: report(
                "fetch", int(evt.get("done") or 0), int(evt.get("total") or 0),
                f"抓取中 {evt.get('done')}/{evt.get('total')}"))
        fetched = len(records)
        report("fetch", 1, 1, f"抓取完成，共 {fetched} 个")
        if not auto_validate:
            result = self.add_records(records)
            return {"fetched": fetched, "validated": 0, "removed": 0, **result}
        if not records:
            return {"fetched": 0, "validated": 0, "removed": 0,
                    "added": 0, "pool_size": len(self.read_pool())}
        validated = self._validate_records(
            records, validate_timeout, workers,
            on_progress=lambda evt: report(
                "check", int(evt.get("done") or 0), int(evt.get("total") or 0),
                f"验证中 {evt.get('done')}/{evt.get('total')}"))
        if not validated:
            raise ProxyError("抓取后自动验证未返回结果（已放弃入池，未静默降级）")
        report("prune", 1, 1, "清理失效…")
        keep, dropped = _prune(validated, self.config.prune_latency_ms)
        result = self.add_records(keep)
        return {"fetched": fetched, "validated": len(validated),
                "removed": len(dropped), **result}

    def validate(self, timeout: float = 1200.0,
                 workers: int = 100, prune: bool = True,
                 on_progress: Callable[[dict], Any] | None = None) -> dict:
        """验证池内全部代理（单目标连通性），回写 status/latency 并（默认）剔除失效。

        2026-10-15：并发 50→100、子进程超时 600→1200s——抓取量提到数千条后，
        原参数在「大量死代理各等满 validate_timeout_s 超时」时会撞 600s 上限。
        """
        report = _progress_reporter(on_progress)
        pool = self.read_pool()
        if not pool:
            raise ProxyError("代理池为空，无可验证代理")
        report("check", 0, len(pool), "验证中…")
        records = self._validate_records(
            pool, timeout, workers,
            on_progress=lambda evt: report(
                "check", int(evt.get("done") or 0), int(evt.get("total") or 0),
                f"验证中 {evt.get('done')}/{evt.get('total')}"))
        if not records:
            return {"validated": 0, "working": len(pool),
                    "removed": 0, "pool_size": len(pool)}
        report("prune", 1, 1, "清理失效…")
        return self._apply_validation(records, prune)

    # ---- 清理

    def close_all(self) -> None:
        """平台 shutdown 兜关：停全局 serve（幂等）。"""
        try:
            self.stop()
        except Exception:  # noqa: BLE001
            log.exception("停止代理池失败（全局）")

    # ---- 控制通道

    def _control(self, serve: _Serve, method: str, path: str,
                 body: dict | None = None) -> dict:
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{serve.control}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTP 层错误（404/500…）绝不能吞成「控制通道不可达」——那会让人
            # 以为 serve 挂了。最常见场景：serve 是更新平台前启动的旧版，不认
            # /select（404 unknown path），对应用「重启代理服务」而不是查网络。
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            if exc.code == 404 and path == "/select":
                raise ProxyError(
                    "代理服务是旧版启动的，不支持精确切换——"
                    "请先「停止服务」再「启动服务」重新拉起") from exc
            raise ProxyError(
                f"代理控制通道 HTTP {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProxyError(f"代理控制通道不可达: {exc}") from exc

    @staticmethod
    def _kill(serve: _Serve) -> None:
        try:
            if serve.alive():
                serve.proc.terminate()
                try:
                    serve.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    serve.proc.kill()
        except OSError:
            pass
        finally:
            # 释放作业句柄：KILL_ON_JOB_CLOSE 下关闭即兜底焚毁 serve 进程树
            if getattr(serve, "job", None) is not None:
                close_job(serve.job)
                serve.job = None
