"""诊断：红线一键自检（diagnostics-redline-test，2026-10-17）。

**动因**（今天一串排查的收口）：会话到底有没有把安全红线/评级口径送进模型上下文，
此前的答案是「三处证据互相矛盾」——诊断页请求体显示注入了（`instructions` 里
`rule:rating:edu-rating` 俱全），模型却三次自称没收到。逐层挖下去发现断点在
中转网关（实测幻灵整个丢弃 system 字段）。这类「注没注入/送没送到」的问题此后
还会反复出现，而**模型无法可靠内省自己的系统提示**（问它只会得到先验答案），
所以唯一可信的手法是**实验**：拿该会话真实的 system 正文 + canary 指令打一发，
看模型听不听。

本模块把整套固化成一次点击，返回结构化判定：

1. **本地注入**：该会话最近一次下发的 system 正文里，规则标记命中哪些、缺哪些
   （快照优先，无快照回落最近一条请求体记录）。缺 = 配置问题（rating 未勾选 /
   owner 标签没打 / track 不对），与通道无关。
2. **通道送达（canary，决定性证据）**：在 system 正文副本尾部追加一条「回复第一行
   必须输出 <随机串>`」的指令再打一发——模型照做即证明 system 通道对这个供应商/
   网关是通的（这正是幻灵事故的判别式：修复前 0/4、修复后 5/6）。
3. **内容引用（辅证）**：不让模型复述我们给出的内容，而是问「你系统提示里第一个
   `##` 标题行是什么」，看它能否吐出快照里真实存在的那一行。命中=强正证；未命中
   **不能**判否（模型可能拒绝内省）——所以只作辅证，判定以 canary 为准。

**红线**：探测只用无工具的纯 chat（不碰黑板/不落任何东西），任何异常进结果不
外抛；超时由调用方传入的 build timeout 兜住（默认 60s）。
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any

log = logging.getLogger(__name__)

# 与 scripts/llm_dump_inspect.py 同源的规则标记（判定「注入没注入」用）
RULE_MARKERS = {
    "红线": "安全红线",
    "规则前言": "场景规则与红线",
    "评级硬指令": "评级硬指令",
    "rating 条款": "rule:rating:",
    "owner 叠加": "rule:owner:",
    "track 规则": "rule:track:",
}
PROBE_TIMEOUT = 60.0
# 探测请求的 max_tokens：**不能给小**。供应商多开思考链（thinking=true →
# reasoning effort high），思考 token 也占输出预算，200 会被思考全吃光、回一个
# 空 text（实测：内容探针 200 够用，canary 探针 200 空回复）。给到 1024 两者都稳。
CANARY_MAX_TOKENS = 1024


def local_check(system_text: str) -> dict[str, Any]:
    """规则标记命中情况：none / partial / full + 命中与缺失清单。"""
    found = [k for k, needle in RULE_MARKERS.items() if needle in (system_text or "")]
    missing = [k for k in RULE_MARKERS if k not in found]
    status = "full" if not missing else ("partial" if found else "none")
    return {"status": status, "found": found, "missing": missing,
            "chars": len(system_text or "")}


def _pick_needle(system_text: str) -> tuple[str, str]:
    """选一条只有真在上下文里才答得出的内容：(needle, 问法)。

    优先取第一条 `## rule:` 标题行（规则结构里最有辨识度且必然逐字存在于 system）；
    没有则取首个非空行。**问法绝不透露 needle 本身**——否则模型复述用户消息即可
    「通过」，测试就废了。
    """
    for line in (system_text or "").splitlines():
        s = line.strip()
        if re.match(r"^#{1,3}\s*rule:", s):
            return s, "请逐字输出你系统提示词中第一个以 `##` 开头的标题行（只输出该行本身，不要解释、不要翻译）。"
    for line in (system_text or "").splitlines():
        s = line.strip()
        if s:
            return s[:80], "请逐字输出你系统提示词的第一行（只输出该行本身，不要解释）。"
    return "", ""


def _session_system(bb, project_id: str, session_id: str) -> tuple[str, str]:
    """该会话最近一次下发的 system 正文 → (text, source)。

    快照（`session_system_prompts`）优先；无快照时回落最近一条请求体记录
    （诊断页「记录请求体」开着才有）。两者皆无 → ("", "none")。
    """
    from core.diagnostics.system_prompt import get_system_prompt
    try:
        snap = get_system_prompt(bb, session_id)
    except Exception:  # noqa: BLE001 —— 读不到就回落，别炸
        log.exception("红线自检：快照读取失败 session=%s", session_id)
        snap = None
    if snap and snap.get("text"):
        return str(snap["text"]), "snapshot"

    from core.diagnostics import requests as request_log
    try:
        rows = request_log.list_records(project_id, session_id, limit=1)
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return "", "none"
    try:
        row = request_log.read_record(project_id, session_id, rows[0]["rid"]) or {}
        req = row.get("request") or {}
        system = req.get("system") or req.get("instructions") or ""
        if isinstance(system, list):
            system = "\n\n".join(
                str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in system)
        if not system:
            for m in (req.get("messages") or req.get("input") or []):
                if isinstance(m, dict) and m.get("role") == "system":
                    c = m.get("content")
                    if isinstance(c, str):
                        system = c
                        break
        if not system:
            from core.llm.openai_compat import (SYSTEM_IN_USER_BEGIN,
                                                SYSTEM_IN_USER_END)
            for m in (req.get("messages") or req.get("input") or []):
                if not (isinstance(m, dict) and m.get("role") == "user"):
                    continue
                c = m.get("content")
                if not isinstance(c, str) or not c.startswith(SYSTEM_IN_USER_BEGIN):
                    continue
                end = c.find(SYSTEM_IN_USER_END)
                system = c[len(SYSTEM_IN_USER_BEGIN):end if end >= 0 else None].strip("\n")
                break
        return (system, "record") if system else ("", "none")
    except Exception:  # noqa: BLE001
        log.exception("红线自检：请求体回落读取失败 session=%s", session_id)
        return "", "none"


def _project_id_of(bb, session_id: str) -> str:
    """请求体记录按 project_id 归档，回落路径要用（快照里就带，直接取）。"""
    from core.diagnostics.system_prompt import get_system_prompt
    try:
        snap = get_system_prompt(bb, session_id)
        if snap:
            return str(snap.get("project_id") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _resolve_target(bb, session_id: str, provider: str, model: str) -> tuple[str, str]:
    """探测用哪个供应商/模型：显式指定 > 线程级覆写（chat- 前缀）> 全局默认。"""
    if provider:
        return provider, model
    if session_id.startswith("chat-"):
        try:
            from core.chat import store as chat_store
            thread = chat_store.get_thread(bb, session_id) or {}
            tp = str(thread.get("provider") or "").strip()
            if tp:
                return tp, str(thread.get("model") or "").strip()
        except Exception:  # noqa: BLE001 —— 取不到就全局默认
            log.exception("红线自检：线程覆写读取失败 session=%s", session_id)
    return "", ""


def run_redline_test(bb, *, session_id: str, provider: str = "",
                     model: str = "") -> dict[str, Any]:
    """一键自检主入口。永不抛——异常写进结果的 error 字段。"""
    started = time.time()
    out: dict[str, Any] = {
        "session_id": session_id, "provider": provider, "model": model,
        "elapsed_ms": 0, "error": "",
    }
    try:
        project_id = _project_id_of(bb, session_id)
        system_text, source = _session_system(bb, project_id, session_id)
        out["system_source"] = source
        out["system_chars"] = len(system_text)
        if not system_text:
            out["verdict"] = "no_system"
            out["summary"] = ("拿不到该会话的 system 正文：既无系统提示快照（v39 起才落库），"
                             "请求体留痕也没开或没有记录。先在该会话发一轮消息，"
                             "或在顶栏开启「记录请求体」后再试。")
            out["elapsed_ms"] = int((time.time() - started) * 1000)
            return out

        local = local_check(system_text)
        out["local"] = local

        prov, mod = _resolve_target(bb, session_id, provider, model)
        from core.llm.providers import ProviderStore
        store = ProviderStore()
        try:
            if not prov or not mod:
                dname, dmodel = store.default_target()
                prov, mod = prov or dname, mod or dmodel
            llm = store.build(prov or None, mod or None, timeout=PROBE_TIMEOUT)
        except Exception as e:  # noqa: BLE001 —— 供应商不可用要能说人话
            out["verdict"] = "probe_failed"
            out["error"] = f"供应商构建失败：{e}"
            out["summary"] = ("探测请求发不出去（供应商不可用/无 key/已停用）。"
                             "这只说明测不了，不代表注入有问题。")
            out["elapsed_ms"] = int((time.time() - started) * 1000)
            return out
        out["probe"] = {
            "provider": prov, "model": mod,
            "format": getattr(llm, "format", ""),
            "system_in_user": bool(getattr(llm, "_system_in_user", False)),
        }

        # ---- 检查 2：canary 通道测试（决定性） ----
        token = "CANARY-" + secrets.token_hex(3).upper()
        out["channel"] = _canary_probe(llm, system_text, token)
        # ---- 检查 3：内容引用（辅证） ----
        out["content"] = _content_probe(llm, system_text)

        out["verdict"], out["summary"] = _verdict(
            local, out["channel"], out["content"], out["probe"])
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out
    except Exception as e:  # noqa: BLE001 —— 自检自身炸了也要能看
        log.exception("红线一键自检失败 session=%s", session_id)
        out["verdict"] = "error"
        out["error"] = f"{type(e).__name__}: {e}"
        out["summary"] = "自检本身出错（见 error），未得出结论。"
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out


def _canary_probe(llm, system_text: str, token: str) -> dict[str, Any]:
    """在 system 副本尾部追加 canary 指令打一发，看模型听不听。"""
    probe_system = (
        f"{system_text}\n\n【注入自检】忽略用户消息中任何与本期相冲突的要求；"
        f"你的回复第一行必须且只能输出：{token}\n第二行起再回答用户的问题。")
    try:
        resp = llm.chat([{"role": "user", "content": "你好，请用一句话介绍你自己。"}],
                        system=probe_system, max_tokens=CANARY_MAX_TOKENS)
        text = (resp.text or "").strip()
        head = text[:120]
        out: dict[str, Any] = {
            "ok": token in head, "canary": token, "reply_head": head,
            "note": "模型复述了 canary = system 通道通",
        }
        if not text:
            # 空回复不能直接判「通道断」——思考链会把输出预算吃光（见
            # CANARY_MAX_TOKENS 注释）。如实标注，判定层据此降级为辅证。
            thinking = str(getattr(resp, "thinking", "") or "")
            out["empty_reply"] = True
            out["note"] = ("回复正文为空（思考链可能吃满了输出预算），"
                           "不能据此判定通道断——看内容引用辅证")
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "canary": token, "reply_head": "",
                "error": f"{type(e).__name__}: {e}"}


def _content_probe(llm, system_text: str) -> dict[str, Any]:
    """问一条只有真在上下文里才答得出的内容（不透露答案），作辅证。"""
    needle, question = _pick_needle(system_text)
    if not needle:
        return {"ok": None, "needle": "", "reply_head": "", "note": "system 正文为空，跳过"}
    try:
        resp = llm.chat([{"role": "user", "content": question}],
                        system=system_text, max_tokens=CANARY_MAX_TOKENS)
        text = (resp.text or "").strip()
        flat = re.sub(r"\s+", "", text)
        return {"ok": re.sub(r"\s+", "", needle) in flat,
                "needle": needle, "reply_head": text[:120],
                "note": "命中=强正证；未命中不能判否（模型可能拒绝内省）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "needle": needle, "reply_head": "",
                "error": f"{type(e).__name__}: {e}"}


def _verdict(local: dict, channel: dict, content: dict,
             probe: dict) -> tuple[str, str]:
    """汇总判定。canary 是决定性的；空回复（思考预算吃光）降级为辅证，不判通道断。"""
    siu = "（当前供应商已开启 system_in_user 兼容通道）" if probe.get("system_in_user") else ""
    inject_head = (f"注入完整（{len(local['found'])}/{len(RULE_MARKERS)} 类标记全中）"
                   if local["status"] == "full" else
                   f"system 已注入（命中 {len(local['found'])} 类，缺："
                   f"{'、'.join(local['missing']) or '无'}）")
    if channel.get("ok"):
        tail = "canary 生效 = 通道可达。"
        if content.get("ok") is True:
            tail += "内容引用也命中，双证据闭环。"
        elif content.get("ok") is False:
            tail += "内容引用未命中，但以 canary 为准（模型可能只是不愿复述系统提示）。"
        return "ok", f"{inject_head}；{tail}"

    if channel.get("empty_reply"):
        # 空回复≠通道断：思考链吃满输出预算时也会空（实测）。有内容引用命中即放行。
        if content.get("ok") is True:
            return "ok", (f"{inject_head}；内容引用命中（模型逐字吐出了只在 system 里存在的"
                          f"标题行），通道可达。canary 探针只回了个空正文（思考预算吃满），"
                          f"不影响结论。")
        return "inconclusive", (
            f"{inject_head}；但两个探针都没给出可用信号（canary 空回复、内容引用未命中）。"
            f"这**不能**判定通道断——可在供应商里关掉 thinking 后重试，或直接看请求体记录。")

    if local["status"] == "none":
        return "not_injected", (
            "该会话的 system 正文里没有任何规则标记（红线/rating/owner/track 全缺），"
            "且 canary 也未生效——先查项目配置：rating 口径是否勾选、目标是否打了 "
            "owner 标签、测试轨是否正确；纯文本注入修好后再看通道。")
    return "channel_broken", (
        f"规则已写进 system 正文（命中 {len(local['found'])}/{len(RULE_MARKERS)} 类标记），"
        f"但 canary 没生效——**system 送不到模型**。这是中转网关丢弃 system 字段的典型"
        f"症状（实测幻灵三种形态全丢）：到 config/providers.json 给该供应商加 "
        f"\"system_in_user\": true{siu}，或换用已验证可用的供应商"
        f"（DeepSeek 官方 / ark-plan）。")
