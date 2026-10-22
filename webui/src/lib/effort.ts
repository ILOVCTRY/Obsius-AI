// 思考强度档位（thinking-effort，2026-10-21）：与 core/llm/effort.py **逐行对应**。
//
// 改这里任何一行，必须同步改 `core/llm/effort.py` 的 WIRE/BUDGET 表——两边不一致
// 的后果是「UI 显示的档位 ≠ 实际下发的值」，而这种错在界面上完全看不出来（滑块
// 位置是对的，模型却没变深），只能靠抓请求体才发现。
//
// ## 为什么 UI 是七档、线上只有三档
//
// 网关认识的 `reasoning_effort` 只有 low/high/max。七档是**给人用的刻度**
// （让「最低」和「最高」有明确端点、中间有默认档可回），归并逻辑放后端，
// 前端只负责显示与选择，绝不自己算线上值。

/** 档位全集，顺序即滑块从左到右（弱→强）。长度就是滑块停靠点数。 */
export const EFFORT_GEARS = [
  "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
] as const

export type EffortGear = (typeof EFFORT_GEARS)[number]

/** 缺省档位（与后端 effort.DEFAULT 一致）。未覆写时按这个显示。 */
export const EFFORT_DEFAULT: EffortGear = "high"

/** 档位显示名：中文单字/双字，用于 composer chip 与浮层标题。 */
export const EFFORT_LABEL: Record<EffortGear, string> = {
  minimal: "最低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "较高",
  max: "最高",
  ultra: "极限",
}

/** 档位说明（浮层里当 title + 一行小字），照抄用户给定的档位表原话。
 *  唯一调整：`minimal` 去掉重复的「最低的思考强度」前缀——标签「最低」与提示
 *  是并排渲染的，原话会读成「最低·最低的思考强度」，像口吃。 */
export const EFFORT_HINT: Record<EffortGear, string> = {
  minimal: "几乎不思考，最省时",
  low: "较低的思考强度，适合简单任务",
  medium: "会被自动提升至默认档位",
  high: "默认档位，平衡之选",
  xhigh: "会被自动下调至默认档位",
  max: "最高强度的深度推理",
  ultra: "会被自动提升至最高档位",
}

/** 任意字符串 → 合法档位；空/未知一律落到默认档（与后端 normalize 同口径）。 */
export function normalizeGear(g: string | null | undefined): EffortGear {
  const s = (g ?? "").trim().toLowerCase()
  return (EFFORT_GEARS as readonly string[]).includes(s)
    ? (s as EffortGear) : EFFORT_DEFAULT
}

/** 档位在滑块上的索引（0..6）。 */
export function gearIndex(g: string | null | undefined): number {
  return EFFORT_GEARS.indexOf(normalizeGear(g))
}
