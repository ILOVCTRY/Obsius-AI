"""会话级常驻容器管理器（P2 自愈回收 + P3 复用，2026-10-07）。

背景：`DockerBackend.run_once` 每条命令新建一个容器，per-call 创建开销大；
且 L2 分支曾漏 `--rm` 致 exited 容器残留堆积（已修）。本模块给 **会话**（`sess-<12hex>`）
提供一个常驻容器：首次执行 docker 命令时 `docker run -d` 起一个 `sleep infinity` 容器，
后续命令走 `docker exec`，消除创建开销；容器生命周期由本管理器统一管理。

设计边界（DESIGN.md §7 执行网关）：
- **只作用于 L2 `docker` runtime**；L3 `sandbox` 保持 per-execution 一次性（零挂载/断网铁律），
  网关 `_dispatch` 里 sandbox 分支前置判定，永不经本管理器。
- 容器带 `csp.*` labels + 确定性名 `csp_<pid>_<sid>`，使残留可识别、可回收（P1）。
- 一切失败回落 `run_once`（`ensure`/`exec` 返回 None 由调用方兜底），绝不因新机制让命令执行失败。
- workspace 是宿主卷挂载，容器 rootfs 里的中间态按「即弃」对待——重建成本 ~0.5s，
  故进程重启后直接对账清空旧容器（P2），不做跨进程续用。
"""

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.runtime.backends import DEFAULT_PENTEST_IMAGE, DockerBackend, ExecOutcome

#: 配置覆盖层（gitignore，缺失/损坏回默认，绝不炸）——仿 core/tools/decompiler.py
CONTAINERS_CONFIG_PATH = Path("config/containers.json")

#: 常驻容器内保持存活的哨兵命令（容器起来即长驻，等 docker exec）
_KEEPALIVE = "sleep infinity"

#: 容器名/label 前缀：`csp_` 命名空间隔离，避免与用户自有容器混淆
_NAME_PREFIX = "csp_"
_LABEL_MANAGED = "csp.managed"

DEFAULT_IDLE_TIMEOUT = 1800.0     # 空闲 30min 回收（下次命令付一次 ~0.5s 重建）
DEFAULT_REAPER_INTERVAL = 60.0


def load_containers_config() -> dict:
    """读 config/containers.json；缺失/损坏给空配置（调用方回默认值，绝不炸）。"""
    try:
        if CONTAINERS_CONFIG_PATH.is_file():
            data = json.loads(CONTAINERS_CONFIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def resolve_session_scoped() -> bool:
    """会话级常驻容器总开关（默认开）；config 显式 false = 一键 kill switch 全局回落。"""
    raw = load_containers_config().get("session_scoped")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def resolve_idle_timeout() -> float:
    raw = load_containers_config().get("idle_timeout")
    try:
        val = float(raw)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return DEFAULT_IDLE_TIMEOUT


def resolve_reaper_interval() -> float:
    raw = load_containers_config().get("reaper_interval")
    try:
        val = float(raw)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return DEFAULT_REAPER_INTERVAL


def container_name(project_id: str, session_id: str) -> str:
    """确定性容器名 `csp_<pid>_<sid>`——项目/会话归属编码进名字，`docker ps` 一眼可辨，
    且无需 `.Label` 解析（Windows argv 引号转义坑，见 backends.ps_by_label）。"""
    return f"{_NAME_PREFIX}{project_id}_{session_id}"


def parse_container_name(name: str) -> tuple[str, str] | None:
    """从容器名反解 (project_id, session_id)；非本管理器命名返回 None。
    pid/sid 均为 `<prefix>-<12hex>` 不含下划线，故按首个下划线切分即准。"""
    if not name.startswith(_NAME_PREFIX):
        return None
    rest = name[len(_NAME_PREFIX):]
    pid, sep, sid = rest.partition("_")
    if not sep or not pid or not sid:
        return None
    return pid, sid


def _labels(project_id: str, session_id: str) -> dict[str, str]:
    return {
        _LABEL_MANAGED: "1",
        "csp.project": project_id,
        "csp.session": session_id,
        "csp.runtime": "docker",
    }


@dataclass
class _Container:
    name: str
    project_id: str
    session_id: str
    last_used: float
    dirty: bool = False   # exec 被 abort/超时 → 容器内可能残留进程，下次 ensure 时 restart 清场


class SessionContainerManager:
    """每 (project_id, session_id) 一个常驻容器；线程安全。

    与 `core/tools/ida_mcp_manager.py` 同构：懒启动 daemon reaper + `shutdown_all` 兜关。
    测试可注入假 backend（实现 run_detached/exec_in/inspect_running/rm/ps_by_label 即可）。
    """

    def __init__(self, backend: DockerBackend, *,
                 enabled: bool | None = None,
                 idle_timeout: float | None = None,
                 reaper_interval: float | None = None,
                 image: str = DEFAULT_PENTEST_IMAGE):
        self.backend = backend
        self.enabled = resolve_session_scoped() if enabled is None else bool(enabled)
        self.idle_timeout = idle_timeout if idle_timeout is not None else resolve_idle_timeout()
        self.reaper_interval = (reaper_interval if reaper_interval is not None
                                else resolve_reaper_interval())
        self.image = image
        self._lock = threading.Lock()
        self._containers: dict[tuple[str, str], _Container] = {}
        self._keylocks: dict[tuple[str, str], threading.Lock] = {}
        self._reaper: threading.Thread | None = None
        self._stopped = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """平台启动挂点：拉起 reaper，首个循环做**启动对账**。禁用时 no-op。

        对账只在此处触发（不在 ensure 路径）——否则首个会话建容器会被并发对账删掉。"""
        if self.enabled:
            self._start_reaper(reconcile=True)

    def shutdown_all(self) -> None:
        """平台 shutdown 兜关：停删全部托管容器（释放卷挂载句柄），此后 ensure 拒绝新建。"""
        self._stopped = True
        with self._lock:
            self._containers.clear()
        if not self.enabled:
            return
        for name in self.backend.ps_by_label(_LABEL_MANAGED + "=1"):
            self.backend.rm(name)

    # ---------- 对内（网关） ----------

    def exec(self, project_id: str, session_id: str, cmd: str, *,
             timeout: float = 300.0,
             abort_event: threading.Event | None = None,
             image: str | None = None, net: str = "none",
             mounts: list[tuple[str, str]] | None = None,
             cwd: str | None = None) -> ExecOutcome | None:
        """在会话常驻容器内执行命令。返回 None = 管理器不可用/建容器失败 → 调用方回落
        `run_once`。容器中途消失（被外部删/退出）自愈一次（重建 + 重试）。"""
        name = self.ensure(project_id, session_id, image=image, net=net,
                           mounts=mounts, cwd=cwd)
        if name is None:
            return None
        out = self.backend.exec_in(name, cmd, timeout, abort_event, cwd=cwd)
        if self._container_gone(out):
            self._forget(project_id, session_id)
            name = self.ensure(project_id, session_id, image=image, net=net,
                               mounts=mounts, cwd=cwd)
            if name is None:
                return None
            out = self.backend.exec_in(name, cmd, timeout, abort_event, cwd=cwd)
        if out.interrupted or out.timed_out:
            self._mark_dirty(project_id, session_id)
        return out

    def ensure(self, project_id: str, session_id: str, *,
               image: str | None = None, net: str = "none",
               mounts: list[tuple[str, str]] | None = None,
               cwd: str | None = None,
               timeout: float = 60.0) -> str | None:
        """取/建该会话的常驻容器；失败一律 None（调用方回落 run_once）。"""
        if self._stopped or not self.enabled:
            return None
        key = (project_id, session_id)
        name = container_name(project_id, session_id)
        with self._keylock(key):
            cur = self._containers.get(key)
            if cur is not None:
                if self.backend.inspect_running(cur.name):
                    if cur.dirty:   # 上条 exec 被中断：restart 清容器内残留进程
                        self.backend.restart(cur.name)
                        cur.dirty = False
                    cur.last_used = time.monotonic()
                    return cur.name
                self._forget(project_id, session_id)   # 已退出/被外部删：重建
            out = self.backend.run_detached(
                image or self.image, _KEEPALIVE, name=name,
                labels=_labels(project_id, session_id), net=net,
                mounts=mounts, cwd=cwd, timeout=timeout)
            if out.exit_code != 0 and "already in use" not in out.stderr:
                return None
            with self._lock:
                self._containers[key] = _Container(
                    name=name, project_id=project_id, session_id=session_id,
                    last_used=time.monotonic())
            self._start_reaper()
            return name

    def remove(self, project_id: str, session_id: str) -> None:
        """会话关闭：停删该会话容器（幂等，尽力而为）。"""
        if not self.enabled:
            return
        key = (project_id, session_id)
        with self._keylock(key):
            with self._lock:
                cur = self._containers.pop(key, None)
            name = cur.name if cur is not None else container_name(project_id, session_id)
            self.backend.rm(name)

    def remove_project(self, project_id: str) -> None:
        """项目删除前置：按 label 停删该项目全部容器。

        Windows 上必须先释放卷挂载句柄，否则随后 `store.delete_project` 的 rename 必 422
        （见 core/api/CLAUDE.md「删除项目前置链」）。"""
        if not self.enabled:
            return
        for name in self.backend.ps_by_label(f"csp.project={project_id}"):
            self.backend.rm(name)
        with self._lock:
            for key in [k for k in self._containers if k[0] == project_id]:
                self._containers.pop(key, None)

    # ---------- 对账与回收 ----------

    def reconcile(self) -> None:
        """启动对账（仅 start() 首跑一次）：清空上一进程遗留的托管容器。

        跳过本进程注册表内的容器（新建中的不被误删）；workspace 是宿主卷挂载、
        容器 rootfs 即弃，故直接全清、下次命令按需重建，免跨进程续用与孤儿判定。"""
        with self._lock:
            known = {c.name for c in self._containers.values()}
        for name in self.backend.ps_by_label(_LABEL_MANAGED + "=1"):
            if name not in known:
                self.backend.rm(name)

    def reap_orphans(self) -> None:
        """周期清扫：① 注册表内空闲超 TTL 的 → rm；② daemon 侧无主且已停止的 → rm。

        无主只清「非运行中」的：新建容器在 run_detached 返回与注册之间存在微窗口，
        正在运行故不会被误删；exited 的才是真泄漏。"""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, c in self._containers.items()
                     if now - c.last_used > self.idle_timeout]
            known = {c.name for c in self._containers.values()}
        for key in stale:
            self.remove(*key)
        for name in self.backend.ps_by_label(_LABEL_MANAGED + "=1"):
            if name in known:
                continue
            if not self.backend.inspect_running(name):
                self.backend.rm(name)

    # ---------- 内部 ----------

    def _keylock(self, key: tuple[str, str]) -> threading.Lock:
        with self._lock:
            kl = self._keylocks.get(key)
            if kl is None:
                kl = threading.Lock()
                self._keylocks[key] = kl
            return kl

    def _forget(self, project_id: str, session_id: str) -> None:
        with self._lock:
            self._containers.pop((project_id, session_id), None)

    def _mark_dirty(self, project_id: str, session_id: str) -> None:
        with self._lock:
            cur = self._containers.get((project_id, session_id))
            if cur is not None:
                cur.dirty = True

    @staticmethod
    def _container_gone(out: ExecOutcome) -> bool:
        """docker exec 因容器不存在/未运行而失败 → 需重建自愈。"""
        if out.exit_code == 0:
            return False
        err = out.stderr or ""
        return ("is not running" in err or "No such container" in err
                or "is not running" in (out.stdout or ""))

    def _start_reaper(self, *, reconcile: bool = False) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, args=(reconcile,),
                                            daemon=True,
                                            name="session-container-reaper")
            self._reaper.start()

    def _reap_loop(self, reconcile: bool) -> None:
        if reconcile:
            try:
                self.reconcile()   # 启动对账（仅 start() 触发，一次）
            except Exception:  # noqa: BLE001 —— 对账失败绝不拖垮 reaper
                pass
        while not self._stopped:
            time.sleep(self.reaper_interval)
            try:
                self.reap_orphans()
            except Exception:  # noqa: BLE001
                pass
