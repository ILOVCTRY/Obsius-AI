#!/usr/bin/env node
/**
 * Playwright MCP — 启动器（Claude Code + 蛙池平台双用）
 *
 * 浏览器控制（Playwright MCP）的唯一入口。独占 CDP 端口（默认 9244，可用
 * CLAUDE_PW_PORT 覆盖），自带独立 Chrome profile 与锁。
 *
 * 注册：
 *  - Claude Code：.mcp.json 的 mcpServers 里的 "playwright-claude" 指向本文件。
 *  - 蛙池平台：config/mcp.json 的 playwright 条目同样指向本文件（MCPBridge 按
 *    会话各起一个进程，用环境变量 PW_SESSION_ID 传入会话标识）。
 * 改完需重启会话（或重连 MCP）后工具才会出现。
 *
 * stdio 全托管给 @playwright/mcp；诊断只走 stderr。
 *
 * 2026-09-08 耐久化修复：
 *  1. 优先用本地固定安装 node_modules/@playwright/mcp/cli.js，不再每次 npx 联网拉 @latest。
 *  2. 配置端口被别的会话占用/持锁时，自动顺延找空闲端口，保证新会话 MCP 能正常加载。
 *
 * 2026-10-01 多会话隔离 + 空闲回收（修 "Browser is already in use ... use --isolated"）：
 *  3. **按会话派生独立 profile 与端口**：PW_SESSION_ID（蛙池平台按会话传入）存在时，
 *     BASE_DIR 下分隔 profile-<session>/锁 claude-<session>，起始端口也按会话哈希错开，
 *     彻底绕开 @playwright/mcp 默认的 workspace-hash 持久化 profile（同 workspace
 *     只能被一个实例持有 → 第二会话报 "already in use"）。
 *  4. **空闲自动关闭（2026-10-07 改「无操作」口径，默认 30 分钟）**：代理 MCP stdio
 *     给每次 tools/call 打点，连续 IDLE_MS 无操作 → 关闭 Chrome + 释放锁 + 删除该会话
 *     profile（PW_IDLE_MS 可覆盖）。**判据是「有没有操作」而非「有没有活动页」**——旧
 *     口径只要挂着一个非 about:blank 页面就永不回收。仅自管/复用 Chrome 路径启用；
 *     内嵌项目浏览器（PW_BROWSER_CDP_ENDPOINT）不起 Chrome，不走此看门狗。
 */

import { execSync, spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";

// 会话标识（蛙池平台 MCPBridge 按会话注入；Claude Code 场景缺省=单会话）
const SESSION_ID = (process.env.PW_SESSION_ID || "").trim();
// 会话级目录段：有会话用其哈希（防路径注入），否则用 "default"
const SESSION_KEY = SESSION_ID
  ? crypto.createHash("sha1").update(SESSION_ID).digest("hex").slice(0, 12)
  : "default";

// 起始端口：可用 CLAUDE_PW_PORT 覆盖；会话存在时按哈希错开起始点；被占时在 main 里自动顺延。
// 2026-10-01：默认基址从 9244 改为 14000 —— 本机 Windows 保留了 8421~9880 一大段
// （Hyper-V/WinNAT），9244 恰好落在其中，Chrome 无法绑定调试端口。14000 之上避开该段。
const _basePort = Number(process.env.CLAUDE_PW_PORT || 14000);
let activePort = SESSION_ID
  ? _basePort + (parseInt(SESSION_KEY.slice(0, 4), 16) % 400)
  : _basePort;
const PORT = activePort; // 保留原始意图日志
const BASE_DIR = path.join(
  process.env.LOCALAPPDATA || os.tmpdir(),
  "mcp-claude-browser"
);
const lockPathFor = (p) => path.join(BASE_DIR, `claude-${SESSION_KEY}-${p}.lock`);
const userDataFor = (p) => path.join(BASE_DIR, `profile-${SESSION_KEY}-${p}`);
// 跟随 activePort 变化，下面各函数直接用（原先是 const，现改为 let 以便顺延后指向新端口）
let LOCK_FILE = lockPathFor(activePort);
let USER_DATA_DIR = userDataFor(activePort);

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\Application\\chrome.exe"),
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

// @playwright/mcp 默认在 cwd 下生成 .playwright-mcp（console 日志/snapshot）。
// 强制输出到工作区 mcp-servers 下，避免污染工作区根。
// 2026-09-08：launcher 收敛到 mcp-servers/playwright-mcp/bin，ROOT 由 import.meta.dirname 动态解析到工作区根。
const ROOT = path.resolve(import.meta.dirname, "..", "..", "..");
const OUTPUT_DIR = path.resolve(ROOT, "mcp-servers", ".playwright-mcp");

// 本地固定安装的 @playwright/mcp 入口；找不到才回退 npx（联网）
const LOCAL_MCP_CLI = (() => {
  const c = path.resolve(import.meta.dirname, "..", "node_modules", "@playwright", "mcp", "cli.js");
  try {
    return fs.existsSync(c) ? c : null;
  } catch {
    return null;
  }
})();

// @playwright/mcp 的 --timeout-action 默认仅 5000ms，截图/等字体等耗时动作在这台上会稳定超时。
// 调大到 25000ms（导航超时独立默认 60000ms 已够）。改完需重启 Claude Code 会话加载新 MCP。
const EXTRA_MCP_ARGS = [
  "--browser=chrome",
  "--caps=vision,pdf",
  "--timeout-action=25000",
  `--output-dir=${OUTPUT_DIR}`,
];

function log(...args) {
  console.error("[playwright-claude]", `[${new Date().toISOString()}]`, ...args);
}

function isPidAlive(pid) {
  if (!pid || !Number.isFinite(pid)) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function hasLiveLock(port) {
  try {
    const data = JSON.parse(fs.readFileSync(lockPathFor(port), "utf8"));
    return isPidAlive(data.pid);
  } catch {
    return false;
  }
}

// 端口上已有 TCP 监听（无论谁占）就算忙
function portBusy(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => resolve(true));
    srv.once("listening", () => srv.close(() => resolve(false)));
    srv.listen(port, "127.0.0.1");
  });
}

// 从起始端口往上找第一个没被活锁占用、且 TCP 空闲的端口
// 2026-10-01：还要跳过 Windows 的 TCP 排除端口段（Hyper-V/WinNAT 保留段）——
// 这些端口 Node 能绑定，但 Chrome 无法绑定调试端口，会导致 CDP 永不就绪。
const EXCLUDED_RANGES = (() => {
  const out = [];
  if (process.platform !== "win32") return out;
  try {
    const r = execSync(
      "netsh interface ipv4 show excludedportrange protocol=tcp",
      { encoding: "utf8", windowsHide: true, timeout: 4000 });
    for (const line of r.split(/\r?\n/)) {
      const m = line.trim().match(/^(\d+)\s+(\d+)/);
      if (m) out.push([Number(m[1]), Number(m[2])]);
    }
  } catch {
    /* netsh 不可用：不排除，退化为端口顺延重试 */
  }
  return out;
})();

function portExcluded(p) {
  return EXCLUDED_RANGES.some(([a, b]) => p >= a && p <= b);
}
async function effectivePort(start) {
  for (let p = start; p < start + 200; p++) {
    if (portExcluded(p)) continue;   // 跳过 Windows 保留段（Chrome 绑不上调试端口）
    if (!hasLiveLock(p) && !(await portBusy(p))) return p;
  }
  return start; // 兜底：交给 acquireLock 上报失败
}

function readLock() {
  try {
    const data = JSON.parse(fs.readFileSync(LOCK_FILE, "utf8"));
    if (isPidAlive(data.pid)) return data;
    fs.unlinkSync(LOCK_FILE);
    return null;
  } catch {
    try {
      fs.unlinkSync(LOCK_FILE);
    } catch {
      /* ignore */
    }
    return null;
  }
}

function acquireLock() {
  if (readLock()) return false; // 已被前台 MCP 会话持有
  try {
    fs.mkdirSync(path.dirname(LOCK_FILE), { recursive: true });
    fs.writeFileSync(
      LOCK_FILE,
      JSON.stringify({ pid: process.pid, port: activePort, startedAt: new Date().toISOString() })
    );
    const again = JSON.parse(fs.readFileSync(LOCK_FILE, "utf8"));
    return again.pid === process.pid;
  } catch {
    return false;
  }
}

function releaseLock() {
  try {
    const d = JSON.parse(fs.readFileSync(LOCK_FILE, "utf8"));
    if (d.pid === process.pid) fs.unlinkSync(LOCK_FILE);
  } catch {
    /* ignore */
  }
}

function findChrome() {
  for (const p of CHROME_CANDIDATES) if (p && fs.existsSync(p)) return p;
  return null;
}

function cdpVersion(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port, path: "/json/version", timeout: 1500 },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch {
            resolve(null);
          }
        });
      }
    );
    req.on("error", () => resolve(null));
    req.on("timeout", () => {
      req.destroy();
      resolve(null);
    });
  });
}

async function ensureChrome(port) {
  const existing = await cdpVersion(port);
  if (existing) {
    log(`Chrome already up on ${port}:`, existing.Browser || "ok");
    return { mode: "existing", pid: null };
  }

  const chrome = findChrome();
  if (!chrome) {
    // 找不到系统 Chrome → 让 @playwright/mcp 自己托管浏览器（降级）
    log("System Chrome not found; letting @playwright/mcp manage its own browser.");
    return { mode: "self-managed", pid: null };
  }

  fs.mkdirSync(USER_DATA_DIR, { recursive: true });
  // 2026-10-01：--user-data-dir 必须放在 --remote-debugging-port **之前**。否则当机器
  // 上已有常规 Chrome 在跑时，新进程会因 Chrome 的进程单例机制移交后直接退出，
  // 调试端口永不监听（表现为「CDP did not come up」并静默降级到自托管浏览器，
  // 进而丢失隔离）。此顺序实测可稳定绑定调试端口。
  const args = [
    `--user-data-dir=${USER_DATA_DIR}`,
    `--remote-debugging-port=${port}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--no-sandbox",
    "about:blank",
  ];

  log(`Starting Chrome for ${port}:`, chrome);
  const child = spawn(chrome, args, { detached: true, stdio: "ignore", windowsHide: false });
  child.unref();

  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const v = await cdpVersion(port);
    if (v) {
      log(`Chrome ready on ${port}`);
      return { mode: "cdp", pid: child.pid };
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  // 启动失败 → 清掉刚才那个没绑上调试端口的 Chrome，再由调用方换端口重试
  log("Chrome CDP did not come up; cleaning up and retrying elsewhere.");
  try { process.kill(child.pid); } catch { /* ignore */ }
  return { mode: "self-managed", pid: null };
}

// ---------- 空闲自动关闭（2026-10-01；2026-10-07 改「无操作」口径） ----------

// 判据 = 距最后一次 MCP tools/call 的时长（由 runMcp 的 stdio 代理打点）。
const IDLE_MS = Number(process.env.PW_IDLE_MS || 30 * 60 * 1000); // 默认 30 分钟
const IDLE_POLL_MS = 60 * 1000; // 每分钟核一次

// MCP stdio 是行分隔 JSON-RPC；命中 tools/call 即视为一次「操作」。
const TOOLS_CALL_RE = /"method"\s*:\s*"tools\/call"/;

// 代理 MCP 的两个 stdio 方向（原样透传），顺带在 tools/call 上打点活动时间。
function proxyStdio(child, onActivity) {
  process.stdin.pipe(child.stdin);
  let buf = "";
  child.stdout.on("data", (chunk) => {
    process.stdout.write(chunk);
    buf += chunk.toString("utf8");
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx);
      buf = buf.slice(idx + 1);
      if (TOOLS_CALL_RE.test(line)) onActivity();
    }
  });
}

// 结束本启动器起的 Chrome 进程树（按已知 pid；Windows 用 taskkill /T 连带子进程）
function killChrome(pid) {
  if (!pid) return;
  try {
    if (process.platform === "win32") {
      spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore", windowsHide: true,
      });
    } else {
      try { process.kill(-pid, "SIGKILL"); } catch { process.kill(pid, "SIGKILL"); }
    }
  } catch {
    /* ignore */
  }
}

// 看门狗：连续 IDLE_MS 无 MCP 操作 → 关 Chrome + 删 profile + 释放锁 + 退出。
// 返回 { touch } 供 stdio 代理在每次 tools/call 时刷新活动时间。
function startIdleWatch(chromePid) {
  let lastActive = Date.now();
  const timer = setInterval(() => {
    if (Date.now() - lastActive < IDLE_MS) return;
    log(`空闲 ${Math.round(IDLE_MS / 60000)} 分钟无 MCP 操作 → 关闭浏览器并清理 profile`);
    clearInterval(timer);
    killChrome(chromePid);
    try {
      fs.rmSync(USER_DATA_DIR, { recursive: true, force: true });
      log(`已删除空闲 profile: ${USER_DATA_DIR}`);
    } catch (e) {
      log(`删除 profile 失败: ${e?.message || e}`);
    }
    releaseLock();
    process.exit(0);
  }, IDLE_POLL_MS);
  timer.unref?.();
  return { touch: () => { lastActive = Date.now(); } };
}

function runMcp(cdpEndpoint, onActivity) {
  const endpointArgs = cdpEndpoint ? [`--cdp-endpoint=${cdpEndpoint}`] : [];
  const args = [...endpointArgs, ...EXTRA_MCP_ARGS];
  log(`Launching Playwright MCP → ${cdpEndpoint || "(self-managed browser)"}`);
  log(
    LOCAL_MCP_CLI
      ? `Local @playwright/mcp: ${LOCAL_MCP_CLI}`
      : "本地无固定安装 → 回退 npx -y @playwright/mcp@latest（需联网）"
  );
  log(`Dedicated port: ${activePort}`);

  // 需要打点时空闲看门狗要求代理 stdio（改成管道自己转发）；否则原样 inherit 透传。
  const stdio = onActivity ? ["pipe", "pipe", "inherit"] : "inherit";
  const child = LOCAL_MCP_CLI
    ? // 本地固定版本：node cli.js …，无网络依赖
      spawn(process.execPath, [LOCAL_MCP_CLI, ...args], { stdio, env: process.env })
    : process.platform === "win32"
      ? spawn("cmd.exe", ["/d", "/s", "/c", "npx", "-y", "@playwright/mcp@latest", ...args], {
          stdio,
          windowsHide: Boolean(cdpEndpoint),
          env: process.env,
        })
      : spawn("npx", ["-y", "@playwright/mcp@latest", ...args], { stdio, env: process.env });
  if (onActivity) proxyStdio(child, onActivity);

  const cleanup = () => releaseLock();
  process.on("exit", cleanup);
  process.on("SIGINT", () => {
    cleanup();
    try {
      child.kill("SIGINT");
    } catch {
      /* ignore */
    }
    process.exit(130);
  });
  process.on("SIGTERM", () => {
    cleanup();
    try {
      child.kill("SIGTERM");
    } catch {
      /* ignore */
    }
    process.exit(143);
  });
  child.on("exit", (code, signal) => {
    cleanup();
    process.exit(signal ? 1 : (code ?? 0));
  });
  child.on("error", (err) => {
    cleanup();
    log("Failed to start Playwright MCP:", err.message);
    process.exit(1);
  });
}

async function main() {
  fs.mkdirSync(BASE_DIR, { recursive: true });
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  // 工具面探针模式（2026-10-01）：只列工具清单，不起 Chrome、不占锁、不看门狗。
  // 平台装配 LLM 工具面时用它拿 playwright 的 tools/list（PW_NO_CHROME=1）。
  // 此时不传 --cdp-endpoint，@playwright/mcp 自己也不主动起浏览器（懒启动）。
  if (process.env.PW_NO_CHROME === "1") {
    log("Probe mode (PW_NO_CHROME=1): listing tools without launching Chrome.");
    runMcp(null);
    return;
  }

  // 项目内嵌模式：Python BrowserPool 已经启动当前项目的持久化浏览器，
  // MCP 只连接它的 loopback CDP，不再另起 Chrome/profile。
  const embeddedCdp = (process.env.PW_BROWSER_CDP_ENDPOINT || "").trim();
  if (embeddedCdp) {
    log(`Embedded project browser: ${embeddedCdp}`);
    runMcp(embeddedCdp);
    return;
  }

  if (SESSION_ID) log(`Session ${SESSION_ID} → key=${SESSION_KEY}`);

  // 2026-10-01 端口顺延重试：Windows 上存在"TCP 可绑定但 Chrome 拒绝绑定"的保留
  // 端口段（Hyper-V/WinNAT 排除段）。若 Chrome 未能在所选端口拉起 CDP，不再静默
  // 降级到自托管浏览器（会丢隔离），而是在后续端口上重试若干次。
  let port = await effectivePort(PORT);
  if (port !== PORT) {
    log(`端口 ${PORT} 被占用/持锁 → 自动顺延到空闲端口 ${port}`);
  }

  let started = null;   // { port, mode, pid }
  const chromeAvailable = !!findChrome();
  for (let i = 0; i < 8; i++) {
    activePort = port;
    LOCK_FILE = lockPathFor(port);
    USER_DATA_DIR = userDataFor(port);
    if (!acquireLock()) {
      log(`端口 ${port} 已被其它会话持锁 → 顺延`);
      port = await effectivePort(port + 1);
      continue;
    }
    const r = await ensureChrome(port);
    if (r.mode === "self-managed" && chromeAvailable) {
      // 有 Chrome 却连不上 CDP：多为保留端口 → 释放锁后换端口重试
      log(`端口 ${port} 上 Chrome CDP 未就绪 → 换端口重试`);
      releaseLock();
      port = await effectivePort(port + 1);
      continue;
    }
    started = { port, ...r };
    break;
  }
  if (!started) {
    log("无法在可用端口上启动浏览器。");
    process.exit(1);
  }

  try {
    const { mode, pid, port: boundPort } = started;
    const usePort = boundPort || port;
    // 空闲看门狗（2026-10-01；2026-10-07 改「无操作」口径）：仅在确起了 CDP Chrome
    // 时启用（有 pid 可回收）；自托管浏览器无 pid，不起看门狗。
    if (mode !== "self-managed") {
      const watch = startIdleWatch(pid);
      runMcp(`http://127.0.0.1:${usePort}`, watch.touch);
    } else {
      runMcp(null);
    }
  } catch (err) {
    releaseLock();
    log(err?.message || err);
    process.exit(1);
  }
}

main().catch((err) => {
  log(err?.stack || err);
  process.exit(1);
});
