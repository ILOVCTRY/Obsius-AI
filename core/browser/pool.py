"""浏览器实例池（F6，DESIGN.md §7）：每项目一个常驻 BrowserInstance。

线程模型红线（定稿）：全项目调用方都是同步线程（FastAPI sync 线程池 /
JobRegistry daemon / Agent worker），playwright sync API 对象绑定创建线程、
无法跨线程调用——所以用 **async_playwright + 每实例独占一个 asyncio loop
线程**，公开 API 全部同步包装（``run_coroutine_threadsafe(...).result(timeout)``）。

实例模型红线：Chromium user_data_dir 是进程级排他锁 →「每项目持久 profile」
收敛为每项目 1 个常驻实例（launch_persistent_context）；F6 的项目并发上限 2
落在**同时活跃 Page（会话）数**上。登录态随项目存活；会话（Page）关闭即焚毁，
profile 不焚毁。

import 红线：本模块（及 core/browser 全包）在 import 时**不得 import
playwright**——只在实例 loop 线程内延迟 import，未装 browser extra 时
``import core.browser`` 不炸（no-tool 降级的前提）。

Windows 红线：profile 目录被 chromium 占用时无法删除——删项目前必须先
``BrowserPool.close_project(pid)``（app 层 delete 端点已接线）。
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import importlib.util
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.browser.capture import CaptureTap
from core.browser.policy import check_target, deny_event

__all__ = [
    "BrowserError", "BrowserConfig", "BrowserPool", "BrowserInstance",
    "BrowserSession", "browser_available", "chromium_available",
    "HUMAN_MAIN_SID",
]

# F6-v3 人工隐式会话（保留 sid）：浏览器页去会话化——前端零会话概念，
# 人工流量统一走这一页；懒创建、不计并发上限、永不自动清除。
HUMAN_MAIN_SID = "human-main"

_NO_TOOL_HINT = ('playwright 未安装。请人工执行：pip install -e ".[browser]" '
                 "&& playwright install chromium，重启平台后本能力可用")


class BrowserError(RuntimeError):
    """浏览器操作失败（白名单拒绝/并发上限/超时/缺依赖）。调用方转文本回填。"""


def browser_available() -> bool:
    """playwright 包是否已安装（不实际 import playwright 顶层模块）。"""
    try:
        return importlib.util.find_spec("playwright") is not None
    except Exception:  # noqa: BLE001 —— 探测失败按未安装
        return False


def chromium_available() -> bool:
    """Chromium 浏览器二进制是否已下载（启发式：ms-playwright 缓存目录探测）。"""
    import os
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return False
        root = str(Path(local) / "ms-playwright")
    try:
        return any(p.name.startswith("chromium")
                   for p in Path(root).iterdir() if p.is_dir())
    except OSError:
        return False


@dataclass
class BrowserConfig:
    """config/browser.json（缺文件/坏文件全默认，绝不 500）。"""
    headless: bool = True                 # False=本机弹窗（headed）
    viewport_width: int = 1280
    viewport_height: int = 720
    max_sessions_per_project: int = 2     # F6 定稿：项目级并发会话（Page）上限
    body_max_bytes: int = 65536           # 抓包/重放 body 存储上限
    action_timeout_s: float = 20.0
    domain_scope: str = "subdomain"       # 白名单子域匹配：subdomain / exact
    intruder_max_concurrency: int = 5     # 爆破并发硬顶（config 可降不可升，replay.py 消费）
    intruder_rate_per_sec: float = 10.0
    intruder_max_requests: int = 1000
    ignore_https_errors: bool = True      # 授权目标默认放行证书错误（过期/自签；
                                          # 2026-09-24 用户定稿——导航前必经 check_target
                                          # 白名单，MITM 面只在已登记授权范围）
    screencast_quality: int = 60          # CDP 实时帧 JPEG 质量（F6-v2）
    screencast_max_width: int = 1280      # 实时帧最大宽（超出缩帧）
    screencast_max_height: int = 720
    intercept_timeout_s: float = 120.0    # F6-v3 拦截挂起超时自动放行原文

    @classmethod
    def from_file(cls, path: str | Path) -> "BrowserConfig":
        cfg = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        allowed = {"headless", "viewport_width", "viewport_height",
                   "max_sessions_per_project", "body_max_bytes",
                   "action_timeout_s", "domain_scope",
                   "intruder_max_concurrency", "intruder_rate_per_sec",
                   "intruder_max_requests", "ignore_https_errors",
                   "screencast_quality",
                   "screencast_max_width", "screencast_max_height",
                   "intercept_timeout_s"}
        for k, v in data.items():
            if k in allowed and not k.startswith("_"):
                setattr(cfg, k, v)
        cfg.max_sessions_per_project = max(1, int(cfg.max_sessions_per_project))
        cfg.intruder_max_concurrency = max(1, int(cfg.intruder_max_concurrency))
        return cfg


@dataclass
class BrowserSession:
    """一个 Page 句柄的元数据（Page 本体只允许在实例 loop 线程内触碰）。"""
    sid: str              # agent 会话 id（sess-…）或 human-<uuid>
    owner: str            # 审计 author（agent=sid / human）
    task_id: str | None = None   # AI 动作由 dispatcher 回填 current_task_id


@dataclass
class _PageEntry:
    page: object = None            # playwright Page（仅 loop 线程触碰）
    info: BrowserSession = None
    url: str = ""
    title: str = ""


class BrowserInstance:
    """每项目一个：私有线程跑 asyncio loop，持久 profile 启动 Chromium。

    公开 API 全部同步且线程安全；内部动作经 _submit 桥到 loop 线程。
    每个动作完成落 browser.action 审计（author=会话 owner）。
    """

    def __init__(self, project_id: str, profile_dir: Path, downloads_dir: Path,
                 bb, config: BrowserConfig, cdp_port: int | None = None):
        self.project_id = project_id
        self.profile_dir = profile_dir
        self.downloads_dir = downloads_dir
        self.bb = bb
        self.config = config
        # MCP Playwright 通过该 loopback CDP 端点接入同一项目浏览器，避免
        # MCP 自己再拉起一个看不见的 Chrome。
        self.cdp_port = cdp_port
        self._capture = None       # CaptureTap（批 F6-2 注入，见 capture.py）
        from core.browser.intercept import InterceptHub
        self._intercept = InterceptHub(self, timeout_s=config.intercept_timeout_s)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._pw = None            # playwright 句柄（仅 loop 线程触碰）
        self._ctx = None           # persistent BrowserContext（仅 loop 线程触碰）
        self._pages: dict[str, _PageEntry] = {}
        self._casts: dict[str, dict] = {}   # sid → {sinks:set[cb], cdp:CDPSession|None}（sinks 锁外判，cdp 仅 loop 线程）
        self._lock = threading.Lock()

    # ---- 生命周期（同步，任意线程） ----

    def start(self) -> None:
        """起 loop 线程（懒启 Chromium：首次动作才 launch_persistent_context）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._loop_main, name=f"browser-{self.project_id[:8]}",
            daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise BrowserError("浏览器实例线程启动超时")

    def stop(self) -> None:
        """停 loop 线程并焚毁 Chromium 进程（删项目/停机前必调）。"""
        self._stopped.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=20)
        self._thread = None

    def _loop_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._started.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(self._aclose())
            loop.close()

    # ---- loop 线程内部协程 ----

    async def _aclose(self) -> None:
        """loop 停止后清理 context 与 playwright（异常全吞：停机不打断退出）。"""
        self._intercept.cancel_all(reason="instance_closed")  # F6-v3
        for sid in list(self._casts):
            try:
                await self._teardown_screencast(sid)
            except Exception:  # noqa: BLE001
                pass
        self._casts.clear()
        for entry in list(self._pages.values()):
            try:
                await entry.page.close()
            except Exception:  # noqa: BLE001
                pass
        self._pages.clear()
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        pw, self._pw = self._pw, None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    async def _ensure_ctx(self):
        """懒启动 Chromium（持久 profile）；context 被外部关闭时自动重建。"""
        if self._ctx is not None:
            try:
                if not self._ctx.is_closed():
                    return self._ctx
            except Exception:  # noqa: BLE001
                pass
            self._ctx = None
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise BrowserError(_NO_TOOL_HINT) from None
        try:
            self._pw = await async_playwright().start()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            launch_args = ([f"--remote-debugging-port={self.cdp_port}"]
                           if self.cdp_port else [])
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.config.headless,
                viewport={"width": self.config.viewport_width,
                          "height": self.config.viewport_height},
                args=launch_args,
                accept_downloads=True,
                ignore_https_errors=self.config.ignore_https_errors,
            )
        except BrowserError:
            raise
        except Exception as e:  # noqa: BLE001 —— 启动失败转 BrowserError（不杀 loop）
            self._ctx = None
            raise BrowserError(f"Chromium 启动失败: {e}") from e
        if self._capture is not None:
            try:
                await self._capture.attach(self._ctx)
            except Exception:  # noqa: BLE001 —— 抓包接入失败不阻断浏览
                pass
        return self._ctx

    def _submit(self, coro, timeout: float | None = None):
        """桥到 loop 线程执行；超时/失败一律转 BrowserError（loop 线程不死于调用方）。"""
        if self._stopped.is_set() or self._loop is None or not self._loop.is_running():
            raise BrowserError("浏览器实例未运行（已停止或未启动）")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout or self.config.action_timeout_s + 10)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise BrowserError("浏览器操作超时") from None
        except BrowserError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BrowserError(str(e) or type(e).__name__) from e

    def _origin(self, sid: str) -> str:
        return "human" if sid.startswith("human-") else "agent"

    def _audit(self, entry: _PageEntry, action: str, **payload) -> None:
        """browser.action 审计（author=owner；agent 动作挂 task_id）。"""
        info = entry.info
        self.bb.append_event(
            self.project_id, "browser.action",
            {"action": action, "origin": self._origin(info.sid),
             "task_id": info.task_id,
             **{k: v for k, v in payload.items() if v is not None}},
            session_id=info.sid, author=info.owner)

    def _entry(self, sid: str) -> _PageEntry:
        entry = self._pages.get(sid)
        if entry is None:
            raise BrowserError(f"浏览器会话不存在: {sid}（先 open_session）")
        return entry

    # ---- 会话管理（同步） ----

    def open_session(self, sid: str, owner: str) -> dict:
        """开一个 Page（会话）。超项目并发上限抛 BrowserError；幂等复用。

        F6-v3：human-main（人工隐式会话）豁免上限——上限只数 AI 页，
        否则人工页在场时 AI 只能开 1 页（误伤）。
        """
        with self._lock:
            if sid in self._pages:
                return self._session_dict(self._pages[sid])
            if sid != HUMAN_MAIN_SID:
                ai_pages = len([s for s in self._pages if s != HUMAN_MAIN_SID])
                if ai_pages >= self.config.max_sessions_per_project:
                    raise BrowserError(
                        f"项目并发浏览器会话已达上限 {self.config.max_sessions_per_project}"
                        "（先关闭空闲会话）")
            self._pages[sid] = _PageEntry(info=BrowserSession(sid=sid, owner=owner))
        self.start()
        self._submit(self._open_session_coro(sid), timeout=30)
        return self._session_dict(self._pages[sid])

    def ensure_human_session(self) -> dict:
        """人工隐式会话懒创建唯一入口（幂等）——API 层 navigate/action/WS 用。"""
        return self.open_session(HUMAN_MAIN_SID, "human")

    async def _open_session_coro(self, sid: str) -> None:
        ctx = await self._ensure_ctx()
        entry = self._pages.get(sid)
        if entry is None or entry.page is not None:
            return
        page = await ctx.new_page()
        entry.page = page
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        page.on("download", self._on_download)
        page.on("close", lambda _p: self._pages.pop(sid, None))
        try:
            entry.url = page.url
            entry.title = await page.title()
        except Exception:  # noqa: BLE001
            pass

    def _on_download(self, download) -> None:
        """下载文件落 artifacts/browser-downloads/ + 审计事件（loop 线程回调；
        Blackboard 写线程安全）。失败只 log 不阻断下载。"""
        import logging
        try:
            name = download.suggested_filename or "download.bin"
            safe = name.replace("\\", "_").replace("/", "_") or "download.bin"
            out = self.downloads_dir / safe
            n = 2
            while out.exists():
                out = self.downloads_dir / f"{out.stem}-{n}{out.suffix}"
                n += 1
            download.save_as(str(out))
            sid = next((s for s, e in self._pages.items()
                        if e.page is download.page), None)
            self.bb.append_event(
                self.project_id, "browser.download",
                {"path": str(out), "filename": safe},
                session_id=sid, author="system")
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug(
                "浏览器下载落盘失败（项目 %s）", self.project_id, exc_info=True)

    def close_session(self, sid: str) -> None:
        """关 Page（会话焚毁；持久 profile 保留登录态）。"""
        if sid not in self._pages:
            return
        self._submit(self._close_session_coro(sid), timeout=15)

    async def _close_session_coro(self, sid: str) -> None:
        self._intercept.cancel_all(reason="session_closed")  # F6-v3：未裁决包放行
        await self._teardown_screencast(sid)
        entry = self._pages.pop(sid, None)
        if entry is not None and entry.page is not None:
            try:
                await entry.page.close()
            except Exception:  # noqa: BLE001
                pass

    def _session_dict(self, entry: _PageEntry) -> dict:
        info = entry.info
        return {"sid": info.sid, "owner": info.owner, "task_id": info.task_id,
                "url": entry.url, "title": entry.title,
                "origin": self._origin(info.sid)}

    def sessions(self) -> list[dict]:
        return [self._session_dict(e) for e in self._pages.values()]

    def set_task_id(self, sid: str, task_id: str | None) -> None:
        """AI 动作挂认领任务（dispatcher 每次调用前回填；审计/http_history 消费）。"""
        entry = self._pages.get(sid)
        if entry is not None and entry.info is not None:
            entry.info.task_id = task_id

    # ---- 动作（同步；白名单在导航入口硬校验） ----

    def navigate(self, sid: str, url: str) -> dict:
        """导航。目标白名单在此硬校验（宁严勿松）；拒绝落 browser.deny。"""
        entry = self._entry(sid)
        verdict = check_target(self.bb, self.project_id, url,
                               domain_scope=self.config.domain_scope)
        if not verdict.allowed:
            deny_event(self.bb, self.project_id, url, verdict,
                       session_id=entry.info.sid, author=entry.info.owner,
                       origin=self._origin(sid))
            raise BrowserError(verdict.reason)
        t0 = time.monotonic()
        result = self._submit(self._nav_coro(entry, url))
        duration_ms = int((time.monotonic() - t0) * 1000)
        self._audit(entry, "navigate", url=url,
                    final_url=result["final_url"], status=result["status"],
                    target_host=verdict.host, duration_ms=duration_ms)
        return {**result, "target_host": verdict.host, "duration_ms": duration_ms}

    async def _nav_coro(self, entry: _PageEntry, url: str) -> dict:
        ctx = await self._ensure_ctx()
        page = entry.page
        if page is None or page.is_closed():
            page = entry.page = await ctx.new_page()
            page.on("download", self._on_download)
            page.on("close", lambda _p: self._pages.pop(entry.info.sid, None))
        resp = await page.goto(url, timeout=self.config.action_timeout_s * 1000,
                               wait_until="domcontentloaded")
        entry.url = page.url
        try:
            entry.title = await page.title()
        except Exception:  # noqa: BLE001
            entry.title = ""
        return {"final_url": page.url, "title": entry.title,
                "status": resp.status if resp else None}

    def act(self, sid: str, action: str, **kw) -> dict:
        """非导航动作：click(selector|x,y) / type(selector,text) / back / content。"""
        entry = self._entry(sid)
        t0 = time.monotonic()
        result = self._submit(self._act_coro(entry, action, kw))
        duration_ms = int((time.monotonic() - t0) * 1000)
        # 审计脱敏：输入文本只留 80 字符
        audit_kw = dict(kw)
        if "text" in audit_kw and audit_kw["text"]:
            audit_kw["text"] = str(audit_kw["text"])[:80]
        self._audit(entry, action, duration_ms=duration_ms,
                    url=entry.url, **audit_kw)
        return {**result, "duration_ms": duration_ms}

    async def _act_coro(self, entry: _PageEntry, action: str, kw: dict) -> dict:
        ctx = await self._ensure_ctx()
        page = entry.page
        if page is None or page.is_closed():
            page = entry.page = await ctx.new_page()
            page.on("download", self._on_download)
            page.on("close", lambda _p: self._pages.pop(entry.info.sid, None))
        if action == "click":
            x, y = kw.get("x"), kw.get("y")
            if x is not None and y is not None:
                await page.mouse.click(float(x), float(y))
            elif kw.get("selector"):
                await page.click(str(kw["selector"]),
                                 timeout=self.config.action_timeout_s * 1000)
            else:
                raise BrowserError("click 需要 selector 或 x/y 坐标")
        elif action == "type":
            if not kw.get("selector"):
                raise BrowserError("type 需要 selector")
            await page.fill(str(kw["selector"]), str(kw.get("text", "")),
                            timeout=self.config.action_timeout_s * 1000)
        elif action == "back":
            await page.go_back(timeout=self.config.action_timeout_s * 1000)
        elif action == "content":
            text = await page.inner_text("body", timeout=self.config.action_timeout_s * 1000)
            return {"content": text[:8192], "truncated": len(text) > 8192}
        else:
            raise BrowserError(f"未知动作: {action}")
        entry.url = page.url
        try:
            entry.title = await page.title()
        except Exception:  # noqa: BLE001
            entry.title = ""
        return {"final_url": page.url, "title": entry.title}

    def screenshot(self, sid: str) -> bytes:
        """单帧截图 PNG（按需，非实时流）。"""
        entry = self._entry(sid)
        png = self._submit(self._shot_coro(entry), timeout=30)
        self._audit(entry, "screenshot", url=entry.url,
                    size=len(png))
        return png

    async def _shot_coro(self, entry: _PageEntry) -> bytes:
        await self._ensure_ctx()
        page = entry.page
        if page is None or page.is_closed():
            raise BrowserError("会话页面未打开（先导航）")
        return await page.screenshot(type="png",
                                     timeout=self.config.action_timeout_s * 1000)

    # ---- 实时画面流 + 人类接管（F6-v2：CDP screencast → WS → 前端回传输入） ----

    def attach_screencast(self, sid: str, sink) -> None:
        """订阅实时帧（sink=可调用，收 {data:base64-jpeg, metadata, ts}，在实例
        loop 线程被调——WS 端负责 call_soon_threadsafe 桥回自己的事件循环）。
        首订阅者懒起 CDP screencast；末订阅者退出自动停流。"""
        with self._lock:
            cast = self._casts.get(sid)
            if cast is None:
                cast = {"sinks": set(), "cdp": None}
                self._casts[sid] = cast
            cast["sinks"].add(sink)
            first = len(cast["sinks"]) == 1
        self.start()
        if first:
            self._submit(self._screencast_start_coro(sid), timeout=30)

    def detach_screencast(self, sid: str, sink) -> None:
        with self._lock:
            cast = self._casts.get(sid)
            if cast is None:
                return
            cast["sinks"].discard(sink)
            last = not cast["sinks"]
        if last:
            try:
                self._submit(self._screencast_stop_coro(sid), timeout=15)
            except BrowserError:  # noqa: TRY301 —— 会话已关/实例已停：清理降级
                pass

    async def _screencast_start_coro(self, sid: str) -> None:
        await self._ensure_ctx()
        entry = self._entry(sid)
        page = entry.page
        if page is None or page.is_closed():
            raise BrowserError("会话页面未打开（先导航）")
        cast = self._casts.get(sid)
        if cast is None or cast.get("cdp") is not None:
            return  # 已在推流（幂等）
        cdp = await self._ctx.new_cdp_session(page)
        cast["cdp"] = cdp

        def _on_frame(params: dict) -> None:
            # screencastFrame 必须回 ack，否则 Chromium 推 2 帧即停（CDP 协议要求）
            frame_sid = params.get("sessionId")
            if frame_sid:
                asyncio.ensure_future(self._safe_ack(cdp, frame_sid))
            frame = {"data": params.get("data", ""),
                     "metadata": params.get("metadata") or {},
                     "ts": time.time()}
            for sink in tuple(cast["sinks"]):
                try:
                    sink(frame)
                except Exception:  # noqa: BLE001 —— 单个订阅者异常不拖垮帧流
                    cast["sinks"].discard(sink)

        cdp.on("Page.screencastFrame", _on_frame)
        await cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": self.config.screencast_quality,
            "maxWidth": self.config.screencast_max_width,
            "maxHeight": self.config.screencast_max_height,
            "everyNthFrame": 1,
        })

    @staticmethod
    async def _safe_ack(cdp, frame_sid: str) -> None:
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": frame_sid})
        except Exception:  # noqa: BLE001 —— 流已停/会话已关
            pass

    async def _screencast_stop_coro(self, sid: str) -> None:
        await self._teardown_screencast(sid)

    async def _teardown_screencast(self, sid: str) -> None:
        cast = self._casts.get(sid)
        cdp = cast.get("cdp") if cast else None
        if cast is not None:
            cast["cdp"] = None
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001
                pass
            try:
                await cdp.detach()
            except Exception:  # noqa: BLE001
                pass

    def human_input(self, sid: str, kind: str, **kw) -> dict:
        """人类接管输入注入（Playwright mouse/keyboard API）。

        红线：仅 human- 前缀会话可接管（AI 会话只读观看，人机不抢同一 Page）。
        审计口径：click/dblclick/wheel/key/type 落 browser.action；move/down/up 不审计。
        """
        entry = self._entry(sid)
        if not sid.startswith("human-"):
            raise BrowserError("仅人类会话（human-…）可接管输入，AI 会话只读")
        audited = kind in ("click", "dblclick", "wheel", "key", "type")
        audit_kw = {k: (str(v)[:80] if k in ("text", "key") and v else v)
                    for k, v in kw.items()}
        t0 = time.monotonic()
        result = self._submit(self._human_input_coro(entry, kind, kw))
        duration_ms = int((time.monotonic() - t0) * 1000)
        if audited:
            self._audit(entry, "human-input", kind=kind, duration_ms=duration_ms,
                        url=entry.url, **audit_kw)
        return {**result, "duration_ms": duration_ms}

    async def _human_input_coro(self, entry: _PageEntry, kind: str, kw: dict) -> dict:
        ctx = await self._ensure_ctx()
        page = entry.page
        if page is None or page.is_closed():
            page = entry.page = await ctx.new_page()
            page.on("download", self._on_download)
            page.on("close", lambda _p: self._pages.pop(entry.info.sid, None))
        if kind in ("click", "dblclick"):
            await page.mouse.click(float(kw.get("x", 0)), float(kw.get("y", 0)),
                                   click_count=2 if kind == "dblclick" else 1)
        elif kind == "move":
            await page.mouse.move(float(kw.get("x", 0)), float(kw.get("y", 0)))
        elif kind == "down":
            await page.mouse.down(button=str(kw.get("button", "left")))
        elif kind == "up":
            await page.mouse.up(button=str(kw.get("button", "left")))
        elif kind == "wheel":
            await page.mouse.wheel(float(kw.get("delta_x", 0)),
                                   float(kw.get("delta_y", 0)))
        elif kind == "key":
            await page.keyboard.press(str(kw.get("key", "")))
        elif kind == "type":
            await page.keyboard.insert_text(str(kw.get("text", "")))
        else:
            raise BrowserError(f"未知接管输入: {kind}")
        entry.url = page.url
        try:
            entry.title = await page.title()
        except Exception:  # noqa: BLE001
            entry.title = ""
        return {"final_url": page.url, "title": entry.title}

    def status(self) -> dict:
        return {
            "project_id": self.project_id,
            "headless": self.config.headless,
            "sessions": self.sessions(),
        }

    @property
    def session_count(self) -> int:
        return len(self._pages)


class BrowserPool:
    """项目 → 常驻 BrowserInstance。懒启动；删项目/停机必须 close。"""

    def __init__(self, workspace_root: str | Path, bb_getter,
                 config: BrowserConfig | None = None,
                 config_path: str | Path = "config/browser.json"):
        self._workspace_root = Path(workspace_root)
        self._bb_getter = bb_getter          # pid -> Blackboard（app 层注入）
        self.config = config or BrowserConfig.from_file(config_path)
        self._instances: dict[str, BrowserInstance] = {}
        self._cdp_ports: dict[str, int] = {}
        self._lock = threading.Lock()

    def _cdp_port_for(self, project_id: str) -> int:
        """为项目挑一个稳定且当前空闲的 loopback CDP 端口。"""
        existing = self._cdp_ports.get(project_id)
        if existing:
            return existing
        seed = int(hashlib.sha1(project_id.encode("utf-8")).hexdigest()[:8], 16)
        start = 16000 + seed % 1800
        for offset in range(200):
            port = start + offset
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("127.0.0.1", port))
                self._cdp_ports[project_id] = port
                return port
            except OSError:
                continue
        raise BrowserError("项目浏览器无法分配 MCP CDP 端口")

    def get_instance(self, project_id: str) -> BrowserInstance:
        with self._lock:
            inst = self._instances.get(project_id)
            if inst is None:
                base = self._workspace_root / project_id
                inst = BrowserInstance(
                    project_id=project_id,
                    profile_dir=base / "browser-profile",
                    downloads_dir=base / "artifacts" / "browser-downloads",
                    bb=self._bb_getter(project_id),
                    config=self.config,
                    cdp_port=self._cdp_port_for(project_id),
                )
                # 抓包拦截（F6 批 2）：context 建立后由 _ensure_ctx 自动 attach
                inst._capture = CaptureTap(inst)
                self._instances[project_id] = inst
            return inst

    def prepare_embedded(self, project_id: str) -> str:
        """启动项目浏览器并返回 MCP 可复用的 loopback CDP 地址。"""
        inst = self.get_instance(project_id)
        inst.ensure_human_session()
        if not inst.cdp_port:
            raise BrowserError("项目浏览器未暴露 MCP CDP 端点")
        return f"http://127.0.0.1:{inst.cdp_port}"

    def close_project(self, project_id: str) -> None:
        """焚毁实例（删项目前必调——profile 目录被 chromium 占用会锁死删除）。"""
        with self._lock:
            inst = self._instances.pop(project_id, None)
        if inst is not None:
            try:
                inst.stop()
            except Exception:  # noqa: BLE001 —— 停机降级：实例残留随进程退出
                pass
            self._cdp_ports.pop(project_id, None)

    def close_all(self) -> None:
        with self._lock:
            insts = list(self._instances.values())
            self._instances.clear()
        for inst in insts:
            try:
                inst.stop()
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> list[dict]:
        with self._lock:
            insts = list(self._instances.values())
        return [{**i.status(), "sessions": i.sessions()} for i in insts]
