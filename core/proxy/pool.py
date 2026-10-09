"""代理池服务（fir-proxy 托管）。

平台侧项目级常驻服务，仿 ``core/browser/pool.py`` 的 ``BrowserPool``：
每项目一个实例，拥有 fir-proxy serve runner 子进程的生命周期，经 **loopback 控制通道**
查询状态 / 手动轮换 / 热增删代理；池记录落项目工作区 ``proxy/pool.json``。

设计要点见 ``docs/plans/proxy-pool-integration.md``：
- **平台托管**：serve 不随会话/MCP 生死，人类 UI 与 AI 共享同一池，重启可恢复。
- **不动上游**：包 fir-proxy 的 ``ProxyRotator``/``ProxyServer``（``tools/fir-proxy/cyberstrike_serve.py``），
  不改 fir-proxy 自身文件。
- **端口**：三端口（http/socks5/control）按项目**动态取空闲口**，多项目不冲突。
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
    validate_timeout_s: int = 5          # 单代理验证超时（check runner --timeout）
    validate_target: str = "http://www.baidu.com"  # 验证目标 URL（经代理 GET 它，通即 Working）
    prune_latency_ms: float | None = 5000.0  # 验证后按延迟剔除阈值（ms）；None/0=不按延迟删
    sources_file: str | None = None      # 抓取源清单覆盖路径；None=默认 config/proxy_sources.json（不存在则用 runner 内置）
    fetch_limit: int = 2000              # 单次抓取条数上限（按协议轮转取）；0=不限

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
                    "anonymity", "score", "status")


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
                 log_file: Path):
        self.proc = proc
        self.http = http
        self.socks5 = socks5
        self.control = control
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
    """项目级代理池：池文件 + serve 子进程 + 控制通道。"""

    def __init__(self, workspace_root: str | Path, tools_root: str | Path,
                 config: ProxyConfig | None = None):
        self.workspace_root = Path(workspace_root)
        self.tools_root = Path(tools_root)
        self.config = config or ProxyConfig()
        self._serves: dict[str, _Serve] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()

    # ---- 路径与锁

    def project_dir(self, pid: str) -> Path:
        d = self.workspace_root / pid / "proxy"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def pool_path(self, pid: str) -> Path:
        return self.project_dir(pid) / "pool.json"

    def _lock(self, pid: str) -> threading.Lock:
        with self._lock_guard:
            return self._locks.setdefault(pid, threading.Lock())

    # ---- 池文件

    def read_pool(self, pid: str) -> list[dict]:
        path = self.pool_path(pid)
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

    def write_pool(self, pid: str, records: list[dict]) -> None:
        path = self.pool_path(pid)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    def add_records(self, pid: str, records: list[dict]) -> dict:
        """合并入池（按 proxy 地址去重）并热推给运行中的 serve。"""
        with self._lock(pid):
            current = self.read_pool(pid)
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
            self.write_pool(pid, current)
            serve = self._serves.get(pid)
            if added and serve and serve.alive():
                self._control(serve, "POST", "/reload", {"add": added})
            return {"added": len(added), "pool_size": len(current)}

    def remove_records(self, pid: str, addrs: list[str]) -> dict:
        with self._lock(pid):
            current = self.read_pool(pid)
            wanted = set(addrs)
            kept = [r for r in current if r.get("proxy") not in wanted]
            removed = len(current) - len(kept)
            self.write_pool(pid, kept)
            serve = self._serves.get(pid)
            if removed and serve and serve.alive():
                self._control(serve, "POST", "/reload", {"remove": list(wanted)})
            return {"removed": removed, "pool_size": len(kept)}

    def replace_pool(self, pid: str, records: list[dict]) -> dict:
        with self._lock(pid):
            old = {r.get("proxy") for r in self.read_pool(pid)}
            self.write_pool(pid, records)
            new = {r.get("proxy") for r in records}
            serve = self._serves.get(pid)
            if serve and serve.alive():
                self._control(serve, "POST", "/reload",
                              {"add": list(new - old), "remove": list(old - new)})
            return {"pool_size": len(records)}

    # ---- 生命周期

    def start(self, pid: str) -> dict:
        """起 serve（幂等：已在跑直接返状态）。池空/依赖缺失给 ProxyError。"""
        with self._lock(pid):
            serve = self._serves.get(pid)
            if serve and serve.alive():
                return self.status(pid)
            ok, detail = proxy_available(self.tools_root, self.config.python)
            if not ok:
                raise ProxyError(detail)
            records = [r for r in self.read_pool(pid)
                       if r.get("status", "Working") == "Working"]
            if not records:
                raise ProxyError("代理池为空（无 status=Working 的代理），先抓取或导入")
            py = resolve_python(self.tools_root, self.config.python)
            taken: set[int] = set()
            http = _free_port(self.config.http_port_base, taken)
            socks5 = _free_port(self.config.socks5_port_base, taken)
            control = _free_port(self.config.control_port_base, taken)
            log_file = self.project_dir(pid) / "serve.log"
            cmd = [py, str(runner_path(self.tools_root).resolve()),
                   "-i", str(self.pool_path(pid).resolve()),
                   "--http-port", str(http), "--socks5-port", str(socks5),
                   "--control-port", str(control),
                   "--region", str(self.config.region),
                   "--request-rotation-count", str(self.config.request_rotation_count),
                   "--target-failover-threshold", str(self.config.target_failover_threshold)]
            if self.config.max_latency_ms is not None:
                cmd += ["--max-latency-ms", str(self.config.max_latency_ms)]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(self.tools_root / "fir-proxy"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env, creationflags=_no_window(),
                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            except OSError as exc:
                raise ProxyError(f"启动 serve 失败: {exc}") from exc
            new = _Serve(proc, http, socks5, control, log_file)
            new.begin()
            self._serves[pid] = new
            if not new.started.wait(timeout=self.config.startup_timeout_s):
                self._kill(new)
                self._serves.pop(pid, None)
                raise ProxyError("serve 启动超时")
            if new.start_error or not new.alive():
                msg = new.start_error or "serve 进程意外退出"
                self._kill(new)
                self._serves.pop(pid, None)
                raise ProxyError(msg)
            log.info("代理池已启动 pid=%s http=%s socks5=%s", pid, http, socks5)
            return self.status(pid)

    def stop(self, pid: str) -> dict:
        with self._lock(pid):
            serve = self._serves.pop(pid, None)
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
            log.info("代理池已停止 pid=%s", pid)
            return {"running": False}

    def status(self, pid: str) -> dict:
        serve = self._serves.get(pid)
        pool = self.read_pool(pid)
        base = {"running": False, "endpoint": None, "current": None,
                "count": 0, "working": 0, "regions": {},
                "pool_size": len(pool)}
        if not serve or not serve.alive():
            return base
        try:
            data = self._control(serve, "GET", "/status")
        except ProxyError:
            return base
        return {"running": True, "endpoint": serve.endpoint,
                "current": data.get("current"), "count": data.get("count", 0),
                "working": data.get("working", 0), "regions": data.get("regions", {}),
                "uptime_s": data.get("uptime_s"), "pool_size": len(pool)}

    def list_proxies(self, pid: str) -> dict:
        """可用代理列表：运行中取 serve 实时池，否则读池文件。"""
        serve = self._serves.get(pid)
        if serve and serve.alive():
            try:
                data = self._control(serve, "GET", "/proxies")
                return {"source": "serve", "proxies": data.get("proxies", [])}
            except ProxyError:
                pass
        return {"source": "file", "proxies": [_proxy_view(r) for r in self.read_pool(pid)]}

    def rotate(self, pid: str) -> dict:
        serve = self._serves.get(pid)
        if not serve or not serve.alive():
            raise ProxyError("代理服务未运行，无法轮换")
        return self._control(serve, "POST", "/rotate")

    def select(self, pid: str, limit: int = 1, region: str | None = None,
               max_latency_ms: float | None = None,
               include_failed: bool = False) -> list[dict]:
        return select_records(self.read_pool(pid), limit=limit, region=region,
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

    def _run_cli(self, pid: str, args: list[str], timeout: float,
                 script: Path | None = None,
                 on_progress: Callable[[dict], Any] | None = None) -> list[dict]:
        ok, detail = proxy_available(self.tools_root, self.config.python)
        if not ok:
            raise ProxyError(detail)
        py = resolve_python(self.tools_root, self.config.python)
        target = script or cli_path(self.tools_root)
        if not target.exists():
            raise ProxyError(f"fir-proxy 脚本缺失: {target}")
        out_file = self.project_dir(pid) / f"cli-{uuid.uuid4().hex[:8]}.json"
        cmd = [py, str(target.resolve()), *args,
               "-o", str(out_file.resolve()), "--output-format", "json", "--quiet"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        if on_progress is None:
            self._exec_blocking(cmd, timeout, env)
        else:
            self._exec_streaming(cmd, timeout, env, on_progress)
        if not out_file.exists():
            return []
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        finally:
            try:
                out_file.unlink()
            except OSError:
                pass
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

    def _validate_records(self, pid: str, records: list[dict],
                          timeout: float, workers: int,
                          on_progress: Callable[[dict], Any] | None = None) -> list[dict]:
        """调 check runner 做单目标连通性验证，返回带结果的记录（含 Failed）。空批次返 []。"""
        if not records:
            return []
        in_file = self.project_dir(pid) / f"validate-in-{uuid.uuid4().hex[:8]}.json"
        in_file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        try:
            return self._run_cli(
                pid,
                ["-i", str(in_file.resolve()),
                 "--target", self.config.validate_target,
                 "--timeout", str(int(self.config.validate_timeout_s)),
                 "--workers", str(workers)],
                timeout,
                script=runner_check_path(self.tools_root),
                on_progress=on_progress)
        finally:
            try:
                in_file.unlink()
            except OSError:
                pass

    def _apply_validation(self, pid: str, validated: list[dict], prune: bool) -> dict:
        """把验证结果按 proxy 地址 merge 回池，按需剔除失效。

        替代整体 ``replace_pool``：本次未验证到的记录保留原样（runner 少返回不误删）、
        并发新增的记录不被覆盖（锁内重读）。
        """
        threshold = self.config.prune_latency_ms
        merge_keys = ("status", "latency", "speed", "anonymity", "location", "score")
        by_addr = {r.get("proxy"): r for r in validated if r.get("proxy")}
        with self._lock(pid):
            current = self.read_pool(pid)
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
            self.write_pool(pid, kept)
            serve = self._serves.get(pid)
            if removed and serve and serve.alive():
                self._control(serve, "POST", "/reload", {"remove": removed})
        working = sum(1 for r in kept if _is_usable(r, threshold))
        return {"validated": len(validated), "working": working,
                "removed": len(removed), "pool_size": len(kept)}

    def fetch(self, pid: str, protocols: list[str] | None = None,
              timeout: float = 600.0, auto_validate: bool = True,
              validate_timeout: float = 600.0, workers: int = 50,
              on_progress: Callable[[dict], Any] | None = None) -> dict:
        """从在线源抓取入池（受 ``fetch_limit`` 限量）；默认抓取后自动验证并剔除失效。

        ``on_progress(dict)`` 汇报 fetch/check/prune 相位进度与 ETA（形状见 ``_progress_reporter``）。
        验证失败（子进程错/超时/无结果）直接抛 ``ProxyError``——不静默塞未验证记录。
        """
        report = _progress_reporter(on_progress)
        report("fetch", 0, 0, "抓取中…")
        args: list[str] = []
        for proto in protocols or []:
            args += ["--protocol", proto]
        if self.config.fetch_limit:
            args += ["--limit", str(int(self.config.fetch_limit))]
        sources = self._sources_file()
        if sources is not None:
            args += ["--sources", str(sources.resolve())]
        records = self._run_cli(
            pid, args, timeout, script=runner_fetch_path(self.tools_root),
            on_progress=lambda evt: report(
                "fetch", int(evt.get("done") or 0), int(evt.get("total") or 0),
                f"抓取中 {evt.get('done')}/{evt.get('total')}"))
        fetched = len(records)
        report("fetch", 1, 1, f"抓取完成，共 {fetched} 个")
        if not auto_validate:
            result = self.add_records(pid, records)
            return {"fetched": fetched, "validated": 0, "removed": 0, **result}
        if not records:
            return {"fetched": 0, "validated": 0, "removed": 0,
                    "added": 0, "pool_size": len(self.read_pool(pid))}
        validated = self._validate_records(
            pid, records, validate_timeout, workers,
            on_progress=lambda evt: report(
                "check", int(evt.get("done") or 0), int(evt.get("total") or 0),
                f"验证中 {evt.get('done')}/{evt.get('total')}"))
        if not validated:
            raise ProxyError("抓取后自动验证未返回结果（已放弃入池，未静默降级）")
        report("prune", 1, 1, "清理失效…")
        keep, dropped = _prune(validated, self.config.prune_latency_ms)
        result = self.add_records(pid, keep)
        return {"fetched": fetched, "validated": len(validated),
                "removed": len(dropped), **result}

    def validate(self, pid: str, timeout: float = 600.0,
                 workers: int = 50, prune: bool = True,
                 on_progress: Callable[[dict], Any] | None = None) -> dict:
        """验证池内全部代理（单目标连通性），回写 status/latency 并（默认）剔除失效。"""
        report = _progress_reporter(on_progress)
        pool = self.read_pool(pid)
        if not pool:
            raise ProxyError("代理池为空，无可验证代理")
        report("check", 0, len(pool), "验证中…")
        records = self._validate_records(
            pid, pool, timeout, workers,
            on_progress=lambda evt: report(
                "check", int(evt.get("done") or 0), int(evt.get("total") or 0),
                f"验证中 {evt.get('done')}/{evt.get('total')}"))
        if not records:
            return {"validated": 0, "working": len(pool),
                    "removed": 0, "pool_size": len(pool)}
        report("prune", 1, 1, "清理失效…")
        return self._apply_validation(pid, records, prune)

    # ---- 清理

    def close_project(self, pid: str) -> None:
        try:
            self.stop(pid)
        except Exception:  # noqa: BLE001
            log.exception("停止代理池失败 pid=%s", pid)
        with self._lock_guard:
            self._locks.pop(pid, None)

    def close_all(self) -> None:
        for pid in list(self._serves.keys()):
            self.close_project(pid)

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
