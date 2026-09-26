import { useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type {
  FofaConfig, FofaSearchRow, ImportPreview, ImportSummary,
} from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

// 网络空间测绘面板（cyberspace-mapping M1+M2，DESIGN.md §12 黑板「测绘」tab）：
// ① FOFA 中转配置（key 脱敏回显，空串=不修改） ② FOFA 查询工作区（结果勾选导入，
// 既有资产灰显） ③ 表格文件导入（xlsx/CSV 列映射 + 预览）。
// ⚠ 信任边界：FOFA 查询语句与 key 明文流经第三方中转——敏感项目慎用（面板常驻注记）。

const SIZES = [100, 500, 1000]

// 列语义（与 core/assetimport.COLUMN_KINDS 对齐；下拉 value 直接作 mapping 项）
const COLUMN_KINDS = ["ignore", "ip", "port", "host", "url", "title", "products"]
const COLUMN_LABEL: Record<string, string> = {
  ignore: "忽略", ip: "IP", port: "端口", host: "域名/主机",
  url: "URL", title: "标题", products: "指纹",
}

export function MappingPane({ pid }: { pid: string }) {
  const [sub, setSub] = useState<"search" | "import">("search")
  return (
    <div className="flex h-full flex-col">
      <ConfigBar />
      <div className="flex items-center gap-0.5 border-b px-3 py-1.5">
        {(["search", "import"] as const).map((s) => (
          <button
            key={s}
            type="button"
            className={cn("rounded px-2 py-0.5 text-[11px]",
              sub === s ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")}
            onClick={() => setSub(s)}
          >
            {s === "search" ? "FOFA 查询" : "表格导入"}
          </button>
        ))}
      </div>
      {sub === "search" ? <SearchWorkspace pid={pid} /> : <ImportWorkspace pid={pid} />}
    </div>
  )
}

// ---------- 配置条 ----------

function ConfigBar() {
  const [cfg, setCfg] = useState<FofaConfig | null>(null)
  const [open, setOpen] = useState(false)
  const [baseUrl, setBaseUrl] = useState("")
  const [key, setKey] = useState("")
  const [msg, setMsg] = useState("")
  const [testing, setTesting] = useState(false)

  useEffect(() => { api.fofaConfig().then(setCfg).catch(() => {}) }, [])

  const save = async () => {
    setMsg("")
    try {
      const next = await api.saveFofaConfig({
        base_url: baseUrl || undefined,
        ...(key.trim() ? { key: key.trim() } : {}),  // 空串=不修改（防回显误覆盖）
      })
      setCfg(next)
      setKey("")
      setMsg("已保存")
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const test = async () => {
    setTesting(true)
    setMsg("")
    try {
      const r = await api.fofaTest()
      setMsg(r.ok
        ? `连接成功 · 剩余配额 ${r.remain ?? "?"}${r.expire ? ` · 到期 ${r.expire}` : ""}`
        : `失败：${r.error ?? "未知错误"}`)
    } catch (e) {
      setMsg(`失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="border-b">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <button
          type="button"
          className={cn("rounded px-2 py-0.5 text-[11px]",
            cfg?.key_set ? "text-muted-foreground hover:bg-accent/40" : "text-(--status-approval) hover:bg-accent/40")}
          onClick={() => { setOpen((o) => !o); setMsg("") }}
        >
          ⚙ FOFA 设置{cfg?.key_set ? "" : "（未配置 key）"}
        </button>
        <span className="text-[10px] text-muted-foreground">
          ⚠ 查询语句明文经第三方中转——敏感项目慎用
        </span>
        {msg && <span className="ml-auto text-[11px] text-muted-foreground">{msg}</span>}
      </div>
      {open && (
        <div className="space-y-1.5 border-t px-3 py-2">
          <div className="flex gap-2">
            <Input className="h-7 flex-1 text-xs" value={baseUrl}
                   onChange={(e) => setBaseUrl(e.target.value)}
                   placeholder={`base url（默认 ${cfg?.base_url ?? "https://fofoapi.com"}）`} />
            <Input className="h-7 w-64 text-xs" type="password" value={key}
                   onChange={(e) => setKey(e.target.value)}
                   placeholder={cfg?.key_set ? `key 已设置（${cfg.key}），留空不修改` : "填入 FOFA key"} />
            <Button size="sm" variant="outline" className="h-7" onClick={test} disabled={testing}>
              {testing ? "测试中…" : "测试连接"}
            </Button>
            <Button size="sm" className="h-7" onClick={save}>保存</Button>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------- FOFA 查询工作区 ----------

function SearchWorkspace({ pid }: { pid: string }) {
  const [query, setQuery] = useState("")
  const [size, setSize] = useState(100)
  const [rows, setRows] = useState<FofaSearchRow[] | null>(null)
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState("")
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [summary, setSummary] = useState<string>("")
  const listRef = useRef<HTMLDivElement>(null)

  const search = async () => {
    if (!query.trim()) return
    setBusy(true)
    setErr("")
    setSummary("")
    try {
      const r = await api.fofaSearch(pid, query.trim(), size)
      setRows(r.rows)
      setTotal(r.total)
      setPicked(new Set())
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      setRows(null)
    } finally {
      setBusy(false)
    }
  }

  const allImportable = rows
    ? rows.map((_, i) => i).filter((i) => !rows[i].existing.domain && !rows[i].existing.service && !rows[i].existing.host)
    : []
  const allPicked = allImportable.length > 0 && allImportable.every((i) => picked.has(i))

  const toggleAll = () => {
    if (allPicked) setPicked(new Set())
    else setPicked(new Set(allImportable))
  }

  const importPicked = async () => {
    if (!rows || picked.size === 0) return
    setErr("")
    try {
      const payload = [...picked].map((i) => {
        const r = rows[i]
        return {
          ip: r.ip, port: r.port, protocol: r.protocol, host: r.host,
          domain: r.domain, title: r.title, products: r.products,
        }
      })
      const s: ImportSummary = await api.assetImport(pid, { source: "fofa", rows: payload })
      setSummary(`导入完成：${s.created} 新建 · ${s.merged} 合并` +
        (s.skipped ? ` · ${s.skipped} 跳过` : "") +
        (s.failed_count ? ` · ${s.failed_count} 失败` : ""))
      // 导入完把已导入的行置灰（existing 精确匹配由服务端做，这里直接按锚点本地置灰）
      setRows(rows.map((r, i) => picked.has(i)
        ? { ...r, existing: { domain: true, service: true, host: true } }
        : r))
      setPicked(new Set())
      requestAnimationFrame(() => listRef.current?.scrollTo({ top: 0 }))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1.5">
        <Input className="h-7 min-w-64 flex-1 font-mono text-xs" value={query}
               onChange={(e) => setQuery(e.target.value)}
               placeholder={'FOFA 语法，如 domain="example.com" && port="443"'}
               onKeyDown={(e) => e.key === "Enter" && search()} />
        <select className="h-7 rounded-md border bg-background px-2 text-xs"
                value={size} onChange={(e) => setSize(Number(e.target.value))}
                title="单次消耗等量查询配额">
          {SIZES.map((s) => <option key={s} value={s}>{s} 条</option>)}
        </select>
        <Button size="sm" className="h-7" onClick={search} disabled={busy || !query.trim()}>
          {busy ? "查询中…" : `查询（耗 ${size} 配额）`}
        </Button>
      </div>
      {err && <p className="px-3 py-1 text-[11px] text-(--status-error)">{err}</p>}
      {rows && (
        <>
          <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-[11px] text-muted-foreground">
            <span>命中 {total} · 本页 {rows.length}</span>
            <button type="button" className="rounded px-1.5 py-0.5 hover:bg-accent/40"
                    onClick={toggleAll} disabled={allImportable.length === 0}>
              {allPicked ? "取消全选" : "全选未入库"}
            </button>
            <Button size="sm" className="h-6 text-[11px]"
                    onClick={importPicked} disabled={picked.size === 0}>
              导入选中（{picked.size}）
            </Button>
            {summary && <span className="text-(--status-ok)">{summary}</span>}
          </div>
          <div ref={listRef} className="min-h-0 flex-1 overflow-auto">
            <table className="w-full border-collapse text-[11px]">
              <thead className="sticky top-0 z-10 bg-background">
                <tr className="border-b text-left text-muted-foreground">
                  <th className="w-8 px-2 py-1 font-normal" />
                  <th className="px-2 py-1 font-normal">IP</th>
                  <th className="w-14 px-2 py-1 font-normal">端口</th>
                  <th className="px-2 py-1 font-normal">域名/主机</th>
                  <th className="px-2 py-1 font-normal">标题</th>
                  <th className="px-2 py-1 font-normal">指纹</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const has = r.existing.domain || r.existing.service || r.existing.host
                  return (
                    <tr key={i} className={cn("border-b", has ? "opacity-40" : "hover:bg-accent/30")}>
                      <td className="px-2 py-1">
                        <input type="checkbox" className="size-3" disabled={has}
                               checked={picked.has(i)}
                               onChange={() => setPicked((p) => {
                                 const n = new Set(p)
                                 if (n.has(i)) n.delete(i)
                                 else n.add(i)
                                 return n
                               })} />
                      </td>
                      <td className="px-2 py-1 font-mono">{r.ip}</td>
                      <td className="px-2 py-1 font-mono">{r.port}</td>
                      <td className="px-2 py-1 font-mono">
                        {r.domain || r.host}
                        {has && <Badge variant="outline" className="ml-1 text-[9px]">已有</Badge>}
                      </td>
                      <td className="max-w-48 truncate px-2 py-1" title={r.title}>{r.title}</td>
                      <td className="px-2 py-1"><ProductChips products={r.products} /></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
      {!rows && !err && (
        <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
          输入 FOFA 语法查询后勾选导入
        </div>
      )}
    </div>
  )
}

export function ProductChips({ products }: { products: string[] }) {
  if (!products?.length) return null
  const shown = products.slice(0, 3)
  return (
    <span className="flex flex-wrap gap-1">
      {shown.map((p) => (
        <Badge key={p} variant="outline" className="text-[9px]">{p}</Badge>
      ))}
      {products.length > 3 && (
        <Badge variant="outline" className="text-[9px] text-muted-foreground">+{products.length - 3}</Badge>
      )}
    </span>
  )
}

// ---------- 表格导入工作区 ----------

function ImportWorkspace({ pid }: { pid: string }) {
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [mapping, setMapping] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState("")
  const [summary, setSummary] = useState<ImportSummary | null>(null)

  const pick = async (file: File | undefined) => {
    if (!file) return
    setErr("")
    setSummary(null)
    setPreview(null)
    try {
      const p = await api.assetImportPreview(pid, file)
      setPreview(p)
      setMapping(p.columns.map((c) => c.kind))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  const doImport = async () => {
    if (!preview) return
    setBusy(true)
    setErr("")
    try {
      const source = (preview.filename ?? "").toLowerCase().endsWith(".xlsx") ? "xlsx" : "csv"
      const s = await api.assetImport(pid, { source, rows: preview.rows, mapping })
      setSummary(s)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1.5">
        <input type="file" accept=".csv,.xlsx" className="text-xs"
               onChange={(e) => pick(e.target.files?.[0])} />
        <span className="text-[10px] text-muted-foreground">xlsx/CSV · ≤5000 行 · 列自动嗅探可手调</span>
        {preview && (
          <Button size="sm" className="ml-auto h-7" onClick={doImport} disabled={busy}>
            {busy ? "导入中…" : `确认导入 ${preview.total_rows} 行`}
          </Button>
        )}
      </div>
      {err && <p className="px-3 py-1 text-[11px] text-(--status-error)">{err}</p>}
      {summary && (
        <p className="px-3 py-1 text-[11px] text-(--status-ok)">
          导入完成：{summary.created} 新建 · {summary.merged} 合并
          {summary.skipped ? ` · ${summary.skipped} 跳过` : ""}
          {summary.failed_count ? ` · ${summary.failed_count} 失败（首条：${summary.failed[0]?.reason ?? ""}）` : ""}
        </p>
      )}
      {preview && (
        <ScrollArea className="min-h-0 flex-1">
          <div className="p-3">
            <p className="mb-2 text-[11px] text-muted-foreground">
              {preview.filename} · {preview.total_rows} 行
              {preview.truncated && <span className="text-(--status-approval)">（超过 5000 行已截断）</span>}
              {preview.header ? " · 已识别表头" : " · 无表头，按内容嗅探"}
            </p>
            <table className="w-full border-collapse text-[11px]">
              <thead className="sticky top-0 z-10 bg-background">
                <tr className="border-b">
                  {(preview.header ?? preview.columns.map((_, i) => `列 ${i + 1}`)).map((h, c) => (
                    <th key={c} className="px-2 py-1 text-left font-normal">
                      <div className="truncate text-muted-foreground" title={String(h)}>{String(h)}</div>
                      <select className="mt-0.5 w-full rounded border bg-background px-1 py-0.5 text-[10px]"
                              value={mapping[c] ?? "ignore"}
                              onChange={(e) => setMapping((m) => m.map((v, i) => (i === c ? e.target.value : v)))}>
                        {COLUMN_KINDS.map((k) => (
                          <option key={k} value={k}>
                            {COLUMN_LABEL[k]}{k !== "ignore" && preview.columns[c]?.kind === k ? "（建议）" : ""}
                          </option>
                        ))}
                      </select>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.rows.slice(0, 20).map((row, i) => (
                  <tr key={i} className="border-b">
                    {preview.columns.map((_, c) => (
                      <td key={c} className="max-w-40 truncate px-2 py-1 font-mono" title={row[c] ?? ""}>
                        {row[c] ?? ""}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {preview.rows.length > 20 && (
              <p className="mt-1 text-[10px] text-muted-foreground">预览前 20 行，确认后全量 {preview.total_rows} 行导入</p>
            )}
          </div>
        </ScrollArea>
      )}
      {!preview && !err && (
        <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
          选择 xlsx/CSV 文件后自动预览与嗅探列映射
        </div>
      )}
    </div>
  )
}
