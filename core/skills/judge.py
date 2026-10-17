# -*- coding: utf-8 -*-
"""F11/C6 共享判定面：把「LLM 对照规则文本核对候选发现」的调用逻辑从
AgentSession._build_vuln_gate 闭包中提出，供三处复用：

- core/agent/loop.py 漏洞核对 hook（登记 vuln 前自我核对）；
- core/chat/runtime.py 工作台对话子专家的同款 hook（2026-10-16 接线）；
- scripts/cleanup_findings.py 存量清洗（复用同一规则注入口径逐条重评）。

放 skills 包（依赖 build_rules_preamble 同域、import 轻），不放 core/agent
（会拖起 loop/tools/runtime 全链 import）。异常一律返回 None，调用方决定
None=跳过放行（与 F11 降级哲学一致）。"""
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def build_vuln_gate(llm: Any, *, packs_root: Any, track: str,
                    capabilities: list[str] | None = None,
                    owner_tags: list[str] | None = None,
                    rule_profiles: dict | None = None):
    """C6 漏洞核对 hook 工厂（2026-10-16 从 AgentSession._build_vuln_gate 提出，
    供任务链与工作台对话链共用同一口径）。

    返回 gate(draft) -> (compliant, new_severity|None, note)：
    - compliant=False → 调用方（tools._tool_bb_add_finding）把 category 降 intel；
    - compliant=True 且 new_severity 非空 → 定级校准降一档（下限 low）；
    - LLM 失败/无 JSON → 降级跳过（返回 True/None），绝不阻断登记。

    `rule_profiles` 决定注入的评级口径——**未勾选 rating 的项目规则串里没有判级
    条款**，校准自然退化为只按红线判，故接线方（chat）可据此只在勾选时挂本 hook。"""
    def gate(draft: dict) -> tuple[bool, str | None, str]:
        # 延迟 import：避免 skills 包顶层拖起 blackboard/rules 依赖环。
        from core.blackboard.store import downgrade_severity
        from core.skills.rules import build_rules_preamble
        sys_p = (
            "你是漏洞合规审核员。候选发现要登记为「漏洞」，请对照以下红线与"
            "评级规则做两件核对：\n"
            "① 合规：是否有可验证的安全问题、方向未被禁止、证据链是否成立；"
            "证据不足/方向被禁/属于信息提示或合规提示而非漏洞 → compliant=false。\n"
            "② 定级校准（反向复验，宁低勿高）：用证据倒推——PoC/repro_steps "
            "实际证实的危害只够低档就按低档；所报 severity 高于证据能支撑的档位"
            " → severity_ok=false，并给出证据能支撑的 suggested_severity。\n"
            "只输出 JSON："
            '{"compliant": true|false, "severity_ok": true|false, '
            '"suggested_severity": "low|medium|high|critical", "reason": "一句话依据"}')
        v = judge_finding(llm, rules_text=build_rules_preamble(
            packs_root, track=track, capabilities=capabilities,
            owner_tags=owner_tags or [], rule_profiles=rule_profiles),
            draft=draft, sys_prompt=sys_p)
        if v is None:
            return True, None, "核对跳过：审核响应无 JSON"
        reason = str(v.get("reason") or "")
        if not bool(v.get("compliant")):
            return False, None, reason or "未通过漏洞标准核对"
        if v.get("severity_ok") is False:
            cur = str(draft.get("severity") or "").strip().lower()
            lower = downgrade_severity(cur)
            if lower is None:
                return False, None, reason or f"证据不足以支撑最低档（{cur}）"
            note = f"定级校准 {cur}→{lower}" + (f"（{reason}）" if reason else "")
            return True, lower, note
        return True, None, reason or "severity 与证据相符"
    return gate


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _extract_json(text: str) -> dict | None:
    """从模型输出里抽第一个**能解析**的 JSON 对象（2026-10-17 加固）。

    旧实现 `re.search(r"\\{.*\\}", text, re.S)` 是贪婪匹配：遇到 ```json 围栏、
    或回答里带两段花括号，整段喂 `json.loads` 必失败 → 上层当成「审核响应无 JSON」
    静默跳过（实测某项目 44 次核对里 30 次这么空转）。现在按围栏 → 全文 →
    平衡括号扫描依次尝试，任一解析成功即用。"""
    if not text:
        return None
    cands: list[str] = [m.group(1) for m in _FENCE_RE.finditer(text)]
    cands.append(text)
    # 平衡括号扫描：按出现顺序收集全部成对片段（模型常在 JSON 前带解释/示例）
    depth, start = 0, -1
    for i, ch in enumerate(text[:4000]):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                cands.append(text[start:i + 1])
                if len(cands) > 12:
                    break
    for cand in cands:
        try:
            v = json.loads(cand.strip())
        except Exception:  # noqa: BLE001 —— 换下一个候选
            continue
        if isinstance(v, dict):
            return v
    return None


def judge_finding(llm: Any, *, rules_text: str, draft: dict,
                  sys_prompt: str, max_tokens: int = 2048,
                  temperature: float = 0.1) -> dict | None:
    """用 llm 按 sys_prompt（内嵌 rules_text）核对 draft，返回解析出的 JSON
    dict；任何失败（无 JSON / 解析失败 / LLM 异常）返回 None。

    max_tokens 默认 2048：executor 档是推理模型（thinking 占输出预算），
    512 会被思考耗尽→text 为空→正则抽不到 JSON→静默降级 None
    （曾致 C6 门禁/清洗重评全部「跳过」，2026-09-18 实测复现）。
    2026-10-17 补：text 为空时**加倍预算重试一次**（同一失效模式的兜底），
    仍失败才降级；失败原因落 warning，别让「跳过」无声无息。"""
    try:
        sys_p = sys_prompt + "\n\n## 红线与评级规则\n" + rules_text
        msgs = [{"role": "user",
                 "content": "候选发现：\n" + json.dumps(draft, ensure_ascii=False)}]
        resp = llm.chat(msgs, system=sys_p, max_tokens=max_tokens,
                        temperature=temperature)
        text = str(getattr(resp, "text", "") or "")
        v = _extract_json(text)
        if v is None and not text.strip():
            resp = llm.chat(msgs, system=sys_p,
                            max_tokens=max(max_tokens, 2048) * 2,
                            temperature=temperature)
            text = str(getattr(resp, "text", "") or "")
            v = _extract_json(text)
        if v is None:
            log.warning("judge_finding 未取到 JSON（text %d 字），核对降级跳过", len(text))
            return None
        return v
    except Exception as e:  # noqa: BLE001 —— 判定失败降级，调用方决定放行/跳过
        log.warning("judge_finding 判定失败（降级 None）: %s", e)
        return None
