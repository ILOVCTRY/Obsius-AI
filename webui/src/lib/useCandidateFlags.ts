import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { ChallengeFlag } from "@/lib/types"

/** 候选 flag 数据源（ctf-flag-wake，2026-10-20）。
 *
 *  子代理提交候选 flag 时后端会唤醒 CTF 解题主控说一段话，本 hook 给那句话配一张
 *  「复制 / 确认」卡。**卡片数据取台账，不取事件载荷**：事件里的 flag 被截到 120
 *  字符（`record_flag` 有意为之，防事件表膨胀），拿它渲染会出现半截 flag——而
 *  「复制」功能复制出半截串比没有更糟。台账端点给完整串。
 *
 *  `wakeKey` 变化即立即重拉（对话页监听到 `challenge.flag.recorded` 时传事件 id），
 *  让卡片「即弹」而不等下一拍轮询。轮询间隔 3s 与 `usePendingApprovals` 同口径。
 */
export function useCandidateFlags(challengeId: string | null | undefined,
                                  wakeKey: string | number = 0) {
  const [flags, setFlags] = useState<ChallengeFlag[]>([])
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(() => {
    if (!challengeId) { setFlags([]); return }
    setLoading(true)
    api.challengeFlags(challengeId)
      .then((d) => setFlags((d.flags ?? []).filter((f) => f.verdict === "pending")))
      .catch(() => { /* 轮询失败静默，下一拍重试 */ })
      .finally(() => setLoading(false))
  }, [challengeId])

  useEffect(() => {
    if (!challengeId) { setFlags([]); return }
    let alive = true
    const tick = () => {
      api.challengeFlags(challengeId)
        .then((d) => { if (alive) setFlags((d.flags ?? []).filter((f) => f.verdict === "pending")) })
        .catch(() => { /* 同上 */ })
    }
    tick()
    const t = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [challengeId, wakeKey])

  return { flags, loading, refresh }
}
