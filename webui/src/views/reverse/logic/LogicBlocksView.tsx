import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { LogicBlock, LogicBlockFunc, LogicBlockSummary } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { fmtDateTime, utcTitle } from "@/lib/datetime"
import { MarkdownView } from "@/components/settings/MarkdownView"

// 业务逻辑块视图（逆向第四页签）：函数协作/业务语义笔记，与攻击链（因果路径）、
// 蓝图（重建管线）并列的第三种载体。人机共写：人工在此建块/挂函数/补描述，
// Agent 经 bb_logic_block_* 工具产出——详情 4s 轮询让双方改动实时可见。

function FuncRow({ f, onLocate, onPatchRole, onRemove }: {
  f: LogicBlockFunc
  onLocate: (sha: string, addr: string) => void
  onPatchRole: (address: string, role: string) => void
  onRemove: (address: string) => void
}) {
  const [role, setRole] = useState(f.role)
  useEffect(() => setRole(f.role), [f.role])
  const label = f.func_name || f.address
  return (
    <div className="flex items-center gap-2 rounded-lg border px-2 py-1.5">
      <button
        type="button"
        onClick={() => onLocate("", f.address)}
        className="shrink-0 rounded bg-accent px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-primary"
        title="回逆向分析视图定位该函数"
      >
        {f.address}
      </button>
      <span className="min-w-0 max-w-36 truncate text-xs" title={label}>{label}</span>
      <input
        value={role}
        onChange={(e) => setRole(e.target.value)}
        onBlur={() => { if (role !== f.role) onPatchRole(f.address, role) }}
        onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur() }}
        placeholder="角色注：该函数在此块中的职责一句话"
        className="min-w-0 flex-1 rounded border-none bg-transparent px-1 py-0.5 text-[11px] outline-none placeholder:text-muted-foreground/60 focus:bg-card"
      />
      <button
        type="button" onClick={() => onRemove(f.address)}
        className="shrink-0 px-1 text-[10px] text-muted-foreground hover:text-(--status-error)"
        title="从本块摘除该函数（不动 func_kb）"
      >
        摘除
      </button>
    </div>
  )
}

export function LogicBlocksView({ pid, tick, sha, onLocate }: {
  pid: string
  tick: number
  /** 当前工作台样本：新建块默认绑定；为空可建项目级块（不可挂函数） */
  sha: string | null
  /** 点挂接函数地址 → 工作台切回分析视图定位 */
  onLocate: (sha: string, addr: string) => void
}) {
  const [items, setItems] = useState<LogicBlockSummary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<LogicBlock | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 新建表单
  const [creating, setCreating] = useState(false)
  const [nName, setNName] = useState("")
  const [nSha, setNSha] = useState("")
  const [nDesc, setNDesc] = useState("")

  // 详情编辑（块名+描述一起）
  const [editing, setEditing] = useState(false)
  const [nameDraft, setNameDraft] = useState("")
  const [descDraft, setDescDraft] = useState("")

  // 增挂函数
  const [newAddr, setNewAddr] = useState("")
  const [newRole, setNewRole] = useState("")

  // 删除二次确认
  const [delArmed, setDelArmed] = useState(false)

  const refresh = useCallback(() => {
    api.logicBlocks(pid).then((rs) => {
      setItems(rs)
      setSelected((cur) => (cur && rs.some((r) => r.id === cur) ? cur : rs[0]?.id ?? null))
    }).catch((e) => setError(String(e)))
  }, [pid])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh, tick])

  // 详情独立轮询：Agent 经工具增量写（挂函数/补描述）实时可见
  useEffect(() => {
    if (!selected) { setDetail(null); return }
    let alive = true
    const load = () => api.logicBlock(pid, selected)
      .then((d) => { if (alive) { setDetail(d); setError(null) } })
      .catch((e) => { if (alive) setError(String(e)) })
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, selected])

  useEffect(() => { setEditing(false); setDelArmed(false) }, [selected])

  const create = async () => {
    const name = nName.trim()
    if (!name) return
    setError(null)
    try {
      const d = await api.createLogicBlock(pid, {
        name, description: nDesc, binary_sha256: nSha.trim(),
      })
      setCreating(false)
      setNName(""); setNSha(""); setNDesc("")
      setSelected(d.id)
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const saveDetail = async () => {
    if (!detail) return
    setError(null)
    try {
      await api.updateLogicBlock(pid, detail.id, {
        name: nameDraft.trim() || detail.name, description: descDraft,
      })
      setEditing(false)
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const remove = async () => {
    if (!detail) return
    if (!delArmed) { setDelArmed(true); return }
    setError(null)
    try {
      await api.deleteLogicBlock(pid, detail.id)
      setDelArmed(false)
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const addFunc = async () => {
    if (!detail || !newAddr.trim()) return
    setError(null)
    try {
      await api.addLogicBlockFunc(pid, detail.id, {
        address: newAddr.trim(), role: newRole,
      })
      setNewAddr(""); setNewRole("")
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const patchRole = async (address: string, role: string) => {
    if (!detail) return
    setError(null)
    try {
      await api.updateLogicBlockFunc(pid, detail.id, address, role)
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const removeFunc = async (address: string) => {
    if (!detail) return
    setError(null)
    try {
      await api.removeLogicBlockFunc(pid, detail.id, address)
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const locate = (s: string, addr: string) => {
    // 块绑定的样本就是详情里的 sha（行级不带），回跳时优先用块 sha
    onLocate(detail?.binary_sha256 || s, addr)
  }

  return (
    <div className="flex h-full min-h-0">
      {/* 左：业务块列表 */}
      <div className="w-72 shrink-0 overflow-auto border-r">
        <div className="border-b px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-xs font-medium">业务逻辑块</h2>
            <button
              type="button" onClick={() => { setCreating((v) => !v); setNSha(sha ?? "") }}
              className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-primary hover:bg-accent"
            >
              {creating ? "收起" : "+ 新建块"}
            </button>
          </div>
          <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
            一个块 = 一项可讲述的业务功能（存档校验/金币结算…）
          </p>
        </div>
        {creating && (
          <div className="space-y-1.5 border-b px-3 py-2">
            <input
              value={nName} onChange={(e) => setNName(e.target.value)}
              placeholder="块名（如：存档校验）"
              className="w-full rounded border bg-card px-2 py-1 text-[11px] outline-none focus:ring-1 focus:ring-ring"
              onKeyDown={(e) => { if (e.key === "Enter") create() }}
            />
            <input
              value={nSha} onChange={(e) => setNSha(e.target.value)}
              placeholder="样本 sha256（留空=项目级块，不可挂函数）"
              className="w-full rounded border bg-card px-2 py-1 font-mono text-[10px] outline-none focus:ring-1 focus:ring-ring"
            />
            <textarea
              value={nDesc} onChange={(e) => setNDesc(e.target.value)}
              placeholder="业务逻辑描述（markdown，可空）"
              rows={3}
              className="w-full resize-y rounded border bg-card px-2 py-1 text-[11px] outline-none focus:ring-1 focus:ring-ring"
            />
            <div className="flex justify-end gap-1">
              <Button size="sm" variant="outline" onClick={() => setCreating(false)}>取消</Button>
              <Button size="sm" onClick={create} disabled={!nName.trim()}>建块</Button>
            </div>
          </div>
        )}
        {items.length === 0 && !creating && (
          <p className="p-4 text-[11px] leading-relaxed text-muted-foreground">
            尚无业务逻辑块。记录函数协作构成的业务功能（函数逻辑/业务逻辑/逆向破解/
            游戏业务理解）；Agent 分析中也会经 bb_logic_block_* 工具产出。
            PWN/漏洞利用登记走攻击链，重建管线走蓝图。
          </p>
        )}
        {items.map((b) => (
          <button
            key={b.id} type="button" onClick={() => setSelected(b.id)}
            className={cn("block w-full border-b px-3 py-2 text-left hover:bg-accent",
              b.id === selected && "bg-accent")}
          >
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-xs font-medium">{b.name}</span>
              {!b.binary_sha256 && (
                <span className="shrink-0 rounded bg-muted px-1 text-[9px] text-muted-foreground">项目级</span>
              )}
            </div>
            <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
              <span>{b.func_count} 函数</span>
              <span title={utcTitle(b.updated_at)}>{fmtDateTime(b.updated_at)}</span>
            </div>
          </button>
        ))}
      </div>

      {/* 右：块详情 */}
      <div className="min-w-0 flex-1 overflow-auto">
        {error && <p className="p-3 text-xs text-(--status-error)">{error}</p>}
        {!detail ? (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
            从左侧选择业务块，或新建一块
          </div>
        ) : (
          <div className="space-y-4 p-4">
            <div className="flex flex-wrap items-center gap-2">
              {editing ? (
                <input
                  value={nameDraft} onChange={(e) => setNameDraft(e.target.value)}
                  className="min-w-0 flex-1 rounded border bg-card px-2 py-1 text-base font-semibold outline-none focus:ring-1 focus:ring-ring"
                />
              ) : (
                <h1 className="text-base font-semibold">{detail.name}</h1>
              )}
              <span className="flex-1" />
              {detail.binary_sha256 ? (
                <span className="font-mono text-[10px] text-muted-foreground" title="目标样本 sha256">
                  sha256 {detail.binary_sha256.slice(0, 12)}…
                </span>
              ) : (
                <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">项目级块</span>
              )}
              {editing ? (
                <>
                  <Button size="sm" variant="outline" onClick={() => setEditing(false)}>取消</Button>
                  <Button size="sm" onClick={saveDetail}>保存</Button>
                </>
              ) : (
                <>
                  <Button size="sm" variant="outline" onClick={() => {
                    setNameDraft(detail.name); setDescDraft(detail.description); setEditing(true)
                  }}>
                    编辑
                  </Button>
                  <Button size="sm" variant="outline"
                          className={cn(delArmed && "text-(--status-error)")}
                          onClick={remove}>
                    {delArmed ? "确认删除？" : "删除"}
                  </Button>
                </>
              )}
            </div>

            <div>
              <h2 className="mb-1 text-xs font-medium text-muted-foreground">业务逻辑描述</h2>
              {editing ? (
                <textarea
                  value={descDraft} onChange={(e) => setDescDraft(e.target.value)}
                  rows={8}
                  placeholder="触发时机 / 输入输出 / 状态流转 / 与其他块的关系（markdown）"
                  className="w-full resize-y rounded-lg border bg-card p-3 font-mono text-[11px] outline-none focus:ring-1 focus:ring-ring"
                />
              ) : detail.description ? (
                <div className="rounded-lg border p-4">
                  <MarkdownView content={detail.description} prefix={`lb-${detail.id}`} />
                </div>
              ) : (
                <p className="rounded-lg border border-dashed p-4 text-[11px] text-muted-foreground">
                  暂无描述——点「编辑」补一段（触发时机/输入输出/状态流转）；Agent 深析后也会回填。
                </p>
              )}
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between">
                <h2 className="text-xs font-medium text-muted-foreground">
                  挂接函数（{detail.funcs?.length ?? 0}）
                </h2>
              </div>
              {detail.binary_sha256 ? (
                <>
                  <div className="mb-2 flex gap-1.5">
                    <input
                      value={newAddr} onChange={(e) => setNewAddr(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") addFunc() }}
                      placeholder="0x140001000"
                      className="w-40 rounded border bg-card px-2 py-1 font-mono text-[11px] outline-none focus:ring-1 focus:ring-ring"
                    />
                    <input
                      value={newRole} onChange={(e) => setNewRole(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") addFunc() }}
                      placeholder="角色注（可空，挂后可改）"
                      className="min-w-0 flex-1 rounded border bg-card px-2 py-1 text-[11px] outline-none focus:ring-1 focus:ring-ring"
                    />
                    <Button size="sm" variant="outline" onClick={addFunc} disabled={!newAddr.trim()}>
                      挂接
                    </Button>
                  </div>
                  <div className="space-y-1.5">
                    {(detail.funcs ?? []).map((f) => (
                      <FuncRow key={f.address} f={f} onLocate={locate}
                               onPatchRole={patchRole} onRemove={removeFunc} />
                    ))}
                    {(detail.funcs?.length ?? 0) === 0 && (
                      <p className="rounded-lg border border-dashed p-3 text-[11px] text-muted-foreground">
                        尚未挂接函数。填地址挂接，或在分析视图点「加入业务块」；
                        仅 func_kb 已登记的函数可挂（先 decompile 入库）。
                      </p>
                    )}
                  </div>
                </>
              ) : (
                <p className="rounded-lg border border-dashed p-3 text-[11px] text-muted-foreground">
                  项目级块不挂函数——如需挂接请删除后新建样本级块。
                </p>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
