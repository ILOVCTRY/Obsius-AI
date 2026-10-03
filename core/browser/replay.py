"""重发与爆破引擎（F6 批 3，DESIGN.md §7）：独立 HTTP 客户端，零 playwright 依赖。

- ``ReplayClient``：单条重放。可从 http_history 取模板（capture_id）后逐项
  覆写（method/url/headers/body）；结果入库 source="replay"。
- ``Intruder``：**人类 UI 专属**爆破（决策红线：AI 无任何发起入口，只能经
  bb_query-式只读读 http_history 结果）。模板 + ``§POS1§`` 标记替换 + payload 集
  + 线程池 + token-bucket 限速；并发**硬顶** config.intruder_max_concurrency
  （config 可降不可升）；结果逐请求入库 source="intruder"，审计只在批次级。

重发与爆破支持任意 URL；HTTP 报文解析与执行沿用现有实现。
"""

from __future__ import annotations

import itertools
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from core.browser.capture import normalize_body
from core.browser.pool import BrowserError

__all__ = ["ReplayClient", "Intruder"]

_MARKER_RE = re.compile(r"§([A-Za-z0-9_]+)§")
_CONSECUTIVE_FAIL_LIMIT = 10


def _now_ms() -> int:
    return int(time.time() * 1000)


class ReplayClient:
    """单条重放（httpx sync Client；JobRegistry 线程内执行）。"""

    def __init__(self, bb, *, config=None):
        self.bb = bb
        self.config = config  # BrowserConfig（body_max_bytes）；None 用默认

    @property
    def _body_max(self) -> int:
        return self.config.body_max_bytes if self.config else 65536

    def replay(self, project_id: str, *, capture_id: int | None = None,
               raw: str | None = None,
               session_id: str | None = None, task_id: str | None = None,
               author: str = "human") -> dict:
        """重放一条请求（F6-v3：原始报文 `raw` 或 capture_id 模板，二选一）。
        raw 给定时经 ``parse_raw_request`` 解析（相对路径用模板 url 兜底拼绝对），
        ``modified=True``。返回入库后的完整行（get_http_history 形态）。"""
        row: dict = {}
        if capture_id is not None:
            row = self.bb.get_http_history(project_id, capture_id)
            if row is None:
                raise ValueError(f"抓包记录不存在: {capture_id}")
        if raw:
            from core.browser.httpmsg import parse_raw_request
            try:
                parsed = parse_raw_request(raw, base_url=row.get("url") or "")
            except ValueError as e:
                raise BrowserError(f"原始报文解析失败: {e}") from e
            m = parsed["method"]
            u = parsed["url"]
            hs = parsed["headers"]
            b = parsed["body"]
            modified = True
        else:
            m = (row.get("method") or "GET").upper()
            u = row.get("url") or ""
            hs = dict(row.get("req_headers") or {})
            b = row.get("req_body")
            modified = False
        if not u:
            raise ValueError("缺少重放目标 url（贴原始报文或给 capture_id）")
        batch_id = f"rp-{uuid.uuid4().hex[:12]}"
        t0 = time.monotonic()
        try:
            # trust_env=False：重放/爆破打授权目标（常为内网/localhost），流量
            # 绝不交给系统代理——httpx trust_env 经 urllib 读 Windows 注册表，
            # 系统代理（如 Clash）开着会把请求全转发走（目标失真+响应被拦改）
            resp = httpx.Client(timeout=15.0, follow_redirects=True,
                                trust_env=False).request(
                m, u, headers=hs, content=b if b else None)
            status = resp.status_code
            resp_headers = dict(resp.headers)
            mime = resp_headers.get("content-type", "")
            resp_text, resp_trunc, resp_bin = normalize_body(
                resp.content, mime, self._body_max)
        except httpx.HTTPError as e:
            # 连接失败也入库（status=None），前端可见失败原因
            self.bb.add_http_history(
                project_id, source="replay", session_id=session_id,
                task_id=task_id, batch_id=batch_id,
                meta={"replayed_from": capture_id, "modified": modified,
                      "error": str(e)[:200]},
                method=m, url=u, status=None,
                req_headers=hs, req_body=b, duration_ms=int((time.monotonic() - t0) * 1000))
            raise BrowserError(f"重放请求失败: {e}") from e
        duration_ms = int((time.monotonic() - t0) * 1000)
        row_id = self.bb.add_http_history(
            project_id, source="replay", session_id=session_id, task_id=task_id,
            batch_id=batch_id,
            meta={"replayed_from": capture_id, "modified": modified},
            method=m, url=u, status=status,
            req_headers=hs, req_body=b,
            resp_headers=resp_headers, resp_body=resp_text, resp_mime=mime,
            body_truncated=resp_trunc, is_binary=resp_bin,
            duration_ms=duration_ms)
        return self.bb.get_http_history(project_id, row_id)


class Intruder:
    """爆破引擎（人类 UI 专属）。run() 阻塞执行（JobRegistry daemon 线程调用），
    逐请求入库，返回批次摘要。"""

    def __init__(self, bb, *, config=None):
        self.bb = bb
        self.config = config

    @property
    def _body_max(self) -> int:
        return self.config.body_max_bytes if self.config else 65536

    # ---- payload 集展开 ----

    @staticmethod
    def expand_payloads(spec: dict) -> list[str]:
        """payload 集：{position, type:"list", values:[...]} 或
        {type:"range", start, stop, step}。返回字符串列表（空则 ValueError）。"""
        ptype = (spec or {}).get("type", "list")
        if ptype == "list":
            values = [str(v) for v in (spec.get("values") or [])]
        elif ptype == "range":
            start = int(spec.get("start", 0))
            stop = int(spec.get("stop", 0))
            step = max(1, int(spec.get("step", 1)))
            values = [str(i) for i in range(start, stop, step)]
        else:
            raise ValueError(f"非法 payload 类型: {ptype}（list / range）")
        if not values:
            raise ValueError("payload 集为空")
        return values

    @staticmethod
    def markers(template: dict) -> list[str]:
        """模板中的标记位（§POS1§…）；body 与 url 都参与替换。"""
        text = str(template.get("url", "")) + "\n" + str(template.get("body") or "")
        return sorted(set(_MARKER_RE.findall(text)))

    def run(self, project_id: str, template: dict, payload_specs: list[dict],
            *, batch_id: str, concurrency: int = 5, rate_per_sec: float = 10.0,
            max_requests: int | None = None, stop_event: threading.Event | None = None,
            progress_cb=None, session_id: str | None = None,
            author: str = "human") -> dict:
        """阻塞执行爆破。返回 {total, done, failed, batch_id, stopped}。"""
        cfg = self.config
        max_conc = cfg.intruder_max_concurrency if cfg else 5
        concurrency = max(1, min(int(concurrency), max_conc))  # 硬顶
        max_requests = min(
            int(max_requests or (cfg.intruder_max_requests if cfg else 1000)),
            cfg.intruder_max_requests if cfg else 1000)
        rate = min(float(rate_per_sec or 10.0),
                   cfg.intruder_rate_per_sec if cfg else 10.0)

        url = str(template.get("url") or "")
        method = str(template.get("method") or "GET").upper()
        headers = dict(template.get("headers") or {})
        body = template.get("body") or None
        if not url:
            raise ValueError("缺少爆破目标 url")
        marks = self.markers(template)
        if not marks:
            raise ValueError("模板未包含任何 §POS1§ 标记——在 url/body 中用 §名字§ 标出替换位")
        specs = {s.get("position"): s for s in (payload_specs or [])}
        missing = [m for m in marks if m not in specs]
        if missing:
            raise ValueError(f"标记缺少 payload 集: {missing}")

        # 展开为 (marker -> value) 的组合列表（多标记笛卡尔积，受 max_requests 截断）
        value_lists = [self.expand_payloads(specs[m]) for m in marks]
        combos = list(itertools.product(*value_lists))[:max_requests]
        total = len(combos)

        self.bb.append_event(
            project_id, "browser.intruder.start",
            {"batch_id": batch_id, "method": method, "url": url,
             "markers": marks, "total": total, "concurrency": concurrency,
             "rate_per_sec": rate},
            session_id=session_id, author=author)

        # token-bucket 限速（全局共享，线程安全）
        interval = 1.0 / rate if rate > 0 else 0.0
        next_slot = [0.0]
        rate_lock = threading.Lock()

        def _acquire_slot():
            if interval <= 0:
                return
            with rate_lock:
                t = time.monotonic()
                wait = next_slot[0] - t
                next_slot[0] = max(next_slot[0], t) + interval
            if wait > 0:
                time.sleep(wait)

        state = {"done": 0, "failed": 0, "consec_fail": 0, "stopped": False}
        state_lock = threading.Lock()

        def _worker(idx: int, combo: tuple) -> None:
            if stop_event is not None and stop_event.is_set():
                return
            with state_lock:
                if state["stopped"]:
                    return
            _acquire_slot()
            payload_map = dict(zip(marks, combo))
            req_url = url
            req_body = body
            for k, v in payload_map.items():
                req_url = req_url.replace(f"§{k}§", v)
                if req_body is not None:
                    req_body = str(req_body).replace(f"§{k}§", v)
            t0 = time.monotonic()
            ok = False
            try:
                # trust_env=False 同重放（见上）：爆破流量不交系统代理
                resp = httpx.Client(timeout=15.0, follow_redirects=True,
                                    trust_env=False).request(
                    method, req_url, headers=headers,
                    content=req_body if req_body else None)
                status = resp.status_code
                resp_headers = dict(resp.headers)
                mime = resp_headers.get("content-type", "")
                resp_text, resp_trunc, resp_bin = normalize_body(
                    resp.content, mime, self._body_max)
                ok = True
            except httpx.HTTPError as e:
                status = None
                resp_headers, mime, resp_text = {}, "", None
                resp_trunc = resp_bin = False
                err = str(e)[:200]
            finally:
                duration_ms = int((time.monotonic() - t0) * 1000)
            try:
                self.bb.add_http_history(
                    project_id, source="intruder", session_id=session_id,
                    batch_id=batch_id,
                    meta={"payload": payload_map, "index": idx,
                          **({} if ok else {"error": err})},
                    method=method, url=req_url, status=status,
                    req_headers=headers, req_body=req_body,
                    resp_headers=resp_headers, resp_body=resp_text,
                    resp_mime=mime, body_truncated=resp_trunc,
                    is_binary=resp_bin, duration_ms=duration_ms)
            except Exception:  # noqa: BLE001 —— 入库失败不中断批次
                pass
            with state_lock:
                state["done"] += 1
                if ok:
                    state["consec_fail"] = 0
                else:
                    state["failed"] += 1
                    state["consec_fail"] += 1
                    if state["consec_fail"] >= _CONSECUTIVE_FAIL_LIMIT:
                        state["stopped"] = True
            if progress_cb is not None:
                try:
                    progress_cb(state["done"], total)
                except Exception:  # noqa: BLE001
                    pass

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(lambda args: _worker(*args),
                          ((i, c) for i, c in enumerate(combos)), chunksize=1))

        stopped = bool(
            state["stopped"] or (stop_event is not None and stop_event.is_set()))
        self.bb.append_event(
            project_id, "browser.intruder.done",
            {"batch_id": batch_id, "total": total, "done": state["done"],
             "failed": state["failed"], "stopped": stopped},
            session_id=session_id, author=author)
        return {"batch_id": batch_id, "total": total, "done": state["done"],
                "failed": state["failed"], "stopped": stopped}
