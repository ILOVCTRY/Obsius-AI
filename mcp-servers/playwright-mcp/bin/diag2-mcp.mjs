// 对照实验：直接 spawn @playwright/mcp cli.js（不经 playwright-claude 包装器），不带 cdp-endpoint，看 MCP 本体 stdio 是否应答
import { spawn } from "node:child_process";

const cwd = "e:/ILOVCTRY/SRC/src-strike";
const MCP = "e:/ILOVCTRY/SRC/src-strike/node_modules/@playwright/mcp/cli.js";

// 用 pipe（和 test-mcp 一致），关键对照：不走 stdio:inherit
const child = spawn(process.execPath, [MCP, "--caps=vision"], { cwd, stdio: ["pipe", "pipe", "pipe"] });

let buffer = "";
let recv = 0;
function ts() { return new Date().toISOString().slice(11, 23); }
function send(obj) {
  const data = JSON.stringify(obj);
  child.stdin.write(`Content-Length: ${Buffer.byteLength(data, "utf8")}\r\n\r\n${data}`);
}

child.stderr.on("data", (d) => process.stderr.write(`[MCP-stderr ${ts()}] ${d}`));
child.stdout.on("data", (d) => {
  process.stderr.write(`[MCP-stdout ${ts()} ${d.length}B]\n`);
  buffer += d.toString("utf8");
  let m;
  while ((m = parseFrame(buffer))) {
    recv++;
    buffer = buffer.slice(m.__skip); delete m.__skip;
    process.stderr.write(`[MSG id=${m.id}] ` + (m.result ? JSON.stringify(m.result).slice(0,200) : m.error ? JSON.stringify(m.error).slice(0,200) : "(notif)") + "\n");
  }
});
function parseFrame(buf) {
  const idx = buf.indexOf("\r\n\r\n"); if (idx < 0) return null;
  const m = buf.slice(0, idx).match(/Content-Length: (\d+)/); if (!m) return null;
  const len = +m[1]; const bs = idx + 4; if (buf.length < bs + len) return null;
  const body = buf.slice(bs, bs + len); try { const o = JSON.parse(body); o.__skip = bs + len; return o; } catch { return null; }
}

send({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "diag2", version: "0.0.1" } } });
setTimeout(() => send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} }), 1500);
setTimeout(() => send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }), 3000);

const t0 = Date.now();
const iv = setInterval(() => process.stderr.write(`[t=${((Date.now()-t0)/1000).toFixed(1)}s recv=${recv} alive=${child.exitCode===null} pid=${child.pid}]\n`), 5000);
setTimeout(() => { clearInterval(iv); child.kill("SIGKILL"); process.exit(0); }, 30000);
process.on("exit", () => { try { child.kill("SIGKILL"); } catch {} });