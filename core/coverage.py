"""覆盖度对账平台化（orchestrator-efficiency M3 B1，2026-09-22，DESIGN §四/§6.4）。

地位仿 phases.py：纯函数查询层，只依赖黑板查询接口（鸭子类型），不 import
core.orchestrator（其反向依赖）。输入资产/发现快照，输出分组对账结论。

对账面：host/domain/url/service 四类资产（与编排器 _assets_view targetable
口径一致；binary 等不入组）。口径拍板记录在
docs/plans/orchestrator-efficiency.md §0-6，**2026-10-09 改三态**（见
docs/plans/归档-已完成/asset-tri-state.md 与 DESIGN.md §三「资产三态」）：
- **三态**（asset-tri-state）= 未测试(`open`，不设状态) / 已访问(`visited`) /
  已测试干净(`tested_clean`)。**有 finding 挂链不再是收口**——有洞 ≠ 测干净；
  收口味只剩 tested_clean/na(人工裁定)/dead_end(此路不通)。FP-only 视作死路味；
  `scanning` 并入 `visited`（派发是一批一批派、无任务认领，无孤儿半程）；
  budget_stop 算 `visited`（预算停 ≠ 测完，宁严勿松）。
- 收敛传播（asset-tree-derived-clean D3，2026-09-24 修订）：叶子 converged =
  自身收口；父节点 converged = 有子 AND 全部子 converged——父状态由子树读时
  派生，不再要求自身 visited 佐证。**加子破 clean**（新子 open → 父掉出 clean）。
- group_key：沿 parent_id 链走到根，根 domain → domain:<value>；根 host →
  host:<value>；无 parent 孤儿自成组 <type>:<value>——与 target_keys_of 的
  归一化键口径对齐（url 派生 host、domain 优先）。
"""

from __future__ import annotations

from collections import defaultdict

# 状态机收口口径（"测完"）；tested_clean 由资产状态机显式流转，na 由人工裁定
TERMINAL_STATUSES = ("tested_clean", "na")
# 对账面资产类型（编排器 _assets_view targetable 同口径）
COVERAGE_TYPES = ("host", "domain", "url", "service")

# ---------- 三态（asset-tri-state，2026-10-09） ----------
# 三态词表与 DB status 同名，前端/API 零新枚举。
TRI_UNTESTED = "open"        # 未测试（不设状态）
TRI_VISITED = "visited"      # 已访问（碰过，未测尽）
TRI_CLEAN = "tested_clean"   # 已测试干净（穷尽）

# 收口味（可计入"已测试干净"）——**发现挂链不在列：有洞 ≠ 测干净**。
_SETTLING_FLAVORS = ("terminal-tested_clean", "terminal-na", "terminal-dead_end")


def is_settled(flavor: str) -> bool:
    """对账态是否收口（可计入已测试干净）。finding 挂链不算收口。"""
    return flavor in _SETTLING_FLAVORS


def tri_state_of(flavor: str) -> str:
    """对账态 → 三态。收口味→tested_clean；terminal-finding→visited（有洞≠干净）；
    visited→visited；其余（open）→open。"""
    if flavor in _SETTLING_FLAVORS:
        return TRI_CLEAN
    if flavor in ("terminal-finding", "visited"):
        return TRI_VISITED
    return TRI_UNTESTED


def asset_terminal_state(asset: dict, findings: list[dict] | None = None) -> str:
    """单资产对账态：terminal-<flavor> | visited | open。

    flavor：tested_clean/na（状态机口径）、dead_end（meta 标记或仅 FP 发现——
    全是误报意味着这条路被否了）、finding（有非 FP 发现挂链——**独立一味，供
    has_findings 识别，但不计入收口**：有洞 ≠ 测干净）。
    判定顺序：状态机终态 > 死路标记 > 发现挂链 > 半程 > open。
    `scanning` 并入 `visited`（三态归并，读时不区分在跑/半程）。
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
        return "visited"
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
        if is_settled(st):
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
            # terminal 口径 = 已测试干净（收口味）；有洞资产不算（2026-10-09 三态）
            "terminal": sum(1 for a in members if is_settled(state[a["id"]])),
            "converged": conv_n,
            "done": conv_n == len(members),
            "uncovered": [
                {"id": a["id"], "type": a["type"], "value": (a["value"] or "")[:60],
                 "state": tri_state_of(state[a["id"]])}
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
    """资产状态读时派生（D3-D5；**2026-10-09 改三态**），返回
    ``aid → {status, basis, settled, has_findings}``，status ∈ 三态
    (`open`/`visited`/`tested_clean`)。

    - **叶子节点**：显式 status 经 `tri_state_of` 归一（basis=explicit）。
      **发现压过显式 clean**——叶子显式 tested_clean 但挂非 FP 发现 → 读时降级
      `visited`（后补的洞作废 clean，无需迁移）。零子资产根行保持 AI 显式管理
      不自动派生（D5）。
    - **有子资产节点**：basis=derived。**有洞 → visited**（不派生 clean）；
      否则全子 `tested_clean` → `tested_clean`；全子 `open` → `open`（父未测）；
      其余（混合）→ `visited`。has_findings 沿子树向上传播。
    - 端口面：service 即 host 的子节点，未全部收口时 host 不得派生 clean
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
        if not kids:
            fnds = findings_by_asset.get(aid) or []
            own_findings = any(f.get("status") != "false-positive" for f in fnds)
            ts = asset_terminal_state(a, fnds)
            # 发现压过显式 clean：有非 FP 发现 → visited（后补的洞作废 clean）。
            # 直接查发现列表——asset_terminal_state 里状态机终态压过发现，取不到。
            status = TRI_VISITED if own_findings else tri_state_of(ts)
            memo[aid] = {
                "status": status, "basis": "explicit",
                "settled": status == TRI_CLEAN,
                "has_findings": own_findings,
            }
        else:
            kid_views = [calc(k["id"]) for k in kids]
            has_findings = any(v["has_findings"] for v in kid_views)
            if has_findings:
                status = TRI_VISITED           # 子树有洞 → 父不 clean（发现不上传 clean）
            elif all(v["status"] == TRI_CLEAN for v in kid_views):
                status = TRI_CLEAN
            elif all(v["status"] == TRI_UNTESTED for v in kid_views):
                status = TRI_UNTESTED          # 全子未测 → 父未测
            else:
                status = TRI_VISITED           # 混合 → 已访问（未测尽）
            memo[aid] = {
                "status": status, "basis": "derived",
                "settled": status == TRI_CLEAN,
                "has_findings": has_findings,
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


# ---------- 三态态势快照（asset-tri-state D8，2026-10-09） ----------

def situation_snapshot(assets: list[dict], findings: list[dict] | None = None,
                       *, untested_cap: int = 30, visited_cap: int = 20) -> dict:
    """三态态势快照：给主控（workbench chat-orchestrator）注入的分桶视图。

    与 `effective_status_map` 同口径（读时派生三态，不落库）。只计
    COVERAGE_TYPES 四类，binary 等不入（与编排器口径一致）。返回：

    - ``untested`` / ``visited``：清单 ``[{id,type,value[,has_findings]}]``，
      各按 cap 截断（``untested_truncated`` / ``visited_truncated`` 标示）；
    - ``clean_ids``：已测干净 id 列表（只给数防重复派，不展开值）；
    - ``counts``：``{"open":n,"visited":n,"tested_clean":n}``。

    读取失败由调用方兜（本函数纯计算，不抛业务异常）。
    """
    emap = effective_status_map(assets, findings)
    untested: list[dict] = []
    visited: list[dict] = []
    clean_ids: list[str] = []
    counts = {TRI_UNTESTED: 0, TRI_VISITED: 0, TRI_CLEAN: 0}
    for a in assets:
        v = emap.get(a["id"])
        if v is None:               # 非 coverage 类型（binary 等）
            continue
        status = v["status"]
        counts[status] = counts.get(status, 0) + 1
        item = {"id": a["id"], "type": a.get("type"),
                "value": (a.get("value") or "")[:60]}
        if status == TRI_UNTESTED:
            untested.append(item)
        elif status == TRI_VISITED:
            item["has_findings"] = v["has_findings"]
            visited.append(item)
        else:
            clean_ids.append(a["id"])
    return {
        "untested": untested[:untested_cap],
        "untested_truncated": len(untested) > untested_cap,
        "visited": visited[:visited_cap],
        "visited_truncated": len(visited) > visited_cap,
        "clean_ids": clean_ids,
        "counts": counts,
    }
