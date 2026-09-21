# -*- coding: utf-8 -*-
"""F11/C6 共享判定面：把「LLM 对照规则文本核对候选发现」的调用逻辑从
AgentSession._build_vuln_gate 闭包中提出，供两处复用：

- core/agent/loop.py 漏洞核对 hook（登记 vuln 前自我核对）；
- scripts/cleanup_findings.py 存量清洗（复用同一规则注入口径逐条重评）。

放 skills 包（依赖 build_rules_preamble 同域、import 轻），不放 core/agent
（会拖起 loop/tools/runtime 全链 import）。异常一律返回 None，调用方决定
None=跳过放行（与 F11 降级哲学一致）。"""
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def judge_finding(llm: Any, *, rules_text: str, draft: dict,
                  sys_prompt: str, max_tokens: int = 2048,
                  temperature: float = 0.1) -> dict | None:
    """用 llm 按 sys_prompt（内嵌 rules_text）核对 draft，返回解析出的 JSON
    dict；任何失败（无 JSON / 解析失败 / LLM 异常）返回 None。

    max_tokens 默认 2048：executor 档是推理模型（thinking 占输出预算），
    512 会被思考耗尽→text 为空→正则抽不到 JSON→静默降级 None
    （曾致 C6 门禁/清洗重评全部「跳过」，2026-09-18 实测复现）。"""
    try:
        sys_p = sys_prompt + "\n\n## 红线与评级规则\n" + rules_text
        resp = llm.chat(
            [{"role": "user",
              "content": "候选发现：\n" + json.dumps(draft, ensure_ascii=False)}],
            system=sys_p, max_tokens=max_tokens, temperature=temperature)
        m = re.search(r"\{.*\}", resp.text, re.S)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001 —— 判定失败降级，调用方决定放行/跳过
        log.warning("judge_finding 判定失败（降级 None）: %s", e)
        return None
