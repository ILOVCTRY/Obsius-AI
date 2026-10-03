// 三栏分割线宽度换算（2026-10-01 去固定上限）：
// 侧栏最大宽 = 容器宽 − 主区保底(WB_MAIN_MIN) − 其余固定占位（兄弟栏 + 分割线 + 边框）。
// el 传自身任一子元素（内部用 closest(".wb-shell") 定位容器）；无容器时返回 Infinity 表示不限制。
// 右栏（发现）与左栏（会话）共用同一口径，保证「保下限、去上限、主区保底」三处一致。

export const WB_MAIN_MIN = 360

export function paneMaxWidth(
  el: HTMLElement | null,
  reserveSelectors: string[] = [],
  extra = 0,
): number {
  const shell = el?.closest(".wb-shell") as HTMLElement | null
  if (!shell) return Number.MAX_SAFE_INTEGER
  let reserved = extra
  for (const sel of reserveSelectors) {
    const n = shell.querySelector<HTMLElement>(sel)
    if (n) reserved += n.getBoundingClientRect().width
  }
  return Math.max(0, Math.floor(shell.clientWidth - WB_MAIN_MIN - reserved))
}
