"""思考强度档位（thinking-effort，2026-10-21）：**七档 UI → 三档线上值**的唯一真源。

## 为什么要有这一层

UI 上有 7 个档位（滑块 7 个停靠点），但上游网关真正认识的值只有 3 个
（`low` / `high` / `max`）。把 `xhigh` 原样发给 OpenAI 兼容接口会被 400 拒掉
——而本仓的 400 降级路径是「整个思考链剥掉重试」，后果是本会话**静默失去
思考**，比降档糟得多。所以在**构造请求前**归并成线上值，而不是让各 provider
自己去猜。

## 档位表（Nan 2026-10-21 给定，挪动任何一行都要同步改 webui/src/lib/effort.ts）

    minimal  low   最低的思考强度
    low      low   较低的思考强度，适合简单任务
    medium   high  会被自动提升至默认档位
    high     high  默认档位，平衡之选
    xhigh    high  会被自动下调至默认档位
    max      max   最高强度的深度推理
    ultra    max   会被自动提升至最高档位

`medium` 与 `xhigh` 落在 `high`、`ultra` 落在 `max`，是刻意的：档位集不连续
时，中间档由**默认档**承接、末端档由**最高档**承接，用户拖到哪一格都能得到
一个确定且可解释的行为，不会出现「这一格无对应值」的空洞。

## Anthropic 通道没有 effort 枚举

Anthropic 协议的深度表达是 `thinking.budget_tokens`（思考预算 token 数），
不是枚举值。故这里另给一张 `BUDGET` 表——它是**同一套档位的另一种编码**，
不是第二套语义。注意协议约束 `budget_tokens < max_tokens`：本仓 2026-09-20
撞过「budget 8192 与 max_tokens 4096 倒挂 → 流截断在工具参数中间」的事故，
所以高预算档在 `anthropic_compat` 里会同步抬高 max_tokens（见 `budget_for`
调用处的注释），而不是只改 budget。
"""

from __future__ import annotations

# 档位全集，顺序即滑块的从左到右（弱 → 强）。前端滑块点数依赖这个长度。
GEARS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

#: 缺省档位。未配置 / 配置了不认识的值都落到这里——与旧行为（默认 high）一致。
DEFAULT = "high"

#: 七档 → 线上值。网关认识的值只有这三种。
WIRE: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}

#: 七档 → Anthropic thinking budget（token）。high=8192 是历史缺省值，不动它
#: （既有行为与测试都按这个数），其余档以它为中心上下铺开。
BUDGET: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 12288,
    "max": 16384,
    "ultra": 32768,
}

#: 线上值的强度序（仅用于文档/一致性断言，不再参与降级决策）。
_WIRE_RANK = {"low": 0, "high": 1, "max": 2}


def normalize(gear: str | None) -> str:
    """任意输入 → 合法档位。空值 / 未知值 / 大小写混写一律落到 `DEFAULT`。

    刻意**不抛异常**：档位存在 DB 与 providers.json 里，配置被手改坏或来自
    旧版本时，抛错会让整轮会话起不来；降级到默认档是更合理的失败姿态
    （与 providers.py 白名单过滤同口径）。
    """
    g = str(gear or "").strip().lower()
    return g if g in WIRE else DEFAULT


def wire(gear: str | None) -> str:
    """档位 → 下发给 OpenAI 兼容接口的 `reasoning_effort` / `reasoning.effort`。"""
    return WIRE[normalize(gear)]


def budget_for(gear: str | None) -> int:
    """档位 → Anthropic `thinking.budget_tokens`。"""
    return BUDGET[normalize(gear)]


def downgrade(gear: str | None) -> str | None:
    """线上值认不出时往下降一档；没有可降的则返回 None（调用方据此彻底剥掉思考参数）。

    只用于「网关 400 拒收 effort」的补救路径。**只降一档**且只在 `max` 上降：
    OpenAI 原生枚举只到 `high`，不少兼容网关照抄这个范围，`max` 会被拒；
    降成 `high` 重试一次通常就过。而 `high` 被拒说明这家根本不支持 reasoning
    参数——此时再降到 `low` 也是白试一轮，直接返回 None 让调用方剥参。
    """
    return "high" if wire(gear) == "max" else None
