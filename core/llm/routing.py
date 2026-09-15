"""模型路由（DESIGN.md §8）：任务角色 → 模型映射。

角色约定（§8 模型路由）：
- planner   重思考（规划上下文、skill-improve）→ 旗舰思考模型
- executor  主循环高频调用 → 响应快、工具调用稳（稳定性 > 智力）
- classifier 类别判定、日志摘要等杂活 → 小模型

配置优先级：config/llm.json > 内置默认。JSON 形如：
    {"planner": "ark-code-latest", "executor": "...", "classifier": "..."}
"""

import json
import os
from pathlib import Path

DEFAULT_MODELS = {
    "planner": "ark-code-latest",
    "executor": "ark-code-latest",
    "classifier": "deepseek-v4-flash",
}
KNOWN_ROLES = tuple(DEFAULT_MODELS)

# 实测可用模型清单（smoke_ark.py 验证 tool_use 正常），供 GET /api/models 与开窗下拉
AVAILABLE_MODELS = ["ark-code-latest", "deepseek-v4-flash"]


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """极简 .env 加载：KEY=VALUE 行，# 注释。不覆盖已存在的环境变量。"""
    p = Path(path)
    result: dict[str, str] = {}
    if not p.is_file():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        result[key] = value
        os.environ.setdefault(key, value)
    return result


class ModelRouter:
    def __init__(self, models: dict[str, str] | None = None, config_path: str | Path | None = "config/llm.json"):
        merged = dict(DEFAULT_MODELS)
        self._overrides: dict[str, tuple[str | None, str]] = {}
        if config_path:
            p = Path(config_path)
            if p.is_file():
                override = json.loads(p.read_text(encoding="utf-8"))
                for role in KNOWN_ROLES:
                    if role in override:
                        v = override[role]
                        if isinstance(v, dict):
                            self._overrides[role] = (v.get("provider"), v["model"])
                            merged[role] = v["model"]
                        else:
                            self._overrides[role] = (None, v)
                            merged[role] = v
        if models:
            for role, m in models.items():
                if role not in KNOWN_ROLES:
                    raise ValueError(f"未知模型角色: {role}，允许: {KNOWN_ROLES}")
                merged[role] = m
        self._models = merged

    def model_for(self, role: str) -> str:
        if role not in KNOWN_ROLES:
            raise ValueError(f"未知模型角色: {role}，允许: {KNOWN_ROLES}")
        return self._models[role]

    def target_for(self, role: str) -> tuple[str | None, str] | None:
        """llm.json 文件级覆写（不上 UI）：返回 (provider_name, model)；
        provider_name=None 表示裸模型名（走全局默认供应商）；无覆写返回 None（走全局默认）。"""
        return self._overrides.get(role)
