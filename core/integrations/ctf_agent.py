"""CyberStrike -> CTF-Agent 单题 worker 适配层。

CTF-Agent 保持独立 Python 环境和进程；本模块只负责构造题目目录、映射供应商
配置并收集 worker 输出。API key 只存在 worker 子进程环境，不写入 job 元数据。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from core.llm.providers import ProviderError, ProviderStore

_FLAG_RE = re.compile(r"FLAG FOUND:\s*(\S+)", re.IGNORECASE)


def _phase_from_line(line: str) -> str:
    text = line.lower()
    for key, words in {
        "sandbox": ("sandbox", "docker", "container"),
        "model": ("model", "llm", "token", "agent"),
        "solving": ("solve", "attempt", "tool", "finding"),
        "flag": ("flag found", "candidate"),
        "error": ("error", "traceback", "exception"),
    }.items():
        if any(word in text for word in words):
            return key
    return "running"


def _safe_name(value: str, fallback: str = "attachment.bin") -> str:
    name = Path(value or fallback).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or fallback)[:180]


def _provider_spec(provider: dict[str, Any], model: str) -> str:
    """将 CyberStrike provider 转成 CTF-Agent 的 provider/model 规格。"""
    name = str(provider.get("name") or "").strip().lower()
    fmt = str(provider.get("format") or "").strip()
    if name in {"claude-sdk", "codex", "azure", "zen", "google", "bedrock"}:
        return f"{name}/{model}"
    # CTF-Agent 当前对通用 OpenAI 兼容端点通过 azure 适配器最接近；
    # 显式标记以便日志中可追溯，真实 endpoint/key 仍由环境变量注入。
    if fmt == "anthropic-messages":
        return f"claude-sdk/{model}"
    return f"azure/{model}"


def _env_for_provider(provider: dict[str, Any], model: str) -> dict[str, str]:
    key = str(provider.get("api_key") or "")
    base = str(provider.get("base_url") or "")
    name = str(provider.get("name") or "").lower()
    env = {"CTF_AGENT_MODEL": model}
    if key:
        env["CTF_AGENT_API_KEY"] = key
        env["OPENAI_API_KEY"] = key
        env["ANTHROPIC_API_KEY"] = key
        env["AZURE_OPENAI_API_KEY"] = key
        env["GEMINI_API_KEY"] = key
        env["AWS_BEARER_TOKEN_BEDROCK"] = key
        env["OPENCODE_ZEN_API_KEY"] = key
    if base:
        env["CTF_AGENT_BASE_URL"] = base
        env["OPENAI_BASE_URL"] = base
        env["AZURE_OPENAI_ENDPOINT"] = base
    if provider.get("proxy"):
        env["HTTP_PROXY"] = str(provider["proxy"])
        env["HTTPS_PROXY"] = str(provider["proxy"])
    if name == "azure":
        env["AZURE_OPENAI_ENDPOINT"] = base
    return env


def prepare_challenge(root: Path, challenge: dict[str, Any], attachments: list[dict[str, Any]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dist = root / "distfiles"
    dist.mkdir(exist_ok=True)
    meta = {
        "name": str(challenge.get("value") or challenge.get("name") or "CyberStrike challenge"),
        "category": str((challenge.get("meta") or {}).get("category") or ""),
        "value": int((challenge.get("meta") or {}).get("value") or 0),
        "description": str(challenge.get("description") or (challenge.get("meta") or {}).get("title") or ""),
        "tags": list(challenge.get("tags") or (challenge.get("meta") or {}).get("tags") or []),
        "connection_info": str((challenge.get("meta") or {}).get("connection_info") or ""),
    }
    (root / "metadata.yml").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in attachments:
        source = Path(str(item.get("path") or ""))
        if source.is_file():
            shutil.copy2(source, dist / _safe_name(str(item.get("name") or source.name)))
    return root


def _find_agent_runtime(ctf_agent_python: str | None = None,
                        ctf_agent_root: str | None = None) -> tuple[Path, str]:
    """只接受包含 backend/cli.py 的 CTF-Agent 根目录，避免误用宿主 venv。"""
    configured_root = ctf_agent_root or os.environ.get("CYBERSTRIKE_CTF_AGENT_ROOT")
    sibling = Path(__file__).resolve().parents[3] / "CTF项目" / "ctf-agent"
    roots = [Path(configured_root)] if configured_root else []
    roots.append(sibling)
    root = next((p for p in roots if p.is_dir() and (p / "backend" / "cli.py").is_file()), None)
    if root is None:
        raise RuntimeError("未找到 CTF-Agent 根目录（需要包含 backend/cli.py），请设置 CYBERSTRIKE_CTF_AGENT_ROOT")
    configured_python = ctf_agent_python or os.environ.get("CYBERSTRIKE_CTF_AGENT_PYTHON")
    candidates = [Path(configured_python)] if configured_python else []
    candidates.append(root / ".venv" / "Scripts" / "python.exe")
    candidates.append(root / ".venv" / "bin" / "python")
    for candidate in candidates:
        if not candidate or not candidate.is_file():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import backend.cli"],
            cwd=str(root), capture_output=True, text=True, timeout=20,
        )
        if probe.returncode == 0:
            return root, str(candidate)
    raise RuntimeError(
        "没有可用的 CTF-Agent Python 环境。请确认独立环境依赖已安装；"
        "设置 CYBERSTRIKE_CTF_AGENT_ROOT 指向包含 backend/cli.py 的目录，"
        "并可用 CYBERSTRIKE_CTF_AGENT_PYTHON 指定该项目的解释器。"
    )


def run_worker(*, challenge_dir: Path, provider: dict[str, Any], model: str,
                ctf_agent_python: str | None = None, ctf_agent_root: str | None = None,
                no_submit: bool = True, progress: dict[str, Any] | None = None) -> dict[str, Any]:
    root, python = _find_agent_runtime(ctf_agent_python, ctf_agent_root)
    env = os.environ.copy()
    # 让 worker 的日志逐行及时传回 Job；否则 Python 子进程管道可能全程缓冲，
    # 前端会长时间只看到 running。
    env["PYTHONUNBUFFERED"] = "1"
    env.update(_env_for_provider(provider, model))
    if root.is_dir():
        env["PYTHONPATH"] = os.pathsep.join([str(root), env.get("PYTHONPATH", "")])
    cmd = [python, "-u", "-m", "backend.cli", "--challenge", str(challenge_dir),
           "--models", _provider_spec(provider, model)]
    if no_submit:
        cmd.append("--no-submit")
    # -u 已作为 Python 解释器参数放在 `python -u -m` 中；这里只追加
    # CTF-Agent CLI 自身支持的详细日志参数，避免把 -u 误传给 Click。
    cmd += ["-v"]
    proc = subprocess.Popen(cmd, cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    lines: list[str] = []
    if progress is not None:
        progress.update({"phase": "starting", "message": "CTF-Agent worker 已启动",
                         "python": python, "root": str(root), "started_at": time.time(),
                         "lines": 0, "log_tail": ""})
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if progress is not None:
            progress.update({"phase": _phase_from_line(line), "message": line.strip()[-500:],
                             "lines": len(lines), "log_tail": "".join(lines)[-12000:],
                             "updated_at": time.time()})
    code = proc.wait()
    output = "".join(lines)
    flags = list(dict.fromkeys(_FLAG_RE.findall(output)))
    return {"exit_code": code, "status": "done" if code == 0 else "error",
            "flags": flags, "output": output[-200_000:], "no_submit": no_submit,
            "provider": provider.get("name"), "model": model,
            "runtime": {"root": str(root), "python": python}}


def make_runner(*, challenge: dict[str, Any], attachments: list[dict[str, Any]],
                provider_store: ProviderStore, provider_name: str | None, model: str | None,
                workspace: Path):
    provider = provider_store.default() if not provider_name else provider_store.get(provider_name)
    if not provider.get("enabled", True):
        raise ProviderError(f"供应商 {provider['name']} 已停用")
    chosen_model = model or str(provider.get("models", [""])[0])
    if not chosen_model:
        raise ProviderError(f"供应商 {provider['name']} 未配置模型")
    # 解析 key 但不把它返回给调用方；worker 生命周期内才注入。
    provider = dict(provider)
    provider["api_key"] = provider_store.resolve_key(provider)
    workspace.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="ctf-agent-", dir=str(workspace)))
    challenge_dir = prepare_challenge(run_dir, challenge, attachments)
    progress: dict[str, Any] = {"phase": "queued", "message": "等待 worker 启动", "lines": 0}

    def runner() -> dict[str, Any]:
        try:
            result = run_worker(challenge_dir=challenge_dir, provider=provider,
                                model=chosen_model, progress=progress)
            if result.get("exit_code") != 0:
                output = str(result.get("output") or "").strip()
                raise RuntimeError(
                    f"CTF-Agent worker exited with code {result.get('exit_code')}: "
                    f"{output[-4000:]}")
            return result
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
    return runner, {"provider": provider.get("name"), "model": chosen_model,
                    "no_submit": True, "challenge": challenge.get("id"),
                    "progress": progress}
