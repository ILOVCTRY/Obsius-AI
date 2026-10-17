"""诊断：LLM 请求体留痕（diagnostics-request-body，2026-10-17）。

**动因**：诊断页能看「系统提示词快照」和消息流，但看不到**真正发往服务端的请求体**
（system 是否在 messages[0]、工具结果有没有随下一步带上去、tools schema 长什么样）。
排查这类问题时只能读代码推断。本模块把 `core/llm/dump.py` 回调上来的 record 按
「项目 / 会话」落成 JSONL，供诊断页直接查看。

**开关**：`config/llm_dump.json` 的 `{"enabled": true}`（诊断页开关写入，**默认关**）。
关闭时 sink 直接返回，不产生任何文件。

**存储**：`logs/llm_requests/<project_id>/<session_id>.jsonl`，一行一条 record；
每个会话**只保留最近 `KEEP_PER_SESSION=20` 条**（追加时裁剪），单条正文超过
`STORE_MAX_BYTES`（默认 4MB）只留元信息——防止长会话把磁盘写满。

**红线**：所有写路径异常只 log 不抛（诊断绝不能影响对话/任务轮）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENV_BASE = "CYBERSTRIKE_LLM_DUMP_STORE_DIR"
ENV_SETTINGS = "CYBERSTRIKE_LLM_DUMP_SETTINGS"
ENV_STORE_MB = "CYBERSTRIKE_LLM_DUMP_STORE_MB"

KEEP_PER_SESSION = 20
DEFAULT_STORE_MB = 4

_lock = threading.Lock()
_enabled_cache: tuple[float, bool] | None = None


# ---------- 开关 ----------

def settings_path() -> Path:
    return Path(os.environ.get(ENV_SETTINGS) or "config/llm_dump.json")


def _read_settings() -> dict[str, Any]:
    try:
        raw = settings_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 —— 坏文件按关闭处理
        log.exception("请求体开关读取失败 path=%s", settings_path())
        return {}


def is_enabled() -> bool:
    """当前是否记录请求体（按文件 mtime 缓存，一次 stat/调用）。"""
    global _enabled_cache
    path = settings_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    if _enabled_cache and _enabled_cache[0] == mtime:
        return _enabled_cache[1]
    value = bool(_read_settings().get("enabled"))
    _enabled_cache = (mtime, value)
    return value


def set_enabled(enabled: bool) -> dict[str, Any]:
    """写开关（幂等）。返回最新设置视图。"""
    global _enabled_cache
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_settings()
        data["enabled"] = bool(enabled)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 —— 写失败由 API 层转 500，不在读写路径炸
        log.exception("请求体开关写入失败 path=%s", path)
        raise
    _enabled_cache = None
    return settings_view()


def settings_view() -> dict[str, Any]:
    return {"enabled": is_enabled(), "dir": str(base_dir())}


# ---------- 存储 ----------

def base_dir() -> Path:
    return Path(os.environ.get(ENV_BASE) or Path("logs") / "llm_requests")


def _session_file(project_id: str, session_id: str) -> Path:
    safe_pid = _safe(project_id) or "_unknown"
    safe_sid = _safe(session_id) or "_unknown"
    return base_dir() / safe_pid / f"{safe_sid}.jsonl"


def _safe(name: str) -> str:
    """目录/文件名白名单化（防路径穿越；会话 id 只会是 chat-xxx/sess-xxx）。"""
    return "".join(c for c in str(name or "")
                   if c.isalnum() or c in "-_.")[:96]


def _rid(ts: str, sha: str) -> str:
    """记录 id：时间戳去符号 + payload 指纹前 8 位（可安全放进 URL 路径）。"""
    stamp = "".join(c for c in str(ts or "") if c.isalnum())
    return f"{stamp}-{sha[:8]}"


def _store_max_bytes() -> int:
    raw = (os.environ.get(ENV_STORE_MB) or "").strip()
    try:
        mb = float(raw) if raw else DEFAULT_STORE_MB
    except ValueError:
        mb = DEFAULT_STORE_MB
    return max(1, int(mb * 1024 * 1024))


def store(record: dict) -> str | None:
    """落一条请求体（sink 入口）。未开启/无会话归属 → 直接丢弃。"""
    if not is_enabled():
        return None
    project_id = str(record.get("project_id") or "")
    session_id = str(record.get("session_id") or "")
    if not session_id:
        return None
    try:
        row = dict(record)
        if int(row.get("bytes") or 0) > _store_max_bytes():
            row["truncated"] = True
            row["request"] = {"_omitted": f"正文 {row['bytes']} bytes 超过存储上限"}
        rid = _rid(row.get("ts", ""), str(row.get("sha256") or ""))
        row["rid"] = rid
        line = json.dumps(row, ensure_ascii=False)
        path = _session_file(project_id, session_id)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            if path.is_file():
                lines = path.read_text(encoding="utf-8").splitlines()
            lines.append(line)
            if len(lines) > KEEP_PER_SESSION:
                lines = lines[-KEEP_PER_SESSION:]
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        return rid
    except Exception:  # noqa: BLE001 —— 诊断写入失败绝不影响请求
        log.exception("请求体落盘失败 project=%s session=%s", project_id, session_id)
        return None


def _extract_inline_system(msgs: list) -> tuple[str, bool]:
    """system 兼容通道（2026-10-17）：system 被前置于首条 user 消息（网关丢 system
    字段时的做法，见 openai_compat.SYSTEM_IN_USER_BEGIN）。返回 (system 文本, 是否命中)。
    """
    try:
        from core.llm.openai_compat import (SYSTEM_IN_USER_BEGIN,
                                            SYSTEM_IN_USER_END)
    except Exception:  # noqa: BLE001 —— 导入失败按没有处理
        return "", False
    for m in msgs:
        if not (isinstance(m, dict) and m.get("role") == "user"):
            continue
        c = m.get("content")
        if not isinstance(c, str) or not c.startswith(SYSTEM_IN_USER_BEGIN):
            continue
        end = c.find(SYSTEM_IN_USER_END)
        body = c[len(SYSTEM_IN_USER_BEGIN):end if end >= 0 else None]
        return body.strip("\n"), True
    return "", False


def _summarize(row: dict) -> dict[str, Any]:
    """从 record 提炼列表页要用的统计（不返回正文）。"""
    req = row.get("request") or {}
    msgs = req.get("messages")
    if msgs is None and isinstance(req.get("input"), list):
        msgs = req["input"]
    msgs = msgs or []
    system = req.get("system") or req.get("instructions") or ""
    if isinstance(system, list):
        system = "".join(str(b.get("text", "")) if isinstance(b, dict) else str(b)
                         for b in system)
    tool_results = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            tool_results += 1
        elif m.get("role") == "tool":
            tool_results += 1
    if not system:
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "system":
                c = m.get("content")
                system = c if isinstance(c, str) else ""
                break
    # system 兼容通道：标准 system 字段缺失时，从首条 user 消息的边界标记里取回，
    # 让「system N 字」仍然反映真实注入量（否则会显示 0，误导成「没注入」）。
    system_in_user = False
    if not system:
        system, system_in_user = _extract_inline_system(msgs)
    head_is_system = bool(msgs) and isinstance(msgs[0], dict) and (
        msgs[0].get("role") == "system"
        or (system_in_user and msgs[0].get("role") == "user"))
    return {
        "rid": row.get("rid", ""),
        "ts": row.get("ts", ""),
        "provider": row.get("provider", ""),
        "model": row.get("model", ""),
        "bytes": int(row.get("bytes") or 0),
        "truncated": bool(row.get("truncated")),
        "messages": len(msgs),
        "tool_results": tool_results,
        "system_chars": len(system or ""),
        "system_first": head_is_system,
        "system_in_user": system_in_user,
        "tools": len(req.get("tools") or []),
        # 真正出网字节（httpx 层录到）：wire_match=False 说明 SDK 重新序列化后
        # 字节与本地 payload 不同（键序/空白/转义/默认字段），字段仍一致。
        "wire_match": bool(row.get("wire_match")),
        "wire_bytes": int((row.get("wire") or [{}])[-1].get("bytes") or 0),
        "wire_sha256": str((row.get("wire") or [{}])[-1].get("sha256") or ""),
    }


def list_records(project_id: str, session_id: str,
                 limit: int = KEEP_PER_SESSION) -> list[dict[str, Any]]:
    """会话的请求体清单（最新在前，只含统计不含正文）。"""
    path = _session_file(project_id, session_id)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        log.exception("请求体清单读取失败 %s", path)
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            out.append(_summarize(json.loads(line)))
        except Exception:  # noqa: BLE001 —— 坏行跳过
            continue
        if len(out) >= max(1, limit):
            break
    return out


def read_record(project_id: str, session_id: str, rid: str) -> dict[str, Any] | None:
    """按 rid 取完整 record（含 request 正文）。"""
    path = _session_file(project_id, session_id)
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        log.exception("请求体详情读取失败 %s", path)
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if str(row.get("rid") or "") == rid:
            return row
    return None


def summary() -> dict[str, Any]:
    """总览：会话文件数 / 记录数 / 占用字节（诊断页开关旁展示）。"""
    base = base_dir()
    files = 0
    records = 0
    size = 0
    if base.is_dir():
        for p in base.glob("*/*.jsonl"):
            files += 1
            try:
                size += p.stat().st_size
                records += sum(1 for ln in p.read_text(encoding="utf-8").splitlines()
                               if ln.strip())
            except Exception:  # noqa: BLE001
                continue
    return {"enabled": is_enabled(), "dir": str(base),
            "sessions": files, "records": records, "bytes": size}
