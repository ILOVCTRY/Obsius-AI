"""宿主能力探测（DESIGN.md §7 detector）。

启动时探测：Docker 可达？WSL 可用？tools/ 清单里哪些工具在 PATH？
生成能力清单 → 注入 Agent 系统提示（"你知道自己有什么工具"）。
Agent 不许自己猜环境——这是 Windows 上 Agent 翻车的根源。
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


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

    def to_json(self) -> str:
        return json.dumps({
            "docker": {"available": self.docker.available, "detail": self.docker.detail},
            "wsl": {"available": self.wsl.available, "detail": self.wsl.detail},
            "tools": [{"name": t.name, "available": t.available, "detail": t.detail}
                      for t in self.tools],
        }, ensure_ascii=False, indent=2)


def _run_probe(cmd: list[str], timeout: float = 10.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)[:120]
    if proc.returncode == 0:
        first = (proc.stdout or "").strip().splitlines()
        return True, first[0][:120] if first else ""
    return False, (proc.stderr or proc.stdout).strip()[:120]


class HostDetector:
    def probe(self, tools_root: str | Path | None = None) -> CapabilityInventory:
        docker_ok, docker_detail = _run_probe(["docker", "version", "--format", "{{.Server.Version}}"])
        wsl_ok, wsl_detail = _run_probe(["wsl.exe", "--status"]) if _is_windows() else (False, "非 Windows 宿主")
        tools = self.probe_tools(tools_root) if tools_root else []
        return CapabilityInventory(
            docker=ProbeResult("docker", docker_ok, docker_detail),
            wsl=ProbeResult("wsl", wsl_ok, "默认发行版" if wsl_ok else wsl_detail),
            tools=tools,
        )

    @staticmethod
    def probe_tools(tools_root: str | Path) -> list[ProbeResult]:
        """扫描 tools/**/manifest.yaml 的 probe 字段逐一探测（DESIGN.md §7 工具目录）。

        manifest 形如：
            name: ghidra
            probe: "where analyzeHeadless"   # Windows
        """
        results: list[ProbeResult] = []
        root = Path(tools_root)
        if not root.is_dir():
            return results
        for manifest in sorted(root.glob("**/manifest.yaml")):
            meta = _load_simple_yaml(manifest)
            name = meta.get("name", manifest.parent.name)
            probe = meta.get("probe", "")
            if not probe:
                results.append(ProbeResult(name, False, "manifest 无 probe 字段"))
                continue
            ok, detail = _run_probe(probe.split())
            results.append(ProbeResult(name, ok, detail if not ok else "PATH 可达"))
        return results


def _is_windows() -> bool:
    import platform
    return platform.system() == "Windows"


def _load_simple_yaml(path: Path) -> dict[str, str]:
    """极简 YAML（仅顶层 key: value，够 manifest 用；引入 PyYAML 前的占位）。"""
    meta: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.rstrip()
            if not line or line.lstrip().startswith("#") or ":" not in line:
                continue
            if line.startswith((" ", "\t")):
                continue  # 跳过嵌套（platforms 等复杂结构后续接 PyYAML 再解析）
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return meta
