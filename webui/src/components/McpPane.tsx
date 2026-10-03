import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { McpServer } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// MCP 配置层（原 SettingsView 内部组件，webui-trae-shell M4 抽出为独立件）：
// 设置页「MCP」tab 与新壳工具姿态「插件·MCP」共用。
// 记录端点与启动方式（stdio=命令+参数，http=URL）；Agent 运行时会动态发现工具。

const selectCls = "rounded border bg-background px-1.5 py-0.5 text-xs [color-scheme:dark] [&>option]:bg-popover [&>option]:text-popover-foreground"

export function McpPane() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.mcpServers().then((r) => setServers(r.servers)).catch(() => {})
  }, [])

  const patch = (i: number, p: Partial<McpServer>) =>
    setServers((ss) => ss.map((s, j) => (j === i ? { ...s, ...p } : s)))

  const save = async () => {
    // http server 需 url；stdio 需 command
    const valid = servers.filter((s) => s.name.trim() &&
      (s.transport === "stdio" ? (s.command ?? "").trim() : s.url.trim()))
    await api.updateMcpServers(valid)
    setSaved(true)
    setTimeout(() => setSaved(false), 1500)
    setServers(valid)
  }

  return (
    <div className="flex h-full flex-col gap-2 overflow-auto p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm">config/mcp.json</span>
        <span className="flex-1" />
        {saved && <span className="text-[10px] text-(--status-ok)">已保存</span>}
        <Button size="sm" onClick={save}>保存</Button>
      </div>
      <div className="space-y-1.5">
        {servers.map((s, i) => (
          <div key={i} className="rounded border p-2">
            <div className="flex items-center gap-2">
              <Input value={s.name} onChange={(e) => patch(i, { name: e.target.value })}
                     className="w-36 font-mono text-xs" placeholder="名称" />
              <select value={s.transport} onChange={(e) => patch(i, { transport: e.target.value })}
                      className={cn(selectCls, "h-7")}>
                <option value="streamable-http">http</option>
                <option value="stdio">stdio</option>
              </select>
              {s.transport === "stdio" ? (
                <>
                  <Input value={s.command ?? ""} onChange={(e) => patch(i, { command: e.target.value })}
                         className="w-28 font-mono text-xs" placeholder="命令，如 uv" />
                  <Input value={(s.args ?? []).join(" ")} onChange={(e) => patch(i, { args: e.target.value.split(" ").filter(Boolean) })}
                         className="flex-1 font-mono text-xs" placeholder="参数（空格分隔）" />
                </>
              ) : (
                <Input value={s.url} onChange={(e) => patch(i, { url: e.target.value })}
                       className="flex-1 font-mono text-xs" placeholder="http://127.0.0.1:8081/mcp" />
              )}
              <Input value={s.domains.join(",")} onChange={(e) => patch(i, { domains: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })}
                     className="w-32 font-mono text-xs" placeholder="适用范围(空=全部)" />
              <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
                <input type="checkbox" checked={s.enabled} onChange={(e) => patch(i, { enabled: e.target.checked })} />
                启用
              </label>
              <Button size="sm" variant="ghost" className="text-[10px] text-(--status-error)"
                      onClick={() => setServers((ss) => ss.filter((_, j) => j !== i))}>删除</Button>
            </div>
          </div>
        ))}
      </div>
      <Button size="sm" variant="outline" className="w-fit"
              onClick={() => setServers((ss) => [...ss, { name: "", url: "", transport: "stdio", enabled: true, domains: [], command: "", args: [] }])}>
        + 添加 server
      </Button>
      <p className="rounded border border-cyan-400/20 bg-cyan-400/10 p-2 text-[10px] text-cyan-200">
        MCP 配置层（stdio 填命令+参数，http 填 URL）。保存后新会话会重新发现工具；Playwright 会复用当前项目的内置浏览器和登录态。
      </p>
    </div>
  )
}
