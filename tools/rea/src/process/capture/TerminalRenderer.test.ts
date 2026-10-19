import { createRequire } from "node:module";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it, vi } from "vitest";

import { TerminalRenderer } from "./TerminalRenderer.js";
import { ProcessCaptureResourceScope } from "./ProcessCaptureLifecycle.js";

const require = createRequire(import.meta.url);
const { Terminal } =
  require("@xterm/headless") as typeof import("@xterm/headless");
const { SerializeAddon } =
  require("@xterm/addon-serialize") as typeof import("@xterm/addon-serialize");

const renderFrames = async (
  data: string,
  normalize: (value: string) => string = (value) => value,
) => {
  const renderer = new TerminalRenderer({
    columns: 20,
    rows: 4,
    scrollback: 10,
    maxBytes: 100_000,
    normalize,
  });
  renderer.write(data, 0);
  const frames = await renderer.frames();
  const retention = renderer.retention();
  await renderer.dispose();
  return { frames, retention };
};

it("omits trailing blank cells from visible lines without losing rows", async () => {
  const { frames, retention } = await renderFrames(
    "left  right   \r\n\r\n  indented",
  );
  const [frame] = frames;
  expect(frame?.lines).toEqual(["left  right", "", "  indented", ""]);
  // Fixed-width rows remain recoverable from the recorded column count.
  expect(frame?.lines.map((line) => line.padEnd(frame.columns, " "))).toEqual([
    "left  right         ",
    " ".repeat(20),
    "  indented          ",
    " ".repeat(20),
  ]);
  expect(retention.retained_bytes).toBe(
    Buffer.byteLength(frame?.serialized_state ?? "") +
      Buffer.byteLength("left  right  indented"),
  );
});

it("applies rendered retention cumulatively across sparse frames", async () => {
  const renderer = new TerminalRenderer({
    columns: 10,
    rows: 1,
    scrollback: 0,
    maxBytes: 5,
    normalize: (value) => value,
  });
  try {
    renderer.write("a", 0);
    renderer.write("b", 1);
    const frames = await renderer.frames();

    expect(frames.map(({ lines }) => lines)).toEqual([["a"]]);
    expect(renderer.retention()).toEqual({
      budget_bytes: 5,
      observed_bytes: 6,
      retained_bytes: 2,
      observed_frames: 2,
      retained_frames: 1,
    });
    expect(renderer.truncated()).toBe(true);
  } finally {
    await renderer.dispose();
  }
});

it("trims only after normalization sees the full-width row", async () => {
  const { frames } = await renderFrames("pid 4242", (value) =>
    value.replace(/4242 +$/u, "<pid>"),
  );
  expect(frames[0]?.lines[0]).toBe("pid <pid>");
});

it.each(["write", "resize"] as const)(
  "releases terminal resources after a queued %s observation fails",
  async (operation) => {
    const failure = new Error("normalization failed");
    const terminalDispose = vi.spyOn(Terminal.prototype, "dispose");
    const addonDispose = vi.spyOn(SerializeAddon.prototype, "dispose");
    const renderer = new TerminalRenderer({
      columns: 20,
      rows: 4,
      scrollback: 10,
      maxBytes: 100_000,
      normalize: () => {
        throw failure;
      },
    });
    const scope = new ProcessCaptureResourceScope();
    const temporaryRoot = await mkdtemp(
      join(tmpdir(), "rea-terminal-cleanup-"),
    );
    try {
      if (operation === "write") renderer.write("output", 0);
      else renderer.resize(30, 4, 0);
      await expect(renderer.frames()).rejects.toBe(failure);
      const cleanup = await scope.release({
        timers: new Set(),
        terminal: undefined,
        renderer,
        runId: "failed-observation",
        temporaryRoot,
      });
      expect(cleanup.terminal_renderer).toEqual({
        state: "cleaned",
        reason: null,
      });
      expect(terminalDispose).toHaveBeenCalledTimes(1);
      expect(addonDispose).toHaveBeenCalledTimes(1);
      await expect(stat(temporaryRoot)).rejects.toMatchObject({
        code: "ENOENT",
      });
      await expect(scope.run(async () => "next capture")).resolves.toBe(
        "next capture",
      );
      await expect(renderer.frames()).rejects.toBe(failure);
    } finally {
      await scope.close();
      terminalDispose.mockRestore();
      addonDispose.mockRestore();
      await rm(temporaryRoot, { recursive: true, force: true });
    }
  },
);

it("retains disposal failures for a later cleanup retry", async () => {
  const failure = new Error("terminal disposal failed");
  const terminalDispose = vi.spyOn(Terminal.prototype, "dispose");
  terminalDispose.mockImplementationOnce(() => {
    throw failure;
  });
  const renderer = new TerminalRenderer({
    columns: 20,
    rows: 4,
    scrollback: 10,
    maxBytes: 100_000,
    normalize: (value) => value,
  });
  const scope = new ProcessCaptureResourceScope();
  const temporaryRoot = await mkdtemp(join(tmpdir(), "rea-terminal-retry-"));
  try {
    const cleanup = await scope.release({
      timers: new Set(),
      terminal: undefined,
      renderer,
      runId: "failed-disposal",
      temporaryRoot,
    });
    expect(cleanup.terminal_renderer).toEqual({
      state: "failed",
      reason: failure.message,
    });
    await expect(scope.run(async () => "next capture")).resolves.toBe(
      "next capture",
    );
    expect(terminalDispose).toHaveBeenCalledTimes(2);
  } finally {
    await scope.close();
    terminalDispose.mockRestore();
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
