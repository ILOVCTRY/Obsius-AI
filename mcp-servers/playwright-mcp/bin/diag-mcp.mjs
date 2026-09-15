// 诊断脚本：转发 MCP 全部 stdout/stderr（不按 [playwright-claude] 过滤），观察 MCP 初始化到底卡在哪
import { spawn } from "node:child_process";

const cwd = "e:/ILOVCTRY/SRC/src-strike";
const child = spawn(process.execPath, ["bin/playwright-claude.mjs"], { cwd, stdio: ["pipe", "pipe", "pipe"] });

let buffer = "";
let recvCount = 0;

function send(obj) {
  const data = JSON.stringify(obj);
  child.stdin.write(`Content-Length: ${Buffer.byteLength(data, "utf8")}\r\n\r\n${data}`);
}

function ts() { return new Date().toISOString().slice(11, 23); }

// 全部 stderr 原样转发 —— 含 MCP 自身的报错（之前被 test-harness 过滤吞掉）
child.stderr.on("data", (d) => process.stderr.write(`[stderr ${ts()}] ${d}`));

child.stdout.on("data", (d) => {
  process.stderr.write(`[stdout ${ts()} ${d.length}B] ` + (d.length < 600 ? d.toString() : d.toString().slice(0, 600) + "…") + "\n");
  buffer += d.toString("utf8");
  let msg;
  while ((msg = parseFrame(buffer))) {
    recvCount++;
    buffer = buffer.slice(msg.__skip);
    delete msg.__skip;
    process.stderr.write(`[MSG id=${msg.id} jsonrpc=${msg.jsonrpc}] ` + (msg.result || msg.error ? JSON.stringify(msg.result ?? msg.error).slice(0, 300) : "(notification)") + "\n");
  }
});

function parseFrame(buf) {
  const idx = buf.indexOf("\r\n\r\n");
  if (idx < 0) return null;
  const m = buf.slice(0, idx).match(/Content-Length: (\d+)/);
  if (!m) return null;
  const len = Number(m[1]);
  const bodyStart = idx + 4;
  if (buf.length < bodyStart + len) return null;
  const body = buf.slice(bodyStart, bodyStart + len);
  try { const msg = JSON.parse(body); msg.__skip = bodyStart + len; return msg; }
  catch { return null; }
}

send({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "diag", version: "0.0.1" } } });
setTimeout(() => send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} }), 1500);
setTimeout(() => send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }), 3000);

const t0 = Date.now();
const iv = setInterval(() => {
  process.stderr.write(`[t=${((Date.now()-t0)/1000).toFixed(1)}s recvMsgs=${recvCount} childAlive=${child.exitCode===null}` + (child.pid ? ` pid=${child.pid}` : "") + "]\n");
}, 5000);
setTimeout(() => { clearInterval(iv); child.kill("SIGKILL"); process.exit(0); }, 45000);

process.on("exit", () => { try { child.kill("SIGKILL"); } catch {} });