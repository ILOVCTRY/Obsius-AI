"""抓包捕获（F6 批 2 / F6-v3 拦截，DESIGN.md §7）：Playwright 路由拦截。

机制：``context.route("**/*", handler)`` → handler 内 ``route.fetch()`` 拿完整
请求+响应体 → ``route.fulfill(response=resp)`` 原样放行（浏览行为不变）→
归一化 → ``bb.add_http_history(source="browser")``。

F6-v3 拦截：仅人工隐式会话（human-main）的流量可被挂起裁决——
- 请求向：fetch **前** hold（改包走 ``route.fetch(url=/method=/headers=/post_data=)``
  覆写，不走 continue_——保证改后流量照常入 http_history）；
- 响应向：fetch 后 hold（改包 ``route.fulfill(status=/headers=/body=)``）；
- 裁决超时/注册满/关开关 → 放行原文；drop → ``route.abort()``（不入库只落事件）；
- ``_store`` 在**裁决之后**执行，记录实际发生的（改后）值；duration_ms 只测
  fetch 段，人工处理耗时进 meta.intercept.hold_ms。

已知限制（v1 接受）：WebSocket 不经 route（抓不到）；流式/超大文件 fetch 失败
走 ``route.continue_()`` 兜底不入库 + browser.capture_error 事件（宁缺勿脏）。

normalize_body 是 capture/replay/intruder 三方共用的 body 归一化规则：
- 文本类 mime（text/* / json / html / xml / javascript / form）按 utf-8 解码
  （解码失败降级 base64）；
- 其余按 base64（is_binary=1）；
- 超过 body_max_bytes 截断（body_truncated=1）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from core.browser.httpmsg import render_raw_request, render_raw_response

__all__ = ["CaptureTap", "normalize_body", "TEXTUAL_MIME_RE"]

_TEXT_MIME_SUBSTR = ("json", "html", "xml", "javascript", "x-www-form-urlencoded",
                     "graphql", "csv", "event-stream")


def _is_textual_mime(mime: str) -> bool:
    m = (mime or "").lower()
    return m.startswith("text/") or any(s in m for s in _TEXT_MIME_SUBSTR)


def normalize_body(body: bytes | None, mime: str,
                   body_max_bytes: int) -> tuple[str | None, bool, bool]:
    """bytes → (存储文本, body_truncated, is_binary)。None → (None, False, False)。"""
    if body is None or len(body) == 0:
        return None, False, False
    truncated = len(body) > body_max_bytes
    data = body[:body_max_bytes]
    if _is_textual_mime(mime):
        try:
            return data.decode("utf-8"), truncated, False
        except UnicodeDecodeError:
            pass
    # 修复截断可能造成的 base64 长度问题：对完整 body 编码后再截尾不必要——
    # base64 允许非 4 倍数解码失败，这里直接对截断后的片段编码（下游只做预览）
    return base64.b64encode(data).decode("ascii"), truncated, True


class CaptureTap:
    """附着在 BrowserInstance 的 persistent context 上的路由拦截器。

    由 BrowserInstance._ensure_ctx 在 context 建立后 attach（pool 注入
    ``instance._capture``）。存库经 Blackboard（线程安全，loop 线程可直写）。
    """

    def __init__(self, instance):
        self.instance = instance

    async def attach(self, ctx) -> None:
        await ctx.route("**/*", self._on_route)

    def _sid_of(self, request) -> str | None:
        """按 request 反查所属会话 sid（route.request.frame.page → _pages 映射）。"""
        try:
            page = request.frame.page
        except Exception:  # noqa: BLE001
            return None
        for sid, entry in self.instance._pages.items():
            if entry.page is page:
                return sid
        return None

    async def _on_route(self, route) -> None:
        inst = self.instance
        request = route.request
        hub = getattr(inst, "_intercept", None)
        sid = self._sid_of(request)
        # F6-v3：仅人工隐式会话可被拦（AI 会话流量照常记录绝不拦）
        from core.browser.pool import HUMAN_MAIN_SID
        is_human = sid == HUMAN_MAIN_SID

        # ---- ① 请求向拦截（fetch 前 hold） ----
        req_hold = None
        if hub is not None and is_human and hub.req_enabled:
            req_hold = self._register_request_hold(request)
        req_mods = None
        if req_hold is not None:
            action, req_mods = await self._await_hold(hub, req_hold)
            if action == "drop":
                await self._abort(route)
                self._intercept_event(sid, "drop", "request", req_hold)
                return
            if req_mods is not None:
                req_mods = _strip_request_headers(req_mods)

        # ---- ② 发出请求（改包=fetch 覆写） ----
        fetch_kw: dict = {}
        if req_mods is not None:
            fetch_kw = {"method": req_mods["method"], "url": req_mods["url"],
                        "headers": req_mods["headers"]}
            if req_mods.get("post_data") is not None:
                fetch_kw["post_data"] = req_mods["post_data"]
        t0 = time.monotonic()
        try:
            resp = await route.fetch(**fetch_kw)
        except Exception:  # noqa: BLE001 —— fetch 失败（流式/断连/非 HTTP）原样放行
            await self._continue(route)
            return
        fetch_ms = int((time.monotonic() - t0) * 1000)

        # ---- ③ 响应向拦截（fetch 后 hold） ----
        resp_hold = None
        if hub is not None and is_human and hub.resp_enabled:
            resp_hold = await self._register_response_hold(request, resp)
        resp_mods = None
        if resp_hold is not None:
            action, resp_mods = await self._await_hold(hub, resp_hold)
            if action == "drop":
                await self._abort(route)
                self._intercept_event(sid, "drop", "response", resp_hold)
                return
            if resp_mods is not None:
                resp_mods = _strip_response_headers(resp_mods)

        # ---- ④ 放行 ----
        try:
            if resp_mods is not None:
                await route.fulfill(
                    status=resp_mods["status"], headers=resp_mods["headers"],
                    body=resp_mods["body"])
            else:
                await route.fulfill(response=resp)
        except Exception:  # noqa: BLE001 —— fulfill 失败（body 消费竞态等）放行兜底
            await self._continue(route)

        # ---- ⑤ 入库（裁决后，记录实际发生的值） ----
        meta_extra = None
        if req_hold is not None or resp_hold is not None:
            meta_extra = {"intercept": {
                "request_modified": req_mods is not None,
                "response_modified": resp_mods is not None,
                "hold_ms": (getattr(req_hold, "hold_ms", 0)
                            + getattr(resp_hold, "hold_ms", 0))}}
        try:
            await self._store(request, resp,
                              req_override=(req_mods if req_mods is not None else None),
                              resp_override=resp_mods,
                              fetch_ms=fetch_ms, meta_extra=meta_extra)
        except Exception:  # noqa: BLE001 —— 入库失败不阻断浏览，落错误事件
            try:
                inst.bb.append_event(
                    inst.project_id, "browser.capture_error",
                    {"url": request.url, "method": request.method},
                    session_id=sid, author="system")
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug(
                    "browser.capture_error 落库失败", exc_info=True)

    # ---- F6-v3 拦截辅助 ----

    def _register_request_hold(self, request):
        hub = self.instance._intercept
        try:
            headers = dict(request.headers) if request.headers else {}
            try:
                body = request.post_data_buffer
            except Exception:  # noqa: BLE001
                body = None
            raw, editable = render_raw_request(
                request.method, request.url, headers, body)
            hold = hub.new_hold(direction="request", method=request.method,
                                url=request.url, status=None,
                                req_headers=headers, req_body=body,
                                raw=raw, editable=editable)
            return hold if hub.try_register(hold) else None
        except Exception:  # noqa: BLE001 —— 组包失败不拦（宁通勿卡）
            return None

    async def _register_response_hold(self, request, resp):
        hub = self.instance._intercept
        try:
            body = await resp.body()
        except Exception:  # noqa: BLE001 —— body 拿不到只存头
            body = None
        try:
            headers = dict(resp.headers or {})
            mime = headers.get("content-type", "")
            raw, editable = render_raw_response(resp.status, headers, body, mime)
            hold = hub.new_hold(
                direction="response", method=request.method, url=request.url,
                status=resp.status, req_headers=dict(request.headers or {}),
                resp_headers=headers, resp_body=body, resp_mime=mime,
                raw=raw, editable=editable)
            return hold if hub.try_register(hold) else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    async def _await_hold(hub, hold) -> tuple:
        """等裁决 → (action, mods|None)。超时/异常一律放行原文；
        人工处理耗时记在 hold.hold_ms（不污染 duration_ms）。"""
        try:
            result = await asyncio.wait_for(hold.future, timeout=hold.timeout_s)
            return result
        except (asyncio.TimeoutError, TimeoutError):
            return ("forward", None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return ("forward", None)
        finally:
            hub.discard(hold.hold_id)
            hold.hold_ms = int((time.time() - hold.created_at) * 1000)

    def _intercept_event(self, sid: str | None, action: str,
                         direction: str, hold) -> None:
        inst = self.instance
        try:
            inst.bb.append_event(
                inst.project_id, "browser.intercept",
                {"action": action, "direction": direction,
                 "hold_id": hold.hold_id, "method": hold.method,
                 "url": hold.url},
                session_id=sid, author="human")
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug(
                "browser.intercept 事件落库失败", exc_info=True)

    @staticmethod
    async def _continue(route) -> None:
        try:
            await route.continue_()
        except Exception:  # noqa: BLE001 —— route 已失效（页面跳转等）
            pass

    @staticmethod
    async def _abort(route) -> None:
        try:
            await route.abort()
        except Exception:  # noqa: BLE001 —— route 已失效
            pass

    async def _store(self, request, resp, *, req_override: dict | None = None,
                     resp_override: dict | None = None, fetch_ms: int | None = None,
                     meta_extra: dict | None = None) -> None:
        inst = self.instance
        t0 = time.monotonic()
        # ---- 请求侧（override=拦截改后的实际发出值） ----
        if req_override is not None:
            req_headers = req_override.get("headers") or {}
            post_data = req_override.get("post_data")
            method, url = req_override["method"], req_override["url"]
        else:
            req_headers = dict(request.headers) if request.headers else {}
            post_data = None
            try:
                buf = request.post_data_buffer
                post_data = buf if buf is not None else None
            except Exception:  # noqa: BLE001
                post_data = None
            method, url = request.method, request.url
        # ---- 响应侧（body 是协程，必须 await；拿不到只存头） ----
        if resp_override is not None:
            body = resp_override.get("body")
            resp_headers = resp_override.get("headers") or {}
            status = resp_override["status"]
        else:
            try:
                body = await resp.body()
            except Exception:  # noqa: BLE001
                body = None
            resp_headers = dict(resp.headers or {})
            status = resp.status
        duration_ms = fetch_ms if fetch_ms is not None \
            else int((time.monotonic() - t0) * 1000)
        # mime 查找大小写不敏感（改包报文里用户可写 Content-Type / content-type）
        mime = next((v for k, v in (resp_headers or {}).items()
                     if k.lower() == "content-type"), "")
        resp_text, resp_trunc, resp_bin = normalize_body(
            body, mime, inst.config.body_max_bytes)
        req_mime = next((v for k, v in (req_headers or {}).items()
                         if k.lower() == "content-type"), "")
        req_text, req_trunc, req_bin = normalize_body(
            post_data, req_mime, inst.config.body_max_bytes)
        sid = self._sid_of(request)
        entry = inst._pages.get(sid) if sid else None
        inst.bb.add_http_history(
            inst.project_id, source="browser",
            session_id=sid, task_id=(entry.info.task_id if entry else None),
            method=method, url=url,
            status=status,
            req_headers=req_headers, req_body=req_text,
            resp_headers=resp_headers, resp_body=resp_text,
            resp_mime=mime,
            body_truncated=resp_trunc or req_trunc,
            is_binary=resp_bin, duration_ms=duration_ms, meta=meta_extra)


def _strip_request_headers(mods: dict) -> dict:
    """改包请求头剥 content-length/host（fetch 会重算）；body 文本→bytes。"""
    headers = {k: v for k, v in (mods.get("headers") or {}).items()
               if k.lower() not in ("content-length", "host")}
    body = mods.get("body")
    post = body.encode("utf-8") if isinstance(body, str) else body
    return {"method": mods["method"], "url": mods["url"],
            "headers": headers, "post_data": post}


def _strip_response_headers(mods: dict) -> dict:
    """改包响应头剥 content-length/content-encoding（body 已解压明文）。"""
    headers = {k: v for k, v in (mods.get("headers") or {}).items()
               if k.lower() not in ("content-length", "content-encoding")}
    body = mods.get("body")
    data = body.encode("utf-8") if isinstance(body, str) else body
    return {"status": mods["status"], "headers": headers, "body": data}
