"""飞书自定义机器人 webhook 客户端（feishu-push v1，docs/plans/feishu-connector.md）。

职责（**纯函数/纯协议层，不 import 黑板**）：
- config/feishu.json 读写 + 归一化 + 脱敏（密钥绝不入库不入日志，同 fofa 口径）
- 自定义机器人签名（secret 非空时 HMAC-SHA256）
- interactive 消息卡片构造（finding 行 → 飞书 card JSON）
- 发送：transport 可注入（测试 fake 零触网）；默认 httpx 就地导入

判定「要不要推」是平台策略，在 hub.py，不在本文件。
"""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

__all__ = [
    "CONFIG_PATH", "DEFAULTS", "SEVERITY_MIN_CHOICES", "SEVERITY_COLORS",
    "FeishuError", "FeishuConfigError", "FeishuAuthError",
    "load_config", "save_config", "normalize_config", "mask_webhook",
    "mask_secret", "gen_sign", "build_vuln_card", "build_test_card",
    "FeishuClient", "clip",
]

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "feishu.json"

# 严重度档位（与 findings.severity 对齐）：排序值越大越严重
SEVERITY_MIN_CHOICES = ("low", "medium", "high", "critical")
SEVERITY_COLORS = {"critical": "red", "high": "orange", "medium": "yellow",
                   "low": "blue", "info": "grey"}
SEVERITY_LABELS = {"critical": "严重", "high": "高危", "medium": "中危",
                   "low": "低危", "info": "信息"}

# 已知键的缺省值；未知键透传（手编字段不丢，同 intel profile 口径）
DEFAULTS = {
    "enabled": False,
    "webhook_url": "",
    "secret": "",
    "min_severity": "medium",
    "projects": [],               # 空 = 全部项目；否则项目 id/slug 白名单
    "notify_on_escalation": False,
    "include_poc": False,
    "rate_limit_per_min": 20,
    "timeout_s": 10,
    "retry": 3,
    "base_url": "http://127.0.0.1:8420",
}

_ALLOWED_HOSTS = ("open.feishu.cn", "open.larksuite.com")


class FeishuError(Exception):
    """飞书推送错误基类（message 可展示）。"""


class FeishuConfigError(FeishuError):
    """webhook 未配置 / 形态非法（不可重试）。"""


class FeishuAuthError(FeishuError):
    """签名错误/机器人被移除（重试无用，需人工修配置）。"""


def clip(text, limit: int = 400) -> str:
    """截断到 limit 字符（飞书 lark_md 单段上限），超长补省略号。"""
    t = str(text or "").strip()
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)] + "…"


def mask_webhook(url: str) -> str:
    """webhook 脱敏：保留 scheme+host+路径前缀，尾段 token 打码。
    https://open.feishu.cn/open-apis/bot/v2/hook/abcd1234 → .../hook/abcd****"""
    u = str(url or "").strip()
    if not u:
        return ""
    marker = "/hook/"
    idx = u.find(marker)
    if idx == -1:
        return u[:12] + "****" if len(u) > 12 else "****"
    head, token = u[: idx + len(marker)], u[idx + len(marker):]
    if len(token) <= 4:
        return head + "*" * len(token)
    return head + token[:4] + "*" * (len(token) - 4)


def mask_secret(secret: str) -> str:
    """secret 脱敏：非空即全 *（短密钥逐位暴露也没意义）。"""
    return "****" if str(secret or "").strip() else ""


def load_config(path) -> dict:
    """读原始配置（缺失/坏 JSON/非 dict → {}，绝不抛——GET 端点永不 500）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(path, cfg: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")


def normalize_config(raw: dict) -> dict:
    """归一化：已知键校验/补默认，未知键透传。非法值抛 ValueError
    （API 层转 422——与 fofa/intel 的宽松兜底分工：读宽松、写严格）。"""
    out: dict = {}
    for k, v in (raw or {}).items():
        out[k] = v
    out["enabled"] = bool(raw.get("enabled", DEFAULTS["enabled"]))
    out["webhook_url"] = str(raw.get("webhook_url") or "").strip()
    out["secret"] = str(raw.get("secret") or "").strip()
    sev = str(raw.get("min_severity") or DEFAULTS["min_severity"]).strip().lower()
    if sev not in SEVERITY_MIN_CHOICES:
        raise ValueError(
            f"非法 min_severity: {sev}（允许 {list(SEVERITY_MIN_CHOICES)}）")
    out["min_severity"] = sev
    projects = raw.get("projects") or []
    if not isinstance(projects, list):
        raise ValueError("projects 必须是字符串数组（空=全部项目）")
    out["projects"] = [str(p).strip() for p in projects if str(p).strip()]
    out["notify_on_escalation"] = bool(raw.get("notify_on_escalation",
                                               DEFAULTS["notify_on_escalation"]))
    out["include_poc"] = bool(raw.get("include_poc", DEFAULTS["include_poc"]))
    for k in ("rate_limit_per_min", "timeout_s", "retry"):
        try:
            out[k] = int(raw.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            raise ValueError(f"{k} 必须是整数")
        if out[k] <= 0:
            raise ValueError(f"{k} 必须为正整数")
    out["base_url"] = str(raw.get("base_url") or DEFAULTS["base_url"]).strip()
    if out["enabled"]:
        validate_webhook(out["webhook_url"])
    return out


def validate_webhook(url: str) -> None:
    """自定义机器人 webhook 形态校验：仅 https；host 限飞书/Lark 开放域。"""
    from urllib.parse import urlparse
    u = str(url or "").strip()
    if not u:
        raise FeishuConfigError("未配置飞书 webhook_url——先填入自定义机器人地址")
    try:
        parsed = urlparse(u)
    except ValueError:
        raise FeishuConfigError(f"webhook_url 形态非法: {u[:80]}")
    if parsed.scheme != "https":
        raise FeishuConfigError("webhook_url 必须为 https（自定义机器人仅支持 https）")
    if parsed.hostname not in _ALLOWED_HOSTS:
        raise FeishuConfigError(
            f"webhook_url 域名须为 {_ALLOWED_HOSTS} 之一（当前 {parsed.hostname}）")


def gen_sign(secret: str, timestamp: str) -> str:
    """自定义机器人签名：HMAC-SHA256("{timestamp}\\n{secret}") → base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _severity_label(sev: str) -> str:
    return SEVERITY_LABELS.get(str(sev or "").lower(), str(sev or "未评级"))


def build_vuln_card(finding: dict, project_name: str, *,
                    base_url: str = "", include_poc: bool = False,
                    trigger: str = "new") -> dict:
    """漏洞 → 飞书 interactive 卡片 JSON（不含 msg_type/sign，由 build_payload 加）。

    finding：黑板 findings 行（键容错——缺字段给占位）。trigger：new/verified/
    escalation（卡头副标题说明为什么推这条）。"""
    sev = str(finding.get("severity") or "info").lower()
    title = str(finding.get("title") or "(未命名漏洞)")
    vuln_class = str(finding.get("vuln_class") or "").strip()
    fid = str(finding.get("id") or "")
    project = str(project_name or "(未知项目)")
    color = SEVERITY_COLORS.get(sev, "grey")
    trigger_note = {"new": "新登记", "verified": "已验证确认",
                    "escalation": "严重度升级"}.get(trigger, "通知")

    lines = [
        f"**严重度**：{_severity_label(sev)}（{sev}）",
        f"**项目**：{project}",
    ]
    if vuln_class:
        lines.append(f"**类别**：{vuln_class}")
    if finding.get("target_asset_id"):
        lines.append(f"**目标资产**：`{finding['target_asset_id']}`")
    basis = clip(finding.get("rating_basis"), 200)
    if basis:
        lines.append(f"**判级依据**：{basis}")
    impact = clip(finding.get("impact"), 300)
    if impact:
        lines.append(f"**影响**：{impact}")
    repro = clip(finding.get("reproduction_steps"), 300)
    if repro:
        lines.append(f"**复现步骤**：{repro}")
    if include_poc:
        pocs = finding.get("pocs")
        if isinstance(pocs, str):
            try:
                pocs = json.loads(pocs)
            except ValueError:
                pocs = []
        snippets = []
        for p in (pocs or [])[:2]:
            code = clip((p or {}).get("code"), 200) if isinstance(p, dict) else ""
            if code:
                snippets.append(code)
        if snippets:
            lines.append("**POC**：\n" + "\n".join(f"```\n{s}\n```" for s in snippets))

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": "\n".join(lines)}},
        {"tag": "note",
         "elements": [{"tag": "plain_text",
                       "content": f"cyberstrike · 漏洞{trigger_note} · "
                                  f"{str(finding.get('updated_at') or '')}"}]},
    ]
    if base_url and fid:
        link = f"{base_url.rstrip('/')}/?finding={fid}"
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "打开平台"},
             "url": link, "type": "primary"}]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text",
                      "content": f"[漏洞] {clip(title, 60)}"},
        },
        "elements": elements,
    }


def build_test_card(base_url: str = "") -> dict:
    """测试卡片（连通性验证，不携带任何真实漏洞数据）。"""
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": "连接器连通性测试：收到本卡片即表示 "
                                           "webhook 推送链路可用。"}}]
    if base_url:
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "打开平台"},
             "url": base_url.rstrip("/"), "type": "default"}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text",
                             "content": "[测试] cyberstrike 飞书连接器"}},
        "elements": elements,
    }


class FeishuClient:
    """自定义机器人发送端。transport 可注入：对象需带
    ``post(url, json=, timeout=)`` 返回含 ``status_code`` / ``text`` /
    ``json()`` 的响应（镜像 httpx 接口）。"""

    RETRYABLE_HINTS = ("too many request", "too many requests", "rate limit",
                       "frequency", "限流")

    def __init__(self, webhook_url: str, secret: str = "", timeout: float = 10.0,
                 transport=None):
        self.webhook_url = str(webhook_url or "").strip()
        self.secret = str(secret or "").strip()
        self.timeout = timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def build_payload(self, card: dict, timestamp: str | None = None) -> dict:
        """card → webhook 请求体（msg_type=interactive；secret 非空带签名头）。"""
        payload: dict = {"msg_type": "interactive", "card": card}
        if self.secret:
            ts = timestamp or str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = gen_sign(self.secret, ts)
        return payload

    def _post(self, payload: dict):
        if self._transport is not None:
            return self._transport.post(self.webhook_url, json=payload,
                                        timeout=self.timeout)
        import httpx  # 就地导入：未装 httpx 时其余功能仍可用
        return httpx.post(self.webhook_url, json=payload, timeout=self.timeout)

    @staticmethod
    def _check(resp) -> None:
        """飞书响应判定：HTTP 200 + (code==0 或 StatusCode==0) 为成功；
        其余按 message 分类（限流可重试 / 签名类不可重试）。"""
        if resp.status_code != 200:
            if resp.status_code >= 500:
                raise FeishuError(f"飞书 HTTP {resp.status_code}（可重试）")
            raise FeishuAuthError(f"飞书 HTTP {resp.status_code}："
                                  f"{(resp.text or '')[:120]}")
        try:
            data = resp.json()
        except Exception:
            raise FeishuError(f"飞书响应非 JSON：{(resp.text or '')[:120]}")
        if not isinstance(data, dict):
            raise FeishuError(f"飞书响应格式异常: {str(data)[:120]}")
        # 成功：code/StatusCode==0（新版/旧版字段），或两者皆缺且无错误文案
        code = data.get("code", data.get("StatusCode"))
        msg = str(data.get("msg") or data.get("StatusMessage") or "").strip()
        if code in (0, "0") or (code is None and not msg):
            return
        low = msg.lower()
        if any(h in low for h in FeishuClient.RETRYABLE_HINTS):
            raise FeishuError(f"飞书限流：{msg[:120]}")
        if code in (19021, 19022, 19024):  # 签名校验失败族
            raise FeishuAuthError(f"飞书签名错误：{msg[:120]}")
        raise FeishuError(f"飞书返回错误 code={code}: {msg[:160]}")

    def send_card(self, card: dict) -> None:
        """发卡片。未配置 → FeishuConfigError（worker 记为不可重试）。"""
        if not self.configured:
            raise FeishuConfigError("未配置飞书 webhook_url")
        self._check(self._post(self.build_payload(card)))
