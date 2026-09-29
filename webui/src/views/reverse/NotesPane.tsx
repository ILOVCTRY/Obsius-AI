import { useEffect, useRef, useState } from "react"
import { Loader2, FileUp } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import type { FuncEntry, LogicBlockSummary, WritebackItem, WritebackResult } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"

// 右栏 tab：人机共写 func_kb。改名（入 name_history）/ risk_tags 全量替换 / 笔记分段追加。
// confidence 是 AI 字段，这里不动；缓存中尚无 kb 行时先 POST /funcs 建行再 PATCH。
// P2：func_kb 命名/注释可 headless 写回 IDA .i64（锁文件=GUI 占用，不强写）。

interface Props {
  pid: string
  sha: string
  addr: string
  cacheName: string | null
  kb: FuncEntry | null
  onSaved: () => void
  /** MCP 在线时后端自动选路：实时写 IDA 当前库（无锁、不存盘）；离线走 headless 写 .i64 */
  mcpLive?: boolean
}

/** 取分析笔记首段（跳过 ## 标题行与空行）作为写回 IDA 的函数注释。 */
function firstNoteParagraph(analysis: string): string {
  for (const block of analysis.split(/\n\s*\n/)) {
    const lines = block.split("\n").map((l) => l.trim())
      .filter((l) => l && !l.startsWith("## "))
    if (lines.length) return lines.join("\n").slice(0, 2000)
  }
  return ""
}

function writebackMessage(res: WritebackResult): string {
  switch (res.status) {
    case "ok":
      return res.channel === "mcp"
        ? `已实时写入当前 IDA 库（${res.applied ?? 0} 处），切到 IDA 即可见；库存盘由你在 IDA 中完成`
        : `已写回 IDA（${res.applied ?? 0} 处）；重新打开库可见`
    case "locked":
      return "IDA 正开着该库：请先关闭 IDA 中的该库，或启用 MCP 实时模式"
    case "no-db":
      return "尚无 IDA 数据库，请先完成 headless 分诊"
    default:
      return res.guidance ?? `写回未执行：${res.status}`
  }
}

export function NotesPane({ pid, sha, addr, cacheName, kb, onSaved, mcpLive = false }: Props) {
  const [name, setName] = useState(kb?.name ?? cacheName ?? "")
  const [tags, setTags] = useState((kb?.risk_tags ?? []).join(", "))
  const [note, setNote] = useState("")
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [withComment, setWithComment] = useState(false)
  const [wbBusy, setWbBusy] = useState(false)
  const [wbMsg, setWbMsg] = useState<string | null>(null)

  // 加入业务块（逆向第四页签）：列表 4s 轮询与 Agent 产出同步
  const [lbItems, setLbItems] = useState<LogicBlockSummary[]>([])
  const [lbSel, setLbSel] = useState("")
  const [lbRole, setLbRole] = useState("")
  const [lbBusy, setLbBusy] = useState(false)
  const [lbMsg, setLbMsg] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => api.logicBlocks(pid, sha)
      .then((rs) => { if (alive) setLbItems(rs) })
      .catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, sha])

  // 只有切函数才全量重置；同函数的 4s 轮询刷新（kb 引用每轮更新）不得清空
  // 正在编辑的表单、勾选态或写回结果文案。
  const prevAddr = useRef(addr)
  useEffect(() => {
    if (prevAddr.current === addr) return
    prevAddr.current = addr
    setName(kb?.name ?? cacheName ?? "")
    setTags((kb?.risk_tags ?? []).join(", "))
    setNote("")
    setMsg(null)
    setWbMsg(null)
    setWithComment(false)
    setLbSel("")
    setLbRole("")
    setLbMsg(null)
    // 切函数瞬间取最新 kb/cacheName 填表单；同函数刷新有意不重跑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addr])

  // 首次挂到一个尚无 kb 的函数、kb 随后到达时，补一次名字占位（仅空表单）
  useEffect(() => {
    if (prevAddr.current !== addr) return
    if (!name && (kb?.name || cacheName)) {
      setName(kb?.name ?? cacheName ?? "")
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kb, cacheName])

  const save = async () => {
    setSaving(true)
    setMsg(null)
    try {
      let entry = kb
      const finalName = name.trim() || cacheName || `sub_${addr.replace(/^0x/, "")}`
      if (!entry) {
        // 为仅缓存函数补建 func_kb 行（人写笔记的前置）
        entry = await api.createFunc(pid, {
          binary_sha256: sha, address: addr, name: finalName, analysis: "",
        })
      }
      const tagList = tags.split(/[,，]/).map((t) => t.trim()).filter(Boolean)
      const body: { name?: string; note?: string; risk_tags: string[] } = { risk_tags: tagList }
      if (entry.name !== finalName) body.name = finalName
      if (note.trim()) body.note = note.trim()
      await api.patchFunc(pid, entry.id, body)
      setNote("")
      setMsg("已保存（笔记按时间分段追加）")
      onSaved()
    } catch (e) {
      setMsg(String(e))
    } finally {
      setSaving(false)
    }
  }

  const writeback = async () => {
    if (!kb) return
    const finalName = name.trim()
    if (!finalName) {
      setWbMsg("先填函数名再写回")
      return
    }
    setWbBusy(true)
    setWbMsg(null)
    try {
      const item: WritebackItem = { address: addr, name: finalName }
      if (withComment) {
        const comment = firstNoteParagraph(kb.analysis ?? "")
        if (comment) item.comment = comment
      }
      const r = await api.writeback(pid, sha, [item])
      const job = await pollJob(r.job_id, () => {}, 1500)
      const res = job.result as WritebackResult | null
      if (job.status === "error") setWbMsg(`写回失败：${job.error}`)
      else if (!res) setWbMsg("写回失败：无结果")
      else setWbMsg(writebackMessage(res))
    } catch (e) {
      setWbMsg(String(e))
    } finally {
      setWbBusy(false)
    }
  }

  const commentReady = !!firstNoteParagraph(kb?.analysis ?? "")

  // 加入业务块：挂接前确保函数已在 func_kb（无行先建，照 save 先例——
  // 后端挂接校验 (sha, address) 必须已登记，这里顺手补齐入库）
  const addToBlock = async () => {
    if (!lbSel) return
    setLbBusy(true)
    setLbMsg(null)
    try {
      if (!kb) {
        const finalName = name.trim() || cacheName || `sub_${addr.replace(/^0x/, "")}`
        await api.createFunc(pid, {
          binary_sha256: sha, address: addr, name: finalName, analysis: "",
        })
      }
      await api.addLogicBlockFunc(pid, lbSel, { address: addr, role: lbRole })
      const b = lbItems.find((x) => x.id === lbSel)
      setLbMsg(`已加入「${b?.name ?? lbSel}」`)
      setLbRole("")
      onSaved()
    } catch (e) {
      setLbMsg(String(e))
    } finally {
      setLbBusy(false)
    }
  }

  return (
    <div className="space-y-2 p-2">
      <label className="block">
        <span className="mb-0.5 block text-[10px] text-muted-foreground">函数名（改名记入 name_history）</span>
        <Input value={name} onChange={(e) => setName(e.target.value)}
               className="h-7 font-mono text-[11px]" placeholder={cacheName ?? addr} />
      </label>
      <label className="block">
        <span className="mb-0.5 block text-[10px] text-muted-foreground">风险标签（逗号分隔，全量替换语义；清空=无标签）</span>
        <Input value={tags} onChange={(e) => setTags(e.target.value)}
               className="h-7 font-mono text-[11px]" placeholder="如 crypto, anti-debug" />
      </label>
      <label className="block">
        <span className="mb-0.5 block text-[10px] text-muted-foreground">
          结论笔记（追加为「## 笔记 时间 by human」分段；客观全量在 headless 缓存，这里只写理解）
        </span>
        <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={6}
                  className="text-[11px]" placeholder="这个函数做什么 / 关键分支 / 与发现的关联…" />
      </label>
      <Button size="sm" className="w-full gap-1 text-[11px]" onClick={save}
              disabled={saving || (!note.trim() && !name.trim() && !tags.trim())}>
        {saving && <Loader2 className="size-3 animate-spin" />}保存到 func_kb
      </Button>
      {msg && <p className="break-all font-mono text-[10px] text-muted-foreground">{msg}</p>}

      {/* P2：写回 IDA .i64（必须先有 kb 行；注释默认不带，避免覆盖 IDA 里的手工注释） */}
      <div className="border-t pt-2">
        <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <input type="checkbox" checked={withComment}
                 disabled={!kb || !commentReady}
                 onChange={(e) => setWithComment(e.target.checked)} />
          把分析笔记首段作为函数注释{kb && !commentReady && "（暂无笔记）"}
        </label>
        <Button size="sm" variant="outline" className="mt-1 w-full gap-1 text-[11px]"
                onClick={writeback}
                disabled={wbBusy || !kb || !name.trim()}
                title={!kb
                  ? "先把该函数保存进 func_kb，再写回 IDA"
                  : mcpLive
                    ? "实时写入当前 IDA 打开的库（MCP；无 headless 锁问题，存盘由你在 IDA 中完成）"
                    : "把当前命名/注释写回 IDA 数据库（headless；GUI 开着会被锁挡住）"}>
          {wbBusy ? <Loader2 className="size-3 animate-spin" /> : <FileUp className="size-3" />}
          ⬆ 写回 IDA
        </Button>
        {wbMsg && <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">{wbMsg}</p>}
      </div>

      {/* 加入业务块（「业务逻辑」页签可见；挂接纪律：仅 func_kb 已登记函数） */}
      <div className="border-t pt-2">
        <span className="mb-0.5 block text-[10px] text-muted-foreground">加入业务块</span>
        {lbItems.length === 0 ? (
          <p className="text-[10px] leading-relaxed text-muted-foreground">
            本样本尚无业务块——到「业务逻辑」页签新建，或让 Agent 经 bb_logic_block_create 产出
          </p>
        ) : (
          <div className="space-y-1">
            <select
              value={lbSel} onChange={(e) => setLbSel(e.target.value)}
              className="h-7 w-full rounded border bg-card px-1 text-[11px] outline-none"
            >
              <option value="">选择业务块…</option>
              {lbItems.map((b) => (
                <option key={b.id} value={b.id}>{b.name}</option>
              ))}
            </select>
            <div className="flex gap-1">
              <Input value={lbRole} onChange={(e) => setLbRole(e.target.value)}
                     className="h-7 min-w-0 flex-1 text-[11px]" placeholder="角色注（可空）" />
              <Button size="sm" variant="outline" className="gap-1 text-[11px]"
                      onClick={addToBlock} disabled={lbBusy || !lbSel}>
                {lbBusy && <Loader2 className="size-3 animate-spin" />}
                加入
              </Button>
            </div>
            {lbMsg && <p className="break-all font-mono text-[10px] text-muted-foreground">{lbMsg}</p>}
          </div>
        )}
      </div>
    </div>
  )
}
