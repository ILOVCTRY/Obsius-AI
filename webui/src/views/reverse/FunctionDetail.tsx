import { useState } from "react"
import { ExternalLink, Bug, Copy, Check } from "lucide-react"
import { api } from "@/lib/api"
import type { CachedFunction, FuncEntry } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { x64dbgScript } from "./x64dbg"
import { ceScript } from "./ce"
import { AddToChainButton } from "./chains/AddToChainDialog"
import { fmtDateTime, utcTitle } from "@/lib/datetime"

// 中栏：函数结论（func_kb 人机共写）+ headless 伪码 + 外跳工具（IDA 精确跳址 / 脚本档）。

function CopyBtn({ text, label = "复制" }: { text: string; label?: string }) {
  const [ok, setOk] = useState(false)
  return (
    <Button
      size="sm" variant="outline" className="h-6 gap-1 px-2 text-[10px]"
      onClick={() => { navigator.clipboard.writeText(text).catch(() => {}); setOk(true); setTimeout(() => setOk(false), 1500) }}
    >
      {ok ? <Check className="size-3" /> : <Copy className="size-3" />}{ok ? "已复制" : label}
    </Button>
  )
}

interface Props {
  pid: string
  sha: string
  addr: string
  detail: CachedFunction | null
  loading: boolean
  kb: FuncEntry | null
  bits: number
  hasDb: boolean
  imagebase?: string | null
  moduleName?: string | null
  /** MCP 实时桥在线（overview 真灯）；缓存缺席时伪码/xref 走 IDA 当前库实时取 */
  mcpLive?: boolean
  /** headless 全量缓存已就绪（overview.cached） */
  cached?: boolean
}

export function FunctionDetail({
  pid, sha, addr, detail, loading, kb, bits, hasDb, imagebase, moduleName,
  mcpLive = false, cached = true,
}: Props) {
  const [scriptOpen, setScriptOpen] = useState(false)
  const [idaMsg, setIdaMsg] = useState<string | null>(null)
  // 伪码 / 反汇编双 tab（2026-09-30：IDA 拉取样本按需详情自动落盘后两样都有）
  const [codeTab, setCodeTab] = useState("pseudo")
  const displayName = kb?.name || detail?.name || addr
  const funcName = detail?.name ?? kb?.name ?? undefined
  const x64 = x64dbgScript(addr, bits, funcName)
  const ce = ceScript({ moduleName: moduleName ?? undefined, addr, imagebase: imagebase ?? undefined,
                         bits, name: funcName })

  const openIda = async () => {
    setIdaMsg(null)
    try {
      const r = await api.openInIda(pid, sha, addr)
      setIdaMsg(`正在打开 IDA 并跳到 ${r.jumping ?? addr}：${r.opening}`)
    } catch (e) {
      setIdaMsg(String(e))
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 标题行 */}
      <div className="shrink-0 border-b px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm text-primary">{displayName}</span>
          <span className="font-mono text-[10px] text-muted-foreground">{addr}</span>
          {detail?.size ? <span className="font-mono text-[10px] text-muted-foreground">{detail.size}B</span> : null}
          {kb?.confidence && <Badge variant="outline" className="text-[10px]">{kb.confidence}</Badge>}
          <span className="flex-1" />
          <CopyBtn text={addr} label="复制地址" />
          <Button size="sm" variant="outline" className="h-6 gap-1 px-2 text-[10px]"
                  title={hasDb ? `在 IDA 中打开并跳到 ${addr}` : "尚无 IDA 数据库，先完成 headless 分诊"}
                  disabled={!hasDb} onClick={openIda}>
            <ExternalLink className="size-3" />IDA 跳转
          </Button>
          <Button size="sm" variant="outline" className="h-6 gap-1 px-2 text-[10px]"
                  title="生成人工动态验证脚本档（x64dbg / Cheat Engine，纯模板不触网）"
                  onClick={() => setScriptOpen(true)}>
            <Bug className="size-3" />脚本档
          </Button>
          <AddToChainButton
            pid={pid} nodeType="func_kb" nodeId={kb?.id ?? ""}
            disabled={!kb}
            title={kb ? "把该函数加入攻击链" : "先在右侧「笔记」保存，建立 func_kb 行后才能入链"}
          />
        </div>
        {idaMsg && <p className="mt-1 font-mono text-[10px] text-muted-foreground">{idaMsg}</p>}
        {kb?.risk_tags?.length ? (
          <div className="mt-1 flex flex-wrap gap-1">
            {kb.risk_tags.map((t) => (
              <span key={t} className="rounded bg-(--status-error)/15 px-1.5 text-[10px] text-(--status-error)">{t}</span>
            ))}
          </div>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {/* 人机结论（func_kb） */}
        <section className="border-b px-3 py-2">
          <h3 className="mb-1 text-[11px] font-medium text-muted-foreground">结论与笔记（func_kb）</h3>
          {kb?.analysis
            ? <pre className="whitespace-pre-wrap break-words font-sans text-xs leading-relaxed">{kb.analysis}</pre>
            : <p className="text-[11px] text-muted-foreground">尚未分析——在右侧「笔记」里写下第一条结论。</p>}
          {kb?.name_history && kb.name_history.length > 1 && (
            <div className="mt-2 border-t pt-1">
              <p className="mb-0.5 text-[10px] text-muted-foreground">改名史</p>
              <div className="space-y-0.5">
                {kb.name_history.map((h, i) => (
                  <div key={i} className="font-mono text-[10px] text-muted-foreground">
                    {h.name} <span className="opacity-70" title={utcTitle(h.at)}>· {h.by} · {fmtDateTime(h.at)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* 伪码 / 反汇编（headless 客观缓存；缓存缺席且 MCP 在线时实时取/自动拉取落盘） */}
        <section className="px-3 py-2">
          <Tabs value={codeTab} onValueChange={setCodeTab} className="flex flex-col gap-0">
            <TabsList className="w-full justify-start rounded-none border-b bg-transparent p-0">
              <TabsTrigger value="pseudo" className="rounded-none border-b-2 px-3 py-1 text-[10px]">
                伪码
                {detail?.source === "mcp" && (
                  <span className="ml-1 rounded bg-primary/15 px-1 text-[9px] text-primary" title="经 MCP 实时读取，未落入 headless 缓存">MCP</span>
                )}
              </TabsTrigger>
              <TabsTrigger value="asm" className="rounded-none border-b-2 px-3 py-1 text-[10px]">
                反汇编{detail?.disasm?.lines?.length ? ` ${detail.disasm.lines.length}` : ""}
              </TabsTrigger>
              <span className="flex-1" />
              {codeTab === "pseudo" && detail?.pseudocode && <CopyBtn text={detail.pseudocode} label="复制伪码" />}
              {codeTab === "asm" && detail?.disasm && <CopyBtn text={detail.disasm.lines.join("\n")} label="复制反汇编" />}
            </TabsList>
            <TabsContent value="pseudo" className="min-h-0">
              {loading
                ? <p className="font-mono text-[10px] text-muted-foreground">
                    {mcpLive && !cached ? "正在经 MCP 读取…" : "加载中…"}
                  </p>
                : detail?.pseudocode
                  ? <pre className="overflow-x-auto rounded bg-muted/40 p-2 font-mono text-[10px] leading-relaxed">{detail.pseudocode}</pre>
                  : <p className="font-mono text-[10px] text-muted-foreground">
                      {detail
                        ? "该函数无伪码（可在「反汇编」页签或 IDA 中查看）"
                        : mcpLive
                          ? "MCP 未返回该函数：确认 IDA 当前打开的库包含此地址"
                          : "缓存缺席，请先完成分诊；或在 IDA 中按 Ctrl-Alt-M 启动 MCP 后实时读取"}
                    </p>}
            </TabsContent>
            <TabsContent value="asm" className="min-h-0">
              {loading
                ? <p className="font-mono text-[10px] text-muted-foreground">加载中…</p>
                : detail?.disasm?.lines?.length
                  ? <>
                      <pre className="overflow-x-auto rounded bg-muted/40 p-2 font-mono text-[10px] leading-relaxed">{detail.disasm.lines.join("\n")}</pre>
                      {detail.disasm.truncated && (
                        <p className="mt-1 font-mono text-[9px] text-muted-foreground">
                          反汇编超长已截断（前 {detail.disasm.lines.length} 行）
                        </p>
                      )}
                    </>
                  : <p className="font-mono text-[10px] text-muted-foreground">
                      {detail
                        ? "该函数无反汇编（未自动拉取或拉取失败——确认 IDA MCP 在线后重选函数）"
                        : mcpLive
                          ? "MCP 未返回该函数：确认 IDA 当前打开的库包含此地址"
                          : "缓存缺席，请先完成分诊；或在 IDA 中按 Ctrl-Alt-M 启动 MCP 后实时读取"}
                    </p>}
            </TabsContent>
          </Tabs>
        </section>
      </div>

      {/* 人工动态验证脚本档：x64dbg / Cheat Engine 双 tab */}
      <Dialog open={scriptOpen} onOpenChange={setScriptOpen}>
        <DialogContent className="max-w-2xl">
          <DialogTitle>动态验证脚本档 — {displayName}</DialogTitle>
          <DialogDescription>
            纯前端模板：人工在调试器里粘贴运行，平台不执行样本。命中后导出日志，到右栏「发现」用「传日志确认」回流验证凭据。
          </DialogDescription>
          <Tabs defaultValue="x64dbg">
            <TabsList>
              <TabsTrigger value="x64dbg">x64dbg</TabsTrigger>
              <TabsTrigger value="ce">Cheat Engine</TabsTrigger>
            </TabsList>
            {[
              { key: "x64dbg", text: x64 },
              { key: "ce", text: ce },
            ].map((t) => (
              <TabsContent key={t.key} value={t.key} className="relative">
                <pre className="max-h-[50vh] overflow-auto whitespace-pre rounded bg-muted/40 p-2 font-mono text-[11px]">{t.text}</pre>
                <div className="absolute right-2 top-2"><CopyBtn text={t.text} /></div>
              </TabsContent>
            ))}
          </Tabs>
        </DialogContent>
      </Dialog>
    </div>
  )
}
