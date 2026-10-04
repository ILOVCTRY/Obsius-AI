import { useEffect, useState } from "react"
import { X } from "lucide-react"
import { api } from "@/lib/api"
import type { CoordinationCommunication, Session } from "@/lib/types"

export function CommunicationDrawer({ pid, sessions, open, onClose, onOpenSession }: { pid: string; sessions: Session[]; open: boolean; onClose: () => void; onOpenSession?: (sid: string) => void }) {
  const [sid, setSid] = useState<string | undefined>(undefined)
  const [items, setItems] = useState<CoordinationCommunication[]>([])
  const [text, setText] = useState("")
  const load = () => void api.coordinationCommunications(pid, { session_id: sid, limit: 200 }).then(setItems).catch(() => {})
  useEffect(() => { if (open) load() }, [open, sid, pid])
  if (!open) return null
  const send = async () => { if (!sid || !text.trim()) return; await api.sessionNote(sid, text.trim()); setText(""); load() }
  return <div className="absolute bottom-0 right-0 top-9 z-30 flex w-[min(520px,92%)] flex-col border-l bg-background shadow-2xl"><div className="flex h-10 shrink-0 items-center gap-2 border-b px-3"><b className="text-xs">通信</b><select className="min-w-0 flex-1 rounded border bg-background px-2 py-1 text-[11px]" value={sid ?? ""} onChange={(e) => setSid(e.target.value || undefined)}><option value="">全部会话</option>{sessions.map((s) => <option key={s.id} value={s.id}>{s.name || s.role || s.id}</option>)}</select><button onClick={onClose}><X size={14} /></button></div><div className="flex min-h-0 flex-1 flex-col overflow-auto p-2">{items.map((item) => <button key={item.id} className="mb-1 rounded border p-2 text-left text-xs hover:bg-accent/40" onClick={() => onOpenSession?.(item.to_session)}><div className="flex gap-2 text-[10px] text-muted-foreground"><span>{item.kind}</span><time>{item.created_at.replace("T", " ").slice(0, 16)}</time>{!item.read_at && <b className="text-primary">未读</b>}</div><p className="mt-1 whitespace-pre-wrap text-foreground">{String(item.payload.text ?? item.payload.title ?? item.payload.note ?? item.kind)}</p></button>)}{!items.length && <p className="py-10 text-center text-xs text-muted-foreground">暂无通信记录</p>}</div><div className="flex gap-2 border-t p-2"><textarea value={text} onChange={(e) => setText(e.target.value)} className="min-h-8 flex-1 resize-none rounded border bg-background p-2 text-xs" placeholder={sid ? "发送人工引导…" : "先选择会话"} /><button className="coord-primary" disabled={!sid || !text.trim()} onClick={() => void send()}>发送</button></div></div>
}
