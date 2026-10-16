"""知识库迭代（kb-iteration，2026-10-14）：可选的**轮末自动提案**飞轮。

开启后（项目级 ``config["kb_iteration"] = {"enabled": true}``，默认关），智能体主控
每轮**正常收尾**时按「规则信号」判定本轮是否值得沉淀：

- 知识库/技能文档内容有误（本轮引用过文档却仍反复失败）；
- 任务较难、试错较多（工具连续失败 / 步数很高）；
- 产出新的 Web 漏洞发现。

命中才调**一次**轻量 LLM 生成变更提案，逐条走 ``proposals.create_proposal(origin=agent)``
落 pending——人类在「变更提案」页审批。提案范围含 kb 文档（新建/改写/删除）与技能
（新建/改写/删除/拆分建议），**全部人审**。

纪律（红线）：

- **不打断会话**：全部 best-effort，任何异常只吞不抛；只落 pending，绝不改会话正文、
  绝不直接写 packs 文件；
- **零 fastapi 依赖**（ChatTurn / API / 脚本共用）；
- 开关**实时重读**（每轮从黑板 projects 行读 config，不缓存）。

审计事件（proposal.created）由本模块经黑板 append_event 发出（与 Agent 工具同口径）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from core.skills import proposals, writing
from core.skills.proposals import ProposalError

log = logging.getLogger(__name__)

# ---- 项目级开关（config["kb_iteration"]，与 autonomy 同款整段归一化） ----


def normalize_kb_iteration(raw: Any) -> dict:
    """归一化 config["kb_iteration"]；非法抛 ValueError（API→422）。缺省 {enabled: False}。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("kb_iteration 必须是对象")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("kb_iteration.enabled 须为布尔值")
    return {"enabled": enabled}


def kb_iteration_of(config: dict | None) -> dict:
    """从项目 config 读 kb_iteration 并归一化（无段/旧库 → 默认关）。"""
    return normalize_kb_iteration((config or {}).get("kb_iteration"))


def is_enabled(config: dict | None) -> bool:
    """读路径专用：非法/缺省一律视为关（不抛）。"""
    try:
        return bool(kb_iteration_of(config)["enabled"])
    except ValueError:
        return False


# ---- 规则信号（零 LLM 的触发判据；命中才值得调 LLM） ----

# 本轮事件窗里计入「失败」的事件类型（diagnostics failog 落库口径）
_FAIL_KINDS = ("tool.failed", "mcp.failed")
# 本轮事件窗里计入「引用文档」的事件类型（有引用才可能发现文档有误）
_KB_REF_KINDS = ("skill.open", "skill.routed", "kb.open", "kb.search")
# 「执行动作」事件（步数代理）
_ACTION_KINDS = ("command", "command.result")

# 触发阈值（试错多/任务难的经验值，偏保守防误触发）
_MIN_REPEAT_FAILURES = 2   # 同一工具/命令重复失败次数
_MIN_TOTAL_FAILURES = 3    # 本轮失败总数
_MIN_ACTIONS_HARD = 25     # 步数（执行动作数）——任务较难的代理

# Web 漏洞类别线索（新 Web 漏洞触发沉淀）：命中发现标题/类别的子串
_WEB_VULN_HINT = re.compile(
    r"sqli|sql\s*注入|注入|xss|csrf|ssrf|rce|命令执行|代码执行|文件上传|任意文件|"
    r"越权|idor|未授权|反序列化|ssti|xxe|目录遍历|路径穿越|jwt|oauth|逻辑漏洞|支付|"
    r"文件包含|弱口令|信息泄露|cve-|web",
    re.I,
)


def signals_of(bb, project_id: str, *, since_event_id: int = 0,
               since_iso: str | None = None) -> dict:
    """汇总本轮信号（纯读，异常向外抛——由调用方 best-effort 包住）。

    **按轮次窗口汇总，不按会话过滤**：主控一轮会 spawn 子专家，子专家的事件/发现
    挂在子线程名下（session_id/author = chat-<子tid>），若按主控 session 过滤会漏掉
    子专家的失败与产出——故只按 ``since_event_id``（事件）与 ``since_iso``（发现时间）
    圈定本轮窗口。同项目并发线程的少量串入可接受（只影响是否触发的近似）。
    """
    events = bb.recent_events(project_id, since_id=since_event_id, limit=500)
    fails: dict[str, int] = {}
    total_fail = 0
    kb_refs = 0
    actions = 0
    for e in events:
        kind = e.get("kind")
        if kind in _FAIL_KINDS:
            total_fail += 1
            name = str((e.get("payload") or {}).get("name")
                       or (e.get("payload") or {}).get("tool") or "?")
            fails[name] = fails.get(name, 0) + 1
        elif kind in _KB_REF_KINDS:
            kb_refs += 1
        elif kind in _ACTION_KINDS:
            actions += 1
    repeat_fail = max(fails.values()) if fails else 0

    new_findings = 0
    new_web_vulns = 0
    try:
        rows = bb.list_findings(project_id)
    except Exception:  # noqa: BLE001 —— 读发现失败不阻断（信号退化）
        rows = []
    for f in rows:
        if since_iso and str(f.get("created_at") or "") < since_iso:
            continue
        new_findings += 1
        blob = f"{f.get('title', '')} {f.get('vuln_class', '')}"
        if _WEB_VULN_HINT.search(blob):
            new_web_vulns += 1
    return {
        "events": len(events),
        "tool_failures": total_fail,
        "repeat_failures": repeat_fail,
        "kb_refs": kb_refs,
        "actions": actions,
        "new_findings": new_findings,
        "new_web_vulns": new_web_vulns,
    }


def should_trigger(sig: dict) -> tuple[bool, str]:
    """规则判据：命中任一即值得沉淀。返回 (bool, 触发原因)。"""
    if sig.get("repeat_failures", 0) >= _MIN_REPEAT_FAILURES:
        return True, f"同一工具重复失败 {sig['repeat_failures']} 次（试错多）"
    if sig.get("new_web_vulns", 0) > 0:
        return True, f"本轮产出 {sig['new_web_vulns']} 条新 Web 漏洞发现"
    if sig.get("tool_failures", 0) >= _MIN_TOTAL_FAILURES:
        return True, f"本轮工具失败 {sig['tool_failures']} 次（任务较难）"
    if sig.get("kb_refs", 0) > 0 and sig.get("tool_failures", 0) >= 2:
        return True, "引用过知识库文档却仍反复失败（文档可能有误/缺失）"
    if sig.get("actions", 0) >= _MIN_ACTIONS_HARD:
        return True, f"本轮执行动作 {sig['actions']} 步（任务较难）"
    return False, ""


# ---- LLM 复盘 → 提案（best-effort） ----

_ITER_SYSTEM = (
    "你是知识库/技能迭代编辑。基于一次智能体会话轮次的证据（引用过的文档、工具失败、"
    "产出的发现），提出对知识库/技能文档的**沉淀或纠错提案**。严格规则：\n"
    "(1) 仅在四种情形提：①知识库/技能文档内容有误（与实际不符）；②文档缺失（有值得"
    "沉淀的新方法/坑）；③某手法已被本轮验证有效；④产出新的 Web 漏洞（补漏洞手册/技巧）。\n"
    "(2) 路径/技能名只能从给出的清单里选，禁止猜；新经验一律 create 新文件/新技能，"
    "禁止覆盖/翻译英文原文快照。\n"
    "(3) 允许 kind=kb（mode=edit/create/delete）与 kind=skill（mode=edit/create/delete）。"
    "删除要谨慎，仅在文档明显错误且无法改写时提 delete。\n"
    "(4) 每条必须在 reason 附本轮证据（失败的工具/引用的文档/产出的发现 id）。\n"
    "(5) 无值得沉淀的内容就返回空列表。\n"
    '只输出 JSON：{"proposals":[{"kind":"kb|skill","mode":"edit|create|delete",'
    '"target":{...},"content":"...","summary":"≤300字","reason":"证据"}]}\n'
    "target 形态：kb={cap:域, path:<域>/<路径>.md}；skill={name:技能名}"
    "（skill 的 edit/create 须给 SKILL.md 全文，frontmatter name 必须等于技能名）。"
)


def _parse_json(text: str) -> dict | None:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t).strip()
        t = re.sub(r"\n?```$", "", t).strip()
    m = re.search(r"\{.*\}", t, re.S)
    try:
        return json.loads(m.group(0) if m else t)
    except (json.JSONDecodeError, ValueError):
        return None


def run_iteration(bb, project_id: str, *, packs_root: str | Path, llm,
                  session_id: str, author: str, thread_id: str,
                  capabilities: list[str] | None = None,
                  since_event_id: int = 0, since_iso: str | None = None,
                  signals: dict | None = None) -> dict:
    """执行一次迭代：算信号 → 命中则调 LLM 生成提案落 pending。**绝不抛**。

    返回 {triggered, reason, signals, landed, rejected}（供测试/日志）。
    """
    try:
        sig = signals if signals is not None else signals_of(
            bb, project_id, since_event_id=since_event_id, since_iso=since_iso)
        hit, reason = should_trigger(sig)
        if not hit:
            return {"triggered": False, "reason": "", "signals": sig,
                    "landed": [], "rejected": []}
        if llm is None:
            return {"triggered": True, "reason": reason, "signals": sig,
                    "landed": [], "rejected": [], "error": "无 LLM 句柄"}
        root = Path(packs_root)
        # 文件清单（防猜路径）：能力域 kb 文件 + 现有技能名
        file_list: dict[str, list[str]] = {}
        for cap in (capabilities or []):
            try:
                file_list[cap] = [f["path"]
                                  for src in writing.list_kb(root, cap)
                                  for f in src["files"]]
            except writing.KbError:
                continue
        from core.skills.registry import SkillRegistry
        reg = SkillRegistry(root)
        reg.load()
        skills = [s.name for s in reg.all()]
        user_msg = json.dumps(
            {"trigger": reason, "signals": sig,
             "kb_files": file_list, "skills": skills},
            ensure_ascii=False)
        resp = llm.chat([{"role": "user", "content": user_msg}], system=_ITER_SYSTEM)
        data = _parse_json(getattr(resp, "text", "") or "")
        if data is None:
            return {"triggered": True, "reason": reason, "signals": sig,
                    "landed": [], "rejected": [],
                    "error": "LLM 输出不是合法 JSON"}
        landed, rejected = [], []
        for item in (data.get("proposals") or [])[:5]:
            target = item.get("target") if isinstance(item, dict) else None
            if not isinstance(target, dict):
                rejected.append({"summary": str(item)[:120], "error": "target 不是对象"})
                continue
            payload = {
                "target": {"kind": item.get("kind"), **target},
                "mode": item.get("mode"),
                "content": item.get("content"),
                "summary": item.get("summary", ""),
                "reason": item.get("reason", ""),
                "project": project_id,
                "session": session_id,
                "evidence": f"知识库迭代（{reason}）；信号 {sig}",
            }
            try:
                p = proposals.create_proposal(root, payload, origin="agent")
            except (ProposalError, writing.KbError) as e:
                rejected.append({"summary": str(item.get("summary"))[:120],
                                 "error": str(e)})
                continue
            try:
                bb.append_event(project_id, "proposal.created",
                                {"id": p["id"], "kind": p["target"].get("kind"),
                                 "mode": p["mode"], "target": p["target"],
                                 "summary": p["summary"], "origin": "agent"},
                                session_id=session_id, author=author)
            except Exception:  # noqa: BLE001 —— 事件失败不影响提案已落库
                log.exception("kb-iteration proposal.created 事件失败")
            landed.append({"id": p["id"], "summary": p["summary"]})
        return {"triggered": True, "reason": reason, "signals": sig,
                "landed": landed, "rejected": rejected}
    except Exception:  # noqa: BLE001 —— 迭代是旁路，绝不炸会话轮
        log.exception("kb-iteration 执行失败 project=%s thread=%s", project_id, thread_id)
        return {"triggered": False, "reason": "", "signals": signals or {},
                "landed": [], "rejected": [], "error": "内部异常（已忽略）"}
