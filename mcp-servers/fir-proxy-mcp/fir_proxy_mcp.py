"""fir-proxy 代理池 MCP server（stdio，平台控制面）。

**薄客户端**：不自己管池、不起 serve——只把平台 API（默认 127.0.0.1:8420）的
代理池端点暴露成 MCP 工具，让 AI 能查端点/IP 列表、起停服务、轮换、抓取验证。

设计依据 `docs/plans/proxy-pool-integration.md`：
- **平台托管 + MCP 控制面**：serve 生命周期归平台（`core/proxy/pool.py`），
  人类 UI 与 AI 共享同一池；MCP 子进程随会话起停，不该持有长驻状态。
- **甲（AI 用轮换代理）**：`proxy_status` 给 http/socks5 端点、`proxy_list` 给可用 IP，
  AI 自行在 `run_cmd` 里 `curl -x socks5://127.0.0.1:<port> ...`；平台不注入。

环境变量：
- ``CYBERSTRIKE_API`` 平台基址（默认 ``http://127.0.0.1:8420``）。
- ``PW_PROJECT_ID`` 当前项目 id（平台按项目注入；也可每个工具显式传 ``project_id``）。
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.getenv("CYBERSTRIKE_API", "http://127.0.0.1:8420").rstrip("/")
DEFAULT_PID = os.getenv("PW_PROJECT_ID", "").strip()
TIMEOUT = float(os.getenv("FIR_PROXY_MCP_TIMEOUT", "120"))

mcp = FastMCP("fir-proxy")


def _pid(project_id: str | None) -> str:
    pid = (project_id or DEFAULT_PID).strip()
    if not pid:
        raise ValueError("缺少 project_id（平台未注入 PW_PROJECT_ID，请显式传 project_id）")
    return pid


def _call(method: str, path: str, body: dict | None = None) -> Any:
    """打平台 API；非 2xx 抛出带 detail 的 RuntimeError（供 MCP 回结构化错误）。"""
    url = f"{API_BASE}{path}"
    try:
        resp = httpx.request(method, url, json=body, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"平台 API 不可达（{API_BASE}）: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:300]
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
    return resp.json()


@mcp.tool()
def proxy_status(project_id: str | None = None) -> dict:
    """查代理池状态：服务是否在跑、本地入口端点（http/socks5）、当前代理、池计数与地区分布。

    服务未启动时 `running=false`（池文件仍在，可 `proxy_list` 看存量）。
    """
    return _call("GET", f"/api/projects/{_pid(project_id)}/proxy/status")


@mcp.tool()
def proxy_list(project_id: str | None = None) -> dict:
    """列可用代理（proxy/protocol/location/latency/score/status）。

    `source=serve` 为运行中实时池；`source=file` 为池文件存量。AI 据此挑代理。
    """
    return _call("GET", f"/api/projects/{_pid(project_id)}/proxy/proxies")


@mcp.tool()
def proxy_start(project_id: str | None = None) -> dict:
    """启动代理池本地服务（HTTP + SOCKS5 入口，内建轮换/故障切换）。池空或依赖缺失会报错。"""
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/start")


@mcp.tool()
def proxy_stop(project_id: str | None = None) -> dict:
    """停止代理池本地服务（池记录保留）。"""
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/stop")


@mcp.tool()
def proxy_rotate(project_id: str | None = None) -> dict:
    """手动轮换到下一个代理，返回新的当前代理。需服务在跑。"""
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/rotate")


@mcp.tool()
def proxy_select(limit: int = 1, region: str | None = None,
                 max_latency_ms: float | None = None,
                 project_id: str | None = None) -> dict:
    """按评分挑最合适的代理（可选地区 / 延迟上限）。不启服务也可用。"""
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/select",
                 {"limit": limit, "region": region, "max_latency_ms": max_latency_ms})


@mcp.tool()
def proxy_add(records: list[dict], project_id: str | None = None) -> dict:
    """把代理记录并入池（每条含 `proxy`（host:port），可选 `protocol`/`location` 等）。"""
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/add",
                 {"records": records})


@mcp.tool()
def proxy_remove(addresses: list[str], project_id: str | None = None) -> dict:
    """按地址（host:port）从池中移除代理。"""
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/remove",
                 {"addresses": addresses})


@mcp.tool()
def proxy_fetch(protocols: list[str] | None = None,
                project_id: str | None = None) -> dict:
    """从在线源抓取代理入池（联网，较慢）。`protocols` 可选 http/socks4/socks5，空=全部。

    返回 `{job_id}`；抓取完成后用 `proxy_list` 查看。
    """
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/fetch",
                 {"protocols": protocols or []})


@mcp.tool()
def proxy_validate(workers: int = 50, project_id: str | None = None) -> dict:
    """批量验证池内全部代理（联网，慢）：回写 status/latency/score。返回 `{job_id}`。"""
    return _call("POST", f"/api/projects/{_pid(project_id)}/proxy/validate",
                 {"workers": workers})


if __name__ == "__main__":
    mcp.run()
