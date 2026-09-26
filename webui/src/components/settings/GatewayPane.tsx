import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { CapabilityInventory, GatewayConfig } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

// 设置页「网关」tab（gateway-config-view M1，DESIGN §7）：执行网关策略只读快照
// + 宿主能力探测实况（app.state.inventory）。单一事实源是代码，页面永不漂移；
// 安全策略永不前端可编辑（改规则走代码+测试，页签底部红线注记）。

const LEVEL_BADGE: Record<number, string> = {
  0: "L0", 1: "L1", 2: "L2", 3: "L3",
}

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        ok ? "bg-(--status-ok)" : "bg-(--status-error)",
      )}
      title={ok ? "可用" : "不可用"}
    />
  )
}

export function GatewayPane({ pid }: { pid?: string | null }) {
  const [cfg, setCfg] = useState<GatewayConfig | null>(null)
  const [inv, setInv] = useState<CapabilityInventory | null>(null)
  const [probing, setProbing] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(() => {
    api.gatewayConfig().then(setCfg).catch((e) => setErr(String(e)))
    // 探测实况初始值复用项目详情已返回的 capability（settings 全局页，无项目时留空）。
    // 形状防御：capability 须为 {docker,wsl,tools} 同构对象（旧后端返回 JSON 字符串则解析），
    // 缺键一律置空——渲染期 undefined.available 会整树崩溃黑屏。
    if (pid) {
      api
        .getProject(pid)
        .then((d) => {
          const raw = d.capability as unknown
          let parsed = raw
          if (typeof raw === "string") {
            try { parsed = JSON.parse(raw) } catch { parsed = null }
          }
          const cap = parsed as CapabilityInventory | null
          setInv(cap && cap.docker && cap.wsl ? cap : null)
        })
        .catch(() => {})
    }
  }, [pid])
  useEffect(() => { reload() }, [reload])

  const probeNow = async () => {
    setProbing(true)
    setErr(null)
    try {
      setInv(await api.gatewayProbe())
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setProbing(false)
    }
  }

  if (!cfg) {
    return <div className="p-4 text-xs text-muted-foreground">{err ?? "加载中…"}</div>
  }

  const dockerOk = inv?.docker?.available ?? null
  const wslOk = inv?.wsl?.available ?? null
  const hardWarn = dockerOk === false
  const softWarn = dockerOk !== false && wslOk === false

  // 四通道可用性：host 恒可用；sandbox 经 Docker 承载（fakenet 后续里程碑）
  const channels: { name: string; level: number; ok: boolean | null; detail: string }[] = [
    { name: "host", level: 0, ok: true, detail: "宿主原生执行（网关审计 + 策略校验）" },
    { name: "wsl", level: 1, ok: wslOk, detail: inv?.wsl?.detail || "与宿主同信任级" },
    { name: "docker", level: 2, ok: dockerOk, detail: inv?.docker?.detail || "未探测" },
    { name: "sandbox", level: 3, ok: dockerOk, detail: "经 Docker 承载（加固沙箱）" },
  ]

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4 text-xs">
      {/* 顶部告警条（结论化核心）：docker 不可用=L2/L3 硬伤；wsl 不可用=弱提示 */}
      {hardWarn ? (
        <div className="mb-3 rounded border border-(--status-warning)/50 bg-(--status-warning)/10 p-2.5 text-(--status-warning)">
          ⚠ L2/L3 通道不可用：不可信代码与恶意样本任务将被拒绝（宁严勿松：拒绝不降级 host）。
          若 Docker Desktop 在平台启动后才开启，点「重新探测」刷新。
        </div>
      ) : softWarn ? (
        <div className="mb-3 rounded border border-(--status-warning)/30 bg-(--status-warning)/5 p-2.5 text-(--status-warning)">
          WSL2 不可用：L1 通道降级（wsl 与 host 同信任级，影响有限，host 仍可用）。
        </div>
      ) : (
        <div className="mb-3 rounded border border-(--status-ok)/30 bg-(--status-ok)/5 p-2.5 text-(--status-ok)">
          {dockerOk === null ? "探测实况未加载（打开项目或点「重新探测」）" : "四通道就绪"}
        </div>
      )}

      {/* 四通道卡 */}
      <div className="mb-4 overflow-hidden rounded border">
        <div className="flex items-center justify-between border-b bg-muted/30 px-3 py-1.5">
          <span className="font-medium">执行通道（detector 实测）</span>
          <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]"
                  disabled={probing} onClick={probeNow}>
            {probing ? "探测中…（最坏 ~20s）" : "重新探测"}
          </Button>
        </div>
        {channels.map((c) => (
          <div key={c.name} className="flex items-center gap-3 border-b px-3 py-2 last:border-b-0">
            <span className="w-9 shrink-0 rounded bg-muted px-1.5 py-0.5 text-center font-mono text-[10px]">
              {LEVEL_BADGE[c.level]}
            </span>
            <span className="w-16 shrink-0 font-medium">{c.name}</span>
            <span className="min-w-0 flex-1 truncate text-muted-foreground" title={c.detail}>{c.detail}</span>
            {c.ok === null
              ? <span className="shrink-0 text-[10px] text-muted-foreground">未探测</span>
              : <Dot ok={c.ok} />}
          </div>
        ))}
      </div>

      {/* 工具探测清单（inventory.tools；工具面板最终归 toolchain-registry，此处只读快照为临时归宿） */}
      {inv?.tools && inv.tools.length > 0 && (
        <div className="mb-4 overflow-hidden rounded border">
          <div className="border-b bg-muted/30 px-3 py-1.5 font-medium">工具探测清单（tools/**/manifest.yaml）</div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 px-3 py-2">
            {inv.tools.map((t) => (
              <span key={t.name} className="flex items-center gap-1.5">
                <Dot ok={t.available} />
                <span className={t.available ? "" : "text-muted-foreground"}>{t.name}</span>
                {!t.available && t.detail && (
                  <span className="text-[10px] text-muted-foreground">({t.detail})</span>
                )}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* threat × runtime 放行矩阵 */}
      <div className="mb-4 overflow-hidden rounded border">
        <div className="border-b bg-muted/30 px-3 py-1.5 font-medium">威胁等级 × 运行时放行矩阵</div>
        <table className="w-full">
          <tbody>
            {cfg.threat_matrix.map((r) => (
              <tr key={r.threat_class} className="border-b last:border-b-0">
                <td className="w-28 px-3 py-1.5 align-top font-mono">{r.threat_class}</td>
                <td className="w-52 px-3 py-1.5 align-top">
                  <span className="flex flex-wrap gap-1">
                    {r.allowed.map((a) => (
                      <span key={a} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{a}</span>
                    ))}
                  </span>
                </td>
                <td className="px-3 py-1.5 text-muted-foreground">{r.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 网络模式 */}
      <div className="mb-4 rounded border px-3 py-2">
        <span className="font-medium">L3 沙箱网络模式：</span>
        <span className="ml-2 inline-flex flex-wrap gap-1">
          {cfg.net_modes.modes.map((m) => (
            <span key={m} className={cn(
              "rounded px-1.5 py-0.5 font-mono text-[10px]",
              m === cfg.net_modes.default ? "bg-(--status-ok)/15 text-(--status-ok)" : "bg-muted",
            )}>
              {m}{m === "real" ? "（须审批）" : ""}{m === cfg.net_modes.default ? "（默认）" : ""}
            </span>
          ))}
        </span>
        <span className="ml-2 text-muted-foreground">{cfg.net_modes.note}</span>
      </div>

      {/* pathguard 语义摘要 */}
      <div className="mb-4 overflow-hidden rounded border">
        <div className="border-b bg-muted/30 px-3 py-1.5 font-medium">路径守卫（pathguard）语义摘要</div>
        <ul className="list-disc space-y-0.5 px-8 py-2">
          {cfg.pathguard_rules.map((r, i) => <li key={i}>{r}</li>)}
        </ul>
      </div>

      {/* rateguard 规则表 */}
      <div className="mb-4 overflow-hidden rounded border">
        <div className="border-b bg-muted/30 px-3 py-1.5 font-medium">扫描限速纪律（rateguard）</div>
        <table className="w-full">
          <tbody>
            {cfg.rate_rules.map((r) => (
              <tr key={r.tool} className="border-b last:border-b-0">
                <td className="w-20 px-3 py-1.5 align-top font-mono">{r.tool}</td>
                <td className="px-3 py-1.5 align-top">{r.requirement}</td>
                <td className="w-32 px-3 py-1.5 align-top">
                  <span className="flex flex-wrap gap-1">
                    {r.params.map((p) => (
                      <span key={p} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{p}</span>
                    ))}
                  </span>
                </td>
                <td className="px-3 py-1.5 align-top text-muted-foreground">{r.hint}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 执行参数 */}
      <div className="mb-4 rounded border px-3 py-2">
        <span className="font-medium">执行参数：</span>
        <span className="ml-2 text-muted-foreground">
          default_timeout={cfg.exec_params.default_timeout}s；
          sandbox_image={cfg.exec_params.sandbox_image}
        </span>
      </div>

      {/* 底部红线注记 */}
      <div className="rounded border border-(--status-approval)/40 bg-(--status-approval)/10 p-2.5 text-(--status-approval)">
        安全策略为代码内审计边界（THREAT_ALLOWED / unknown→按恶意 / real 须审批等宁严勿松规则永不前端可编辑）——
        改规则必须走代码 + 测试，本页只读快照。
      </div>
    </div>
  )
}
