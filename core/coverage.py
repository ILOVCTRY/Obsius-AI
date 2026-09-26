"""覆盖度对账平台化（orchestrator-efficiency M3 B1，2026-09-22，DESIGN §四/§6.4）。

地位仿 phases.py：纯函数查询层，只依赖黑板查询接口（鸭子类型），不 import
core.orchestrator（其反向依赖）。输入资产/发现快照，输出分组对账结论。

对账面：host/domain/url/service 四类资产（与编排器 _assets_view targetable
口径一致；binary 等不入组）。口径拍板记录在
docs/plans/orchestrator-efficiency.md §0-6：
- 终态 = status ∈ {tested_clean, na} ∪ meta.dead_end ∪ 有 finding 挂链
  （FP-only 视作死路味）；visited/scanning 算 visited；budget_stop 算 open
  （预算停 ≠ 测完，宁严勿松）。
- 收敛传播（asset-tree-derived-clean D3，2026-09-24 修订）：叶子 converged =
  自身 terminal；父节点 converged = 自身 terminal OR（有子 AND 全部子 converged）
  ——父状态由子树读时派生，不再要求自身 visited 佐证。
- group_key：沿 parent_id 链走到根，根 domain → domain:<value>；根 host →
  host:<value>；无 parent 孤儿自成组 <type>:<value>——与 target_keys_of 的
  归一化键口径对齐（url 派生 host、domain 优先）。
"""

from __future__ import annotations

from collections import defaultdict

# 终态资产状态机口径（"测完"）；tested_clean 由资产状态机显式流转，na 由人工裁定
TERMINAL_STATUSES = ("tested_clean", "na")
# 对账面资产类型（编排器 _assets_view targetable 同口径）
COVERAGE_TYPES = ("host", "domain", "url", "service")


def asset_terminal_state(asset: dict, findings: list[dict] | None = None) -> str:
    """单资产对账态：terminal-<flavor> | visited | scanning | open。

    flavor：tested_clean/na（状态机口径）、dead_end（meta 标记或仅 FP 发现——
    全是误报意味着这条路被否了）、finding（有非 FP 发现挂链即收口）。
    判定顺序：状态机终态 > 死路标记 > 发现挂链 > 半程 > open。
    """
    st = asset.get("status") or "open"
    if st in TERMINAL_STATUSES:
        return f"terminal-{st}"
    meta = asset.get("meta") or {}
    fnds = findings or []
    non_fp = [f for f in fnds if f.get("status") != "false-positive"]
    if meta.get("dead_end") or (fnds and not non_fp):
        return "terminal-dead_end"
    if non_fp:
        return "terminal-finding"
    if st in ("visited", "scanning"):
        return st
    return "open"


def coverage_report(bb, project_id: str, *, uncovered_cap: int = 30,
                    assets: list[dict] | None = None) -> dict:
    """全项目覆盖度对账 → {"overall": {...}, "by_group": [...]}。

    by_group 按 group_key 归组，每组：total/terminal/converged 计数 + done
    （全收敛）+ uncovered 清单（cap uncovered_cap，open 态优先于 visited——
    编排器应先派未摸过的面）。overall 汇总组数/收敛组数/资产数/收敛资产数。
    """
    if assets is None:
        assets = bb.list_assets(project_id)
    scoped = [a for a in assets if a.get("type") in COVERAGE_TYPES]
    by_id = {a["id"]: a for a in scoped}

    findings_by_asset: dict[str, list[dict]] = defaultdict(list)
    for f in bb.list_findings(project_id):
        aid = f.get("target_asset_id")
        if aid in by_id:
            findings_by_asset[aid].append(f)

    state: dict[str, str] = {}
    for a in scoped:
        state[a["id"]] = asset_terminal_state(a, findings_by_asset.get(a["id"]))

    children: dict[str, list[dict]] = defaultdict(list)
    roots: list[dict] = []
    for a in scoped:
        pid = a.get("parent_id")
        if pid and pid in by_id:
            children[pid].append(a)
        else:
            roots.append(a)

    def group_key_of(a: dict) -> str:
        cur, seen = a, set()
        while cur.get("parent_id") and cur["parent_id"] in by_id \
                and cur["parent_id"] not in seen:
            seen.add(cur["id"])
            cur = by_id[cur["parent_id"]]
        return f"{cur['type']}:{(cur['value'] or '').lower()}"

    # 收敛传播（memo 化；parent_id 理论不成环，防御性标记防死循环）
    converged: dict[str, bool] = {}

    def is_converged(aid: str) -> bool:
        if aid in converged:
            return converged[aid]
        converged[aid] = False
        st = state[aid]
        if st.startswith("terminal"):
            ok = True
        else:
            # D3（2026-09-24）：父收敛只看子树是否全收敛，不要求自身 visited
            kids = children.get(aid, [])
            ok = bool(kids) and all(is_converged(k["id"]) for k in kids)
        converged[aid] = ok
        return ok

    groups: dict[str, list[dict]] = defaultdict(list)
    for a in scoped:
        groups[group_key_of(a)].append(a)

    by_group = []
    for key in sorted(groups):
        members = groups[key]
        # uncovered 排序：open（未摸）优先于 visited（半程），组内 HVT 逻辑由注入层另行加权
        members.sort(key=lambda a: 0 if state[a["id"]] == "open" else 1)
        conv_n = sum(1 for a in members if is_converged(a["id"]))
        by_group.append({
            "group": key,
            "total": len(members),
            "terminal": sum(1 for a in members if state[a["id"]].startswith("terminal")),
            "converged": conv_n,
            "done": conv_n == len(members),
            "uncovered": [
                {"id": a["id"], "type": a["type"], "value": (a["value"] or "")[:60],
                 "state": state[a["id"]]}
                for a in members if not is_converged(a["id"])
            ][:uncovered_cap],
        })

    overall = {
        "groups": len(by_group),
        "groups_done": sum(1 for g in by_group if g["done"]),
        "assets": len(scoped),
        "converged": sum(1 for a in scoped if is_converged(a["id"])),
    }
    return {"overall": overall, "by_group": by_group}


# ---------- 根状态读时派生（asset-tree-derived-clean M2，2026-09-24） ----------

def effective_status_map(assets: list[dict],
                         findings: list[dict] | None = None) -> dict[str, dict]:
    """资产状态读时派生（D3-D5），返回 ``aid → {status, basis, settled, has_findings}``。

    - **叶子节点**：显式 status 原样（basis=explicit）；settled 按终态味判定
      （tested_clean/na/dead_end/finding 挂链皆收口；finding 标 has_findings）。
      零子资产根行由此保持 AI 显式管理，不自动派生（D5）。
    - **有子资产节点**：孩子全部 settled → status=tested_clean（basis=derived）；
      任一未 settled → open（宁严，D3）。has_findings 沿子树向上传播。
    - 端口面：service 即 host 的子节点，未全部 settled 时 host 不得派生 clean
      （通用子树规则天然覆盖）。
    """
    scoped = [a for a in assets if a.get("type") in COVERAGE_TYPES]
    by_id = {a["id"]: a for a in scoped}

    findings_by_asset: dict[str, list[dict]] = defaultdict(list)
    for f in findings or []:
        aid = f.get("target_asset_id")
        if aid in by_id:
            findings_by_asset[aid].append(f)

    children: dict[str, list[dict]] = defaultdict(list)
    for a in scoped:
        pid = a.get("parent_id")
        if pid and pid in by_id:
            children[pid].append(a)

    memo: dict[str, dict] = {}

    def calc(aid: str) -> dict:
        if aid in memo:
            return memo[aid]
        a = by_id[aid]
        kids = children.get(aid, [])
        explicit = a.get("status") or "open"
        if not kids:
            ts = asset_terminal_state(a, findings_by_asset.get(aid))
            settled = ts.startswith("terminal")
            memo[aid] = {
                "status": explicit, "basis": "explicit",
                "settled": settled,
                "has_findings": ts == "terminal-finding",
            }
        else:
            kid_views = [calc(k["id"]) for k in kids]
            all_settled = all(v["settled"] for v in kid_views)
            memo[aid] = {
                "status": "tested_clean" if all_settled else "open",
                "basis": "derived",
                "settled": all_settled,
                "has_findings": any(v["has_findings"] for v in kid_views),
            }
        return memo[aid]

    for a in scoped:
        calc(a["id"])
    return memo


def attach_effective_status(assets: list[dict],
                            findings: list[dict] | None = None) -> list[dict]:
    """把派生结果挂回资产 dict（浅拷贝，不改原对象）：
    新增 ``effective_status`` / ``status_basis`` / ``settled`` / ``has_findings``。
    API 资产列表与前端徽章的单一出处。非 coverage 类型（binary 等）不挂。"""
    emap = effective_status_map(assets, findings)
    out = []
    for a in assets:
        v = emap.get(a["id"])
        if v is None:
            out.append(a)
            continue
        out.append({**a, "effective_status": v["status"],
                    "status_basis": v["basis"], "settled": v["settled"],
                    "has_findings": v["has_findings"]})
    return out
