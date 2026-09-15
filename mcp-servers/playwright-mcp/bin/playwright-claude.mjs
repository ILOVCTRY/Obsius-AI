#!/usr/bin/env node
/**
 * Playwright MCP — Claude 专用启动器
 *
 * 项目只适配 Claude，本脚本是浏览器控制（Playwright MCP）的唯一入口。
 * 独占 CDP 端口（默认 9244，可用 CLAUDE_PW_PORT 覆盖），自带独立 Chrome profile 与锁。
 *
 * 注册：.mcp.json 的 mcpServers 里的 "playwright-claude" 指向本文件。
 * 改完需重启 Claude Code 会话（或重连 MCP）后，mcp__playwright-claude__* 工具才会出现。
 *
 * stdio 全托管给 @playwright/mcp；诊断只走 stderr。
 *
 * 2026-09-08 耐久化修复：
 *  1. 优先用本地固定安装 node_modules/@playwright/mcp/cli.js，不再每次 npx 联网拉 @latest。
 *  2. 配置端口被别的会话占用/持锁时，自动顺延找空闲端口，保证新会话 MCP 能正常加载。
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";

// 起始端口：可用 CLAUDE_PW_PORT 覆盖；被占时在 main 里自动顺延
let activePort = Number(process.env.CLAUDE_PW_PORT || 9244);
const PORT = activePort; // 保留原始意图日志
const BASE_DIR = path.join(
  process.env.LOCALAPPDATA || os.tmpdir(),
  "mcp-claude-browser"
);
const lockPathFor = (p) => path.join(BASE_DIR, `claude-${p}.lock`);
const userDataFor = (p) => path.join(BASE_DIR, `profile-${p}`);
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
async function effectivePort(start) {
  for (let p = start; p < start + 40; p++) {
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
    return;
  }

  const chrome = findChrome();
  if (!chrome) {
    // 找不到系统 Chrome → 让 @playwright/mcp 自己托管浏览器（降级）
    log("System Chrome not found; letting @playwright/mcp manage its own browser.");
    return "self-managed";
  }

  fs.mkdirSync(USER_DATA_DIR, { recursive: true });
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${USER_DATA_DIR}`,
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
      return;
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  // 启动失败 → 降级由 @playwright/mcp 自托管
  log("Chrome CDP did not come up; letting @playwright/mcp manage its own browser.");
  return "self-managed";
}

function runMcp(cdpEndpoint) {
  const endpointArgs = cdpEndpoint ? [`--cdp-endpoint=${cdpEndpoint}`] : [];
  const args = [...endpointArgs, ...EXTRA_MCP_ARGS];

  log(`Launching Playwright MCP → ${cdpEndpoint || "(self-managed browser)"}`);
  log(
    LOCAL_MCP_CLI
      ? `Local @playwright/mcp: ${LOCAL_MCP_CLI}`
      : "本地无固定安装 → 回退 npx -y @playwright/mcp@latest（需联网）"
  );
  log(`Dedicated port: ${activePort}`);

  const child = LOCAL_MCP_CLI
    ? // 本地固定版本：node cli.js …，无网络依赖
      spawn(process.execPath, [LOCAL_MCP_CLI, ...args], { stdio: "inherit", env: process.env })
    : process.platform === "win32"
      ? spawn("cmd.exe", ["/d", "/s", "/c", "npx", "-y", "@playwright/mcp@latest", ...args], {
          stdio: "inherit",
          windowsHide: Boolean(cdpEndpoint),
          env: process.env,
        })
      : spawn("npx", ["-y", "@playwright/mcp@latest", ...args], { stdio: "inherit", env: process.env });

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

  const port = await effectivePort(PORT);
  if (port !== PORT) {
    log(`端口 ${PORT} 被占用/持锁 → 自动顺延到空闲端口 ${port}`);
  }
  activePort = port;
  LOCK_FILE = lockPathFor(port);
  USER_DATA_DIR = userDataFor(port);

  if (!acquireLock()) {
    log(`Failed to acquire lock for port ${port}.`);
    process.exit(2);
  }

  try {
    const mode = await ensureChrome(port);
    runMcp(mode === "self-managed" ? null : `http://127.0.0.1:${port}`);
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