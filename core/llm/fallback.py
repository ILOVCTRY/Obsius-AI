"""跨供应商兜底（provider-failover，2026-10-21）。

**动因**：传输层（`openai_compat` / `anthropic_compat`）对同一家供应商有 11 次
尝试的预算（首次 + 10 次重试），但这只解决「抖动」。真正不能用的场合——额度耗尽、
key 失效、渠道风控超过 5 分钟窗口、模型在全账号池冷却——重试同一家只是把用户钉在
等待里。用户定稿的行为是：

    某模型重试 11 次仍失败 → 按 providers.json 顺序换下一个供应商的默认模型
    → 直到所有供应商都失败，才真正停止（抛最后一个错误）

**分层**：本模块只做「换一家再试一次」，**不重试**——每家自己的 11 次预算仍由
传输层负责。所以最坏耗时 = N 家 × 单家预算，这是用户明确要的语义（宁可等，也别
直接失败），`should_cancel` 在每次切换前都会判一次，点停止仍然立刻生效。

**三个必须守住的语义**（否则比不兜底更糟）：

1. **粘住，不回头**：一旦切到 B，本实例后续调用都从 B 开始（`_idx` 前移）。
   否则每一步都要先烧掉 A 的 11 次重试（最坏 5 分钟）才轮到 B，会话会「每步都
   卡五分钟」。A 恢复后不自动切回——同一会话内稳定优先。
2. **不换的四种错**（`retry.should_failover`）：`partial`（已吐增量，换家重发会
   让半截答复重复上屏，上层走「接着续写」）、`truncated`（上层整轮重发 + 末轮
   降级非流式）、`context`（上层强制压缩后重试）、`bad_request`（换家同样 400）。
   换错了会**破坏上层已有的自愈链**，比不换更糟。
3. **图不能发给看不见的供应商**：请求里带 image 块时，跳过 `vision` 未声明的
   兜底项——往不支持看图的网关塞图片块会被 400 打死，等于把「换家」变成「必挂」。

**诊断**：每次失败 / 切换 / 全链耗尽都交给 `core/diagnostics/llm_failover.py`
落一条记录（JSONL + 项目黑板事件 `llm.provider_failed` / `llm.failover` /
`llm.failover.exhausted`），category 与 `retry.classify_provider_error` 同源。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from core.llm.provider import LLMError
from core.llm.retry import classify_provider_error, should_failover

log = logging.getLogger(__name__)

# 兜底链构建器：() -> [(provider_name, provider 实例), ...]（顺序即 providers.json 顺序）
AlternatesFactory = Callable[[], list[tuple[str, Any]]]


def _label_of(prov: Any, fallback: str = "") -> str:
    """供应商名（provider 实例没存名字，只能从 base_url 猜一个可读标签）。"""
    name = getattr(prov, "provider_name", None)
    if name:
        return str(name)
    url = str(getattr(prov, "base_url", "") or "")
    return fallback or (url or "primary")


def _has_images(messages: list[dict[str, Any]]) -> bool:
    """请求里是否带图片块（vision 通道）。只看 content 数组，纯文本消息零成本跳过。"""
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image":
                return True
    return False


class FallbackProvider:
    """主供应商 + 按序兜底链（鸭子类型实现 `LLMProvider`，其余属性透传当前供应商）。

    只由 `ProviderStore.build()` 构造；探活/自检路径用 `build(..., fallback=False)`。
    """

    def __init__(self, primary: Any, *, label: str = "",
                 alternates: AlternatesFactory | None = None,
                 timeout: float | None = None) -> None:
        self._primary = primary
        self._current = primary
        self._label = label or _label_of(primary)
        self._alternates = alternates
        self._timeout = timeout
        # 惰性构建：正常跑（不失败）时一次都不建兜底实例，零开销
        self._entries: list[tuple[str, Any]] | None = None
        self._idx = 0

    # ---------- 属性透传 ----------

    @property
    def model(self) -> str | None:
        """**当前**供应商的模型（兜底后随之变化，usage 记账才是真的）。"""
        return getattr(self._current, "model", None)

    @property
    def provider_name(self) -> str:
        return self._entries[self._idx][0] if self._entries else self._label

    def __getattr__(self, name: str) -> Any:
        # 下划线开头（_current/_entries/…）一律不转发：既防 __init__ 期间递归，
        # 也避免把包装器内部状态漏给调用方。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._current, name)

    # ---------- 兜底链 ----------

    def chain(self) -> list[tuple[str, Any]]:
        """完整兜底链（含主供应商，已按序实例化；构建失败的项跳过）。"""
        if self._entries is None:
            entries: list[tuple[str, Any]] = [(self._label, self._primary)]
            if self._alternates is not None:
                try:
                    entries.extend(self._alternates())
                except Exception:  # noqa: BLE001 —— 兜底链构建失败不该拖垮主调用
                    log.exception("兜底供应商链构建失败（只用主供应商）")
            self._entries = entries
        return self._entries

    def chain_labels(self) -> list[str]:
        return [n for n, _ in self.chain()]

    # ---------- 调用 ----------

    def chat(self, messages: list[dict[str, Any]], *, system: Any = None,
             tools: list[dict[str, Any]] | None = None,
             max_tokens: int = 16384, temperature: float | None = None,
             model: str | None = None, on_thinking: Any = None,
             on_text: Any = None, should_cancel: Any = None,
             on_retry: Any = None, on_failover: Any = None):
        """同 `LLMProvider.chat`；额外 ``on_failover(info)`` 在换家前回调。

        ``on_failover`` 的用途是**清掉上游增量缓冲**（思考/回复 delta）：前端按
        「最新 seq 的累计全文」覆盖显示，缓冲从零重流即自愈；不清则会拼出
        「失败那家的半截 + 新家的全文」。
        """
        entries = self.chain()
        needs_vision = _has_images(messages)
        last_err: BaseException | None = None
        prev_label = ""
        prev_idx = self._idx
        for idx in range(self._idx, len(entries)):
            label, prov = entries[idx]
            if idx > self._idx:
                # 换家前判中止：用户在等前一家退避时点了停止
                if should_cancel is not None and should_cancel():
                    raise LLMError("已中断")
                if needs_vision and not bool(getattr(prov, "supports_vision", False)):
                    log.info("跳过兜底供应商 %s：请求含图片但它未声明 vision", label)
                    continue
                # 真要换到这一家 → 先让调用方清掉上游增量缓冲（见 docstring 第 3 条）
                if on_failover is not None:
                    try:
                        on_failover({"from": prev_label or self._label, "to": label,
                                     "category": classify_provider_error(last_err)
                                     if last_err is not None else "",
                                     "error": str(last_err)[:300] if last_err else ""})
                    except Exception:  # noqa: BLE001 —— 回调炸了不能拖垮主流程
                        log.exception("on_failover 回调失败")
            started = time.monotonic()
            try:
                resp = self._call(prov, messages, system=system, tools=tools,
                                  max_tokens=max_tokens, temperature=temperature,
                                  model=model, on_thinking=on_thinking,
                                  on_text=on_text, should_cancel=should_cancel,
                                  on_retry=on_retry)
            except (LLMError, OSError) as e:
                elapsed = int((time.monotonic() - started) * 1000)
                last_err = e
                prev_label = label
                prev_idx = idx
                # 下一家「真能用」的（带图请求要跳过未声明 vision 的）
                nxt = next((lbl for lbl, alt in entries[idx + 1:]
                            if not needs_vision
                            or bool(getattr(alt, "supports_vision", False))), None)
                if not should_failover(e):
                    self._record_failure(label, prov, e, idx, entries, elapsed,
                                         next_label=None)
                    raise
                self._record_failure(label, prov, e, idx, entries, elapsed,
                                     next_label=nxt)
                if nxt is None:
                    self._record_exhausted(label, e, idx, entries)
                    raise
                log.warning("供应商 %s 调用失败（%s），切换到 %s 重试",
                            label, classify_provider_error(e), nxt)
                continue
            # 成功：粘住这一家（后续调用不再回头烧前一家 11 次预算）
            self._idx = idx
            self._current = prov
            if idx > 0:
                log.info("供应商兜底生效：%s 失败后改用 %s（%s）", self._label,
                         label, getattr(prov, "model", ""))
            return resp
        # 走到这里只有一种情形：链上剩余项全被 vision 条件跳过
        if last_err is not None:
            self._record_exhausted(prev_label or self._label, last_err,
                                   prev_idx, entries)
            raise last_err
        raise LLMError("没有可用的供应商（兜底链为空）")

    @staticmethod
    def _call(prov: Any, messages: list[dict[str, Any]], **kw: Any):
        """把非 None 的可选参数交给 provider（保持与直接调用逐字等价）。"""
        kwargs = {k: v for k, v in kw.items() if v is not None}
        return prov.chat(messages, **kwargs)

    # ---------- 诊断 ----------

    def _record_failure(self, label: str, prov: Any, err: BaseException,
                        idx: int, entries: list[tuple[str, Any]],
                        elapsed_ms: int, *, next_label: str | None) -> None:
        try:
            from core.diagnostics import llm_failover as failover_log
            failover_log.record_attempt(
                provider=label,
                model=str(getattr(prov, "model", "") or ""),
                category=classify_provider_error(err),
                status=int(getattr(err, "status", 0) or 0),
                error=str(err),
                elapsed_ms=elapsed_ms,
                index=idx + 1,
                chain=self.chain_labels() if entries else [],
                next_provider=next_label,
                next_model=(str(getattr(entries[idx + 1][1], "model", "") or "")
                            if next_label and idx + 1 < len(entries) else ""),
            )
        except Exception:  # noqa: BLE001 —— 诊断绝不能影响调用
            log.exception("兜底失败记录写入异常 provider=%s", label)

    def _record_exhausted(self, label: str, err: BaseException, idx: int,
                          entries: list[tuple[str, Any]]) -> None:
        try:
            from core.diagnostics import llm_failover as failover_log
            failover_log.record_exhausted(
                chain=self.chain_labels() if entries else [label],
                last_provider=label,
                last_model=str(getattr(entries[idx][1], "model", "") or ""),
                category=classify_provider_error(err),
                status=int(getattr(err, "status", 0) or 0),
                error=str(err),
            )
        except Exception:  # noqa: BLE001
            log.exception("兜底耗尽记录写入异常")


def with_fallback(primary: Any, store: Any, *, label: str = "",
                  timeout: float | None = None,
                  thinking_effort: str | None = None) -> Any:
    """给已构建的主供应商挂上兜底链；没有其它启用供应商时**原样返回**（零开销）。

    ``thinking_effort``：线程级思考强度覆盖，**要一并传给兜底实例**。否则主供应商
    挂了切到兜底那家时，强度会悄悄回落到那家的配置值——用户明明拖到 max，切家之后
    变成该供应商的缺省档，且没有任何提示（切家已经够混乱了，不该再叠一层静默变化）。
    """
    try:
        targets = store.alternate_targets(exclude=label)
    except Exception:  # noqa: BLE001 —— 读配置失败就退化成单供应商
        log.exception("兜底候选读取失败（退化为单供应商）")
        return primary
    if not targets:
        return primary

    def _factory() -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for pname, pmodel in targets:
            try:
                out.append((pname, store.build(pname, pmodel, timeout=timeout,
                                               fallback=False,
                                               thinking_effort=thinking_effort)))
            except Exception as e:  # noqa: BLE001 —— 缺 key/停用等：跳过这一家
                log.warning("兜底供应商 %s 不可构建（跳过）：%s", pname, e)
        return out

    return FallbackProvider(primary, label=label, alternates=_factory,
                            timeout=timeout)
