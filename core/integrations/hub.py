"""飞书推送中枢（feishu-push v1，docs/plans/feishu-connector.md）。

职责（平台策略层，与协议层 feishu.py 分工）：
- 订阅各项目黑板 EventBus（finding.new / finding.updated / finding.merged）
- 「确认的中危及以上漏洞」边沿判定：category=vuln 且 status=verified 且
  severity ≥ min_severity；首报即 verified 或 unverified→verified 升级两种路径
- ledger 去重（(pid, fid) 只推一次；escalation 升档再推可开）
- 投递 PushWorker（未启动 = 丢弃不炸）；失败隔离：异常绝不回传写路径

挂载点：core/projects.py 的 Project.bb 懒建处 attach_store；CLI/demo 独立
进程直写黑板不覆盖（v1 范围外）。
"""

import logging
import os
import threading
from pathlib import Path

from core.integrations import feishu
from core.integrations.worker import PushWorker

log = logging.getLogger(__name__)

# 关心的 finding 事件（merged 与 new 同 payload 形态；防重启丢 ledger 后
# merged 重放时至少补推一次——at-least-once，去重交给 ledger）
EVENT_KINDS = ("finding.new", "finding.updated", "finding.merged")

SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _rank(sev: str) -> int:
    return SEV_RANK.get(str(sev or "").lower(), -1)


class FeishuPushHub:
    """config_path 可注入（测试 hermetic）；worker/client_factory 可注入。"""

    def __init__(self, config_path=None, worker: PushWorker | None = None,
                 client_factory=None):
        self._config_path = Path(config_path or feishu.CONFIG_PATH)
        self._worker = worker
        self._client_factory = client_factory or self._default_client
        self._lock = threading.RLock()
        self._subs: dict[str, tuple] = {}     # pid -> (store, unsubscribe)
        self._pushed: dict[str, dict] = {}    # pid -> {fid: {"severity", "at"}}
        self._pushed_count = 0                # 累计判定推送（不随项目关闭清）
        self._cfg_mtime = None
        self._cfg: dict = {**feishu.DEFAULTS, "enabled": False}

    # ---- 配置（mtime 缓存：事件热路径不重复读盘；坏配置=停用不炸）----

    def config(self) -> dict:
        try:
            st = os.stat(self._config_path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        with self._lock:
            if key != self._cfg_mtime:
                raw = feishu.load_config(self._config_path)
                try:
                    cfg = feishu.normalize_config(raw)
                except (ValueError, feishu.FeishuError):
                    # 读路径宽松：坏配置（非法 webhook/severity）= 停用，绝不炸事件路径
                    cfg = {**feishu.DEFAULTS, **raw, "enabled": False}
                self._cfg_mtime, self._cfg = key, cfg
            return dict(self._cfg)

    def reload_config(self) -> dict:
        """强制重读（PUT 配置后立即生效用）。"""
        with self._lock:
            self._cfg_mtime = None
            return self.config()

    # ---- 订阅 ----

    def attach_store(self, pid: str, store, project_name: str = "") -> None:
        """订阅一个项目的黑板总线（幂等按 pid）。store 需有 .bus / .get_finding。"""
        with self._lock:
            if pid in self._subs:
                return
        unsub = store.bus.subscribe(
            lambda ev: self._on_event(pid, store, project_name, ev))
        with self._lock:
            self._subs[pid] = (store, unsub)
            self._pushed.setdefault(pid, {})

    def detach(self, pid: str) -> None:
        with self._lock:
            entry = self._subs.pop(pid, None)
            self._pushed.pop(pid, None)
        if entry is None:
            return
        try:
            entry[1]()
        except Exception:  # noqa: BLE001 —— 退订失败只记日志
            log.debug("飞书推送退订失败 pid=%s", pid)

    def shutdown(self) -> None:
        for pid in list(self._subs):
            self.detach(pid)
        if self._worker is not None:
            self._worker.shutdown()

    def attached(self) -> list[str]:
        with self._lock:
            return sorted(self._subs)

    # ---- 判定（纯函数，测试直接打）----

    def decide(self, kind: str, payload: dict, row: dict, cfg: dict,
               pushed: dict | None) -> str | None:
        """是否推送 → trigger 字符串（"new"/"verified"/"escalation"）或 None。

        pushed：该 finding 的已推记录（None=从未推过）。"""
        if kind not in EVENT_KINDS:
            return None
        if not row or str(row.get("category") or "vuln") != "vuln":
            return None
        if str(row.get("status") or "") != "verified":
            return None
        sev = str(row.get("severity") or "info").lower()
        if _rank(sev) < _rank(cfg.get("min_severity")):
            return None
        if pushed is None:
            if kind == "finding.updated":
                # 只有 unverified→verified 的跳变瞬间算（重复 PATCH 已推过
                # 的行会被 leduer 拦在上一层，这里防的是同行内其它字段改动）
                if "status" not in (payload.get("changed") or []):
                    return None
                if str(payload.get("status") or "") != "verified":
                    return None
            return "new" if kind != "finding.updated" else "verified"
        # 已推过：仅 severity 升档且开关允许时再推
        if (cfg.get("notify_on_escalation")
                and _rank(sev) > _rank(pushed.get("severity"))):
            return "escalation"
        return None

    # ---- 事件入口 ----

    def _on_event(self, pid: str, store, project_name: str, event: dict) -> None:
        try:
            self._handle(pid, store, project_name, event)
        except Exception:  # noqa: BLE001 —— 通知失败绝不回传写路径
            log.exception("飞书推送事件处理异常（已吞）: pid=%s kind=%s",
                          pid, event.get("kind"))

    def _handle(self, pid: str, store, project_name: str, event: dict) -> None:
        kind = event.get("kind")
        if kind not in EVENT_KINDS:
            return
        cfg = self.config()
        if not cfg.get("enabled"):
            return
        allow = cfg.get("projects") or []
        if allow and pid not in allow and project_name not in allow:
            return
        payload = event.get("payload") or {}
        fid = str(payload.get("finding_id") or "")
        if not fid:
            return
        row = self._load_row(pid, store, fid, payload)
        with self._lock:
            pushed = (self._pushed.get(pid) or {}).get(fid)
        trigger = self.decide(kind, payload, row, cfg, pushed)
        if trigger is None:
            return
        with self._lock:  # 判定即记账（at-most-once：宁漏不重推）
            self._pushed.setdefault(pid, {})[fid] = {
                "severity": str(row.get("severity") or "info").lower(),
                "at": event.get("created_at") or ""}
            self._pushed_count += 1
        card = feishu.build_vuln_card(
            row, project_name, base_url=cfg.get("base_url", ""),
            include_poc=bool(cfg.get("include_poc")), trigger=trigger)
        meta = {"project_id": pid, "project": project_name,
                "finding_id": fid,
                "title": str(row.get("title") or "")[:120],
                "severity": str(row.get("severity") or ""),
                "trigger": trigger}
        if self._worker is None:
            log.debug("飞书推送 worker 未启动，丢弃: %s", meta["title"])
            return
        try:
            client = self._client_factory(cfg)
        except Exception:  # noqa: BLE001
            log.exception("飞书客户端构造失败")
            return
        self._worker.submit({"card": card, "client": client, "meta": meta})

    @staticmethod
    def _load_row(pid: str, store, fid: str, payload: dict) -> dict:
        """拉 finding 行；失败/缺失 → 事件 payload 兜底（severity 缺失则判不出，跳过）。"""
        try:
            row = store.get_finding(pid, fid)
        except Exception:  # noqa: BLE001 —— 黑板已关等
            row = None
        if isinstance(row, dict) and row:
            return row
        return {"id": fid,
                "severity": payload.get("severity"),
                "status": payload.get("status"),
                "category": payload.get("category"),
                "title": payload.get("title"),
                "target_asset_id": payload.get("target_asset_id")}

    # ---- worker 生命周期 / 测试端点 ----

    def start_worker(self, rate_per_min: int | None = None,
                     retry: int | None = None) -> None:
        """建（或复用）worker 并启动。rate/retry 取启动时配置，改后需重启生效。"""
        cfg = self.config()
        rate = rate_per_min or int(cfg.get("rate_limit_per_min") or 20)
        n_retry = retry if retry is not None else int(cfg.get("retry") or 3)
        if self._worker is None:
            self._worker = PushWorker(self._send_task, rate_per_min=rate,
                                      retry=n_retry)
        self._worker.start()

    def _send_task(self, task: dict) -> None:
        task["client"].send_card(task["card"])

    def restart_worker_if_needed(self) -> None:
        """PUT 改 rate/retry 后按新值重建 worker（未变则不动；不丢 recent
        之外的状态——计数随重置，可接受）。"""
        cfg = self.config()
        rate = int(cfg.get("rate_limit_per_min") or 20)
        n_retry = int(cfg.get("retry") or 3)
        w = self._worker
        if w is None:
            self.start_worker()
            return
        if w.running and w.rate_per_min == rate and w.retry_attempts == n_retry:
            return
        w.shutdown()
        self._worker = PushWorker(self._send_task, rate_per_min=rate,
                                  retry=n_retry)
        self._worker.start()

    def send_test(self) -> tuple[bool, str]:
        """测试连接：同步发一张测试卡（API 层放 threadpool 调用）。
        成功即复位 worker 熔断。返回 (ok, detail)。"""
        cfg = self.config()
        client = self._client_factory(cfg)
        if not client.configured:
            return False, "未配置 webhook_url——先填入自定义机器人地址"
        try:
            client.send_card(feishu.build_test_card(cfg.get("base_url", "")))
        except feishu.FeishuError as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            return False, f"发送失败: {e}"
        if self._worker is not None:
            self._worker.reset()
        return True, "测试卡片已发送"

    def stats(self) -> dict:
        out: dict = {"attached": self.attached(),
                     "pushed_total": self._pushed_count,
                     "worker": self._worker.stats() if self._worker else None}
        return out

    @staticmethod
    def _default_client(cfg: dict):
        return feishu.FeishuClient(cfg.get("webhook_url", ""),
                                   cfg.get("secret", ""),
                                   timeout=float(cfg.get("timeout_s") or 10))


# ---- 进程级单例（Project.bb 钩子用；未 start_worker 时只判定不发送）----

_default_hub: FeishuPushHub | None = None
_default_lock = threading.Lock()


def get_default_hub() -> FeishuPushHub:
    global _default_hub
    with _default_lock:
        if _default_hub is None:
            _default_hub = FeishuPushHub()
        return _default_hub
