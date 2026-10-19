"""ARTEX Planner：把项目目标拆解为可执行的父子 Intent。"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from core.blackboard.artex_goals import (
    decompose_goals,
    fingerprint,
    goals_stats,
    list_goals,
    set_goals,
    verify_goals_met,
)
from core.blackboard.artex_intents import create_intent, get_intent, list_intents
from core.blackboard.artex_exploration import (add_edge, create_node, get_node,
                                               list_nodes)


log = logging.getLogger(__name__)


_SYSTEM = """你是 ARTEX 任务规划器。只输出 JSON，不要 Markdown。
格式：{"objective":"...","expert":"...","priority":整数,"constraints":{},"asset_ids":[],"children":[{同样字段}]}
children 是可并行执行的自包含子任务；不要输出命令、凭据或超出授权范围的目标。
"""


def _text(response: Any) -> str:
    value = getattr(response, "text", response)
    return str(value or "").strip()


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("ARTEX Planner 未返回 JSON")
        try:
            value = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("ARTEX Planner JSON 无法解析") from exc
    if not isinstance(value, dict):
        raise ValueError("ARTEX Planner 顶层必须是对象")
    return value


def _normalize(node: dict, *, default_objective: str, allowed_experts: set[str]) -> dict:
    objective = str(node.get("objective") or default_objective).strip()
    if not objective:
        raise ValueError("ARTEX Planner Intent objective 不能为空")
    expert = str(node.get("expert") or "").strip()
    if expert and allowed_experts and expert not in allowed_experts:
        raise ValueError(f"ARTEX Planner 专家不在当前轨道专家池: {expert}")
    try:
        priority = int(node.get("priority", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("ARTEX Planner priority 必须是整数") from exc
    assets = [str(x).strip() for x in (node.get("asset_ids") or []) if str(x).strip()]
    constraints = node.get("constraints") or {}
    if not isinstance(constraints, dict):
        raise ValueError("ARTEX Planner constraints 必须是对象")
    children = node.get("children") or []
    if not isinstance(children, list):
        raise ValueError("ARTEX Planner children 必须是数组")
    # depends_on（artex-deps）：串行链的前置 Intent id；模型给编号（如 "1"）
    # 时无法对应真实 id，故只在**同一批 items** 内按序号解析（见 _append_items）。
    depends = node.get("depends_on") or []
    if isinstance(depends, (str, int)):
        depends = [depends]
    if not isinstance(depends, list):
        raise ValueError("ARTEX Planner depends_on 必须是数组")
    return {"objective": objective, "expert": expert, "priority": priority,
            "constraints": constraints, "asset_ids": assets, "children": children,
            "depends_on": [str(x).strip() for x in depends
                           if x is not None and str(x).strip()]}


def plan_intents(*, bb, project_id: str, llm, goal: str, track: str,
                 allowed_experts: list[str] | None = None, asset_ids=None,
                 task_id: str = "", constraints_extra: dict | None = None) -> dict:
    """调用 planner LLM 并把任务树持久化为 ARTEX Intent。"""
    goal = str(goal or "").strip()
    if not goal:
        raise ValueError("ARTEX Planner goal 必填")
    allowed = {str(x).strip() for x in (allowed_experts or []) if str(x).strip()}
    prompt = (f"轨道: {track}\n目标: {goal}\n可用专家: {', '.join(sorted(allowed)) or '由系统选择'}\n"
              f"初始资产: {json.dumps(list(asset_ids or []), ensure_ascii=False)}")
    response = llm.chat([{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": prompt}])
    root = _normalize(_json_object(_text(response)), default_objective=goal,
                      allowed_experts=allowed)
    if not task_id:
        task_id = f"artex-task-{uuid.uuid4().hex[:12]}"
    created: list[dict] = []
    root_item = _persist_tree(bb, project_id, root, task_id=task_id, track=track,
                              goal=goal, allowed=allowed, asset_ids=asset_ids,
                              created=created, parent_id=None,
                              constraints_extra=constraints_extra)
    # 任务范围锚定（artex-coverage）：把 Planner 落树用到的资产登记为任务范围
    # （覆盖度分母）。上游 AddAutoScope 同口径——**只**从显式插入的资产登记，
    # 不从派生资产盲目扩范围。锚点缺失会让覆盖度回退到「Intent 资产并集」的
    # 推断口径，量化验收核对因此不够硬。
    anchored: list[str] = []
    try:
        from core.blackboard.artex_coverage import anchor_task_assets
        scope_ids: list[str] = [str(x).strip() for x in (asset_ids or [])
                                if str(x).strip()]
        for item in created:
            scope_ids.extend(str(x) for x in (item.get("asset_ids") or []) if str(x))
        scope_ids = list(dict.fromkeys(scope_ids))
        if scope_ids:
            reg = anchor_task_assets(bb=bb, project_id=project_id, task_id=task_id,
                                     asset_ids=scope_ids, source="planner",
                                     note="Planner 落树自动登记")
            anchored = reg.get("added") or []
    except Exception as exc:  # noqa: BLE001 —— 锚定失败不拦任务树落库
        log.warning("ARTEX 任务范围锚定失败 task=%s: %s", task_id, exc)
    # 目标注册（artex-goals）：LLM 分解 + 规则兜底，保证目标节点始终存在——
    # 没有它，终态只能退回「有 fact 即达成」的粗糙代理。
    try:
        goal_reg = set_goals(
            bb=bb, project_id=project_id, task_id=task_id,
            goals=decompose_goals(llm=llm, goal=goal, track=track),
            author="planner", asset_ids=asset_ids)
    except Exception as exc:  # noqa: BLE001 —— 目标登记失败不拦任务树落库
        log.warning("ARTEX 目标登记失败 task=%s: %s", task_id, exc)
        goal_reg = {"added": [], "skipped": [], "total": 0, "goals": []}
    return {"root": root_item, "intents": created, "count": len(created),
            "goals": goal_reg.get("added") or [],
            "goal_total": goal_reg.get("total") or 0,
            "anchored_assets": anchored}


def _ensure_parent_node(bb, project_id: str, parent_id: str) -> str | None:
    """确保父 Intent 在探索图里有对应节点，返回其节点 id（父 Intent 不存在则 None）。

    Intent 可能由 Planner 之外的入口创建（API 手工建树、测试、历史数据），
    这类 Intent 没有 ``{id}:explore`` 节点；此时直接 add_edge 会因「边两端
    节点必须存在」抛错，导致整批追加失败。补建节点让血缘始终可写。
    """
    node_id = f"{parent_id}:explore"
    if get_node(bb, project_id, node_id) is not None:
        return node_id
    parent = get_intent(bb, project_id, parent_id)
    if parent is None:
        return None
    objective = str(parent.get("objective") or "")
    create_node(bb, project_id, "intent", node_id=node_id, title=objective,
                content=objective, task_id=str(parent.get("task_id") or ""),
                intent_id=parent_id, status=str(parent.get("status") or "open"),
                asset_ids=parent.get("asset_ids") or [])
    return node_id


def _persist_tree(bb, project_id: str, node: dict, *, task_id: str, track: str,
                  goal: str, allowed: set[str], asset_ids, created: list[dict],
                  parent_id: str | None, derived_from: list[str] | None = None,
                  constraints_extra: dict | None = None,
                  dep_resolver=None) -> dict:
    """递归持久化 Planner 任务树，并同步写探索图 Goal/Intent 血缘。

    ``constraints_extra`` 是系统级约束（如 ``parent_thread_id`` 血缘），
    优先级高于 Planner 输出，保证「谁发起的任务」不被模型改写。

    ``dep_resolver`` 把节点 ``depends_on`` 里的引用（序号 ``"1"``/``"#1"`` 或真实
    Intent id）解析成本任务真实 id；无法解析的丢弃（不写幻觉依赖）。
    """
    normalized = _normalize(node, default_objective=goal, allowed_experts=allowed)
    raw_deps = normalized.get("depends_on") or []
    deps: list[str] = []
    if raw_deps:
        resolve = dep_resolver or (lambda values: [v for v in values])
        deps = [str(x) for x in resolve(raw_deps) if str(x)]
    item = create_intent(
        bb, project_id, normalized["objective"], task_id=task_id,
        expert=normalized["expert"], asset_ids=normalized["asset_ids"] or asset_ids,
        constraints={**normalized["constraints"], **(constraints_extra or {}),
                     "track": track},
        depends_on=deps,
        priority=normalized["priority"], parent_id=parent_id)
    created.append(item)
    explore = create_node(
        bb, project_id, "goal" if parent_id is None else "intent",
        node_id=f"{item['id']}:explore", title=normalized["objective"],
        content=normalized["objective"], task_id=task_id, intent_id=item["id"],
        status=item.get("status", "pending"), confidence=1.0,
        asset_ids=item.get("asset_ids") or [],
        metadata={"expert": normalized["expert"], "priority": normalized["priority"]},
    )
    if parent_id:
        parent_node = _ensure_parent_node(bb, project_id, parent_id)
        if parent_node:
            add_edge(bb, project_id, parent_node, explore["id"], "spawns",
                     metadata={"task_id": task_id})
    # 再规划生成的新方向：记录它依据的 Fact/Finding 血缘。递归子节点不传
    # derived_from，故只有顶层新 Intent 会连边。
    if derived_from:
        for source_id in derived_from:
            try:
                add_edge(bb, project_id, explore["id"], source_id, "derived_from",
                         metadata={"task_id": task_id, "source": "planner"})
            except ValueError:
                continue  # 来源节点已删除/跨项目：跳过，不影响 Intent 落库
    for child in normalized["children"]:
        _persist_tree(bb, project_id, child, task_id=task_id, track=track,
                      goal=goal, allowed=allowed, asset_ids=asset_ids,
                      created=created, parent_id=item["id"],
                      constraints_extra=constraints_extra, dep_resolver=dep_resolver)
    return item


def _fingerprint(text: str) -> str:
    """objective 归一化指纹（去空白与常见标点差异），用于防重复派发。

    与 ``artex_goals.fingerprint`` 同口径（同一函数），保证「Intent 去重」
    与「目标去重」不会因两套归一化规则产生偏差。
    """
    return fingerprint(text)


def _dep_resolver(existing: list[dict], created: list[dict]):
    """把 depends_on 里的引用解析成本任务真实 Intent id。

    支持三种写法（Planner 是 LLM，无法预知新建 Intent 的真实 id）：
    - ``"#1"`` / ``"1"`` / ``1``：**同一批 items 内**的序号（1 起，按输出顺序，
      含已创建项）；解析失败则尝试当作已存在 Intent 的序号。
    - ``"intent-<n>"`` / ``"步骤n"``：同一批内的宽松写法。
    - 真实 id（形如 ``artex-xxxxxxxx``）：原样保留（同任务校验由落库后兜底）。
    无法解析的引用直接丢弃——宁缺勿幻，避免写下指向不存在 Intent 的悬空依赖。
    """
    import re as _re

    def _index_of(value: str):
        token = str(value or "").strip().lstrip("#").strip()
        if not token:
            return None
        m = _re.fullmatch(r"(?:intent[-_ ]?|step[-_ ]?|步骤)?(\d+)", token,
                          flags=_re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                return None
        return None

    def resolve(values):
        out: list[str] = []
        for value in values or []:
            token = str(value or "").strip()
            if not token:
                continue
            idx = _index_of(token)
            if idx is not None:
                # 序号 1 起：先在本批 created 里取，再回落到已存在 Intent 序列。
                pool = created if 1 <= idx <= len(created) else existing
                if 1 <= idx <= len(pool):
                    out.append(str(pool[idx - 1].get("id") or ""))
                    continue
                # 本批序号指向尚未创建的后续项：留空由调用方丢弃（不做前向依赖）
                continue
            if token.startswith("artex-") or token.startswith("intent-") or len(token) > 12:
                out.append(token)
        return [x for x in out if x]

    return resolve


def _append_items(bb, project_id: str, raw_items: list, *, task_id: str, track: str,
                  goal: str, allowed: set[str], asset_ids, existing: list[dict],
                  parent_id: str | None, source_ids: list[str],
                  max_new: int, constraints_extra: dict | None = None) -> tuple[list[dict], int]:
    """把一批方向落成 Intent：指纹去重 + 数量上限，返回 (created, skipped)。

    LLM 规划（``plan_followup``）与主控显式操舵（``add_intents``）共用同一
    去重与持久化语义，避免两条入口对「重复方向」判定不一致。

    依赖解析（artex-deps）：``depends_on`` 里的序号引用在本批内按输出顺序解析
    为真实 id，使「串行链分步派」的声明可以只写 ``"#1"`` 而不必预知 id。
    """
    seen = {_fingerprint(row.get("objective") or "") for row in existing}
    created: list[dict] = []
    skipped = 0
    dep_resolver = _dep_resolver(existing, created)
    for raw in raw_items:
        if len(created) >= max(1, int(max_new)):
            break
        normalized = _normalize(raw, default_objective=goal, allowed_experts=allowed)
        fp = _fingerprint(normalized["objective"])
        if not fp or fp in seen:
            skipped += 1
            continue
        seen.add(fp)
        # 追加到任务根节点下，保持同一任务的 frontier 扩展语义。
        _persist_tree(bb, project_id, normalized, task_id=task_id, track=track,
                      goal=goal, allowed=allowed, asset_ids=asset_ids,
                      created=created, parent_id=parent_id,
                      derived_from=source_ids, constraints_extra=constraints_extra,
                      dep_resolver=dep_resolver)
    # 新增方向可能带入尚未锚定的资产：补登任务范围（覆盖度分母随之扩大）。
    try:
        from core.blackboard.artex_coverage import anchor_task_assets
        new_assets: list[str] = []
        for item in created:
            new_assets.extend(str(x) for x in (item.get("asset_ids") or []) if str(x))
        if new_assets:
            anchor_task_assets(bb=bb, project_id=project_id, task_id=task_id,
                               asset_ids=list(dict.fromkeys(new_assets)),
                               source="planner", note="Planner 新增方向锚定")
    except Exception as exc:  # noqa: BLE001 —— 锚定失败不拦方向落库
        log.warning("ARTEX 任务范围补锚失败 task=%s: %s", task_id, exc)
    return created, skipped


_FOLLOWUP_SYSTEM = """你是 ARTEX 探索规划器，正在同一个任务内推进探索。只输出 JSON，不要 Markdown。
格式：{"goal_covered":true|false,"goals_met":["目标ID"],"reason":"...","todos":[{"text":"...","status":"pending|in_progress|completed"}],"intents":[{"objective":"...","expert":"...","priority":整数,"constraints":{},"asset_ids":[],"depends_on":[],"children":[]}]}
判断规则：
- 已注册目标（见输入「任务目标」）是收官的唯一依据：只有**全部**目标被证明达成时，goal_covered 才可为 true。
- goals_met 只能填**已被证据证明达成**的目标 ID；系统会核验证据链，无证据的声明会被驳回。
- **量化验收核对（严禁提前盖章）**：目标含可量化条件（覆盖度达 X%、测过 N 个资产等）时，
  声明达成前【必须】核对输入「资产覆盖度」的实测值（tested/denominator、pct）；未达标就【禁止】
  声明该目标达成，改派意图补测未覆盖资产（见「未覆盖资产」清单）。不得以「大体达成」为由提前盖章。
- **深度优先于覆盖度**：覆盖度是下限/验收项，不是探索目标本身——发现高价值入口后优先把那条路
  打穿，而不是为拉平覆盖度去铺广逐个浅测。
- **串行利用链：分步派，别拆成并行**。强依赖的串行链（如 发现注入点 → 取凭据 → 横向 → 提权，
  后一步要用前一步的**实际产出**）不要一次性并行下发——下游拿不到还不存在的前置只会空转或重复。
  做法：用 todos 把整条链记成待办（每步一条，首步 in_progress、其余 pending），本轮只派
  「前置已满足」的第一步（通常 1 条）；下一轮唤醒时（输入会带上「规划待办」与已完成事实）
  再派下一步，并把已被事实满足的待办标 completed。已落库的后续步骤可以用 depends_on 指向
  本批内序号（同一批内按**输出顺序**从 1 开始编号，如第 2 项填 ["#1"]）作为双保险。
- **todos 是本任务的规划备忘，跨轮保留**：每轮输出**完整清单**（不是增量）——已完成的保留并标
  completed，新增步骤追加。清单已无剩余步骤时输出空数组或省略该字段以清空。没有串行链要记时可省略。
- 依据已有事实、发现和未完成 Intent，判断目标是否已被充分覆盖；已覆盖时 goal_covered=true 且 intents 必须为空数组。
- 仍需探索时，只列真正新增的方向，严禁重复已存在或已完成的方向。
- 未完成 Intent 已经在推进的方向不要再派发。
- **exhausted/blocked 不等于「已覆盖」**：exhausted = 撞运行预算被动收尾（试过但没做完），
  blocked = 模型/网络失败或前置未成功（基本没探成）。两者都属「中途没善终、信息不全」，
  【绝不能】当成该方向已覆盖而永久跳过——先从输入「没善终的 Intent」看清它是什么状态，
  再决定：接近突破被预算掐 → 派「接上次进度继续」的新意图；纯外部故障 → 直接重派同方向；
  反复卡同一处 → 换打法。依据是实际进度，不是状态本身。
- 不要输出命令、凭据或超出授权范围的目标。"""


def plan_followup(*, bb, project_id: str, llm, task_id: str, track: str,
                  goal: str = "", allowed_experts: list[str] | None = None,
                  asset_ids=None,                   facts: list[dict] | None = None,
                  findings: list[dict] | None = None,
                  max_new: int = 3, max_intents: int = 50,
                  constraints_extra: dict | None = None,
                  final: bool = False) -> dict:
    """基于探索图在同一 task_id 内追加 Intent，并判断目标是否已覆盖。

    与 ``plan_intents`` 的区别：不新建 task、不重建根节点，只在既有任务树
    下追加子 Intent；objective 归一化指纹去重，任务级 Intent 数超预算即停。

    ``final``（artex-taskclock）：任务到点后的**终局规划轮**（上游
    ``server/engine_timeout.go::runFinalPlannerRound`` + ``planner.go:386``
    ``tc.Final``）。此时：

    * 把「任务超时收尾词」作为**本轮特殊指令**拼进 user 输入（覆盖常规流程）；
    * **不落任何新 Intent**——上游注释：「此时派意图也不会再被执行」。确定性
      兜住这点，而不是只靠提示词（模型仍可能照常输出 intents 数组）。

    目标纪律（artex-goals）：``goal_covered`` 不再采信模型自述——有注册目标
    时一律由 ``goals_stats``（全部 met）派生；模型可在 ``goals_met`` 里声明
    目标达成，但须过 ``verify_goals_met`` 的证据硬闸。

    事实纪律（artex-record-fact）：``facts=None`` 时**自动读探索图事实库**。
    Planner 的决策质量完全取决于它看到什么——「忘了传 facts」就退化成盲规划，
    这种失误不该由调用方负责。显式传列表（含空列表）仍按传入内容处理。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    allowed = {str(x).strip() for x in (allowed_experts or []) if str(x).strip()}
    if facts is None:
        try:
            facts = list_nodes(bb, project_id, task_id=task_id, kind="fact")
        except Exception:  # noqa: BLE001 —— 读事实失败不拦规划
            log.warning("ARTEX 事实库自动读取失败 task=%s", task_id, exc_info=True)
            facts = []
    existing = [row for row in list_intents(bb, project_id, limit=2000)
                if str(row.get("task_id") or "") == task_id]
    root = next((row for row in existing if not row.get("parent_id")), None)
    goal = str(goal or (root or {}).get("objective") or "").strip()
    if len(existing) >= max(1, int(max_intents)):
        return {"task_id": task_id, "goal_covered": False, "intents": [],
                "added": 0, "budget_exhausted": True,
                "reason": f"任务 Intent 数已达上限 {max_intents}"}
    goals = list_goals(bb, project_id, task_id)
    stats = goals_stats(bb, project_id, task_id)
    open_rows = [row for row in existing
                 if row.get("status") in {"pending", "claimed", "running", "paused"}]
    prompt_parts = [
        f"轨道: {track}",
        f"任务 ID: {task_id}",
        f"目标: {goal or '（未记录）'}",
        f"可用专家: {', '.join(sorted(allowed)) or '由系统选择'}",
        f"初始资产: {json.dumps(list(asset_ids or []), ensure_ascii=False)}",
    ]
    if final:
        # 终局规划轮（artex-taskclock）：任务已到点，这一轮的唯一目的是「做最后
        # 目标判定」。上游 planner.go:388 把任务超时收尾词拼进 situational（本轮
        # user 输入）并声明「覆盖上面的常规规划流程」——同样放在最前面，避免被
        # 后面的大段态势/清单稀释。
        from core.chat.wrapup import task_timeout_prompt
        final_words = task_timeout_prompt("planner")
        if final_words:
            prompt_parts.insert(0, final_words)
    # 资产覆盖度（artex-coverage）：量化验收核对的实测值来源。没有它，Planner
    # 只能凭「大体达成」盖章——上游 planner.go:316 明确要求核对 coverage.pct。
    try:
        from core.blackboard.artex_coverage import list_untested_assets, task_coverage
        coverage = task_coverage(bb, project_id, task_id)
    except Exception:  # noqa: BLE001 —— 覆盖度读取失败不拦规划
        coverage = {"enabled": False}
    if coverage.get("enabled") and coverage.get("denominator"):
        by_type = "，".join(
            f"{row['type']} {row['tested']}/{row['total']}"
            for row in coverage.get("by_type") or [])
        pct = coverage.get("pct")
        prompt_parts.append(
            f"资产覆盖度（粗粒度参考，量化目标的实测值来源）："
            f"已测 {coverage.get('tested')}/{coverage.get('denominator')}"
            f"（{('%.0f%%' % (pct * 100)) if pct is not None else '—'}）"
            + (f"；分类型：{by_type}" if by_type else "")
            + f"；范围来源：{'任务锚点' if coverage.get('scope_source') == 'anchored' else '由 Intent/目标推断'}")
        if coverage.get("untested"):
            try:
                backlog = list_untested_assets(bb, project_id, task_id, limit=12)
            except Exception:  # noqa: BLE001
                backlog = {"assets": [], "total": coverage.get("untested")}
            prompt_parts.append(
                f"未覆盖资产（共 {coverage.get('untested')} 个，优先补测）:\n"
                + "\n".join(f"- {a['id']}｜{a['type']}｜{str(a['label'])[:100]}"
                            for a in backlog.get("assets") or [])
                or "- （无）")
    if goals:
        def _goal_line(g: dict) -> str:
            marks = [f"状态={g.get('status') or 'open'}"]
            vc = str((g.get("metadata") or {}).get("vulnclass") or "")
            if vc:
                marks.append(f"漏洞类={vc}")
            if g.get("evidence"):
                marks.append(f"证据={len(g['evidence'])}条")
            return f"- {g.get('id')}｜{str(g.get('title') or '')[:120]}（{'，'.join(marks)}）"
        prompt_parts.append(
            f"任务目标（共 {stats['total']} 条，已达成 {stats['met']}，未达成 {stats['open']}；"
            "goal_covered 只有在未达成=0 时才可为 true）:\n"
            + "\n".join(_goal_line(g) for g in goals[:20]))
    if facts:
        # 事实的**置信度与血缘**必须进提示词（artex-record-fact）：只给标题正文时
        # 「8080 关闭」和「8080 开放」在 Planner 眼里完全等价，observed 与 inferred
        # 也不可分——而这两条正是它决定「该方向是否已穷尽」的依据。
        def _fact_line(item: dict) -> str:
            meta = item.get("metadata") or {}
            level = str(meta.get("confidence_level") or "")
            if not level:
                level = ("observed" if float(item.get("confidence") or 0) >= 1.0
                         else "inferred")
            src = str(item.get("intent_id") or "")
            tags = [level]
            if src:
                tags.append(f"来自 {src}")
            evidence = str(meta.get("evidence") or "").strip()
            if evidence:
                tags.append(f"证据 {evidence[:120]}")
            return (f"- {str(item.get('title') or '')[:80]}"
                    f"（{'；'.join(tags)}）：{str(item.get('content') or '')[:300]}")
        prompt_parts.append("已有事实（confidence=observed 是直接看到、"
                            "inferred 是推断；否定结论多为 inferred）:\n"
                            + "\n".join(_fact_line(item) for item in facts[:12]))
        if len(facts) > 12:
            prompt_parts.append(f"（另有 {len(facts) - 12} 条更早的事实未列出；"
                                "用 list_facts 按需翻看）")
    if findings:
        prompt_parts.append("已有发现:\n" + "\n".join(
            f"- [{item.get('severity') or 'info'}] {str(item.get('title') or '')[:100]}"
            for item in findings[:12]))
    if open_rows:
        prompt_parts.append("仍在执行的 Intent:\n" + "\n".join(
            f"- {str(row.get('objective') or '')[:100]}（{row.get('status')}）"
            for row in open_rows[:12]))
    # 没善终的 work（artex-exhausted）：exhausted/blocked/failed 都是「中途没做完、
    # 信息不全」——必须先看 trace 再决定续跑还是换向，绝不能当成已覆盖永久跳过。
    # 上游 planner.go:327 的口径即此；这里把三类分别列出并给出处置指引。
    unfinished = [row for row in existing
                  if str(row.get("status") or "") in {"exhausted", "blocked", "failed"}]
    if unfinished:
        def _unfinished_line(row: dict) -> str:
            status = str(row.get("status") or "")
            note = {
                "exhausted": "撞运行预算被动收尾（试过但没做完）",
                "blocked": "模型/网络失败或前置未成功（基本没探成）",
                "failed": "执行失败",
            }.get(status, status)
            err = str(row.get("error") or "")[:120]
            return (f"- {str(row.get('objective') or '')[:80]}（{status}：{note}"
                    + (f"；{err}" if err else "") + "）")
        prompt_parts.append(
            "没善终的 Intent（**不要当成已覆盖**——先用 artex_worker_trace_get 看它实际"
            "做到哪、卡在哪，再从下面选：接近突破被预算掐 → 派「接上次进度继续」；"
            "纯外部故障没跑成 → 直接重派同方向；每次卡同一处 → 换打法/方向）:\n"
            + "\n".join(_unfinished_line(row) for row in unfinished[:12]))
    # 依赖关系（artex-deps）：让 Planner 看清哪些是串行链下游，避免重复派/错序派。
    dep_lines: list[str] = []
    by_id = {str(row.get("id")): row for row in existing}
    for row in existing:
        deps = [str(x) for x in (row.get("depends_on") or []) if str(x)]
        if not deps:
            continue
        labels = []
        for dep_id in deps:
            dep = by_id.get(dep_id)
            if dep is None:
                labels.append("（前置已删除，视为满足）")
            else:
                labels.append(f"{str(dep.get('objective') or '')[:60]}［{dep.get('status')}］")
        dep_lines.append(
            f"- {str(row.get('objective') or '')[:80]}（{row.get('status')}）"
            f" ← 依赖：{'；'.join(labels)}")
    if dep_lines:
        prompt_parts.append(
            "依赖关系（下游必须等前置 completed 才会被调度；前置 failed/cancelled 时"
            "下游会被置 blocked，需要先处理前置）:\n" + "\n".join(dep_lines[:20]))
    prompt_parts.append("已存在方向（禁止重复）:\n" + "\n".join(
        f"- {str(row.get('objective') or '')[:100]}" for row in existing[:20]))
    # 规划待办（artex-planner-todos）：跨唤醒保留的串行链清单。首轮为空不注入。
    # 先按既有 Intent 的实际状态校正（确定性），再渲染给模型，避免模型凭记忆
    # 误判「上一步还没做完」而重复派。
    try:
        from core.blackboard.artex_runtime import (reconcile_planner_todos,
                                                   render_planner_todos)
        todos_note = render_planner_todos(reconcile_planner_todos(bb, project_id, task_id))
        if todos_note:
            prompt_parts.append(todos_note)
    except Exception:  # noqa: BLE001 —— 待办读取失败不拦规划
        log.warning("ARTEX 规划待办注入失败 task=%s", task_id, exc_info=True)
    prompt = "\n\n".join(prompt_parts)
    response = llm.chat([{"role": "system", "content": _FOLLOWUP_SYSTEM},
                         {"role": "user", "content": prompt}])
    payload = _json_object(_text(response))
    model_covered = bool(payload.get("goal_covered", False))
    reason = str(payload.get("reason") or "")[:500]
    raw_items = payload.get("intents") or []
    if not isinstance(raw_items, list):
        raise ValueError("ARTEX Planner intents 必须是数组")
    # 目标达成声明：模型只能提名，翻转由证据链裁决（无证据进 rejected）。
    verdict = {"accepted": [], "rejected": []}
    claimed = payload.get("goals_met") or []
    if claimed:
        if not isinstance(claimed, list):
            claimed = [claimed]
        verdict = verify_goals_met(bb, project_id, task_id, claimed)
        stats = goals_stats(bb, project_id, task_id)
    reject_note = ""
    if verdict["rejected"]:
        reject_note = "；目标达成声明被驳回: " + "；".join(
            f"{row['goal_id']}（{row['why']}）" for row in verdict["rejected"][:5])
    # 规划待办回写（artex-planner-todos）：模型每轮输出**完整清单**（TodoWrite 语义），
    # 落 runtime metadata 跨唤醒保留。显式给了 todos 才覆盖——没给时保留上轮清单，
    # 不因模型漏写字段而清空整条链。
    todo_rows: list[dict] = []
    if "todos" in payload:
        try:
            from core.blackboard.artex_runtime import set_planner_todos
            raw_todos = payload.get("todos")
            todo_rows = set_planner_todos(
                bb, project_id, task_id,
                raw_todos if isinstance(raw_todos, list) else [])
        except Exception:  # noqa: BLE001 —— 待办落库失败不拦规划
            log.warning("ARTEX 规划待办回写失败 task=%s", task_id, exc_info=True)
    # 收官判定：有注册目标时以目标态为准，模型的 goal_covered 只是参考。
    if stats["total"] > 0:
        goal_covered = stats["open"] == 0
    else:
        goal_covered = model_covered
    base = {"task_id": task_id, "goals_met": verdict["accepted"],
            "goals_rejected": verdict["rejected"], "goal_stats": stats,
            "coverage": coverage, "todos": todo_rows}
    if goal_covered:
        covered_reason = reason + reject_note
        if stats["total"] > 0 and model_covered and stats["open"] == 0:
            covered_reason = (reason or "全部注册目标已达成") + reject_note
        return {**base, "goal_covered": True, "intents": [], "added": 0,
                "reason": covered_reason[:500], "budget_exhausted": False}
    if final:
        # 终局规划轮**不落新 Intent**（artex-taskclock）：上游「此时派意图也不会
        # 再被执行」。确定性兜住——即便模型仍输出 intents 数组，也只记数不落库，
        # 避免留下一批永远跑不到 pending，把任务终态拖回 exploring。
        return {**base, "goal_covered": False, "intents": [], "added": 0,
                "skipped": len(raw_items), "final": True,
                "reason": ("任务已到点：终局轮只做目标判定，不派新方向"
                           + reject_note)[:500],
                "budget_exhausted": False}
    # 新方向依据的既有探索节点（Fact 优先，其次 Finding），供 derived_from 留痕。
    source_ids = [str(n.get("id") or "") for n in (facts or [])][:8]
    source_ids += [str(n.get("id") or "") for n in (findings or [])][:8]
    source_ids = [sid for sid in source_ids if sid]
    created, skipped = _append_items(
        bb, project_id, raw_items, task_id=task_id, track=track, goal=goal,
        allowed=allowed, asset_ids=asset_ids, existing=existing,
        parent_id=(root or {}).get("id"), source_ids=source_ids,
        max_new=max_new, constraints_extra=constraints_extra)
    return {**base, "goal_covered": False, "intents": created,
            "added": len(created), "skipped": skipped,
            "reason": (reason + reject_note)[:500], "budget_exhausted": False}


def add_intents(*, bb, project_id: str, task_id: str, track: str,
                items: list[dict], goal: str = "",
                allowed_experts: list[str] | None = None, asset_ids=None,
                derived_from: list[str] | None = None,
                max_new: int = 3, max_intents: int = 50,
                constraints_extra: dict | None = None,
                todos=None) -> dict:
    """把主控给定的方向直接落成 Intent（不经 LLM），追加到既有任务。

    与 ``plan_followup`` 的区别只有一点：方向由调用方（ARTEX 主控）显式给出，
    不再让 Planner LLM 二次决策。去重、数量上限、``derived_from`` 血缘与
    持久化语义完全共用 ``_append_items``，因此两条入口不会产生口径差异。

    ``todos``（可选）：主控指定的规划待办整表，覆盖任务级清单（artex-planner-todos）。
    传 ``[]`` 清空；不传（``None``）保持原清单不动。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    if not isinstance(items, list) or not items:
        raise ValueError("items 必须是非空数组")
    allowed = {str(x).strip() for x in (allowed_experts or []) if str(x).strip()}
    existing = [row for row in list_intents(bb, project_id, limit=1000)
                if str(row.get("task_id") or "") == task_id]
    if len(existing) >= max(1, int(max_intents)):
        return {"task_id": task_id, "goal_covered": False, "intents": [],
                "added": 0, "skipped": 0, "budget_exhausted": True,
                "reason": f"任务 Intent 数已达上限 {max_intents}"}
    root = next((row for row in existing if not row.get("parent_id")), None)
    goal = str(goal or (root or {}).get("objective") or "").strip()
    source_ids = [str(sid).strip() for sid in (derived_from or []) if str(sid).strip()]
    created, skipped = _append_items(
        bb, project_id, items, task_id=task_id, track=track, goal=goal,
        allowed=allowed, asset_ids=asset_ids, existing=existing,
        parent_id=(root or {}).get("id"), source_ids=source_ids,
        max_new=max_new, constraints_extra=constraints_extra)
    todo_rows: list[dict] = []
    if todos is not None:
        try:
            from core.blackboard.artex_runtime import set_planner_todos
            todo_rows = set_planner_todos(
                bb, project_id, task_id, todos if isinstance(todos, list) else [])
        except Exception:  # noqa: BLE001 —— 待办落库失败不拦方向落库
            log.warning("ARTEX 规划待办写入失败 task=%s", task_id, exc_info=True)
    return {"task_id": task_id, "goal_covered": False, "intents": created,
            "added": len(created), "skipped": skipped, "reason": "",
            "todos": todo_rows, "budget_exhausted": False}

