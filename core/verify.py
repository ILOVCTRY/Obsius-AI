"""独立验证器（independent-verification-audit M1，2026-09-23，DESIGN §六）。

核心命题：题目做对了没有，不是 Agent 自己说了算的。验收条目带 verify 规格
（acceptance 从纯字符串升级为 ``str | {text, verify}``）时，met/failed 由服务端
验证器判定——Agent 对 verify 条目自报 met/failed 被 store 拒绝（tasks.py
``set_reconcile_state`` 红线），只能置 blocked（附不适用理由）。判定执行经执行
网关（平台身份 trusted + host + 工作区隔离，宁严勿松），回执脱敏红线（照抄
VulnHouse VE）：只含 passed + 结构化摘要（长度/哈希前缀），flag/token 原文不回显
——防 Agent 经验证接口反套答案。

四策略（规格受约束 schema，未知键拒绝，仿 phases.yaml 精神）：
- flag_capture  读文件（``src: "file:<工作区相对路径>"``）或执行命令取输出，
                与预期值三种匹配（exact/contains/regex）
- effect_proof  检查命令清单逐项比对（expect: contains/regex/exit0）
- poc_crash     PoC 使目标崩溃（detector: signal 退出信号 / asan 特征）
- oracle        自定义脚本输出 JSON ``{pass, detail}``（业务逻辑/复杂链兜底）

模块地位仿 phases.py：纯判定引擎，不 import orchestrator；黑板写全部走
TaskQueue 既有写口（set_reconcile_state + append_event）。gateway 在函数内
惰性 import——gateway 顶层反向依赖 blackboard.store，此处顶层 import 会成环。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

STRATEGIES = ("flag_capture", "effect_proof", "poc_crash", "oracle")
MATCH_MODES = ("exact", "contains", "regex")
EXPECT_MODES = ("exit0", "contains", "regex")
DETECTORS = ("signal", "asan")

# 崩溃退出码启发（signal 探测器）：POSIX 信号为负；Windows 常见崩溃码；
# 134/139 是 128+信号 的 shell 惯例（SIGABRT/SIGSEGV）。超时不算崩溃。
CRASH_EXIT_CODES = {134, 139, 3221225477, 2147483651}  # 0xC0000005 / 0x80000003
ASAN_SIGNATURES = ("AddressSanitizer",)

MAX_CMD_LEN = 2000
MAX_VALUE_LEN = 1000
MAX_CHECKS = 20

Spec = dict[str, Any]


# ---------- 规格校验（发布期 fail-fast；纯 stdlib，blackboard 可安全顶层 import） ----------

def validate_verify_spec(spec: Any) -> Spec:
    """受约束 schema 校验，返回归一化 spec；违例 ValueError（publish→422/工具 [拒绝]）。"""
    if not isinstance(spec, dict):
        raise ValueError("verify 规格必须是对象（{strategy, ...}）")
    strategy = spec.get("strategy")
    if strategy not in STRATEGIES:
        raise ValueError(f"未知验证策略: {strategy!r}（可选 {', '.join(STRATEGIES)}）")

    def _cmd(key: str) -> str:
        v = spec.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{strategy}: {key} 必须是非空命令字符串")
        if len(v) > MAX_CMD_LEN:
            raise ValueError(f"{strategy}: {key} 超长（>{MAX_CMD_LEN} 字符）")
        return v

    if strategy == "flag_capture":
        allowed = {"strategy", "src", "cmd", "match", "value"}
        unknown = set(spec) - allowed
        if unknown:
            raise ValueError(f"flag_capture 未知键: {sorted(unknown)}（允许 {sorted(allowed)}）")
        has_src, has_cmd = "src" in spec, "cmd" in spec
        if has_src == has_cmd:
            raise ValueError("flag_capture: src 与 cmd 二选一（取输出源）")
        if has_src:
            src = spec["src"]
            if not isinstance(src, str) or not src.startswith("file:") or len(src) < 6:
                raise ValueError('flag_capture: src 必须是 "file:<工作区相对路径>" 形式')
            if len(src) > MAX_CMD_LEN:
                raise ValueError(f"flag_capture: src 超长（>{MAX_CMD_LEN} 字符）")
        match = spec.get("match", "contains")
        if match not in MATCH_MODES:
            raise ValueError(f"flag_capture: match 须为 {MATCH_MODES}，得到 {match!r}")
        value = spec.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("flag_capture: value（预期值）必填")
        if len(value) > MAX_VALUE_LEN:
            raise ValueError(f"flag_capture: value 超长（>{MAX_VALUE_LEN} 字符）")
        if match == "regex":
            try:
                re.compile(value)
            except re.error as e:
                raise ValueError(f"flag_capture: value 不是合法正则: {e}") from e
        return {**spec, "match": match}

    if strategy == "effect_proof":
        allowed = {"strategy", "checks"}
        unknown = set(spec) - allowed
        if unknown:
            raise ValueError(f"effect_proof 未知键: {sorted(unknown)}（允许 {sorted(allowed)}）")
        checks = spec.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("effect_proof: checks 必须是非空数组")
        if len(checks) > MAX_CHECKS:
            raise ValueError(f"effect_proof: checks 超上限（>{MAX_CHECKS} 项）")
        norm = []
        for i, c in enumerate(checks, 1):
            if not isinstance(c, dict):
                raise ValueError(f"effect_proof: checks[{i}] 必须是对象")
            unknown = set(c) - {"cmd", "expect", "value"}
            if unknown:
                raise ValueError(f"effect_proof: checks[{i}] 未知键 {sorted(unknown)}")
            cmd = c.get("cmd")
            if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_CMD_LEN:
                raise ValueError(f"effect_proof: checks[{i}].cmd 必须是非空命令（≤{MAX_CMD_LEN} 字符）")
            expect = c.get("expect", "exit0")
            if expect not in EXPECT_MODES:
                raise ValueError(f"effect_proof: checks[{i}].expect 须为 {EXPECT_MODES}，得到 {expect!r}")
            value = c.get("value")
            if expect in ("contains", "regex"):
                if not isinstance(value, str) or not value:
                    raise ValueError(f"effect_proof: checks[{i}] expect={expect} 时 value 必填")
                if len(value) > MAX_VALUE_LEN:
                    raise ValueError(f"effect_proof: checks[{i}].value 超长（>{MAX_VALUE_LEN} 字符）")
                if expect == "regex":
                    try:
                        re.compile(value)
                    except re.error as e:
                        raise ValueError(f"effect_proof: checks[{i}].value 不是合法正则: {e}") from e
            norm.append({"cmd": cmd, "expect": expect, **({"value": value} if value is not None else {})})
        return {"strategy": strategy, "checks": norm}

    if strategy == "poc_crash":
        allowed = {"strategy", "cmd", "detector"}
        unknown = set(spec) - allowed
        if unknown:
            raise ValueError(f"poc_crash 未知键: {sorted(unknown)}（允许 {sorted(allowed)}）")
        _cmd("cmd")
        detector = spec.get("detector", "signal")
        if detector not in DETECTORS:
            raise ValueError(f"poc_crash: detector 须为 {DETECTORS}，得到 {detector!r}")
        return {**spec, "detector": detector}

    # oracle
    allowed = {"strategy", "cmd"}
    unknown = set(spec) - allowed
    if unknown:
        raise ValueError(f"oracle 未知键: {sorted(unknown)}（允许 {sorted(allowed)}）")
    _cmd("cmd")
    return dict(spec)


# ---------- 判定引擎 ----------

class Verdict(dict):
    """判定结果：{passed, strategy, evidence_head, duration_s}。

    evidence_head 恒为结构化摘要（长度/哈希前缀），绝不含输出/预期值原文
    （脱敏红线：Agent 可见的工具返回与事件 payload 都从这里出）。"""


def _mask(text: str) -> str:
    """输出/文件内容的脱敏摘要——只露长度与哈希前缀，不露原文。"""
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"输出 {len(text)} 字符 · sha256:{digest}…"


def _workspace_file(workspace: Path, src: str) -> Path:
    """file: 路径解析（防穿越）：仅工作区相对路径，resolve 后必须落在工作区内。"""
    rel = src[len("file:"):]
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"src 越界：仅允许工作区相对路径（得到 {rel!r}）")
    resolved = (workspace / p).resolve()
    root = Path(workspace).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"src 越界工作区: {rel!r}")
    return resolved


def _match(mode: str, content: str, value: str) -> bool:
    if mode == "exact":
        return content.strip() == value
    if mode == "regex":
        return re.search(value, content) is not None
    return value in content  # contains


def evaluate(spec: Spec, *, executor: Callable[[str], Any],
             workspace: Path) -> Verdict:
    """按策略执行判定。executor(cmd) → 有 ok/exit_code/stdout/stderr/timed_out 的
    结果对象（真实链路是 ExecutionGateway.run；测试注入 fake）。规格问题按
    fail-closed 处理（未过 + 原因入摘要），绝不向 Agent 回显敏感内容。"""
    started = time.monotonic()
    strategy = spec.get("strategy")

    def done(passed: bool, evidence: str) -> Verdict:
        return Verdict(passed=passed, strategy=strategy, evidence_head=evidence,
                       duration_s=round(time.monotonic() - started, 3))

    def run(cmd: str) -> Any:
        return executor(cmd)

    try:
        if strategy == "flag_capture":
            if "src" in spec:
                path = _workspace_file(workspace, spec["src"])
                if not path.is_file():
                    return done(False, f"{strategy}: 文件不存在（src={spec['src'][5:]!r}）")
                content = path.read_text(encoding="utf-8", errors="replace")
                exit_note = "file"
            else:
                r = run(spec["cmd"])
                content = r.stdout or ""
                exit_note = f"exit={r.exit_code}"
            hit = _match(spec.get("match", "contains"), content, spec["value"])
            return done(hit, f"{strategy} {spec.get('match', 'contains')} "
                             f"{'命中' if hit else '未命中'}（{exit_note}）· 实际{_mask(content)}")

        if strategy == "effect_proof":
            checks = spec["checks"]
            for i, c in enumerate(checks, 1):
                r = run(c["cmd"])
                out = r.stdout or ""
                expect = c.get("expect", "exit0")
                if expect == "exit0":
                    ok = r.exit_code == 0
                elif expect == "regex":
                    ok = re.search(c["value"], out) is not None
                else:
                    ok = c["value"] in out
                if not ok:
                    return done(False, f"{strategy}: 检查 {i}/{len(checks)} 未过"
                                       f"（expect={expect} · exit={r.exit_code}）· 实际{_mask(out)}")
            return done(True, f"{strategy}: {len(checks)} 项检查全过")

        if strategy == "poc_crash":
            r = run(spec["cmd"])
            out = (r.stdout or "") + (r.stderr or "")
            if spec.get("detector", "signal") == "asan":
                hit = any(sig in out for sig in ASAN_SIGNATURES)
                return done(hit, f"{strategy} asan 特征{'命中' if hit else '未命中'}"
                                 f"（exit={r.exit_code}）· {_mask(out)}")
            hit = (not r.timed_out) and (r.exit_code < 0 or r.exit_code in CRASH_EXIT_CODES)
            note = "崩溃特征" if hit else ("超时（不算崩溃）" if r.timed_out else "非崩溃")
            return done(hit, f"{strategy} signal: 退出码 {r.exit_code}（{note}）· {_mask(out)}")

        if strategy == "oracle":
            r = run(spec["cmd"])
            out = r.stdout or ""
            data = _extract_json(out)
            if not isinstance(data, dict) or "pass" not in data:
                return done(False, f"{strategy}: 输出不是 {{pass, detail}} JSON"
                                   f"（exit={r.exit_code}）· {_mask(out)}")
            passed = bool(data.get("pass"))
            return done(passed, f"{strategy}: 裁决 {'pass' if passed else 'fail'}"
                                f"（exit={r.exit_code}）· {_mask(out)}")

        return done(False, f"未知验证策略: {strategy!r}")
    except Exception as exc:  # noqa: BLE001 —— 策略拒绝/文件越界/后端不可用一律 fail-closed
        return done(False, f"{strategy}: 验证执行被拒/失败（{type(exc).__name__}）")


def _extract_json(text: str) -> Any:
    """从命令输出提取 JSON 对象：整体解析失败时取首个 { 到末个 }。"""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        lo, hi = text.find("{"), text.rfind("}")
        if 0 <= lo < hi:
            try:
                return json.loads(text[lo:hi + 1])
            except json.JSONDecodeError:
                return None
    return None


# ---------- 收尾验证钩子（tasks.py complete 前调用） ----------

def run_reconcile_verifications(tq: Any, task_id: str, session_id: str, *,
                                gateway: Any = None) -> list[dict]:
    """任务收尾钩子：跑 reconcile 中带 verify 规格且 state ∈ {pending, failed} 的
    条目（failed 也重跑 = 「修正后重新 complete」重验语义；blocked 视为不适用跳过）。
    通过 → met、未过 → failed（note=脱敏摘要），逐条落 verify.result 事件
    （author=verifier）。存在未过条目 → ValueError 拦下本次 complete（与
    reconcile_blocked 同口径）。无 verify 条目零成本直返。

    gateway 参数供测试注入 fake；生产路径惰性构造 ExecutionGateway（平台身份
    trusted + host + 工作区隔离，命令与拒绝都落 command/command.result 审计）。"""
    bb = tq.bb
    row = bb.conn.execute(
        "SELECT project_id, status, claimed_by, context FROM tasks WHERE id=?",
        (task_id,)).fetchone()
    if row is None or row["claimed_by"] != session_id or row["status"] != "claimed":
        return []  # 权限/存在性由 _finish 统一报错，钩子只管自己的活
    try:
        ctx = json.loads(row["context"] or "{}")
    except json.JSONDecodeError:
        ctx = {}
    entries = [e for e in (ctx.get("reconcile") or [])
               if e.get("verify") and e.get("state") in ("pending", "failed")]
    if not entries:
        return []

    from core.runtime.gateway import ExecutionGateway  # 惰性：防与 blackboard 成环

    workspace = Path(bb.db_path).parent
    if gateway is None:
        gateway = ExecutionGateway(bb=bb)

    def executor(cmd: str) -> Any:
        return gateway.run(cmd, "host", threat_class="trusted",
                           project_id=row["project_id"], session_id=session_id,
                           author="verifier", workspace=workspace)

    results: list[dict] = []
    for e in entries:
        verdict = evaluate(e["verify"], executor=executor, workspace=workspace)
        passed = bool(verdict["passed"])
        tq.set_reconcile_state(task_id, session_id, e["id"],
                               "met" if passed else "failed",
                               note=verdict["evidence_head"][:300], as_verifier=True)
        bb.append_event(row["project_id"], "verify.result",
                        {"task_id": task_id, "item_id": e["id"],
                         "strategy": verdict.get("strategy"), "passed": passed,
                         "evidence_head": verdict["evidence_head"],
                         "duration_s": verdict["duration_s"]},
                        session_id=session_id, author="verifier")
        results.append({"item_id": e["id"], **verdict})

    failed = [r for r in results if not r["passed"]]
    if failed:
        items = "\n".join(f"  #{r['item_id']} [{r.get('strategy')}] {r['evidence_head']}"
                          for r in failed)
        raise ValueError(
            "[独立验证] 以下验收条目由验证器判定未过（met 不由 Agent 自报）：\n"
            f"{items}\n"
            "修正后重新 complete（验证器会重跑未过条目）；确不适用可 task_reconcile "
            "置 blocked（note 附原因，人类可审计）。")
    return results
