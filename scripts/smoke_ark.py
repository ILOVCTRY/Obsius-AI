"""Ark coding plan 端点实证冒烟测试（标准库 urllib，零依赖）。

用法：在项目根目录执行（Key 从 .env / ARK_API_KEY 解析）
    E:\\Miniconda3\\python.exe scripts\\smoke_ark.py
"""

import json
import urllib.request
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm.ark import DEFAULT_BASE_URL, resolve_api_key  # noqa: E402

BASE = DEFAULT_BASE_URL
KEY = resolve_api_key()

def call(model: str, auth_style: str) -> None:
    body = json.dumps({
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply with exactly: OK"}],
    }).encode("utf-8")
    req = urllib.request.Request(f"{BASE}/v1/messages", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("anthropic-version", "2023-06-01")
    if auth_style == "bearer":
        req.add_header("Authorization", f"Bearer {KEY}")
    else:
        req.add_header("x-api-key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"[{model} / {auth_style}] HTTP {resp.status}")
            print("  content:", json.dumps(data.get("content", data), ensure_ascii=False)[:200])
            print("  usage:", data.get("usage"))
            print("  model:", data.get("model"))
    except urllib.error.HTTPError as e:
        print(f"[{model} / {auth_style}] HTTP {e.code}: {e.read().decode('utf-8')[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[{model} / {auth_style}] 异常: {e!r}")

def call_tools(model: str) -> None:
    """验证 tool_use：Agent 主循环的命脉（DESIGN.md §8 工具调用稳定性优先）。"""
    body = json.dumps({
        "model": model,
        "max_tokens": 512,
        "tools": [{
            "name": "run_cmd",
            "description": "执行命令",
            "input_schema": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}, "runtime": {"type": "string"}},
                "required": ["cmd"],
            },
        }],
        "messages": [{"role": "user", "content": "用工具列出 /tmp 目录，runtime 用 sandbox"}],
    }).encode("utf-8")
    req = urllib.request.Request(f"{BASE}/v1/messages", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("anthropic-version", "2023-06-01")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            blocks = [(b.get("type"), b.get("name") or (b.get("text") or b.get("thinking", ""))[:60])
                      for b in data.get("content", [])]
            print(f"[tools / {model}] HTTP {resp.status}, stop={data.get('stop_reason')}")
            for t in blocks:
                print("   block:", t)
            for b in data.get("content", []):
                if b.get("type") == "tool_use":
                    print("   tool input:", b.get("input"))
    except urllib.error.HTTPError as e:
        print(f"[tools / {model}] HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
    except Exception as e:  # noqa: BLE001
        print(f"[tools / {model}] 异常: {e!r}")

if __name__ == "__main__":
    call("deepseek-v4-flash", "bearer")
    call("deepseek-v4-flash", "x-api-key")
    call("ark-code-latest", "bearer")
    call_tools("deepseek-v4-flash")
    call_tools("ark-code-latest")
