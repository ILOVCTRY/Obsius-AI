"""诊断：跨供应商兜底记录（provider-failover，2026-10-21）。

**动因**：`core/llm/fallback.py` 让「某供应商重试 11 次仍失败」时自动换下一家，
直到全链失败才停。这条链路把**失败原因**变成了必须留痕的东西——否则用户只会看到
「等了很久，最后报了个错」，无从判断是额度、风控、key 失效还是网络，也看不出到底
换过几家。本模块把三类事实落成可查的记录：

- ``llm.provider_failed``：**每一家**失败一条（含最后一家），带
  ``category``（与 ``retry.classify_provider_error`` 同源）/``status``/``error``/
  ``elapsed_ms``/``index``/``chain``/``next_provider``；
- ``llm.failover``：真正换家时一条（``from`` → ``to``），前端/审计能一眼看出
  「这一刻换过供应商」；
- ``llm.failover.exhausted``：全链耗尽、真正停止时一条（链 + 最后一家 + 最后错误）。

**双出口**（与 requests.py 同构）：

1. ``logs/llm_failover.jsonl``（``CYBERSTRIKE_LLM_FAILOVER_LOG`` 可改）——**无条件
   写**，一行一条。它是「没有项目作用域」时唯一的留痕（例如情报分类器、离线脚本），
   也是事后跨项目翻查的入口。超 ``ROTATE_BYTES`` 轮转成 ``.1``。
2. 项目黑板事件——经 ``set_sink`` 注册的回调（``create_app`` 里接），按
   ``core.llm.dump.current_scope()`` 归属到项目/会话；无作用域只写文件。诊断页
   「模型兜底」页签读的就是这条（``query_failover``）。

**红线**：任何异常只 log 不抛——诊断绝不能影响 LLM 调用本身（与 failog 同口径）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ENV_LOG = "CYBERSTRIKE_LLM_FAILOVER_LOG"
DEFAULT_LOG = "logs/llm_failover.jsonl"
ROTATE_BYTES = 8 * 1024 * 1024
ERR_MAX = 800          # error 截断（与 failog._ERR_MAX 同量级）
_SCAN_CAP = 2000

KINDS = ("llm.provider_failed", "llm.failover", "llm.failover.exhausted")

# 进程级回调：(kind, payload, project_id, session_id) -> None
_sink: Callable[[str, dict, str, str], None] | None = None
_lock = threading.Lock()


def set_sink(fn: Callable[[str, dict, str, str], None] | None) -> None:
    """注册/清除黑板落库回调（进程级单例，``create_app`` 里接）。"""
    global _sink
    _sink = fn


def log_path() -> Path:
    return Path(os.environ.get(ENV_LOG) or DEFAULT_LOG)


# ---------- 写 ----------

def _rotate(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size > ROTATE_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:  # 轮转失败不阻断（下一条照写）
        pass


def _append_file(record: dict) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            _rotate(path)
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 诊断写盘失败绝不外抛
        log.exception("兜底记录写盘失败 path=%s", log_path())


def _emit(kind: str, payload: dict, *, extra: dict | None = None) -> None:
    """一条记录：无条件写 JSONL；有作用域再交给黑板 sink。"""
    project_id = session_id = ""
    try:
        from core.llm.dump import current_scope
        sc = current_scope() or {}
        project_id = str(sc.get("project_id") or "")
        session_id = str(sc.get("session_id") or "")
    except Exception:  # noqa: BLE001
        pass
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "kind": kind,
        "project_id": project_id,
        "session_id": session_id,
        **payload,
        **(extra or {}),
    }
    _append_file(record)
    sink = _sink
    if sink is not None and project_id:
        try:
            sink(kind, payload, project_id, session_id)
        except Exception:  # noqa: BLE001 —— 事件落库失败不影响调用
            log.exception("兜底事件落库失败 kind=%s", kind)


def _clip(text: str) -> str:
    return (text or "")[:ERR_MAX]


def record_attempt(*, provider: str, model: str, category: str, status: int,
                   error: str, elapsed_ms: int, index: int, chain: list[str],
                   next_provider: str | None = None,
                   next_model: str = "") -> None:
    """记一次「某供应商失败」。``next_provider`` 非空=随后换到了它（再补一条 failover）。"""
    payload = {
        "provider": provider, "model": model, "category": category,
        "status": int(status or 0), "error": _clip(error),
        "elapsed_ms": int(elapsed_ms or 0), "index": int(index or 1),
        "chain": list(chain or []),
        "next_provider": next_provider or "", "next_model": next_model or "",
        "final": not next_provider,
    }
    _emit("llm.provider_failed", payload)
    if next_provider:
        _emit("llm.failover", {
            "from": provider, "to": next_provider,
            "from_model": model, "to_model": next_model or "",
            "category": category, "status": int(status or 0),
            "error": _clip(error),
        })


def record_exhausted(*, chain: list[str], last_provider: str, last_model: str,
                     category: str, status: int, error: str) -> None:
    """全链耗尽（真正停止）：链 + 最后一家 + 最后错误。"""
    _emit("llm.failover.exhausted", {
        "chain": list(chain or []), "provider": last_provider,
        "model": last_model, "category": category,
        "status": int(status or 0), "error": _clip(error),
        "tried": len(chain or []),
    })


# ---------- 查 ----------

def query_failover(bb, project_id: str, *, kinds: tuple[str, ...] | None = None,
                   category: str | None = None, provider: str | None = None,
                   since_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    """兜底记录查询（倒序，最新在前）。过滤在 Python 侧做（记录量级小）。"""
    want = tuple(kinds) if kinds else KINDS
    ph = ",".join("?" * len(want))
    rows = bb.conn.execute(
        f"SELECT * FROM events WHERE project_id=? AND kind IN ({ph})"
        f" AND id>? ORDER BY id DESC LIMIT ?",
        (project_id, *want, since_id, _SCAN_CAP)).fetchall()
    cat_l = (category or "").strip().lower()
    prov_l = (provider or "").strip().lower()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        p = d.get("payload")
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        p = p if isinstance(p, dict) else {}
        if cat_l and cat_l != str(p.get("category") or "").lower():
            continue
        if prov_l and prov_l not in str(p.get("provider") or "").lower() \
                and prov_l not in str(p.get("from") or "").lower():
            continue
        d["payload"] = p
        out.append(d)
        if len(out) >= limit:
            break
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """页签顶部的汇总：各类错误分布 + 换家次数 + 真正停下的次数。"""
    by_cat: dict[str, int] = {}
    by_prov: dict[str, int] = {}
    switches = exhausted = 0
    for r in rows:
        kind = r.get("kind")
        p = r.get("payload") or {}
        if kind == "llm.failover":
            switches += 1
        elif kind == "llm.failover.exhausted":
            exhausted += 1
        elif kind == "llm.provider_failed":
            cat = str(p.get("category") or "unknown")
            by_cat[cat] = by_cat.get(cat, 0) + 1
            prov = str(p.get("provider") or "?")
            by_prov[prov] = by_prov.get(prov, 0) + 1
    return {"by_category": by_cat, "by_provider": by_prov,
            "switches": switches, "exhausted": exhausted}
