"""重发与爆破引擎（F6 批 3，DESIGN.md §7）：独立 HTTP 客户端，零 playwright 依赖。

- ``ReplayClient``：单条重放。可从 http_history 取模板（capture_id）或直接收原始
  报文（raw，经 ``httpmsg.parse_raw_request`` 解析）；结果入库 source="replay"。
  **双传输（2026-10-07）**：默认 httpx（显式代理、恒 ``trust_env=False``）；勾选国密
  TLS 时切 ``gmhttp`` sidecar（Python 生态无带 SM 密码套件的 TLS 栈，见其模块 docstring）。
- ``Intruder``：**人类 UI 专属**爆破（决策红线：AI 无任何发起入口，只能经
  bb_query-式只读读 http_history 结果）。模板 + ``§POS1§`` 标记替换 + payload 集
  + 线程池 + token-bucket 限速；并发**硬顶** config.intruder_max_concurrency
  （config 可降不可升）；结果逐请求入库 source="intruder"，审计只在批次级。
  **本期仍走 httpx**（国密爆破后置）。

重发与爆破支持任意 URL；HTTP 报文解析与执行沿用现有实现。
"""

from __future__ import annotations

import base64
import itertools
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from core.browser.capture import normalize_body
from core.browser.pool import BrowserError

__all__ = ["ReplayClient", "Intruder", "ReplayOptions", "ReplayStopped"]


class ReplayStopped(RuntimeError):
    """重发被 stop_event 中断（用户点「停止」）。"""


@dataclass
class ReplayOptions:
    """重发传输选项（2026-10-07，全量对齐 Yakit 式 Repeater 控件）。

    默认值即「与既有行为一致」：跟随重定向、无代理、走 httpx、证书校验开。
    ``gm_tls=True`` 才切国密 sidecar（其余控件在两条传输上都生效）。
    """
    force_https: bool = False          # 请求行是 http:// 时升级为 https://
    follow_redirects: bool = True
    proxy: str | None = None           # 显式代理；None=直连（绝不读系统代理）
    body_max_bytes: int | None = None  # 响应体截断上限；None=用 config/默认
    insecure: bool = False             # 跳过证书校验
    gm_tls: bool = False               # 国密 TLS（GM/T 0024），走 gmhttp sidecar
    timeout_s: float = 15.0
    server_name: str | None = None     # SNI 覆写（国密站点常用）
    client_cert_pem: str | None = None  # 国密双证书：两块 CERTIFICATE 拼接
    client_key_pem: str | None = None
    ca_cert_pem: str | None = None


def _apply_force_https(url: str) -> str:
    """强制 HTTPS：仅把明文 http:// 升级为 https://（已是 https 或其它 scheme 不动）。"""
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _b64_or_empty(text: str | None) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii") if text else ""


def _meta_of(opts: "ReplayOptions", capture_id: int | None, modified: bool) -> dict:
    """入库 meta（成功与失败两条路共用，口径一致）。"""
    meta: dict = {"replayed_from": capture_id, "modified": modified}
    if opts.gm_tls:
        meta["gm_tls"] = True
    if opts.proxy:
        meta["proxy"] = opts.proxy
    return meta


_MARKER_RE = re.compile(r"§([A-Za-z0-9_]+)§")
_CONSECUTIVE_FAIL_LIMIT = 10


def _now_ms() -> int:
    return int(time.time() * 1000)


# DNS 解析失败文案（跨平台；httpx 会把底层 socket.gaierror 包成 ConnectError，
# 异常链或消息里二者其一即可判定）
_DNS_FAIL_HINTS = (
    "getaddrinfo failed",            # Windows WSAHOST_NOT_FOUND（Errno 11002）
    "name or service not known",     # Linux
    "nodename nor servname provided",  # macOS
    "no address associated with hostname",
    "temporary failure in name resolution",
)


def _dns_failure(exc: BaseException) -> bool:
    """判定异常（含 ``__cause__``/``__context__`` 链）是否 DNS 解析失败。"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return True
        cur = cur.__cause__ or cur.__context__
    text = str(exc).lower()
    return any(hint in text for hint in _DNS_FAIL_HINTS)


def _host_of(url: str) -> str:
    """URL → 主机名（解析不出时回退原串，供错误文案展示）。"""
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


class ReplayClient:
    """单条重放（httpx sync Client 或国密 sidecar；JobRegistry 线程内执行）。"""

    def __init__(self, bb, *, config=None, tools_root: str = "tools"):
        self.bb = bb
        self.config = config  # BrowserConfig（body_max_bytes）；None 用默认
        self.tools_root = tools_root

    @property
    def _body_max(self) -> int:
        return self.config.body_max_bytes if self.config else 65536

    def replay(self, project_id: str, *, capture_id: int | None = None,
               raw: str | None = None,
               session_id: str | None = None, task_id: str | None = None,
               author: str = "human",
               opts: ReplayOptions | None = None,
               stop_event: threading.Event | None = None) -> dict:
        """重放一条请求（F6-v3：原始报文 `raw` 或 capture_id 模板，二选一）。
        raw 给定时经 ``parse_raw_request`` 解析（相对路径用模板 url 兜底拼绝对），
        ``modified=True``。返回入库后的完整行（get_http_history 形态）。

        ``opts`` 控制传输（强制HTTPS/重定向/代理/体长/跳过校验/国密）；``stop_event``
        置位即中断（httpx=放弃等待，国密 sidecar=kill 子进程）。
        """
        opts = opts or ReplayOptions()
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
        # 改包重发语义：报文里的 Content-Length 常与改后 body 不符（改一行就错位），
        # 剥掉 framing 头由客户端按实际 body 重算——同 Burp/Yakit 自动重算，与 capture
        # 拦截路径（剥 content-length/host）同口径。否则 httpx 直接报「Too little data
        # for declared Content-Length」请求发不出去。
        hs = {k: v for k, v in hs.items()
              if k.lower() not in ("content-length", "transfer-encoding")}
        if not u:
            raise ValueError("缺少重放目标 url（贴原始报文或给 capture_id）")
        if opts.force_https:
            u = _apply_force_https(u)
        batch_id = f"rp-{uuid.uuid4().hex[:12]}"
        body_max = opts.body_max_bytes if opts.body_max_bytes else self._body_max
        t0 = time.monotonic()
        try:
            (status, resp_headers, mime, resp_text,
             resp_trunc, resp_bin) = self._execute(m, u, hs, b, opts, body_max, stop_event)
        except (httpx.HTTPError, ReplayStopped) as e:
            self._store_failure(project_id, capture_id, modified, session_id, task_id,
                                batch_id, m, u, hs, b, t0, str(e), opts)
            if isinstance(e, ReplayStopped):
                raise BrowserError(str(e)) from e
            if _dns_failure(e):
                raise BrowserError(
                    f"域名解析失败：{_host_of(u)} 在本机 DNS 解析不出来"
                    f"（内网域名，或需经代理访问）；原始错误: {e}") from e
            raise BrowserError(f"重放请求失败: {e}") from e
        except Exception as e:  # noqa: BLE001 —— 国密 sidecar 不可用等：失败也入库
            self._store_failure(project_id, capture_id, modified, session_id, task_id,
                                batch_id, m, u, hs, b, t0, str(e), opts)
            raise BrowserError(f"重放请求失败: {e}") from e
        duration_ms = int((time.monotonic() - t0) * 1000)
        meta = _meta_of(opts, capture_id, modified)
        row_id = self.bb.add_http_history(
            project_id, source="replay", session_id=session_id, task_id=task_id,
            batch_id=batch_id, meta=meta,
            method=m, url=u, status=status,
            req_headers=hs, req_body=b,
            resp_headers=resp_headers, resp_body=resp_text, resp_mime=mime,
            body_truncated=resp_trunc, is_binary=resp_bin,
            duration_ms=duration_ms)
        return self.bb.get_http_history(project_id, row_id)

    # ---------- 传输 ----------

    def _execute(self, m, u, hs, b, opts: ReplayOptions, body_max: int,
                 stop_event: threading.Event | None):
        if opts.gm_tls:
            return self._execute_gm(m, u, hs, b, opts, body_max, stop_event)
        return self._execute_httpx(m, u, hs, b, opts, body_max, stop_event)

    def _execute_httpx(self, m, u, hs, b, opts: ReplayOptions, body_max: int,
                       stop_event: threading.Event | None):
        """httpx 传输（默认路径）。中断语义=放弃等待（sync 调用不可真中止）。"""
        # trust_env=False：重放打授权目标（常为内网/localhost），流量绝不交给系统代理
        # ——httpx trust_env 经 urllib 读 Windows 注册表，系统代理（如 Clash）开着会把
        # 请求全转发走（目标失真+响应被拦改）。显式 proxy 是用户主动指定，另当别论。
        with httpx.Client(timeout=opts.timeout_s,
                          follow_redirects=opts.follow_redirects,
                          trust_env=False, proxy=opts.proxy or None,
                          verify=not opts.insecure) as client:
            box: dict = {}

            def _do() -> None:
                try:
                    box["resp"] = client.request(m, u, headers=hs,
                                                 content=b if b else None)
                except Exception as e:  # noqa: BLE001 —— 传给主线程统一处置
                    box["exc"] = e

            t = threading.Thread(target=_do, daemon=True, name="replay-http")
            t.start()
            deadline = time.monotonic() + opts.timeout_s + 5.0
            while t.is_alive():
                if stop_event is not None and stop_event.is_set():
                    raise ReplayStopped("重发已被中断")
                if time.monotonic() > deadline:
                    raise httpx.TimeoutException("重发超时")
                t.join(0.2)
            if "exc" in box:
                raise box["exc"]
            resp = box["resp"]
            resp_headers = dict(resp.headers)
            mime = resp_headers.get("content-type", "")
            resp_text, resp_trunc, resp_bin = normalize_body(resp.content, mime, body_max)
            return (resp.status_code, resp_headers, mime, resp_text, resp_trunc, resp_bin)

    def _execute_gm(self, m, u, hs, b, opts: ReplayOptions, body_max: int,
                    stop_event: threading.Event | None):
        """国密 TLS 传输：走 gmhttp sidecar（中断=kill 子进程，真停止）。"""
        from core.browser.gmhttp import gm_request
        spec = {
            "method": m,
            "url": u,
            "headers": {str(k): str(v) for k, v in (hs or {}).items()},
            "body_b64": _b64_or_empty(b),
            "gm": True,
            "follow_redirects": opts.follow_redirects,
            "insecure": opts.insecure,
            "proxy": opts.proxy or "",
            "timeout_ms": int(opts.timeout_s * 1000),
            "body_max_bytes": body_max,
            "server_name": opts.server_name or "",
            "client_cert_b64": _b64_or_empty(opts.client_cert_pem),
            "client_key_b64": _b64_or_empty(opts.client_key_pem),
            "ca_cert_b64": _b64_or_empty(opts.ca_cert_pem),
        }
        res = gm_request(spec, tools_root=self.tools_root,
                         timeout=opts.timeout_s + 10.0, abort_event=stop_event)
        if res.get("error"):
            raise httpx.HTTPError(f"国密 TLS 请求失败: {res['error']}")
        raw = base64.b64decode(res.get("body_b64") or "")
        mime = res.get("mime") or ""
        resp_text, resp_trunc, resp_bin = normalize_body(raw, mime, body_max)
        return (res.get("status"), (res.get("headers") or {}), mime,
                resp_text, bool(res.get("body_truncated")), resp_bin)

    def _store_failure(self, project_id, capture_id, modified, session_id, task_id,
                       batch_id, m, u, hs, b, t0, err, opts: ReplayOptions) -> None:
        """连接失败/中断也入库（status=None），前端可见失败原因。"""
        meta = _meta_of(opts, capture_id, modified)
        meta["error"] = str(err)[:200]
        self.bb.add_http_history(
            project_id, source="replay", session_id=session_id,
            task_id=task_id, batch_id=batch_id, meta=meta,
            method=m, url=u, status=None,
            req_headers=hs, req_body=b,
            duration_ms=int((time.monotonic() - t0) * 1000))


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
            author: str = "human", proxy: str | None = None) -> dict:
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
             "rate_per_sec": rate, **({"proxy": proxy} if proxy else {})},
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
                # trust_env=False 同重放（见上）：爆破流量不交系统代理；
                # 显式 proxy 是使用者/Agent 主动指定（如代理池本地入口做 IP 轮换）
                resp = httpx.Client(timeout=15.0, follow_redirects=True,
                                    trust_env=False, proxy=proxy or None).request(
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
                err = (f"域名解析失败：{_host_of(req_url)} 在本机 DNS 解析不出来"
                       if _dns_failure(e) else str(e))[:200]
            finally:
                duration_ms = int((time.monotonic() - t0) * 1000)
            try:
                self.bb.add_http_history(
                    project_id, source="intruder", session_id=session_id,
                    batch_id=batch_id,
                    meta={"payload": payload_map, "index": idx,
                          **({"proxy": proxy} if proxy else {}),
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
