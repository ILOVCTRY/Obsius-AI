import { readFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";

const RSS_SAMPLE_INTERVAL_MS = 10;
const MAXIMUM_PROFILE_BUFFER_BYTES = 2 * 1_024 * 1_024;
const MAXIMUM_PROFILE_MODULES = 10_000;

/**
 * Measure the first usable MCP catalog through the installed process boundary.
 */
export async function measureMcpStartup({
  command,
  args,
  environment,
  policy,
}) {
  const transport = new StdioClientTransport({
    command,
    args,
    env: environment,
    stderr: "pipe",
  });
  let stderr = "";
  transport.stderr?.on("data", (chunk) => {
    stderr = boundedAppend(stderr, chunk.toString());
  });
  const rss = createLinuxRssSampler(() => transport.pid);
  const client = new Client({
    name: "package-startup-probe",
    version: "1.0.0",
  });
  const startedAt = performance.now();
  rss.start();
  try {
    await client.connect(transport, { timeout: policy.initializeBudgetMs });
    const initializedAt = performance.now();
    const tools = await client.listTools(undefined, {
      timeout: policy.toolsListBudgetMs,
    });
    const listedAt = performance.now();
    rss.stop();
    const measurement = {
      process_started_to_initialized_ms: elapsed(startedAt, initializedAt),
      initialized_to_tools_list_ms: elapsed(initializedAt, listedAt),
      process_started_to_tools_list_ms: elapsed(startedAt, listedAt),
      listed_tool_count: tools.tools.length,
      rss: rss.result(),
    };
    assertStartupBudgets(measurement, policy);
    return measurement;
  } catch (cause) {
    throw new Error(`packaged MCP startup probe failed: ${stderr}`, { cause });
  } finally {
    rss.stop();
    await Promise.allSettled([client.close(), transport.close()]);
  }
}

/**
 * Profile ESM file loading in a separate diagnostic child.
 */
export async function profileMcpModuleLoading({
  command,
  args,
  environment,
  policy,
  packageName,
}) {
  const profile = createEsmModuleProfileCollector(packageName);
  const transport = new StdioClientTransport({
    command,
    args,
    env: { ...environment, NODE_DEBUG: "esm" },
    stderr: "pipe",
  });
  transport.stderr?.on("data", (chunk) => profile.append(chunk.toString()));
  const client = new Client({
    name: "package-module-profile",
    version: "1.0.0",
  });
  try {
    await client.connect(transport, {
      timeout: policy.doctorDeadlineMs,
    });
    await client.listTools(undefined, {
      timeout: policy.toolsListBudgetMs,
    });
    return profile.result();
  } finally {
    await Promise.allSettled([client.close(), transport.close()]);
  }
}

/**
 * Parse Linux procfs VmRSS text into bytes.
 */
export const parseLinuxVmRssBytes = (status) => {
  const match = /^VmRSS:\s+(\d+)\s+kB$/mu.exec(status);
  return match === null ? null : Number(match[1]) * 1_024;
};

/**
 * Collect unique file-backed ESM module URLs from Node's diagnostic stream.
 */
export const createEsmModuleProfileCollector = (packageName = "rea-agents") => {
  const modules = new Set();
  let buffer = "";
  let debugLines = 0;
  let truncated = false;
  const consume = (line) => {
    if (line.startsWith("ESM ")) debugLines += 1;
    for (const match of line.matchAll(/file:\/\/\/[^\s'",}\])]+/gu)) {
      if (modules.size >= MAXIMUM_PROFILE_MODULES) {
        truncated = true;
        break;
      }
      modules.add(match[0]);
    }
  };
  return {
    append(chunk) {
      buffer += chunk;
      if (Buffer.byteLength(buffer) > MAXIMUM_PROFILE_BUFFER_BYTES) {
        buffer = buffer.slice(-MAXIMUM_PROFILE_BUFFER_BYTES);
        truncated = true;
      }
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) consume(line);
    },
    result() {
      if (buffer.length > 0) consume(buffer);
      const values = [...modules];
      const packageSegment = `/node_modules/${packageName}/`;
      return {
        supported: true,
        method: "node-debug-esm",
        unique_file_modules: values.length,
        unique_package_modules: values.filter((value) =>
          value.includes(packageSegment),
        ).length,
        unique_dependency_modules: values.filter(
          (value) =>
            value.includes("/node_modules/") && !value.includes(packageSegment),
        ).length,
        debug_lines: debugLines,
        truncated,
      };
    },
  };
};

const createLinuxRssSampler = (pid) => {
  if (process.platform !== "linux")
    return {
      start() {},
      stop() {},
      result: () => ({
        supported: false,
        method: null,
        peak_bytes: null,
      }),
    };
  let peakBytes = null;
  let timer;
  const sample = () => {
    const currentPid = pid();
    if (currentPid === null) return;
    try {
      const current = parseLinuxVmRssBytes(
        readFileSync(`/proc/${String(currentPid)}/status`, "utf8"),
      );
      if (current !== null && (peakBytes === null || current > peakBytes))
        peakBytes = current;
    } catch {
      // The short-lived child may exit between resolving its PID and reading.
    }
  };
  return {
    start() {
      sample();
      timer = setInterval(sample, RSS_SAMPLE_INTERVAL_MS);
      timer.unref();
    },
    stop() {
      if (timer !== undefined) clearInterval(timer);
      sample();
    },
    result: () => ({
      supported: true,
      method: "linux-proc-status-vmrss-sampled",
      peak_bytes: peakBytes,
    }),
  };
};

const assertStartupBudgets = (measurement, policy) => {
  if (measurement.process_started_to_initialized_ms > policy.initializeBudgetMs)
    throw new Error("packaged MCP exceeded the initialize regression budget");
  if (measurement.initialized_to_tools_list_ms > policy.toolsListBudgetMs)
    throw new Error("packaged MCP exceeded the tools/list regression budget");
  if (
    measurement.process_started_to_tools_list_ms > policy.firstCatalogBudgetMs
  )
    throw new Error(
      "packaged MCP exceeded the first-catalog regression budget",
    );
};

const elapsed = (startedAt, completedAt) =>
  Math.round((completedAt - startedAt) * 100) / 100;

const boundedAppend = (current, chunk) =>
  `${current}${chunk}`.slice(-64 * 1_024);
