"""出网请求体转储（llm-request-dump，2026-10-17）。

**动因**：系统提示已有落库快照（`core/diagnostics/system_prompt.py`），但「这一轮
真实发给服务端的**完整 body**」此前零留痕——排查「规则/红线到底进没进上下文」
「工具结果有没有随下一步带上去」只能靠读代码推断，无法核对线上字节。本模块在
payload 序列化之后、交给 transport 之前，原样落一份 JSON 到指定目录。

**开关（默认全关，零开销）**：
- ``CYBERSTRIKE_LLM_DUMP_DIR``：转储目录；未设/空 = 不落文件（连 env 读取都省）。
- ``set_sink(fn)``：注册回调，把同一份 record 交给诊断层（`core/diagnostics/requests.py`
  写 JSONL 供诊断页查看）。诊断页的「记录请求体」开关走这条路径，**不需要设环境变量**。
- ``CYBERSTRIKE_LLM_DUMP_MAX_MB``：单份上限，默认 32；超限只落元信息 + 摘要。

**会话归属**：LLM 层不知道项目/会话，故由运行链用 ``scope(project_id, session_id)``
（contextvar）包住一轮，``dump_request`` 读当前作用域一起交给 sink。无作用域时
sink 不落（后台线程等场景宁缺不错记）。

**红线**：转储**自身**的任何异常只 log 不抛——绝不能影响真实请求（与系统提示快照同口径）。
但注意方向：只许降级自己，**不许吞调用方的异常**。`capture_request` 的 ``with`` 体
必须完全透明（见该函数 docstring，2026-10-24 修）。
每次调用内按 payload 的 sha256 去重（``seen``），故重试/降级重发同一份不会刷屏，
只有内容真变了（如剥掉 thinking 参数）才多落一份。

落盘形态::

    {
      "ts": "2026-10-17T02:50:11.123+00:00",
      "provider": "openai",           # openai / anthropic
      "format": "openai-chat-completions",
      "url": "https://host/v1/chat/completions",
      "model": "deepseek-v3",
      "bytes": 18234,
      "sha256": "…",                  # payload 原文指纹
      "truncated": false,             # 超限时 true，request 缺省
      "request": { …原始 body… },      # 解析失败时退 {"_raw": "<文本>"}
      "wire": [{"bytes": 2173, "sha256": "bc6c42b1…"}],  # httpx hook 录到的真实出网字节（capture_request 才有）
      "wire_match": false             # 唯一一次出网且字节==payload 才 true；false=SDK 重序列化，字段仍一致
    }

核对脚本：``scripts/llm_dump_inspect.py <目录|文件>``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

log = logging.getLogger(__name__)

ENV_DIR = "CYBERSTRIKE_LLM_DUMP_DIR"
ENV_MAX_MB = "CYBERSTRIKE_LLM_DUMP_MAX_MB"
DEFAULT_MAX_MB = 32

_seq_lock = threading.Lock()
_seq = 0

# 诊断层回调（core/diagnostics/requests.py 注册）：record → 落库/落盘
_sink: Callable[[dict], None] | None = None

# 当前会话作用域：运行链每轮 set，dump_request 读取后随 record 交给 sink
_scope: ContextVar[dict | None] = ContextVar("llm_dump_scope", default=None)


def set_sink(fn: Callable[[dict], None] | None) -> None:
    """注册/清除诊断回调（进程级，单例）。异常由本模块吞掉。"""
    global _sink
    _sink = fn


@contextmanager
def scope(project_id: str, session_id: str) -> Iterator[None]:
    """把一段执行（一轮对话/一次任务）标记为某个项目会话，供 sink 归属。

    contextvar 不跨线程传播：子线程（专家/后台任务）需各自在入口再包一层。
    """
    token = _scope.set({"project_id": project_id or "",
                        "session_id": session_id or ""})
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> dict | None:
    return _scope.get()


def dump_dir() -> str:
    """当前转储目录（未启用返回空串）。"""
    return (os.environ.get(ENV_DIR) or "").strip()


def enabled() -> bool:
    return bool(dump_dir())


def _max_bytes() -> int:
    raw = (os.environ.get(ENV_MAX_MB) or "").strip()
    try:
        mb = float(raw) if raw else DEFAULT_MAX_MB
    except ValueError:
        mb = DEFAULT_MAX_MB
    return max(1, int(mb * 1024 * 1024))


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def _body_of(payload: bytes) -> tuple[dict, bool]:
    """payload → (可序列化 dict, 是否截断)。超限只回元信息。"""
    if len(payload) > _max_bytes():
        return {"_omitted": f"payload {len(payload)} bytes 超过上限，仅记元信息"}, True
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001 —— 非 JSON（理论上不会）也照样留原文
        return {"_raw": payload.decode("utf-8", "replace")}, False
    return (parsed if isinstance(parsed, dict) else {"_raw": parsed}), False


def _record_enabled() -> bool:
    """是否值得构建 record（无文件出口且无可用 sink+作用域时直接跳过）。

    有 sink + 作用域时还要看诊断页开关：``requests.store()`` 首行就是
    ``if not is_enabled(): return None``，开关关着时建 record 纯属白做。
    此前本函数不看开关，导致「诊断功能已关闭」≠「这条代码路径没走」——
    排查时极易误判（2026-10-24 那次吞异常的 bug 正是这样被掩盖的）。
    """
    if dump_dir():
        return True
    sink = _sink
    if sink is None or current_scope() is None:
        return False
    try:
        # 延迟 import：core.diagnostics.requests 目前不 import 本模块，
        # 但不把这条依赖提到模块顶部，免得将来反向引用时形成环。
        from core.diagnostics import requests as req_log
        return bool(req_log.is_enabled())
    except Exception:  # noqa: BLE001 —— 读开关失败按「关」处理（宁缺不错记）
        return False


def _emit(record: dict) -> str | None:
    """把 record 分别交给文件转储与诊断回调（sink）。"""
    root = dump_dir()
    path = _write_file(root, record["provider"], record) if root else None
    sink = _sink
    sc = current_scope() if sink is not None else None
    if sink is not None and sc is not None:
        try:
            sink({**record, "project_id": sc.get("project_id", ""),
                  "session_id": sc.get("session_id", "")})
        except Exception:  # noqa: BLE001 —— 诊断回调失败同样不得影响请求
            log.exception("请求体诊断回调失败 provider=%s", record.get("provider"))
    return path


def dump_request(url: str, payload: bytes, *, provider: str,
                 model: str | None = None, fmt: str = "",
                 seen: set[str] | None = None) -> str | None:
    """即时落一份请求体（文件 / 诊断回调）。返回文件路径；未启用/已转储/失败返回 None。

    注意：**不含实际出网字节**（SDK 会重新序列化）。运行链请用 `capture_request`——
    它会在调用结束后补上 httpx 层录到的真实请求字节。本函数保留给脚本/测试用。
    """
    if not _record_enabled():
        return None
    try:
        digest = hashlib.sha256(payload).hexdigest()
        if seen is not None:
            if digest in seen:
                return None
            seen.add(digest)
        body, truncated = _body_of(payload)
        return _emit(_new_record(url, payload, provider, model, fmt,
                                 digest, body, truncated))
    except Exception:  # noqa: BLE001 —— 转储失败绝不影响请求
        log.exception("请求体转储失败 url=%s provider=%s", url, provider)
        return None


def _begin_capture(url: str, payload: bytes, *, provider: str,
                   model: str | None, fmt: str, seen: set[str] | None) -> dict | None:
    """进入阶段：算指纹、取 body、武装 wire 钩子。返回收尾上下文。

    返回 None 表示「本次不必留痕」（同一 payload 已转储过）。
    **本函数允许抛异常**——由 `capture_request` 兜住并降级为「不留痕」。
    """
    digest = hashlib.sha256(payload).hexdigest()
    if seen is not None:
        if digest in seen:
            return None
        seen.add(digest)
    body, truncated = _body_of(payload)
    end_wire: Callable[[], list[bytes]] | None = None
    try:
        from core.llm.sdk_engine import (begin_wire_capture,
                                         end_wire_capture)
    except Exception:  # noqa: BLE001 —— 拿不到钩子就只记 payload
        end_wire = None
    else:
        try:
            begin_wire_capture()
            end_wire = end_wire_capture
        except Exception:  # noqa: BLE001 —— 同上，降级为只记 payload
            try:
                end_wire_capture()   # 清掉可能已武装的线程缓冲，免得污染后续调用
            except Exception:        # noqa: BLE001
                pass
            end_wire = None
    return {"url": url, "payload": payload, "provider": provider,
            "model": model, "fmt": fmt, "digest": digest, "body": body,
            "truncated": truncated, "end_wire": end_wire}


def _finish_capture(ctx: dict) -> None:
    """收尾阶段：取回本次（含重试）真实出网字节，补进 record 后落盘。"""
    wire: list[bytes] = []
    end_wire = ctx.get("end_wire")
    if end_wire is not None:
        try:
            wire = end_wire()
        except Exception:  # noqa: BLE001
            wire = []
    digest = ctx["digest"]
    payload = ctx["payload"]
    record = _new_record(ctx["url"], payload, ctx["provider"], ctx["model"],
                         ctx["fmt"], digest, ctx["body"], ctx["truncated"])
    record["wire"] = [
        {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
        for b in wire if b]
    record["wire_match"] = (
        len(wire) == 1 and len(wire[0]) == len(payload)
        and hashlib.sha256(wire[0]).hexdigest() == digest)
    _emit(record)


@contextmanager
def capture_request(url: str, payload: bytes, *, provider: str,
                    model: str | None = None, fmt: str = "",
                    seen: set[str] | None = None) -> Iterator[None]:
    """包住一次出网调用：进入时武装捕获，退出时补上**真正出网字节**再落盘。

    为什么要这个：两条 SDK 传输层都是 ``json.loads(body) → create(**req)``，真正写进
    socket 的字节由 SDK 重新序列化，与本 payload 字段相同、字节不保证相同。故在
    ``sdk_engine`` 的 httpx request hook 里按线程录制 ``request.content``，与 payload
    的 sha256 并排写进 record（``wire`` 字段），诊断页据此标注「字节是否一致」。

    **上下文管理器必须透明（红线）**：``with`` 体内的异常一律原样穿透。
    曾经这里把 ``yield`` 包在 ``try/except Exception`` 里，于是调用方的真实异常被
    转储模块吞掉，``openai_compat.chat`` 里 ``status`` 从未赋值 → 上层收到
    ``UnboundLocalError``（既非 LLMError 也非 OSError）→ 跨供应商兜底失效、
    错误分类落 ``unknown``（2026-10-22~24，13 例）。故现在只允许降级**自己**
    （装配钩子 / 落盘），``yield`` 不处于任何 ``except`` 覆盖区内。
    守护用例：``tests/test_diag_requests.py::test_capture_request_propagates_caller_exception``，
    静态扫描：``scripts/check_contextmanager_transparency.py``。
    """
    ctx: dict | None = None
    try:
        if _record_enabled():
            ctx = _begin_capture(url, payload, provider=provider, model=model,
                                 fmt=fmt, seen=seen)
    except Exception:  # noqa: BLE001 —— 转储初始化失败只降级，绝不影响请求
        log.exception("请求体转储初始化失败 url=%s provider=%s", url, provider)
        ctx = None
    if ctx is None:
        yield
        return
    try:
        yield                     # ← 不处于任何 except 覆盖区内：调用方异常原样穿透
    finally:
        # 收尾独立降级。注意 finally 里抛异常会**盖掉**正在传播的调用方异常，
        # 所以这一整段必须自己兜住（同一个坑换了个位置）。
        try:
            _finish_capture(ctx)
        except Exception:  # noqa: BLE001 —— 转储收尾失败绝不影响请求
            log.exception("请求体转储收尾失败 url=%s provider=%s", url, provider)


def _new_record(url: str, payload: bytes, provider: str, model: str | None,
                fmt: str, digest: str, body: dict, truncated: bool) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "ts": now.isoformat(timespec="milliseconds"),
        "provider": provider,
        "format": fmt,
        "url": url,
        "model": model or "",
        "bytes": len(payload),
        "sha256": digest,
        "truncated": truncated,
        "request": body,
    }



def _write_file(root: str, provider: str, record: dict) -> str | None:
    """原子写一份 JSON（文件名含时间 + 递增序号）。"""
    os.makedirs(root, exist_ok=True)
    name = (f"{time.strftime('%Y%m%dT%H%M%S')}-{_next_seq():05d}"
            f"-{provider}.json")
    path = os.path.join(root, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
