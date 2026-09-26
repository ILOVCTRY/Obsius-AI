"""分阶段工作流引擎（pentest-phased-workflow M1+M2，DESIGN.md「分阶段工作流」）。

渗透测试项目三阶段推进（信息收集 → 渗透测试 → 报告，可逆流转）：

- **阶段剧本** = `packs/tracks/<track>/phases/<phase>.yaml`（轨级默认；项目
  `config.phases` 同名条目整体覆写）。文件名 stem=阶段 id（英文），`name`=中文
  显示名。解析用受约束极简 yaml 子集（零 PyYAML，同 experts/profiles 约定：
  顶层 `key: value`、2 空格缩进二级块、`- ` 列表项、内联列表/整数经
  `_parse_inline_value`）。
- **项目 meta JSON**（project.json，单一真相源）存 `current_phase` /
  `phase_history` / `playbook_fired` / `gate_open_notified`——与 phase_goal 同层，
  黑板 projects 行无此列。
- **入场门**（唯一硬约束）：当前阶段 `gate` 指标未过且 task_type ∈ 该阶段
  `gate_types` 时任务双层拦截（编排器派单侧 + 发布 API 422 带原因）；认领侧
  不动。过门动作按自主档分流（L0 事件提示 / L1 审批单 / L2 自动流转），由
  API 层执行（本模块只提供判定与流转原语）。
- 阶段首发（D3）：显式流转进入阶段时剧本 `tasks[]` 原样发布
  （`created_by=playbook`），`playbook_fired` 按（阶段，任务指纹）去重——回退
  重进不重发。项目创建只登记初始阶段（`publish=False`）不直发——mission/目标
  尚未商议，静态任务直发会在 L2 下空转烧 LLM；初始阶段剧本随第一次显式流转
  回退重进时补发。首发任务统一 `noise_budget="passive"`（阶段引导是 objective
  级、无具体目标，不占 active 互斥键；注册表噪声只约束编排器/Agent 的 target
  级派单，实施修正于定稿「噪声取注册表默认」——exploit 类剧本任务无
  conflict_keys 会被队列硬拦）。
- 回退不走门（降级动作低风险），但走事件留痕；抵达时门已过则抑制过门
  通知重发（防人工回退后被 L2 秒弹回）。

本模块不 import core.orchestrator（其反向依赖本模块），空闲轮数由调用方读
orchestrator_state 后以 `idle_rounds` 传入。
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.blackboard import TaskQueue
from core.blackboard.tasks import dedup_fp
from core.projects import PROJECT_FILE
from core.skills.roles import _parse_inline_value
from core.skills.taxonomy import load_task_types, track_dir

log = logging.getLogger(__name__)

PHASES_DIR = "phases"
HIGH_VALUE_TAG = "高价值"  # 与编排器 _assets_view HVT 口径一致

# gate 指标键 → (metrics 键, 人读标签)；未知键 doctor 报 warning、判定忽略
_GATE_KEYS = (
    ("min_assets", "assets", "资产"),
    ("min_high_value", "high_value", "高价值资产"),
    ("min_verified", "verified", "verified 发现"),
)
GATE_KEYS = frozenset(k for k, _, _ in _GATE_KEYS) | {"idle_rounds"}


# ---------- 剧本解析 ----------

def parse_phase_file(path: Path) -> dict:
    """阶段剧本极简解析。仅支持：顶层 `key: value`；`focus:`/`gate:` 二级块
    （2 空格缩进）；`tasks:` 的 `- key: value` 列表项（2 空格起，**续键 4 空格**
    与常规 YAML 对齐）。其余缩进/形态一律 ValueError（加载侧跳过、doctor 报
    phase-bad-yaml）。"""
    out: dict = {}
    block: str | None = None      # 当前二级块名（focus/gate）
    tasks: list | None = None
    item: dict | None = None
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
            block, tasks, item = None, None, None
            if key in ("focus", "gate"):
                if value.strip():
                    raise ValueError(f"{key} 须为缩进块（不支持内联 map）")
                block = key
                out[key] = {}
            elif key == "tasks":
                tasks = out.setdefault("tasks", [])
            else:
                out[key] = _parse_inline_value(value)
        elif indent == 2:
            if tasks is not None:
                if is_item:
                    item = {}
                    tasks.append(item)
                elif item is None:
                    raise ValueError(f"tasks 列表项缩进错误: {stripped!r}")
                item[key] = _parse_inline_value(value)
            elif block:
                out[block][key] = _parse_inline_value(value)
            else:
                raise ValueError(f"未知缩进块: {stripped!r}")
        elif indent == 4:
            # 仅允许 tasks 列表项的续键（- 项的后续字段）；他处 4 空格一律拒
            if tasks is None or item is None:
                raise ValueError(f"缩进层级过深（4 空格仅限 tasks 项续键）: {stripped!r}")
            item[key] = _parse_inline_value(value)
        else:
            raise ValueError(f"缩进层级过深（仅支持 0/2 空格）: {stripped!r}")
    return out


def phase_spec(stem: str, data: dict) -> dict:
    """剧本原始 dict → 归一化规格（非法值 ValueError，加载侧跳过）。"""
    if not isinstance(data, dict):
        raise ValueError("剧本须为映射")
    spec: dict = {
        "id": str(stem),
        "name": str(data.get("name") or stem),
        "goal": str(data.get("goal") or ""),
        "order": data.get("order") if isinstance(data.get("order"), int) else 0,
        "next": [str(x).strip() for x in (data.get("next") or []) if str(x).strip()],
        "gate_types": [str(x).strip() for x in (data.get("gate_types") or [])
                       if str(x).strip()],
    }
    for key in ("focus", "gate"):
        raw = data.get(key)
        if raw is None:
            spec[key] = {}
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{key} 须为映射")
        conv: dict[str, int] = {}
        for k, v in raw.items():
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{key}.{k} 须为非负整数，得到: {v!r}")
            conv[str(k).strip()] = v
        spec[key] = conv
    spec["tasks"] = []
    for t in data.get("tasks") or []:
        if not isinstance(t, dict) or not str(t.get("task_type") or "").strip() \
                or not str(t.get("objective") or "").strip():
            raise ValueError("tasks 条目缺 task_type/objective")
        spec["tasks"].append({
            "task_type": str(t["task_type"]).strip(),
            "role": str(t.get("role") or "").strip(),
            "objective": str(t["objective"]).strip(),
            "acceptance": str(t.get("acceptance") or "").strip(),
        })
    return spec


def load_track_phases(packs_root: str | Path, track: str,
                      project_config: dict | None = None) -> dict[str, dict]:
    """轨级阶段剧本 {phase_id: spec}（order 升序，同序按 stem）。目录缺失=空
    （该轨无阶段机制）。坏文件 log 跳过（doctor 报 phase-bad-yaml）。
    项目级覆写：`config.phases` 同名条目整体替换（也允许新增），坏条目跳过。"""
    book: dict[str, dict] = {}
    base = track_dir(packs_root, track) / PHASES_DIR
    if base.is_dir():
        for i, p in enumerate(sorted(base.glob("*.yaml"))):
            try:
                spec = phase_spec(p.stem, parse_phase_file(p))
            except ValueError as e:
                log.warning("阶段剧本解析失败 %s: %s", p, e)
                continue
            if not spec["order"]:
                spec["order"] = i + 1
            book[p.stem] = spec
    override = (project_config or {}).get("phases")
    if isinstance(override, dict):
        for pid, data in override.items():
            try:
                spec = phase_spec(str(pid), data if isinstance(data, dict) else {})
            except ValueError as e:
                log.warning("项目级阶段覆写非法 %s: %s", pid, e)
                continue
            if not spec["order"]:
                spec["order"] = book[pid]["order"] if pid in book else len(book) + 1
            book[str(pid)] = spec
    return dict(sorted(book.items(), key=lambda kv: (kv[1]["order"], kv[0])))


def phase_enabled(packs_root: str | Path, track: str,
                  project_config: dict | None = None) -> bool:
    return bool(load_track_phases(packs_root, track, project_config))


def default_phase_id(book: dict[str, dict]) -> str | None:
    return next(iter(book), None)


def forward_targets(book: dict[str, dict], spec: dict) -> list[str]:
    """spec.next 中 order 递增（前向）的目标，按 order 排序——过门分流与
    抵达校准只针对首个（主推进方向）；回退（order 递减）不走门。"""
    out = [nid for nid in spec.get("next") or []
           if nid in book and int(book[nid]["order"]) > int(spec["order"])]
    return sorted(out, key=lambda nid: int(book[nid]["order"]))


# ---------- 项目 meta 状态 ----------

def read_state(meta: dict) -> dict:
    """读项目 meta 的阶段状态（缺键=缺省：current None→调用方落首个阶段）。"""
    return {
        "current": str(meta.get("current_phase") or "") or None,
        "history": list(meta.get("phase_history") or []),
        "fired": {k: list(v) for k, v in (meta.get("playbook_fired") or {}).items()},
        "notified": str(meta.get("gate_open_notified") or "") or None,
    }


def _load_meta(proj) -> dict:
    return json.loads((proj.path / PROJECT_FILE).read_text(encoding="utf-8"))


def _save_meta(proj, meta: dict) -> None:
    (proj.path / PROJECT_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    proj.meta = meta


def read_gate_state(meta: dict) -> dict | None:
    """读 meta 的出口门评估状态（B3 单一事实源；缺键/阶段不符由调用方判断）。"""
    st = meta.get("phase_gate_state")
    if isinstance(st, dict) and st.get("phase"):
        return dict(st)
    return None


def save_gate_state(proj, state: dict) -> None:
    """出口门评估状态落 meta（B3：注入侧 _phase_section 只读不重算，与动作同源）。
    内容不变跳过写盘——_phase_gate_check 每轮 tick 都评估，防无谓重写 project.json。"""
    meta = _load_meta(proj)
    if meta.get("phase_gate_state") == state:
        return
    meta["phase_gate_state"] = state
    _save_meta(proj, meta)


def set_gate_notified(proj, target: str) -> None:
    """过门动作已分流（事件/审批/自动）标记：target 阶段 id。抵达目标阶段时
    由 enter_phase 重置。"""
    meta = _load_meta(proj)
    meta["gate_open_notified"] = target
    _save_meta(proj, meta)


# ---------- 入场门 ----------

def gate_metrics(bb, project_id: str, *, idle_rounds: int = 0) -> dict:
    """门指标现值：资产总数 / ⭐ 高价值资产数 / verified 发现数 / 空闲轮数
    （idle_rounds 由调用方读 orchestrator_state.derive_idle_rounds 传入）。"""
    return {
        "assets": len(bb.list_assets(project_id)),
        "high_value": len(bb.list_assets(project_id, tag=HIGH_VALUE_TAG)),
        "verified": sum(1 for f in bb.list_findings(project_id)
                        if f.get("status") == "verified"),
        "idle_rounds": int(idle_rounds),
    }


def evaluate_gate(gate: dict, metrics: dict) -> tuple[bool, list[str]]:
    """确定性判门（不依赖 LLM）：非 idle 指标全达标=过门；未达标但
    idle_rounds 达标=收集饱和逃生（连续 N 轮派不出新任务）。返回 (过门, 未达标描述)。"""
    gate = gate or {}
    unmet = [f"{label} {metrics.get(mk, 0)}/{gate[key]}"
             for key, mk, label in _GATE_KEYS
             if isinstance(gate.get(key), int) and metrics.get(mk, 0) < gate[key]]
    idle_req = gate.get("idle_rounds")
    if unmet and isinstance(idle_req, int) and metrics.get("idle_rounds", 0) >= idle_req:
        return True, []
    return not unmet, unmet


def current_spec(book: dict[str, dict], meta: dict) -> tuple[str, dict] | None:
    """当前阶段 (id, spec)；meta 无记录时落首个阶段（不写盘，读态缺省）。"""
    if not book:
        return None
    cur = read_state(meta)["current"] or default_phase_id(book)
    return cur, book[cur]


def gate_block_reason(bb, project_id: str, meta: dict, track: str,
                      packs_root: str | Path, *, task_type: str,
                      idle_rounds: int = 0) -> str | None:
    """入场门拦截判定：当前阶段 `gate_types` 含 task_type 且门未过 → 返回
    拒发原因（含门进度），None=放行。认领侧不拦；人工流转阶段是放行阀。"""
    book = load_track_phases(packs_root, track, meta.get("config"))
    cur = current_spec(book, meta)
    if cur is None:
        return None
    pid, spec = cur
    gate = spec.get("gate") or {}
    if task_type not in (spec.get("gate_types") or []) or not gate:
        return None
    metrics = gate_metrics(bb, project_id, idle_rounds=idle_rounds)
    met, unmet = evaluate_gate(gate, metrics)
    if met:
        return None
    return (f"「{task_type}」类任务被阶段入场门拦截：当前阶段「{spec['name']}」"
            f"门指标未达标（{'；'.join(unmet)}）——先推进收集，或由人类流转阶段"
            "（POST /api/projects/{id}/phase）")


# ---------- 阶段流转（剧本首发 + 状态落盘 + 事件留痕） ----------

def enter_phase(proj, to: str, *, by: str, packs_root: str | Path,
                auto: bool = False, reason: str = "", publish: bool = True,
                idle_rounds: int = 0) -> dict:
    """流转到目标阶段（方向合法性由调用方把关：人工限 spec.next 内、L2 自动
    只走前向过门、审批批准即流转）。

    职责：①meta 写 current_phase/phase_history/playbook_fired（playbook_fired
    按（阶段，指纹）去重——重进不重发）②publish=True 时剧本 tasks[] 原样首发
    （created_by=playbook、acceptance→判据、noise=passive——阶段引导 objective
    级不占 active 互斥键、优先级 1；同款任务已在队（find_dedup_target）也跳过；
    项目创建登记初始阶段传 publish=False——mission 商议前不发静态任务，首发随
    显式流转触发）③抵达校准 gate_open_notified（新阶段门已过则标记，防 L2
    秒弹回）④phase.changed 事件留痕。返回 {"from", "to", "published", "spec"}。"""
    meta = _load_meta(proj)
    book = load_track_phases(packs_root, proj.track, meta.get("config"))
    if to not in book:
        raise ValueError(f"目标阶段不存在: {to}（本轨阶段: {sorted(book)}）")
    st = read_state(meta)
    prev = st["current"] or default_phase_id(book)
    now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tq = TaskQueue(proj.bb)
    table = load_task_types(packs_root, proj.track)
    fired: list[str] = list(st["fired"].get(to) or [])
    published: list[str] = []
    for t in (book[to]["tasks"] if publish else []):
        fp = dedup_fp(t["task_type"], "", t["objective"])
        if fp in fired:
            continue
        if tq.find_dedup_target(proj.id, fp) is not None:
            fired.append(fp)  # 同款已在队/在跑：吸收首发（指纹计入，重进不重发）
            continue
        try:
            tid = tq.publish(
                proj.id, t["objective"], task_type=t["task_type"],
                noise_budget="passive",
                priority=1, role=t["role"],
                acceptance=([t["acceptance"]] if t["acceptance"] else None),
                created_by="playbook", allowed_types=table.keys())
        except ValueError as e:
            log.warning("剧本首发任务被拒 phase=%s type=%s: %s", to, t["task_type"], e)
            continue
        fired.append(fp)
        published.append(tid)
    meta["current_phase"] = to
    history = [*st["history"], {"phase": to, "entered_at": now_ts, "by": by,
                                "auto": auto,
                                **({"reason": reason} if reason else {})}]
    meta["phase_history"] = history
    meta["playbook_fired"] = {**st["fired"], to: fired}
    # 抵达校准：新阶段门已过（含空闲逃生）→ 标记已分流，抑制 L2 抵达即弹回。
    # 同处顺写 phase_gate_state（B3 单一事实源）：流转即落新阶段门状态，注入侧
    # 第一轮就有真值，无「清空空窗」；gate_metrics 本就在此计算，零额外开销。
    notified = None
    fwd = forward_targets(book, book[to])
    gate = book[to].get("gate") or {}
    if fwd and gate:
        metrics = gate_metrics(proj.bb, proj.id, idle_rounds=idle_rounds)
        met, unmet = evaluate_gate(gate, metrics)
        meta["phase_gate_state"] = {"phase": to, "gate": True,
                                    "passed": met, "unmet": unmet}
        if met:
            notified = fwd[0]
    else:
        meta["phase_gate_state"] = {"phase": to, "gate": False,
                                    "passed": False, "unmet": []}
    if notified:
        meta["gate_open_notified"] = notified
    else:
        meta.pop("gate_open_notified", None)
    _save_meta(proj, meta)
    proj.bb.append_event(
        proj.id, "phase.changed",
        {"from": prev, "to": to, "by": by, "auto": auto, "published": published,
         **({"reason": reason} if reason else {})},
        author=by or "system")
    return {"from": prev, "to": to, "published": published, "spec": book[to]}
