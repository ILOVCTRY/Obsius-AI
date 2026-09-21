"""统一变更提案通道（DESIGN.md §4，C4）。

技能修改与 kb 变更（新建/改写/改名/删除）统一走提案：

- 提案落 ``packs/.proposals/pp_<UTCts>_<6hex>.json``，三态
  pending/approved/rejected；
- **不存旧内容快照**——审批时对照磁盘现状实时做 unified diff（磁盘已变就如实展示）；
- AI 对技能有 edit 提案权 + suggest（拆分建议，产文档由人执行）；kb 允许全部 4 模式；
  index edit + create（K4，为无索引包建骨架；删除归人类）；
  case edit + create（K6 成功链沉淀：测试包 成功案例.md 补段 / payloads/ 补弹药）；
- 应用复用 writing.py 的同一套备份与 refs.py 的改名引用联动；
- 状态转移只在本模块发生，零 fastapi 依赖，Agent 工具/API/job 共用。

审计事件（proposal.created/applied/rejected）由调用方（Agent 工具/API/job）
经黑板 append_event 发出——核心模块不持有黑板引用。
"""

from __future__ import annotations

import difflib
import json
import re
import secrets
import time
from pathlib import Path

from core.skills import writing
from core.skills.registry import SkillRegistry, parse_frontmatter

# 模式权限：技能 edit（编辑）+ suggest（拆分建议，产文档由人执行）；kb 四模式全允许；
# index 仅 edit（M0 全局单表 packs/kb/route_index.yaml 恒存在，K4 的 create 退役；
# 条目 kb 路径须带本域前缀，apply 时按域归并写回，删除仍归人类）
KB_MODES = {"edit", "create", "rename", "delete"}
SKILL_MODES = {"edit", "suggest"}
INDEX_MODES = {"edit"}
# K6（2026-09-20 外部对标升级项 A，RapidPen 成功案例复用）：成功链沉淀——
# verified 攻击链补测试包 成功案例.md（edit）或 payloads/ 补弹药（create）；
# rename/delete 归人类。校验/应用管道与 kb 同源，人审闸门防 EVOMAL 自投毒。
CASE_MODES = {"edit", "create"}
STATUSES = {"pending", "approved", "rejected"}
ORIGINS = {"agent", "review", "human"}
_PROPOSAL_RE = re.compile(r"^(pp_\d{8}T\d{6}Z_[0-9a-f]{6})$")
_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


class ProposalError(ValueError):
    """提案非法（API→422）。"""


class ProposalStateError(ProposalError):
    """提案状态不符/不存在（API→409/404）。"""


def _now() -> str:
    return time.strftime(_UTC_FMT, time.gmtime())


def proposals_dir(packs_root: str | Path) -> Path:
    return Path(packs_root) / ".proposals"


def _path(packs_root: str | Path, pid: str) -> Path:
    if not _PROPOSAL_RE.fullmatch(pid):
        raise ProposalError(f"非法提案 id: {pid}")
    return proposals_dir(packs_root) / f"{pid}.json"


def get_proposal(packs_root: str | Path, pid: str) -> dict:
    p = _path(packs_root, pid)
    if not p.is_file():
        raise ProposalStateError(f"提案不存在: {pid}")
    return json.loads(p.read_text(encoding="utf-8"))


def list_proposals(packs_root: str | Path, status: str | None = None) -> list[dict]:
    d = proposals_dir(packs_root)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("pp_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status is None or data.get("status") == status:
            out.append(data)
    return out


# ---------------- 校验/模拟（落地前 + 应用前都跑） ----------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ProposalError(msg)


def _find_skill(packs_root: Path, skill_kind: str, owner: str, name: str):
    reg = SkillRegistry(packs_root)
    reg.load()
    sk = reg.get(name)
    if sk is None:
        raise ProposalError(f"技能不存在: {name}")
    if sk.kind != skill_kind or sk.pack != owner:
        raise ProposalError(
            f"技能归属不符: {name} 是 {sk.kind}/{sk.pack}，"
            f"提案写的是 {skill_kind}/{owner}")
    return sk


def _validate_common(p: dict) -> None:
    t = p["target"]
    _require(isinstance(p.get("summary"), str) and p["summary"].strip(),
             "summary 必填且不能为空")
    _require(isinstance(p.get("reason"), str) and p["reason"].strip(),
             "reason 必填且不能为空（为什么改/凭什么）")
    _require(len(p["summary"]) <= 300, "summary 限 300 字")
    _require(t.get("kind") in {"kb", "skill", "index", "case"},
             "target.kind 仅 kb|skill|index|case")
    _require(p["mode"] in (KB_MODES | SKILL_MODES | INDEX_MODES | CASE_MODES),
             f"非法 mode: {p['mode']}")
    if p.get("origin", "agent") not in ORIGINS:
        raise ProposalError(f"非法 origin: {p.get('origin')}")


def _validate_target(packs_root: Path, p: dict, *, applying: bool) -> None:
    """模拟校验：路径合法、模式与磁盘现状一致、内容合法。非法直接抛错，绝不落地。

    ``applying=True`` 时按"应用这一刻"的磁盘现状复核（提案排队期间文件可能已变）。"""
    t = p["target"]
    kind, mode = t["kind"], p["mode"]
    content = p.get("content")

    if kind in {"kb", "case"}:
        if kind == "case":
            _require(mode in CASE_MODES,
                     "case 提案仅 edit（成功案例.md 补段）/create（payloads/ 补弹药），"
                     "rename/delete 归人类")
        cap = t.get("cap")
        path = t.get("path")
        _require(cap and path, f"{kind} 提案必须给 target.cap 与 target.path")
        target = writing.resolve_kb(packs_root, cap, path)
        exists = target.path.is_file()
        if mode in {"edit", "delete", "rename"}:
            _require(exists, f"kb 文件不存在，无法 {mode}: {target.module}")
        if mode == "edit":
            writing._validate_content(content or "")
        if mode == "create":
            _require(not exists,
                     f"kb 文件已存在，无法 create: {target.module}")
            writing._validate_content(content or "")
        if mode == "rename":
            new_path = t.get("new_path")
            _require(new_path and new_path != path,
                     "rename 必须给不同的 new_path")
            new_target = writing.resolve_kb(packs_root, cap, new_path)
            _require(new_target.source.root == target.source.root,
                     "改名仅限同一知识库源内")
            _require(not new_target.path.exists(),
                     f"目标已存在: {new_target.module}")
        if mode == "delete" and applying:
            pass  # 存在性上面已查
    elif kind == "index":
        cap = t.get("cap")
        _require(cap, "index 提案必须给 target.cap")
        _require((packs_root / "capabilities" / cap).is_dir(),
                 f"能力包不存在: {cap}")
        _require(mode in INDEX_MODES,
                 "index 提案仅 edit（增补全局索引中本域条目；M0 后全局单表恒存在，"
                 "create 已退役），删除归人类")
        _require((packs_root / "kb" / "route_index.yaml").is_file(),
                 "全局 route_index.yaml 不存在（packs/kb/ 树异常）")
        writing._validate_content(content or "")
        from core.skills import routeindex
        try:
            entries = routeindex.parse_entries(content or "")
        except Exception as e:  # noqa: BLE001
            raise ProposalError(f"索引内容解析失败: {e}") from e
        bad = [e.kb for e in entries if e.kb.split("/", 1)[0] != cap]
        _require(not bad, f"条目 kb 路径须带本域前缀 {cap}/: {'、'.join(bad[:3])}")
    else:
        _require(mode in SKILL_MODES,
                 "技能提案仅 edit（改正文）或 suggest（拆分建议，产文档由人执行），"
                 "新建/改名/删除归人类直接操作")
        sk = _find_skill(packs_root, t.get("skill_kind", "capability"),
                         t.get("owner", ""), t.get("name", ""))
        if mode == "edit":
            writing._validate_content(content or "")
            meta = parse_frontmatter(content)
            _require(meta.get("name") == sk.name,
                     f"frontmatter name 必须与技能目录名一致: {sk.name!r}")
        elif mode == "suggest":
            writing._validate_content(content or "")


# ---------------- 创建（只落 pending） ----------------

def create_proposal(packs_root: str | Path, proposal: dict,
                    *, origin: str = "agent") -> dict:
    """校验并落一条 pending 提案；非法抛 ProposalError（422），文件绝不创建。

    proposal 入参：target{kind,...}、mode、content?、summary、reason、
    evidence?（任务证据，可选）；上下文 project/session/task 可选。"""
    packs_root = Path(packs_root)
    p = {
        "id": "",
        "status": "pending",
        "origin": origin,
        "project": proposal.get("project"),
        "session": proposal.get("session"),
        "task": proposal.get("task"),
        "target": proposal.get("target") or {},
        "mode": proposal.get("mode"),
        "content": proposal.get("content"),
        "summary": (proposal.get("summary") or "").strip(),
        "reason": (proposal.get("reason") or "").strip(),
        "evidence": (proposal.get("evidence") or "").strip()[:2000],
        "revisions": [],
        "created_at": _now(),
        "decided_at": None,
        "decided_by": None,
        "decision_note": None,
    }
    _validate_common(p)
    _validate_target(packs_root, p, applying=False)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    pid = f"pp_{ts}_{secrets.token_hex(3)}"
    p["id"] = pid
    with writing.pack_write_lock():
        writing.json_dump_atomic(_path(packs_root, pid), p)
    return p


# ---------------- 审批视图（实时 diff + 引用面） ----------------

def _index_path(packs_root: Path, cap: str) -> Path:
    """M0：全局单表（cap 参数保留调用方兼容，域归并在 apply 侧区分）。"""
    return packs_root / "kb" / "route_index.yaml"


def _current_text(packs_root: Path, p: dict) -> str | None:
    t = p["target"]
    try:
        if t["kind"] in {"kb", "case"}:
            target = writing.resolve_kb(packs_root, t["cap"], t["path"])
            return target.path.read_text(encoding="utf-8") if target.path.is_file() else None
        if t["kind"] == "index":
            # M0：返回本域过滤视图（与提案 content 同视野，diff 不误报他域条目被删）
            path = _index_path(packs_root, t["cap"])
            if not path.is_file():
                return None
            try:
                from core.skills import routeindex
                dom_e = [e for e in routeindex.parse_entries(
                    path.read_text(encoding="utf-8"))
                    if e.kb.split("/", 1)[0] == t["cap"]]
                return routeindex.serialize_entries(dom_e)
            except Exception:  # noqa: BLE001 —— 解析失败回退全文
                return path.read_text(encoding="utf-8")
        sk = _find_skill(packs_root, t.get("skill_kind", "capability"),
                         t.get("owner", ""), t.get("name", ""))
        return sk.path.read_text(encoding="utf-8") if sk.path.is_file() else None
    except writing.KbError:
        return None


def live_diff(packs_root: str | Path, p: dict) -> dict:
    """对照审批这一刻磁盘现状的实时差异（不依赖任何存储快照）。"""
    packs_root = Path(packs_root)
    t = p["target"]
    mode = p["mode"]
    current = _current_text(packs_root, p)
    label = (f"{t.get('cap') + '/' if t['kind'] in {'kb', 'index', 'case'} else ''}"
             f"{t.get('path') or t.get('name') or 'route_index.yaml'}")
    if mode == "create":
        old_name, new_name = "(不存在)", label
        old_lines, new_lines = [], (p.get("content") or "").splitlines()
    elif mode == "delete":
        old_name, new_name = label, "(删除)"
        old_lines, new_lines = (current or "").splitlines(), []
    elif mode == "rename":
        old_name, new_name = label, t.get("new_path")
        old_lines = new_lines = []
    else:  # edit
        old_name, new_name = f"{label}（磁盘现状）", f"{label}（提案）"
        old_lines = (current or "").splitlines()
        new_lines = (p.get("content") or "").splitlines()
    diff = "\n".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=old_name, tofile=new_name, lineterm=""))
    refs = None
    if t["kind"] in {"kb", "case"} and mode in {"rename", "delete"}:
        from core.skills import refs
        refs = [h.to_dict() for h in refs.find_module_refs(packs_root, t["path"])]
    return {"exists_now": current is not None or (
                mode == "rename" and _rename_src_exists(packs_root, p)),
            "diff": diff, "refs": refs}


def _rename_src_exists(packs_root: Path, p: dict) -> bool:
    t = p["target"]
    try:
        return writing.resolve_kb(packs_root, t["cap"], t["path"]).path.is_file()
    except writing.KbError:
        return False


def proposal_detail(packs_root: str | Path, pid: str) -> dict:
    p = get_proposal(packs_root, pid)
    p = dict(p)
    p["live"] = live_diff(packs_root, p)
    return p


# ---------------- 状态转移 ----------------

def _save(packs_root: Path, p: dict) -> None:
    with writing.pack_write_lock():
        writing.json_dump_atomic(_path(packs_root, p["id"]), p)


def apply_proposal(packs_root: str | Path, pid: str, decided_by: str) -> dict:
    """应用 pending 提案。decided_by: 'human' 或显式 'demo-script(auto)'。

    全程在 pack_write_lock 临界区内：应用前再校验一次磁盘现状，
    落盘复用 writing 备份；kb rename 连带 refs 引用替换。"""
    packs_root = Path(packs_root)
    p = get_proposal(packs_root, pid)
    if p["status"] != "pending":
        raise ProposalStateError(f"提案已 {p['status']}，不可应用")
    _require(decided_by in {"human", "demo-script(auto)"},
             "应用提案只接受人类审批（demo 脚本必须显式署名 demo-script(auto)）")
    # 应用前复核（提案排队期间磁盘可能已变）
    _validate_target(packs_root, p, applying=True)
    t = p["target"]
    result: dict = {}
    with writing.pack_write_lock():
        if t["kind"] in {"kb", "case"}:  # case（K6）校验/应用与 kb 同源，模式仅 edit/create
            cap, module, mode = t["cap"], t["path"], p["mode"]
            if mode == "edit":
                result = writing.write_kb_file(
                    packs_root, cap, module, p["content"], create=False)
            elif mode == "create":
                result = writing.write_kb_file(
                    packs_root, cap, module, p["content"], create=True)
            elif mode == "delete":
                result = writing.delete_kb_file(packs_root, cap, module)
            elif mode == "rename":
                move = writing.rename_kb_move(
                    packs_root, cap, module, t["new_path"])
                cascade = refs_rewrite(packs_root, cap, module, t["new_path"])
                result = {**move, **cascade}
        elif t["kind"] == "index":
            # M0 全局单表：提案 content=本域条目，与其他域条目归并后全文写回
            # （Agent 只看得见本域视野，直接覆盖会丢其他域条目）
            from core.skills import routeindex
            path = _index_path(packs_root, t["cap"])
            cap = t["cap"]
            try:
                keep = [e for e in routeindex.parse_entries(
                    path.read_text(encoding="utf-8"))
                    if e.kb.split("/", 1)[0] != cap]
                merged = routeindex.serialize_entries(
                    keep + routeindex.parse_entries(p["content"]))
            except Exception as e:  # noqa: BLE001 —— 归并失败不写盘
                raise ProposalError(f"索引归并失败: {e}") from e
            backup = writing.backup_and_write(path, merged)
            result = {"path": str(path),
                      "backup": backup.name if backup else None}
        else:
            sk = _find_skill(packs_root, t.get("skill_kind", "capability"),
                             t.get("owner", ""), t.get("name", ""))
            if p["mode"] == "suggest":
                # K4 拆分建议：产建议文档落技能目录（注册表只扫 SKILL.md，不干扰路由），
                # 由人按文档执行实际拆分
                sug = sk.path.parent / "拆分建议.md"
                backup = writing.backup_and_write(sug, p["content"])
                result = {"path": str(sug),
                          "backup": backup.name if backup else None}
            else:
                backup = writing.backup_and_write(sk.path, p["content"])
                result = {"path": str(sk.path),
                          "backup": backup.name if backup else None}
        p["status"] = "approved"
        p["decided_at"] = _now()
        p["decided_by"] = decided_by
        writing.json_dump_atomic(_path(packs_root, pid), p)
    return {"id": pid, "result": result}


def refs_rewrite(packs_root: Path, cap: str, old: str, new: str) -> dict:
    """包一层方便 apply/测试复用（实际逻辑在 refs.rewrite_module）。"""
    from core.skills import refs
    return refs.rewrite_module(packs_root, cap, old, new)


def reject_proposal(packs_root: str | Path, pid: str, decided_by: str,
                    note: str = "") -> dict:
    p = get_proposal(packs_root, pid)
    if p["status"] != "pending":
        raise ProposalStateError(f"提案已 {p['status']}，不可拒绝")
    p["status"] = "rejected"
    p["decided_at"] = _now()
    p["decided_by"] = decided_by or "human"
    p["decision_note"] = (note or "").strip()[:1000] or None
    _save(Path(packs_root), p)
    return {"id": pid, "status": "rejected"}


def revise_proposal(packs_root: str | Path, pid: str, by: str,
                    changes: dict, note: str) -> dict:
    """改后采纳流程的"改"：人类/Agent 修订 pending 提案（内容/新路径/摘要/理由），
    重新校验，仍为 pending，并留修订轨迹。"""
    packs_root = Path(packs_root)
    p = get_proposal(packs_root, pid)
    if p["status"] != "pending":
        raise ProposalStateError(f"提案已 {p['status']}，不可修订")
    for key in ("content", "summary", "reason"):
        if changes.get(key) is not None:
            p[key] = changes[key]
    if changes.get("new_path") is not None and p["target"]["kind"] == "kb":
        p["target"]["new_path"] = changes["new_path"]
    p["summary"] = (p.get("summary") or "").strip()
    p["reason"] = (p.get("reason") or "").strip()
    _validate_common(p)
    _validate_target(packs_root, p, applying=False)
    p["revisions"].append({"at": _now(), "by": by or "human",
                           "note": (note or "").strip()[:1000]})
    _save(packs_root, p)
    return p
