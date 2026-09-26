"""宿主能力探测（DESIGN.md §7 detector）。

启动时探测：Docker 可达？WSL 可用？tools/ 清单里哪些工具在 PATH？
生成能力清单 → 注入 Agent 系统提示（"你知道自己有什么工具"）。
Agent 不许自己猜环境——这是 Windows 上 Agent 翻车的根源。
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from core.runtime.backends import DEFAULT_PENTEST_IMAGE, NO_WINDOW_FLAGS


@dataclass
class ProbeResult:
    name: str
    available: bool
    detail: str = ""


@dataclass
class CapabilityInventory:
    docker: ProbeResult
    wsl: ProbeResult
    tools: list[ProbeResult] = field(default_factory=list)

    def to_prompt(self) -> str:
        """渲染为注入系统提示的能力清单文本。"""
        lines = ["## 执行环境能力清单（detector 实测，禁止自行猜测）"]
        lines.append(f"- Docker: {'可用' if self.docker.available else '不可用'} {self.docker.detail}".rstrip())
        lines.append(f"- WSL2: {'可用' if self.wsl.available else '不可用'} {self.wsl.detail}".rstrip())
        if self.tools:
            ok = [t for t in self.tools if t.available]
            miss = [t for t in self.tools if not t.available]
            if ok:
                lines.append("- 可用工具: " + ", ".join(t.name for t in ok))
            if miss:
                lines.append("- 缺失工具: " + ", ".join(f"{t.name}({t.detail})" for t in miss if t.detail))
        else:
            lines.append("- 工具清单: 暂无注册（tools/ 为空）")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """API 响应形状（GET /api/projects/{pid}.capability 与 POST /api/gateway/probe 同构）。"""
        return {
            "docker": {"available": self.docker.available, "detail": self.docker.detail},
            "wsl": {"available": self.wsl.available, "detail": self.wsl.detail},
            "tools": [{"name": t.name, "available": t.available, "detail": t.detail}
                      for t in self.tools],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _run_probe(cmd: list[str], timeout: float = 10.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW_FLAGS)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)[:120]
    if proc.returncode == 0:
        first = (proc.stdout or "").strip().splitlines()
        return True, first[0][:120] if first else ""
    return False, (proc.stderr or proc.stdout).strip()[:120]


class HostDetector:
    def probe(self, tools_root: str | Path | None = None) -> CapabilityInventory:
        docker_ok, docker_detail = _run_probe(["docker", "version", "--format", "{{.Server.Version}}"])
        if docker_ok:
            # pentest-tools-container-m0：docker 引擎热时顺带查渗透工具箱镜像，
            # 缺失即在能力清单给出构建指引（Agent/前端都消费同一份文本）
            img_ok, _ = _run_probe(
                ["docker", "image", "inspect", DEFAULT_PENTEST_IMAGE,
                 "--format", "{{.Id}}"])
            docker_detail += (
                "；pentest-box 镜像就绪" if img_ok
                else "；pentest-box 镜像缺失（构建：python scripts/build_pentest_box.py）")
        wsl_ok, wsl_detail = _run_probe(["wsl.exe", "--status"]) if _is_windows() else (False, "非 Windows 宿主")
        tools = self.probe_tools(tools_root) if tools_root else []
        return CapabilityInventory(
            docker=ProbeResult("docker", docker_ok, docker_detail),
            wsl=ProbeResult("wsl", wsl_ok, "默认发行版" if wsl_ok else wsl_detail),
            tools=tools,
        )

    @staticmethod
    def probe_tools(tools_root: str | Path) -> list[ProbeResult]:
        """工具链注册表驱动探测（toolchain-registry M1，2026-09-23）：四来源检测
        （config/tools.json 覆盖层 → tools/ 规范位 → fallback glob → PATH），
        机制见 core/toolchain.py。原 manifest.yaml 通道零使用者，随之退役。
        registry 坏结构降级为单条 issue 行，不阻断 docker/wsl 主探测。"""
        try:
            from core.toolchain import load_tool_overrides, probe_tools as registry_probe
            rows = registry_probe(tools_root, overrides=load_tool_overrides())
        except Exception as e:  # noqa: BLE001 —— 探测面故障不影响能力清单主体
            return [ProbeResult("registry", False, f"registry 读取失败: {e}")]
        return [
            ProbeResult(
                r["name"], r["status"] == "ready",
                (f"{r['source']}: {r['path']}" if r["status"] == "ready"
                 else (r["detail"] or "缺失")))
            for r in rows
        ]


def _is_windows() -> bool:
    import platform
    return platform.system() == "Windows"
