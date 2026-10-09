"""测试维度清单（agent-path-intent-loop M1，2026-10-09）。

**地位仿 phases.py**：纯函数查询层，只依赖黑板查询接口（鸭子类型），不 import
core.orchestrator（其反向依赖）。

**要解决的问题**：现在"这个资产测完了吗"只能看覆盖度（有没有测过），而覆盖度
回答不了"测的深度/维度够不够"——一个资产可以 coverage=100% 却只测了可达性。
本模块把"该测哪些面"显式成一张**可判定的维度清单**，于是：

- 子代理领到资产 → 按资产 type 匹配 `applies_to` → 得到「该资产的维度清单」，
  逐面声明意图并收尾；
- **收敛判据 = 该资产所有适用面都覆盖**（替代"覆盖度达标"）；
- **面覆盖 ⟺ 该面有 ≥1 条收尾意图（vuln/finding/dead_end）且无 open 意图**。

**数据源**：`packs/tracks/<track>/dimensions.yaml`（轨级默认；项目
`config.dimensions` 同名条目整体覆写）。解析用受约束极简 yaml 子集（零 PyYAML，
同 phases/experts/profiles 约定）。

**演进（接 skill）**：后续把本表条目迁进各 skill 的 `covers` 字段，由「专家
skill 白名单 → 面清单」推导取代本静态表；在此之前本表是唯一真相源。
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.skills.roles import _parse_inline_value
from core.skills.taxonomy import track_dir

log = logging.getLogger(__name__)

DIMENSIONS_FILE = "dimensions.yaml"

# 维度适用的资产类型值域（与 assets.detect_type / coverage.COVERAGE_TYPES 对齐）
ASSET_TYPES = ("domain", "host", "service", "url", "binary")


# ---------- 解析 ----------

def parse_dimensions_file(path: Path) -> list[dict]:
    """维度清单极简解析。仅支持：顶层 `dimensions:` 块 + 2 空格缩进的
    `- key: value` 条目（**续键 4 空格**，与常规 YAML 对齐）。其余缩进/形态
    一律 ValueError（加载侧跳过、doctor 报 dimensions-bad-yaml）。"""
    items: list[dict] = []
    item: dict | None = None
    header = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            raise ValueError(f"行不是 key: value 形态: {raw.strip()!r}")
        key, _, value = stripped.partition(":")
        is_item = stripped.startswith("- ")
        key = key.lstrip("- ").strip()
        if indent == 0:
            if is_item:
                raise ValueError(f"顶层不允许列表项: {stripped!r}")
            if key != "dimensions":
                raise ValueError(f"未知顶层键: {key!r}（仅支持 dimensions:）")
            if value.strip():
                raise ValueError("dimensions 须为缩进块（不支持内联列表）")
            header = True
        elif indent == 2:
            if not header:
                raise ValueError(f"条目出现在 dimensions 之前: {stripped!r}")
            if is_item:
                item = {}
                items.append(item)
            elif item is None:
                raise ValueError(f"维度条目缩进错误: {stripped!r}")
            item[key] = _parse_inline_value(value)
        elif indent == 4:
            # 仅允许条目的续键（- 项的后续字段）；他处 4 空格一律拒
            if item is None:
                raise ValueError(f"缩进层级过深（4 空格仅限条目续键）: {stripped!r}")
            item[key] = _parse_inline_value(value)
        else:
            raise ValueError(f"缩进层级过深（仅支持 0/2/4 空格）: {stripped!r}")
    return items


def dimension_spec(data: dict) -> dict:
    """维度条目原始 dict → 归一化规格（非法值 ValueError，加载侧跳过）。"""
    if not isinstance(data, dict):
        raise ValueError("维度条目须为映射")
    did = str(data.get("id") or "").strip()
    if not did:
        raise ValueError("维度缺 id")
    applies = [str(x).strip() for x in (data.get("applies_to") or [])
               if str(x).strip()]
    if not applies:
        raise ValueError(f"维度 {did} 缺 applies_to")
    bad = [t for t in applies if t not in ASSET_TYPES]
    if bad:
        raise ValueError(f"维度 {did} 的 applies_to 含非法资产类型: {bad}"
                         f"（允许 {ASSET_TYPES}）")
    return {
        "id": did,
        "name": str(data.get("name") or did).strip(),
        "applies_to": applies,
        "intent": str(data.get("intent") or "").strip(),
        "evidence_hint": str(data.get("evidence_hint") or "").strip(),
    }


def load_track_dimensions(packs_root: str | Path, track: str,
                          project_config: dict | None = None) -> list[dict]:
    """轨级维度清单 [spec, ...]（文件序）。文件缺失=空清单（该轨无维度机制，
    收敛判据退化为无面可查——宁松勿误判）。坏条目 log 跳过（doctor 报
    dimensions-bad-yaml）。项目级覆写：`config.dimensions` 同名 id 整体替换
    （也允许新增），坏条目跳过。"""
    book: dict[str, dict] = {}
    order: list[str] = []
    path = track_dir(packs_root, track) / DIMENSIONS_FILE
    if path.is_file():
        try:
            raw_items = parse_dimensions_file(path)
        except (ValueError, OSError) as e:
            log.warning("维度清单解析失败 %s: %s", path, e)
            raw_items = []
        for data in raw_items:
            try:
                spec = dimension_spec(data)
            except ValueError as e:
                log.warning("维度条目非法 %s: %s", path, e)
                continue
            if spec["id"] not in book:
                order.append(spec["id"])
            book[spec["id"]] = spec
    override = (project_config or {}).get("dimensions")
    if isinstance(override, list):
        for data in override:
            try:
                spec = dimension_spec(data if isinstance(data, dict) else {})
            except ValueError as e:
                log.warning("项目级维度覆写非法: %s", e)
                continue
            if spec["id"] not in book:
                order.append(spec["id"])
            book[spec["id"]] = spec
    return [book[i] for i in order]


def dimensions_for_type(dims: list[dict], asset_type: str | None) -> list[dict]:
    """该资产类型适用的维度子集（顺序与清单一致）。"""
    if not asset_type:
        return []
    return [d for d in dims if asset_type in d["applies_to"]]


def render_intent(spec: dict, asset_value: str) -> str:
    """维度意图模板 → 实际意图陈述（{asset} 替换成资产值）。"""
    return (spec.get("intent") or "").replace("{asset}", asset_value or "").strip()


# ---------- 面覆盖判定 ----------

def dimension_coverage(intents: list[dict], dims: list[dict]) -> dict:
    """面覆盖判定（纯函数）。intents 须是**已按资产/子树过滤**的意图行，每条
    可带 ``dimension`` 字段（意图声明的面 id）。

    **面覆盖 ⟺ 该面有 ≥1 条收尾意图（status=closed）且无 open 意图。**

    返回 ``{"covered": [id...], "uncovered": [id...], "open": [id...]}``：
    - covered   面已覆盖（可计入收敛）；
    - uncovered 面未覆盖（无收尾意图，或还有 open 意图在跑）；
    - open      子集：面有 open 意图在跑（用于区分"没测"与"在测"）。
    """
    by_dim: dict[str, list[dict]] = {}
    for it in intents:
        d = str(it.get("dimension") or "").strip()
        if d:
            by_dim.setdefault(d, []).append(it)
    covered: list[str] = []
    uncovered: list[str] = []
    open_dims: list[str] = []
    for spec in dims:
        rows = by_dim.get(spec["id"], [])
        has_open = any(r.get("status") == "open" for r in rows)
        has_closed = any(r.get("status") == "closed" for r in rows)
        if has_open:
            open_dims.append(spec["id"])
        if has_closed and not has_open:
            covered.append(spec["id"])
        else:
            uncovered.append(spec["id"])
    return {"covered": covered, "uncovered": uncovered, "open": open_dims}


def is_converged(intents: list[dict], dims: list[dict]) -> bool:
    """该资产（或其子树）是否"测完"——所有适用面都覆盖。dims 为空时返回 True
    （无维度机制可查，不拦）。"""
    if not dims:
        return True
    return not dimension_coverage(intents, dims)["uncovered"]
