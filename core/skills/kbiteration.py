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
# kb.search 零命中次数阈值（2026-10-19 命中质量信号）：1 次即可——
# 「问了但库里没有」本身就是知识缺口，不需要累积；累积阈值只适用于
# 「失败」这类可能是偶发（权限/网络）的信号。
_MIN_KB_ZERO_HITS = 1

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
    # 命中质量信号（2026-10-19）：迭代不该只看「失败多」，还要看「检索/路由没打中」。
    # kb.search 的 payload 带 hits，hits==0 即「问了但知识库没有」——这是补文档的
    # 最直接证据，比「工具失败」精确得多（失败可能是权限/网络，与知识缺口无关）。
    kb_search_zero = 0        # kb.search 零命中次数
    kb_search_total = 0
    skill_opens = 0           # 打开技能次数（用于对照「打开了但没解决问题」）
    for e in events:
        kind = e.get("kind")
        payload = e.get("payload") or {}
        if kind in _FAIL_KINDS:
            total_fail += 1
            name = str(payload.get("name") or payload.get("tool") or "?")
            fails[name] = fails.get(name, 0) + 1
        elif kind in _KB_REF_KINDS:
            kb_refs += 1
            if kind == "kb.search":
                kb_search_total += 1
                try:
                    if int(payload.get("hits") or 0) == 0:
                        kb_search_zero += 1
                except (TypeError, ValueError):
                    pass
            elif kind == "skill.open":
                skill_opens += 1
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
        "kb_search_total": kb_search_total,
        "kb_search_zero": kb_search_zero,
        "skill_opens": skill_opens,
        "actions": actions,
        "new_findings": new_findings,
        "new_web_vulns": new_web_vulns,
    }


def should_trigger(sig: dict) -> tuple[bool, str]:
    """规则判据：命中任一即值得沉淀。返回 (bool, 触发原因)。"""
    for rule in TRIGGER_RULES:
        if rule["hit"](sig):
            return True, rule["reason"](sig)
    return False, ""


# ---- 规则表（判定与展示同源，2026-10-20） ----
#
# 前端「变更提案」页要告诉人类「什么情况下 Agent 才会提提案」。此前这些判据只
# 存在于 should_trigger 的 if 链里，页面上一个字也看不到，人类只能靠猜。
#
# 这里把每条规则提成数据：``hit`` 判命中、``reason`` 出人话、``detail`` 给页面
# 展示阈值。**should_trigger 直接遍历本表**——展示与判定是同一份数据，改阈值
# 不可能出现「页面写着 2 次、实际按 3 次判」的分叉（历史上提示文案与判定逻辑
# 分头维护，已经分叉过）。
#
# 顺序敏感（首个命中即返回）：先报「同一工具反复失败」这种最具体的信号，再报
# 汇总型（失败总数/步数），人类看到的原因才最贴证据。

TRIGGER_RULES: tuple[dict[str, Any], ...] = (
    {
        "key": "repeat_failures",
        "label": "同一工具反复失败",
        "detail": f"同一工具/命令失败 ≥ {_MIN_REPEAT_FAILURES} 次（试错多，说明打法或文档有问题）",
        "hit": lambda s: s.get("repeat_failures", 0) >= _MIN_REPEAT_FAILURES,
        "reason": lambda s: f"同一工具重复失败 {s['repeat_failures']} 次（试错多）",
    },
    {
        "key": "new_web_vulns",
        "label": "产出新的 Web 漏洞",
        "detail": "本轮新增 Web 类漏洞发现 ≥ 1 条（补漏洞手册/技巧）",
        "hit": lambda s: s.get("new_web_vulns", 0) > 0,
        "reason": lambda s: f"本轮产出 {s['new_web_vulns']} 条新 Web 漏洞发现",
    },
    {
        "key": "tool_failures",
        "label": "工具失败偏多",
        "detail": f"本轮工具失败 ≥ {_MIN_TOTAL_FAILURES} 次（任务较难）",
        "hit": lambda s: s.get("tool_failures", 0) >= _MIN_TOTAL_FAILURES,
        "reason": lambda s: f"本轮工具失败 {s['tool_failures']} 次（任务较难）",
    },
    {
        "key": "kb_refs_with_failures",
        "label": "查了文档仍失败",
        "detail": "引用过知识库/技能文档，且本轮工具失败 ≥ 2 次（文档可能有误或缺失）",
        "hit": lambda s: s.get("kb_refs", 0) > 0 and s.get("tool_failures", 0) >= 2,
        "reason": lambda s: "引用过知识库文档却仍反复失败（文档可能有误/缺失）",
    },
    {
        "key": "kb_search_zero",
        "label": "检索零命中",
        "detail": (f"知识库检索零命中 ≥ {_MIN_KB_ZERO_HITS} 次"
                   "（问了但库里没有 = 知识缺口，最直接的补文档证据）"),
        "hit": lambda s: s.get("kb_search_zero", 0) >= _MIN_KB_ZERO_HITS,
        "reason": lambda s: (f"知识库检索 {s['kb_search_zero']} 次零命中"
                             f"（共 {s.get('kb_search_total', 0)} 次）——存在知识缺口"),
    },
    {
        "key": "actions",
        "label": "执行步数偏高",
        "detail": f"本轮执行动作 ≥ {_MIN_ACTIONS_HARD} 步（任务较难）",
        "hit": lambda s: s.get("actions", 0) >= _MIN_ACTIONS_HARD,
        "reason": lambda s: f"本轮执行动作 {s['actions']} 步（任务较难）",
    },
)


def trigger_rules() -> list[dict[str, str]]:
    """规则表的人话描述（API 直出给「变更提案」页；不含 lambda，可 JSON 序列化）。"""
    return [{"key": r["key"], "label": r["label"], "detail": r["detail"]}
            for r in TRIGGER_RULES]


def rules_doc() -> dict:
    """触发规则的完整说明（页面展示用）：开关语义 + 两条触发通道 + 各条阈值。

    ``enabled`` 由调用方按项目 config 传入；其余为静态口径。**本函数不读磁盘**，
    保证前端一次请求即可渲染（不因读不到项目而空白）。
    """
    return {
        "switch": {
            "key": "config.kb_iteration.enabled",
            "label": "知识库迭代",
            "scope": "项目级（每个项目独立开关，默认关闭）",
            "off_meaning": "关闭时 Agent 不生成任何提案：不下发 propose_pack_edit 工具、"
                           "系统提示不注入沉淀纪律、轮末不做规则判定",
        },
        "channels": [
            {
                "key": "round_end",
                "label": "轮末自动判定",
                "detail": "主控每轮正常收尾时，按下方规则信号判定本轮是否值得沉淀；"
                          "命中才调一次轻量 LLM 生成提案。只看主控线程（其轮次涵盖"
                          "本轮全部子专家工作），后台执行不阻塞会话。",
            },
            {
                "key": "agent_tool",
                "label": "Agent 主动沉淀",
                "detail": "主控与子专家可主动调用 propose_pack_edit 提提案，需满足四种"
                          "情形之一：文档内容有误 / 文档缺失 / 手法已本轮验证有效 / "
                          "产出新 Web 漏洞。同样受项目开关门禁。",
            },
            {
                "key": "session_review",
                "label": "会话复盘（手动）",
                "detail": "在直播间对某个会话发起「复盘沉淀」，由 planner LLM 基于该"
                          "会话证据生成提案。此通道不受项目开关限制，是人工主动"
                          "触发的补沉淀手段。",
            },
        ],
        "rules": trigger_rules(),
        "limits": {
            "per_session_proposals": 3,
            "note": "每会话最多 3 条 pending 提案，防凑数；LLM 单次最多产出 5 条。",
        },
        "landing": "所有提案一律落 pending，全部人工审批；批准前不改任何文件。",
    }


# ---- LLM 复盘 → 提案（best-effort） ----

_ITER_SYSTEM = (
    "你是知识库/技能迭代编辑。基于一次智能体会话轮次的证据（引用过的文档、工具失败、"
    "产出的发现），提出对知识库/技能文档的**沉淀或纠错提案**。严格规则：\n"
    "(1) 仅在四种情形提：①知识库/技能文档内容有误（与实际不符）；②文档缺失（有值得"
    "沉淀的新方法/坑）；③某手法已被本轮验证有效；④产出新的 Web 漏洞（补漏洞手册/技巧）。\n"
    "(2) 路径/技能名只能从给出的清单里选，禁止猜。**优先 mode=edit 把新经验补进已有的"
    "对应手册**；仅当确实没有对应手册时才 create 新文件。禁止覆盖/翻译英文原文快照。\n"
    "(3) 允许 kind=kb（mode=edit/create/delete）与 kind=skill（mode=edit/create/delete）。"
    "删除要谨慎，仅在文档明显错误且无法改写时提 delete。\n"
    "(4) 每条必须在 reason 附本轮证据（失败的工具/引用的文档/产出的发现 id）。\n"
    "(5) 无值得沉淀的内容就返回空列表。\n"
    "\n【最重要的红线：禁止写解题流水账】\n"
    "写进知识库的必须是**可复用的手法/判据/坑**，不是这次解题的过程记录。\n"
    "- 禁止：按步骤复述本次解题（「第一步…第二步…」「先做 A 再做 B 最后拿到 flag」）、"
    "赛题名+解题过程、具体靶机地址/凭据/flag 值、按时间顺序的行动日志。\n"
    "- 这类内容会让 AI 在同一题复测时直接检索到旧解法、跳过分析过程，"
    "把「解题能力」退化成「答案检索」，换个变种题就失效——是本系统明确要防的污染。\n"
    "- 允许：题目作为**手法例证**出现，且必须写成「什么特征 → 用什么手法 → 为什么」，"
    "例如「Solidity 0.8.15 calldata tuple head overflow（0CTF 2022 NFT Market）→ "
    "ABI 重编码时校验被绕过 → 判据：合约编译器版本落在受影响区间」。"
    "题目名只能作为该手法的典型场景标注，不能展开成过程。\n"
    "- 自检：写完问自己「换个同类型但不同题目的目标，这段话还有用吗？」"
    "答案为否 → 说明写的是流水账，删掉重写。\n"
    "\n【表达红线：硬核、精简、无废话】\n"
    "- 只写判据、手法、坑、边界条件。禁止「本文将介绍」「综上所述」「需要注意的是」"
    "这类填充语，禁止重复标题已表达的意思，禁止解释显而易见的概念。\n"
    "- 优先短句、表格、判据式表达（「若 X 则 Y」「A 命中说明 B」）。\n"
    "- 一段话能删掉而不损失信息，就删掉。宁可少写，不要注水。\n"
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
