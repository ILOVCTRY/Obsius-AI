"""IDA-MCP 实例生命周期管理器（2026-09-20，DESIGN.md §7「按需拉起 + 空闲关」定稿）。

按需拉起无窗口 idat（``-A -S bootstrap_mcp.py <port>``）并连其 MCP http 端点：

- **触发**：Agent 侧 decompile/xrefs 点查需要 MCP 且对应样本无在线实例时
  （DecompilerService 经 mcp_provider 回调进到这里）；headless 缓存管线不动。
- **复用**：同项目同样本（pid, sha）后续任务直接复用在线实例。
- **回收**：空闲超时自动 kill（reaper 线程）+ 实例上限 LRU + 平台 shutdown 兜关。
- **红线守恒**：只连 127.0.0.1 loopback；每个实例只服务自己打开的那个库，绝
  不用 MCP 全量列表替换 headless 缓存；任何失败一律返回 None 走 headless 降级，
  绝不抛 500。
- **db 复用**：实例库与 headless 分诊同一落盘（db_dir/sha），命名互通；拉起
  前查 IDA 锁文件，GUI 开着该库时绝不抢。
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.tools.decompiler import (
    MCP_PROBE_TIMEOUT,
    DB_LOCK_EXTS,
    MCPBackend,
    resolve_ida_headless,
    sha256_file,
)
from core.runtime.backends import _kill_tree

_IDA_BOOTSTRAP = (Path(__file__).resolve().parents[2] / "tools" / "mcp"
                  / "ida-pro-mcp" / "bootstrap_mcp.py")


def _popen_detached(args: list[str], env: dict[str, str] | None = None) -> subprocess.Popen:
    """分离式拉起：平台退出不连坐（同 /open 端点惯例，进程句柄仍由本类持有）。"""
    if os.name == "nt":
        return subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS
                                | subprocess.CREATE_NEW_PROCESS_GROUP,
                                env=env, close_fds=False)
    return subprocess.Popen(args, start_new_session=True, env=env, close_fds=False)


@dataclass
class _Instance:
    project_id: str
    sha: str
    port: int
    endpoint: str
    proc: Any
    started_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)


class IdaMcpManager:
    """每 (project_id, sha) 一个 idat 实例；线程安全。测试经 popen/probe 注入。"""

    def __init__(self, *, bootstrap: str | Path | None = None,
                 db_dir: str | Path | None = None,
                 idat_cmd: str | None = None,
                 port_base: int = 13338, port_span: int = 32,
                 idle_timeout: float = 600.0, max_instances: int = 2,
                 ready_timeout: float = 180.0, probe_interval: float = 1.0,
                 probe_timeout: float = MCP_PROBE_TIMEOUT,
                 reaper_interval: float = 30.0,
                 popen: Callable[[list[str]], Any] | None = None,
                 probe: Callable[[str], bool] | None = None):
        self.bootstrap = Path(bootstrap) if bootstrap else _IDA_BOOTSTRAP
        self.db_dir = Path(db_dir) if db_dir else None
        self.idat_cmd = idat_cmd          # None=resolve_ida_headless() 探测
        self.port_base = port_base
        self.port_span = port_span
        self.idle_timeout = idle_timeout
        self.max_instances = max(1, max_instances)
        self.ready_timeout = ready_timeout
        self.probe_interval = probe_interval
        self.probe_timeout = probe_timeout
        self.reaper_interval = reaper_interval
        self._popen_fn = popen or _popen_detached
        self._probe_fn = probe            # None=MCPBackend 真探活
        self._lock = threading.Lock()
        self._instances: dict[tuple[str, str], _Instance] = {}
        self._reaper: threading.Thread | None = None
        self._stopped = False

    # ---------- 对内（DecompilerService / app.py） ----------

    def ensure(self, project_id: str, binary: str, *,
               db_dir: str | Path | None = None) -> str | None:
        """取/拉起该样本的 MCP 实例端点；失败一律 None（调用方 headless 降级）。

        db_dir 按项目传（样本库与 headless 分诊同落盘）；缺省用构造时的全局值。
        """
        if self._stopped:
            return None
        idat = self.idat_cmd or resolve_ida_headless()
        if not idat or not self.bootstrap.is_file() or " " in str(self.bootstrap):
            return None  # -S 不带引号，路径含空格无法传（项目根不含空格的既定布局）
        try:
            sha = sha256_file(binary)
        except OSError:
            return None
        key = (project_id, sha)
        with self._lock:
            inst = self._instances.get(key)
            if inst is not None:
                inst.last_used = time.monotonic()
                endpoint = inst.endpoint
        if inst is not None:
            if self._probe(endpoint):
                return endpoint
            self._drop(key, kill=True)   # 进程死了：清掉重新拉起
        self._start_reaper()
        return self._launch(key, idat, binary, db_dir or self.db_dir)

    def shutdown_all(self) -> None:
        """平台 shutdown 兜关（shutdown 钩子）；此后 ensure 拒绝新拉起。"""
        self._stopped = True
        with self._lock:
            items = list(self._instances.items())
            self._instances.clear()
        for _key, inst in items:
            self._kill(inst)

    def online_for_project(self, project_id: str) -> bool:
        """overview 三态灯用：该项目名下有无真探活在线的实例（懒缓存交给
        MCPBackend 自己的 TTL）。"""
        with self._lock:
            insts = [i for i in self._instances.values()
                     if i.project_id == project_id]
        return any(self._probe(i.endpoint) for i in insts)

    # ---------- 拉起 / 回收 ----------

    def _launch(self, key: tuple[str, str], idat: str, binary: str,
                db_dir: Path | None) -> str | None:
        project_id, sha = key
        db_dir = Path(db_dir) if db_dir else None
        if db_dir is not None and any(
                (db_dir / sha).with_suffix(ext).exists() for ext in DB_LOCK_EXTS):
            return None  # IDA GUI 正开着该库：绝不抢（同 headless 写回的锁语义）
        port = self._free_port()
        if port is None:
            return None
        db_stem = (db_dir / sha) if db_dir else Path(binary).with_suffix("")
        if self.db_dir is not None:
            db_stem.parent.mkdir(parents=True, exist_ok=True)
        # -S 必须是无空格单参数（同 app.py /open 惯例规避 list2cmdline 引号转义坑：
        # -S"script port" 经转义后 IDA 自家解析器不认，实测 could not locate file）；
        # 端口改走环境变量传给 bootstrap（-S 带参形态只留给手工验证）
        args = [idat, "-A", f"-S{self.bootstrap}", f"-o{db_stem}", str(binary)]
        env = {**os.environ, "CYBERSTRIKE_IDA_MCP_PORT": str(port)}
        try:
            proc = self._popen_fn(args, env=env)
        except Exception:  # noqa: BLE001 —— 拉起失败不抛，降级 headless
            return None
        inst = _Instance(project_id=project_id, sha=sha, port=port,
                         endpoint=f"http://127.0.0.1:{port}/mcp", proc=proc)
        with self._lock:
            self._instances[key] = inst
            self._evict_locked()
        # 就绪等待：首次自动分析分钟级，等到 ready_timeout；超时杀掉——留一个
        # 半死实例只会和后续 headless 导出抢同一份 db，回收比留观干净
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if self._probe(inst.endpoint):
                return inst.endpoint
            time.sleep(self.probe_interval)
        with self._lock:
            self._instances.pop(key, None)
        self._kill(inst)
        return None

    def _free_port(self) -> int | None:
        with self._lock:
            taken = {i.port for i in self._instances.values()}
        for port in range(self.port_base, self.port_base + self.port_span):
            if port in taken:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        return None

    def _drop(self, key: tuple[str, str], *, kill: bool) -> None:
        with self._lock:
            inst = self._instances.pop(key, None)
        if inst is not None and kill:
            self._kill(inst)

    @staticmethod
    def _kill(inst: _Instance) -> None:
        try:
            _kill_tree(inst.proc)
        except Exception:  # noqa: BLE001 —— 回收尽力而为
            pass

    def _evict_locked(self) -> None:
        """实例上限：LRU 关最旧（持锁调用；kill 在锁外补做）。"""
        surplus = []
        while len(self._instances) > self.max_instances:
            oldest = min(self._instances, key=lambda k: self._instances[k].last_used)
            surplus.append(self._instances.pop(oldest))
        for inst in surplus:
            self._kill(inst)

    def _start_reaper(self) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True,
                                            name="ida-mcp-reaper")

            self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stopped:
            time.sleep(self.reaper_interval)
            now = time.monotonic()
            stale = []
            with self._lock:
                for key, inst in self._instances.items():
                    if now - inst.last_used > self.idle_timeout:
                        stale.append(key)
            for key in stale:
                self._drop(key, kill=True)

    def _probe(self, endpoint: str) -> bool:
        if self._probe_fn is not None:
            return bool(self._probe_fn(endpoint))
        try:
            return MCPBackend(endpoint, probe_timeout=self.probe_timeout).available()
        except Exception:  # noqa: BLE001 —— 脏端点视为离线
            return False
