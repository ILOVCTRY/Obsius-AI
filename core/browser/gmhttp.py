"""国密 TLS HTTP sidecar 客户端封装（2026-10-07）。

**为什么需要 sidecar**：Python 的 ``ssl`` 是 OpenSSL 的薄绑定，本机 OpenSSL 构建未编入
SM 密码套件，且 Python 未暴露 ``set_ciphersuites``；PyPI 上的 ``gmssl`` / ``gmalg`` /
``pygmssl`` 只有 SM2/SM3/SM4 **密码学原语**、没有 TLS 栈。Go 生态有成熟的国密 TLS 实现
（``tjfoc/gmsm`` 的 ``gmtls``，fork 自 crypto/tls 并补上 GM/T 0024 套件），故**国密传输**
走 ``tools/bin/gmhttp.exe`` 这个一次性 sidecar，其余（标准 TLS）仍走 httpx。

**协议**（一次性进程）：stdin 收一个 JSON 规格 → stdout 出一个 JSON 结果（二进制 body 走
base64）。规格/结果结构见 ``tools/gmhttp/main.go``。

红线：
- **二进制缺失 = 国密不可用**（``gm_available()`` 返 False），**绝不静默降级成普通 TLS**
  ——否则会制造「以为走了国密、实际没走」的安全假象；
- 中断 = kill 子进程（真停止，比 httpx sync 的「放弃等待」干净）；
- 只做传输，不含任何入库/策略逻辑（调用方 ``ReplayClient`` 负责）。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from core.runtime.backends import NO_WINDOW_FLAGS
from core.toolchain import load_registry, load_tool_overrides, resolve_tool

__all__ = ["GmHttpError", "GmHttpInterrupted", "gm_available", "resolve_gmhttp",
           "gm_request"]

_TOOL_NAME = "gmhttp"
_POLL_INTERVAL = 0.2


class GmHttpError(RuntimeError):
    """sidecar 不可用 / 启动失败 / 超时 / 结果不可解析。"""


class GmHttpInterrupted(GmHttpError):
    """请求被 abort_event 中断（用户点「停止」）。"""


def resolve_gmhttp(tools_root: str | Path = "tools") -> str | None:
    """经 toolchain 四来源探测 sidecar 路径；不可用返 None（不抛）。"""
    try:
        registry = load_registry(tools_root)
    except ValueError:
        return None
    entry = registry.get(_TOOL_NAME)
    if not entry:
        return None
    try:
        res = resolve_tool(_TOOL_NAME, entry, tools_root=tools_root,
                           overrides=load_tool_overrides())
    except Exception:  # noqa: BLE001 —— 探测失败一律视为不可用，绝不炸重发链路
        return None
    if res.get("status") != "ready":
        return None
    path = res.get("path")
    return str(path) if path and Path(path).is_file() else None


def gm_available(tools_root: str | Path = "tools") -> bool:
    """国密 TLS 通道是否可用（供 API 端点做能力探测，前端据此置灰开关）。"""
    return resolve_gmhttp(tools_root) is not None


def gm_request(spec: dict, *, tools_root: str | Path = "tools",
               timeout: float = 30.0,
               abort_event: threading.Event | None = None) -> dict:
    """跑一次 sidecar 请求。返回结果 dict（含 status/headers/body_b64/mime/error…）。

    超时/中断/启动失败抛 ``GmHttpError`` / ``GmHttpInterrupted``——调用方
    ``ReplayClient`` 据此决定是否回落 httpx（仅在**未勾国密**时才允许回落）。
    """
    exe = resolve_gmhttp(tools_root)
    if not exe:
        raise GmHttpError(
            "国密 sidecar 不可用：tools/bin/gmhttp.exe 缺失。"
            "构建见 scripts/build_gmhttp.py（需 Go 1.21+）")

    payload = json.dumps(spec, ensure_ascii=False).encode("utf-8")
    try:
        proc = subprocess.Popen(
            [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=NO_WINDOW_FLAGS)
    except OSError as e:
        raise GmHttpError(f"国密 sidecar 启动失败: {e}") from e

    box: dict = {}

    def _io() -> None:
        try:
            box["out"], box["err"] = proc.communicate(payload, timeout=None)
        except Exception as e:  # noqa: BLE001 —— 传给主线程统一处置
            box["exc"] = e

    t = threading.Thread(target=_io, daemon=True, name="gmhttp-io")
    t.start()

    deadline = time.monotonic() + max(1.0, timeout)
    while t.is_alive():
        if abort_event is not None and abort_event.is_set():
            _terminate(proc)
            t.join(5)
            raise GmHttpInterrupted("国密请求已被中断")
        if time.monotonic() > deadline:
            _terminate(proc)
            t.join(5)
            raise GmHttpError(f"国密 sidecar 超时（{timeout:.0f}s）")
        t.join(_POLL_INTERVAL)

    if "exc" in box:
        raise GmHttpError(f"国密 sidecar 通信失败: {box['exc']}") from box["exc"]
    out = box.get("out") or b""
    err = (box.get("err") or b"").decode("utf-8", "replace").strip()
    if not out:
        raise GmHttpError(f"国密 sidecar 无输出（rc={proc.returncode}）: {err[:200]}")
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except ValueError as e:
        raise GmHttpError(f"国密 sidecar 结果不可解析: {e}（stderr: {err[:200]}）") from e


def _terminate(proc: subprocess.Popen) -> None:
    """终止 sidecar（无孙进程，kill 足够；失败静默）。"""
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
