"""角色加载（DESIGN.md §6.6）。

roles/*.yaml 用极简解析：平铺 key: value、内联列表 [a, b]、null = 不过滤。
不支持嵌套结构（角色文件刻意保持扁平）。
"""

import re
from pathlib import Path


def _strip_inline_comment(value: str) -> str:
    """去行内注释：# 前有空格（或行首）才视为注释，引号内的 # 不动。"""
    out: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(value):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in {'"', "'"}:
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or value[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_inline_value(value: str):
    v = _strip_inline_comment(value)
    if v in {"null", "~", ""}:
        return None
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v.strip('"').strip("'")


def load_role(packs_root: str | Path, track: str, role_name: str) -> dict:
    """加载场景轨角色定义（packs/tracks/<track>/roles/）。

    角色不存在时回退 _generalist（不存在则抛错）。
    角色 yaml 增强字段（§6.6，均可选）：description / tools / max_runtime / max_steps。
    """
    base = Path(packs_root) / "tracks" / track / "roles"
    path = base / f"{role_name}.yaml"
    if not path.is_file():
        path = base / "_generalist.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"角色与兜底角色均不存在: tracks/{track}/roles/{role_name}")
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = _parse_inline_value(value)
    return out
